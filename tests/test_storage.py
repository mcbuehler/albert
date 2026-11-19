"""Supabase Storage uploads, without external services."""

import io
import unittest
from unittest.mock import Mock, patch

import requests

from albert import storage

ENV = {"SUPABASE_URL": "https://project.supabase.co/", "SUPABASE_SECRET_KEY": "sb_secret_x"}


class UploadObjectTests(unittest.TestCase):
    def upload(self, response: Mock | Exception, env: dict = ENV) -> Mock:
        """Upload a small body against one mocked Supabase response."""
        effect = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
        with patch("albert.storage.config.env", side_effect=env.get), patch("albert.storage.requests.request", **effect) as post:
            storage.upload_object("calls/a b.wav", io.BytesIO(b"data"), "audio/wav", 4)
        return post

    def test_upserts_private_object(self) -> None:
        """Verify URL, credentials and upsert headers."""
        post = self.upload(Mock(status_code=200))
        self.assertEqual(post.call_args.args, ("POST", "https://project.supabase.co/storage/v1/object/call-recordings/calls/a%20b.wav"))
        headers = post.call_args.kwargs["headers"]
        self.assertEqual((headers["apikey"], headers["Authorization"]), ("sb_secret_x", "Bearer sb_secret_x"))
        self.assertEqual((headers["Content-Type"], headers["Content-Length"], headers["x-upsert"]), ("audio/wav", "4", "true"))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_missing_configuration(self) -> None:
        """Verify nothing is sent without both URL and secret key."""
        for missing in ENV:
            env = {key: value for key, value in ENV.items() if key != missing}
            with self.assertRaisesRegex(storage.StorageUnavailable, "missing_storage_config"):
                self.upload(Mock(status_code=200), env)

    def test_malformed_secret_key_is_reported_without_sending(self) -> None:
        """Verify a key with a pasted ellipsis, smart quote or space fails clearly instead of a UnicodeEncodeError."""
        for secret in ("sb_secret_…", "“sb_secret_x”", " sb_secret_x", "sb_secret x"):
            env = {**ENV, "SUPABASE_SECRET_KEY": secret}
            with self.subTest(secret=secret), patch("albert.storage.config.env", side_effect=env.get), \
                    patch("albert.storage.requests.request") as request:
                with self.assertRaisesRegex(storage.StorageUnavailable, "invalid_storage_key"):
                    storage.upload_object("calls/a.wav", io.BytesIO(b"data"), "audio/wav", 4)
                request.assert_not_called()

    def test_rejected_or_failed_writes(self) -> None:
        """Verify errors, redirects and network failures raise a generic error."""
        for response in (Mock(status_code=400), Mock(status_code=302), requests.ConnectionError("secret detail")):
            with self.assertRaises(storage.StorageUnavailable) as caught:
                self.upload(response)
            self.assertEqual(str(caught.exception), "storage_write_failed")


class DeleteObjectTests(unittest.TestCase):
    def delete(self, response: Mock | Exception) -> Mock:
        """Delete one object against one mocked Supabase response."""
        effect = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
        with patch("albert.storage.config.env", side_effect=ENV.get), patch("albert.storage.requests.request", **effect) as request:
            storage.delete_object("calls/a.wav")
        return request

    def test_bulk_delete_by_key(self) -> None:
        """Verify the bucket-level delete with the key as prefix list."""
        request = self.delete(Mock(status_code=200))
        self.assertEqual(request.call_args.args, ("DELETE", "https://project.supabase.co/storage/v1/object/call-recordings"))
        self.assertEqual(request.call_args.kwargs["json"], {"prefixes": ["calls/a.wav"]})
        self.assertEqual(request.call_args.kwargs["headers"]["apikey"], "sb_secret_x")

    def test_failures_raise_a_generic_error(self) -> None:
        """Verify rejected and failed deletions raise storage_delete_failed."""
        for response in (Mock(status_code=403), requests.Timeout("secret detail")):
            with self.assertRaisesRegex(storage.StorageUnavailable, "storage_delete_failed"):
                self.delete(response)


class SignedUrlTests(unittest.TestCase):
    def sign(self, response: Mock) -> tuple[str, Mock]:
        """Sign one object against one mocked Supabase response."""
        with patch("albert.storage.config.env", side_effect=ENV.get), \
                patch("albert.storage.requests.request", return_value=response) as request:
            return storage.signed_url("calls/a.wav", 300), request

    def test_returns_absolute_short_lived_link(self) -> None:
        """Verify the sign endpoint, lifetime and absolute link."""
        reply = Mock(status_code=200, json=Mock(return_value={"signedURL": "/object/sign/call-recordings/calls/a.wav?token=t"}))
        url, request = self.sign(reply)
        self.assertEqual(request.call_args.args, ("POST", "https://project.supabase.co/storage/v1/object/sign/call-recordings/calls/a.wav"))
        self.assertEqual(request.call_args.kwargs["json"], {"expiresIn": 300})
        self.assertEqual(url, "https://project.supabase.co/storage/v1/object/sign/call-recordings/calls/a.wav?token=t")

    def test_refused_or_malformed_replies(self) -> None:
        """Verify errors, foreign links and invalid JSON raise a generic error."""
        replies = (Mock(status_code=404), Mock(status_code=200, json=Mock(return_value={"signedURL": "https://evil.example/x"})),
                   Mock(status_code=200, json=Mock(side_effect=ValueError("bad json"))), Mock(status_code=200, json=Mock(return_value=[])))
        for reply in replies:
            with self.assertRaisesRegex(storage.StorageUnavailable, "storage_sign_failed"):
                self.sign(reply)
