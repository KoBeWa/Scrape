#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]

DRAFTS_DIR  = ROOT / "output" / "history-drafts"
TEAMGC_ALL  = ROOT / "output" / "teamgamecenter_all_2015_2025_w1_16.csv"

OUT_DIR = ROOT / "public" / "data" / "league"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- Helpers ----------------
def norm_name(s: str) -> str:
    return (
        (s or "").lower()
        .replace(".", "")
        .replace("-", " ")
        .replace("'", "")
        .replace("’", "")
        .replace(",", "")
        .replace(" jr", "").replace(" sr", "")
        .replace(" iii", "").replace(" ii", "").replace(" iv", "")
        .replace("  ", " ")
        .strip()
    )

def to_float(x):
    if x is None:
        return 0.0
    s = str(x).strip()
    if not s or s.lower() in ("nan", "none"):
        return 0.0
    try:
        return float(s.replace(",", "."))
    except:
        return 0.0

def to_intish_str(x):
    if x is None:
        return ""
    s = str(x).strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    try:
        f = float(s.replace(",", "."))
        if f.is_integer():
            return str(int(f))
        return str(f)
    except:
        return s

def sniff_delimiter(path: Path, default=";") -> str:
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            sample = f.read(4096)
        return csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except:
        return default

def lower_map(fieldnames):
    return { (c or "").strip().lower(): c for c in (fieldnames or []) }

def pick_col(fieldnames, candidates_lower):
    lm = lower_map(fieldnames)
    for c in candidates_lower:
        if c in lm:
            return lm[c]
    return None

def make_player_key(name, sleeper_id=None, espn_id=None):
    """IDs wenn vorhanden, sonst Name"""
    sid = to_intish_str(sleeper_id)
    if sid:
        return f"s:{sid}"
    eid = to_intish_str(espn_id)
    if eid:
        return f"e:{eid}"
    n = norm_name(name)
    if n:
        return f"n:{n}"
    return ""

def detect_slot_groups(header):
    """
    Erwartet Spaltenblöcke: SLOT, SLOT_Sleeper_ID, SLOT_ESPN_ID, PointsX
    Gibt Liste: (slot, sleeper_col, espn_col, points_col)
    """
    groups = []
    if not header:
        return groups

    reserved = {"season", "week", "owner", "rank", "total", "opponent", "opponent total"}
    i = 0
    while i < len(header) - 3:
        c = header[i]
        cl = (c or "").strip().lower()

        if cl in reserved:
            i += 1
            continue

        if c.endswith("_Sleeper_ID") or c.endswith("_ESPN_ID") or (c or "").startswith("Points"):
            i += 1
            continue

        s_col = f"{c}_Sleeper_ID"
        e_col = f"{c}_ESPN_ID"
        p_col = header[i + 3]

        if header[i + 1] == s_col and header[i + 2] == e_col and (p_col or "").startswith("Points"):
            groups.append((c, s_col, e_col, p_col))
            i += 4
        else:
            i += 1

    return groups

def write_csv(path: Path, rows, cols):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=",", quoting=csv.QUOTE_MINIMAL)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])

# ---------------- Draft Frequencies ----------------
def read_draft_frequencies():
    """
    Liest output/history-drafts/*-draft.tsv
    Erwartete Spalten (typisch): ManagerName, Player, Overall (oder Round/PickInRound)

    Returns:
      draft_counts[(owner, pkey)] = count
      draft_years[(owner, pkey)]  = set(years)
      name_by_key[pkey]           = display name
    """
    draft_counts = defaultdict(int)
    draft_years = defaultdict(set)
    name_by_key = {}

    if not DRAFTS_DIR.exists():
        return draft_counts, draft_years, name_by_key

    for p in sorted(DRAFTS_DIR.glob("*-draft.tsv")):
        try:
            year = int(p.name.split("-")[0])
        except:
            continue

        with p.open("r", encoding="utf-8", newline="") as f:
            r = csv.DictReader(f, delimiter="\t")
            fns = r.fieldnames or []

            col_owner  = pick_col(fns, ["managername", "owner", "teamname"])
            col_player = pick_col(fns, ["player", "name"])
            col_sid    = pick_col(fns, ["sleeper_id", "sleeperid", "sleeper id"])
            col_eid    = pick_col(fns, ["espn_id", "espnid", "espn id"])

            for row in r:
                owner = (row.get(col_owner) or "").strip() if col_owner else ""
                player = (row.get(col_player) or "").strip() if col_player else ""
                if not owner or not player:
                    continue

                pkey = make_player_key(
                    player,
                    row.get(col_sid) if col_sid else None,
                    row.get(col_eid) if col_eid else None
                )
                if not pkey:
                    continue

                draft_counts[(owner, pkey)] += 1
                draft_years[(owner, pkey)].add(year)
                name_by_key.setdefault(pkey, player)

    return draft_counts, draft_years, name_by_key

