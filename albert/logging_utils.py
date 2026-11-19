"""Logging helpers shared by code that also runs outside a Flask app context."""

import logging

from flask import current_app, has_app_context


def get_logger(name: str) -> logging.Logger:
    """Use the configured Flask logger in the app, with a module logger for standalone helpers/tests."""
    return current_app.logger if has_app_context() else logging.getLogger(name)
