"""Private Supabase Storage objects via REST (no SDK). Holds archived call recordings."""

import urllib.parse
from typing import BinaryIO

import requests

from albert import config


class StorageUnavailable(Exception):
    """Storage couldn't be changed (missing credentials, network error, rejected request)."""


def _request(method: str, path: str, error: str, headers: dict, action: str = "", **kwargs: object) -> requests.Response:
    """Send an authenticated request below the archive bucket, raising `error` unless Supabase answers 200/201.

    Args:
        method: HTTP method.
        path: Suffix after `/storage/v1/object<action>/<bucket>`, empty or starting with "/".
        error: Generic failure code for StorageUnavailable.
        headers: Extra request headers.
        action: Object API prefix, e.g. "/sign"; empty for plain object requests.
        **kwargs: Body and timeout arguments passed to `requests.request`.

    Raises:
        StorageUnavailable: Credentials are missing or malformed (non-ASCII or spaces in the
            secret key), the request fails or isn't confirmed.
    """
    url, secret = config.env("SUPABASE_URL"), config.env("SUPABASE_SECRET_KEY")
    if not url or not secret:
        raise StorageUnavailable("missing_storage_config")
    if not secret.isascii() or secret != secret.strip() or " " in secret:  # e.g. a pasted "…" or smart quote
        raise StorageUnavailable("invalid_storage_key")
    try:
        response = requests.request(method, f"{url.rstrip('/')}/storage/v1/object{action}/{config.ARCHIVE_AUDIO_BUCKET}{path}",
                                    headers={"Authorization": f"Bearer {secret}", "apikey": secret, **headers},
                                    allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise StorageUnavailable(error) from None
    if response.status_code not in (200, 201):
        raise StorageUnavailable(error)
    return response


def upload_object(key: str, body: BinaryIO, media_type: str, size: int) -> None:
    """Create or replace one private object in the archive bucket.

    Args:
        key: Deterministic object path, so a repeated upload replaces the same object.
        body: Readable file positioned at the start of the content.
        media_type: Content type stored with the object.
        size: Exact byte count of `body`.

    Raises:
        StorageUnavailable: Credentials are missing or Supabase doesn't confirm the write.
    """
    _request("POST", f"/{urllib.parse.quote(key, safe='/')}", "storage_write_failed", data=body, timeout=(5, 60),
             headers={"Content-Type": media_type, "Content-Length": str(size), "x-upsert": "true"})


def delete_object(key: str) -> None:
    """Delete one archive object; Supabase's bulk delete treats an already missing object as success.

    Raises:
        StorageUnavailable: Credentials are missing or Supabase doesn't confirm the deletion.
    """
    _request("DELETE", "", "storage_delete_failed", json={"prefixes": [key]}, timeout=(5, 15), headers={})


def signed_url(key: str, seconds: int) -> str:
    """Create a short-lived download link for one private object, for browser playback.

    Args:
        key: Object path in the archive bucket.
        seconds: Link lifetime; links can't be revoked before they expire.

    Raises:
        StorageUnavailable: Credentials are missing, Supabase refuses or the reply has no link.
    """
    response = _request("POST", f"/{urllib.parse.quote(key, safe='/')}", "storage_sign_failed", headers={},
                        action="/sign", json={"expiresIn": seconds}, timeout=(5, 10))
    try:
        link = response.json().get("signedURL")
    except (ValueError, AttributeError):
        link = None
    if not isinstance(link, str) or not link.startswith("/object/sign/"):
        raise StorageUnavailable("storage_sign_failed")
    return f"{config.env('SUPABASE_URL').rstrip('/')}/storage/v1{link}"
