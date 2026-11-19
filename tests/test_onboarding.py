import os
import unittest
from unittest.mock import patch

import phonenumbers
import psycopg

from albert import config, onboarding
from main import app

PHONE = "+41791234567"
US_PHONE = "+12125551234"
ENV = {"VAPI_ASSISTANT_ID_CH": "assistant-ch", "VAPI_PHONE_NUMBER_ID_CH": "number-ch",
       "VAPI_ASSISTANT_ID_US": "assistant-us", "VAPI_PHONE_NUMBER_ID_US": "number-us", "HINDSIGHT_API_KEY": "hs-key"}
ARGS = {"first_name": " Anna ", "last_name": "Muster", "notes_consent": True}


class RegionAndTimezoneTests(unittest.TestCase):
    def test_region_for(self):
        for number, region in (("+41791234567", "CH"), ("0041 79 123 45 67", "CH"), (US_PHONE, "US"),
                               ("+16045551234", "CA"), ("+18765551234", None), ("+4930123456789", None),
                               ("+447911123456", None), ("079 123 45 67", None), ("anonymous", None), (None, None)):
            with self.subTest(number=number):
                self.assertEqual(onboarding.region_for(number), region)

    def test_timezone_guess(self):
        for number, zone in ((PHONE, "Europe/Zurich"), (US_PHONE, "America/New_York"),
                             ("+14155551234", "America/Los_Angeles"), ("+13125551234", "America/Chicago"),
                             ("+16045551234", "America/Vancouver"), ("+4930123456789", config.DEFAULT_TIMEZONE),
                             (None, config.DEFAULT_TIMEZONE)):
            with self.subTest(number=number):
                self.assertEqual(onboarding.timezone_guess(number), zone)

    def test_ambiguous_number_uses_region_default(self):
        with patch("albert.onboarding.phone_timezones.time_zones_for_number",
                   return_value=("America/New_York", "America/Chicago")):
            self.assertEqual(onboarding.timezone_guess(US_PHONE), "America/New_York")
        with patch("albert.onboarding.phone_timezones.time_zones_for_number", return_value=("Etc/Unknown",)):
            self.assertEqual(onboarding.timezone_guess("+16045551234"), "America/Toronto")

    def test_unexpected_phone_parse_error_is_logged_without_number(self):
        error = phonenumbers.NumberParseException(phonenumbers.NumberParseException.NOT_A_NUMBER, "bad")
        with patch("albert.onboarding.phonenumbers.parse", side_effect=error), \
                self.assertLogs("albert.onboarding", "ERROR") as logs:
            self.assertIsNone(onboarding.region_for(PHONE))
        text = "\n".join(logs.output)
        self.assertIn("could not be parsed by phonenumbers (NumberParseException)", text)
        self.assertNotIn(PHONE, text)


