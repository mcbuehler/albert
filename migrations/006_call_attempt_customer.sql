-- Link every call attempt to its customer (ad-hoc /api/call attempts have no scheduled call),
-- so the customer overview can show the last call. Safe to re-run.

-- Replaced by the customer overview query (GET /api/customers); drop it if an earlier draft was applied.
drop view if exists customers_without_callback;

alter table call_attempts add column if not exists customer_id uuid references customers(id) on delete cascade;

update call_attempts a set customer_id = sc.customer_id
from scheduled_calls sc
where sc.id = a.scheduled_call_id and a.customer_id is null;

create index if not exists call_attempts_customer_idx on call_attempts (customer_id, started_at desc);
