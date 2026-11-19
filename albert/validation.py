"""Validation and parsing of request values."""

import re
from datetime import datetime

from albert import config
from albert.logging_utils import get_logger

E164_RE = re.compile(r"^\+[1-9]\d{6,14}\Z")  # \Z, not $: $ also matches before a trailing newline
VAPI_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)  # Vapi ids are UUIDs


# Customer-local time from forms and the Vapi tool: exactly YYYY-MM-DDTHH:MM, optionally with seconds.
LOCAL_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?")


# Separators people and phones put into numbers: whitespace (incl. non-breaking), dashes, dots, slashes, parentheses.
_PHONE_SEPARATORS_RE = re.compile(r"[\s\-./()]")
# A Swiss-style "(0)" right after the country code ("+41 (0)79 …"): the 0 is not dialled internationally.
_TRUNK_ZERO_RE = re.compile(r"^((?:\+|00)\s*\d{1,3})\s*\(0\)")


def normalize_phone(value) -> str | None:
    """One canonical E.164 form per number, e.g. "+4179 7381155" and "0041797381155" -> "+41797381155".

    Drops a Swiss-style "(0)" right after the country code, removes separators (elsewhere "(0)" keeps
    its digit), turns a leading "00" into "+".
    Returns None if the result isn't E.164 (e.g. national numbers like "079 738 11 55": no country code).
    Memory banks (`user-<number>`) and customer lookups use this form, so a person always gets the same bank.
    """
    if not isinstance(value, str):
        return None
    number = _PHONE_SEPARATORS_RE.sub("", _TRUNK_ZERO_RE.sub(r"\1", value.strip()))
    if number.startswith("00"):
        number = "+" + number[2:]
    return number if E164_RE.fullmatch(number) else None


def is_valid_vapi_id(value) -> bool:
    """True if value is a Vapi id (UUID); surrounding whitespace is allowed."""
    return isinstance(value, str) and VAPI_ID_RE.fullmatch(value.strip()) is not None


def vapi_ids(data: dict, required: bool) -> tuple[dict, str | None]:
    """Read vapi_phone_number_id / vapi_assistant_id from a request body; returns (ids, error)."""
    ids = {}
    for field in ("vapi_phone_number_id", "vapi_assistant_id"):
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                return {}, f"{field} is required."
            ids[field] = None
            continue
        if not is_valid_vapi_id(value):
            return {}, f"{field} must be a Vapi id (UUID, e.g. 3f2b1c9e-8d4a-4b6e-9f10-2a7c5d8e1b34)."
        ids[field] = value.strip().lower()
    return ids, None


def parse_local_time(value) -> datetime | None:
    """Parse a naive customer-local time `YYYY-MM-DDTHH:MM` (seconds allowed, then dropped); None if invalid.

    Strict on purpose: a bare date, week dates or a time zone are rejected, so the Vapi tool gets
    "wrong format" back instead of a callback silently scheduled at midnight.
    """
    if not isinstance(value, str) or not LOCAL_TIME_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:  # e.g. month 13, Feb 30, hour 25
        get_logger(__name__).error(
            "Local time matched the accepted format but is not a valid calendar time (%s)", type(e).__name__
        )
        return None
    return parsed.replace(second=0, microsecond=0)


def parse_instructions(value) -> tuple[str | None, str | None]:
    """Optional instructions for a call (staff, or the customer via the AI): trimmed text or None; returns (instructions, error)."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "instructions must be text."
    text = value.strip()
    if len(text) > config.MAX_INSTRUCTIONS_LENGTH:
        return None, f"instructions can be at most {config.MAX_INSTRUCTIONS_LENGTH} characters."
    return text or None, None
