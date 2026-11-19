"""Database access (Supabase Postgres via the transaction pooler)."""

from datetime import datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from albert import config


def json_row(row: dict) -> dict:
    """A row with datetimes as ISO strings (Flask's jsonify would turn them into HTTP dates)."""
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()}


def connect() -> psycopg.Connection:
    url = config.env("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    # Supabase's transaction pooler doesn't support prepared statements.
    return psycopg.connect(url, prepare_threshold=None, row_factory=dict_row, connect_timeout=5)


def ping() -> None:
    """Raise if the database can't be reached (health check)."""
    with connect() as conn:
        conn.execute("SELECT 1")


def get_customer_by_phone(phone: str) -> dict | None:
    with connect() as conn:
        return conn.execute(
            "SELECT id, first_name, middle_name, last_name, phone, timezone, opted_out, "
            "  vapi_phone_number_id, vapi_assistant_id "
            "FROM customers WHERE phone = %s",
            (phone,),
        ).fetchone()


def get_customer(customer_id) -> dict | None:
    """A customer by id, with `calling_now` (an outbound call to them is still open)."""
    with connect() as conn:
        return conn.execute(
            "SELECT c.id, c.first_name, c.last_name, c.phone, "
            "  EXISTS (SELECT 1 FROM call_attempts a WHERE a.customer_id = c.id AND a.ended_at IS NULL) AS calling_now "
            "FROM customers c WHERE c.id = %s",
            (customer_id,),
        ).fetchone()


def delete_customer(customer_id: UUID | str) -> str | None:
    """Delete a customer; their callbacks and call attempts go with them (on delete cascade).

    Their archived calls, recordings and notes are purged as well (see `_purge_customer_archive`).

    Returns "deleted", "calling" (not deleted: a call is still open) or None (unknown customer).
    """
    with connect() as conn:
        _archive_lock(conn)  # before the customer row: archive writers take this lock first
        # The row lock makes a call attempt inserted meanwhile (FK key share lock) wait for us.
        customer = conn.execute("SELECT id, phone FROM customers WHERE id = %s FOR UPDATE", (customer_id,)).fetchone()
        if not customer:
            return None
        if conn.execute(
            "SELECT 1 FROM call_attempts WHERE customer_id = %s AND ended_at IS NULL LIMIT 1", (customer_id,)
        ).fetchone():
            return "calling"
        _purge_customer_archive(conn, customer_id, customer["phone"])
        conn.execute("DELETE FROM customers WHERE id = %s", (customer_id,))
        return "deleted"


def insert_customer(first_name: str, middle_name: str | None, last_name: str, phone: str, timezone: str,
                    vapi_phone_number_id: str, vapi_assistant_id: str, source: str = "staff",
                    needs_review: bool = False) -> dict:
    """Insert a customer; raises psycopg.errors.UniqueViolation if the phone exists."""
    with connect() as conn:
        return conn.execute(
            "INSERT INTO customers (first_name, middle_name, last_name, phone, timezone, "
            "  vapi_phone_number_id, vapi_assistant_id, source, needs_review) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, first_name, middle_name, last_name, phone, timezone, opted_out, "
            "  vapi_phone_number_id, vapi_assistant_id, source, needs_review, created_at",
            (first_name, middle_name, last_name, phone, timezone, vapi_phone_number_id, vapi_assistant_id,
             source, needs_review),
        ).fetchone()


def count_inbound_signups(hours: int) -> int:
    """Customers who signed up on an inbound call in the last `hours` hours."""
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM customers WHERE source = 'inbound' AND created_at > now() - make_interval(hours => %s)",
            (hours,),
        ).fetchone()["n"]


CUSTOMER_EDIT_FIELDS = ("first_name", "middle_name", "last_name", "timezone", "vapi_assistant_id")


def update_customer(customer_id, changes: dict) -> dict | None:
    """Change the given CUSTOMER_EDIT_FIELDS of a customer; None if there is no such customer.

    A new time zone moves the customer's pending callbacks so they keep their local clock time
    (e.g. still 10:00, now in the new zone). Returns the edited fields plus `id` and `callbacks_moved`.
    """
    columns = [field for field in CUSTOMER_EDIT_FIELDS if field in changes]
    if not columns:
        raise ValueError("Nothing to change.")
    assignments = ", ".join(f"{column} = %({column})s" for column in columns)
    with connect() as conn:
        old = conn.execute("SELECT timezone FROM customers WHERE id = %s FOR UPDATE", (customer_id,)).fetchone()
        if not old:
            return None
        customer = conn.execute(
            f"UPDATE customers SET {assignments} WHERE id = %(id)s "
            "RETURNING id, first_name, middle_name, last_name, timezone, vapi_assistant_id",
            {**{column: changes[column] for column in columns}, "id": customer_id},
        ).fetchone()
        moved = 0
        if customer["timezone"] != old["timezone"]:
            moved = conn.execute(
                "UPDATE scheduled_calls SET due_at = (due_at AT TIME ZONE %s) AT TIME ZONE %s "
                "WHERE customer_id = %s AND status = 'pending'",
                (old["timezone"], customer["timezone"], customer_id),
            ).rowcount
        return {**customer, "callbacks_moved": moved}


def mark_customer_reviewed(customer_id) -> bool:
    """Clear needs_review (a staff member checked an inbound sign-up); False if there is no such customer."""
    with connect() as conn:
        return conn.execute(
            "UPDATE customers SET needs_review = false WHERE id = %s RETURNING id", (customer_id,)
        ).fetchone() is not None


class PendingLimitReached(Exception):
    """The customer already has the maximum number of pending callbacks."""


class CustomerOptedOut(Exception):
    """An outbound call was requested for a customer who has opted out."""


class OutboundCallBusy(Exception):
    """The destination or Vapi phone number already has an open outbound call."""


class CustomerChanged(Exception):
    """The customer record changed after the caller looked it up."""


def insert_scheduled_call(customer_id, local_time, due_at, max_pending: int, source_vapi_call_id: str | None = None,
                          vapi_phone_number_id: str | None = None, vapi_assistant_id: str | None = None,
                          instructions: str | None = None) -> dict:
    """Insert a pending callback; raises PendingLimitReached if the customer already has max_pending.

    Idempotent: if the customer already has a pending/calling callback at the same due_at (a Vapi tool
    retry, the AI calling the tool twice, a double click), that row is returned instead of a new one.
    The returned row has `already_scheduled` True/False (an existing row keeps its own instructions).
    vapi_phone_number_id / vapi_assistant_id override the customer's defaults (None = use the default).
    """
    columns = ("id, customer_id, local_time, due_at, status, source_vapi_call_id, "
               "vapi_phone_number_id, vapi_assistant_id, instructions, created_at")
    with connect() as conn:
        # Lock the customer row so concurrent inserts can't both pass the duplicate and limit checks.
        conn.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
        existing = conn.execute(
            f"SELECT {columns} FROM scheduled_calls "
            "WHERE customer_id = %s AND due_at = %s AND status IN ('pending', 'calling') "
            "ORDER BY created_at LIMIT 1",
            (customer_id, due_at),
        ).fetchone()
        if existing:
            return {**existing, "already_scheduled": True}
        pending = conn.execute(
            "SELECT count(*) AS n FROM scheduled_calls WHERE customer_id = %s AND status IN ('pending', 'calling')",
            (customer_id,),
        ).fetchone()["n"]
        if pending >= max_pending:
            raise PendingLimitReached()
        created = conn.execute(
            "INSERT INTO scheduled_calls (customer_id, local_time, due_at, source_vapi_call_id, "
            "  vapi_phone_number_id, vapi_assistant_id, instructions) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {columns}",
            (customer_id, local_time, due_at, source_vapi_call_id, vapi_phone_number_id, vapi_assistant_id, instructions),
        ).fetchone()
        return {**created, "already_scheduled": False}


DISPATCH_LOCK_ID = 7_351_001  # pg advisory lock: one dispatcher run at a time


