#!/usr/bin/env python3
import csv
from pathlib import Path

# Pfade relativ zum FF-Scraping Repo
ROOT = Path(__file__).resolve().parents[1]
DRAFT_DIR = ROOT / "output" / "history-drafts"
RANKINGS_FILE = ROOT / "output" / "season_pos_rankings.csv"
OUT_DIR = ROOT / "public" / "data" / "league"
OUT_FILE = OUT_DIR / "draft_scores.tsv"

def sniff_dict_reader(path: Path):
    f = path.open("r", encoding="utf-8", newline="")
    sample = f.read(4096)
    f.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        return f, csv.DictReader(f, dialect=dialect)
    except Exception:
        f.seek(0)
        delim = "\t" if "\t" in sample else ","
        return f, csv.DictReader(f, delimiter=delim)

def to_num(x):
    if x is None: return None
    s = str(x).strip()
    if s == "" or s.upper() in {"-", "BYE"}: return None
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None

def norm_pos(p: str) -> str:
    p = (p or "").upper().strip()
    if p in {"D/ST", "DST", "DEF"}:
        return "DST"
    return p

print("=== Evaluate Drafts Script Start ===")
print(f"Reading rankings from: {RANKINGS_FILE}")
print(f"Reading drafts from:   {DRAFT_DIR}")

if not RANKINGS_FILE.exists():
    raise SystemExit("❌ Rankings file missing.")
if not DRAFT_DIR.exists():
    raise SystemExit("❌ Draft folder missing.")

# ---------- Rankings laden ----------
rankings = {}
maxrank = {}

f, reader = sniff_dict_reader(RANKINGS_FILE)
with f:
    for row in reader:
        year = row.get("Year") or row.get("year")
        pos  = row.get("Position") or row.get("Pos")
        player = row.get("Player") or row.get("Name")
        rank = row.get("Rank") or row.get("#")
        points = row.get("Points") or row.get("TTL") or row.get("Total")
        if not (year and pos and player and rank):
            continue
        try:
            y = int(year)
        except Exception:
            continue
        ppos = norm_pos(pos)
        prank = to_num(rank)
        ppoints = to_num(points)
        if prank is None:
            continue
        key = (y, ppos)
        rankings.setdefault(key, {})[player.strip().lower()] = {
            "rank": int(prank),
            "points": ppoints
        }

for key, players in rankings.items():
    maxrank[key] = max(v["rank"] for v in players.values())

print(f"Loaded rankings for {len(rankings)} (year/pos groups)")

# ---------- Drafts laden ----------
draft_rows = []

for file in sorted(DRAFT_DIR.glob("*-draft.tsv")):
    try:
        year = int(file.stem.split("-")[0])
    except Exception:
        continue

    f, reader = sniff_dict_reader(file)
    with f:
        for row in reader:
            owner = (row.get("Owner") or row.get("ManagerName") or "").strip()
            player = (row.get("Player") or "").strip()
            pos = norm_pos(row.get("Pos") or "")
            pick = to_num(row.get("Overall") or row.get("OverallPick") or row.get("Pick"))
            if not owner or not player or not pos or pick is None:
                continue

            key = (year, pos)
            pdata = rankings.get(key, {}).get(player.lower())
            if not pdata:
                continue

            end_rank = int(pdata["rank"])
            max_r = maxrank.get(key, end_rank)
            perf = max(0.0, 1.0 - (end_rank - 1) / max(1, (max_r - 1)))  # 1..0
            weight = max(0.5, 1.0 - 0.03 * (pick / 10.0))                # leichte Pick-Strafe
            score = round(10.0 * perf * weight, 2)

            draft_rows.append({
                "Year": year,
                "Owner": owner,
                "Player": player,
                "Pos": pos,
                "Pick": int(pick),
                "EndRank": end_rank,
                "Score": score
            })

print(f"Draft picks scored: {len(draft_rows)}")

# ---------- Output ----------
OUT_DIR.mkdir(parents=True, exist_ok=True)
with OUT_FILE.open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(
        f, delimiter="\t",
        fieldnames=["Year","Owner","Player","Pos","Pick","EndRank","Score"]
    )
    w.writeheader()
    for r in draft_rows:
        w.writerow(r)

print(f"✅ Wrote: {OUT_FILE}")
print("=== Evaluate Drafts Done ===")
