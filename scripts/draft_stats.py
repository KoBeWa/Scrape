#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Draft Stats Generator (Grade4-based)

Reads:  output/PlayerRanks_with_vorp_draftpick.csv
Writes: output/draft_stats/
  - REPORT.md                (human readable)
  - REPORT.json              (all results in one JSON)
  - seasons/<YEAR>.json      (per season details)

Robust column mapping:
- season:   Year | Season | league_year
- owner:    ManagerName | Owner | manager | TeamOwner
- player:   Player | player_name | Name
- pos:      Pos | Position
- grade:    Grade4 (required)
- overall:  Overall | DraftPick | draft_pick | Pick
- round:    Round | draft_round
- pick_in_round: PickInRound | draft_round_pick
- nfl team: NFLTeam | Team | player_team
- keeper:   is_keeper | IsKeeper | keeper

If keeper column missing -> treated as 0.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


def _first_existing_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _to_float_series(s: pd.Series) -> pd.Series:
    # accepts "12.3", "12,3", None
    return (
        s.astype(str)
        .str.replace("\u00a0", " ", regex=False)
        .str.replace(",", ".", regex=False)
        .str.strip()
        .replace({"nan": None, "None": None, "": None})
        .astype(float)
    )


def _normalize_pos(pos: str) -> str:
    p = (pos or "").strip().upper()
    if p in {"DST", "D/ST", "DEFENSE"}:
        return "DEF"
    return p


def _safe_int(x: Any) -> Optional[int]:
    try:
        if pd.isna(x):
            return None
        return int(float(x))
    except Exception:
        return None


@dataclass
class Cols:
    season: str
    owner: str
    player: str
    pos: str
    grade: str

    overall: Optional[str] = None
    rnd: Optional[str] = None
    pick_in_rnd: Optional[str] = None
    nfl_team: Optional[str] = None
    keeper: Optional[str] = None


def detect_columns(df: pd.DataFrame) -> Cols:
    season = _first_existing_col(df, ["Year", "Season", "league_year"])
    owner = _first_existing_col(df, ["ManagerName", "Owner", "manager", "TeamOwner"])
    player = _first_existing_col(df, ["Player", "player_name", "Name"])
    pos = _first_existing_col(df, ["Pos", "Position"])
    grade = _first_existing_col(df, ["Grade4"])

    missing = [k for k, v in [("season", season), ("owner", owner), ("player", player), ("pos", pos), ("grade", grade)] if v is None]
    if missing:
        raise ValueError(
            f"Missing required columns: {missing}. "
            f"Found columns: {list(df.columns)}"
        )

    return Cols(
        season=season, owner=owner, player=player, pos=pos, grade=grade,
        overall=_first_existing_col(df, ["Overall", "DraftPick", "draft_pick", "Pick"]),
        rnd=_first_existing_col(df, ["Round", "draft_round"]),
        pick_in_rnd=_first_existing_col(df, ["PickInRound", "draft_round_pick"]),
        nfl_team=_first_existing_col(df, ["NFLTeam", "Team", "player_team"]),
        keeper=_first_existing_col(df, ["is_keeper", "IsKeeper", "keeper"]),
    )


def _pick_row_to_dict(row: pd.Series, cols: Cols) -> Dict[str, Any]:
    return {
        "season": _safe_int(row[cols.season]),
        "owner": str(row[cols.owner]),
        "player": str(row[cols.player]),
        "pos": _normalize_pos(str(row[cols.pos])),
        "nflTeam": (str(row[cols.nfl_team]) if cols.nfl_team else None),
        "round": (_safe_int(row[cols.rnd]) if cols.rnd else None),
        "pickInRound": (_safe_int(row[cols.pick_in_rnd]) if cols.pick_in_rnd else None),
        "overallPick": (_safe_int(row[cols.overall]) if cols.overall else None),
        "isKeeper": (int(row["_is_keeper"]) if "_is_keeper" in row else 0),
        "grade4": float(row["_grade4"]),
    }


