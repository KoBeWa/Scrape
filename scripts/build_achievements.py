#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# ============================================================
# Defaults / knobs
# ============================================================

DEFAULTS = {
    # playoffs begin week 15 -> regular season weeks = 14
    "default_regular_season_weeks": 14,
    "regular_season_weeks": {},

    # ranking rule split:
    # 2015-2021 use CSV Rank snapshots
    # 2022+ derive rank by points_for
    "rank_legacy_end_season": 2021,

    # playoff appearance logic
    "playoff_teams": 4,

    # milestone scopes for "league achievements"
    # (R) regular season only, (P) playoffs only, (R+P) all
    "milestone_scope": "R",

    # start-pattern achievements (definitions)
    "start_undefeated_weeks": 5,   # for "Slipped Away"
    "start_0_5_weeks": 5,          # for "Comeback Kid"

    # matchup thresholds
    "firing_squad_share": 0.90,
    "double_up_factor": 2.0,

    # medal scoring
    "medal_points": {"1": 3, "2": 2, "3": 1},

    # "Season score tiers" – we must define what "season score" means.
    # Here: regular-season win_pct * 100
    "season_score_metric": "win_pct_x100",  # future-proof hook
}


# ============================================================
# Helper: config loader (yaml optional)
# ============================================================

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


