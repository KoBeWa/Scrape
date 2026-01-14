#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import re
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]

PLAYERRANKS = ROOT / "output" / "PlayerRanks_with_vorp_draftpick.csv"
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

def abbrev_key(name: str) -> str:
    """Nachname + Initiale: 'Patrick Mahomes' -> 'mahomes_p' """
    parts = norm_name(name).split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}_{parts[0][0]}"

def to_float(x):
    """Komma-Dezimal -> float, robust gegen None/'' """
    if x is None:
        return 0.0
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return 0.0
    try:
        return float(s.replace(",", "."))
    except:
        return 0.0

def to_intish_str(x):
    """IDs kommen teils als '1234.0' -> '1234' """
    if x is None:
        return ""
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return ""
    try:
        f = float(s.replace(",", "."))
        if f.is_integer():
            return str(int(f))
        return str(f)
    except:
        return s

def make_player_key(name, sleeper_id, espn_id):
    """Primär IDs, fallback Name"""
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

def is_drafted(drafted_set, name, key):
    """Check über ID-key + Name + Abbrev"""
    if key and key in drafted_set:
        return True
    n = norm_name(name)
    if n and f"n:{n}" in drafted_set:
        return True
    a = abbrev_key(name)
    if a and f"a:{a}" in drafted_set:
        return True
    return False

def detect_slot_groups(header):
    """
    Sucht Sequenzen: SLOT, SLOT_Sleeper_ID, SLOT_ESPN_ID, PointsX
    Gibt Liste (slot, sleeper_col, espn_col, points_col)
    """
    groups = []
    i = 0
    while i < len(header) - 3:
        c = header[i]
        if c in ("Season", "Week", "Owner", "Rank", "Total", "Opponent", "Opponent Total"):
            i += 1
            continue
        if c.endswith("_Sleeper_ID") or c.endswith("_ESPN_ID") or c.startswith("Points"):
            i += 1
            continue

        s_col = f"{c}_Sleeper_ID"
        e_col = f"{c}_ESPN_ID"
        p_col = header[i + 3]

        if header[i + 1] == s_col and header[i + 2] == e_col and p_col.startswith("Points"):
            groups.append((c, s_col, e_col, p_col))
            i += 4
        else:
            i += 1
    return groups

# ---------------- Read PlayerRanks (drafted + metadata lookups) ----------------
def read_playerranks():
    """
    Returns:
      drafted_by_year: year -> set(keys)
      meta_by_year: year -> dict(key -> {Player, Pos, AVG, TTL, GP, VORP})
    """
    drafted_by_year = defaultdict(set)
    meta_by_year = defaultdict(dict)

    with PLAYERRANKS.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=";")
        for row in r:
            year = int(row.get("Year") or 0)
            if not year:
                continue

            player = (row.get("Player") or "").strip()
            pos    = (row.get("Pos") or "").strip()
            sid    = row.get("Tabelle4.sleeper_id.1")
            eid    = row.get("Tabelle4.espn_id.1")

            keys = []
            sid_s = to_intish_str(sid)
            eid_s = to_intish_str(eid)

            if sid_s:
                keys.append(f"s:{sid_s}")
            if eid_s:
                keys.append(f"e:{eid_s}")

            n = norm_name(player)
            a = abbrev_key(player)
            if n:
                keys.append(f"n:{n}")
            if a:
                keys.append(f"a:{a}")

            # meta
            meta = {
                "Player": player,
                "Pos": pos,
                "SeasonAVG": to_float(row.get("AVG")),
                "SeasonTTL": to_float(row.get("TTL")),
                "SeasonGP":  to_float(row.get("GP")),
                "SeasonVORP": to_float(row.get("VORP")),
            }
            for k in keys:
                meta_by_year[year].setdefault(k, meta)

            # drafted?
            # (dein File hat DraftPick für gedraftete Spieler)
            draftpick = row.get("DraftPick")
            if str(draftpick).strip() not in ("", "nan", "None"):
                for k in keys:
                    drafted_by_year[year].add(k)

    return drafted_by_year, meta_by_year

