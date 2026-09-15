#!/usr/bin/env python3
"""
Lädt das nflverse-Play-by-Play der laufenden Season
(https://github.com/nflverse/nflverse-data/releases/tag/pbp), behält nur die
für Fantasy-Scoring nötigen Spalten und schreibt pro Woche eine kleine
gz-CSV nach instagram/pbp/<season>/weekNN.csv.gz (~0,3–0,6 MB statt ~100 MB).

build_week.py benutzt diese Dateien automatisch, wenn sie vorhanden sind.

Aufruf:  python instagram/fetch_pbp.py              # Season aus Sleeper-State
         python instagram/fetch_pbp.py --season 2026
"""
import argparse, csv, gzip, io, os, sys
from collections import defaultdict
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = [
    "game_id", "season", "week", "season_type", "game_date", "start_time",
    "home_team", "away_team", "posteam", "defteam",
    "qtr", "time", "game_seconds_remaining", "play_type", "desc", "yards_gained",
    "passer_player_id", "passer_player_name", "rusher_player_id", "rusher_player_name",
    "receiver_player_id", "receiver_player_name",
    "passing_yards", "rushing_yards", "receiving_yards", "complete_pass",
    "pass_touchdown", "rush_touchdown", "touchdown", "td_team", "td_player_id", "td_player_name",
    "interception", "sack", "safety", "fumble_lost", "fumbled_1_player_id", "fumbled_1_player_name",
    "fumble_recovery_1_team", "two_point_conv_result",
    "kicker_player_id", "kicker_player_name", "field_goal_result", "kick_distance", "extra_point_result",
    "total_home_score", "total_away_score",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int)
    a = ap.parse_args()
    season = a.season
    if not season:
        st = requests.get("https://api.sleeper.app/v1/state/nfl", timeout=30).json()
        season = int(st["season"])

    url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
    print("laden:", url, file=sys.stderr)
    r = requests.get(url, timeout=600)
    if r.status_code == 404:
        print("noch keine PBP-Datei für", season, file=sys.stderr)
        return
    r.raise_for_status()
    text = gzip.decompress(r.content).decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    cols = [c for c in KEEP if c in reader.fieldnames]
    missing = [c for c in KEEP if c not in reader.fieldnames]
    if missing:
        print("fehlende Spalten (ignoriert):", missing, file=sys.stderr)

    by_week = defaultdict(list)
    for row in reader:
        try:
            w = int(float(row.get("week") or 0))
        except ValueError:
            continue
        if w:
            by_week[w].append([row.get(c, "") for c in cols])

    out_dir = os.path.join(ROOT, "instagram", "pbp", str(season))
    os.makedirs(out_dir, exist_ok=True)
    for w in sorted(by_week):
        path = os.path.join(out_dir, f"week{w:02d}.csv.gz")
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(cols)
        wr.writerows(by_week[w])
        data = gzip.compress(buf.getvalue().encode("utf-8"), mtime=0)   # mtime=0 -> stabiler Diff
        old = open(path, "rb").read() if os.path.exists(path) else None
        if old != data:
            with open(path, "wb") as f:
                f.write(data)
            print(f"week{w:02d}: {len(by_week[w])} Plays, {len(data)//1024} KB", file=sys.stderr)
    print("fertig:", len(by_week), "Wochen", file=sys.stderr)


if __name__ == "__main__":
    main()
