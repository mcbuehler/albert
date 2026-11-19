import json
import os
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import jwt
import psycopg
import requests

from albert import config, db, memory, scheduling
from albert.vapi import MemoryUnavailable, OutboundCallError
from main import app

API_TOKEN = "api-token-1234567890"
CRON_SECRET = "test-cron"
STAFF_JWT = "local.staff.jwt"  # fake; verify_user_token is patched
PHONE = "+41791234567"
OTHER_PHONE = "+41447654321"
SECRET_DETAIL = "SECRET-DETAIL from the database"
PHONE_ID = "3f2b1c9e-8d4a-4b6e-9f10-2a7c5d8e1b34"
ASSISTANT_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
CALLBACK_ID = "c0ffee00-0000-4000-8000-000000000001"
CUSTOMER_ID = "c0ffee00-0000-4000-8000-0000000000c7"
CUSTOMER = {"id": 7, "first_name": "Anna", "middle_name": None, "last_name": "Muster", "phone": PHONE,
            "timezone": "Europe/Zurich", "opted_out": False,
            "vapi_phone_number_id": "phone-anna", "vapi_assistant_id": "assistant-anna"}
JS_DIR = Path(__file__).resolve().parent.parent / "public" / "assets" / "js"

ENV = {"REL8_API_AUTH_TOKEN": API_TOKEN, "CRON_SECRET": CRON_SECRET, "SUPABASE_URL": "https://p.supabase.co",
       "SUPABASE_PUBLISHABLE_KEY": "test-key", "HINDSIGHT_API_KEY": "hs-key", "VAPI_API_KEY": "vapi-key"}
UNSET = ("VAPI_ASSISTANT_ID", "SUPABASE_JWT_SECRET")
AUTH = {None: None, "api": f"Bearer {API_TOKEN}", "cron": f"Bearer {CRON_SECRET}", "staff": f"Bearer {STAFF_JWT}"}


def fake_verify(token):
    if token == STAFF_JWT:
        return {"sub": "user-1", "email": "staff@example.com"}
    raise jwt.InvalidTokenError("bad token")


class RouteTestCase(unittest.TestCase):
    """Patches everything below the routes with harmless defaults; tests override what they need."""

    def setUp(self) -> None:
        """Patch external dependencies before exercising route contracts."""
        self._start(patch.dict(os.environ, ENV))
        for name in UNSET:
            os.environ.pop(name, None)
        self._start(patch("albert.auth.verify_user_token", side_effect=fake_verify))

        self.db = self._start(patch("albert.routes.db"))
        self.db.json_row = db.json_row
        self.db.CustomerOptedOut = db.CustomerOptedOut
        self.db.OutboundCallBusy = db.OutboundCallBusy
        self.db.CustomerChanged = db.CustomerChanged
        self.db.CALL_SORT_COLUMNS = db.CALL_SORT_COLUMNS
        self.db.list_calls.return_value = {"calls": [], "total": 0}
        self.db.get_call.return_value = None
        self.db.call_audio_key.return_value = None
        self.db.add_call_note.return_value = None
        self.storage = self._start(patch("albert.routes.storage"))
        self.db.get_customer_by_phone.return_value = CUSTOMER
        self.db.list_customers.return_value = []
        self.db.get_dispatcher_status.return_value = None
        self.db.reserve_adhoc_attempt.return_value = "attempt-1"
        self.db.set_attempt_call_id.return_value = True
        self.db.delete_undialled_attempt.return_value = True
        self.memory = self._start(patch("albert.routes.memory"))
        self.memory.bank_id_for = memory.bank_id_for
        self.memory.process_event.return_value = {}
        self.memory.ensure_bank.return_value = True
        self.scheduling = self._start(patch("albert.routes.scheduling"))
        self.scheduling.ScheduleError = scheduling.ScheduleError
        self.scheduling.schedule_callback_tool.return_value = "Callback scheduled."
        self.scheduling.cancel_callback_tool.return_value = "Callback cancelled."
        self.scheduling.reschedule_callback_tool.return_value = "Callback moved."
        self.onboarding = self._start(patch("albert.routes.onboarding"))
        self.onboarding.onboard_customer_tool.return_value = "Anna Muster is signed up."
        self.notify = self._start(patch("albert.routes.notify"))
        self.notify.notify_team_tool.return_value = "The team has been notified."
        self.timezones = self._start(patch("albert.routes.timezones"))
        self.timezones.list_timezones.return_value = [{"id": "UTC", "country": "", "keywords": "GMT"}]
        self.place_outbound_call = self._start(patch("albert.routes.place_outbound_call", return_value="vapi-call-1"))
        self.assistant_request_response = self._start(patch("albert.routes.assistant_request_response",
                                                            return_value={"assistantId": "a"}))
        self.list_vapi = self._start(patch("albert.routes.list_vapi", return_value=[]))
        self.search_brave = self._start(patch("albert.routes.search_brave", return_value=([], "Success")))
        self.get_weather = self._start(patch("albert.routes.get_weather", return_value="Sunny"))
        self.dispatch_due_calls = self._start(patch("albert.routes.dispatch_due_calls", return_value=([], True)))
        self.archive = self._start(patch("albert.routes.archive"))
        self.archive.discover_calls.return_value = {"ok": True, "saved": 0, "refreshed": 0, "busy": False}
        self.client = app.test_client()

    def _start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def request(self, method, path, auth=None, headers=None, **kwargs):
        """Send a request; returns (response, log text). Always checks that no full phone number or
        exception detail leaks into the logs or the response."""
        headers = {**({"Authorization": AUTH[auth]} if auth else {}), **(headers or {})}
        with self.assertLogs(app.logger, "DEBUG") as logs:
            response = self.client.open(path, method=method, headers=headers, **kwargs)
        text = "\n".join(logs.output)
        body = response.get_data(as_text=True)
        response.close()
        for phone in (PHONE, OTHER_PHONE):
            self.assertNotIn(phone, text)
        if response.status_code >= 400:
            self.assertNotIn(SECRET_DETAIL, body)
        return response, text

    def staff(self, method, path, **kwargs):
        return self.request(method, path, "staff", **kwargs)


# --- Auth wiring and route contract -------------------------------------------

ROUTES = [  # (method, path, who may call it)
    ("GET", "/", "public"),
    ("GET", "/favicon.svg", "public"),
    ("GET", "/assets/css/app.css", "public"),
    ("GET", "/api/timezones", "public"),
    ("GET", "/api/page-config", "public"),
    ("GET", "/api/health", "public"),
    ("POST", "/api/webhook", "api"),
    ("POST", "/api/tools/schedule-callback", "api"),
    ("POST", "/api/tools/cancel-callback", "api"),
    ("POST", "/api/tools/reschedule-callback", "api"),
    ("POST", "/api/tools/notify-team", "api_or_staff"),
    ("POST", "/api/tools/onboard-customer", "api"),
    ("GET", "/api/tools/search/news", "api"),
    ("GET", "/api/tools/weather/Zurich/en", "api"),
    ("GET", "/api/vapi/options", "staff"),
    ("POST", "/api/tools/call", "staff"),
    ("GET", "/api/customers", "staff"),
    ("POST", "/api/customers", "staff"),
    ("POST", "/api/scheduled-calls", "staff"),
    ("POST", f"/api/scheduled-calls/{CALLBACK_ID}/cancel", "staff"),
    ("POST", f"/api/customers/{CUSTOMER_ID}/delete", "staff"),
    ("POST", f"/api/customers/{CUSTOMER_ID}/erase-memory", "staff"),
    ("POST", f"/api/customers/{CUSTOMER_ID}", "staff"),
    ("POST", f"/api/customers/{CUSTOMER_ID}/reviewed", "staff"),
    ("GET", "/api/cron/dispatch", "cron"),
    ("GET", "/api/dispatcher/status", "staff"),
    ("GET", "/api/cron/archive", "cron"),
    ("GET", "/api/archive/status", "staff"),
    ("GET", "/api/calls", "staff"),
    ("GET", f"/api/calls/call:{CALLBACK_ID}", "staff"),
    ("POST", f"/api/calls/call:{CALLBACK_ID}/audio-url", "staff"),
    ("POST", f"/api/calls/call:{CALLBACK_ID}/notes", "staff"),
]
ACCEPTS = {"public": {None, "api", "cron", "staff"}, "api": {"api"}, "staff": {"staff"}, "cron": {"cron", "staff"},
           "api_or_staff": {"api", "staff"}}


class AuthWiringTests(RouteTestCase):
    def test_every_route_accepts_exactly_its_credentials(self):
        for method, path, kind in ROUTES:
            for credential in (None, "api", "cron", "staff"):
                with self.subTest(route=f"{method} {path}", credential=credential):
                    kwargs = {"json": {}} if method == "POST" else {}
                    response, _ = self.request(method, path, credential, **kwargs)
                    if credential in ACCEPTS[kind]:
                        self.assertNotIn(response.status_code, (401, 403))
                    else:
                        self.assertEqual(response.status_code, 401)

    def test_route_table_covers_every_registered_rule(self):
        registered = {(method, rule.rule) for rule in app.url_map.iter_rules() if rule.endpoint != "static"
                      for method in rule.methods - {"HEAD", "OPTIONS"}}
        adapter = app.url_map.bind("localhost")
        covered = {(method, adapter.match(path, method=method, return_rule=True)[0].rule)
                   for method, path, _ in ROUTES}
        self.assertEqual(registered, covered)


def page_api_calls():
    """(method, path) for every /api call in the page's JS. Concatenated parts (also at the end) become 'x'."""
    calls = set()
    pattern = re.compile(r'(postJson\(\s*api\s*,\s*)?"(/api/[^"]*)"((?:\s*\+\s*[^"+]+?\s*\+\s*"[^"]*")*)(\s*\+)?')
    for path in JS_DIR.glob("*.js"):
        for post, literal, rest, open_end in pattern.findall(path.read_text(encoding="utf-8")):
            tail = "".join("x" + part for part in re.findall(r'"([^"]*)"', rest))
            calls.add(("POST" if post else "GET", literal + tail + ("x" if open_end else "")))
    return calls


