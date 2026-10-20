#!/usr/bin/env python3
"""Sync the current Sleeper season into Supabase (Tim Tebow Tournament).

Writes, for every completed week of the season:
  weekly_matchups, weekly_lineups, season_results, seasons
and once the playoffs are done also playoff_results. Weekly NFL player stats
come from nflverse (player_weekly_stats). Finally the database rebuilds
records and achievements (public.refresh_league_derived()) and Elo ratings
are recalculated from all matchups since 2015 (elo.py).

Environment:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (required)
  SLEEPER_LEAGUE_ID   optional, any league id of the dynasty (default: the
                      2026 league); the requested season's league is found
                      through the previous_league_id chain
  SEASON              optional, default: current NFL season (Sleeper state)
  WEEKS               optional, e.g. "1-6" (default: all completed weeks)
  ONLY                optional, "transactions" rebuilds only the transactions
                      of SEASON, "elo" only the Elo ratings (matchups, lineups
                      and draft stay untouched)
  DRY_RUN=1           print what would be written, write nothing
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from collections import defaultdict

import requests

from draft_grades import draft_rows
from elo import compute_elo
from transactions import score_rows, transaction_rows

SLEEPER = "https://api.sleeper.app/v1"
NFLVERSE_STATS = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
# SUPABASE_KEY is the name the other Scrape workflows use
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

BENCH_SLOTS = 7
TEAMS = 8
PLAYOFF_TEAMS = 4

# Sleeper team / display names -> manager id used on the website
_ALIASES = {
    "dreggsverein": "Benni", "shamh": "Benni",
    "talahonunited": "Simi", "simon2307": "Simi",
    "snatchvillelubricators": "Kessi", "kesso": "Kessi",
    "ihatekowa": "Tommy", "thebiglebronski": "Tommy",
    "extremdurstigewelse": "Ritz", "lossausages": "Ritz",
    "suckmemourdocknetwork": "Marv", "lancemourdock": "Marv",
    "ogunited": "Erik", "jottage": "Erik",
    "juschka": "Juschka",
}
MANAGERS = {"Benni", "Erik", "Juschka", "Kessi", "Marv", "Ritz", "Simi", "Tommy"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def log(*a):
    print(*a, flush=True)


# ── HTTP ─────────────────────────────────────────────────────────────────────

def sleeper(path: str):
    r = requests.get(f"{SLEEPER}/{path}", timeout=60)
    r.raise_for_status()
    return r.json()


def sb_headers(extra: dict | None = None) -> dict:
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def sb_upsert(table: str, rows: list[dict], on_conflict: str):
    if not rows:
        return
    if DRY_RUN:
        log(f"  [dry-run] upsert {len(rows)} rows into {table}")
        return
    for i in range(0, len(rows), 500):
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            data=json.dumps(rows[i:i + 500]), timeout=120,
        )
        if r.status_code >= 300:
            raise SystemExit(f"Upsert into {table} failed: {r.status_code} {r.text[:500]}")


def sb_delete(table: str, filters: dict):
    if DRY_RUN:
        log(f"  [dry-run] delete from {table} where {filters}")
        return
    q = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}?{q}", headers=sb_headers(), timeout=120)
    if r.status_code >= 300:
        raise SystemExit(f"Delete from {table} failed: {r.status_code} {r.text[:500]}")


def sb_insert(table: str, rows: list[dict]):
    if not rows:
        return
    if DRY_RUN:
        log(f"  [dry-run] insert {len(rows)} rows into {table}")
        return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=sb_headers({"Prefer": "return=minimal"}),
                      data=json.dumps(rows), timeout=120)
    if r.status_code >= 300:
        raise SystemExit(f"Insert into {table} failed: {r.status_code} {r.text[:500]}")


def sb_rpc(fn: str, args: dict | None = None):
    if DRY_RUN:
        log(f"  [dry-run] rpc {fn}")
        return None
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{fn}", headers=sb_headers(),
                      data=json.dumps(args or {}), timeout=600)
    if r.status_code >= 300:
        raise SystemExit(f"RPC {fn} failed: {r.status_code} {r.text[:500]}")
    return r.json() if r.text else None


def sb_get(path: str) -> list[dict]:
    """GET all rows of a PostgREST query, paging through 1000-row responses."""
    rows, offset = [], 0
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}",
                         headers=sb_headers({"Range": f"{offset}-{offset + 999}"}), timeout=120)
        if r.status_code >= 300:
            raise SystemExit(f"GET {path.split('?')[0]} failed: {r.status_code} {r.text[:500]}")
        batch = r.json()
        rows += batch
        if len(batch) < 1000:
            return rows
        offset += 1000


NFLVERSE_PLAYERS = "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
PLAYER_COLS = ["gsis_id", "espn_id", "display_name", "first_name", "last_name", "position",
               "position_group", "headshot", "latest_team", "status", "height", "weight",
               "college_name", "jersey_number", "rookie_season", "last_season", "draft_year",
               "draft_round", "draft_pick", "draft_team"]
PLAYER_INT_COLS = {"height", "weight", "jersey_number", "rookie_season", "last_season",
                   "draft_year", "draft_round", "draft_pick"}
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)


def name_key(name: str) -> str:
    return _norm(_SUFFIX.sub("", name or ""))


def load_nflverse_players() -> list[dict]:
    r = requests.get(NFLVERSE_PLAYERS, timeout=300)
    r.raise_for_status()
    return [rec for rec in csv.DictReader(io.StringIO(r.content.decode("utf-8"))) if rec.get("gsis_id")]


def sync_players(nfl_players: list[dict]):
    """Refresh public.players (ids, headshots) so new rookies resolve too."""
    rows = []
    for rec in nfl_players:
        row = {}
        for c in PLAYER_COLS:
            v = rec.get(c)
            v = None if v in (None, "", "NA") else v
            if v is not None and c in PLAYER_INT_COLS:
                try:
                    v = int(float(v))
                except ValueError:
                    v = None
            row[c] = v
        rows.append(row)
    sb_upsert("players", rows, "gsis_id")
    log(f"  players: {len(rows)} rows")


class EspnIds:
    """Sleeper player -> ESPN id. Sleeper often has no espn_id for newer
    players, so fall back to nflverse (via gsis id, then via a unique name +
    position match, ignoring suffixes like Jr./III)."""

    def __init__(self, players: dict, nfl_players: list[dict]):
        self.players = players
        self.by_gsis: dict[str, str] = {}
        self.by_name: dict[tuple[str, str], str | None] = {}
        self.gsis_by_name: dict[tuple[str, str], str | None] = {}
        for r in nfl_players:
            espn = (r.get("espn_id") or "").strip()
            espn = "" if espn == "NA" else espn
            if espn:
                self.by_gsis[r["gsis_id"]] = espn
            pos = (r.get("position") or "").upper()
            names = {r.get("display_name"), f"{r.get('first_name')} {r.get('last_name')}",
                     f"{r.get('common_first_name')} {r.get('last_name')}",
                     f"{r.get('football_name')} {r.get('last_name')}"}
            for n in names:
                key = (name_key(n or ""), pos)
                if not key[0]:
                    continue
                # ambiguous names map to None so they are never guessed
                if espn:
                    self.by_name[key] = espn if self.by_name.get(key, espn) == espn else None
                gs = r["gsis_id"]
                self.gsis_by_name[key] = gs if self.gsis_by_name.get(key, gs) == gs else None

    def gsis(self, pid: str) -> str | None:
        p = self.players.get(pid) or {}
        if p.get("position") == "DEF":
            return None
        gsis = (p.get("gsis_id") or "").strip()
        if gsis:
            return gsis
        pos = (p.get("position") or "").upper()
        return self.gsis_by_name.get((name_key(player_name(self.players, pid)), pos))

    def get(self, pid: str) -> str:
        p = self.players.get(pid) or {}
        if p.get("position") == "DEF":
            return ""
        if p.get("espn_id"):
            return str(p["espn_id"])
        gsis = (p.get("gsis_id") or "").strip()
        if gsis in self.by_gsis:
            return self.by_gsis[gsis]
        pos = (p.get("position") or "").upper()
        return self.by_name.get((name_key(player_name(self.players, pid)), pos)) or ""


# ── League ───────────────────────────────────────────────────────────────────

def find_season_league(any_league_id: str, season: int) -> dict:
    """Return the league object of `season`, walking forward or backward."""
    league = sleeper(f"league/{any_league_id}")
    # Walk back through previous seasons if the given id is newer than wanted
    while int(league["season"]) > season and league.get("previous_league_id"):
        league = sleeper(f"league/{league['previous_league_id']}")
    if int(league["season"]) == season:
        return league
    # Walk forward: a newer league names the current one as previous_league_id
    users = sleeper(f"league/{league['league_id']}/users")
    current = league
    for target in range(int(league["season"]) + 1, season + 1):
        found = None
        for u in users:
            for lg in sleeper(f"user/{u['user_id']}/leagues/nfl/{target}") or []:
                if lg.get("previous_league_id") == current["league_id"]:
                    found = lg
                    break
            if found:
                break
        if not found:
            raise SystemExit(f"No {target} league found that follows league {current['league_id']}")
        current = sleeper(f"league/{found['league_id']}")
    return current


def potential_points(league_id: str, managers: dict[int, str]) -> dict[str, float]:
    """Sleeper's own max points ("ppts") per roster, if it reports them."""
    out = {}
    for r in sleeper(f"league/{league_id}/rosters"):
        st = r.get("settings") or {}
        if st.get("ppts") is not None:
            out[managers[r["roster_id"]]] = float(st["ppts"]) + float(st.get("ppts_decimal") or 0) / 100
    return out


