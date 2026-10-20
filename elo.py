"""
Elo ratings for public.elo_ratings (sync_supabase.py rebuilds the table after
every run). Reconstructed from the original 2015-2025 table and reproduces all
1408 of its rows exactly, every column.

SPEC
----
Processing order
  * Matchups sorted by (season_year, week, id). game_seq is a global counter
    across all seasons: matchup #n (1-based) gives manager_a seq 2n-1 and
    manager_b seq 2n. Every weekly_matchups row is included (regular season,
    playoffs and consolation alike; is_playoff changes nothing in the math).
  * All games of a week use the ratings from before that week (each manager
    plays once per week, so this is the same as sequential processing).

Ratings state
  * Stored at 2 decimals: elo_after = elo_before + elo_change (elo_change rounded 2).
  * First season (or a new manager): 1500.
  * Season rollover: start = round(1500 + 0.7 * (prev_season_end - 1500), 2)
    (30% regression toward 1500, not toward the league mean).

Per manager per game (opp = opponent; all intermediates UNROUNDED, only the
stored columns are rounded: elo_*, mov, upset, perf, k_used to 2dp;
expected_score, actual_score, rank_component to 4dp):
  expected_score = 1 / (1 + 10 ** ((opp_elo_before - elo_before) / 400))
  weekly_rank    = SQL RANK() of score among all scores that week, descending
                   (ties share the best rank)
  rank_component = (8 - weekly_rank) / 7          # 8 = number of teams that week
  actual_score   = 0.35 * rank_component + 0.65 * score / (score + opp_score)
  mov_multiplier = min(|score - opp_score| / 10, 8)
  is_upset       = score > opp_score and (opp_elo_before - elo_before) > 20
  upset_multiplier = (opp_elo_before - elo_before) / 20 if is_upset else 0  (no cap observed; max seen 4.21)
  perf_multiplier  = 0 for the manager's first game of the season, else
                   clamp(15 * (score - avg_prior) / avg_prior, -3, 3)
                   where avg_prior = mean of the manager's earlier scores in
                   the SAME season (playoff weeks included).
  k_used     = 20 + mov_multiplier + upset_multiplier + perf_multiplier
  elo_change = round(k_used * (actual_score - expected_score), 2)
  won        = score > opp_score  (a tie is won = False for both)

Rounding: half away from zero (Postgres numeric). Carryover, mov, upset and
k_used must be computed in exact decimal; with binary floats, values like
1514.035, 1.205 or 33.665 round the wrong way.

Verification: per-season md5 of every column (game_seq, manager, elo_before/
after/change, expected, actual, rank, rank_component, mov, upset, perf, k,
won, is_upset) is identical to the DB for all 11 seasons (1408/1408 rows).
"""

from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

BASE = 1500.0
CARRY = 0.7
K_BASE = 20.0
MOV_DIV, MOV_CAP = 10.0, 8.0
UPSET_GAP, UPSET_DIV = 20.0, 20.0
PERF_SCALE, PERF_CAP = 15.0, 3.0
W_RANK, W_SHARE = 0.35, 0.65


def _r(x, nd):
    """Round half away from zero, like Postgres numeric round()."""
    q = Decimal(1).scaleb(-nd)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def _rd(d, nd):
    return float(d.quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP))


def compute_elo(matchups, prior_ratings=None, start_seq=0, prior_season=None,
                prior_season_scores=None):
    """
    matchups: list of dicts with season_year, week, manager_a, score_a,
      manager_b, score_b, is_playoff and (ideally) id. Sorted internally by
      (season_year, week, id).
    For a full rebuild call with all seasons from 2015. To append only new
    weeks, pass prior_ratings={manager: last elo_after}, prior_season=<season of
    those ratings>, start_seq=<max existing game_seq>, and, if continuing a
    season mid-way, prior_season_scores={manager: [earlier scores this season]}.
    Returns list of row dicts with all elo_ratings columns.
    """
    ms = sorted(matchups, key=lambda m: (int(m["season_year"]), int(m["week"]),
                                         m.get("id") or 0))
    ratings = dict(prior_ratings or {})        # manager -> rating (2dp)
    rating_season = {m: prior_season for m in ratings}
    season_scores = defaultdict(list)
    if prior_season_scores:
        for k, v in prior_season_scores.items():
            season_scores[(prior_season, k)] = [float(s) for s in v]

    weeks = defaultdict(list)
    for m in ms:
        weeks[(int(m["season_year"]), int(m["week"]))].append(m)

    rows, seq = [], start_seq
    for (season, week) in sorted(weeks):
        games = weeks[(season, week)]
        # season rollover / new managers
        for g in games:
            for mgr in (g["manager_a"], g["manager_b"]):
                if mgr not in ratings:
                    ratings[mgr] = BASE
                    rating_season[mgr] = season
                elif rating_season[mgr] != season:
                    # exact decimal arithmetic (Postgres numeric semantics): 1514.035 -> 1514.04
                    d = Decimal("1500") + Decimal("0.7") * (Decimal(repr(ratings[mgr])) - Decimal("1500"))
                    ratings[mgr] = float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    rating_season[mgr] = season
        scores = []
        for g in games:
            scores += [float(g["score_a"]), float(g["score_b"])]
        n_teams = len(scores)

        def rank_of(s):
            return 1 + sum(1 for x in scores if x > s)

        before = dict(ratings)
        for g in games:
            sides = [(g["manager_a"], float(g["score_a"]), g["manager_b"], float(g["score_b"])),
                     (g["manager_b"], float(g["score_b"]), g["manager_a"], float(g["score_a"]))]
            for me, s, opp, os_ in sides:
                seq += 1
                eb, ob = before[me], before[opp]
                gap = ob - eb
                expected = 1.0 / (1.0 + 10 ** (gap / 400.0))
                rk = rank_of(s)
                rc = (n_teams - rk) / (n_teams - 1)
                actual = W_RANK * rc + W_SHARE * s / (s + os_)
                # mov / upset / k in exact decimal so .xx5 halves round like Postgres numeric
                D = lambda x: Decimal(repr(float(x)))
                mov = min(abs(D(s) - D(os_)) / D(MOV_DIV), D(MOV_CAP))
                upset = s > os_ and gap > UPSET_GAP
                up = (D(ob) - D(eb)) / D(UPSET_DIV) if upset else Decimal(0)
                prev = season_scores[(season, me)]
                if prev:
                    avg = sum(prev) / len(prev)
                    perf = max(-PERF_CAP, min(PERF_CAP, PERF_SCALE * (s - avg) / avg))
                else:
                    perf = 0.0
                k_dec = D(K_BASE) + mov + up + D(perf)
                k = float(k_dec)
                change = _r(k * (actual - expected), 2)
                after = float(Decimal(repr(eb)) + Decimal(repr(change)))
                ratings[me] = after
                rows.append(dict(
                    season_year=season, week=week, is_playoff=bool(g["is_playoff"]),
                    manager_id=me, opponent_id=opp,
                    elo_before=eb, elo_after=after, elo_change=change,
                    score=s, opp_score=os_, won=s > os_,
                    expected_score=_r(expected, 4), actual_score=_r(actual, 4),
                    game_seq=seq, weekly_rank=rk, rank_component=_r(rc, 4),
                    mov_multiplier=_rd(mov, 2), upset_multiplier=_rd(up, 2),
                    perf_multiplier=_r(perf, 2), k_used=_rd(k_dec, 2), is_upset=upset,
                ))
        for g in games:
            season_scores[(season, g["manager_a"])].append(float(g["score_a"]))
            season_scores[(season, g["manager_b"])].append(float(g["score_b"]))
    return rows

