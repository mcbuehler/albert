import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from albert import config
from albert.search import search_brave
from main import app

FAILED = "Failed to perform web search."


def fake_response(status=200, body=None, invalid_json=False):
    response = MagicMock(status_code=status)
    if invalid_json:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body
    return response


def grounding(*items):
    return {"grounding": {"generic": [{"title": title, "snippets": snippets, "url": "https://x"}
                                      for title, snippets in items]}}


@patch("albert.search.requests.get")
class SearchBraveTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"BRAVE_API_KEY": "brave-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    def test_search_success_returns_title_to_joined_snippets(self, get):
        get.return_value = fake_response(200, grounding(("Weather Zurich", ["Sunny.", "18 C."]), ("Other", [])))
        self.assertEqual(search_brave("weather zurich"),
                         ([{"Weather Zurich": "Sunny. 18 C."}, {"Other": ""}], "Success"))

    def test_search_request_params_and_headers(self, get):
        get.return_value = fake_response(200, grounding(("t", ["s"])))
        search_brave("  swiss news  ", max_results=5)
        args, kwargs = get.call_args
        self.assertEqual(args, (config.BRAVE_SEARCH_URL,))
        self.assertEqual(kwargs["headers"], {"Accept": "application/json", "Accept-Encoding": "gzip",
                                             "X-Subscription-Token": "brave-key"})
        self.assertEqual(kwargs["params"], {"q": "swiss news", "count": 5, "text_decorations": False,
                                            "freshness": "pd", "maximum_number_of_tokens": 2048})
        self.assertEqual(kwargs["timeout"], 4)

    def test_search_default_max_results_is_ten(self, get):
        get.return_value = fake_response(200, grounding(("t", ["s"])))
        search_brave("news")
        self.assertEqual(get.call_args.kwargs["params"]["count"], 10)

    def test_search_empty_query_rejected_without_request(self, get):
        for query in ("", "   ", "\n\t"):
            with self.subTest(query=query):
                self.assertEqual(search_brave(query), ([], "Search query cannot be empty."))
        get.assert_not_called()

    def test_search_query_length_limits(self, get):
        get.return_value = fake_response(200, grounding(("t", ["s"])))
        self.assertEqual(search_brave("a" * 601), ([], "Search query cannot exceed 600 characters."))
        self.assertEqual(search_brave(" ".join(["w"] * 76)), ([], "Search query cannot exceed 75 words."))
        self.assertEqual(get.call_count, 0)
        self.assertEqual(search_brave("a" * 600)[1], "Success")
        self.assertEqual(search_brave(" ".join(["w"] * 75))[1], "Success")

    def test_search_max_results_bounds(self, get):
        get.return_value = fake_response(200, grounding(("t", ["s"])))
        for value in (0, 21, -1, True, False, 5.0, "5", None):
            with self.subTest(value=value):
                self.assertEqual(search_brave("news", max_results=value),
                                 ([], "Search result count must be an integer between 1 and 20."))
        get.assert_not_called()
        for value in (1, 20):
            with self.subTest(value=value):
                self.assertEqual(search_brave("news", max_results=value)[1], "Success")

    def test_search_missing_api_key_without_request(self, get):
        os.environ.pop("BRAVE_API_KEY")
        self.assertEqual(search_brave("news"), ([], "Search API error: BRAVE_API_KEY is not configured."))
        get.assert_not_called()

    def test_search_non_200_with_detail_or_code(self, get):
        cases = [({"error": {"detail": "Invalid token", "code": "AUTH"}}, "Search API error: 401 - Invalid token"),
                 ({"error": {"code": "RATE_LIMITED"}}, "Search API error: 401 - RATE_LIMITED"),
                 ({"error": {}}, "Search API error: 401"),
                 ({}, "Search API error: 401")]
        for body, message in cases:
            with self.subTest(body=body):
                get.return_value = fake_response(401, body)
                self.assertEqual(search_brave("news"), ([], message))

    def test_search_non_200_non_json_body(self, get):
        get.return_value = fake_response(502, invalid_json=True)
        with self.assertLogs(app.logger, "ERROR") as logs:
            self.assertEqual(search_brave("news"), ([], "Search API error: 502"))
        self.assertIn("status 502 was not valid JSON (ValueError)", "\n".join(logs.output))

    def test_search_no_results(self, get):
        for body in ({}, {"grounding": {}}, {"grounding": {"generic": []}}):
            with self.subTest(body=body):
                get.return_value = fake_response(200, body)
                self.assertEqual(search_brave("news"), ([], "No relevant search results found."))

    def test_search_malformed_success_shapes_fail_generically(self, get):
        malformed = (
            (None, "response must be an object"),
            ([], "response must be an object"),
            ("x", "response must be an object"),
            ({"grounding": []}, "field 'grounding' must be an object"),
            ({"grounding": {"generic": {}}}, "field 'grounding.generic' must be an array"),
            ({"grounding": {"generic": [None]}}, "result 0 must be an object with string 'title'"),
            ({"grounding": {"generic": [{"snippets": ["s"]}]}},
             "result 0 must be an object with string 'title'"),
            ({"grounding": {"generic": [{"title": "t", "snippets": "s"}]}},
             "result 0 field 'snippets' must be an array of strings"),
            ({"grounding": {"generic": [{"title": "t", "snippets": [1]}]}},
             "result 0 field 'snippets' must be an array of strings"),
        )
        for body, expected in malformed:
            with self.subTest(body=body):
                get.return_value = fake_response(200, body)
                with self.assertLogs(app.logger, "ERROR") as logs:
                    self.assertEqual(search_brave("news"), ([], FAILED))
                self.assertIn(expected, "\n".join(logs.output))

    def test_search_malformed_error_body_still_returns_http_error(self, get):
        for body in (None, [], {"error": []}, {"error": {"detail": []}}):
            with self.subTest(body=body):
                get.return_value = fake_response(429, body)
                self.assertEqual(search_brave("news"), ([], "Search API error: 429"))

    def test_search_network_error_and_bad_json_fail_generically(self, get):
        for name, setup in (("timeout", dict(side_effect=requests.Timeout("slow"))),
                            ("connection", dict(side_effect=requests.ConnectionError("refused"))),
                            ("bad json", dict(return_value=fake_response(200, invalid_json=True)))):
            with self.subTest(name):
                get.reset_mock(return_value=True, side_effect=True)
                get.configure_mock(**setup)
                with self.assertLogs(app.logger, "ERROR"):
                    self.assertEqual(search_brave("news"), ([], FAILED))

    def test_search_logs_never_contain_api_key(self, get):
        get.side_effect = requests.ConnectionError("refused")
        with self.assertLogs(app.logger, "ERROR") as logs:
            search_brave("news")
        self.assertNotIn("brave-key", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