def manager_by_roster(league_id: str) -> dict[int, str]:
    users = {u["user_id"]: u for u in sleeper(f"league/{league_id}/users")}
    out = {}
    for r in sleeper(f"league/{league_id}/rosters"):
        u = users.get(r.get("owner_id")) or {}
        names = [(u.get("metadata") or {}).get("team_name"), u.get("display_name"), u.get("username")]
        manager = next((_ALIASES[_norm(n)] for n in names if n and _norm(n) in _ALIASES), None)
        if not manager:
            raise SystemExit(f"Unknown Sleeper owner {names} – please add it to _ALIASES")
        out[r["roster_id"]] = manager
    if set(out.values()) != MANAGERS:
        raise SystemExit(f"Roster mapping incomplete: {sorted(out.values())}")
    return out


# ── Lineups ──────────────────────────────────────────────────────────────────

FLEX_POS = {"RB", "WR", "TE"}


def player_name(players: dict, pid: str) -> str:
    p = players.get(pid) or {}
    if p.get("position") == "DEF":
        return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid
    return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid


def lineup_rows(season, week, manager, entry, roster_positions, players, espn: EspnIds):
    """Starters by their Sleeper slot order, bench sorted by points (BN1..BN7)."""
    pts = entry.get("players_points") or {}
    starters = entry.get("starters") or []
    slots = [s for s in roster_positions if s not in ("BN", "IR", "TAXI")]
    counters = defaultdict(int)
    rows = []

    def row(slot, pid, starter):
        p = players.get(pid) or {}
        is_def = p.get("position") == "DEF"
        return {
            "season_year": season, "week": week, "manager_id": manager, "slot": slot,
            "player_name": player_name(players, pid),
            "sleeper_id": "" if is_def else pid,
            "espn_id": espn.get(pid),
            "points": round(float(pts.get(pid, 0) or 0), 2), "is_starter": starter,
        }

    for slot, pid in zip(slots, starters):
        if not pid or pid == "0":
            continue
        name = {"DEF": "DST", "REC_FLEX": "FLEX", "WRRB_FLEX": "FLEX", "SUPER_FLEX": "FLEX"}.get(slot, slot)
        if name in ("RB", "WR"):
            counters[name] += 1
            name = f"{name}{counters[name]}"
        rows.append(row(name, pid, True))

    bench = [pid for pid in (entry.get("players") or []) if pid not in set(starters)]
    bench.sort(key=lambda pid: -float(pts.get(pid, 0) or 0))
    for i, pid in enumerate(bench[:BENCH_SLOTS], start=1):
        rows.append(row(f"BN{i}", pid, False))
    return rows


