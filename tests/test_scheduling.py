import unittest
from datetime import date, datetime, time, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from albert import config, db
from albert.scheduling import (
    ScheduleError,
    cancel_callback_tool,
    is_answered,
    reschedule_callback_tool,
    schedule_callback,
    schedule_callback_tool,
    to_utc,
)
from main import app

FUTURE = datetime(2099, 6, 15, 14, 30)  # a Monday, inside calling hours
PAST = datetime(2000, 1, 1, 14, 30)
ROW = {"id": "c0ffee00-0000-4000-8000-000000000001"}
NOW_UTC = datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc)  # 09:00 in Zurich (CEST)


class FrozenDatetime(datetime):
    """datetime with a fixed now(); patched into albert.scheduling so 'now' checks are deterministic."""

    @classmethod
    def now(cls, tz=None):
        return NOW_UTC if tz is None else NOW_UTC.astimezone(tz)


def make_customer(**overrides):
    customer = {"id": 7, "phone": "+41791234567", "timezone": "Europe/Zurich", "opted_out": False}
    customer.update(overrides)
    return customer


def at(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment)


START, END = config.CALLING_START, config.CALLING_END


class ToUtcTests(unittest.TestCase):
    def test_to_utc_zurich_summer_is_minus_two_hours(self):
        self.assertEqual(to_utc(datetime(2026, 7, 1, 14, 30), "Europe/Zurich"),
                         datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc))

    def test_to_utc_zurich_winter_is_minus_one_hour(self):
        self.assertEqual(to_utc(datetime(2026, 1, 15, 14, 30), "Europe/Zurich"),
                         datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc))

    def test_to_utc_zurich_dst_end_day_uses_winter_offset_after_change(self):
        """2026-10-25: clocks go back at 03:00 CEST, so 10:00 that day is CET (+01:00)."""
        self.assertEqual(to_utc(datetime(2026, 10, 25, 10, 0), "Europe/Zurich"),
                         datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc))

    def test_to_utc_zurich_dst_start_day_uses_summer_offset_after_change(self):
        """2027-03-28: clocks go forward at 02:00, so 10:00 that day is CEST (+02:00)."""
        self.assertEqual(to_utc(datetime(2027, 3, 28, 10, 0), "Europe/Zurich"),
                         datetime(2027, 3, 28, 8, 0, tzinfo=timezone.utc))

    def test_to_utc_new_york_dst_start_day(self):
        """2027-03-14: EST -> EDT at 02:00; 09:00 that day is -04:00."""
        self.assertEqual(to_utc(datetime(2027, 3, 14, 9, 0), "America/New_York"),
                         datetime(2027, 3, 14, 13, 0, tzinfo=timezone.utc))

    def test_to_utc_new_york_dst_end_day(self):
        """2026-11-01: EDT -> EST at 02:00; 09:00 that day is -05:00."""
        self.assertEqual(to_utc(datetime(2026, 11, 1, 9, 0), "America/New_York"),
                         datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc))

    def test_to_utc_new_york_day_before_dst_end_still_summer(self):
        self.assertEqual(to_utc(datetime(2026, 10, 31, 9, 0), "America/New_York"),
                         datetime(2026, 10, 31, 13, 0, tzinfo=timezone.utc))

    def test_to_utc_ambiguous_hour_uses_first_occurrence(self):
        """Current behaviour: 02:30 on DST-end day exists twice; fold=0 picks the earlier (summer) one."""
        self.assertEqual(to_utc(datetime(2026, 10, 25, 2, 30), "Europe/Zurich"),
                         datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))

    def test_to_utc_result_is_utc_aware(self):
        self.assertEqual(to_utc(datetime(2026, 7, 1, 12, 0), "UTC").utcoffset().total_seconds(), 0)

    def test_to_utc_unknown_zone_raises(self):
        with self.assertRaises(ZoneInfoNotFoundError):
            to_utc(datetime(2026, 7, 1, 12, 0), "Mars/Olympus")


class IsAnsweredTests(unittest.TestCase):
    def test_is_answered_customer_ended_call_true(self):
        self.assertIs(is_answered("customer-ended-call"), True)

    def test_is_answered_all_configured_reasons_true(self):
        for reason in config.ANSWERED_REASONS:
            with self.subTest(reason=reason):
                self.assertIs(is_answered(reason), True)

    def test_is_answered_not_reached_reasons_false(self):
        for reason in ("customer-did-not-answer", "customer-busy", "voicemail", "", None):
            with self.subTest(reason=reason):
                self.assertIs(is_answered(reason), False)


