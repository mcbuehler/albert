"""Scheduling callbacks: calling-hours rules and creating scheduled calls."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app

from albert import config, db
from albert.config import ANSWERED_REASONS
from albert.customers import callback_time_text, callback_variables, lookup_customer, pending_callback_text
from albert.logging_utils import get_logger
from albert.masking import mask_phone
from albert.validation import parse_instructions, parse_local_time


def to_utc(local: datetime, zone_name: str) -> datetime:
    """Convert a naive customer-local time to an aware UTC datetime."""
    return local.replace(tzinfo=ZoneInfo(zone_name)).astimezone(timezone.utc)


def is_answered(ended_reason: str) -> bool:
    """True if the Vapi endedReason means the customer was reached."""
    return ended_reason in ANSWERED_REASONS


class ScheduleError(Exception):
    """A callback could not be scheduled; `message` is safe to show to staff and the assistant."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def schedule_callback(customer: dict, requested: datetime, source_vapi_call_id: str | None = None,
                      vapi_phone_number_id: str | None = None, vapi_assistant_id: str | None = None,
                      instructions: str | None = None) -> tuple[dict, datetime]:
    """Schedule a callback at a customer-local time; returns (row, due local time).

    Calling hours don't apply here: the first attempt rings at the requested time (staff and customers
    choose it deliberately). Only retries wait for calling hours (see db.claim_due_call).
    """
    phone = customer["phone"]
    if customer["opted_out"]:
        raise ScheduleError("This customer has opted out of calls.", 409)

    due_at = to_utc(requested, customer["timezone"])
    if due_at <= datetime.now(timezone.utc):
        raise ScheduleError("local_time must be in the future (customer's time zone).", 400)

    try:
        scheduled = db.insert_scheduled_call(
            customer["id"], requested, due_at, config.MAX_PENDING_PER_CUSTOMER, source_vapi_call_id,
            vapi_phone_number_id, vapi_assistant_id, instructions,
        )
    except db.PendingLimitReached:
        current_app.logger.warning("Rejected callback for %s: pending limit reached", mask_phone(phone))
        raise ScheduleError(f"This customer already has {config.MAX_PENDING_PER_CUSTOMER} pending callbacks.", 409) from None
    except Exception as e:
        current_app.logger.exception("Failed to schedule callback for %s", mask_phone(phone))
        raise ScheduleError("Could not schedule the call. Please try again later.", 500) from e

    if scheduled.get("already_scheduled"):
        current_app.logger.info("Callback %s for %s at %s already existed; not scheduling it twice",
                                scheduled["id"], mask_phone(phone), due_at.isoformat())
    else:
        current_app.logger.info("Scheduled callback %s for %s at %s", scheduled["id"], mask_phone(phone), due_at.isoformat())
    return scheduled, requested


def schedule_callback_tool(arguments: dict, phone_number: str | None, vapi_call_id: str | None) -> str:
    """schedule_callback tool: returns a sentence for the assistant to act on.

    Optional `instructions` (e.g. a reminder to take medicine) are stored with the callback, marked as the
    customer's, and given to the assistant on every attempt as {{call_instructions}}.
    """
    customer = lookup_customer(phone_number)
    if not customer:
        return "Could not schedule the callback: this caller is not a registered customer. Tell them a colleague will get back to them."
    requested = parse_local_time(arguments.get("local_time"))
    if requested is None:
        return ("Could not schedule the callback: local_time must be the customer's local date and time "
                "without time zone, e.g. 2026-10-17T14:30. Try again with that format.")
    note = arguments.get("instructions")
    if isinstance(note, str) and note.strip():
        note = note.strip()
        instructions, error = parse_instructions(config.CUSTOMER_INSTRUCTIONS_PREFIX + note)
    else:
        instructions, error = parse_instructions(note)
    if error:
        return f"Could not schedule the callback: {error} Shorten or fix them and try again."
    try:
        scheduled, due_local = schedule_callback(customer, requested, source_vapi_call_id=vapi_call_id,
                                                 instructions=instructions)
    except ScheduleError as e:
        get_logger(__name__).info("Rejected callback tool request for %s: scheduling returned HTTP %s",
                                  mask_phone(customer["phone"]), e.status)
        if e.status == 400:
            return "Could not schedule the callback: that time has already passed for the customer. Ask for a time in the future."
        return f"Could not schedule the callback: {e.message}"
    when = callback_time_text(due_local)
    if scheduled.get("already_scheduled"):
        if instructions:
            return (f"A callback for {when} (customer's local time) was already scheduled and keeps its own "
                    "instructions; the new ones were not saved. Offer the customer a different time.")
        return f"A callback for {when} (customer's local time) was already scheduled; nothing else to do."
    if instructions:
        return f"Callback scheduled for {when} (customer's local time) with these instructions: {note}"
    return f"Callback scheduled for {when} (customer's local time)."


# --- Customer self-service: cancel / reschedule (Vapi API Request tools) ----------------------------

def _local(customer: dict, due_at: datetime) -> datetime:
    """A callback's due time as a naive customer-local datetime."""
    return due_at.astimezone(ZoneInfo(customer["timezone"])).replace(tzinfo=None)


