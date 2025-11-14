import json
import time
from pathlib import Path

import pandas as pd
import requests

# Welche Saisons du abdecken willst
SEASONS = range(2015, 2026)

# Mapping von defaultPositionId -> Text
POSITION_MAP = {
    0: "QB",
    2: "RB",
    4: "WR",
    6: "TE",
    16: "D/ST",  # Defenses / Special Teams
    17: "K",
}

BASE_URL_TEMPLATE = (
    "https://fantasy.espn.com/apis/v3/games/ffl/"
    "seasons/{season}/segments/0/leaguedefaults/1"
)


def fetch_players_for_season(season: int):
    """
    Holt alle Spieler einer Saison über leaguedefaults + kona_player_info.

    Das entspricht einem Call, den ESPN selbst für Player-Projections nutzt.
    """
    url = BASE_URL_TEMPLATE.format(season=season)

    params = {
        "scoringPeriodId": 0,
        "view": "kona_player_info",
    }

    # sehr vereinfachter Filter: wir wollen einfach "viele Spieler"
    filter_obj = {
        "players": {
            # Positionsslots – kann man erweitern, aber für normale Ligen reicht das
            "filterSlotIds": {"value": [0, 2, 4, 6, 16, 17]},
            "limit": 10000,
            "offset": 0,
        }
    }

    headers = {"x-fantasy-filter": json.dumps(filter_obj)}

    print(f"Hole Spieler für Saison {season} ...")
    resp = requests.get(url, params=params, headers=headers)

    if resp.status_code != 200:
        # Debug-Ausgabe, falls ESPN wieder etwas ändert
        print("Fehler beim ESPN-Call:")
        print("Status:", resp.status_code)
        print("Response (gekürzt):", resp.text[:500])
        resp.raise_for_status()

    data = resp.json()

    # Im leaguedefaults-Response liegen die Spieler unter data["players"]
    players = data.get("players", [])

    rows = []
    for p in players:
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

    print(f"  --> {len(rows)} Spieler gefunden")
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
        print("Keine Daten erhalten – prüf Verbindung oder ob ESPN etwas blockt.")
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
