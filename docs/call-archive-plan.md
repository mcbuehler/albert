# Calls archive and staff notes — design history

This document records the original design and the implementation completed through chunk 7 on 2026-11-05.
For the current schema and installation requirements, see the [archive contracts](call-archive-schema.md)
and [quickstart](../README.md#quickstart).

## Proposed scope

Add **Customers | Calls | Debug**. Calls combines upcoming callbacks, active attempts, and historical inbound/outbound calls. Historical records remain useful after Vapi removes its copies.

Confirmed by the user: archive **all calls, including unknown callers**, and use a **private Supabase Storage bucket** for audio. This archive is separate from Hindsight: non-customer conversations still must not enter Hindsight memory. The reported 14-day Vapi retention is treated as this account's deadline; Vapi documents configurable retention rather than a universal 14-day guarantee.

Confirmed capture approach: **cron only**, with a dedicated archive job every minute. A 1–2 minute delay before a completed call appears is acceptable. This is the normal discovery target, subject to provider processing and service availability; audio can remain pending longer. Do not add archive writes or downloads to the webhook. Existing webhook behavior for Hindsight stays independent.

Use PostgreSQL for metadata, transcripts, speaker messages, and staff notes; store audio files in private object storage and save their object keys in PostgreSQL. This keeps large audio files out of ordinary SQL queries and backups. It is still our own durable copy, independent of Vapi. Verify storage limits, backup/recovery coverage for objects as well as SQL, and expected call lengths before implementation.

## 1. Define the list and its lifecycle

Columns: **Number, Name, Call date, Status**, plus direction, duration, and a note-count indicator if space allows. Every displayed sortable column sorts across the full result set. Default order: newest call date first. Label dates with an explicit display timezone; save timestamps in UTC. Call date means actual start time for attempts and scheduled due time for upcoming callbacks.

Search by name or number, including formatted phone input and partial digits. Add a status filter and server-side pagination. Debounce search and discard stale responses. Use a deterministic secondary ID sort and an allowlist of sortable SQL columns.

Lifecycle labels: Scheduled, Calling, Completed, No answer, Voicemail, Failed, Cancelled. “Past calls” is a filter grouping historical outcomes, not the only outcome label. Preserve the original Vapi ended reason in the detail view. Archive availability is separate: Pending, Ready, Partially available, Retry needed, Unavailable.

Keep `scheduled_calls` and `call_attempts` as the scheduling source of truth. A new archive table supplements them; it must not change retries or busy checks. Inbound calls can exist without an outbound attempt.

Each actual dial is a historical row. A callback awaiting retry has a separate upcoming row at its next due time. While an attempt is active, suppress the duplicate scheduled row. Completed/cancelled schedule placeholders with actual attempts do not produce redundant historical rows; retain a cancelled placeholder when no dial occurred. Examples:

- 09:00 No answer; 10:00 Scheduled retry → two meaningful rows.
- 10:00 Calling → one active row for that attempt.
- Inbound conversation → one historical row without a scheduled callback.

## 2. Add archive and note records

Proposed `call_records` fields (one durable identity for an actual attempt and its later archive):

- Internal UUID, nullable unique Vapi call ID, nullable unique attempt link, nullable scheduled-callback link and customer link. A local attempt may have a record before Vapi accepts a call; inbound records need no attempt.
- Normalized remote number when available, name snapshot, direction, assistant and originating-number IDs. Customer links are nullable; unknown names display “Unknown caller,” and withheld/unusable numbers have an explicit fallback label.
- Created/start/end timestamps, duration, provider status, ended reason; cost/currency if supplied.
- Transcript text and speaker/timestamp messages. Explicitly allowlist customer/assistant conversation roles and spoken-text/timing fields at ingestion; exclude system prompts, tool calls/results, assistant tool arguments and injected memories. Do not copy the provider messages array wholesale. Retain selected useful provider metadata, not a full payload. Test with system-memory and tool-result fixtures.
- Audio object key, media type, byte count/checksum; separate transcript/audio availability states.
- Archive attempts, next retry, last error category, lease timestamp, archived timestamp.

Use explicit availability states to distinguish processing delay, an empty transcript, intentionally disabled recording, and an artifact that expired before capture. A later partial response must not erase a transcript or audio already saved.

Proposed `call_notes`: ID, target, author user ID, author label snapshot, message, created timestamp. Support either a durable call record or a scheduled callback using checked, mutually exclusive target links. Scheduled notes remain visible as a clearly labelled callback discussion on its attempts; notes written on a specific attempt remain attached to that attempt.

Create or resolve a durable call record before accepting a note on an attempt-only entry. Serialize matching by Vapi call ID and enforce unique attempt/provider links. If provider discovery precedes dispatcher recovery, merge the records transactionally, preserve/reparent notes, and retain an alias for the old record ID so open dialogs still resolve. Attempt links use `ON DELETE SET NULL`: existing recovery deletes undialled attempt rows, which must not destroy staff notes. Retained undialled records show “Not placed,” with no invented Vapi call or retry attempt. These identity rules must be covered by real-database concurrency tests before the UI is enabled.

First version: chronological, append-only messages; plain text, bounded length, no editing/deletion UI or threaded replies. Luce's message and Tom's response are separate entries. Derive author identity server-side from the verified staff login. Use verified email as fallback when a friendly name is unavailable. Notes are staff context, not automatically instructions for the assistant or Hindsight memory.

Enable RLS on new tables, matching the existing backend-only access model. Add indexes for customer/time, archive work, links, and search as needed for actual volume.

## 3. Capture promptly and archive reliably

1. A dedicated archive cron discovers inbound and outbound calls through Vapi's list API every minute and persists metadata/transcripts plus durable pending audio work. Archive capture must not depend on webhook delivery, the Hindsight key, or Hindsight success. Preserve existing customer-only Hindsight and voicemail rules independently.
2. Capture metadata for eligible failed/no-answer attempts too, even when no conversation exists. Artifact absence for such calls is expected.
3. A dedicated archive cron claims a small batch with leases, fetches Vapi details, downloads audio server-side, uploads it under a deterministic private object key, and marks completion only after successful upload. Keep it separate from the minute-by-minute dial dispatcher so long recordings cannot delay callbacks.
4. Bound runtime, download size, request timeouts, memory and temporary-disk use. Download/upload in a way suitable for long calls; verify maximum supported files against the deployed function limits. If measured call sizes exceed those limits, move the same durable jobs to a worker before launch.
5. Retry delayed artifacts and transient errors with backoff. Treat 404 shortly after a call as potentially not ready, rather than immediately lost. Repeated scans, overlapping jobs, and interrupted uploads must converge on one call record and one object.
6. Run resumable discovery over Vapi inbound and outbound calls with overlapping timestamp windows. Subdivide full result windows rather than assuming a fixed-limit response is complete. Vapi documents at most 1,000 results and no cursor/offset; if a minimum-width window is still saturated (for example, many calls sharing a timestamp), stop its checkpoint and alert rather than silently skipping records. Verify timestamp precision and a provider-supported fallback before claiming completeness at that volume. Persist progress only after records are durable; periodically revisit the remaining retention window to catch late updates and gaps. Give recent-call discovery its own bounded budget so old backfill or a slow audio download cannot starve it. Track discovered active calls until they end; do not advance a timestamp cursor in a way that loses calls that started before the scan window and finished afterward.
7. Expose last successful scan, archive backlog, oldest pending age, and failure count in Debug. Alert on sustained failures well before the account's retention deadline. Existing dispatcher health does not prove archival is working.

Use Vapi's authenticated recording endpoint, not a saved expiring recording URL. Restrict download origins/redirect handling and avoid forwarding provider credentials to storage hosts. Retain only our own object key for long-term playback.

Deployment prerequisites: add the archive path to `vercel.json` with `* * * * *`; configure duration against the `main.py` Flask entry point, verify plan concurrency/resource limits, and provision the private bucket and a backend-only storage credential. Separate routes have separate invocations, but Vercel packages Flask as one function: they still share deployment/resource constraints. Vercel does not automatically retry failed cron invocations; subsequent scheduled runs recover durable jobs after lease expiry. Confirm transfer size/runtime using a representative long call. Provide setup instructions and placeholders without setting the user's secrets.

## 4. Backfill before the retention window closes

Make a one-off, resumable import for all eligible calls still available in Vapi. Include inbound calls, callbacks, and Call now; do not rely only on existing outbound attempt IDs. Import older available calls first, deduplicate by Vapi ID, and preserve existing notes.

Include unknown callers and calls predating customer onboarding, as requested. Preserve any available recording/consent indicators and respect artifacts that were disabled or never created. Calls already expired cannot be reconstructed from Vapi. Existing attempt records can still appear with “Recording/transcript unavailable.”

Deliver archival and begin backfill before spending time polishing the new tab.

## 5. Build authenticated list and detail access

Proposed new routes, since existing routes do not expose this information:

- `GET /api/calls` — paginated combined list, search, sort, status filter.
- `GET /api/calls/<entry-id>` — metadata, available transcript/messages, notes and archive state.
- `POST /api/calls/<entry-id>/notes` — append staff message.
- `POST /api/calls/<entry-id>/audio-url` — fresh short-lived playback URL after staff authorization.
- Archive cron route protected by cron/staff auth.

Define stable typed entry IDs for scheduled and durable call records, resolving legacy attempt-only IDs to durable records before notes are accepted. All staff routes use existing Supabase staff auth. Validate note ownership targets server-side and render text safely. Never send Vapi or storage administration credentials to the browser. Private Supabase signed URLs allow browser playback/seeking without routing audio through Flask. The user approved short-lived links with renewal and their revocation limitation; initial planned expiry is five minutes, subject to browser testing. Renewal preserves playback position. Issued links can remain valid until expiry even after application access is blocked. Immediate revocation would require a different delivery architecture.

## 6. Build the Calls tab and modal

Reuse plain ES modules, existing tabs, table styling and native dialogs. Load list metadata only; fetch transcript and notes on opening a call. Fetch a playback URL only when audio is available.

Modal: name/number, date, direction, duration, outcome; native audio controls; scrollable speaker-labelled transcript; chronological staff messages and composer. Upcoming calls show their schedule and notes without audio/transcript. Offer explicit loading, missing, failed and empty states. Notes remain usable when audio is unavailable.

Support keyboard selection, sortable headers with `aria-sort`, focus return, Escape/close, and responsive mobile layout. Stop audio and clear loaded call content on logout; pause playback when closing the dialog. Refresh active/upcoming rows while the view is visible without replacing an open draft note.

## 7. Define deletion and retention before launch

Recommended: Delete customer removes archived recordings, transcripts, notes and metadata as well as existing records. Erase memory continues to mean Hindsight memory only, and its dialog explicitly says archived recordings/transcripts remain. Confirm the archive retention period; do not silently introduce automatic expiry.

Object deletion and SQL deletion cannot be one transaction. Use durable cleanup jobs and a deletion marker that prevents reconciliation/backfill from recreating deleted call content. Deny access immediately while cleanup is pending; retry object deletion until complete. This also requires handling an archive upload already in flight when deletion begins.

“Deny access immediately” means deny detail requests and new playback URLs; previously issued links have the expiry/caching limitation above. Use short cache settings and verify actual deletion/CDN behavior.

Customer deletion must match both customer links and normalized historical phone identity up to a deletion cutoff, including pre-onboarding or not-yet-discovered calls. Tombstones retain the minimum matching identifiers/cutoff and known provider IDs; future calls after the cutoff are eligible even if the same number is onboarded again. Add a staff deletion action for an individual archived call, including unknown callers, and prevent that Vapi ID being reimported. Resolve ambiguous/shared-number history explicitly rather than silently treating a customer foreign key as complete coverage.

## Reviewable implementation chunks

1. With all-call scope and Supabase Storage confirmed, settle lifecycle rows, note behavior, retention/deletion rules and friendly author names; add schema and data contracts.
2. Add cron metadata discovery, stable identity matching, checkpoints and scan status reporting.
3. Add audio transfer, artifact retries, runtime bounds and lease recovery. **Done 2026-11-04:** stereo recording transfer (Vapi redirect, bounded download, Storage upsert), leased audio jobs with backoff, 24 h give-up, 10-minute empty-transcript finalization, run budget.
4. Add deletion coordination/tombstones and resumable reconciliation/backfill; enable production archival after these protections are complete and verify one real audio/transcript round trip. **4a done 2026-11-04:** customer deletion purges archive + markers, recording cleanup worker, dialog texts. **4b done 2026-11-05:** one `reconcile` stream sweeps the 14-day retention oldest first (first sweep = backfill). **4c done 2026-11-05 (code):** flag on, vercel.json cron, `SUPABASE_SECRET_KEY` required; real round trip pending on the user's side. Single-call deletion moves to the Calls tab API (chunk 5/6).
5. Add staff list API and Calls tab with pagination, search, sort and statuses. **5a done 2026-11-05:** `GET /api/calls` (see [contracts](call-archive-schema.md#calls-list-chunk-5a)); Debug "Run archive now". **5b done 2026-11-05:** Calls tab (`calls.js`).
6. Add detail modal, transcript and private audio playback. **6a done 2026-11-05:** `GET /api/calls/<entry-id>`, `POST /api/calls/<entry-id>/audio-url` (5-minute Supabase signed links). **6b done 2026-11-05:** detail dialog (`call-detail.js`) with renewing audio player, transcript and read-only notes.
7. Add note API and conversation UI; complete end-to-end acceptance and documentation. **Done 2026-11-05 (code):** `POST /api/calls/<entry-id>/notes`, note box in the dialog, author = Supabase profile name or email. Open: end-to-end acceptance on the deployed site, single-call deletion, archive retention.

Stop for review after each implemented chunk, per project preference. Implementation updates README, architecture, handoff and memory. Provide setup/migration commands and real-service checks for the user; do not set their secrets or run against their services here.

Validation: local database tests for joins/lifecycle deduplication/search/sort/notes; mocked archive tests for repeated/out-of-order provider responses, missing artifacts, webhook/Hindsight outages, worker crashes, retries and full result windows; auth/route-contract coverage; deletion versus in-flight archival/reimport; manual browser checks for seeking, expired playback URLs, concurrent notes, mobile and logout. Acceptance includes a locally archived call that remains playable and readable without Vapi access, and normal completed-call discovery within 1–2 minutes without webhook delivery.

## Sources checked

- [Vapi call recording and artifacts](https://docs.vapi.ai/assistants/call-recording): recording/transcript settings and configurable retention.
- [Vapi authenticated artifact retrieval](https://docs.vapi.ai/assistants/retrieve-call-artifacts): recording endpoints return short-lived signed redirects.
- [Vapi list calls](https://docs.vapi.ai/api-reference/calls/list): creation/update timestamp filters and bounded result limits.
- [Vapi data flow](https://docs.vapi.ai/security-and-privacy/data-flow): custom storage is another option, including Supabase; assess direct delivery separately if desired.
- [Supabase private storage](https://supabase.com/docs/guides/storage/buckets/fundamentals) and [serving downloads](https://supabase.com/docs/guides/storage/serving/downloads): private objects and signed playback URLs.
- [Vercel Flask deployment](https://vercel.com/docs/frameworks/backend/flask) and [cron management](https://vercel.com/docs/cron-jobs/manage-cron-jobs): function packaging, duration and cron failure behavior.

## Plan review

The 2026-11-02 review refined call and note identity, transcript filtering, deletion and reimport handling, signed-link expiry, saturated discovery-window detection, and deployment prerequisites. The review also identified real-service checks for provider payloads and timestamp precision, account retention, recording sizes, upload and runtime limits, and browser playback and link renewal.