def claim_due_call(stale_minutes: int, calling_start: str, calling_end: str) -> dict | None:
    """Claim the next due callback whose Vapi phone number isn't busy; returns it with a new attempt_id.

    One outbound call per phone number at a time. The first attempt is claimed at any time (the
    time was chosen by staff or the customer); retries only inside the customer's calling hours,
    so a retry that falls outside them waits until the next morning.
    """
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (DISPATCH_LOCK_ID,))
        # Safety net when Vapi can't tell us the outcome: attempts open this long count as ended.
        # (Attempts without a Vapi call id are recovered earlier from Vapi's call list, see
        # dispatcher.recover_unrecorded_attempts; this only catches them if Vapi stays unreachable.)
        stale = conn.execute(
            "UPDATE call_attempts SET ended_at = now(), ended_reason = 'unknown' "
            "WHERE ended_at IS NULL AND started_at < now() - make_interval(mins => %s) "
            "RETURNING scheduled_call_id, attempt_no",
            (stale_minutes,),
        ).fetchall()
        for attempt in stale:
            if attempt["scheduled_call_id"]:
                _settle_scheduled_call(conn, attempt["scheduled_call_id"], attempt["attempt_no"], answered=False)
        conn.execute(
            "UPDATE scheduled_calls sc SET status = 'cancelled' FROM customers c "
            "WHERE c.id = sc.customer_id AND c.opted_out AND sc.status = 'pending'"
        )
        due = conn.execute(
            "SELECT * FROM ("
            "  SELECT sc.id, sc.customer_id, sc.due_at, sc.instructions, c.phone, "
            "    coalesce(sc.vapi_phone_number_id, c.vapi_phone_number_id) AS vapi_phone_number_id, "
            "    coalesce(sc.vapi_assistant_id, c.vapi_assistant_id) AS vapi_assistant_id, "
            "    (SELECT count(*) FROM call_attempts a WHERE a.scheduled_call_id = sc.id) AS attempts "
            "  FROM scheduled_calls sc JOIN customers c ON c.id = sc.customer_id "
            "  WHERE sc.status = 'pending' AND sc.due_at <= now() "
            "    AND (NOT EXISTS (SELECT 1 FROM call_attempts a WHERE a.scheduled_call_id = sc.id) "
            "      OR ((now() AT TIME ZONE c.timezone)::time >= %s::time "
            "      AND (now() AT TIME ZONE c.timezone)::time <  %s::time)) "
            "  FOR UPDATE OF sc SKIP LOCKED"
            ") due "
            "WHERE NOT EXISTS (SELECT 1 FROM call_attempts a "
            "  WHERE a.ended_at IS NULL AND (a.vapi_phone_number_id = due.vapi_phone_number_id "
            "    OR a.customer_phone = due.phone OR a.customer_id = due.customer_id)) "
            "ORDER BY due.due_at LIMIT 1",
            (calling_start, calling_end),
        ).fetchone()
        if not due:
            return None
        attempt_no = due["attempts"] + 1
        attempt = conn.execute(
            "INSERT INTO call_attempts (scheduled_call_id, customer_id, customer_phone, attempt_no, "
            "  vapi_phone_number_id, vapi_assistant_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (due["id"], due["customer_id"], due["phone"], attempt_no,
             due["vapi_phone_number_id"], due["vapi_assistant_id"]),
        ).fetchone()
        conn.execute("UPDATE scheduled_calls SET status = 'calling' WHERE id = %s", (due["id"],))
        return {**due, "attempt_no": attempt_no, "attempt_id": attempt["id"]}


def set_attempt_call_id(attempt_id, vapi_call_id: str) -> bool:
    """Record the Vapi call id on an open attempt that has none yet; False if there was nothing to update."""
    with connect() as conn:
        return conn.execute(
            "UPDATE call_attempts SET vapi_call_id = %s WHERE id = %s AND vapi_call_id IS NULL AND ended_at IS NULL",
            (vapi_call_id, attempt_id),
        ).rowcount == 1


def unrecorded_attempts(min_age_minutes: int) -> list[dict]:
    """Open attempts that never got a Vapi call id and are older than min_age_minutes, with the customer's phone."""
    with connect() as conn:
        return conn.execute(
            "SELECT a.id, a.scheduled_call_id, a.started_at, a.vapi_phone_number_id, "
            "  coalesce(a.customer_phone, c.phone) AS phone "
            "FROM call_attempts a LEFT JOIN customers c ON c.id = a.customer_id "
            "WHERE a.ended_at IS NULL AND a.vapi_call_id IS NULL "
            "  AND a.started_at < now() - make_interval(mins => %s)",
            (min_age_minutes,),
        ).fetchall()


def known_call_ids(vapi_call_ids: list[str]) -> set[str]:
    """The given Vapi call ids that are already recorded on some attempt."""
    if not vapi_call_ids:
        return set()
    with connect() as conn:
        rows = conn.execute(
            "SELECT vapi_call_id FROM call_attempts WHERE vapi_call_id = ANY(%s)", (list(vapi_call_ids),)
        ).fetchall()
        return {row["vapi_call_id"] for row in rows}


def reserve_adhoc_attempt(customer_id, customer_phone: str, vapi_phone_number_id: str, vapi_assistant_id: str):
    """Reserve a customer and outbound number before `/api/tools/call` dials.

    The dispatcher uses the same advisory lock, so no scheduled or second ad-hoc call can claim the
    destination or Vapi number between this check and the insert. `customer_id` is None when staff
    deliberately call a non-customer.
    """
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (DISPATCH_LOCK_ID,))
        current = conn.execute(
            "SELECT id, opted_out FROM customers WHERE phone = %s FOR UPDATE", (customer_phone,)
        ).fetchone()
        if customer_id is None:
            if current:
                raise CustomerChanged()
        elif not current or current["id"] != customer_id:
            raise CustomerChanged()
        elif current["opted_out"]:
            raise CustomerOptedOut()
        busy = conn.execute(
            "SELECT 1 FROM call_attempts WHERE ended_at IS NULL AND ("
            "  vapi_phone_number_id = %s OR customer_phone = %s OR (%s::uuid IS NOT NULL AND customer_id = %s)"
            ") LIMIT 1",
            (vapi_phone_number_id, customer_phone, customer_id, customer_id),
        ).fetchone()
        if busy:
            raise OutboundCallBusy()
        return conn.execute(
            "INSERT INTO call_attempts (customer_id, customer_phone, vapi_phone_number_id, vapi_assistant_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (customer_id, customer_phone, vapi_phone_number_id, vapi_assistant_id),
        ).fetchone()["id"]


def delete_undialled_attempt(attempt_id) -> bool:
    """Release a pre-dial reservation after a failure known not to have reached Vapi."""
    with connect() as conn:
        return conn.execute(
            "DELETE FROM call_attempts WHERE id = %s AND vapi_call_id IS NULL RETURNING id", (attempt_id,)
        ).fetchone() is not None


def open_attempts() -> list[dict]:
    """Attempts whose Vapi call hasn't been seen ending yet."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, scheduled_call_id, vapi_call_id FROM call_attempts "
            "WHERE ended_at IS NULL AND vapi_call_id IS NOT NULL"
        ).fetchall()


def finish_attempt(attempt_id, ended_reason: str, ended_at=None, answered: bool = False) -> str | None:
    """End an attempt and settle its callback; returns the callback's new status (None for ad-hoc calls)."""
    with connect() as conn:
        attempt = conn.execute(
            "UPDATE call_attempts SET ended_at = coalesce(%s, now()), ended_reason = %s "
            "WHERE id = %s AND ended_at IS NULL RETURNING scheduled_call_id, attempt_no",
            (ended_at, ended_reason, attempt_id),
        ).fetchone()
        if not attempt or not attempt["scheduled_call_id"]:
            return None
        return _settle_scheduled_call(conn, attempt["scheduled_call_id"], attempt["attempt_no"], answered)


