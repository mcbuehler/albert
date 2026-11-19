"""albert/db.py against a real, local throwaway Postgres (env TEST_DATABASE_URL; never Supabase).

The schema is rebuilt from migrations/ once per run and every table is emptied before each test.
Skipped when TEST_DATABASE_URL isn't set (e.g. in CI without a Postgres service).
"""

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from albert import archive, config, db

TEST_URL = os.environ.get("TEST_DATABASE_URL")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
ARCHIVE_TABLES = ("call_records", "call_record_aliases", "call_notes", "call_archive_scans", "call_archive_deletions", "call_audio_cleanup")
TABLES = "customers, scheduled_calls, call_attempts, team_notifications, dispatcher_status, " + ", ".join(ARCHIVE_TABLES)

PHONE = "+41791234567"
OTHER_PHONE = "+12125551234"
# Calling-hours windows for claim_due_call: retries always / never allowed, whatever the time of day.
ALWAYS = ("00:00", "24:00")
NEVER = ("00:00", "00:00")
STALE = config.STALE_CALL_MINUTES


def setUpModule():
    if not TEST_URL:
        raise unittest.SkipTest("TEST_DATABASE_URL is not set")
    # The tests drop the schema and truncate tables: only ever against a local database.
    if urlsplit(TEST_URL).hostname not in ("localhost", "127.0.0.1"):
        raise RuntimeError(f"TEST_DATABASE_URL must point at localhost, not {urlsplit(TEST_URL).hostname}")
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(migration.read_text())


def sql(query, params=()):
    """Run SQL on a separate connection (committed); returns the rows, if any."""
    with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def add_customer(phone=PHONE, first_name="Anna", last_name="Muster", source="staff", needs_review=False,
                 vapi_phone_number_id="phone-anna"):
    return db.insert_customer(first_name, None, last_name, phone, "Europe/Zurich", vapi_phone_number_id,
                              "assistant-anna", source=source, needs_review=needs_review)


def add_callback(customer_id, minutes=0, status="pending", instructions=None,
                 vapi_phone_number_id=None, vapi_assistant_id=None):
    """A callback due `minutes` from now (negative: overdue)."""
    return sql(
        "INSERT INTO scheduled_calls (customer_id, local_time, due_at, status, instructions, "
        "  vapi_phone_number_id, vapi_assistant_id) "
        "VALUES (%s, now()::timestamp, now() + make_interval(mins => %s), %s, %s, %s, %s) RETURNING id",
        (customer_id, minutes, status, instructions, vapi_phone_number_id, vapi_assistant_id),
    )[0]["id"]


def callback(callback_id):
    return sql("SELECT status, due_at, due_at - now() AS due_in FROM scheduled_calls WHERE id = %s",
               (callback_id,))[0]


def attempt(attempt_id):
    return sql("SELECT * FROM call_attempts WHERE id = %s", (attempt_id,))[0]


def add_attempt(customer_id, scheduled_call_id=None, attempt_no=1, ended=False, minutes_ago=0,
                vapi_call_id=None, vapi_phone_number_id="phone-anna", customer_phone=None):
    return sql(
        "INSERT INTO call_attempts (customer_id, customer_phone, scheduled_call_id, attempt_no, vapi_call_id, "
        "  vapi_phone_number_id, started_at, ended_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now() - make_interval(mins => %s), "
        "  CASE WHEN %s THEN now() END) RETURNING id",
        (customer_id, customer_phone, scheduled_call_id, attempt_no, vapi_call_id,
         vapi_phone_number_id, minutes_ago, ended),
    )[0]["id"]


class DbTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"DATABASE_URL": TEST_URL})
        patcher.start()
        self.addCleanup(patcher.stop)
        sql(f"TRUNCATE {TABLES} CASCADE")

    def assertRetryDue(self, callback_id, minutes):
        """Pending again, due in `minutes` (whole minutes, like scheduled times)."""
        row = callback(callback_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual((row["due_at"].second, row["due_at"].microsecond), (0, 0))
        self.assertAlmostEqual(row["due_in"], timedelta(minutes=minutes), delta=timedelta(minutes=1))


class CallArchiveSchemaTests(DbTestCase):
    """Database contracts for the archive foundation; no capture worker is enabled yet."""

    def record(self, **fields: object) -> dict:
        """Insert a call-record fixture with optional field overrides."""
        fields = {"direction": "inbound", **fields}
        columns = ", ".join(fields)
        placeholders = ", ".join(["%s"] * len(fields))
        return sql(f"INSERT INTO call_records ({columns}) VALUES ({placeholders}) RETURNING *", tuple(fields.values()))[0]

    def note(self, **fields: object) -> dict:
        """Insert a staff-note fixture with optional field overrides."""
        fields = {"author_user_id": uuid.uuid4(), "author_label": "Luce", "body": "Talked about cooking", **fields}
        columns = ", ".join(fields)
        placeholders = ", ".join(["%s"] * len(fields))
        return sql(f"INSERT INTO call_notes ({columns}) VALUES ({placeholders}) RETURNING *", tuple(fields.values()))[0]

    def test_unknown_call_needs_no_customer_number_or_attempt(self) -> None:
        """Verify unknown call needs no customer number or attempt."""
        row = self.record(vapi_call_id="inbound-unknown")
        self.assertIsNone(row["customer_id"])
        self.assertIsNone(row["remote_phone"])
        self.assertIsNone(row["attempt_id"])
        self.assertEqual((row["audio_state"], row["transcript_state"], row["messages"]), ("pending", "pending", []))

    def test_vapi_and_attempt_links_are_unique_but_nullable(self) -> None:
        """Verify vapi and attempt links are unique but nullable."""
        attempt_id = add_attempt(add_customer()["id"])
        self.record(vapi_call_id="call-1", attempt_id=attempt_id)
        for fields in ({"vapi_call_id": "call-1"}, {"attempt_id": attempt_id}):
            with self.subTest(fields=fields), self.assertRaises(psycopg.errors.UniqueViolation):
                self.record(**fields)
        self.record()
        self.record()

    def test_attempt_deletion_preserves_record_and_its_note(self) -> None:
        """Verify attempt deletion preserves record and its note."""
        attempt_id = add_attempt(add_customer()["id"])
        record = self.record(attempt_id=attempt_id, direction="outbound")
        note = self.note(call_record_id=record["id"])
        self.assertTrue(db.delete_undialled_attempt(attempt_id))
        self.assertIsNone(sql("SELECT attempt_id FROM call_records WHERE id = %s", (record["id"],))[0]["attempt_id"])
        self.assertEqual(sql("SELECT call_record_id FROM call_notes WHERE id = %s", (note["id"],))[0]["call_record_id"], record["id"])

    def test_removed_attempt_and_callback_links_are_set_null(self) -> None:
        """Verify a record not owned by the deleted customer survives with its schedule/attempt links cleared."""
        customer_id = add_customer()["id"]
        scheduled_id = add_callback(customer_id)
        attempt_id = add_attempt(customer_id, scheduled_id, ended=True)
        record = self.record(scheduled_call_id=scheduled_id, attempt_id=attempt_id, remote_phone=OTHER_PHONE)
        self.assertEqual(db.delete_customer(customer_id), "deleted")
        row = sql("SELECT * FROM call_records WHERE id = %s", (record["id"],))[0]
        self.assertEqual((row["scheduled_call_id"], row["attempt_id"]), (None, None))
        self.assertEqual(row["remote_phone"], OTHER_PHONE)

    def test_invalid_metadata_rejected(self) -> None:
        """Verify invalid metadata rejected."""
        for fields in ({"direction": "sideways"}, {"remote_phone": "0791234567"}, {"duration_seconds": -1},
                       {"cost": -1}, {"archive_attempts": -1}, {"messages": '{}'}, {"vapi_call_id": " "},
                       {"started_at": datetime(2026, 11, 2, 12, tzinfo=timezone.utc),
                        "ended_at": datetime(2026, 11, 2, 11, tzinfo=timezone.utc)}):
            with self.subTest(fields=fields), self.assertRaises(psycopg.errors.CheckViolation):
                self.record(**fields)

    def test_artifact_states_distinguish_empty_disabled_and_unavailable(self) -> None:
        """Verify artifact states distinguish empty disabled and unavailable."""
        for state in ("pending", "empty", "disabled", "unavailable"):
            self.assertEqual(self.record(transcript_state=state)["transcript_state"], state)
        for state in ("pending", "disabled", "unavailable", "not_applicable"):
            self.assertEqual(self.record(audio_state=state)["audio_state"], state)
        for fields in ({"transcript_state": "ready"}, {"audio_state": "ready"}, {"audio_state": "lost"}):
            with self.subTest(fields=fields), self.assertRaises(psycopg.errors.CheckViolation):
                self.record(**fields)
        row = self.record(transcript_state="ready", transcript="Customer: Hello", audio_state="ready",
                          audio_object_key="calls/one.mp3", audio_media_type="audio/mpeg", audio_bytes=123,
                          audio_sha256="a" * 64)
        self.assertEqual((row["transcript_state"], row["audio_state"]), ("ready", "ready"))

    def test_archive_claim_requires_timestamp_and_token(self) -> None:
        """Verify archive claim requires timestamp and token."""
        for fields in ({"archive_claimed_at": datetime.now(timezone.utc)}, {"archive_claim_token": uuid.uuid4()}):
            with self.subTest(fields=fields), self.assertRaises(psycopg.errors.CheckViolation):
                self.record(**fields)
        self.record(archive_claimed_at=datetime.now(timezone.utc), archive_claim_token=uuid.uuid4())

    def test_note_has_exactly_one_existing_target(self) -> None:
        """Verify note has exactly one existing target."""
        record_id = self.record()["id"]
        scheduled_id = add_callback(add_customer()["id"])
        self.note(call_record_id=record_id)
        self.note(scheduled_call_id=scheduled_id)
        for fields in ({}, {"call_record_id": record_id, "scheduled_call_id": scheduled_id}):
            with self.subTest(fields=fields), self.assertRaises(psycopg.errors.CheckViolation):
                self.note(**fields)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.note(call_record_id=uuid.uuid4())

    def test_note_text_limits_and_separate_authors(self) -> None:
        """Verify note text limits and separate authors."""
        record_id = self.record()["id"]
        luce = self.note(call_record_id=record_id)
        tom = self.note(call_record_id=record_id, author_label="Tom", body="Added a custom prompt for next call")
        self.assertNotEqual(luce["author_user_id"], tom["author_user_id"])
        self.assertEqual(len(sql("SELECT * FROM call_notes WHERE call_record_id = %s", (record_id,))), 2)
        for fields in ({"body": " "}, {"body": "a" * 4001}, {"author_label": " "}, {"author_label": "a" * 321}):
            with self.subTest(fields=fields), self.assertRaises(psycopg.errors.CheckViolation):
                self.note(call_record_id=record_id, **fields)

    def test_record_deletion_cascades_notes_and_aliases_but_not_cleanup(self) -> None:
        """Verify record deletion cascades notes and aliases but not cleanup."""
        record_id = self.record()["id"]
        self.note(call_record_id=record_id)
        sql("INSERT INTO call_record_aliases (alias_id, call_record_id) VALUES (%s, %s)", (uuid.uuid4(), record_id))
        sql("INSERT INTO call_audio_cleanup (object_key) VALUES ('calls/deleted.mp3')")
        sql("DELETE FROM call_records WHERE id = %s", (record_id,))
        self.assertEqual(sql("SELECT * FROM call_notes"), [])
        self.assertEqual(sql("SELECT * FROM call_record_aliases"), [])
        self.assertEqual(len(sql("SELECT * FROM call_audio_cleanup")), 1)

    def test_alias_cannot_reference_itself(self) -> None:
        """Verify alias cannot reference itself."""
        record_id = self.record()["id"]
        with self.assertRaises(psycopg.errors.CheckViolation):
            sql("INSERT INTO call_record_aliases (alias_id, call_record_id) VALUES (%s, %s)", (record_id, record_id))

    def test_scan_window_and_claim_contracts(self) -> None:
        """Verify scan window and claim contracts."""
        for query in (
            "INSERT INTO call_archive_scans (stream, window_start) VALUES ('recent', now())",
            "INSERT INTO call_archive_scans (stream, window_start, window_end) VALUES ('recent', now(), now())",
            "INSERT INTO call_archive_scans (stream, claimed_at) VALUES ('recent', now())",
            "INSERT INTO call_archive_scans (stream) VALUES ('typo')",
        ):
            with self.subTest(query=query), self.assertRaises(psycopg.errors.CheckViolation):
                sql(query)
        for stream in ("recent", "backfill", "reconcile"):
            sql("INSERT INTO call_archive_scans (stream) VALUES (%s)", (stream,))
        self.assertEqual(len(sql("SELECT * FROM call_archive_scans")), 3)

    def test_deletion_markers_survive_customer_deletion(self) -> None:
        """Verify existing deletion markers survive customer deletion, which adds its own phone cutoff."""
        customer_id = add_customer()["id"]
        sql("INSERT INTO call_archive_deletions (remote_phone, cutoff_at) VALUES (%s, now())", (PHONE,))
        sql("INSERT INTO call_archive_deletions (vapi_call_id) VALUES ('unknown-call')")
        self.assertEqual(db.delete_customer(customer_id), "deleted")
        self.assertEqual(len(sql("SELECT * FROM call_archive_deletions")), 3)
        for query in (
            "INSERT INTO call_archive_deletions DEFAULT VALUES",
            "INSERT INTO call_archive_deletions (remote_phone) VALUES ('+41791234567')",
            "INSERT INTO call_archive_deletions (vapi_call_id, remote_phone, cutoff_at) VALUES ('both', '+41791234567', now())",
        ):
            with self.subTest(query=query), self.assertRaises(psycopg.errors.CheckViolation):
                sql(query)

    def test_cleanup_claim_requires_timestamp_and_token(self) -> None:
        """Verify cleanup claim requires timestamp and token."""
        with self.assertRaises(psycopg.errors.CheckViolation):
            sql("INSERT INTO call_audio_cleanup (object_key, claimed_at) VALUES ('calls/one.mp3', now())")
        sql("INSERT INTO call_audio_cleanup (object_key, claimed_at, claim_token) VALUES ('calls/one.mp3', now(), %s)",
            (uuid.uuid4(),))

    def test_all_archive_tables_have_rls_without_policies(self) -> None:
        """Verify all archive tables have rls without policies."""
        rows = sql("SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s)",
                   (list(ARCHIVE_TABLES),))
        self.assertEqual(len(rows), len(ARCHIVE_TABLES))
        self.assertTrue(all(row["relrowsecurity"] for row in rows))
        self.assertEqual(sql("SELECT * FROM pg_policies WHERE schemaname = 'public' AND tablename = ANY(%s)",
                             (list(ARCHIVE_TABLES),)), [])

    def test_migration_can_be_rerun_without_losing_data(self) -> None:
        """Verify migration can be rerun without losing data."""
        record_id = self.record(vapi_call_id="retained")["id"]
        sql((MIGRATIONS / "014_call_archive.sql").read_text())
        self.assertEqual(sql("SELECT id FROM call_records WHERE vapi_call_id = 'retained'")[0]["id"], record_id)


class ArchiveDiscoveryDbTests(DbTestCase):
    def data(self, **values: object) -> dict:
        """Build validated archive fields from a provider response fixture."""
        return archive.call_data({"id": "provider", "createdAt": "2026-11-02T12:00:00Z", "updatedAt": "2026-11-02T12:01:00Z",
                                  "type": "inboundPhoneCall", "status": "ended", **values})

    def test_repeated_discovery_updates_one_record_with_customer_snapshot(self) -> None:
        """Verify repeated discovery updates one record with customer snapshot."""
        customer = add_customer()
        data = self.data(customer={"number": PHONE})
        first = db.save_archive_call(data)
        self.assertEqual(db.save_archive_call(data), first)
        row = sql("SELECT * FROM call_records")[0]
        self.assertEqual((row["customer_id"], row["name_snapshot"]), (customer["id"], "Anna Muster"))

    def test_window_is_saved_over_one_connection_with_markers_honoured(self) -> None:
        """Verify a batch saves each call, skips a blocked one and keeps earlier saves when a later one fails."""
        sql("INSERT INTO call_archive_deletions (vapi_call_id) VALUES ('blocked')")
        self.assertEqual(db.save_archive_calls([self.data(id="one"), self.data(id="blocked"), self.data(id="two")]), 2)
        with self.assertRaises(psycopg.Error):
            db.save_archive_calls([self.data(id="three"), {**self.data(id="bad"), "direction": "sideways"}])
        self.assertEqual({row["vapi_call_id"] for row in sql("SELECT vapi_call_id FROM call_records")}, {"one", "two", "three"})

    def test_transcript_survives_partial_and_older_responses(self) -> None:
        """Verify transcript survives partial and older responses."""
        data = self.data(messages=[{"role": "user", "message": "Hello"}, {"role": "bot", "message": "Hi"}])
        db.save_archive_call(data)
        db.save_archive_call(self.data(updatedAt="2026-11-02T12:02:00Z", messages=[{"role": "user", "message": "Hello"}]))
        db.save_archive_call(self.data(updatedAt="2026-11-02T12:00:00Z", status="in-progress"))
        row = sql("SELECT * FROM call_records")[0]
        self.assertEqual((row["transcript"], row["provider_status"]), ("Customer: Hello\nAssistant: Hi", "ended"))

    def test_audio_ready_is_preserved_by_metadata_refresh(self) -> None:
        """Verify audio ready is preserved by metadata refresh."""
        record = db.save_archive_call(self.data())
        sql("UPDATE call_records SET audio_state = 'ready', audio_object_key = 'one.mp3', "
            "audio_bytes = 10, audio_media_type = 'audio/mpeg' WHERE id = %s", (record,))
        db.save_archive_call(self.data())
        row = sql("SELECT * FROM call_records")[0]
        self.assertEqual((row["audio_state"], row["audio_object_key"]), ("ready", "one.mp3"))

    def test_attempt_identity_and_alias_merge_preserve_both_note_threads(self) -> None:
        """Verify attempt identity and alias merge preserve both note threads."""
        customer_id = add_customer()["id"]
        attempt_id = add_attempt(customer_id)
        local = db.ensure_attempt_call_record(attempt_id)
        self.assertEqual(db.ensure_attempt_call_record(attempt_id), local)
        provider = db.save_archive_call(self.data(type="outboundPhoneCall", customer={"number": PHONE}))
        self.assertNotEqual(provider, local)
        for record, label in ((local, "Luce"), (provider, "Tom")):
            sql("INSERT INTO call_notes (call_record_id, author_user_id, author_label, body) VALUES (%s, %s, %s, 'message')",
                (record, uuid.uuid4(), label))
        old_alias = uuid.uuid4()
        sql("INSERT INTO call_record_aliases (alias_id, call_record_id) VALUES (%s, %s)", (old_alias, local))
        db.set_attempt_call_id(attempt_id, "provider")
        self.assertEqual(db.save_archive_call(self.data(type="outboundPhoneCall", customer={"number": PHONE})), provider)
        self.assertEqual(len(sql("SELECT * FROM call_records")), 1)
        self.assertEqual({row["call_record_id"] for row in sql("SELECT * FROM call_notes")}, {provider})
        aliases = sql("SELECT * FROM call_record_aliases")
        self.assertEqual({row["alias_id"] for row in aliases}, {local, old_alias})
        self.assertEqual({row["call_record_id"] for row in aliases}, {provider})
        self.assertEqual(sql("SELECT attempt_id FROM call_records")[0]["attempt_id"], attempt_id)

    def test_merge_transfers_source_audio_and_schedules_redundant_object_cleanup(self) -> None:
        """Verify merge transfers source audio and schedules redundant object cleanup."""
        customer_id = add_customer()["id"]
        attempt_id = add_attempt(customer_id)
        local = db.ensure_attempt_call_record(attempt_id)
        provider = db.save_archive_call(self.data(type="outboundPhoneCall"))
        for record, key in ((local, "local.mp3"), (provider, "provider.mp3")):
            sql("UPDATE call_records SET audio_state = 'ready', audio_object_key = %s, audio_bytes = 10, "
                "audio_media_type = 'audio/mpeg' WHERE id = %s", (key, record))
        db.set_attempt_call_id(attempt_id, "provider")
        db.save_archive_call(self.data(type="outboundPhoneCall"))
        self.assertEqual(sql("SELECT audio_object_key FROM call_records")[0]["audio_object_key"], "provider.mp3")
        self.assertEqual(sql("SELECT object_key FROM call_audio_cleanup")[0]["object_key"], "local.mp3")

    def test_concurrent_discovery_and_local_identity_converge(self) -> None:
        """Verify concurrent discovery and local identity converge."""
        customer_id = add_customer()["id"]
        attempt_id = add_attempt(customer_id, vapi_call_id="provider")
        results, failures = [], []
        barrier = threading.Barrier(2)

        def work(local: bool) -> None:
            """Race one identity operation and retain any failure for the parent test."""
            try:
                barrier.wait(timeout=5)
                results.append(db.ensure_attempt_call_record(attempt_id) if local else db.save_archive_call(self.data()))
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=work, args=(local,)) for local in (True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(sql("SELECT * FROM call_records")), 1)

    def test_tombstones_suppress_ids_and_phone_history_but_allow_future_calls(self) -> None:
        """Verify tombstones suppress ids and phone history but allow future calls."""
        sql("INSERT INTO call_archive_deletions (vapi_call_id) VALUES ('blocked')")
        sql("INSERT INTO call_archive_deletions (remote_phone, cutoff_at) VALUES (%s, '2026-11-02T12:00:00Z')", (PHONE,))
        self.assertIsNone(db.save_archive_call(self.data(id="blocked")))
        self.assertIsNone(db.save_archive_call(self.data(customer={"number": PHONE})))
        self.assertIsNotNone(db.save_archive_call(self.data(createdAt="2026-11-02T12:00:01Z", customer={"number": PHONE})))

    def test_deleted_record_is_never_resurrected(self) -> None:
        """Verify deleted record is never resurrected."""
        record = db.save_archive_call(self.data())
        sql("UPDATE call_records SET deleted_at = now() WHERE id = %s", (record,))
        self.assertIsNone(db.save_archive_call(self.data()))

    def test_scan_resumes_subdivisions_and_fences_expired_worker(self) -> None:
        """Verify scan resumes subdivisions and fences expired worker."""
        now = datetime.now(timezone.utc)
        claim = db.claim_archive_scan(now)
        self.assertIsNone(db.claim_archive_scan(now))
        windows = claim["pending_windows"]
        split = [[windows[0][0], (now - timedelta(minutes=5)).isoformat()],
                 [(now - timedelta(minutes=5)).isoformat(), windows[0][1]]]
        self.assertTrue(db.save_archive_scan_progress(claim["claim_token"], split))
        self.assertTrue(db.finish_archive_scan(claim["claim_token"], None))
        resumed = db.claim_archive_scan(now + timedelta(seconds=1))
        self.assertEqual(resumed["pending_windows"], split)
        self.assertIsNone(resumed["completed_through"])
        takeover = db.claim_archive_scan(now + timedelta(minutes=3))
        self.assertIsNotNone(takeover)
        self.assertFalse(db.save_archive_scan_progress(resumed["claim_token"], []))
        self.assertFalse(db.finish_archive_scan(resumed["claim_token"], None))
        self.assertTrue(db.save_archive_scan_progress(takeover["claim_token"], []))
        self.assertTrue(db.finish_archive_scan(takeover["claim_token"], None))
        row = sql("SELECT * FROM call_archive_scans")[0]
        self.assertEqual(row["completed_through"], claim["window_end"])
        self.assertIsNone(row["window_start"])
        self.assertIsNotNone(row["last_succeeded_at"])

    def test_failed_scan_preserves_checkpoint_and_pending_work(self) -> None:
        """Verify failed scan preserves checkpoint and pending work."""
        now = datetime.now(timezone.utc)
        claim = db.claim_archive_scan(now)
        db.finish_archive_scan(claim["claim_token"], "provider_read_failed")
        status = db.get_archive_status()
        self.assertEqual(status["scan"]["consecutive_failures"], 1)
        self.assertEqual(status["scan"]["pending_windows"], 1)
        self.assertIsNone(status["scan"]["completed_through"])
        resumed = db.claim_archive_scan(now + timedelta(seconds=1))
        self.assertEqual(resumed["pending_windows"], claim["pending_windows"])

    def test_refresh_rotates_failed_calls_and_retains_calls_that_end_later(self) -> None:
        """Verify refresh rotates failed calls and retains calls that end later."""
        record = db.save_archive_call(self.data(status="in-progress"))
        sql("UPDATE call_records SET last_polled_at = now() - interval '5 minutes' WHERE id = %s", (record,))
        self.assertEqual(db.archive_calls_to_refresh(5), ["provider"])
        db.mark_archive_poll_failed("provider")
        self.assertEqual(db.archive_calls_to_refresh(5), [])


