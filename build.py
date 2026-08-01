#!/usr/bin/env python3
"""Builds index.html for the Acquisition Almanac from live public data.
Sources: MLB Stats API (drafts, transactions, rosters, people, standings)
and Baseball-Reference daily WAR files. Run: python3 build.py"""
import json, csv, collections, bisect, urllib.request, datetime, sys, time, gzip, io

csv.field_size_limit(10**9)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Safari/605.1.15"}
TODAY = datetime.date.today()
CUR = TODAY.year
OUT_END = CUR - 2          # mature-enough classes for outcome totals
MAT_END = CUR - 5          # classes with 5+ dev years (hit rates, surplus, busts)
REC_START, REC_END = CUR - 6, CUR - 2

def get(url, binary=False, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                b = r.read()
            return b if binary else b.decode("utf-8", "replace")
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(3 * (i + 1))

def jget(url): return json.loads(get(url))
S = lambda x: round(x, 1)

FRMAP = {
 "Arizona Diamondbacks":"ARI","Atlanta Braves":"ATL","Baltimore Orioles":"BAL","Boston Red Sox":"BOS",
 "Chicago Cubs":"CHC","Chicago White Sox":"CHW","Cincinnati Reds":"CIN","Cleveland Indians":"CLE",
 "Cleveland Guardians":"CLE","Colorado Rockies":"COL","Detroit Tigers":"DET","Houston Astros":"HOU",
 "Kansas City Royals":"KCR","Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA",
 "Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM","New York Yankees":"NYY",
 "Oakland Athletics":"OAK","Athletics":"OAK","Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT",
 "San Diego Padres":"SDP","San Francisco Giants":"SFG","Seattle Mariners":"SEA","St. Louis Cardinals":"STL",
 "Tampa Bay Rays":"TBR","Texas Rangers":"TEX","Toronto Blue Jays":"TOR","Washington Nationals":"WSN"}
TEAMS = sorted(set(FRMAP.values()))
ACQ={"TR","SFA","CLW"}; AMA={"SGN","DR"}; CUT={"REL","OUT","DES","NT"}

# ---------------- fetch ----------------
print("fetch: drafts 2000-%d" % CUR, flush=True)
draft_json = {y: jget(f"https://statsapi.mlb.com/api/v1/draft/{y}") for y in range(2000, CUR + 1)}

print("fetch: transactions", flush=True)
tx = []
rng = [("2014-11-01", "2014-12-31")]
for y in range(2015, CUR + 1):
    rng.append((f"{y}-01-01", f"{y}-06-30"))
    if y < CUR or TODAY.month > 6:
        rng.append((f"{y}-07-01", f"{y}-12-31" if y < CUR else TODAY.isoformat()))
for a, b in rng:
    tx += jget(f"https://statsapi.mlb.com/api/v1/transactions?startDate={a}&endDate={b}").get("transactions", [])
print("  tx records:", len(tx), flush=True)

print("fetch: WAR files", flush=True)
war_bat = get("https://www.baseball-reference.com/data/war_daily_bat.txt")
war_pitch = get("https://www.baseball-reference.com/data/war_daily_pitch.txt")

print("fetch: teams/rosters/standings", flush=True)
teams_api = jget("https://statsapi.mlb.com/api/v1/teams?sportId=1")["teams"]
rosters = {t["name"]: jget(f"https://statsapi.mlb.com/api/v1/teams/{t['id']}/roster?rosterType=active") for t in teams_api}
try:
    st = jget(f"https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season={CUR}")
    gp = [tr.get("gamesPlayed", 0) for rec in st.get("records", []) for tr in rec.get("teamRecords", [])]
    GAMES = max(1, round(sum(gp) / max(1, len(gp)))) if gp and sum(gp) else 109
except Exception:
    GAMES = 109
print("  avg games played:", GAMES, flush=True)

# ---------------- drafts ----------------
drafted = {}          # pid -> (year, team, pick, name)  (latest draft wins)
lr = {}               # (pid,y,team) -> (round_int, pick, pos_type)
classes = collections.defaultdict(dict)
def rint(r):
    try: return int(r)
    except: return 0
for y, d in draft_json.items():
    for rnd in d["drafts"]["rounds"]:
        for p in rnd.get("picks", []):
            if p.get("isPass"): continue
            per = p.get("person") or {}; pid = per.get("id")
            t = FRMAP.get((p.get("team") or {}).get("name", ""))
            if not pid or not t: continue
            if pid not in drafted or y > drafted[pid][0]:
                drafted[pid] = (y, t, p.get("pickNumber") or 9999, per.get("fullName", ""))
            if y >= 2015:
                lr[(pid, y, t)] = (rint(str(p.get("pickRound", ""))), p.get("pickNumber") or 0,
                                   (per.get("primaryPosition") or {}).get("type", ""))
n_picks_1525 = sum(1 for pid,(y,t,pk,nm) in drafted.items() if 2015 <= y <= CUR)

# ---------------- transactions ----------------
events = collections.defaultdict(list); cuts = collections.defaultdict(list)
names = {}; fromteam = {}
for t in tx:
    code = t.get("typeCode"); per = t.get("person") or {}
    pid = per.get("id"); date = t.get("date") or t.get("effectiveDate")
    if not pid or not date: continue
    if code in CUT: cuts[pid].append(date); continue
    if code not in ACQ and code not in AMA: continue
    team = FRMAP.get((t.get("toTeam") or {}).get("name", ""))
    if not team: continue
    minor = "minor league contract" in (t.get("description") or "").lower()
    kind = "AMA" if code in AMA else code
    events[pid].append((date, kind, team, minor))
    if code in ("TR", "CLW"):
        ft = FRMAP.get((t.get("fromTeam") or {}).get("name", ""))
        if ft: fromteam[(pid, date, team)] = ft
    names[pid] = per.get("fullName", "")
for pid in events: events[pid].sort()
for pid in cuts: cuts[pid].sort()
for pid, (y, t, pk, nm) in drafted.items(): names.setdefault(pid, nm)

# ---------------- WAR ----------------
rows = collections.defaultdict(float); rows_p = collections.defaultdict(float)
allrows = collections.defaultdict(float)     # (pid, y, fr) all years - player cards
seasons = collections.defaultdict(set); debut = {}
sal = {}; g26 = collections.Counter(); teampick = {}
for blob, pit in ((war_bat, False), (war_pitch, True)):
    for r in csv.DictReader(io.StringIO(blob)):
        mid, w, y = r.get("mlb_ID"), r.get("WAR"), r.get("year_ID")
        if not mid or w in (None, "", "NULL"): continue
        try: mid = int(mid); w = float(w); y = int(y)
        except: continue
        tid = r.get("team_ID", ""); fr = "OAK" if tid in ("OAK", "ATH") else tid
        seasons[mid].add((y, fr)); debut[mid] = min(debut.get(mid, 9999), y)
        names.setdefault(mid, r.get("name_common", ""))
        allrows[(mid, y, fr)] += w
        if fr not in set(FRMAP.values()): continue
        if y >= 2015:
            rows[(mid, fr, y)] += w
            if pit: rows_p[(mid, fr, y)] += w
        if y == CUR:
            s = r.get("salary")
            if s not in (None, "", "NULL"):
                try: sal[mid] = max(sal.get(mid, 0), int(float(s)))
                except: pass
            try: g = float(r.get("G") or 0)
            except: g = 0
            if g >= g26[mid]: g26[mid] = g; teampick[mid] = fr

cw = collections.Counter(); cw_p = collections.Counter()
w26 = collections.Counter(); w26club = collections.Counter(); p26 = collections.Counter()
for (m, f, y), w in rows.items():
    cw[m] += w
    if y == CUR: w26[m] += w; w26club[(m, f)] += w
for (m, f, y), w in rows_p.items():
    cw_p[m] += w
    if y == CUR: p26[m] += w
mlbset = set(cw.keys())

# ---------------- helpers ----------------
def prior_mlb(pid, d): return any(y < int(d[:4]) for y, _ in seasons.get(pid, ()))
def prior_cut(pid, d): return bisect.bisect_left(cuts.get(pid, []), d) > 0
def eff_kind(e, pid):
    date, kind, team, minor = e
    if kind == "SFA" and minor and not prior_mlb(pid, date) and not prior_cut(pid, date): return "AMA"
    return kind
def recent_cut(pid, d):
    ds = cuts.get(pid, []); i = bisect.bisect_left(ds, d)
    if i == 0: return False
    a = datetime.date(*map(int, ds[i-1].split("-"))); b = datetime.date(*map(int, d.split("-")))
    return (b - a).days <= 400
def established(pid, d, team):
    ey = int(d[:4]); ss = seasons.get(pid, set())
    return any(y < ey for y, _ in ss) or any(y == ey and f != team for y, f in ss)

# ---------------- attribution ----------------
per_event = collections.defaultdict(float); noevent = collections.defaultdict(float)
per26 = collections.defaultdict(float); noev26 = collections.defaultdict(float)
for (pid, fr, y), w in rows.items():
    evs = events.get(pid); cutoff = f"{y}-10-05"; cand = None
    if evs:
        for e in reversed(evs):
            if e[0] <= cutoff:
                if e[2] == fr: cand = e
                break
        if cand is None:
            for e in reversed(evs):
                if e[0] <= cutoff and e[2] == fr: cand = e; break
    if cand:
        per_event[(pid,) + cand] += w
        if y == CUR: per26[(pid,) + cand] += w
    else:
        noevent[(pid, fr)] += w
        if y == CUR: noev26[(pid, fr)] += w

# ---------------- buckets: era + current season ----------------
agg = collections.defaultdict(collections.Counter); a26 = collections.defaultdict(collections.Counter)
tops = collections.defaultdict(list)
gain = collections.Counter(); lost = collections.Counter(); lost_top = collections.defaultdict(list)
gain26 = collections.Counter(); lost26 = collections.Counter()
intl = collections.Counter(); intl_top = collections.defaultdict(list); i26 = collections.Counter()
detail = {t: dict(draft=[], intl=[], tr_pre=[], tr_mlb=[], fa=[], scrap=[], waiver=[], away=[]) for t in TEAMS}

for pid, (y, t, pk, nm) in drafted.items():
    if 2015 <= y <= OUT_END and cw.get(pid, 0.0) >= 2.0:
        detail[t]["draft"].append(dict(n=names.get(pid, nm) or nm, y=y, pk=pk,
                                       w=S(max(0.0, cw[pid])), s=S(w26.get(pid, 0.0))))

def bucket(pe, era):
    for (pid, date, kind, team, minor), w in pe.items():
        wp = max(0.0, w); k = eff_kind((date, kind, team, minor), pid)
        nm = names.get(pid, "") or str(pid); yr = date[:4]; sv = S(w26.get(pid, 0.0))
        if k == "AMA":
            if pid in drafted and drafted[pid][1] == team: continue
            (intl if era else i26)[team] += wp
            if era:
                intl_top[team].append((wp, nm))
                if wp >= 0.5: detail[team]["intl"].append(dict(n=nm, y=yr, w=S(wp), s=sv))
            continue
        A = agg if era else a26
        if k == "CLW":
            A[team]["waiver"] += wp
            if era:
                tops[(team, "W")].append((wp, nm))
                if wp >= 0.5: detail[team]["waiver"].append(dict(n=nm, y=yr, f=fromteam.get((pid, date, team), ""), w=S(wp), s=sv))
            continue
        if k == "TR":
            est = established(pid, date, team)
            A[team]["trade"] += wp; A[team]["trade_mlb" if est else "trade_milb"] += wp
            (gain if era else gain26)[team] += wp
            ft = fromteam.get((pid, date, team))
            if ft:
                (lost if era else lost26)[ft] += wp
                if era: lost_top[ft].append((wp, nm))
            if era:
                tops[(team, "T")].append((wp, nm, "est" if est else "pre"))
                if wp >= 0.5:
                    detail[team]["tr_mlb" if est else "tr_pre"].append(dict(n=nm, y=yr, f=ft or "", w=S(wp), s=sv))
                    if ft: detail[ft]["away"].append(dict(n=nm, y=yr, f=team, w=S(wp), s=sv))
            continue
        if k == "SFA":
            A[team]["fa"] += wp
            scrap = minor or recent_cut(pid, date)
            if scrap: A[team]["fa_scrap"] += wp
            if era:
                tops[(team, "F")].append((wp, nm))
                if wp >= 0.5:
                    (detail[team]["scrap"] if scrap else detail[team]["fa"]).append(dict(n=nm, y=yr, w=S(wp), s=sv))
bucket(per_event, True); bucket(per26, False)
for src, era in ((noevent, True), (noev26, False)):
    for (pid, fr), w in src.items():
        if pid not in drafted and debut.get(pid, 0) >= 2015 and seasons.get(pid) and fr == min(seasons[pid])[1]:
            wp = max(0.0, w)
            (intl if era else i26)[fr] += wp
            if era:
                intl_top[fr].append((wp, names.get(pid, "") or str(pid)))
                if wp >= 0.5: detail[fr]["intl"].append(dict(n=names.get(pid, "") or str(pid), y=str(debut[pid]), w=S(wp), s=S(w26.get(pid, 0.0))))
for t in TEAMS:
    for k in detail[t]: detail[t][k] = sorted(detail[t][k], key=lambda r: -r["w"])[:40]

# ---------------- draft table / recent / surplus / arms-bats / late / busts / classes / positions ----------------
first = collections.defaultdict(dict); firstR = collections.defaultdict(dict)
posagg = collections.defaultdict(collections.Counter)
def posgroup(pt):
    return {"Pitcher": "P", "Catcher": "C", "Infielder": "IF", "Outfielder": "OF"}.get(pt, "Other")
for y, d in draft_json.items():
    if y < 2015: continue
    for rnd in d["drafts"]["rounds"]:
        for p in rnd.get("picks", []):
            if p.get("isPass"): continue
            per = p.get("person") or {}; pid = per.get("id")
            t = FRMAP.get((p.get("team") or {}).get("name", "")); pk = p.get("pickNumber")
            if not pid or not t: continue
            fin = drafted.get(pid, (0,))[0] == y
            wv = S(max(0.0, cw.get(pid, 0.0))) if fin else 0.0
            classes[t].setdefault(str(y), []).append([str(p.get("pickRound", "")), pk or 0,
                per.get("fullName", ""), wv, 1 if (pid in mlbset and fin) else 0, 0 if fin else 1])
            if pk and y <= OUT_END and (y not in first[t] or pk < first[t][y]): first[t][y] = pk
            if pk and REC_START <= y <= REC_END and (y not in firstR[t] or pk < firstR[t][y]): firstR[t][y] = pk
            if fin and 2015 <= y <= OUT_END:
                posagg[t][posgroup((per.get("primaryPosition") or {}).get("type", ""))] += max(0.0, cw.get(pid, 0.0))
positions = {t: {k: S(v) for k, v in c.items()} for t, c in posagg.items()}

D = collections.defaultdict(lambda: collections.Counter())
top6 = collections.defaultdict(list)
late = collections.defaultdict(list); brows = []; bcnt = collections.Counter()
for pid, (y, t, pk, nm) in drafted.items():
    nmm = names.get(pid, nm) or nm
    w = cw.get(pid, 0.0); wp = max(0.0, w); v26 = w26.get(pid, 0.0)
    if 2015 <= y <= CUR:
        rd, pkn, _ = lr.get((pid, y, t), (0, pk, ""))
        if rd >= 5 and wp >= 0.5: late[t].append([nmm, y, rd, pkn, S(wp), S(v26)])
    if not (2015 <= y <= OUT_END): 
        if REC_START <= y <= REC_END: pass
    if 2015 <= y <= OUT_END:
        a = D[t]; a["picks"] += 1; a["war"] += wp; a["d26"] += v26
        codes = {"OAK", "ATH"} if t == "OAK" else {t}
        a["war_with"] += max(0.0, sum(v for (m, f, yy), v in rows.items() if m == pid and f == t))
        if pid in mlbset: a["mlb"] += 1
        if w >= 5: a["w5"] += 1
        if w >= 10: a["w10"] += 1
        if pk > 100: a["late"] += wp
        pv = max(0.0, cw_p.get(pid, 0.0)); a["pw"] += pv; a["bw"] += max(0.0, cw.get(pid, 0.0) - pv)
        a["p26"] += max(0.0, p26.get(pid, 0.0)); a["b26"] += max(0.0, v26 - p26.get(pid, 0.0))
        top6[t].append((wp, nmm, y, str(lr.get((pid, y, t), ("?",))[0]), pk, S(w)))
        if y <= MAT_END:
            a["picks_m"] += 1
            if pid in mlbset: a["mlb_m"] += 1
            if w >= 5: a["w5_m"] += 1
            if pk <= 15 and w < 2.0:
                brows.append(dict(year=y, pick=pk, name=nmm, team=t, war=S(w), w26=S(v26), mlb="Y" if pid in mlbset else "N"))
                bcnt[t] += 1
for t in late: late[t] = sorted(late[t], key=lambda r: -r[4])[:60]
brows.sort(key=lambda r: (r["year"], r["pick"]))

draftT = []
for t in TEAMS:
    a = D[t]
    draftT.append(dict(team=t, picks=a["picks"], mlb=a["mlb"], war=S(a["war"]), d26=S(a["d26"]),
        war_with=S(a["war_with"]), w5=a["w5"], w10=a["w10"],
        late_share=S(100 * a["late"] / a["war"]) if a["war"] else 0,
        mlb_rate_m=S(100 * a["mlb_m"] / a["picks_m"]) if a["picks_m"] else 0,
        kept=round(100 * a["war_with"] / a["war"]) if a["war"] else 0,
        avg_first=S(sum(first[t].values()) / len(first[t])) if first[t] else 0))
draftT.sort(key=lambda r: -r["war"])
top5 = {t: sorted(top6[t], reverse=True)[:3] for t in TEAMS}
for r in draftT:
    r["tops"] = "; ".join(f"{n} {w}" for _, n, y, rd, pk, w in top5[r["team"]])

recent = []
for t in TEAMS:
    a = collections.Counter()
    for pid, (y, tt, pk, nm) in drafted.items():
        if tt != t or not (REC_START <= y <= REC_END): continue
        w = cw.get(pid, 0.0); wp = max(0.0, w)
        a["war"] += wp; a["d26"] += w26.get(pid, 0.0)
        a["with"] += max(0.0, sum(v for (m, f, yy), v in rows.items() if m == pid and f == t))
        if pid in mlbset: a["mlb"] += 1
        if w >= 5: a["w5"] += 1
        if w >= 10: a["w10"] += 1
        if pk > 100: a["late"] += wp
        if y <= REC_END - 1:
            a["pr"] += 1
            if pid in mlbset: a["mr"] += 1
    recent.append(dict(team=t, war=S(a["war"]), d26=S(a["d26"]),
        kept=round(100 * a["with"] / a["war"]) if a["war"] else 0, mlb=a["mlb"], w5=a["w5"], w10=a["w10"],
        late_share=S(100 * a["late"] / a["war"]) if a["war"] else 0,
        mlb_rate_m=S(100 * a["mr"] / a["pr"]) if a["pr"] else 0,
        avg_first=S(sum(firstR[t].values()) / len(firstR[t])) if firstR[t] else 0))
recent.sort(key=lambda r: -r["war"])

BUCK = [(1,5),(6,10),(11,15),(16,20),(21,30),(31,50),(51,75),(76,100),(101,150),(151,250),(251,400),(401,3000)]
def bidx(p):
    for i, (a, b) in enumerate(BUCK):
        if a <= p <= b: return i
    return len(BUCK) - 1
p1521 = [(pid, t, pk) for pid, (y, t, pk, nm) in drafted.items() if 2015 <= y <= MAT_END]
bs = collections.Counter(); bc = collections.Counter()
for pid, t, pk in p1521: bs[bidx(pk)] += max(0.0, cw.get(pid, 0.0)); bc[bidx(pk)] += 1
exp = {i: bs[i] / bc[i] for i in bc}
sur = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0])
for pid, t, pk in p1521:
    a = sur[t]; a[0] += 1; a[1] += exp[bidx(pk)]; a[2] += max(0.0, cw.get(pid, 0.0)); a[3] += w26.get(pid, 0.0)
