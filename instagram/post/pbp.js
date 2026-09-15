// Play-by-Play-Parser für nflverse-PBP-CSV (load_pbp-Export).
// Liefert Scoring-Events mit echter Uhrzeit + Quarter, plus Spielfenster.
// Scoring-Settings unten anpassbar – Default: TimTebowTournament (Half-PPR).

const DAY_DE = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

const REC_PTS = { ppr: 1, half: 0.5, standard: 0 };

// Defense / Special Teams
const DEF = {
  sack: 1,
  interception: 2,
  fumbleRecovery: 2,      // auch Special Teams
  safety: 2,
  td: 6,                  // Def-TD und ST-TD
  // Points allowed: [maxPunkte, Fantasy-Punkte]
  pointsAllowed: [[0, 10], [6, 7], [13, 4], [20, 1], [27, 0], [34, -1], [999, -4]],
};

function paPoints(score) {
  for (const [max, pts] of DEF.pointsAllowed) if (score <= max) return pts;
  return DEF.pointsAllowed[DEF.pointsAllowed.length - 1][1];
}

function splitRow(line) {
  const out = [];
  let cur = "", q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === '"') { if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q; }
    else if (c === "," && !q) { out.push(cur); cur = ""; }
    else cur += c;
  }
  out.push(cur);
  return out;
}

// "Jaxon Smith-Njigba" -> "J.Smith-Njigba" (PBP-Schreibweise)
export function abbrev(full) {
  const clean = String(full).replace(/\s+(Jr\.?|Sr\.?|II|III|IV|V)$/i, "").trim();
  const parts = clean.split(/\s+/);
  if (parts.length < 2) return clean;
  return parts[0][0] + "." + parts.slice(1).join(" ");
}

function parseStart(dateStr, timeStr, tzOffset) {
  // start_time: "9/9/26, 20:23:21" (UTC) – game_date: "2026-09-09"
  const t = String(timeStr).match(/(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$/);
  const d = String(dateStr).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!d) return null;
  const base = Date.UTC(+d[1], +d[2] - 1, +d[3], t ? +t[1] : 18, t ? +t[2] : 0);
  return base + tzOffset * 3600000;
}

