#!/usr/bin/env python3
import csv, difflib, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRAFT_DIR = ROOT / "output" / "history-drafts"
RANKINGS_FILE = ROOT / "output" / "season_pos_rankings.csv"
OUT_DIR = ROOT / "public" / "data" / "league"
OUT_FILE = OUT_DIR / "draft_scores.tsv"

# Jahre ohne finale Endrankings hier ausschließen (bei dir z. B. 2025)
EXCLUDE_YEARS = {2025}

# Positions-Gewichte (kannst du feinjustieren)
POS_WEIGHTS = {
    "RB": 1.15,
    "WR": 1.10,
    "TE": 1.00,
    "QB": 0.95,
    "DST": 0.60,
    "K" : 0.50,
}

def sniff_reader(path: Path):
    f = path.open("r", encoding="utf-8-sig", newline="")
    sample = f.read(4096); f.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        rdr = csv.DictReader(f, dialect=dialect)
    except Exception:
        f.seek(0); delim = "\t" if "\t" in sample else ","
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
    if p in {"D/ST","DEF","DST"}: return "DST"
    if p == "PK": return "K"
    return p

def clean_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"[\.\'\-]", "", n)
    n = re.sub(r"\b(jr|sr|iii|ii|iv)\b", "", n)
    n = n.replace(" ", "")
    return n.strip()

# -------------------- Rankings laden -------------------- #
rankings = {}   # (year,pos)-> {clean_name: {rank, points}}
maxrank  = {}   # (year,pos)-> max rank

f, rdr = sniff_reader(RANKINGS_FILE)
with f:
    for row in rdr:
        year = row.get("Year") or row.get("year")
        pos  = row.get("Position") or row.get("Pos")
        player = row.get("Player") or row.get("Name")
        rank = row.get("Rank") or row.get("#")
        points = row.get("Points") or row.get("TTL") or row.get("Total")
        if not (year and pos and player and rank): continue
        try: y = int(year)
        except: continue
        ppos = norm_pos(pos)
        prank = to_num(rank)
        ppoints = to_num(points) or 0.0
        if prank is None: continue
        key = (y, ppos)
        rankings.setdefault(key, {})[clean_name(player)] = {"rank": int(prank), "points": ppoints}

for key, grp in rankings.items():
    maxrank[key] = max(v["rank"] for v in grp.values()) if grp else 50

# -------------------- Scoring -------------------- #
def base_score(pick_overall: int, end_rank: int, max_r_for_pos: int) -> float:
    """
    Steal-Bias:
      - performance: 1 bei Rank 1, → 0 bei (schlecht) max_r
      - pick_weight: späte Picks werden belohnt, sehr frühe Picks nicht
    Ergebnis auf 0..10 skaliert.
    """
    max_r = max(1, max_r_for_pos)
    perf = max(0.0, 1.0 - (end_rank - 1) / max(1, (max_r - 1)))  # 1..0

    max_pick_guess = 200  # bei Bedarf anpassen
    pick_norm = max(0.0, min(1.0, (pick_overall - 1) / (max_pick_guess - 1)))
    # früh ~0.35 … spät ~1.00
    pick_weight = 0.35 + 0.65 * (pick_norm ** 0.8)

    score = 10.0 * perf * pick_weight
    return max(0.0, min(10.0, score))

def apply_pos_weight(score: float, pos: str) -> float:
    w = POS_WEIGHTS.get(pos.upper(), 1.0)
    # nach oben weiterhin bei 10 deckeln
    return max(0.0, min(10.0, round(score * w, 2)))

# -------------------- Drafts bewerten -------------------- #
draft_rows = []

for file in sorted(DRAFT_DIR.glob("*-draft.tsv")):
    try:
        year = int(file.stem.split("-")[0])
    except:
        continue
    if year in EXCLUDE_YEARS:
        continue

    f, rdr = sniff_reader(file)
    with f:
        for row in rdr:
            owner  = (row.get("Owner") or row.get("ManagerName") or "").strip()
            player = (row.get("Player") or "").strip()
            pos    = norm_pos(row.get("Pos") or row.get("Position") or "")
            pick   = to_num(row.get("Overall") or row.get("OverallPick") or row.get("Pick"))
            if not owner or not player or not pos or pick is None:
                continue
            pick = int(pick)

            key = (year, pos)
            grp = rankings.get(key, {})
            pdata = grp.get(clean_name(player))
            if not pdata and grp:
                # Fuzzy Match für Fälle wie "Odell Beckham" vs "Odell Beckham Jr."
                matches = difflib.get_close_matches(clean_name(player), grp.keys(), n=1, cutoff=0.85)
                if matches:
                    pdata = grp[matches[0]]

            if pdata:
                end_rank = int(pdata["rank"])
                pts = float(pdata["points"])
            else:
                # nicht gefunden → 0 Punkte, sehr schlechter Rank
                end_rank = maxrank.get(key, 50) + 10
                pts = 0.0

            raw = base_score(pick, end_rank, maxrank.get(key, end_rank))
            final = apply_pos_weight(raw, pos)

            draft_rows.append({
                "Year": year,
                "Owner": owner,
                "Player": player,
                "Pos": pos,
                "Pick": pick,
                "EndRank": end_rank,
                "Points": round(pts, 2),
                "Score": round(final, 2)
            })

OUT_DIR.mkdir(parents=True, exist_ok=True)
with OUT_FILE.open("w", encoding="utf-8", newline="") as f_out:
    w = csv.DictWriter(
        f_out, delimiter="\t",
        fieldnames=["Year","Owner","Player","Pos","Pick","EndRank","Points","Score"]
    )
    w.writeheader()
    for r in draft_rows:
        w.writerow(r)

print(f"✅ Wrote: {OUT_FILE} (rows={len(draft_rows)})")
