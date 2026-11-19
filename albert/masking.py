"""Log-safe masking of personal data."""

import re


def mask_phone(number) -> str:
    """Return a log-safe phone number: first 4 and last 2 digits only."""
    digits = re.sub(r"\D", "", str(number))
    if len(digits) <= 7:
        return "***"
    return f"+{digits[:4]}***{digits[-2:]}"


def mask_bank_id(bank_id: str) -> str:
    """Mask the phone number inside a `user-<number>` bank id for logging."""
    prefix = "user-"
    return prefix + mask_phone(bank_id[len(prefix):]) if bank_id.startswith(prefix) else bank_id