def _top_bottom_picks(df: pd.DataFrame, cols: Cols, n: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    if df.empty:
        return {"top": [], "worst": []}

    top = df.sort_values("_grade4", ascending=False).head(n)
    worst = df.sort_values("_grade4", ascending=True).head(n)

    return {
        "top": [_pick_row_to_dict(r, cols) for _, r in top.iterrows()],
        "worst": [_pick_row_to_dict(r, cols) for _, r in worst.iterrows()],
    }


def _owner_ranking(df: pd.DataFrame, cols: Cols) -> List[Dict[str, Any]]:
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


def _drafts_best_worst(df: pd.DataFrame, cols: Cols, n: int = 5) -> Dict[str, List[Dict[str, Any]]]:
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
        out = []
        for _, r in d.iterrows():
            out.append({
                "season": _safe_int(r[cols.season]),
                "owner": str(r[cols.owner]),
                "avgGrade4": float(r["avgGrade4"]),
                "picks": int(r["picks"]),
            })
        return out

    return {"best": pack(best), "worst": pack(worst)}


def _pos_section(df: pd.DataFrame, cols: Cols) -> Dict[str, Any]:
    # per position: best pick, worst pick, ranking by owner
    out: Dict[str, Any] = {}
    positions = sorted({ _normalize_pos(p) for p in df[cols.pos].astype(str).tolist() if str(p).strip() != "" })

    for pos in positions:
        dpos = df[df["_pos_norm"] == pos]
        out[pos] = {
            "bestPick": (_pick_row_to_dict(dpos.sort_values("_grade4", ascending=False).iloc[0], cols) if not dpos.empty else None),
            "worstPick": (_pick_row_to_dict(dpos.sort_values("_grade4", ascending=True).iloc[0], cols) if not dpos.empty else None),
            "ownerRanking": _owner_ranking(dpos, cols),
        }
    return out


def _records(all_df: pd.DataFrame, cols: Cols, years_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Computes the "Draft Stats & Records" block (all-time winners/losers).
    years_df = filtered seasons 2015-2024 (or args)
    """

    def best_owner(filter_df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        r = _owner_ranking(filter_df, cols)
        return r[0] if r else None

    def best_draft_season(filter_df: pd.DataFrame, best: bool) -> Optional[Dict[str, Any]]:
        if filter_df.empty:
            return None
        g = (
            filter_df.groupby([cols.season, cols.owner])["_grade4"]
            .agg(avgGrade4="mean", picks="count")
            .reset_index()
        )
        g = g.sort_values(["avgGrade4", "picks"], ascending=[not best, False])
        r = g.iloc[0]
        return {"season": _safe_int(r[cols.season]), "owner": str(r[cols.owner]), "avgGrade4": float(r["avgGrade4"]), "picks": int(r["picks"])}

    def best_pick(filter_df: pd.DataFrame, best: bool) -> Optional[Dict[str, Any]]:
        if filter_df.empty:
            return None
        d = filter_df.sort_values("_grade4", ascending=not best).iloc[0]
        return _pick_row_to_dict(d, cols)

    rec: Dict[str, Any] = {}

    # Overall owner averages (league history)
    rec["Overall Draft Value"] = best_owner(years_df)

    # Position owner averages
    for pos in ["QB", "RB", "WR", "TE", "K", "DEF"]:
        rec[f"{pos} Draft Value"] = best_owner(years_df[years_df["_pos_norm"] == pos])

    # Keeper owner averages
    rec["Keeper Draft Value"] = best_owner(years_df[years_df["_is_keeper"] == 1])

    # Best/Worst draft seasons (single season + owner)
    rec["Overall Top Draft Season"] = best_draft_season(years_df, best=True)
    rec["Overall Worst Draft Season"] = best_draft_season(years_df, best=False)

    for pos in ["QB", "RB", "WR", "TE", "K", "DEF"]:
        rec[f"{pos} Top Draft Season"] = best_draft_season(years_df[years_df["_pos_norm"] == pos], best=True)
        rec[f"{pos} Worst Draft Season"] = best_draft_season(years_df[years_df["_pos_norm"] == pos], best=False)

    # Best/Worst picks (single pick)
    rec["Overall Best Draft Pick"] = best_pick(years_df, best=True)
    rec["Overall Worst Draft Pick"] = best_pick(years_df, best=False)

    for pos in ["QB", "RB", "WR", "TE", "K", "DEF"]:
        rec[f"{pos} Best Draft Pick"] = best_pick(years_df[years_df["_pos_norm"] == pos], best=True)
        rec[f"{pos} Worst Draft Pick"] = best_pick(years_df[years_df["_pos_norm"] == pos], best=False)

    return rec


def _md_pick(p: Optional[Dict[str, Any]]) -> str:
    if not p:
        return "—"
    parts = []
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


def _md_table_rows(items: List[Dict[str, Any]], kind: str) -> str:
    if not items:
        return "_(none)_\n"
    lines = []
    for i, it in enumerate(items, start=1):
        lines.append(f"{i}. {_md_pick(it)}")
    return "\n".join(lines) + "\n"


def build_report(all_time: Dict[str, Any], seasons: Dict[int, Dict[str, Any]], records: Dict[str, Any], min_year: int, max_year: int) -> str:
    md = []
    md.append("# Draft Stats Report (Grade4)\n")
    md.append(f"Seasons included: **{min_year}–{max_year}**\n")

    # Overview (All-time)
    md.append("## Overview (All-Time)\n")
    md.append("### Top Picks all time (top 5)\n")
    md.append(_md_table_rows(all_time["topWorst"]["top"], "top"))
    md.append("### Worst Picks all time (top 5)\n")
    md.append(_md_table_rows(all_time["topWorst"]["worst"], "worst"))

    md.append("### Best all time Draft (top 5)\n")
    for i, d in enumerate(all_time["bestWorstDrafts"]["best"], start=1):
        md.append(f"{i}. {d['season']} — {d['owner']} — avg Grade4: {d['avgGrade4']:.3f} (picks: {d['picks']})")
    md.append("\n### Worst all time Draft (top 5)\n")
    for i, d in enumerate(all_time["bestWorstDrafts"]["worst"], start=1):
        md.append(f"{i}. {d['season']} — {d['owner']} — avg Grade4: {d['avgGrade4']:.3f} (picks: {d['picks']})")

    md.append("\n### All time Draft Ranking (Owner & avg draft pick value)\n")
    for i, r in enumerate(all_time["ownerRanking"], start=1):
        md.append(f"{i}. {r['owner']} — avg Grade4: {r['avgGrade4']:.3f} (picks: {r['picks']})")

    md.append("\n### Position Overview (All-Time)\n")
    for pos, block in all_time["positions"].items():
        md.append(f"#### {pos}\n")
        md.append(f"- Best {pos} Pick: {_md_pick(block['bestPick'])}")
        md.append(f"- Worst {pos} Pick: {_md_pick(block['worstPick'])}")
        md.append(f"- {pos} Draft Ranking (Owner avg):")
        if block["ownerRanking"]:
            md.append("\n".join([f"  - {i+1}. {rr['owner']} — {rr['avgGrade4']:.3f} (picks: {rr['picks']})" for i, rr in enumerate(block["ownerRanking"])]))
        else:
            md.append("  - _(none)_")
        md.append("")

    # Per Season
    md.append("\n## Einzelne Saisons\n")
    for year in sorted(seasons.keys()):
        s = seasons[year]
        md.append(f"### Season {year}\n")
        md.append("#### Top Picks (1–5)\n")
        md.append(_md_table_rows(s["topWorst"]["top"], "top"))
        md.append("#### Worst Picks (1–5)\n")
        md.append(_md_table_rows(s["topWorst"]["worst"], "worst"))

        md.append("#### Draft Ranking (1–8)\n")
        for i, r in enumerate(s["ownerRanking"], start=1):
            md.append(f"{i}. {r['owner']} — avg Grade4: {r['avgGrade4']:.3f} (picks: {r['picks']})")
        md.append("")

        md.append("#### Position Breakdown\n")
        for pos, block in s["positions"].items():
            md.append(f"- **{pos}**: Best: {_md_pick(block['bestPick'])} | Worst: {_md_pick(block['worstPick'])}")
            if block["ownerRanking"]:
                md.append("  - Ranking:")
                md.append("\n".join([f"    - {i+1}. {rr['owner']} — {rr['avgGrade4']:.3f} (picks: {rr['picks']})" for i, rr in enumerate(block["ownerRanking"])]))
            else:
                md.append("  - Ranking: _(none)_")
        md.append("")

    # Records
    md.append("\n## Draft Stats & Records\n")
    for k, v in records.items():
        if isinstance(v, dict) and "owner" in v and "avgGrade4" in v and "season" not in v:
            md.append(f"- **{k}**: {v['owner']} — avg Grade4: {v['avgGrade4']:.3f} (picks: {v['picks']})")
        elif isinstance(v, dict) and "owner" in v and "avgGrade4" in v and "season" in v:
            md.append(f"- **{k}**: {v['season']} — {v['owner']} — avg Grade4: {v['avgGrade4']:.3f} (picks: {v['picks']})")
        elif isinstance(v, dict) and "player" in v and "grade4" in v:
            md.append(f"- **{k}**: {_md_pick(v)}")
        else:
            md.append(f"- **{k}**: —")

    md.append("")
    return "\n".join(md)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="output/PlayerRanks_with_vorp_draftpick.csv")
    ap.add_argument("--outdir", default="output/draft_stats")
    ap.add_argument("--min-year", type=int, default=2015)
    ap.add_argument("--max-year", type=int, default=2024)
    args = ap.parse_args()

    df = pd.read_csv(args.input)
    cols = detect_columns(df)

    # normalize
    df["_grade4"] = _to_float_series(df[cols.grade])
    df["_pos_norm"] = df[cols.pos].astype(str).map(_normalize_pos)

    if cols.keeper:
        df["_is_keeper"] = pd.to_numeric(df[cols.keeper], errors="coerce").fillna(0).astype(int)
    else:
        df["_is_keeper"] = 0

    df[cols.season] = pd.to_numeric(df[cols.season], errors="coerce")
    df = df.dropna(subset=[cols.season, "_grade4"])
    df[cols.season] = df[cols.season].astype(int)

    # filter years for stats/records
    years_df = df[(df[cols.season] >= args.min_year) & (df[cols.season] <= args.max_year)].copy()

    # all-time overview (within selected year range)
    all_time: Dict[str, Any] = {
        "topWorst": _top_bottom_picks(years_df, cols, n=5),
        "bestWorstDrafts": _drafts_best_worst(years_df, cols, n=5),
        "ownerRanking": _owner_ranking(years_df, cols),
        "positions": _pos_section(years_df, cols),
    }

    # per-season blocks
    seasons: Dict[int, Dict[str, Any]] = {}
    for year in range(args.min_year, args.max_year + 1):
        d = years_df[years_df[cols.season] == year]
        seasons[year] = {
            "topWorst": _top_bottom_picks(d, cols, n=5),
            "ownerRanking": _owner_ranking(d, cols),
            "positions": _pos_section(d, cols),
        }

    # records
    records = _records(df, cols, years_df)

    # write outputs
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(os.path.join(args.outdir, "seasons"), exist_ok=True)

    report_json = {
        "meta": {
            "input": args.input,
            "minYear": args.min_year,
            "maxYear": args.max_year,
            "gradeColumn": "Grade4",
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

    md = build_report(all_time, seasons, records, args.min_year, args.max_year)
    with open(os.path.join(args.outdir, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write(md)

    print(f"✅ Wrote: {os.path.join(args.outdir, 'REPORT.md')} and REPORT.json + seasons/*.json")


if __name__ == "__main__":
    main()