# ---------------- Roster Frequencies (mit LineupWeeks) ----------------
def read_roster_frequencies():
    """
    Liest teamgamecenter_all_2015_2025_w1_16.csv

    Zählt:
      roster_weeks[(owner,pkey)] += 1  (jede Woche, in der er irgendwo im Team auftaucht inkl. Bench)
      lineup_weeks[(owner,pkey)] += 1  (nur Wochen in aktiven Slots, nicht BN/RES/IR/Taxi)
      roster_years[(owner,pkey)]  = set(Seasons)  (Roster)
      lineup_years[(owner,pkey)]  = set(Seasons)  (Lineup; optional, aber hilfreich)
      points_total[(owner,pkey)] += Points (wie in der Quelle; inkl. 0 möglich)

    Returns:
      roster_weeks, lineup_weeks, roster_years, lineup_years, points_total, name_by_key
    """
    roster_weeks = defaultdict(int)
    lineup_weeks = defaultdict(int)
    roster_years = defaultdict(set)
    lineup_years = defaultdict(set)
    points_total = defaultdict(float)
    name_by_key = {}

    if not TEAMGC_ALL.exists():
        return roster_weeks, lineup_weeks, roster_years, lineup_years, points_total, name_by_key

    inactive_slots = {"BN", "BENCH", "RES", "IR", "INJ", "TAXI"}

    delim = sniff_delimiter(TEAMGC_ALL, default=";")

    with TEAMGC_ALL.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=delim)
        header = r.fieldnames or []

        col_season = pick_col(header, ["season", "year"])
        col_week   = pick_col(header, ["week"])
        col_owner  = pick_col(header, ["owner", "managername", "teamname"])

        groups = detect_slot_groups(header)

        for row in r:
            season = int(to_float(row.get(col_season))) if col_season else 0
            week   = int(to_float(row.get(col_week))) if col_week else 0
            owner  = (row.get(col_owner) or "").strip() if col_owner else ""
            if not season or not week or not owner:
                continue

            # pro Woche Spieler nur einmal zählen (falls Doppelung durch Slot-Bugs)
            seen_this_week = set()

            for slot, s_col, e_col, p_col in groups:
                name = (row.get(slot) or "").strip()
                if not name or name == "-":
                    continue

                sid = row.get(s_col)
                eid = row.get(e_col)
                pkey = make_player_key(name, sid, eid)
                if not pkey:
                    continue

                key = (owner, pkey)
                if key in seen_this_week:
                    continue
                seen_this_week.add(key)

                roster_weeks[key] += 1
                roster_years[key].add(season)
                points_total[key] += to_float(row.get(p_col))
                name_by_key.setdefault(pkey, name)

                # NEU: LineupWeeks (aktive Slots)
                slot_clean = (slot or "").strip().upper()
                if slot_clean not in inactive_slots:
                    lineup_weeks[key] += 1
                    lineup_years[key].add(season)

    return roster_weeks, lineup_weeks, roster_years, lineup_years, points_total, name_by_key

