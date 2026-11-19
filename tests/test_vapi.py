import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from albert import config
from albert.vapi import (
    MemoryUnavailable,
    OutboundCallError,
    VapiUnavailable,
    assistant_request_response,
    caller_number,
    get_vapi_call,
    list_outbound_calls,
    list_vapi,
    place_outbound_call,
)
from main import app

PHONE = "+41791234567"
MEMORY = "Anna likes tea and asked about her invoice."
VARIABLE_KEYS = {config.MEMORY_VARIABLE, config.INSTRUCTIONS_VARIABLE, config.PENDING_CALLBACKS_VARIABLE,
                 "customer_first_name", "customer_last_name", "customer_local_now"}
PENDING_ROWS = [{"id": "cb-1", "due_at": datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc), "instructions": None}]
PENDING_TEXT = "Thursday, 22 October 2026 at 14:30"  # Europe/Zurich
CUSTOMER = {"id": 7, "first_name": "Anna", "last_name": "Muster", "phone": PHONE, "timezone": "Europe/Zurich",
            "opted_out": False, "vapi_assistant_id": "assistant-anna", "vapi_phone_number_id": "phone-anna"}
UNSET = ("VAPI_API_KEY", "HINDSIGHT_API_KEY", "VAPI_ASSISTANT_ID", "VAPI_INBOUND_ASSISTANT_ID_US",
         "VAPI_INBOUND_ASSISTANT_ID_CH")


def request_message(number=PHONE):
    return {"type": "assistant-request", "call": {"customer": {"number": number}}}


def fake_response(status=200, body=None, text="", invalid_json=False):
    response = MagicMock()
    response.status_code = status
    response.ok = status < 400
    response.text = text
    if invalid_json:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
    return response


