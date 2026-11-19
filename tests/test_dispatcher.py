import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import requests

from albert import config, dispatcher
from albert.vapi import MemoryUnavailable, OutboundCallError, VapiUnavailable
from main import app

PHONE = "+41791234567"
OTHER_PHONE = "+41447654321"
CUSTOMER = {"id": 7, "first_name": "Anna", "last_name": "Muster", "phone": PHONE, "timezone": "Europe/Zurich"}


def claimed(n=1, phone=PHONE):
    return {"id": f"cb-{n}", "attempt_id": f"att-{n}", "attempt_no": 1, "phone": phone,
            "vapi_assistant_id": "assistant-anna", "vapi_phone_number_id": "phone-anna"}


class DispatcherTestCase(unittest.TestCase):
    def setUp(self):
        self.db = {}
        for name in ("open_attempts", "finish_attempt", "claim_due_call", "get_customer_by_phone",
                     "drop_attempt_and_retry", "set_attempt_call_id", "unrecorded_attempts", "known_call_ids", "record_dispatcher_run"):
            patcher = patch(f"albert.db.{name}")
            self.db[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.db["get_customer_by_phone"].return_value = CUSTOMER
        self.db["finish_attempt"].return_value = "pending"
        self.db["open_attempts"].return_value = []
        self.db["claim_due_call"].return_value = None
        self.db["unrecorded_attempts"].return_value = []
        self.db["known_call_ids"].return_value = set()
        self.db["set_attempt_call_id"].return_value = True
        for name in ("place_outbound_call", "get_vapi_call", "list_outbound_calls"):
            patcher = patch(f"albert.dispatcher.{name}")
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.place_outbound_call.return_value = "vapi-call-1"
        patcher = patch("albert.dispatcher.notify.send_pending_emails", return_value=0)
        self.send_pending_emails = patcher.start()
        self.addCleanup(patcher.stop)
        self.list_outbound_calls.return_value = []
        patcher = patch("albert.dispatcher.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    def logs(self):
        return self.assertLogs(app.logger, "DEBUG")

    def assert_phone_masked(self, logs):
        text = "\n".join(logs.output)
        self.assertNotIn(PHONE, text)
        self.assertIn("+4179***67", text)


class SyncOpenAttemptsTests(DispatcherTestCase):
    def test_sync_ended_call_finishes_with_reason_time_and_answered(self):
        self.db["open_attempts"].return_value = [{"id": "att-1", "vapi_call_id": "vc-1"}]
        self.get_vapi_call.return_value = {"status": "ended", "endedReason": "customer-ended-call",
                                           "endedAt": "2026-10-17T12:30:00.000Z"}
        with self.logs():
            dispatcher.sync_open_attempts()
        self.get_vapi_call.assert_called_once_with("vc-1")
        self.db["finish_attempt"].assert_called_once_with(
            "att-1", "customer-ended-call", datetime(2026, 10, 17, 12, 30, tzinfo=timezone.utc), answered=True)

    def test_sync_ended_with_offset_parsed(self):
        self.db["open_attempts"].return_value = [{"id": "att-1", "vapi_call_id": "vc-1"}]
        self.get_vapi_call.return_value = {"status": "ended", "endedReason": "voicemail",
                                           "endedAt": "2026-10-17T14:30:00+02:00"}
        with self.logs():
            dispatcher.sync_open_attempts()
        ended_at = self.db["finish_attempt"].call_args.args[2]
        self.assertEqual(ended_at, datetime(2026, 10, 17, 12, 30, tzinfo=timezone.utc))
        self.assertEqual(ended_at.utcoffset(), timedelta(hours=2))
        self.assertIs(self.db["finish_attempt"].call_args.kwargs["answered"], False)

    def test_sync_not_answered_reason_answered_false(self):
        self.db["open_attempts"].return_value = [{"id": "att-1", "vapi_call_id": "vc-1"}]
        self.get_vapi_call.return_value = {"status": "ended", "endedReason": "customer-did-not-answer",
                                           "endedAt": "2026-10-17T12:30:00Z"}
        with self.logs():
            dispatcher.sync_open_attempts()
        self.assertIs(self.db["finish_attempt"].call_args.kwargs["answered"], False)

    def test_sync_missing_reason_and_bad_time_use_defaults(self):
        for ended_reason, ended_at in ((None, None), ([], "yesterday"), ({}, 12345), (7, "MISSING"), ("", None)):
            with self.subTest(ended_reason=ended_reason, ended_at=ended_at):
                self.db["finish_attempt"].reset_mock()
                self.db["open_attempts"].return_value = [{"id": "att-1", "vapi_call_id": "vc-1"}]
                call_data = {"status": "ended", "endedReason": ended_reason}
                if ended_at != "MISSING":
                    call_data["endedAt"] = ended_at
                self.get_vapi_call.return_value = call_data
                with self.logs() as logs:
                    dispatcher.sync_open_attempts()
                self.db["finish_attempt"].assert_called_once_with("att-1", "unknown", None, answered=False)
                self.assertIn("ERROR", "\n".join(logs.output))
                self.assertIn("attempt att-1 has a missing or invalid endedAt", "\n".join(logs.output))

    def test_sync_skips_unloadable_and_running_calls(self):
        self.db["open_attempts"].return_value = [{"id": "a1", "vapi_call_id": "vc-1"},
                                                 {"id": "a2", "vapi_call_id": "vc-2"},
                                                 {"id": "a3", "vapi_call_id": "vc-3"},
                                                 {"id": "a4", "vapi_call_id": "vc-4"}]
        self.get_vapi_call.side_effect = [None, {"status": "in-progress"}, {}, {"status": "ended",
                                                                               "endedReason": "customer-busy"}]
        with self.logs():
            dispatcher.sync_open_attempts()
        self.assertEqual(self.get_vapi_call.call_count, 4)
        self.db["finish_attempt"].assert_called_once_with("a4", "customer-busy", None, answered=False)

    def test_sync_no_open_attempts_no_vapi_calls(self):
        dispatcher.sync_open_attempts()
        self.get_vapi_call.assert_not_called()


class DispatchDueCallsTests(DispatcherTestCase):
    def test_dispatch_claims_with_config_until_none(self):
        self.db["claim_due_call"].side_effect = [claimed(1), claimed(2), None]
        self.place_outbound_call.side_effect = ["vc-1", "vc-2"]
        with self.logs():
            results, ok = dispatcher.dispatch_due_calls()
        self.assertTrue(ok)
        self.assertEqual([r["vapi_call_id"] for r in results], ["vc-1", "vc-2"])
        self.assertEqual(self.db["claim_due_call"].call_count, 3)
        self.db["claim_due_call"].assert_called_with(config.STALE_CALL_MINUTES, "09:00:00", "22:00:00")

    def test_dispatch_stops_at_max_per_run(self):
        self.db["claim_due_call"].side_effect = lambda *a: claimed(1)
        with self.logs():
            results, ok = dispatcher.dispatch_due_calls()
        self.assertTrue(ok)
        self.assertEqual(len(results), config.MAX_DISPATCH_PER_RUN)
        self.assertEqual(self.db["claim_due_call"].call_count, config.MAX_DISPATCH_PER_RUN)

    def test_dispatch_nothing_due_returns_empty_ok(self):
        self.assertEqual(dispatcher.dispatch_due_calls(), ([], True))
        self.place_outbound_call.assert_not_called()

    def test_dispatch_recovers_then_syncs_then_claims(self):
        order = MagicMock()
        self.db["unrecorded_attempts"].side_effect = lambda *a: (order("recover"), [])[1]
        self.db["open_attempts"].side_effect = lambda: (order("sync"), [])[1]
        self.db["claim_due_call"].side_effect = lambda *a: (order("claim"), None)[1]
        dispatcher.dispatch_due_calls()
        self.assertEqual(order.call_args_list, [call("recover"), call("sync"), call("claim")])

    def test_dispatch_recover_error_logged_and_run_continues(self):
        self.db["unrecorded_attempts"].side_effect = RuntimeError("db down")
        self.db["open_attempts"].return_value = [{"id": "a1", "vapi_call_id": "vc-0"}]
        self.db["claim_due_call"].side_effect = [claimed(1), None]
        with self.logs() as logs:
            results, ok = dispatcher.dispatch_due_calls()
        self.assertTrue(ok)
        self.assertEqual(len(results), 1)
        self.get_vapi_call.assert_called_once_with("vc-0")
        self.assertIn("failed to recover attempts without call id", "\n".join(logs.output))

    def test_dispatch_sync_error_logged_and_claiming_continues(self):
        self.db["open_attempts"].side_effect = RuntimeError("db down")
        self.db["claim_due_call"].side_effect = [claimed(1), None]
        with self.logs() as logs:
            results, ok = dispatcher.dispatch_due_calls()
        self.assertTrue(ok)
        self.assertEqual(len(results), 1)
        self.assertIn("failed to sync", "\n".join(logs.output))

    def test_dispatch_claim_error_returns_results_so_far_and_false(self):
        self.db["claim_due_call"].side_effect = [claimed(1), RuntimeError("db down")]
        with self.logs() as logs:
            results, ok = dispatcher.dispatch_due_calls()
        self.assertFalse(ok)
        self.assertEqual([r["id"] for r in results], ["cb-1"])
        self.assertIn("failed to claim", "\n".join(logs.output))

    def test_dispatch_failed_call_does_not_stop_run(self):
        self.db["claim_due_call"].side_effect = [claimed(1), claimed(2), None]
        self.place_outbound_call.side_effect = [OutboundCallError("nope", 502), "vc-2"]
        with self.logs():
            results, ok = dispatcher.dispatch_due_calls()
        self.assertTrue(ok)
        self.assertEqual(results[0]["error"], "Could not start the call.")
        self.assertEqual(results[1]["vapi_call_id"], "vc-2")

    def test_dispatch_records_run_with_started_count(self):
        self.db["claim_due_call"].side_effect = [claimed(1), claimed(2), claimed(3), None]
        self.place_outbound_call.side_effect = ["vc-1", OutboundCallError("nope", 502), "vc-3"]
        with self.logs():
            results, ok = dispatcher.dispatch_due_calls()
        self.assertEqual(len(results), 3)
        self.db["record_dispatcher_run"].assert_called_once_with(True, 2)  # only results with a call id

    def test_dispatch_records_empty_run(self):
        self.assertEqual(dispatcher.dispatch_due_calls(), ([], True))
        self.db["record_dispatcher_run"].assert_called_once_with(True, 0)

    def test_dispatch_records_failed_claim_as_not_ok(self):
        self.db["claim_due_call"].side_effect = [claimed(1), RuntimeError("db down")]
        with self.logs():
            results, ok = dispatcher.dispatch_due_calls()
        self.assertFalse(ok)
        self.db["record_dispatcher_run"].assert_called_once_with(False, 1)

    def test_dispatch_record_failure_keeps_result_and_logs_error(self):
        self.db["record_dispatcher_run"].side_effect = RuntimeError("db down")
        self.db["claim_due_call"].side_effect = [claimed(1), None]
        with self.logs() as logs:
            results, ok = dispatcher.dispatch_due_calls()
        self.assertTrue(ok)
        self.assertEqual([r["vapi_call_id"] for r in results], ["vapi-call-1"])
        errors = [r for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual([r.getMessage() for r in errors], ["Failed to record the dispatcher run"])
        self.assertIsNotNone(errors[0].exc_info)

    def test_dispatch_without_ping_url_sends_no_ping(self):
        with patch.dict("os.environ", {}, clear=True), patch("albert.dispatcher.requests.post") as post:
            dispatcher.dispatch_due_calls()
        post.assert_not_called()

    def test_dispatch_pings_healthcheck_with_started_count(self):
        self.db["claim_due_call"].side_effect = [claimed(1), None]
        with patch.dict("os.environ", {"HEALTHCHECKS_PING_URL": "https://hc-ping.com/abc"}), \
                patch("albert.dispatcher.requests.post") as post, self.logs():
            dispatcher.dispatch_due_calls()
        post.assert_called_once_with("https://hc-ping.com/abc", data="started 1 calls",
                                     timeout=config.HEALTHCHECK_TIMEOUT_SECONDS)

    def test_dispatch_failed_claim_pings_fail(self):
        self.db["claim_due_call"].side_effect = RuntimeError("db down")
        with patch.dict("os.environ", {"HEALTHCHECKS_PING_URL": "https://hc-ping.com/abc/"}), \
                patch("albert.dispatcher.requests.post") as post, self.logs():
            self.assertEqual(dispatcher.dispatch_due_calls(), ([], False))
        post.assert_called_once_with("https://hc-ping.com/abc/fail", data="started 0 calls",
                                     timeout=config.HEALTHCHECK_TIMEOUT_SECONDS)

    def test_dispatch_ping_failure_keeps_result_and_logs_warning(self):
        with patch.dict("os.environ", {"HEALTHCHECKS_PING_URL": "https://hc-ping.com/abc"}), \
                patch("albert.dispatcher.requests.post", side_effect=requests.ConnectionError("down")), \
                self.logs() as logs:
            self.assertEqual(dispatcher.dispatch_due_calls(), ([], True))
        self.assertIn("Healthchecks.io ping failed", "\n".join(logs.output))
        self.assertNotIn("hc-ping.com", "\n".join(logs.output))  # the ping URL is a secret

    def test_dispatch_unexpected_error_ends_attempt_and_continues(self):
        self.db["claim_due_call"].side_effect = [claimed(1), claimed(2), None]
        self.place_outbound_call.side_effect = [KeyError("boom"), "vc-2"]
        with self.logs() as logs:
            results, ok = dispatcher.dispatch_due_calls()
        self.assertTrue(ok)
        self.assertEqual(results[0], {"id": "cb-1", "attempt_no": 1, "error": "Could not start the call."})
        self.assertEqual(results[1]["vapi_call_id"], "vc-2")
        self.db["finish_attempt"].assert_called_once_with("att-1", "dispatch-error")
        self.db["set_attempt_call_id"].assert_called_once_with("att-2", "vc-2")
        self.assertIn("failed unexpectedly", "\n".join(logs.output))
        self.assertTrue(any(r.levelname == "ERROR" and r.exc_info for r in logs.records))


STARTED = datetime(2026, 10, 18, 8, 0, 0, tzinfo=timezone.utc)


def unrecorded(n=1, phone=PHONE, pn_id="phone-anna", started=STARTED):
    return {"id": f"att-{n}", "scheduled_call_id": f"cb-{n}", "started_at": started,
            "vapi_phone_number_id": pn_id, "phone": phone}


def vapi_call(call_id, created="2026-10-18T08:00:05.000Z", number=PHONE, call_type="outboundPhoneCall"):
    data = {"id": call_id, "createdAt": created, "customer": {"number": number}}
    if call_type is not None:
        data["type"] = call_type
    return data


class RecoverUnrecordedAttemptsTests(DispatcherTestCase):
    def setUp(self):
        super().setUp()
        self.db["unrecorded_attempts"].return_value = [unrecorded()]

    def test_recover_uses_config_age_and_exact_time_window(self):
        self.recover()
        self.db["unrecorded_attempts"].assert_called_once_with(config.UNRECORDED_ATTEMPT_MINUTES)
        self.list_outbound_calls.assert_called_once_with(
            "phone-anna", STARTED - timedelta(minutes=config.VAPI_CALL_MATCH_BEFORE_MINUTES),
            STARTED + timedelta(minutes=config.VAPI_CALL_MATCH_AFTER_MINUTES))

    def test_recover_found_call_sets_id(self):
        self.list_outbound_calls.return_value = [vapi_call("vc-1")]
        logs = self.recover()
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vc-1")
        self.db["drop_attempt_and_retry"].assert_not_called()
        self.assertIn("Recovered Vapi call vc-1 for attempt att-1", logs)
        self.assertNotIn(PHONE, logs)
        self.assertIn("+4179***67", logs)

    def test_recover_type_missing_counts_as_outbound(self):
        self.list_outbound_calls.return_value = [vapi_call("vc-1", call_type=None)]
        self.recover()
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vc-1")

    def test_recover_closest_created_at_chosen(self):
        self.list_outbound_calls.return_value = [
            vapi_call("vc-far", "2026-10-18T08:04:00.000Z"),
            vapi_call("vc-bad-time", "yesterday"),
            vapi_call("vc-naive-time", "2026-10-18T08:00:01"),
            vapi_call("vc-no-time", None),
            vapi_call("vc-close-before", "2026-10-18T07:59:50.000Z"),
            vapi_call("vc-mid", "2026-10-18T08:00:30.000Z"),
        ]
        self.recover()
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vc-close-before")

    def test_recover_unparsable_times_only_still_sets_an_id(self):
        self.list_outbound_calls.return_value = [vapi_call("vc-1", "garbage")]
        logs = self.recover()
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vc-1")
        self.assertIn("ERROR", logs)
        self.assertIn("attempt att-1 has an invalid createdAt (ValueError)", logs)
        self.assertNotIn("garbage", logs)

    def test_recover_naive_time_does_not_abort_recovery(self):
        self.list_outbound_calls.return_value = [vapi_call("vc-1", "2026-10-18T08:00:05")]
        logs = self.recover()
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vc-1")
        self.assertIn("ERROR", logs)
        self.assertIn("attempt att-1 has a createdAt without a timezone", logs)

    def test_recover_normalizes_call_id_whitespace(self):
        self.list_outbound_calls.return_value = [vapi_call("  vc-1\n")]
        self.recover()
        self.db["known_call_ids"].assert_called_once_with(["vc-1"])
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vc-1")

    def test_recover_known_ids_skipped(self):
        self.list_outbound_calls.return_value = [vapi_call("vc-known", "2026-10-18T08:00:00.000Z"),
                                                 vapi_call("vc-new", "2026-10-18T08:03:00.000Z")]
        self.db["known_call_ids"].return_value = {"vc-known"}
        self.recover()
        self.db["known_call_ids"].assert_called_once_with(["vc-known", "vc-new"])
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vc-new")

    def test_recover_only_known_ids_drops_and_retries(self):
        self.list_outbound_calls.return_value = [vapi_call("vc-known")]
        self.db["known_call_ids"].return_value = {"vc-known"}
        self.recover()
        self.db["set_attempt_call_id"].assert_not_called()
        self.db["drop_attempt_and_retry"].assert_called_once_with("att-1", config.UNDIALLED_RETRY_MINUTES)

    def test_recover_ignores_inbound_other_customer_and_id_less_calls(self):
        self.list_outbound_calls.return_value = [
            vapi_call("vc-inbound", call_type="inboundPhoneCall"),
            vapi_call("vc-web", call_type="webCall"),
            vapi_call("vc-other", number=OTHER_PHONE),
            vapi_call(None),
            {"id": "vc-no-customer", "type": "outboundPhoneCall"},
            {"id": "vc-list-customer", "customer": []},
            {"id": "vc-string-customer", "customer": "x"},
            {"id": "   ", "customer": {"number": PHONE}},
            {"id": 7, "customer": {"number": PHONE}},
            {"id": ["vc-list-id"], "customer": {"number": PHONE}},
        ]
        logs = self.recover()
        self.db["known_call_ids"].assert_called_once_with([])
        self.db["set_attempt_call_id"].assert_not_called()
        self.db["drop_attempt_and_retry"].assert_called_once_with("att-1", config.UNDIALLED_RETRY_MINUTES)
        self.assertNotIn(PHONE, logs)

    def test_recover_nothing_found_drops_and_retries(self):
        self.db["drop_attempt_and_retry"].return_value = "pending"
        logs = self.recover()
        self.db["drop_attempt_and_retry"].assert_called_once_with("att-1", config.UNDIALLED_RETRY_MINUTES)
        self.db["set_attempt_call_id"].assert_not_called()
        self.assertIn("Attempt att-1 had no Vapi call", logs)
        self.assertNotIn(PHONE, logs)
        self.assertIn("+4179***67", logs)

    def test_recover_vapi_unavailable_leaves_attempt_open(self):
        self.list_outbound_calls.side_effect = VapiUnavailable("Could not list Vapi calls: ConnectionError")
        logs = self.recover()
        self.db["set_attempt_call_id"].assert_not_called()
        self.db["drop_attempt_and_retry"].assert_not_called()
        self.db["known_call_ids"].assert_not_called()
        self.assertIn("WARNING", logs)

    def test_recover_vapi_unavailable_for_one_continues_with_next(self):
        self.db["unrecorded_attempts"].return_value = [unrecorded(1), unrecorded(2)]
        self.list_outbound_calls.side_effect = [VapiUnavailable("down"), [vapi_call("vc-2")]]
        self.recover()
        self.db["set_attempt_call_id"].assert_called_once_with("att-2", "vc-2")

    def test_recover_rows_without_phone_or_number_id_skipped(self):
        self.db["unrecorded_attempts"].return_value = [unrecorded(1, phone=None), unrecorded(2, pn_id=None),
                                                       unrecorded(3, phone=""), unrecorded(4, pn_id="")]
        dispatcher.recover_unrecorded_attempts()
        self.list_outbound_calls.assert_not_called()
        self.db["set_attempt_call_id"].assert_not_called()
        self.db["drop_attempt_and_retry"].assert_not_called()

    def test_recover_set_id_false_logs_nothing(self):
        """The attempt was resolved meanwhile (set_attempt_call_id updates nothing): no 'Recovered' log."""
        self.list_outbound_calls.return_value = [vapi_call("vc-1")]
        self.db["set_attempt_call_id"].return_value = False
        logs = self.recover()
        self.assertNotIn("Recovered", logs)
        self.db["drop_attempt_and_retry"].assert_not_called()

    def recover(self):
        """Run recovery; returns the log text (a marker record keeps assertLogs happy when nothing is logged)."""
        with self.assertLogs(app.logger, "DEBUG") as logs:
            app.logger.debug("start")
            dispatcher.recover_unrecorded_attempts()
        return "\n".join(logs.output)


class StartClaimedCallTests(DispatcherTestCase):
    def test_start_success_records_call_id(self):
        with self.logs() as logs:
            result = dispatcher.start_claimed_call(claimed(1))
        self.assertEqual(result, {"id": "cb-1", "attempt_no": 1, "vapi_call_id": "vapi-call-1"})
        self.db["get_customer_by_phone"].assert_called_once_with(PHONE)
        self.place_outbound_call.assert_called_once_with(PHONE, "assistant-anna", "phone-anna", CUSTOMER, None)
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vapi-call-1")
        self.db["finish_attempt"].assert_not_called()
        self.db["drop_attempt_and_retry"].assert_not_called()
        self.assert_phone_masked(logs)

    def test_start_passes_claimed_instructions(self):
        for instructions in ("Ask about the invoice.", None):
            with self.subTest(instructions=instructions):
                self.place_outbound_call.reset_mock()
                with self.logs():
                    dispatcher.start_claimed_call({**claimed(1), "instructions": instructions})
                self.place_outbound_call.assert_called_once_with(PHONE, "assistant-anna", "phone-anna", CUSTOMER,
                                                                 instructions)

    def test_start_id_is_stringified(self):
        row = {**claimed(1), "id": 42}
        with self.logs():
            self.assertEqual(dispatcher.start_claimed_call(row)["id"], "42")

    def test_start_customer_lookup_error_retries_soon_without_dial(self):
        self.db["get_customer_by_phone"].side_effect = RuntimeError("connection refused")
        with self.logs() as logs:
            result = dispatcher.start_claimed_call(claimed(1))
        self.assertIn("error", result)
        self.assertNotIn("vapi_call_id", result)
        self.db["drop_attempt_and_retry"].assert_called_once_with("att-1", config.MEMORY_RETRY_MINUTES)
        self.db["finish_attempt"].assert_not_called()
        self.place_outbound_call.assert_not_called()
        self.assert_phone_masked(logs)

    def test_start_customer_missing_dispatch_error_without_dial(self):
        self.db["get_customer_by_phone"].return_value = None
        with self.logs() as logs:
            result = dispatcher.start_claimed_call(claimed(1))
        self.assertEqual(result["error"], "Could not start the call.")
        self.db["finish_attempt"].assert_called_once_with("att-1", "dispatch-error")
        self.place_outbound_call.assert_not_called()
        self.assert_phone_masked(logs)

    def test_start_memory_unavailable_retries_soon_not_counted(self):
        self.place_outbound_call.side_effect = MemoryUnavailable("Could not load customer memory.", 502)
        with self.logs() as logs:
            result = dispatcher.start_claimed_call(claimed(1))
        self.assertIn("memory unavailable", result["error"])
        self.db["drop_attempt_and_retry"].assert_called_once_with("att-1", config.MEMORY_RETRY_MINUTES)
        self.db["finish_attempt"].assert_not_called()
        self.db["set_attempt_call_id"].assert_not_called()
        self.assert_phone_masked(logs)

    def test_start_definite_outbound_errors_finish_attempt(self):
        for name, effect in (("503", OutboundCallError("VAPI_API_KEY must be configured.", 503)),
                             ("502", OutboundCallError("The calling service could not start the call.", 502))):
            with self.subTest(name):
                self.db["finish_attempt"].reset_mock()
                self.place_outbound_call.reset_mock(side_effect=True, return_value=True)
                self.place_outbound_call.side_effect = effect
                with self.logs() as logs:
                    result = dispatcher.start_claimed_call(claimed(1))
                self.assertEqual(result, {"id": "cb-1", "attempt_no": 1, "error": "Could not start the call."})
                self.db["finish_attempt"].assert_called_once_with("att-1", "dispatch-error")
                self.db["drop_attempt_and_retry"].assert_not_called()
                self.db["set_attempt_call_id"].assert_not_called()
                self.assert_phone_masked(logs)

    def test_start_uncertain_outbound_error_retains_attempt_for_recovery(self):
        self.place_outbound_call.side_effect = OutboundCallError(
            "The calling service response was lost.", 502, may_have_started=True,
        )
        with self.logs() as logs:
            result = dispatcher.start_claimed_call(claimed(1))
        self.assertEqual(result, {"id": "cb-1", "attempt_no": 1, "error": "Could not start the call."})
        self.db["finish_attempt"].assert_not_called()
        self.db["drop_attempt_and_retry"].assert_not_called()
        self.db["set_attempt_call_id"].assert_not_called()
        self.assert_phone_masked(logs)

    def test_start_missing_call_id_retains_attempt_for_recovery(self):
        for name, call_id in (("no id", None), ("empty id", "")):
            with self.subTest(name):
                self.db["finish_attempt"].reset_mock()
                self.place_outbound_call.reset_mock(side_effect=True, return_value=True)
                self.place_outbound_call.return_value = call_id
                with self.logs() as logs:
                    result = dispatcher.start_claimed_call(claimed(1))
                self.assertEqual(result, {"id": "cb-1", "attempt_no": 1, "error": "Could not start the call."})
                self.db["finish_attempt"].assert_not_called()
                self.db["drop_attempt_and_retry"].assert_not_called()
                self.db["set_attempt_call_id"].assert_not_called()
                self.assert_phone_masked(logs)

    def test_start_record_call_id_failure_tries_three_times_then_reports_call(self):
        self.db["set_attempt_call_id"].side_effect = RuntimeError("db down")
        with self.logs() as logs:
            result = dispatcher.start_claimed_call(claimed(1))
        self.assertEqual(result["vapi_call_id"], "vapi-call-1")
        self.assertEqual(self.db["set_attempt_call_id"].call_args_list,
                         [call("att-1", "vapi-call-1")] * dispatcher.SET_CALL_ID_TRIES)
        self.assertEqual(self.sleep.call_args_list,
                         [call(dispatcher.SET_CALL_ID_PAUSE_SECONDS)] * (dispatcher.SET_CALL_ID_TRIES - 1))
        errors = [r for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Failed to record Vapi call vapi-call-1", errors[0].getMessage())
        self.assertIsNotNone(errors[0].exc_info)
        self.db["finish_attempt"].assert_not_called()

    def test_start_record_call_id_succeeds_on_second_try_without_error_log(self):
        self.db["set_attempt_call_id"].side_effect = [RuntimeError("db blip"), True]
        with self.logs() as logs:
            result = dispatcher.start_claimed_call(claimed(1))
        self.assertEqual(result["vapi_call_id"], "vapi-call-1")
        self.assertEqual(self.db["set_attempt_call_id"].call_count, 2)
        self.sleep.assert_called_once_with(dispatcher.SET_CALL_ID_PAUSE_SECONDS)
        self.assertFalse(any(r.levelname == "ERROR" for r in logs.records))

    def test_start_record_call_id_first_try_no_sleep(self):
        with self.logs():
            dispatcher.start_claimed_call(claimed(1))
        self.db["set_attempt_call_id"].assert_called_once_with("att-1", "vapi-call-1")
        self.sleep.assert_not_called()

    def test_start_requeue_failures_logged_not_raised(self):
        cases = {
            "lookup error, drop fails": ("get_customer_by_phone", RuntimeError("db"), "drop_attempt_and_retry"),
            "memory unavailable, drop fails": (None, MemoryUnavailable("m", 502), "drop_attempt_and_retry"),
            "outbound error, finish fails": (None, OutboundCallError("x", 502), "finish_attempt"),
        }
        for name, (db_name, error, failing) in cases.items():
            with self.subTest(name):
                self.db["get_customer_by_phone"].side_effect = error if db_name else None
                self.place_outbound_call.side_effect = None if db_name else error
                self.db[failing].side_effect = RuntimeError("db down")
                with self.logs() as logs:
                    result = dispatcher.start_claimed_call(claimed(1))
                self.assertIn("error", result)
                self.assertIn("Failed to requeue callback cb-1", "\n".join(logs.output))
                self.db[failing].side_effect = None


if __name__ == "__main__":
    unittest.main()
