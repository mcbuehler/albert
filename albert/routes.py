"""HTTP endpoints. Request parsing and responses only; the work happens in the albert modules."""

from pathlib import Path
from urllib.parse import unquote
from uuid import UUID
from zoneinfo import available_timezones

import psycopg
import requests
from flask import Blueprint, Response, current_app, g, jsonify, request, send_from_directory

from albert import archive, auth, config, db, memory, notify, onboarding, scheduling, storage, timezones
from albert.dispatcher import dispatch_due_calls
from albert.masking import mask_phone
from albert.search import search_brave
from albert.validation import (
    VAPI_ID_RE,
    is_valid_vapi_id,
    normalize_phone,
    parse_instructions,
    parse_local_time,
    vapi_ids,
)
from albert.vapi import OutboundCallError, assistant_request_response, caller_number, list_vapi, place_outbound_call
from albert.weather import get_weather

api_bp = Blueprint("api", __name__)
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


# --- Page ------------------------------------------------------------------

@api_bp.get('/')
def index():
    """Serve the hello world page."""
    return send_from_directory(PUBLIC_DIR, "index.html")


@api_bp.get('/favicon.svg')
def favicon():
    """Serve the owl favicon when running locally (Vercel serves public/ itself)."""
    return send_from_directory(PUBLIC_DIR, request.path.lstrip("/"))


@api_bp.get('/assets/<path:filename>')
def assets(filename: str):
    """Serve the page's CSS/JS when running locally (Vercel serves public/ itself)."""
    return send_from_directory(PUBLIC_DIR / "assets", filename)


@api_bp.get('/api/timezones')
def list_timezones():
    """Public reference data for the onboarding form's time zone search."""
    response = jsonify(timezones.list_timezones())
    response.cache_control.public = True
    response.cache_control.max_age = 86400
    return response


@api_bp.get('/api/health')
def health():
    """Public liveness check for an uptime monitor: required env vars set and the database reachable.

    Only says which check failed (details go to the log); no external services, so pinging it often is cheap.
    """
    checks = {}
    missing = [name for name in config.REQUIRED_ENV if not config.env(name)]
    if missing:
        current_app.logger.error("Health check: missing env vars %s", ", ".join(missing))
    checks["config"] = "fail" if missing else "ok"
    try:
        db.ping()
        checks["database"] = "ok"
    except Exception:
        current_app.logger.exception("Health check: database unreachable")
        checks["database"] = "fail"
    ok = all(value == "ok" for value in checks.values())
    response = jsonify({"status": "ok" if ok else "fail", "checks": checks})
    response.cache_control.no_store = True
    return response, 200 if ok else 503