def drop_attempt_and_retry(attempt_id, retry_minutes: int) -> str | None:
    """Undo an attempt that never reached Vapi (no call id) and put its callback back in the queue.

    Deleting the row means it doesn't count towards MAX_ATTEMPTS. Returns the callback's status.
    """
    with connect() as conn:
        attempt = conn.execute(
            "DELETE FROM call_attempts WHERE id = %s AND vapi_call_id IS NULL RETURNING scheduled_call_id",
            (attempt_id,),
        ).fetchone()
        if not attempt or not attempt["scheduled_call_id"]:
            return None
        row = conn.execute(
            "UPDATE scheduled_calls SET status = 'pending', "
            "  due_at = date_trunc('minute', now()) + make_interval(mins => %s) "
            "WHERE id = %s AND status = 'calling' RETURNING status",
            (retry_minutes, attempt["scheduled_call_id"]),
        ).fetchone()
        return row["status"] if row else None


def _settle_scheduled_call(conn, scheduled_call_id, attempt_no: int, answered: bool) -> str | None:
    """Answered -> completed; otherwise retry RETRY_MINUTES later, or failed after MAX_ATTEMPTS.

    Calling hours are enforced when claiming a retry, so a retry after 22:00 waits until 09:00.
    Retry times are whole minutes, like scheduled times: duplicates are detected and the AI finds a
    callback by its exact due_at.
    """
    retry = not answered and attempt_no < config.MAX_ATTEMPTS
    row = conn.execute(
        "UPDATE scheduled_calls SET "
        "  status = %(status)s, "
        "  due_at = CASE WHEN %(retry)s THEN date_trunc('minute', now()) + make_interval(mins => %(minutes)s) "
        "    ELSE due_at END "
        "WHERE id = %(id)s AND status = 'calling' RETURNING status",
        {
            "status": "completed" if answered else "pending" if retry else "failed",
            "retry": retry,
            "minutes": config.RETRY_MINUTES,
            "id": scheduled_call_id,
        },
    ).fetchone()
    return row["status"] if row else None


def list_customers() -> list[dict]:
    """All customers with their next pending callback and last call (for dropdowns and the overview)."""
    with connect() as conn:
        return conn.execute(
            "SELECT c.id, c.first_name, c.middle_name, c.last_name, c.phone, c.timezone, c.opted_out, c.vapi_assistant_id, "
            "  c.source, c.needs_review, "
            "  next_call.due_at AS next_call_at, "
            "  (SELECT count(*) FROM scheduled_calls sc "
            "     WHERE sc.customer_id = c.id AND sc.status = 'pending') AS pending_callbacks, "
            "  (SELECT coalesce(json_agg(json_build_object("
            "     'id', sc.id, 'due_at', sc.due_at, 'instructions', sc.instructions, "
            "     'attempts', (SELECT count(*) FROM call_attempts a WHERE a.scheduled_call_id = sc.id)"
            "   ) ORDER BY sc.due_at), '[]') "
            "   FROM scheduled_calls sc WHERE sc.customer_id = c.id AND sc.status = 'pending') AS pending, "
            "  last_call.started_at AS last_call_at, last_call.ended_reason AS last_call_ended_reason, "
            "  (last_call.started_at IS NOT NULL AND last_call.ended_at IS NULL) AS calling_now "
            "FROM customers c "
            "LEFT JOIN LATERAL (SELECT sc.due_at FROM scheduled_calls sc "
            "  WHERE sc.customer_id = c.id AND sc.status = 'pending' ORDER BY sc.due_at LIMIT 1) next_call ON true "
            "LEFT JOIN LATERAL (SELECT a.started_at, a.ended_at, a.ended_reason FROM call_attempts a "
            "  WHERE a.customer_id = c.id ORDER BY a.started_at DESC LIMIT 1) last_call ON true "
            "ORDER BY lower(c.first_name), lower(c.last_name)"
        ).fetchall()



def cancel_scheduled_call(scheduled_call_id) -> str | None:
    """Cancel a pending callback; returns its status afterwards ('cancelled' on success), None if not found."""
    with connect() as conn:
        row = conn.execute(
            "UPDATE scheduled_calls SET status = 'cancelled' WHERE id = %s AND status = 'pending' RETURNING status",
            (scheduled_call_id,),
        ).fetchone()
        if row:
            return row["status"]
        row = conn.execute("SELECT status FROM scheduled_calls WHERE id = %s", (scheduled_call_id,)).fetchone()
        return row["status"] if row else None


def reschedule_callback(old_id, customer_id, local_time, due_at, source_vapi_call_id: str | None) -> dict | None:
    """Move a pending callback to a new time in one transaction; None if it's no longer pending.

    The old row is cancelled and a new one created with the same overrides and instructions, so the
    attempts start fresh. If the customer already has another pending/calling callback at the new
    time, the old one is only cancelled (no duplicate). Returns {"id": <callback at the new time>, "merged": bool}.
    """
    with connect() as conn:
        conn.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
        old = conn.execute(
            "SELECT id, vapi_phone_number_id, vapi_assistant_id, instructions FROM scheduled_calls "
            "WHERE id = %s AND customer_id = %s AND status = 'pending' FOR UPDATE",
            (old_id, customer_id),
        ).fetchone()
        if not old:
            return None
        existing = conn.execute(
            "SELECT id FROM scheduled_calls WHERE customer_id = %s AND due_at = %s "
            "AND status IN ('pending', 'calling') AND id <> %s LIMIT 1",
            (customer_id, due_at, old_id),
        ).fetchone()
        conn.execute("UPDATE scheduled_calls SET status = 'cancelled' WHERE id = %s", (old_id,))
        if existing:
            return {"id": existing["id"], "merged": True}
        created = conn.execute(
            "INSERT INTO scheduled_calls (customer_id, local_time, due_at, source_vapi_call_id, "
            "  vapi_phone_number_id, vapi_assistant_id, instructions) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (customer_id, local_time, due_at, source_vapi_call_id,
             old["vapi_phone_number_id"], old["vapi_assistant_id"], old["instructions"]),
        ).fetchone()
        return {"id": created["id"], "merged": False}


def record_dispatcher_run(ok: bool, started: int) -> None:
    """Remember when the dispatcher last ran (one-row table, for the staff page)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO dispatcher_status (id, last_run_at, last_run_ok, last_started) VALUES (1, now(), %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET last_run_at = excluded.last_run_at, "
            "  last_run_ok = excluded.last_run_ok, last_started = excluded.last_started",
            (ok, started),
        )


def insert_team_notification(reason: str, caller_phone: str | None, contact_name: str | None,
                             organization: str | None, summary: str | None, vapi_call_id: str | None) -> dict:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO team_notifications (reason, caller_phone, contact_name, organization, summary, vapi_call_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at",
            (reason, caller_phone, contact_name, organization, summary, vapi_call_id),
        ).fetchone()


def claim_unsent_notifications(limit: int, max_age_hours: int, claim_minutes: int) -> list[dict]:
    """Lease up to `limit` recent unsent notifications, oldest first.

    An expired lease can be claimed again after a dispatcher crash. `emailed_at` is only set after
    SMTP succeeds, so a claimed row is never mistaken for a delivered one.
    """
    with connect() as conn:
        # UPDATE ... RETURNING has no order of its own: sort the claimed rows afterwards.
        return conn.execute(
            "WITH claimed AS ("
            "  UPDATE team_notifications SET email_claimed_at = now() WHERE id IN ("
            "    SELECT id FROM team_notifications "
            "    WHERE emailed_at IS NULL AND ("
            "      (email_claimed_at IS NULL AND created_at > now() - make_interval(hours => %s)) "
            "      OR email_claimed_at < now() - make_interval(mins => %s)) "
            "    ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED) "
            "  RETURNING id, created_at, reason, caller_phone, contact_name, organization, summary, "
            "    vapi_call_id, email_claimed_at) "
            "SELECT * FROM claimed ORDER BY created_at",
            (max_age_hours, claim_minutes, limit),
        ).fetchall()


def mark_notification_emailed(notification_id, claimed_at) -> bool:
    """Mark a successfully emailed notification sent if this caller still owns its lease."""
    with connect() as conn:
        return conn.execute(
            "UPDATE team_notifications SET emailed_at = now(), email_claimed_at = NULL "
            "WHERE id = %s AND emailed_at IS NULL AND email_claimed_at = %s",
            (notification_id, claimed_at),
        ).rowcount == 1


def release_notification(notification_id, claimed_at) -> bool:
    """Release a failed send if this caller still owns its lease, allowing an immediate retry."""
    with connect() as conn:
        return conn.execute(
            "UPDATE team_notifications SET email_claimed_at = NULL "
            "WHERE id = %s AND emailed_at IS NULL AND email_claimed_at = %s",
            (notification_id, claimed_at),
        ).rowcount == 1


def get_dispatcher_status() -> dict | None:
    with connect() as conn:
        return conn.execute(
            "SELECT last_run_at, last_run_ok, last_started FROM dispatcher_status WHERE id = 1"
        ).fetchone()


def pending_callbacks(customer_id) -> list[dict]:
    """A customer's pending callbacks (not the one currently calling), soonest first."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, due_at, instructions FROM scheduled_calls "
            "WHERE customer_id = %s AND status = 'pending' ORDER BY due_at",
            (customer_id,),
        ).fetchall()


