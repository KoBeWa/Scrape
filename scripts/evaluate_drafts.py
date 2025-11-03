#!/usr/bin/env python3
import csv, difflib, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRAFT_DIR = ROOT / "output" / "history-drafts"
RANKINGS_FILE = ROOT / "output" / "season_pos_rankings.csv"
OUT_DIR = ROOT / "public" / "data" / "league"
OUT_FILE = OUT_DIR / "draft_scores.tsv"

# -------------------- Hilfsfunktionen -------------------- #
def sniff_reader(path: Path):
    f = path.open("r", encoding="utf-8-sig", newline="")
    sample = f.read(4096)
    f.seek(0)
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
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None

def norm_pos(p: str) -> str:
    p = (p or "").upper().strip()
    if p in {"D/ST", "DEF", "DST"}: return "DST"
    if p == "PK": return "K"
    return p

def clean_name(name: str) -> str:
    """normalize player name for fuzzy matching"""
    n = name.lower()
    n = re.sub(r"[\.\'\-]", "", n)
    n = re.sub(r"\b(jr|sr|iii|ii|iv)\b", "", n)
    n = n.replace(" ", "")
    return n.strip()

# -------------------- Rankings laden -------------------- #
print("=== Evaluate Drafts Script Start ===")
rankings = {}
maxrank = {}

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
        ppoints = to_num(points)
        if prank is None: continue
        key = (y, ppos)
        rankings.setdefault(key, {})[clean_name(player)] = {"rank": int(prank), "points": ppoints or 0.0}

for key, players in rankings.items():
    maxrank[key] = max(v["rank"] for v in players.values())

print(f"Loaded {len(rankings)} position groups from rankings")

# -------------------- Drafts laden + bewerten -------------------- #
draft_rows = []

for file in sorted(DRAFT_DIR.glob("*-draft.tsv")):
    try:
        year = int(file.stem.split("-")[0])
    except:
        continue
    f, rdr = sniff_reader(file)
    with f:
        for row in rdr:
            owner  = (row.get("Owner") or row.get("ManagerName") or "").strip()
            player = (row.get("Player") or "").strip()
            pos    = norm_pos(row.get("Pos") or "")
            pick   = to_num(row.get("Overall") or row.get("OverallPick") or row.get("Pick"))
            if not owner or not player or not pos or pick is None:
                continue

            key = (year, pos)
            player_key = clean_name(player)
            group = rankings.get(key, {})
            pdata = group.get(player_key)

            # fuzzy match fallback (z.B. Odell Beckham vs. Odell Beckham Jr.)
            if not pdata and group:
                matches = difflib.get_close_matches(player_key, group.keys(), n=1, cutoff=0.85)
                if matches:
                    pdata = group[matches[0]]

            # falls immer noch nichts gefunden → 0 Punkte, sehr schlechter Rank
            if not pdata:
                end_rank = maxrank.get(key, 50) + 10
                points = 0.0
            else:
                end_rank = pdata["rank"]
                points = pdata["points"]

            max_r = maxrank.get(key, end_rank)
            total_players = max_r if max_r > 0 else 50

            # -------------- Bewertung ---------------- #
            # "Steal"-Faktor: wie stark übertroffen
            # später Pick (große Zahl) + hoher Endrang = bessere Note
            # normalisiert auf 0..10
            relative_value = max(0.0, 1.0 - (end_rank - 1) / total_players)
            draft_depth_factor = 1.0 / (pick ** 0.5)  # frühe Picks leicht bestraft
            score = 10.0 * relative_value * draft_depth_factor
            score = max(0.0, min(score, 10.0))

            draft_rows.append({
                "Year": year,
                "Owner": owner,
                "Player": player,
                "Pos": pos,
                "Pick": int(pick),
                "EndRank": end_rank,
                "Points": round(points, 2),
                "Score": round(score, 2)
            })

print(f"Draft picks scored: {len(draft_rows)}")

# -------------------- Output schreiben -------------------- #
OUT_DIR.mkdir(parents=True, exist_ok=True)
with OUT_FILE.open("w", encoding="utf-8", newline="") as f_out:
    w = csv.DictWriter(
        f_out,
        delimiter="\t",
        fieldnames=["Year","Owner","Player","Pos","Pick","EndRank","Points","Score"]
    )
    w.writeheader()
    for r in draft_rows:
        w.writerow(r)

print(f"✅ Wrote: {OUT_FILE}")
print("=== Evaluate Drafts Done ===")
