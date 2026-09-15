#!/usr/bin/env python3
"""
TimTebowTournament – Wochen-Build für den Instagram-Post.

Holt Sleeper-Daten (Standings, Matchups, Lineups, Projektionen) und
nflverse-Play-by-Play der abgeschlossenen Woche, reduziert das PBP auf
Fantasy-Scoring-Events (Half-PPR, TTT-Regeln) und schreibt eine kleine
JSON nach instagram/data/<season>/weekNN.json, die das Post-Template lädt.

Aufruf:  python instagram/build_week.py            # letzte abgeschlossene Woche
         python instagram/build_week.py --week 3   # feste Woche
         python instagram/build_week.py --season 2026 --week 3 --no-pbp
"""
import argparse, csv, gzip, io, json, os, re, sys
from datetime import datetime, timezone, timedelta
import requests

LEAGUE_ID = os.environ.get("TTT_LEAGUE_ID", "1321866747418001408")
SCORING = "half"                # ppr | half | standard
TZ_OFFSET_H = 2                 # UTC -> deutsche Zeit (2 Sommer, 1 Winter)
NAMES = {                       # Sleeper-Displayname -> Name im Post
    "kesso": "Kessi", "nubischerprinz": "Simi", "thebiglebronski": "Tommy",
    "lancemourdock": "Marv", "lossausages": "Ritz", "shamh": "Benni", "jottage": "Erik",
}
NICK = {"ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers", "CHI": "Bears",
        "CIN": "Bengals", "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos", "DET": "Lions", "GB": "Packers",
        "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars", "KC": "Chiefs", "LV": "Raiders", "LAC": "Chargers",
        "LAR": "Rams", "MIA": "Dolphins", "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants",
        "NYJ": "Jets", "PHI": "Eagles", "PIT": "Steelers", "SF": "49ers", "SEA": "Seahawks", "TB": "Buccaneers",
        "TEN": "Titans", "WAS": "Commanders"}
REC_PTS = {"ppr": 1.0, "half": 0.5, "standard": 0.0}
DEF = {"sack": 1, "interception": 2, "fumbleRecovery": 2, "safety": 2, "td": 6,
       "pointsAllowed": [(0, 10), (6, 7), (13, 4), (20, 1), (27, 0), (34, -1), (999, -4)]}
DAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]   # datetime.weekday(): Mo=0
API = "https://api.sleeper.app/v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def j(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def pa_points(score):
    for mx, pts in DEF["pointsAllowed"]:
        if score <= mx:
            return pts
    return DEF["pointsAllowed"][-1][1]


