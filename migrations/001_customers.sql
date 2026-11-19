-- Customers onboarded through the backend form.
create table if not exists customers (
    id          uuid primary key default gen_random_uuid(),
    name        text not null check (length(trim(name)) > 0),
    phone       text not null unique check (phone ~ '^\+[1-9][0-9]{6,14}$'),  -- E.164
    timezone    text not null,  -- IANA name, e.g. Europe/Zurich (validated in the app)
    opted_out   boolean not null default false,
    created_at  timestamptz not null default now()
);

-- The app connects with the database password, which bypasses RLS. Enabling it
-- without policies keeps the table closed to Supabase's public (anon key) API.
alter table customers enable row level security;