surplus = [dict(team=t, picks=v[0], expected=S(v[1]), actual=S(v[2]), s26=S(v[3]),
                surplus=S(v[2] - v[1]), per_pick=round((v[2] - v[1]) / v[0], 3)) for t, v in sur.items()]
surplus.sort(key=lambda r: -r["surplus"])

acq_out = []
for t in TEAMS:
    a = agg[t]; b = a26[t]
    acq_out.append(dict(team=t, trade=S(a["trade"]), trade26=S(b["trade"]), trade_milb=S(a["trade_milb"]),
        trade_mlb=S(a["trade_mlb"]), fa=S(a["fa"]), fa26=S(b["fa"]), fa_scrap=S(a["fa_scrap"]),
        scrap26=S(b["fa_scrap"]), waiver=S(a["waiver"]), waiver26=S(b["waiver"]),
        top_t="; ".join(f"{n} {S(w)}" for w, n, _ in sorted(tops[(t,'T')], reverse=True)[:3]),
        top_f="; ".join(f"{n} {S(w)}" for w, n in sorted(tops[(t,'F')], reverse=True)[:3]),
        top_milb="; ".join(f"{n} {S(w)}" for w, n, k in sorted(tops[(t,'T')], reverse=True) if k == "pre")))
for r in acq_out: r["top_milb"] = "; ".join(r["top_milb"].split("; ")[:3])
net = [dict(team=t, gained=S(gain[t]), surrendered=S(lost[t]), net=S(gain[t] - lost[t]),
            net26=S(gain26[t] - lost26[t]),
            worst="; ".join(f"{n} {S(w)}" for w, n in sorted(lost_top[t], reverse=True)[:3])) for t in TEAMS]
