"""Vapi REST API and server-message helpers (no Vapi SDK)."""

import urllib.parse
from datetime import datetime, timezone

import requests
from flask import current_app

from albert import config, db, memory, onboarding
from albert.customers import callback_variables, customer_variables
from albert.masking import mask_bank_id, mask_phone
from albert.validation import normalize_phone


class OutboundCallError(Exception):
    """A call could not be started; `message` is safe to show to clients."""

    def __init__(self, message: str, status: int, may_have_started: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.may_have_started = may_have_started


class VapiUnavailable(Exception):
    """Vapi couldn't be asked (no key, network error, unexpected response)."""


class MemoryUnavailable(OutboundCallError):
    """The customer's memory couldn't be loaded, so the call wasn't placed (calls never run without memory)."""


def _json_type(value) -> str:
    """A payload-safe name for a JSON value's type."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def caller_number(message: dict) -> str | None:
    """The customer's phone number from a Vapi server message (`message.call.customer.number`), normalized
    (see `normalize_phone`); a number that can't be normalized (e.g. "anonymous") is returned as is."""
    call = message.get("call") if isinstance(message, dict) else None
    customer = call.get("customer") if isinstance(call, dict) else None
    number = customer.get("number") if isinstance(customer, dict) else None
    return normalize_phone(number) or number


def assistant_request_response(message: dict, api_key: str) -> dict:
    """Answer an inbound assistant-request.

    Customers always get their memory: if it can't be loaded in time (or we can't tell whether the
    caller is a customer), the reply is an error, which Vapi speaks before ending the call.
    Non-customers haven't consented to memory: they get the default assistant without memory and
    no Hindsight bank. Callers without a usable number are rejected.
    """
    phone_number = caller_number(message)
    # Withheld, anonymous ("anonymous", "Restricted", …) or otherwise non-E.164 numbers have no memory: reject.
    if normalize_phone(phone_number) is None:
        current_app.logger.info("Rejecting inbound call without a usable caller number")
        return {"error": config.INBOUND_NO_NUMBER_MESSAGE}

    try:
        customer = db.get_customer_by_phone(phone_number)
    except Exception:
        # Not knowing whether this is a customer, we can neither load memory nor skip it: reject.
        current_app.logger.exception("Customer lookup failed for inbound call %s; rejecting the call", mask_phone(phone_number))
        return {"error": config.INBOUND_NO_MEMORY_MESSAGE}

    if not customer:
        if onboarding.region_for(phone_number) is None:
            current_app.logger.info("Rejecting inbound call from non-customer %s: country not supported",
                                    mask_phone(phone_number))
            return {"error": config.INBOUND_COUNTRY_NOT_SUPPORTED_MESSAGE}
        # The country's assistant for unknown callers (US: English, CH: German), else the shared default.
        suffix = config.ONBOARDING_REGIONS[onboarding.region_for(phone_number)]
        assistant_id = config.env(f"VAPI_INBOUND_ASSISTANT_ID_{suffix}") or config.env("VAPI_ASSISTANT_ID")
        if not assistant_id:
            current_app.logger.error("VAPI_INBOUND_ASSISTANT_ID_%s and VAPI_ASSISTANT_ID are not configured; "
                                     "cannot answer inbound call", suffix)
            return {"error": "Sorry, this service is unavailable right now."}
        current_app.logger.info("Inbound call from non-customer %s: %s inbound assistant, no memory",
                                mask_phone(phone_number), suffix)
        return {
            "assistantId": assistant_id,
            "assistantOverrides": {
                "variableValues": {
                    config.MEMORY_VARIABLE: config.NO_MEMORY_TEXT,
                    config.INSTRUCTIONS_VARIABLE: config.NO_INSTRUCTIONS_TEXT,
                    **customer_variables(None, onboarding.timezone_guess(phone_number)),
                    **callback_variables(None),
                },
            },
        }

    current_app.logger.info("Handling assistant request for phone number %s.", mask_phone(phone_number))
    try:
        bank_id = memory.bank_id_for(phone_number)
        memories, is_new = memory.recall_for_phone(phone_number, api_key, config.INBOUND_MEMORY_TIMEOUT_SECONDS)
        current_app.logger.info("Inbound call: %s memory bank %s", "created" if is_new else "loaded", mask_bank_id(bank_id))
    except Exception:
        current_app.logger.exception("Memory lookup failed for inbound call %s; rejecting the call", mask_phone(phone_number))
        return {"error": config.INBOUND_NO_MEMORY_MESSAGE}

    return {
        "assistantId": customer["vapi_assistant_id"],
        "assistantOverrides": {
            "variableValues": {
                config.MEMORY_VARIABLE: memories,
                config.INSTRUCTIONS_VARIABLE: config.NO_INSTRUCTIONS_TEXT,
                **customer_variables(customer),
                **callback_variables(customer),
            },
        },
    }


def place_outbound_call(phone_number: str, assistant_id: str, assistant_phone_id: str, customer: dict | None,
                        instructions: str | None = None) -> str | None:
    """Start an outbound Vapi call with customer variables and, for customers, their Hindsight memories.

    Returns the Vapi call id. Customers are never called without memory (raises MemoryUnavailable);
    non-customers (customer=None) haven't consented to memory and are called without it.
    """
    vapi_key = config.env("VAPI_API_KEY")
    if not vapi_key:
        raise OutboundCallError("VAPI_API_KEY must be configured.", 503)

    if customer is None:
        memories = config.NO_MEMORY_TEXT
        current_app.logger.info("Outbound call to non-customer %s: no memory", mask_phone(phone_number))
    else:
        hindsight_key = config.env("HINDSIGHT_API_KEY")
        if not hindsight_key:
            raise OutboundCallError("HINDSIGHT_API_KEY must be configured.", 503)
        try:
            bank_id = memory.bank_id_for(phone_number)
            memories, is_new = memory.recall_for_phone(phone_number, hindsight_key, config.OUTBOUND_MEMORY_TIMEOUT_SECONDS)
            if is_new:
                current_app.logger.info("Created new memory bank with name '%s'", mask_bank_id(bank_id))
            else:
                current_app.logger.info("Loaded existing memory bank with name '%s'", mask_bank_id(bank_id))
        except Exception as e:
            current_app.logger.exception("Memory lookup failed for %s", mask_phone(phone_number))
            raise MemoryUnavailable("Could not load customer memory. Please try again later.", 502) from e
    overrides = {"variableValues": {
        config.MEMORY_VARIABLE: memories,
        config.INSTRUCTIONS_VARIABLE: instructions or config.NO_INSTRUCTIONS_TEXT,  # staff instructions, if any
        **customer_variables(customer),
        **callback_variables(customer),
    }}
    current_app.logger.info("Outbound call memories: %s", "none recalled" if memories == config.NO_MEMORY_TEXT else "injected")

    try:
        response = requests.post(
            config.VAPI_CALL_URL,
            headers={"Authorization": f"Bearer {vapi_key}"},
            json={
                "assistantId": assistant_id,
                "phoneNumberId": assistant_phone_id,
                "assistantOverrides": overrides,
                "customer": {"number": phone_number},
            },
            timeout=10,
        )
    except requests.RequestException as e:
        current_app.logger.exception("Failed to create outbound call")
        # A timeout or disconnect can happen after Vapi accepted the request. The pre-dial attempt
        # must stay open so the dispatcher can recover the call id instead of placing a duplicate.
        raise OutboundCallError("Could not reach the calling service. Please try again later.", 502,
                                may_have_started=True) from e

    current_app.logger.info("Vapi create call responded with %s", response.status_code)
    if not response.ok:
        # Don't log the response body: Vapi may echo submitted phone, memory or instructions.
        current_app.logger.warning("Vapi rejected outbound call (%s)", response.status_code)
        raise OutboundCallError("The calling service could not start the call.", 502)
    try:
        body = response.json()
    except ValueError as e:
        current_app.logger.error("Vapi create-call response was not valid JSON (%s)", type(e).__name__)
        return None
    call_id = body.get("id") if isinstance(body, dict) else None
    call_id = call_id.strip() if isinstance(call_id, str) else ""
    return call_id or None


def get_vapi_call(call_id: str) -> dict | None:
    """Fetch a call from Vapi (status, endedReason, endedAt, ...); None if it can't be loaded."""
    vapi_key = config.env("VAPI_API_KEY")
    if not vapi_key:
        return None
    try:
        response = requests.get(
            f"{config.VAPI_CALL_URL}/{urllib.parse.quote(call_id, safe='')}",
            headers={"Authorization": f"Bearer {vapi_key}"},
            timeout=5,
        )
        if not response.ok:
            current_app.logger.warning("Vapi get call %s responded with %s", call_id, response.status_code)
            return None
        try:
            call = response.json()
        except ValueError as e:
            current_app.logger.error("Vapi get call %s response was not valid JSON (%s)", call_id, type(e).__name__)
            return None
    except requests.RequestException:
        current_app.logger.exception("Failed to load Vapi call %s", call_id)
        return None
    if not isinstance(call, dict):
        current_app.logger.warning(
            "Vapi get call %s response must be a JSON object; got %s", call_id, _json_type(call)
        )
        return None
    return call


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def list_outbound_calls(phone_number_id: str, created_from: datetime, created_to: datetime) -> list[dict]:
    """Vapi calls placed from one of our phone numbers, created in [created_from, created_to].

    Vapi can't filter by the customer's number; callers match `customer.number` themselves.
    Raises VapiUnavailable if Vapi can't be asked.
    """
    vapi_key = config.env("VAPI_API_KEY")
    if not vapi_key:
        raise VapiUnavailable("VAPI_API_KEY is not configured.")
    try:
        response = requests.get(
            config.VAPI_CALL_URL,
            headers={"Authorization": f"Bearer {vapi_key}"},
            params={
                "phoneNumberId": phone_number_id,
                "createdAtGe": _iso_utc(created_from),
                "createdAtLe": _iso_utc(created_to),
                "limit": 100,
            },
            timeout=10,
        )
        response.raise_for_status()
        calls = response.json()
    except ValueError:
        # requests.JSONDecodeError is both ValueError and RequestException; keep the
        # malformed-response message instead of misreporting it as a network failure.
        raise VapiUnavailable("Vapi call list response was not valid JSON.") from None
    except requests.RequestException as e:
        raise VapiUnavailable(f"Could not list Vapi calls: {type(e).__name__}") from e
    if not isinstance(calls, list):
        raise VapiUnavailable("Vapi call list response must be a JSON array.")
    return [call for call in calls if isinstance(call, dict)]


def _archive_get(path: str, params: dict | None = None) -> requests.Response:
    """Send an authenticated call read, never forwarding the key through a redirect.

    Args:
        path: Encoded suffix below the configured call API URL.
        params: Optional query parameters for a call-list request.

    Returns:
        The unread provider response, including redirects and error statuses.

    Raises:
        VapiUnavailable: Credentials are missing or the request fails.
    """
    key = config.env("VAPI_API_KEY")
    if not key:
        raise VapiUnavailable("missing_key")
    try:
        return requests.get(f"{config.VAPI_CALL_URL}{path}", headers={"Authorization": f"Bearer {key}"},
                            params=params, timeout=(3, 8), allow_redirects=False)
    except requests.RequestException:
        raise VapiUnavailable("provider_read_failed") from None


def _archive_request(path: str, params: dict | None = None) -> object:
    """Read provider JSON without treating failed requests as empty results; raises VapiUnavailable."""
    response = _archive_get(path, params)
    try:
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        raise VapiUnavailable("provider_read_failed") from None


def recording_url(call_id: str) -> str | None:
    """Resolve the stereo recording's (customer and assistant channels) short-lived signed download URL.

    Args:
        call_id: Provider call ID.

    Returns:
        The HTTPS redirect target, or None while Vapi has no recording (404).

    Raises:
        VapiUnavailable: The request fails or Vapi answers without a usable redirect.
    """
    response = _archive_get(f"/{urllib.parse.quote(call_id, safe='')}/stereo-recording")
    if response.status_code == 404:
        return None
    location = response.headers.get("Location", "")
    if response.status_code not in (301, 302, 303, 307, 308) or not location.startswith("https://"):
        raise VapiUnavailable("invalid_recording_redirect")
    return location


def list_archive_calls(created_from: datetime, created_to: datetime) -> list[dict]:
    """List all call types in an inclusive creation-time window without dropping bad rows.

    Args:
        created_from: UTC lower bound of the scan window.
        created_to: UTC upper bound of the scan window.

    Returns:
        Provider call objects, up to the configured page limit.

    Raises:
        VapiUnavailable: The request fails or any result is not a call object.
    """
    calls = _archive_request("", {"createdAtGe": _iso_utc(created_from), "createdAtLe": _iso_utc(created_to),
                                  "limit": config.ARCHIVE_LIST_LIMIT})
    if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
        raise VapiUnavailable("invalid_call_list")
    return calls


def get_archive_call(call_id: str) -> dict:
    """Fetch one provider call, raising on errors rather than returning an empty result."""
    call = _archive_request(f"/{urllib.parse.quote(call_id, safe='')}")
    if not isinstance(call, dict):
        raise VapiUnavailable("invalid_call_detail")
    return call


def list_vapi(resource: str, vapi_key: str) -> list[dict]:
    """List Vapi phone numbers or assistants as [{id, name}] sorted by name; raises on errors."""
    response = requests.get(
        f"{config.VAPI_API_URL}/{resource}",
        headers={"Authorization": f"Bearer {vapi_key}"},
        params={"limit": 1000},
        timeout=10,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        raise ValueError(f"Vapi {resource} list response was not valid JSON.") from None
    if not isinstance(body, list):
        raise ValueError(f"Vapi {resource} list response must be a JSON array.")
    items = []
    for item in body:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        item_id = item["id"].strip()
        if not item_id:
            continue
        # Phone numbers without a usable name fall back to the number itself.
        name = item.get("name")
        if not isinstance(name, str) or not name:
            number = item.get("number")
            name = number if isinstance(number, str) and number else "Unnamed"
        items.append({"id": item_id, "name": name})
    return sorted(items, key=lambda item: item["name"].lower())