@api_bp.get('/api/page-config')
def page_config():
    """Public settings for the backend page: Supabase login (the publishable key is meant to be public)
    and the calling hours, so the page doesn't hard-code them."""
    url = config.env("SUPABASE_URL")
    key = config.env("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key:
        current_app.logger.error("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be configured for the login")
        return jsonify({"error": "Login is not configured."}), 503
    return jsonify({
        "supabase_url": url,
        "supabase_publishable_key": key,
        "calling_start": config.CALLING_START.strftime("%H:%M"),
        "calling_end": config.CALLING_END.strftime("%H:%M"),
    })


# --- Vapi server events and tools ------------------------------------------

@api_bp.get('/api/cron/archive')
@auth.require_cron_or_staff
def archive_calls() -> Response | tuple[Response, int]:
    """Run guarded call discovery for an authenticated cron or staff request."""
    if not config.ARCHIVE_CAPTURE_ENABLED:
        return jsonify({"error": "Call archival is not enabled yet."}), 503
    try:
        result = archive.discover_calls()
        response = jsonify(result)
        response.cache_control.no_store = True
        return response, 200 if result["ok"] else 503
    except Exception as exc:
        current_app.logger.error("Call discovery failed (%s)", type(exc).__name__)
        return jsonify({"error": "Call discovery failed."}), 503


@api_bp.get('/api/archive/status')
@auth.require_staff
def archive_status() -> Response | tuple[Response, int]:
    """Return private archive diagnostics without querying the DB while disabled."""
    try:
        result = db.get_archive_status() if config.ARCHIVE_CAPTURE_ENABLED else {}
        response = jsonify({"enabled": config.ARCHIVE_CAPTURE_ENABLED, **result})
        response.cache_control.no_store = True
        return response
    except Exception as exc:
        current_app.logger.error("Could not load call archive status (%s)", type(exc).__name__)
        return jsonify({"error": "Could not load call archive status."}), 503


def _call_list_query(args: dict) -> tuple[dict | None, str | None]:
    """Validate the Calls list query string; returns (db.list_calls arguments, error).

    Args:
        args: Request args: `q` (name or number), `status` (comma-separated config.CALL_STATUSES or
            "past"), `sort` (db.CALL_SORT_COLUMNS, default call_date), `order` (asc/desc, default desc),
            `limit` (1–CALL_LIST_MAX_LIMIT) and `offset`.
    """
    statuses = set()
    for status in filter(None, args.get("status", "").split(",")):
        if status != "past" and status not in config.CALL_STATUSES:
            return None, "Unknown status."
        statuses.update(config.CALL_PAST_STATUSES if status == "past" else (status,))
    sort, order = args.get("sort", "call_date"), args.get("order", "desc")
    if sort not in db.CALL_SORT_COLUMNS or order not in ("asc", "desc"):
        return None, "Unknown sort order."
    try:
        limit, offset = int(args.get("limit", config.CALL_LIST_DEFAULT_LIMIT)), int(args.get("offset", 0))
    except ValueError:
        return None, "limit and offset must be whole numbers."
    if not 1 <= limit <= config.CALL_LIST_MAX_LIMIT or offset < 0:
        return None, f"limit must be 1–{config.CALL_LIST_MAX_LIMIT} and offset at least 0."
    return {"search": args.get("q", "").strip()[:100], "statuses": sorted(statuses), "sort": sort,
            "descending": order == "desc", "limit": limit, "offset": offset}, None


@api_bp.get('/api/calls')
@auth.require_staff
def list_calls() -> Response | tuple[Response, int]:
    """One page of archived calls, local attempts and upcoming callbacks, searched, filtered and sorted."""
    query, error = _call_list_query(request.args)
    if error:
        return jsonify({"error": error}), 400
    try:
        response = jsonify(db.list_calls(**query))
        response.cache_control.no_store = True
        return response
    except Exception as exc:
        current_app.logger.error("Could not list calls (%s)", type(exc).__name__)
        return jsonify({"error": "Could not load calls. Please try again later."}), 503


def _call_entry(entry_id: str) -> tuple[str, UUID] | None:
    """Split a typed Calls entry ID ("call:", "attempt:" or "scheduled:" + UUID); None if malformed.

    The page URL-encodes the ID; Vercel passes the path still encoded ("call%3A…"), gunicorn decoded.
    """
    kind, _, raw = unquote(entry_id).partition(":")
    try:
        return (kind, UUID(raw)) if kind in ("call", "attempt", "scheduled") else None
    except ValueError:
        return None


@api_bp.get('/api/calls/<string:entry_id>')
@auth.require_staff
def get_call(entry_id: str) -> Response | tuple[Response, int]:
    """One call or callback with transcript, schedule details and staff notes."""
    entry = _call_entry(entry_id)
    try:
        call = db.get_call(*entry) if entry else None
    except Exception as exc:
        current_app.logger.error("Could not load call (%s)", type(exc).__name__)
        return jsonify({"error": "Could not load the call. Please try again later."}), 503
    if not call:
        current_app.logger.info("Call %s not found (%s)", entry_id[:60], "listed ID" if entry else "malformed ID")
        return jsonify({"error": "Call not found. It may have been deleted."}), 404
    response = jsonify(call)
    response.cache_control.no_store = True
    return response


@api_bp.post('/api/calls/<string:entry_id>/audio-url')
@auth.require_staff
def call_audio_url(entry_id: str) -> Response | tuple[Response, int]:
    """Short-lived playback link for a call's saved recording; the browser streams it from Supabase."""
    entry = _call_entry(entry_id)
    try:
        key = db.call_audio_key(*entry) if entry else None
        url = storage.signed_url(key, config.ARCHIVE_PLAYBACK_SECONDS) if key else None
    except Exception as exc:
        current_app.logger.error("Could not create playback link (%s)", type(exc).__name__)
        return jsonify({"error": "The recording can't be played right now. Please try again later."}), 503
    if not url:
        return jsonify({"error": "No recording is available for this call."}), 404
    response = jsonify({"url": url, "expires_in": config.ARCHIVE_PLAYBACK_SECONDS})
    response.cache_control.no_store = True
    return response


@api_bp.post('/api/calls/<string:entry_id>/notes')
@auth.require_staff
def add_call_note(entry_id: str) -> Response | tuple[Response, int]:
    """Append a staff note; the author is the verified login (name from its profile, else its email)."""
    data = request.get_json(silent=True)
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, str) or not body.strip() or len(body) > config.CALL_NOTE_MAX_CHARS:
        return jsonify({"error": f"Write a note of at most {config.CALL_NOTE_MAX_CHARS} characters."}), 400
    entry = _call_entry(entry_id)
    author = (g.user.get("name") or g.user.get("email") or "Staff")[:config.CALL_NOTE_AUTHOR_MAX_CHARS]
    try:
        result = db.add_call_note(*entry, g.user["id"], author, body.strip()) if entry else None
    except Exception as exc:
        current_app.logger.error("Could not save call note (%s)", type(exc).__name__)
        return jsonify({"error": "The note couldn't be saved. Please try again."}), 503
    if not result:
        return jsonify({"error": "Call not found. It may have been deleted."}), 404
    current_app.logger.info("User %s added a note to %s", g.user["id"], result["entry_id"])
    return jsonify(result), 201


