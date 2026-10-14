-- Einmal im Supabase SQL Editor ausführen.
-- Rechnet Rekorde und Achievements neu und schreibt sie in die Live-Tabellen.
-- Wird von sync_supabase.py nach jedem Sync aufgerufen (nur mit Service-Key erlaubt).

create or replace function public.refresh_league_derived() returns json
language plpgsql security definer set search_path = public, rebuild as $$
declare
  rec_rows int; ach_rows int;
begin
  perform rebuild.compute_records();

  delete from public.record_timeline where record_key in (select record_key from rebuild.record_defs);
  insert into public.record_timeline
    (record_key, record_label, period_id, manager_id, from_year, from_week, to_year, to_week, weeks_held, record_value, is_current)
  select record_key, record_label, period_id, manager_id, from_year, from_week, to_year, to_week, weeks_held, record_value, is_current
  from rebuild.record_timeline_new;
  get diagnostics rec_rows = row_count;

  delete from public.record_owner_values where record_key in (select record_key from rebuild.record_defs);
  insert into public.record_owner_values (record_key, manager_id, current_value)
  select record_key, manager_id, current_value from rebuild.record_owner_values_new;

  create temp table _meta on commit drop as
    select distinct on (achievement_key) achievement_key, achievement_category, description
    from public.achievements order by achievement_key, id;
  create temp table _calc on commit drop as select * from rebuild.achievements_calc;

  delete from public.achievements a
  where not coalesce(a.is_manual, false)
    and a.achievement_key in (select distinct achievement_key from _calc);
  insert into public.achievements
    (season_year, manager_id, achievement_key, achievement_category, value, notes, is_manual, description, matchup_id)
  select c.season_year, c.manager_id, c.achievement_key, m.achievement_category, c.value, c.notes, false, m.description, c.matchup_id
  from _calc c join _meta m using (achievement_key);
  get diagnostics ach_rows = row_count;

  return json_build_object('record_timeline_rows', rec_rows, 'achievement_rows', ach_rows);
end;
$$;

revoke all on function public.refresh_league_derived() from public, anon, authenticated;
grant execute on function public.refresh_league_derived() to service_role;