# ---------------- Main ----------------
def main():
    draft_counts, draft_years, draft_name = read_draft_frequencies()
    roster_weeks, lineup_weeks, roster_years, lineup_years, roster_pts, roster_name = read_roster_frequencies()

    # unify name map
    name_by_key = dict(roster_name)
    for k, v in draft_name.items():
        name_by_key.setdefault(k, v)

    # 1) draft freq rows
    draft_rows = []
    for (owner, pkey), cnt in draft_counts.items():
        yrs = sorted(list(draft_years.get((owner, pkey), set())))
        draft_rows.append({
            "Owner": owner,
            "Player": name_by_key.get(pkey, ""),
            "PlayerKey": pkey,
            "DraftCount": cnt,
            "DraftSeasonsCount": len(yrs),
            "DraftYears": ",".join(map(str, yrs)),
        })

    draft_cols = ["Owner","Player","PlayerKey","DraftCount","DraftSeasonsCount","DraftYears"]
    draft_out = OUT_DIR / "owner_player_draft_freq.csv"
    write_csv(
        draft_out,
        sorted(draft_rows, key=lambda x: (-x["DraftCount"], x["Owner"], x["Player"])),
        draft_cols
    )

    # 2) roster freq rows (mit LineupWeeks)
    roster_rows = []
    for (owner, pkey), wcnt in roster_weeks.items():
        yrs = sorted(list(roster_years.get((owner, pkey), set())))
        lw  = lineup_weeks.get((owner, pkey), 0)

        roster_rows.append({
            "Owner": owner,
            "Player": name_by_key.get(pkey, ""),
            "PlayerKey": pkey,
            "RosterWeeks": wcnt,
            "LineupWeeks": lw,
            "RosterSeasonsCount": len(yrs),
            "RosterSeasons": ",".join(map(str, yrs)),
            "TotalPointsWhileRostered": round(roster_pts.get((owner, pkey), 0.0), 2),
        })

    roster_cols = [
        "Owner","Player","PlayerKey",
        "RosterWeeks","LineupWeeks",
        "RosterSeasonsCount","RosterSeasons",
        "TotalPointsWhileRostered"
    ]
    roster_out = OUT_DIR / "owner_player_roster_freq.csv"
    write_csv(
        roster_out,
        sorted(roster_rows, key=lambda x: (-x["RosterWeeks"], -x["LineupWeeks"], x["Owner"], x["Player"])),
        roster_cols
    )

    # 3) combined (outer join) inkl. LineupWeeks
    combined_rows = []
    all_keys = set(draft_counts.keys()) | set(roster_weeks.keys())

    for (owner, pkey) in all_keys:
        dcnt = draft_counts.get((owner, pkey), 0)
        dyrs = sorted(list(draft_years.get((owner, pkey), set())))

        rw   = roster_weeks.get((owner, pkey), 0)
        lw   = lineup_weeks.get((owner, pkey), 0)
        ryrs = sorted(list(roster_years.get((owner, pkey), set())))
        pts  = roster_pts.get((owner, pkey), 0.0)

        combined_rows.append({
            "Owner": owner,
            "Player": name_by_key.get(pkey, ""),
            "PlayerKey": pkey,
            "DraftCount": dcnt,
            "DraftSeasonsCount": len(dyrs),
            "DraftYears": ",".join(map(str, dyrs)),
            "RosterWeeks": rw,
            "LineupWeeks": lw,
            "RosterSeasonsCount": len(ryrs),
            "RosterSeasons": ",".join(map(str, ryrs)),
            "TotalPointsWhileRostered": round(pts, 2),
        })

    combined_cols = [
        "Owner","Player","PlayerKey",
        "DraftCount","DraftSeasonsCount","DraftYears",
        "RosterWeeks","LineupWeeks","RosterSeasonsCount","RosterSeasons",
        "TotalPointsWhileRostered"
    ]
    combined_out = OUT_DIR / "owner_player_combined_freq.csv"
    write_csv(
        combined_out,
        sorted(combined_rows, key=lambda x: (-x["RosterWeeks"], -x["LineupWeeks"], -x["DraftCount"], x["Owner"], x["Player"])),
        combined_cols
    )

    print(f"✓ Wrote {draft_out}")
    print(f"✓ Wrote {roster_out}")
    print(f"✓ Wrote {combined_out}")

if __name__ == "__main__":
    main()
