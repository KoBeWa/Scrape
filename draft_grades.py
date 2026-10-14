"""Draft pick values (actual_pts, vorp, projected_pts, grade4) for draft_picks.

Reproduces the formulas of the original Excel workbook
(output/PlayerRanks_with_vorp_draftpick.csv) from nflverse data:

  points     standard scoring (no PPR, INT -1, FG 3/4/5 by distance), weeks 1-16
  GP         weeks with a stat line or offensive snaps
  vorp       max(0, TTL - TTL of the replacement player): 9th best QB/TE/K/DST,
             21st best RB/WR among all players of the season
  projected  31.545 - 6.134 * ln(overall pick)
  grade2     0.3*VORP + 0.4*AVG - 0.15*Std - 0.2*(15 - GP) + 0.05*(wk15 + wk16)
  grade4     ROUND((grade2 - projected) * (1 + 0.7*pick/120) - 0.2*(1 - pick/120), 2)

Checked against 2024/2025: replacement levels exact, 104/120 resp. 111/120
grades exact (rest: DST scoring approximations, manual edits in the workbook).
During a running season the values are season-to-date.
"""

from __future__ import annotations

import io
import math
import re

import numpy as np
import pandas as pd
import requests

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
WEEKS = list(range(1, 17))
REPLACEMENT_RANK = {"QB": 8, "TE": 8, "K": 8, "DST": 8, "RB": 20, "WR": 20}
PICKS = 120

TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LA": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
SLEEPER_TEAM = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}


def round_half_up(x, n: int):
    """Excel ROUND (half away from zero); Python's round() would give 7.0 for 7.05."""
    return np.sign(x) * np.floor(np.abs(x) * 10 ** n + 0.5 + 1e-9) / 10 ** n


def _csv(url: str) -> pd.DataFrame | None:
    r = requests.get(url, timeout=300)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content), compression="gzip" if url.endswith(".gz") else None,
                       low_memory=False)


def _name_key(s: str) -> str:
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", str(s).lower())
    return re.sub(r"[^a-z]", "", s)


def _dst_tier(pa: float) -> int:
    return 10 if pa == 0 else 7 if pa <= 6 else 4 if pa <= 13 else 1 if pa <= 20 \
        else 0 if pa <= 27 else -1 if pa <= 34 else -4


def season_universe(season: int, nfl_players: list[dict]) -> pd.DataFrame | None:
    """One row per player/DST with weekly points (NaN = did not play)."""
    d = _csv(f"{NFLVERSE}/stats_player/stats_player_week_{season}.csv.gz")
    if d is None:
        return None
    d = d[(d.season_type == "REG") & (d.week <= 16)].copy()
    d = d.fillna({c: 0 for c in d.columns if d[c].dtype != object})
    col = lambda c: d[c] if c in d.columns else 0  # noqa: E731
    d = d.assign(pts=(
        0.04 * col("passing_yards") + 4 * col("passing_tds") - col("passing_interceptions")
        + 2 * col("passing_2pt_conversions")
        + 0.1 * col("rushing_yards") + 6 * col("rushing_tds") + 2 * col("rushing_2pt_conversions")
        + 0.1 * col("receiving_yards") + 6 * col("receiving_tds") + 2 * col("receiving_2pt_conversions")
        - 2 * col("fumbles_lost_total") + 6 * col("special_teams_tds")
        + 3 * (col("fg_made_0_19") + col("fg_made_20_29") + col("fg_made_30_39"))
        + 4 * col("fg_made_40_49") + 5 * (col("fg_made_50_59") + col("fg_made_60_"))
        + col("pat_made")
    ))
    # Every position stays in (e.g. Travis Hunter is listed as CB), but only
    # QB/RB/WR/TE/K count for the replacement levels
    d = d.assign(pos=d.position.fillna("").replace({"FB": "RB"}))
    weekly = d.pivot_table(index="player_id", columns="week", values="pts", aggfunc="sum")
    info = d.drop_duplicates("player_id").set_index("player_id")[["player_display_name", "pos"]]
    players = info.join(weekly)

    # Weeks with offensive snaps but no stat line count as played with 0 points
    snaps = _csv(f"{NFLVERSE}/snap_counts/snap_counts_{season}.csv.gz")
    if snaps is not None:
        pfr_to_gsis = {p["pfr_id"]: p["gsis_id"] for p in nfl_players if p.get("pfr_id")}
        snaps = snaps[(snaps.game_type == "REG") & (snaps.week <= 16) & (snaps.offense_snaps.fillna(0) > 0)]
        for pfr, week in zip(snaps.pfr_player_id, snaps.week):
            gsis = pfr_to_gsis.get(pfr)
            if gsis in players.index:
                if week not in players.columns:
                    players[week] = np.nan
                if pd.isna(players.at[gsis, week]):
                    players.at[gsis, week] = 0.0

    # Team defenses: standard scoring incl. points-allowed tiers
    t = _csv(f"{NFLVERSE}/stats_team/stats_team_week_{season}.csv.gz")
    g = _csv(f"{NFLVERSE}/schedules/games.csv")
    dst = pd.DataFrame()
    if t is not None and g is not None:
        t = t[(t.season_type == "REG") & (t.week <= 16)].copy()
        t = t.fillna({c: 0 for c in t.columns if t[c].dtype != object})
        g = g[(g.season == season) & (g.game_type == "REG")]
        pa = {}
        for r in g.itertuples():
            pa[(r.home_team, r.week)] = r.away_score
            pa[(r.away_team, r.week)] = r.home_score
        tc = lambda c: t[c] if c in t.columns else 0  # noqa: E731
        allowed = [pa.get((a, w)) for a, w in zip(t.team, t.week)]
        t = t.assign(pts=(
            tc("def_sacks") + 2 * tc("def_interceptions") + 2 * tc("fumble_recovery_opp")
            + 6 * (tc("def_tds") + tc("special_teams_tds")) + 2 * tc("def_safeties")
            + 2 * (tc("def_fg_blocks") + tc("def_punt_blocks") + tc("def_pat_blocks"))
            + pd.Series([_dst_tier(x) if x == x and x is not None else 0 for x in allowed], index=t.index)
        ))
        dw = t.pivot_table(index="team", columns="week", values="pts", aggfunc="sum")
        dst = pd.DataFrame({"player_display_name": [TEAM_NAMES.get(a, a) for a in dw.index],
                            "pos": "DST"}, index="DST:" + dw.index).join(dw.set_axis("DST:" + dw.index))

    u = pd.concat([players, dst])
    for w in WEEKS:
        if w not in u.columns:
            u[w] = np.nan
    # The total uses unrounded weekly points, the weekly cells are shown rounded
    u["TTL"] = u[WEEKS].astype(float).sum(axis=1).round(1)
    u[WEEKS] = u[WEEKS].astype(float).round(1)
    u["GP"] = u[WEEKS].notna().sum(axis=1)
    rep = {p: grp.TTL.sort_values(ascending=False).values[REPLACEMENT_RANK[p]]
           for p, grp in u.groupby("pos") if p in REPLACEMENT_RANK and len(grp) > REPLACEMENT_RANK[p]}
    u["VORP"] = (u.TTL - u.pos.map(rep)).clip(lower=0).round(1).fillna(0)
    u["AVG"] = round_half_up(u.TTL / u.GP.replace(0, np.nan), 1).fillna(0)
    u["STD"] = u[WEEKS].std(axis=1, ddof=0).fillna(0)
    u["GRADE2"] = (0.3 * u.VORP + 0.4 * u.AVG - 0.15 * u.STD - 0.2 * (15 - u.GP)
                   + 0.05 * u[[15, 16]].fillna(0).sum(axis=1))
    u["key"] = u.player_display_name.map(_name_key) + "|" + u.pos.fillna("").astype(str)
    return u


