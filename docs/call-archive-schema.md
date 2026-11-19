# Calls archive: database contracts

Apply `migrations/014_call_archive.sql` and then `migrations/015_archive_scan_windows.sql` after 001–013.
Both are additive and rerunnable. Migration 015 adds a JSON array of pending scan subdivisions.
The scheduler and private storage bucket are set up separately in the [quickstart](setup.md).

## Capture and retention

The archive job scans the calls visible to your Vapi API key, including unknown and rejected callers. It stores call metadata and spoken transcripts in Postgres and available recordings in the private Supabase bucket. This runs independently of Hindsight memory and its onboarding consent. Use a dedicated Vapi organization if the calls in another project should stay out of this archive.

The reconciliation window defaults to 14 days to match the original Vapi account's retention. Set `ARCHIVE_PROVIDER_RETENTION_DAYS` in [`albert/config.py`](../albert/config.py) for your account. Albert keeps its archived copies independently of Vapi's retention. Deleting a customer removes their archive entries and queues recordings for deletion. Erasing Hindsight memory keeps the archive. To disable archive capture, set `ARCHIVE_CAPTURE_ENABLED = False`. The database contracts are described below.

## Tables

| Table | Purpose |
|---|---|
| `call_records` | Durable identity and eventual metadata/transcript/audio reference for one actual call or local attempt |
| `call_record_aliases` | Resolve an old call-record UUID after two records are merged |
| `call_notes` | Chronological staff messages on a call record or a scheduled callback |
| `call_archive_scans` | Separate checkpoint, active scan window, lease and diagnostic state for recent/backfill/reconcile scans |
| `call_archive_deletions` | Persistent suppression by Vapi ID, or normalized phone through a provider creation-time cutoff |
| `call_audio_cleanup` | Durable retries for object deletion, independent of call/customer rows |

All six tables have RLS enabled without policies. The backend accesses them through its SQL connection.
The browser uses staff-authenticated Flask endpoints, including to request signed links for recordings.
`SUPABASE_SECRET_KEY` is a backend-only credential used for storage operations and checked by `/api/health`.

## Call identity and lifecycle

`call_records.id` is the permanent staff-facing UUID. Nullable `vapi_call_id` and `attempt_id`
are each unique. Unknown/inbound calls need neither a customer nor a local attempt. Phone
and name snapshots are nullable; no placeholder name or unusable phone is invented in storage.
Provider IDs stay text to match the existing tables. All instants use `timestamptz`.

Links to customers, scheduled callbacks and attempts use `ON DELETE SET NULL`. Existing
dispatcher recovery deliberately removes undialled attempt rows; an independently saved
call record and its staff notes must survive. A retained local attempt with no actual dial
will eventually display “Not placed,” rather than a fabricated Vapi call. Scheduled callbacks
remain in `scheduled_calls`, and no attempt counts or retry behavior change.

Discovery/matching now serializes archive writes using a dedicated transaction advisory lock,
merges collisions atomically, moves notes to the canonical provider record, repoints aliases,
and retains an alias for the removed UUID. `ensure_attempt_call_record` creates/resolves an
identity for future note endpoints. These functions do not change scheduling status or counts.
The same advisory lock is used by customer deletion and recording finalization. DB tests exercise identity
races, note/alias preservation, audio preservation and redundant-object cleanup.

Keep raw provider status/ended reason separate from staff-facing labels. `direction` is
inbound/outbound/web/unknown; `provider_type` retains the original provider type. Preserve
provider creation/update times separately from local insertion/update times. `updated_at`
must be explicitly maintained by writers; no trigger changes existing application behavior.

## Transcripts and audio

Transcript states: pending, ready, empty, disabled, unavailable. Audio states: pending, ready,
disabled, unavailable, not_applicable. A ready transcript requires text; ready audio requires
an object key, media type and positive byte count. A SHA-256 checksum can be saved when known.
Audio bytes themselves live in the planned private Supabase bucket, not PostgreSQL.

`messages` is a JSON array; its safe shape is an ingestion responsibility. Only customer and
assistant spoken text and supported timing fields will be retained. System prompts, injected
memories, tool calls/results and credentials must never be copied from the raw provider array.
Later partial responses must not erase previously saved artifacts. No raw provider-payload
column is provided. Duration and cost are nonnegative; unknown values stay null.