@api_bp.post('/api/webhook')
@auth.require_api_token
def vapi_webhook():
    """Answer Vapi's assistant-request; store customers' end-of-call-report transcripts in Hindsight.

    Reports of calls that reached voicemail or weren't answered are not stored.
    """
    event = request.get_json(silent=True)  # no/invalid JSON → our 400 below, not Flask's 415
    if not isinstance(event, dict):
        current_app.logger.warning("Rejected Vapi webhook: expected a JSON object")
        return jsonify({"error": "Expected a JSON object."}), 400

    message = event.get("message")
    event_type = message.get("type") if isinstance(message, dict) else None
    current_app.logger.info("Received Vapi webhook event: %s", event_type or "unknown")

    # hindsight-vapi only acts on these two; any other event (status updates etc.) needs nothing from us.
    if event_type not in ("assistant-request", "end-of-call-report"):
        current_app.logger.info("Ignoring Vapi webhook event: %s", event_type or "unknown")
        return jsonify({})

    # Voicemail or no answer: there's no conversation to remember (a voicemail greeting isn't a memory).
    if event_type == "end-of-call-report" and message.get("endedReason") in config.NOT_REACHED_REASONS:
        current_app.logger.info("Not storing Vapi end-of-call-report: customer not reached (%s)", message["endedReason"])
        return jsonify({})

    api_key = config.env("HINDSIGHT_API_KEY")
    if not api_key:
        current_app.logger.warning("Rejected Vapi webhook: HINDSIGHT_API_KEY is not configured")
        return jsonify({"error": "HINDSIGHT_API_KEY is not configured."}), 503

    if event_type == "assistant-request":
        return jsonify(assistant_request_response(message, api_key))

    # One bank per customer, keyed by their phone number. Only customers have consented to memory:
    # anyone else's transcript is not stored.
    phone_number = caller_number(message)
    if not phone_number:
        current_app.logger.warning("Skipping Vapi webhook %s: no customer phone number", event_type)
        return jsonify({})
    try:
        customer = db.get_customer_by_phone(phone_number)
    except Exception:
        current_app.logger.exception("Customer lookup failed; not storing Vapi %s for %s", event_type, mask_phone(phone_number))
        return jsonify({})
    if not customer:
        current_app.logger.info("Not storing Vapi %s for non-customer %s (no consent)", event_type, mask_phone(phone_number))
        return jsonify({})
    try:
        bank_id = memory.bank_id_for(phone_number)
    except ValueError:
        current_app.logger.exception("Not storing Vapi %s for %s: number can't be normalized", event_type, mask_phone(phone_number))
        return jsonify({})

    try:
        response = memory.process_event(event, bank_id, api_key)
    except Exception:
        current_app.logger.exception("Failed to process Vapi webhook event: %s", event_type or "unknown")
        raise

    current_app.logger.info("Processed Vapi webhook event: %s", event_type or "unknown")
    return jsonify(response)


