import unittest

from albert.masking import mask_bank_id, mask_phone


class MaskPhoneTests(unittest.TestCase):
    def test_mask_phone_e164_keeps_first_four_and_last_two_digits(self):
        self.assertEqual(mask_phone("+491512345678"), "+4915***78")

    def test_mask_phone_formatted_number_strips_non_digits(self):
        self.assertEqual(mask_phone("+49 (151) 234-56 78"), "+4915***78")

    def test_mask_phone_int_input_is_masked(self):
        self.assertEqual(mask_phone(491512345678), "+4915***78")

    def test_mask_phone_without_plus_adds_plus(self):
        self.assertEqual(mask_phone("41791234567"), "+4179***67")

    def test_mask_phone_seven_digits_fully_hidden(self):
        self.assertEqual(mask_phone("1234567"), "***")

    def test_mask_phone_eight_digits_partially_shown(self):
        self.assertEqual(mask_phone("12345678"), "+1234***78")

    def test_mask_phone_empty_string_fully_hidden(self):
        self.assertEqual(mask_phone(""), "***")

    def test_mask_phone_none_fully_hidden(self):
        self.assertEqual(mask_phone(None), "***")

    def test_mask_phone_no_digits_fully_hidden(self):
        self.assertEqual(mask_phone("anonymous"), "***")

    def test_mask_phone_output_does_not_contain_middle_digits(self):
        masked = mask_phone("+491512345678")
        self.assertNotIn("123456", masked)


class MaskBankIdTests(unittest.TestCase):
    def test_mask_bank_id_user_prefix_masks_number(self):
        self.assertEqual(mask_bank_id("user-+491512345678"), "user-+4915***78")

    def test_mask_bank_id_user_prefix_digits_only(self):
        self.assertEqual(mask_bank_id("user-491512345678"), "user-+4915***78")

    def test_mask_bank_id_short_number_fully_hidden(self):
        self.assertEqual(mask_bank_id("user-123"), "user-***")

    def test_mask_bank_id_prefix_only_fully_hidden(self):
        self.assertEqual(mask_bank_id("user-"), "user-***")

    def test_mask_bank_id_other_prefix_returned_unchanged(self):
        self.assertEqual(mask_bank_id("org-+491512345678"), "org-+491512345678")

    def test_mask_bank_id_prefix_is_case_sensitive(self):
        self.assertEqual(mask_bank_id("User-+491512345678"), "User-+491512345678")

    def test_mask_bank_id_empty_string_returned_unchanged(self):
        self.assertEqual(mask_bank_id(""), "")


if __name__ == "__main__":
    unittest.main()
