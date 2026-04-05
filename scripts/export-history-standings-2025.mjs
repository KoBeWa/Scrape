// scripts/export-history-standings-2025.mjs
// Aufruf:
// node scripts/export-history-standings-2025.mjs <league_id>

import fs from "node:fs/promises";
import path from "node:path";

const LEAGUE_ID = process.argv[2];
const SEASON_YEAR = 2025;
const OUT_DIR = path.resolve("output/history-standings");

if (!LEAGUE_ID) {
  console.error("Bitte league_id angeben:");
  console.error("node scripts/export-history-standings-2025.mjs <league_id>");
  process.exit(1);
}

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} bei ${url}`);
  }
  return res.json();
}

function toFixed2(value) {
  if (value === null || value === undefined || value === "") return "";
  return Number(value).toFixed(2);
}

function clean(value) {
  return value === null || value === undefined ? "" : String(value);
}

function tsv(rows, columns) {
  const header = columns.join("\t");
  const body = rows
    .map((row) => columns.map((col) => clean(row[col])).join("\t"))
    .join("\n");
  return `${header}\n${body}\n`;
}

function rosterPoints(settings, keyMain, keyDec) {
  const main = Number(settings?.[keyMain] ?? 0);
  const dec = Number(settings?.[keyDec] ?? 0);
  return Number(`${main}.${dec}`);
}

function sortRegularSeason(rosters) {
  return [...rosters].sort((a, b) => {
    const aw = Number(a.settings?.wins ?? 0);
    const bw = Number(b.settings?.wins ?? 0);
    if (bw !== aw) return bw - aw;

    const at = Number(a.settings?.ties ?? 0);
    const bt = Number(b.settings?.ties ?? 0);
    if (bt !== at) return bt - at;

    const apf = rosterPoints(a.settings, "fpts", "fpts_decimal");
    const bpf = rosterPoints(b.settings, "fpts", "fpts_decimal");
    if (bpf !== apf) return bpf - apf;

    const apa = rosterPoints(a.settings, "fpts_against", "fpts_against_decimal");
    const bpa = rosterPoints(b.settings, "fpts_against", "fpts_against_decimal");
    if (apa !== bpa) return apa - bpa;

    return Number(a.roster_id) - Number(b.roster_id);
  });
}

function buildDraftPositionMap(draftPicks, usersById) {
  const map = new Map();

  const firstRound = draftPicks
    .filter((p) => Number(p.round) === 1)
    .sort((a, b) => Number(a.pick_no) - Number(b.pick_no));

  for (const pick of firstRound) {
    const pickedBy = pick.picked_by;
    if (!pickedBy) continue;

    const user = usersById.get(String(pickedBy));
    if (!user) continue;

    map.set(String(user.user_id), Number(pick.pick_no));
  }

  return map;
}

function buildWeekPointsMap(matchups) {
  const map = new Map();
  for (const m of matchups) {
    map.set(String(m.roster_id), Number(m.points ?? 0));
  }
  return map;
}

async function main() {
  const base = "https://api.sleeper.app/v1";

  const league = await getJson(`${base}/league/${LEAGUE_ID}`);
  const [rosters, users, drafts] = await Promise.all([
    getJson(`${base}/league/${LEAGUE_ID}/rosters`),
    getJson(`${base}/league/${LEAGUE_ID}/users`),
    getJson(`${base}/league/${LEAGUE_ID}/drafts`)
  ]);

  const usersById = new Map(users.map((u) => [String(u.user_id), u]));
  const rostersById = new Map(rosters.map((r) => [String(r.roster_id), r]));

  // Draftposition aus erstem Draft der Liga
  let draftPositionByManager = new Map();
  if (Array.isArray(drafts) && drafts.length > 0) {
    const draft = drafts[0];
    const draftPicks = await getJson(`${base}/draft/${draft.draft_id}/picks`);
    draftPositionByManager = buildDraftPositionMap(draftPicks, usersById);
  }

  // Playoff-Daten
  const [week15Matchups, week16Matchups] = await Promise.all([
    getJson(`${base}/league/${LEAGUE_ID}/matchups/15`).catch(() => []),
    getJson(`${base}/league/${LEAGUE_ID}/matchups/16`).catch(() => [])
  ]);

  const week15ByRoster = buildWeekPointsMap(week15Matchups);
  const week16ByRoster = buildWeekPointsMap(week16Matchups);

  // Reg-Season Rank selbst sortiert
  const sortedRegular = sortRegularSeason(rosters);
  const regRankByRoster = new Map(
    sortedRegular.map((r, idx) => [String(r.roster_id), idx + 1])
  );

  const seasonRows = rosters.map((roster) => {
    const ownerId = String(roster.owner_id ?? "");
    const settings = roster.settings ?? {};

    const pointsFor = rosterPoints(settings, "fpts", "fpts_decimal");
    const pointsAgainst = rosterPoints(
      settings,
      "fpts_against",
      "fpts_against_decimal"
    );

    return {
      season_year: SEASON_YEAR,
      manager_id: ownerId,
      reg_rank: regRankByRoster.get(String(roster.roster_id)) ?? "",
      wins: Number(settings.wins ?? 0),
      losses: Number(settings.losses ?? 0),
      ties: Number(settings.ties ?? 0),
      points_for: toFixed2(pointsFor),
      points_against: toFixed2(pointsAgainst),
      moves: Number(settings.total_moves ?? settings.moves ?? 0),
      trades: Number(settings.trades ?? 0),
      draft_position: draftPositionByManager.get(ownerId) ?? "",
      // Sleeper liefert meist playoff_seed und rank direkt in roster.settings
      playoff_rank: settings.rank ?? "",
      championship_finish: settings.rank ?? ""
    };
  });

  const playoffRows = rosters
    .filter((roster) => {
      const seed = roster.settings?.playoff_seed;
      return seed !== undefined && seed !== null;
    })
    .map((roster) => {
      const ownerId = String(roster.owner_id ?? "");
      const seed = Number(roster.settings?.playoff_seed ?? 0);

      return {
        season_year: SEASON_YEAR,
        manager_id: ownerId,
        seed,
        week15_pts: toFixed2(week15ByRoster.get(String(roster.roster_id)) ?? ""),
        week16_pts: toFixed2(week16ByRoster.get(String(roster.roster_id)) ?? ""),
        final_rank: roster.settings?.rank ?? ""
      };
    })
    .sort((a, b) => a.seed - b.seed);

  // Sortierung TSV 1 nach Reg Rank
  seasonRows.sort((a, b) => Number(a.reg_rank) - Number(b.reg_rank));

  await fs.mkdir(OUT_DIR, { recursive: true });

  const seasonFile = path.join(OUT_DIR, `${SEASON_YEAR}.tsv`);
  const playoffFile = path.join(OUT_DIR, `playoffs-${SEASON_YEAR}.tsv`);

  await fs.writeFile(
    seasonFile,
    tsv(seasonRows, [
      "season_year",
      "manager_id",
      "reg_rank",
      "wins",
      "losses",
      "ties",
      "points_for",
      "points_against",
      "moves",
      "trades",
      "draft_position",
      "playoff_rank",
      "championship_finish"
    ]),
    "utf8"
  );

  await fs.writeFile(
    playoffFile,
    tsv(playoffRows, [
      "season_year",
      "manager_id",
      "seed",
      "week15_pts",
      "week16_pts",
      "final_rank"
    ]),
    "utf8"
  );

  console.log(`Erstellt: ${seasonFile}`);
  console.log(`Erstellt: ${playoffFile}`);
  console.log(`League: ${league.name} (${league.league_id})`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