# --- Call archive ----------------------------------------------------------

ARCHIVE_CALL_FIELDS = (
    "vapi_call_id", "attempt_id", "scheduled_call_id", "customer_id", "remote_phone", "name_snapshot", "direction",
    "provider_type", "vapi_assistant_id", "vapi_phone_number_id", "provider_created_at", "provider_updated_at",
    "started_at", "ended_at", "duration_seconds", "provider_status", "ended_reason", "cost", "currency",
    "transcript", "messages", "transcript_state", "audio_state", "audio_object_key", "audio_media_type",
    "audio_bytes", "audio_sha256", "archived_at",
)
ARCHIVE_TRANSCRIPT_FIELDS = ("transcript", "messages", "transcript_state")
ARCHIVE_AUDIO_FIELDS = ("audio_state", "audio_object_key", "audio_media_type", "audio_bytes", "audio_sha256", "archived_at")


def _archive_lock(conn: psycopg.Connection) -> None:
    """Serialize archive identity writes with the lock reserved for deletion coordination."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (config.ARCHIVE_LOCK_ID,))


def _purge_customer_archive(conn: psycopg.Connection, customer_id: UUID | str, phone: str) -> None:
    """Delete a customer's archived calls and keep later scans from importing them again.

    Args:
        conn: Transaction holding the archive advisory lock and the customer row lock.
        customer_id: Customer being deleted.
        phone: Their normalized number; calls from it up to now count as theirs, linked or not.

    Note:
        Writes a phone/cutoff marker plus markers for every known Vapi call ID, queues their
        recordings for object cleanup and deletes the records (notes and aliases cascade).
        Calls from the same number after the cutoff are archived again.
    """
    match = "(customer_id = %s OR remote_phone = %s)"
    conn.execute("INSERT INTO call_archive_deletions (remote_phone, cutoff_at) VALUES (%s, now()) ON CONFLICT DO NOTHING", (phone,))
    conn.execute(f"INSERT INTO call_archive_deletions (vapi_call_id) SELECT vapi_call_id FROM call_records WHERE {match} "
                 "AND vapi_call_id IS NOT NULL UNION SELECT vapi_call_id FROM call_attempts WHERE customer_id = %s "
                 "AND vapi_call_id IS NOT NULL ON CONFLICT DO NOTHING", (customer_id, phone, customer_id))
    conn.execute(f"INSERT INTO call_audio_cleanup (object_key) SELECT audio_object_key FROM call_records WHERE {match} "
                 "AND audio_object_key IS NOT NULL ON CONFLICT DO NOTHING", (customer_id, phone))
    conn.execute(f"DELETE FROM call_records WHERE {match}", (customer_id, phone))


def _insert_attempt_call_record(conn: psycopg.Connection, attempt: dict) -> UUID:
    """Insert an outbound identity from the already locked local attempt."""
    return conn.execute(
        "INSERT INTO call_records (attempt_id, vapi_call_id, scheduled_call_id, customer_id, remote_phone, direction, "
        "started_at, ended_at, ended_reason, vapi_assistant_id, vapi_phone_number_id) "
        "VALUES (%s, %s, %s, %s, %s, 'outbound', %s, %s, %s, %s, %s) RETURNING id",
        (attempt["id"], attempt["vapi_call_id"], attempt["scheduled_call_id"], attempt["customer_id"], attempt["customer_phone"],
         attempt["started_at"], attempt["ended_at"], attempt["ended_reason"],
         attempt["vapi_assistant_id"], attempt["vapi_phone_number_id"]),
    ).fetchone()["id"]


def ensure_attempt_call_record(attempt_id: UUID | str) -> UUID | None:
    """Resolve a durable call identity before a future note is accepted.

    Args:
        attempt_id: Local outbound attempt whose call record is needed.

    Returns:
        Existing or newly created record UUID, or None when the attempt is gone.
        The transaction lock prevents racing discovery from creating another identity.
    """
    with connect() as conn:
        _archive_lock(conn)
        attempt = conn.execute("SELECT * FROM call_attempts WHERE id = %s FOR SHARE", (attempt_id,)).fetchone()
        if not attempt:
            return None
        record = conn.execute("SELECT id FROM call_records WHERE attempt_id = %s OR "
                              "(vapi_call_id IS NOT NULL AND vapi_call_id = %s) ORDER BY attempt_id NULLS LAST LIMIT 1",
                              (attempt_id, attempt["vapi_call_id"])).fetchone()
        if not record:
            return _insert_attempt_call_record(conn, attempt)
        conn.execute("UPDATE call_records SET attempt_id = %s, scheduled_call_id = %s, customer_id = %s, updated_at = now() "
                     "WHERE id = %s", (attempt_id, attempt["scheduled_call_id"], attempt["customer_id"], record["id"]))
        return record["id"]


def _archive_call_suppressed(conn: psycopg.Connection, data: dict) -> bool:
    """Check persistent provider-ID and phone/cutoff deletion markers before writing."""
    return conn.execute(
        "SELECT 1 FROM call_archive_deletions WHERE vapi_call_id = %s OR "
        "(remote_phone = %s AND cutoff_at >= %s) LIMIT 1",
        (data["vapi_call_id"], data["remote_phone"], data["provider_created_at"]),
    ).fetchone() is not None


def _archive_matches(conn: psycopg.Connection, call_id: str) -> tuple[dict | None, list[dict]]:
    """Lock matching identities, preferring the provider record as the merge survivor."""
    attempt = conn.execute("SELECT * FROM call_attempts WHERE vapi_call_id = %s FOR SHARE", (call_id,)).fetchone()
    records = conn.execute("SELECT * FROM call_records WHERE vapi_call_id = %s OR attempt_id = %s FOR UPDATE",
                           (call_id, attempt["id"] if attempt else None)).fetchall()
    records.sort(key=lambda row: row["vapi_call_id"] != call_id)
    return attempt, records


def _merge_archive_fields(existing: dict, source: dict) -> dict:
    """Combine stored fields without losing an already archived transcript or recording."""
    merged = dict(existing)
    for key, value in source.items():
        if merged.get(key) is None:
            merged[key] = value
    for state, fields in (("transcript_state", ARCHIVE_TRANSCRIPT_FIELDS), ("audio_state", ARCHIVE_AUDIO_FIELDS)):
        if existing.get(state) != "ready" and source.get(state) == "ready":
            for key in fields:
                merged[key] = source[key]
    return merged


def _merge_archive_identity(conn: psycopg.Connection, existing: dict, source: dict, merged: dict) -> None:
    """Move notes and aliases before removing a duplicate identity in the caller's transaction.

    Args:
        conn: Connection holding the archive advisory lock and both record row locks.
        existing: Provider identity that survives the merge.
        source: Duplicate local identity being removed.
        merged: Combined fields, including the chosen audio object if either has one.

    Note:
        A redundant recording is queued for cleanup. Clearing unique links on the
        source allows the survivor to acquire them without violating constraints.
    """
    if source["audio_object_key"] and source["audio_object_key"] != merged["audio_object_key"]:
        conn.execute("INSERT INTO call_audio_cleanup (object_key) VALUES (%s) ON CONFLICT DO NOTHING", (source["audio_object_key"],))
    conn.execute("UPDATE call_records SET attempt_id = NULL, audio_object_key = NULL, audio_state = 'pending' WHERE id = %s",
                 (source["id"],))
    conn.execute("UPDATE call_notes SET call_record_id = %s WHERE call_record_id = %s", (existing["id"], source["id"]))
    conn.execute("UPDATE call_record_aliases SET call_record_id = %s WHERE call_record_id = %s", (existing["id"], source["id"]))
    conn.execute("DELETE FROM call_records WHERE id = %s", (source["id"],))
    conn.execute("INSERT INTO call_record_aliases (alias_id, call_record_id) VALUES (%s, %s)", (source["id"], existing["id"]))


def _archive_update_allowed(key: str, value: object, data: dict, merged: dict, existing: dict) -> bool:
    """Reject empty or partial artifact updates and regressions from ended status."""
    if value is None or (key == "messages" and not value):
        return False
    if key in ARCHIVE_TRANSCRIPT_FIELDS:
        if merged.get("transcript_state") == "ready" and data["transcript_state"] != "ready":
            return False
        if len(data.get("messages", [])) < len(merged.get("messages", [])):
            return False
    if key == "audio_state" and merged.get("audio_state") == "ready":
        return False
    if key in ("transcript_state", "audio_state") and value == "pending" and merged.get(key) not in (None, "pending"):
        return False  # a finalized artifact (empty, unavailable) is never queued again by a later scan
    return key != "provider_status" or existing.get("provider_status") != "ended" or value == "ended"


def _apply_archive_metadata(merged: dict, data: dict, existing: dict) -> None:
    """Apply newer provider metadata while preserving complete stored artifacts."""
    updated = existing.get("provider_updated_at")
    if updated and data.get("provider_updated_at") and data["provider_updated_at"] < updated:
        return
    for key, value in data.items():
        if _archive_update_allowed(key, value, data, merged, existing):
            merged[key] = value


def _link_archive_customer(conn: psycopg.Connection, merged: dict, data: dict, attempt: dict | None, existing: dict) -> None:
    """Attach known local links and a name snapshot while keeping the customer FK safe."""
    if attempt:
        merged.update(attempt_id=attempt["id"], scheduled_call_id=attempt["scheduled_call_id"], customer_id=attempt["customer_id"])
    # Keep this lookup on the caller's transaction; get_customer_by_phone opens a
    # different connection and cannot hold the required key-share lock for us.
    customer = conn.execute("SELECT id, first_name, middle_name, last_name FROM customers WHERE phone = %s FOR KEY SHARE",
                            (data["remote_phone"],)).fetchone()
    if customer:
        merged["customer_id"] = customer["id"]
        if not existing.get("name_snapshot"):
            merged["name_snapshot"] = " ".join(customer[key] for key in ("first_name", "middle_name", "last_name") if customer[key])


def _write_archive_call(conn: psycopg.Connection, merged: dict, record_id: UUID | None) -> UUID:
    """Persist the fixed archive field allowlist and mark the provider poll time."""
    values = [Jsonb(merged.get(key, [])) if key == "messages" else merged.get(key) for key in ARCHIVE_CALL_FIELDS]
    if record_id is not None:
        assignments = ", ".join(f"{key} = %s" for key in ARCHIVE_CALL_FIELDS)
        return conn.execute(f"UPDATE call_records SET {assignments}, last_polled_at = now(), updated_at = now() "
                            "WHERE id = %s RETURNING id", (*values, record_id)).fetchone()["id"]
    return conn.execute(f"INSERT INTO call_records ({', '.join(ARCHIVE_CALL_FIELDS)}, last_polled_at) "
                        f"VALUES ({', '.join(['%s'] * len(ARCHIVE_CALL_FIELDS))}, now()) RETURNING id", values).fetchone()["id"]


def save_archive_call(data: dict) -> UUID | None:
    """Upsert filtered provider data and merge identities in one transaction.

    Args:
        data: Validated metadata and spoken fields returned by archive.call_data.

    Returns:
        Canonical record UUID, or None when a tombstone or deleted record blocks it.
        Existing notes, aliases and complete audio/transcripts survive identity merges.

    Raises:
        psycopg.Error: A database operation fails; the whole merge is rolled back.
    """
    with connect() as conn:
        return _save_archive_call(conn, data)


def save_archive_calls(rows: list[dict]) -> int:
    """Save a scan window's calls over one connection, each in its own transaction (like save_archive_call).

    Returns:
        Number of calls saved (not blocked by deletion markers). Calls saved before a failure stay saved.
    """
    saved = 0
    with connect() as conn:
        for data in rows:
            with conn.transaction():
                saved += _save_archive_call(conn, data) is not None
    return saved


def _save_archive_call(conn: psycopg.Connection, data: dict) -> UUID | None:
    """Upsert and merge one call inside the caller's transaction, holding the archive lock until it ends."""
    _archive_lock(conn)
    if _archive_call_suppressed(conn, data):
        return None
    attempt, records = _archive_matches(conn, data["vapi_call_id"])
    if any(row["deleted_at"] for row in records):
        return None
    existing = records[0] if records else {}
    source = records[1] if len(records) == 2 else {}
    merged = _merge_archive_fields(existing, source)
    if source:
        _merge_archive_identity(conn, existing, source, merged)
    _apply_archive_metadata(merged, data, existing)
    _link_archive_customer(conn, merged, data, attempt, existing)
    return _write_archive_call(conn, merged, existing.get("id"))