net.sort(key=lambda r: -r["net"])
intl_out = [dict(team=t, war=S(intl[t]), i26=S(i26[t]),
    tops="; ".join(f"{n} {S(w)}" for w, n in sorted(intl_top[t], reverse=True)[:3])) for t in TEAMS]
intl_out.sort(key=lambda r: -r["war"])
armsbats = [dict(team=t, pitch=S(D[t]["pw"]), bat=S(D[t]["bw"]), p26=S(D[t]["p26"]), b26=S(D[t]["b26"])) for t in TEAMS]
armsbats.sort(key=lambda r: -(r["pitch"] + r["bat"]))

# ---------------- deals ----------------
groups = collections.defaultdict(lambda: collections.defaultdict(list))
for t in tx:
    if t.get("typeCode") != "TR": continue
    per = t.get("person") or {}; pid = per.get("id"); date = t.get("date") or t.get("effectiveDate")
    to = FRMAP.get((t.get("toTeam") or {}).get("name", "")); fr = FRMAP.get((t.get("fromTeam") or {}).get("name", ""))
    if pid and date and to and fr and to != fr: groups[(date, tuple(sorted((fr, to))))][to].append(pid)
def recvw(pid, date, team):
    for minor in (False, True):
        v = per_event.get((pid, date, "TR", team, minor))
        if v is not None: return max(0.0, v)
    return 0.0
