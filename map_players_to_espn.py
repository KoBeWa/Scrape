import pandas as pd
from pathlib import Path
import re
from rapidfuzz import process, fuzz


# ==============================
# CONFIG – ggf. anpassen
# ==============================

# Draft-Dateien: z.B. output/history-drafts/2015-draft.tsv
DRAFTS_DIR = Path("output/history-drafts")

# Matchup-Dateien: z.B. output/teamgamecenter/2015/1.csv
MATCHUPS_DIR = Path("output/teamgamecenter")

# ESPN-Spielerliste (musst du selbst anlegen)
# erwartete Spalten: espn_id, fullName, position (team optional)
ESPN_PLAYERS_CSV = Path("data/espn_players.csv")

# Output-Dateien
OUTPUT_PLAYERS_MAPPED = Path("output/players_mapped_full.csv")
OUTPUT_PLAYERS_REVIEW = Path("output/players_mapped_review.csv")
OUTPUT_DRAFTS_WITH_ID = Path("output/history-drafts-with-espn-id.csv")
OUTPUT_MATCHUPS_WITH_ID = Path("output/teamgamecenter-with-espn-id.csv")

MIN_FUZZY_SCORE = 85  # Mindest-Punktzahl (0–100) für automatisches Match


# ==============================
# HILFSFUNKTIONEN
# ==============================

def normalize_name(name: str) -> str:
    """Normalisiert Spielernamen für Matching (klein, ohne Punkte etc.)."""
    if pd.isna(name):
        return ""
    s = str(name).lower().strip()
    s = re.sub(r"[.\']", "", s)   # Punkte und Apostrophe raus
    s = re.sub(r"\s+", " ", s)    # Mehrfach-Leerzeichen zu einem
    return s


def parse_matchup_cell(cell: str):
    """
    Zerlegt eine Zelle wie:
      'P. Manning QB - DEN ' -> ('P. Manning', 'QB', 'DEN')
      'Rams DEF '            -> ('Rams', 'DEF', None)
      '-' oder ''            -> (None, None, None)
    """
    if pd.isna(cell):
        return None, None, None

    cell = str(cell).strip()
    if cell in ["", "-"]:
        return None, None, None

    # Split nach " - " um Team zu bekommen
    parts = cell.split(" - ")
    left = parts[0]       # z.B. 'P. Manning QB' oder 'Rams DEF'
    team = parts[1].strip() if len(parts) > 1 else None

    tokens = left.split()
    pos = None
    name = left

    # letztes Token könnte Position sein (QB, RB, WR, TE, K, DEF, DST etc.)
    last = tokens[-1]
    if last.isupper() and len(last) <= 4:
        pos = last
        name = " ".join(tokens[:-1])

    return name, pos, team


def get_player_columns(df: pd.DataFrame):
    """
    Bestimmt automatisch alle Spalten, in denen Spieler stehen.
    Ignoriert Owner, Rank, Total, Opponent, Opponent Total und alle *Points-Spalten.
    """
    ignore = {"Owner", "Rank", "Total", "Opponent", "Opponent Total"}
    player_cols = []
    for col in df.columns:
        if col in ignore:
            continue
        if "Points" in col:
            continue
        player_cols.append(col)
    return player_cols


def load_all_drafts(drafts_dir: Path) -> pd.DataFrame:
    """
    Liest alle Draft-TSV-Dateien aus output/history-drafts ein und hängt sie zusammen.
    Beispiel-Datei: 2015-draft.tsv
    """
    dfs = []
    for f in drafts_dir.glob("*.tsv"):
        print(f"Lade Draft-Datei: {f}")
        # TSV -> Tab als Trennzeichen
        df = pd.read_csv(f, sep="\t")
        # Season aus Dateinamen extrahieren (z.B. 2015-draft.tsv)
        match = re.search(r"(\d{4})", f.stem)
        season = int(match.group(1)) if match else None
        df["Season"] = season
        dfs.append(df)

    if not dfs:
        print("WARNUNG: Keine Draft-Dateien gefunden.")
        return pd.DataFrame()

    return pd.concat(dfs, ignore_index=True)