def projected(pick: int) -> float:
    return 31.545 - 6.134 * math.log(pick)


def grade4(grade2: float, pick: int) -> float:
    g3 = grade2 - projected(pick)
    return float(round_half_up(g3 * (1 + 0.7 * pick / PICKS) - 0.2 * (1 - pick / PICKS), 2))


def draft_rows(season: int, picks: list[dict], managers: dict[int, str], sleeper_players: dict,
               espn, nfl_players: list[dict], log=print) -> list[dict]:
    """Rows for public.draft_picks from Sleeper draft picks."""
    u = season_universe(season, nfl_players)
    if u is None:
        log("  nflverse has no player stats yet – draft values left empty")
    rows = []
    for p in picks:
        pid = str(p.get("player_id") or "")
        meta = p.get("metadata") or {}
        sp = sleeper_players.get(pid) or {}
        pos = (meta.get("position") or sp.get("position") or "").upper()
        is_def = pos == "DEF"
        pos = "DST" if is_def else pos
        if is_def:
            abbr = SLEEPER_TEAM.get(pid, pid)
            name = TEAM_NAMES.get(abbr, f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip())
            key = "DST:" + abbr
        else:
            name = sp.get("full_name") or f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
            key = None
        pick = int(p["pick_no"])
        row = {
            "season_year": season, "manager_id": managers.get(p.get("roster_id")),
            "round": int(p["round"]), "overall_pick": pick, "player_name": name, "position": pos,
            "projected_pts": round(projected(pick), 2),
            "sleeper_id": None if is_def else (pid or None),
            "espn_id": None if is_def else (espn.get(pid) or None),
            "actual_pts": None, "vorp": None, "grade4": None,
        }
        if u is not None:
            hit = None
            if key is not None:
                hit = u.loc[key] if key in u.index else None
            else:
                gsis = (sp.get("gsis_id") or "").strip()
                if gsis and gsis in u.index:
                    hit = u.loc[gsis]
                else:
                    m = u[u.key == f"{_name_key(name)}|{pos}"]
                    hit = m.iloc[0] if len(m) == 1 else None
            if hit is None:
                row.update(actual_pts=0.0, vorp=0.0, grade4=grade4(-3.0, pick))
            else:
                row.update(actual_pts=round(float(hit.TTL), 2), vorp=round(float(hit.VORP), 2),
                           grade4=grade4(float(hit.GRADE2), pick))
        rows.append(row)
    return rows