deals = []
for (date, pair), sides in groups.items():
    A, B = pair
    ra = [[names.get(p, "") or str(p), S(recvw(p, date, A)), S(max(0.0, w26club.get((p, A), 0.0)))] for p in sides.get(A, [])]
    rb = [[names.get(p, "") or str(p), S(recvw(p, date, B)), S(max(0.0, w26club.get((p, B), 0.0)))] for p in sides.get(B, [])]
    sa = sum(x[1] for x in ra); sb = sum(x[1] for x in rb)
    if max(sa, sb) < 0.5: continue
    ra.sort(key=lambda x: -x[1]); rb.sort(key=lambda x: -x[1])
    deals.append([date[:7], A, B, ra, rb, S(sa - sb)])
deals.sort(key=lambda d: -abs(d[5]))

# ---------------- roster map / refresh / originals ----------------
def original_org(pid):
    if pid in drafted: return drafted[pid][1]
    evs = events.get(pid) or []
    dy = debut.get(pid, 9999)
    if not evs:
        return min(seasons[pid])[1] if seasons.get(pid) else None
    e0 = evs[0]; ey = int(e0[0][:4])
    if dy < ey and dy != 9999 and seasons.get(pid): return min(seasons[pid])[1]
    date, kind, team, minor = e0
    if kind in ("TR", "CLW"): return fromteam.get((pid, date, team)) or team
    return team

