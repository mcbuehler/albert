import unittest
from datetime import datetime

from albert import config
from albert.validation import E164_RE, VAPI_ID_RE, is_valid_vapi_id, normalize_phone, parse_instructions, parse_local_time, vapi_ids

PHONE_ID = "3f2b1c9e-8d4a-4b6e-9f10-2a7c5d8e1b34"
ASSISTANT_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


class E164ReTests(unittest.TestCase):
    def test_e164_re_typical_number_matches(self):
        self.assertTrue(E164_RE.match("+491512345678"))

    def test_e164_re_shortest_seven_digits_matches(self):
        self.assertTrue(E164_RE.match("+1234567"))

    def test_e164_re_longest_fifteen_digits_matches(self):
        self.assertTrue(E164_RE.match("+123456789012345"))

    def test_e164_re_six_digits_rejected(self):
        self.assertIsNone(E164_RE.match("+123456"))

    def test_e164_re_sixteen_digits_rejected(self):
        self.assertIsNone(E164_RE.match("+1234567890123456"))

    def test_e164_re_missing_plus_rejected(self):
        self.assertIsNone(E164_RE.match("491512345678"))

    def test_e164_re_leading_zero_rejected(self):
        self.assertIsNone(E164_RE.match("+0491512345678"))

    def test_e164_re_spaces_rejected(self):
        self.assertIsNone(E164_RE.match("+49 151 2345678"))

    def test_e164_re_empty_string_rejected(self):
        self.assertIsNone(E164_RE.match(""))

    def test_e164_re_trailing_newline_rejected(self):
        self.assertIsNone(E164_RE.match("+491512345678\n"))


class VapiIdReTests(unittest.TestCase):
    def test_vapi_id_re_uppercase_uuid_matches(self):
        self.assertTrue(VAPI_ID_RE.match(PHONE_ID.upper()))

    def test_vapi_id_re_without_dashes_rejected(self):
        self.assertIsNone(VAPI_ID_RE.match(PHONE_ID.replace("-", "")))

    def test_vapi_id_re_trailing_newline_rejected(self):
        self.assertIsNone(VAPI_ID_RE.match(PHONE_ID + "\n"))


class IsValidVapiIdTests(unittest.TestCase):
    def test_is_valid_vapi_id_lowercase_uuid_valid(self):
        self.assertIs(True, is_valid_vapi_id(PHONE_ID))

    def test_is_valid_vapi_id_uppercase_uuid_valid(self):
        self.assertIs(True, is_valid_vapi_id(PHONE_ID.upper()))

    def test_is_valid_vapi_id_surrounding_whitespace_valid(self):
        self.assertIs(True, is_valid_vapi_id(f"  {PHONE_ID}\n"))

    def test_is_valid_vapi_id_non_hex_character_invalid(self):
        self.assertIs(False, is_valid_vapi_id(PHONE_ID[:-1] + "g"))

    def test_is_valid_vapi_id_too_short_invalid(self):
        self.assertIs(False, is_valid_vapi_id(PHONE_ID[:-1]))

    def test_is_valid_vapi_id_extra_characters_invalid(self):
        self.assertIs(False, is_valid_vapi_id(PHONE_ID + "0"))

    def test_is_valid_vapi_id_empty_string_invalid(self):
        self.assertIs(False, is_valid_vapi_id(""))

    def test_is_valid_vapi_id_non_string_invalid(self):
        for value in (None, 123, ["x"], {"id": PHONE_ID}):
            with self.subTest(value=value):
                self.assertIs(False, is_valid_vapi_id(value))