def _recent_window(row: dict, now: datetime) -> tuple[datetime, datetime]:
    """Select a new recent window, overlapping the completed watermark."""
    if not row["completed_through"]:
        return now - timedelta(minutes=config.ARCHIVE_INITIAL_LOOKBACK_MINUTES), now
    return row["completed_through"] - timedelta(minutes=config.ARCHIVE_SCAN_OVERLAP_MINUTES), now


def _reconcile_window(row: dict, now: datetime) -> tuple[datetime, datetime]:
    """Select the next oldest-first window of the retention sweep, restarting once it reaches recent scans."""
    oldest = now - timedelta(days=config.ARCHIVE_PROVIDER_RETENTION_DAYS)
    newest = now - timedelta(minutes=config.ARCHIVE_INITIAL_LOOKBACK_MINUTES)  # the recent scan covers the rest
    done = row["completed_through"]
    start = max(done - timedelta(minutes=config.ARCHIVE_SCAN_OVERLAP_MINUTES), oldest) if done and done < newest else oldest
    return start, min(start + timedelta(hours=config.ARCHIVE_RECONCILE_WINDOW_HOURS), newest)


def _archive_scan_window(row: dict, now: datetime) -> tuple[datetime, datetime, list[list[str]]]:
    """Resume pending subdivisions or select the stream's next root window."""
    if row["window_start"]:
        return row["window_start"], row["window_end"], row["pending_windows"]
    start, end = (_reconcile_window if row["stream"] == "reconcile" else _recent_window)(row, now)
    return start, end, [[start.isoformat(), end.isoformat()]]


