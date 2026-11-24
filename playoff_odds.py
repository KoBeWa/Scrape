"""Playoff odds for the running season (Monte Carlo).

Every remaining regular-season week is played many times. A team's score in a
week is drawn from a normal distribution around its expected score:

    expected = a * projection + (1 - a) * season average
      projection      Sleeper's player projections for that week, best possible
                      lineup from the current roster (byes and injuries project
                      to 0, so they are in here)
      season average  the team's actual points per game so far, pulled towards
                      the league average while there are few games
      a               PROJ_WEIGHT_NEXT for the next week, PROJ_WEIGHT_LATER after
                      that (rosters change, so far-off projections count less)
    spread = the team's week-to-week standard deviation, pulled towards the
             league's while there are few games

The table is sorted like the real one: wins, then points for. The top
`playoff_teams` make the playoffs. Weeks before the current one have no stored
projections, so their odds are computed from season averages only (model
"averages") to give the trend over the season.
"""

from __future__ import annotations

import math

import numpy as np

SIMS = 20000
PROJ_WEIGHT_NEXT = 0.65
PROJ_WEIGHT_LATER = 0.5
PRIOR_GAMES_MEAN = 3     # how many league-average games a team's average starts from
PRIOR_GAMES_SD = 6       # same for the spread
MIN_SD = 15.0


def team_params(played: dict[str, list[float]]) -> tuple[dict[str, float], dict[str, float], float]:
    """Shrunk mean and spread per team from the scores played so far."""
    all_scores = [s for v in played.values() for s in v]
    league_mean = float(np.mean(all_scores)) if all_scores else 110.0
    resid = [s - np.mean(v) for v in played.values() if len(v) > 1 for s in v]
    league_sd = max(MIN_SD, float(np.std(resid, ddof=1))) if len(resid) > 2 else 25.0
    mean, sd = {}, {}
    for m, v in played.items():
        n = len(v)
        mean[m] = (sum(v) + PRIOR_GAMES_MEAN * league_mean) / (n + PRIOR_GAMES_MEAN)
        var = float(np.var(v, ddof=1)) if n > 1 else league_sd ** 2
        sd[m] = max(MIN_SD, math.sqrt((n * var + PRIOR_GAMES_SD * league_sd ** 2) / (n + PRIOR_GAMES_SD)))
    return mean, sd, league_mean


def expected_scores(remaining: list[tuple[int, str, str]], mean: dict[str, float],
                    proj: dict[tuple[int, str], float] | None) -> dict[tuple[int, str], float]:
    """Expected score per (week, manager) for the remaining games."""
    first = min((w for w, _, _ in remaining), default=None)
    out = {}
    for w, a, b in remaining:
        for m in (a, b):
            p = (proj or {}).get((w, m))
            if p is None or p <= 0:
                out[(w, m)] = mean[m]
            else:
                k = PROJ_WEIGHT_NEXT if w == first else PROJ_WEIGHT_LATER
                out[(w, m)] = k * p + (1 - k) * mean[m]
    return out


def win_probability(mu_a: float, sd_a: float, mu_b: float, sd_b: float) -> float:
    z = (mu_a - mu_b) / math.sqrt(sd_a ** 2 + sd_b ** 2)
    return 0.5 * math.erfc(-z / math.sqrt(2))


