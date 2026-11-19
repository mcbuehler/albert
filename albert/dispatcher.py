"""Scheduled-callback dispatcher (run every minute by Vercel Cron)."""

import time
from datetime import datetime, timedelta

import requests
from flask import current_app

from albert import config, db, notify, scheduling
from albert.masking import mask_phone
from albert.vapi import (
    MemoryUnavailable,
    OutboundCallError,
    VapiUnavailable,
    get_vapi_call,
    list_outbound_calls,
    place_outbound_call,
)

SET_CALL_ID_TRIES = 3
SET_CALL_ID_PAUSE_SECONDS = 0.5


def _parse_time(value, attempt_id) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as e:
        current_app.logger.error(
            "Vapi call candidate for attempt %s has an invalid createdAt (%s)", attempt_id, type(e).__name__
        )
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        current_app.logger.error(
            "Vapi call candidate for attempt %s has a createdAt without a timezone", attempt_id
        )
        return None
    return parsed


def recover_unrecorded_attempts() -> None:
    """Resolve attempts that never got a Vapi call id, by looking the call up in Vapi.

    Happens when the dispatcher died or the DB write failed right after dialling. Instead of guessing,
    find the call Vapi placed from that number to that customer around the attempt's start:
    found → record its id (the normal sync then sees the real outcome and the line stays busy until
    it ends); not found → nothing was dialled: undo the attempt and retry soon (not counted);
    Vapi unreachable → leave it for the next run (STALE_CALL_MINUTES is the last resort).
    """
    for attempt in db.unrecorded_attempts(config.UNRECORDED_ATTEMPT_MINUTES):
        if not attempt["phone"] or not attempt["vapi_phone_number_id"]:
            continue  # can't match (e.g. attempts recorded before customer ids were stored)
        started = attempt["started_at"]
        try:
            calls = list_outbound_calls(
                attempt["vapi_phone_number_id"],
                started - timedelta(minutes=config.VAPI_CALL_MATCH_BEFORE_MINUTES),
                started + timedelta(minutes=config.VAPI_CALL_MATCH_AFTER_MINUTES),
            )
        except VapiUnavailable as e:
            current_app.logger.warning("Can't resolve attempt %s without call id yet: %s", attempt["id"], e)
            continue
        candidates = []
        for call in calls:
            call_id = call.get("id")
            customer = call.get("customer")
            call_id = call_id.strip() if isinstance(call_id, str) else ""
            if (call_id
                    and call.get("type") in (None, "outboundPhoneCall")
                    and isinstance(customer, dict) and customer.get("number") == attempt["phone"]):
                candidates.append({**call, "id": call_id})
        known = db.known_call_ids([call["id"] for call in candidates])
        candidates = [call for call in candidates if call["id"] not in known]
        if candidates:
            def distance(call, attempt_id=attempt["id"], started_at=started):
                created = _parse_time(call.get("createdAt"), attempt_id)
                return abs((created - started_at).total_seconds()) if created else float("inf")

            call_id = min(candidates, key=distance)["id"]
            if db.set_attempt_call_id(attempt["id"], call_id):
                current_app.logger.warning("Recovered Vapi call %s for attempt %s (%s)",
                                           call_id, attempt["id"], mask_phone(attempt["phone"]))
        else:
            status = db.drop_attempt_and_retry(attempt["id"], config.UNDIALLED_RETRY_MINUTES)
            current_app.logger.warning("Attempt %s had no Vapi call (%s): undone, callback %s %s",
                                       attempt["id"], mask_phone(attempt["phone"]), attempt["scheduled_call_id"], status)


def sync_open_attempts() -> None:
    """Close attempts whose Vapi call has ended, with Vapi's endedReason and endedAt."""
    for attempt in db.open_attempts():
        call = get_vapi_call(attempt["vapi_call_id"])
        if not call or call.get("status") != "ended":
            continue
        reason = call.get("endedReason")
        if not isinstance(reason, str) or not reason:
            reason = "unknown"
        try:
            ended_at = datetime.fromisoformat(call["endedAt"])
        except (KeyError, TypeError, ValueError) as e:
            current_app.logger.error(
                "Vapi result for attempt %s has a missing or invalid endedAt (%s)", attempt["id"], type(e).__name__
            )
            ended_at = None
        status = db.finish_attempt(attempt["id"], reason, ended_at, answered=scheduling.is_answered(reason))
        current_app.logger.info("Vapi call %s ended: %s (callback %s)", attempt["vapi_call_id"], reason, status or "n/a")


def dispatch_due_calls() -> tuple[list[dict], bool]:
    """Sync ended calls, then start due callbacks (one running call per Vapi phone number).

    Also emails pending team notifications. Returns (dispatched callbacks, False if claiming failed).
    Every run is recorded for the staff page and reported to Healthchecks.io.
    """
    dispatched, ok = _dispatch()
    try:
        notify.send_pending_emails()
    except Exception:  # team emails must never stop the dispatcher
        current_app.logger.exception("Failed to send team notification emails")
    started = sum(1 for d in dispatched if d.get("vapi_call_id"))
    try:
        db.record_dispatcher_run(ok, started)
    except Exception:  # only a status display: never fail the run because of it
        current_app.logger.exception("Failed to record the dispatcher run")
    ping_healthcheck(ok, started)
    return dispatched, ok