def _remaining_text(customer: dict) -> str:
    """The customer's pending callbacks after a change, so the assistant stays up to date during the call."""
    pending = callback_variables(customer)[config.PENDING_CALLBACKS_VARIABLE]
    if pending == config.NO_PENDING_CALLBACKS_TEXT:
        return "The customer has no other pending callbacks."
    if pending == config.PENDING_CALLBACKS_UNAVAILABLE_TEXT:
        return ""
    return f"Pending callbacks now: {pending}."


def _find_pending(customer: dict, callback_time) -> tuple[dict | None, str | None]:
    """The customer's pending callback at `callback_time` (customer-local), or the only one if none is given.

    Returns (callback, None) or (None, a sentence telling the assistant what to do).
    """
    try:
        pending = db.pending_callbacks(customer["id"])
    except Exception:
        current_app.logger.exception("Failed to load pending callbacks for %s", mask_phone(customer["phone"]))
        return None, "The customer's callbacks could not be loaded right now. Ask them to try again later."
    if not pending:
        return None, "The customer has no pending callbacks."
    zone = ZoneInfo(customer["timezone"])
    listed = "; ".join(pending_callback_text(c, zone) for c in pending)
    # Only a missing/blank time means "the only one"; anything else that isn't text is a wrong format,
    # so a bad value never silently picks (and cancels or moves) a callback.
    if callback_time is None or (isinstance(callback_time, str) and not callback_time.strip()):
        if len(pending) == 1:
            return pending[0], None
        return None, (f"The customer has several pending callbacks: {listed}. Ask which one they mean "
                      "and pass its callback_time.")
    requested = parse_local_time(callback_time.strip() if isinstance(callback_time, str) else callback_time)
    if requested is None:
        return None, ("callback_time must be the callback's local date and time, e.g. 2026-10-22T14:30. "
                      f"Pending callbacks: {listed}.")
    for callback in pending:
        if _local(customer, callback["due_at"]) == requested:
            return callback, None
    return None, (f"There is no pending callback on {callback_time_text(requested)}. Pending callbacks: {listed}. "
                  "Ask which one they mean.")


def cancel_callback_tool(arguments: dict, phone_number: str | None, vapi_call_id: str | None) -> str:
    """cancel_callback tool: cancels one of the caller's pending callbacks; returns a sentence for the assistant."""
    customer = lookup_customer(phone_number)
    if not customer:
        return "Nothing was cancelled: this caller is not a registered customer."
    callback, problem = _find_pending(customer, arguments.get("callback_time"))
    if problem:
        return f"Nothing was cancelled. {problem}"
    when = callback_time_text(_local(customer, callback["due_at"]))
    try:
        status = db.cancel_scheduled_call(callback["id"])
    except Exception:
        current_app.logger.exception("Failed to cancel callback %s for %s", callback["id"], mask_phone(customer["phone"]))
        return "Nothing was cancelled: the callback could not be cancelled right now. Ask the customer to try again later."
    if status != "cancelled":
        return f"Nothing was cancelled: the callback on {when} can't be cancelled anymore."
    current_app.logger.info("Customer %s cancelled callback %s (call %s)", mask_phone(customer["phone"]), callback["id"], vapi_call_id)
    return f"The callback on {when} (customer's local time) was cancelled. {_remaining_text(customer)}".strip()


def reschedule_callback_tool(arguments: dict, phone_number: str | None, vapi_call_id: str | None) -> str:
    """reschedule_callback tool: moves one of the caller's pending callbacks; returns a sentence for the assistant.

    The new time is checked first; only then is the old callback replaced (one transaction).
    """
    customer = lookup_customer(phone_number)
    if not customer:
        return "Nothing was changed: this caller is not a registered customer."
    if customer["opted_out"]:
        return "Nothing was changed: this customer has opted out of calls."
    callback, problem = _find_pending(customer, arguments.get("callback_time"))
    if problem:
        return f"Nothing was changed. {problem}"
    requested = parse_local_time(arguments.get("new_local_time"))
    if requested is None:
        return ("Nothing was changed. new_local_time must be the customer's local date and time without "
                "time zone, e.g. 2026-10-23T10:00. Try again with that format.")
    due_at = to_utc(requested, customer["timezone"])
    if due_at <= datetime.now(timezone.utc):
        return "Nothing was changed: that new time has already passed for the customer. Ask for a time in the future."
    old_when = callback_time_text(_local(customer, callback["due_at"]))
    new_when = callback_time_text(requested)
    if due_at == callback["due_at"]:
        return f"Nothing was changed: the callback is already on {new_when}."
    try:
        moved = db.reschedule_callback(callback["id"], customer["id"], requested, due_at, vapi_call_id)
    except Exception:
        current_app.logger.exception("Failed to reschedule callback %s for %s", callback["id"], mask_phone(customer["phone"]))
        return "Nothing was changed: the callback could not be moved right now. Ask the customer to try again later."
    if moved is None:
        return f"Nothing was changed: the callback on {old_when} can't be moved anymore."
    current_app.logger.info("Customer %s moved callback %s to %s (new callback %s, call %s)",
                            mask_phone(customer["phone"]), callback["id"], due_at.isoformat(), moved["id"], vapi_call_id)
    sentence = f"The callback was moved from {old_when} to {new_when} (customer's local time)."
    return f"{sentence} {_remaining_text(customer)}".strip()