class ScheduleErrorTests(unittest.TestCase):
    def test_schedule_error_keeps_message_and_status(self):
        error = ScheduleError("nope", 409)
        self.assertEqual((error.message, error.status, str(error)), ("nope", 409, "nope"))


@patch("albert.db.insert_scheduled_call", return_value=ROW)
class ScheduleCallbackTests(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def test_schedule_callback_in_hours_returns_row_and_unchanged_local_time(self, insert):
        row, due_local = schedule_callback(make_customer(), FUTURE)
        self.assertEqual((row, due_local), (ROW, FUTURE))

    def test_schedule_callback_inserts_utc_due_time_and_limit(self, insert):
        schedule_callback(make_customer(), FUTURE, "call-1", "phone-id", "assistant-id")
        insert.assert_called_once_with(7, FUTURE, datetime(2099, 6, 15, 12, 30, tzinfo=timezone.utc),
                                       config.MAX_PENDING_PER_CUSTOMER, "call-1", "phone-id", "assistant-id", None)

    def test_schedule_callback_passes_instructions_last(self, insert):
        schedule_callback(make_customer(), FUTURE, vapi_phone_number_id="phone-id", instructions="Ask about the invoice.")
        self.assertEqual(insert.call_args.args[-1], "Ask about the invoice.")
        self.assertEqual(insert.call_args.args[5], "phone-id")

    def test_schedule_callback_uses_customer_timezone(self, insert):
        schedule_callback(make_customer(timezone="America/New_York"), FUTURE)
        self.assertEqual(insert.call_args.args[2], datetime(2099, 6, 15, 18, 30, tzinfo=timezone.utc))

    def test_schedule_callback_outside_hours_not_moved(self, insert):
        for moment in (time(0, 0), time(8, 59), END, time(23, 59)):
            with self.subTest(moment=moment):
                requested = at(date(2099, 6, 15), moment)
                _, due_local = schedule_callback(make_customer(), requested)
                self.assertEqual(due_local, requested)
                self.assertEqual(insert.call_args.args[1:3], (requested, to_utc(requested, "Europe/Zurich")))

    def test_schedule_callback_opted_out_raises_409_without_insert(self, insert):
        with self.assertRaises(ScheduleError) as cm:
            schedule_callback(make_customer(opted_out=True), FUTURE)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("opted out", cm.exception.message)
        insert.assert_not_called()

    def test_schedule_callback_past_raises_400_without_insert(self, insert):
        with self.assertRaises(ScheduleError) as cm:
            schedule_callback(make_customer(), PAST)
        self.assertEqual(cm.exception.status, 400)
        insert.assert_not_called()

    def test_schedule_callback_pending_limit_raises_409(self, insert):
        insert.side_effect = db.PendingLimitReached()
        with self.assertLogs(app.logger, "WARNING"):
            with self.assertRaises(ScheduleError) as cm:
                schedule_callback(make_customer(), FUTURE)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.message,
                         f"This customer already has {config.MAX_PENDING_PER_CUSTOMER} pending callbacks.")

    def test_schedule_callback_db_error_raises_500(self, insert):
        insert.side_effect = RuntimeError("connection refused")
        with self.assertLogs(app.logger, "ERROR"):
            with self.assertRaises(ScheduleError) as cm:
                schedule_callback(make_customer(), FUTURE)
        self.assertEqual(cm.exception.status, 500)
        self.assertNotIn("connection refused", cm.exception.message)

    @patch("albert.scheduling.datetime", FrozenDatetime)
    def test_schedule_callback_due_exactly_now_raises_400(self, insert):
        with self.assertRaises(ScheduleError) as cm:
            schedule_callback(make_customer(), datetime(2026, 10, 20, 9, 0))
        self.assertEqual(cm.exception.status, 400)
        insert.assert_not_called()

    @patch("albert.scheduling.datetime", FrozenDatetime)
    def test_schedule_callback_one_minute_after_now_accepted(self, insert):
        _, due_local = schedule_callback(make_customer(), datetime(2026, 10, 20, 9, 1))
        self.assertEqual(due_local, datetime(2026, 10, 20, 9, 1))
        insert.assert_called_once()

    @patch("albert.scheduling.datetime", FrozenDatetime)
    def test_schedule_callback_early_morning_in_past_raises_400(self, insert):
        """Now is 09:00 Zurich: 07:00 today has passed (no longer moved to a later time)."""
        with self.assertRaises(ScheduleError) as cm:
            schedule_callback(make_customer(), datetime(2026, 10, 20, 7, 0))
        self.assertEqual(cm.exception.status, 400)

    @patch("albert.scheduling.datetime", FrozenDatetime)
    def test_schedule_callback_early_morning_in_later_zone_kept(self, insert):
        """Now is 03:00 in New York: 07:00 today (11:00 UTC) is still ahead and stays at 07:00."""
        _, due_local = schedule_callback(make_customer(timezone="America/New_York"), datetime(2026, 10, 20, 7, 0))
        self.assertEqual(due_local, datetime(2026, 10, 20, 7, 0))
        self.assertEqual(insert.call_args.args[2], datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc))

    def test_schedule_callback_already_scheduled_returns_row_and_logs_it(self, insert):
        existing = {**ROW, "already_scheduled": True}
        insert.return_value = existing
        with self.assertLogs(app.logger, "INFO") as logs:
            row, due_local = schedule_callback(make_customer(), FUTURE)
        self.assertEqual((row, due_local), (existing, FUTURE))
        text = "\n".join(logs.output)
        self.assertIn("already existed; not scheduling it twice", text)
        self.assertNotIn("Scheduled callback", text)
        self.assertIn("+4179***67", text)
        self.assertNotIn("+41791234567", text)

    def test_schedule_callback_new_row_logs_scheduled(self, insert):
        for row in ({**ROW, "already_scheduled": False}, ROW):  # missing key = new row
            with self.subTest(row=row):
                insert.return_value = row
                with self.assertLogs(app.logger, "INFO") as logs:
                    self.assertEqual(schedule_callback(make_customer(), FUTURE)[0], row)
                text = "\n".join(logs.output)
                self.assertIn("Scheduled callback", text)
                self.assertNotIn("already existed", text)

    def test_schedule_callback_logs_masked_phone_only(self, insert):
        with self.assertLogs(app.logger, "INFO") as logs:
            schedule_callback(make_customer(), FUTURE)
        self.assertNotIn("+41791234567", "\n".join(logs.output))