def optimal_points(entry, roster_positions, players) -> float:
    """Best possible starting lineup from the whole roster (greedy is optimal here)."""
    pts = {pid: float(v or 0) for pid, v in (entry.get("players_points") or {}).items()}
    by_pos = defaultdict(list)
    for pid in entry.get("players") or []:
        by_pos[(players.get(pid) or {}).get("position")].append(pts.get(pid, 0.0))
    for v in by_pos.values():
        v.sort(reverse=True)
    slots = [s for s in roster_positions if s not in ("BN", "IR", "TAXI")]
    total, used = 0.0, defaultdict(int)
    for s in slots:
        if s in ("QB", "RB", "WR", "TE", "K", "DEF"):
            lst = by_pos.get(s, [])
            if used[s] < len(lst):
                total += lst[used[s]]
                used[s] += 1
    for s in slots:
        if s in ("FLEX", "REC_FLEX", "WRRB_FLEX"):
            cands = [(by_pos[p][used[p]], p) for p in FLEX_POS if used[p] < len(by_pos.get(p, []))]
            if cands:
                v, p = max(cands)
                total += v
                used[p] += 1
    return round(total, 2)


# ── NFL stats (nflverse) ─────────────────────────────────────────────────────

STAT_COLS = [
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions", "sacks_suffered",
    "passing_2pt_conversions", "carries", "rushing_yards", "rushing_tds", "rushing_fumbles",
    "rushing_2pt_conversions", "receptions", "targets", "receiving_yards", "receiving_tds",
    "receiving_fumbles", "receiving_2pt_conversions", "fg_made", "fg_att", "fg_made_0_19",
    "fg_made_20_29", "fg_made_30_39", "fg_made_40_49", "fg_made_50_59", "fg_made_60_", "pat_made", "pat_att",
]


