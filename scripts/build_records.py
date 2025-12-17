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
    # playoffs begin week 15 -> regular season weeks = 14
    "default_regular_season_weeks": 14,
    "regular_season_weeks": {},  # optional: {"2015": 14, ...}

    # weekly award tiers (8 teams -> top 3 / bottom 3)
    "top_tier_count": 3,
    "bottom_tier_count": 3,

    # blowout/narrow margins
    "blowout_margin": 30,
    "narrow_margin": 5,

    # playoff appearance logic from end-of-regular-season rank
    "playoff_teams": 4,

    # medal scoring
    "medal_points": {"1": 3, "2": 2, "3": 1},

    # ranking rule split:
    # 2015-2021 use CSV Rank
    # 2022+ derive rank by points_for
    "rank_legacy_end_season": 2021,
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


def reg_end_week(season: int, cfg: dict) -> int:
    rm = cfg.get("regular_season_weeks", {}) or {}
    return int(rm.get(str(season), cfg.get("default_regular_season_weeks", 14)))


def is_playoff_week(season: int, week: int, cfg: dict) -> bool:
    return week > reg_end_week(season, cfg)


# ============================================================
# RECORD SCOPES (R / P / R+P)
# ============================================================
# Du kannst diese Zuordnung jederzeit ändern.
LEAGUE_RECORD_SCOPES = {
    # Basic record book (typisch: Regular Season)
    "Total Wins": "R",
    "Total Losses": "R",
    "Win Percent": "R",

    "All Play Wins": "R",
    "All Play Losses": "R",
    "All Play Win Percent": "R",

    "Total Points": "R",
    "Total Opponent Points": "R",
    "Points Share Average": "R",
    "Opponent Points Share Average": "R",

    "Luckiest": "R",
    "Luckiest (Least)": "R",

    "Strength of Schedule": "R",
    "Strength of Schedule (Easiest)": "R",

    "High Scores": "R",
    "High Scores Percent": "R",
    "Top Scores": "R",
    "Top Scores Percent": "R",
    "Top Half Scores": "R",
    "Top Half Score Percent": "R",

    "Worst Scores": "R",
    "Worst Scores Percent": "R",
    "Bottom Scores": "R",
    "Bottom Scores Percent": "R",
    "Bottom Half Scores": "R",
    "Bottom Half Score Percent": "R",

    "Blowout Wins": "R",
    "Blowout Losses": "R",
    "Narrow Wins": "R",
    "Narrow Losses": "R",

    "Regular Season Titles": "R",        # aus Saison-Rank (reg end)
    "Season Points Titles": "R",
    "Season All Play Titles": "R",
    "Seasons Winning Record": "R",
    "Seasons Losing Record": "R",

    "Championships": "R+P",              # aus final rank
    "Medal Score": "R+P",
    "Total Medals": "R+P",
    "Playoff Appearances": "R",          # aus reg end rank

    # Playoff-only records
    "Playoff Wins": "P",
    "Playoff Losses": "P",
    "Total Playoff Points": "P",
    "Total Playoff Opponent Points": "P",
}


def apply_scope(df: pd.DataFrame, scope: str) -> pd.DataFrame:
    scope = scope.upper().strip()
    if scope == "R":
        return df[df["is_playoff"] == False].copy()
    if scope == "P":
        return df[df["is_playoff"] == True].copy()
    if scope in {"R+P", "RP", "ALL"}:
        return df.copy()
    raise ValueError(f"Unknown scope '{scope}' (expected R, P, R+P)")


# ============================================================
# CSV NORMALIZATION (fixed header schema)
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
        return int(float(str(x).strip().replace(",", ".")))
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


def normalize_week_csv(df: pd.DataFrame, season: int, week: int, cfg: dict) -> List[TeamGame]:
    """
    Your schema:
      Owner, Rank, ..., Total, Opponent, Opponent Total
    Duplicate "Points" columns are ignored.
    """
    df = df.copy()
    df.columns = [norm_col(c) for c in df.columns]

    required = ["owner", "opponent", "total", "opponent_total"]
    for r in required:
        if r not in df.columns:
            raise ValueError(f"Missing required column '{r}' in season={season} week={week}. Found: {list(df.columns)}")

    out: List[TeamGame] = []
    po = is_playoff_week(season, week, cfg)

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
                is_playoff=po,
                matchup_key=make_matchup_key(season, week, team, opp),
            )
        )
    return out