def claim_archive_scan(now: datetime, stream: str = "recent") -> dict | None:
    """Lease a discovery stream, resuming an interrupted root window when necessary.

    Args:
        now: Current UTC time, rounded down to provider query millisecond precision.
        stream: "recent" (new calls) or "reconcile" (oldest-first retention sweep).

    Returns:
        Scan state with a fresh ownership token, or None while another lease is live.
        Expired ownership is replaced under the scan row lock.
    """
    now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    with connect() as conn:
        conn.execute("INSERT INTO call_archive_scans (stream) VALUES (%s) ON CONFLICT DO NOTHING", (stream,))
        row = conn.execute("SELECT * FROM call_archive_scans WHERE stream = %s FOR UPDATE", (stream,)).fetchone()
        if row["claimed_at"] and row["claimed_at"] > now - timedelta(minutes=config.ARCHIVE_SCAN_LEASE_MINUTES):
            return None
        start, end, windows = _archive_scan_window(row, now)
        return conn.execute(
            "UPDATE call_archive_scans SET window_start = %s, window_end = %s, pending_windows = %s, "
            "claim_token = gen_random_uuid(), claimed_at = %s, last_started_at = %s, updated_at = now() "
            "WHERE stream = %s RETURNING *", (start, end, Jsonb(windows), now, now, stream),
        ).fetchone()


def save_archive_scan_progress(token: UUID, windows: list[list[str]]) -> bool:
    """Persist remaining scan windows only while the caller still owns the lease."""
    with connect() as conn:
        return conn.execute("UPDATE call_archive_scans SET pending_windows = %s, updated_at = now() "
                            "WHERE claim_token = %s", (Jsonb(windows), token)).rowcount == 1


def finish_archive_scan(token: UUID, error: str | None) -> bool:
    """Release ownership and advance the watermark only for a fully saved root window.

    Args:
        token: Ownership token issued when the scan was claimed.
        error: Generic failure category, or None for a run without errors.

    Returns:
        Whether the caller still owned the lease and finalized it. Completed discovery
        remains checkpointed even if a separate known-call refresh failed afterward.
    """
    with connect() as conn:
        return conn.execute(
            "UPDATE call_archive_scans SET "
            "completed_through = CASE WHEN pending_windows = '[]'::jsonb THEN window_end ELSE completed_through END, "
            "window_start = CASE WHEN pending_windows = '[]'::jsonb THEN NULL ELSE window_start END, "
            "window_end = CASE WHEN pending_windows = '[]'::jsonb THEN NULL ELSE window_end END, "
            "last_succeeded_at = CASE WHEN %s::text IS NULL AND pending_windows = '[]'::jsonb THEN now() ELSE last_succeeded_at END, "
            "consecutive_failures = CASE WHEN %s::text IS NULL THEN 0 ELSE consecutive_failures + 1 END, "
            "last_error_code = %s, claimed_at = NULL, claim_token = NULL, updated_at = now() "
            "WHERE claim_token = %s", (error, error, error, token),
        ).rowcount == 1


def archive_calls_to_refresh(limit: int) -> list[str]:
    """Select eligible known calls fairly, giving active calls priority over ended ones."""
    with connect() as conn:
        return [row["vapi_call_id"] for row in conn.execute(
            "SELECT vapi_call_id FROM call_records WHERE deleted_at IS NULL AND vapi_call_id IS NOT NULL "
            "AND (provider_status IS DISTINCT FROM 'ended' OR transcript_state = 'pending' "
            "OR (attempt_id IS NULL AND EXISTS (SELECT 1 FROM call_attempts a WHERE a.vapi_call_id = call_records.vapi_call_id))) "
            "AND (last_polled_at IS NULL OR last_polled_at < now() - make_interval(secs => %s)) "
            "ORDER BY CASE WHEN provider_status = 'ended' THEN 1 ELSE 0 END, last_polled_at NULLS FIRST, id LIMIT %s",
            (config.ARCHIVE_REFRESH_SECONDS, limit),
        ).fetchall()]


def mark_archive_poll_failed(call_id: str) -> None:
    """Rotate a failed provider read out of the next refresh batch."""
    with connect() as conn:
        conn.execute("UPDATE call_records SET last_polled_at = now(), last_error_code = 'provider_refresh_failed' "
                     "WHERE vapi_call_id = %s", (call_id,))


def claim_archive_audio() -> dict | None:
    """Lease the next due recording of an ended call, taking over claims of crashed runs."""
    with connect() as conn:
        return conn.execute(
            "UPDATE call_records SET archive_claimed_at = now(), archive_claim_token = gen_random_uuid(), updated_at = now() "
            "WHERE id = (SELECT id FROM call_records WHERE deleted_at IS NULL AND audio_state = 'pending' "
            "AND vapi_call_id IS NOT NULL AND provider_status = 'ended' AND next_archive_at <= now() "
            "AND (archive_claimed_at IS NULL OR archive_claimed_at < now() - make_interval(mins => %s)) "
            "ORDER BY next_archive_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, vapi_call_id, archive_claim_token", (config.ARCHIVE_AUDIO_CLAIM_MINUTES,),
        ).fetchone()


def _mark_archive_audio_ready(conn: psycopg.Connection, record_id: UUID, fields: dict) -> None:
    """Save the uploaded recording's fields and release its claim."""
    conn.execute(
        "UPDATE call_records SET audio_state = 'ready', audio_object_key = %s, audio_media_type = %s, audio_bytes = %s, "
        "audio_sha256 = %s, archived_at = now(), archive_attempts = archive_attempts + 1, archive_claimed_at = NULL, "
        "archive_claim_token = NULL, last_error_code = NULL, updated_at = now() WHERE id = %s",
        (fields["audio_object_key"], fields["audio_media_type"], fields["audio_bytes"], fields["audio_sha256"], record_id),
    )


def _discard_archive_audio(conn: psycopg.Connection, object_key: str) -> None:
    """Queue an uploaded object for cleanup unless a call record already references it."""
    conn.execute("INSERT INTO call_audio_cleanup (object_key) SELECT %s WHERE NOT EXISTS "
                 "(SELECT 1 FROM call_records WHERE audio_object_key = %s) ON CONFLICT DO NOTHING", (object_key, object_key))


def finish_archive_audio(record_id: UUID, token: UUID, fields: dict) -> bool:
    """Mark an uploaded recording ready if the claim is still ours and the call may be kept.

    Args:
        record_id: Claimed call record.
        token: Claim token returned by `claim_archive_audio`.
        fields: Ready audio fields from `archive.transfer_audio`.

    Returns:
        Whether the recording was saved. A record that is gone (customer deleted, merged away)
        queues the object for cleanup. A deleted or deletion-marked record is released as
        unavailable and its object queued too. When another run took the claim over, nothing is
        queued: it uploads to the same key, so cleanup could delete the object it is about to save.
    """
    with connect() as conn:
        _archive_lock(conn)
        row = conn.execute("SELECT * FROM call_records WHERE id = %s FOR UPDATE", (record_id,)).fetchone()
        if row and row["archive_claim_token"] != token:
            return False
        if row and not row["deleted_at"] and not _archive_call_suppressed(conn, row):
            _mark_archive_audio_ready(conn, record_id, fields)
            return True
        if row:
            conn.execute("UPDATE call_records SET audio_state = 'unavailable', archived_at = now(), archive_claimed_at = NULL, "
                         "archive_claim_token = NULL, updated_at = now() WHERE id = %s", (record_id,))
        _discard_archive_audio(conn, fields["audio_object_key"])
        return False