def sync_player_stats(season: int, weeks: list[int]):
    r = requests.get(NFLVERSE_STATS.format(season=season), timeout=300)
    if r.status_code == 404:
        log(f"  nflverse has no weekly stats for {season} yet – skipped")
        return
    r.raise_for_status()
    rows = []
    for rec in csv.DictReader(io.StringIO(r.content.decode("utf-8"))):
        if rec.get("season_type") != "REG" or int(rec["week"]) not in weeks:
            continue
        row = {
            "player_id": rec["player_id"], "player_display_name": rec.get("player_display_name"),
            "position": rec.get("position"), "season": season, "week": int(rec["week"]),
            "season_type": "REG", "team": rec.get("team") or rec.get("recent_team"),
            "opponent_team": rec.get("opponent_team"),
        }
        for c in STAT_COLS:
            v = rec.get(c)
            row[c] = int(float(v)) if v not in (None, "", "NA") else 0
        rows.append(row)
    log(f"  player_weekly_stats: {len(rows)} rows")
    sb_upsert("player_weekly_stats", rows, "player_id,season,week,season_type")


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_weeks(spec: str) -> list[int] | None:
    if not spec:
        return None
    out = set()
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return sorted(out)


DEFAULT_LEAGUE_ID = "1321866747418001408"  # 2026 season


