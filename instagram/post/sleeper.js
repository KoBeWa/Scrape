// Sleeper-Anbindung: League, Manager, Standings, Matchups, Lineups, Projektionen.
// Öffentliche Read-only-API, kein Login nötig.

const API = "https://api.sleeper.app/v1";
const NICK = { ARI: "Cardinals", ATL: "Falcons", BAL: "Ravens", BUF: "Bills", CAR: "Panthers", CHI: "Bears", CIN: "Bengals", CLE: "Browns", DAL: "Cowboys", DEN: "Broncos", DET: "Lions", GB: "Packers", HOU: "Texans", IND: "Colts", JAX: "Jaguars", KC: "Chiefs", LV: "Raiders", LAC: "Chargers", LAR: "Rams", MIA: "Dolphins", MIN: "Vikings", NE: "Patriots", NO: "Saints", NYG: "Giants", NYJ: "Jets", PHI: "Eagles", PIT: "Steelers", SF: "49ers", SEA: "Seahawks", TB: "Buccaneers", TEN: "Titans", WAS: "Commanders" };

async function j(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url.replace(/^https?:\/\/[^/]+/, "") + " → " + r.status);
  return r.json();
}

export async function currentWeek() {
  const s = await j(API + "/state/nfl");
  return { week: s.week, displayWeek: s.display_week, season: s.season };
}

// opts: { scoring: "half"|"ppr"|"standard", names: { sleeperDisplayName: "Anzeigename" } }
export async function loadLeague(leagueId, week, opts) {
  const o = opts || {};
  const key = o.scoring === "ppr" ? "pts_ppr" : (o.scoring === "standard" ? "pts_std" : "pts_half_ppr");
  const [league, users, rosters] = await Promise.all([
    j(API + "/league/" + leagueId), j(API + "/league/" + leagueId + "/users"), j(API + "/league/" + leagueId + "/rosters"),
  ]);
  const season = league.season;
  const weeks = []; for (let w = 1; w <= week; w++) weeks.push(w);
  const perWeek = await Promise.all(weeks.map(w => j(API + "/league/" + leagueId + "/matchups/" + w).catch(() => [])));

  // Spieler-Lexikon + Projektionen (eine Anfrage); Fallback: komplette Spielerliste
  const dict = {}, proj = {};
  try {
    const pos = ["QB", "RB", "WR", "TE", "K", "DEF"].map(p => "position[]=" + p).join("&");
    const arr = await j("https://api.sleeper.com/projections/nfl/" + season + "/" + week + "?season_type=regular&" + pos + "&order_by=" + key);
    arr.forEach(e => {
      const p = e.player || {};
      dict[e.player_id] = { name: p.position === "DEF" ? (NICK[e.player_id] || e.player_id) : ((p.first_name || "") + " " + (p.last_name || "")).trim(), team: e.team || p.team || "", pos: p.position || "" };
      proj[e.player_id] = (e.stats || {})[key] || 0;
    });
  } catch (e) { /* unten Fallback */ }
  if (!Object.keys(dict).length) {
    const all = await j(API + "/players/nfl");
    Object.keys(all).forEach(id => {
      const p = all[id];
      dict[id] = { name: p.position === "DEF" ? (NICK[id] || id) : ((p.first_name || "") + " " + (p.last_name || "")).trim(), team: p.team || "", pos: p.position || "" };
    });
  }

  const userById = {}; users.forEach(u => { userById[u.user_id] = u; });
  const names = {};
  Object.keys(o.names || {}).forEach(k => { names[k.toLowerCase()] = o.names[k]; });
  const ownerOf = r => {
    const u = userById[r.owner_id] || {};
    const dn = u.display_name || u.username || ("Team " + r.roster_id);
    return names[dn.toLowerCase()] || names[String(u.username || "").toLowerCase()] || dn;
  };
  const rosterById = {}; rosters.forEach(r => { rosterById[r.roster_id] = r; });

  // Wochenergebnisse je Roster → Punkte-Historie + Streak
  const hist = {}; rosters.forEach(r => { hist[r.roster_id] = []; });
  perWeek.forEach((ms, wi) => {
    const byM = {};
    ms.forEach(m => { (byM[m.matchup_id] = byM[m.matchup_id] || []).push(m); });
    ms.forEach(m => {
      const opp = (byM[m.matchup_id] || []).find(x => x.roster_id !== m.roster_id);
      const played = (m.points || 0) > 0 || (opp && (opp.points || 0) > 0);
      hist[m.roster_id][wi] = { pts: m.points || 0, res: !played || !opp ? null : (m.points > opp.points ? "W" : (m.points < opp.points ? "L" : "T")) };
    });
  });
  const streakOf = arr => {
    const done = arr.filter(x => x && x.res);
    if (!done.length) return "–";
    const last = done[done.length - 1].res;
    let n = 0; for (let i = done.length - 1; i >= 0 && done[i].res === last; i--) n++;
    return last + n;
  };

  const managers = rosters.map(r => {
    const s = r.settings || {};
    const pf = (s.fpts || 0) + (s.fpts_decimal || 0) / 100;
    return {
      owner: ownerOf(r), rosterId: r.roster_id, wins: s.wins || 0, losses: s.losses || 0, ties: s.ties || 0,
      record: (s.wins || 0) + "-" + (s.losses || 0) + (s.ties ? "-" + s.ties : ""),
      streak: streakOf(hist[r.roster_id] || []),
      scores: (hist[r.roster_id] || []).map(x => x ? x.pts : 0), total: pf,
    };
  }).sort((a, b) => b.wins - a.wins || b.total - a.total);

  const SLOT = { SUPER_FLEX: "SFLX", WR_RB_FLEX: "FLEX", WR_TE_FLEX: "FLEX", RB_WR_TE: "FLEX", REC_FLEX: "FLEX", IDP_FLEX: "IDP" };
  const slots = (league.roster_positions || []).filter(p => p !== "BN" && p !== "IR" && p !== "TAXI").map(p => SLOT[p] || (p.includes("FLEX") ? "FLEX" : p));
  const lineup = m => (m.starters || []).map((id, i) => {
    const d = dict[id] || { name: id, team: "", pos: slots[i] || "" };
    const pts = m.starters_points ? m.starters_points[i] : ((m.players_points || {})[id] || 0);
    return { pos: slots[i] || d.pos, name: d.name, nfl: d.team || (d.pos === "DEF" ? id : ""), pts: (pts || 0).toFixed(2), proj: proj[id] == null ? null : proj[id] };
  });

  const cur = perWeek[week - 1] || [];
  const byM = {};
  cur.forEach(m => { (byM[m.matchup_id] = byM[m.matchup_id] || []).push(m); });
  const matchups = Object.keys(byM).sort((a, b) => a - b).map(id => {
    const [A, B] = byM[id];
    if (!A || !B) return null;
    return {
      a: ownerOf(rosterById[A.roster_id]), b: ownerOf(rosterById[B.roster_id]),
      ptsA: A.points || 0, ptsB: B.points || 0,
      lineupA: lineup(A), lineupB: lineup(B),
    };
  }).filter(Boolean);

  return { season, week, leagueName: league.name, managers, matchups, hasProjections: Object.keys(proj).length > 0 };
}
