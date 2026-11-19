-- Keep the destination on every outbound attempt, including ad-hoc calls to non-customers.
-- This lets an interrupted pre-dial reservation be recovered from Vapi and prevents two
-- simultaneous calls to the same person even when different Vapi phone numbers are used.
alter table call_attempts add column if not exists customer_phone text;

update call_attempts a set customer_phone = c.phone
from customers c
where c.id = a.customer_id and a.customer_phone is null;

alter table call_attempts drop constraint if exists call_attempts_customer_phone_check;
alter table call_attempts add constraint call_attempts_customer_phone_check
    check (customer_phone is null or customer_phone ~ '^\+[1-9][0-9]{6,14}$');

create index if not exists call_attempts_open_customer_phone_idx
    on call_attempts (customer_phone) where ended_at is null;