class RouteContractTests(unittest.TestCase):
    def test_page_api_calls_found(self):
        self.assertTrue({("POST", "/api/tools/call"), ("POST", "/api/scheduled-calls/x/cancel"),
                         ("GET", "/api/customers"), ("GET", "/api/vapi/options"),
                         ("GET", "/api/dispatcher/status"), ("POST", "/api/customers/x/delete"),
                         ("POST", "/api/customers/x/erase-memory"),
                         ("POST", "/api/customers/x"),
                         ("POST", "/api/customers/x/reviewed"),
                         ("POST", "/api/tools/notify-team"), ("GET", "/api/calls"),
                         ("GET", "/api/cron/archive"), ("GET", "/api/calls/x"),
                         ("POST", "/api/calls/x/audio-url"), ("POST", "/api/calls/x/notes")} <= page_api_calls())

    def test_every_page_api_call_matches_a_rule_and_method(self):
        adapter = app.url_map.bind("localhost")
        for method, path in sorted(page_api_calls()):
            with self.subTest(method=method, path=path):
                adapter.match(path, method=method)  # raises NotFound / MethodNotAllowed

    def test_vercel_crons_are_cron_routes(self) -> None:
        """Verify every scheduled path in vercel.json, including the archive, is a GET route with cron auth."""
        crons = json.loads((JS_DIR.parents[2] / "vercel.json").read_text())["crons"]
        paths = {cron["path"] for cron in crons}
        self.assertEqual(paths, {"/api/cron/dispatch", "/api/cron/archive"})
        self.assertTrue({("GET", path, "cron") for path in paths} <= set(ROUTES))


# --- Page and public data -------------------------------------------------