class OnboardCustomerToolTests(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self._start(patch.dict(os.environ, ENV))
        self.db = self._start(patch("albert.onboarding.db"))
        self.db.get_customer_by_phone.return_value = None
        self.db.count_inbound_signups.return_value = 0
        self.db.insert_customer.return_value = {"id": "cust-1"}
        self.memory = self._start(patch("albert.onboarding.memory"))
        self.notify = self._start(patch("albert.onboarding.notify"))

    def _start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def run_tool(self, arguments=None, phone=PHONE):
        with self.assertLogs(app.logger, "DEBUG") as logs:
            app.logger.debug("start")
            result = onboarding.onboard_customer_tool(ARGS if arguments is None else arguments, phone, "call-1")
        self.assertNotIn(PHONE, "\n".join(logs.output))
        self.assertNotIn(US_PHONE, "\n".join(logs.output))
        return result, "\n".join(logs.output)

    def assert_nothing_created(self):
        self.memory.ensure_bank.assert_not_called()
        self.db.insert_customer.assert_not_called()

    def test_signs_up_swiss_caller_with_guessed_zone(self):
        result, _ = self.run_tool()
        self.assertEqual(result, "Anna Muster is signed up (time zone Europe/Zurich). "
                                 "Now agree on the next call and use schedule_callback.")
        self.memory.ensure_bank.assert_called_once_with(PHONE, "hs-key", config.INBOUND_SIGNUP_MEMORY_TIMEOUT_SECONDS)
        self.db.insert_customer.assert_called_once_with("Anna", None, "Muster", PHONE, "Europe/Zurich",
                                                        "number-ch", "assistant-ch", source="inbound",
                                                        needs_review=True)
        self.notify.notify_team.assert_called_once_with(
            "inbound_signup", PHONE, contact_name="Anna Muster",
            summary="Signed up on an inbound call; time zone Europe/Zurich. Please review the customer.",
            vapi_call_id="call-1")

    def test_us_and_canada_use_us_assistant_and_number(self):
        for phone, zone in ((US_PHONE, "America/New_York"), ("+16045551234", "America/Vancouver")):
            with self.subTest(phone=phone):
                self.db.insert_customer.reset_mock()
                self.run_tool(phone=phone)
                self.assertEqual(self.db.insert_customer.call_args.args[3:],
                                 (phone, zone, "number-us", "assistant-us"))

    def test_confirmed_timezone_overrides_guess(self):
        result, _ = self.run_tool({**ARGS, "timezone": " America/Denver "}, phone=US_PHONE)
        self.assertIn("time zone America/Denver", result)
        self.assertEqual(self.db.insert_customer.call_args.args[4], "America/Denver")

    def test_invalid_timezone(self):
        for timezone in ("Mountain Time", "Mars/Olympus", 5):
            with self.subTest(timezone=timezone):
                result, _ = self.run_tool({**ARGS, "timezone": timezone})
                self.assertIn("timezone must be an IANA time zone name", result)
        self.assert_nothing_created()

    def test_unsupported_or_missing_number(self):
        for phone in (None, "+4930123456789", "+18765551234"):
            with self.subTest(phone=phone):
                result, _ = self.run_tool(phone=phone)
                self.assertIn("not from the United States, Canada or Switzerland", result)
        self.assert_nothing_created()

    def test_consent_required(self):
        for consent in (None, False, "true", "yes", 1):
            with self.subTest(consent=consent):
                result, _ = self.run_tool({**ARGS, "notes_consent": consent})
                self.assertIn("first ask whether they agree that you keep notes", result)
        self.assert_nothing_created()

    def test_names_required(self):
        for arguments in ({**ARGS, "first_name": " "}, {**ARGS, "last_name": None}, {**ARGS, "last_name": "x" * 101},
                          {**ARGS, "first_name": 5}):
            with self.subTest(arguments=arguments):
                result, _ = self.run_tool(arguments)
                self.assertEqual(result, "Not signed up: ask for their first and last name, then try again.")
        self.assert_nothing_created()

    def test_already_customer(self):
        self.db.get_customer_by_phone.return_value = {"id": "cust-0"}
        result, _ = self.run_tool()
        self.assertIn("Anna is already a customer", result)
        self.assert_nothing_created()

    def test_unique_violation_means_already_customer(self):
        self.db.insert_customer.side_effect = psycopg.errors.UniqueViolation()
        result, _ = self.run_tool()
        self.assertIn("Anna is already a customer", result)
        self.notify.notify_team.assert_not_called()

    def test_daily_limit(self):
        self.db.count_inbound_signups.return_value = config.MAX_INBOUND_SIGNUPS_PER_DAY
        result, logs = self.run_tool()
        self.assertEqual(result, onboarding.FAILED)
        self.assertIn("sign-ups in 24 h", logs)
        self.db.count_inbound_signups.assert_called_once_with(24)
        self.assert_nothing_created()

    def test_missing_config(self):
        for name in ("VAPI_ASSISTANT_ID_CH", "VAPI_PHONE_NUMBER_ID_CH", "HINDSIGHT_API_KEY"):
            with self.subTest(name), patch.dict(os.environ, {name: ""}):
                result, logs = self.run_tool()
                self.assertEqual(result, onboarding.FAILED)
                self.assertIn("is not configured", logs)
        self.assert_nothing_created()

    def test_db_check_failure(self):
        self.db.count_inbound_signups.side_effect = RuntimeError("db down")
        self.assertEqual(self.run_tool()[0], onboarding.FAILED)
        self.assert_nothing_created()

    def test_memory_failure_saves_nothing(self):
        self.memory.ensure_bank.side_effect = TimeoutError()
        self.assertEqual(self.run_tool()[0], onboarding.FAILED)
        self.db.insert_customer.assert_not_called()
        self.notify.notify_team.assert_not_called()

    def test_insert_failure(self):
        self.db.insert_customer.side_effect = RuntimeError("db down")
        self.assertEqual(self.run_tool()[0], onboarding.FAILED)
        self.notify.notify_team.assert_not_called()


if __name__ == "__main__":
    unittest.main()