# ── Sleeper ─────────────────────────────────────────────────────────
def load_sleeper(league_id, week):
    key = {"ppr": "pts_ppr", "standard": "pts_std"}.get(SCORING, "pts_half_ppr")
    league = j(f"{API}/league/{league_id}")
    users = j(f"{API}/league/{league_id}/users")
    rosters = j(f"{API}/league/{league_id}/rosters")
    season = league["season"]
    per_week = []
    for w in range(1, week + 1):
        try:
            per_week.append(j(f"{API}/league/{league_id}/matchups/{w}") or [])
        except Exception:
            per_week.append([])

    dct, proj = {}, {}
    try:
        pos = "&".join(f"position[]={p}" for p in ["QB", "RB", "WR", "TE", "K", "DEF"])
        arr = j(f"https://api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular&{pos}&order_by={key}")
        for e in arr:
            p = e.get("player") or {}
            pid = e["player_id"]
            name = NICK.get(pid, pid) if p.get("position") == "DEF" else f"{p.get('first_name','')} {p.get('last_name','')}".strip()
            dct[pid] = {"name": name, "team": e.get("team") or p.get("team") or "", "pos": p.get("position") or ""}
            proj[pid] = (e.get("stats") or {}).get(key) or 0
    except Exception as ex:
        print("Projektionen nicht verfügbar:", ex, file=sys.stderr)
    if not dct:
        allp = j(f"{API}/players/nfl")
        for pid, p in allp.items():
            name = NICK.get(pid, pid) if p.get("position") == "DEF" else f"{p.get('first_name','')} {p.get('last_name','')}".strip()
            dct[pid] = {"name": name, "team": p.get("team") or "", "pos": p.get("position") or ""}

    user_by_id = {u["user_id"]: u for u in users}

    def owner_of(r):
        u = user_by_id.get(r.get("owner_id"), {})
        dn = u.get("display_name") or u.get("username") or f"Team {r['roster_id']}"
        return NAMES.get(dn.lower()) or NAMES.get(str(u.get("username", "")).lower()) or dn

    roster_by_id = {r["roster_id"]: r for r in rosters}
    hist = {r["roster_id"]: [None] * week for r in rosters}
    for wi, ms in enumerate(per_week):
        by_m = {}
        for m in ms:
            by_m.setdefault(m["matchup_id"], []).append(m)
        for m in ms:
            opp = next((x for x in by_m.get(m["matchup_id"], []) if x["roster_id"] != m["roster_id"]), None)
            played = (m.get("points") or 0) > 0 or (opp and (opp.get("points") or 0) > 0)
            res = None
            if played and opp:
                a, b = m.get("points") or 0, opp.get("points") or 0
                res = "W" if a > b else ("L" if a < b else "T")
            if m["roster_id"] in hist:
                hist[m["roster_id"]][wi] = {"pts": m.get("points") or 0, "res": res}

    def streak_of(arr):
        done = [x for x in arr if x and x["res"]]
        if not done:
            return "–"
        last = done[-1]["res"]
        n = 0
        for x in reversed(done):
            if x["res"] != last:
                break
            n += 1
        return f"{last}{n}"

    managers = []
    for r in rosters:
        s = r.get("settings") or {}
        pf = (s.get("fpts") or 0) + (s.get("fpts_decimal") or 0) / 100
        w, l, t = s.get("wins") or 0, s.get("losses") or 0, s.get("ties") or 0
        managers.append({
            "owner": owner_of(r), "rosterId": r["roster_id"], "wins": w, "losses": l, "ties": t,
            "record": f"{w}-{l}" + (f"-{t}" if t else ""), "streak": streak_of(hist[r["roster_id"]]),
            "scores": [x["pts"] if x else 0 for x in hist[r["roster_id"]]], "total": round(pf, 2),
        })
    managers.sort(key=lambda m: (-m["wins"], -m["total"]))

    SLOT = {"SUPER_FLEX": "SFLX", "WR_RB_FLEX": "FLEX", "WR_TE_FLEX": "FLEX", "RB_WR_TE": "FLEX", "REC_FLEX": "FLEX", "IDP_FLEX": "IDP"}
    slots = [SLOT.get(p, "FLEX" if "FLEX" in p else p) for p in league.get("roster_positions") or [] if p not in ("BN", "IR", "TAXI")]

    def lineup(m):
        out = []
        for i, pid in enumerate(m.get("starters") or []):
            d = dct.get(pid) or {"name": pid, "team": "", "pos": slots[i] if i < len(slots) else ""}
            sp = m.get("starters_points")
            pts = sp[i] if sp and i < len(sp) else (m.get("players_points") or {}).get(pid) or 0
            slot = slots[i] if i < len(slots) else d["pos"]
            out.append({"pos": slot, "name": d["name"], "nfl": d["team"] or (pid if d["pos"] == "DEF" else ""),
                        "pts": f"{pts or 0:.2f}", "proj": proj.get(pid)})
        return out

    cur = per_week[week - 1] if week - 1 < len(per_week) else []
    by_m = {}
    for m in cur:
        by_m.setdefault(m["matchup_id"], []).append(m)
    matchups = []
    for mid in sorted(by_m, key=lambda x: int(x)):
        pair = by_m[mid]
        if len(pair) < 2:
            continue
        A, B = pair[0], pair[1]
        matchups.append({"a": owner_of(roster_by_id[A["roster_id"]]), "b": owner_of(roster_by_id[B["roster_id"]]),
                         "ptsA": A.get("points") or 0, "ptsB": B.get("points") or 0,
                         "lineupA": lineup(A), "lineupB": lineup(B)})
    return {"season": season, "week": week, "leagueName": league["name"], "managers": managers,
            "matchups": matchups, "hasProjections": bool(proj)}