@patch("albert.scheduling.schedule_callback")
@patch("albert.scheduling.lookup_customer")
class ScheduleCallbackToolTests(unittest.TestCase):
    def test_tool_unknown_caller_not_registered(self, lookup, schedule):
        lookup.return_value = None
        sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30"}, "+41790000000", "call-1")
        self.assertIn("not a registered customer", sentence)
        schedule.assert_not_called()

    def test_tool_invalid_local_time_asks_for_format(self, lookup, schedule):
        lookup.return_value = make_customer()
        for arguments in ({"local_time": "tomorrow 3pm"}, {"local_time": "2099-06-15"}, {}):
            with self.subTest(arguments=arguments):
                sentence = schedule_callback_tool(arguments, "+41791234567", "call-1")
                self.assertIn("local_time must be the customer's local date and time", sentence)
        schedule.assert_not_called()

    def test_tool_success_in_hours_confirms_time(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = (ROW, FUTURE)
        sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30"}, "+41791234567", "call-1")
        self.assertEqual(sentence, "Callback scheduled for Monday, 15 June 2099 at 14:30 (customer's local time).")

    def test_tool_passes_customer_time_and_call_id(self, lookup, schedule):
        customer = make_customer()
        lookup.return_value = customer
        schedule.return_value = (ROW, FUTURE)
        schedule_callback_tool({"local_time": "2099-06-15T14:30:45"}, "+41791234567", "call-1")
        lookup.assert_called_once_with("+41791234567")
        schedule.assert_called_once_with(customer, FUTURE, source_vapi_call_id="call-1", instructions=None)

    def test_tool_instructions_stored_as_customers_and_confirmed(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = (ROW, FUTURE)
        sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30", "instructions": "  Take your tablets. "},
                                          "+41791234567", "call-1")
        self.assertEqual(schedule.call_args.kwargs["instructions"], "Requested by the customer: Take your tablets.")
        self.assertEqual(sentence, "Callback scheduled for Monday, 15 June 2099 at 14:30 (customer's local time) "
                                   "with these instructions: Take your tablets.")

    def test_tool_blank_instructions_are_none(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = (ROW, FUTURE)
        for value in (None, "", "   "):
            with self.subTest(value=value):
                sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30", "instructions": value},
                                                  "+41791234567", "call-1")
                self.assertIsNone(schedule.call_args.kwargs["instructions"])
                self.assertEqual(sentence, "Callback scheduled for Monday, 15 June 2099 at 14:30 (customer's local time).")

    def test_tool_invalid_instructions_not_scheduled(self, lookup, schedule):
        lookup.return_value = make_customer()
        too_long = "x" * (1000 - len("Requested by the customer: ") + 1)
        for value in (42, ["a"], too_long):
            with self.subTest(value=type(value).__name__):
                sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30", "instructions": value},
                                                  "+41791234567", "call-1")
                self.assertTrue(sentence.startswith("Could not schedule the callback: instructions "), sentence)
        schedule.assert_not_called()

    def test_tool_longest_instructions_fit_with_prefix(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = (ROW, FUTURE)
        schedule_callback_tool({"local_time": "2099-06-15T14:30", "instructions": "x" * (1000 - len("Requested by the customer: "))},
                               "+41791234567", "call-1")
        self.assertEqual(len(schedule.call_args.kwargs["instructions"]), 1000)

    def test_tool_already_scheduled_with_instructions_says_not_saved(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = ({**ROW, "already_scheduled": True}, FUTURE)
        sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30", "instructions": "Doctor at 15:00"},
                                          "+41791234567", "call-1")
        self.assertIn("the new ones were not saved", sentence)

    def test_tool_late_evening_scheduled_as_asked(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = (ROW, datetime(2099, 6, 15, 23, 0))
        sentence = schedule_callback_tool({"local_time": "2099-06-15T23:00"}, "+41791234567", "call-1")
        self.assertEqual(sentence, "Callback scheduled for Monday, 15 June 2099 at 23:00 (customer's local time).")

    def test_tool_already_scheduled_says_so(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = ({**ROW, "already_scheduled": True}, FUTURE)
        sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30"}, "+41791234567", "call-1")
        self.assertEqual(sentence, "A callback for Monday, 15 June 2099 at 14:30 (customer's local time) "
                                   "was already scheduled; nothing else to do.")

    def test_tool_already_scheduled_false_is_new(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.return_value = ({**ROW, "already_scheduled": False}, FUTURE)
        sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30"}, "+41791234567", "call-1")
        self.assertEqual(sentence, "Callback scheduled for Monday, 15 June 2099 at 14:30 (customer's local time).")

    def test_tool_past_time_asks_for_future(self, lookup, schedule):
        lookup.return_value = make_customer()
        schedule.side_effect = ScheduleError("local_time must be in the future (customer's time zone).", 400)
        sentence = schedule_callback_tool({"local_time": "2000-01-01T14:30"}, "+41791234567", "call-1")
        self.assertEqual(sentence, "Could not schedule the callback: that time has already passed for the customer. "
                                   "Ask for a time in the future.")

    def test_tool_other_schedule_errors_pass_message_through(self, lookup, schedule):
        lookup.return_value = make_customer()
        for message, status in (("This customer has opted out of calls.", 409),
                                ("This customer already has 3 pending callbacks.", 409),
                                ("Could not schedule the call. Please try again later.", 500)):
            with self.subTest(status=status, message=message):
                schedule.side_effect = ScheduleError(message, status)
                sentence = schedule_callback_tool({"local_time": "2099-06-15T14:30"}, "+41791234567", "call-1")
                self.assertEqual(sentence, f"Could not schedule the callback: {message}")

# --- cancel_callback / reschedule_callback tools ------------------------------------------------------

PHONE = "+41791234567"
FIRST = {"id": "cb-1", "due_at": datetime(2099, 6, 15, 12, 30, tzinfo=timezone.utc), "instructions": None}  # 14:30 Zurich
SECOND = {"id": "cb-2", "due_at": datetime(2099, 6, 16, 7, 0, tzinfo=timezone.utc), "instructions": None}  # 09:00 Zurich
FIRST_TEXT = "Monday, 15 June 2099 at 14:30"
SECOND_TEXT = "Tuesday, 16 June 2099 at 09:00"
BOTH_TEXT = f"{FIRST_TEXT}; {SECOND_TEXT}"


class CallbackToolTestCase(unittest.TestCase):
    def setUp(self):
        patches = {"lookup": patch("albert.scheduling.lookup_customer", return_value=make_customer()),
                   "pending": patch("albert.db.pending_callbacks", return_value=[FIRST, SECOND]),
                   "cancel": patch("albert.db.cancel_scheduled_call", return_value="cancelled"),
                   "reschedule": patch("albert.db.reschedule_callback", return_value={"id": "cb-new"})}
        for name, patcher in patches.items():
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    def run_tool(self, tool, **arguments):
        """Run a tool; returns (sentence, log text). Checks the full phone number is never logged."""
        with self.assertLogs(app.logger, "DEBUG") as logs:
            app.logger.debug("start")
            sentence = tool(arguments, PHONE, "call-1")
        text = "\n".join(logs.output)
        self.assertNotIn(PHONE, text)
        return sentence, text


class CancelCallbackToolTests(CallbackToolTestCase):
    def cancel_tool(self, **arguments):
        return self.run_tool(cancel_callback_tool, **arguments)

    def test_cancel_lists_customer_instructions_to_choose_from(self):
        self.pending.return_value = [{**FIRST, "instructions": "Requested by the customer: Doctor at 15:00"}, SECOND]
        sentence, _ = self.cancel_tool()
        self.assertIn(f"{FIRST_TEXT} (customer's instructions: Doctor at 15:00); {SECOND_TEXT}", sentence)
        self.cancel.assert_not_called()

    def test_cancel_not_a_customer(self):
        self.lookup.return_value = None
        self.assertEqual(self.cancel_tool(callback_time="2099-06-15T14:30")[0],
                         "Nothing was cancelled: this caller is not a registered customer.")
        self.lookup.assert_called_once_with(PHONE)
        self.pending.assert_not_called()

    def test_cancel_by_time_cancels_and_lists_remaining(self):
        self.pending.side_effect = [[FIRST, SECOND], [FIRST]]
        sentence, logs = self.cancel_tool(callback_time="2099-06-16T09:00")
        self.cancel.assert_called_once_with("cb-2")
        self.pending.assert_called_with(7)
        self.assertEqual(sentence, f"The callback on {SECOND_TEXT} (customer's local time) was cancelled. "
                                   f"Pending callbacks now: {FIRST_TEXT}.")
        self.assertIn("+4179***67", logs)

    def test_cancel_only_callback_without_time(self):
        for callback_time in (None, "", "   "):
            with self.subTest(callback_time=callback_time):
                self.cancel.reset_mock()
                self.pending.side_effect = [[FIRST], []]
                sentence, _ = self.cancel_tool(callback_time=callback_time)
                self.cancel.assert_called_once_with("cb-1")
                self.assertEqual(sentence, f"The callback on {FIRST_TEXT} (customer's local time) was cancelled. "
                                           "The customer has no other pending callbacks.")

    def test_cancel_missing_argument_key_with_one_callback(self):
        self.pending.side_effect = [[FIRST], []]
        self.cancel_tool()
        self.cancel.assert_called_once_with("cb-1")

    def test_cancel_non_string_time_is_wrong_format_even_with_one_callback(self):
        self.pending.return_value = [FIRST]
        for callback_time in (5, 14.5, True, ["2099-06-15T14:30"], {"time": "2099-06-15T14:30"}):
            with self.subTest(callback_time=callback_time):
                sentence, _ = self.cancel_tool(callback_time=callback_time)
                self.assertEqual(sentence, "Nothing was cancelled. callback_time must be the callback's local date and "
                                           f"time, e.g. 2026-10-22T14:30. Pending callbacks: {FIRST_TEXT}.")
        self.cancel.assert_not_called()

    def test_cancel_seconds_in_time_still_match(self):
        self.cancel_tool(callback_time="2099-06-15T14:30:59")
        self.cancel.assert_called_once_with("cb-1")

    def test_cancel_matches_in_customer_zone(self):
        self.lookup.return_value = make_customer(timezone="America/New_York")
        sentence, _ = self.cancel_tool(callback_time="2099-06-15T08:30")  # 12:30 UTC in New York (EDT)
        self.cancel.assert_called_once_with("cb-1")
        self.assertTrue(sentence.startswith("The callback on Monday, 15 June 2099 at 08:30 (customer's local time) was cancelled."))

    def test_cancel_several_without_time_asks_which(self):
        sentence, _ = self.cancel_tool(callback_time=None)
        self.assertEqual(sentence, f"Nothing was cancelled. The customer has several pending callbacks: {BOTH_TEXT}. "
                                   "Ask which one they mean and pass its callback_time.")
        self.cancel.assert_not_called()

    def test_cancel_invalid_time_format_lists_callbacks(self):
        for callback_time in ("tomorrow at 9", "2099-06-15", "2099-06-15T14:30Z"):
            with self.subTest(callback_time=callback_time):
                sentence, _ = self.cancel_tool(callback_time=callback_time)
                self.assertEqual(sentence, "Nothing was cancelled. callback_time must be the callback's local date and "
                                           f"time, e.g. 2026-10-22T14:30. Pending callbacks: {BOTH_TEXT}.")
        self.cancel.assert_not_called()

    def test_cancel_no_match_lists_callbacks(self):
        sentence, _ = self.cancel_tool(callback_time="2099-06-15T15:00")
        self.assertEqual(sentence, "Nothing was cancelled. There is no pending callback on Monday, 15 June 2099 at 15:00. "
                                   f"Pending callbacks: {BOTH_TEXT}. Ask which one they mean.")
        self.cancel.assert_not_called()

    def test_cancel_none_pending(self):
        self.pending.return_value = []
        self.assertEqual(self.cancel_tool(callback_time="2099-06-15T14:30")[0],
                         "Nothing was cancelled. The customer has no pending callbacks.")
        self.cancel.assert_not_called()

    def test_cancel_pending_load_error(self):
        self.pending.side_effect = RuntimeError("db down")
        sentence, logs = self.cancel_tool(callback_time="2099-06-15T14:30")
        self.assertEqual(sentence, "Nothing was cancelled. The customer's callbacks could not be loaded right now. "
                                   "Ask them to try again later.")
        self.assertIn("ERROR", logs)
        self.assertIn("+4179***67", logs)
        self.cancel.assert_not_called()

    def test_cancel_not_cancellable_anymore(self):
        for status in ("calling", "completed", None):
            with self.subTest(status=status):
                self.cancel.return_value = status
                self.assertEqual(self.cancel_tool(callback_time="2099-06-15T14:30")[0],
                                 f"Nothing was cancelled: the callback on {FIRST_TEXT} can't be cancelled anymore.")

    def test_cancel_db_error(self):
        self.cancel.side_effect = RuntimeError("db down")
        sentence, logs = self.cancel_tool(callback_time="2099-06-15T14:30")
        self.assertEqual(sentence, "Nothing was cancelled: the callback could not be cancelled right now. "
                                   "Ask the customer to try again later.")
        self.assertIn("Failed to cancel callback cb-1 for +4179***67", logs)

    def test_cancel_remaining_unavailable_ends_after_cancelled(self):
        self.pending.side_effect = [[FIRST], RuntimeError("db down")]
        sentence, _ = self.cancel_tool(callback_time="2099-06-15T14:30")
        self.assertEqual(sentence, f"The callback on {FIRST_TEXT} (customer's local time) was cancelled.")


class RescheduleCallbackToolTests(CallbackToolTestCase):
    def reschedule_tool(self, **arguments):
        return self.run_tool(reschedule_callback_tool, **arguments)

    def test_reschedule_not_a_customer(self):
        self.lookup.return_value = None
        self.assertEqual(self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00")[0],
                         "Nothing was changed: this caller is not a registered customer.")
        self.reschedule.assert_not_called()

    def test_reschedule_opted_out_refused(self):
        self.lookup.return_value = make_customer(opted_out=True)
        self.assertEqual(self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00")[0],
                         "Nothing was changed: this customer has opted out of calls.")
        self.pending.assert_not_called()
        self.reschedule.assert_not_called()

    def test_reschedule_success(self):
        self.pending.side_effect = [[FIRST, SECOND], [SECOND, {**FIRST, "due_at": datetime(2099, 6, 17, 8, 0, tzinfo=timezone.utc)}]]
        sentence, logs = self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00")
        self.reschedule.assert_called_once_with("cb-1", 7, datetime(2099, 6, 17, 10, 0),
                                                datetime(2099, 6, 17, 8, 0, tzinfo=timezone.utc), "call-1")
        self.assertEqual(sentence, f"The callback was moved from {FIRST_TEXT} to Wednesday, 17 June 2099 at 10:00 "
                                   f"(customer's local time). Pending callbacks now: {SECOND_TEXT}; "
                                   "Wednesday, 17 June 2099 at 10:00.")
        self.assertIn("+4179***67", logs)

    def test_reschedule_only_callback_without_time(self):
        self.pending.return_value = [FIRST]
        self.reschedule_tool(new_local_time="2099-06-17T10:00")
        self.assertEqual(self.reschedule.call_args.args[0], "cb-1")

    def test_reschedule_outside_calling_hours_kept(self):
        requested = at(date(2099, 6, 17), END)
        sentence, _ = self.reschedule_tool(callback_time="2099-06-15T14:30",
                                           new_local_time=requested.strftime("%Y-%m-%dT%H:%M"))
        args = self.reschedule.call_args.args
        self.assertEqual(args[2:4], (requested, to_utc(requested, "Europe/Zurich")))
        self.assertIn("to Wednesday, 17 June 2099 at " + f"{END:%H:%M}", sentence)
        self.assertNotIn("calling hours", sentence)

    def test_reschedule_non_string_time_is_wrong_format_even_with_one_callback(self):
        self.pending.return_value = [FIRST]
        for callback_time in (5, 14.5, True, ["2099-06-15T14:30"], {"time": "2099-06-15T14:30"}):
            with self.subTest(callback_time=callback_time):
                sentence, _ = self.reschedule_tool(callback_time=callback_time, new_local_time="2099-06-17T10:00")
                self.assertEqual(sentence, "Nothing was changed. callback_time must be the callback's local date and "
                                           f"time, e.g. 2026-10-22T14:30. Pending callbacks: {FIRST_TEXT}.")
        self.reschedule.assert_not_called()

    def test_reschedule_seconds_dropped(self):
        self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00:45")
        self.assertEqual(self.reschedule.call_args.args[2], datetime(2099, 6, 17, 10, 0))

    def test_reschedule_find_problems_prefixed(self):
        sentence, _ = self.reschedule_tool(callback_time=None, new_local_time="2099-06-17T10:00")
        self.assertEqual(sentence, f"Nothing was changed. The customer has several pending callbacks: {BOTH_TEXT}. "
                                   "Ask which one they mean and pass its callback_time.")
        self.pending.return_value = []
        self.assertEqual(self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00")[0],
                         "Nothing was changed. The customer has no pending callbacks.")
        self.reschedule.assert_not_called()

    def test_reschedule_invalid_new_time(self):
        for new_local_time in (None, "", "next Tuesday", "2099-06-17", "2099-06-17T10:00+02:00", 5):
            with self.subTest(new_local_time=new_local_time):
                sentence, _ = self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time=new_local_time)
                self.assertEqual(sentence, "Nothing was changed. new_local_time must be the customer's local date and "
                                           "time without time zone, e.g. 2026-10-23T10:00. Try again with that format.")
        self.reschedule.assert_not_called()

    def test_reschedule_past_new_time(self):
        sentence, _ = self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2000-01-01T10:00")
        self.assertEqual(sentence, "Nothing was changed: that new time has already passed for the customer. "
                                   "Ask for a time in the future.")
        self.reschedule.assert_not_called()

    def test_reschedule_same_time_nothing_to_do(self):
        sentence, _ = self.reschedule_tool(callback_time="2099-06-16T09:00", new_local_time="2099-06-16T09:00")
        self.assertEqual(sentence, f"Nothing was changed: the callback is already on {SECOND_TEXT}.")
        self.reschedule.assert_not_called()

    def test_reschedule_cannot_be_moved_anymore(self):
        self.reschedule.return_value = None
        self.assertEqual(self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00")[0],
                         f"Nothing was changed: the callback on {FIRST_TEXT} can't be moved anymore.")

    def test_reschedule_db_error(self):
        self.reschedule.side_effect = RuntimeError("db down")
        sentence, logs = self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00")
        self.assertEqual(sentence, "Nothing was changed: the callback could not be moved right now. "
                                   "Ask the customer to try again later.")
        self.assertIn("Failed to reschedule callback cb-1 for +4179***67", logs)

    def test_reschedule_remaining_unavailable_ends_after_moved(self):
        self.pending.side_effect = [[FIRST], RuntimeError("db down")]
        sentence, _ = self.reschedule_tool(callback_time="2099-06-15T14:30", new_local_time="2099-06-17T10:00")
        self.assertEqual(sentence, f"The callback was moved from {FIRST_TEXT} to Wednesday, 17 June 2099 at 10:00 "
                                   "(customer's local time).")


if __name__ == "__main__":
    unittest.main()