def main():
    if not (SUPABASE_URL and SUPABASE_KEY) and not DRY_RUN:
        missing = [n for n, v in (("SUPABASE_URL", SUPABASE_URL),
                                  ("SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY", SUPABASE_KEY)) if not v]
        raise SystemExit("Missing repository secret(s): " + ", ".join(missing))
    state = sleeper("state/nfl")
    season = int(os.environ.get("SEASON") or state["season"])
    league = find_season_league((os.environ.get("SLEEPER_LEAGUE_ID") or DEFAULT_LEAGUE_ID).strip(), season)
    league_id = league["league_id"]
    settings = league.get("settings") or {}
    playoff_start = int(settings.get("playoff_week_start") or 15)
    reg_weeks = playoff_start - 1
    last_week = reg_weeks + 2
    roster_positions = league.get("roster_positions") or []
    log(f"Season {season}: league {league_id} ({league.get('name')}), status {league.get('status')}")

    only = os.environ.get("ONLY", "").strip()
    if only == "elo":
        sync_elo()
        log("Done.")
        return
    if only == "transactions":
        managers = manager_by_roster(league_id)
        players = sleeper("players/nfl")
        nfl_players = load_nflverse_players()
        log("Transactions …")
        sync_transactions(season, league_id, managers, players, EspnIds(players, nfl_players), nfl_players)
        log(f"  {sb_rpc('refresh_league_derived')}")
        sb_rpc("refresh_player_stats")
        log("Done.")
        return

    # Completed weeks: everything before the current NFL week of this season
    if int(state["season"]) > season or league.get("status") == "complete":
        done = last_week
    elif state.get("season_type") == "regular" or state.get("season_type") == "post":
        done = min(int(state["week"]) - 1, last_week)
    else:
        done = 0
    weeks = parse_weeks(os.environ.get("WEEKS", "")) or list(range(1, done + 1))
    weeks = [w for w in weeks if 1 <= w <= last_week]
    if not weeks:
        log("No completed weeks yet – nothing to do")
        return
    log(f"Weeks: {weeks}")

    managers = manager_by_roster(league_id)
    players = sleeper("players/nfl")
    nfl_players = load_nflverse_players()
    espn = EspnIds(players, nfl_players)

    sb_upsert("seasons", [{"year": season, "reg_season_weeks": reg_weeks,
                           "playoff_teams": PLAYOFF_TEAMS, "total_managers": TEAMS}], "year")

    scores: dict[int, dict[str, float]] = {}
    optimal: dict[int, dict[str, float]] = {}
    pairs: dict[int, list[tuple[str, str]]] = {}
    for week in weeks:
        entries = sleeper(f"league/{league_id}/matchups/{week}") or []
        if not entries or not any(float(e.get("points") or 0) for e in entries):
            log(f"  week {week}: no points yet – skipped")
            continue
        scores[week] = {managers[e["roster_id"]]: round(float(e.get("points") or 0), 2) for e in entries}
        optimal[week] = {managers[e["roster_id"]]: optimal_points(e, roster_positions, players) for e in entries}

        by_mid = defaultdict(list)
        for e in entries:
            if e.get("matchup_id") is not None:
                by_mid[e["matchup_id"]].append(managers[e["roster_id"]])
        week_pairs = [tuple(sorted(v)) for v in by_mid.values() if len(v) == 2]
        if week > reg_weeks and len(week_pairs) < TEAMS // 2:
            week_pairs = bracket_pairs(league_id, week - reg_weeks, managers) or week_pairs
        pairs[week] = week_pairs

        matchup_rows = [{
            "season_year": season, "week": week, "manager_a": a, "score_a": scores[week][a],
            "manager_b": b, "score_b": scores[week][b], "is_playoff": week > reg_weeks,
        } for a, b in week_pairs]
        sb_upsert("weekly_matchups", matchup_rows, "season_year,week,manager_a,manager_b")

        lineups = []
        for e in entries:
            lineups += lineup_rows(season, week, managers[e["roster_id"]], e, roster_positions, players, espn)
        sb_delete("weekly_lineups", {"season_year": season, "week": week})
        sb_insert("weekly_lineups", lineups)
        log(f"  week {week}: {len(matchup_rows)} matchups, {len(lineups)} lineup rows")

    # Standings after the completed regular season weeks
    reg_done = [w for w in sorted(scores) if w <= reg_weeks]
    if reg_done:
        st = {m: {"W": 0, "L": 0, "T": 0, "PF": 0.0, "PA": 0.0, "MAX": 0.0} for m in MANAGERS}
        for w in reg_done:
            for a, b in pairs[w]:
                sa, sbs = scores[w][a], scores[w][b]
                st[a]["PF"] += sa; st[a]["PA"] += sbs
                st[b]["PF"] += sbs; st[b]["PA"] += sa
                if sa > sbs: st[a]["W"] += 1; st[b]["L"] += 1
                elif sbs > sa: st[b]["W"] += 1; st[a]["L"] += 1
                else: st[a]["T"] += 1; st[b]["T"] += 1
            for m in MANAGERS:
                st[m]["MAX"] += optimal[w].get(m, 0.0)
        order = sorted(MANAGERS, key=lambda m: (-st[m]["W"], -round(st[m]["PF"], 2), round(st[m]["PA"], 2), m))
        moves, trades = count_transactions(league_id, reg_weeks, managers)
        draft_pos = draft_positions(league_id, managers)
        final = final_ranks(order, scores, reg_weeks) if reg_weeks + 2 in scores else {}
        sleeper_max = potential_points(league_id, managers)
        rows = []
        for rank, m in enumerate(order, start=1):
            s = st[m]
            rows.append({
                "season_year": season, "manager_id": m, "reg_rank": rank,
                "wins": s["W"], "losses": s["L"], "ties": s["T"],
                "points_for": round(s["PF"], 2), "points_against": round(s["PA"], 2),
                "moves": moves.get(m, 0), "trades": trades.get(m, 0), "draft_position": draft_pos.get(m),
                "playoff_rank": final.get(m), "championship_finish": final.get(m),
                "max_points_for": round(sleeper_max.get(m, s["MAX"]), 2),
                "lineup_efficiency": round(s["PF"] / sleeper_max.get(m, s["MAX"]), 4) if sleeper_max.get(m, s["MAX"]) else None,
            })
        sb_upsert("season_results", rows, "season_year,manager_id")
        log(f"  season_results: {len(rows)} rows")

        if final:
            po = [{
                "season_year": season, "manager_id": m, "seed": order.index(m) + 1,
                "week15_pts": scores[reg_weeks + 1].get(m), "week16_pts": scores[reg_weeks + 2].get(m),
                "final_rank": final[m],
            } for m in MANAGERS]
            sb_upsert("playoff_results", po, "season_year,manager_id")
            log("  playoff_results written")

    log("NFL players and stats …")
    sync_players(nfl_players)
    sync_player_stats(season, sorted(scores))

    log("Transactions …")
    sync_transactions(season, league_id, managers, players, espn, nfl_players)

    log("Draft …")
    sync_draft(season, league_id, managers, players, espn, nfl_players)

    log("Elo …")
    sync_elo()

    log("Rebuilding records and achievements …")
    log(f"  {sb_rpc('refresh_league_derived')}")
    sb_rpc("refresh_player_stats")  # player pages (materialized views)
    log("Done.")


