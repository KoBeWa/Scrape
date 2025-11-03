#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate_drafts.py
Bewertet alle Drafts seit 2015 anhand der saisonalen Spielergebnisse.
- berücksichtigt Positionsrang (Draft vs. Endrang)
- späte Hits werden höher gewertet
- frühe Busts härter bestraft
- Positionsgewichte
- 0 Punkte = Score 0.0
"""

import csv, math, re, difflib
from pathlib import Path

# === Pfade ===
ROOT = Path(__file__).resolve().parents[1]
DRAFT_DIR = ROOT / "output" / "history-drafts"
RANKINGS_FILE = ROOT / "output" / "season_pos_rankings.csv"
OUT_DIR = ROOT / "public" / "data" / "league"
OUT_FILE = OUT_DIR / "draft_scores.tsv"

# === Einstellungen ===
EXCLUDE_YEARS = {2025}  # Jahr ohne finale Rankings
POS_W = {"RB": 1.20, "WR": 1.00, "QB": 0.70, "TE": 0.80, "DST": 0.30, "K": 0.30}
SOFTSIGN_C = 18.0  # steuert Stärke der Skalierung (niedriger = extremer)

# === Helper ===
def sniff_reader(path: Path):
    f = path.open("r", encoding="utf-8-sig", newline="")
    sample = f.read(4096); f.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        rdr = csv.DictReader(f, dialect=dialect)
    except Exception:
        f.seek(0)
        delim = "\t" if "\t" in sample else ","
        rdr = csv.DictReader(f, delimiter=delim)
    if rdr.fieldnames:
        rdr.fieldnames = [(h or "").lstrip("\ufeff").strip() for h in rdr.fieldnames]
    return f, rdr

def to_num(x):
    if x is None: return None
    s = str(x).strip()
    if s == "" or s.upper() in {"-", "BYE"}: return None
    try: return float(s.replace(",", "."))
    except: return None

def norm_pos(p: str) -> str:
    p = (p or "").upper().strip()
    if p in {"D/ST", "DEF", "DST"}: return "DST"
    if p == "PK": return "K"
    return p

def clean_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"[\.\'\-]", "", n)
    n = re.sub(r"\b(jr|sr|iii|ii|iv)\b", "", n)
    n = n.replace(" ", "")
    return n.strip()

# === Rankings laden ===
rankings = {}
maxrank = {}

f, rdr = sniff_reader(RANKINGS_FILE)
with f:
    for row in rdr:
        year = row.get("Year") or row.get("year")
        pos = row.get("Position") or row.get("Pos")
        player = row.get("Player") or row.get("Name")
        rank = row.get("Rank") or row.get("#")
        points = row.get("Points") or row.get("TTL") or row.get("Total")
        if not (year and pos and player and rank):
            continue
        try:
            y = int(year)
        except:
            continue
        ppos = norm_pos(pos)
        prank = to_num(rank)
        ppoints = to_num(points) or 0.0
        if prank is None:
            continue
        key = (y, ppos)
        rankings.setdefault(key, {})[clean_name(player)] = {
            "rank": int(prank),
            "points": ppoints,
        }

for key, grp in rankings.items():
    maxrank[key] = max(v["rank"] for v in grp.values()) if grp else 50

# === Draft-Positions-Ränge bestimmen ===
def compute_draft_pos_ranks(rows):
    def overall_of(r): return to_num(r.get("Overall") or r.get("OverallPick") or r.get("Pick")) or 0
    sorted_idx = sorted(range(len(rows)), key=lambda i: overall_of(rows[i]))
    counters, result = {}, {}
    for idx in sorted_idx:
        pos = norm_pos(rows[idx].get("Pos") or rows[idx].get("Position") or "")
        if not pos:
            result[idx] = None
            continue
        counters[pos] = counters.get(pos, 0) + 1
        result[idx] = counters[pos]
    return result

# === Scoring ===
def draft_capital_factor(overall_pick: int) -> float:
    return math.log(max(1.0, overall_pick) + 10.0)

def squash_softsign(x: float, C: float = SOFTSIGN_C) -> float:
    return 5.0 + 5.0 * (x / (abs(x) + C))

def compute_pick_score(delta_pos_rank: int, overall_pick: int, pos: str, points: float) -> float:
    if points == 0.0:
        return 0.0  # Nur echte 0-Punkte-Spieler haben Score 0.0

    cap = draft_capital_factor(overall_pick)
    raw = delta_pos_rank * cap
    weighted = raw * POS_W.get(pos.upper(), 1.0)
    score = squash_softsign(weighted, SOFTSIGN_C)
    if delta_pos_rank > 0:
        score *= 1.05  # Bonus für gute Steals
    return round(max(0.0, min(10.0, score)), 2)

# === Hauptlogik ===
draft_rows_out = []

for file in sorted(DRAFT_DIR.glob("*-draft.tsv")):
    try:
        year = int(file.stem.split("-")[0])
    except:
        continue
    if year in EXCLUDE_YEARS:
        continue

    f, rdr = sniff_reader(file)
    with f:
        draft_rows = [row for row in rdr]

    draft_pos_rank_map = compute_draft_pos_ranks(draft_rows)

    for idx, row in enumerate(draft_rows):
        owner = (row.get("Owner") or row.get("ManagerName") or "").strip()
        player = (row.get("Player") or "").strip()
        pos = norm_pos(row.get("Pos") or row.get("Position") or "")
        overall = to_num(row.get("Overall") or row.get("OverallPick") or row.get("Pick"))
        if not owner or not player or not pos or overall is None:
            continue
        overall = int(overall)
        draft_pos_rank = draft_pos_rank_map.get(idx) or 999

        key = (year, pos)
        grp = rankings.get(key, {})
        pdata = grp.get(clean_name(player))
        if not pdata and grp:
            matches = difflib.get_close_matches(clean_name(player), grp.keys(), n=1, cutoff=0.85)
            if matches:
                pdata = grp[matches[0]]

        if pdata:
            EndRank = int(pdata["rank"])
            points = float(pdata["points"])
        else:
            EndRank = maxrank.get(key, 50) + 10
            points = 0.0

        delta_pos_rank = draft_pos_rank - EndRank
        score = compute_pick_score(delta_pos_rank, overall, pos, points)

        draft_rows_out.append({
            "Year": year,
            "Owner": owner,
            "Player": player,
            "Pos": pos,
            "Pick": overall,
            "Draft_Pos_Rank": draft_pos_rank,
            "EndRank": endrank,
            "Delta_Pos_Rank": delta_pos_rank,
            "Points": round(points, 2),
            "Score": score,
        })

# === Ausgabe ===
OUT_DIR.mkdir(parents=True, exist_ok=True)
with OUT_FILE.open("w", encoding="utf-8", newline="") as f_out:
    w = csv.DictWriter(
        f_out,
        delimiter="\t",
        fieldnames=[
            "Year",
            "Owner",
            "Player",
            "Pos",
            "Pick",
            "Draft_Pos_Rank",
            "EndRank",
            "Delta_Pos_Rank",
            "Points",
            "Score",
        ],
    )
    w.writeheader()
    for r in draft_rows_out:
        w.writerow(r)

print(f"✅ Wrote: {OUT_FILE} (rows={len(draft_rows_out)})")