# ── nflverse PBP -> Scoring-Events ──────────────────────────────────
def load_pbp(season, week):
    local = os.path.join(ROOT, "instagram", "pbp", str(season), f"week{week:02d}.csv.gz")
    if os.path.exists(local):
        print("PBP lokal:", local, file=sys.stderr)          # von fetch_pbp.py (täglich 08:00)
        text = gzip.open(local, "rt", encoding="utf-8").read()
    else:
        url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
        print("PBP laden:", url, file=sys.stderr)
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        text = gzip.decompress(r.content).decode("utf-8", errors="replace")
    rec = REC_PTS[SCORING]
    games, events = {}, []

    def num(row, k):
        try:
            return float(row.get(k) or 0)
        except ValueError:
            return 0.0

    def parse_start(date_str, time_str):
        d = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_str or "")
        if not d:
            return None
        t = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$", time_str or "")
        hh, mm = (int(t.group(1)), int(t.group(2))) if t else (18, 0)
        base = datetime(int(d.group(1)), int(d.group(2)), int(d.group(3)), hh, mm, tzinfo=timezone.utc)
        return int((base + timedelta(hours=TZ_OFFSET_H)).timestamp() * 1000)

    for row in csv.DictReader(io.StringIO(text)):
        if str(int(num(row, "week"))) != str(week):
            continue
        gid = row.get("game_id")
        if not gid:
            continue
        qtr = int(num(row, "qtr") or 1)
        elapsed = max(3600 - num(row, "game_seconds_remaining"), 0)
        if gid not in games:
            start = parse_start(row.get("game_date"), row.get("start_time"))
            g = {"id": gid, "start": start, "home": row.get("home_team"), "away": row.get("away_team"), "pa": {}, "end": 0}
            games[gid] = g
            if start is not None:
                for team in (g["home"], g["away"]):
                    g["pa"][team] = 0
                    events.append({"t": start, "qtr": 1, "clock": "15:00", "name": "DEF:" + team,
                                   "pts": pa_points(0), "note": "Points allowed 0", "game": gid})
        g = games[gid]
        if g["start"] is None:
            continue
        wall = int(g["start"] + elapsed * 3.15 * 1000)
        g["end"] = max(g["end"], wall)

        def add(name, pts, note):
            if not name or not pts:
                return
            events.append({"t": wall, "qtr": qtr, "clock": row.get("time") or "", "name": name,
                           "pts": round(pts, 2), "note": note, "game": gid})

        passer, rusher, receiver = row.get("passer_player_name"), row.get("rusher_player_name"), row.get("receiver_player_name")
        complete = num(row, "complete_pass") == 1
        if passer:
            add(passer, num(row, "passing_yards") * 0.04, "Pass-Yards")
            if num(row, "pass_touchdown") == 1: add(passer, 4, "Pass-TD")
            if num(row, "interception") == 1: add(passer, -2, "INT")
        if rusher:
            add(rusher, num(row, "rushing_yards") * 0.1, "Rush-Yards")
            if num(row, "rush_touchdown") == 1: add(rusher, 6, "Rush-TD")
        if receiver and complete:
            add(receiver, num(row, "receiving_yards") * 0.1 + rec, "Reception")
            if num(row, "pass_touchdown") == 1: add(receiver, 6, "Rec-TD")
        if row.get("two_point_conv_result") == "success":
            add(rusher or receiver or passer, 2, "2PT")
        fum = row.get("fumbled_1_player_name")
        if fum and num(row, "fumble_lost") == 1:
            add(fum, -2, "Fumble lost")
        kicker = row.get("kicker_player_name")
        if kicker:
            if row.get("field_goal_result") == "made":
                d = num(row, "kick_distance")
                add(kicker, 5 if d >= 50 else (4 if d >= 40 else 3), f"FG {int(d)}")
            if row.get("extra_point_result") == "good":
                add(kicker, 1, "XP")

        posteam, defteam = row.get("posteam"), row.get("defteam")
        if defteam:
            if num(row, "sack") == 1: add("DEF:" + defteam, DEF["sack"], "Sack")
            if num(row, "interception") == 1: add("DEF:" + defteam, DEF["interception"], "INT")
            if num(row, "safety") == 1: add("DEF:" + defteam, DEF["safety"], "Safety")
        recov = row.get("fumble_recovery_1_team")
        if recov and posteam and num(row, "fumble_lost") == 1 and recov != posteam:
            add("DEF:" + recov, DEF["fumbleRecovery"], "Fumble Recovery")
        td_team = row.get("td_team")
        if td_team and posteam and td_team != posteam and num(row, "touchdown") == 1:
            add("DEF:" + td_team, DEF["td"], "Def/ST-TD")

        scores = {g["home"]: num(row, "total_home_score"), g["away"]: num(row, "total_away_score")}
        for team in (g["home"], g["away"]):
            opp = g["away"] if team == g["home"] else g["home"]
            allowed = int(scores.get(opp) or 0)
            if allowed != g["pa"][team]:
                delta = pa_points(allowed) - pa_points(g["pa"][team])
                g["pa"][team] = allowed
                if delta:
                    add("DEF:" + team, delta, f"Points allowed {allowed}")

    events.sort(key=lambda e: e["t"])
    lst = sorted([g for g in games.values() if g["start"] is not None], key=lambda g: g["start"])
    windows = []
    for g in lst:
        end = g["end"] or g["start"] + int(3.2 * 3600000)
        if windows and g["start"] - windows[-1]["t0"] < 100 * 60000:
            windows[-1]["t1"] = max(windows[-1]["t1"], end)
            windows[-1]["games"].append(g["id"])
        else:
            windows.append({"t0": g["start"], "t1": end, "games": [g["id"]]})
    for w in windows:
        d = datetime.fromtimestamp(w["t0"] / 1000, tz=timezone.utc)
        w["label"] = DAY_DE[d.weekday()]
        w["sub"] = f"{d.hour:02d}:{d.minute:02d}"
        w["count"] = len(w["games"])
    return {"events": events, "windows": windows, "games": len(lst),
            "gameList": [{"id": g["id"], "start": g["start"], "end": g["end"] or g["start"] + int(3.2 * 3600000),
                          "home": g["home"], "away": g["away"]} for g in lst]}



