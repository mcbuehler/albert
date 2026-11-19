-- Persist subdivided windows, so a dense scan can resume within the request budget.
alter table call_archive_scans add column if not exists pending_windows jsonb not null default '[]'::jsonb;
alter table call_archive_scans drop constraint if exists call_archive_scans_pending_windows_check;
alter table call_archive_scans add constraint call_archive_scans_pending_windows_check
    check (jsonb_typeof(pending_windows) = 'array');
