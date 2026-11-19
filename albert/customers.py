"""Customer lookup and the customer variables for the assistant prompt."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app

from albert import config, db
from albert.masking import mask_phone
from albert.validation import normalize_phone


def lookup_customer(phone_number: str | None) -> dict | None:
    """Best-effort customer lookup: None for unknown callers and on database errors."""
    customer = None
    phone_number = normalize_phone(phone_number) or phone_number
    if phone_number:
        try:
            customer = db.get_customer_by_phone(phone_number)
        except Exception:
            current_app.logger.exception("Customer lookup failed for %s", mask_phone(phone_number))
    current_app.logger.info("Customer %s: %s", mask_phone(phone_number or ""), "found" if customer else "not found")
    return customer


def customer_variables(customer: dict | None, timezone: str | None = None) -> dict:
    """Return {{customer_first_name}}, {{customer_last_name}} and {{customer_local_now}} for the assistant prompt.

    Vapi's own {{now}} is UTC, so we send the time in the customer's zone. An unknown customer falls back
    to config.UNKNOWN_CUSTOMER_NAME and `timezone` (e.g. guessed from their number) or config.DEFAULT_TIMEZONE.
    """
    zone = ZoneInfo(customer["timezone"] if customer else timezone or config.DEFAULT_TIMEZONE)
    now = datetime.now(zone)
    offset = now.strftime("%z")
    return {
        "customer_first_name": customer['first_name'] if customer else config.UNKNOWN_CUSTOMER_NAME,
        "customer_last_name": customer['last_name'] if customer else config.UNKNOWN_CUSTOMER_NAME,
        "customer_local_now": f"{now:%A, %d %B %Y, %H:%M} ({zone.key}, UTC{offset[:3]}:{offset[3:]})",
    }


def callback_time_text(local: datetime) -> str:
    """How a callback time is told to the assistant, e.g. "Monday, 2 November 2026 at 09:00" (no leading zero)."""
    return f"{local:%A}, {local.day} {local:%B %Y} at {local:%H:%M}"


def pending_callback_text(callback: dict, zone: ZoneInfo) -> str:
    """A pending callback for the assistant: its local time plus the customer's own instructions, if any.

    e.g. "Monday, 2 November 2026 at 09:00 (customer's instructions: Remind them to take their tablets)", so
    the customer can name a reminder by what it's for. Staff instructions are only for the call itself.
    """
    text = callback_time_text(callback["due_at"].astimezone(zone))
    instructions = callback.get("instructions") or ""
    if instructions.startswith(config.CUSTOMER_INSTRUCTIONS_PREFIX):
        text += f" (customer's instructions: {instructions.removeprefix(config.CUSTOMER_INSTRUCTIONS_PREFIX)})"
    return text


def callback_variables(customer: dict | None) -> dict:
    """Return {{pending_callbacks}}: the customer's pending callbacks in their local time.

    e.g. "Thursday, 22 October 2026 at 14:30; Monday, 2 November 2026 at 09:00" (each with the customer's own
    instructions, see pending_callback_text). Non-customers have none
    (no DB lookup). Best-effort: a DB error must not stop the call, so the assistant is told the list
    couldn't be loaded rather than that there are none.
    """
    if not customer:
        return {config.PENDING_CALLBACKS_VARIABLE: config.NO_PENDING_CALLBACKS_TEXT}
    try:
        callbacks = db.pending_callbacks(customer["id"])
    except Exception:
        current_app.logger.exception("Failed to load pending callbacks for %s", mask_phone(customer.get("phone", "")))
        return {config.PENDING_CALLBACKS_VARIABLE: config.PENDING_CALLBACKS_UNAVAILABLE_TEXT}
    zone = ZoneInfo(customer["timezone"])
    texts = [pending_callback_text(callback, zone) for callback in callbacks]
    return {config.PENDING_CALLBACKS_VARIABLE: "; ".join(texts) if texts else config.NO_PENDING_CALLBACKS_TEXT}
