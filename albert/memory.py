"""Hindsight persistent memory: one bank per customer (`user-<phone>`)."""

import asyncio
import time

from flask import current_app
from hindsight_client import Hindsight
from hindsight_vapi import HindsightVapiWebhook

from albert import config
from albert.masking import mask_bank_id
from albert.validation import normalize_phone


def bank_id_for(phone_number: str) -> str:
    """The customer's Hindsight bank, always keyed by the normalized number (one bank per person).

    Raises ValueError if the number can't be normalized: a bank must never get a non-normalized name
    (that would split one person's memory across banks). The message doesn't include the number.
    """
    normalized = normalize_phone(phone_number)
    if normalized is None:
        raise ValueError("Not a normalizable phone number; refusing to build a memory bank id.")
    return f"user-{normalized}"


def memory_text(wrapped: dict) -> str:
    """Extract the recalled memory text from build_assistant_overrides' result."""
    if not isinstance(wrapped, dict):
        raise ValueError("Hindsight memory response must be an object.")
    overrides = wrapped.get("assistantOverrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("Hindsight memory response field 'assistantOverrides' must be an object.")
    model = overrides.get("model", {})
    if not isinstance(model, dict):
        raise ValueError("Hindsight memory response field 'assistantOverrides.model' must be an object.")
    messages = model.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("Hindsight memory response field 'assistantOverrides.model.messages' must be an array.")
    if not messages:
        return config.NO_MEMORY_TEXT
    first = messages[0]
    if not isinstance(first, dict) or not isinstance(first.get("content"), str):
        raise ValueError("Hindsight memory response first message must be an object with string 'content'.")
    return first["content"] or config.NO_MEMORY_TEXT


def _client(api_key: str, timeout: float) -> Hindsight:
    # One attempt per request (no capacity retries); callers add an overall deadline, so the
    # library's defaults (300 s per request, 3 attempts) can't stretch a lookup.
    return Hindsight(
        base_url=config.HINDSIGHT_API_URL,
        api_key=api_key,
        timeout=timeout,
        max_attempts=1,
    )


async def _bank_exists(client: Hindsight, bank_id: str) -> bool:
    listing = await client.banks.list_banks(q=bank_id)
    return any(bank.bank_id == bank_id for bank in listing.banks)


async def _create_bank_if_missing(client: Hindsight, bank_id: str) -> bool:
    """Create the bank unless it exists; returns True if it was created."""
    try:
        if await _bank_exists(client, bank_id):
            return False
        current_app.logger.info("Creating new Hindsight bank %s", mask_bank_id(bank_id))
        await client.acreate_bank(bank_id=bank_id)
        return True
    finally:
        await client.aclose()


def ensure_bank(phone_number: str, api_key: str, timeout: float) -> bool:
    """Create a customer's (empty) memory bank at onboarding; True if created, False if it existed.

    Raises TimeoutError after `timeout`, or the Hindsight error.
    """
    bank_id = bank_id_for(phone_number)  # before creating the client: a bad number must not leave it unclosed
    client = _client(api_key, timeout)
    return asyncio.run(asyncio.wait_for(_create_bank_if_missing(client, bank_id), timeout))


async def _delete_bank(client: Hindsight, bank_id: str, recreate: bool) -> None:
    """Delete the bank if it exists (a missing bank is fine); with `recreate`, create a new empty one.

    Each step is logged before it starts and with its duration, so a timeout shows which step hung.
    """
    masked = mask_bank_id(bank_id)
    try:
        started = time.monotonic()
        current_app.logger.info("Looking up Hindsight bank %s", masked)
        exists = await _bank_exists(client, bank_id)
        current_app.logger.info("Hindsight bank %s %s (%.1f s)", masked, "found" if exists else "not found",
                                time.monotonic() - started)
        if exists:
            started = time.monotonic()
            current_app.logger.info("Deleting Hindsight bank %s", masked)
            await client.adelete_bank(bank_id)
            current_app.logger.info("Deleted Hindsight bank %s (%.1f s)", masked, time.monotonic() - started)
        if recreate:
            started = time.monotonic()
            current_app.logger.info("Creating empty Hindsight bank %s", masked)
            await client.acreate_bank(bank_id=bank_id)
            current_app.logger.info("Created empty Hindsight bank %s (%.1f s)", masked, time.monotonic() - started)
    finally:
        await client.aclose()


def delete_bank(phone_number: str, api_key: str, timeout: float) -> None:
    """Delete a customer's memory bank with all memories (when the customer is deleted).

    Raises TimeoutError after `timeout`, or the Hindsight error.
    """
    bank_id = bank_id_for(phone_number)
    client = _client(api_key, timeout)
    asyncio.run(asyncio.wait_for(_delete_bank(client, bank_id, recreate=False), timeout))


def erase_memory(phone_number: str, api_key: str, timeout: float) -> None:
    """Erase a customer's memories: delete their bank and create a new empty one (the customer stays).

    Raises TimeoutError after `timeout`, or the Hindsight error.
    """
    bank_id = bank_id_for(phone_number)
    client = _client(api_key, timeout)
    asyncio.run(asyncio.wait_for(_delete_bank(client, bank_id, recreate=True), timeout))


async def recall_or_create_bank(client: Hindsight, memory: HindsightVapiWebhook, bank_id: str) -> tuple[dict, bool]:
    """Recall memories if the bank exists; otherwise create an empty bank (only for customers onboarded
    before banks were created at onboarding; non-customers never get here)."""
    try:
        if await _bank_exists(client, bank_id):
            return await memory.build_assistant_overrides("the caller's name, preferences and past conversations"), False
        current_app.logger.info("Creating new Hindsight bank %s", mask_bank_id(bank_id))
        await client.acreate_bank(bank_id=bank_id)
        return {}, True
    finally:
        await client.aclose()


def recall_for_phone(phone_number: str, api_key: str, timeout: float) -> tuple[str, bool]:
    """Return (memory text, bank was just created) for a customer's phone.

    `timeout` is a hard deadline for the whole lookup (bank listing + recall); raises TimeoutError
    when it passes, or the Hindsight error otherwise.
    """
    bank_id = bank_id_for(phone_number)
    client = _client(api_key, timeout)
    memory = HindsightVapiWebhook(bank_id=bank_id, client=client)
    wrapped, is_new = asyncio.run(asyncio.wait_for(recall_or_create_bank(client, memory, bank_id), timeout))
    return memory_text(wrapped), is_new


async def handle_vapi_memory(event: dict, memory: HindsightVapiWebhook) -> dict:
    """Run the webhook and finish retention before closing this request's event loop."""
    try:
        response = await memory.handle(event)
        if event.get("message", {}).get("type") == "end-of-call-report":
            # Hindsight schedules retention with create_task; asyncio.run would
            # cancel it on exit unless we wait for it here.
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            if pending:
                await asyncio.gather(*pending)
        if response is None:
            return {}
        if not isinstance(response, dict):
            raise ValueError("Hindsight webhook response must be an object or null.")
        return response
    finally:
        # Close the aiohttp session before this request's event loop goes away.
        await memory._client.aclose()


def process_event(event: dict, bank_id: str, api_key: str) -> dict:
    """Let Hindsight handle a Vapi server event (e.g. retain an end-of-call-report transcript)."""
    memory = HindsightVapiWebhook(
        bank_id=bank_id,
        hindsight_api_url=config.HINDSIGHT_API_URL,
        api_key=api_key,
    )
    return asyncio.run(handle_vapi_memory(event, memory))
