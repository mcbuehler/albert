-- Retries used to be due at now() + N minutes with seconds, so a callback scheduled for the same minute wasn't
-- detected as a duplicate and the AI couldn't find the retried callback by its time. Retries are whole minutes
-- now; round the open callbacks to match. Safe to re-run.
update scheduled_calls set due_at = date_trunc('minute', due_at)
where status in ('pending', 'calling') and due_at <> date_trunc('minute', due_at);
