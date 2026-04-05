"""
Sleeper Standings Fetcher — für GitHub Codespaces
===================================================
Liest Saison-Standings aus Sleeper und schreibt direkt in den Repo-Ordner:

  output/history-standings/{YEAR}.tsv
  output/history-standings/playoffs-{YEAR}.tsv
  output/history-standings/season_results_{YEAR}.csv    ← Supabase Import
  output/history-standings/playoff_results_{YEAR}.csv   ← Supabase Import

Setup (einmalig in Codespaces Terminal):
  pip install requests

Ausführen:
  python fetch_sleeper_standings.py
"""

import requests
import csv
import os
import sys

# ─────────────────────────────────────────────────────────────
#  EINSTELLUNGEN — hier anpassen
# ─────────────────────────────────────────────────────────────

LEAGUE_ID = "DEINE_LEAGUE_ID_HIER"
# Die League-ID steht in der Sleeper-URL:
# https://sleeper.com/leagues/123456789012/team
#                               ↑ diese Zahlen

SEASON = 2025

# Playoff-Wochen
PLAYOFF_WEEK_A = 15
PLAYOFF_WEEK_B = 16

# Manager-Mapping: Sleeper User-ID → manager_id in der Datenbank
# Beim ersten Run leer lassen → Script zeigt alle User-IDs an
# Dann hier eintragen und nochmal ausführen
MANAGER_MAP = {
    # "123456789012345678": "benni",
    # "987654321098765432": "erik",
    # "111111111111111111": "juschka",
    # "222222222222222222": "kessi",
    # "333333333333333333": "marv",
    # "444444444444444444": "ritz",
    # "555555555555555555": "simi",
    # "666666666666666666": "tommy",
}

# Ausgabe-Ordner relativ zum Repo-Root
# In Codespaces ist der Repo-Root das aktuelle Verzeichnis
OUTPUT_DIR = os.path.join("output", "history-standings")

# ─────────────────────────────────────────────────────────────
#  SLEEPER API
# ─────────────────────────────────────────────────────────────

BASE = "https://api.sleeper.app/v1"

def api_get(path):
    url = f"{BASE}{path}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"  API-Fehler für {url}: {e}")
        return None

def get_league():
    return api_get(f"/league/{LEAGUE_ID}")

def get_rosters():
    return api_get(f"/league/{LEAGUE_ID}/rosters")

def get_users():
    return api_get(f"/league/{LEAGUE_ID}/users")

def get_matchups(week):
    return api_get(f"/league/{LEAGUE_ID}/matchups/{week}")

def get_transactions(week):
    return api_get(f"/league/{LEAGUE_ID}/transactions/{week}")

def get_drafts():
    return api_get(f"/league/{LEAGUE_ID}/drafts")

def get_draft_picks(draft_id):
    return api_get(f"/draft/{draft_id}/picks")


# ─────────────────────────────────────────────────────────────
#  HILFSFUNKTIONEN
# ─────────────────────────────────────────────────────────────

def resolve_manager_id(user_id, display_name):
    """Gibt die manager_id zurück — aus MANAGER_MAP oder bereinigter display_name."""
    if user_id in MANAGER_MAP:
        return MANAGER_MAP[user_id]
    return display_name.lower().strip()


def get_move_and_trade_counts(reg_season_weeks):
    """Zählt Moves (Free Agent Adds) und Trades pro Roster-ID."""
    moves  = {}
    trades = {}
    print(f"  Zähle Transaktionen für Wochen 1–{reg_season_weeks}...")
    for week in range(1, reg_season_weeks + 1):
        txs = get_transactions(week)
        if not txs:
            continue
        for tx in txs:
            if tx.get("status") != "complete":
                continue
            roster_ids = tx.get("roster_ids", [])
            tx_type    = tx.get("type", "")
            if tx_type == "free_agent":
                for rid in roster_ids:
                    moves[rid] = moves.get(rid, 0) + 1
            elif tx_type == "trade":
                for rid in roster_ids:
                    trades[rid] = trades.get(rid, 0) + 1
    return moves, trades