def simulate(managers: list[str], record: dict[str, tuple[int, int, int, float]],
             remaining: list[tuple[int, str, str]], mu: dict[tuple[int, str], float],
             sd: dict[str, float], playoff_teams: int, sims: int = SIMS, seed: int = 7,
             with_scenarios: bool = False) -> dict[str, dict]:
    """Play the remaining weeks `sims` times. `record` is (wins, losses, ties, points for)."""
    rng = np.random.default_rng(seed)
    idx = {m: i for i, m in enumerate(managers)}
    t = len(managers)
    wins = np.tile(np.array([record[m][0] for m in managers], float), (sims, 1))
    pf = np.tile(np.array([record[m][3] for m in managers], float), (sims, 1))
    added = np.zeros((sims, t))
    weeks = sorted({w for w, _, _ in remaining})
    next_result = {}  # manager -> bool array: won the next game
    for w in weeks:
        for _, a, b in [g for g in remaining if g[0] == w]:
            ia, ib = idx[a], idx[b]
            sa = rng.normal(mu[(w, a)], sd[a], sims)
            sb = rng.normal(mu[(w, b)], sd[b], sims)
            a_won = sa > sb
            wins[:, ia] += a_won
            wins[:, ib] += ~a_won
            added[:, ia] += a_won
            added[:, ib] += ~a_won
            pf[:, ia] += sa
            pf[:, ib] += sb
            if w == weeks[0]:
                next_result[a], next_result[b] = a_won, ~a_won
    # Rank: wins first, points for second (points for are far below 1e6)
    key = wins * 1e6 + pf
    order = np.argsort(-key, axis=1)
    rank = np.empty_like(order)
    rank[np.arange(sims)[:, None], order] = np.arange(t)
    made = rank < playoff_teams

    out = {}
    left = {m: sum(1 for _, a, b in remaining if m in (a, b)) for m in managers}
    for m in managers:
        i = idx[m]
        r = rank[:, i]
        res = {
            "playoff_pct": float(made[:, i].mean()),
            "seed_pct": [round(float((r == k).mean()), 4) for k in range(t)],
            "last_pct": float((r == t - 1).mean()),
            "proj_wins": float(wins[:, i].mean()),
            "proj_pf": float(pf[:, i].mean()),
            "games_left": left[m],
        }
        if m in next_result:
            won = next_result[m]
            res["odds_if_win"] = float(made[won, i].mean()) if won.any() else None
            res["odds_if_loss"] = float(made[~won, i].mean()) if (~won).any() else None
        else:
            res["odds_if_win"] = res["odds_if_loss"] = None
        by_wins = {}
        for k in range(left[m] + 1):
            sel = added[:, i] == k
            if sel.any():
                by_wins[str(k)] = [round(float(sel.mean()), 4), round(float(made[sel, i].mean()), 4)]
        res["odds_by_wins"] = by_wins
        out[m] = res
    if with_scenarios:
        for m, sc in scenarios(managers, made, added, left).items():
            out[m]["scenarios"] = sc
    return out


SURE = 0.99           # "sicher drin" / "raus" at 99 % / 1 %
COND = 0.95           # a rival condition counts when it lifts the odds to at least this
MIN_SIMS = 40         # ignore slices of the simulation smaller than this


