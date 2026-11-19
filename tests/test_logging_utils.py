import unittest
from unittest.mock import patch

from albert.logging_utils import get_logger
from main import app


class GetLoggerTests(unittest.TestCase):
    def test_uses_named_logger_without_app_context(self):
        with patch("albert.logging_utils.logging.getLogger") as get:
            self.assertIs(get_logger("albert.example"), get.return_value)
        get.assert_called_once_with("albert.example")

    def test_uses_configured_application_logger_in_app_context(self):
        with app.app_context():
            self.assertIs(get_logger("albert.example"), app.logger)


if __name__ == "__main__":
    unittest.main()
