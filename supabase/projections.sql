-- Sleeper projections of the upcoming week. Filled by sync_supabase.py
-- (ONLY=projections or the weekly sync); rows of a week are replaced on every run.
-- Apply once in the Supabase SQL editor (or via migration) before the first run.

create table if not exists public.player_projections (
  season_year  integer not null,
  week         integer not null,
  sleeper_id   text    not null,
  player_name  text,
  position     text,
  team         text,
  pts_half_ppr numeric,
  pts_ppr      numeric,
  pts_std      numeric,
  updated_at   timestamptz not null default now(),
  primary key (season_year, week, sleeper_id)
);

-- Projected result per matchup: best possible lineup of the whole roster
-- (half-PPR), win_prob_a = logistic of the point difference (as in the Preview post).
create table if not exists public.matchup_projections (
  season_year integer not null,
  week        integer not null,
  manager_a   text    not null,
  manager_b   text    not null,
  proj_a      numeric,
  proj_b      numeric,
  win_prob_a  numeric,
  is_playoff  boolean not null default false,
  updated_at  timestamptz not null default now(),
  primary key (season_year, week, manager_a, manager_b)
);

create index if not exists idx_player_projections_pos on public.player_projections (season_year, week, position);

alter table public.player_projections enable row level security;
alter table public.matchup_projections enable row level security;

drop policy if exists "Allow public read access" on public.player_projections;
create policy "Allow public read access" on public.player_projections for select to anon using (true);
drop policy if exists "Allow public read access" on public.matchup_projections;
create policy "Allow public read access" on public.matchup_projections for select to anon using (true);