class PageTests(RouteTestCase):
    def test_index_and_favicon_served(self):
        response, _ = self.request("GET", "/")
        self.assertEqual((response.status_code, response.mimetype), (200, "text/html"))
        response, _ = self.request("GET", "/favicon.svg")
        self.assertEqual((response.status_code, response.mimetype), (200, "image/svg+xml"))

    def test_assets_served_with_mimetype(self):
        for path, mimetypes in (("/assets/css/app.css", {"text/css"}),
                                ("/assets/js/main.js", {"text/javascript", "application/javascript"})):
            with self.subTest(path=path):
                response, _ = self.request("GET", path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(response.mimetype, mimetypes)

    def test_assets_missing_and_traversal_404(self):
        for path in ("/assets/js/missing.js", "/assets/../index.html", "/assets/%2e%2e/index.html",
                     "/assets/..%2f..%2fmain.py", "/assets/%2e%2e/%2e%2e/main.py", "/assets/js/../../../main.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status_code, 404)

    def test_health_ok_when_configured_and_database_reachable(self):
        with patch.dict(os.environ, {name: "set" for name in config.REQUIRED_ENV}):
            response, _ = self.request("GET", "/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok", "checks": {"config": "ok", "database": "ok"}})
        self.assertTrue(response.cache_control.no_store)
        self.db.ping.assert_called_once_with()

    def test_health_missing_env_var_503_names_it_only_in_the_log(self):
        with patch.dict(os.environ, {name: "set" for name in config.REQUIRED_ENV}):
            os.environ.pop("BRAVE_API_KEY")
            response, logs = self.request("GET", "/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"status": "fail", "checks": {"config": "fail", "database": "ok"}})
        self.assertIn("BRAVE_API_KEY", logs)
        self.assertNotIn("BRAVE_API_KEY", response.get_data(as_text=True))

    def test_health_database_error_503_without_details(self):
        self.db.ping.side_effect = psycopg.OperationalError(SECRET_DETAIL)
        with patch.dict(os.environ, {name: "set" for name in config.REQUIRED_ENV}):
            response, logs = self.request("GET", "/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"status": "fail", "checks": {"config": "ok", "database": "fail"}})
        self.assertIn("Health check: database unreachable", logs)

    def test_page_config_returns_supabase_and_calling_hours(self):
        response, _ = self.request("GET", "/api/page-config")
        self.assertEqual(response.get_json(), {"supabase_url": "https://p.supabase.co",
                                               "supabase_publishable_key": "test-key",
                                               "calling_start": "09:00", "calling_end": "22:00"})

    def test_page_config_without_supabase_settings_503(self):
        for name in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY"):
            with self.subTest(missing=name), patch.dict(os.environ):
                os.environ.pop(name)
                response, _ = self.request("GET", "/api/page-config")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("supabase_url", response.get_json())

    def test_timezones_public_and_cached_for_a_day(self):
        response, _ = self.request("GET", "/api/timezones")
        self.assertEqual(response.get_json(), [{"id": "UTC", "country": "", "keywords": "GMT"}])
        self.assertTrue(response.cache_control.public)
        self.assertEqual(response.cache_control.max_age, 86400)


# --- Vapi webhook ----------------------------------------------------------

def event(event_type, number=PHONE):
    message = {"type": event_type, "call": {"id": "call-1", "customer": {"number": number} if number else {}}}
    return {"message": message}


class WebhookTests(RouteTestCase):
    def post(self, body=None, **kwargs):
        return self.request("POST", "/api/webhook", "api", json=body, **kwargs)

    def test_webhook_non_object_body_400(self):
        for body in ([1], "text", 5):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0].status_code, 400)
        response, _ = self.request("POST", "/api/webhook", "api", data="null", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.memory.process_event.assert_not_called()

    def test_webhook_without_json_body_400(self):
        for kwargs in ({}, {"data": "not json", "content_type": "application/json"}, {"data": "x=1"}):
            with self.subTest(kwargs=kwargs):
                response, _ = self.request("POST", "/api/webhook", "api", **kwargs)
                self.assertEqual((response.status_code, response.get_json()), (400, {"error": "Expected a JSON object."}))
        self.memory.process_event.assert_not_called()

    def test_webhook_missing_hindsight_key_503(self):
        os.environ.pop("HINDSIGHT_API_KEY")
        self.assertEqual(self.post(event("end-of-call-report"))[0].status_code, 503)
        self.memory.process_event.assert_not_called()

    def test_webhook_assistant_request_delegates(self):
        body = event("assistant-request")
        response, _ = self.post(body)
        self.assertEqual(response.get_json(), {"assistantId": "a"})
        self.assistant_request_response.assert_called_once_with(body["message"], "hs-key")
        self.memory.process_event.assert_not_called()

    def test_webhook_customer_event_stored_in_customer_bank(self):
        self.memory.process_event.return_value = {"ok": True}
        body = event("end-of-call-report")
        response, _ = self.post(body)
        self.assertEqual((response.status_code, response.get_json()), (200, {"ok": True}))
        self.db.get_customer_by_phone.assert_called_once_with(PHONE)
        self.memory.process_event.assert_called_once_with(body, f"user-{PHONE}", "hs-key")

    def test_webhook_non_customer_not_stored(self):
        self.db.get_customer_by_phone.return_value = None
        response, logs = self.post(event("end-of-call-report", OTHER_PHONE))
        self.assertEqual((response.status_code, response.get_json()), (200, {}))
        self.assertIn("no consent", logs)
        self.memory.process_event.assert_not_called()

    def test_webhook_customer_lookup_error_not_stored(self):
        self.db.get_customer_by_phone.side_effect = RuntimeError(SECRET_DETAIL)
        response, logs = self.post(event("end-of-call-report"))
        self.assertEqual((response.status_code, response.get_json()), (200, {}))
        self.assertIn("ERROR", logs)
        self.memory.process_event.assert_not_called()

    def test_webhook_customer_with_unnormalizable_number_not_stored(self):
        for number in ("0791234567", "079 123 45 67"):
            with self.subTest(number=number):
                response, logs = self.post(event("end-of-call-report", number))
                self.assertEqual((response.status_code, response.get_json()), (200, {}))
                self.assertIn("ERROR", logs)
                self.assertIn("number can't be normalized", logs)
                self.assertIn("+0791***67", logs)
                self.assertNotIn(number, logs)
        self.memory.process_event.assert_not_called()

    def test_webhook_voicemail_and_no_answer_not_stored(self):
        os.environ.pop("HINDSIGHT_API_KEY")  # not needed: nothing is stored
        for reason in ("voicemail", "customer-did-not-answer"):
            with self.subTest(reason=reason):
                body = event("end-of-call-report")
                body["message"]["endedReason"] = reason
                response, logs = self.post(body)
                self.assertEqual((response.status_code, response.get_json()), (200, {}))
                self.assertIn(f"customer not reached ({reason})", logs)
        self.memory.process_event.assert_not_called()
        self.db.get_customer_by_phone.assert_not_called()

    def test_webhook_other_ended_reasons_stored(self):
        self.memory.process_event.return_value = {"ok": True}
        for reason in ("customer-ended-call", "silence-timed-out", None):
            with self.subTest(reason=reason):
                body = event("end-of-call-report")
                body["message"]["endedReason"] = reason
                self.assertEqual(self.post(body)[0].get_json(), {"ok": True})
        self.assertEqual(self.memory.process_event.call_count, 3)

    def test_webhook_end_of_call_report_without_number_skipped(self):
        response, _ = self.post(event("end-of-call-report", number=None))
        self.assertEqual(response.get_json(), {})
        self.memory.process_event.assert_not_called()
        self.db.get_customer_by_phone.assert_not_called()

    def test_webhook_other_events_ignored_without_hindsight_or_db(self):
        os.environ.pop("HINDSIGHT_API_KEY")  # not needed for events we ignore
        for body in (event("status-update"), event("status-update", number=None), event("speech-update", OTHER_PHONE),
                     {"foo": "bar"}, {"message": "text"}, {"message": {"type": None}}):
            with self.subTest(body=body):
                response, logs = self.post(body)
                self.assertEqual((response.status_code, response.get_json()), (200, {}))
                self.assertIn("Ignoring Vapi webhook event", logs)
        self.memory.process_event.assert_not_called()
        self.db.get_customer_by_phone.assert_not_called()

    def test_webhook_process_event_error_500(self):
        self.memory.process_event.side_effect = RuntimeError(SECRET_DETAIL)
        response, logs = self.post(event("end-of-call-report"))
        self.assertEqual(response.status_code, 500)
        self.assertIn("Failed to process Vapi webhook event", logs)


# --- Vapi tools ------------------------------------------------------------

REJECTED_TOOL_BODY = ("Expected a body with local_time and instructions and customer_number (static field "
                      "\"{{customer.number}}\").")


class ScheduleCallbackApiRequestTests(RouteTestCase):
    """Vapi "API Request" tool: {"local_time", "customer_number"} body, call id in X-Call-Id."""

    def post(self, body=None, call_id="call-1", auth="api", **kwargs):
        headers = {"X-Call-Id": call_id} if call_id is not None else {}
        if body is not None:
            kwargs["json"] = body
        return self.request("POST", "/api/tools/schedule-callback", auth, headers=headers, **kwargs)

    def test_api_request_happy_path(self):
        response, _ = self.post({"local_time": "2099-06-15T14:30", "customer_number": f" {PHONE} "})
        self.assertEqual((response.status_code, response.get_json()), (200, {"result": "Callback scheduled."}))
        self.scheduling.schedule_callback_tool.assert_called_once_with(
            {"local_time": "2099-06-15T14:30", "instructions": None}, PHONE, "call-1")

    def test_api_request_passes_instructions(self):
        self.post({"local_time": "2099-06-15T14:30", "instructions": "Remind: tablets", "customer_number": PHONE})
        self.scheduling.schedule_callback_tool.assert_called_once_with(
            {"local_time": "2099-06-15T14:30", "instructions": "Remind: tablets"}, PHONE, "call-1")

    def test_api_request_customer_number_normalized(self):
        for spelling in ("+41 79 123 45 67", "0041791234567", "+41 (0)79 123 45 67", PHONE + "\n"):
            with self.subTest(spelling=spelling):
                self.scheduling.schedule_callback_tool.reset_mock()
                _, logs = self.post({"local_time": "2099-06-15T14:30", "customer_number": spelling})
                self.assertEqual(self.scheduling.schedule_callback_tool.call_args.args[1], PHONE)
                self.assertNotIn("customer_number is not a phone number", logs)

    def test_api_request_extra_keys_ignored_phone_only_from_customer_number(self):
        self.post({"local_time": "2099-06-15T14:30", "customer_number": PHONE, "phone": OTHER_PHONE,
                   "phone_number": OTHER_PHONE, "source_vapi_call_id": "evil"})
        self.scheduling.schedule_callback_tool.assert_called_once_with(
            {"local_time": "2099-06-15T14:30", "instructions": None}, PHONE, "call-1")

    def test_api_request_bad_customer_number_still_handled_with_warning(self):
        for value, passed in (("{{customer.number}}", "{{customer.number}}"), ("", ""), ("   ", ""),
                              (None, None), (41791234567, None), ("0791234567", "0791234567")):
            with self.subTest(value=value):
                self.scheduling.schedule_callback_tool.reset_mock()
                response, logs = self.post({"local_time": "2099-06-15T14:30", "customer_number": value})
                self.assertEqual((response.status_code, response.get_json()), (200, {"result": "Callback scheduled."}))
                self.assertIn("WARNING", logs)
                self.assertIn("customer_number is not a phone number; check the tool's static body field", logs)
                self.assertEqual(self.scheduling.schedule_callback_tool.call_args.args[1], passed)

    def test_api_request_valid_number_no_warning(self):
        _, logs = self.post({"local_time": "2099-06-15T14:30", "customer_number": PHONE})
        self.assertNotIn("customer_number is not a phone number", logs)

    def test_api_request_call_id_header(self):
        for header, expected in ((None, None), ("", None), ("   ", None), ("  call-9  ", "call-9"),
                                 ("x" * 150, "x" * 100)):
            with self.subTest(header=header):
                self.scheduling.schedule_callback_tool.reset_mock()
                self.post({"local_time": "2099-06-15T14:30", "customer_number": PHONE}, call_id=header)
                self.assertEqual(self.scheduling.schedule_callback_tool.call_args.args[2], expected)

    def test_api_request_missing_local_time_passes_none(self):
        self.scheduling.schedule_callback_tool.return_value = "Could not schedule the callback: local_time must be ..."
        response, _ = self.post({"customer_number": PHONE})
        self.assertEqual(response.status_code, 200)
        self.scheduling.schedule_callback_tool.assert_called_once_with({"local_time": None, "instructions": None}, PHONE, "call-1")

    def test_api_request_needs_api_token(self):
        body = {"local_time": "2099-06-15T14:30", "customer_number": PHONE}
        for auth in (None, "cron", "staff"):
            with self.subTest(auth=auth):
                self.assertEqual(self.post(body, auth=auth)[0].status_code, 401)
        self.scheduling.schedule_callback_tool.assert_not_called()

    def test_unrecognised_bodies_400_with_shape_only_logged(self):
        cases = {
            "no body": dict(),
            "invalid json": dict(data="{not json", content_type="application/json"),
            "form body": dict(data=f"customer_number={PHONE}"),
            "list": dict(json=[{"customer_number": PHONE}]),
            "dict without customer_number": dict(json={"local_time": "2099-06-15T14:30", "phone": PHONE}),
            "other message": dict(json={"message": {"type": "status-update", "customer": {"number": PHONE}}}),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                response, logs = self.post(**kwargs)  # request() also checks no phone number in the logs
                self.assertEqual((response.status_code, response.get_json()), (400, {"error": REJECTED_TOOL_BODY}))
                self.assertIn("WARNING", logs)
                self.assertIn("Rejected schedule_callback tool request: expected a body with customer_number; "
                              "got content type", logs)
        self.scheduling.schedule_callback_tool.assert_not_called()

    def test_function_tool_calls_message_rejected_400(self):
        body = {"message": {"type": "tool-calls", "call": {"id": "call-1", "customer": {"number": PHONE}},
                            "toolCallList": [{"id": "t1", "function": {"name": "schedule_callback",
                                                                       "arguments": {"local_time": "2099-06-15T14:30"}}}]}}
        response, logs = self.post(json=body)
        self.assertEqual((response.status_code, response.get_json()), (400, {"error": REJECTED_TOOL_BODY}))
        self.assertIn("top-level keys ['message']", logs)
        self.scheduling.schedule_callback_tool.assert_not_called()

    def test_unrecognised_body_log_shows_shape(self):
        _, logs = self.post(json={"local_time": "2099-06-15T14:30", "phone": PHONE})
        self.assertIn("content type 'application/json', body dict, top-level keys ['local_time', 'phone']", logs)
        self.assertNotIn("2099-06-15T14:30", logs)
        self.assertNotIn("message type", logs)
        _, logs = self.post(data="x", content_type="text/plain")
        self.assertIn("body not JSON", logs)


class NotifyTeamToolRouteTests(RouteTestCase):
    """notify_team API Request tool (shared handling with the callback tools)."""

    def post(self, body, auth="api"):
        return self.request("POST", "/api/tools/notify-team", auth, headers={"X-Call-Id": "call-1"}, json=body)

    def test_passes_arguments_phone_and_call_id(self):
        body = {"reason": "institution_lead", "contact_name": "Maria", "organization": "Home", "summary": "Demo",
                "local_time": "x", "customer_number": "0041 79 123 45 67"}
        response, _ = self.post(body)
        self.assertEqual((response.status_code, response.get_json()), (200, {"result": "The team has been notified."}))
        self.notify.notify_team_tool.assert_called_once_with(
            {"reason": "institution_lead", "contact_name": "Maria", "organization": "Home", "summary": "Demo"},
            PHONE, "call-1")

    def test_without_customer_number_400(self):
        response, _ = self.post({"reason": "other"})
        self.assertEqual(response.status_code, 400)
        self.notify.notify_team_tool.assert_not_called()


class OnboardCustomerToolRouteTests(RouteTestCase):
    def post(self, body, auth="api"):
        return self.request("POST", "/api/tools/onboard-customer", auth, headers={"X-Call-Id": "call-1"}, json=body)

    def test_passes_arguments_phone_and_call_id(self):
        body = {"first_name": "Anna", "last_name": "Muster", "timezone": "Europe/Zurich", "notes_consent": True,
                "phone": OTHER_PHONE, "customer_number": PHONE}
        response, _ = self.post(body)
        self.assertEqual((response.status_code, response.get_json()), (200, {"result": "Anna Muster is signed up."}))
        self.onboarding.onboard_customer_tool.assert_called_once_with(
            {"first_name": "Anna", "last_name": "Muster", "timezone": "Europe/Zurich", "notes_consent": True},
            PHONE, "call-1")

    def test_without_customer_number_400(self):
        self.assertEqual(self.post({"first_name": "Anna"})[0].status_code, 400)
        self.onboarding.onboard_customer_tool.assert_not_called()


class CancelAndRescheduleToolTests(RouteTestCase):
    """cancel_callback / reschedule_callback API Request tools (same handling as schedule_callback)."""

    TOOLS = (("/api/tools/cancel-callback", "cancel_callback", ("callback_time",)),
             ("/api/tools/reschedule-callback", "reschedule_callback", ("callback_time", "new_local_time")))

    def post(self, path, body=None, call_id="call-1", auth="api", **kwargs):
        headers = {"X-Call-Id": call_id} if call_id is not None else {}
        if body is not None:
            kwargs["json"] = body
        return self.request("POST", path, auth, headers=headers, **kwargs)

    def tool_mock(self, name):
        return getattr(self.scheduling, f"{name}_tool")

    def test_tools_pass_only_their_arguments_phone_and_call_id(self):
        body = {"callback_time": "2099-06-15T14:30", "new_local_time": "2099-06-17T10:00", "local_time": "x",
                "customer_number": f" {PHONE} ", "phone": OTHER_PHONE}
        for path, name, argument_names in self.TOOLS:
            with self.subTest(name):
                response, _ = self.post(path, body)
                self.assertEqual(response.status_code, 200)
                self.tool_mock(name).assert_called_once_with({key: body[key] for key in argument_names}, PHONE, "call-1")
        self.scheduling.schedule_callback_tool.assert_not_called()

    def test_tools_customer_number_normalized(self):
        for path, name, _ in self.TOOLS:
            with self.subTest(name):
                self.post(path, {"customer_number": "0041 79 123 45 67"})
                self.assertEqual(self.tool_mock(name).call_args.args[1], PHONE)

    def test_tools_response_shape(self):
        for (path, name, _), result in zip(self.TOOLS, ("Callback cancelled.", "Callback moved."), strict=True):
            with self.subTest(name):
                self.assertEqual(self.post(path, {"customer_number": PHONE})[0].get_json(), {"result": result})

    def test_tools_missing_arguments_passed_as_none(self):
        for path, name, argument_names in self.TOOLS:
            with self.subTest(name):
                self.post(path, {"customer_number": PHONE}, call_id=None)
                self.tool_mock(name).assert_called_once_with(dict.fromkeys(argument_names), PHONE, None)

    def test_tools_long_call_id_truncated(self):
        for path, name, _ in self.TOOLS:
            with self.subTest(name):
                self.post(path, {"customer_number": PHONE}, call_id="x" * 150)
                self.assertEqual(self.tool_mock(name).call_args.args[2], "x" * 100)

    def test_tools_bad_customer_number_warning_names_tool(self):
        for path, name, _ in self.TOOLS:
            with self.subTest(name):
                response, logs = self.post(path, {"customer_number": "{{customer.number}}"})
                self.assertEqual(response.status_code, 200)
                self.assertIn(f"{name} API request: customer_number is not a phone number; "
                              "check the tool's static body field", logs)

    def test_tools_without_customer_number_400(self):
        errors = {"cancel_callback": "Expected a body with callback_time and customer_number "
                                     "(static field \"{{customer.number}}\").",
                  "reschedule_callback": "Expected a body with callback_time and new_local_time and customer_number "
                                         "(static field \"{{customer.number}}\")."}
        for path, name, _ in self.TOOLS:
            for kwargs in (dict(body={"callback_time": "2099-06-15T14:30", "phone": PHONE}), dict(body=[1]),
                           dict(data="not json", content_type="application/json"), dict()):
                with self.subTest(name=name, kwargs=kwargs):
                    response, logs = self.post(path, **kwargs)
                    self.assertEqual((response.status_code, response.get_json()), (400, {"error": errors[name]}))
                    self.assertIn(f"Rejected {name} tool request: expected a body with customer_number; got", logs)
            self.tool_mock(name).assert_not_called()

    def test_tools_need_api_token(self):
        for path, name, _ in self.TOOLS:
            for auth in (None, "cron", "staff"):
                with self.subTest(name=name, auth=auth):
                    self.assertEqual(self.post(path, {"customer_number": PHONE}, auth=auth)[0].status_code, 401)
            self.tool_mock(name).assert_not_called()


class SearchAndWeatherToolTests(RouteTestCase):
    def test_search_result_shape(self):
        self.search_brave.return_value = ([{"Title": "snippet"}], "Success")
        response, _ = self.request("GET", "/api/tools/search/swiss%20news%20", "api")
        self.assertEqual(response.get_json(), {"results": [{"result": [{"Title": "snippet"}], "message": "Success"}]})
        self.search_brave.assert_called_once_with("swiss news")

    def test_weather_result_shape(self):
        response, _ = self.request("GET", "/api/tools/weather/Z%C3%BCrich/de", "api")
        self.assertEqual(response.get_json(), {"results": [{"result": "Sunny"}]})
        self.get_weather.assert_called_once_with("Zürich", "de")


class VapiOptionsTests(RouteTestCase):
    def test_options_missing_key_503(self):
        os.environ.pop("VAPI_API_KEY")
        self.assertEqual(self.staff("GET", "/api/vapi/options")[0].status_code, 503)
        self.list_vapi.assert_not_called()

    def test_options_success(self):
        self.list_vapi.side_effect = lambda resource, key: [{"id": resource, "name": key}]
        response, _ = self.staff("GET", "/api/vapi/options")
        self.assertEqual(response.get_json(), {"phone_numbers": [{"id": "phone-number", "name": "vapi-key"}],
                                               "assistants": [{"id": "assistant", "name": "vapi-key"}]})

    def test_options_errors_502_generic(self):
        for error in (requests.HTTPError(SECRET_DETAIL), requests.Timeout(SECRET_DETAIL),
                      ValueError(SECRET_DETAIL), TypeError(SECRET_DETAIL)):
            with self.subTest(error=type(error).__name__):
                self.list_vapi.side_effect = error
                response, _ = self.staff("GET", "/api/vapi/options")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.get_json(), {"error": "Could not load phone numbers and assistants from Vapi."})


# --- Calls -----------------------------------------------------------------

class CallNowTests(RouteTestCase):
    def post(self, body, **kwargs):
        return self.staff("POST", "/api/tools/call", json=body, **kwargs)

    def test_call_body_validation_400(self):
        for body in ([1], "x"):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0].status_code, 400)
        for number in (None, "0791234567", "079 123 45 67", "anonymous", 41791234567, ""):
            with self.subTest(number=number):
                response, _ = self.post({"customer_phone_number": number})
                self.assertEqual(
                    (response.status_code, response.get_json()),
                    (400, {"error": "customer_phone_number must be an international number with country code, "
                                    "e.g. +41 79 123 45 67."}),
                )
        self.db.get_customer_by_phone.assert_not_called()
        self.place_outbound_call.assert_not_called()

    def test_call_number_normalized_for_lookup_call_and_record(self):
        for number in (PHONE + "\n", " " + PHONE, "+41 79 123 45 67", "0041 (0)79 123 45 67"):
            with self.subTest(number=number):
                self.db.get_customer_by_phone.reset_mock()
                self.place_outbound_call.reset_mock()
                response, _ = self.post({"customer_phone_number": number})
                self.assertEqual(response.status_code, 201)
                self.db.get_customer_by_phone.assert_called_once_with(PHONE)
                self.assertEqual(self.place_outbound_call.call_args.args[0], PHONE)

    def test_call_without_json_body_400(self):
        for kwargs in ({}, {"data": "not json", "content_type": "application/json"}, {"data": "x=1"}):
            with self.subTest(kwargs=kwargs):
                response, _ = self.staff("POST", "/api/tools/call", **kwargs)
                self.assertEqual((response.status_code, response.get_json()), (400, {"error": "Expected a JSON object."}))
        self.place_outbound_call.assert_not_called()

    def test_call_invalid_override_id_400_without_call(self):
        for name in ("assistant_id", "assistant_phone_id"):
            for value in ("assistant-anna", ASSISTANT_ID + "0", ASSISTANT_ID + "\n" + "x", 5, ["x"]):
                with self.subTest(name=name, value=value):
                    response, _ = self.post({"customer_phone_number": PHONE, name: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.get_json(), {"error": f"{name} must be a Vapi id (UUID, e.g. "
                                                                    "3f2b1c9e-8d4a-4b6e-9f10-2a7c5d8e1b34)."})
        self.place_outbound_call.assert_not_called()
        self.db.reserve_adhoc_attempt.assert_not_called()

    def test_call_override_ids_trimmed_and_lowercased(self):
        self.post({"customer_phone_number": PHONE, "assistant_id": f"  {ASSISTANT_ID.upper()} ",
                   "assistant_phone_id": f"\t{PHONE_ID.upper()}\n"})
        self.place_outbound_call.assert_called_once_with(PHONE, ASSISTANT_ID, PHONE_ID, CUSTOMER, None)
        self.db.reserve_adhoc_attempt.assert_called_once_with(7, PHONE, PHONE_ID, ASSISTANT_ID)
        self.db.set_attempt_call_id.assert_called_once_with("attempt-1", "vapi-call-1")

    def test_call_blank_overrides_use_customer_defaults(self):
        for blank in (None, "", "   "):
            with self.subTest(blank=blank):
                self.place_outbound_call.reset_mock()
                self.post({"customer_phone_number": PHONE, "assistant_id": blank, "assistant_phone_id": blank})
                self.place_outbound_call.assert_called_once_with(PHONE, "assistant-anna", "phone-anna", CUSTOMER, None)

    def test_call_one_override_mixed_with_default(self):
        self.post({"customer_phone_number": PHONE, "assistant_id": ASSISTANT_ID})
        self.place_outbound_call.assert_called_once_with(PHONE, ASSISTANT_ID, "phone-anna", CUSTOMER, None)

    def test_call_instructions_passed_trimmed(self):
        self.post({"customer_phone_number": PHONE, "instructions": "  Remind her about the invoice.\n"})
        self.place_outbound_call.assert_called_once_with(PHONE, "assistant-anna", "phone-anna", CUSTOMER,
                                                         "Remind her about the invoice.")

    def test_call_blank_instructions_become_none(self):
        for value in (None, "", "   \n"):
            with self.subTest(value=value):
                self.place_outbound_call.reset_mock()
                self.post({"customer_phone_number": PHONE, "instructions": value})
                self.assertIsNone(self.place_outbound_call.call_args.args[4])

    def test_call_instructions_at_limit_accepted(self):
        text = "a" * config.MAX_INSTRUCTIONS_LENGTH
        self.assertEqual(self.post({"customer_phone_number": PHONE, "instructions": f" {text} "})[0].status_code, 201)
        self.assertEqual(self.place_outbound_call.call_args.args[4], text)

    def test_call_invalid_instructions_400_without_calls(self):
        cases = {"too long": ("a" * (config.MAX_INSTRUCTIONS_LENGTH + 1), "instructions can be at most 1000 characters."),
                 "number": (5, "instructions must be text."),
                 "list": (["x"], "instructions must be text."),
                 "object": ({"a": 1}, "instructions must be text.")}
        for name, (value, error) in cases.items():
            with self.subTest(name):
                response, _ = self.post({"customer_phone_number": PHONE, "instructions": value})
                self.assertEqual((response.status_code, response.get_json()), (400, {"error": error}))
        self.db.get_customer_by_phone.assert_not_called()
        self.place_outbound_call.assert_not_called()
        self.db.reserve_adhoc_attempt.assert_not_called()

    def test_call_lookup_error_500_without_call(self):
        self.db.get_customer_by_phone.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.post({"customer_phone_number": PHONE})
        self.assertEqual(response.status_code, 500)
        self.place_outbound_call.assert_not_called()

    def test_call_customer_defaults(self):
        response, _ = self.post({"customer_phone_number": PHONE})
        self.assertEqual((response.status_code, response.get_json()), (201, {"id": "vapi-call-1"}))
        self.place_outbound_call.assert_called_once_with(PHONE, "assistant-anna", "phone-anna", CUSTOMER, None)
        self.db.reserve_adhoc_attempt.assert_called_once_with(7, PHONE, "phone-anna", "assistant-anna")
        self.db.set_attempt_call_id.assert_called_once_with("attempt-1", "vapi-call-1")

    def test_call_overrides_win_over_defaults(self):
        self.post({"customer_phone_number": PHONE, "assistant_id": ASSISTANT_ID, "assistant_phone_id": PHONE_ID})
        self.place_outbound_call.assert_called_once_with(PHONE, ASSISTANT_ID, PHONE_ID, CUSTOMER, None)
        self.db.reserve_adhoc_attempt.assert_called_once_with(7, PHONE, PHONE_ID, ASSISTANT_ID)
        self.db.set_attempt_call_id.assert_called_once_with("attempt-1", "vapi-call-1")

    def test_call_non_customer_needs_ids(self):
        self.db.get_customer_by_phone.return_value = None
        for extra in ({}, {"assistant_id": ASSISTANT_ID}, {"assistant_phone_id": PHONE_ID},
                      {"assistant_id": ASSISTANT_ID, "assistant_phone_id": "  "}):
            with self.subTest(extra=extra):
                response, _ = self.post({"customer_phone_number": OTHER_PHONE, **extra})
                self.assertEqual(response.status_code, 400)
                self.assertIn("is required", response.get_json()["error"])
        self.place_outbound_call.assert_not_called()

    def test_call_non_customer_with_ids(self):
        self.db.get_customer_by_phone.return_value = None
        response, _ = self.post({"customer_phone_number": OTHER_PHONE, "assistant_id": ASSISTANT_ID,
                                 "assistant_phone_id": PHONE_ID})
        self.assertEqual(response.status_code, 201)
        self.place_outbound_call.assert_called_once_with(OTHER_PHONE, ASSISTANT_ID, PHONE_ID, None, None)
        self.db.reserve_adhoc_attempt.assert_called_once_with(None, OTHER_PHONE, PHONE_ID, ASSISTANT_ID)
        self.db.set_attempt_call_id.assert_called_once_with("attempt-1", "vapi-call-1")

    def test_call_outbound_errors_use_their_status_and_message(self):
        for error in (OutboundCallError("VAPI_API_KEY must be configured.", 503),
                      MemoryUnavailable("Could not load customer memory. Please try again later.", 502)):
            with self.subTest(error=error.message):
                self.place_outbound_call.side_effect = error
                response, _ = self.post({"customer_phone_number": PHONE})
                self.assertEqual((response.status_code, response.get_json()), (error.status, {"error": error.message}))
        self.assertEqual(self.db.reserve_adhoc_attempt.call_count, 2)
        self.assertEqual(self.db.delete_undialled_attempt.call_count, 2)
        self.db.set_attempt_call_id.assert_not_called()

    def test_call_record_failure_keeps_reservation_and_still_returns_201(self):
        self.db.set_attempt_call_id.side_effect = RuntimeError(SECRET_DETAIL)
        response, logs = self.post({"customer_phone_number": PHONE})
        self.assertEqual((response.status_code, response.get_json()), (201, {"id": "vapi-call-1"}))
        self.assertIn("Failed to record Vapi call", logs)
        self.db.delete_undialled_attempt.assert_not_called()

    def test_call_without_call_id_returns_502_and_keeps_reservation_for_recovery(self):
        self.place_outbound_call.return_value = None
        response, _ = self.post({"customer_phone_number": PHONE})
        self.assertEqual((response.status_code, response.get_json()),
                         (502, {"error": "The calling service returned an invalid response."}))
        self.db.set_attempt_call_id.assert_not_called()
        self.db.delete_undialled_attempt.assert_not_called()

    def test_call_uncertain_network_error_keeps_reservation_for_recovery(self):
        self.place_outbound_call.side_effect = OutboundCallError("Network failed.", 502, may_have_started=True)
        response, _ = self.post({"customer_phone_number": PHONE})
        self.assertEqual((response.status_code, response.get_json()), (502, {"error": "Network failed."}))
        self.db.delete_undialled_attempt.assert_not_called()

    def test_call_reservation_business_errors_return_409_without_dial(self):
        cases = ((db.CustomerOptedOut(), "This customer has opted out of calls."),
                 (db.OutboundCallBusy(), "A call is already in progress for this customer or outbound phone number."),
                 (db.CustomerChanged(), "The customer changed while the call was being prepared. Please try again."))
        for error, message in cases:
            with self.subTest(error=type(error).__name__):
                self.db.reserve_adhoc_attempt.reset_mock(side_effect=True)
                self.db.reserve_adhoc_attempt.side_effect = error
                response, _ = self.post({"customer_phone_number": PHONE})
                self.assertEqual((response.status_code, response.get_json()), (409, {"error": message}))
        self.place_outbound_call.assert_not_called()
        self.db.set_attempt_call_id.assert_not_called()

    def test_call_reservation_database_error_returns_500_without_dial(self):
        self.db.reserve_adhoc_attempt.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.post({"customer_phone_number": PHONE})
        self.assertEqual(response.status_code, 500)
        self.place_outbound_call.assert_not_called()


class DispatchTests(RouteTestCase):
    def test_dispatch_ok_200(self):
        self.dispatch_due_calls.return_value = ([{"id": "cb-1", "vapi_call_id": "vc-1"}], True)
        response, _ = self.request("GET", "/api/cron/dispatch", "cron")
        self.assertEqual((response.status_code, response.get_json()),
                         (200, {"dispatched": [{"id": "cb-1", "vapi_call_id": "vc-1"}]}))

    def test_dispatch_failed_500_with_partial_results(self):
        self.dispatch_due_calls.return_value = ([{"id": "cb-1"}], False)
        response, _ = self.staff("GET", "/api/cron/dispatch")
        self.assertEqual((response.status_code, response.get_json()),
                         (500, {"error": "Dispatcher failed.", "dispatched": [{"id": "cb-1"}]}))


class ArchiveRoutesTests(RouteTestCase):
    def test_disabled_archive_never_queries_database_or_provider(self) -> None:
        """Verify disabled archive never queries database or provider."""
        with patch.object(config, "ARCHIVE_CAPTURE_ENABLED", False):
            response, _ = self.request("GET", "/api/cron/archive", "cron")
            self.assertEqual(response.status_code, 503)
            self.archive.discover_calls.assert_not_called()
            response, _ = self.staff("GET", "/api/archive/status")
            self.assertEqual(response.get_json(), {"enabled": False})
            self.db.get_archive_status.assert_not_called()

    def test_enabled_cron_returns_discovery_result(self) -> None:
        """Verify enabled cron returns discovery result."""
        with patch.object(config, "ARCHIVE_CAPTURE_ENABLED", True):
            response, _ = self.request("GET", "/api/cron/archive", "cron")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(response.cache_control.no_store)

    def test_failed_or_exceptional_scan_is_retryable_and_private(self) -> None:
        """Verify failed or exceptional scan is retryable and private."""
        with patch.object(config, "ARCHIVE_CAPTURE_ENABLED", True):
            self.archive.discover_calls.return_value = {"ok": False, "error": "provider_read_failed"}
            response, _ = self.request("GET", "/api/cron/archive", "cron")
            self.assertEqual(response.status_code, 503)
            self.archive.discover_calls.side_effect = RuntimeError(SECRET_DETAIL)
            response, _ = self.request("GET", "/api/cron/archive", "cron")
            self.assertEqual(response.status_code, 503)

    def test_enabled_status_and_database_error(self) -> None:
        """Verify enabled status and database error."""
        with patch.object(config, "ARCHIVE_CAPTURE_ENABLED", True):
            self.db.get_archive_status.return_value = {"scan": None, "calls": 3, "pending_audio": 3, "oldest_pending_at": None}
            response, _ = self.staff("GET", "/api/archive/status")
            self.assertEqual(response.get_json()["calls"], 3)
            self.assertTrue(response.cache_control.no_store)
            self.db.get_archive_status.side_effect = RuntimeError(SECRET_DETAIL)
            response, _ = self.staff("GET", "/api/archive/status")
            self.assertEqual(response.status_code, 503)


class CallsListTests(RouteTestCase):
    def test_defaults_are_newest_first(self) -> None:
        """Verify an empty query lists the first page, newest call date first, without caching."""
        response, _ = self.staff("GET", "/api/calls")
        self.assertEqual(response.get_json(), {"calls": [], "total": 0})
        self.assertTrue(response.cache_control.no_store)
        self.db.list_calls.assert_called_once_with(search="", statuses=[], sort="call_date", descending=True,
                                                   limit=config.CALL_LIST_DEFAULT_LIMIT, offset=0)

    def test_query_is_passed_through(self) -> None:
        """Verify search, the past group, sort, order and paging reach the database layer."""
        self.staff("GET", "/api/calls?q=%20Anna%20&status=past,cancelled&sort=name&order=asc&limit=20&offset=40")
        self.db.list_calls.assert_called_once_with(
            search="Anna", statuses=sorted({*config.CALL_PAST_STATUSES, "cancelled"}), sort="name", descending=False,
            limit=20, offset=40)

    def test_invalid_query_is_rejected(self) -> None:
        """Verify unknown statuses, sort columns and bad paging return 400 without a query."""
        for query in ("status=lost", "sort=transcript", "order=up", "limit=0", "limit=101", "offset=-1", "limit=x"):
            with self.subTest(query=query):
                response, _ = self.staff("GET", f"/api/calls?{query}")
                self.assertEqual(response.status_code, 400)
        self.db.list_calls.assert_not_called()

    def test_database_error_is_private(self) -> None:
        """Verify a database failure returns a generic 503."""
        self.db.list_calls.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.staff("GET", "/api/calls")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SECRET_DETAIL, response.get_data(as_text=True))