# ============================================================
# ENRICHMENT: weekly awards, all-play, matchup flags
# ============================================================

def compute_week_awards(tg: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    tg = tg.copy()
    tg["points_for"] = tg["points_for"].astype(float)

    tg["week_team_count"] = tg.groupby(["season", "week"])["team"].transform("nunique").astype(int)
    tg["week_rank"] = tg.groupby(["season", "week"])["points_for"].rank(method="min", ascending=False).astype(int)

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
    tg = tg.copy()

    def _per_week(g: pd.DataFrame) -> pd.DataFrame:
        pts = g["points_for"].astype(float).values
        n = len(pts)
        wins, losses = [], []
        for i in range(n):
            p = pts[i]
            less = (pts < p).sum()
            greater = (pts > p).sum()
            equal = (pts == p).sum() - 1
            w = float(less) + 0.5 * float(equal)
            l = float(greater) + 0.5 * float(equal)
            wins.append(w)
            losses.append(l)

        out = g.copy()
        out["all_play_wins"] = wins
        out["all_play_losses"] = losses
        out["all_play_win_pct"] = out["all_play_wins"] / (n - 1 if n > 1 else 1)
        return out

    return tg.groupby(["season", "week"], group_keys=False).apply(_per_week)


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
# CAREER STATS (by scope)
# ============================================================

def career_from_matchups(df: pd.DataFrame) -> pd.DataFrame:
    """
    df is already filtered by scope (R / P / R+P)
    Computes team totals / averages.
    """
    d = df.copy()

    d["w"] = d["is_win"].astype(int)
    d["l"] = d["is_loss"].astype(int)
    d["t"] = d["is_tie"].astype(int)

    agg = d.groupby("team", as_index=False).agg(
        wins=("w", "sum"),
        losses=("l", "sum"),
        ties=("t", "sum"),
        points_for=("points_for", "sum"),
        points_against=("points_against", "sum"),

        points_share_avg=("points_share", "mean"),
        opp_points_share_avg=("opp_points_share", "mean"),

        all_play_wins=("all_play_wins", "sum"),
        all_play_losses=("all_play_losses", "sum"),

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
    )

    agg["games"] = agg["wins"] + agg["losses"] + agg["ties"]
    agg["win_pct"] = (agg["wins"] + 0.5 * agg["ties"]) / agg["games"].replace(0, float("nan"))

    ap_games = (agg["all_play_wins"] + agg["all_play_losses"]).replace(0, float("nan"))
    agg["all_play_win_pct"] = agg["all_play_wins"] / ap_games

    # Luck on this scope: actual win value minus all-play win%
    d["actual_win_value"] = d["is_win"].astype(float) + 0.5 * d["is_tie"].astype(float)
    d["luck_week"] = d["actual_win_value"] - d["all_play_win_pct"].astype(float)
    luck = d.groupby("team", as_index=False)["luck_week"].mean().rename(columns={"luck_week": "luck_avg"})
    agg = agg.merge(luck, on="team", how="left")

    return agg


def compute_sos_on_scope(df: pd.DataFrame) -> pd.DataFrame:
    """
    SOS (scope-based):
      For each season: compute each team's season points_for (within scope)
      SOS(team, season) = avg opponents' season points_for
      Career SOS = mean over seasons
    """
    d = df.copy()
    season_pts = d.groupby(["season", "team"], as_index=False)["points_for"].sum().rename(columns={"points_for": "season_points_for"})
    pts_map = season_pts.set_index(["season", "team"])["season_points_for"].to_dict()

    def opp_pts(row) -> float:
        return float(pts_map.get((row["season"], row["opponent"]), float("nan")))

    d["opp_season_points_for"] = d.apply(opp_pts, axis=1)
    sos_season = d.groupby(["season", "team"], as_index=False)["opp_season_points_for"].mean().rename(columns={"opp_season_points_for": "sos"})
    sos_career = sos_season.groupby("team", as_index=False)["sos"].mean().rename(columns={"sos": "sos_avg_opp_points"})
    return sos_career


# ============================================================
# RANK TABLES (2015-2021 from CSV Rank, 2022+ by points_for)
# ============================================================

def build_rank_tables(tg: pd.DataFrame, cfg: dict) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
      reg_rank: season/team/reg_rank  (rank at end of regular season)
      final_rank: season/team/final_rank (rank at end of season)

    Rules:
      - seasons <= rank_legacy_end_season: use CSV Rank if present
      - seasons >= legacy_end+1: derive rank by points_for (sorted desc)
          reg_rank: based on regular season points_for
          final_rank: based on total points_for (regular + playoffs)
    """
    legacy_end = int(cfg.get("rank_legacy_end_season", 2021))

    # Helper: rank by points_for in a dataframe with columns season, team, points_for
    def rank_by_pf(df_pf: pd.DataFrame, rank_col: str) -> pd.DataFrame:
        out = []
        for season, g in df_pf.groupby("season"):
            g2 = g.copy()
            g2 = g2.sort_values(["points_for"], ascending=False)
            # method="min": ties get same best rank
            g2[rank_col] = g2["points_for"].rank(method="min", ascending=False).astype(int)
            out.append(g2[["season", "team", rank_col]])
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["season", "team", rank_col])

    # --- legacy seasons: take Rank snapshot from CSV
    has_rank = "rank" in tg.columns and tg["rank"].notna().any()

    reg_snapshots = []
    final_snapshots = []

    if has_rank:
        r = tg[["season", "week", "team", "rank"]].dropna(subset=["rank"]).copy()
        r["rank"] = r["rank"].astype(int)

        # reg snapshot at week 14 (or per-season override)
        r["reg_end_week"] = r["season"].apply(lambda s: reg_end_week(int(s), cfg))
        reg_legacy = r[(r["season"] <= legacy_end) & (r["week"] == r["reg_end_week"])].copy()
        if not reg_legacy.empty:
            reg_snapshots.append(reg_legacy.rename(columns={"rank": "reg_rank"})[["season", "team", "reg_rank"]])

        # final snapshot at last week per season
        last_week = r.groupby("season")["week"].max().to_dict()
        finals = []
        for (season, team), g in r[r["season"] <= legacy_end].groupby(["season", "team"]):
            lw = last_week.get(season)
            rr = g[g["week"] == lw]["rank"]
            if not rr.empty:
                finals.append({"season": int(season), "team": team, "final_rank": int(rr.iloc[0])})
        if finals:
            final_snapshots.append(pd.DataFrame(finals))

    # --- derived seasons: 2022+ by PF
    # reg PF: regular season only
    reg_df = tg[tg["is_playoff"] == False].groupby(["season", "team"], as_index=False)["points_for"].sum()
    reg_df = reg_df[reg_df["season"] >= (legacy_end + 1)]
    if not reg_df.empty:
        reg_snapshots.append(rank_by_pf(reg_df, "reg_rank"))

    # final PF: all games
    total_df = tg.groupby(["season", "team"], as_index=False)["points_for"].sum()
    total_df = total_df[total_df["season"] >= (legacy_end + 1)]
    if not total_df.empty:
        final_snapshots.append(rank_by_pf(total_df, "final_rank"))

    reg_rank = pd.concat(reg_snapshots, ignore_index=True) if reg_snapshots else pd.DataFrame(columns=["season", "team", "reg_rank"])
    final_rank = pd.concat(final_snapshots, ignore_index=True) if final_snapshots else pd.DataFrame(columns=["season", "team", "final_rank"])

    # Deduplicate if anything overlaps (shouldn't, but safe)
    if not reg_rank.empty:
        reg_rank = reg_rank.sort_values(["season", "team"]).drop_duplicates(["season", "team"], keep="last")
    if not final_rank.empty:
        final_rank = final_rank.sort_values(["season", "team"]).drop_duplicates(["season", "team"], keep="last")

    return reg_rank, final_rank


def add_titles_medals_blocks(career: pd.DataFrame, tg: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    playoff_teams = int(cfg.get("playoff_teams", 4))
    medal_points = cfg.get("medal_points", {"1": 3, "2": 2, "3": 1})

    reg_rank, final_rank = build_rank_tables(tg, cfg)

    # Regular Season Titles + Playoff Appearances from reg_rank
    if not reg_rank.empty:
        rr = reg_rank.copy()
        rr["is_reg_title"] = rr["reg_rank"] == 1
        rr["is_po_app"] = rr["reg_rank"] <= playoff_teams

        t1 = rr.groupby("team", as_index=False)["is_reg_title"].sum().rename(columns={"is_reg_title": "regular_season_titles"})
        t2 = rr.groupby("team", as_index=False)["is_po_app"].sum().rename(columns={"is_po_app": "playoff_appearances"})
        career = career.merge(t1, on="team", how="left").merge(t2, on="team", how="left")

    # Championships / medals from final_rank
    if not final_rank.empty:
        fr = final_rank.copy()
        fr["is_champ"] = fr["final_rank"] == 1
        fr["is_title_game"] = fr["final_rank"] <= 2
        fr["is_medal"] = fr["final_rank"] <= 3
        fr["medal_score"] = fr["final_rank"].astype(str).map(medal_points).fillna(0).astype(int)

        champs = fr.groupby("team", as_index=False).agg(
            championships=("is_champ", "sum"),
            title_games=("is_title_game", "sum"),
            total_medals=("is_medal", "sum"),
            medal_score=("medal_score", "sum"),
        )
        career = career.merge(champs, on="team", how="left")

    # Fill missing
    for col in ["regular_season_titles", "playoff_appearances", "championships", "title_games", "total_medals", "medal_score"]:
        if col not in career.columns:
            career[col] = 0
        career[col] = career[col].fillna(0).astype(int)

    return career


# ============================================================
# SEASON-SUMMARY (Regular season based; used for some league records)
# ============================================================

def season_summary_regular(tg: pd.DataFrame) -> pd.DataFrame:
    reg = tg[tg["is_playoff"] == False].copy()
    reg["w"] = reg["is_win"].astype(int)
    reg["l"] = reg["is_loss"].astype(int)
    reg["t"] = reg["is_tie"].astype(int)

    st = reg.groupby(["season", "team"], as_index=False).agg(
        wins=("w", "sum"),
        losses=("l", "sum"),
        ties=("t", "sum"),
        points_for=("points_for", "sum"),
        all_play_wins=("all_play_wins", "sum"),
    )
    st["winning_record"] = st["wins"] > st["losses"]
    st["losing_record"] = st["wins"] < st["losses"]
    return st


def compute_season_titles_points_allplay(st: pd.DataFrame) -> pd.DataFrame:
    """
    Counts:
      season_points_titles: seasons with most PF (regular season)
      season_all_play_titles: seasons with best all-play wins (regular season)
    """
    pts_titles = []
    ap_titles = []

    for season, g in st.groupby("season"):
        if g.empty:
            continue
        pmax = g["points_for"].max()
        for t in g[g["points_for"] == pmax]["team"].tolist():
            pts_titles.append({"team": t, "season_points_titles": 1})

        apmax = g["all_play_wins"].max()
        for t in g[g["all_play_wins"] == apmax]["team"].tolist():
            ap_titles.append({"team": t, "season_all_play_titles": 1})

    pts_df = pd.DataFrame(pts_titles).groupby("team", as_index=False)["season_points_titles"].sum() if pts_titles else pd.DataFrame(columns=["team", "season_points_titles"])
    ap_df = pd.DataFrame(ap_titles).groupby("team", as_index=False)["season_all_play_titles"].sum() if ap_titles else pd.DataFrame(columns=["team", "season_all_play_titles"])
    out = pts_df.merge(ap_df, on="team", how="outer").fillna(0)
    out["season_points_titles"] = out["season_points_titles"].astype(int)
    out["season_all_play_titles"] = out["season_all_play_titles"].astype(int)
    return out


# ============================================================
# RECORD BUILDERS
# ============================================================

def pick_leader(df: pd.DataFrame, col: str, higher: bool = True) -> pd.Series:
    if df.empty or col not in df.columns:
        return pd.Series(dtype=object)
    s = pd.to_numeric(df[col], errors="coerce")
    if s.isna().all():
        return pd.Series(dtype=object)
    idx = s.idxmax() if higher else s.idxmin()
    return df.loc[idx]


def build_league_records(tg: pd.DataFrame, cfg: dict) -> List[dict]:
    """
    Builds league records using per-record scopes.
    """
    out: List[dict] = []

    # Precompute season-based (regular season) stuff used by some records
    st_reg = season_summary_regular(tg)

    # Career "seasons winning/losing record"
    wl_seasons = st_reg.groupby("team", as_index=False).agg(
        seasons_winning_record=("winning_record", "sum"),
        seasons_losing_record=("losing_record", "sum"),
    )
    # Season titles (points, all-play) from regular season
    season_titles = compute_season_titles_points_allplay(st_reg)

    # Helper to get career stats by scope
    career_cache: Dict[str, pd.DataFrame] = {}
    sos_cache: Dict[str, pd.DataFrame] = {}

    def career(scope: str) -> pd.DataFrame:
        if scope not in career_cache:
            df_scoped = apply_scope(tg, scope)
            c = career_from_matchups(df_scoped)
            # SOS for this scope
            sos = compute_sos_on_scope(df_scoped)
            c = c.merge(sos, on="team", how="left")
            career_cache[scope] = c
        return career_cache[scope]

    # Titles/medals blocks (rank logic rules) — independent of record scope
    # We merge into whichever career df we output records from (safe).
    titles_medals = None  # computed lazily

    def ensure_titles_medals():
        nonlocal titles_medals
        if titles_medals is None:
            # start from all-teams list
            base = pd.DataFrame({"team": sorted(tg["team"].dropna().unique().tolist())})
            titles_medals = add_titles_medals_blocks(base, tg, cfg)
            titles_medals = titles_medals.merge(wl_seasons, on="team", how="left").merge(season_titles, on="team", how="left").fillna(0)
        return titles_medals

    def add_record(key: str, desc: str, df: pd.DataFrame, col: str, higher=True, as_pct_of_games: bool = False):
        d = df.copy()
        if as_pct_of_games:
            d[col + "_pct"] = pd.to_numeric(d[col], errors="coerce") / pd.to_numeric(d["games"], errors="coerce").replace(0, float("nan"))
            col_use = col + "_pct"
        else:
            col_use = col

        row = pick_leader(d, col_use, higher)
        if row.empty:
            return
        out.append({
            "record": key,
            "description": desc,
            "leader": row["team"],
            "scope": LEAGUE_RECORD_SCOPES.get(key, "R"),
            "value": float(row[col_use]) if pd.notna(row[col_use]) else None,
        })

    # ---- Basic / Matchup-based records (use career(scope)) ----
    add_record("Total Wins", "Most total wins in league history.", career("R"), "wins", True)
    add_record("Total Losses", "Most total losses in league history.", career("R"), "losses", True)
    add_record("Win Percent", "Best win percentage in league history.", career("R"), "win_pct", True)

    add_record("All Play Wins", "Most total all-play wins in league history.", career("R"), "all_play_wins", True)
    add_record("All Play Losses", "Most total all-play losses in league history.", career("R"), "all_play_losses", True)
    add_record("All Play Win Percent", "Best all-play win percentage in league history.", career("R"), "all_play_win_pct", True)

    add_record("Total Points", "Most total points scored in league history.", career("R"), "points_for", True)
    add_record("Total Opponent Points", "Most total opponent points allowed in league history.", career("R"), "points_against", True)

    add_record("Points Share Average", "Highest average share of total points scored in matchups over league history.", career("R"), "points_share_avg", True)
    add_record("Opponent Points Share Average", "Highest average share of total points scored by opponents over league history.", career("R"), "opp_points_share_avg", True)

    add_record("Luckiest", "Luckiest member in league history (based on opponent performance).", career("R"), "luck_avg", True)
    add_record("Luckiest (Least)", "Unluckiest member in league history (based on opponent performance).", career("R"), "luck_avg", False)

    add_record("Strength of Schedule", "Toughest average strength of schedule in league history.", career("R"), "sos_avg_opp_points", True)
    add_record("Strength of Schedule (Easiest)", "Easiest average strength of schedule in league history.", career("R"), "sos_avg_opp_points", False)

    add_record("High Scores", "Most matchups with the weekly high score in league history.", career("R"), "high_scores", True)
    add_record("High Scores Percent", "Highest percentage of matchups with the weekly high score.", career("R"), "high_scores", True, as_pct_of_games=True)

    add_record("Top Scores", "Most matchups with a top score (top tier) in league history.", career("R"), "top_scores", True)
    add_record("Top Scores Percent", "Highest percentage of matchups with a top score.", career("R"), "top_scores", True, as_pct_of_games=True)

    add_record("Top Half Scores", "Most matchups finishing in the top half of weekly scores in league history.", career("R"), "top_half_scores", True)
    add_record("Top Half Score Percent", "Highest percentage of matchups finishing in the top half.", career("R"), "top_half_scores", True, as_pct_of_games=True)

    add_record("Worst Scores", "Most matchups with the lowest score of the week in league history.", career("R"), "worst_scores", True)
    add_record("Worst Scores Percent", "Highest percentage of matchups with the lowest score.", career("R"), "worst_scores", True, as_pct_of_games=True)

    add_record("Bottom Scores", "Most matchups with a bottom score (bottom tier) in league history.", career("R"), "bottom_scores", True)
    add_record("Bottom Scores Percent", "Highest percentage of matchups with a bottom score.", career("R"), "bottom_scores", True, as_pct_of_games=True)

    add_record("Bottom Half Scores", "Most matchups finishing in the bottom half of weekly scores in league history.", career("R"), "bottom_half_scores", True)
    add_record("Bottom Half Score Percent", "Highest percentage of matchups finishing in the bottom half.", career("R"), "bottom_half_scores", True, as_pct_of_games=True)

    add_record("Blowout Wins", "Most wins by a large margin in league history.", career("R"), "blowout_wins", True)
    add_record("Blowout Losses", "Most losses by a large margin in league history.", career("R"), "blowout_losses", True)
    add_record("Narrow Wins", "Most wins by a narrow margin in league history.", career("R"), "narrow_wins", True)
    add_record("Narrow Losses", "Most losses by a narrow margin in league history.", career("R"), "narrow_losses", True)

    # ---- Title/Medal/Playoff appearance records (rank rules) ----
    tm = ensure_titles_medals()

    def add_tm(key: str, desc: str, col: str, higher=True):
        row = pick_leader(tm, col, higher)
        if row.empty:
            return
        out.append({
            "record": key,
            "description": desc,
            "leader": row["team"],
            "scope": LEAGUE_RECORD_SCOPES.get(key, "R"),
            "value": float(row[col]) if pd.notna(row[col]) else None,
        })

    add_tm("Regular Season Titles", "Most regular-season titles in league history.", "regular_season_titles", True)
    add_tm("Season Points Titles", "Most seasons leading the regular season in points scored.", "season_points_titles", True)
    add_tm("Season All Play Titles", "Most seasons with the best all-play record in league history.", "season_all_play_titles", True)
    add_tm("Seasons Winning Record", "Most seasons with an overall winning record in league history.", "seasons_winning_record", True)
    add_tm("Seasons Losing Record", "Most seasons with an overall losing record in league history.", "seasons_losing_record", True)

    add_tm("Championships", "Most league championships in league history.", "championships", True)
    add_tm("Medal Score", "Best medal score over league history.", "medal_score", True)
    add_tm("Total Medals", "Most total medals in league history.", "total_medals", True)
    add_tm("Playoff Appearances", "Most playoff appearances in league history.", "playoff_appearances", True)

    # ---- Playoff-only totals ----
    add_record("Playoff Wins", "Most playoff wins in league history.", career("P"), "wins", True)
    add_record("Playoff Losses", "Most playoff losses in league history.", career("P"), "losses", True)
    add_record("Total Playoff Points", "Most total playoff points scored in league history.", career("P"), "points_for", True)
    add_record("Total Playoff Opponent Points", "Most total playoff points allowed to opponents in league history.", career("P"), "points_against", True)

    return out


def build_season_records(tg: pd.DataFrame, cfg: dict) -> List[dict]:
    """
    Season records (still regular-season-based like before).
    """
    out: List[dict] = []
    reg = tg[tg["is_playoff"] == False].copy()

    # season/team summary
    reg_sum = reg.groupby(["season", "team"], as_index=False).agg(
        wins=("is_win", "sum"),
        losses=("is_loss", "sum"),
        points_for=("points_for", "sum"),
        points_against=("points_against", "sum"),
        points_share=("points_share", "mean"),
        opp_points_share=("opp_points_share", "mean"),
        all_play_wins=("all_play_wins", "sum"),
        all_play_losses=("all_play_losses", "sum"),
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
    )
    ap_games = (reg_sum["all_play_wins"] + reg_sum["all_play_losses"]).replace(0, float("nan"))
    reg_sum["all_play_win_pct"] = reg_sum["all_play_wins"] / ap_games

    # season luck
    reg["actual_win_value"] = reg["is_win"].astype(float) + 0.5 * reg["is_tie"].astype(float)
    reg["luck_week"] = reg["actual_win_value"] - reg["all_play_win_pct"].astype(float)
    luck = reg.groupby(["season", "team"], as_index=False)["luck_week"].mean().rename(columns={"luck_week": "season_luck"})
    reg_sum = reg_sum.merge(luck, on=["season", "team"], how="left")

    def add(key: str, desc: str, col: str, higher=True):
        row = pick_leader(reg_sum, col, higher)
        if row.empty:
            return
        out.append({
            "record": key,
            "description": desc,
            "season": int(row["season"]),
            "leader": row["team"],
            "value": float(row[col]) if pd.notna(row[col]) else None,
        })

    add("Season Score", "Highest season score in a single season.", "points_for", True)
    add("Season Score (Lowest)", "Lowest season score in a single season.", "points_for", False)
    add("Season Luckiest", "Luckiest team in a season (based on opponents’ performance).", "season_luck", True)
    add("Season Luckiest (Lowest)", "Unluckiest team in a season (based on opponents’ performance).", "season_luck", False)

    add("Most Wins", "Most wins in a single season.", "wins", True)
    add("Most Losses", "Most losses in a single season.", "losses", True)

    add("Most All Play Wins", "Most all-play wins in a season.", "all_play_wins", True)
    add("Most All Play Losses", "Most all-play losses in a season.", "all_play_losses", True)
    add("Best All Play Win Percent", "Best all-play win percentage in a season.", "all_play_win_pct", True)

    add("Most Points", "Most points scored in a season.", "points_for", True)
    add("Most Opponent Points", "Most opponent points allowed in a season.", "points_against", True)
    add("Fewest Points", "Fewest points scored in a season.", "points_for", False)
    add("Fewest Opponent Points", "Fewest opponent points allowed in a season.", "points_against", False)

    add("Highest Points Share", "Highest points share in a season.", "points_share", True)
    add("Lowest Points Share", "Lowest points share in a season.", "points_share", False)
    add("Highest Opponent Points Share", "Highest opponents’ points share in a season.", "opp_points_share", True)
    add("Lowest Opponent Points Share", "Lowest opponents’ points share in a season.", "opp_points_share", False)

    add("High Scores", "Most matchups finishing with the weekly high score in a season.", "high_scores", True)
    add("Top Scores", "Most matchups finishing among the top scores of the week in a season.", "top_scores", True)
    add("Top Half Scores", "Most matchups finishing in the top half of weekly scoring in a season.", "top_half_scores", True)
    add("Worst Scores", "Most matchups finishing with the lowest score of the week in a season.", "worst_scores", True)
    add("Bottom Scores", "Most matchups finishing in the bottom scoring tier in a season.", "bottom_scores", True)
    add("Bottom Half Scores", "Most matchups finishing in the bottom half of weekly scoring in a season.", "bottom_half_scores", True)

    add("Most Blowout Wins", "Most wins by a large margin in a season.", "blowout_wins", True)
    add("Most Blowout Losses", "Most losses by a large margin in a season.", "blowout_losses", True)
    add("Most Narrow Wins", "Most wins by a small margin in a season.", "narrow_wins", True)
    add("Most Narrow Losses", "Most losses by a small margin in a season.", "narrow_losses", True)

    return out


def build_matchup_records(tg: pd.DataFrame) -> List[dict]:
    out: List[dict] = []
    m = tg.copy()
    m["combined_score"] = m["points_for"] + m["points_against"]

    def add_team(key: str, desc: str, col: str, higher=True, df=None):
        d = m if df is None else df
        row = pick_leader(d, col, higher)
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

    add_team("Most Matchup Points", "Most points scored by a single team in one matchup.", "points_for", True)
    add_team("Fewest Matchup Points", "Fewest points scored by a single team in one matchup.", "points_for", False)

    g = m.groupby(["season", "week", "matchup_key"], as_index=False).agg(
        combined=("combined_score", "max"),
        abs_margin=("abs_margin", "max"),
        is_playoff=("is_playoff", "max"),
    )

    def add_matchup(key: str, desc: str, col: str, higher=True, df=None):
        d = g if df is None else df
        row = pick_leader(d, col, higher)
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

    add_matchup("Highest Combined Score", "Highest combined score by both teams in a matchup.", "combined", True)
    add_matchup("Lowest Combined Score", "Lowest combined score by both teams in a matchup.", "combined", False)
    add_matchup("Biggest Blowout", "Largest score margin in any matchup.", "abs_margin", True)
    add_matchup("Narrowest Win", "Smallest score margin in any matchup.", "abs_margin", False)

    add_team("Highest Points Share", "Highest % of total points scored by one team in a matchup.", "points_share", True)
    add_team("Lowest Points Share", "Lowest % of total points scored by one team in a matchup.", "points_share", False)

    mpo = m[m["is_playoff"] == True].copy()
    if not mpo.empty:
        add_team("Most Playoff Matchup Points", "Most points scored by a single team in a playoff matchup.", "points_for", True, df=mpo)
        add_team("Fewest Playoff Matchup Points", "Fewest points scored by a single team in a playoff matchup.", "points_for", False, df=mpo)

        gpo = g[g["is_playoff"] == True].copy()
        if not gpo.empty:
            add_matchup("Highest Playoff Combined Score", "Highest combined score by both teams in a playoff matchup.", "combined", True, df=gpo)
            add_matchup("Lowest Playoff Combined Score", "Lowest combined score by both teams in a playoff matchup.", "combined", False, df=gpo)
            add_matchup("Biggest Playoff Win", "Largest score margin in a playoff matchup.", "abs_margin", True, df=gpo)
            add_matchup("Narrowest Playoff Win", "Smallest score margin in a playoff matchup.", "abs_margin", False, df=gpo)

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

    # outputs dir
    out_dir = repo_root / "output" / "records"
    out_dir.mkdir(parents=True, exist_ok=True)

    # records
    records = {
        "league": build_league_records(tg, cfg),
        "season": build_season_records(tg, cfg),
        "matchup": build_matchup_records(tg),
    }

    # write tables
    tg.to_csv(out_dir / "team_games_enriched.csv", index=False)

    for k, items in records.items():
        pd.DataFrame(items).to_csv(out_dir / f"{k}_records.csv", index=False)

    with (out_dir / "records.json").open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"OK: wrote records to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