`archive_attempts`, `next_archive_at`, claim timestamp/token, last polling time and generic
error code support future artifact retries. A claim requires both timestamp and UUID token;
workers will check the token before committing to prevent stale workers overwriting a newer
claim. `archived_at` is set by the worker when required artifact processing is complete or
terminally accounted for, rather than implying every call necessarily has audio.

## Audio transfer (chunk 3)

`vapi.recording_url` asks `GET /call/{id}/stereo-recording` (customer and assistant channels) with redirects disabled and returns the
HTTPS `Location` of Vapi's short-lived signed URL (404 means no recording yet; anything else is a
failure). `archive.download_audio` fetches it without credentials, streams into a temporary file
(memory up to 8 MB, then disk), stops above 100 MB or at the caller's deadline, detects WAV/MP3
from the first bytes and computes SHA-256. `storage.upload_object` upserts the private object
`call-recordings/calls/<call_records.id>.<wav|mp3>` with `SUPABASE_SECRET_KEY`, so retries replace
one object. `archive.transfer_audio` returns the ready `audio_*` fields. Signed URLs and response
bodies are never logged; logs and `last_error_code` hold only generic codes.

Jobs: each cron run, after discovery and refresh, starts up to 3 recordings while at least 15 s of
its budget remain. `claim_archive_audio` leases one ended call with `audio_state = 'pending'` and a
due `next_archive_at` (`FOR UPDATE SKIP LOCKED`; claims older than 5 minutes are taken over).
`finish_archive_audio` (under the archive advisory lock) saves the fields, `archived_at` and clears
the claim only while the token still matches, the record isn't deleted and no deletion marker
matches. Otherwise the uploaded key is queued in `call_audio_cleanup` unless a record references it,
and a suppressed record becomes `unavailable`. **The chunk-4 cleanup worker must re-check that no
record references a key before deleting it** (a slower stale worker can queue a key that the new
owner then saves). `retry_archive_audio` counts the attempt, saves the code and waits
1, 2, 4 … minutes (max 60); 24 hours after the record was created (so imported older calls get a
full day too), or immediately for `audio_too_large`,
the audio becomes `unavailable`. "No recording yet" (404) is a quiet retry; other failures are
logged and counted in the run's `audio_failed`, never failing the scan. `archived_at` marks when
audio processing became final (ready or unavailable).

`finalize_archive_transcripts` marks ended calls still `pending` 10 minutes after their end as
`empty`, so unanswered calls leave the refresh rotation. A later scan never moves a final transcript
or audio state back to `pending`; a real transcript can still replace `empty`. Debug shows retrying
and unavailable recording counts.

## Notes

Each note targets exactly one call record or one scheduled callback. A 4,000-character maximum
and nonblank text are enforced in SQL. The future route supplies author UUID and label from
the verified staff session; the client cannot choose another author. Labels are at most 320
characters, allowing email as a fallback. Staff account removal does not erase history.

Notes are append-only through the planned API; no database trigger forbids maintenance or
deletion cleanup. Concurrent messages are separate rows, ordered by timestamp then UUID.
Scheduled discussions stay attached to the callback and can be displayed alongside each
attempt's own discussion. Notes do not automatically enter Hindsight or assistant prompts.

## Scans, deletion and cleanup

Scan rows are created when discovery starts, for streams `recent`, `backfill`, and `reconcile`.
`completed_through` is an ascending provider creation-time watermark: a complete root window is
durably saved before this advances. `window_start/end` preserve its bounds; `pending_windows`
stores a FIFO subdivision stack as pairs of ISO timestamps. Neither bound alone is valid. A
two-minute timestamp/token lease prevents concurrent scan ownership; checkpoints reject stale
tokens after takeover. An interrupted root window is resumed before selecting another.

Recent scans initially cover ten minutes, then start two minutes before the last completed
watermark. Requests use inclusive millisecond bounds and pages of 100. Full windows split into
overlapping halves and persist the remaining stack; a full one-millisecond window fails with
`saturated_window` rather than advancing. Each invocation allows six list requests and five
individual refreshes, with a 45-second nominal budget and twelve seconds reserved for refresh.
Provider calls have connect/read timeouts; database writes already in flight may overrun the
nominal budget. Real deployment latency/recording-size checks remain necessary before activation.

