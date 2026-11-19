"""Entry point for Vercel and gunicorn (`main:app`); the app lives in the albert package."""

from albert.app import app

__all__ = ["app"]