def _api_request_tool(tool_name: str, tool, argument_names: tuple[str, ...]):
    """Run one of our Vapi API Request tools.

    Body: the AI-filled arguments plus `customer_number` (static body field "{{customer.number}}", filled by
    Vapi, not the AI); the call id comes in the `X-Call-Id` header. Answers `{"result": "<sentence>"}` with
    200 (Vapi treats non-2xx as a failed tool). The phone number never comes from the AI's arguments.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "customer_number" not in body:
        # Log only the shape of what arrived (never values: they can contain phone numbers).
        current_app.logger.warning(
            "Rejected %s tool request: expected a body with customer_number; got "
            "content type %r, body %s, top-level keys %s",
            tool_name,
            request.content_type,
            type(body).__name__ if body is not None else "not JSON",
            sorted(body)[:20] if isinstance(body, dict) else "-",
        )
        expected = " and ".join((*argument_names, "customer_number"))
        return jsonify({"error": f"Expected a body with {expected} "
                                 "(static field \"{{customer.number}}\")."}), 400

    raw_number = body.get("customer_number")
    phone_number = normalize_phone(raw_number)
    if phone_number is None:
        phone_number = raw_number.strip() if isinstance(raw_number, str) else None
        # Usually a setup problem: the static body field isn't "{{customer.number}}" (or didn't resolve).
        current_app.logger.warning("%s API request: customer_number is not a phone number; "
                                   "check the tool's static body field", tool_name)
    call_id = (request.headers.get("X-Call-Id") or "").strip()[:100] or None
    current_app.logger.info("Vapi API request tool %s (call %s)", tool_name, call_id)
    result = tool({name: body.get(name) for name in argument_names}, phone_number, call_id)
    return jsonify({"result": result})


@api_bp.post('/api/tools/schedule-callback')
@auth.require_api_token
def schedule_callback_tool_endpoint():
    """Vapi `schedule_callback` API Request tool: body `local_time`, optional `instructions` (AI) +
    `customer_number` (static)."""
    return _api_request_tool("schedule_callback", scheduling.schedule_callback_tool, ("local_time", "instructions"))


@api_bp.post('/api/tools/cancel-callback')
@auth.require_api_token
def cancel_callback_tool_endpoint():
    """Vapi `cancel_callback` API Request tool: body `callback_time` (AI, optional when only one callback is
    pending) + `customer_number` (static)."""
    return _api_request_tool("cancel_callback", scheduling.cancel_callback_tool, ("callback_time",))


@api_bp.post('/api/tools/reschedule-callback')
@auth.require_api_token
def reschedule_callback_tool_endpoint():
    """Vapi `reschedule_callback` API Request tool: body `callback_time` (AI, optional when only one callback
    is pending), `new_local_time` (AI) + `customer_number` (static)."""
    return _api_request_tool("reschedule_callback", scheduling.reschedule_callback_tool,
                             ("callback_time", "new_local_time"))


@api_bp.post('/api/tools/onboard-customer')
@auth.require_api_token
def onboard_customer_tool_endpoint():
    """Vapi `onboard_customer` API Request tool: body `first_name`, `last_name`, `timezone` (optional),
    `notes_consent` (AI) + `customer_number` (static). Signs up the caller as a customer marked for review."""
    return _api_request_tool("onboard_customer", onboarding.onboard_customer_tool,
                             ("first_name", "last_name", "timezone", "notes_consent"))


@api_bp.post('/api/tools/notify-team')
@auth.require_api_token_or_staff
def notify_team_tool_endpoint():
    """Vapi `notify_team` API Request tool: body `reason`, `contact_name`, `organization`, `summary` (AI)
    + `customer_number` (static). Stored in team_notifications and emailed to TEAM_EMAIL.
    Staff use it for the Debug tab's test email."""
    return _api_request_tool("notify_team", notify.notify_team_tool,
                             ("reason", "contact_name", "organization", "summary"))


@api_bp.get('/api/tools/search/<string:query>')
@auth.require_api_token
def handle_search(query: str):
    """Return Brave web search results for the given query."""
    query = query.strip()
    search_result, message = search_brave(query)
    results = [{"result": search_result, "message": message}]
    return jsonify({"results": results})


@api_bp.get('/api/tools/weather/<string:city>/<string:language>')
@auth.require_api_token
def handle_vapi_weather(city: str, language: str = 'en'):
    """Return the weather for a city in Vapi's tool result format."""
    return jsonify({"results": [{"result": get_weather(city, language)}]})


@api_bp.get('/api/vapi/options')
@auth.require_staff
def vapi_options():
    """Phone numbers and assistants for the form dropdowns."""
    vapi_key = config.env("VAPI_API_KEY")
    if not vapi_key:
        return jsonify({"error": "VAPI_API_KEY must be configured."}), 503
    try:
        return jsonify({
            "phone_numbers": list_vapi("phone-number", vapi_key),
            "assistants": list_vapi("assistant", vapi_key),
        })
    except (requests.RequestException, ValueError, TypeError):
        current_app.logger.exception("Failed to list Vapi phone numbers / assistants")
        return jsonify({"error": "Could not load phone numbers and assistants from Vapi."}), 502


# --- Calls -----------------------------------------------------------------

