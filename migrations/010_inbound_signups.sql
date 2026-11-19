-- Customers who signed up themselves on an inbound call (Vapi tool onboard_customer): live immediately, but marked
-- for the team to review. Safe to re-run.
alter table customers add column if not exists source text not null default 'staff';
alter table customers add column if not exists needs_review boolean not null default false;

alter table customers drop constraint if exists customers_source_check;
alter table customers add constraint customers_source_check check (source in ('staff', 'inbound'));

create index if not exists customers_inbound_created_idx on customers (created_at) where source = 'inbound';

-- Team notification for each inbound sign-up.
alter table team_notifications drop constraint if exists team_notifications_reason_check;
alter table team_notifications add constraint team_notifications_reason_check
    check (reason in ('no_consent', 'institution_lead', 'caregiver_lead', 'request', 'inbound_signup', 'other'));