def bracket_pairs(league_id: str, rnd: int, managers: dict[int, str]) -> list[tuple[str, str]]:
    pairs = []
    for kind in ("winners_bracket", "losers_bracket"):
        for m in sleeper(f"league/{league_id}/{kind}") or []:
            if m.get("r") == rnd and m.get("t1") and m.get("t2"):
                pairs.append(tuple(sorted((managers[m["t1"]], managers[m["t2"]]))))
    return pairs


def final_ranks(order: list[str], scores: dict, reg_weeks: int) -> dict[str, int]:
    """League format: 1v4, 2v3 (title), 5v8, 6v7 (placement); week 2 decides 1-8."""
    w15, w16 = scores[reg_weeks + 1], scores[reg_weeks + 2]

    def win(a, b, pts):
        if pts.get(a, 0) != pts.get(b, 0):
            return (a, b) if pts.get(a, 0) > pts.get(b, 0) else (b, a)
        return (a, b) if order.index(a) < order.index(b) else (b, a)

    s = order
    r1 = [win(s[0], s[3], w15), win(s[1], s[2], w15), win(s[4], s[7], w15), win(s[5], s[6], w15)]
    champ, runner = win(r1[0][0], r1[1][0], w16)
    third, fourth = win(r1[0][1], r1[1][1], w16)
    fifth, sixth = win(r1[2][0], r1[3][0], w16)
    seventh, eighth = win(r1[2][1], r1[3][1], w16)
    return {m: i for i, m in enumerate([champ, runner, third, fourth, fifth, sixth, seventh, eighth], start=1)}