@api_bp.post('/api/tools/call')
@auth.require_staff
def create_outbound_call():
    """Start an outbound Vapi call with Hindsight memories injected into the assistant."""
    event = request.get_json(silent=True)  # no/invalid JSON → our 400 below, not Flask's 415
    if not isinstance(event, dict):
        current_app.logger.warning("Rejected outbound call: expected a JSON object")
        return jsonify({"error": "Expected a JSON object."}), 400
    phone_number = normalize_phone(event.get("customer_phone_number"))
    if phone_number is None:
        current_app.logger.warning("Rejected outbound call: invalid customer_phone_number")
        return jsonify({"error": "customer_phone_number must be an international number with country code, e.g. +41 79 123 45 67."}), 400
    instructions, error = parse_instructions(event.get("instructions"))
    if error:
        return jsonify({"error": error}), 400

    # Not lookup_customer(): a DB error must not make a customer look like a non-customer,
    # which would call them without memory.
    try:
        customer = db.get_customer_by_phone(phone_number)
    except Exception:
        current_app.logger.exception("Customer lookup failed for outbound call to %s", mask_phone(phone_number))
        return jsonify({"error": "Could not start the call. Please try again later."}), 500

    # assistant_id / assistant_phone_id are optional overrides of the customer's defaults; like
    # everywhere else they must be Vapi ids (UUIDs) and are normalised to lowercase.
    overrides = {}
    for name in ("assistant_id", "assistant_phone_id"):
        value = event.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if not is_valid_vapi_id(value):
            current_app.logger.warning("Rejected outbound call: invalid %s", name)
            return jsonify({"error": f"{name} must be a Vapi id (UUID, e.g. 3f2b1c9e-8d4a-4b6e-9f10-2a7c5d8e1b34)."}), 400
        overrides[name] = value.strip().lower()
    assistant_id = overrides.get("assistant_id") or (customer or {}).get("vapi_assistant_id")
    assistant_phone_id = overrides.get("assistant_phone_id") or (customer or {}).get("vapi_phone_number_id")
    for name, value in (("assistant_id", assistant_id), ("assistant_phone_id", assistant_phone_id)):
        if not isinstance(value, str) or not value.strip():
            current_app.logger.warning("Rejected outbound call: missing %s", name)
            return jsonify({"error": f"{name} is required for phone numbers that aren't a customer."}), 400

    try:
        attempt_id = db.reserve_adhoc_attempt(
            (customer or {}).get("id"), phone_number, assistant_phone_id, assistant_id,
        )
    except db.CustomerOptedOut:
        current_app.logger.warning("Rejected outbound call to %s: customer opted out", mask_phone(phone_number))
        return jsonify({"error": "This customer has opted out of calls."}), 409
    except db.OutboundCallBusy:
        current_app.logger.warning("Rejected outbound call to %s: customer or outbound line is busy",
                                   mask_phone(phone_number))
        return jsonify({"error": "A call is already in progress for this customer or outbound phone number."}), 409
    except db.CustomerChanged:
        current_app.logger.warning("Rejected outbound call to %s: customer changed during reservation",
                                   mask_phone(phone_number))
        return jsonify({"error": "The customer changed while the call was being prepared. Please try again."}), 409
    except Exception:
        current_app.logger.exception("Failed to reserve outbound call to %s", mask_phone(phone_number))
        return jsonify({"error": "Could not start the call. Please try again later."}), 500

    try:
        call_id = place_outbound_call(phone_number, assistant_id, assistant_phone_id, customer, instructions)
    except OutboundCallError as e:
        if not e.may_have_started:
            try:
                db.delete_undialled_attempt(attempt_id)
            except Exception:
                current_app.logger.exception("Failed to release outbound call reservation %s", attempt_id)
        return jsonify({"error": e.message}), e.status
    if not call_id:
        # Vapi may have accepted the call but returned a malformed response. Leave the reservation
        # open; the dispatcher resolves attempts without ids from Vapi's call list.
        current_app.logger.error("Vapi accepted an outbound call but returned no call id (attempt %s)", attempt_id)
        return jsonify({"error": "The calling service returned an invalid response."}), 502
    try:
        if not db.set_attempt_call_id(attempt_id, call_id):
            current_app.logger.error("Could not attach Vapi call %s to reserved attempt %s", call_id, attempt_id)
    except Exception:
        # The reservation remains open and recover_unrecorded_attempts can attach the id later.
        current_app.logger.exception("Failed to record Vapi call %s on reserved attempt %s", call_id, attempt_id)
    # Don't echo Vapi's full call object: it contains the injected memories.
    return jsonify({"id": call_id}), 201


@api_bp.get('/api/cron/dispatch')
@auth.require_cron_or_staff
def dispatch_scheduled_call():
    """Vercel Cron (every minute): start due callbacks, at most one running call per Vapi phone number."""
    dispatched, ok = dispatch_due_calls()
    if not ok:
        return jsonify({"error": "Dispatcher failed.", "dispatched": dispatched}), 500
    return jsonify({"dispatched": dispatched})