class VapiTestCase(unittest.TestCase):
    env = {}

    def setUp(self):
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in UNSET:
            if name not in self.env:
                os.environ.pop(name, None)
        patcher = patch("albert.customers.db.pending_callbacks", return_value=PENDING_ROWS)
        self.pending_callbacks = patcher.start()
        self.addCleanup(patcher.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    def assert_no_secrets(self, logs):
        """Neither the full phone number nor the memory text may appear in the logs."""
        text = "\n".join(logs.output)
        self.assertNotIn(PHONE, text)
        self.assertNotIn(MEMORY, text)


class CallerNumberTests(unittest.TestCase):
    def test_caller_number_present_returned(self):
        self.assertEqual(caller_number(request_message()), PHONE)

    def test_caller_number_normalized(self):
        for number in ("+41 79 123 45 67", "0041791234567", PHONE + "\n", "+41 (0)79 123 45 67"):
            with self.subTest(number=number):
                self.assertEqual(caller_number(request_message(number)), PHONE)

    def test_caller_number_unnormalizable_returned_raw(self):
        for number in ("anonymous", "Restricted", "079 123 45 67", 41791234567):
            with self.subTest(number=number):
                self.assertEqual(caller_number(request_message(number)), number)

    def test_caller_number_missing_shapes_none(self):
        for message in ({}, {"call": None}, {"call": {}}, {"call": {"customer": None}},
                        {"call": {"customer": "x"}}, {"call": {"customer": {}}}, None, [], "text"):
            with self.subTest(message=message):
                self.assertIsNone(caller_number(message))

    def test_caller_number_non_dict_call_none(self):
        for message in ({"call": "x"}, {"call": ["x"]}):
            with self.subTest(message=message):
                self.assertIsNone(caller_number(message))


@patch("albert.vapi.memory.recall_for_phone", return_value=(MEMORY, False))
@patch("albert.vapi.db.get_customer_by_phone", return_value=CUSTOMER)
class AssistantRequestTests(VapiTestCase):
    env = {"VAPI_ASSISTANT_ID": "assistant-default"}

    def test_customer_gets_own_assistant_and_memory(self, get, recall):
        with self.assertLogs(app.logger, "INFO") as logs:
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(reply["assistantId"], "assistant-anna")
        variables = reply["assistantOverrides"]["variableValues"]
        self.assertEqual(set(variables), VARIABLE_KEYS)
        self.assertEqual((variables[config.MEMORY_VARIABLE], variables["customer_first_name"],
                          variables["customer_last_name"]), (MEMORY, "Anna", "Muster"))
        self.assertIn("(Europe/Zurich, UTC", variables["customer_local_now"])
        get.assert_called_once_with(PHONE)
        recall.assert_called_once_with(PHONE, "hs-key", config.INBOUND_MEMORY_TIMEOUT_SECONDS)
        self.assert_no_secrets(logs)

    def test_inbound_replies_have_no_instructions_text(self, get, recall):
        for customer in (CUSTOMER, None):
            with self.subTest(customer=bool(customer)):
                get.return_value = customer
                with self.assertLogs(app.logger, "INFO"):
                    reply = assistant_request_response(request_message(), "hs-key")
                self.assertEqual(reply["assistantOverrides"]["variableValues"][config.INSTRUCTIONS_VARIABLE],
                                 config.NO_INSTRUCTIONS_TEXT)

    def test_inbound_customer_gets_pending_callbacks(self, get, recall):
        with self.assertLogs(app.logger, "INFO"):
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(reply["assistantOverrides"]["variableValues"][config.PENDING_CALLBACKS_VARIABLE], PENDING_TEXT)
        self.pending_callbacks.assert_called_once_with(7)

    def test_inbound_non_customer_no_pending_callbacks_without_lookup(self, get, recall):
        get.return_value = None
        with self.assertLogs(app.logger, "INFO"):
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(reply["assistantOverrides"]["variableValues"][config.PENDING_CALLBACKS_VARIABLE],
                         config.NO_PENDING_CALLBACKS_TEXT)
        self.pending_callbacks.assert_not_called()

    def test_inbound_pending_callbacks_error_still_answers(self, get, recall):
        self.pending_callbacks.side_effect = RuntimeError("db down")
        with self.assertLogs(app.logger, "INFO"):
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(reply["assistantId"], "assistant-anna")
        self.assertEqual(reply["assistantOverrides"]["variableValues"][config.PENDING_CALLBACKS_VARIABLE],
                         config.PENDING_CALLBACKS_UNAVAILABLE_TEXT)

    def test_customer_new_bank_still_answered(self, get, recall):
        recall.return_value = (config.NO_MEMORY_TEXT, True)
        with self.assertLogs(app.logger, "INFO"):
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(reply["assistantOverrides"]["variableValues"][config.MEMORY_VARIABLE], config.NO_MEMORY_TEXT)

    def test_unusable_number_rejected_without_lookups(self, get, recall):
        for number in (None, "", "anonymous", "Restricted", "0791234567", "079 123 45 67", "41791234567",
                       41791234567):
            with self.subTest(number=number):
                with self.assertLogs(app.logger, "INFO"):
                    reply = assistant_request_response(request_message(number), "hs-key")
                self.assertEqual(reply, {"error": config.INBOUND_NO_NUMBER_MESSAGE})
        for message in ({}, {"call": {}}):
            with self.subTest(message=message), self.assertLogs(app.logger, "INFO"):
                self.assertEqual(assistant_request_response(message, "hs-key"),
                                 {"error": config.INBOUND_NO_NUMBER_MESSAGE})
        get.assert_not_called()
        recall.assert_not_called()

    def test_differently_written_numbers_normalized_for_lookup_and_memory(self, get, recall):
        for number in ("+41 79 123 45 67", PHONE + "\n", "0041 79 123 45 67", "+41 (0)79 123 45 67"):
            with self.subTest(number=number):
                get.reset_mock()
                recall.reset_mock()
                with self.assertLogs(app.logger, "INFO"):
                    reply = assistant_request_response(request_message(number), "hs-key")
                self.assertEqual(reply["assistantId"], "assistant-anna")
                get.assert_called_once_with(PHONE)
                recall.assert_called_once_with(PHONE, "hs-key", config.INBOUND_MEMORY_TIMEOUT_SECONDS)

    def test_customer_lookup_error_rejected_without_hindsight(self, get, recall):
        get.side_effect = RuntimeError("connection refused")
        with self.assertLogs(app.logger, "INFO") as logs:
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(reply, {"error": config.INBOUND_NO_MEMORY_MESSAGE})
        recall.assert_not_called()
        self.assertNotIn(PHONE, "\n".join(logs.output))

    def test_non_customer_default_assistant_without_memory(self, get, recall):
        get.return_value = None
        with self.assertLogs(app.logger, "INFO") as logs:
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(reply["assistantId"], "assistant-default")
        variables = reply["assistantOverrides"]["variableValues"]
        self.assertEqual(set(variables), VARIABLE_KEYS)
        self.assertEqual((variables[config.MEMORY_VARIABLE], variables["customer_first_name"],
                          variables["customer_last_name"]),
                         (config.NO_MEMORY_TEXT, config.UNKNOWN_CUSTOMER_NAME, config.UNKNOWN_CUSTOMER_NAME))
        recall.assert_not_called()
        self.assert_no_secrets(logs)

    def test_non_customer_local_time_in_zone_guessed_from_number(self, get, recall):
        get.return_value = None
        for number, zone in (("+12125551234", "America/New_York"), ("+14155551234", "America/Los_Angeles"),
                             (PHONE, "Europe/Zurich")):
            with self.subTest(number=number):
                reply = assistant_request_response(request_message(number), "hs-key")
                self.assertIn(f"({zone}, UTC", reply["assistantOverrides"]["variableValues"]["customer_local_now"])

    def test_non_customer_from_unsupported_country_rejected(self, get, recall):
        get.return_value = None
        for number in ("+4930123456789", "+447911123456", "+18765551234"):  # Germany, UK, Jamaica (+1)
            with self.subTest(number=number):
                with self.assertLogs(app.logger, "INFO") as logs:
                    reply = assistant_request_response(request_message(number), "hs-key")
                self.assertEqual(reply, {"error": config.INBOUND_COUNTRY_NOT_SUPPORTED_MESSAGE})
                self.assertIn("country not supported", "\n".join(logs.output))
                self.assertNotIn(number, "\n".join(logs.output))
        recall.assert_not_called()

    def test_customer_from_unsupported_country_still_answered(self, get, recall):
        get.return_value = {**CUSTOMER, "phone": "+4930123456789"}
        recall.return_value = (MEMORY, False)
        reply = assistant_request_response(request_message("+4930123456789"), "hs-key")
        self.assertEqual(reply["assistantId"], "assistant-anna")

    def test_non_customer_gets_their_countrys_inbound_assistant(self, get, recall):
        get.return_value = None
        env = {"VAPI_INBOUND_ASSISTANT_ID_US": "assistant-en", "VAPI_INBOUND_ASSISTANT_ID_CH": "assistant-de"}
        with patch.dict(os.environ, env):
            for number, assistant, suffix in (("+12125551234", "assistant-en", "US"),
                                              ("+16045551234", "assistant-en", "US"),  # Canada
                                              (PHONE, "assistant-de", "CH")):
                with self.subTest(number=number):
                    with self.assertLogs(app.logger, "INFO") as logs:
                        reply = assistant_request_response(request_message(number), "hs-key")
                    self.assertEqual(reply["assistantId"], assistant)
                    self.assertIn(f"{suffix} inbound assistant, no memory", "\n".join(logs.output))
        recall.assert_not_called()

    def test_non_customer_country_without_inbound_assistant_uses_default(self, get, recall):
        get.return_value = None
        with patch.dict(os.environ, {"VAPI_INBOUND_ASSISTANT_ID_CH": "assistant-de"}):
            self.assertEqual(assistant_request_response(request_message("+12125551234"), "hs-key")["assistantId"],
                             "assistant-default")
        with patch.dict(os.environ, {"VAPI_INBOUND_ASSISTANT_ID_US": ""}):
            self.assertEqual(assistant_request_response(request_message("+12125551234"), "hs-key")["assistantId"],
                             "assistant-default")

    def test_non_customer_inbound_assistant_without_default(self, get, recall):
        get.return_value = None
        os.environ.pop("VAPI_ASSISTANT_ID")
        with patch.dict(os.environ, {"VAPI_INBOUND_ASSISTANT_ID_CH": "assistant-de"}):
            self.assertEqual(assistant_request_response(request_message(), "hs-key")["assistantId"], "assistant-de")

    def test_non_customer_without_default_assistant_error(self, get, recall):
        get.return_value = None
        os.environ.pop("VAPI_ASSISTANT_ID")
        with self.assertLogs(app.logger, "ERROR"):
            reply = assistant_request_response(request_message(), "hs-key")
        self.assertEqual(set(reply), {"error"})
        self.assertNotIn("assistantId", reply)
        recall.assert_not_called()

    def test_customer_recall_failure_rejected(self, get, recall):
        for error in (TimeoutError(), RuntimeError("hindsight 500")):
            with self.subTest(error=type(error).__name__):
                recall.side_effect = error
                with self.assertLogs(app.logger, "INFO") as logs:
                    reply = assistant_request_response(request_message(), "hs-key")
                self.assertEqual(reply, {"error": config.INBOUND_NO_MEMORY_MESSAGE})
                self.assertNotIn(PHONE, "\n".join(logs.output))


@patch("albert.vapi.requests.post")
@patch("albert.vapi.memory.recall_for_phone", return_value=(MEMORY, False))
class PlaceOutboundCallTests(VapiTestCase):
    env = {"VAPI_API_KEY": "vapi-key", "HINDSIGHT_API_KEY": "hs-key"}

    def call(self, customer=CUSTOMER):
        return place_outbound_call(PHONE, "assistant-anna", "phone-anna", customer)

    def test_customer_call_posts_exact_payload_and_returns_id(self, recall, post):
        post.return_value = fake_response(201, {"id": "call-1"})
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertEqual(self.call(), "call-1")
        recall.assert_called_once_with(PHONE, "hs-key", config.OUTBOUND_MEMORY_TIMEOUT_SECONDS)
        args, kwargs = post.call_args
        self.assertEqual(args, (config.VAPI_CALL_URL,))
        self.assertEqual((kwargs["headers"], kwargs["timeout"]), ({"Authorization": "Bearer vapi-key"}, 10))
        payload = kwargs["json"]
        variables = payload["assistantOverrides"].pop("variableValues")
        self.assertEqual(payload, {"assistantId": "assistant-anna", "phoneNumberId": "phone-anna",
                                   "assistantOverrides": {}, "customer": {"number": PHONE}})
        self.assertEqual(set(variables), VARIABLE_KEYS)
        self.assertEqual((variables[config.MEMORY_VARIABLE], variables["customer_first_name"]), (MEMORY, "Anna"))
        self.assert_no_secrets(logs)

    def test_call_instructions_variable(self, recall, post):
        post.return_value = fake_response(201, {"id": "call-1"})
        cases = (("Ask about the invoice.", CUSTOMER, "Ask about the invoice."),
                 ("Ask about the invoice.", None, "Ask about the invoice."),
                 (None, CUSTOMER, config.NO_INSTRUCTIONS_TEXT),
                 ("", CUSTOMER, config.NO_INSTRUCTIONS_TEXT),
                 (None, None, config.NO_INSTRUCTIONS_TEXT))
        for instructions, customer, expected in cases:
            with self.subTest(instructions=instructions, customer=bool(customer)):
                with self.assertLogs(app.logger, "INFO") as logs:
                    place_outbound_call(PHONE, "assistant-anna", "phone-anna", customer, instructions)
                variables = post.call_args.kwargs["json"]["assistantOverrides"]["variableValues"]
                self.assertEqual(variables[config.INSTRUCTIONS_VARIABLE], expected)
                self.assertEqual(set(variables), VARIABLE_KEYS)
                if instructions:
                    self.assertNotIn(instructions, "\n".join(logs.output))

    def test_call_pending_callbacks_variable(self, recall, post):
        post.return_value = fake_response(201, {"id": "call-1"})
        for customer, expected in ((CUSTOMER, PENDING_TEXT), (None, config.NO_PENDING_CALLBACKS_TEXT)):
            with self.subTest(customer=bool(customer)):
                self.pending_callbacks.reset_mock()
                with self.assertLogs(app.logger, "INFO"):
                    place_outbound_call(PHONE, "assistant-anna", "phone-anna", customer)
                variables = post.call_args.kwargs["json"]["assistantOverrides"]["variableValues"]
                self.assertEqual(variables[config.PENDING_CALLBACKS_VARIABLE], expected)
                self.assertEqual(self.pending_callbacks.call_count, 1 if customer else 0)

    def test_call_default_instructions_argument(self, recall, post):
        post.return_value = fake_response(201, {"id": "call-1"})
        with self.assertLogs(app.logger, "INFO"):
            self.call()
        variables = post.call_args.kwargs["json"]["assistantOverrides"]["variableValues"]
        self.assertEqual(variables[config.INSTRUCTIONS_VARIABLE], config.NO_INSTRUCTIONS_TEXT)

    def test_non_customer_call_without_hindsight(self, recall, post):
        post.return_value = fake_response(201, {"id": "call-2"})
        os.environ.pop("HINDSIGHT_API_KEY")  # not needed for non-customers
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertEqual(self.call(customer=None), "call-2")
        recall.assert_not_called()
        variables = post.call_args.kwargs["json"]["assistantOverrides"]["variableValues"]
        self.assertEqual((variables[config.MEMORY_VARIABLE], variables["customer_first_name"]),
                         (config.NO_MEMORY_TEXT, config.UNKNOWN_CUSTOMER_NAME))
        self.assertNotIn(PHONE, "\n".join(logs.output))

    def test_missing_vapi_key_503_without_calls(self, recall, post):
        os.environ.pop("VAPI_API_KEY")
        with self.assertRaises(OutboundCallError) as cm:
            self.call()
        self.assertEqual(cm.exception.status, 503)
        recall.assert_not_called()
        post.assert_not_called()

    def test_customer_missing_hindsight_key_503_without_post(self, recall, post):
        os.environ.pop("HINDSIGHT_API_KEY")
        with self.assertRaises(OutboundCallError) as cm:
            self.call()
        self.assertEqual(cm.exception.status, 503)
        self.assertNotIsInstance(cm.exception, MemoryUnavailable)
        post.assert_not_called()

    def test_customer_recall_failure_memory_unavailable_without_post(self, recall, post):
        for error in (TimeoutError(), RuntimeError("hindsight 500")):
            with self.subTest(error=type(error).__name__):
                recall.side_effect = error
                with self.assertLogs(app.logger, "INFO") as logs, self.assertRaises(MemoryUnavailable) as cm:
                    self.call()
                self.assertIsInstance(cm.exception, OutboundCallError)
                self.assertEqual(cm.exception.status, 502)
                self.assertNotIn(PHONE, cm.exception.message + "\n".join(logs.output))
        post.assert_not_called()

    def test_customer_unnormalizable_number_memory_unavailable_without_post(self, recall, post):
        for number in ("0791234567", "anonymous"):
            with self.subTest(number=number):
                with self.assertLogs(app.logger, "INFO") as logs, self.assertRaises(MemoryUnavailable) as cm:
                    place_outbound_call(number, "assistant-anna", "phone-anna", CUSTOMER)
                self.assertEqual(cm.exception.status, 502)
                text = "\n".join(logs.output)
                self.assertIn("Memory lookup failed for", text)
                self.assertNotIn("0791234567", text)
        recall.assert_not_called()
        post.assert_not_called()

    def test_vapi_network_error_502(self, recall, post):
        post.side_effect = requests.ConnectionError("refused")
        with self.assertLogs(app.logger, "INFO") as logs, self.assertRaises(OutboundCallError) as cm:
            self.call()
        self.assertEqual(cm.exception.status, 502)
        self.assertTrue(cm.exception.may_have_started)
        self.assertNotIsInstance(cm.exception, MemoryUnavailable)
        self.assert_no_secrets(logs)

    def test_vapi_rejection_502_body_neither_logged_nor_returned(self, recall, post):
        post.return_value = fake_response(400, {"message": "bad"}, text="private phone, memory or instructions")
        with self.assertLogs(app.logger, "INFO") as logs, self.assertRaises(OutboundCallError) as cm:
            self.call()
        self.assertEqual(cm.exception.status, 502)
        self.assertFalse(cm.exception.may_have_started)
        self.assertNotIn("private phone, memory or instructions", cm.exception.message)
        self.assertNotIn("private phone, memory or instructions", "\n".join(logs.output))
        self.assertIn("Vapi rejected outbound call (400)", "\n".join(logs.output))
        self.assert_no_secrets(logs)

    def test_vapi_ok_invalid_json_returns_none(self, recall, post):
        post.return_value = fake_response(201, invalid_json=True)
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertIsNone(self.call())
        self.assertIn("Vapi create-call response was not valid JSON (ValueError)", "\n".join(logs.output))

    def test_vapi_ok_malformed_response_or_id_returns_none(self, recall, post):
        for body in (None, [], "x", {}, {"id": None}, {"id": 7}, {"id": []}, {"id": ""}, {"id": "   "}):
            with self.subTest(body=body):
                post.return_value = fake_response(201, body)
                with self.assertLogs(app.logger, "INFO"):
                    self.assertIsNone(self.call())

    def test_vapi_ok_call_id_is_trimmed(self, recall, post):
        post.return_value = fake_response(201, {"id": "  call-2\n"})
        with self.assertLogs(app.logger, "INFO"):
            self.assertEqual(self.call(), "call-2")


@patch("albert.vapi.requests.get")
class GetVapiCallTests(VapiTestCase):
    env = {"VAPI_API_KEY": "vapi-key"}

    def test_get_call_returns_json(self, get):
        get.return_value = fake_response(200, {"id": "call-1", "status": "ended"})
        self.assertEqual(get_vapi_call("call-1"), {"id": "call-1", "status": "ended"})
        args, kwargs = get.call_args
        self.assertEqual(args, (f"{config.VAPI_CALL_URL}/call-1",))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer vapi-key"})

    def test_get_call_id_is_url_encoded(self, get):
        get.return_value = fake_response(200, {})
        get_vapi_call("../assistant/x?y=1#z")
        self.assertEqual(get.call_args.args[0], f"{config.VAPI_CALL_URL}/..%2Fassistant%2Fx%3Fy%3D1%23z")

    def test_get_call_without_key_none_without_request(self, get):
        os.environ.pop("VAPI_API_KEY")
        self.assertIsNone(get_vapi_call("call-1"))
        get.assert_not_called()

    def test_get_call_failures_none(self, get):
        cases = {"not ok": dict(return_value=fake_response(404)),
                 "network": dict(side_effect=requests.Timeout("slow")),
                 "bad json": dict(return_value=fake_response(200, invalid_json=True))}
        for name, setup in cases.items():
            with self.subTest(name):
                get.reset_mock(return_value=True, side_effect=True)
                get.configure_mock(**setup)
                with self.assertLogs(app.logger, "WARNING"):
                    self.assertIsNone(get_vapi_call("call-1"))

    def test_get_call_malformed_json_shape_logs_safe_type_only(self, get):
        for body, json_type in ((["private response body"], "array"), (None, "null"),
                                ("private response body", "string"), (7, "number"), (True, "boolean")):
            with self.subTest(json_type=json_type):
                get.return_value = fake_response(200, body)
                with self.assertLogs(app.logger, "WARNING") as logs:
                    self.assertIsNone(get_vapi_call("call-1"))
                text = "\n".join(logs.output)
                self.assertIn(f"response must be a JSON object; got {json_type}", text)
                self.assertNotIn("private response body", text)

    def test_get_call_invalid_json_logs_context_without_body(self, get):
        get.return_value = fake_response(200, invalid_json=True, text="private response body")
        with self.assertLogs(app.logger, "ERROR") as logs:
            self.assertIsNone(get_vapi_call("call-1"))
        text = "\n".join(logs.output)
        self.assertIn("response was not valid JSON (ValueError)", text)
        self.assertNotIn("private response body", text)


FROM = datetime(2026, 10, 18, 7, 59, 2, tzinfo=timezone.utc)
TO = datetime(2026, 10, 18, 10, 5, 0, 123456, tzinfo=timezone(timedelta(hours=2)))  # 08:05:00.123 UTC


@patch("albert.vapi.requests.get")
class ListOutboundCallsTests(VapiTestCase):
    env = {"VAPI_API_KEY": "vapi-key"}

    def test_list_calls_request_params_and_utc_millisecond_times(self, get):
        get.return_value = fake_response(200, [])
        list_outbound_calls("phone-anna", FROM, TO)
        args, kwargs = get.call_args
        self.assertEqual(args, (config.VAPI_CALL_URL,))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer vapi-key"})
        self.assertEqual(kwargs["params"], {"phoneNumberId": "phone-anna", "createdAtGe": "2026-10-18T07:59:02.000Z",
                                            "createdAtLe": "2026-10-18T08:05:00.123Z", "limit": 100})
        self.assertEqual(kwargs["timeout"], 10)

    def test_list_calls_returns_only_dict_items(self, get):
        get.return_value = fake_response(200, [{"id": "vc-1"}, "junk", None, 5, ["x"], {"id": "vc-2"}])
        self.assertEqual(list_outbound_calls("phone-anna", FROM, TO), [{"id": "vc-1"}, {"id": "vc-2"}])

    def test_list_calls_missing_key_raises_without_request(self, get):
        os.environ.pop("VAPI_API_KEY")
        with self.assertRaises(VapiUnavailable):
            list_outbound_calls("phone-anna", FROM, TO)
        get.assert_not_called()

    def test_list_calls_failures_raise_vapi_unavailable(self, get):
        cases = {"network": dict(side_effect=requests.ConnectionError("refused")),
                 "timeout": dict(side_effect=requests.Timeout("slow")),
                 "http error": dict(return_value=fake_response(500)),
                 "bad json": dict(return_value=fake_response(200, invalid_json=True)),
                 "object instead of list": dict(return_value=fake_response(200, {"results": []})),
                 "null": dict(return_value=fake_response(200, None))}
        for name, setup in cases.items():
            with self.subTest(name):
                get.reset_mock(return_value=True, side_effect=True)
                get.configure_mock(**setup)
                with self.assertRaises(VapiUnavailable):
                    list_outbound_calls("phone-anna", FROM, TO)

    def test_list_calls_error_message_has_no_key(self, get):
        get.side_effect = requests.ConnectionError("https://api.vapi.ai/call?token=vapi-key")
        with self.assertRaises(VapiUnavailable) as cm:
            list_outbound_calls("phone-anna", FROM, TO)
        self.assertNotIn("vapi-key", str(cm.exception))

    def test_list_calls_invalid_json_has_contextual_error(self, get):
        get.return_value = fake_response(200, invalid_json=True)
        with self.assertRaisesRegex(VapiUnavailable, "Vapi call list response was not valid JSON"):
            list_outbound_calls("phone-anna", FROM, TO)

    def test_list_calls_requests_json_error_is_reported_as_invalid_json(self, get):
        get.return_value = fake_response(200, [])
        get.return_value.json.side_effect = requests.JSONDecodeError("bad JSON", "private body", 0)
        with self.assertRaisesRegex(VapiUnavailable, "Vapi call list response was not valid JSON") as cm:
            list_outbound_calls("phone-anna", FROM, TO)
        self.assertNotIn("private body", str(cm.exception))


@patch("albert.vapi.requests.get")
class ListVapiTests(unittest.TestCase):
    def test_list_sorted_case_insensitive_with_fallback_names(self, get):
        get.return_value = fake_response(200, [
            {"id": "a1", "name": "zeta"},
            {"id": "p1", "number": "+41440000000"},
            {"id": "a2", "name": "Alpha"},
            {"id": "x1"},
            {"id": "a3", "name": "beta"},
            {"name": "no id"},
            {"id": "", "name": "empty id"},
            {"id": 7, "name": "numeric id"},
            {"id": "   ", "name": "blank id"},
            {"id": "  a4\n", "name": "Gamma"},
            {"id": "x2", "name": []},
            "junk",
        ])
        self.assertEqual(list_vapi("phone-number", "vapi-key"), [
            {"id": "p1", "name": "+41440000000"},
            {"id": "a2", "name": "Alpha"},
            {"id": "a3", "name": "beta"},
            {"id": "a4", "name": "Gamma"},
            {"id": "x1", "name": "Unnamed"},
            {"id": "x2", "name": "Unnamed"},
            {"id": "a1", "name": "zeta"},
        ])
        args, kwargs = get.call_args
        self.assertEqual(args, (f"{config.VAPI_API_URL}/phone-number",))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer vapi-key"})

    def test_list_http_error_raises(self, get):
        get.return_value = fake_response(401)
        with self.assertRaises(requests.HTTPError):
            list_vapi("assistant", "bad-key")

    def test_list_network_error_raises(self, get):
        get.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(requests.RequestException):
            list_vapi("assistant", "vapi-key")

    def test_list_rejects_non_array_json(self, get):
        for body in (None, {}, {"items": []}, "x", 7):
            with self.subTest(body=body):
                get.return_value = fake_response(200, body)
                with self.assertRaisesRegex(ValueError, "Vapi assistant list response must be a JSON array"):
                    list_vapi("assistant", "vapi-key")

    def test_list_invalid_json_has_contextual_error(self, get):
        get.return_value = fake_response(200, invalid_json=True)
        with self.assertRaisesRegex(ValueError, "Vapi assistant list response was not valid JSON"):
            list_vapi("assistant", "vapi-key")


if __name__ == "__main__":
    unittest.main()