def classify(pid, fr):
    evs = events.get(pid); today = TODAY.isoformat(); cand = None
    if evs:
        for e in reversed(evs):
            if e[0] <= today:
                if e[2] == fr: cand = e
                break
        if cand is None:
            for e in reversed(evs):
                if e[0] <= today and e[2] == fr: cand = e; break
    if cand:
        date, kind, team, minor = cand; k = eff_kind(cand, pid)
        if k == "TR": return ("Trade (pre-debut)" if not established(pid, date, fr) else "Trade (MLB)"), date[:4]
        if k == "CLW": return "Waiver claim", date[:4]
        if k == "SFA":
            if minor or recent_cut(pid, date): return "Reclamation FA", date[:4]
            return "Free agent", date[:4]
        if k == "AMA":
            if pid in drafted and drafted[pid][1] == fr: return "Drafted & developed", str(drafted[pid][0])
            return "Intl/amateur signing", date[:4]
    if pid in drafted and drafted[pid][1] == fr: return "Drafted & developed", str(drafted[pid][0])
    if pid in drafted: return "Other/pre-window", ""
    if debut.get(pid, 9999) >= 2015: return "Intl/amateur signing", ""
    return "Original club (pre-2015)", ""

warfr = collections.defaultdict(float)
for (m, f, y), w in rows.items(): warfr[(m, f)] += w
roster_rows = []; rc = collections.defaultdict(collections.Counter)
for tname, rj in rosters.items():
    fr = FRMAP.get(tname)
    if not fr: continue
    for e in rj.get("roster", []):
        per = e.get("person") or {}; pid = per.get("id")
        how, yr = classify(pid, fr)
        names.setdefault(pid, per.get("fullName", ""))
        roster_rows.append(dict(team=fr, player=per.get("fullName", ""), pos=(e.get("position") or {}).get("abbreviation", ""),
                                how=how, year=yr, war=S(warfr.get((pid, fr), 0.0)), w26=S(w26club.get((pid, fr), 0.0))))
        rc[fr][how] += 1