def retry_archive_audio(record_id: UUID, token: UUID, error: str, final: bool = False) -> bool:
    """Release a failed recording claim with exponential backoff, giving up a day after the call was archived.

    Args:
        record_id: Claimed call record.
        token: Claim token returned by `claim_archive_audio`.
        error: Generic failure category, saved as `last_error_code`.
        final: Mark the recording unavailable now (e.g. it is too large to ever fit).

    Returns:
        Whether the claim was still ours.
    """
    with connect() as conn:
        return conn.execute(
            "UPDATE call_records r SET archive_attempts = r.archive_attempts + 1, last_error_code = %s, "
            "next_archive_at = now() + make_interval(mins => least(power(2, least(r.archive_attempts, 10))::int, %s)), "
            "audio_state = CASE WHEN g.give_up THEN 'unavailable' ELSE r.audio_state END, "
            "archived_at = CASE WHEN g.give_up THEN now() ELSE r.archived_at END, "
            "archive_claimed_at = NULL, archive_claim_token = NULL, updated_at = now() "
            "FROM (SELECT %s::boolean OR created_at < now() - make_interval(hours => %s) AS give_up "
            "FROM call_records WHERE id = %s) g "
            "WHERE r.id = %s AND r.archive_claim_token = %s",
            (error, config.ARCHIVE_AUDIO_RETRY_MAX_MINUTES, final, config.ARCHIVE_AUDIO_GIVE_UP_HOURS, record_id, record_id, token),
        ).rowcount == 1


def finalize_archive_transcripts() -> int:
    """Mark ended calls that still have no transcript after the wait as empty; returns the count."""
    with connect() as conn:
        return conn.execute(
            "UPDATE call_records SET transcript_state = 'empty', updated_at = now() WHERE deleted_at IS NULL "
            "AND transcript_state = 'pending' AND provider_status = 'ended' "
            "AND coalesce(ended_at, provider_updated_at, provider_created_at, created_at) < now() - make_interval(mins => %s)",
            (config.ARCHIVE_TRANSCRIPT_WAIT_MINUTES,),
        ).rowcount


def claim_audio_cleanup() -> dict | None:
    """Lease the next due object deletion, first dropping jobs for keys a call record still uses."""
    with connect() as conn:
        conn.execute("DELETE FROM call_audio_cleanup c WHERE EXISTS "
                     "(SELECT 1 FROM call_records r WHERE r.audio_object_key = c.object_key)")
        return conn.execute(
            "UPDATE call_audio_cleanup SET claimed_at = now(), claim_token = gen_random_uuid() "
            "WHERE object_key = (SELECT object_key FROM call_audio_cleanup WHERE next_attempt_at <= now() "
            "AND (claimed_at IS NULL OR claimed_at < now() - make_interval(mins => %s)) "
            "ORDER BY next_attempt_at, object_key LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING object_key, claim_token", (config.ARCHIVE_AUDIO_CLAIM_MINUTES,),
        ).fetchone()


def finish_audio_cleanup(object_key: str, token: UUID) -> bool:
    """Remove a cleanup job once its object is deleted, if the claim is still ours."""
    with connect() as conn:
        return conn.execute("DELETE FROM call_audio_cleanup WHERE object_key = %s AND claim_token = %s",
                            (object_key, token)).rowcount == 1


def retry_audio_cleanup(object_key: str, token: UUID, error: str) -> bool:
    """Release a failed object deletion with exponential backoff; it is retried until it succeeds."""
    with connect() as conn:
        return conn.execute(
            "UPDATE call_audio_cleanup SET attempts = attempts + 1, last_error_code = %s, claimed_at = NULL, claim_token = NULL, "
            "next_attempt_at = now() + make_interval(mins => least(power(2, least(attempts, 10))::int, %s)) "
            "WHERE object_key = %s AND claim_token = %s",
            (error, config.ARCHIVE_AUDIO_RETRY_MAX_MINUTES, object_key, token),
        ).rowcount == 1


def get_archive_status() -> dict:
    """Return recent/reconcile scan diagnostics and aggregate audio counts without call content."""
    with connect() as conn:
        scans = {row.pop("stream"): json_row(row) for row in conn.execute(
            "SELECT stream, completed_through, last_started_at, last_succeeded_at, consecutive_failures, "
            "last_error_code, jsonb_array_length(pending_windows) AS pending_windows FROM call_archive_scans").fetchall()}
        counts = conn.execute("SELECT count(*) AS calls, count(*) FILTER (WHERE audio_state = 'pending') AS pending_audio, "
                              "count(*) FILTER (WHERE audio_state = 'pending' AND archive_attempts > 0) AS retrying_audio, "
                              "count(*) FILTER (WHERE audio_state = 'unavailable') AS unavailable_audio, "
                              "min(created_at) FILTER (WHERE audio_state = 'pending') AS oldest_pending_at, "
                              "(SELECT count(*) FROM call_audio_cleanup) AS pending_cleanup "
                              "FROM call_records WHERE deleted_at IS NULL").fetchone()
        return {"scan": scans.get("recent"), "reconcile": scans.get("reconcile"), **json_row(counts)}


# --- Calls list --------------------------------------------------------------

_CALL_NAME_SQL = "nullif(concat_ws(' ', c.first_name, c.middle_name, c.last_name), '')"
# Lifecycle of an actual call; {t} is the table alias, {ended} its "has ended" condition.
_CALL_OUTCOME_SQL = ("CASE WHEN {t}.ended_reason = 'voicemail' THEN 'voicemail' "
                     "WHEN {t}.ended_reason = ANY(%(no_answer)s) THEN 'no_answer' "
                     "WHEN {t}.ended_reason ~ '(error|failed|fault)' THEN 'failed' "
                     "WHEN {t}.ended_reason IS NOT NULL OR {ended} THEN 'completed' ELSE 'calling' END")
_RECORD_OUTCOME_SQL = _CALL_OUTCOME_SQL.format(t="r", ended="(r.ended_at IS NOT NULL OR r.provider_status = 'ended')")
_ATTEMPT_OUTCOME_SQL = _CALL_OUTCOME_SQL.format(t="a", ended="a.ended_at IS NOT NULL")
# One row per archived call, per local attempt not archived yet, and per upcoming or never-dialled
# cancelled callback. Callbacks being dialled have status 'calling', so only their attempt shows.
_CALL_ENTRIES_SQL = (
    f"SELECT 'call:' || r.id AS id, 'call' AS kind, coalesce(r.remote_phone, c.phone) AS phone, "
    f"coalesce({_CALL_NAME_SQL}, r.name_snapshot) AS name, coalesce(r.started_at, r.provider_created_at, r.created_at) AS call_date, "
    f"{_RECORD_OUTCOME_SQL} AS status, "
    "r.direction, r.duration_seconds::float8 AS duration_seconds, r.ended_reason, r.transcript_state, r.audio_state, "
    "(SELECT count(*) FROM call_notes n WHERE n.call_record_id = r.id) AS note_count "
    "FROM call_records r LEFT JOIN customers c ON c.id = r.customer_id WHERE r.deleted_at IS NULL "
    f"UNION ALL SELECT 'attempt:' || a.id, 'attempt', coalesce(a.customer_phone, c.phone), {_CALL_NAME_SQL}, a.started_at, "
    f"{_ATTEMPT_OUTCOME_SQL}, 'outbound', "
    "extract(epoch FROM a.ended_at - a.started_at)::float8, a.ended_reason, NULL, NULL, 0 "
    "FROM call_attempts a LEFT JOIN customers c ON c.id = a.customer_id WHERE NOT EXISTS ("
    "SELECT 1 FROM call_records r WHERE r.attempt_id = a.id OR r.vapi_call_id = a.vapi_call_id) "
    f"UNION ALL SELECT 'scheduled:' || s.id, 'scheduled', c.phone, {_CALL_NAME_SQL}, s.due_at, "
    "CASE s.status WHEN 'pending' THEN 'scheduled' ELSE 'cancelled' END, 'outbound', NULL, NULL, NULL, NULL, "
    "(SELECT count(*) FROM call_notes n WHERE n.scheduled_call_id = s.id) "
    "FROM scheduled_calls s JOIN customers c ON c.id = s.customer_id WHERE s.status = 'pending' "
    "OR (s.status = 'cancelled' AND NOT EXISTS (SELECT 1 FROM call_attempts a WHERE a.scheduled_call_id = s.id))"
)
CALL_SORT_COLUMNS = {"call_date": "call_date", "name": "lower(name)", "number": "phone", "status": "status",
                     "direction": "direction", "duration": "duration_seconds"}