Known active calls and pending transcripts rotate by last polling time with active calls first;
late dispatcher ID attachments are also revisited. Failed refreshes update their polling time
to avoid repeatedly blocking the same batch. Older response timestamps do not regress saved
metadata, and shorter/empty message histories do not replace longer saved conversations.
An ended call without transcript becomes `empty` after 10 minutes (chunk 3).
The `reconcile` stream (chunk 4b) shares the same window/lease/split code with its own row: after
recent discovery, each run allows 2 list requests. Root windows are 6 hours, oldest first, from
`now - ARCHIVE_PROVIDER_RETENTION_DAYS` (14) up to `now - ARCHIVE_INITIAL_LOOKBACK_MINUTES`; the next
window overlaps the watermark by 2 minutes, and once the watermark reaches that end the sweep
restarts at the oldest time. Its first sweep is the backfill (no separate `backfill` stream is used;
the value stays reserved). Saved calls are upserted (later provider data refreshes metadata; a window's calls share one connection via `save_archive_calls`, one transaction and archive lock per call),
deletion markers are honoured. Reconcile failures stay in its row (`consecutive_failures`) and the
run's `reconcile_error`, never failing the cron response. Debug shows how far older calls are
checked. Scan progress/finish match by claim token only. No checkpoint may advance over failed
or incomplete discovery results. Saved count means records processed, not newly inserted rows.

Deletion markers target either a Vapi call ID, or a normalized phone plus cutoff time. Phone
markers suppress calls with `provider_created_at <= cutoff_at`, while future calls remain
eligible. Uncertain creation times require conservative handling before import, not bypassing
the marker. Known IDs are suppressed regardless of phone visibility. Markers deliberately have
no customer or call FK.

`deleted_at` reserves immediate application hiding for a future single-call deletion. Object keys in `call_audio_cleanup` are unique,
and cleanup requires timestamp/token pairs for leased work. These jobs have no call FK so
removing SQL history cannot silently abandon a recording in storage. Successful deletion
removes the cleanup job; missing objects count as success. In-flight uploads must check markers
before becoming visible and enqueue cleanup if deletion won the race.

**Customer deletion (chunk 4a).** `db.delete_customer` takes the archive advisory lock *before*
the customer row lock (archive writers lock in that order, so no deadlock) and, unless a call is
open, `_purge_customer_archive` in the same transaction: a phone marker with cutoff `now()`, a
marker for every known Vapi call ID (archived records and the customer's attempts), cleanup jobs
for their recordings, then deletes every record linked to the customer or from their number
(notes/aliases cascade; scheduled-callback notes go with the callbacks). Calls from the number
after the cutoff are archived again. An upload finishing after the deletion finds no record and
queues its object. A run whose claim was taken over queues nothing (the new owner writes the same
key). `claim_audio_cleanup` first drops jobs for keys a record still references, then leases one
(5-minute takeover); the cron deletes the object via Supabase's bulk delete (a missing object counts
as deleted), `finish_audio_cleanup` removes the job, `retry_audio_cleanup` backs off 1–60 minutes and
retries forever. Up to 10 per run, before audio transfer. Erase memory keeps the archive (dialog says
so). Single-call deletion, including for unknown callers, remains a production gap.

Short-lived playback links with renewal were approved by the user. Five minutes is the initial
planned lifetime; access revocation cannot recall already downloaded audio. Archive retention
duration remains undecided; this migration schedules no automatic expiry.

## Calls list (chunk 5a)

`GET /api/calls` (staff) returns `{calls, total}` from `db.list_calls`, one query over three sources:

| Entry ID | Rows | Call date |
|---|---|---|
| `call:<call_records.id>` | archived calls not marked deleted | start, else provider creation, else insert time |
| `attempt:<call_attempts.id>` | local attempts without a call record (matched by attempt ID or Vapi ID) | attempt start |
| `scheduled:<scheduled_calls.id>` | pending callbacks, and cancelled ones that were never dialled | due time |

A callback being dialled has status `calling` in `scheduled_calls`, so only its attempt is listed; a
retried callback shows each past attempt plus its upcoming retry. Status: `voicemail`, `no_answer`
(`config.CALL_NO_ANSWER_REASONS`), `failed` (ended reason contains error/failed/fault), `completed`
(any other ended reason, `ended_at` or provider status `ended`), otherwise `calling`; callbacks are
`scheduled` or `cancelled`. Name is the linked customer's current name, else the snapshot (null =
unknown caller); phone is the record's number, else the customer's. Search matches the name
(ILIKE, wildcards escaped) or the digits of the number with formatting ignored. Filters take
statuses or `past` (completed, no answer, voicemail, failed). Sort columns are allowlisted
(`db.CALL_SORT_COLUMNS`), missing values last, ties by entry ID; `limit` ≤ 100, `offset` paging,
`total` from a window count. Entries carry transcript/audio state and note count, never content.