@api_bp.get('/api/dispatcher/status')
@auth.require_staff
def dispatcher_status():
    """When the dispatcher last ran (for the staff page's "Last dispatch" line)."""
    try:
        status = db.get_dispatcher_status()
    except Exception:
        current_app.logger.exception("Failed to load the dispatcher status")
        return jsonify({"error": "Could not load the dispatcher status."}), 500
    if not status:
        return jsonify({"last_run_at": None, "last_run_ok": None, "last_started": 0})
    return jsonify(db.json_row(status))


# --- Customers -------------------------------------------------------------

@api_bp.get('/api/customers')
@auth.require_staff
def list_customers():
    """Customers with next pending callback and last call, for the overview and the dropdowns."""
    try:
        return jsonify([db.json_row(row) for row in db.list_customers()])
    except Exception:
        current_app.logger.exception("Failed to list customers")
        return jsonify({"error": "Could not load customers. Please try again later."}), 500


@api_bp.post('/api/customers')
@auth.require_staff
def create_customer():
    """Onboard a customer with their name, phone number and time zone."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object."}), 400
    fields, error = _customer_fields(data, partial=False)
    if error:
        return jsonify({"error": error}), 400
    phone = normalize_phone(data.get("phone"))  # e.g. "+41 79 738 11 55" / "0041797381155" -> "+41797381155"
    if phone is None:
        return jsonify({
            "error": "phone must be an international number with country code, "
                     "e.g. +41 79 123 45 67 or 0041 79 123 45 67."
        }), 400
    ids, error = vapi_ids(data, required=True)
    if error:
        return jsonify({"error": error}), 400

    # Create the memory bank first: a customer is only saved once their memory exists
    # (calls never run without memory). Idempotent, so retrying an onboarding is safe.
    hindsight_key = config.env("HINDSIGHT_API_KEY")
    if not hindsight_key:
        current_app.logger.error("HINDSIGHT_API_KEY is not configured; cannot onboard customers")
        return jsonify({"error": "Customer memory is not configured."}), 503
    try:
        created = memory.ensure_bank(phone, hindsight_key, config.ONBOARDING_MEMORY_TIMEOUT_SECONDS)
    except Exception:
        current_app.logger.exception("Failed to create memory bank for %s", mask_phone(phone))
        return jsonify({"error": "Could not set up the customer's memory. Please try again later."}), 502
    current_app.logger.info("Memory bank for %s %s", mask_phone(phone), "created" if created else "already existed")

    try:
        customer = db.insert_customer(
            fields["first_name"], fields["middle_name"], fields["last_name"], phone, fields["timezone"], **ids
        )
    except psycopg.errors.UniqueViolation:
        current_app.logger.warning("Rejected customer %s: phone already exists", mask_phone(phone))
        return jsonify({"error": "A customer with this phone number already exists."}), 409
    except Exception:
        current_app.logger.exception("Failed to create customer %s", mask_phone(phone))
        return jsonify({"error": "Could not save the customer. Please try again later."}), 500

    current_app.logger.info("Created customer %s (%s)", customer["id"], mask_phone(phone))
    return jsonify(db.json_row(customer)), 201


def _customer_fields(data: dict, partial: bool) -> tuple[dict, str | None]:
    """Names and time zone from a staff request body; returns (fields, error).

    partial: only the fields present in the body are read (editing); otherwise all are (onboarding).
    An empty middle name becomes None.
    """
    fields = {}
    for field in ("first_name", "middle_name", "last_name"):
        if partial and field not in data:
            continue
        value = data.get(field)
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value.strip()) > 100:
            return {}, f"{field} must be text (max 100 characters)."
        fields[field] = value.strip()
    if any(field in fields and not fields[field] for field in ("first_name", "last_name")):
        return {}, "first_name and last_name are required."
    if "middle_name" in fields:
        fields["middle_name"] = fields["middle_name"] or None
    if not partial or "timezone" in data:
        timezone = data.get("timezone")
        if not isinstance(timezone, str) or timezone not in available_timezones():
            return {}, "timezone must be an IANA name, e.g. Europe/Zurich."
        fields["timezone"] = timezone
    return fields, None


@api_bp.post('/api/customers/<string:customer_id>')
@auth.require_staff
def update_customer(customer_id: str):
    """Edit a customer. Body: any of first_name, middle_name, last_name, timezone, vapi_assistant_id.

    Only the fields sent are changed. The default assistant is used for inbound calls and callbacks
    without their own. A new time zone keeps pending callbacks at the same local time (`callbacks_moved`).
    """
    if not VAPI_ID_RE.fullmatch(customer_id):  # customer ids are UUIDs too
        return jsonify({"error": "Unknown customer."}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object."}), 400
    changes, error = _customer_fields(data, partial=True)
    if error:
        return jsonify({"error": error}), 400
    if "vapi_assistant_id" in data:
        assistant_id = data["vapi_assistant_id"]
        if not is_valid_vapi_id(assistant_id):
            return jsonify({"error": "vapi_assistant_id must be a Vapi id (UUID, e.g. 3f2b1c9e-8d4a-4b6e-9f10-2a7c5d8e1b34)."}), 400
        changes["vapi_assistant_id"] = assistant_id.strip().lower()
    if not changes:
        return jsonify({"error": "Nothing to change."}), 400
    try:
        customer = db.update_customer(customer_id, changes)
    except Exception:
        current_app.logger.exception("Failed to update customer %s", customer_id)
        return jsonify({"error": "Could not save the customer. Please try again later."}), 500
    if not customer:
        return jsonify({"error": "Unknown customer."}), 404
    # Field names only: names are personal data.
    current_app.logger.info("User %s changed %s of customer %s (%d callbacks moved)",
                            g.user["id"], ", ".join(sorted(changes)), customer_id, customer["callbacks_moved"])
    return jsonify(customer)


@api_bp.post('/api/customers/<string:customer_id>/reviewed')
@auth.require_staff
def mark_customer_reviewed(customer_id: str):
    """Mark a customer who signed up on an inbound call as reviewed (clears needs_review)."""
    if not VAPI_ID_RE.fullmatch(customer_id):  # customer ids are UUIDs too
        return jsonify({"error": "Unknown customer."}), 404
    try:
        found = db.mark_customer_reviewed(customer_id)
    except Exception:
        current_app.logger.exception("Failed to mark customer %s as reviewed", customer_id)
        return jsonify({"error": "Could not save the review. Please try again later."}), 500
    if not found:
        return jsonify({"error": "Unknown customer."}), 404
    current_app.logger.info("User %s marked customer %s as reviewed", g.user["id"], customer_id)
    return jsonify({"id": customer_id, "needs_review": False})


# --- Scheduled callbacks ---------------------------------------------------

@api_bp.post('/api/scheduled-calls')
@auth.require_staff
def create_scheduled_call():
    """Schedule a one-time callback for a customer at a customer-local time (any time; only retries keep calling hours)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object."}), 400
    phone = normalize_phone(data.get("phone"))
    local_time = data.get("local_time")
    if phone is None:
        return jsonify({
            "error": "phone must be an international number with country code, "
                     "e.g. +41 79 123 45 67 or 0041 79 123 45 67."
        }), 400
    requested = parse_local_time(local_time)
    if requested is None:
        return jsonify({"error": "local_time must be a customer-local time without zone, e.g. 2026-10-17T14:30."}), 400
    overrides, error = vapi_ids(data, required=False)  # None = the customer's default
    if error:
        return jsonify({"error": error}), 400
    instructions, error = parse_instructions(data.get("instructions"))
    if error:
        return jsonify({"error": error}), 400

    try:
        customer = db.get_customer_by_phone(phone)
    except Exception:
        current_app.logger.exception("Customer lookup failed for %s", mask_phone(phone))
        return jsonify({"error": "Could not schedule the call. Please try again later."}), 500
    if not customer:
        return jsonify({"error": "No customer with this phone number."}), 404

    try:
        scheduled, due_local = scheduling.schedule_callback(customer, requested, instructions=instructions, **overrides)
    except scheduling.ScheduleError as e:
        current_app.logger.warning("Rejected callback for %s: scheduling returned HTTP %s",
                                   mask_phone(phone), e.status)
        return jsonify({"error": e.message}), e.status
    body = db.json_row(scheduled)
    body["due_local_time"] = due_local.isoformat(timespec="minutes")
    # 200 when the same callback already existed (idempotent), 201 when it was created.
    return jsonify(body), 200 if scheduled.get("already_scheduled") else 201


