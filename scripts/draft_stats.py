#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Draft Stats Generator (Grade4)

Input:
  output/PlayerRanks_with_vorp_draftpick.csv

Output:
  output/draft_stats/
    REPORT.md
    REPORT.json
    seasons/<YEAR>.json
  output/draft_stats_bad_lines.log  (wenn kaputte CSV-Zeilen übersprungen wurden)

Features:
- Overview (All-Time): Top/Worst Picks, Best/Worst Drafts (Season+Owner), Owner-Ranking, Position Blocks
- Einzelne Seasons (2015-2024): Top/Worst Picks, Draft Ranking, Position Blocks
- Draft Stats & Records (All-Time & Season Records): wie in deiner Liste
- Robust CSV read:
    - auto delimiter detect (, ; \t |)
    - python engine fallback
    - bad lines werden geloggt und geskippt (statt Crash)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


# -----------------------------
# Robust CSV reading
# -----------------------------

def detect_delimiter(sample_text: str) -> str:
    # Try csv.Sniffer first
    try:
        sniff = csv.Sniffer().sniff(sample_text, delimiters=[",", ";", "\t", "|"])
        if sniff.delimiter:
            return sniff.delimiter
    except Exception:
        pass

    # Heuristic fallback: choose delimiter that yields most-consistent column count across sample lines
    lines = [ln for ln in sample_text.splitlines() if ln.strip()]
    if not lines:
        return ","

    candidates = [",", ";", "\t", "|"]
    best_delim = ","
    best_score = -1.0

    for d in candidates:
        header_len = len(lines[0].split(d))
        if header_len <= 1:
            continue
        same = 0
        total = 0
        for ln in lines[1:]:
            total += 1
            if len(ln.split(d)) == header_len:
                same += 1
        score = same / max(total, 1)
        if score > best_score:
            best_score = score
            best_delim = d

    return best_delim


def read_csv_robust(path: str, badlog_path: str, encoding: str = "utf-8") -> pd.DataFrame:
    p = Path(path)
    raw = p.read_text(encoding=encoding, errors="replace")

    # Remove BOM if present
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff")

    sample = "\n".join(raw.splitlines()[:300])
    sep = detect_delimiter(sample)

    # Try fast C engine first
    try:
        return pd.read_csv(path, sep=sep, engine="c", encoding=encoding)
    except Exception:
        pass

    # Fallback: python engine with bad line handling
    badlog = Path(badlog_path)
    badlog.parent.mkdir(parents=True, exist_ok=True)

    def _bad_line_handler(bad_fields: List[str]) -> Optional[List[str]]:
        with badlog.open("a", encoding="utf-8") as f:
            f.write(f"BAD LINE ({len(bad_fields)} fields): {bad_fields}\n")
        return None  # skip that line

    # pandas compatibility: callable on_bad_lines supported in newer versions
    try:
        df = pd.read_csv(
            path,
            sep=sep,
            engine="python",
            encoding=encoding,
            quoting=csv.QUOTE_MINIMAL,
            on_bad_lines=_bad_line_handler,
        )
        return df
    except TypeError:
        # Older pandas: no callable support
        df = pd.read_csv(
            path,
            sep=sep,
            engine="python",
            encoding=encoding,
            quoting=csv.QUOTE_MINIMAL,
            on_bad_lines="skip",
        )
        with badlog.open("a", encoding="utf-8") as f:
            f.write("NOTE: pandas does not support callable on_bad_lines; used on_bad_lines='skip'.\n")
        return df


# -----------------------------
# Column detection & normalization
# -----------------------------

def first_existing_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def normalize_pos(pos: Any) -> str:
    p = str(pos).strip().upper()
    if p in {"DST", "D/ST", "DEFENSE"}:
        return "DEF"
    return p


def to_numeric_grade(s: pd.Series) -> pd.Series:
    # Handles "12,3" and "12.3" and blanks -> NaN
    ss = (
        s.astype(str)
        .str.replace("\u00a0", " ", regex=False)
        .str.replace(",", ".", regex=False)
        .str.strip()
        .replace({"": None, "nan": None, "None": None})
    )
    return pd.to_numeric(ss, errors="coerce")


def safe_int(x: Any) -> Optional[int]:
    try:
        if pd.isna(x):
            return None
        return int(float(x))
    except Exception:
        return None