class CallDetailTests(RouteTestCase):
    def test_typed_entry_is_loaded_without_caching(self) -> None:
        """Verify the entry ID is split into kind and UUID and the reply isn't cached."""
        self.db.get_call.return_value = {"id": f"call:{CALLBACK_ID}", "transcript": "Customer: Hi", "notes": []}
        response, _ = self.staff("GET", f"/api/calls/attempt:{CALLBACK_ID}")
        self.assertEqual(response.get_json()["transcript"], "Customer: Hi")
        self.assertTrue(response.cache_control.no_store)
        self.db.get_call.assert_called_once_with("attempt", uuid.UUID(CALLBACK_ID))

    def test_still_encoded_entry_id_is_decoded(self) -> None:
        """Verify an ID whose colon arrives encoded (Vercel passes "call%3A…" through) is still found."""
        self.db.get_call.return_value = {"id": f"call:{CALLBACK_ID}", "notes": []}
        for path in (f"/api/calls/call%253A{CALLBACK_ID}", f"/api/calls/call%253A{CALLBACK_ID}/notes"):
            method, kwargs = ("POST", {"json": {"body": "Hi"}}) if path.endswith("notes") else ("GET", {})
            self.staff(method, path, **kwargs)
        self.db.get_call.assert_called_once_with("call", uuid.UUID(CALLBACK_ID))
        self.db.add_call_note.assert_called_once_with("call", uuid.UUID(CALLBACK_ID), "user-1", "staff@example.com", "Hi")

    def test_malformed_or_missing_entries_are_not_found(self) -> None:
        """Verify bad IDs never reach the database and missing entries return 404."""
        for entry_id in ("call:nope", "customer:" + CALLBACK_ID, CALLBACK_ID):
            with self.subTest(entry_id=entry_id):
                response, _ = self.staff("GET", f"/api/calls/{entry_id}")
                self.assertEqual(response.status_code, 404)
        self.db.get_call.assert_not_called()
        response, _ = self.staff("GET", f"/api/calls/call:{CALLBACK_ID}")
        self.assertEqual(response.status_code, 404)

    def test_database_error_is_private(self) -> None:
        """Verify a database failure returns a generic 503."""
        self.db.get_call.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.staff("GET", f"/api/calls/call:{CALLBACK_ID}")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SECRET_DETAIL, response.get_data(as_text=True))


