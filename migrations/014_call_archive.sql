-- Foundation only: no capture job, storage bucket or staff endpoints are enabled here.
-- Independent of call_attempts: inbound/unknown calls have no local outbound attempt.
create table if not exists call_records (
    id                    uuid primary key default gen_random_uuid(),
    vapi_call_id          text unique check (length(trim(vapi_call_id)) > 0),
    attempt_id            uuid unique references call_attempts(id) on delete set null,
    scheduled_call_id     uuid references scheduled_calls(id) on delete set null,
    customer_id           uuid references customers(id) on delete set null,
    remote_phone          text check (remote_phone ~ '^\+[1-9][0-9]{6,14}$'),
    name_snapshot         text check (length(trim(name_snapshot)) > 0),
    direction             text not null check (direction in ('inbound', 'outbound', 'web', 'unknown')),
    provider_type         text,
    vapi_assistant_id     text,
    vapi_phone_number_id  text,
    provider_created_at   timestamptz,
    provider_updated_at   timestamptz,
    started_at            timestamptz,
    ended_at              timestamptz,
    duration_seconds      numeric check (duration_seconds >= 0),
    provider_status       text,
    ended_reason          text,
    cost                  numeric check (cost >= 0),
    currency              text,
    transcript            text,
    -- Only allowlisted spoken messages, never raw system/tool messages or prompts.
    messages              jsonb not null default '[]'::jsonb check (jsonb_typeof(messages) = 'array'),
    transcript_state      text not null default 'pending'
                          check (transcript_state in ('pending', 'ready', 'empty', 'disabled', 'unavailable')),
    audio_state           text not null default 'pending'
                          check (audio_state in ('pending', 'ready', 'disabled', 'unavailable', 'not_applicable')),
    audio_object_key      text unique check (length(trim(audio_object_key)) > 0),
    audio_media_type      text,
    audio_bytes           bigint check (audio_bytes > 0),
    audio_sha256          text check (audio_sha256 ~ '^[a-f0-9]{64}$'),
    archive_attempts      integer not null default 0 check (archive_attempts >= 0),
    next_archive_at       timestamptz not null default now(),
    archive_claimed_at    timestamptz,
    archive_claim_token   uuid,
    last_error_code       text check (length(last_error_code) between 1 and 100),
    last_polled_at        timestamptz,
    archived_at           timestamptz,
    deleted_at            timestamptz,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    constraint call_records_time_order check (ended_at >= started_at),
    constraint call_records_transcript_ready check (transcript_state <> 'ready' or transcript is not null),
    constraint call_records_audio_ready check (
        audio_state <> 'ready' or (audio_object_key is not null and audio_media_type is not null and audio_bytes is not null)
    ),
    constraint call_records_claim_pair check ((archive_claimed_at is null) = (archive_claim_token is null))
);

create index if not exists call_records_customer_time_idx on call_records (customer_id, started_at desc, id);
create index if not exists call_records_phone_idx on call_records (remote_phone);
create index if not exists call_records_schedule_idx on call_records (scheduled_call_id);
create index if not exists call_records_time_idx
    on call_records ((coalesce(started_at, provider_created_at, created_at)) desc, id) where deleted_at is null;
create index if not exists call_records_archive_due_idx
    on call_records (next_archive_at, id) where deleted_at is null and audio_state = 'pending';
create index if not exists call_records_poll_idx on call_records (last_polled_at, id) where deleted_at is null;

-- Old IDs continue to resolve after discovery and local attempt records are merged.
-- The application must flatten aliases to a canonical call record in the merge transaction.
create table if not exists call_record_aliases (
    alias_id        uuid primary key,
    call_record_id  uuid not null references call_records(id) on delete cascade,
    created_at      timestamptz not null default now(),
    check (alias_id <> call_record_id)
);
create index if not exists call_record_aliases_target_idx on call_record_aliases (call_record_id);

-- No FK to Supabase auth.users: labels/history survive staff account removal and
-- local tests do not require Supabase's auth schema. Authors come from verified JWTs.
create table if not exists call_notes (
    id                 uuid primary key default gen_random_uuid(),
    call_record_id     uuid references call_records(id) on delete cascade,
    scheduled_call_id  uuid references scheduled_calls(id) on delete cascade,
    author_user_id     uuid not null,
    author_label       text not null check (length(trim(author_label)) between 1 and 320),
    body               text not null check (length(trim(body)) between 1 and 4000),
    created_at         timestamptz not null default now(),
    constraint call_notes_one_target check (num_nonnulls(call_record_id, scheduled_call_id) = 1)
);
create index if not exists call_notes_record_idx on call_notes (call_record_id, created_at, id);
create index if not exists call_notes_schedule_idx on call_notes (scheduled_call_id, created_at, id);

-- Separate progress for recent discovery, historical import, and reconciliation.
-- A checkpoint advances only after the complete window is persisted, never on failure.
create table if not exists call_archive_scans (
    stream               text primary key check (stream in ('recent', 'backfill', 'reconcile')),
    completed_through    timestamptz,
    window_start         timestamptz,
    window_end           timestamptz,
    claimed_at           timestamptz,
    claim_token          uuid,
    last_started_at      timestamptz,
    last_succeeded_at    timestamptz,
    last_error_code      text check (length(last_error_code) between 1 and 100),
    consecutive_failures integer not null default 0 check (consecutive_failures >= 0),
    updated_at           timestamptz not null default now(),
    constraint call_archive_scans_window_pair check ((window_start is null) = (window_end is null)),
    constraint call_archive_scans_window_order check (window_end > window_start),
    constraint call_archive_scans_claim_pair check ((claimed_at is null) = (claim_token is null))
);

-- Keep after customer/call deletion, so scans cannot recreate removed history.
-- Phone markers apply to calls created at/before cutoff; later calls remain eligible.
create table if not exists call_archive_deletions (
    id             uuid primary key default gen_random_uuid(),
    vapi_call_id   text unique check (length(trim(vapi_call_id)) > 0),
    remote_phone   text check (remote_phone ~ '^\+[1-9][0-9]{6,14}$'),
    cutoff_at      timestamptz,
    created_at     timestamptz not null default now(),
    constraint call_archive_deletions_target check (
        (vapi_call_id is not null and remote_phone is null and cutoff_at is null)
        or (vapi_call_id is null and remote_phone is not null and cutoff_at is not null)
    ),
    unique (remote_phone, cutoff_at)
);
create index if not exists call_archive_deletions_phone_idx on call_archive_deletions (remote_phone, cutoff_at);

-- SQL row removal and object deletion are not atomic. Cleanup survives both.
-- No call/customer FK: deleting those records must never discard pending cleanup.
create table if not exists call_audio_cleanup (
    object_key      text primary key check (length(trim(object_key)) > 0),
    attempts        integer not null default 0 check (attempts >= 0),
    next_attempt_at timestamptz not null default now(),
    claimed_at      timestamptz,
    claim_token     uuid,
    last_error_code text check (length(last_error_code) between 1 and 100),
    created_at      timestamptz not null default now(),
    constraint call_audio_cleanup_claim_pair check ((claimed_at is null) = (claim_token is null))
);
create index if not exists call_audio_cleanup_due_idx on call_audio_cleanup (next_attempt_at, object_key);

-- Backend SQL credentials bypass RLS. No public/anon/authenticated policies.
alter table call_records enable row level security;
alter table call_record_aliases enable row level security;
alter table call_notes enable row level security;
alter table call_archive_scans enable row level security;
alter table call_archive_deletions enable row level security;
alter table call_audio_cleanup enable row level security;