roster_rows.sort(key=lambda r: (r["team"], -r["war"]))
refresh = {}
for (m, f) in {(m, f) for (m, f, y) in rows if y >= CUR - 1}:
    how, yr = classify(m, f)
    refresh[f"{m}|{f}"] = [how, yr, S(warfr.get((m, f), 0.0)), S(w26club.get((m, f), 0.0))]
tid = {str(t["id"]): FRMAP.get(t["name"], "") for t in teams_api}

loyW = collections.defaultdict(lambda: [0.0, 0.0]); topn = collections.defaultdict(list)
for m in set(list(w26.keys()) + list(cw.keys())):
    o = original_org(m)
    if not o or o not in set(TEAMS): continue
    loyW[o][0] += w26.get(m, 0.0); loyW[o][1] += cw.get(m, 0.0)
    if w26.get(m, 0.0) >= 1.0: topn[o].append((S(w26[m]), names.get(m, "") or str(m)))
loyalty = []
for t in TEAMS:
    war26 = loyW[t][0]
    w162 = max(0, min(162, round(47.7 + war26 * 162 / GAMES)))
    loyalty.append(dict(team=t, war26=S(war26), rec=f"{w162}\u2013{162 - w162}", wins=w162, tot=S(loyW[t][1]),
                        tops="; ".join(f"{n} {w}" for w, n in sorted(topn[t], reverse=True)[:4])))
loyalty.sort(key=lambda r: -r["war26"])

# ---------------- market ----------------
pids26 = sorted(w26.keys())
pos = {}
for i in range(0, len(pids26), 100):
    for p in jget("https://statsapi.mlb.com/api/v1/people?personIds=" + ",".join(map(str, pids26[i:i+100]))).get("people", []):
        pos[p["id"]] = (p.get("primaryPosition") or {}).get("abbreviation", "")
MINSAL = 780000
market = sorted(([names.get(pid, str(pid)), pos.get(pid, ""), sal.get(pid, MINSAL), S(w26[pid]), teampick.get(pid, "")]
                 for pid in pids26 if pos.get(pid)), key=lambda r: -r[3])


# ---------------- prospect comps (Phase 1) ----------------
import gzip, statistics, math, os
cwALL = collections.Counter()
for (m, y, fr), w in allrows.items(): cwALL[m] += w