def get_draft_positions():
    """Gibt Draft-Positionen (Pick-Nr. Runde 1) pro Roster-ID zurück."""
    draft_pos = {}
    drafts = get_drafts()
    if not drafts:
        return draft_pos
    snake = [d for d in drafts
             if d.get("type") == "snake" and str(d.get("season")) == str(SEASON)]
    if not snake:
        print("  Warnung: Kein Snake-Draft für diese Saison gefunden.")
        return draft_pos
    picks = get_draft_picks(snake[0]["draft_id"])
    if not picks:
        return draft_pos
    for pick in picks:
        if pick.get("round") == 1:
            roster_id = pick.get("roster_id")
            draft_pos[roster_id] = pick.get("pick_no")
    return draft_pos


def get_week_scores(week):
    """Gibt Punkte pro Roster-ID für eine Woche zurück."""
    scores = {}
    matchups = get_matchups(week)
    if not matchups:
        return scores
    for m in matchups:
        rid    = m.get("roster_id")
        pts    = m.get("points", 0) or 0
        scores[rid] = round(float(pts), 2)
    return scores


def assign_reg_season_ranks(rows):
    """Sortiert nach Wins DESC, dann Points For DESC und vergibt Ränge."""
    rows.sort(key=lambda x: (-x["wins"], -x["points_for"]))
    for i, row in enumerate(rows, start=1):
        row["reg_rank"] = i
    return rows


def get_playoff_seeds(rosters):
    """Liest Playoff-Seed aus Roster-Metadata (falls vorhanden)."""
    seeds = {}
    for r in rosters:
        meta = r.get("metadata") or {}
        seed = meta.get("playoff_seed")
        if seed is not None:
            seeds[r["roster_id"]] = int(seed)
    return seeds


def get_playoff_ranks(rosters):
    """Liest finalen Playoff-Rang aus Roster-Metadata."""
    ranks = {}
    for r in rosters:
        meta = r.get("metadata") or {}
        rank = meta.get("rank")
        if rank is not None:
            ranks[r["roster_id"]] = int(rank)
        else:
            # Fallback: aus settings.rank wenn vorhanden
            rank2 = (r.get("settings") or {}).get("rank")
            if rank2 is not None:
                ranks[r["roster_id"]] = int(rank2)
    return ranks


# ─────────────────────────────────────────────────────────────
#  DATEIEN SCHREIBEN
# ─────────────────────────────────────────────────────────────

def write_standings_tsv(season_rows, path):
    """Schreibt das klassische .tsv Format (wie deine alten Dateien)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "TeamName", "RegularSeasonRank", "Record", "PointsFor",
            "PointsAgainst", "PlayoffRank", "ManagerName", "Moves",
            "Trades", "DraftPosition"
        ], delimiter="\t")
        writer.writeheader()
        for row in season_rows:
            record = f"{row['wins']}-{row['losses']}-{row['ties']}"
            writer.writerow({
                "TeamName":          row["display_name"],
                "RegularSeasonRank": row["reg_rank"],
                "Record":            record,
                "PointsFor":         f"{row['points_for']:.2f}",
                "PointsAgainst":     f"{row['points_against']:.2f}",
                "PlayoffRank":       row["playoff_rank"] if row["playoff_rank"] is not None else "",
                "ManagerName":       row["manager_id"],
                "Moves":             row["moves"],
                "Trades":            row["trades"],
                "DraftPosition":     row["draft_position"] if row["draft_position"] else "",
            })


def write_playoffs_tsv(playoff_rows, season_rows, path):
    """Schreibt das klassische playoffs-YEAR.tsv Format."""
    playoff_rows_sorted = sorted(playoff_rows, key=lambda x: (x["final_rank"] or 99))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "TeamName", "PlayoffRank", "ManagerName", "Seed", "Week15Pts", "Week16Pts"
        ], delimiter="\t")
        writer.writeheader()
        for row in playoff_rows_sorted:
            display = next(
                (s["display_name"] for s in season_rows if s["manager_id"] == row["manager_id"]),
                row["manager_id"]
            )
            writer.writerow({
                "TeamName":    display,
                "PlayoffRank": row["final_rank"] if row["final_rank"] is not None else "",
                "ManagerName": row["manager_id"],
                "Seed":        row["seed"] if row["seed"] is not None else "",
                "Week15Pts":   f"{row['week15_pts']:.2f}" if row["week15_pts"] is not None else "",
                "Week16Pts":   f"{row['week16_pts']:.2f}" if row["week16_pts"] is not None else "",
            })


def write_season_results_csv(season_rows, path):
    """Schreibt die CSV für den Supabase-Import in season_results."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "season_year", "manager_id", "reg_rank", "wins", "losses", "ties",
            "points_for", "points_against", "moves", "trades",
            "draft_position", "playoff_rank", "championship_finish"
        ])
        writer.writeheader()
        for row in season_rows:
            champ = row["playoff_rank"] if row["playoff_rank"] in (1, 2, 3) else ""
            writer.writerow({
                "season_year":         SEASON,
                "manager_id":          row["manager_id"],
                "reg_rank":            row["reg_rank"],
                "wins":                row["wins"],
                "losses":              row["losses"],
                "ties":                row["ties"],
                "points_for":          f"{row['points_for']:.2f}",
                "points_against":      f"{row['points_against']:.2f}",
                "moves":               row["moves"],
                "trades":              row["trades"],
                "draft_position":      row["draft_position"] if row["draft_position"] else "",
                "playoff_rank":        row["playoff_rank"] if row["playoff_rank"] is not None else "",
                "championship_finish": champ,
            })


