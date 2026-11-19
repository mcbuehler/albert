import unittest
from zoneinfo import ZoneInfo, available_timezones

from albert.timezones import ALIASES, list_timezones


class ListTimezonesTests(unittest.TestCase):
    def setUp(self):
        self.zones = list_timezones()
        self.by_id = {zone["id"]: zone for zone in self.zones}

    def test_list_shape_is_id_country_keywords_strings(self):
        self.assertGreater(len(self.zones), 300)
        for zone in self.zones:
            with self.subTest(zone=zone.get("id")):
                self.assertEqual(set(zone), {"id", "country", "keywords"})
                self.assertTrue(all(isinstance(value, str) for value in zone.values()))

    def test_list_starts_with_utc(self):
        self.assertEqual(self.zones[0], {"id": "UTC", "country": "", "keywords": ALIASES["UTC"]})

    def test_list_ids_unique(self):
        ids = [zone["id"] for zone in self.zones]
        self.assertEqual(len(ids), len(set(ids)))

    def test_list_ids_are_valid_zoneinfo_zones(self):
        available = available_timezones()
        for zone in self.zones:
            with self.subTest(zone=zone["id"]):
                self.assertIn(zone["id"], available)
                ZoneInfo(zone["id"])

    def test_list_countries_are_names_not_codes(self):
        for zone in self.zones[1:]:
            with self.subTest(zone=zone["id"]):
                self.assertGreater(len(zone["country"]), 2)

    def test_list_known_entries(self):
        self.assertEqual(self.by_id["Europe/Zurich"]["country"], "Switzerland")
        self.assertEqual(self.by_id["America/New_York"]["country"], "United States")
        self.assertEqual(self.by_id["Asia/Kolkata"]["country"], "India")
        self.assertIn("Geneva", self.by_id["Europe/Zurich"]["keywords"])

    def test_list_los_angeles_keywords_include_comment_and_aliases(self):
        keywords = self.by_id["America/Los_Angeles"]["keywords"]
        for word in ("Pacific", "California", "San Francisco", "Seattle", "PST"):
            with self.subTest(word=word):
                self.assertIn(word, keywords)

    def test_list_every_alias_zone_is_listed(self):
        for zone_id, aliases in ALIASES.items():
            with self.subTest(zone=zone_id):
                self.assertIn(zone_id, self.by_id)
                self.assertIn(aliases, self.by_id[zone_id]["keywords"])

    def test_list_no_empty_keyword_padding(self):
        for zone in self.zones:
            with self.subTest(zone=zone["id"]):
                self.assertEqual(zone["keywords"], zone["keywords"].strip())
                self.assertNotIn("  ", zone["keywords"])

    def test_list_is_cached(self):
        self.assertIs(list_timezones(), list_timezones())


if __name__ == "__main__":
    unittest.main()