class ArchiveAudioDbTests(DbTestCase):
    FIELDS = {"audio_object_key": "calls/x.wav", "audio_media_type": "audio/wav", "audio_bytes": 10, "audio_sha256": "a" * 64}

    def record(self, **fields: object) -> uuid.UUID:
        """Insert an ended provider call whose recording is due."""
        fields = {"direction": "inbound", "vapi_call_id": "call-1", "provider_status": "ended", "remote_phone": PHONE,
                  "provider_created_at": datetime.now(timezone.utc), "ended_at": datetime.now(timezone.utc), **fields}
        columns, placeholders = ", ".join(fields), ", ".join(["%s"] * len(fields))
        return sql(f"INSERT INTO call_records ({columns}) VALUES ({placeholders}) RETURNING id", tuple(fields.values()))[0]["id"]

    def row(self, record_id: uuid.UUID) -> dict:
        """Read one call record."""
        return sql("SELECT * FROM call_records WHERE id = %s", (record_id,))[0]

    def test_claims_only_due_ended_calls_once(self) -> None:
        """Verify active, future, finished and already claimed calls are skipped."""
        self.record(vapi_call_id="active", provider_status="in-progress")
        self.record(vapi_call_id="later", next_archive_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        self.record(vapi_call_id="done", audio_state="unavailable")
        due = self.record()
        job = db.claim_archive_audio()
        self.assertEqual((job["id"], job["vapi_call_id"]), (due, "call-1"))
        self.assertIsNone(db.claim_archive_audio())

    def test_expired_claim_is_taken_over_and_stale_owner_loses(self) -> None:
        """Verify a crashed run's claim is reclaimed and its late finish discards nothing in use."""
        record_id = self.record()
        stale = db.claim_archive_audio()
        sql("UPDATE call_records SET archive_claimed_at = now() - make_interval(mins => %s)", (config.ARCHIVE_AUDIO_CLAIM_MINUTES + 1,))
        fresh = db.claim_archive_audio()
        self.assertNotEqual(fresh["archive_claim_token"], stale["archive_claim_token"])
        self.assertTrue(db.finish_archive_audio(record_id, fresh["archive_claim_token"], self.FIELDS))
        self.assertFalse(db.finish_archive_audio(record_id, stale["archive_claim_token"], self.FIELDS))
        self.assertFalse(db.retry_archive_audio(record_id, stale["archive_claim_token"], "late"))
        row = self.row(record_id)
        self.assertEqual((row["audio_state"], row["audio_bytes"], row["archive_claim_token"]), ("ready", 10, None))
        self.assertIsNotNone(row["archived_at"])
        self.assertEqual(sql("SELECT * FROM call_audio_cleanup"), [])

    def test_unreferenced_or_suppressed_upload_is_queued_for_cleanup(self) -> None:
        """Verify an upload for a merged-away or deletion-marked call is never shown."""
        record_id = self.record()
        job = db.claim_archive_audio()
        sql("INSERT INTO call_archive_deletions (vapi_call_id) VALUES ('call-1')")
        self.assertFalse(db.finish_archive_audio(record_id, job["archive_claim_token"], self.FIELDS))
        self.assertEqual(self.row(record_id)["audio_state"], "unavailable")
        self.assertIsNone(db.claim_archive_audio())
        self.assertFalse(db.finish_archive_audio(uuid.uuid4(), uuid.uuid4(), {**self.FIELDS, "audio_object_key": "calls/gone.wav"}))
        self.assertEqual({row["object_key"] for row in sql("SELECT * FROM call_audio_cleanup")}, {"calls/x.wav", "calls/gone.wav"})

    def test_retry_backs_off_and_gives_up(self) -> None:
        """Verify exponential backoff, the age limit and final failures."""
        record_id = self.record()
        sql("UPDATE call_records SET archive_attempts = 3 WHERE id = %s", (record_id,))
        self.assertTrue(db.retry_archive_audio(record_id, db.claim_archive_audio()["archive_claim_token"], "recording_not_ready"))
        row = sql("SELECT *, next_archive_at - now() AS wait FROM call_records")[0]
        self.assertEqual((row["audio_state"], row["archive_attempts"], row["last_error_code"]), ("pending", 4, "recording_not_ready"))
        self.assertAlmostEqual(row["wait"], timedelta(minutes=8), delta=timedelta(seconds=30))
        sql("UPDATE call_records SET next_archive_at = now(), created_at = now() - make_interval(hours => %s)",
            (config.ARCHIVE_AUDIO_GIVE_UP_HOURS + 1,))
        db.retry_archive_audio(record_id, db.claim_archive_audio()["archive_claim_token"], "recording_not_ready")
        self.assertEqual(self.row(record_id)["audio_state"], "unavailable")
        large = self.record(vapi_call_id="large")
        db.retry_archive_audio(large, db.claim_archive_audio()["archive_claim_token"], "audio_too_large", final=True)
        self.assertEqual(self.row(large)["audio_state"], "unavailable")

    def test_backoff_is_capped(self) -> None:
        """Verify retries wait at most the configured maximum."""
        record_id = self.record(archive_attempts=50)
        db.retry_archive_audio(record_id, db.claim_archive_audio()["archive_claim_token"], "storage_write_failed")
        wait = sql("SELECT next_archive_at - now() AS wait FROM call_records")[0]["wait"]
        self.assertAlmostEqual(wait, timedelta(minutes=config.ARCHIVE_AUDIO_RETRY_MAX_MINUTES), delta=timedelta(seconds=30))

    def test_transcripts_are_finalized_after_the_wait(self) -> None:
        """Verify only ended calls past the wait become empty, and a later scan can't requeue them."""
        old = datetime.now(timezone.utc) - timedelta(minutes=config.ARCHIVE_TRANSCRIPT_WAIT_MINUTES + 1)
        stale = self.record(vapi_call_id="provider", ended_at=old)
        self.record(vapi_call_id="recent")
        self.record(vapi_call_id="active", provider_status="in-progress", ended_at=None)
        self.assertEqual(db.finalize_archive_transcripts(), 1)
        self.assertEqual(self.row(stale)["transcript_state"], "empty")
        db.save_archive_call(archive.call_data({"id": "provider", "createdAt": "2026-11-02T12:00:00Z", "status": "ended"}))
        self.assertEqual(self.row(stale)["transcript_state"], "empty")

    def test_status_counts_retrying_and_unavailable_audio(self) -> None:
        """Verify Debug counts for audio retries and give-ups."""
        self.record(vapi_call_id="retrying", archive_attempts=2)
        self.record(vapi_call_id="lost", audio_state="unavailable")
        status = db.get_archive_status()
        self.assertEqual((status["pending_audio"], status["retrying_audio"], status["unavailable_audio"]), (1, 1, 1))


class ArchiveDeletionDbTests(DbTestCase):
    def data(self, **values: object) -> dict:
        """Build validated archive fields from a provider response fixture."""
        return archive.call_data({"id": "provider", "createdAt": "2026-11-02T12:00:00Z", "type": "inboundPhoneCall",
                                  "status": "ended", "customer": {"number": PHONE}, **values})

    def test_customer_deletion_purges_archive_and_blocks_reimport(self) -> None:
        """Verify linked and unlinked calls, notes and recordings go, and markers stop rediscovery."""
        customer = add_customer()
        linked = db.save_archive_call(self.data())
        sql("UPDATE call_records SET audio_state = 'ready', audio_object_key = 'calls/a.wav', audio_media_type = 'audio/wav', "
            "audio_bytes = 1 WHERE id = %s", (linked,))
        sql("INSERT INTO call_records (direction, remote_phone, vapi_call_id) VALUES ('inbound', %s, 'unlinked')", (PHONE,))
        sql("INSERT INTO call_notes (call_record_id, author_user_id, author_label, body) VALUES (%s, %s, 'Luce', 'Hi')",
            (linked, uuid.uuid4()))
        other = db.save_archive_call(self.data(id="other", customer={"number": OTHER_PHONE}))
        add_attempt(customer["id"], ended=True, vapi_call_id="attempt-call")
        self.assertEqual(db.delete_customer(customer["id"]), "deleted")
        self.assertEqual([row["id"] for row in sql("SELECT id FROM call_records")], [other])
        self.assertEqual(sql("SELECT count(*) FROM call_notes")[0]["count"], 0)
        self.assertEqual([row["object_key"] for row in sql("SELECT object_key FROM call_audio_cleanup")], ["calls/a.wav"])
        markers = sql("SELECT vapi_call_id, remote_phone FROM call_archive_deletions")
        self.assertEqual({row["vapi_call_id"] for row in markers} - {None}, {"provider", "unlinked", "attempt-call"})
        self.assertIn(PHONE, {row["remote_phone"] for row in markers})
        self.assertIsNone(db.save_archive_call(self.data()))
        self.assertIsNone(db.save_archive_call(self.data(id="unseen")))
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        self.assertIsNotNone(db.save_archive_call(self.data(id="after", createdAt=later)))

    def test_open_call_keeps_archive(self) -> None:
        """Verify a refused deletion leaves archive rows and writes no markers."""
        customer = add_customer()
        db.save_archive_call(self.data())
        add_attempt(customer["id"], add_callback(customer["id"]))
        self.assertEqual(db.delete_customer(customer["id"]), "calling")
        self.assertEqual(sql("SELECT count(*) FROM call_records")[0]["count"], 1)
        self.assertEqual(sql("SELECT count(*) FROM call_archive_deletions")[0]["count"], 0)

    def test_upload_finishing_after_deletion_is_queued_for_cleanup(self) -> None:
        """Verify an in-flight recording of a deleted customer's call never becomes visible."""
        customer = add_customer()
        record_id = db.save_archive_call(self.data())
        job = db.claim_archive_audio()
        db.delete_customer(customer["id"])
        fields = {"audio_object_key": f"calls/{record_id}.wav", "audio_media_type": "audio/wav", "audio_bytes": 1, "audio_sha256": None}
        self.assertFalse(db.finish_archive_audio(record_id, job["archive_claim_token"], fields))
        self.assertEqual(sql("SELECT object_key FROM call_audio_cleanup")[0]["object_key"], f"calls/{record_id}.wav")

    def test_cleanup_jobs_lease_retry_and_skip_used_keys(self) -> None:
        """Verify cleanup claims once, backs off, finishes, and drops keys still in use."""
        sql("INSERT INTO call_audio_cleanup (object_key) VALUES ('calls/gone.wav'), ('calls/used.wav')")
        sql("INSERT INTO call_records (direction, audio_state, audio_object_key, audio_media_type, audio_bytes) "
            "VALUES ('inbound', 'ready', 'calls/used.wav', 'audio/wav', 1)")
        job = db.claim_audio_cleanup()
        self.assertEqual(job["object_key"], "calls/gone.wav")
        self.assertIsNone(db.claim_audio_cleanup())
        self.assertEqual([row["object_key"] for row in sql("SELECT object_key FROM call_audio_cleanup")], ["calls/gone.wav"])
        self.assertTrue(db.retry_audio_cleanup("calls/gone.wav", job["claim_token"], "storage_delete_failed"))
        self.assertIsNone(db.claim_audio_cleanup())
        sql("UPDATE call_audio_cleanup SET next_attempt_at = now()")
        retry = db.claim_audio_cleanup()
        self.assertFalse(db.finish_audio_cleanup("calls/gone.wav", job["claim_token"]))
        self.assertTrue(db.finish_audio_cleanup("calls/gone.wav", retry["claim_token"]))
        self.assertEqual(sql("SELECT count(*) FROM call_audio_cleanup")[0]["count"], 0)
        self.assertEqual(db.get_archive_status()["pending_cleanup"], 0)


class ArchiveReconcileDbTests(DbTestCase):
    def test_sweep_runs_oldest_first_and_restarts(self) -> None:
        """Verify the reconcile stream walks the retention period in windows and wraps near recent scans."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        oldest = now - timedelta(days=config.ARCHIVE_PROVIDER_RETENTION_DAYS)
        span = timedelta(hours=config.ARCHIVE_RECONCILE_WINDOW_HOURS)
        overlap = timedelta(minutes=config.ARCHIVE_SCAN_OVERLAP_MINUTES)
        first = db.claim_archive_scan(now, "reconcile")
        self.assertEqual((first["window_start"], first["window_end"]), (oldest, oldest + span))
        self.assertIsNone(db.claim_archive_scan(now, "reconcile"))
        self.assertIsNotNone(db.claim_archive_scan(now, "recent"))
        self.assertTrue(db.save_archive_scan_progress(first["claim_token"], []))
        self.assertTrue(db.finish_archive_scan(first["claim_token"], None))
        second = db.claim_archive_scan(now, "reconcile")
        self.assertEqual((second["window_start"], second["window_end"]), (oldest + span - overlap, oldest + 2 * span - overlap))
        db.save_archive_scan_progress(second["claim_token"], [])
        db.finish_archive_scan(second["claim_token"], None)
        sql("UPDATE call_archive_scans SET completed_through = %s WHERE stream = 'reconcile'", (now - timedelta(minutes=5),))
        wrapped = db.claim_archive_scan(now, "reconcile")
        self.assertEqual(wrapped["window_start"], oldest)
        db.finish_archive_scan(wrapped["claim_token"], "provider_read_failed")
        status = db.get_archive_status()
        self.assertEqual(status["reconcile"]["consecutive_failures"], 1)
        self.assertIsNone(status["scan"]["last_succeeded_at"])

    def test_sweep_end_stays_behind_recent_scans(self) -> None:
        """Verify the last window ends where recent discovery's initial lookback begins."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        newest = now - timedelta(minutes=config.ARCHIVE_INITIAL_LOOKBACK_MINUTES)
        sql("INSERT INTO call_archive_scans (stream, completed_through) VALUES ('reconcile', %s)", (newest - timedelta(hours=1),))
        claim = db.claim_archive_scan(now, "reconcile")
        self.assertEqual(claim["window_end"], newest)


def add_record(minutes_ago: int = 0, **fields: object) -> str:
    """Insert an archived call started `minutes_ago` minutes ago; returns its typed Calls list ID."""
    fields = {"direction": "inbound", **fields}
    columns = ", ".join(fields)
    row = sql(f"INSERT INTO call_records (started_at, {columns}) VALUES (now() - make_interval(mins => %s), "
              f"{', '.join(['%s'] * len(fields))}) RETURNING id", (minutes_ago, *fields.values()))[0]
    return f"call:{row['id']}"


def list_calls(search: str = "", statuses: tuple = (), sort: str = "call_date", descending: bool = True,
               limit: int = 50, offset: int = 0) -> dict:
    """Call db.list_calls with the route's defaults."""
    return db.list_calls(search, list(statuses), sort, descending, limit, offset)


class CallsListDbTests(DbTestCase):
    def test_lifecycle_rows_are_not_duplicated(self) -> None:
        """Verify a retried callback shows its archived attempt and the upcoming retry, nothing twice."""
        customer_id = add_customer()["id"]
        retry = add_callback(customer_id, minutes=60)
        tried = add_attempt(customer_id, retry, ended=True, minutes_ago=60, vapi_call_id="vapi-1")
        record = add_record(60, vapi_call_id="vapi-1", attempt_id=tried, customer_id=customer_id, direction="outbound",
                            ended_reason="customer-did-not-answer", provider_status="ended")
        open_attempt = add_attempt(customer_id, add_callback(customer_id, status="calling"), minutes_ago=1)
        never_dialled = add_callback(customer_id, minutes=-30, status="cancelled")
        add_attempt(customer_id, add_callback(customer_id, minutes=-90, status="cancelled"), ended=True, minutes_ago=90)
        add_callback(customer_id, minutes=-120, status="completed")
        rows = {row["id"]: row["status"] for row in list_calls()["calls"]}
        self.assertEqual(rows[f"scheduled:{retry}"], "scheduled")
        self.assertEqual(rows[record], "no_answer")
        self.assertEqual(rows[f"attempt:{open_attempt}"], "calling")
        self.assertEqual(rows[f"scheduled:{never_dialled}"], "cancelled")
        self.assertEqual((len(rows), list(rows.values()).count("completed")), (5, 1))  # the cancelled callback's attempt

    def test_outcomes_follow_the_ended_reason(self) -> None:
        """Verify voicemail, failure, completion and live calls get their lifecycle status."""
        expected = {add_record(ended_reason="voicemail"): "voicemail", add_record(ended_reason="customer-busy"): "no_answer",
                    add_record(ended_reason="pipeline-error-openai-llm-failed"): "failed",
                    add_record(ended_reason="customer-ended-call"): "completed",
                    add_record(provider_status="ended"): "completed", add_record(provider_status="in-progress"): "calling"}
        self.assertEqual({row["id"]: row["status"] for row in list_calls()["calls"]}, expected)
        self.assertEqual(list_calls(statuses=("failed", "voicemail"))["total"], 2)

    def test_search_matches_names_and_number_digits(self) -> None:
        """Verify search covers customer names, snapshots and formatted partial numbers, with wildcards escaped."""
        customer_id = add_customer()["id"]
        anna = add_record(customer_id=customer_id, remote_phone=PHONE, name_snapshot="Old name")
        unknown = add_record(remote_phone=OTHER_PHONE, name_snapshot="Tom Caller")
        self.assertEqual([row["id"] for row in list_calls("muster")["calls"]], [anna])
        self.assertEqual([row["id"] for row in list_calls("tom")["calls"]], [unknown])
        self.assertEqual([row["id"] for row in list_calls("+1 (212) 555")["calls"]], [unknown])
        self.assertEqual(list_calls("%")["total"], 0)
        self.assertEqual(list_calls("Anna Muster")["calls"][0]["name"], "Anna Muster")

    def test_sorting_paging_and_deleted_records(self) -> None:
        """Verify stable sorted pages with a full total, unknown names last and deleted calls hidden."""
        ids = [add_record(minutes, name_snapshot=name) for minutes, name in ((3, "Bea"), (2, None), (1, "Al"))]
        add_record(0, name_snapshot="Gone", deleted_at=datetime.now(timezone.utc))
        page = list_calls(limit=2)
        self.assertEqual(([row["id"] for row in page["calls"]], page["total"]), ([ids[2], ids[1]], 3))
        self.assertEqual([row["id"] for row in list_calls(limit=2, offset=2)["calls"]], [ids[0]])
        self.assertEqual([row["name"] for row in list_calls(sort="name", descending=False)["calls"]], ["Al", "Bea", None])
        self.assertEqual(list_calls("nobody"), {"calls": [], "total": 0})


def add_note(body: str, record: str | None = None, scheduled_call_id: object = None) -> None:
    """Insert a staff note on a call record (typed list ID) or a scheduled callback."""
    sql("INSERT INTO call_notes (call_record_id, scheduled_call_id, author_user_id, author_label, body) "
        "VALUES (%s, %s, gen_random_uuid(), 'staff@example.com', %s)",
        (record.split(":")[1] if record else None, scheduled_call_id, body))


class CallDetailDbTests(DbTestCase):
    def test_record_has_transcript_and_both_note_threads(self) -> None:
        """Verify an archived call returns its transcript plus its own and its callback's notes in order."""
        customer_id = add_customer()["id"]
        callback_id = add_callback(customer_id, status="completed")
        record = add_record(5, customer_id=customer_id, scheduled_call_id=callback_id, ended_reason="customer-ended-call",
                            transcript="Customer: Hi", transcript_state="ready")
        add_note("About the callback", scheduled_call_id=callback_id)
        add_note("About this call", record=record)
        call = db.get_call("call", uuid.UUID(record.split(":")[1]))
        self.assertEqual((call["id"], call["status"], call["transcript"]), (record, "completed", "Customer: Hi"))
        self.assertEqual([(n["target"], n["body"]) for n in call["notes"]],
                         [("callback", "About the callback"), ("call", "About this call")])

    def test_old_ids_resolve_to_the_current_entry(self) -> None:
        """Verify merged-record aliases and archived attempts open the canonical record."""
        customer_id = add_customer()["id"]
        attempt_id = add_attempt(customer_id, ended=True, vapi_call_id="vapi-1")
        record = add_record(vapi_call_id="vapi-1", attempt_id=attempt_id, customer_id=customer_id, direction="outbound")
        alias = uuid.uuid4()
        sql("INSERT INTO call_record_aliases (alias_id, call_record_id) VALUES (%s, %s)", (alias, record.split(":")[1]))
        self.assertEqual(db.get_call("attempt", attempt_id)["id"], record)
        self.assertEqual(db.get_call("call", alias)["id"], record)

    def test_attempt_and_scheduled_entries(self) -> None:
        """Verify unarchived attempts and upcoming callbacks open without transcripts; finished callbacks don't."""
        customer_id = add_customer()["id"]
        upcoming = add_callback(customer_id, minutes=60, instructions="Ask about the trip")
        attempt_id = add_attempt(customer_id, upcoming, ended=True, minutes_ago=60)
        self.assertEqual(db.get_call("attempt", attempt_id)["messages"], [])
        call = db.get_call("scheduled", upcoming)
        self.assertEqual((call["status"], call["instructions"], call["transcript"]), ("scheduled", "Ask about the trip", None))
        self.assertIsNone(db.get_call("scheduled", add_callback(customer_id, status="completed")))
        self.assertIsNone(db.get_call("call", uuid.uuid4()))

    def test_audio_key_only_for_ready_listed_recordings(self) -> None:
        """Verify a ready recording returns its key; pending, deleted and non-call entries return None."""
        ready = add_record(audio_state="ready", audio_object_key="calls/a.wav", audio_media_type="audio/wav", audio_bytes=4)
        deleted = add_record(audio_state="ready", audio_object_key="calls/b.wav", audio_media_type="audio/wav", audio_bytes=4,
                             deleted_at=datetime.now(timezone.utc))
        pending = add_record()
        keys = [db.call_audio_key("call", uuid.UUID(entry.split(":")[1])) for entry in (ready, deleted, pending)]
        self.assertEqual(keys, ["calls/a.wav", None, None])
        self.assertIsNone(db.call_audio_key("scheduled", add_callback(add_customer()["id"])))


class CallNoteDbTests(DbTestCase):
    def test_note_on_attempt_creates_durable_record(self) -> None:
        """Verify a note on an unarchived attempt is stored on a new call record that the list then shows."""
        customer_id = add_customer()["id"]
        attempt_id = add_attempt(customer_id, ended=True, vapi_call_id="vapi-1")
        result = db.add_call_note("attempt", attempt_id, str(uuid.uuid4()), "Luce", "Sounded tired")
        self.assertTrue(result["entry_id"].startswith("call:"))
        self.assertEqual((result["note"]["target"], result["note"]["author_label"]), ("call", "Luce"))
        self.assertEqual(db.get_call("attempt", attempt_id)["notes"][0]["body"], "Sounded tired")
        self.assertEqual([row["id"] for row in list_calls()["calls"]], [result["entry_id"]])

    def test_note_on_callback_and_missing_targets(self) -> None:
        """Verify callback notes are labelled; deleted, merged-away and unknown targets return None or resolve."""
        callback_id = add_callback(add_customer()["id"], minutes=60)
        result = db.add_call_note("scheduled", callback_id, str(uuid.uuid4()), "Tom", "Ask about the trip")
        self.assertEqual((result["entry_id"], result["note"]["target"]), (f"scheduled:{callback_id}", "callback"))
        deleted = add_record(deleted_at=datetime.now(timezone.utc))
        self.assertIsNone(db.add_call_note("call", uuid.UUID(deleted.split(":")[1]), str(uuid.uuid4()), "Tom", "x"))
        self.assertIsNone(db.add_call_note("call", uuid.uuid4(), str(uuid.uuid4()), "Tom", "x"))
        self.assertIsNone(db.add_call_note("attempt", uuid.uuid4(), str(uuid.uuid4()), "Tom", "x"))


class ConnectionTests(DbTestCase):
    def test_ping(self):
        db.ping()

    def test_connect_without_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                db.connect()

    def test_json_row_turns_datetimes_into_iso_strings(self):
        at = datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(db.json_row({"at": at, "n": 1, "s": None}),
                         {"at": "2026-10-22T09:30:00+00:00", "n": 1, "s": None})


class CustomerTests(DbTestCase):
    def test_insert_customer_defaults(self):
        customer = add_customer()
        self.assertEqual(customer["phone"], PHONE)
        self.assertIsNone(customer["middle_name"])
        self.assertFalse(customer["opted_out"])
        self.assertEqual(customer["source"], "staff")
        self.assertFalse(customer["needs_review"])
        self.assertIsInstance(customer["created_at"], datetime)

    def test_insert_inbound_customer(self):
        customer = add_customer(source="inbound", needs_review=True)
        self.assertEqual((customer["source"], customer["needs_review"]), ("inbound", True))

    def test_duplicate_phone_raises_unique_violation(self):
        add_customer()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            add_customer(first_name="Other")

    def test_constraints(self):
        for kwargs in ({"phone": "0791234567"}, {"source": "web"}, {"first_name": "  "}):
            with self.subTest(**kwargs), self.assertRaises(psycopg.errors.CheckViolation):
                add_customer(**kwargs)

    def test_get_customer_by_phone(self):
        customer = add_customer()
        found = db.get_customer_by_phone(PHONE)
        self.assertEqual(found["id"], customer["id"])
        self.assertEqual(found["vapi_assistant_id"], "assistant-anna")
        self.assertIsNone(db.get_customer_by_phone(OTHER_PHONE))

    def test_get_customer_calling_now(self):
        customer_id = add_customer()["id"]
        self.assertFalse(db.get_customer(customer_id)["calling_now"])
        add_attempt(customer_id, ended=True)
        self.assertFalse(db.get_customer(customer_id)["calling_now"])
        add_attempt(customer_id)
        self.assertTrue(db.get_customer(customer_id)["calling_now"])

    def test_get_unknown_customer(self):
        self.assertIsNone(db.get_customer(uuid.uuid4()))

    def test_update_customer_assistant(self):
        customer_id = add_customer()["id"]
        updated = db.update_customer(customer_id, {"vapi_assistant_id": "assistant-new"})
        self.assertEqual(updated["vapi_assistant_id"], "assistant-new")
        self.assertEqual(db.get_customer_by_phone(PHONE)["vapi_assistant_id"], "assistant-new")
        self.assertIsNone(db.update_customer(uuid.uuid4(), {"vapi_assistant_id": "assistant-new"}))

    def test_mark_customer_reviewed(self):
        customer_id = add_customer(source="inbound", needs_review=True)["id"]
        self.assertTrue(db.mark_customer_reviewed(customer_id))
        self.assertFalse(sql("SELECT needs_review FROM customers WHERE id = %s", (customer_id,))[0]["needs_review"])
        self.assertFalse(db.mark_customer_reviewed(uuid.uuid4()))

    def test_count_inbound_signups(self):
        add_customer(phone="+41791111111")  # staff: not counted
        add_customer(phone="+41792222222", source="inbound")
        old = add_customer(phone="+41793333333", source="inbound")["id"]
        sql("UPDATE customers SET created_at = now() - interval '25 hours' WHERE id = %s", (old,))
        self.assertEqual(db.count_inbound_signups(24), 1)
        self.assertEqual(db.count_inbound_signups(26), 2)


class DeleteCustomerTests(DbTestCase):
    def test_deletes_customer_with_callbacks_and_attempts(self):
        customer_id = add_customer()["id"]
        callback_id = add_callback(customer_id)
        add_attempt(customer_id, callback_id, ended=True)
        add_attempt(customer_id, ended=True)  # ad hoc
        self.assertEqual(db.delete_customer(customer_id), "deleted")
        for table in ("customers", "scheduled_calls", "call_attempts"):
            self.assertEqual(sql(f"SELECT count(*) AS n FROM {table}")[0]["n"], 0, table)

    def test_not_deleted_while_calling(self):
        customer_id = add_customer()["id"]
        add_attempt(customer_id)
        self.assertEqual(db.delete_customer(customer_id), "calling")
        self.assertIsNotNone(db.get_customer(customer_id))

    def test_unknown_customer(self):
        self.assertIsNone(db.delete_customer(uuid.uuid4()))

    def test_other_customers_untouched(self):
        customer_id = add_customer()["id"]
        other_id = add_customer(phone=OTHER_PHONE)["id"]
        add_attempt(other_id)  # the other customer is being called
        self.assertEqual(db.delete_customer(customer_id), "deleted")
        self.assertIsNotNone(db.get_customer(other_id))


class InsertScheduledCallTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.customer_id = add_customer()["id"]
        self.due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)

    def schedule(self, due=None, max_pending=3, **kwargs):
        due = due or self.due
        return db.insert_scheduled_call(self.customer_id, due.replace(tzinfo=None), due, max_pending, **kwargs)

    def test_inserts_pending_callback(self):
        row = self.schedule(source_vapi_call_id="call-1", instructions="Ask about the doctor.")
        self.assertFalse(row["already_scheduled"])
        self.assertEqual((row["status"], row["due_at"]), ("pending", self.due))
        self.assertEqual((row["source_vapi_call_id"], row["instructions"]), ("call-1", "Ask about the doctor."))
        self.assertIsNone(row["vapi_phone_number_id"])
        self.assertIsNone(row["vapi_assistant_id"])

    def test_stores_overrides(self):
        row = self.schedule(vapi_phone_number_id="phone-x", vapi_assistant_id="assistant-x")
        self.assertEqual((row["vapi_phone_number_id"], row["vapi_assistant_id"]), ("phone-x", "assistant-x"))

    def test_same_time_returns_existing_callback(self):
        first = self.schedule(instructions="first")
        again = self.schedule(instructions="second")
        self.assertTrue(again["already_scheduled"])
        self.assertEqual((again["id"], again["instructions"]), (first["id"], "first"))
        self.assertEqual(sql("SELECT count(*) AS n FROM scheduled_calls")[0]["n"], 1)

    def test_same_time_as_calling_callback_returns_it(self):
        first = self.schedule()
        sql("UPDATE scheduled_calls SET status = 'calling' WHERE id = %s", (first["id"],))
        self.assertEqual(self.schedule()["id"], first["id"])

    def test_same_time_as_finished_callback_inserts_new_one(self):
        for status in ("cancelled", "completed", "failed"):
            with self.subTest(status=status):
                sql("UPDATE scheduled_calls SET status = %s", (status,))
                self.assertFalse(self.schedule()["already_scheduled"])

    def test_pending_limit(self):
        self.schedule()
        calling = self.schedule(self.due + timedelta(hours=1))
        sql("UPDATE scheduled_calls SET status = 'calling' WHERE id = %s", (calling["id"],))  # counts too
        with self.assertRaises(db.PendingLimitReached):
            self.schedule(self.due + timedelta(hours=2), max_pending=2)

    def test_finished_callbacks_dont_count_towards_limit(self):
        for status in ("cancelled", "completed", "failed"):
            add_callback(self.customer_id, status=status)
        self.assertFalse(self.schedule(max_pending=1)["already_scheduled"])

    def test_duplicate_at_limit_returns_existing_instead_of_raising(self):
        first = self.schedule(max_pending=1)
        self.assertEqual(self.schedule(max_pending=1)["id"], first["id"])

    def test_limit_is_per_customer(self):
        add_callback(add_customer(phone=OTHER_PHONE)["id"], minutes=60)
        self.assertFalse(self.schedule(max_pending=1)["already_scheduled"])


class ClaimDueCallTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.customer_id = add_customer()["id"]

    def claim(self, hours=ALWAYS):
        return db.claim_due_call(STALE, *hours)

    def test_nothing_due(self):
        self.assertIsNone(self.claim())
        add_callback(self.customer_id, minutes=10)
        self.assertIsNone(self.claim())

    def test_claims_due_callback(self):
        callback_id = add_callback(self.customer_id, minutes=-1, instructions="Say hi.")
        claimed = self.claim()
        self.assertEqual(claimed["id"], callback_id)
        self.assertEqual(claimed["customer_id"], self.customer_id)
        self.assertEqual((claimed["phone"], claimed["instructions"]), (PHONE, "Say hi."))
        self.assertEqual(claimed["attempt_no"], 1)
        self.assertEqual(callback(callback_id)["status"], "calling")
        row = attempt(claimed["attempt_id"])
        self.assertEqual((row["scheduled_call_id"], row["customer_id"], row["attempt_no"]),
                         (callback_id, self.customer_id, 1))
        self.assertEqual((row["vapi_phone_number_id"], row["vapi_assistant_id"]), ("phone-anna", "assistant-anna"))
        self.assertIsNone(row["vapi_call_id"])
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(self.claim())  # already calling

    def test_uses_callback_overrides_or_customer_defaults(self):
        add_callback(self.customer_id, minutes=-2, vapi_phone_number_id="phone-x", vapi_assistant_id="assistant-x")
        add_callback(self.customer_id, minutes=-1)
        first = self.claim()
        db.finish_attempt(first["attempt_id"], "customer-ended-call", answered=True)
        second = self.claim()
        self.assertEqual((first["vapi_phone_number_id"], first["vapi_assistant_id"]), ("phone-x", "assistant-x"))
        self.assertEqual((second["vapi_phone_number_id"], second["vapi_assistant_id"]),
                         ("phone-anna", "assistant-anna"))

    def test_oldest_first(self):
        other_id = add_customer(phone=OTHER_PHONE, vapi_phone_number_id="phone-other")["id"]
        newer = add_callback(self.customer_id, minutes=-1)
        older = add_callback(other_id, minutes=-5)
        self.assertEqual(self.claim()["id"], older)
        self.assertEqual(self.claim()["id"], newer)

    def test_first_attempt_ignores_calling_hours(self):
        callback_id = add_callback(self.customer_id, minutes=-1)
        self.assertEqual(self.claim(NEVER)["id"], callback_id)

    def test_retry_only_inside_calling_hours(self):
        callback_id = add_callback(self.customer_id, minutes=-1)
        add_attempt(self.customer_id, callback_id, ended=True)
        self.assertIsNone(self.claim(NEVER))
        claimed = self.claim(ALWAYS)
        self.assertEqual((claimed["id"], claimed["attempt_no"]), (callback_id, 2))

    def test_skips_busy_phone_number(self):
        add_attempt(self.customer_id)  # an open ad-hoc call on phone-anna
        add_callback(self.customer_id, minutes=-5)
        self.assertIsNone(self.claim())
        other_id = add_customer(phone=OTHER_PHONE, vapi_phone_number_id="phone-other")["id"]
        other_callback = add_callback(other_id, minutes=-1)
        self.assertEqual(self.claim()["id"], other_callback)

    def test_skips_busy_customer_even_when_callback_uses_another_number(self):
        add_attempt(self.customer_id, vapi_phone_number_id="phone-other")
        add_callback(self.customer_id, minutes=-1, vapi_phone_number_id="phone-anna")
        self.assertIsNone(self.claim())

    def test_override_number_busy(self):
        other_id = add_customer(phone=OTHER_PHONE, vapi_phone_number_id="phone-other")["id"]
        add_attempt(other_id, vapi_phone_number_id="phone-x")
        add_callback(self.customer_id, minutes=-2, vapi_phone_number_id="phone-x")
        default_number = add_callback(self.customer_id, minutes=-1)
        self.assertEqual(self.claim()["id"], default_number)
        self.assertIsNone(self.claim())

    def test_ended_attempt_frees_phone_number(self):
        add_attempt(self.customer_id, ended=True)
        callback_id = add_callback(self.customer_id, minutes=-1)
        self.assertEqual(self.claim()["id"], callback_id)

    def test_claim_records_destination_phone(self):
        callback_id = add_callback(self.customer_id, minutes=-1)
        claimed = self.claim()
        self.assertEqual(claimed["id"], callback_id)
        self.assertEqual(attempt(claimed["attempt_id"])["customer_phone"], PHONE)

    def test_cancels_pending_callbacks_of_opted_out_customers(self):
        due = add_callback(self.customer_id, minutes=-1)
        future = add_callback(self.customer_id, minutes=60)
        calling = add_callback(self.customer_id, minutes=-10, status="calling")
        sql("UPDATE customers SET opted_out = true")
        self.assertIsNone(self.claim())
        self.assertEqual([callback(i)["status"] for i in (due, future, calling)], ["cancelled", "cancelled", "calling"])

    def test_stale_attempt_is_ended_and_retried(self):
        callback_id = add_callback(self.customer_id, minutes=-STALE - 10, status="calling")
        attempt_id = add_attempt(self.customer_id, callback_id, minutes_ago=STALE + 1, vapi_call_id="call-1")
        self.assertIsNone(self.claim())  # the retry is due later
        row = attempt(attempt_id)
        self.assertEqual(row["ended_reason"], "unknown")
        self.assertIsNotNone(row["ended_at"])
        self.assertRetryDue(callback_id, config.RETRY_MINUTES)

    def test_stale_last_attempt_fails_callback(self):
        callback_id = add_callback(self.customer_id, minutes=-STALE - 10, status="calling")
        add_attempt(self.customer_id, callback_id, attempt_no=config.MAX_ATTEMPTS, minutes_ago=STALE + 1)
        self.claim()
        self.assertEqual(callback(callback_id)["status"], "failed")

    def test_stale_attempt_frees_phone_number_in_same_run(self):
        add_attempt(self.customer_id, minutes_ago=STALE + 1)  # stale ad-hoc call
        callback_id = add_callback(self.customer_id, minutes=-1)
        self.assertEqual(self.claim()["id"], callback_id)

    def test_recent_open_attempt_is_not_stale(self):
        attempt_id = add_attempt(self.customer_id, minutes_ago=STALE - 1)
        self.claim()
        self.assertIsNone(attempt(attempt_id)["ended_at"])


class AttemptTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.customer_id = add_customer()["id"]

    def test_set_attempt_call_id_only_once(self):
        attempt_id = add_attempt(self.customer_id)
        self.assertTrue(db.set_attempt_call_id(attempt_id, "call-1"))
        self.assertFalse(db.set_attempt_call_id(attempt_id, "call-2"))
        self.assertEqual(attempt(attempt_id)["vapi_call_id"], "call-1")

    def test_set_attempt_call_id_not_on_ended_or_unknown_attempt(self):
        attempt_id = add_attempt(self.customer_id, ended=True)
        self.assertFalse(db.set_attempt_call_id(attempt_id, "call-1"))
        self.assertIsNone(attempt(attempt_id)["vapi_call_id"])
        self.assertFalse(db.set_attempt_call_id(uuid.uuid4(), "call-1"))

    def test_unrecorded_attempts(self):
        unrecorded = add_attempt(self.customer_id, minutes_ago=10)
        add_attempt(self.customer_id, minutes_ago=1)  # too young
        add_attempt(self.customer_id, minutes_ago=10, vapi_call_id="call-1")
        add_attempt(self.customer_id, minutes_ago=10, ended=True)
        rows = db.unrecorded_attempts(5)
        self.assertEqual([(r["id"], r["phone"]) for r in rows], [(unrecorded, PHONE)])

    def test_unrecorded_adhoc_attempt_without_customer(self):
        attempt_id = add_attempt(None, minutes_ago=10)
        self.assertEqual([(r["id"], r["phone"]) for r in db.unrecorded_attempts(5)], [(attempt_id, None)])

    def test_unrecorded_adhoc_attempt_uses_stored_non_customer_phone(self):
        attempt_id = db.reserve_adhoc_attempt(None, OTHER_PHONE, "phone-x", "assistant-x")
        sql("UPDATE call_attempts SET started_at = now() - interval '10 minutes' WHERE id = %s", (attempt_id,))
        self.assertEqual([(r["id"], r["phone"]) for r in db.unrecorded_attempts(5)],
                         [(attempt_id, OTHER_PHONE)])

    def test_known_call_ids(self):
        add_attempt(self.customer_id, vapi_call_id="call-1", ended=True)
        self.assertEqual(db.known_call_ids(["call-1", "call-2"]), {"call-1"})
        self.assertEqual(db.known_call_ids([]), set())

    def test_reserve_adhoc_attempt_for_customer_and_non_customer(self):
        customer_attempt = db.reserve_adhoc_attempt(self.customer_id, PHONE, "phone-x", "assistant-x")
        non_customer_attempt = db.reserve_adhoc_attempt(None, OTHER_PHONE, "phone-y", "assistant-y")
        rows = {row["id"]: row for row in sql("SELECT * FROM call_attempts")}
        self.assertEqual((rows[customer_attempt]["customer_id"], rows[customer_attempt]["customer_phone"],
                          rows[customer_attempt]["vapi_phone_number_id"], rows[customer_attempt]["vapi_assistant_id"]),
                         (self.customer_id, PHONE, "phone-x", "assistant-x"))
        self.assertEqual((rows[non_customer_attempt]["customer_id"], rows[non_customer_attempt]["customer_phone"]),
                         (None, OTHER_PHONE))

    def test_reserve_adhoc_attempt_refuses_opted_out_customer(self):
        sql("UPDATE customers SET opted_out = true WHERE id = %s", (self.customer_id,))
        with self.assertRaises(db.CustomerOptedOut):
            db.reserve_adhoc_attempt(self.customer_id, PHONE, "phone-x", "assistant-x")
        self.assertEqual(sql("SELECT count(*) AS n FROM call_attempts")[0]["n"], 0)

    def test_reserve_adhoc_attempt_refuses_busy_number_customer_or_destination(self):
        cases = (
            (lambda: add_attempt(self.customer_id, vapi_phone_number_id="phone-x"),
             (None, OTHER_PHONE, "phone-x", "assistant-x")),
            (lambda: add_attempt(self.customer_id, vapi_phone_number_id="phone-other"),
             (self.customer_id, PHONE, "phone-x", "assistant-x")),
            (lambda: add_attempt(None, vapi_phone_number_id="phone-other", customer_phone=OTHER_PHONE),
             (None, OTHER_PHONE, "phone-x", "assistant-x")),
        )
        for setup, args in cases:
            with self.subTest(args=args):
                sql("TRUNCATE call_attempts CASCADE")
                setup()
                with self.assertRaises(db.OutboundCallBusy):
                    db.reserve_adhoc_attempt(*args)

    def test_reserve_adhoc_attempt_detects_customer_changed_after_lookup(self):
        cases = ((None, PHONE), (uuid.uuid4(), PHONE), (self.customer_id, OTHER_PHONE))
        for customer_id, phone in cases:
            with self.subTest(customer_id=customer_id, phone=phone):
                with self.assertRaises(db.CustomerChanged):
                    db.reserve_adhoc_attempt(customer_id, phone, "phone-x", "assistant-x")

    def test_delete_undialled_attempt_only_deletes_reservation_without_call_id(self):
        reserved = db.reserve_adhoc_attempt(self.customer_id, PHONE, "phone-x", "assistant-x")
        self.assertTrue(db.delete_undialled_attempt(reserved))
        self.assertFalse(db.delete_undialled_attempt(reserved))
        dialled = add_attempt(self.customer_id, vapi_call_id="call-1", vapi_phone_number_id="phone-x")
        self.assertFalse(db.delete_undialled_attempt(dialled))
        self.assertIsNotNone(attempt(dialled))

    def test_open_attempts_only_with_call_id(self):
        callback_id = add_callback(self.customer_id, status="calling")
        open_id = add_attempt(self.customer_id, callback_id, vapi_call_id="call-1")
        add_attempt(self.customer_id, vapi_call_id="call-2", ended=True)
        add_attempt(self.customer_id)  # no call id yet
        self.assertEqual(db.open_attempts(), [{"id": open_id, "scheduled_call_id": callback_id, "vapi_call_id": "call-1"}])


class FinishAttemptTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.customer_id = add_customer()["id"]
        self.callback_id = add_callback(self.customer_id, minutes=-1, status="calling")

    def test_answered_completes_callback(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id, vapi_call_id="call-1")
        self.assertEqual(db.finish_attempt(attempt_id, "customer-ended-call", answered=True), "completed")
        row = attempt(attempt_id)
        self.assertEqual(row["ended_reason"], "customer-ended-call")
        self.assertIsNotNone(row["ended_at"])

    def test_not_answered_retries_later(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id, vapi_call_id="call-1")
        self.assertEqual(db.finish_attempt(attempt_id, "customer-did-not-answer"), "pending")
        self.assertRetryDue(self.callback_id, config.RETRY_MINUTES)

    def test_new_callback_in_the_retry_minute_is_a_duplicate(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id, vapi_call_id="call-1")
        db.finish_attempt(attempt_id, "customer-did-not-answer")
        due = callback(self.callback_id)["due_at"]
        row = db.insert_scheduled_call(self.customer_id, due.replace(tzinfo=None), due, 3, instructions="new")
        self.assertTrue(row["already_scheduled"])
        self.assertEqual(row["id"], self.callback_id)

    def test_last_attempt_fails_callback(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id, attempt_no=config.MAX_ATTEMPTS)
        self.assertEqual(db.finish_attempt(attempt_id, "customer-busy"), "failed")

    def test_answered_last_attempt_completes(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id, attempt_no=config.MAX_ATTEMPTS)
        self.assertEqual(db.finish_attempt(attempt_id, "customer-ended-call", answered=True), "completed")

    def test_explicit_ended_at(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id)
        ended_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=3)
        db.finish_attempt(attempt_id, "customer-ended-call", ended_at=ended_at, answered=True)
        self.assertEqual(attempt(attempt_id)["ended_at"], ended_at)

    def test_adhoc_attempt(self):
        attempt_id = add_attempt(self.customer_id, vapi_call_id="call-1")
        self.assertIsNone(db.finish_attempt(attempt_id, "customer-ended-call", answered=True))
        self.assertIsNotNone(attempt(attempt_id)["ended_at"])

    def test_second_finish_is_a_no_op(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id)
        db.finish_attempt(attempt_id, "customer-ended-call", answered=True)
        self.assertIsNone(db.finish_attempt(attempt_id, "unknown"))
        self.assertEqual(attempt(attempt_id)["ended_reason"], "customer-ended-call")
        self.assertEqual(callback(self.callback_id)["status"], "completed")

    def test_callback_no_longer_calling_is_left_alone(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id)
        sql("UPDATE scheduled_calls SET status = 'cancelled'")
        self.assertIsNone(db.finish_attempt(attempt_id, "customer-ended-call", answered=True))
        self.assertEqual(callback(self.callback_id)["status"], "cancelled")
        self.assertIsNotNone(attempt(attempt_id)["ended_at"])


class DropAttemptTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.customer_id = add_customer()["id"]
        self.callback_id = add_callback(self.customer_id, minutes=-1, status="calling")

    def test_drops_attempt_and_requeues_callback(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id)
        self.assertEqual(db.drop_attempt_and_retry(attempt_id, 5), "pending")
        self.assertEqual(sql("SELECT count(*) AS n FROM call_attempts")[0]["n"], 0)
        self.assertRetryDue(self.callback_id, 5)

    def test_dropped_attempt_doesnt_count(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id)
        db.drop_attempt_and_retry(attempt_id, 0)
        claimed = db.claim_due_call(STALE, *NEVER)  # still a first attempt: calling hours don't apply
        self.assertEqual((claimed["id"], claimed["attempt_no"]), (self.callback_id, 1))

    def test_attempt_with_call_id_is_kept(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id, vapi_call_id="call-1")
        self.assertIsNone(db.drop_attempt_and_retry(attempt_id, 5))
        self.assertIsNotNone(attempt(attempt_id))
        self.assertEqual(callback(self.callback_id)["status"], "calling")

    def test_adhoc_attempt(self):
        attempt_id = add_attempt(self.customer_id)
        self.assertIsNone(db.drop_attempt_and_retry(attempt_id, 5))
        self.assertEqual(sql("SELECT count(*) AS n FROM call_attempts WHERE id = %s", (attempt_id,))[0]["n"], 0)

    def test_callback_no_longer_calling_is_left_alone(self):
        attempt_id = add_attempt(self.customer_id, self.callback_id)
        sql("UPDATE scheduled_calls SET status = 'cancelled'")
        self.assertIsNone(db.drop_attempt_and_retry(attempt_id, 5))
        self.assertEqual(callback(self.callback_id)["status"], "cancelled")


class CancelCallbackTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.customer_id = add_customer()["id"]

    def test_cancels_pending_callback(self):
        callback_id = add_callback(self.customer_id, minutes=60)
        self.assertEqual(db.cancel_scheduled_call(callback_id), "cancelled")
        self.assertEqual(callback(callback_id)["status"], "cancelled")

    def test_other_statuses_are_returned_unchanged(self):
        for status in ("calling", "completed", "failed", "cancelled"):
            with self.subTest(status=status):
                callback_id = add_callback(self.customer_id, status=status)
                self.assertEqual(db.cancel_scheduled_call(callback_id), status)
                self.assertEqual(callback(callback_id)["status"], status)

    def test_unknown_callback(self):
        self.assertIsNone(db.cancel_scheduled_call(uuid.uuid4()))


class RescheduleCallbackTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.customer_id = add_customer()["id"]
        self.old_id = add_callback(self.customer_id, minutes=60, instructions="Ask about the doctor.",
                                   vapi_phone_number_id="phone-x", vapi_assistant_id="assistant-x")
        self.new_due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)

    def reschedule(self, old_id=None, customer_id=None, due=None):
        due = due or self.new_due
        return db.reschedule_callback(old_id or self.old_id, customer_id or self.customer_id,
                                      due.replace(tzinfo=None), due, "call-9")

    def test_moves_callback_to_new_row(self):
        result = self.reschedule()
        self.assertFalse(result["merged"])
        self.assertNotEqual(result["id"], self.old_id)
        self.assertEqual(callback(self.old_id)["status"], "cancelled")
        new = sql("SELECT * FROM scheduled_calls WHERE id = %s", (result["id"],))[0]
        self.assertEqual((new["status"], new["due_at"], new["source_vapi_call_id"]), ("pending", self.new_due, "call-9"))
        self.assertEqual((new["vapi_phone_number_id"], new["vapi_assistant_id"], new["instructions"]),
                         ("phone-x", "assistant-x", "Ask about the doctor."))

    def test_merges_into_existing_callback_at_new_time(self):
        for status in ("pending", "calling"):
            with self.subTest(status=status):
                sql("UPDATE scheduled_calls SET status = 'pending' WHERE id = %s", (self.old_id,))
                existing = sql("INSERT INTO scheduled_calls (customer_id, local_time, due_at, status) "
                               "VALUES (%s, now()::timestamp, %s, %s) RETURNING id",
                               (self.customer_id, self.new_due, status))[0]["id"]
                self.assertEqual(self.reschedule(), {"id": existing, "merged": True})
                self.assertEqual(callback(self.old_id)["status"], "cancelled")
                sql("DELETE FROM scheduled_calls WHERE id = %s", (existing,))

    def test_same_time_as_itself_creates_new_row(self):
        old_due = callback(self.old_id)["due_at"]
        result = self.reschedule(due=old_due)
        self.assertFalse(result["merged"])
        self.assertNotEqual(result["id"], self.old_id)

    def test_not_pending(self):
        sql("UPDATE scheduled_calls SET status = 'calling'")
        self.assertIsNone(self.reschedule())
        self.assertEqual(callback(self.old_id)["status"], "calling")
        self.assertEqual(sql("SELECT count(*) AS n FROM scheduled_calls")[0]["n"], 1)

    def test_other_customers_callback(self):
        other_id = add_customer(phone=OTHER_PHONE)["id"]
        self.assertIsNone(self.reschedule(customer_id=other_id))
        self.assertEqual(callback(self.old_id)["status"], "pending")

    def test_unknown_callback(self):
        self.assertIsNone(self.reschedule(old_id=uuid.uuid4()))


class PendingCallbacksTests(DbTestCase):
    def test_pending_callbacks_soonest_first(self):
        customer_id = add_customer()["id"]
        later = add_callback(customer_id, minutes=120, instructions="later")
        sooner = add_callback(customer_id, minutes=60)
        for status in ("calling", "completed", "failed", "cancelled"):
            add_callback(customer_id, minutes=30, status=status)
        add_callback(add_customer(phone=OTHER_PHONE)["id"], minutes=10)
        rows = db.pending_callbacks(customer_id)
        self.assertEqual([r["id"] for r in rows], [sooner, later])
        self.assertEqual(rows[1]["instructions"], "later")
        self.assertEqual(set(rows[0]), {"id", "due_at", "instructions"})


class ListCustomersTests(DbTestCase):
    def test_sorted_by_name_ignoring_case(self):
        add_customer(phone="+41791111111", first_name="Bob", last_name="Muster")
        add_customer(phone="+41792222222", first_name="anna", last_name="Zed")
        add_customer(phone="+41793333333", first_name="Anna", last_name="ark")
        self.assertEqual([c["last_name"] for c in db.list_customers()], ["ark", "Zed", "Muster"])

    def test_customer_without_calls(self):
        add_customer(source="inbound", needs_review=True)
        [customer] = db.list_customers()
        self.assertEqual((customer["source"], customer["needs_review"]), ("inbound", True))
        self.assertIsNone(customer["next_call_at"])
        self.assertEqual((customer["pending_callbacks"], customer["pending"]), (0, []))
        self.assertIsNone(customer["last_call_at"])
        self.assertIsNone(customer["last_call_ended_reason"])
        self.assertFalse(customer["calling_now"])

    def test_pending_callbacks(self):
        customer_id = add_customer()["id"]
        later = add_callback(customer_id, minutes=120, instructions="later")
        sooner = add_callback(customer_id, minutes=60)
        add_attempt(customer_id, later, ended=True)
        for status in ("calling", "completed", "failed", "cancelled"):
            add_callback(customer_id, minutes=30, status=status)
        add_callback(add_customer(phone=OTHER_PHONE)["id"], minutes=10)
        customer = next(c for c in db.list_customers() if c["id"] == customer_id)
        self.assertEqual(customer["next_call_at"], callback(sooner)["due_at"])
        self.assertEqual(customer["pending_callbacks"], 2)
        self.assertEqual([(p["id"], p["instructions"], p["attempts"]) for p in customer["pending"]],
                         [(str(sooner), None, 0), (str(later), "later", 1)])

    def test_last_call(self):
        customer_id = add_customer()["id"]
        add_attempt(customer_id, minutes_ago=30, ended=True)
        latest = add_attempt(customer_id, minutes_ago=5)
        customer = db.list_customers()[0]
        self.assertEqual(customer["last_call_at"], attempt(latest)["started_at"])
        self.assertTrue(customer["calling_now"])
        sql("UPDATE call_attempts SET ended_at = now(), ended_reason = 'customer-busy' WHERE id = %s", (latest,))
        customer = db.list_customers()[0]
        self.assertEqual(customer["last_call_ended_reason"], "customer-busy")
        self.assertFalse(customer["calling_now"])


class DispatcherStatusTests(DbTestCase):
    def test_record_and_read(self):
        self.assertIsNone(db.get_dispatcher_status())
        db.record_dispatcher_run(True, 3)
        first = db.get_dispatcher_status()
        self.assertEqual((first["last_run_ok"], first["last_started"]), (True, 3))
        db.record_dispatcher_run(False, 0)
        second = db.get_dispatcher_status()
        self.assertEqual((second["last_run_ok"], second["last_started"]), (False, 0))
        self.assertGreaterEqual(second["last_run_at"], first["last_run_at"])
        self.assertEqual(sql("SELECT count(*) AS n FROM dispatcher_status")[0]["n"], 1)


