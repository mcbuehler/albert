import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from albert import config
from albert.customers import callback_time_text, callback_variables, customer_variables, lookup_customer
from main import app

PHONE = "+41791234567"
NOW_UTC = datetime(2026, 10, 15, 13, 36, 45, tzinfo=timezone.utc)  # a Thursday


class FrozenDatetime(datetime):
    """datetime with a fixed now(); patched into albert.customers so the local time is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return NOW_UTC if tz is None else NOW_UTC.astimezone(tz)


def make_customer(**overrides):
    customer = {"id": 7, "first_name": "Anna", "last_name": "Muster", "phone": PHONE,
                "timezone": "Europe/Zurich", "opted_out": False}
    customer.update(overrides)
    return customer


@patch("albert.db.get_customer_by_phone")
class LookupCustomerTests(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def test_lookup_customer_found_returns_row(self, get):
        customer = make_customer()
        get.return_value = customer
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertEqual(lookup_customer(PHONE), customer)
        get.assert_called_once_with(PHONE)
        self.assertIn("found", "\n".join(logs.output))
        self.assertNotIn("not found", "\n".join(logs.output))

    def test_lookup_customer_normalizes_number(self, get):
        get.return_value = make_customer()
        for spelling in ("+41 79 123 45 67", "0041791234567", PHONE + "\n"):
            with self.subTest(spelling=spelling):
                get.reset_mock()
                with self.assertLogs(app.logger, "INFO"):
                    lookup_customer(spelling)
                get.assert_called_once_with(PHONE)

    def test_lookup_customer_unnormalizable_uses_raw_value(self, get):
        get.return_value = None
        with self.assertLogs(app.logger, "INFO"):
            self.assertIsNone(lookup_customer("{{customer.number}}"))
        get.assert_called_once_with("{{customer.number}}")

    def test_lookup_customer_not_found_returns_none(self, get):
        get.return_value = None
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertIsNone(lookup_customer(PHONE))
        self.assertIn("not found", "\n".join(logs.output))

    def test_lookup_customer_missing_number_returns_none_without_db(self, get):
        for phone in (None, ""):
            with self.subTest(phone=phone):
                with self.assertLogs(app.logger, "INFO"):
                    self.assertIsNone(lookup_customer(phone))
        get.assert_not_called()

    def test_lookup_customer_db_error_returns_none_and_logs_error(self, get):
        get.side_effect = RuntimeError("connection refused")
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertIsNone(lookup_customer(PHONE))
        self.assertTrue(any(r.levelname == "ERROR" for r in logs.records))
        self.assertIn("not found", "\n".join(logs.output))

    def test_lookup_customer_logs_never_contain_full_phone(self, get):
        for outcome in (make_customer(), None, RuntimeError("db down")):
            with self.subTest(outcome=type(outcome).__name__):
                get.reset_mock()
                if isinstance(outcome, Exception):
                    get.side_effect, get.return_value = outcome, None
                else:
                    get.side_effect, get.return_value = None, outcome
                with self.assertLogs(app.logger, "INFO") as logs:
                    lookup_customer(PHONE)
                text = "\n".join(logs.output) + "\n".join(r.getMessage() for r in logs.records)
                self.assertNotIn(PHONE, text)
                self.assertNotIn(PHONE[1:], text)


@patch("albert.customers.datetime", FrozenDatetime)
class CustomerVariablesTests(unittest.TestCase):
    def test_customer_variables_known_customer_names_and_zurich_time(self):
        self.assertEqual(customer_variables(make_customer()), {
            "customer_first_name": "Anna",
            "customer_last_name": "Muster",
            "customer_local_now": "Thursday, 15 October 2026, 15:36 (Europe/Zurich, UTC+02:00)",
        })

    def test_customer_variables_unknown_customer_uses_defaults(self):
        variables = customer_variables(None)
        self.assertEqual(variables["customer_first_name"], config.UNKNOWN_CUSTOMER_NAME)
        self.assertEqual(variables["customer_last_name"], config.UNKNOWN_CUSTOMER_NAME)
        self.assertIn(f"({config.DEFAULT_TIMEZONE}, UTC", variables["customer_local_now"])

    def test_customer_variables_unknown_customer_default_zone_time(self):
        with patch.object(config, "DEFAULT_TIMEZONE", "Europe/Zurich"):
            self.assertEqual(customer_variables(None)["customer_local_now"],
                             "Thursday, 15 October 2026, 15:36 (Europe/Zurich, UTC+02:00)")

    def test_customer_variables_unknown_customer_given_zone(self):
        self.assertEqual(customer_variables(None, "America/Chicago")["customer_local_now"],
                         "Thursday, 15 October 2026, 08:36 (America/Chicago, UTC-05:00)")

    def test_customer_variables_given_zone_ignored_for_customers(self):
        self.assertIn("(Europe/Zurich,", customer_variables(make_customer(), "America/Chicago")["customer_local_now"])

    def test_customer_variables_negative_offset_new_york(self):
        self.assertEqual(customer_variables(make_customer(timezone="America/New_York"))["customer_local_now"],
                         "Thursday, 15 October 2026, 09:36 (America/New_York, UTC-04:00)")

    def test_customer_variables_half_hour_offset_kolkata(self):
        self.assertEqual(customer_variables(make_customer(timezone="Asia/Kolkata"))["customer_local_now"],
                         "Thursday, 15 October 2026, 19:06 (Asia/Kolkata, UTC+05:30)")

    def test_customer_variables_utc_zone_zero_offset(self):
        self.assertEqual(customer_variables(make_customer(timezone="UTC"))["customer_local_now"],
                         "Thursday, 15 October 2026, 13:36 (UTC, UTC+00:00)")

    def test_customer_variables_local_date_differs_from_utc_date(self):
        """13:36 UTC is already Friday 02:36 in Auckland (+13:00 in October)."""
        self.assertEqual(customer_variables(make_customer(timezone="Pacific/Auckland"))["customer_local_now"],
                         "Friday, 16 October 2026, 02:36 (Pacific/Auckland, UTC+13:00)")

    def test_customer_variables_unknown_zone_raises(self):
        with self.assertRaises(ZoneInfoNotFoundError):
            customer_variables(make_customer(timezone="Mars/Olympus"))

class CallbackTimeTextTests(unittest.TestCase):
    def test_callback_time_text_single_digit_day_has_no_leading_zero(self):
        self.assertEqual(callback_time_text(datetime(2026, 11, 2, 9, 0)), "Monday, 2 November 2026 at 09:00")

    def test_callback_time_text_double_digit_day(self):
        self.assertEqual(callback_time_text(datetime(2026, 10, 22, 14, 30)), "Thursday, 22 October 2026 at 14:30")

    def test_callback_time_text_keeps_leading_zero_in_time(self):
        self.assertEqual(callback_time_text(datetime(2027, 1, 1, 0, 5)), "Friday, 1 January 2027 at 00:05")


@patch("albert.customers.db.pending_callbacks")
class CallbackVariablesTests(unittest.TestCase):
    def setUp(self):
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    @staticmethod
    def rows(*due_ats):
        return [{"id": f"cb-{i}", "due_at": due_at, "instructions": None} for i, due_at in enumerate(due_ats)]

    def test_callback_variables_zurich_across_dst_end(self, pending):
        """2026-10-25 Zurich goes from +02:00 to +01:00: 12:30 UTC -> 14:30, then 08:00 UTC -> 09:00."""
        pending.return_value = self.rows(datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc),
                                         datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(callback_variables(make_customer()), {config.PENDING_CALLBACKS_VARIABLE:
                         "Thursday, 22 October 2026 at 14:30; Monday, 26 October 2026 at 09:00"})
        pending.assert_called_once_with(7)

    def test_callback_variables_new_york_across_dst_end(self, pending):
        """2026-11-01 New York goes from -04:00 to -05:00."""
        pending.return_value = self.rows(datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc),
                                         datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(callback_variables(make_customer(timezone="America/New_York")),
                         {config.PENDING_CALLBACKS_VARIABLE:
                          "Thursday, 22 October 2026 at 08:30; Monday, 2 November 2026 at 09:00"})

    def test_callback_variables_local_date_can_differ_from_utc(self, pending):
        pending.return_value = self.rows(datetime(2026, 10, 22, 23, 30, tzinfo=timezone.utc))
        self.assertEqual(callback_variables(make_customer())[config.PENDING_CALLBACKS_VARIABLE],
                         "Friday, 23 October 2026 at 01:30")

    def test_callback_variables_keeps_db_order(self, pending):
        pending.return_value = self.rows(datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc),
                                         datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(callback_variables(make_customer())[config.PENDING_CALLBACKS_VARIABLE],
                         "Thursday, 22 October 2026 at 10:00; Wednesday, 21 October 2026 at 10:00")

    def test_callback_variables_lists_customer_instructions_not_staff(self, pending):
        rows = self.rows(datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc), datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc))
        rows[0]["instructions"] = "Requested by the customer: Remind them to take their tablets"
        rows[1]["instructions"] = "Staff note: ask about the new glasses"
        pending.return_value = rows
        self.assertEqual(callback_variables(make_customer())[config.PENDING_CALLBACKS_VARIABLE],
                         "Thursday, 22 October 2026 at 10:00 (customer's instructions: Remind them to take their tablets); "
                         "Friday, 23 October 2026 at 10:00")

    def test_callback_variables_none_pending(self, pending):
        pending.return_value = []
        self.assertEqual(callback_variables(make_customer()),
                         {config.PENDING_CALLBACKS_VARIABLE: config.NO_PENDING_CALLBACKS_TEXT})

    def test_callback_variables_non_customer_without_db(self, pending):
        self.assertEqual(callback_variables(None), {config.PENDING_CALLBACKS_VARIABLE: config.NO_PENDING_CALLBACKS_TEXT})
        pending.assert_not_called()

    def test_callback_variables_db_error_unavailable_text_and_masked_log(self, pending):
        pending.side_effect = RuntimeError("connection refused")
        with self.assertLogs(app.logger, "ERROR") as logs:
            result = callback_variables(make_customer())
        self.assertEqual(result, {config.PENDING_CALLBACKS_VARIABLE: config.PENDING_CALLBACKS_UNAVAILABLE_TEXT})
        text = "\n".join(logs.output)
        self.assertIn("Failed to load pending callbacks for +4179***67", text)
        self.assertNotIn(PHONE, text)
        self.assertIsNotNone(logs.records[0].exc_info)


if __name__ == "__main__":
    unittest.main()
