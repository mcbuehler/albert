-- Notes from Albert to the team (Vapi tool notify_team): institution / caregiver leads, callers who don't want to be
-- recorded, requests Albert can't handle. The dispatcher cron emails each one (env TEAM_EMAIL) and sets emailed_at.
-- Safe to re-run.
create table if not exists team_notifications (
    id            uuid primary key default gen_random_uuid(),
    created_at    timestamptz not null default now(),
    reason        text not null check (reason in ('no_consent', 'institution_lead', 'caregiver_lead', 'request', 'other')),
    caller_phone  text,                   -- normalized E.164, null if the number wasn't usable
    contact_name  text check (contact_name is null or length(contact_name) <= 200),
    organization  text check (organization is null or length(organization) <= 200),
    summary       text check (summary is null or length(summary) <= 1000),
    vapi_call_id  text,
    emailed_at    timestamptz
);

alter table team_notifications enable row level security;