# --- Danger zone: delete a customer / erase their memory --------------------

def _confirmed_customer(customer_id: str):
    """The customer for a danger-zone action, the Hindsight key, and None; or (None, None, error response).

    The request must repeat the customer's phone number (`confirm_phone`), checked here as well as on the page.
    """
    if not VAPI_ID_RE.fullmatch(customer_id):  # customer ids are UUIDs too
        return None, None, (jsonify({"error": "Unknown customer."}), 404)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, None, (jsonify({"error": "Expected a JSON object."}), 400)
    try:
        customer = db.get_customer(customer_id)
    except Exception:
        current_app.logger.exception("Customer lookup failed for %s", customer_id)
        return None, None, (jsonify({"error": "Could not load the customer. Please try again later."}), 500)
    if not customer:
        return None, None, (jsonify({"error": "Unknown customer."}), 404)
    if normalize_phone(data.get("confirm_phone")) != customer["phone"]:
        return None, None, (jsonify({"error": "The phone number doesn't match this customer."}), 400)
    if customer["calling_now"]:
        # The end-of-call report would otherwise store this call's transcript right after.
        return None, None, (jsonify({"error": "A call with this customer is in progress. Try again when it has ended."}), 409)
    hindsight_key = config.env("HINDSIGHT_API_KEY")
    if not hindsight_key:
        current_app.logger.error("HINDSIGHT_API_KEY is not configured; cannot change customer memory")
        return None, None, (jsonify({"error": "Customer memory is not configured."}), 503)
    return customer, hindsight_key, None