def count_transactions(league_id: str, reg_weeks: int, managers: dict[int, str]):
    moves, trades = defaultdict(int), defaultdict(int)
    for week in range(1, reg_weeks + 1):
        for tx in sleeper(f"league/{league_id}/transactions/{week}") or []:
            if tx.get("status") != "complete":
                continue
            rids = tx.get("roster_ids") or []
            if tx.get("type") in ("waiver", "free_agent"):
                for rid in rids:
                    moves[managers[rid]] += 1
            elif tx.get("type") == "trade":
                for rid in rids:
                    trades[managers[rid]] += 1
    return moves, trades


def sync_transactions(season, league_id, managers, players, espn, nfl_players):
    """All transactions of the season, replaced on every run (values are rest-of-season)."""
    txs = []
    for leg in range(1, 19):
        txs += sleeper(f"league/{league_id}/transactions/{leg}") or []
    items, pts = transaction_rows(season, txs, managers, players, espn, nfl_players)
    scores = score_rows(season, items, pts)
    sb_delete("fa_transaction_scores", {"season_year": season})
    sb_delete("fa_transactions", {"season_year": season})
    for i in range(0, len(items), 500):
        sb_insert("fa_transactions", items[i:i + 500])
    for i in range(0, len(scores), 500):
        sb_insert("fa_transaction_scores", scores[i:i + 500])
    sb_rpc("refresh_transaction_scores")
    log(f"  fa_transactions: {len(items)} rows, fa_transaction_scores: {len(scores)} rows")


def sync_elo():
    """Recalculate elo_ratings from every matchup since 2015 (deterministic, so
    rows of past seasons come out unchanged)."""
    matchups = sb_get("weekly_matchups?select=id,season_year,week,manager_a,score_a,manager_b,score_b,is_playoff"
                      "&order=season_year,week,id")
    rows = compute_elo(matchups)
    sb_upsert("elo_ratings", rows, "season_year,week,manager_id")
    log(f"  elo_ratings: {len(rows)} rows up to {rows[-1]['season_year']} week {rows[-1]['week']}" if rows else "  no matchups")


def sync_draft(season, league_id, managers, players, espn, nfl_players):
    """draft_picks with season-to-date values (see draft_grades.py)."""
    drafts = [d for d in sleeper(f"league/{league_id}/drafts") or [] if d.get("status") == "complete"]
    if not drafts:
        log("  no completed draft yet")
        return
    picks = sleeper(f"draft/{drafts[0]['draft_id']}/picks") or []
    rows = draft_rows(season, picks, managers, players, espn, nfl_players, log=log)
    sb_upsert("draft_picks", rows, "season_year,overall_pick")
    log(f"  draft_picks: {len(rows)} rows")


def draft_positions(league_id: str, managers: dict[int, str]) -> dict[str, int]:
    drafts = sleeper(f"league/{league_id}/drafts") or []
    if not drafts:
        return {}
    slot_to_roster = drafts[0].get("slot_to_roster_id") or {}
    return {managers[int(rid)]: int(slot) for slot, rid in slot_to_roster.items() if rid}


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        sys.exit(f"HTTP error: {e}")
