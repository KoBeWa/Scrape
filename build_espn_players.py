import json
import time
from pathlib import Path

import pandas as pd
import requests

# Welche Saisons du abdecken willst
SEASONS = range(2015, 2026)

# Mapping von defaultPositionId -> Text
# Falls dir hier irgendwas komisch vorkommt, kannst du es später einfach anpassen.
POSITION_MAP = {
    0: "QB",
    2: "RB",
    4: "WR",
    6: "TE",
    16: "D/ST",
    17: "K",
}

def fetch_players_for_season(season: int):
    """
    Holt alle Spieler einer Saison über das öffentliche ESPN-Players-API.
    Nutzt den players_wl-View und einen x-fantasy-filter, damit nicht nur 50 Spieler kommen.
    """
    base_url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/players"
    params = {"view": "players_wl"}

    # Filter: alle aktiven Spieler, Limit hochdrehen
    filter_obj = {
        "filterActive": {"value": True},
        "limit": 10000,
    }
    headers = {"x-fantasy-filter": json.dumps(filter_obj)}

    print(f"Hole Spieler für Saison {season} ...")
    resp = requests.get(base_url, params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    rows = []
    for p in data:
        pid = p.get("id")
        full_name = p.get("fullName")
        pos_id = p.get("defaultPositionId")
        pro_team_id = p.get("proTeamId")

        rows.append(
            {
                "espn_id": pid,
                "fullName": full_name,
                "position_id": pos_id,
                "position": POSITION_MAP.get(pos_id),
                "proTeamId": pro_team_id,
                "season_first_seen": season,
            }
        )

    return rows


def main():
    all_rows = []

    for season in SEASONS:
        rows = fetch_players_for_season(season)
        all_rows.extend(rows)
        # kleine Pause, um ESPN nicht zu hart zu spammen
        time.sleep(0.5)

    df = pd.DataFrame(all_rows)

    if df.empty:
        print("Keine Daten erhalten – prüf deine Internetverbindung oder ob ESPN irgendwas geblockt hat.")
        return

    # Pro espn_id nur eine Zeile behalten (erste Saison, in der der Spieler auftaucht)
    df = (
        df.sort_values(["espn_id", "season_first_seen"])
        .drop_duplicates(subset=["espn_id"], keep="first")
    )

    # data-Ordner sicherstellen
    Path("data").mkdir(exist_ok=True)

    out_path = Path("data/espn_players.csv")
    df.to_csv(out_path, index=False, encoding="utf-8")
    print(f"FERTIG – Datei geschrieben nach: {out_path.resolve()}")


if __name__ == "__main__":
    main()