class CallAudioUrlTests(RouteTestCase):
    def test_ready_recording_gets_short_lived_link(self) -> None:
        """Verify the saved object is signed for the configured lifetime."""
        self.db.call_audio_key.return_value = "calls/a.wav"
        self.storage.signed_url.return_value = "https://project.supabase.co/storage/v1/object/sign/x?token=t"
        response, _ = self.staff("POST", f"/api/calls/call:{CALLBACK_ID}/audio-url")
        self.assertEqual(response.get_json(), {"url": "https://project.supabase.co/storage/v1/object/sign/x?token=t",
                                               "expires_in": config.ARCHIVE_PLAYBACK_SECONDS})
        self.assertTrue(response.cache_control.no_store)
        self.storage.signed_url.assert_called_once_with("calls/a.wav", config.ARCHIVE_PLAYBACK_SECONDS)

    def test_no_recording_is_not_found(self) -> None:
        """Verify calls without a saved recording and bad IDs return 404 without signing."""
        for entry_id in (f"call:{CALLBACK_ID}", "call:nope"):
            response, _ = self.staff("POST", f"/api/calls/{entry_id}/audio-url")
            self.assertEqual(response.status_code, 404)
        self.storage.signed_url.assert_not_called()

    def test_storage_error_is_private(self) -> None:
        """Verify a signing failure returns a generic 503."""
        self.db.call_audio_key.return_value = "calls/a.wav"
        self.storage.signed_url.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.staff("POST", f"/api/calls/call:{CALLBACK_ID}/audio-url")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SECRET_DETAIL, response.get_data(as_text=True))


