from __future__ import annotations

from pathlib import Path
import csv
from collections import defaultdict

# === Einstellungen ===
SEASONS = range(2015, 2026)   # 2015–2025
WEEKS = range(1, 17)          # 1–16

ADD_CONTEXT_COLUMNS = True    # True => Season + Week vorne rein; False => wirklich exakt nur dein Format

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = REPO_ROOT / "output" / "teamgamecenter"
OUT = REPO_ROOT / "output" / "teamgamecenter_all_2015_2025_w1_16.csv"

# Dein gewünschter Header (aus deinem Beispiel) – inkl. mehrfach "Points"
TEMPLATE_HEADER = [
    "Owner","Rank","QB","Points","RB","Points","RB","Points","WR","Points","WR","Points",
    "TE","Points","W/R","Points","K","Points","DEF","Points",
    "BN","Points","BN","Points","BN","Points","BN","Points","BN","Points","BN","Points",
    "Total","Opponent","Opponent Total"
]

def sniff_dialect(sample_path: Path) -> csv.Dialect:
    """Auto-detect delimiter (Komma/Tab/;) aus einer Beispieldatei."""
    sample = sample_path.read_text(encoding="utf-8", errors="replace")[:4096]
    sniffer = csv.Sniffer()
    try:
        return sniffer.sniff(sample, delimiters=[",", "\t", ";"])
    except csv.Error:
        # Fallback: Tab ist bei solchen Exports häufig
        class _D(csv.excel_tab): pass
        return _D

def build_occurrence_map(header: list[str]) -> dict[tuple[str,int], int]:
    """
    Mappe (colname, occurrence_index) -> position
    Beispiel: header = ["Points","Points"] => ("Points",1)->0, ("Points",2)->1
    """
    seen = defaultdict(int)
    occ_map = {}
    for i, col in enumerate(header):
        col = col.strip()
        seen[col] += 1
        occ_map[(col, seen[col])] = i
    return occ_map

def align_row_to_template(src_header: list[str], src_row: list[str], template: list[str]) -> list[str]:
    """
    Erzeuge eine Ausgabezeile exakt in Template-Reihenfolge.
    Match nach (Spaltenname, Vorkommensnummer), damit doppelte Namen korrekt zugeordnet werden.
    """
    src_occ = build_occurrence_map([c.strip() for c in src_header])

    # für Template ebenfalls die Vorkommensnummern zählen
    tpl_seen = defaultdict(int)
    out = [""] * len(template)
    for j, col in enumerate(template):
        tpl_seen[col] += 1
        key = (col, tpl_seen[col])
        if key in src_occ:
            idx = src_occ[key]
            if idx < len(src_row):
                out[j] = src_row[idx]
    return out

def find_first_existing_file() -> Path | None:
    for season in SEASONS:
        for week in WEEKS:
            p = BASE / str(season) / f"{week}.csv"
            if p.exists():
                return p
    return None

def main() -> None:
    first = find_first_existing_file()
    if not first:
        raise SystemExit(f"Keine Dateien gefunden unter: {BASE}")

    dialect = sniff_dialect(first)

    out_header = TEMPLATE_HEADER.copy()
    if ADD_CONTEXT_COLUMNS:
        out_header = ["Season", "Week"] + out_header

    OUT.parent.mkdir(parents=True, exist_ok=True)

    rows_written = 0
    files_used = 0

    with OUT.open("w", newline="", encoding="utf-8") as f_out:
        writer = csv.writer(f_out, dialect=dialect)
        writer.writerow(out_header)

        for season in SEASONS:
            for week in WEEKS:
                path = BASE / str(season) / f"{week}.csv"
                if not path.exists():
                    continue

                with path.open("r", newline="", encoding="utf-8", errors="replace") as f_in:
                    reader = csv.reader(f_in, dialect=dialect)
                    try:
                        src_header = next(reader)
                    except StopIteration:
                        continue

                    # Header matchen (inkl. "Points" mehrfach)
                    for src_row in reader:
                        aligned = align_row_to_template(src_header, src_row, TEMPLATE_HEADER)
                        if ADD_CONTEXT_COLUMNS:
                            aligned = [str(season), str(week)] + aligned
                        writer.writerow(aligned)
                        rows_written += 1

                files_used += 1

    print(f"[ok] Dateien verwendet: {files_used}")
    print(f"[ok] Zeilen geschrieben: {rows_written}")
    print(f"[ok] Output: {OUT}")

if __name__ == "__main__":
    main()
