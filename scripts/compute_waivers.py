#!/usr/bin/env python3
import csv, re
from pathlib import Path
from collections import defaultdict

ROOT        = Path(__file__).resolve().parents[1]
TEAMGC_DIR  = ROOT / "output" / "teamgamecenter"
DRAFTS_DIR  = ROOT / "output" / "history-drafts"
OUT_DIR     = ROOT / "public" / "data" / "league"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Helpers ----------
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
        .replace("  ", " ").strip()
    )

def abbrev_key(name: str) -> str:
    """Nachname + Initiale (z. B. Mahomes_P) – für P. Mahomes ≈ Patrick Mahomes"""
    parts = norm_name(name).split()
    if not parts: return ""
    if len(parts) == 1:  # z. B. „Chiefs“
        return parts[0]
    last  = parts[-1]
    first = parts[0][0]
    return f"{last}_{first}"

name_pos_re = re.compile(
    r"^\s*([A-Za-z].*?)(?:\s+(QB|RB|WR|TE|K|DEF))?(?:\s*-\s*[A-Z]{2,3})?\s*$"
)
def parse_name_pos(cell: str):
    """
    'C. Palmer QB - ARI' → ('C. Palmer','QB')
    'Chiefs DEF' → ('Chiefs','DST')
    """
    cell = (cell or "").strip()
    if not cell or cell == "-": return ("", "")
    m = name_pos_re.match(cell)
    if not m: return (cell.strip(), "")
    name = m.group(1).strip()
    pos  = (m.group(2) or "").strip()
    if pos == "DEF": pos = "DST"
    return (name, pos)

def to_float(x):
    try: return float(str(x).replace(",", "."))
    except: return 0.0

# ---------- Readers ----------
def read_draft_keys(year: int) -> set:
    """Alle Draft-Spieler als Keys"""
    p = DRAFTS_DIR / f"{year}-draft.tsv"
    if not p.exists(): return set()
    keys = set()
    with p.open(encoding="utf-8") as f:
        r = csv.DictReader(f, delimiter="\t")
        for row in r:
            name = (row.get("Player") or "").strip()
            if not name: continue
            keys.add(norm_name(name))
            keys.add(abbrev_key(name))
    return keys

def read_week(year: int, week: int):
    fp = TEAMGC_DIR / str(year) / f"{week}.csv"
    if not fp.exists(): return []
    rows = list(csv.reader(fp.open(encoding="utf-8")))
    if not rows: return []
    header = rows[0]
    out = []
    for line in rows[1:]:
        if not line: continue
        owner = (line[0] or "").strip()
        triples = []
        i = 2
        while i + 1 < len(line) and i < len(header) - 3:
            name_cell = (line[i] or "").strip()
            pts_cell  = (line[i + 1] or "").strip()
            name, pos = parse_name_pos(name_cell)
            pts       = to_float(pts_cell)
            if name:
                triples.append((name, pos, pts))
            i += 2
        out.append({"owner": owner, "players": triples})
    return out

# ---------- Main ----------
def main():
    records = []
    years = sorted([int(p.name) for p in TEAMGC_DIR.iterdir() if p.is_dir() and p.name.isdigit()])

    for year in years:
        drafted = read_draft_keys(year)

        first_seen = defaultdict(dict)
        total_pts  = defaultdict(lambda: defaultdict(float))
        weeks_cnt  = defaultdict(lambda: defaultdict(int))
        pos_seen   = defaultdict(dict)

        for week in range(1, 17):
            for row in read_week(year, week):
                owner = row["owner"]
                for name, pos, pts in row["players"]:
                    key_f = norm_name(name)
                    key_a = abbrev_key(name)

                    # Nur undrafted
                    if key_f in drafted or key_a in drafted:
                        continue

                    # Initialisierung
                    if key_f not in first_seen[owner]:
                        first_seen[owner][key_f] = week
                    pos_seen[owner][key_f] = pos
                    # Punkte und Wochen zählen nur, wenn Punkte > 0 oder != None
                    if pts != 0:
                        weeks_cnt[owner][key_f] += 1
                        total_pts[owner][key_f] += pts

        # Ausgabe vorbereiten
        for owner in first_seen:
            for key in first_seen[owner]:
                wcnt = weeks_cnt[owner][key]
                pts  = total_pts[owner][key]
                if wcnt == 0 and pts == 0:
                    continue  # nichts gespielt
                avg = pts / wcnt if wcnt > 0 else 0
                pos = pos_seen[owner].get(key, "")
                if pos in ("K", "DST"):
                    continue  # optional: K/DST ausschließen
                player_name = " ".join(w.capitalize() for w in key.split())
                records.append({
                    "Year": year,
                    "Owner": owner,
                    "Player": player_name,
                    "Pos": pos,
                    "FirstWeek": first_seen[owner][key],
                    "WeeksPlayed": wcnt,
                    "PointsAfterPickup": round(pts, 2),
                    "AvgPoints": round(avg, 2),
                })

    # Schreiben
    out_p = OUT_DIR / "waivers.tsv"
    with out_p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow([
            "Year","Owner","Player","Pos",
            "FirstWeek","WeeksPlayed","PointsAfterPickup","AvgPoints"
        ])
        for r in sorted(records, key=lambda x: (-x["Year"], -x["PointsAfterPickup"])):
            w.writerow([
                r["Year"], r["Owner"], r["Player"], r["Pos"],
                r["FirstWeek"], r["WeeksPlayed"],
                f"{r['PointsAfterPickup']:.2f}", f"{r['AvgPoints']:.2f}"
            ])

    print(f"✓ Wrote {out_p} ({len(records)} pickups)")

if __name__ == "__main__":
    main()
