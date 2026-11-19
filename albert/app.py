"""Flask app: logging, request logging hooks and the API blueprint."""

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, g, request
from flask.logging import default_handler

from albert import config
from albert.routes import api_bp

app = Flask(__name__)

formatter = logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(filename)s:%(lineno)d %(message)s"
)
default_handler.setFormatter(formatter)  # Vercel / default logging


if config.env("LOCAL_LOGGING") == "1":
    log_dir = Path(__file__).resolve().parent.parent / "logs"  # repo root
    log_dir.mkdir(exist_ok=True)

    logger = app.logger
    logger.removeHandler(default_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=5_000_000, backupCount=3
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if config.env("LOG_STDOUT") == "1":
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        logger.addHandler(stdout_handler)

app.register_blueprint(api_bp)


@app.before_request
def log_request():
    """Log every incoming request (never the token, only the Authorization scheme)."""
    g.request_started = time.perf_counter()
    auth_header = request.headers.get("Authorization", "")
    app.logger.info(
        "Incoming %s %s from %s (Authorization header: %s)",
        request.method,
        request.path,
        request.headers.get("X-Forwarded-For", request.remote_addr),
        auth_header.split(" ", 1)[0] if auth_header else "missing",
    )


@app.after_request
def log_response(response):
    started = g.get("request_started")
    duration = f"{(time.perf_counter() - started) * 1000:.0f} ms" if started else "n/a"
    app.logger.info("Completed %s %s -> %s in %s", request.method, request.path, response.status_code, duration)
    return response