class CallNoteTests(RouteTestCase):
    def test_note_is_saved_with_verified_author(self) -> None:
        """Verify the trimmed note is stored for the login's ID with its email as author label."""
        note = {"id": "n1", "author_label": "staff@example.com", "body": "Called back", "target": "call"}
        self.db.add_call_note.return_value = {"entry_id": f"call:{CALLBACK_ID}", "note": note}
        response, _ = self.staff("POST", f"/api/calls/attempt:{CALLBACK_ID}/notes",
                                 json={"body": "  Called back ", "author_label": "Someone else"})
        self.assertEqual((response.status_code, response.get_json()["note"]), (201, note))
        self.db.add_call_note.assert_called_once_with("attempt", uuid.UUID(CALLBACK_ID), "user-1", "staff@example.com", "Called back")

    def test_invalid_notes_are_rejected(self) -> None:
        """Verify missing, blank, non-text and too long notes return 400 without saving."""
        for data in (None, {}, {"body": "  "}, {"body": 5}, {"body": "x" * (config.CALL_NOTE_MAX_CHARS + 1)}):
            with self.subTest(data=data):
                response, _ = self.staff("POST", f"/api/calls/call:{CALLBACK_ID}/notes", json=data)
                self.assertEqual(response.status_code, 400)
        self.db.add_call_note.assert_not_called()

    def test_missing_target_and_database_error(self) -> None:
        """Verify a missing or malformed target is 404 and a database failure a generic 503."""
        for entry_id in (f"call:{CALLBACK_ID}", "call:nope"):
            response, _ = self.staff("POST", f"/api/calls/{entry_id}/notes", json={"body": "Hi"})
            self.assertEqual(response.status_code, 404)
        self.db.add_call_note.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.staff("POST", f"/api/calls/call:{CALLBACK_ID}/notes", json={"body": "Hi"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SECRET_DETAIL, response.get_data(as_text=True))


class DispatcherStatusTests(RouteTestCase):
    def test_status_never_run(self):
        response, _ = self.staff("GET", "/api/dispatcher/status")
        self.assertEqual((response.status_code, response.get_json()),
                         (200, {"last_run_at": None, "last_run_ok": None, "last_started": 0}))

    def test_status_row_with_iso_time(self):
        self.db.get_dispatcher_status.return_value = {
            "last_run_at": datetime(2026, 10, 18, 8, 1, 0, tzinfo=timezone.utc), "last_run_ok": True, "last_started": 2}
        response, _ = self.staff("GET", "/api/dispatcher/status")
        self.assertEqual((response.status_code, response.get_json()),
                         (200, {"last_run_at": "2026-10-18T08:01:00+00:00", "last_run_ok": True, "last_started": 2}))

    def test_status_db_error_500(self):
        self.db.get_dispatcher_status.side_effect = RuntimeError(SECRET_DETAIL)
        response, logs = self.staff("GET", "/api/dispatcher/status")
        self.assertEqual((response.status_code, response.get_json()),
                         (500, {"error": "Could not load the dispatcher status."}))
        self.assertIn("ERROR", logs)
        self.assertIn("Failed to load the dispatcher status", logs)


# --- Customers -------------------------------------------------------------

def customer_body(**overrides):
    body = {"first_name": "Anna", "middle_name": "", "last_name": "Muster", "phone": PHONE,
            "timezone": "Europe/Zurich", "vapi_phone_number_id": PHONE_ID, "vapi_assistant_id": ASSISTANT_ID}
    body.update(overrides)
    return body


class CustomersListTests(RouteTestCase):
    def test_list_customers_datetimes_as_iso(self):
        self.db.list_customers.return_value = [{"id": 7, "phone": PHONE,
                                                "next_callback_at": datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc),
                                                "last_call_at": None}]
        response, _ = self.staff("GET", "/api/customers")
        self.assertEqual(response.get_json(), [{"id": 7, "phone": PHONE, "next_callback_at": "2026-10-20T07:00:00+00:00",
                                                "last_call_at": None}])

    def test_list_customers_db_error_500(self):
        self.db.list_customers.side_effect = RuntimeError(SECRET_DETAIL)
        self.assertEqual(self.staff("GET", "/api/customers")[0].status_code, 500)


class CustomersCreateTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.db.insert_customer.return_value = {"id": 8, "phone": PHONE}

    def post(self, body):
        return self.staff("POST", "/api/customers", json=body)

    def assert_rejected(self, body, status=400):
        response, _ = self.post(body)
        self.assertEqual(response.status_code, status, response.get_json())
        return response

    def test_create_validation_400(self):
        cases = {
            "not an object": [1],
            "first name missing": customer_body(first_name=None),
            "first name blank": customer_body(first_name="   "),
            "last name missing": customer_body(last_name=""),
            "name too long": customer_body(first_name="a" * 101),
            "middle too long": customer_body(middle_name="a" * 101),
            "name not text": customer_body(last_name=5),
            "phone national": customer_body(phone="0791234567"),
            "phone national spaced": customer_body(phone="079 123 45 67"),
            "phone not text": customer_body(phone=41791234567),
            "phone missing": customer_body(phone=None),
            "timezone unknown": customer_body(timezone="Mars/Olympus"),
            "timezone missing": customer_body(timezone=None),
            "phone id missing": customer_body(vapi_phone_number_id=None),
            "assistant id blank": customer_body(vapi_assistant_id=" "),
            "assistant id invalid": customer_body(vapi_assistant_id="assistant-anna"),
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assert_rejected(body)
        self.memory.ensure_bank.assert_not_called()
        self.db.insert_customer.assert_not_called()

    def test_create_success_trims_names_and_creates_bank_first(self):
        order = MagicMock()
        order.attach_mock(self.memory.ensure_bank, "ensure_bank")
        order.attach_mock(self.db.insert_customer, "insert_customer")
        response, _ = self.post(customer_body(first_name=" Anna ", middle_name="  ", last_name=" Muster ",
                                              vapi_assistant_id=ASSISTANT_ID.upper()))
        self.assertEqual((response.status_code, response.get_json()), (201, {"id": 8, "phone": PHONE}))
        self.assertEqual(order.mock_calls, [
            call.ensure_bank(PHONE, "hs-key", config.ONBOARDING_MEMORY_TIMEOUT_SECONDS),
            call.insert_customer("Anna", None, "Muster", PHONE, "Europe/Zurich",
                                 vapi_phone_number_id=PHONE_ID, vapi_assistant_id=ASSISTANT_ID),
        ])

    def test_create_phone_error_message(self):
        response = self.assert_rejected(customer_body(phone="079 123 45 67"))
        self.assertEqual(
            response.get_json(),
            {"error": "phone must be an international number with country code, "
                      "e.g. +41 79 123 45 67 or 0041 79 123 45 67."},
        )

    def test_create_normalizes_phone_for_bank_and_insert(self):
        for spelling in ("0041 79 123 45 67", "+41 (0)79 123 45 67", PHONE + "\n", "+41-79-123.45/67"):
            with self.subTest(spelling=spelling):
                self.memory.ensure_bank.reset_mock()
                self.db.insert_customer.reset_mock()
                self.assertEqual(self.post(customer_body(phone=spelling))[0].status_code, 201)
                self.assertEqual(self.memory.ensure_bank.call_args.args[0], PHONE)
                self.assertEqual(self.db.insert_customer.call_args.args[3], PHONE)

    def test_create_name_lengths_at_limit_and_middle_name_kept(self):
        response, _ = self.post(customer_body(first_name="a" * 100, middle_name=" Maria "))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.db.insert_customer.call_args.args[:2], ("a" * 100, "Maria"))

    def test_create_existing_bank_still_creates_customer(self):
        self.memory.ensure_bank.return_value = False
        self.assertEqual(self.post(customer_body())[0].status_code, 201)

    def test_create_without_hindsight_key_503(self):
        os.environ.pop("HINDSIGHT_API_KEY")
        self.assert_rejected(customer_body(), 503)
        self.memory.ensure_bank.assert_not_called()
        self.db.insert_customer.assert_not_called()

    def test_create_bank_failure_502_without_insert(self):
        for error in (TimeoutError(), RuntimeError(SECRET_DETAIL)):
            with self.subTest(error=type(error).__name__):
                self.memory.ensure_bank.side_effect = error
                self.assert_rejected(customer_body(), 502)
        self.db.insert_customer.assert_not_called()

    def test_create_bank_value_error_502_without_insert(self):
        self.memory.ensure_bank.side_effect = ValueError("Not a normalizable phone number; refusing to build a memory bank id.")
        response = self.assert_rejected(customer_body(), 502)
        self.assertEqual(response.get_json(), {"error": "Could not set up the customer's memory. Please try again later."})
        self.db.insert_customer.assert_not_called()

    def test_create_duplicate_phone_409(self):
        self.db.insert_customer.side_effect = psycopg.errors.UniqueViolation(SECRET_DETAIL)
        response = self.assert_rejected(customer_body(), 409)
        self.assertEqual(response.get_json(), {"error": "A customer with this phone number already exists."})

    def test_create_db_error_500(self):
        self.db.insert_customer.side_effect = RuntimeError(SECRET_DETAIL)
        self.assert_rejected(customer_body(), 500)

    def test_create_created_at_is_iso_string(self):
        self.db.insert_customer.return_value = {"id": 8, "created_at": datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)}
        response, _ = self.post(customer_body())
        self.assertEqual(response.get_json(), {"id": 8, "created_at": "2026-10-17T12:00:00+00:00"})


# --- Scheduled callbacks ---------------------------------------------------

class ScheduledCallsCreateTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.scheduling.schedule_callback.return_value = (
            {"id": CALLBACK_ID, "due_at": datetime(2099, 6, 16, 7, 0, tzinfo=timezone.utc), "status": "pending"},
            datetime(2099, 6, 16, 9, 0))

    def post(self, body):
        return self.staff("POST", "/api/scheduled-calls", json=body)

    def test_schedule_validation_400(self):
        good = {"phone": PHONE, "local_time": "2099-06-15T21:00"}
        for body in ([1], {}, {**good, "phone": "079"}, {**good, "phone": "079 123 45 67"}, {**good, "phone": 41791234567},
                     {**good, "local_time": "2099-06-15"}, {**good, "local_time": "2099-06-15T21:00Z"},
                     {**good, "local_time": None}, {**good, "vapi_assistant_id": "not-a-uuid"}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0].status_code, 400)
        self.db.get_customer_by_phone.assert_not_called()

    def test_schedule_success_201_with_iso_times(self):
        response, _ = self.post({"phone": PHONE, "local_time": "2099-06-15T21:00"})
        self.assertEqual((response.status_code, response.get_json()), (201, {
            "id": CALLBACK_ID, "due_at": "2099-06-16T07:00:00+00:00", "status": "pending",
            "due_local_time": "2099-06-16T09:00"}))
        self.scheduling.schedule_callback.assert_called_once_with(
            CUSTOMER, datetime(2099, 6, 15, 21, 0), instructions=None, vapi_phone_number_id=None, vapi_assistant_id=None)

    def test_schedule_already_scheduled_200_with_flag(self):
        row, due_local = self.scheduling.schedule_callback.return_value
        self.scheduling.schedule_callback.return_value = ({**row, "already_scheduled": True}, due_local)
        response, _ = self.post({"phone": PHONE, "local_time": "2099-06-15T21:00"})
        self.assertEqual((response.status_code, response.get_json()), (200, {
            "id": CALLBACK_ID, "due_at": "2099-06-16T07:00:00+00:00", "status": "pending",
            "already_scheduled": True, "due_local_time": "2099-06-16T09:00"}))

    def test_schedule_new_row_with_flag_false_201(self):
        row, due_local = self.scheduling.schedule_callback.return_value
        self.scheduling.schedule_callback.return_value = ({**row, "already_scheduled": False}, due_local)
        response, _ = self.post({"phone": PHONE, "local_time": "2099-06-15T21:00"})
        self.assertEqual(response.status_code, 201)
        self.assertIs(response.get_json()["already_scheduled"], False)

    def test_schedule_phone_error_message(self):
        response, _ = self.post({"phone": "anonymous", "local_time": "2099-06-15T21:00"})
        self.assertEqual(
            (response.status_code, response.get_json()),
            (400, {"error": "phone must be an international number with country code, "
                            "e.g. +41 79 123 45 67 or 0041 79 123 45 67."}),
        )

    def test_schedule_normalizes_phone_for_lookup(self):
        for spelling in (PHONE + "\n", "0041 79 123 45 67", "+41 (0)79 123 45 67"):
            with self.subTest(spelling=spelling):
                self.db.get_customer_by_phone.reset_mock()
                response, _ = self.post({"phone": spelling, "local_time": "2099-06-15T21:00"})
                self.assertEqual(response.status_code, 201)
                self.db.get_customer_by_phone.assert_called_once_with(PHONE)

    def test_schedule_passes_id_overrides(self):
        self.post({"phone": PHONE, "local_time": "2099-06-15T21:00:30", "vapi_phone_number_id": PHONE_ID,
                   "vapi_assistant_id": ""})
        self.scheduling.schedule_callback.assert_called_once_with(
            CUSTOMER, datetime(2099, 6, 15, 21, 0), instructions=None, vapi_phone_number_id=PHONE_ID, vapi_assistant_id=None)

    def test_schedule_instructions_passed_trimmed(self):
        self.post({"phone": PHONE, "local_time": "2099-06-15T21:00", "instructions": " Ask about the invoice. "})
        self.assertEqual(self.scheduling.schedule_callback.call_args.kwargs["instructions"], "Ask about the invoice.")

    def test_schedule_blank_instructions_become_none(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                self.scheduling.schedule_callback.reset_mock()
                self.post({"phone": PHONE, "local_time": "2099-06-15T21:00", "instructions": value})
                self.assertIsNone(self.scheduling.schedule_callback.call_args.kwargs["instructions"])

    def test_schedule_invalid_instructions_400_without_calls(self):
        for value, error in (("a" * (config.MAX_INSTRUCTIONS_LENGTH + 1), "instructions can be at most 1000 characters."),
                             (5, "instructions must be text."), (["x"], "instructions must be text.")):
            with self.subTest(value=str(value)[:10]):
                response, _ = self.post({"phone": PHONE, "local_time": "2099-06-15T21:00", "instructions": value})
                self.assertEqual((response.status_code, response.get_json()), (400, {"error": error}))
        self.db.get_customer_by_phone.assert_not_called()
        self.scheduling.schedule_callback.assert_not_called()

    def test_schedule_lookup_error_500(self):
        self.db.get_customer_by_phone.side_effect = RuntimeError(SECRET_DETAIL)
        self.assertEqual(self.post({"phone": PHONE, "local_time": "2099-06-15T21:00"})[0].status_code, 500)
        self.scheduling.schedule_callback.assert_not_called()

    def test_schedule_unknown_customer_404(self):
        self.db.get_customer_by_phone.return_value = None
        self.assertEqual(self.post({"phone": OTHER_PHONE, "local_time": "2099-06-15T21:00"})[0].status_code, 404)
        self.scheduling.schedule_callback.assert_not_called()

    def test_schedule_error_uses_its_status_and_message(self):
        for message, status in (("This customer has opted out of calls.", 409),
                                ("local_time must be in the future (customer's time zone).", 400),
                                ("Could not schedule the call. Please try again later.", 500)):
            with self.subTest(status=status):
                self.scheduling.schedule_callback.side_effect = scheduling.ScheduleError(message, status)
                response, _ = self.post({"phone": PHONE, "local_time": "2099-06-15T21:00"})
                self.assertEqual((response.status_code, response.get_json()), (status, {"error": message}))


class CancelScheduledCallTests(RouteTestCase):
    def post(self, callback_id):
        return self.staff("POST", f"/api/scheduled-calls/{callback_id}/cancel")

    def test_cancel_invalid_id_404_without_db(self):
        for callback_id in ("abc", "1", CALLBACK_ID + "%0A", CALLBACK_ID + "0", "%20" + CALLBACK_ID):
            with self.subTest(callback_id=callback_id):
                self.assertEqual(self.post(callback_id)[0].status_code, 404)
        self.db.cancel_scheduled_call.assert_not_called()

    def test_cancel_success(self):
        self.db.cancel_scheduled_call.return_value = "cancelled"
        response, _ = self.post(CALLBACK_ID)
        self.assertEqual((response.status_code, response.get_json()), (200, {"id": CALLBACK_ID, "status": "cancelled"}))
        self.db.cancel_scheduled_call.assert_called_once_with(CALLBACK_ID)

    def test_cancel_unknown_404(self):
        self.db.cancel_scheduled_call.return_value = None
        self.assertEqual(self.post(CALLBACK_ID)[0].status_code, 404)

    def test_cancel_not_pending_409_with_status(self):
        for status in ("calling", "completed", "failed"):
            with self.subTest(status=status):
                self.db.cancel_scheduled_call.return_value = status
                response, _ = self.post(CALLBACK_ID)
                self.assertEqual(response.status_code, 409)
                self.assertIn(f"it is {status}", response.get_json()["error"])

    def test_cancel_db_error_500(self):
        self.db.cancel_scheduled_call.side_effect = RuntimeError(SECRET_DETAIL)
        self.assertEqual(self.post(CALLBACK_ID)[0].status_code, 500)


class UpdateCustomerTests(RouteTestCase):
    def post(self, body, customer_id=CUSTOMER_ID):
        return self.staff("POST", f"/api/customers/{customer_id}", json=body)

    def updated(self, **fields):
        return {"id": CUSTOMER_ID, "callbacks_moved": 0, **fields}

    def test_assistant_only_normalizes_the_id(self):
        self.db.update_customer.return_value = self.updated(vapi_assistant_id=ASSISTANT_ID)
        response, _ = self.post({"vapi_assistant_id": f" {ASSISTANT_ID.upper()} "})
        self.assertEqual((response.status_code, response.get_json()),
                         (200, self.updated(vapi_assistant_id=ASSISTANT_ID)))
        self.db.update_customer.assert_called_once_with(CUSTOMER_ID, {"vapi_assistant_id": ASSISTANT_ID})

    def test_names_and_timezone_trimmed(self):
        self.db.update_customer.return_value = self.updated(callbacks_moved=2)
        response, logs = self.post({"first_name": " Anna ", "middle_name": "  ", "last_name": "Muster ",
                                    "timezone": "America/New_York"})
        self.assertEqual(response.status_code, 200)
        self.db.update_customer.assert_called_once_with(CUSTOMER_ID, {
            "first_name": "Anna", "middle_name": None, "last_name": "Muster", "timezone": "America/New_York"})
        self.assertIn("changed first_name, last_name, middle_name, timezone of customer", logs)
        self.assertIn("(2 callbacks moved)", logs)
        self.assertNotIn("Anna", logs)

    def test_only_sent_fields_changed(self):
        self.db.update_customer.return_value = self.updated()
        self.post({"last_name": "Neu"})
        self.db.update_customer.assert_called_once_with(CUSTOMER_ID, {"last_name": "Neu"})

    def test_invalid_fields_400_without_db(self):
        for body in ({"first_name": ""}, {"last_name": "   "}, {"first_name": None}, {"first_name": 5},
                     {"middle_name": "x" * 101}, {"timezone": "Mars/Base"}, {"timezone": None},
                     {"vapi_assistant_id": "abc"}, {"vapi_assistant_id": None},
                     {}, {"phone": "+41791234567"}, [ASSISTANT_ID]):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0].status_code, 400)
        self.db.update_customer.assert_not_called()

    def test_invalid_customer_id_404_without_db(self):
        self.assertEqual(self.post({"vapi_assistant_id": ASSISTANT_ID}, "abc")[0].status_code, 404)
        self.db.update_customer.assert_not_called()

    def test_unknown_customer_404(self):
        self.db.update_customer.return_value = None
        self.assertEqual(self.post({"vapi_assistant_id": ASSISTANT_ID})[0].status_code, 404)

    def test_db_error_500_without_details(self):
        self.db.update_customer.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.post({"first_name": "Anna"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SECRET_DETAIL, response.get_data(as_text=True))


class MarkCustomerReviewedTests(RouteTestCase):
    def post(self, customer_id=CUSTOMER_ID):
        return self.staff("POST", f"/api/customers/{customer_id}/reviewed")

    def test_success(self):
        self.db.mark_customer_reviewed.return_value = True
        response, logs = self.post()
        self.assertEqual((response.status_code, response.get_json()),
                         (200, {"id": CUSTOMER_ID, "needs_review": False}))
        self.db.mark_customer_reviewed.assert_called_once_with(CUSTOMER_ID)
        self.assertIn(f"User user-1 marked customer {CUSTOMER_ID} as reviewed", logs)

    def test_invalid_customer_id_404_without_db(self):
        self.assertEqual(self.post("abc")[0].status_code, 404)
        self.db.mark_customer_reviewed.assert_not_called()

    def test_unknown_customer_404(self):
        self.db.mark_customer_reviewed.return_value = False
        self.assertEqual(self.post()[0].status_code, 404)

    def test_db_error_500_without_details(self):
        self.db.mark_customer_reviewed.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SECRET_DETAIL, response.get_data(as_text=True))


