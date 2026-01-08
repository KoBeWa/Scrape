from __future__ import annotations

from pathlib import Path
import csv
from collections import defaultdict

# =========================
# Konfiguration
# =========================
SEASONS = range(2015, 2026)   # 2015–2025
WEEKS = range(1, 17)          # 1–16

ADD_CONTEXT_COLUMNS = True    # True => Season+Week vorne rein (empfohlen)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = REPO_ROOT / "output" / "teamgamecenter"
OUT = REPO_ROOT / "output" / "teamgamecenter_all_2015_2025_w1_16.csv"

# Ziel-Format: "Superset" mit 7 Bench-Spots (ab neueren Saisons)
# Hinweis: In 2020/2021 heißt der zusätzliche Spot oft "RES" -> wird beim Einlesen zu "BN" normalisiert.
OUTPUT_TEMPLATE = [
    "Owner","Rank","QB","Points","RB","Points","RB","Points","WR","Points","WR","Points",
    "TE","Points","W/R","Points","K","Points","DEF","Points",
    "BN","Points","BN","Points","BN","Points","BN","Points","BN","Points","BN","Points","BN","Points",
    "Total","Opponent","Opponent Total"
]

# =========================
# Helper
# =========================
def sniff_dialect(sample_path: Path) -> csv.Dialect:
    """Auto-detect delimiter (Komma/Tab/;) aus einer Beispieldatei."""
    sample = sample_path.read_text(encoding="utf-8", errors="replace")[:4096]
    sniffer = csv.Sniffer()
    try:
        return sniffer.sniff(sample, delimiters=[",", "\t", ";"])
    except csv.Error:
        # Fallback: Tab (bei euch sehr häufig)
        class _D(csv.excel_tab): pass
        return _D


def build_occurrence_map(header: list[str]) -> dict[tuple[str, int], int]:
    """
    Mappe (colname, occurrence_index) -> position.
    Wichtig bei mehrfach identischen Spaltennamen (z.B. 'Points').
    """
    seen = defaultdict(int)
    occ_map: dict[tuple[str, int], int] = {}
    for i, col in enumerate(header):
        col = col.strip()
        seen[col] += 1
        occ_map[(col, seen[col])] = i
    return occ_map


def align_row(src_header: list[str], src_row: list[str], out_template: list[str]) -> list[str]:
    """
    Erzeuge Ausgabezeile exakt in out_template-Reihenfolge.
    Zuordnung über (Spaltenname, Vorkommensnummer).
    """
    src_occ = build_occurrence_map([c.strip() for c in src_header])

    tpl_seen = defaultdict(int)
    out = [""] * len(out_template)

    for j, col in enumerate(out_template):
        tpl_seen[col] += 1
        key = (col, tpl_seen[col])
        if key in src_occ:
            idx = src_occ[key]
            if idx < len(src_row):
                out[j] = src_row[idx]
    return out


def normalize_header(header: list[str]) -> list[str]:
    """
    Normalisiert Besonderheiten im Header:
    - 'RES' wird als zusätzlicher Bench-Slot behandelt => zu 'BN' umbenennen
    - Whitespace trimmen
    """
    norm = []
    for c in header:
        c = c.strip()
        if c == "RES":
            c = "BN"
        norm.append(c)
    return norm


def find_first_existing_file() -> Path | None:
    for season in SEASONS:
        for week in WEEKS:
            p = BASE / str(season) / f"{week}.csv"
            if p.exists():
                return p
    return None


def count_bench_slots(header: list[str]) -> int:
    """Zählt BN-Spalten (Bench-Slots) im Header."""
    return sum(1 for c in header if c.strip() == "BN")


# =========================
# Main
# =========================
def main() -> None:
    first = find_first_existing_file()
    if not first:
        raise SystemExit(f"Keine Dateien gefunden unter: {BASE}")

    dialect = sniff_dialect(first)

    out_header = OUTPUT_TEMPLATE.copy()
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
                        src_header_raw = next(reader)
                    except StopIteration:
                        continue

                    src_header = normalize_header(src_header_raw)

                    # Optional: Plausibilitätscheck (nur Logging)
                    bn_slots = count_bench_slots(src_header)
                    if bn_slots not in (6, 7):
                        print(f"[warn] {path}: ungewöhnliche Bench-Slots (BN/RES) = {bn_slots}")

                    for src_row in reader:
                        aligned = align_row(src_header, src_row, OUTPUT_TEMPLATE)

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