def load_config(repo_root: Path) -> dict:
    cfg = dict(DEFAULTS)
    p = repo_root / "config" / "league_records.yml"
    if not p.exists() or yaml is None:
        return cfg
    with p.open("r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    cfg.update(user_cfg)
    cfg["regular_season_weeks"] = {**DEFAULTS.get("regular_season_weeks", {}), **(user_cfg.get("regular_season_weeks", {}) or {})}
    cfg["medal_points"] = {**DEFAULTS.get("medal_points", {}), **(user_cfg.get("medal_points", {}) or {})}
    return cfg


def reg_end_week(season: int, cfg: dict) -> int:
    rm = cfg.get("regular_season_weeks", {}) or {}
    return int(rm.get(str(season), cfg.get("default_regular_season_weeks", 14)))


# ============================================================
# Data model
# ============================================================

@dataclass
class Achievement:
    category: str
    achievement: str
    description: str
    team: str

    # optional context
    season: Optional[int] = None
    week: Optional[int] = None
    opponent: Optional[str] = None

    # optional numeric context
    value: Optional[float] = None

    # optional metadata
    scope: Optional[str] = None
    status: str = "AWARDED"   # AWARDED | SKIPPED_MISSING_DATA


def tg_scope_filter(tg: pd.DataFrame, scope: str) -> pd.DataFrame:
    s = scope.strip().upper()
    if s == "R":
        return tg[tg["is_playoff"] == False].copy()
    if s == "P":
        return tg[tg["is_playoff"] == True].copy()
    if s in {"R+P", "RP", "ALL"}:
        return tg.copy()
    raise ValueError(f"Unknown scope: {scope}")


def safe_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")


# ============================================================
# Rank tables: 2015-2021 from Rank, 2022+ by PF
# ============================================================

def build_rank_tables(tg: pd.DataFrame, cfg: dict) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
      reg_rank: season/team/reg_rank (rank at end of regular season)
      final_rank: season/team/final_rank (rank at end of season)

    Rules:
      - seasons <= legacy_end: use CSV Rank snapshots if available
      - seasons >= legacy_end+1: derive rank from points_for
          reg_rank uses regular season PF
          final_rank uses total PF (R+P)
    """
    legacy_end = int(cfg.get("rank_legacy_end_season", 2021))

    # helper: rank by PF within each season
    def rank_by_pf(df_pf: pd.DataFrame, col_out: str) -> pd.DataFrame:
        out = []
        for season, g in df_pf.groupby("season"):
            g2 = g.copy().sort_values(["points_for"], ascending=False)
            g2[col_out] = g2["points_for"].rank(method="min", ascending=False).astype(int)
            out.append(g2[["season", "team", col_out]])
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["season", "team", col_out])

    reg_parts = []
    final_parts = []

    # --- legacy (Rank snapshots)
    if "rank" in tg.columns and tg["rank"].notna().any():
        r = tg[["season", "week", "team", "rank"]].dropna(subset=["rank"]).copy()
        r["rank"] = r["rank"].astype(int)
        r["reg_end_week"] = r["season"].apply(lambda s: reg_end_week(int(s), cfg))

        reg_legacy = r[(r["season"] <= legacy_end) & (r["week"] == r["reg_end_week"])].copy()
        if not reg_legacy.empty:
            reg_parts.append(reg_legacy.rename(columns={"rank": "reg_rank"})[["season", "team", "reg_rank"]])

        # final snapshot at last week in that season
        last_week = r.groupby("season")["week"].max().to_dict()
        finals = []
        for (season, team), g in r[r["season"] <= legacy_end].groupby(["season", "team"]):
            lw = last_week.get(season)
            rr = g[g["week"] == lw]["rank"]
            if not rr.empty:
                finals.append({"season": int(season), "team": team, "final_rank": int(rr.iloc[0])})
        if finals:
            final_parts.append(pd.DataFrame(finals))

    # --- derived 2022+
    reg_pf = tg[tg["is_playoff"] == False].groupby(["season", "team"], as_index=False)["points_for"].sum()
    reg_pf = reg_pf[reg_pf["season"] >= (legacy_end + 1)]
    if not reg_pf.empty:
        reg_parts.append(rank_by_pf(reg_pf, "reg_rank"))

    total_pf = tg.groupby(["season", "team"], as_index=False)["points_for"].sum()
    total_pf = total_pf[total_pf["season"] >= (legacy_end + 1)]
    if not total_pf.empty:
        final_parts.append(rank_by_pf(total_pf, "final_rank"))

    reg_rank = pd.concat(reg_parts, ignore_index=True) if reg_parts else pd.DataFrame(columns=["season", "team", "reg_rank"])
    final_rank = pd.concat(final_parts, ignore_index=True) if final_parts else pd.DataFrame(columns=["season", "team", "final_rank"])

    # dedupe
    if not reg_rank.empty:
        reg_rank = reg_rank.sort_values(["season", "team"]).drop_duplicates(["season", "team"], keep="last")
    if not final_rank.empty:
        final_rank = final_rank.sort_values(["season", "team"]).drop_duplicates(["season", "team"], keep="last")

    return reg_rank, final_rank


# ============================================================
# Season summaries (regular + playoff split)
# ============================================================

def season_team_summaries(tg: pd.DataFrame, cfg: dict) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Returns:
      reg_season: per (season, team) regular season summary
      po_season: per (season, team) playoffs summary
      season_luck_sos: per (season, team) luck + sos (computed on regular season)
    """
    reg = tg[tg["is_playoff"] == False].copy()
    po = tg[tg["is_playoff"] == True].copy()

    reg["w"] = reg["is_win"].astype(int)
    reg["l"] = reg["is_loss"].astype(int)
    reg["t"] = reg["is_tie"].astype(int)

    reg_sum = reg.groupby(["season", "team"], as_index=False).agg(
        reg_wins=("w", "sum"),
        reg_losses=("l", "sum"),
        reg_ties=("t", "sum"),
        reg_points_for=("points_for", "sum"),
        reg_points_against=("points_against", "sum"),
        reg_all_play_wins=("all_play_wins", "sum"),
        reg_all_play_losses=("all_play_losses", "sum"),
        reg_high_scores=("is_week_high", "sum"),
        reg_worst_scores=("is_week_low", "sum"),
        reg_top_half=("is_top_half", "sum"),
        reg_bottom_half=("is_bottom_half", "sum"),
    )

    reg_sum["reg_games"] = reg_sum["reg_wins"] + reg_sum["reg_losses"] + reg_sum["reg_ties"]
    reg_sum["reg_win_pct"] = (reg_sum["reg_wins"] + 0.5 * reg_sum["reg_ties"]) / reg_sum["reg_games"].replace(0, float("nan"))
    reg_sum["reg_all_play_win_pct"] = reg_sum["reg_all_play_wins"] / (reg_sum["reg_all_play_wins"] + reg_sum["reg_all_play_losses"]).replace(0, float("nan"))

    if po.empty:
        po_sum = pd.DataFrame(columns=["season", "team", "po_wins", "po_losses", "po_points_for", "po_points_against"])
    else:
        po["w"] = po["is_win"].astype(int)
        po["l"] = po["is_loss"].astype(int)
        po_sum = po.groupby(["season", "team"], as_index=False).agg(
            po_wins=("w", "sum"),
            po_losses=("l", "sum"),
            po_points_for=("points_for", "sum"),
            po_points_against=("points_against", "sum"),
        )

    # Luck (regular season): actual_win_value - all_play_win_pct per week, averaged over weeks
    reg2 = reg.copy()
    reg2["actual_win_value"] = reg2["is_win"].astype(float) + 0.5 * reg2["is_tie"].astype(float)
    reg2["luck_week"] = reg2["actual_win_value"] - reg2["all_play_win_pct"].astype(float)
    season_luck = reg2.groupby(["season", "team"], as_index=False)["luck_week"].mean().rename(columns={"luck_week": "season_luck"})

    # SOS (regular season): avg opponents' season PF (regular season)
    pts_map = reg_sum.set_index(["season", "team"])["reg_points_for"].to_dict()
    reg2["opp_reg_season_pf"] = reg2.apply(lambda r: float(pts_map.get((r["season"], r["opponent"]), float("nan"))), axis=1)
    season_sos = reg2.groupby(["season", "team"], as_index=False)["opp_reg_season_pf"].mean().rename(columns={"opp_reg_season_pf": "season_sos"})

    season_ls = reg_sum.merge(season_luck, on=["season", "team"], how="left").merge(season_sos, on=["season", "team"], how="left")
    return reg_sum, po_sum, season_ls


# ============================================================
# Achievement builders
# ============================================================

def add(out: List[Achievement], **kwargs):
    out.append(Achievement(**kwargs))


# -------------------------
# League milestones
# -------------------------

def milestone_achievements(tg: pd.DataFrame, cfg: dict) -> List[Achievement]:
    out: List[Achievement] = []

    scope = str(cfg.get("milestone_scope", "R"))
    df = tg_scope_filter(tg, scope).copy()

    # chronological for "achieved at"
    df = df.sort_values(["season", "week", "team"]).copy()

    # cumulative stats per team
    df["w"] = df["is_win"].astype(int)
    df["l"] = df["is_loss"].astype(int)

    # all-play wins/losses are floats; cum-sum fine
    milestones = [
        ("Total Wins - 25",  "Reached 25 total wins in league history.",  "w", 25),
        ("Total Wins - 50",  "Reached 50 total wins in league history.",  "w", 50),
        ("Total Wins - 100", "Reached 100 total wins in league history.", "w", 100),
        ("Total Wins - 250", "Reached 250 total wins in league history.", "w", 250),

        ("All-Play Wins - 250",  "Reached 250 all-play wins in league history.",   "all_play_wins", 250),
        ("All-Play Wins - 500",  "Reached 500 all-play wins in league history.",   "all_play_wins", 500),
        ("All-Play Wins - 1000", "Reached 1,000 all-play wins in league history.", "all_play_wins", 1000),
        ("All-Play Wins - 2500", "Reached 2,500 all-play wins in league history.", "all_play_wins", 2500),

        ("Total Points - 10000", "Reached 10,000 total points scored in league history.", "points_for", 10000),
        ("Total Points - 25000", "Reached 25,000 total points scored in league history.", "points_for", 25000),
        ("Total Points - 50000", "Reached 50,000 total points scored in league history.", "points_for", 50000),

        ("Total Losses - 25",  "Reached 25 total losses in league history.",  "l", 25),
        ("Total Losses - 50",  "Reached 50 total losses in league history.",  "l", 50),
        ("Total Losses - 100", "Reached 100 total losses in league history.", "l", 100),
        ("Total Losses - 250", "Reached 250 total losses in league history.", "l", 250),

        ("All-Play Losses - 250",  "Reached 250 all-play losses in league history.",   "all_play_losses", 250),
        ("All-Play Losses - 500",  "Reached 500 all-play losses in league history.",   "all_play_losses", 500),
        ("All-Play Losses - 1000", "Reached 1,000 all-play losses in league history.", "all_play_losses", 1000),
        ("All-Play Losses - 2500", "Reached 2,500 all-play losses in league history.", "all_play_losses", 2500),

        ("Total Opponent Points - 10000", "Opponents have scored 10,000 total points against this team.", "points_against", 10000),
        ("Total Opponent Points - 25000", "Opponents have scored 25,000 total points against this team.", "points_against", 25000),
        ("Total Opponent Points - 50000", "Opponents have scored 50,000 total points against this team.", "points_against", 50000),
    ]

    for team, g in df.groupby("team"):
        g = g.copy()
        # cumulative columns
        g["cum_w"] = g["w"].cumsum()
        g["cum_l"] = g["l"].cumsum()
        g["cum_ap_w"] = g["all_play_wins"].cumsum()
        g["cum_ap_l"] = g["all_play_losses"].cumsum()
        g["cum_pf"] = g["points_for"].cumsum()
        g["cum_pa"] = g["points_against"].cumsum()

        mapping = {
            "w": "cum_w",
            "l": "cum_l",
            "all_play_wins": "cum_ap_w",
            "all_play_losses": "cum_ap_l",
            "points_for": "cum_pf",
            "points_against": "cum_pa",
        }

        for ach_name, desc, base_col, threshold in milestones:
            cum_col = mapping[base_col]
            hit = g[g[cum_col] >= threshold]
            if hit.empty:
                continue
            first = hit.iloc[0]
            add(
                out,
                category="League Achievement",
                achievement=ach_name,
                description=desc,
                team=team,
                season=int(first["season"]),
                week=int(first["week"]),
                value=float(threshold),
                scope=scope,
            )

    return out


# -------------------------
# Champion / Season-based achievements
# -------------------------

def champion_and_season_achievements(tg: pd.DataFrame, cfg: dict) -> List[Achievement]:
    out: List[Achievement] = []

    reg_rank, final_rank = build_rank_tables(tg, cfg)
    reg_sum, po_sum, season_ls = season_team_summaries(tg, cfg)

    # Team count per season
    team_count = reg_sum.groupby("season")["team"].nunique().to_dict()

    # Season Titles
    # - Season Title = reg_rank == 1
    if not reg_rank.empty:
        for _, r in reg_rank[reg_rank["reg_rank"] == 1].iterrows():
            add(out,
                category="Champion",
                achievement="Season Title",
                description="Team that finishes 1st in the regular-season standings.",
                team=r["team"],
                season=int(r["season"]),
                value=1.0
            )

    # Season Points Title (regular season PF max)
    for season, g in reg_sum.groupby("season"):
        if g.empty:
            continue
        mx = g["reg_points_for"].max()
        winners = g[g["reg_points_for"] == mx]
        for _, r in winners.iterrows():
            add(out,
                category="Champion",
                achievement="Season Points Title",
                description="Team with the most points scored in the regular season.",
                team=r["team"],
                season=int(season),
                value=float(mx),
            )

    # Season All-Play Title
    for season, g in reg_sum.groupby("season"):
        if g.empty:
            continue
        mx = g["reg_all_play_wins"].max()
        winners = g[g["reg_all_play_wins"] == mx]
        for _, r in winners.iterrows():
            add(out,
                category="Champion",
                achievement="Season All-Play Title",
                description="Team with the best all-play record for the season.",
                team=r["team"],
                season=int(season),
                value=float(mx),
            )

    # Triple Crown / Grand Slam
    # Build season winners sets
    season_title = set()
    if not reg_rank.empty:
        season_title = set((int(s), t) for s, t in zip(reg_rank["season"], reg_rank["team"]) if int(reg_rank.loc[(reg_rank["season"] == s) & (reg_rank["team"] == t), "reg_rank"].iloc[0]) == 1)

    # easier: compute dicts
    season_title_winners = {}
    if not reg_rank.empty:
        for s, g in reg_rank.groupby("season"):
            w = g[g["reg_rank"] == 1]["team"].tolist()
            season_title_winners[int(s)] = set(w)

    points_title_winners = {}
    for s, g in reg_sum.groupby("season"):
        mx = g["reg_points_for"].max()
        points_title_winners[int(s)] = set(g[g["reg_points_for"] == mx]["team"].tolist())

    allplay_title_winners = {}
    for s, g in reg_sum.groupby("season"):
        mx = g["reg_all_play_wins"].max()
        allplay_title_winners[int(s)] = set(g[g["reg_all_play_wins"] == mx]["team"].tolist())

    # Champions from final_rank == 1
    champ_winners = {}
    if not final_rank.empty:
        for s, g in final_rank.groupby("season"):
            champ_winners[int(s)] = set(g[g["final_rank"] == 1]["team"].tolist())

    for season in sorted(reg_sum["season"].unique()):
        s = int(season)
        for team in sorted(set(reg_sum[reg_sum["season"] == s]["team"].tolist())):
            if team in season_title_winners.get(s, set()) and team in points_title_winners.get(s, set()) and team in allplay_title_winners.get(s, set()):
                add(out,
                    category="Champion",
                    achievement="Triple Crown",
                    description="Wins Season Title, Season Points Title and Season All-Play Title in the same season.",
                    team=team,
                    season=s
                )
            if (team in season_title_winners.get(s, set())
                and team in points_title_winners.get(s, set())
                and team in allplay_title_winners.get(s, set())
                and team in champ_winners.get(s, set())):
                add(out,
                    category="Champion",
                    achievement="Grand Slam",
                    description="In one season, wins Season Title, Points Title, All-Play Title and the Championship.",
                    team=team,
                    season=s
                )

    # Champion
    if not final_rank.empty:
        for _, r in final_rank[final_rank["final_rank"] == 1].iterrows():
            add(out,
                category="Champion",
                achievement="Champion",
                description="Wins the playoffs and becomes league champion.",
                team=r["team"],
                season=int(r["season"]),
            )

    # Back-to-back / three-peat + career count milestones (4th/5th/6th)
    if not final_rank.empty:
        fr = final_rank.copy().sort_values(["season"])
        # champs per season (allow ties)
        champ_by_season = {int(s): set(g[g["final_rank"] == 1]["team"].tolist()) for s, g in fr.groupby("season")}
        seasons_sorted = sorted(champ_by_season.keys())

        # per-team champ seasons list
        champ_seasons_by_team: Dict[str, List[int]] = {}
        for s in seasons_sorted:
            for t in champ_by_season[s]:
                champ_seasons_by_team.setdefault(t, []).append(s)

        for team, seasons in champ_seasons_by_team.items():
            seasons = sorted(seasons)
            # consecutive streaks
            for i in range(len(seasons) - 1):
                if seasons[i + 1] == seasons[i] + 1:
                    add(out, category="Champion", achievement="Back-to-Back Champ",
                        description="Wins two championships in consecutive seasons.",
                        team=team, season=seasons[i + 1])
            for i in range(len(seasons) - 2):
                if seasons[i + 1] == seasons[i] + 1 and seasons[i + 2] == seasons[i] + 2:
                    add(out, category="Champion", achievement="Three-peat Champ",
                        description="Wins three championships in a row.",
                        team=team, season=seasons[i + 2])

            # career championship count milestones
            # award at season when reaching 4/5/6
            for target, name, desc in [
                (4, "Four-Told", "Wins their 4th career championship."),
                (5, "High-Five", "Wins their 5th career championship."),
                (6, "The Jordan", "Wins their 6th career championship."),
            ]:
                if len(seasons) >= target:
                    add(out, category="Champion", achievement=name, description=desc, team=team, season=seasons[target - 1])

            # The Ringer: championship in their first season in league
            first_season = int(reg_sum[reg_sum["team"] == team]["season"].min())
            if seasons and seasons[0] == first_season:
                add(out, category="Champion", achievement="The Ringer",
                    description="Wins the championship in their first season in the league.",
                    team=team, season=seasons[0])

            # Dynasty: 3 championships within 5-year span
            # award at season of 3rd title when first satisfied
            for i in range(len(seasons) - 2):
                window = seasons[i:i+3]
                if window[-1] - window[0] <= 4:
                    add(out, category="Champion", achievement="Dynasty",
                        description="Wins three championships within a five-year span.",
                        team=team, season=window[-1])
                    break

    # Immaculate Season: undefeated regular + playoffs AND champion
    # define undefeated as (reg_losses + po_losses) == 0 and champion
    if not final_rank.empty:
        champs = set(final_rank[final_rank["final_rank"] == 1]["team"].tolist())
        merged = reg_sum.merge(po_sum, on=["season", "team"], how="left").fillna(0)
        for _, r in merged.iterrows():
            season = int(r["season"])
            team = r["team"]
            if team not in champ_winners.get(season, set()):
                continue
            if int(r["reg_losses"]) == 0 and int(r.get("po_losses", 0)) == 0:
                add(out, category="Champion", achievement="Immaculate Season",
                    description="Goes undefeated in the regular season and playoffs.",
                    team=team, season=season)

    # Phoenix Award: last previous season -> champ this season
    if not final_rank.empty:
        # last means final_rank == team_count
        fr = final_rank.copy()
        for team in sorted(fr["team"].unique()):
            tfr = fr[fr["team"] == team].sort_values("season")
            for i in range(1, len(tfr)):
                prev = tfr.iloc[i - 1]
                cur = tfr.iloc[i]
                prev_season = int(prev["season"])
                cur_season = int(cur["season"])
                tc = int(team_count.get(prev_season, 0) or 0)
                if tc <= 0:
                    continue
                if int(prev["final_rank"]) == tc and int(cur["final_rank"]) == 1:
                    add(out, category="Champion", achievement="Phoenix Award",
                        description="Finished last the previous season and wins the championship this season.",
                        team=team, season=cur_season)

    # Heavyweight Champion / Cupcake Champion: toughest/easiest SOS & champ
    # Against the Odds / Fortune Favored: min/max luck & champ
    if not final_rank.empty:
        champs_by_season = {int(s): set(g[g["final_rank"] == 1]["team"].tolist()) for s, g in final_rank.groupby("season")}
        for season, g in season_ls.groupby("season"):
            s = int(season)
            if g.empty:
                continue
            champs = champs_by_season.get(s, set())
            if not champs:
                continue

            # toughest schedule => max season_sos
            max_sos = g["season_sos"].max()
            min_sos = g["season_sos"].min()
            toughest = set(g[g["season_sos"] == max_sos]["team"].tolist())
            easiest = set(g[g["season_sos"] == min_sos]["team"].tolist())

            # luck
            max_luck = g["season_luck"].max()
            min_luck = g["season_luck"].min()
            luckiest = set(g[g["season_luck"] == max_luck]["team"].tolist())
            unluckiest = set(g[g["season_luck"] == min_luck]["team"].tolist())

            for team in champs:
                if team in toughest:
                    add(out, category="Champion", achievement="Heavyweight Champion",
                        description="Wins the championship despite having the toughest schedule.",
                        team=team, season=s)
                if team in easiest:
                    add(out, category="Champion", achievement="Cupcake Champion",
                        description="Wins the championship with the easiest schedule of the season.",
                        team=team, season=s)
                if team in unluckiest:
                    add(out, category="Champion", achievement="Against the Odds",
                        description="Wins the championship despite having the most bad luck in the season.",
                        team=team, season=s)
                if team in luckiest:
                    add(out, category="Champion", achievement="Fortune Favored",
                        description="Wins the championship with maximum measured luck.",
                        team=team, season=s)

    # Playoff Appearances milestones
    # based on reg_rank <= playoff_teams
    playoff_teams = int(cfg.get("playoff_teams", 4))
    if not reg_rank.empty:
        rr = reg_rank.copy()
        rr["made_playoffs"] = rr["reg_rank"] <= playoff_teams
        for team, g in rr.sort_values("season").groupby("team"):
            count = 0
            for _, r in g.iterrows():
                if bool(r["made_playoffs"]):
                    count += 1
                    if count in {5, 10, 20}:
                        add(out, category="Champion",
                            achievement=f"Playoff Appearances - {count}",
                            description=f"Reaches the playoffs for the {count}th time.",
                            team=team, season=int(r["season"]), value=float(count))

    # The Worst / Still / Always Worst (final_rank max)
    if not final_rank.empty:
        fr = final_rank.copy()
        for season, g in fr.groupby("season"):
            s = int(season)
            tc = int(team_count.get(s, 0) or 0)
            if tc <= 0:
                continue
            losers = g[g["final_rank"] == tc]["team"].tolist()
            for team in losers:
                add(out, category="Champion",
                    achievement="The Worst",
                    description="Finishes dead last in the league.",
                    team=team, season=s)

        # consecutive last-place streaks
        for team, g in fr.sort_values("season").groupby("team"):
            g2 = g.copy()
            flags = []
            seasons = g2["season"].astype(int).tolist()
            for _, r in g2.iterrows():
                s = int(r["season"])
                tc = int(team_count.get(s, 0) or 0)
                flags.append(tc > 0 and int(r["final_rank"]) == tc)

            # scan for 2 / 3 in a row
            for i in range(len(flags) - 1):
                if flags[i] and flags[i+1] and seasons[i+1] == seasons[i] + 1:
                    add(out, category="Champion", achievement="Still the Worst",
                        description="Finishes last in two consecutive seasons.",
                        team=team, season=seasons[i+1])
            for i in range(len(flags) - 2):
                if (flags[i] and flags[i+1] and flags[i+2]
                    and seasons[i+1] == seasons[i] + 1 and seasons[i+2] == seasons[i] + 2):
                    add(out, category="Champion", achievement="Always the Worst",
                        description="Finishes last in three consecutive seasons.",
                        team=team, season=seasons[i+2])

    # Trash Trifecta / Flawless Garbage
    # Trash Trifecta: last in standings (reg_rank max), points, all-play
    # Flawless Garbage: + overall finish last (final_rank max)
    if not reg_rank.empty:
        for season in sorted(reg_sum["season"].unique()):
            s = int(season)
            season_reg = reg_sum[reg_sum["season"] == s]
            if season_reg.empty:
                continue

            # points last
            min_pf = season_reg["reg_points_for"].min()
            points_last = set(season_reg[season_reg["reg_points_for"] == min_pf]["team"].tolist())

            # all-play last
            min_ap = season_reg["reg_all_play_wins"].min()
            ap_last = set(season_reg[season_reg["reg_all_play_wins"] == min_ap]["team"].tolist())

            # standings last
            rr = reg_rank[reg_rank["season"] == s]
            if rr.empty:
                continue
            max_rank = rr["reg_rank"].max()
            standings_last = set(rr[rr["reg_rank"] == max_rank]["team"].tolist())

            trifecta = standings_last & points_last & ap_last
            for team in trifecta:
                add(out, category="Champion", achievement="Trash Trifecta",
                    description="In the same season, finishes last in standings, points scored and all-play.",
                    team=team, season=s)

            # flawless garbage also last overall
            if not final_rank.empty:
                fr = final_rank[final_rank["season"] == s]
                if not fr.empty:
                    tc = int(team_count.get(s, 0) or 0)
                    overall_last = set(fr[fr["final_rank"] == tc]["team"].tolist()) if tc > 0 else set()
                    for team in trifecta & overall_last:
                        add(out, category="Champion", achievement="Flawless Garbage",
                            description="In the same season, finishes last in standings, points, all-play and overall finish.",
                            team=team, season=s)

    # Imperfection: 0 wins regular AND 0 wins playoffs
    merged = reg_sum.merge(po_sum, on=["season", "team"], how="left").fillna(0)
    for _, r in merged.iterrows():
        if int(r["reg_wins"]) == 0 and int(r.get("po_wins", 0)) == 0:
            add(out, category="Champion", achievement="Imperfection",
                description="Zero wins in both the regular season and playoffs.",
                team=r["team"], season=int(r["season"]))

    # Ultimate Fumble: undefeated regular season but not champion
    if not final_rank.empty:
        champs_by_season = {int(s): set(g[g["final_rank"] == 1]["team"].tolist()) for s, g in final_rank.groupby("season")}
        for _, r in reg_sum.iterrows():
            s = int(r["season"])
            team = r["team"]
            if int(r["reg_losses"]) == 0 and team not in champs_by_season.get(s, set()):
                add(out, category="Champion", achievement="Ultimate Fumble",
                    description="Goes undefeated in the regular season but does not win the championship.",
                    team=team, season=s)

    # Icarus Award: champ one year, last next year
    if not final_rank.empty:
        fr = final_rank.copy().sort_values("season")
        for team, g in fr.groupby("team"):
            g = g.sort_values("season").reset_index(drop=True)
            for i in range(len(g) - 1):
                s1 = int(g.loc[i, "season"])
                s2 = int(g.loc[i+1, "season"])
                if s2 != s1 + 1:
                    continue
                tc2 = int(team_count.get(s2, 0) or 0)
                if tc2 <= 0:
                    continue
                if int(g.loc[i, "final_rank"]) == 1 and int(g.loc[i+1, "final_rank"]) == tc2:
                    add(out, category="Champion", achievement="Icarus Award",
                        description="Wins the championship one year, finishes last the next.",
                        team=team, season=s2)

    # Silverback-to-back / Bronze Age (final ranks 2/3 consecutive)
    if not final_rank.empty:
        fr = final_rank.copy().sort_values("season")
        for team, g in fr.groupby("team"):
            g = g.sort_values("season").reset_index(drop=True)
            for i in range(len(g) - 1):
                s1 = int(g.loc[i, "season"])
                s2 = int(g.loc[i+1, "season"])
                if s2 != s1 + 1:
                    continue
                if int(g.loc[i, "final_rank"]) == 2 and int(g.loc[i+1, "final_rank"]) == 2:
                    add(out, category="Champion", achievement="Silverback-to-back",
                        description="Finishes 2nd place in back-to-back seasons.",
                        team=team, season=s2)
                if int(g.loc[i, "final_rank"]) == 3 and int(g.loc[i+1, "final_rank"]) == 3:
                    add(out, category="Champion", achievement="A Bronze Age",
                        description="Finishes 3rd place in back-to-back seasons.",
                        team=team, season=s2)

    # Comeback Kid: starts 0-5 but wins championship
    start_n = int(cfg.get("start_0_5_weeks", 5))
    if not final_rank.empty:
        champs_by_season = {int(s): set(g[g["final_rank"] == 1]["team"].tolist()) for s, g in final_rank.groupby("season")}
        reg = tg[tg["is_playoff"] == False].copy()
        for (season, team), g in reg.sort_values(["week"]).groupby(["season", "team"]):
            s = int(season)
            g2 = g[g["week"] <= start_n]
            if len(g2) < start_n:
                continue
            if g2["is_loss"].all() and team in champs_by_season.get(s, set()):
                add(out, category="Champion", achievement="Comeback Kid",
                    description="Starts the season 0–5 but still wins the championship.",
                    team=team, season=s)

    # Slipped Away: starts undefeated but misses playoffs
    start_u = int(cfg.get("start_undefeated_weeks", 5))
    playoff_teams = int(cfg.get("playoff_teams", 4))
    if not reg_rank.empty:
        reg = tg[tg["is_playoff"] == False].copy()
        for (season, team), g in reg.sort_values(["week"]).groupby(["season", "team"]):
            s = int(season)
            g2 = g[g["week"] <= start_u]
            if len(g2) < start_u:
                continue
            started_undefeated = (g2["is_loss"].sum() == 0)
            if not started_undefeated:
                continue
            rr = reg_rank[(reg_rank["season"] == s) & (reg_rank["team"] == team)]
            if rr.empty:
                continue
            missed_playoffs = int(rr.iloc[0]["reg_rank"]) > playoff_teams
            if missed_playoffs:
                add(out, category="Champion", achievement="Slipped Away",
                    description="Starts the season undefeated but still misses the playoffs.",
                    team=team, season=s)

    # ---- Missing-data achievements (skeleton markers) ----
    for name, desc in [
        ("Draft King", "Has the best draft grade and goes on to win the championship."),
        ("Action King", "Has the best waiver/trade performance and goes on to win the championship."),
        ("Optimus Prime", "Has the best coach rating (optimal lineups) and goes on to win the championship."),
        ("Trash to Treasure", "Wins the championship despite having the worst draft grade."),
        ("Salvaged from Sabotage", "Wins the championship despite being the worst transaction manager."),
        ("Accidental King", "Wins the championship despite being the worst coach (lineup decisions)."),
        ("One-Man Show", "Has the highest share of the team’s total points in a season (one team carries the scoring)."),
        ("No-Show", "Has the lowest share of points scored in a season (least offensive contribution)."),
    ]:
        add(out, category="Champion", achievement=name, description=desc, team="(n/a)", status="SKIPPED_MISSING_DATA")

    return out


# -------------------------
# Season achievements (tiers + unique awards)
# -------------------------

def season_achievements(tg: pd.DataFrame, cfg: dict) -> List[Achievement]:
    out: List[Achievement] = []
    reg_sum, po_sum, season_ls = season_team_summaries(tg, cfg)

    # season score definition: reg_win_pct * 100
    reg_sum["season_score"] = reg_sum["reg_win_pct"] * 100.0

    def award_tier(team: str, season: int, name: str, desc: str, val: float):
        add(out, category="Season", achievement=name, description=desc, team=team, season=season, value=val)

    # tiers (per team per season)
    for _, r in reg_sum.iterrows():
        s = int(r["season"])
        team = r["team"]
        score = float(r["season_score"]) if pd.notna(r["season_score"]) else float("nan")

        # Diamond/Gold/Silver/Bronze + Iron/Wood/Clay
        if pd.notna(score):
            if score >= 95:
                award_tier(team, s, "Diamond Season", "Season score of 95+.", score)
            elif 90 <= score < 95:
                award_tier(team, s, "Gold Season", "Season score in the 90–95 range.", score)
            elif 85 <= score < 90:
                award_tier(team, s, "Silver Season", "Season score in the 85–90 range.", score)
            elif 80 <= score < 85:
                award_tier(team, s, "Bronze Season", "Season score in the 80–85 range.", score)
            elif 70 <= score < 80:
                award_tier(team, s, "Iron Season", "Season score in the 70–80 range.", score)
            elif 60 <= score < 70:
                award_tier(team, s, "Wood Season", "Season score in the 60–70 range.", score)
            elif 50 <= score < 60:
                award_tier(team, s, "Clay Season", "Season score in the 50–60 range.", score)

        # Flawless: undefeated regular season
        if int(r["reg_losses"]) == 0:
            add(out, category="Season", achievement="Flawless", description="Undefeated regular season (no regular-season losses).",
                team=team, season=s)

        # Goose Egg: zero regular-season wins
        if int(r["reg_wins"]) == 0:
            add(out, category="Season", achievement="Goose Egg", description="Zero regular-season wins.",
                team=team, season=s)

        # Even Steven: PF == PA in regular season
        if pd.notna(r["reg_points_for"]) and pd.notna(r["reg_points_against"]) and float(r["reg_points_for"]) == float(r["reg_points_against"]):
            add(out, category="Season", achievement="Even Steven", description="Finishes the season with equal points for and against.",
                team=team, season=s)

        # Average Joe: all-play wins == losses (regular season)
        apw = safe_float(r["reg_all_play_wins"])
        apl = safe_float(r["reg_all_play_losses"])
        if pd.notna(apw) and pd.notna(apl) and abs(apw - apl) < 1e-9:
            add(out, category="Season", achievement="Average Joe", description="All-play record with exactly as many wins as losses.",
                team=team, season=s)

        # High-scores thresholds
        if int(r["reg_high_scores"]) >= 5:
            add(out, category="Season", achievement="Season High-Scores - 5", description="Achieves five weekly high scores in a single season.",
                team=team, season=s, value=float(r["reg_high_scores"]))
        if int(r["reg_high_scores"]) >= 10:
            add(out, category="Season", achievement="Season High-Scores - 10", description="Achieves ten weekly high scores in a single season.",
                team=team, season=s, value=float(r["reg_high_scores"]))

        # Worst-scores thresholds
        if int(r["reg_worst_scores"]) >= 5:
            add(out, category="Season", achievement="Season Worst-Scores - 5", description="Records five weekly lowest scores in a single season.",
                team=team, season=s, value=float(r["reg_worst_scores"]))
        if int(r["reg_worst_scores"]) >= 10:
            add(out, category="Season", achievement="Season Worst-Scores - 10", description="Records ten weekly lowest scores in a single season.",
                team=team, season=s, value=float(r["reg_worst_scores"]))

        # Top/bottom half scoring thresholds
        weeks = reg_end_week(s, cfg)
        if weeks > 0:
            top_half_pct = float(r["reg_top_half"]) / float(weeks)
            bottom_half_pct = float(r["reg_bottom_half"]) / float(weeks)

            if top_half_pct >= 0.75:
                add(out, category="Season", achievement="Season Top-Half Scoring - 75%", description="Scores in the top half of the league in at least 75% of weeks.",
                    team=team, season=s, value=top_half_pct)
            if top_half_pct >= 1.0:
                add(out, category="Season", achievement="Season Top-Half Scoring - 100%", description="Scores in the top half of the league in 100% of weeks.",
                    team=team, season=s, value=top_half_pct)

            if bottom_half_pct >= 0.75:
                add(out, category="Season", achievement="Season Bottom-Half Scoring - 75%", description="Scores in the bottom half of the league in at least 75% of weeks.",
                    team=team, season=s, value=bottom_half_pct)
            if bottom_half_pct >= 1.0:
                add(out, category="Season", achievement="Season Bottom-Half Scoring - 100%", description="Scores in the bottom half of the league in every week.",
                    team=team, season=s, value=bottom_half_pct)

    # Unique per season:
    # The Gauntlet (toughest schedule) / The Cupcake (easiest)
    # Lady Luck (least luck) / Cursed (same as least luck, “heavily cursed”)
    for season, g in season_ls.groupby("season"):
        s = int(season)
        if g.empty:
            continue
        max_sos = g["season_sos"].max()
        min_sos = g["season_sos"].min()
        min_luck = g["season_luck"].min()

        for team in g[g["season_sos"] == max_sos]["team"].tolist():
            add(out, category="Season", achievement="The Gauntlet", description="Season with the toughest schedule in the league.",
                team=team, season=s, value=float(max_sos))
        for team in g[g["season_sos"] == min_sos]["team"].tolist():
            add(out, category="Season", achievement="The Cupcake", description="Plays the season with the easiest opponent schedule.",
                team=team, season=s, value=float(min_sos))
        for team in g[g["season_luck"] == min_luck]["team"].tolist():
            add(out, category="Season", achievement="Lady Luck", description="Team with the least amount of luck in the season.",
                team=team, season=s, value=float(min_luck))
            add(out, category="Season", achievement="Cursed", description="Team with minimal luck in the season (heavily “cursed”).",
                team=team, season=s, value=float(min_luck))

    return out


# -------------------------
# Matchup achievements (per season)
# -------------------------

def matchup_achievements(tg: pd.DataFrame, cfg: dict) -> List[Achievement]:
    out: List[Achievement] = []
    firing_share = float(cfg.get("firing_squad_share", 0.90))
    double_up = float(cfg.get("double_up_factor", 2.0))

    # We'll work per season
    for season, g in tg.groupby("season"):
        s = int(season)
        g = g.copy()

        # team-level extremes per season
        # Peak Performance (max points_for)
        idx = g["points_for"].astype(float).idxmax()
        r = g.loc[idx]
        add(out, category="Matchup Achievements", achievement="Peak Performance",
            description="Highest single-team score in a matchup for the season.",
            team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=float(r["points_for"]))

        # Collapse to unique matchups
        matchups = g.groupby(["matchup_key"], as_index=False).agg(
            season=("season", "first"),
            week=("week", "first"),
            is_playoff=("is_playoff", "first"),
            combined=("points_for", "sum"),  # because each matchup appears twice (team rows), sum=PF+PA twice? Wait:
        )
        # Fix combined: since both rows contain PF and PA swapped, sum(points_for) equals PF+PA (correct).
        # Great: combined = sum(points_for) across two rows.

        # Highest combined / lowest combined
        mx = matchups["combined"].max()
        mn = matchups["combined"].min()
        shoot = matchups[matchups["combined"] == mx]
        dud = matchups[matchups["combined"] == mn]

        # Participants in a matchup key
        def participants(key: str) -> pd.DataFrame:
            return g[g["matchup_key"] == key][["team", "opponent", "points_for", "points_against", "margin", "points_share", "week"]].copy()

        for _, mrow in shoot.iterrows():
            part = participants(mrow["matchup_key"])
            for _, pr in part.iterrows():
                add(out, category="Matchup Achievements", achievement="The Shootout",
                    description="Participates in the matchup with the highest combined points of the season.",
                    team=pr["team"], season=s, week=int(pr["week"]), opponent=pr["opponent"], value=float(mrow["combined"]))

        for _, mrow in dud.iterrows():
            part = participants(mrow["matchup_key"])
            for _, pr in part.iterrows():
                add(out, category="Matchup Achievements", achievement="The Dud Bowl",
                    description="Matchup with the fewest combined points in the season.",
                    team=pr["team"], season=s, week=int(pr["week"]), opponent=pr["opponent"], value=float(mrow["combined"]))
                add(out, category="Matchup Achievements", achievement="The Snoozer",
                    description="Participates in the matchup with the lowest combined score of the season.",
                    team=pr["team"], season=s, week=int(pr["week"]), opponent=pr["opponent"], value=float(mrow["combined"]))

        # Massacre (biggest blowout WIN) / The Bye Week (biggest blowout LOSS)
        wins_only = g[g["is_win"] == True].copy()
        if not wins_only.empty:
            idx = wins_only["margin"].astype(float).idxmax()
            wr = wins_only.loc[idx]
            add(out, category="Matchup Achievements", achievement="Massacre",
                description="Records the biggest blowout win of the season.",
                team=wr["team"], season=s, week=int(wr["week"]), opponent=wr["opponent"], value=float(wr["margin"]))
            # corresponding loser (same matchup_key, other row)
            loser = g[(g["matchup_key"] == wr["matchup_key"]) & (g["team"] == wr["opponent"])]
            if not loser.empty:
                lr = loser.iloc[0]
                add(out, category="Matchup Achievements", achievement="The Bye Week",
                    description="Loses in the largest blowout of the season.",
                    team=lr["team"], season=s, week=int(lr["week"]), opponent=lr["opponent"], value=float(lr["margin"]))

        # Thread the Needle (narrowest margin of victory) / Heartbreaker (closest loss)
        wins_pos = g[g["margin"] > 0].copy()
        if not wins_pos.empty:
            idx = wins_pos["margin"].astype(float).idxmin()
            wr = wins_pos.loc[idx]
            add(out, category="Matchup Achievements", achievement="Thread the Needle",
                description="Records the narrowest margin of victory of the season.",
                team=wr["team"], season=s, week=int(wr["week"]), opponent=wr["opponent"], value=float(wr["margin"]))
            loser = g[(g["matchup_key"] == wr["matchup_key"]) & (g["team"] == wr["opponent"])]
            if not loser.empty:
                lr = loser.iloc[0]
                add(out, category="Matchup Achievements", achievement="Heartbreaker",
                    description="Loses the closest matchup of the season.",
                    team=lr["team"], season=s, week=int(lr["week"]), opponent=lr["opponent"], value=float(lr["margin"]))

        # Firing Squad (points_share >= threshold)
        fs = g[g["points_share"].astype(float) >= firing_share]
        for _, r in fs.iterrows():
            add(out, category="Matchup Achievements", achievement="Firing Squad",
                description="Has the highest points share in a matchup (e.g. 90%+ of total points).",
                team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=float(r["points_share"]))

        # Small/Micro/Nano victory + defeat
        def approx_equal(a: float, b: float, eps: float = 1e-9) -> bool:
            return abs(a - b) <= eps

        for _, r in g.iterrows():
            margin = float(r["margin"])
            if margin > 0:
                if margin < 1.0:
                    add(out, category="Matchup Achievements", achievement="A Small Victory",
                        description="Wins by less than 1.0 point.",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=margin)
                if margin < 0.5:
                    add(out, category="Matchup Achievements", achievement="A Micro Victory",
                        description="Wins by less than 0.5 points.",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=margin)
                if approx_equal(margin, 0.01, eps=1e-6):
                    add(out, category="Matchup Achievements", achievement="A Nano Victory",
                        description="Wins by exactly 0.01 points (smallest possible margin).",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=margin)

                # Double Up
                if float(r["points_for"]) >= double_up * float(r["points_against"]) and float(r["points_against"]) > 0:
                    add(out, category="Matchup Achievements", achievement="Double Up",
                        description="Scores at least double the opponent’s points.",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=float(r["points_for"]))

            elif margin < 0:
                if abs(margin) < 1.0:
                    add(out, category="Matchup Achievements", achievement="A Small Defeat",
                        description="Loses by less than 1.0 point.",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=margin)
                if abs(margin) < 0.5:
                    add(out, category="Matchup Achievements", achievement="A Micro Defeat",
                        description="Loses by less than 0.5 points.",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=margin)
                if approx_equal(abs(margin), 0.01, eps=1e-6):
                    add(out, category="Matchup Achievements", achievement="A Nano Defeat",
                        description="Loses by only 0.01 points.",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=margin)

                # Doubled Up
                if float(r["points_against"]) >= double_up * float(r["points_for"]) and float(r["points_for"]) > 0:
                    add(out, category="Matchup Achievements", achievement="Doubled Up",
                        description="Opponent scores at least double this team’s points.",
                        team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=float(r["points_against"]))

        # Perfectly Peaked:
        # winner beats opponent who has the 2nd-highest score of the week
        # -> opponent week_rank == 2 AND winner is win
        peaked = g[(g["is_win"] == True) & (g["week_rank"] == 1) & (g["opponent"].notna())].copy()
        # Opponent rank per row: we need join to opponent's row that week
        # Make map (week, team) -> week_rank
        wk_map = g.set_index(["week", "team"])["week_rank"].to_dict()
        for _, r in peaked.iterrows():
            opp_rank = wk_map.get((int(r["week"]), r["opponent"]), None)
            if opp_rank == 2:
                add(out, category="Matchup Achievements", achievement="Perfectly Peaked",
                    description="Beats the opponent who has the second-highest score of the week.",
                    team=r["team"], season=s, week=int(r["week"]), opponent=r["opponent"], value=float(r["points_for"]))

        # True Lowlight:
        # lowest weekly score loses to second-lowest scorer
        # -> loser week_rank == team_count AND opponent week_rank == team_count-1
        week_counts = g.groupby("week")["team"].nunique().to_dict()
        for _, r in g[g["is_loss"] == True].iterrows():
            wk = int(r["week"])
            n = int(week_counts.get(wk, 0))
            if n <= 1:
                continue
            opp_rank = wk_map.get((wk, r["opponent"]), None)
            if int(r["week_rank"]) == n and opp_rank == (n - 1):
                add(out, category="Matchup Achievements", achievement="True Lowlight",
                    description="Posts the lowest weekly score and loses to the second-lowest scorer.",
                    team=r["team"], season=s, week=wk, opponent=r["opponent"], value=float(r["points_for"]))

        # Spoiled Goods:
        # second-highest weekly score but loses to top scorer
        for _, r in g[g["is_loss"] == True].iterrows():
            wk = int(r["week"])
            opp_rank = wk_map.get((wk, r["opponent"]), None)
            if int(r["week_rank"]) == 2 and opp_rank == 1:
                add(out, category="Matchup Achievements", achievement="Spoiled Goods",
                    description="Has the second-highest weekly score but loses to the top scorer.",
                    team=r["team"], season=s, week=wk, opponent=r["opponent"], value=float(r["points_for"]))

        # Undercard:
        # team with lowest points_share in the season (award that team)
        idx = g["points_share"].astype(float).idxmin()
        ur = g.loc[idx]
        add(out, category="Matchup Achievements", achievement="The Undercard",
            description="Matchup where one team has the lowest points share of any game that season.",
            team=ur["team"], season=s, week=int(ur["week"]), opponent=ur["opponent"], value=float(ur["points_share"]))

        # Bully / Bullied: beat/lose to same opponent >= 3x in season
        # count wins per (team, opponent)
        wins = g[g["is_win"] == True].groupby(["team", "opponent"]).size().reset_index(name="wins_vs")
        for _, r in wins[wins["wins_vs"] >= 3].iterrows():
            add(out, category="Matchup Achievements", achievement="Bully",
                description="Beats the same opponent three or more times in one season.",
                team=r["team"], season=s, opponent=r["opponent"], value=float(r["wins_vs"]))

        losses = g[g["is_loss"] == True].groupby(["team", "opponent"]).size().reset_index(name="losses_vs")
        for _, r in losses[losses["losses_vs"] >= 3].iterrows():
            add(out, category="Matchup Achievements", achievement="Bullied",
                description="Loses to the same opponent three or more times in one season.",
                team=r["team"], season=s, opponent=r["opponent"], value=float(r["losses_vs"]))

    return out


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_config(repo_root)

    # We intentionally consume the enriched output from build_records
    tg_path = repo_root / "output" / "records" / "team_games_enriched.csv"
    if not tg_path.exists():
        raise SystemExit(f"Missing {tg_path}. Run scripts/build_records.py first.")

    tg = pd.read_csv(tg_path)

    required_cols = {
        "season", "week", "team", "opponent", "points_for", "points_against",
        "is_playoff", "is_win", "is_loss", "is_tie",
        "all_play_wins", "all_play_losses", "all_play_win_pct",
        "week_rank", "is_week_high", "is_week_low", "is_top_half", "is_bottom_half",
        "points_share"
    }
    missing = sorted(list(required_cols - set(tg.columns)))
    if missing:
        raise SystemExit(f"team_games_enriched.csv missing columns: {missing}")

    # Normalize dtypes
    tg["season"] = tg["season"].astype(int)
    tg["week"] = tg["week"].astype(int)
    tg["is_playoff"] = tg["is_playoff"].astype(bool)

    achievements: List[Achievement] = []
    achievements += milestone_achievements(tg, cfg)
    achievements += champion_and_season_achievements(tg, cfg)
    achievements += season_achievements(tg, cfg)
    achievements += matchup_achievements(tg, cfg)

    # Convert to tables
    rows = []
    for a in achievements:
        rows.append({
            "category": a.category,
            "achievement": a.achievement,
            "description": a.description,
            "team": a.team,
            "season": a.season,
            "week": a.week,
            "opponent": a.opponent,
            "value": a.value,
            "scope": a.scope,
            "status": a.status,
        })

    df = pd.DataFrame(rows)

    out_dir = repo_root / "output" / "achievements"
    out_dir.mkdir(parents=True, exist_ok=True)

    df.to_csv(out_dir / "achievements.csv", index=False)

    # JSON grouped by team
    grouped: Dict[str, List[dict]] = {}
    for _, r in df[df["status"] == "AWARDED"].sort_values(["team", "season", "week"], na_position="last").iterrows():
        grouped.setdefault(str(r["team"]), []).append({k: (None if pd.isna(v) else v) for k, v in r.to_dict().items()})

    with (out_dir / "achievements.json").open("w", encoding="utf-8") as f:
        json.dump(grouped, f, ensure_ascii=False, indent=2)

    print(f"OK: wrote {len(df)} achievements rows to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
