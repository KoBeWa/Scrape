-- Percent ranks of fa_transaction_scores.net_pts_adj per season and over all
-- seasons. Called by sync_supabase.py after each transaction sync (adding a
-- season shifts every transaction_score_alltime). Already applied in Supabase.

create or replace function public.refresh_transaction_scores() returns void
language sql security definer set search_path = public as $$
  update public.fa_transaction_scores s
  set transaction_score = r.ts, transaction_score_alltime = r.ta
  from (
    select ctid as c,
      round((100 * percent_rank() over (partition by season_year order by net_pts_adj))::numeric, 1) as ts,
      round((100 * percent_rank() over (order by net_pts_adj))::numeric, 1) as ta
    from public.fa_transaction_scores
  ) r
  where s.ctid = r.c
    and (s.transaction_score is distinct from r.ts or s.transaction_score_alltime is distinct from r.ta);
$$;
revoke all on function public.refresh_transaction_scores() from public, anon, authenticated;
grant execute on function public.refresh_transaction_scores() to service_role;