@api_bp.post('/api/customers/<string:customer_id>/delete')
@auth.require_staff
def delete_customer(customer_id: str):
    """Delete a customer with their memory, callbacks and call history. Body: {"confirm_phone": <their number>}."""
    customer, hindsight_key, error = _confirmed_customer(customer_id)
    if error:
        return error
    phone = customer["phone"]
    # Memory first: if the database step fails, the customer is still there (and gets an empty bank on
    # their next call); the other order could leave memories behind without a customer.
    try:
        memory.delete_bank(phone, hindsight_key, config.DANGER_MEMORY_TIMEOUT_SECONDS)
    except Exception:
        current_app.logger.exception("Failed to delete memory bank of customer %s (%s)", customer_id, mask_phone(phone))
        return jsonify({"error": "Could not delete the customer's memory. Nothing was deleted; please try again."}), 502
    try:
        status = db.delete_customer(customer_id)
    except Exception:
        current_app.logger.exception("Failed to delete customer %s (%s)", customer_id, mask_phone(phone))
        return jsonify({"error": "The memory was erased, but the customer could not be deleted. Please try again."}), 500
    if status is None:
        return jsonify({"error": "Unknown customer."}), 404
    if status == "calling":
        current_app.logger.warning("Customer %s not deleted: a call started meanwhile (memory already erased)", customer_id)
        return jsonify({"error": "A call with this customer started. The memory was erased; "
                                 "delete the customer when the call has ended."}), 409
    current_app.logger.info("User %s deleted customer %s (%s)", g.user["id"], customer_id, mask_phone(phone))
    return jsonify({"id": customer_id, "deleted": True})


@api_bp.post('/api/customers/<string:customer_id>/erase-memory')
@auth.require_staff
def erase_customer_memory(customer_id: str):
    """Erase a customer's memories (new empty bank); the customer stays. Body: {"confirm_phone": <their number>}."""
    customer, hindsight_key, error = _confirmed_customer(customer_id)
    if error:
        return error
    phone = customer["phone"]
    try:
        memory.erase_memory(phone, hindsight_key, config.DANGER_MEMORY_TIMEOUT_SECONDS)
    except Exception:
        current_app.logger.exception("Failed to erase memory of customer %s (%s)", customer_id, mask_phone(phone))
        return jsonify({"error": "Could not erase the customer's memory. Please try again."}), 502
    current_app.logger.info("User %s erased the memory of customer %s (%s)", g.user["id"], customer_id, mask_phone(phone))
    return jsonify({"id": customer_id, "memory_erased": True})


@api_bp.post('/api/scheduled-calls/<string:scheduled_call_id>/cancel')
@auth.require_staff
def cancel_scheduled_call(scheduled_call_id: str):
    """Cancel a pending callback (a callback that is already calling can't be cancelled)."""
    if not VAPI_ID_RE.fullmatch(scheduled_call_id):  # scheduled call ids are UUIDs too
        return jsonify({"error": "Unknown callback."}), 404
    try:
        status = db.cancel_scheduled_call(scheduled_call_id)
    except Exception:
        current_app.logger.exception("Failed to cancel callback %s", scheduled_call_id)
        return jsonify({"error": "Could not cancel the callback. Please try again later."}), 500
    if status is None:
        return jsonify({"error": "Unknown callback."}), 404
    if status != "cancelled":
        current_app.logger.warning("Callback %s not cancelled: status is %s", scheduled_call_id, status)
        return jsonify({"error": f"This callback can't be cancelled anymore (it is {status})."}), 409
    current_app.logger.info("Cancelled callback %s", scheduled_call_id)
    return jsonify({"id": scheduled_call_id, "status": status})