## Call detail and playback (chunk 6a)

`GET /api/calls/<entry-id>` (`db.get_call`) returns the list fields plus start/end, transcript,
spoken messages, callback local time and instructions, and notes (the record's own and its
callback's, oldest first, each with `target` `call`/`callback`). Old IDs still open: a merged
record's alias and an attempt archived since the list was loaded resolve to the current `call:`
entry, returned as `id`. Deleted records and callbacks no longer listed return 404.

`POST /api/calls/<entry-id>/audio-url` signs the saved object (`audio_state = 'ready'`, not deleted)
via Supabase `POST /storage/v1/object/sign/<bucket>/<key>` for `ARCHIVE_PLAYBACK_SECONDS` (300) and
returns `{url, expires_in}`; the browser streams it from Supabase. Issued links stay valid until they
expire, even after the call is deleted. Both replies are `no-store`.

## Writing notes (chunk 7)

`POST /api/calls/<entry-id>/notes` takes `{body}` (trimmed, 1–4,000 characters) and stores the
verified login as author: `author_user_id` = token subject, `author_label` = Supabase
`user_metadata.full_name` or `name`, else the email. A note on an attempt first creates its durable
call record (`ensure_attempt_call_record`), so it survives recovery and merges. The target row is
key-share locked while inserting; a deleted or missing target returns 404. The reply has the
current entry ID and the stored note.

## Verification

`tests/test_db.py` rebuilds the schema from all migrations against local throwaway PostgreSQL.
Archive tests cover nullable/unique identities, FK deletion behavior, note targeting and limits,
artifact/lease/window constraints, surviving cleanup/markers, RLS and rerunning migration 014.
No real Vapi, Supabase or Hindsight services are used.

`tests/test_archive.py` covers safe provider parsing, recording redirects, bounded downloads, audio transfer, audio job retries/budget, strict list/detail reads, window splitting,
budget limits, lease loss and independent refresh after scan failure. Route tests cover staff and
cron access, disabled capture, status errors and generic responses. See the [test instructions](development.md#tests)
to run the full suite against disposable PostgreSQL.

`config.ARCHIVE_CAPTURE_ENABLED` defaults to `True`, and `vercel.json` schedules `/api/cron/archive` every minute.
Apply migrations 014–015 before running the cron. The private bucket and `SUPABASE_SECRET_KEY` are required
to transfer and play recordings. Check the staff Debug tab for scan status and the Calls tab for playback.
With the capture flag set to `False`, the status endpoint returns `{"enabled": false}` and capture is paused.

Archive implementation functions use argument/return annotations and docstrings, with Google-style
contracts on ingestion, discovery, identity merging and scan ownership. Parsing, window processing,
refresh, field merging and persistence are separate short helpers; Debug status has JSDoc types and
separate loading/rendering. This refactor leaves capture behavior and schema unchanged.

Run the entire suite with the project's local test DB configured:

```bash
uv run ruff check .
uv run python -m unittest discover -s tests
```

For Supabase, paste migrations 014 and 015 into its SQL editor in order after confirming migrations
001–013 are already applied. Do not run the destructive local integration-test harness against
Supabase; it rejects non-local hosts and rebuilds the test schema.