class DangerZoneTests(RouteTestCase):
    """POST /api/customers/<id>/delete and /erase-memory: shared checks, then each action."""

    ACTIONS = ("delete", "erase-memory")

    def setUp(self):
        super().setUp()
        self.db.get_customer.return_value = {"id": CUSTOMER_ID, "first_name": "Anna", "last_name": "Muster",
                                             "phone": PHONE, "calling_now": False}
        self.db.delete_customer.return_value = "deleted"

    def post(self, action, customer_id=CUSTOMER_ID, **kwargs):
        if "data" not in kwargs:
            kwargs.setdefault("json", {"confirm_phone": PHONE})
        return self.staff("POST", f"/api/customers/{customer_id}/{action}", **kwargs)

    def assert_nothing_changed(self):
        self.memory.delete_bank.assert_not_called()
        self.memory.erase_memory.assert_not_called()
        self.db.delete_customer.assert_not_called()

    def test_invalid_id_404_without_db(self):
        for action in self.ACTIONS:
            for customer_id in ("7", "abc", CUSTOMER_ID + "%0A"):
                with self.subTest(action=action, customer_id=customer_id):
                    self.assertEqual(self.post(action, customer_id)[0].status_code, 404)
        self.db.get_customer.assert_not_called()
        self.assert_nothing_changed()

    def test_no_json_object_400(self):
        for action in self.ACTIONS:
            for kwargs in ({"data": "x", "content_type": "application/json"}, {"json": ["a"]}):
                with self.subTest(action=action, kwargs=kwargs):
                    self.assertEqual(self.post(action, **kwargs)[0].status_code, 400)
        self.assert_nothing_changed()

    def test_unknown_customer_404(self):
        self.db.get_customer.return_value = None
        for action in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(action)[0].status_code, 404)
        self.assert_nothing_changed()

    def test_lookup_error_500(self):
        self.db.get_customer.side_effect = RuntimeError(SECRET_DETAIL)
        for action in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(action)[0].status_code, 500)
        self.assert_nothing_changed()

    def test_wrong_or_missing_phone_400(self):
        for action in self.ACTIONS:
            for body in ({}, {"confirm_phone": None}, {"confirm_phone": OTHER_PHONE}, {"confirm_phone": PHONE[:-1]},
                         {"confirm_phone": "079 123 45 67"}, {"confirm_phone": 41791234567}):
                with self.subTest(action=action, body=body):
                    response, _ = self.post(action, json=body)
                    self.assertEqual((response.status_code, response.get_json()),
                                     (400, {"error": "The phone number doesn't match this customer."}))
        self.assert_nothing_changed()

    def test_phone_formatting_ignored(self):
        for action in self.ACTIONS:
            for typed in ("+41 79 123 45 67", "0041 79-123.45.67"):
                with self.subTest(action=action, typed=typed):
                    self.assertEqual(self.post(action, json={"confirm_phone": typed})[0].status_code, 200)

    def test_call_in_progress_409(self):
        self.db.get_customer.return_value["calling_now"] = True
        for action in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(action)[0].status_code, 409)
        self.assert_nothing_changed()

    def test_missing_hindsight_key_503(self):
        os.environ.pop("HINDSIGHT_API_KEY")
        for action in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(action)[0].status_code, 503)
        self.assert_nothing_changed()

    def test_delete_success_memory_first_and_logged(self):
        order = MagicMock()
        self.memory.delete_bank.side_effect = lambda *a: order.memory()
        self.db.delete_customer.side_effect = lambda *a: (order.db(), "deleted")[1]
        response, logs = self.post("delete")
        self.assertEqual((response.status_code, response.get_json()), (200, {"id": CUSTOMER_ID, "deleted": True}))
        self.assertEqual(order.mock_calls, [call.memory(), call.db()])
        self.memory.delete_bank.assert_called_once_with(PHONE, "hs-key", config.DANGER_MEMORY_TIMEOUT_SECONDS)
        self.db.delete_customer.assert_called_once_with(CUSTOMER_ID)
        self.assertIn(f"User user-1 deleted customer {CUSTOMER_ID} (+4179***67)", logs)

    def test_delete_memory_error_502_customer_kept(self):
        self.memory.delete_bank.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.post("delete")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Nothing was deleted", response.get_json()["error"])
        self.db.delete_customer.assert_not_called()

    def test_delete_db_error_500_says_memory_erased(self):
        self.db.delete_customer.side_effect = RuntimeError(SECRET_DETAIL)
        response, _ = self.post("delete")
        self.assertEqual(response.status_code, 500)
        self.assertIn("memory was erased", response.get_json()["error"])

    def test_delete_customer_gone_meanwhile_404(self):
        self.db.delete_customer.return_value = None
        self.assertEqual(self.post("delete")[0].status_code, 404)

    def test_delete_call_started_meanwhile_409(self):
        self.db.delete_customer.return_value = "calling"
        response, _ = self.post("delete")
        self.assertEqual(response.status_code, 409)
        self.assertIn("memory was erased", response.get_json()["error"])

    def test_erase_success_customer_kept_and_logged(self):
        response, logs = self.post("erase-memory")
        self.assertEqual((response.status_code, response.get_json()), (200, {"id": CUSTOMER_ID, "memory_erased": True}))
        self.memory.erase_memory.assert_called_once_with(PHONE, "hs-key", config.DANGER_MEMORY_TIMEOUT_SECONDS)
        self.memory.delete_bank.assert_not_called()
        self.db.delete_customer.assert_not_called()
        self.assertIn(f"User user-1 erased the memory of customer {CUSTOMER_ID} (+4179***67)", logs)

    def test_erase_error_502(self):
        self.memory.erase_memory.side_effect = TimeoutError()
        self.assertEqual(self.post("erase-memory")[0].status_code, 502)


if __name__ == "__main__":
    unittest.main()