# ---------------- Main build ----------------
def main():
    if not PLAYERRANKS.exists():
        raise SystemExit(f"Missing: {PLAYERRANKS}")
    if not TEAMGC_ALL.exists():
        raise SystemExit(f"Missing: {TEAMGC_ALL}")

    drafted_by_year, meta_by_year = read_playerranks()

    # stats: year -> (owner, pkey) -> stats
    first_week   = defaultdict(lambda: defaultdict(lambda: 99))
    roster_weeks = defaultdict(lambda: defaultdict(int))
    weeks_scored = defaultdict(lambda: defaultdict(int))
    total_pts    = defaultdict(lambda: defaultdict(float))
    max_pts      = defaultdict(lambda: defaultdict(lambda: -10**9))
    name_seen    = defaultdict(dict)

    with TEAMGC_ALL.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=";")
        header = r.fieldnames or []
        groups = detect_slot_groups(header)

        # optional: K/DST ausschließen (Slot-basiert)
        excluded_slots = {"K", "DST"}

        for row in r:
            year  = int(row.get("Season") or 0)
            week  = int(row.get("Week") or 0)
            owner = (row.get("Owner") or "").strip()
            if not year or not week or not owner:
                continue

            drafted_set = drafted_by_year.get(year, set())

            for slot, s_col, e_col, p_col in groups:
                if slot in excluded_slots:
                    continue

                name = (row.get(slot) or "").strip()
                if not name or name == "-":
                    continue

                sid = row.get(s_col)
                eid = row.get(e_col)
                pts = to_float(row.get(p_col))

                pkey = make_player_key(name, sid, eid)
                if not pkey:
                    continue

                # nur UNDRAFTED
                if is_drafted(drafted_set, name, pkey):
                    continue

                key = (owner, pkey)

                # first seen week (pickup week)
                if week < first_week[year][key]:
                    first_week[year][key] = week

                roster_weeks[year][key] += 1
                name_seen[year].setdefault(pkey, name)

                # deine alte Logik: nur zählen, wenn Punkte != 0
                if pts != 0:
                    weeks_scored[year][key] += 1
                    total_pts[year][key] += pts
                    if pts > max_pts[year][key]:
                        max_pts[year][key] = pts

    # Build rows
    records = []
    for year in sorted(first_week.keys()):
        for (owner, pkey), fw in first_week[year].items():
            pts = total_pts[year].get((owner, pkey), 0.0)
            wsc = weeks_scored[year].get((owner, pkey), 0)
            rw  = roster_weeks[year].get((owner, pkey), 0)

            if wsc == 0 and pts == 0:
                continue

            avg = pts / wsc if wsc else 0.0
            mx  = max_pts[year].get((owner, pkey), 0.0)

            # meta (falls vorhanden)
            meta = (
                meta_by_year.get(year, {}).get(pkey)
                or meta_by_year.get(year, {}).get(f"n:{norm_name(name_seen[year].get(pkey,''))}")
                or meta_by_year.get(year, {}).get(f"a:{abbrev_key(name_seen[year].get(pkey,''))}")
                or {}
            )

            pos = (meta.get("Pos") or "").strip()
            if pos in ("K", "DST", "DEF"):
                continue

            season_ttl = meta.get("SeasonTTL", 0.0) or 0.0
            pickup_share = (pts / season_ttl) if season_ttl else 0.0

            records.append({
                "Year": year,
                "Owner": owner,
                "Player": meta.get("Player") or name_seen[year].get(pkey, ""),
                "Pos": pos,
                "FirstWeek": fw,
                "RosterWeeks": rw,
                "WeeksScored": wsc,
                "PointsAfterPickup": round(pts, 2),
                "AvgPoints": round(avg, 2),
                "MaxWeekPoints": round(mx, 2),
                "SeasonTTL": round(season_ttl, 2) if season_ttl else 0.0,
                "SeasonAVG": round(meta.get("SeasonAVG", 0.0) or 0.0, 2),
                "SeasonGP": round(meta.get("SeasonGP", 0.0) or 0.0, 0),
                "SeasonVORP": round(meta.get("SeasonVORP", 0.0) or 0.0, 2),
                "PickupShare": round(pickup_share, 3) if season_ttl else 0.0,
            })

    # Write TSV
    out_p = OUT_DIR / "waivers.tsv"
    cols = [
        "Year","Owner","Player","Pos",
        "FirstWeek","RosterWeeks","WeeksScored",
        "PointsAfterPickup","AvgPoints","MaxWeekPoints",
        "SeasonTTL","SeasonAVG","SeasonGP","SeasonVORP","PickupShare"
    ]

    with out_p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(cols)
        for r in sorted(records, key=lambda x: (-x["Year"], -x["PointsAfterPickup"], x["Owner"], x["Player"])):
            w.writerow([r.get(c, "") for c in cols])

    print(f"✓ Wrote {out_p} ({len(records)} pickups)")

if __name__ == "__main__":
    main()
