#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
from pathlib import Path
from collections import defaultdict
from statistics import mean

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

def fmt_num(x, decimals=2):
    """
    Formatiert Zahlen als String mit Dezimal-Komma.
    - None/'' -> ''
    - 12.3 -> '12,30'
    """
    if x is None:
        return ""
    try:
        v = float(x)
    except:
        s = str(x).strip()
        return "" if s.lower() in ("", "nan", "none") else s

    s = f"{v:.{decimals}f}"
    return s.replace(".", ",")

def abbrev_key(name: str) -> str:
    parts = norm_name(name).split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}_{parts[0][0]}"

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
        d = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
        return d or default
    except:
        return default

def lower_map(fieldnames):
    return { (c or "").strip().lower(): c for c in (fieldnames or []) }

def pick_col(fieldnames, candidates_lower):
    """
    candidates_lower: list of lower-case candidate names
    returns actual column name from fieldnames (case exact) or None
    """
    lm = lower_map(fieldnames)
    for c in candidates_lower:
        if c in lm:
            return lm[c]
    return None

def make_player_key(name, sleeper_id, espn_id):
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
        if c.endswith("_Sleeper_ID") or c.endswith("_ESPN_ID") or c.startswith("Points"):
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

def percentile_ranks(values):
    """
    values: list[float]
    returns dict(value->percentile) is not safe for duplicates,
    so we compute percentile per index using stable sorting and then map back by index externally.
    We'll instead return a list aligned with values.
    """
    n = len(values)
    if n <= 1:
        return [1.0 for _ in values]
    idx_sorted = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    for rank, i in enumerate(idx_sorted):
        out[i] = rank / (n - 1)
    return out

