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
    """Nachname + Initiale des Vornamens: 'Mahomes_P' – robust für 'P. Mahomes'."""
    parts = norm_name(name).split()
    if not parts: return ""
    if len(parts) == 1:  # z.B. 'Chiefs'
        return parts[0]
    last  = parts[-1]
    first = parts[0][0]
    return f"{last}_{first}"

name_pos_re = re.compile(
    r"^\s*([A-Za-z].*?)(?:\s+(QB|RB|WR|TE|K|DEF))?(?:\s*-\s*[A-Z]{2,3})?\s*$"
)
def parse_name_pos(cell: str):
    """
    'C. Palmer QB - ARI' -> ('C. Palmer','QB')
    'Chiefs DEF'         -> ('Chiefs','DEF' -> 'DST')
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

# ---------- IO ----------
def read_draft_keys_for_year(year: int) -> set:
    """Alle Draft-Spieler eines Jahres als Keys (voll & abgekürzt)"""
    p = DRAFTS_DIR / f"{year}-draft.tsv"
    if not p.exists(): return set()
    keys = set()
    with p.open(encoding="utf-8") as f:
        r = csv.DictReader(f, delimiter="\t")
        for row in r:
            player = (row.get("Player") or "").strip()
            if not player: continue
            keys.add(norm_name(player))
            keys.add(abbrev_key(player))
    return keys

def read_week_rows(year: int, week: int):
    """Liest eine Teamgamecenter-CSV (Owner, Slots, Bench …, Total, Opponent, Opp Total)."""
    fp = TEAMGC_DIR / str(year) / f"{week}.csv"
    if not fp.exists(): return []
    with fp.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows: return []
    header = rows[0]
    out = []
    for line in rows[1:]:
        if not line: continue
        owner = (line[0] or "").strip()
        triples = []
        i = 2  # ab Spalte 2: QB,Points,RB,Points,…
        # bis vor den letzten drei Spalten (Total, Opponent, OppTotal)
        while i + 1 < len(line) and i < len(header) - 3:
            name_cell = (line[i] or "").strip()
            pts_cell  = (line[i+1] or "").strip()
            name, pos = parse_name_pos(name_cell)
            pts       = to_float(pts_cell) if pts_cell not in ("", "-") else 0.0
            if name:
                triples.append((name, pos, pts))
            i += 2
        out.append({"owner": owner, "players": triples})
    return out

# ---------- Main ----------
def main():
    records = []
    years = sorted([int(p.name) for p in TEAMGC_DIR.iterdir()
                    if p.is_dir() and p.name.isdigit()])

    for year in years:
        drafted_keys = read_draft_keys_for_year(year)  # alle gedrafteten (egal von wem)

        # Tracking ab erster Sichtung beim Owner
        first_seen = defaultdict(dict)                 # owner -> key_full -> first_week
        sum_pts    = defaultdict(lambda: defaultdict(float))
        weeks_cnt  = defaultdict(lambda: defaultdict(int))
        pos_seen   = defaultdict(dict)                # owner -> key_full -> pos

        for w in range(1, 17):  # Regular Season
            week_rows = read_week_rows(year, w)
            if not week_rows: continue
            for row in week_rows:
                owner = row["owner"]
                for name, pos, pts in row["players"]:
                    key_full = norm_name(name)
                    key_abbr = abbrev_key(name)

                    # **Nur undrafted** zählen: kommt in keinem Draft des Jahres vor
                    if key_full in drafted_keys or key_abbr in drafted_keys:
                        continue

                    if key_full not in first_seen[owner]:
                        first_seen[owner][key_full] = w
                    # Position merken, falls vorhanden
                    if pos and key_full not in pos_seen[owner]:
                        pos_seen[owner][key_full] = pos
                    sum_pts[owner][key_full] += pts
                    weeks_cnt[owner][key_full] += 1

        # Ausgabe
        for owner in first_seen:
            for key, fw in first_seen[owner].items():
                pts  = sum_pts[owner][key]
                wcnt = weeks_cnt[owner][key]
                pos  = pos_seen[owner].get(key, "")
                # Optional: K/DST rausfiltern – hier auskommentiert; bei Bedarf aktivieren:
                # if pos in ("K","DST"): continue

                pretty = " ".join(t.capitalize() for t in key.split())
                records.append({
                    "Year": year,
                    "Owner": owner,
                    "Player": pretty,
                    "Pos": pos,
                    "FirstWeek": fw,
                    "WeeksPlayed": wcnt,
                    "PointsAfterPickup": round(pts, 2)
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
            avg = r["PointsAfterPickup"] / r["WeeksPlayed"] if r["WeeksPlayed"] else 0.0
            w.writerow([
                r["Year"], r["Owner"], r["Player"], r["Pos"],
                r["FirstWeek"], r["WeeksPlayed"],
                f'{r["PointsAfterPickup"]:.2f}', f'{avg:.2f}'
            ])
    print(f"✓ Wrote {out_p} ({len(records)} pickups)")

if __name__ == "__main__":
    main()
