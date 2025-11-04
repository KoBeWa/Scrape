#!/usr/bin/env python3
import csv, re, os
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
TEAMGC_DIR = ROOT / "output" / "teamgamecenter"
DRAFTS_DIR = ROOT / "output" / "history-drafts"
OUT_DIR = ROOT / "public" / "data" / "league"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Helpers ----------
def norm_name(s: str) -> str:
    """normalize name for comparison"""
    return (
        s.lower()
        .replace(".", "")
        .replace("-", " ")
        .replace("'", "")
        .replace("’", "")
        .replace(",", "")
        .replace(" jr", "")
        .replace(" sr", "")
        .replace(" iii", "")
        .replace(" ii", "")
        .replace(" iv", "")
        .replace("  ", " ")
        .strip()
    )

def to_float(x):
    try:
        return float(str(x).replace(",", "."))
    except:
        return 0.0

# extract first initial + last name (for fuzzy match)
def abbrev_key(name: str):
    parts = norm_name(name).split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return parts[-1] + "_" + parts[0][0]  # "Mahomes_P"

name_pos_re = re.compile(r"^\s*([A-Za-z].*?)(?:\s+(QB|RB|WR|TE|K|DEF))?(?:\s*-\s*[A-Z]{2,3})?\s*$")

def parse_name_pos(cell: str):
    cell = (cell or "").strip()
    if not cell or cell == "-":
        return ("", "")
    m = name_pos_re.match(cell)
    if not m:
        return (cell.strip(), "")
    name = m.group(1).strip()
    pos = (m.group(2) or "").strip()
    if pos == "DEF":
        pos = "DST"
    return (name, pos)

def read_draft_map(year: int):
    """map both full and abbreviated versions"""
    p = DRAFTS_DIR / f"{year}-draft.tsv"
    if not p.exists():
        return {}
    m = {}
    with p.open(encoding="utf-8") as f:
        r = csv.DictReader(f, delimiter="\t")
        for row in r:
            player = (row.get("Player") or "").strip()
            pos = (row.get("Pos") or row.get("Position") or "").strip().upper()
            owner = (row.get("ManagerName") or row.get("Owner") or "").strip()
            if not player:
                continue
            key_full = norm_name(player)
            key_abbr = abbrev_key(player)
            m[key_full] = (owner, pos)
            m[key_abbr] = (owner, pos)
    return m

def read_week_file(year: int, week: int):
    fp = TEAMGC_DIR / str(year) / f"{week}.csv"
    if not fp.exists():
        return []
    out = []
    with fp.open(encoding="utf-8") as f:
        r = csv.reader(f)
        rows = list(r)
    if not rows:
        return []
    header = rows[0]
    for line in rows[1:]:
        if not line:
            continue
        owner = (line[0] or "").strip()
        triples = []
        i = 2
        while i + 1 < len(line) and i < len(header) - 3:
            name_cell = (line[i] or "").strip()
            pts_cell = (line[i + 1] or "").strip()
            name, pos = parse_name_pos(name_cell)
            pts = to_float(pts_cell) if pts_cell not in ("", "-") else 0.0
            if name:
                triples.append((name, pos, pts))
            i += 2
        out.append({"owner": owner, "players": triples})
    return out

# ---------- Main ----------
records = []

years = sorted([int(p.name) for p in TEAMGC_DIR.iterdir() if p.is_dir() and p.name.isdigit()])

for year in years:
    draft_map = read_draft_map(year)
    all_drafted_keys = set(draft_map.keys())

    first_seen = defaultdict(dict)
    total_pts = defaultdict(lambda: defaultdict(float))
    weeks_cnt = defaultdict(lambda: defaultdict(int))
    pos_seen = defaultdict(dict)

    for w in range(1, 17):
        week_rows = read_week_file(year, w)
        if not week_rows:
            continue
        for row in week_rows:
            owner = row["owner"]
            for name, pos, pts in row["players"]:
                key_full = norm_name(name)
                key_abbr = abbrev_key(name)

                # skip drafted players: we want only undrafted pickups
                if key_full in all_drafted_keys or key_abbr in all_drafted_keys:
                    continue

                if key_full not in first_seen[owner]:
                    first_seen[owner][key_full] = w
                pos_seen[owner][key_full] = pos
                total_pts[owner][key_full] += pts
                weeks_cnt[owner][key_full] += 1

    for owner in first_seen:
        for key, fw in first_seen[owner].items():
            pts = total_pts[owner][key]
            wcnt = weeks_cnt[owner][key]
            pos = pos_seen[owner].get(key, "")
            if pos in ("K", "DST"):
                continue  # optional filter
            player_pretty = " ".join(p.capitalize() for p in key.split())
            records.append({
                "Year": year,
                "Owner": owner,
                "Player": player_pretty,
                "Pos": pos,
                "FirstWeek": fw,
                "WeeksPlayed": wcnt,
                "PointsAfterPickup": round(pts, 2)
            })

# write TSV
out_p = OUT_DIR / "waivers.tsv"
with out_p.open("w", encoding="utf-8", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["Year","Owner","Player","Pos","FirstWeek","WeeksPlayed","PointsAfterPickup"])
    for r in sorted(records, key=lambda x: (-x["Year"], -x["PointsAfterPickup"])):
        w.writerow([
            r["Year"], r["Owner"], r["Player"], r["Pos"], r["FirstWeek"],
            r["WeeksPlayed"], f'{r["PointsAfterPickup"]:.2f}'
        ])

print(f"✓ Wrote {out_p} ({len(records)} pickups)")