def write_playoff_results_csv(playoff_rows, path):
    """Schreibt die CSV für den Supabase-Import in playoff_results."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "season_year", "manager_id", "seed",
            "week15_pts", "week16_pts", "final_rank"
        ])
        writer.writeheader()
        for row in playoff_rows:
            writer.writerow({
                "season_year": SEASON,
                "manager_id":  row["manager_id"],
                "seed":        row["seed"] if row["seed"] is not None else "",
                "week15_pts":  f"{row['week15_pts']:.2f}" if row["week15_pts"] is not None else "",
                "week16_pts":  f"{row['week16_pts']:.2f}" if row["week16_pts"] is not None else "",
                "final_rank":  row["final_rank"] if row["final_rank"] is not None else "",
            })


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main():
    if LEAGUE_ID == "DEINE_LEAGUE_ID_HIER":
        print("\nFEHLER: Bitte LEAGUE_ID eintragen (Zeile 22).")
        print("  URL-Format: https://sleeper.com/leagues/HIER/team\n")
        sys.exit(1)

    print("\n╔══════════════════════════════════════════════╗")
    print("║  Sleeper Standings Fetcher — Codespaces      ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"\n  League: {LEAGUE_ID}  |  Saison: {SEASON}")

    # ── League-Info ──────────────────────────────────────
    print("\n[1/6] League laden...")
    league = get_league()
    if not league:
        print("FEHLER: League nicht gefunden. League-ID prüfen.")
        sys.exit(1)
    league_name      = league.get("name", "?")
    reg_season_weeks = league.get("settings", {}).get("last_scored_leg", 14)
    print(f"  {league_name}  |  Regular Season: {reg_season_weeks} Wochen")

    # ── Users + Rosters ──────────────────────────────────
    print("\n[2/6] Users und Rosters laden...")
    users   = get_users()
    rosters = get_rosters()
    if not users or not rosters:
        print("FEHLER: Users oder Rosters konnten nicht geladen werden.")
        sys.exit(1)

    user_map    = {u["user_id"]: u.get("display_name", "?") for u in users}
    roster_user = {r["roster_id"]: r.get("owner_id") for r in rosters}

    # Beim ersten Run mit leerem MANAGER_MAP: IDs anzeigen
    if not MANAGER_MAP:
        print("\n  ┌─ USER-IDs FÜR MANAGER_MAP ─────────────────────────┐")
        print("  │ Trag diese Zeilen in MANAGER_MAP ein und starte neu │")
        print("  └─────────────────────────────────────────────────────┘")
        for r in sorted(rosters, key=lambda x: x["roster_id"]):
            uid   = r.get("owner_id", "?")
            dname = user_map.get(uid, "?")
            print(f'    "{uid}": "{dname.lower()}",   # {dname}')
        print()

    # ── Transaktionen ────────────────────────────────────
    print("\n[3/6] Transaktionen zählen...")
    moves_map, trades_map = get_move_and_trade_counts(reg_season_weeks)

    # ── Draft-Positionen ─────────────────────────────────
    print("\n[4/6] Draft-Positionen laden...")
    draft_pos_map = get_draft_positions()

    # ── Playoff-Daten ────────────────────────────────────
    print(f"\n[5/6] Playoff-Daten laden (Wochen {PLAYOFF_WEEK_A} + {PLAYOFF_WEEK_B})...")
    scores_wk15   = get_week_scores(PLAYOFF_WEEK_A)
    scores_wk16   = get_week_scores(PLAYOFF_WEEK_B)
    playoff_seeds = get_playoff_seeds(rosters)
    playoff_ranks = get_playoff_ranks(rosters)

    # ── Daten zusammenbauen ──────────────────────────────
    print("\n[6/6] Daten zusammenstellen...")
    season_rows  = []
    playoff_rows = []

    for r in rosters:
        rid       = r["roster_id"]
        uid       = roster_user.get(rid)
        dname     = user_map.get(uid, f"team_{rid}")
        mgr_id    = resolve_manager_id(uid, dname)
        s         = r.get("settings") or {}

        wins   = s.get("wins", 0)
        losses = s.get("losses", 0)
        ties   = s.get("ties", 0)

        # Sleeper speichert Punkte aufgeteilt: fpts + fpts_decimal (Nachkommastellen)
        pf = round(
            float(s.get("fpts", 0) or 0) +
            float(s.get("fpts_decimal", 0) or 0) / 100, 2
        )
        pa = round(
            float(s.get("fpts_against", 0) or 0) +
            float(s.get("fpts_against_decimal", 0) or 0) / 100, 2
        )

        season_rows.append({
            "roster_id":      rid,
            "manager_id":     mgr_id,
            "display_name":   dname,
            "wins":           wins,
            "losses":         losses,
            "ties":           ties,
            "points_for":     pf,
            "points_against": pa,
            "moves":          moves_map.get(rid, 0),
            "trades":         trades_map.get(rid, 0),
            "draft_position": draft_pos_map.get(rid),
            "playoff_rank":   playoff_ranks.get(rid),
        })

        playoff_rows.append({
            "manager_id":  mgr_id,
            "seed":        playoff_seeds.get(rid),
            "week15_pts":  scores_wk15.get(rid),
            "week16_pts":  scores_wk16.get(rid),
            "final_rank":  playoff_ranks.get(rid),
        })

    # Regular Season Rang vergeben
    season_rows = assign_reg_season_ranks(season_rows)

    # ── Output-Ordner sicherstellen ───────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Dateien schreiben ────────────────────────────────
    p_standings      = os.path.join(OUTPUT_DIR, f"{SEASON}.tsv")
    p_playoffs       = os.path.join(OUTPUT_DIR, f"playoffs-{SEASON}.tsv")
    p_season_csv     = os.path.join(OUTPUT_DIR, f"season_results_{SEASON}.csv")
    p_playoff_csv    = os.path.join(OUTPUT_DIR, f"playoff_results_{SEASON}.csv")

    write_standings_tsv(season_rows, p_standings)
    write_playoffs_tsv(playoff_rows, season_rows, p_playoffs)
    write_season_results_csv(season_rows, p_season_csv)
    write_playoff_results_csv(playoff_rows, p_playoff_csv)

    # ── Ergebnis anzeigen ────────────────────────────────
    print(f"\n  ✓ {p_standings}")
    print(f"  ✓ {p_playoffs}")
    print(f"  ✓ {p_season_csv}  ← Supabase: season_results")
    print(f"  ✓ {p_playoff_csv}  ← Supabase: playoff_results")

    print(f"\n  Saison {SEASON} — Endstand:")
    print(f"  {'#':<3} {'Manager':<12} {'W-L-T':<10} {'PF':>8}  {'Playoff':>7}")
    print(f"  {'─'*3} {'─'*12} {'─'*10} {'─'*8}  {'─'*7}")
    for row in season_rows:
        record = f"{row['wins']}-{row['losses']}-{row['ties']}"
        pr     = str(row["playoff_rank"]) if row["playoff_rank"] else "-"
        print(f"  {row['reg_rank']:<3} {row['manager_id']:<12} {record:<10} "
              f"{row['points_for']:>8.2f}  {pr:>7}")

    print(f"\n  → Dateien committen und pushen:")
    print(f"     git add {OUTPUT_DIR}/")
    print(f"     git commit -m 'Add {SEASON} standings'")
    print(f"     git push\n")


if __name__ == "__main__":
    try:
        import requests
    except ImportError:
        print("\nFEHLER: 'requests' ist nicht installiert.")
        print("  Lösung: pip install requests\n")
        sys.exit(1)
    main()
