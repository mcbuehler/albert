"""Sign-up of new customers on an inbound call (Vapi tool onboard_customer).

Only callers from config.ONBOARDING_REGIONS are answered and signed up. New customers are live immediately
(so the same call can schedule their callback) but marked needs_review, and the team is notified.
"""

from zoneinfo import available_timezones

import phonenumbers
import psycopg
from flask import current_app
from phonenumbers import timezone as phone_timezones

from albert import config, db, memory, notify
from albert.logging_utils import get_logger
from albert.masking import mask_phone
from albert.validation import normalize_phone

FAILED = ("Could not sign up the caller right now. Use notify_team with reason request, their name and "
          "\"sign-up failed\" in the summary, and tell them the team will be in touch.")


def _parse(phone: str | None):
    phone = normalize_phone(phone)
    if phone is None:
        return None
    try:
        return phonenumbers.parse(phone)
    except phonenumbers.NumberParseException as e:
        get_logger(__name__).error("Normalized phone number could not be parsed by phonenumbers (%s)", type(e).__name__)
        return None


def region_for(phone: str | None) -> str | None:
    """The supported region (e.g. "US", "CA", "CH") of a phone number, or None."""
    parsed = _parse(phone)
    region = phonenumbers.region_code_for_number(parsed) if parsed else None
    return region if region in config.ONBOARDING_REGIONS else None


def timezone_guess(phone: str | None) -> str:
    """The caller's likely time zone from the area code; the region's default if ambiguous; else DEFAULT_TIMEZONE."""
    region = region_for(phone)
    if region is None:
        return config.DEFAULT_TIMEZONE
    zones = phone_timezones.time_zones_for_number(_parse(phone))
    if len(zones) == 1 and zones[0] in available_timezones():
        return zones[0]
    return config.REGION_DEFAULT_TIMEZONES[region]


def _name(value) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        return None
    return value.strip()


def onboard_customer_tool(arguments: dict, phone_number: str | None, vapi_call_id: str | None) -> str:
    """onboard_customer tool: returns a sentence for the assistant to act on."""
    region = region_for(phone_number)
    if region is None:
        return ("Could not sign up the caller: their phone number is unknown or not from the United States, "
                "Canada or Switzerland. Don't try again.")
    if arguments.get("notes_consent") is not True:
        return ("Not signed up: first ask whether they agree that you keep notes of your chats to remember them "
                "next time. Only after a clear yes, call onboard_customer again with notes_consent true.")
    first_name, last_name = _name(arguments.get("first_name")), _name(arguments.get("last_name"))
    if not first_name or not last_name:
        return "Not signed up: ask for their first and last name, then try again."
    timezone = arguments.get("timezone")
    if timezone in (None, ""):
        timezone = timezone_guess(phone_number)
    elif not isinstance(timezone, str) or timezone.strip() not in available_timezones():
        return ("Not signed up: timezone must be an IANA time zone name such as America/Chicago or Europe/Zurich. "
                "Ask which city they live in if unsure, then try again.")
    timezone = timezone.strip()

    suffix = config.ONBOARDING_REGIONS[region]
    assistant_id = config.env(f"VAPI_ASSISTANT_ID_{suffix}")
    phone_number_id = config.env(f"VAPI_PHONE_NUMBER_ID_{suffix}")
    hindsight_key = config.env("HINDSIGHT_API_KEY")
    if not (assistant_id and phone_number_id and hindsight_key):
        current_app.logger.error("Cannot sign up inbound callers from %s: VAPI_ASSISTANT_ID_%s, "
                                 "VAPI_PHONE_NUMBER_ID_%s or HINDSIGHT_API_KEY is not configured", region, suffix, suffix)
        return FAILED

    phone = normalize_phone(phone_number)
    try:
        if db.get_customer_by_phone(phone):
            return f"{first_name} is already a customer; no sign-up needed. Agree on the next call and use schedule_callback."
        if db.count_inbound_signups(24) >= config.MAX_INBOUND_SIGNUPS_PER_DAY:
            current_app.logger.warning("Inbound sign-up of %s refused: more than %s sign-ups in 24 h",
                                       mask_phone(phone), config.MAX_INBOUND_SIGNUPS_PER_DAY)
            return FAILED
    except Exception:
        current_app.logger.exception("Inbound sign-up checks failed for %s", mask_phone(phone))
        return FAILED

    # Memory bank first, as in staff onboarding: a customer is only saved once their memory exists.
    try:
        memory.ensure_bank(phone, hindsight_key, config.INBOUND_SIGNUP_MEMORY_TIMEOUT_SECONDS)
        customer = db.insert_customer(first_name, None, last_name, phone, timezone, phone_number_id, assistant_id,
                                      source="inbound", needs_review=True)
    except psycopg.errors.UniqueViolation:
        get_logger(__name__).info("Inbound sign-up found an existing customer for %s", mask_phone(phone))
        return f"{first_name} is already a customer; no sign-up needed. Agree on the next call and use schedule_callback."
    except Exception:
        current_app.logger.exception("Inbound sign-up failed for %s", mask_phone(phone))
        return FAILED

    current_app.logger.info("Inbound sign-up: created customer %s (%s, call %s)",
                            customer["id"], mask_phone(phone), vapi_call_id)
    notify.notify_team("inbound_signup", phone, contact_name=f"{first_name} {last_name}",
                       summary=f"Signed up on an inbound call; time zone {timezone}. Please review the customer.",
                       vapi_call_id=vapi_call_id)
    return (f"{first_name} {last_name} is signed up (time zone {timezone}). Now agree on the next call and use "
            "schedule_callback.")