def scenarios(managers: list[str], made: np.ndarray, added: np.ndarray, left: dict[str, int]) -> dict[str, dict]:
    """Paths to the playoffs per manager, read off the simulation.

    For every number of remaining games a manager wins (0..games left): the chance, and when it is
    neither sure nor gone, the least demanding rival results that settle it ("Ritz wins at most 1 of
    his 3 games", alone or two rivals together), plus the rival result that would knock them out.
    `rivals` holds the two rivals whose results matter most as a grid: own wins x rival wins ->
    [chance, share of simulations] (chance null where too few simulations land).
    """
    idx = {m: i for i, m in enumerate(managers)}
    race = {m: float(made[:, idx[m]].mean()) for m in managers}
    out = {}
    for m in managers:
        i, n = idx[m], left[m]
        # Only rivals still in the race are named; a team that is in or out either way is no condition
        others = [r for r in managers if r != m and left[r] > 0 and 0.005 < race[r] < 0.995]
        rows = []
        for a in range(n + 1):
            sel = added[:, i] == a
            cnt = int(sel.sum())
            if cnt < MIN_SIMS:
                rows.append({"wins": a, "share": round(cnt / len(sel), 4), "pct": None, "status": "rare"})
                continue
            p = float(made[sel, i].mean())
            row = {"wins": a, "share": round(cnt / len(sel), 4), "pct": round(p, 4)}
            if p >= SURE:
                row["status"] = "in"
            elif p <= 1 - SURE:
                row["status"] = "out"
            else:
                row["status"] = "open"
                singles = []  # (rival, max wins, chance, share of this row, mask)
                for r in others:
                    for b in range(left[r]):
                        s2 = sel & (added[:, idx[r]] <= b)
                        c2 = int(s2.sum())
                        if c2 >= MIN_SIMS:
                            singles.append((r, b, float(made[s2, i].mean()), c2 / cnt, s2))
                pairs = []
                for x in range(len(singles)):
                    for y in range(x + 1, len(singles)):
                        r1, b1, _, _, m1 = singles[x]
                        r2, b2, _, _, m2 = singles[y]
                        if r1 == r2:
                            continue
                        both = m1 & m2
                        c3 = int(both.sum())
                        if c3 >= MIN_SIMS:
                            pairs.append(([(r1, b1), (r2, b2)], float(made[both, i].mean()), c3 / cnt))
                # Prefer what makes it sure, then what makes it very likely; one rival before two
                pick = None
                for level in (SURE, COND):
                    one = [([(r, b)], q, sh) for r, b, q, sh, _ in singles if q >= level and q - p >= 0.03]
                    two = [c for c in pairs if c[1] >= level and c[1] - p >= 0.03]
                    for cands in (one, two):
                        if cands:
                            pick = max(cands, key=lambda c: (c[2], c[1]))
                            break
                    if pick:
                        break
                if pick:
                    row["needs"] = [{"rival": r, "max_wins": b, "games": left[r]} for r, b in pick[0]]
                    row["needs_pct"], row["needs_share"] = round(pick[1], 4), round(pick[2], 4)
                # What knocks them out: a rival winning at least this many
                danger = []
                for r in others:
                    for b in range(1, left[r] + 1):
                        s2 = sel & (added[:, idx[r]] >= b)
                        c2 = int(s2.sum())
                        if c2 >= MIN_SIMS:
                            q = float(made[s2, i].mean())
                            if q <= 1 - COND:
                                danger.append((c2, r, b, q))
                if danger:
                    c2, r, b, q = max(danger, key=lambda x: (-x[2], x[0]))
                    row["out_if"] = {"rival": r, "min_wins": b, "games": left[r], "pct": round(q, 4)}
            rows.append(row)

        # The rivals whose results move this manager's chance the most
        grids = []
        for r in others:
            j, k = idx[r], left[r]
            grid, impact = [], 0.0
            for a in range(n + 1):
                sel_a = added[:, i] == a
                line, ps = [], []
                for b in range(k + 1):
                    s2 = sel_a & (added[:, j] == b)
                    c2 = int(s2.sum())
                    pct = round(float(made[s2, i].mean()), 4) if c2 >= MIN_SIMS else None
                    line.append([pct, round(c2 / len(sel_a), 4)])
                    if pct is not None:
                        ps.append(pct)
                grid.append(line)
                if len(ps) > 1:
                    impact += float(sel_a.mean()) * (max(ps) - min(ps))
            grids.append((impact, {"rival": r, "games": k, "grid": grid}))
        grids.sort(key=lambda g: -g[0])
        out[m] = {"games": n, "rows": rows, "rivals": [g for imp, g in grids[:2] if imp > 0.01]}
    return out


def odds_rows(season: int, week: int, model: str, managers: list[str],
              record: dict[str, tuple[int, int, int, float]], result: dict[str, dict], sims: int) -> list[dict]:
    rows = []
    for m in managers:
        r = result[m]
        w, l, t, pf = record[m]
        rows.append({
            "season_year": season, "week": week, "manager_id": m, "model": model,
            "wins": w, "losses": l, "ties": t, "points_for": round(pf, 2),
            "playoff_pct": round(r["playoff_pct"], 4),
            "last_pct": round(r["last_pct"], 4),
            "seed_pct": r["seed_pct"],
            "proj_wins": round(r["proj_wins"], 2),
            "proj_pf": round(r["proj_pf"], 1),
            "games_left": r["games_left"],
            "odds_if_win": None if r["odds_if_win"] is None else round(r["odds_if_win"], 4),
            "odds_if_loss": None if r["odds_if_loss"] is None else round(r["odds_if_loss"], 4),
            "odds_by_wins": r["odds_by_wins"],
            "scenarios": r.get("scenarios"),
            "sims": sims,
        })
    return rows