class VapiIdsTests(unittest.TestCase):
    def test_vapi_ids_both_valid_returns_ids_and_no_error(self):
        ids, error = vapi_ids({"vapi_phone_number_id": PHONE_ID, "vapi_assistant_id": ASSISTANT_ID}, required=True)
        self.assertIsNone(error)
        self.assertEqual(ids, {"vapi_phone_number_id": PHONE_ID, "vapi_assistant_id": ASSISTANT_ID})

    def test_vapi_ids_uppercase_and_whitespace_normalized(self):
        data = {"vapi_phone_number_id": f" {PHONE_ID.upper()} ", "vapi_assistant_id": ASSISTANT_ID.upper()}
        ids, error = vapi_ids(data, required=True)
        self.assertIsNone(error)
        self.assertEqual(ids, {"vapi_phone_number_id": PHONE_ID, "vapi_assistant_id": ASSISTANT_ID})

    def test_vapi_ids_required_missing_phone_id_returns_error(self):
        ids, error = vapi_ids({"vapi_assistant_id": ASSISTANT_ID}, required=True)
        self.assertEqual(ids, {})
        self.assertEqual(error, "vapi_phone_number_id is required.")

    def test_vapi_ids_required_blank_assistant_id_returns_error(self):
        ids, error = vapi_ids({"vapi_phone_number_id": PHONE_ID, "vapi_assistant_id": "   "}, required=True)
        self.assertEqual(ids, {})
        self.assertEqual(error, "vapi_assistant_id is required.")

    def test_vapi_ids_required_empty_body_reports_first_field(self):
        ids, error = vapi_ids({}, required=True)
        self.assertEqual(ids, {})
        self.assertEqual(error, "vapi_phone_number_id is required.")

    def test_vapi_ids_optional_missing_returns_none_values(self):
        ids, error = vapi_ids({}, required=False)
        self.assertIsNone(error)
        self.assertEqual(ids, {"vapi_phone_number_id": None, "vapi_assistant_id": None})

    def test_vapi_ids_optional_blank_and_null_treated_as_missing(self):
        ids, error = vapi_ids({"vapi_phone_number_id": "", "vapi_assistant_id": None}, required=False)
        self.assertIsNone(error)
        self.assertEqual(ids, {"vapi_phone_number_id": None, "vapi_assistant_id": None})

    def test_vapi_ids_optional_one_given_keeps_other_none(self):
        ids, error = vapi_ids({"vapi_assistant_id": ASSISTANT_ID}, required=False)
        self.assertIsNone(error)
        self.assertEqual(ids, {"vapi_phone_number_id": None, "vapi_assistant_id": ASSISTANT_ID})

    def test_vapi_ids_invalid_id_returns_format_error(self):
        ids, error = vapi_ids({"vapi_phone_number_id": "not-a-uuid", "vapi_assistant_id": ASSISTANT_ID}, required=True)
        self.assertEqual(ids, {})
        self.assertTrue(error.startswith("vapi_phone_number_id must be a Vapi id"))

    def test_vapi_ids_invalid_id_rejected_even_when_optional(self):
        ids, error = vapi_ids({"vapi_assistant_id": "abc"}, required=False)
        self.assertEqual(ids, {})
        self.assertTrue(error.startswith("vapi_assistant_id must be a Vapi id"))

    def test_vapi_ids_non_string_id_returns_format_error(self):
        ids, error = vapi_ids({"vapi_phone_number_id": 12345}, required=False)
        self.assertEqual(ids, {})
        self.assertTrue(error.startswith("vapi_phone_number_id must be a Vapi id"))

    def test_vapi_ids_ignores_unrelated_fields(self):
        data = {"vapi_phone_number_id": PHONE_ID, "vapi_assistant_id": ASSISTANT_ID, "phone": "+491512345678"}
        ids, _ = vapi_ids(data, required=True)
        self.assertEqual(set(ids), {"vapi_phone_number_id", "vapi_assistant_id"})


class ParseLocalTimeTests(unittest.TestCase):
    def test_parse_local_time_minutes_precision_parsed(self):
        self.assertEqual(parse_local_time("2026-10-17T14:30"), datetime(2026, 10, 17, 14, 30))

    def test_parse_local_time_with_seconds_accepted_and_seconds_dropped(self):
        self.assertEqual(parse_local_time("2026-10-17T14:30:59"), datetime(2026, 10, 17, 14, 30))

    def test_parse_local_time_fractional_seconds_rejected(self):
        self.assertIsNone(parse_local_time("2026-10-17T14:30:59.999"))

    def test_parse_local_time_result_is_naive(self):
        self.assertIsNone(parse_local_time("2026-10-17T14:30").tzinfo)

    def test_parse_local_time_space_separator_rejected(self):
        self.assertIsNone(parse_local_time("2026-10-17 14:30"))

    def test_parse_local_time_date_only_rejected(self):
        self.assertIsNone(parse_local_time("2026-10-17"))

    def test_parse_local_time_compact_form_rejected(self):
        self.assertIsNone(parse_local_time("20261020T1430"))

    def test_parse_local_time_week_date_rejected(self):
        self.assertIsNone(parse_local_time("2026-W42-6T14:30"))

    def test_parse_local_time_surrounding_whitespace_rejected(self):
        self.assertIsNone(parse_local_time(" 2026-10-17T14:30\n"))

    def test_parse_local_time_utc_offset_rejected(self):
        self.assertIsNone(parse_local_time("2026-10-17T14:30+02:00"))

    def test_parse_local_time_z_suffix_rejected(self):
        self.assertIsNone(parse_local_time("2026-10-17T14:30Z"))

    def test_parse_local_time_invalid_strings_return_none(self):
        for value in ("", "tomorrow", "17.10.2026 14:30"):
            with self.subTest(value=value):
                self.assertIsNone(parse_local_time(value))

    def test_parse_local_time_invalid_calendar_value_is_logged(self):
        for value in ("2026-13-01T10:00", "2026-02-30T10:00", "2026-10-17T25:00"):
            with self.subTest(value=value), self.assertLogs("albert.validation", "ERROR") as logs:
                self.assertIsNone(parse_local_time(value))
            self.assertIn("not a valid calendar time (ValueError)", "\n".join(logs.output))
            self.assertNotIn(value, "\n".join(logs.output))

    def test_parse_local_time_non_string_returns_none(self):
        for value in (None, 1760711400, datetime(2026, 10, 17, 14, 30), ["2026-10-17T14:30"]):
            with self.subTest(value=value):
                self.assertIsNone(parse_local_time(value))

    def test_parse_local_time_far_future_accepted(self):
        """Current behaviour: no upper bound here (see HANDOFF: scheduling horizon cap)."""
        self.assertEqual(parse_local_time("9999-12-31T23:59"), datetime(9999, 12, 31, 23, 59))

    def test_parse_local_time_past_accepted(self):
        """Past times are not rejected here; that is the caller's job."""
        self.assertEqual(parse_local_time("2000-01-01T08:00"), datetime(2000, 1, 1, 8, 0))


