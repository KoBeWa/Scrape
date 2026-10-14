"""fa_transactions and fa_transaction_scores from Sleeper transactions.

Semantics reverse-engineered from the 2015-2024 rows:

fa_transactions (one row per player and side)
  transaction_type  add / drop / add/drop, trade for Sleeper trades
  team_name         manager id; gains from `adds`, losses from `drops`
  transaction_week  Sleeper leg (week 1 includes all preseason moves)
  item_value        round((P_rest - R) * (17 - w) / 16, 2) with
                    P_rest = PPR points of the player in weeks w..16 and
                    R      = same sum of the 9th best QB/TE/K/DEF resp.
                             17th best RB/WR (replacement level)

fa_transaction_scores (one row per gain x loss of a team in a transaction)
  window    weeks gain_week+1 .. week the manager drops the player again (else 16)
  *_pts     standard (non-PPR) points in the window, every rushing/receiving
            fumble -2, DEF counts 0
  *_pts_adj sum over game weeks of (points - mean of all players at that
            position who scored > 0 that week)
  transaction_score(_alltime) percent rank of net_pts_adj per season / overall,
            set in the database (public.refresh_transaction_scores())
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from draft_grades import NFLVERSE, SLEEPER_TEAM, TEAM_NAMES, _csv, dst_weekly

LAST_WEEK = 16
REPLACEMENT_RANK = {"QB": 8, "TE": 8, "K": 8, "DEF": 8, "RB": 16, "WR": 16}
# DEF-00NN: index of the full team name in alphabetical order
DEF_ID = {abbr: f"DEF-{i:04d}" for i, abbr in
          enumerate(sorted(TEAM_NAMES, key=lambda a: TEAM_NAMES[a]), start=1)}


def weekly_points(season: int) -> pd.DataFrame | None:
    """Rows (gsis, week, pos, team, ppr, std); kickers 3/4/5 by distance + PAT."""
    d = _csv(f"{NFLVERSE}/stats_player/stats_player_week_{season}.csv.gz")
    if d is None:
        return None
    d = d[(d.season_type == "REG") & (d.week <= LAST_WEEK)].copy()
    num = [c for c in d.columns if d[c].dtype != object]
    d[num] = d[num].fillna(0)
    col = lambda c: d[c] if c in d.columns else 0  # noqa: E731
    kick = (3 * (col("fg_made_0_19") + col("fg_made_20_29") + col("fg_made_30_39"))
            + 4 * col("fg_made_40_49") + 5 * (col("fg_made_50_59") + col("fg_made_60_"))
            + col("pat_made"))
    is_k = d.position == "K"
    out = pd.DataFrame({
        "gsis": d.player_id, "week": d.week.astype(int), "pos": d.position.fillna(""),
        "team": d.team if "team" in d.columns else d.get("recent_team"),
        "ppr": np.where(is_k, kick, col("fantasy_points_ppr")),
        # The old scores count every rushing/receiving fumble (lost or not, but no
        # sack fumbles) as -2; matches the 2015-2024 rows
        "std": np.where(is_k, kick, col("fantasy_points") + 2 * col("fumbles_lost_total")
                        - 2 * (col("rushing_fumbles") + col("receiving_fumbles"))),
    })
    dw = dst_weekly(season)
    if dw is not None:
        dst = dw.stack().rename("ppr").reset_index()
        dst.columns = ["team", "week", "ppr"]
        dst = dst.assign(gsis=dst.team.map(lambda a: DEF_ID.get(a, f"DEF-{a}")), pos="DEF",
                         std=0.0, week=dst.week.astype(int))
        out = pd.concat([out, dst[out.columns]], ignore_index=True)
    return out


def _rest_sums(pts: pd.DataFrame, week: int) -> pd.Series:
    return pts[pts.week >= week].groupby("gsis").ppr.sum()


class Values:
    """item_value lookups, cached per week."""

    def __init__(self, pts: pd.DataFrame | None):
        self.pts = pts
        self.pos = {} if pts is None else pts.drop_duplicates("gsis").set_index("gsis").pos.to_dict()
        self.cache: dict[int, tuple[pd.Series, dict]] = {}

    def value(self, gsis: str | None, pos: str, week: int) -> float | None:
        if self.pts is None or not gsis or pos not in REPLACEMENT_RANK:
            return None
        week = max(1, min(week, LAST_WEEK))
        if week not in self.cache:
            rest = _rest_sums(self.pts, week)
            rep = {}
            for p, n in REPLACEMENT_RANK.items():
                ids = [g for g, q in self.pos.items() if q == p]
                vals = rest.reindex(ids).fillna(0).sort_values(ascending=False).values
                rep[p] = float(vals[n]) if len(vals) > n else 0.0
            self.cache[week] = (rest, rep)
        rest, rep = self.cache[week]
        p_rest = float(rest.get(gsis, 0.0))
        return round((p_rest - rep[pos]) * (17 - week) / 16, 2)


def _player(pid, players, ids, nfl_players_by_gsis):
    """(name, position, gsis, nfl team fallback) for a Sleeper player id."""
    p = players.get(pid) or {}
    if p.get("position") == "DEF" or pid in TEAM_NAMES or pid in SLEEPER_TEAM:
        abbr = SLEEPER_TEAM.get(pid, pid)
        return TEAM_NAMES.get(abbr, pid), "DEF", DEF_ID.get(abbr), abbr
    name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid
    gsis = ids.gsis(pid)
    team = p.get("team") or (nfl_players_by_gsis.get(gsis) or {}).get("latest_team") or "FA"
    return name, (p.get("position") or "").upper(), gsis, team


def transaction_rows(season: int, txs: list[dict], managers: dict[int, str], players: dict,
                     ids, nfl_players: list[dict]) -> tuple[list[dict], pd.DataFrame | None]:
    pts = weekly_points(season)
    values = Values(pts)
    team_at = {} if pts is None else {(g, w): t for g, w, t in zip(pts.gsis, pts.week, pts.team)}
    by_gsis = {r["gsis_id"]: r for r in nfl_players}
    rows = []
    for tx in sorted(txs, key=lambda t: (t.get("created") or 0, t["transaction_id"])):
        if tx.get("status") != "complete":
            continue
        week = int(tx.get("leg") or 1)
        if week > LAST_WEEK:
            continue
        adds, drops = tx.get("adds") or {}, tx.get("drops") or {}
        is_trade = tx.get("type") == "trade"
        at = datetime.fromtimestamp((tx.get("created") or 0) / 1000, tz=timezone.utc)
        sides: dict[str, list] = defaultdict(list)
        for pid, rid in adds.items():
            sides[managers.get(int(rid))].append((pid, "gain"))
        for pid, rid in drops.items():
            sides[managers.get(int(rid))].append((pid, "loss"))
        for team, items in sides.items():
            if team is None:
                continue
            dirs = {d for _, d in items}
            ttype = "trade" if is_trade else "add/drop" if dirs == {"gain", "loss"} else \
                "add" if dirs == {"gain"} else "drop"
            for pid, direction in items:
                name, pos, gsis, fallback_team = _player(pid, players, ids, by_gsis)
                nfl_team = team_at.get((gsis, week)) or next(
                    (team_at[(gsis, w)] for w in range(week, LAST_WEEK + 1) if (gsis, w) in team_at),
                    fallback_team)
                rows.append({
                    "season_year": season, "transaction_id": str(tx["transaction_id"]),
                    "transaction_type": ttype, "transaction_category": tx.get("type"),
                    "team_name": team, "transaction_week": week,
                    "transaction_at": at.strftime("%Y-%m-%dT%H:%M:%S"),
                    "item_id": ids.get(pid) or None, "item_direction": direction,
                    "player_id": gsis, "player_name": name, "position": pos,
                    "nfl_team": nfl_team, "item_value": values.value(gsis, pos, week),
                })
    return rows, pts


def score_rows(season: int, items: list[dict], pts: pd.DataFrame | None) -> list[dict]:
    """fa_transaction_scores without the percent ranks (set in SQL)."""
    std: dict[tuple[str, int], float] = {}
    base: dict[tuple[str, int], float] = {}
    if pts is not None:
        std = {(g, w): float(v) for g, w, v in zip(pts.gsis, pts.week, pts["std"])}
        pos_week = pts[(pts["std"] > 0) & (pts.pos != "DEF")].groupby(["pos", "week"])["std"].mean()
        base = {(p, w): float(v) for (p, w), v in pos_week.items()}
    pos_of = {} if pts is None else pts.drop_duplicates("gsis").set_index("gsis").pos.to_dict()

    # later losses of a player by the same manager end the window
    losses = defaultdict(list)
    for it in items:
        if it["item_direction"] == "loss" and it["player_id"]:
            losses[(it["team_name"], it["player_id"])].append(it["transaction_week"])

    def window_pts(gsis, first, last):
        total = adj = 0.0
        games = 0
        if not gsis or gsis.startswith("DEF"):
            return 0.0, 0.0, 0
        for w in range(first, last + 1):
            if (gsis, w) in std:
                total += std[(gsis, w)]
                adj += std[(gsis, w)] - base.get((pos_of.get(gsis), w), 0.0)
                games += 1
        return total, adj, games

    groups = defaultdict(list)
    for it in items:
        groups[(it["transaction_id"], it["team_name"])].append(it)

    rows = []
    for (tid, team), group in groups.items():
        gains = [i for i in group if i["item_direction"] == "gain"]
        drops = [i for i in group if i["item_direction"] == "loss"]
        if not gains:
            continue
        gain_week = gains[0]["transaction_week"]
        add_pts = add_adj = drop_pts = drop_adj = 0.0
        window_weeks, window_end = 0, LAST_WEEK
        for g in gains:
            later = [w for w in losses.get((team, g["player_id"]), []) if w > gain_week]
            end = min(later) if later else LAST_WEEK
            window_end = end
            p, a, n = window_pts(g["player_id"], gain_week + 1, end)
            add_pts, add_adj, window_weeks = add_pts + p, add_adj + a, window_weeks + n
        for d in drops:
            p, a, _ = window_pts(d["player_id"], gain_week + 1, window_end)
            drop_pts, drop_adj = drop_pts + p, drop_adj + a
        for g in gains:
            for d in drops or [None]:
                rows.append({
                    "transaction_id": tid, "season_year": season, "gain_week": gain_week,
                    "transaction_at": g["transaction_at"], "manager_id": team,
                    "add_player": g["player_name"], "add_pos": g["position"], "add_pid": g["player_id"],
                    "drop_player": d and d["player_name"], "drop_pos": d and d["position"],
                    "drop_pid": d and d["player_id"], "drop_week": d and d["transaction_week"],
                    "window_end": window_end, "window_weeks": window_weeks,
                    "add_pts": round(add_pts, 2), "drop_pts": round(drop_pts, 2),
                    "add_pts_adj": round(add_adj, 2), "drop_pts_adj": round(drop_adj, 2),
                    "net_pts": round(add_pts - drop_pts, 2), "net_pts_adj": round(add_adj - drop_adj, 2),
                })
    return rows