def _like(text: str) -> str:
    """Contains-pattern for LIKE/ILIKE with the user's wildcards escaped."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_calls(search: str, statuses: list[str], sort: str, descending: bool, limit: int, offset: int) -> dict:
    """Return one page of the combined Calls list and the total number of matching entries.

    Args:
        search: Name text or phone digits (formatting ignored); empty matches everything.
        statuses: Lifecycle statuses to include (config.CALL_STATUSES); empty includes all.
        sort: Key of CALL_SORT_COLUMNS; ties are ordered by entry ID, so pages are stable.
        descending: Sort direction; missing values (unknown name, duration) always come last.
        limit: Page size.
        offset: Number of entries to skip.

    Returns:
        {"calls": [...], "total": n}. Entry IDs are typed ("call:", "attempt:", "scheduled:" + UUID);
        dates are ISO UTC strings. Call content (transcripts, notes) isn't included.
    """
    digits = "".join(ch for ch in search if ch.isdigit())
    order = "DESC" if descending else "ASC"
    params = {"no_answer": list(config.CALL_NO_ANSWER_REASONS), "statuses": statuses or None, "limit": limit,
              "offset": offset, "name": _like(search) if search else None, "digits": _like(digits) if digits else None}
    with connect() as conn:
        rows = conn.execute(
            f"SELECT *, count(*) OVER () AS total FROM ({_CALL_ENTRIES_SQL}) e "
            "WHERE (%(statuses)s::text[] IS NULL OR status = ANY(%(statuses)s)) "
            "AND (%(name)s::text IS NULL OR name ILIKE %(name)s OR replace(phone, '+', '') LIKE %(digits)s) "
            f"ORDER BY {CALL_SORT_COLUMNS[sort]} {order} NULLS LAST, id {order} LIMIT %(limit)s OFFSET %(offset)s",
            params).fetchall()
    total = rows[0]["total"] if rows else 0
    return {"calls": [json_row({k: v for k, v in row.items() if k != "total"}) for row in rows], "total": total}


def _resolve_call_entry(conn: psycopg.Connection, kind: str, entry_uuid: UUID) -> str:
    """Map an entry ID to the one the Calls list shows now: merged records via aliases, archived attempts to their record."""
    if kind == "call":
        alias = conn.execute("SELECT call_record_id FROM call_record_aliases WHERE alias_id = %s", (entry_uuid,)).fetchone()
        return f"call:{alias['call_record_id'] if alias else entry_uuid}"
    if kind == "attempt":
        record = conn.execute("SELECT r.id FROM call_records r JOIN call_attempts a ON a.id = %s "
                              "WHERE r.attempt_id = a.id OR r.vapi_call_id = a.vapi_call_id "
                              "ORDER BY r.attempt_id NULLS LAST LIMIT 1", (entry_uuid,)).fetchone()
        if record:
            return f"call:{record['id']}"
    return f"{kind}:{entry_uuid}"


def _call_notes(conn: psycopg.Connection, record_id: UUID | None, scheduled_call_id: UUID | None) -> list[dict]:
    """Staff notes on a call record and on its callback, oldest first, each labelled with its target."""
    rows = conn.execute(
        "SELECT id, author_label, body, created_at, CASE WHEN call_record_id IS NULL THEN 'callback' ELSE 'call' END AS target "
        "FROM call_notes WHERE call_record_id = %s OR scheduled_call_id = %s ORDER BY created_at, id",
        (record_id, scheduled_call_id)).fetchall()
    return [json_row(row) for row in rows]


def _call_details(conn: psycopg.Connection, kind: str, entry_uuid: str) -> dict:
    """Times, transcript or schedule details and notes for one resolved entry."""
    if kind == "call":
        details = conn.execute("SELECT started_at, ended_at, transcript, messages, scheduled_call_id "
                               "FROM call_records WHERE id = %s", (entry_uuid,)).fetchone()
        return {**details, "notes": _call_notes(conn, entry_uuid, details["scheduled_call_id"])}
    if kind == "attempt":
        details = conn.execute("SELECT started_at, ended_at, scheduled_call_id FROM call_attempts WHERE id = %s",
                               (entry_uuid,)).fetchone()
        return {**details, "transcript": None, "messages": [], "notes": _call_notes(conn, None, details["scheduled_call_id"])}
    details = conn.execute("SELECT local_time, instructions, id AS scheduled_call_id FROM scheduled_calls WHERE id = %s",
                           (entry_uuid,)).fetchone()
    return {**details, "transcript": None, "messages": [], "notes": _call_notes(conn, None, entry_uuid)}


def get_call(kind: str, entry_uuid: UUID) -> dict | None:
    """Return one Calls list entry with its transcript, spoken messages, schedule details and notes.

    Args:
        kind: "call", "attempt" or "scheduled" (the prefix of the entry ID).
        entry_uuid: UUID part of the entry ID. Old IDs of merged records and attempts archived since
            the list was loaded resolve to the current entry.

    Returns:
        The list fields plus details (`id` is the current entry ID), or None when the entry is gone,
        deleted or no longer listed (e.g. a completed callback).
    """
    with connect() as conn:
        entry_id = _resolve_call_entry(conn, kind, entry_uuid)
        row = conn.execute(f"SELECT * FROM ({_CALL_ENTRIES_SQL}) e WHERE id = %(id)s",
                           {"no_answer": list(config.CALL_NO_ANSWER_REASONS), "id": entry_id}).fetchone()
        if not row:
            return None
        kind, entry_uuid = entry_id.split(":", 1)
        return json_row({**row, **_call_details(conn, kind, entry_uuid)})


def call_audio_key(kind: str, entry_uuid: UUID) -> str | None:
    """Object key of a listed call's saved recording, or None when there is none (yet) or the call is deleted."""
    with connect() as conn:
        entry_id = _resolve_call_entry(conn, kind, entry_uuid)
        if not entry_id.startswith("call:"):
            return None
        row = conn.execute("SELECT audio_object_key FROM call_records WHERE id = %s AND deleted_at IS NULL "
                           "AND audio_state = 'ready'", (entry_id.split(":", 1)[1],)).fetchone()
        return row["audio_object_key"] if row else None


def add_call_note(kind: str, entry_uuid: UUID, author_id: str, author_label: str, body: str) -> dict | None:
    """Append a staff note to a call or a callback.

    Args:
        kind: "call", "attempt" or "scheduled" (the prefix of the entry ID). A note on an attempt
            first gets the attempt a durable call record (ensure_attempt_call_record), so the note
            survives dispatcher recovery and later discovery merges.
        entry_uuid: UUID part of the entry ID; old IDs resolve like in get_call.
        author_id: Verified staff user ID.
        author_label: Name or email shown with the note.
        body: Nonblank text of at most config.CALL_NOTE_MAX_CHARS characters.

    Returns:
        {"entry_id": current entry ID, "note": the stored note}, or None when the target is gone or deleted.
        The target row is key-share locked, so a concurrent deletion waits and then removes the note too.
    """
    if kind == "attempt":
        record = ensure_attempt_call_record(entry_uuid)
        if record is None:
            return None
        kind, entry_uuid = "call", record
    with connect() as conn:
        entry_id = _resolve_call_entry(conn, kind, entry_uuid)
        kind, target = entry_id.split(":", 1)
        table, column = ("call_records", "call_record_id") if kind == "call" else ("scheduled_calls", "scheduled_call_id")
        deleted = " AND deleted_at IS NULL" if kind == "call" else ""
        if not conn.execute(f"SELECT 1 FROM {table} WHERE id = %s{deleted} FOR KEY SHARE", (target,)).fetchone():
            return None
        note = conn.execute(
            f"INSERT INTO call_notes ({column}, author_user_id, author_label, body) VALUES (%s, %s, %s, %s) "
            f"RETURNING id, author_label, body, created_at, '{'call' if kind == 'call' else 'callback'}' AS target",
            (target, author_id, author_label, body)).fetchone()
        return {"entry_id": entry_id, "note": json_row(note)}