class ParseInstructionsTests(unittest.TestCase):
    def test_parse_instructions_none_is_no_instructions(self):
        self.assertEqual(parse_instructions(None), (None, None))

    def test_parse_instructions_text_is_trimmed(self):
        self.assertEqual(parse_instructions("  Ask about the invoice.\n"), ("Ask about the invoice.", None))

    def test_parse_instructions_inner_newlines_kept(self):
        self.assertEqual(parse_instructions("Line one.\nLine two."), ("Line one.\nLine two.", None))

    def test_parse_instructions_blank_is_none(self):
        for value in ("", "   ", "\n\t"):
            with self.subTest(value=value):
                self.assertEqual(parse_instructions(value), (None, None))

    def test_parse_instructions_non_string_error(self):
        for value in (5, 1.5, True, ["x"], {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(parse_instructions(value), (None, "instructions must be text."))

    def test_parse_instructions_length_limit_after_trim(self):
        limit = config.MAX_INSTRUCTIONS_LENGTH
        self.assertEqual(limit, 1000)
        self.assertEqual(parse_instructions("a" * limit), ("a" * limit, None))
        self.assertEqual(parse_instructions(f"  {'a' * limit}  "), ("a" * limit, None))
        self.assertEqual(parse_instructions("a" * (limit + 1)), (None, "instructions can be at most 1000 characters."))


class NormalizePhoneTests(unittest.TestCase):
    def test_normalize_phone_examples(self):
        cases = {
            "+41797381155": "+41797381155",
            "+4179 7381155": "+41797381155",
            "0041798526655": "+41798526655",
            "+41 (0)79 738 11 55": "+41797381155",
            "0041-79-738.11/55": "+41797381155",
            "+41 79 738 11 55": "+41797381155",
            "+41797381155\n": "+41797381155",
            " \t+41797381155 ": "+41797381155",
            "+41\u00a079\u00a0738\u00a011\u00a055": "+41797381155",  # non-breaking spaces
            "+1 (555) 123-4567": "+15551234567",
            "0041 (0)79 738 11 55": "+41797381155",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_phone(value), expected)

    def test_normalize_phone_trunk_zero_dropped_only_after_country_code(self):
        cases = {
            "+41 (0)79 738 11 55": "+41797381155",
            "0041 (0)79 738 11 55": "+41797381155",
            "+41(0)797381155": "+41797381155",
            "  +41 (0) 79 738 11 55 ": "+41797381155",
            "+41 79 738 (0)1 55": "+41797380155",  # elsewhere the digit is kept
            "+41 (0)79 738 (0)1 55": "+41797380155",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_phone(value), expected)

    def test_normalize_phone_unusable_is_none(self):
        for value in ("079 738 11 55", "0797381155", "41797381155", "anonymous", "Restricted", "", "   ", "+",
                      "+0041797381155", "00", "+41 79 738 11 55 ext 2", "+41797381155#", None, 41797381155, ["+41797381155"]):
            with self.subTest(value=value):
                self.assertIsNone(normalize_phone(value))

    def test_normalize_phone_length_limits_after_normalization(self):
        self.assertEqual(normalize_phone("+1 234 567"), "+1234567")                       # 7 digits: shortest
        self.assertIsNone(normalize_phone("+1 234 56"))                                    # 6 digits
        self.assertEqual(normalize_phone("00 123 456 789 012 345"), "+123456789012345")   # 15 digits: longest
        self.assertIsNone(normalize_phone("+1234 5678 9012 3456"))                         # 16 digits

    def test_normalize_phone_result_is_idempotent(self):
        for value in ("+4179 7381155", "0041798526655", "+1 (555) 123-4567"):
            with self.subTest(value=value):
                self.assertEqual(normalize_phone(normalize_phone(value)), normalize_phone(value))


if __name__ == "__main__":
    unittest.main()
