#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


# ============================================================
# CONFIG
# ============================================================

DEFAULTS = {
    # weekly award tiers (e.g. 8 teams -> top 3 / bottom 3)
    "top_tier_count": 3,
    "bottom_tier_count": 3,

    # blowout/narrow margins
    "blowout_margin": 30,
    "narrow_margin": 5,

    # playoffs begin week 15 -> regular season weeks = 14
    "default_regular_season_weeks": 14,
    "regular_season_weeks": {},  # optional: {"2015": 14, ...}

    # playoff settings (for appearances from reg-season rank)
    "playoff_teams": 4,

    # medal scoring
    "medal_points": {"1": 3, "2": 2, "3": 1},
}


def load_config(repo_root: Path) -> dict:
    cfg = dict(DEFAULTS)
    cfg_path = repo_root / "config" / "league_records.yml"
    if not cfg_path.exists():
        return cfg
    if yaml is None:
        return cfg

    with cfg_path.open("r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}

    cfg.update(user_cfg)
    cfg["regular_season_weeks"] = {**DEFAULTS.get("regular_season_weeks", {}), **(user_cfg.get("regular_season_weeks", {}) or {})}
    cfg["medal_points"] = {**DEFAULTS.get("medal_points", {}), **(user_cfg.get("medal_points", {}) or {})}
    return cfg


# ============================================================
# CSV NORMALIZATION (your header is fixed, but robust anyway)
# ============================================================

def norm_col(c: str) -> str:
    c = str(c).strip().lower()
    c = re.sub(r"[\s\-\/]+", "_", c)
    c = re.sub(r"[^a-z0-9_\.]", "", c)
    return c


def to_float(x) -> float:
    if pd.isna(x):
        return float("nan")
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", ".")
    s = re.sub(r"[^\d\.\-]", "", s)
    try:
        return float(s)
    except Exception:
        return float("nan")


def to_int(x) -> Optional[int]:
    if pd.isna(x):
        return None
    try:
        v = int(float(str(x).strip().replace(",", ".")))
        return v
    except Exception:
        return None


@dataclass
class TeamGame:
    season: int
    week: int
    team: str
    opponent: str
    points_for: float
    points_against: float
    rank: Optional[int]
    is_playoff: bool
    matchup_key: str


def make_matchup_key(season: int, week: int, team: str, opp: str) -> str:
    a, b = sorted([team.strip().lower(), opp.strip().lower()])
    return f"{season}-{week}-{a}__vs__{b}"


def infer_is_playoff(season: int, week: int, cfg: dict) -> bool:
    reg_map = cfg.get("regular_season_weeks", {}) or {}
    reg_weeks = int(reg_map.get(str(season), cfg.get("default_regular_season_weeks", 14)))
    return week > reg_weeks


def normalize_week_csv(df: pd.DataFrame, season: int, week: int, cfg: dict) -> List[TeamGame]:
    """
    Supports your schema:
      Owner, Rank, ..., Total, Opponent, Opponent Total
    Ignores duplicate "Points" columns (pandas will make them points, points.1, ...)
    """
    df = df.copy()
    df.columns = [norm_col(c) for c in df.columns]

    # Required in your export
    # owner -> team
    # opponent -> opponent
    # total -> pf
    # opponent_total -> pa
    required = ["owner", "opponent", "total", "opponent_total"]
    for r in required:
        if r not in df.columns:
            raise ValueError(f"Missing required column '{r}' in season={season} week={week}. Found: {list(df.columns)}")

    out: List[TeamGame] = []
    is_po = infer_is_playoff(season, week, cfg)

    for _, row in df.iterrows():
        team = str(row["owner"]).strip()
        opp = str(row["opponent"]).strip()
        pf = to_float(row["total"])
        pa = to_float(row["opponent_total"])
        rk = to_int(row["rank"]) if "rank" in df.columns else None

        out.append(
            TeamGame(
                season=season,
                week=week,
                team=team,
                opponent=opp,
                points_for=pf,
                points_against=pa,
                rank=rk,
                is_playoff=is_po,
                matchup_key=make_matchup_key(season, week, team, opp),
            )
        )
    return out


# ============================================================
# METRICS
# ============================================================

def compute_week_awards(tg: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Computes weekly ranks, high/low, top/bottom tier, top/bottom half.
    Done per (season, week) across ALL teams (regular+playoff weeks as they are in that week).
    """
    tg = tg.copy()
    tg["points_for"] = tg["points_for"].astype(float)

    tg["week_team_count"] = tg.groupby(["season", "week"])["team"].transform("nunique").astype(int)
    tg["week_rank"] = (
        tg.groupby(["season", "week"])["points_for"]
        .rank(method="min", ascending=False)
        .astype(int)
    )

    top_n = int(cfg.get("top_tier_count", 3))
    bottom_n = int(cfg.get("bottom_tier_count", 3))

    tg["is_week_high"] = tg["week_rank"] == 1
    tg["is_week_low"] = tg["week_rank"] == tg["week_team_count"]

    tg["is_top_tier"] = tg["week_rank"] <= top_n
    tg["is_bottom_tier"] = tg["week_rank"] > (tg["week_team_count"] - bottom_n)

    tg["is_top_half"] = tg["week_rank"] <= tg["week_team_count"].apply(lambda n: int(math.ceil(n / 2)))
    tg["is_bottom_half"] = ~tg["is_top_half"]

    return tg


def compute_all_play(tg: pd.DataFrame) -> pd.DataFrame:
    """
    All-play per week: each team plays every other team that week.
    Ties get 0.5 win / 0.5 loss vs tied teams.
    """
    tg = tg.copy()

    def _per_week(g: pd.DataFrame) -> pd.DataFrame:
        pts = g["points_for"].astype(float).values
        n = len(pts)

        wins = []
        losses = []
        for i in range(n):
            p = pts[i]
            less = (pts < p).sum()
            greater = (pts > p).sum()
            equal = (pts == p).sum() - 1  # exclude self
            w = float(less) + 0.5 * float(equal)
            l = float(greater) + 0.5 * float(equal)
            wins.append(w)
            losses.append(l)

        out = g.copy()
        out["all_play_wins"] = wins
        out["all_play_losses"] = losses
        out["all_play_win_pct"] = out["all_play_wins"] / (n - 1 if n > 1 else 1)
        return out

    tg = tg.groupby(["season", "week"], group_keys=False).apply(_per_week)
    return tg


def compute_matchup_flags(tg: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    tg = tg.copy()

    tg["margin"] = (tg["points_for"] - tg["points_against"]).astype(float)
    tg["abs_margin"] = tg["margin"].abs()

    tg["is_win"] = tg["margin"] > 0
    tg["is_loss"] = tg["margin"] < 0
    tg["is_tie"] = tg["margin"] == 0

    denom = (tg["points_for"] + tg["points_against"]).replace(0, float("nan"))
    tg["points_share"] = tg["points_for"] / denom
    tg["opp_points_share"] = tg["points_against"] / denom

    blowout = float(cfg.get("blowout_margin", 30))
    narrow = float(cfg.get("narrow_margin", 5))

    tg["is_blowout_win"] = tg["is_win"] & (tg["abs_margin"] >= blowout)
    tg["is_blowout_loss"] = tg["is_loss"] & (tg["abs_margin"] >= blowout)
    tg["is_narrow_win"] = tg["is_win"] & (tg["abs_margin"] <= narrow)
    tg["is_narrow_loss"] = tg["is_loss"] & (tg["abs_margin"] <= narrow)

    return tg


# ============================================================
# SEASON + CAREER SUMMARIES
# ============================================================

def season_team_summary(tg: pd.DataFrame) -> pd.DataFrame:
    """
    Regular season only aggregation by (season, team).
    """
    reg = tg[~tg["is_playoff"]].copy()

    reg["w"] = reg["is_win"].astype(int)
    reg["l"] = reg["is_loss"].astype(int)
    reg["t"] = reg["is_tie"].astype(int)

    st = reg.groupby(["season", "team"], as_index=False).agg(
        wins=("w", "sum"),
        losses=("l", "sum"),
        ties=("t", "sum"),
        points_for=("points_for", "sum"),
        points_against=("points_against", "sum"),

        points_share_avg=("points_share", "mean"),
        opp_points_share_avg=("opp_points_share", "mean"),

        high_scores=("is_week_high", "sum"),
        top_scores=("is_top_tier", "sum"),
        top_half_scores=("is_top_half", "sum"),
        worst_scores=("is_week_low", "sum"),
        bottom_scores=("is_bottom_tier", "sum"),
        bottom_half_scores=("is_bottom_half", "sum"),

        blowout_wins=("is_blowout_win", "sum"),
        blowout_losses=("is_blowout_loss", "sum"),
        narrow_wins=("is_narrow_win", "sum"),
        narrow_losses=("is_narrow_loss", "sum"),

        all_play_wins=("all_play_wins", "sum"),
        all_play_losses=("all_play_losses", "sum"),
    )

    st["games"] = st["wins"] + st["losses"] + st["ties"]
    st["win_pct"] = (st["wins"] + 0.5 * st["ties"]) / st["games"].replace(0, float("nan"))

    ap_games = (st["all_play_wins"] + st["all_play_losses"]).replace(0, float("nan"))
    st["all_play_win_pct"] = st["all_play_wins"] / ap_games

    # winning/losing record flags
    st["winning_record"] = st["wins"] > st["losses"]
    st["losing_record"] = st["wins"] < st["losses"]

    return st


def playoff_team_summary(tg: pd.DataFrame) -> pd.DataFrame:
    po = tg[tg["is_playoff"]].copy()
    if po.empty:
        return pd.DataFrame(columns=["season", "team", "playoff_wins", "playoff_losses", "playoff_points_for", "playoff_points_against"])

    po["w"] = po["is_win"].astype(int)
    po["l"] = po["is_loss"].astype(int)

    return po.groupby(["season", "team"], as_index=False).agg(
        playoff_wins=("w", "sum"),
        playoff_losses=("l", "sum"),
        playoff_points_for=("points_for", "sum"),
        playoff_points_against=("points_against", "sum"),
    )


def compute_strength_of_schedule(st: pd.DataFrame, tg: pd.DataFrame) -> pd.DataFrame:
    """
    SOS = average opponents' regular-season season points_for.
    """
    reg = tg[~tg["is_playoff"]].copy()
    pts_map = st.set_index(["season", "team"])["points_for"].to_dict()

    def opp_season_points(row) -> float:
        return float(pts_map.get((row["season"], row["opponent"]), float("nan")))

    reg["opp_season_points_for"] = reg.apply(opp_season_points, axis=1)
    sos = reg.groupby(["season", "team"], as_index=False)["opp_season_points_for"].mean().rename(columns={"opp_season_points_for": "sos_avg_opp_points"})

    return st.merge(sos, on=["season", "team"], how="left")


def compute_luck(tg: pd.DataFrame) -> pd.DataFrame:
    """
    Luck per matchup (regular season only):
      luck_week = actual_win_value - all_play_win_pct
    """
    reg = tg[~tg["is_playoff"]].copy()
    reg["actual_win_value"] = reg["is_win"].astype(float) + 0.5 * reg["is_tie"].astype(float)
    reg["luck_week"] = reg["actual_win_value"] - reg["all_play_win_pct"].astype(float)
    return reg.groupby("team", as_index=False)["luck_week"].mean().rename(columns={"luck_week": "luck_avg"})


# ============================================================
# RANK-BASED TITLES / MEDALS / PLAYOFF APPEARANCES
# ============================================================

def rank_snapshots(tg: pd.DataFrame, cfg: dict) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
      reg_rank: ranks at end of regular season (week = reg_end_week)
      final_rank: final ranks at last week available per season
    """
    if "rank" not in tg.columns:
        return pd.DataFrame(), pd.DataFrame()

    r = tg[["season", "week", "team", "rank"]].dropna(subset=["rank"]).copy()
    if r.empty:
        return pd.DataFrame(), pd.DataFrame()

    r["rank"] = r["rank"].astype(int)

    # regular season end week per season
    reg_end_default = int(cfg.get("default_regular_season_weeks", 14))
    reg_map = cfg.get("regular_season_weeks", {}) or {}

    def reg_end(season: int) -> int:
        return int(reg_map.get(str(season), reg_end_default))

    r["reg_end_week"] = r["season"].apply(reg_end)
    reg_rank = r[r["week"] == r["reg_end_week"]].drop(columns=["reg_end_week"]).copy()

    # final week per season
    final_week = r.groupby("season")["week"].max().to_dict()

    finals = []
    for (season, team), g in r.groupby(["season", "team"]):
        fw = final_week.get(season)
        rr = g[g["week"] == fw]["rank"]
        if not rr.empty:
            finals.append({"season": int(season), "team": team, "final_week": int(fw), "final_rank": int(rr.iloc[0])})
    final_rank = pd.DataFrame(finals)

    return reg_rank, final_rank


def add_title_blocks(career: pd.DataFrame, st: pd.DataFrame, tg: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Adds:
      regular_season_titles (rank==1 at week 14)
      playoff_appearances (rank <= playoff_teams at week 14)
      championships (final_rank==1)
      title_games (final_rank<=2)
      total_medals (final_rank<=3)
      medal_score (medal_points map)
      season_points_titles (max points_for regular season in season)
      season_all_play_titles (max all_play_wins regular season in season)
    """
    playoff_teams = int(cfg.get("playoff_teams", 4))
    medal_points = cfg.get("medal_points", {"1": 3, "2": 2, "3": 1})

    reg_rank, final_rank = rank_snapshots(tg, cfg)

    # --- Rank-based season outcomes
    title_df = None
    if not reg_rank.empty:
        reg_rank = reg_rank.copy()
        reg_rank["is_reg_title"] = reg_rank["rank"] == 1
        reg_rank["is_po_app"] = reg_rank["rank"] <= playoff_teams

        t1 = reg_rank.groupby("team", as_index=False)["is_reg_title"].sum().rename(columns={"is_reg_title": "regular_season_titles"})
        t2 = reg_rank.groupby("team", as_index=False)["is_po_app"].sum().rename(columns={"is_po_app": "playoff_appearances"})
        title_df = t1.merge(t2, on="team", how="outer").fillna(0)

    champ_df = None
    if not final_rank.empty:
        fr = final_rank.copy()
        fr["is_champ"] = fr["final_rank"] == 1
        fr["is_title_game"] = fr["final_rank"] <= 2
        fr["is_medal"] = fr["final_rank"] <= 3
        fr["medal_score"] = fr["final_rank"].astype(str).map(medal_points).fillna(0).astype(int)

        champ_df = fr.groupby("team", as_index=False).agg(
            championships=("is_champ", "sum"),
            title_games=("is_title_game", "sum"),
            total_medals=("is_medal", "sum"),
            medal_score=("medal_score", "sum"),
        ).fillna(0)

    # --- Season titles by points / all-play (computed from regular season totals)
    # season_points_titles: max points_for within season
    points_titles = []
    allplay_titles = []
    for season, g in st.groupby("season"):
        g2 = g.copy()
        if not g2.empty:
            pmax = g2["points_for"].max()
            winners = g2[g2["points_for"] == pmax]["team"].tolist()
            for w in winners:
                points_titles.append({"team": w, "season_points_titles": 1})

            apmax = g2["all_play_wins"].max()
            apwinners = g2[g2["all_play_wins"] == apmax]["team"].tolist()
            for w in apwinners:
                allplay_titles.append({"team": w, "season_all_play_titles": 1})

    pts_df = pd.DataFrame(points_titles).groupby("team", as_index=False)["season_points_titles"].sum() if points_titles else pd.DataFrame(columns=["team", "season_points_titles"])
    ap_df = pd.DataFrame(allplay_titles).groupby("team", as_index=False)["season_all_play_titles"].sum() if allplay_titles else pd.DataFrame(columns=["team", "season_all_play_titles"])

    # Merge into career
    for add in [title_df, champ_df, pts_df, ap_df]:
        if add is not None and not add.empty:
            career = career.merge(add, on="team", how="left")

    for col in ["regular_season_titles", "playoff_appearances", "championships", "title_games", "total_medals", "medal_score", "season_points_titles", "season_all_play_titles"]:
        if col not in career.columns:
            career[col] = 0
        career[col] = career[col].fillna(0).astype(int)

    return career


# ============================================================
# RECORD BUILDERS
# ============================================================

def pick_leader(df: pd.DataFrame, col: str, higher_is_better: bool = True) -> pd.Series:
    if df.empty or col not in df.columns:
        return pd.Series(dtype=object)
    if higher_is_better:
        idx = df[col].astype(float).idxmax()
    else:
        idx = df[col].astype(float).idxmin()
    return df.loc[idx]


def league_records(tg: pd.DataFrame, st: pd.DataFrame, po: pd.DataFrame, cfg: dict) -> List[dict]:
    """
    Career totals/averages across seasons (regular season only for W/L/PF/PA, etc).
    """
    # base career from st
    career = st.groupby("team", as_index=False).agg(
        total_wins=("wins", "sum"),
        total_losses=("losses", "sum"),
        total_ties=("ties", "sum"),
        total_points=("points_for", "sum"),
        total_opp_points=("points_against", "sum"),

        points_share_avg=("points_share_avg", "mean"),
        opp_points_share_avg=("opp_points_share_avg", "mean"),

        all_play_wins=("all_play_wins", "sum"),
        all_play_losses=("all_play_losses", "sum"),

        high_scores=("high_scores", "sum"),
        top_scores=("top_scores", "sum"),
        top_half_scores=("top_half_scores", "sum"),
        worst_scores=("worst_scores", "sum"),
        bottom_scores=("bottom_scores", "sum"),
        bottom_half_scores=("bottom_half_scores", "sum"),

        blowout_wins=("blowout_wins", "sum"),
        blowout_losses=("blowout_losses", "sum"),
        narrow_wins=("narrow_wins", "sum"),
        narrow_losses=("narrow_losses", "sum"),

        seasons_winning_record=("winning_record", "sum"),
        seasons_losing_record=("losing_record", "sum"),
        seasons_played=("season", "nunique"),
    )

    career["games"] = career["total_wins"] + career["total_losses"] + career["total_ties"]
    career["win_pct"] = (career["total_wins"] + 0.5 * career["total_ties"]) / career["games"].replace(0, float("nan"))

    ap_games = (career["all_play_wins"] + career["all_play_losses"]).replace(0, float("nan"))
    career["all_play_win_pct"] = career["all_play_wins"] / ap_games

    # Luck + SOS
    luck = compute_luck(tg)
    career = career.merge(luck, on="team", how="left")

    if "sos_avg_opp_points" in st.columns:
        sos_c = st.groupby("team", as_index=False)["sos_avg_opp_points"].mean()
        career = career.merge(sos_c, on="team", how="left")

    # Titles/Medals
    career = add_title_blocks(career, st, tg, cfg)

    # Playoff totals
    if not po.empty:
        po_c = po.groupby("team", as_index=False).agg(
            playoff_wins=("playoff_wins", "sum"),
            playoff_losses=("playoff_losses", "sum"),
            total_playoff_points=("playoff_points_for", "sum"),
            total_playoff_opp_points=("playoff_points_against", "sum"),
        )
        career = career.merge(po_c, on="team", how="left")

    for col in ["playoff_wins", "playoff_losses", "total_playoff_points", "total_playoff_opp_points"]:
        if col not in career.columns:
            career[col] = 0
        career[col] = career[col].fillna(0)

    # Helper to add one record
    out: List[dict] = []

    def add(key: str, desc: str, col: str, higher=True):
        row = pick_leader(career, col, higher)
        if row.empty:
            return
        val = row[col]
        out.append({"record": key, "description": desc, "leader": row["team"], "value": float(val) if pd.notna(val) else None})

    add("Total Wins", "Most total wins in league history.", "total_wins", True)
    add("Total Losses", "Most total losses in league history.", "total_losses", True)
    add("Win Percent", "Best win percentage in league history.", "win_pct", True)

    add("All Play Wins", "Most total all-play wins (if playing every team each week) in league history.", "all_play_wins", True)
    add("All Play Losses", "Most total all-play losses (if playing every team each week) in league history.", "all_play_losses", True)
    add("All Play Win Percent", "Best all-play win percentage in league history.", "all_play_win_pct", True)

    add("Total Points", "Most total points scored in league history.", "total_points", True)
    add("Total Opponent Points", "Most total opponent points allowed in league history.", "total_opp_points", True)

    add("Points Share Average", "Highest average share of total points scored in matchups over league history.", "points_share_avg", True)
    add("Opponent Points Share Average", "Highest average share of total points scored by opponents over league history.", "opp_points_share_avg", True)

    add("Luckiest", "Luckiest member in league history (based on opponent performance).", "luck_avg", True)
    add("Luckiest (Least)", "Unluckiest member in league history (based on opponent performance).", "luck_avg", False)

    if "sos_avg_opp_points" in career.columns:
        add("Strength of Schedule", "Toughest average strength of schedule (highest opponents’ points rank) in league history.", "sos_avg_opp_points", True)
        add("Strength of Schedule (Easiest)", "Easiest average strength of schedule (lowest opponents’ points rank) in league history.", "sos_avg_opp_points", False)

    # weekly award counts + percents
    def add_pct(key: str, desc: str, count_col: str):
        tmp = career.copy()
        tmp[key] = tmp[count_col] / tmp["games"].replace(0, float("nan"))
        row = pick_leader(tmp, key, True)
        if row.empty:
            return
        out.append({"record": key, "description": desc, "leader": row["team"], "value": float(row[key]) if pd.notna(row[key]) else None})

    add("High Scores", "Most matchups with the weekly high score in league history.", "high_scores", True)
    add_pct("High Scores Percent", "Highest percentage of matchups with the weekly high score in league history.", "high_scores")

    add("Top Scores", "Most matchups with a top score (top tier) in league history.", "top_scores", True)
    add_pct("Top Scores Percent", "Highest percentage of matchups with a top score in league history.", "top_scores")

    add("Top Half Scores", "Most matchups finishing in the top half of weekly scores in league history.", "top_half_scores", True)
    add_pct("Top Half Score Percent", "Highest percentage of matchups finishing in the top half of weekly scores.", "top_half_scores")

    add("Worst Scores", "Most matchups with the lowest score of the week in league history.", "worst_scores", True)
    add_pct("Worst Scores Percent", "Highest percentage of matchups with the lowest score of the week.", "worst_scores")

    add("Bottom Scores", "Most matchups with a bottom score (bottom tier) in league history.", "bottom_scores", True)
    add_pct("Bottom Scores Percent", "Highest percentage of matchups with a bottom score in league history.", "bottom_scores")

    add("Bottom Half Scores", "Most matchups finishing in the bottom half of weekly scores in league history.", "bottom_half_scores", True)
    add_pct("Bottom Half Score Percent", "Highest percentage of matchups finishing in the bottom half of weekly scores.", "bottom_half_scores")

    add("Blowout Wins", "Most wins by a large margin in league history.", "blowout_wins", True)
    add("Blowout Losses", "Most losses by a large margin in league history.", "blowout_losses", True)
    add("Narrow Wins", "Most wins by a narrow margin in league history.", "narrow_wins", True)
    add("Narrow Losses", "Most losses by a narrow margin in league history.", "narrow_losses", True)

    # titles/medals/playoffs
    add("Regular Season Titles", "Most regular-season titles in league history.", "regular_season_titles", True)
    add("Season Points Titles", "Most seasons leading the regular season in points scored.", "season_points_titles", True)
    add("Season All Play Titles", "Most seasons with the best all-play record in league history.", "season_all_play_titles", True)

    add("Seasons Winning Record", "Most seasons with an overall winning record in league history.", "seasons_winning_record", True)
    add("Seasons Losing Record", "Most seasons with an overall losing record in league history.", "seasons_losing_record", True)

    add("Championships", "Most league championships in league history.", "championships", True)
    add("Medal Score", "Best medal score over league history (points based on final rank).", "medal_score", True)
    add("Total Medals", "Most total medals (finishes counted as medal ranks) in league history.", "total_medals", True)

    add("Playoff Appearances", "Most playoff appearances in league history.", "playoff_appearances", True)
    add("Playoff Wins", "Most playoff wins in league history.", "playoff_wins", True)
    add("Playoff Losses", "Most playoff losses in league history.", "playoff_losses", True)
    add("Total Playoff Points", "Most total playoff points scored in league history.", "total_playoff_points", True)
    add("Total Playoff Opponent Points", "Most total playoff points allowed to opponents in league history.", "total_playoff_opp_points", True)

    return out


def season_records(st: pd.DataFrame, tg: pd.DataFrame, cfg: dict) -> List[dict]:
    """
    Season best/worst records (single season).
    Uses regular season only (st is regular season summary).
    """
    out: List[dict] = []

    def add(key: str, desc: str, df: pd.DataFrame, col: str, higher=True):
        row = pick_leader(df, col, higher)
        if row.empty:
            return
        out.append({
            "record": key,
            "description": desc,
            "season": int(row["season"]),
            "leader": row["team"],
            "value": float(row[col]) if pd.notna(row[col]) else None,
        })

    # base
    add("Season Score", "Highest season score in a single season.", st, "points_for", True)
    add("Season Score (Lowest)", "Lowest season score in a single season.", st, "points_for", False)

    # season luck (regular season only)
    reg = tg[~tg["is_playoff"]].copy()
    reg["actual_win_value"] = reg["is_win"].astype(float) + 0.5 * reg["is_tie"].astype(float)
    reg["luck_week"] = reg["actual_win_value"] - reg["all_play_win_pct"].astype(float)
    sl = reg.groupby(["season", "team"], as_index=False)["luck_week"].mean().rename(columns={"luck_week": "season_luck"})
    st2 = st.merge(sl, on=["season", "team"], how="left")

    add("Season Luckiest", "Luckiest team in a season (based on opponents’ performance).", st2, "season_luck", True)
    add("Season Luckiest (Lowest)", "Unluckiest team in a season (based on opponents’ performance).", st2, "season_luck", False)

    if "sos_avg_opp_points" in st.columns:
        add("Strength of Schedule", "Toughest strength of schedule in a season (highest opponents’ points rank).", st, "sos_avg_opp_points", True)
        add("Strength of Schedule (Easiest)", "Easiest strength of schedule in a season (lowest opponents’ points rank).", st, "sos_avg_opp_points", False)

    add("Most Wins", "Most wins in a single season.", st, "wins", True)
    add("Most Losses", "Most losses in a single season.", st, "losses", True)
    add("Most All Play Wins", "Most all-play wins (vs every team each week) in a season.", st, "all_play_wins", True)
    add("Most All Play Losses", "Most all-play losses (vs every team each week) in a season.", st, "all_play_losses", True)
    add("Best All Play Win Percent", "Best all-play win percentage in a season.", st, "all_play_win_pct", True)

    add("Most Points", "Most points scored in a season.", st, "points_for", True)
    add("Most Opponent Points", "Most opponent points allowed in a season.", st, "points_against", True)
    add("Fewest Points", "Fewest points scored in a season.", st, "points_for", False)
    add("Fewest Opponent Points", "Fewest opponent points allowed in a season.", st, "points_against", False)

    add("Highest Points Share", "Highest points share in a season (share of total points scored in matchups).", st, "points_share_avg", True)
    add("Lowest Points Share", "Lowest / worst points share in a season.", st, "points_share_avg", False)
    add("Highest Opponent Points Share", "Highest opponents’ points share in a season.", st, "opp_points_share_avg", True)
    add("Lowest Opponent Points Share", "Lowest / worst opponents’ points share in a season.", st, "opp_points_share_avg", False)

    add("High Scores", "Most matchups finishing with the weekly high score in a season.", st, "high_scores", True)
    add("Top Scores", "Most matchups finishing among the top scores of the week in a season.", st, "top_scores", True)
    add("Top Half Scores", "Most matchups finishing in the top half of weekly scoring in a season.", st, "top_half_scores", True)
    add("Worst Scores", "Most matchups finishing with the lowest score of the week in a season.", st, "worst_scores", True)
    add("Bottom Scores", "Most matchups finishing in the bottom scoring tier in a season.", st, "bottom_scores", True)
    add("Bottom Half Scores", "Most matchups finishing in the bottom half of weekly scoring in a season.", st, "bottom_half_scores", True)

    add("Most Blowout Wins", "Most wins by a large margin in a season.", st, "blowout_wins", True)
    add("Most Blowout Losses", "Most losses by a large margin in a season.", st, "blowout_losses", True)
    add("Most Narrow Wins", "Most wins by a small margin in a season.", st, "narrow_wins", True)
    add("Most Narrow Losses", "Most losses by a small margin in a season.", st, "narrow_losses", True)

    return out


def matchup_records(tg: pd.DataFrame) -> List[dict]:
    out: List[dict] = []
    m = tg.copy()
    m["combined_score"] = m["points_for"] + m["points_against"]

    def add_team_matchup(key: str, desc: str, df: pd.DataFrame, col: str, higher=True):
        row = pick_leader(df, col, higher)
        if row.empty:
            return
        out.append({
            "record": key,
            "description": desc,
            "season": int(row["season"]),
            "week": int(row["week"]),
            "team": row["team"],
            "opponent": row["opponent"],
            "is_playoff": bool(row["is_playoff"]),
            "value": float(row[col]) if pd.notna(row[col]) else None,
            "points_for": float(row["points_for"]),
            "points_against": float(row["points_against"]),
            "margin": float(row["margin"]),
        })

    add_team_matchup("Most Matchup Points", "Most points scored by a single team in one matchup.", m, "points_for", True)
    add_team_matchup("Fewest Matchup Points", "Fewest points scored by a single team in one matchup.", m, "points_for", False)

    # combined/margin records: collapse to unique matchup_key per season/week
    g = m.groupby(["season", "week", "matchup_key"], as_index=False).agg(
        combined=("combined_score", "max"),
        abs_margin=("abs_margin", "max"),
        is_playoff=("is_playoff", "max"),
    )

    def add_matchup(key: str, desc: str, df: pd.DataFrame, col: str, higher=True):
        row = pick_leader(df, col, higher)
        if row.empty:
            return
        out.append({
            "record": key,
            "description": desc,
            "season": int(row["season"]),
            "week": int(row["week"]),
            "matchup_key": row["matchup_key"],
            "is_playoff": bool(row["is_playoff"]),
            "value": float(row[col]) if pd.notna(row[col]) else None,
        })

    add_matchup("Highest Combined Score", "Highest combined score by both teams in a matchup.", g, "combined", True)
    add_matchup("Lowest Combined Score", "Lowest combined score by both teams in a matchup.", g, "combined", False)
    add_matchup("Biggest Blowout", "Largest score margin in any matchup.", g, "abs_margin", True)
    add_matchup("Narrowest Win", "Smallest score margin in any matchup.", g, "abs_margin", False)

    add_team_matchup("Highest Points Share", "Highest percentage of total points scored by one team in a matchup.", m, "points_share", True)
    add_team_matchup("Lowest Points Share", "Lowest percentage of total points scored by one team in a matchup.", m, "points_share", False)

    # playoff-specific
    mpo = m[m["is_playoff"]].copy()
    if not mpo.empty:
        add_team_matchup("Most Playoff Matchup Points", "Most points scored by a single team in a playoff matchup.", mpo, "points_for", True)
        add_team_matchup("Fewest Playoff Matchup Points", "Fewest points scored by a single team in a playoff matchup.", mpo, "points_for", False)

        gpo = g[g["is_playoff"]].copy()
        if not gpo.empty:
            add_matchup("Highest Playoff Combined Score", "Highest combined score by both teams in a playoff matchup.", gpo, "combined", True)
            add_matchup("Lowest Playoff Combined Score", "Lowest combined score by both teams in a playoff matchup.", gpo, "combined", False)
            add_matchup("Biggest Playoff Win", "Largest score margin in a playoff matchup.", gpo, "abs_margin", True)
            add_matchup("Narrowest Playoff Win", "Smallest score margin in a playoff matchup.", gpo, "abs_margin", False)

    return out


def streak_records(tg: pd.DataFrame, st: pd.DataFrame, cfg: dict) -> List[dict]:
    """
    Streaks/droughts:
      - matchup-level: wins/losses/high score/top tier/top half
      - season-level: winning record, championship/title game/medal/playoff appearance if Rank data exists
    """
    out: List[dict] = []

    def longest_streak(flags: List[bool]) -> int:
        best = cur = 0
        for v in flags:
            if v:
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
        return best

    def longest_drought(flags: List[bool]) -> int:
        # drought = consecutive False
        best = cur = 0
        for v in flags:
            if not v:
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
        return best

    # matchup-level streaks across all matchups (chronological)
    s = tg.copy().sort_values(["season", "week"])
    per_team = []
    for team, g in s.groupby("team"):
        g = g.copy()
        per_team.append({
            "team": team,
            "win_streak": longest_streak(g["is_win"].fillna(False).astype(bool).tolist()),
            "loss_streak": longest_streak(g["is_loss"].fillna(False).astype(bool).tolist()),
            "high_score_streak": longest_streak(g["is_week_high"].fillna(False).astype(bool).tolist()),
            "top_score_streak": longest_streak(g["is_top_tier"].fillna(False).astype(bool).tolist()),
            "top_half_streak": longest_streak(g["is_top_half"].fillna(False).astype(bool).tolist()),

            "high_score_drought": longest_drought(g["is_week_high"].fillna(False).astype(bool).tolist()),
            "top_score_drought": longest_drought(g["is_top_tier"].fillna(False).astype(bool).tolist()),
            "top_half_drought": longest_drought(g["is_top_half"].fillna(False).astype(bool).tolist()),
        })
    per_team_df = pd.DataFrame(per_team)

    def add(key: str, desc: str, df: pd.DataFrame, col: str):
        row = pick_leader(df, col, True)
        if row.empty:
            return
        out.append({"record": key, "description": desc, "leader": row["team"], "value": int(row[col])})

    add("Win Streak", "Most consecutive matchups won.", per_team_df, "win_streak")
    add("Loss Streak", "Most consecutive matchups lost.", per_team_df, "loss_streak")
    add("High Score Streak", "Most consecutive matchups with the highest weekly score.", per_team_df, "high_score_streak")
    add("Top Score Streak", "Most consecutive matchups finishing among the top scores of the week.", per_team_df, "top_score_streak")
    add("Top Half Streak", "Most consecutive matchups finishing in the top half of weekly scoring.", per_team_df, "top_half_streak")

    add("High Score Drought", "Most consecutive matchups without scoring the highest weekly score.", per_team_df, "high_score_drought")
    add("Top Score Drought", "Most consecutive matchups without finishing among the top scores of the week.", per_team_df, "top_score_drought")
    add("Top Half Drought", "Most consecutive matchups without finishing in the top half of scoring.", per_team_df, "top_half_drought")

    # season-level streaks/droughts for winning record
    st_sorted = st.sort_values(["season"])
    season_blocks = []
    for team, g in st_sorted.groupby("team"):
        flags = g["winning_record"].fillna(False).astype(bool).tolist()
        season_blocks.append({
            "team": team,
            "winning_record_streak": longest_streak(flags),
            "winning_record_drought": longest_drought(flags),
        })
    season_df = pd.DataFrame(season_blocks)

    add("Winning Record Streak", "Most consecutive seasons with a winning record.", season_df, "winning_record_streak")
    add("Winning Record Drought", "Most consecutive seasons without a winning record.", season_df, "winning_record_drought")

    # rank-based season streaks/droughts (if Rank exists)
    reg_rank, final_rank = rank_snapshots(tg, cfg)
    playoff_teams = int(cfg.get("playoff_teams", 4))

    if not reg_rank.empty:
        reg_rank_sorted = reg_rank.sort_values(["season"])
        blocks = []
        for team, g in reg_rank_sorted.groupby("team"):
            po_flags = (g["rank"] <= playoff_teams).astype(bool).tolist()
            blocks.append({
                "team": team,
                "playoff_appearance_streak": longest_streak(po_flags),
                "playoff_appearance_drought": longest_drought(po_flags),
            })
        sdf = pd.DataFrame(blocks)
        add("Playoff Appearance Streak", "Most consecutive seasons making the playoffs.", sdf, "playoff_appearance_streak")
        add("Playoff Appearance Drought", "Most consecutive seasons without making the playoffs.", sdf, "playoff_appearance_drought")

    if not final_rank.empty:
        fr_sorted = final_rank.sort_values(["season"])
        blocks = []
        for team, g in fr_sorted.groupby("team"):
            champ = (g["final_rank"] == 1).astype(bool).tolist()
            title_game = (g["final_rank"] <= 2).astype(bool).tolist()
            medal = (g["final_rank"] <= 3).astype(bool).tolist()
            blocks.append({
                "team": team,
                "championship_streak": longest_streak(champ),
                "championship_drought": longest_drought(champ),
                "title_game_streak": longest_streak(title_game),
                "title_game_drought": longest_drought(title_game),
                "medal_streak": longest_streak(medal),
                "medal_drought": longest_drought(medal),
            })
        sdf = pd.DataFrame(blocks)
        add("Championship Streak", "Most consecutive seasons winning the championship.", sdf, "championship_streak")
        add("Championship Drought", "Most consecutive seasons without winning the championship.", sdf, "championship_drought")
        add("Title Game Streak", "Most consecutive seasons reaching the title game.", sdf, "title_game_streak")
        add("Title Game Drought", "Most consecutive seasons without reaching the title game.", sdf, "title_game_drought")
        add("Medal Streak", "Most consecutive seasons finishing in a medal position (e.g. 1st/2nd/3rd).", sdf, "medal_streak")
        add("Medal Drought", "Most consecutive seasons without a medal finish (no top final rank).", sdf, "medal_drought")

    return out


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_config(repo_root)

    in_root = repo_root / "output" / "teamgamecenter"
    if not in_root.exists():
        raise SystemExit(f"Input folder not found: {in_root}")

    games: List[TeamGame] = []

    # Read seasons/weeks
    for season_dir in sorted([p for p in in_root.iterdir() if p.is_dir() and p.name.isdigit()], key=lambda p: int(p.name)):
        season = int(season_dir.name)
        week_files = [p for p in season_dir.glob("*.csv") if p.stem.isdigit()]
        for csv_path in sorted(week_files, key=lambda p: int(p.stem)):
            week = int(csv_path.stem)
            df = pd.read_csv(csv_path)
            games.extend(normalize_week_csv(df, season, week, cfg))

    if not games:
        raise SystemExit("No games loaded. Check output/teamgamecenter/<season>/<week>.csv")

    tg = pd.DataFrame([g.__dict__ for g in games])

    # enrich
    tg = compute_week_awards(tg, cfg)
    tg = compute_all_play(tg)
    tg = compute_matchup_flags(tg, cfg)

    # season summaries
    st = season_team_summary(tg)
    st = compute_strength_of_schedule(st, tg)

    po = playoff_team_summary(tg)

    # records
    records = {
        "league": league_records(tg, st, po, cfg),
        "season": season_records(st, tg, cfg),
        "matchup": matchup_records(tg),
        "streak": streak_records(tg, st, cfg),
    }

    # outputs
    out_dir = repo_root / "output" / "records"
    out_dir.mkdir(parents=True, exist_ok=True)

    tg.to_csv(out_dir / "team_games_enriched.csv", index=False)
    st.to_csv(out_dir / "season_team_summary.csv", index=False)
    po.to_csv(out_dir / "playoff_summary.csv", index=False)

    for k, items in records.items():
        pd.DataFrame(items).to_csv(out_dir / f"{k}_records.csv", index=False)

    with (out_dir / "records.json").open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"OK: wrote records to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
