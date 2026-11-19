-- Customers are called from different Vapi phone numbers (US, CH, ...) and may use different assistants.

-- 1. Defaults per customer.
alter table customers
    add column vapi_phone_number_id text,
    add column vapi_assistant_id    text;

-- 2. Backfill existing customers: REPLACE the two ids before running (or delete the test customers instead).
update customers set
    vapi_phone_number_id = 'REPLACE_WITH_VAPI_PHONE_NUMBER_ID',
    vapi_assistant_id    = 'REPLACE_WITH_VAPI_ASSISTANT_ID'
where vapi_phone_number_id is null or vapi_assistant_id is null;

alter table customers
    alter column vapi_phone_number_id set not null,
    alter column vapi_assistant_id    set not null;

-- 3. Optional per-callback overrides (null = the customer's default).
alter table scheduled_calls
    add column vapi_phone_number_id text,
    add column vapi_assistant_id    text;

-- 4. What was actually used for each call (null for attempts recorded before this migration).
alter table call_attempts
    add column vapi_phone_number_id text,
    add column vapi_assistant_id    text;

-- Dispatcher: is this phone number busy?
create index if not exists call_attempts_open_number_idx on call_attempts (vapi_phone_number_id) where ended_at is null;