class Cols:
    def __init__(self, season: str, owner: str, player: str, pos: str, grade: str,
                 overall: Optional[str], rnd: Optional[str], pick_in_rnd: Optional[str],
                 nfl_team: Optional[str], keeper: Optional[str]):
        self.season = season
        self.owner = owner
        self.player = player
        self.pos = pos
        self.grade = grade
        self.overall = overall
        self.rnd = rnd
        self.pick_in_rnd = pick_in_rnd
        self.nfl_team = nfl_team
        self.keeper = keeper


def detect_columns(df: pd.DataFrame) -> Cols:
    season = first_existing_col(df, ["Year", "Season", "league_year"])
    owner = first_existing_col(df, ["ManagerName", "Owner", "manager", "TeamOwner"])
    player = first_existing_col(df, ["Player", "player_name", "Name"])
    pos = first_existing_col(df, ["Pos", "Position"])
    grade = first_existing_col(df, ["Grade4"])  # required

    missing = [k for k, v in [("season", season), ("owner", owner), ("player", player), ("pos", pos), ("grade", grade)] if v is None]
    if missing:
        raise ValueError(
            f"Missing required columns: {missing}. Found: {list(df.columns)}"
        )

    overall = first_existing_col(df, ["Overall", "DraftPick", "draft_pick", "Pick"])
    rnd = first_existing_col(df, ["Round", "draft_round"])
    pick_in_rnd = first_existing_col(df, ["PickInRound", "draft_round_pick"])
    nfl_team = first_existing_col(df, ["NFLTeam", "Team", "player_team"])
    keeper = first_existing_col(df, ["is_keeper", "IsKeeper", "keeper"])

    return Cols(season, owner, player, pos, grade, overall, rnd, pick_in_rnd, nfl_team, keeper)


# -----------------------------
# Core computations
# -----------------------------

def pick_to_dict(row: pd.Series, cols: Cols) -> Dict[str, Any]:
    return {
        "season": safe_int(row[cols.season]),
        "owner": str(row[cols.owner]),
        "player": str(row[cols.player]),
        "pos": normalize_pos(row[cols.pos]),
        "nflTeam": (str(row[cols.nfl_team]) if cols.nfl_team else None),
        "round": (safe_int(row[cols.rnd]) if cols.rnd else None),
        "pickInRound": (safe_int(row[cols.pick_in_rnd]) if cols.pick_in_rnd else None),
        "overallPick": (safe_int(row[cols.overall]) if cols.overall else None),
        "isKeeper": int(row["_is_keeper"]),
        "grade4": float(row["_grade4"]),
    }