export function parsePbp(csv, opts) {
  const o = opts || {};
  const tzOffset = o.tzOffset == null ? 2 : o.tzOffset;
  const week = o.week;
  const rec = REC_PTS[o.scoring] == null ? REC_PTS.half : REC_PTS[o.scoring];

  const rows = csv.split("\n");
  const hdr = splitRow(rows[0]);
  const I = {};
  hdr.forEach((h, i) => { I[h.trim()] = i; });
  const g = (r, k) => (I[k] == null ? "" : (r[I[k]] || "").trim());
  const num = (r, k) => { const v = parseFloat(g(r, k)); return isNaN(v) ? 0 : v; };

  const games = {};
  const events = [];

  for (let i = 1; i < rows.length; i++) {
    if (!rows[i].trim()) continue;
    const r = splitRow(rows[i]);
    if (week != null && String(num(r, "week")) !== String(week)) continue;

    const gid = g(r, "game_id");
    if (!gid) continue;

    const qtr = num(r, "qtr") || 1;
    const gsr = num(r, "game_seconds_remaining");
    const elapsed = Math.max(3600 - gsr, 0);              // Spielzeit in Sekunden

    if (!games[gid]) {
      const start = parseStart(g(r, "game_date"), g(r, "start_time"), tzOffset);
      games[gid] = { id: gid, start: start, home: g(r, "home_team"), away: g(r, "away_team"), pa: {} };
      if (start != null) {
        // Points allowed startet für beide Defenses bei "0 zugelassen"
        [games[gid].home, games[gid].away].forEach(team => {
          games[gid].pa[team] = 0;
          events.push({ t: start, qtr: 1, clock: "15:00", name: "DEF:" + team, pts: paPoints(0), note: "Points allowed 0", game: gid });
        });
      }
    }
    const game = games[gid];
    if (game.start == null) continue;

    const wall = game.start + elapsed * 3.15 * 1000;      // ~3h10 Realzeit für 60 Min
    game.end = Math.max(game.end || 0, wall);

    const add = (name, pts, note) => {
      if (!name || !pts) return;
      events.push({ t: wall, qtr: qtr, clock: g(r, "time"), name: name, pts: Math.round(pts * 100) / 100, note: note, game: gid });
    };

    // ── Offense ───────────────────────────────────────────────
    const passer = g(r, "passer_player_name");
    const rusher = g(r, "rusher_player_name");
    const receiver = g(r, "receiver_player_name");
    const complete = num(r, "complete_pass") === 1;

    if (passer) {
      add(passer, num(r, "passing_yards") * 0.04, "Pass-Yards");
      if (num(r, "pass_touchdown") === 1) add(passer, 4, "Pass-TD");
      if (num(r, "interception") === 1) add(passer, -2, "INT");
    }
    if (rusher) {
      add(rusher, num(r, "rushing_yards") * 0.1, "Rush-Yards");
      if (num(r, "rush_touchdown") === 1) add(rusher, 6, "Rush-TD");
    }
    if (receiver && complete) {
      add(receiver, num(r, "receiving_yards") * 0.1 + rec, "Reception");
      if (num(r, "pass_touchdown") === 1) add(receiver, 6, "Rec-TD");
    }
    if (g(r, "two_point_conv_result") === "success") add(rusher || receiver || passer, 2, "2PT");
    const fum = g(r, "fumbled_1_player_name");
    if (fum && num(r, "fumble_lost") === 1) add(fum, -2, "Fumble lost");

    const kicker = g(r, "kicker_player_name");
    if (kicker) {
      if (g(r, "field_goal_result") === "made") {
        const d = num(r, "kick_distance");
        add(kicker, d >= 50 ? 5 : (d >= 40 ? 4 : 3), "FG " + d);
      }
      if (g(r, "extra_point_result") === "good") add(kicker, 1, "XP");
    }

    // ── Defense / Special Teams ───────────────────────────────
    const posteam = g(r, "posteam"), defteam = g(r, "defteam");
    if (defteam) {
      if (num(r, "sack") === 1) add("DEF:" + defteam, DEF.sack, "Sack");
      if (num(r, "interception") === 1) add("DEF:" + defteam, DEF.interception, "INT");
      if (num(r, "safety") === 1) add("DEF:" + defteam, DEF.safety, "Safety");
    }
    const recovTeam = g(r, "fumble_recovery_1_team");
    if (recovTeam && posteam && num(r, "fumble_lost") === 1 && recovTeam !== posteam) {
      add("DEF:" + recovTeam, DEF.fumbleRecovery, "Fumble Recovery");
    }
    const tdTeam = g(r, "td_team");
    if (tdTeam && posteam && tdTeam !== posteam && num(r, "touchdown") === 1) {
      add("DEF:" + tdTeam, DEF.td, "Def/ST-TD");
    }

    // Points allowed: bei jeder Punkteänderung des Gegners nachjustieren
    const scores = { [game.home]: num(r, "total_home_score"), [game.away]: num(r, "total_away_score") };
    [game.home, game.away].forEach(team => {
      const opp = team === game.home ? game.away : game.home;
      const allowed = scores[opp] || 0;
      if (allowed !== game.pa[team]) {
        const delta = paPoints(allowed) - paPoints(game.pa[team]);
        game.pa[team] = allowed;
        if (delta) add("DEF:" + team, delta, "Points allowed " + allowed);
      }
    });
  }

  events.sort((a, b) => a.t - b.t);

  // Spielfenster: Spiele mit ähnlichem Kickoff (< 100 Min Abstand) zusammenfassen
  const list = Object.keys(games).map(k => games[k]).filter(x => x.start != null).sort((a, b) => a.start - b.start);
  const windows = [];
  list.forEach(game => {
    const last = windows[windows.length - 1];
    if (last && game.start - last.t0 < 100 * 60000) {
      last.t1 = Math.max(last.t1, game.end || game.start + 3.2 * 3600000);
      last.games.push(game.id);
    } else {
      windows.push({ t0: game.start, t1: game.end || game.start + 3.2 * 3600000, games: [game.id] });
    }
  });
  windows.forEach(w => {
    const d = new Date(w.t0);
    w.label = DAY_DE[d.getUTCDay()];
    w.sub = String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
    w.count = w.games.length;
  });

  return {
    events: events, windows: windows, games: list.length,
    gameList: list.map(x => ({ id: x.id, start: x.start, end: x.end || x.start + 3.2 * 3600000, home: x.home, away: x.away })),
  };
}

// Events -> kumulierte Serie für ein Lineup ([{name, nfl, pos}, …])
export function seriesFor(events, roster) {
  const want = {};
  roster.forEach(p => {
    const isDef = /^(DEF|DST|D\/ST)$/i.test(p.pos || "");
    want[(isDef ? "def:" + (p.nfl || p.name) : abbrev(p.name)).toLowerCase()] = p.name;
  });
  const byPlayer = {};
  let total = 0;
  const pts = [];
  events.forEach(e => {
    const label = want[String(e.name).toLowerCase()];
    if (!label) return;
    total = Math.round((total + e.pts) * 10) / 10;
    byPlayer[label] = Math.round(((byPlayer[label] || 0) + e.pts) * 10) / 10;
    pts.push({ t: e.t, v: total, name: label, note: e.note, qtr: e.qtr, clock: e.clock });
  });
  return { points: pts, byPlayer: byPlayer, total: total };
}
