-- When the dispatcher last ran (Vercel Cron every minute, or "Run dispatcher now"), shown on the staff page.
-- Exactly one row (id = 1), upserted at the end of every run. Safe to re-run.
create table if not exists dispatcher_status (
    id            smallint primary key default 1 check (id = 1),
    last_run_at   timestamptz not null,
    last_run_ok   boolean not null,             -- false if claiming callbacks failed
    last_started  integer not null default 0    -- calls started in that run
);

alter table dispatcher_status enable row level security;