class TeamNotificationTests(DbTestCase):
    def notify(self, reason="request", summary="Please call back.", minutes_ago=0):
        row = db.insert_team_notification(reason, PHONE, "Anna", "Home", summary, "call-1")
        sql("UPDATE team_notifications SET created_at = now() - make_interval(mins => %s) WHERE id = %s",
            (minutes_ago, row["id"]))
        return row["id"]

    def test_insert(self):
        row = db.insert_team_notification("inbound_signup", None, None, None, None, None)
        self.assertIsInstance(row["created_at"], datetime)
        stored = sql("SELECT emailed_at, email_claimed_at FROM team_notifications")[0]
        self.assertIsNone(stored["emailed_at"])
        self.assertIsNone(stored["email_claimed_at"])

    def test_constraints(self):
        for kwargs in ({"reason": "spam"}, {"summary": "x" * 1001}):
            with self.subTest(**kwargs), self.assertRaises(psycopg.errors.CheckViolation):
                self.notify(**kwargs)

    def test_claim_oldest_first_up_to_limit_and_only_once(self):
        newest = self.notify(minutes_ago=1)
        oldest = self.notify(minutes_ago=3)
        middle = self.notify(minutes_ago=2)
        claimed = db.claim_unsent_notifications(2, 24, 10)
        self.assertEqual([n["id"] for n in claimed], [oldest, middle])
        self.assertEqual((claimed[0]["reason"], claimed[0]["caller_phone"], claimed[0]["summary"]),
                         ("request", PHONE, "Please call back."))
        self.assertTrue(all(n["email_claimed_at"] is not None for n in claimed))
        self.assertEqual([n["id"] for n in db.claim_unsent_notifications(2, 24, 10)], [newest])
        self.assertEqual(db.claim_unsent_notifications(2, 24, 10), [])

    def test_too_old_not_claimed(self):
        self.notify(minutes_ago=25 * 60)
        self.assertEqual(db.claim_unsent_notifications(10, 24, 10), [])

    def test_released_notification_is_claimed_again(self):
        notification_id = self.notify()
        claimed = db.claim_unsent_notifications(10, 24, 10)[0]
        self.assertTrue(db.release_notification(notification_id, claimed["email_claimed_at"]))
        self.assertEqual([n["id"] for n in db.claim_unsent_notifications(10, 24, 10)], [notification_id])

    def test_expired_claim_is_recovered_but_fresh_claim_is_not(self):
        notification_id = self.notify()
        claimed = db.claim_unsent_notifications(10, 24, 10)[0]
        self.assertEqual(db.claim_unsent_notifications(10, 24, 10), [])
        sql("UPDATE team_notifications SET email_claimed_at = now() - interval '11 minutes' WHERE id = %s",
            (notification_id,))
        recovered = db.claim_unsent_notifications(10, 24, 10)
        self.assertEqual([row["id"] for row in recovered], [notification_id])
        self.assertGreater(recovered[0]["email_claimed_at"], claimed["email_claimed_at"])

    def test_claim_that_expires_after_age_cutoff_is_recovered_once(self):
        notification_id = self.notify(minutes_ago=23 * 60 + 55)
        db.claim_unsent_notifications(10, 24, 10)
        sql("UPDATE team_notifications SET created_at = now() - interval '25 hours', "
            "email_claimed_at = now() - interval '11 minutes' WHERE id = %s", (notification_id,))
        recovered = db.claim_unsent_notifications(10, 24, 10)[0]
        self.assertEqual(recovered["id"], notification_id)
        self.assertTrue(db.release_notification(notification_id, recovered["email_claimed_at"]))
        self.assertEqual(db.claim_unsent_notifications(10, 24, 10), [])

    def test_successful_send_is_marked_and_never_reclaimed(self):
        notification_id = self.notify()
        claimed = db.claim_unsent_notifications(10, 24, 10)[0]
        self.assertTrue(db.mark_notification_emailed(notification_id, claimed["email_claimed_at"]))
        sql("UPDATE team_notifications SET email_claimed_at = now() - interval '11 minutes' WHERE id = %s",
            (notification_id,))
        self.assertEqual(db.claim_unsent_notifications(10, 24, 10), [])
        stored = sql("SELECT emailed_at FROM team_notifications WHERE id = %s", (notification_id,))[0]
        self.assertIsNotNone(stored["emailed_at"])

    def test_stale_worker_cannot_finish_or_release_a_new_claim(self):
        notification_id = self.notify()
        old = db.claim_unsent_notifications(10, 24, 10)[0]
        sql("UPDATE team_notifications SET email_claimed_at = now() - interval '11 minutes' WHERE id = %s",
            (notification_id,))
        new = db.claim_unsent_notifications(10, 24, 10)[0]
        self.assertFalse(db.mark_notification_emailed(notification_id, old["email_claimed_at"]))
        self.assertFalse(db.release_notification(notification_id, old["email_claimed_at"]))
        self.assertEqual(sql("SELECT email_claimed_at FROM team_notifications WHERE id = %s", (notification_id,))[0]
                         ["email_claimed_at"], new["email_claimed_at"])


class WholeMinuteMigrationTests(DbTestCase):
    def test_rounds_open_callbacks_only_and_is_rerunnable(self):
        customer_id = add_customer()["id"]
        ids = {status: add_callback(customer_id, status=status) for status in ("pending", "calling", "completed")}
        sql("UPDATE scheduled_calls SET due_at = '2026-10-22 14:56:54.079+00'")
        migration = (MIGRATIONS / "011_whole_minute_due_at.sql").read_text()
        sql(migration)
        sql(migration)
        due = {status: callback(i)["due_at"] for status, i in ids.items()}
        self.assertEqual(due["pending"], datetime(2026, 10, 22, 14, 56, tzinfo=timezone.utc))
        self.assertEqual(due["calling"], due["pending"])
        self.assertEqual(due["completed"].second, 54)


class ConcurrencyTests(DbTestCase):
    """Another transaction holds the lock; the db function must wait for it and then see its changes."""

    def run_in_thread(self, func, *args):
        result = {}

        def target():
            try:
                result["value"] = func(*args)
            except Exception as e:  # noqa: BLE001 (re-raised in the test thread)
                result["error"] = e

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(0.5)
        self.assertTrue(thread.is_alive(), "should wait for the other transaction's lock")
        return thread, result

    def finish(self, thread, result):
        thread.join(10)
        self.assertFalse(thread.is_alive())
        if "error" in result:
            raise result["error"]
        return result["value"]

    def test_parallel_insert_scheduled_call_creates_one_row(self):
        customer_id = add_customer()["id"]
        due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
        with psycopg.connect(TEST_URL) as other:  # another request scheduling the same callback
            other.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
            other.execute("INSERT INTO scheduled_calls (customer_id, local_time, due_at) VALUES (%s, %s, %s)",
                          (customer_id, due.replace(tzinfo=None), due))
            thread, result = self.run_in_thread(db.insert_scheduled_call, customer_id, due.replace(tzinfo=None), due, 3)
        self.assertTrue(self.finish(thread, result)["already_scheduled"])
        self.assertEqual(sql("SELECT count(*) AS n FROM scheduled_calls")[0]["n"], 1)

    def test_parallel_claims_dont_claim_the_same_callback(self):
        customer_id = add_customer()["id"]
        callback_id = add_callback(customer_id, minutes=-1)
        with psycopg.connect(TEST_URL) as other:  # another dispatcher run claiming the callback
            other.execute("SELECT pg_advisory_xact_lock(%s)", (db.DISPATCH_LOCK_ID,))
            other.execute("UPDATE scheduled_calls SET status = 'calling' WHERE id = %s", (callback_id,))
            other.execute("INSERT INTO call_attempts (scheduled_call_id, customer_id, vapi_phone_number_id) "
                          "VALUES (%s, %s, 'phone-anna')", (callback_id, customer_id))
            thread, result = self.run_in_thread(db.claim_due_call, STALE, *ALWAYS)
        self.assertIsNone(self.finish(thread, result))
        self.assertEqual(sql("SELECT count(*) AS n FROM call_attempts")[0]["n"], 1)

    def test_parallel_adhoc_reservations_create_one_attempt(self):
        customer_id = add_customer()["id"]
        args = (customer_id, PHONE, "phone-anna", "assistant-anna")
        with psycopg.connect(TEST_URL) as other:
            other.execute("SELECT pg_advisory_xact_lock(%s)", (db.DISPATCH_LOCK_ID,))
            first_thread, first = self.run_in_thread(db.reserve_adhoc_attempt, *args)
            second_thread, second = self.run_in_thread(db.reserve_adhoc_attempt, *args)
        first_thread.join(10)
        second_thread.join(10)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        outcomes = (first, second)
        self.assertEqual(sum("value" in outcome for outcome in outcomes), 1)
        errors = [outcome.get("error") for outcome in outcomes if "error" in outcome]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], db.OutboundCallBusy)
        self.assertEqual(sql("SELECT count(*) AS n FROM call_attempts")[0]["n"], 1)

    def test_parallel_adhoc_reservation_and_scheduled_claim_create_one_attempt(self):
        customer_id = add_customer()["id"]
        callback_id = add_callback(customer_id, minutes=-1)
        with psycopg.connect(TEST_URL) as other:
            other.execute("SELECT pg_advisory_xact_lock(%s)", (db.DISPATCH_LOCK_ID,))
            reserve_thread, reserved = self.run_in_thread(
                db.reserve_adhoc_attempt, customer_id, PHONE, "phone-anna", "assistant-anna",
            )
            claim_thread, claimed = self.run_in_thread(db.claim_due_call, STALE, *ALWAYS)
        reserve_thread.join(10)
        claim_thread.join(10)
        self.assertFalse(reserve_thread.is_alive())
        self.assertFalse(claim_thread.is_alive())
        self.assertEqual(sql("SELECT count(*) AS n FROM call_attempts")[0]["n"], 1)
        if "value" in reserved:
            self.assertIsNone(claimed.get("value"))
            self.assertNotIn("error", claimed)
            self.assertEqual(callback(callback_id)["status"], "pending")
        else:
            self.assertIsInstance(reserved.get("error"), db.OutboundCallBusy)
            self.assertIsInstance(claimed.get("value"), dict)
            self.assertNotIn("error", claimed)
            self.assertEqual(callback(callback_id)["status"], "calling")

    def test_parallel_notification_claims_are_disjoint(self):
        notification_ids = {
            db.insert_team_notification("request", PHONE, None, None, str(n), None)["id"] for n in range(2)
        }
        barrier = threading.Barrier(3)
        results = [{}, {}]

        def claim(index):
            try:
                barrier.wait()
                results[index]["value"] = db.claim_unsent_notifications(1, 24, 10)
            except Exception as e:  # noqa: BLE001 (re-raised in the test thread)
                results[index]["error"] = e

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        for result in results:
            if "error" in result:
                raise result["error"]
            self.assertEqual(len(result["value"]), 1)
        claimed = [result["value"][0] for result in results]
        self.assertEqual({row["id"] for row in claimed}, notification_ids)
        self.assertTrue(db.release_notification(claimed[0]["id"], claimed[0]["email_claimed_at"]))
        stored = {row["id"]: row for row in sql("SELECT id, email_claimed_at FROM team_notifications")}
        self.assertIsNone(stored[claimed[0]["id"]]["email_claimed_at"])
        self.assertEqual(stored[claimed[1]["id"]]["email_claimed_at"], claimed[1]["email_claimed_at"])

    def test_reservation_waits_for_delete_customer_and_detects_change(self):
        customer_id = add_customer()["id"]
        with psycopg.connect(TEST_URL) as other:  # delete_customer holds the row lock
            other.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
            other.execute("DELETE FROM customers WHERE id = %s", (customer_id,))
            thread, result = self.run_in_thread(
                db.reserve_adhoc_attempt, customer_id, PHONE, "phone-anna", "assistant-anna",
            )
        with self.assertRaises(db.CustomerChanged):
            self.finish(thread, result)


if __name__ == "__main__":
    unittest.main()
