from __future__ import annotations

from pathlib import Path
import re
import pandas as pd

RE_SEASON = re.compile(r"^\d{4}$")
RE_WEEK = re.compile(r"^\d+\.csv$", re.IGNORECASE)

ROOT = Path(__file__).resolve().parents[1]  # Repo-Root, wenn Script in scripts/ liegt
BASE = ROOT / "output" / "teamgamecenter"

SEASONS = range(2015, 2026)   # 2015–2025
MAX_WEEK = 16                # nur Weeks 1–16 (ändern auf 18, wenn du alles willst)

out_path = ROOT / "output" / "teamgamecenter_all_2015_2025_w1_16.csv"

frames: list[pd.DataFrame] = []
all_cols: set[str] = set()

for season in SEASONS:
    season_dir = BASE / str(season)
    if not season_dir.exists():
        print(f"[skip] season dir missing: {season_dir}")
        continue

    # alle *.csv im Season-Ordner, die nach Woche aussehen
    for f in sorted(season_dir.glob("*.csv"), key=lambda p: int(p.stem) if p.stem.isdigit() else 9999):
        if not RE_WEEK.match(f.name):
            continue
        if not f.stem.isdigit():
            continue
        week = int(f.stem)
        if week < 1 or week > MAX_WEEK:
            continue

        try:
            df = pd.read_csv(f)  # falls Semikolon: pd.read_csv(f, sep=";")
        except Exception as e:
            raise RuntimeError(f"Failed reading {f}: {e}") from e

        df.insert(0, "Week", week)
        df.insert(0, "Season", season)

        frames.append(df)
        all_cols.update(df.columns)

if not frames:
    raise SystemExit("No files found to merge. Check paths / seasons / week range.")

# Spalten vereinheitlichen (Union), damit nichts verloren geht, auch wenn Jahre leicht abweichen
all_cols = list(all_cols)
# Season/Week nach vorn
front = ["Season", "Week"]
rest = [c for c in all_cols if c not in front]
cols_order = front + rest

merged = pd.concat([f.reindex(columns=cols_order) for f in frames], ignore_index=True)

out_path.parent.mkdir(parents=True, exist_ok=True)
merged.to_csv(out_path, index=False)

print(f"[ok] merged rows: {len(merged):,}")
print(f"[ok] wrote: {out_path}")