def load_all_matchups(matchups_dir: Path) -> pd.DataFrame:
    """
    Liest alle Matchup-CSV-Dateien aus output/teamgamecenter/<year>/*.csv ein.
    Beispiel-Dateien: output/teamgamecenter/2015/1.csv, 2015/2.csv, ...
    """
    dfs = []
    for f in matchups_dir.rglob("*.csv"):
        print(f"Lade Matchup-Datei: {f}")
        df = pd.read_csv(f)
        # Season aus Ordnernamen (z.B. output/teamgamecenter/2015/1.csv)
        try:
            season = int(f.parent.name)
        except ValueError:
            season = None
        # Week aus Dateiname (optional, falls du es später brauchst)
        week = f.stem
        df["Season"] = season
        df["Week"] = week
        dfs.append(df)

    if not dfs:
        print("WARNUNG: Keine Matchup-Dateien gefunden.")
        return pd.DataFrame()

    return pd.concat(dfs, ignore_index=True)


def match_to_espn(row, espn_df: pd.DataFrame, min_score=85):
    """
    Matched einen Datensatz (name_key + pos) gegen die ESPN-Spielerliste.
    Gibt (espn_id, espn_name, match_score) zurück.
    """
    name_key = row["name_key"]
    pos = row.get("pos", None)

    candidates = espn_df

    # erst nach Position filtern, falls vorhanden
    if pd.notna(pos) and pos:
        cand_pos = candidates[candidates["position"].str.upper() == str(pos).upper()]
        if not cand_pos.empty:
            candidates = cand_pos

    if not name_key:
        return pd.Series([None, None, 0])

    choices = candidates["name_key"].tolist()
    if not choices:
        return pd.Series([None, None, 0])

    best_name_key, score, idx = process.extractOne(
        name_key,
        choices,
        scorer=fuzz.token_sort_ratio
    )

    if score < min_score:
        return pd.Series([None, None, score])

    espn_row = candidates.iloc[idx]
    return pd.Series([espn_row["espn_id"], espn_row["fullName"], score])


def get_espn_id_from_cell(cell, mapping_df: pd.DataFrame):
    """
    Holt zu einer Zelle 'P. Manning QB - DEN ' die espn_id aus dem Mapping.
    """
    raw_name, pos, team = parse_matchup_cell(cell)
    if raw_name is None:
        return None

    # exaktes Match im Mapping versuchen
    m = mapping_df[
        (mapping_df["raw_name"] == raw_name) &
        (mapping_df["pos"].fillna("") == (pos or "")) &
        (mapping_df["team"].fillna("") == (team or ""))
    ]

    if not m.empty:
        return m["espn_id"].iloc[0]

    return None


# ==============================
# HAUPTLOGIK
# ==============================