pcx = None
HFILE = next((f for f in ("milb_history.json.gz", "milb_history.json.gz.json", "milb history.json.gz") if os.path.exists(f)), None)
if HFILE:
    with gzip.open(HFILE, "rt", encoding="utf-8") as f:
        H = json.load(f)
    SPORTS = [11, 12, 13, 14]
    LVL = {11: "AAA", 12: "AA", 13: "A+", 14: "A"}
    def ip_outs(s):
        try:
            a = str(s).split("."); return int(a[0]) * 3 + (int(a[1]) if len(a) > 1 else 0)
        except: return 0
    def fetch_milb(year, min_pa, min_outs, min_bf):
        out = []
        for sp in SPORTS:
            for grp, g in (("hitting", 0), ("pitching", 1)):
                off = 0
                while True:
                    d = jget(f"https://statsapi.mlb.com/api/v1/stats?stats=season&group={grp}&season={year}&sportId={sp}&limit=1000&offset={off}&playerPool=all")
                    st = (d.get("stats") or [{}])[0]; sps = st.get("splits", [])
                    for s in sps:
                        x = s.get("stat", {}); pl = s.get("player", {})
                        pid = pl.get("id"); nm = pl.get("fullName", ""); age = x.get("age")
                        if not pid or age is None: continue
                        lg = (s.get("league") or {}).get("id", 0)
                        if g == 0:
                            pa = x.get("plateAppearances", 0) or 0
                            if pa < min_pa: continue
                            so = x.get("strikeOuts", 0) or 0; bb = x.get("baseOnBalls", 0) or 0
                            try:
                                avg = float(x.get("avg") or 0); slg = float(x.get("slg") or 0)
                                obp = float(x.get("obp") or 0); ops = float(x.get("ops") or 0)
                            except: continue
                            out.append(dict(pid=pid, nm=nm, y=year, sp=sp, g=0, lg=lg, age=age,
                                f=dict(k=so/pa, bb=bb/pa, iso=slg-avg, ops=ops, sb=(x.get("stolenBases",0) or 0)/pa),
                                disp=[pa, round(avg*1000), round(obp*1000), round(slg*1000), x.get("homeRuns",0) or 0, x.get("stolenBases",0) or 0]))
                        else:
                            bf = x.get("battersFaced", 0) or 0; outs = ip_outs(x.get("inningsPitched", "0"))
                            if outs < min_outs or bf < min_bf: continue
                            so = x.get("strikeOuts", 0) or 0; bb = x.get("baseOnBalls", 0) or 0
                            gp = x.get("gamesPitched", x.get("gamesPlayed", 1)) or 1; gs = x.get("gamesStarted", 0) or 0
                            try: era = float(x.get("era") or 0)
                            except: era = 0
                            out.append(dict(pid=pid, nm=nm, y=year, sp=sp, g=1, lg=lg, age=age,
                                f=dict(k=so/bf, bb=bb/bf, hr=(x.get("homeRuns",0) or 0)/bf, era=era, role=gs/gp),
                                disp=[round(outs/3*10), gs, so, round(era*100)]))
                    off += 1000
                    if off >= (st.get("totalSplits") or 0): break
        return out
    def zize(rows):
        coh = collections.defaultdict(list); coh2 = collections.defaultdict(list); ages = collections.defaultdict(list)
        for r in rows:
            coh[(r["y"], r["sp"], r["g"], r["lg"])].append(r); coh2[(r["y"], r["sp"], r["g"])].append(r)
            ages[(r["y"], r["sp"], r["g"])].append(r["age"])
        def stx(gr):
            ks = sorted(gr[0]["f"].keys()); o = {}
            for k in ks:
                v = [q["f"][k] for q in gr]; o[k] = (statistics.fmean(v), statistics.pstdev(v) or 1e-6)
            return o
        c1 = {k: (stx(v) if len(v) >= 25 else None) for k, v in coh.items()}
        c2 = {k: stx(v) for k, v in coh2.items()}
        am = {k: statistics.fmean(v) for k, v in ages.items()}
        for r in rows:
            st = c1.get((r["y"], r["sp"], r["g"], r["lg"])) or c2[(r["y"], r["sp"], r["g"])]
            r["z"] = [round((r["f"][k] - st[k][0]) / st[k][1] * 100) for k in sorted(r["f"].keys())]
            r["ad"] = round((r["age"] - am[(r["y"], r["sp"], r["g"])]) * 10)
    # append any newly-completed seasons to the cache
    if H["maxseason"] < CUR - 1:
        add = []
        for yy in range(H["maxseason"] + 1, CUR):
            add += fetch_milb(yy, 200, 180, 240)
        if add:
            zize(add)
            nix = {n: i for i, n in enumerate(H["names"])}
            for r in add:
                if r["nm"] not in nix: nix[r["nm"]] = len(H["names"]); H["names"].append(r["nm"])
                H["rows"].append([r["pid"], nix[r["nm"]], r["y"], r["sp"], r["g"], r["age"], r["ad"], r["z"], r["disp"]])
            H["maxseason"] = CUR - 1
            with gzip.open(HFILE, "wt", encoding="utf-8") as f: json.dump(H, f, separators=(",", ":"))
    # current season
    curp = fetch_milb(CUR, 80, 75, 100)
    zize(curp)
    # positions + orgs for current
    cpids = sorted({r["pid"] for r in curp})
    cpos = {}
    for i in range(0, len(cpids), 100):
        for p in jget("https://statsapi.mlb.com/api/v1/people?personIds=" + ",".join(map(str, cpids[i:i+100]))).get("people", []):
            cpos[p["id"]] = (p.get("primaryPosition") or {}).get("abbreviation", "")
    torg = {}
    for sp in SPORTS:
        for t in jget(f"https://statsapi.mlb.com/api/v1/teams?sportId={sp}&season={CUR}").get("teams", []):
            torg[t["id"]] = FRMAP.get(t.get("parentOrgName", ""), "")
    # re-fetch team ids for current rows (stats splits carry team) - fold into fetch: quick second pass via splits not stored; use league-free org from people? keep org via team in fetch:
    WH = {"bb": 1.0, "iso": 1.2, "k": 1.2, "ops": 1.0, "sb": 0.6}
    WP = {"bb": 1.1, "era": 0.5, "hr": 0.7, "k": 1.4, "role": 0.8}
    KH = sorted(WH.keys()); KP = sorted(WP.keys())
    hist = [r for r in H["rows"]]
    hby = collections.defaultdict(list)
    for i, r in enumerate(hist): hby[(r[4], r[3])].append(i)
    names2 = list(H["names"]); nix2 = {n: i for i, n in enumerate(names2)}
    def NI2(n):
        if n not in nix2: nix2[n] = len(names2); names2.append(n)
        return nix2[n]
    used_h = {}
    cur_rows = []
    for r in curp:
        ws = WH if r["g"] == 0 else WP; ks = KH if r["g"] == 0 else KP
        wv = [ws[k] for k in ks]
        cand = hby[(r["g"], r["sp"])]
        best = []
        za = r["z"]; ada = r["ad"]
        for i in cand:
            hrow = hist[i]
            if hrow[0] == r["pid"]: continue
            zb = hrow[7]
            d = 2.0 * (((ada - hrow[6]) / 10.0) / 1.2) ** 2
            for j in range(len(ks)):
                dd = (za[j] - zb[j]) / 100.0
                d += wv[j] * dd * dd
            best.append((d, i))
        best.sort()
        comps = []
        for d, i in best[:12]:
            if i not in used_h: used_h[i] = len(used_h)
            comps.append([used_h[i], max(1, round(100 - math.sqrt(d) * 22))])
        # upside from matured comps
        num = den = 0.0; nmat = 0
        for (d, i) in best[:12]:
            hrow = hist[i]
            if hrow[2] <= CUR - 5:
                w = 1.0 / (0.3 + d); num += w * max(0.0, cwALL.get(hrow[0], 0.0)); den += w; nmat += 1
        up = round(num / den * 10) if nmat >= 3 else -1
        flag = 1 if (r["disp"][0] < 160 if r["g"] == 0 else r["disp"][0] < 1350) else 0
        cur_rows.append([NI2(r["nm"]), "", r["sp"], r["g"], r["age"], r["ad"], cpos.get(r["pid"], ""), r["disp"], flag, up, comps, r["pid"]])
    # orgs: need team per current player - fetch rosters? use statsapi people currentTeam
    for i in range(0, len(cpids), 100):
        for p in jget("https://statsapi.mlb.com/api/v1/people?personIds=" + ",".join(map(str, cpids[i:i+100])) + "&hydrate=currentTeam").get("people", []):
            ct = (p.get("currentTeam") or {}).get("id")
            org = torg.get(ct, "")
            for cr in cur_rows:
                if cr[11] == p["id"]: cr[1] = org
    hist_out = [None] * len(used_h)
    for i, idx in used_h.items():
        hrow = hist[i]
        hist_out[idx] = [hrow[1], hrow[2], hrow[3], hrow[4], hrow[5], round(max(-9.9, cwALL.get(hrow[0], 0.0)) * 10),
                         1 if hrow[2] <= CUR - 5 else 0, hrow[8], 1 if hrow[0] in cwALL else 0, hrow[0]]
    stars = collections.defaultdict(list)
    for ci, cr in enumerate(cur_rows):
        for hi, sim in cr[10]:
            hp = hist_out[hi]
            if cwALL.get(hp[9], 0.0) >= 12:
                stars[hp[0]].append([ci, sim])
    stars = {names2[k] if isinstance(k, int) else k: sorted(v, key=lambda x: -x[1])[:10] for k, v in stars.items()}
    for cr in cur_rows: cr.pop()  # drop pid
    for h in hist_out: h.pop()    # drop pid
    pcx = dict(lv=LVL, names=names2, cur=cur_rows, hist=hist_out, stars=stars, mat=CUR - 5)
    print("pcx: cur", len(cur_rows), "| hist referenced", len(hist_out), "| stars", len(stars), flush=True)