# ── Liga-Historie (data/processed/seasons/*/matchups.json) ──────────
def load_history():
    """Alle Matchups seit 2015 als flache Liste: season, week, a, b, pa, pb, playoff."""
    base = os.path.join(ROOT, "data", "processed", "seasons")
    out = []
    if not os.path.isdir(base):
        return out
    for s in sorted(os.listdir(base)):
        p = os.path.join(base, s, "matchups.json")
        if not os.path.exists(p):
            continue
        try:
            for m in json.load(open(p, encoding="utf-8")):
                a, b = m.get("home_team"), m.get("away_team")
                if not a or not b or m.get("home_points") is None or m.get("away_points") is None:
                    continue
                out.append({"season": int(m.get("season") or s), "week": int(m.get("week") or 0), "a": a, "b": b,
                            "pa": float(m["home_points"]), "pb": float(m["away_points"]), "playoff": bool(m.get("is_playoff"))})
        except Exception as ex:
            print("Historie", s, "unlesbar:", ex, file=sys.stderr)
    return out


def h2h(history, a, b, exclude=None):
    """Head-to-Head a vs b: wins/losses/ties aus Sicht von a + letztes Duell."""
    wa = wb = t = 0
    last = None
    for m in history:
        if exclude and (m["season"], m["week"]) == exclude:
            continue
        if {m["a"], m["b"]} != {a, b}:
            continue
        pa, pb = (m["pa"], m["pb"]) if m["a"] == a else (m["pb"], m["pa"])
        if pa > pb: wa += 1
        elif pb > pa: wb += 1
        else: t += 1
        if last is None or (m["season"], m["week"]) > (last["season"], last["week"]):
            last = {"season": m["season"], "week": m["week"], "pa": pa, "pb": pb, "playoff": m["playoff"]}
    return {"wins": wa, "losses": wb, "ties": t, "games": wa + wb + t, "last": last}


def team_facts(history, name, pts):
    """Kontext zu einem Wochen-Score: Karriere-Rang, Karriere-Hoch, Schnitt."""
    scores = []
    for m in history:
        if m["a"] == name: scores.append(m["pa"])
        elif m["b"] == name: scores.append(m["pb"])
    if not scores:
        return {}
    hi = max(scores); lo = min(scores); avg = sum(scores) / len(scores)
    rank = 1 + sum(1 for s in scores if s > pts)
    return {"games": len(scores), "high": round(hi, 2), "low": round(lo, 2), "avg": round(avg, 2), "rank": rank}


