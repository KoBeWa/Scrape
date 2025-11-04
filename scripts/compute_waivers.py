#!/usr/bin/env python3
import csv, re, os
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
TEAMGC_DIR = ROOT / "output" / "teamgamecenter"
DRAFTS_DIR = ROOT / "output" / "history-drafts"
OUT_DIR = ROOT / "public" / "data" / "league"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# --------- Helpers ----------
def norm_name(s: str) -> str:
    return (
        s.lower()
        .replace(".", "")
        .replace("-", " ")
        .replace("'", "")
        .replace("’", "")
        .replace(" jr", "")
        .replace(" sr", "")
        .replace(" iii", "")
        .replace(" ii", "")
        .replace(" iv", "")
        .replace(",", "")
        .replace("  ", " ")
        .strip()
    )

# versucht aus "P. Manning QB - DEN" -> ("P Manning", "QB")
# und aus "Rams DEF" -> ("Rams", "DST")
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

def to_float(x):
    try:
        return float(str(x).replace(",", "."))
    except:
        return 0.0

def read_draft_map(year: int):
    """map: norm_player -> owner (ManagerName) & position at draft (optional)"""
    p = DRAFTS_DIR / f"{year}-draft.tsv"
    if not p.exists():
        return {}
    m = {}
    with p.open(encoding="utf-8") as f:
        r = csv.DictReader(f, delimiter="\t")
        for row in r:
            player = (row.get("Player") or "").strip()
            owner  = (row.get("ManagerName") or row.get("Owner") or "").strip()
            pos    = (row.get("Pos") or row.get("Position") or "").strip().upper()
            if player and owner:
                m[norm_name(player)] = (owner, pos)
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
    # baue (index -> ist_points) map: jede zweite Spalte Points
    # wir lesen Name/Points-Paare für alle Start/Bench Slots
    for line in rows[1:]:
        if not line:
            continue
        owner = (line[0] or "").strip()
        # sammle (player, pos, pts)
        triples = []
        i = 2  # ab Spalte 2 beginnen: QB,Points,RB,Points,...
        while i+1 < len(line) and i < len(header)-3:
            name_cell = (line[i] or "").strip()
            pts_cell  = (line[i+1] or "").strip()
            name, pos = parse_name_pos(name_cell)
            pts = to_float(pts_cell) if pts_cell not in ("", "-") else 0.0
            if name:
                triples.append((name, pos, pts))
            i += 2
        out.append({"owner": owner, "players": triples})
    return out

# --------- Scan all seasons ---------
# Kicker/DST evtl. später filtern im Frontend; hier nehmen wir erstmal alle mit.
# Definition "Pickup": Spieler erscheint bei Owner, der ihn NICHT gedraftet hat (oder gar nicht gedraftet wurde).
# Score: Summe aller Punkte ab erster Woche der Zugehörigkeit (Start+Bench in den CSVs).
records = []  # Row: Year,Owner,Player,Pos,FirstWeek,WeeksPlayed,PointsAfterPickup,WasDrafted,DraftOwner

years = sorted([int(p.name) for p in TEAMGC_DIR.iterdir() if p.is_dir() and p.name.isdigit()])
for year in years:
    draft_map = read_draft_map(year)
    # owner -> player -> first_week_seen; points_sum; weeks_count
    first_seen = defaultdict(dict)
    total_pts  = defaultdict(lambda: defaultdict(float))
    weeks_cnt  = defaultdict(lambda: defaultdict(int))
    pos_seen   = defaultdict(dict)
    draft_owner_of = {k: v[0] for k, v in draft_map.items()}

    # Wochen 1..16 (Regular Season)
    for w in range(1, 17):
        week_rows = read_week_file(year, w)
        if not week_rows:
            continue
        for row in week_rows:
            owner = row["owner"]
            for name, pos, pts in row["players"]:
                key = norm_name(name)
                if key not in pos_seen[owner] and pos:
                    pos_seen[owner][key] = pos or ""
                # first seen?
                if key not in first_seen[owner]:
                    first_seen[owner][key] = w
                # add points
                total_pts[owner][key] += float(pts)
                weeks_cnt[owner][key] += 1

    # Ableitung: Pickups = Spieler die bei diesem Owner auftauchen, aber nicht von ihm gedraftet wurden
    for owner in first_seen:
        for key, fw in first_seen[owner].items():
            draft_tup = draft_map.get(key)  # (draft_owner, pos_at_draft)
            drafted_by_owner = draft_tup and draft_tup[0] == owner
            was_drafted = bool(draft_tup)
            if drafted_by_owner:
                # kein Waiver-Pickup
                continue
            # pickup
            pts = total_pts[owner][key]
            wcnt = weeks_cnt[owner][key]
            # bestimme nice name & pos (wir nehmen den zuletzt gesehenen Originalnamen nicht mit; CSV hat nur Darstellungen)
            # wir speichern key als "Player" normalisiert zurück? Besser nicht – wir brauchen hübschen Namen:
            # heuristik: Key wieder "Titelisieren"
            def prettify(k: str) -> str:
                return " ".join([t.capitalize() for t in k.split()])
            player_nice = prettify(key)
            pos = pos_seen[owner].get(key, (draft_tup[1] if draft_tup else "")) or ""
            records.append({
                "Year": year,
                "Owner": owner,
                "Player": player_nice,
                "Pos": pos,
                "FirstWeek": fw,
                "WeeksPlayed": wcnt,
                "PointsAfterPickup": round(pts, 2),
                "WasDrafted": "Y" if was_drafted else "N",
                "DraftOwner": draft_tup[0] if draft_tup else "",
            })

# schreibe TSV
out_p = OUT_DIR / "waivers.tsv"
with out_p.open("w", encoding="utf-8", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["Year","Owner","Player","Pos","FirstWeek","WeeksPlayed","PointsAfterPickup","WasDrafted","DraftOwner"])
    # sort all-time by Points desc
    for r in sorted(records, key=lambda x: (-x["Year"], -x["PointsAfterPickup"])):
        w.writerow([
            r["Year"], r["Owner"], r["Player"], r["Pos"], r["FirstWeek"],
            r["WeeksPlayed"], f'{r["PointsAfterPickup"]:.2f}', r["WasDrafted"], r["DraftOwner"]
        ])

print(f"✓ Wrote {out_p}")