def main():
    # 1. Drafts & Matchups laden
    drafts = load_all_drafts(DRAFTS_DIR)
    matchups = load_all_matchups(MATCHUPS_DIR)

    if drafts.empty and matchups.empty:
        print("Es wurden weder Drafts noch Matchups gefunden. Abbruch.")
        return

    # 2. Draft-Spieler vorbereiten
    if not drafts.empty:
        draft_players = drafts[["Player", "Pos", "NFLTeam", "Season"]].copy()
        draft_players["raw_name"] = draft_players["Player"].astype(str)
        draft_players["pos"] = draft_players["Pos"].astype(str)
        draft_players["team"] = draft_players["NFLTeam"].astype(str)
        draft_players["name_key"] = draft_players["raw_name"].map(normalize_name)
    else:
        draft_players = pd.DataFrame(columns=["raw_name", "pos", "team", "Season", "name_key"])

    # 3. Matchup-Spieler vorbereiten
    if not matchups.empty:
        player_cols = get_player_columns(matchups)
        rows = []
        for _, row in matchups.iterrows():
            season = row.get("Season", None)
            for col in player_cols:
                cell = row[col]
                raw_name, pos, team = parse_matchup_cell(cell)
                if raw_name is None:
                    continue
                rows.append({
                    "raw_name": raw_name,
                    "pos": pos,
                    "team": team,
                    "Season": season,
                    "slot": col,
                })
        matchup_players = pd.DataFrame(rows)
        if not matchup_players.empty:
            matchup_players["name_key"] = matchup_players["raw_name"].map(normalize_name)
        else:
            matchup_players = pd.DataFrame(columns=["raw_name", "pos", "team", "Season", "name_key"])
    else:
        matchup_players = pd.DataFrame(columns=["raw_name", "pos", "team", "Season", "name_key"])

    # 4. Roh-Spielerliste zusammenführen
    players_raw = pd.concat(
        [
            draft_players[["raw_name", "pos", "team", "Season", "name_key"]],
            matchup_players[["raw_name", "pos", "team", "Season", "name_key"]],
        ],
        ignore_index=True
    )

    players_raw = players_raw.drop_duplicates(subset=["name_key", "pos", "team"])
    print(f"Anzahl unterschiedlicher Spieler-Kombinationen (name+pos+team): {len(players_raw)}")

    # 5. ESPN-Spielerliste laden
    print(f"Lade ESPN-Spielerliste aus: {ESPN_PLAYERS_CSV}")
    espn = pd.read_csv(ESPN_PLAYERS_CSV)

    # Erwartete Spalten: espn_id, fullName, position
    required_cols = {"espn_id", "fullName", "position"}
    if not required_cols.issubset(set(espn.columns)):
        raise ValueError(f"ESPN-Datei muss mindestens diese Spalten enthalten: {required_cols}")

    espn["name_key"] = espn["fullName"].map(normalize_name)
    espn["position"] = espn["position"].astype(str)

    # 6. Matching Spieler → ESPN
    print("Starte Matching gegen ESPN-Spielerliste...")
    players_raw[["espn_id", "espn_name", "match_score"]] = players_raw.apply(
        match_to_espn,
        axis=1,
        result_type="expand",
        espn_df=espn,
        min_score=MIN_FUZZY_SCORE
    )

    # 7. Ergebnisse aufteilen in gute und unsichere Matches
    good = players_raw[players_raw["espn_id"].notna() & (players_raw["match_score"] >= MIN_FUZZY_SCORE)]
    review = players_raw[(players_raw["espn_id"].isna()) | (players_raw["match_score"] < MIN_FUZZY_SCORE)]

    print(f"Gute Matches: {len(good)}")
    print(f"Unklare/fehlende Matches: {len(review)}")

    OUTPUT_PLAYERS_MAPPED.parent.mkdir(parents=True, exist_ok=True)
    good.to_csv(OUTPUT_PLAYERS_MAPPED, index=False)
    review.to_csv(OUTPUT_PLAYERS_REVIEW, index=False)
    print(f"Gesamtes Mapping gespeichert in: {OUTPUT_PLAYERS_MAPPED}")
    print(f"Unklare Fälle gespeichert in: {OUTPUT_PLAYERS_REVIEW}")

    # 8. Mapping vorbereiten für Drafts & Matchups
    map_simple = players_raw[["raw_name", "pos", "team", "espn_id"]].dropna(subset=["espn_id"]).drop_duplicates()

    # 9. Drafts mit espn_id versehen
    if not drafts.empty:
        drafts_with_id = drafts.merge(
            map_simple,
            left_on=["Player", "Pos", "NFLTeam"],
            right_on=["raw_name", "pos", "team"],
            how="left"
        )
        drafts_with_id.to_csv(OUTPUT_DRAFTS_WITH_ID, index=False)
        print(f"Drafts mit espn_id gespeichert in: {OUTPUT_DRAFTS_WITH_ID}")
    else:
        print("Keine Drafts vorhanden, überspringe Draft-Output.")

    # 10. Matchups mit espn_id-Spalten versehen
    if not matchups.empty and not map_simple.empty:
        player_cols = get_player_columns(matchups)
        for col in player_cols:
            id_col = f"{col}_espn_id"
            print(f"Füge espn_id-Spalte für {col} hinzu -> {id_col}")
            matchups[id_col] = matchups[col].apply(lambda c: get_espn_id_from_cell(c, map_simple))

        matchups.to_csv(OUTPUT_MATCHUPS_WITH_ID, index=False)
        print(f"Matchups mit espn_id gespeichert in: {OUTPUT_MATCHUPS_WITH_ID}")
    else:
        print("Keine Matchups oder kein Mapping vorhanden, überspringe Matchup-Output.")

    print("FERTIG.")


if __name__ == "__main__":
    main()