def ping_healthcheck(ok: bool, started: int) -> None:
    """Tell Healthchecks.io the dispatcher ran (it alerts when pings stop or report a failure).

    Skipped without HEALTHCHECKS_PING_URL; a failed ping is only logged, never fails the run.
    """
    url = config.env("HEALTHCHECKS_PING_URL")
    if not url:
        return
    try:
        requests.post(url if ok else f"{url.rstrip('/')}/fail", data=f"started {started} calls",
                      timeout=config.HEALTHCHECK_TIMEOUT_SECONDS)
    except requests.RequestException:
        current_app.logger.warning("Healthchecks.io ping failed")


def _dispatch() -> tuple[list[dict], bool]:
    try:
        recover_unrecorded_attempts()
    except Exception:
        current_app.logger.exception("Dispatcher failed to recover attempts without call id")
    try:
        sync_open_attempts()
    except Exception:
        current_app.logger.exception("Dispatcher failed to sync open call attempts")
    dispatched = []
    for _ in range(config.MAX_DISPATCH_PER_RUN):
        try:
            claimed = db.claim_due_call(
                config.STALE_CALL_MINUTES,
                config.CALLING_START.isoformat(),
                config.CALLING_END.isoformat(),
            )
        except Exception:
            current_app.logger.exception("Dispatcher failed to claim a callback")
            return dispatched, False
        if not claimed:
            break
        dispatched.append(start_claimed_call(claimed))
    return dispatched, True


def start_claimed_call(claimed: dict) -> dict:
    """Dial a claimed callback, retaining uncertain attempts so they can be recovered."""
    phone = claimed["phone"]
    result = {"id": str(claimed["id"]), "attempt_no": claimed["attempt_no"]}
    current_app.logger.info(
        "Dispatching callback %s attempt %s to %s", claimed["id"], claimed["attempt_no"], mask_phone(phone)
    )
    try:
        # Claimed callbacks always belong to a customer. Not lookup_customer(): a DB error must not
        # turn the customer into "non-customer" and call them without memory.
        customer = db.get_customer_by_phone(phone)
    except Exception:
        current_app.logger.exception("Customer lookup failed for callback %s; retrying in %s min",
                                     claimed["id"], config.MEMORY_RETRY_MINUTES)
        try:
            db.drop_attempt_and_retry(claimed["attempt_id"], config.MEMORY_RETRY_MINUTES)
        except Exception:
            current_app.logger.exception("Failed to requeue callback %s", claimed["id"])
        return {**result, "error": "Customer lookup failed; retrying soon."}
    try:
        if customer is None:
            raise OutboundCallError("The customer no longer exists.", 404)
        call_id = place_outbound_call(phone, claimed["vapi_assistant_id"], claimed["vapi_phone_number_id"], customer,
                                      claimed.get("instructions"))
    except MemoryUnavailable:
        # Not the customer's fault and nothing was dialled: retry soon without using up an attempt.
        current_app.logger.warning("Callback %s not started: memory unavailable; retrying in %s min",
                                   claimed["id"], config.MEMORY_RETRY_MINUTES)
        try:
            db.drop_attempt_and_retry(claimed["attempt_id"], config.MEMORY_RETRY_MINUTES)
        except Exception:
            current_app.logger.exception("Failed to requeue callback %s", claimed["id"])
        return {**result, "error": "Customer memory unavailable; retrying soon."}
    except OutboundCallError as e:
        if e.may_have_started:
            # Vapi may have accepted the request before the connection failed. Keep the id-less
            # reservation for recover_unrecorded_attempts instead of risking a duplicate call.
            current_app.logger.warning(
                "Callback %s attempt %s has an uncertain Vapi outcome: %s",
                claimed["id"], claimed["attempt_no"], e.message,
            )
            return {**result, "error": "Could not start the call."}
        current_app.logger.warning(
            "Callback %s attempt %s not started: %s", claimed["id"], claimed["attempt_no"], e.message
        )
        try:
            status = db.finish_attempt(claimed["attempt_id"], "dispatch-error")
            current_app.logger.info("Callback %s is now %s", claimed["id"], status)
        except Exception:
            current_app.logger.exception("Failed to requeue callback %s", claimed["id"])
        return {**result, "error": "Could not start the call."}
    except Exception:
        # Unexpected local errors are definite failures unless an OutboundCallError explicitly
        # marks the request outcome as uncertain.
        current_app.logger.exception("Callback %s attempt %s failed unexpectedly", claimed["id"], claimed["attempt_no"])
        try:
            status = db.finish_attempt(claimed["attempt_id"], "dispatch-error")
            current_app.logger.info("Callback %s is now %s", claimed["id"], status)
        except Exception:
            current_app.logger.exception("Failed to requeue callback %s", claimed["id"])
        return {**result, "error": "Could not start the call."}

    if not call_id:
        # A successful HTTP exchange without an id is also ambiguous: retain the reservation and
        # let recover_unrecorded_attempts match the call (or prove that nothing was dialled).
        current_app.logger.warning(
            "Callback %s attempt %s received no Vapi call id; leaving it for recovery",
            claimed["id"], claimed["attempt_no"],
        )
        return {**result, "error": "Could not start the call."}

    # The call is running: try hard to record its id (recover_unrecorded_attempts is the fallback).
    for attempt in range(1, SET_CALL_ID_TRIES + 1):
        try:
            db.set_attempt_call_id(claimed["attempt_id"], call_id)
            break
        except Exception:
            if attempt == SET_CALL_ID_TRIES:
                current_app.logger.exception("Failed to record Vapi call %s for callback %s", call_id, claimed["id"])
            else:
                time.sleep(SET_CALL_ID_PAUSE_SECONDS)
    current_app.logger.info("Callback %s attempt %s started as Vapi call %s", claimed["id"], claimed["attempt_no"], call_id)
    return {**result, "vapi_call_id": call_id}
