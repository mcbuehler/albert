-- Keep temporary email claims separate from successful delivery. A dispatcher that crashes
-- after claiming rows no longer leaves them permanently marked as emailed.
alter table team_notifications add column if not exists email_claimed_at timestamptz;

create index if not exists team_notifications_pending_email_idx
    on team_notifications (created_at) where emailed_at is null;