# ---------------- Read PlayerRanks ----------------
def read_playerranks():
    """
    Returns:
      drafted_by_year: year -> set(keys)
      meta_by_year: year -> dict(key -> meta)
    """
    delim = sniff_delimiter(PLAYERRANKS, default=";")

    drafted_by_year = defaultdict(set)
    meta_by_year = defaultdict(dict)

    with PLAYERRANKS.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=delim)
        fns = r.fieldnames or []

        col_year   = pick_col(fns, ["year", "season"])
        col_player = pick_col(fns, ["player", "name"])
        col_pos    = pick_col(fns, ["pos", "position"])
        col_avg    = pick_col(fns, ["avg"])
        col_ttl    = pick_col(fns, ["ttl", "total"])
        col_gp     = pick_col(fns, ["gp", "games", "gamesplayed"])
        col_vorp   = pick_col(fns, ["vorp"])
        col_dp     = pick_col(fns, ["draftpick", "draft_pick", "draft overall", "draftposition", "draft_position"])

        # sleeper/espn id columns (verschiedene Varianten)
        col_sid = pick_col(fns, [
            "tabelle4.sleeper_id.1", "sleeper_id", "sleeperid", "sleeper id"
        ])
        col_eid = pick_col(fns, [
            "tabelle4.espn_id.1", "espn_id", "espnid", "espn id"
        ])

        for row in r:
            try:
                year = int(to_float(row.get(col_year)))
            except:
                year = 0
            if not year:
                continue

            player = (row.get(col_player) or "").strip() if col_player else ""
            pos    = (row.get(col_pos) or "").strip() if col_pos else ""

            sid = row.get(col_sid) if col_sid else ""
            eid = row.get(col_eid) if col_eid else ""

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

            meta = {
                "Player": player,
                "Pos": pos,
                "SeasonAVG": to_float(row.get(col_avg)) if col_avg else 0.0,
                "SeasonTTL": to_float(row.get(col_ttl)) if col_ttl else 0.0,
                "SeasonGP":  to_float(row.get(col_gp)) if col_gp else 0.0,
                "SeasonVORP": to_float(row.get(col_vorp)) if col_vorp else 0.0,
            }

            for k in keys:
                # erstes meta gewinnt (sollte identisch sein)
                meta_by_year[year].setdefault(k, meta)

            # drafted?
            draftpick_val = (row.get(col_dp) if col_dp else "")
            if str(draftpick_val).strip() not in ("", "nan", "None", "none"):
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

    # stats per year: key=(owner,pkey)
    first_week   = defaultdict(lambda: defaultdict(lambda: 99))
    roster_weeks = defaultdict(lambda: defaultdict(int))
    weeks_scored = defaultdict(lambda: defaultdict(int))
    total_pts    = defaultdict(lambda: defaultdict(float))
    max_pts      = defaultdict(lambda: defaultdict(lambda: -1e9))
    name_seen    = defaultdict(dict)

    delim_gc = sniff_delimiter(TEAMGC_ALL, default=";")

    with TEAMGC_ALL.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=delim_gc)
        header = r.fieldnames or []
        groups = detect_slot_groups(header)

        col_season = pick_col(header, ["season", "year"])
        col_week   = pick_col(header, ["week"])
        col_owner  = pick_col(header, ["owner", "teamname", "managername"])

        excluded_slots = {"K", "DST"}  # optional
        for row in r:
            try:
                year = int(to_float(row.get(col_season)))
            except:
                year = 0
            try:
                week = int(to_float(row.get(col_week)))
            except:
                week = 0
            owner = (row.get(col_owner) or "").strip() if col_owner else ""

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

                if week < first_week[year][key]:
                    first_week[year][key] = week

                roster_weeks[year][key] += 1
                name_seen[year].setdefault(pkey, name)

                # deine Logik: nur zählen, wenn pts != 0
                if pts != 0:
                    weeks_scored[year][key] += 1
                    total_pts[year][key] += pts
                    if pts > max_pts[year][key]:
                        max_pts[year][key] = pts

    # --- Build pickup records (ohne Score) ---
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

            # meta lookup
            seen_name = name_seen[year].get(pkey, "")
            meta = (
                meta_by_year.get(year, {}).get(pkey)
                or meta_by_year.get(year, {}).get(f"n:{norm_name(seen_name)}")
                or meta_by_year.get(year, {}).get(f"a:{abbrev_key(seen_name)}")
                or {}
            )

            pos = (meta.get("Pos") or "").strip()
            if pos in ("K", "DST", "DEF"):
                continue

            season_ttl = float(meta.get("SeasonTTL", 0.0) or 0.0)
            pickup_share = (pts / season_ttl) if season_ttl else 0.0
            if pickup_share < 0:
                pickup_share = 0.0
            if pickup_share > 1.0:
                pickup_share = 1.0

            reliability = (wsc / rw) if rw else 0.0
            if reliability < 0:
                reliability = 0.0
            if reliability > 1.0:
                reliability = 1.0

            # early factor: week 1 => 1.0, week 16 => 0.0625
            early = (17 - fw) / 16.0
            if early < 0:
                early = 0.0
            if early > 1.0:
                early = 1.0

            records.append({
                "Year": year,
                "Owner": owner,
                "Player": meta.get("Player") or seen_name,
                "Pos": pos,
                "FirstWeek": fw,
                "RosterWeeks": rw,
                "WeeksScored": wsc,
                "PointsAfterPickup": round(pts, 2),
                "AvgPoints": round(avg, 2),
                "MaxWeekPoints": round(mx, 2),
                "SeasonTTL": round(season_ttl, 2) if season_ttl else 0.0,
                "SeasonAVG": round(float(meta.get("SeasonAVG", 0.0) or 0.0), 2),
                "SeasonGP": round(float(meta.get("SeasonGP", 0.0) or 0.0), 0),
                "SeasonVORP": round(float(meta.get("SeasonVORP", 0.0) or 0.0), 2),
                "PickupShare": round(pickup_share, 3) if season_ttl else 0.0,
                "Reliability": round(reliability, 3),
                "EarlyFactor": round(early, 3),
                "PlayerKey": pkey,  # intern hilfreich
            })

    # --- Score berechnen (pro Jahr normalisieren) ---
    # Score = 100 * (0.50*P_pct + 0.20*A_pct + 0.15*Share + 0.10*Early + 0.05*Reliability)
    by_year = defaultdict(list)
    for rec in records:
        by_year[rec["Year"]].append(rec)

    for year, lst in by_year.items():
        P = [float(x["PointsAfterPickup"]) for x in lst]
        A = [float(x["AvgPoints"]) for x in lst]
        P_pct = percentile_ranks(P)
        A_pct = percentile_ranks(A)

        # zusätzlich pos-spezifische Perzentile (für Vergleich innerhalb Position)
        idx_by_pos = defaultdict(list)
        for i, x in enumerate(lst):
            idx_by_pos[(x["Pos"] or "").strip()].append(i)

        P_pos_pct = [0.0] * len(lst)
        A_pos_pct = [0.0] * len(lst)
        for pos, idxs in idx_by_pos.items():
            P2 = [P[i] for i in idxs]
            A2 = [A[i] for i in idxs]
            pp = percentile_ranks(P2)
            ap = percentile_ranks(A2)
            for k, i in enumerate(idxs):
                P_pos_pct[i] = pp[k]
                A_pos_pct[i] = ap[k]

        for i, x in enumerate(lst):
            share = float(x["PickupShare"])
            early = float(x["EarlyFactor"])
            rel   = float(x["Reliability"])

            score = 100.0 * (
                0.50 * P_pct[i] +
                0.20 * A_pct[i] +
                0.15 * share +
                0.10 * early +
                0.05 * rel
            )

            score_pos = 100.0 * (
                0.50 * P_pos_pct[i] +
                0.20 * A_pos_pct[i] +
                0.15 * share +
                0.10 * early +
                0.05 * rel
            )

            x["PickupScore"] = round(score, 2)
            x["PickupScorePos"] = round(score_pos, 2)

    # --- waivers.tsv schreiben ---
    out_waivers = OUT_DIR / "waivers.csv"
    cols = [
        "Year","Owner","Player","Pos",
        "FirstWeek","RosterWeeks","WeeksScored",
        "PointsAfterPickup","AvgPoints","MaxWeekPoints",
        "SeasonTTL","SeasonAVG","SeasonGP","SeasonVORP",
        "PickupShare","Reliability","EarlyFactor",
        "PickupScore","PickupScorePos"
    ]
    
    float_cols_2 = {
        "PointsAfterPickup","AvgPoints","MaxWeekPoints","SeasonTTL","SeasonAVG","SeasonVORP",
        "PickupScore","PickupScorePos"
    }
    float_cols_3 = {"PickupShare","Reliability","EarlyFactor"}
    
    with out_waivers.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cols)
    
        for r in sorted(records, key=lambda x: (-x["Year"], -x.get("PickupScore", 0), -x["PointsAfterPickup"], x["Owner"], x["Player"])):
            row_out = []
            for c in cols:
                val = r.get(c, "")
                if c in float_cols_2:
                    row_out.append(fmt_num(val, 2))
                elif c in float_cols_3:
                    row_out.append(fmt_num(val, 3))
                else:
                    # ints bleiben ints (Year/Week counts), strings bleiben strings
                    row_out.append(val)
            w.writerow(row_out)

    # --- Owner-Yearly Ranking ---
    # Kennzahlen:
    #  - TotalPickupScore, AvgPickupScore, PickupsCount
    #  - TotalPickupPoints, AvgPickupPoints
    #  - BestPickupScore, BestPickupPlayer
    #  - Top3AvgScore
    owner_rows = []
    for year, lst in by_year.items():
        by_owner = defaultdict(list)
        for r in lst:
            by_owner[r["Owner"]].append(r)

        for owner, picks in by_owner.items():
            scores = sorted([float(p["PickupScore"]) for p in picks], reverse=True)
            points = [float(p["PointsAfterPickup"]) for p in picks]

            best = max(picks, key=lambda p: float(p["PickupScore"]))
            top3 = scores[:3]
            top3_avg = mean(top3) if top3 else 0.0

            owner_rows.append({
                "Year": year,
                "Owner": owner,
                "PickupsCount": len(picks),
                "TotalPickupScore": round(sum(scores), 2),
                "AvgPickupScore": round(mean(scores), 2) if scores else 0.0,
                "Top3AvgScore": round(top3_avg, 2),
                "BestPickupScore": round(float(best["PickupScore"]), 2),
                "BestPickupPlayer": best["Player"],
                "TotalPickupPoints": round(sum(points), 2),
                "AvgPickupPoints": round(mean(points), 2) if points else 0.0,
            })

    out_owner = OUT_DIR / "waivers_owner_yearly.csv"
    owner_cols = [
        "Year","Owner",
        "PickupsCount",
        "TotalPickupScore","AvgPickupScore","Top3AvgScore",
        "BestPickupScore","BestPickupPlayer",
        "TotalPickupPoints","AvgPickupPoints",
    ]
    
    float_cols_owner = {
        "TotalPickupScore","AvgPickupScore","Top3AvgScore",
        "BestPickupScore","TotalPickupPoints","AvgPickupPoints"
    }
    
    with out_owner.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(owner_cols)
    
        for r in sorted(owner_rows, key=lambda x: (-x["Year"], -x["TotalPickupScore"], -x["TotalPickupPoints"], x["Owner"])):
            row_out = []
            for c in owner_cols:
                val = r.get(c, "")
                if c in float_cols_owner:
                    row_out.append(fmt_num(val, 2))
                else:
                    row_out.append(val)
            w.writerow(row_out)

    print(f"✓ Wrote {out_waivers} ({len(records)} pickups)")
    print(f"✓ Wrote {out_owner} ({len(owner_rows)} owner-year rows)")

if __name__ == "__main__":
    main()