# ---------------- grades ----------------
mm = lambda lst, k: {r["team"]: r[k] for r in lst}
surM = mm(surplus, "surplus"); netM = mm(net, "net"); intlM = mm(intl_out, "war")
scrapM = mm(acq_out, "fa_scrap"); wavM = mm(acq_out, "waiver")
comp = {t: surM[t] + netM[t] + intlM[t] + scrapM[t] + wavM[t] for t in TEAMS}
def pct(d, t):
    vals = sorted(d.values()); return sum(1 for x in vals if x <= d[t]) / len(vals)
grades = []
for t in sorted(TEAMS, key=lambda x: -comp[x]):
    g = max(20, min(80, int(round((20 + pct(comp, t) * 60) / 5) * 5)))
    grades.append(dict(team=t, grade=g, comp=S(comp[t]),
        bars=[round(pct(m, t), 2) for m in (surM, netM, intlM, scrapM, wavM)]))

# ---------------- player cards ----------------
pc_pids = {m for (m, f, y) in rows}
TLset = set(TEAMS)
persea = collections.defaultdict(lambda: collections.defaultdict(float))
for (m, y, fr), w in allrows.items():
    if m in pc_pids: persea[m][(y, fr)] += w; TLset.add(fr)
TL = sorted(TLset); TI = {t: i for i, t in enumerate(TL)}
pc = collections.defaultdict(list)
for m, se in persea.items():
    nm = names.get(m, "") or str(m)
    lines = sorted(([y - 2000, TI[fr], round(w * 10)] for (y, fr), w in se.items()), key=lambda r: (r[0], r[1]))
    dr = drafted.get(m)
    pc[nm].append([lines, [dr[0], TI[dr[1]], dr[2]] if dr else 0])

# ---------------- bundle + render ----------------
data = dict(asof=TODAY.strftime("%B %d, %Y"), games=GAMES, grades=grades, draft=draftT, recent=recent,
    acq=acq_out, net=net, surplus=surplus, intl=intl_out, armsbats=armsbats,
    busts=dict(rows=brows, counts=dict(bcnt)), roster=dict(rows=roster_rows, counts={t: dict(c) for t, c in rc.items()}),
    detail=detail, deals=deals, late={t: late.get(t, []) for t in TEAMS}, classes=classes,
    positions=positions, refresh=refresh, tid=tid, loyalty=loyalty, market=market, pc=pc, pcteams=TL, pcx=pcx)

tpl = open("template.html", encoding="utf-8").read()
html = tpl.replace('<script src="dash_data.js"></script>',
                   "<script>const DATA=" + json.dumps(data, separators=(",", ":")) + ";</script>")
# keep year labels honest as seasons roll over
n_tx = f"{len(tx):,}"
n_dr = f"{sum(1 for pid,(y,t,pk,nm) in drafted.items() if y>=2015):,}"
for a, b in [("8,515 draft selections", n_dr + " draft selections"),
             ("636,172 transactions", n_tx + " transactions"),
             ("2015\u20132026", f"2015\u2013{CUR}"), ("2015\u20132024", f"2015\u2013{OUT_END}"),
             ("'15\u2013'24", f"'15\u2013'{str(OUT_END)[2:]}"), ("2015\u20132021", f"2015\u2013{MAT_END}"),
             ("2015\u201321", f"2015\u2013{str(MAT_END)[2:]}"), ("'15\u2013'21", f"'15\u2013'{str(MAT_END)[2:]}"),
             ("2020\u20132024", f"{REC_START}\u2013{REC_END}"), ("'20\u2013'24", f"'{str(REC_START)[2:]}\u2013'{str(REC_END)[2:]}"),
             ("2020\u201323", f"{REC_START}\u2013{str(REC_END-1)[2:]}")]:
    html = html.replace(a, b)
open("index.html", "w", encoding="utf-8").write(html)
print("WROTE index.html:", len(html), "bytes | drafted 2015+:", n_dr, "| tx:", n_tx,
      "| pc players:", len(pc), "| games:", GAMES)
