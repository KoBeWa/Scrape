#!/usr/bin/env python3
import csv
from pathlib import Path

# Pfade relativ zum FF-Scraping Repo (ff-data)
DRAFT_DIR = Path("output/history-drafts")
RANKINGS_FILE = Path("output/season_pos_rankings.csv")
OUT_DIR = Path("public/data/league")
OUT_FILE = OUT_DIR / "draft_scores.tsv"

def sniff_reader(path: Path):
    """Open a CSV/TSV with auto delimiter detection (',', '\\t', ';')."""
    f = path.open("r", encoding="utf-8", newline="")
    sample = f.read(4096)
    f.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        return f, csv.DictReader(f, dialect=dialect)
    except Exception:
        f.seek(0)
        # fallback: tab, dann komma
        text = sample
        delim = "\t" if "\t" in text else ","
        return f, csv.DictReader(f, delimiter=delim)

def to_num(x):
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.upper() in {"-", "BYE"}:
        return None
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None

def normalize_pos(p: str) -> str:
    p = (p or "").upper().strip()
    # kleine Normalisierung
    if p in {"D/ST", "DST", "DEF"}:
        return "DST"
    return p

# ---------- Rankings laden ----------
print(f"Loading rankings: {RANKINGS_FILE}")
fr, reader = sniff_reader(RANKINGS_FILE)

rankings = {}   # key: (year, pos) -> { player_lower: {rank, points} }
maxrank = {}    # key: (year, pos) -> max rank for that group

with fr:
    for row in reader:
        # Header-Varianten abfangen
        year = row.get("Year") or row.get("year")
        pos  = row.get("Position") or row.get("Pos")
        player = row.get("Player") or row.get("Name")
        rank = row.get("Rank") or row.get("#")
        points = row.get("Points") or row.get("TTL") or row.get("Total")

        if not year or not pos or not player or not rank:
            continue

        try:
            y = int(str(year).strip())
        except Exception:
            continue

        ppos = normalize_pos(pos)
        prank = to_num(rank)
        ppoints = to_num(points)

        if prank is None:
            continue

        key = (y, ppos)
        d = rankings.setdefault(key, {})
        d[player.strip().lower()] = {"rank": prank, "points": ppoints}

# max Rank je (Year,Pos)
for key, players in rankings.items():
    try:
        maxrank[key] = int(max(v["rank"] for v in players.values()))
    except ValueError:
        maxrank[key] = 50

print(f"Groups loaded: {len(rankings)} (with max ranks)")

# ---------- Drafts laden & bewerten ----------
draft_rows = []

def draft_rows_from_file(path: Path):
    f, rdr = sniff_reader(path)
    with f:
        for row in rdr:
            yield row

for file in sorted(DRAFT_DIR.glob("*-draft.tsv")):
    try:
        year = int(file.stem.split("-")[0])
    except Exception:
        continue

    for row in draft_rows_from_file(file):
        owner  = (row.get("Owner") or row.get("ManagerName") or "").strip()
        player = (row.get("Player") or "").strip()
        pos    = normalize_pos(row.get("Pos") or row.get("Position") or "")
        pick   = to_num(row.get("OverallPick") or row.get("Pick") or row.get("Overall"))

        if not owner or not player or not pos or pick is None:
            continue

        key = (year, pos)
        pkey = player.lower()
        rank_data = rankings.get(key, {}).get(pkey)
        if not rank_data:
            # kein Saisonrang gefunden -> skip
            continue

        end_rank = int(rank_data["rank"])
        max_r = maxrank.get(key, 50)

        # Performance skaliert 0..1 (1 = Rank1, 0 = letzter)
        perf = max(0.0, 1.0 - (end_rank - 1) / max(1, (max_r - 1)))

        # leichte Abwertung extrem früher Picks (damit „späte Steals“ nicht benachteiligt werden)
        weight = max(0.5, 1.0 - 0.03 * (pick / 10.0))

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
    w = csv.DictWriter(f, delimiter="\t",
                       fieldnames=["Year","Owner","Player","Pos","Pick","EndRank","Score"])
    w.writeheader()
    for r in draft_rows:
        w.writerow(r)

print(f"✅ Saved: {OUT_FILE}")
