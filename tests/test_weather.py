import unittest
from unittest.mock import MagicMock, patch

import requests

from albert.weather import get_weather

FORMAT = "format=Condition:+%C,+Temp:+%t,+Wind:+%w"


@patch("albert.weather.requests.get")
class GetWeatherTests(unittest.TestCase):
    def test_weather_returns_stripped_text(self, get):
        get.return_value = MagicMock(ok=True, text="Condition: Sunny, Temp: +18°C, Wind: ↓5km/h\n")
        self.assertEqual(get_weather("Zurich"), "Condition: Sunny, Temp: +18°C, Wind: ↓5km/h")

    def test_weather_url_has_metric_format_and_default_language(self, get):
        get.return_value = MagicMock(ok=True, text="ok")
        get_weather("Zurich")
        get.assert_called_once_with(f"https://wttr.in/Zurich?m&{FORMAT}&lang=en", timeout=5)

    def test_weather_city_is_stripped_and_url_encoded(self, get):
        get.return_value = MagicMock(ok=True, text="ok")
        get_weather("  São Paulo/../x?y#z  ", language="pt")
        self.assertEqual(get.call_args.args[0],
                         f"https://wttr.in/S%C3%A3o%20Paulo/../x%3Fy%23z?m&{FORMAT}&lang=pt")

    def test_weather_language_is_trimmed_and_url_encoded(self, get):
        get.return_value = MagicMock(ok=True, text="ok")
        get_weather("Zurich", language=" de&format=j1/x ")
        self.assertTrue(get.call_args.args[0].endswith("&lang=de%26format%3Dj1%2Fx"))
        self.assertEqual(get.call_args.args[0].count("&"), 2)  # only m&format&lang, no injected parameter

    def test_weather_http_error_returns_sentence_not_error_page(self, get):
        for status in (404, 500, 503):
            with self.subTest(status=status):
                get.return_value = MagicMock(status_code=status, ok=False, text="Unknown location; please try ~1.2,3.4")
                with self.assertLogs("albert.weather", "ERROR") as logs:
                    self.assertEqual(get_weather("Atlantis"), "Unable to retrieve weather for Atlantis right now.")
                self.assertIn(f"Weather service returned HTTP {status}", "\n".join(logs.output))

    def test_weather_network_error_returns_sentence(self, get):
        for error in (requests.Timeout("slow"), requests.ConnectionError("refused")):
            with self.subTest(error=type(error).__name__):
                get.side_effect = error
                with self.assertLogs("albert.weather", "ERROR") as logs:
                    self.assertEqual(get_weather(" Zurich "), "Unable to retrieve weather for  Zurich  right now.")
                self.assertIn(f"Weather request failed ({type(error).__name__})", "\n".join(logs.output))

    def test_weather_unexpected_error_logs_traceback_and_returns_sentence(self, get):
        get.side_effect = RuntimeError("unexpected bug")
        with self.assertLogs("albert.weather", "ERROR") as logs:
            self.assertEqual(get_weather("Zurich"), "Unable to retrieve weather for Zurich right now.")
        text = "\n".join(logs.output)
        self.assertIn("Weather request failed unexpectedly", text)
        self.assertIn("RuntimeError: unexpected bug", text)

    def test_weather_non_text_success_is_logged_and_unavailable(self, get):
        get.return_value = MagicMock(ok=True, text=None)
        with self.assertLogs("albert.weather", "ERROR") as logs:
            self.assertEqual(get_weather("Zurich"), "Unable to retrieve weather for Zurich right now.")
        self.assertIn("Weather service returned a non-text success response", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
