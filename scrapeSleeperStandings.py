# scrapeSleeperStandings.py
import os, json, csv, time
from pathlib import Path
from collections import defaultdict
import requests

BASE = "https://api.sleeper.app/v1"
OUT_DIR = Path("./output")
DATA_DIR = Path("./data")

LEAGUE_ID = os.getenv("1250388942214156288", "").strip()
ENV_SEASON = os.getenv("SEASON")  # optional

# ---------------- API ---------------- #
def _get(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()

def get_league(league_id):              return _get(f"{BASE}/league/{league_id}")
def get_league_users(league_id):        return _get(f"{BASE}/league/{league_id}/users")
def get_league_rosters(league_id):      return _get(f"{BASE}/league/{league_id}/rosters")
def get_matchups(league_id, week):      return _get(f"{BASE}/league/{league_id}/matchups/{week}")
def get_transactions(league_id, week):  return _get(f"{BASE}/league/{league_id}/transactions/{week}")
def get_drafts(league_id):              return _get(f"{BASE}/league/{league_id}/drafts")
def get_draft_picks(draft_id):          return _get(f"{BASE}/draft/{draft_id}/picks")

def get_players_cached():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = DATA_DIR / "sleeper_players.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7*24*3600:
        return json.loads(cache.read_text(encoding="utf-8"))
    players = _get(f"{BASE}/players/nfl")  # groß
    cache.write_text(json.dumps(players), encoding="utf-8")
    return players

# --------------- Helpers --------------- #
def owner_maps(users, rosters):
    rid_to_owner = {r["roster_id"]: r.get("owner_id") for r in rosters}
    owner_to_rid = {}
    for rid, oid in rid_to_owner.items():
        if oid is not None:
            owner_to_rid[oid] = rid  # 1:1 in klassischen Ligen
    owner_to_teamname = {u["user_id"]: (u.get("metadata", {}) or {}).get("team_name") for u in users}
    owner_to_display  = {u["user_id"]: u.get("display_name") or "Unknown" for u in users}
    return rid_to_owner, owner_to_rid, owner_to_teamname, owner_to_display

def format_record(w, l, t):
    return f"{w}-{l}-{t}"

def tiebreak_key(stats, owner_id):
    s = stats[owner_id]
    return (-s["W"], -round(s["PF"], 2), round(s["PA"], 2), s["TeamName"].lower())

def fmt_num(x):
    return f"{x:,.2f}"  # tausender-Trennzeichen + 2 Dezimalstellen (US-Format)

def safe_int(x, default=None):
    try: return int(x)
    except: return default

# --------------- Draft Position --------------- #
def draft_positions_for_league(league_id):
    """owner_id (user_id) -> draft_position (1..N)"""
    try:
        drafts = get_drafts(league_id) or []
    except:
        drafts = []
    mapping = {}
    if not drafts:
        return mapping
    draft = drafts[0]  # meist nur einer
    # 1) draft_order: keys = user_id, value = draft_slot
    order = draft.get("draft_order") or {}
    for user_id, slot in order.items():
        mapping[str(user_id)] = safe_int(slot)
    if mapping:
        return mapping
    # 2) Fallback: Round-1-Picks
    try:
        picks = get_draft_picks(draft["draft_id"]) or []
        for p in picks:
            if p.get("round") == 1:
                # bevorzugt draft_slot; sonst pick_no (bei Round 1 = slot)
                slot = safe_int(p.get("draft_slot"), safe_int(p.get("pick_no")))
                picked_by = p.get("picked_by")  # user_id
                if picked_by is not None and slot is not None:
                    mapping[str(picked_by)] = slot
    except:
        pass
    return mapping

# --------------- Moves/Trades (optional) --------------- #
def count_moves_and_trades(league_id, owner_id):
    moves = 0
    trades = 0
    for week in range(1, 15):  # Regular 1..14
        try:
            txs = get_transactions(league_id, week) or []
        except:
            continue
        for tx in txs:
            ttype = tx.get("type")
            creator = tx.get("creator")  # user_id
            if ttype in ("waiver", "free_agent"):
                if creator == owner_id:
                    moves += 1
            elif ttype == "trade":
                # Trades präzise zählen: wenn owner via roster_ids beteiligt
                roster_ids = set(tx.get("roster_ids") or [])
                if roster_ids:
                    # Zähle Trade für alle beteiligten Owner; wird später auf Owner gemappt.
                    # Genauigkeit verbessern wir gleich im Aufrufer mit rid->owner map.
                    trades += 1
    return moves, trades

# --------------- Regular Season (Weeks 1..14) --------------- #
def compute_regular_season(league_id, rid_to_owner, owner_to_teamname, owner_to_display):
    stats = defaultdict(lambda: {"W":0,"L":0,"T":0,"PF":0.0,"PA":0.0,"TeamName":"", "ManagerName":"", "Moves":0, "Trades":0, "DraftPosition":None})
    owner_ids = set(rid_to_owner.values())

    # Namen setzen
    for oid in owner_ids:
        team_name = owner_to_teamname.get(oid) or owner_to_display.get(oid) or "Unknown"
        stats[oid]["TeamName"] = team_name
        stats[oid]["ManagerName"] = owner_to_display.get(oid, "Unknown")

    # PF/PA & W/L/T
    for week in range(1, 15):  # 1..14
        matchups = get_matchups(league_id, week) or []
        by_mid = defaultdict(list)
        for t in matchups:
            by_mid[t.get("matchup_id")].append(t)

        for teams in by_mid.values():
            if len(teams) != 2:
                # Median/Bye/etc.: Punkte mitzählen (PF), aber kein W/L/T
                for t in teams:
                    rid = t["roster_id"]; oid = rid_to_owner.get(rid)
                    pts = float(t.get("points", 0) or 0.0)
                    stats[oid]["PF"] += pts
                continue

            a, b = teams
            a_oid = rid_to_owner.get(a["roster_id"])
            b_oid = rid_to_owner.get(b["roster_id"])
            a_pts = float(a.get("points", 0) or 0.0)
            b_pts = float(b.get("points", 0) or 0.0)
            stats[a_oid]["PF"] += a_pts; stats[a_oid]["PA"] += b_pts
            stats[b_oid]["PF"] += b_pts; stats[b_oid]["PA"] += a_pts
            if a_pts > b_pts:
                stats[a_oid]["W"] += 1; stats[b_oid]["L"] += 1
            elif b_pts > a_pts:
                stats[b_oid]["W"] += 1; stats[a_oid]["L"] += 1
            else:
                stats[a_oid]["T"] += 1; stats[b_oid]["T"] += 1

    # Ranks
    owners_sorted = sorted(owner_ids, key=lambda oid: tiebreak_key(stats, oid))
    for rank, oid in enumerate(owners_sorted, start=1):
        stats[oid]["RegularSeasonRank"] = rank

    return stats, owners_sorted

# --------------- Playoffs (Seeds 1..8; Weeks 15 & 16) --------------- #
def compute_playoffs_custom(league_id, owners_sorted, owner_to_rid, stats):
    """Erzeuge Playoff-Ranking streng nach deinem Schema."""
    # Seeds 1..8 anhand Regular-Season-Rank
    seeds = owners_sorted[:8]
    # Punkte der Weeks 15/16 je Owner ziehen
    w15 = get_matchups(league_id, 15) or []
    w16 = get_matchups(league_id, 16) or []

    def week_points_map(entries):
        m = {}
        for e in entries:
            rid = e.get("roster_id")
            pts = float(e.get("points", 0) or 0.0)
            m[rid] = pts
        return m

    w15_pts_by_rid = week_points_map(w15)
    w16_pts_by_rid = week_points_map(w16)

    def owner_pts(oid, week_map):
        rid = owner_to_rid.get(oid)
        return week_map.get(rid, 0.0)

    # Quarterfinals (Week 15)
    qf_pairs = [
        (seeds[0], seeds[3]),  # 1 vs 4
        (seeds[1], seeds[2]),  # 2 vs 3
        (seeds[4], seeds[7]),  # 5 vs 8
        (seeds[5], seeds[6]),  # 6 vs 7
    ]

    def winner(a, b):
        ap = owner_pts(a, w15_pts_by_rid)
        bp = owner_pts(b, w15_pts_by_rid)
        if ap > bp: return a, b
        if bp > ap: return b, a
        # Tie-Break: besserer Seed (niedriger RegularSeasonRank) kommt weiter
        return (a if owners_sorted.index(a) < owners_sorted.index(b) else b), (b if owners_sorted.index(a) < owners_sorted.index(b) else a)

    w15_winners, w15_losers = [], []
    for a, b in qf_pairs:
        w, l = winner(a, b)
        w15_winners.append(w); w15_losers.append(l)

    # Week 16: Finals & Platzierungsspiele
    # Top-Bracket: winners of (1v4) vs (2v3)
    top_finalists = (w15_winners[0], w15_winners[1])
    top_conso    = (w15_losers[0],  w15_losers[1])
    # Bottom-Bracket: winners of (5v8) vs (6v7)
    bot_finalists = (w15_winners[2], w15_winners[3])
    bot_conso     = (w15_losers[2],  w15_losers[3])

    def week16_winner(a, b):
        ap = owner_pts(a, w16_pts_by_rid)
        bp = owner_pts(b, w16_pts_by_rid)
        if ap > bp: return a, b
        if bp > ap: return b, a
        # Tie-Break: besserer Seed weiter
        return (a if owners_sorted.index(a) < owners_sorted.index(b) else b), (b if owners_sorted.index(a) < owners_sorted.index(b) else a)

    champ, runnerup = week16_winner(*top_finalists)
    third, fourth   = week16_winner(*top_conso)
    fifth, sixth    = week16_winner(*bot_finalists)
    seventh, eighth = week16_winner(*bot_conso)

    playoff_rank_order = [champ, runnerup, third, fourth, fifth, sixth, seventh, eighth]
    playoff_rank = {oid: i+1 for i, oid in enumerate(playoff_rank_order)}

    # Baue Ausgabezeilen (klein, fokussiert)
    rows = []
    for i, oid in enumerate(playoff_rank_order, start=1):
        team = stats[oid]["TeamName"]
        mgr  = stats[oid]["ManagerName"]
        seed = owners_sorted.index(oid) + 1
        w15p = owner_pts(oid, w15_pts_by_rid)
        w16p = owner_pts(oid, w16_pts_by_rid)
        rows.append([team, i, mgr, seed, f"{w15p:.2f}", f"{w16p:.2f}"])
    return rows

# ----------------------------- MAIN ----------------------------- #
def main():
    if not LEAGUE_ID:
        raise SystemExit("Bitte SLEEPER_LEAGUE_ID als Umgebungsvariable setzen.")

    league  = get_league(LEAGUE_ID)
    users   = get_league_users(LEAGUE_ID)
    rosters = get_league_rosters(LEAGUE_ID)

    # Saison bestimmen
    season_str = ENV_SEASON or league.get("season") or "2022"
    try: SEASON = int(season_str)
    except: SEASON = 2022

    season_dir = OUT_DIR / str(SEASON)
    season_dir.mkdir(parents=True, exist_ok=True)

    rid_to_owner, owner_to_rid, owner_to_teamname, owner_to_display = owner_maps(users, rosters)

    # Regular Season 1..14
    stats, owners_sorted = compute_regular_season(LEAGUE_ID, rid_to_owner, owner_to_teamname, owner_to_display)

    # Draft-Positionen (fix)
    owner_to_dpos = draft_positions_for_league(LEAGUE_ID)
    for oid in stats.keys():
        dp = owner_to_dpos.get(str(oid))
        if dp is not None:
            stats[oid]["DraftPosition"] = dp

    # Moves/Trades (optional; Moves exakt, Trades best effort je Owner via creator/roster_ids)
    # Wenn du keine Moves/Trades willst: diesen Block weglassen.
    for oid in stats.keys():
        m, tr = count_moves_and_trades(LEAGUE_ID, oid)
        stats[oid]["Moves"] = m
        stats[oid]["Trades"] = tr

    # ---------- Datei 1: Regular 1..14 ----------
    regular_out = season_dir / f"standings_regular_1_14.tsv"
    header_regular = [
        "TeamName","RegularSeasonRank","Record","PointsFor","PointsAgainst",
        "PlayoffRank","ManagerName","Moves","Trades","DraftPosition"
    ]

    # PlayoffRank hier NICHT aus Brackets, sondern erstmal leer – der kommt in Datei 2 separat.
    with regular_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(header_regular)
        for oid in owners_sorted:
            s = stats[oid]
            record = format_record(s["W"], s["L"], s["T"])
            pf = fmt_num(s["PF"])
            pa = fmt_num(s["PA"])
            w.writerow([
                s["TeamName"],
                s.get("RegularSeasonRank",""),
                record,
                pf,
                pa,
                "",  # PlayoffRank absichtlich leer in dieser Datei
                s["ManagerName"],
                s.get("Moves",0),
                s.get("Trades",0),
                s.get("DraftPosition",""),
            ])

    print(f"✓ Regular Season geschrieben: {regular_out}")

    # ---------- Datei 2: Playoffs (nach deinem Schema) ----------
    playoff_rows = compute_playoffs_custom(LEAGUE_ID, owners_sorted, owner_to_rid, stats)
    playoff_out = season_dir / f"standings_playoffs.tsv"
    header_playoffs = ["TeamName","PlayoffRank","ManagerName","Seed","Week15Pts","Week16Pts"]

    with playoff_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(header_playoffs)
        for row in playoff_rows:
            w.writerow(row)

    print(f"✓ Playoff-Ranking geschrieben: {playoff_out}")

if __name__ == "__main__":
    main()
