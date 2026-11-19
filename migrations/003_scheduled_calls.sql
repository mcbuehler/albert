-- One-time callbacks and every outbound call attempt (scheduled or not).

create table if not exists scheduled_calls (
    id                   uuid primary key default gen_random_uuid(),
    customer_id          uuid not null references customers(id) on delete cascade,
    local_time           timestamp not null,    -- requested time, customer-local (no zone)
    due_at               timestamptz not null,  -- next dial time in UTC (moved by calling hours / retries)
    status               text not null default 'pending'
                         check (status in ('pending', 'calling', 'completed', 'failed', 'cancelled')),
    source_vapi_call_id  text,                  -- call in which the callback was requested, if any
    created_at           timestamptz not null default now()
);

-- Dispatcher: next due callback.
create index if not exists scheduled_calls_due_idx on scheduled_calls (due_at) where status = 'pending';
-- Pending-per-customer limit and the "no pending callback" view.
create index if not exists scheduled_calls_customer_idx on scheduled_calls (customer_id, status);

create table if not exists call_attempts (
    id                 uuid primary key default gen_random_uuid(),
    scheduled_call_id  uuid references scheduled_calls(id) on delete cascade,  -- null for ad-hoc /api/call calls
    attempt_no         integer not null default 1 check (attempt_no >= 1),
    vapi_call_id       text not null unique,
    started_at         timestamptz not null default now(),
    ended_reason       text,
    ended_at           timestamptz,             -- null while dialing / in progress
    unique (scheduled_call_id, attempt_no)
);

-- Dispatcher: is an outbound call still running?
create index if not exists call_attempts_open_idx on call_attempts (started_at) where ended_at is null;

alter table scheduled_calls enable row level security;
alter table call_attempts   enable row level security;
