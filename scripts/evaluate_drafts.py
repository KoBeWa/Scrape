#!/usr/bin/env python3
import csv
from pathlib import Path
from statistics import mean

# INPUTS
DRAFT_DIR = Path("output/history-drafts")
RANKINGS_FILE = Path("output/season_pos_rankings.csv")
OUT_DIR = Path("public/data/league")
OUT_FILE = OUT_DIR / "draft_scores.tsv"

# --- Helper ---
def to_num(x):
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return None

# load rankings
rankings = {}
with RANKINGS_FILE.open("r", encoding="utf-8") as f:
    reader = csv.DictReader(f, delimiter="\t" if "\t" in f.readline() else ",")
    for row in reader:
        year = int(row["Year"])
        pos = row["Position"].strip()
        player = row["Player"].strip().lower()
        rank = to_num(row["Rank"])
        pts = to_num(row["Points"])
        if not rank:
            continue
        rankings.setdefault((year, pos), {})[player] = {
            "rank": rank, "points": pts
        }

# max ranks per pos per year
maxrank = {k: max(v["rank"] for v in pos.values()) for k, pos in rankings.items()}

# load all drafts
drafts = []
for file in DRAFT_DIR.glob("*-draft.tsv"):
    year = int(file.stem.split("-")[0])
    with file.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            owner = row.get("Owner") or row.get("ManagerName") or ""
            player = (row.get("Player") or "").strip().lower()
            pos = (row.get("Pos") or row.get("Position") or "").strip()
            pick = to_num(row.get("OverallPick") or row.get("Pick") or row.get("Overall"))
            if not pos or not pick or not player:
                continue
            key = (year, pos)
            rank_data = rankings.get(key, {}).get(player)
            if not rank_data:
                continue
            end_rank = rank_data["rank"]
            max_r = maxrank.get(key, 50)
            # score 0–10
            perf = max(0.0, 1 - (end_rank - 1) / (max_r - 1))
            weight = max(0.5, 1 - 0.03 * (pick / 10))  # leichte Abwertung früher Picks
            score = round(10 * perf * weight, 2)
            drafts.append({
                "Year": year,
                "Owner": owner,
                "Player": row["Player"],
                "Pos": pos,
                "Pick": int(pick),
                "EndRank": int(end_rank),
                "Score": score
            })

OUT_DIR.mkdir(parents=True, exist_ok=True)
with OUT_FILE.open("w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, delimiter="\t", fieldnames=["Year","Owner","Player","Pos","Pick","EndRank","Score"])
    writer.writeheader()
    for d in drafts:
        writer.writerow(d)

print(f"✅ Saved draft scores: {len(drafts)} picks → {OUT_FILE}")