def top_bottom_picks(df: pd.DataFrame, cols: Cols, n: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    if df.empty:
        return {"top": [], "worst": []}
    top = df.sort_values("_grade4", ascending=False).head(n)
    worst = df.sort_values("_grade4", ascending=True).head(n)
    return {
        "top": [pick_to_dict(r, cols) for _, r in top.iterrows()],
        "worst": [pick_to_dict(r, cols) for _, r in worst.iterrows()],
    }


def owner_ranking(df: pd.DataFrame, cols: Cols) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    g = (
        df.groupby(cols.owner, dropna=False)["_grade4"]
        .agg(avgGrade4="mean", picks="count")
        .reset_index()
        .sort_values(["avgGrade4", "picks"], ascending=[False, False])
    )
    return [
        {"owner": str(r[cols.owner]), "avgGrade4": float(r["avgGrade4"]), "picks": int(r["picks"])}
        for _, r in g.iterrows()
    ]


def best_worst_drafts(df: pd.DataFrame, cols: Cols, n: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    if df.empty:
        return {"best": [], "worst": []}
    g = (
        df.groupby([cols.season, cols.owner], dropna=False)["_grade4"]
        .agg(avgGrade4="mean", picks="count")
        .reset_index()
    )
    best = g.sort_values(["avgGrade4", "picks"], ascending=[False, False]).head(n)
    worst = g.sort_values(["avgGrade4", "picks"], ascending=[True, False]).head(n)

    def pack(d: pd.DataFrame) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _, r in d.iterrows():
            out.append({
                "season": safe_int(r[cols.season]),
                "owner": str(r[cols.owner]),
                "avgGrade4": float(r["avgGrade4"]),
                "picks": int(r["picks"]),
            })
        return out

    return {"best": pack(best), "worst": pack(worst)}


def pos_blocks(df: pd.DataFrame, cols: Cols, positions: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for pos in positions:
        dpos = df[df["_pos_norm"] == pos]
        out[pos] = {
            "bestPick": (pick_to_dict(dpos.sort_values("_grade4", ascending=False).iloc[0], cols) if not dpos.empty else None),
            "worstPick": (pick_to_dict(dpos.sort_values("_grade4", ascending=True).iloc[0], cols) if not dpos.empty else None),
            "ownerRanking": owner_ranking(dpos, cols),
        }
    return out


def best_owner_avg(df: pd.DataFrame, cols: Cols) -> Optional[Dict[str, Any]]:
    r = owner_ranking(df, cols)
    return r[0] if r else None


def best_season_owner_avg(df: pd.DataFrame, cols: Cols, best: bool) -> Optional[Dict[str, Any]]:
    if df.empty:
        return None
    g = (
        df.groupby([cols.season, cols.owner])["_grade4"]
        .agg(avgGrade4="mean", picks="count")
        .reset_index()
    )
    g = g.sort_values(["avgGrade4", "picks"], ascending=[not best, False])
    r = g.iloc[0]
    return {
        "season": safe_int(r[cols.season]),
        "owner": str(r[cols.owner]),
        "avgGrade4": float(r["avgGrade4"]),
        "picks": int(r["picks"]),
    }


def best_pick(df: pd.DataFrame, cols: Cols, best: bool) -> Optional[Dict[str, Any]]:
    if df.empty:
        return None
    d = df.sort_values("_grade4", ascending=not best).iloc[0]
    return pick_to_dict(d, cols)


def build_records(years_df: pd.DataFrame, cols: Cols, positions: List[str]) -> Dict[str, Any]:
    rec: Dict[str, Any] = {}

    rec["Overall Draft Value"] = best_owner_avg(years_df, cols)

    for pos in positions:
        rec[f"{pos} Draft Value"] = best_owner_avg(years_df[years_df["_pos_norm"] == pos], cols)

    rec["Keeper Draft Value"] = best_owner_avg(years_df[years_df["_is_keeper"] == 1], cols)

    rec["Overall Top Draft Season"] = best_season_owner_avg(years_df, cols, best=True)
    rec["Overall Worst Draft Season"] = best_season_owner_avg(years_df, cols, best=False)

    for pos in positions:
        rec[f"{pos} Top Draft Season"] = best_season_owner_avg(years_df[years_df["_pos_norm"] == pos], cols, best=True)
        rec[f"{pos} Worst Draft Season"] = best_season_owner_avg(years_df[years_df["_pos_norm"] == pos], cols, best=False)

    rec["Overall Best Draft Pick"] = best_pick(years_df, cols, best=True)
    rec["Overall Worst Draft Pick"] = best_pick(years_df, cols, best=False)

    for pos in positions:
        rec[f"{pos} Best Draft Pick"] = best_pick(years_df[years_df["_pos_norm"] == pos], cols, best=True)
        rec[f"{pos} Worst Draft Pick"] = best_pick(years_df[years_df["_pos_norm"] == pos], cols, best=False)

    return rec


# -----------------------------
# Markdown formatting
# -----------------------------

def md_pick(p: Optional[Dict[str, Any]]) -> str:
    if not p:
        return "—"
    parts: List[str] = []
    if p.get("season") is not None:
        parts.append(str(p["season"]))
    if p.get("round") is not None and p.get("pickInRound") is not None:
        parts.append(f"R{p['round']}.{p['pickInRound']}")
    if p.get("overallPick") is not None:
        parts.append(f"(Overall {p['overallPick']})")
    parts.append(f"{p['player']} [{p['pos']}]")
    if p.get("nflTeam"):
        parts.append(f"({p['nflTeam']})")
    parts.append(f"— {p['owner']}")
    parts.append(f"— Grade4: {p['grade4']:.3f}")
    if p.get("isKeeper"):
        parts.append("— Keeper")
    return " ".join(parts)


def md_list_picks(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "_(none)_\n"
    return "\n".join([f"{i}. {md_pick(it)}" for i, it in enumerate(items, start=1)]) + "\n"


def build_report(all_time: Dict[str, Any],
                 seasons: Dict[int, Dict[str, Any]],
                 records: Dict[str, Any],
                 min_year: int,
                 max_year: int,
                 positions: List[str],
                 bad_lines_log: str) -> str:
    md: List[str] = []
    md.append("# Draft Stats Report (Grade4)\n")
    md.append(f"Seasons included: **{min_year}–{max_year}**\n")
    md.append(f"Bad-lines log: `{bad_lines_log}` (nur wenn Zeilen kaputt waren)\n")

    md.append("## Overview (All-Time)\n")
    md.append("### Top Picks all time (top 5)\n")
    md.append(md_list_picks(all_time["topWorst"]["top"]))
    md.append("### Worst Picks all time (top 5)\n")
    md.append(md_list_picks(all_time["topWorst"]["worst"]))

    md.append("### Best all time Draft (top 5)\n")
    if all_time["bestWorstDrafts"]["best"]:
        for i, d in enumerate(all_time["bestWorstDrafts"]["best"], start=1):
            md.append(f"{i}. {d['season']} — {d['owner']} — avg Grade4: {d['avgGrade4']:.3f} (picks: {d['picks']})")
    else:
        md.append("_(none)_")
    md.append("\n### Worst all time Draft (top 5)\n")
    if all_time["bestWorstDrafts"]["worst"]:
        for i, d in enumerate(all_time["bestWorstDrafts"]["worst"], start=1):
            md.append(f"{i}. {d['season']} — {d['owner']} — avg Grade4: {d['avgGrade4']:.3f} (picks: {d['picks']})")
    else:
        md.append("_(none)_")

    md.append("\n### All time Draft Ranking (nach Owner & avg draft pick value)\n")
    if all_time["ownerRanking"]:
        for i, r in enumerate(all_time["ownerRanking"], start=1):
            md.append(f"{i}. {r['owner']} — avg Grade4: {r['avgGrade4']:.3f} (picks: {r['picks']})")
    else:
        md.append("_(none)_")

    md.append("\n### Position Overview (All-Time)\n")
    for pos in positions:
        block = all_time["positions"].get(pos, {})
        md.append(f"#### {pos}\n")
        md.append(f"- Best {pos} Pick: {md_pick(block.get('bestPick'))}")
        md.append(f"- Worst {pos} Pick: {md_pick(block.get('worstPick'))}")
        md.append(f"- Pos {pos} Draft Ranking (8 Owner):")
        ranking = block.get("ownerRanking") or []
        if ranking:
            md.extend([f"  - {i+1}. {rr['owner']} — {rr['avgGrade4']:.3f} (picks: {rr['picks']})" for i, rr in enumerate(ranking)])
        else:
            md.append("  - _(none)_")
        md.append("")

    md.append("\n## Einzelne Saisons (2015–2024)\n")
    for year in sorted(seasons.keys()):
        s = seasons[year]
        md.append(f"### Season {year}\n")

        md.append("#### Draft Stats & Records\n")
        md.append("##### Top Picks (1–5)\n")
        md.append(md_list_picks(s["topWorst"]["top"]))
        md.append("##### Worst Picks (1–5)\n")
        md.append(md_list_picks(s["topWorst"]["worst"]))

        md.append("##### Draft Ranking (1–8)\n")
        if s["ownerRanking"]:
            for i, r in enumerate(s["ownerRanking"], start=1):
                md.append(f"{i}. {r['owner']} — avg Grade4: {r['avgGrade4']:.3f} (picks: {r['picks']})")
        else:
            md.append("_(none)_")
        md.append("")

        md.append("##### Position Blocks\n")
        for pos in positions:
            block = s["positions"].get(pos, {})
            md.append(f"- **{pos}**")
            md.append(f"  - Best {pos} Pick: {md_pick(block.get('bestPick'))}")
            md.append(f"  - Worst {pos} Pick: {md_pick(block.get('worstPick'))}")
            ranking = block.get("ownerRanking") or []
            md.append(f"  - Pos {pos} Draft Ranking (8 Owner):")
            if ranking:
                md.extend([f"    - {i+1}. {rr['owner']} — {rr['avgGrade4']:.3f} (picks: {rr['picks']})" for i, rr in enumerate(ranking)])
            else:
                md.append("    - _(none)_")
        md.append("")

    md.append("\n## Draft Stats & Records (League History)\n")
    for k, v in records.items():
        if isinstance(v, dict) and "owner" in v and "avgGrade4" in v and "season" not in v:
            md.append(f"- **{k}**: {v['owner']} — avg Grade4: {v['avgGrade4']:.3f} (picks: {v['picks']})")
        elif isinstance(v, dict) and "owner" in v and "avgGrade4" in v and "season" in v:
            md.append(f"- **{k}**: {v['season']} — {v['owner']} — avg Grade4: {v['avgGrade4']:.3f} (picks: {v['picks']})")
        elif isinstance(v, dict) and "player" in v and "grade4" in v:
            md.append(f"- **{k}**: {md_pick(v)}")
        else:
            md.append(f"- **{k}**: —")

    md.append("")
    return "\n".join(md)


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="output/PlayerRanks_with_vorp_draftpick.csv")
    ap.add_argument("--outdir", default="output/draft_stats")
    ap.add_argument("--min-year", type=int, default=2015)
    ap.add_argument("--max-year", type=int, default=2024)
    ap.add_argument("--encoding", default="utf-8")
    args = ap.parse_args()

    badlog_path = "output/draft_stats_bad_lines.log"

    df = read_csv_robust(args.input, badlog_path, encoding=args.encoding)
    cols = detect_columns(df)

    # Normalize fields
    df["_grade4"] = to_numeric_grade(df[cols.grade])
    df["_pos_norm"] = df[cols.pos].apply(normalize_pos)

    if cols.keeper:
        df["_is_keeper"] = pd.to_numeric(df[cols.keeper], errors="coerce").fillna(0).astype(int)
    else:
        df["_is_keeper"] = 0

    # season numeric
    df[cols.season] = pd.to_numeric(df[cols.season], errors="coerce")
    df = df.dropna(subset=[cols.season, "_grade4"])
    df[cols.season] = df[cols.season].astype(int)

    # Filter requested years
    years_df = df[(df[cols.season] >= args.min_year) & (df[cols.season] <= args.max_year)].copy()

    # Positions to report (fixed order like your list)
    positions = ["QB", "RB", "WR", "TE", "K", "DEF"]

    # All-time overview (within year filter)
    all_time: Dict[str, Any] = {
        "topWorst": top_bottom_picks(years_df, cols, n=5),
        "bestWorstDrafts": best_worst_drafts(years_df, cols, n=5),
        "ownerRanking": owner_ranking(years_df, cols),
        "positions": pos_blocks(years_df, cols, positions),
    }

    # Per season blocks
    seasons: Dict[int, Dict[str, Any]] = {}
    for year in range(args.min_year, args.max_year + 1):
        d = years_df[years_df[cols.season] == year]
        seasons[year] = {
            "topWorst": top_bottom_picks(d, cols, n=5),
            "ownerRanking": owner_ranking(d, cols),
            "positions": pos_blocks(d, cols, positions),
        }

    # Records (league history within year filter, as requested 2015–2024)
    records = build_records(years_df, cols, positions)

    # Write outputs
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(os.path.join(args.outdir, "seasons"), exist_ok=True)

    report_json = {
        "meta": {
            "input": args.input,
            "minYear": args.min_year,
            "maxYear": args.max_year,
            "encoding": args.encoding,
            "gradeColumn": "Grade4",
            "badLinesLog": badlog_path,
        },
        "allTime": all_time,
        "seasons": seasons,
        "records": records,
    }

    with open(os.path.join(args.outdir, "REPORT.json"), "w", encoding="utf-8") as f:
        json.dump(report_json, f, ensure_ascii=False, indent=2)

    for year, block in seasons.items():
        with open(os.path.join(args.outdir, "seasons", f"{year}.json"), "w", encoding="utf-8") as f:
            json.dump(block, f, ensure_ascii=False, indent=2)

    md = build_report(all_time, seasons, records, args.min_year, args.max_year, positions, badlog_path)
    with open(os.path.join(args.outdir, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write(md)

    print(f"✅ Wrote: {os.path.join(args.outdir, 'REPORT.md')} + REPORT.json + seasons/*.json")
    if Path(badlog_path).exists() and Path(badlog_path).stat().st_size > 0:
        print(f"⚠️  Some CSV lines were skipped. See: {badlog_path}")


if __name__ == "__main__":
    main()