def build_caption(season, week, sleeper, history, takes):
    """Automatische Instagram-Caption mit H2H und Liga-Historie."""
    mgr = sleeper["managers"]
    ms = sleeper["matchups"]
    lines = [f"WEEK {week} RECAP 🏈  #TimTebowTournament {season}", ""]
    facts = []
    for m in ms:
        a, b, pa, pb = m["a"], m["b"], m["ptsA"], m["ptsB"]
        win, lose, pw, pl = (a, b, pa, pb) if pa >= pb else (b, a, pb, pa)
        diff = abs(pa - pb)
        rec = h2h(history, win, lose)
        h = f"H2H jetzt {rec['wins'] + 1}-{rec['losses']}" + (f"-{rec['ties']}" if rec["ties"] else "") if rec["games"] else "erstes Duell überhaupt"
        tag = "Blowout" if diff >= 30 else ("Thriller" if diff < 5 else "")
        lines.append(f"▸ {win} {pw:.2f} : {pl:.2f} {lose}" + (f" · {tag}" if tag else "") + f" · {h}")
        tf = team_facts(history, win, pw)
        if tf and tf.get("rank", 99) <= 3:
            facts.append(f"{win}: {pw:.2f} ist die #{tf['rank']} Score seiner Karriere ({tf['games']} Spiele).")
        if rec["games"] and rec["last"] and rec["last"]["pa"] < rec["last"]["pb"]:
            facts.append(f"{win} revanchiert sich für die Pleite gegen {lose} in {rec['last']['season']} (Week {rec['last']['week']}).")
        lf = team_facts(history, lose, pl)
        if lf and lf.get("games") and pl <= lf["low"] + 0.01:
            facts.append(f"{lose}: {pl:.2f} – niedrigster Score seiner Karriere.")
    lines.append("")
    top = max(mgr, key=lambda p: (p["scores"][week - 1] if len(p["scores"]) >= week else 0))
    lines.append(f"🔥 Top Score: {top['owner']} {top['scores'][week - 1]:.2f}")
    lead = mgr[0]
    lines.append(f"👑 Leader: {lead['owner']} ({lead['record']})")
    if facts:
        lines += ["", "📚 Aus der Liga-Historie:"] + [f"• {f}" for f in facts[:3]]
    if takes.get("caption"):
        lines += ["", takes["caption"]]
    lines += ["", f"#FantasyFootball #NFL #Week{week} #Sleeper #TTT{season}"]
    return "\n".join(lines)


# ── Trash-Talk aus Markdown (instagram/takes/weekNN.md) ─────────────
def load_takes(season, week):
    path = os.path.join(ROOT, "instagram", "takes", str(season), f"week{week:02d}.md")
    if not os.path.exists(path):
        return {"takes": ["", "", "", ""], "cover": "", "coverSub": "", "standingsNote": "", "caption": ""}
    out = {"takes": [], "cover": "", "coverSub": "", "standingsNote": "", "caption": ""}
    section, buf = None, []
    def flush():
        txt = "\n".join(buf).strip()
        if section is None: return
        if section.startswith("matchup"): out["takes"].append(txt)
        elif section in out: out[section] = txt
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^##\s*(.+?)\s*$", line)
        if m:
            flush(); buf = []
            s = m.group(1).strip().lower()
            section = ("matchup" if s.startswith("matchup") else
                       "cover" if s == "cover" else "coverSub" if s in ("cover sub", "coversub", "sub") else
                       "standingsNote" if s.startswith("standings") else "caption" if s == "caption" else None)
        else:
            buf.append(line.rstrip("\n"))
    flush()
    while len(out["takes"]) < 4: out["takes"].append("")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    ap.add_argument("--no-pbp", action="store_true")
    a = ap.parse_args()

    state = j(f"{API}/state/nfl")
    season = a.season or int(state["season"])
    # Dienstag: state.week ist bereits die neue Woche -> abgeschlossene = week-1
    week = a.week or max(1, int(state.get("week") or 1) - (0 if state.get("season_type") != "regular" else 1))
    print(f"Season {season}, Week {week}", file=sys.stderr)

    sleeper = load_sleeper(LEAGUE_ID, week)
    pbp = None
    if not a.no_pbp:
        try:
            pbp = load_pbp(season, week)
            print(f"PBP: {pbp['games']} Spiele, {len(pbp['events'])} Events", file=sys.stderr)
        except Exception as ex:
            print("PBP fehlgeschlagen:", ex, file=sys.stderr)

    takes = load_takes(season, week)
    history = load_history()
    print(f"Historie: {len(history)} Matchups", file=sys.stderr)
    for m in sleeper["matchups"]:
        m["h2h"] = h2h(history, m["a"], m["b"])            # aus Sicht von a, vor dieser Woche
        m["factsA"] = team_facts(history, m["a"], m["ptsA"])
        m["factsB"] = team_facts(history, m["b"], m["ptsB"])
    out = {"season": season, "week": week, "scoring": SCORING, "tzOffset": TZ_OFFSET_H,
           "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "sleeper": sleeper, "pbp": pbp, **takes,
           "autoCaption": build_caption(season, week, sleeper, history, takes)}
    d = os.path.join(ROOT, "instagram", "data", str(season))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"week{week:02d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(d, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({"season": season, "week": week, "file": f"week{week:02d}.json"}, f)
    print("geschrieben:", path, os.path.getsize(path) // 1024, "KB", file=sys.stderr)


if __name__ == "__main__":
    main()
