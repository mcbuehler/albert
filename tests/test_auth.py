import json
import os
import time
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, g, jsonify

from albert import auth
from albert.auth import AuthUnavailable, require_api_token, require_cron_or_staff, require_staff, verify_user_token

SUPABASE_URL = "https://project.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
API_TOKEN = "api-token-1234567890"
CRON_SECRET = "test-cron"
HS_SECRET = "test" * 8
KID = "test-key-1"

KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
JWKS = {"keys": [{**json.loads(jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key())),
                  "kid": KID, "alg": "ES256", "use": "sig"}]}
ENV = {"SUPABASE_URL": SUPABASE_URL, "REL8_API_AUTH_TOKEN": API_TOKEN, "CRON_SECRET": CRON_SECRET}
UNSET = ("SUPABASE_URL", "REL8_API_AUTH_TOKEN", "CRON_SECRET", "SUPABASE_JWT_SECRET")


def claims(**overrides):
    now = int(time.time())
    data = {"sub": "user-1", "email": "staff@example.com", "aud": "authenticated", "iss": ISSUER,
            "role": "authenticated", "is_anonymous": False, "iat": now, "exp": now + 3600}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def make_token(key=KEY, kid=KID, **overrides):
    return jwt.encode(claims(**overrides), key, algorithm="ES256", headers={"kid": kid})


def make_hs_token(secret=HS_SECRET, **overrides):
    return jwt.encode(claims(**overrides), secret, algorithm="HS256")


def clean_env(**values):
    """patch.dict that starts from the auth env vars unset and sets only `values`."""
    patcher = patch.dict(os.environ, values)
    patcher.start()
    for name in UNSET:
        if name not in values:
            os.environ.pop(name, None)
    return patcher


class AuthTestCase(unittest.TestCase):
    env = ENV

    def setUp(self):
        auth._jwks_clients.clear()
        self.addCleanup(auth._jwks_clients.clear)
        self.addCleanup(clean_env(**self.env).stop)
        fetch = patch("jwt.PyJWKClient.fetch_data", return_value=JWKS)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)


class VerifyUserTokenTests(AuthTestCase):
    def test_verify_valid_login_returns_claims(self):
        self.assertEqual(verify_user_token(make_token())["sub"], "user-1")

    def test_verify_expired_raises_expired(self):
        with self.assertRaises(jwt.ExpiredSignatureError):
            verify_user_token(make_token(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600))

    def test_verify_wrong_audience_raises(self):
        with self.assertRaises(jwt.InvalidAudienceError):
            verify_user_token(make_token(aud="other"))

    def test_verify_wrong_issuer_raises(self):
        with self.assertRaises(jwt.InvalidIssuerError):
            verify_user_token(make_token(iss="https://evil.supabase.co/auth/v1"))

    def test_verify_role_not_authenticated_raises(self):
        for role in ("anon", "service_role", None):
            with self.subTest(role=role):
                with self.assertRaises(jwt.InvalidTokenError):
                    verify_user_token(make_token(role=role))

    def test_verify_anonymous_user_raises(self):
        with self.assertRaises(jwt.InvalidTokenError):
            verify_user_token(make_token(is_anonymous=True))

    def test_verify_tampered_signature_raises(self):
        header, payload, signature = make_token().split(".")
        forged = jwt.utils.base64url_encode(json.dumps(claims(sub="admin")).encode()).decode()
        with self.assertRaises(jwt.InvalidSignatureError):
            verify_user_token(f"{header}.{forged}.{signature}")

    def test_verify_signed_by_other_key_raises(self):
        with self.assertRaises(jwt.InvalidSignatureError):
            verify_user_token(make_token(key=OTHER_KEY))

    def test_verify_unknown_kid_raises(self):
        with self.assertRaises(jwt.PyJWTError):
            verify_user_token(make_token(kid="unknown"))

    def test_verify_missing_required_claims_raises(self):
        for claim in ("exp", "sub", "iat"):
            with self.subTest(claim=claim):
                with self.assertRaises(jwt.MissingRequiredClaimError):
                    verify_user_token(make_token(**{claim: None}))

    def test_verify_hs256_without_secret_rejected(self):
        with self.assertRaises(jwt.InvalidAlgorithmError):
            verify_user_token(make_hs_token())

    def test_verify_hs256_with_secret_accepted(self):
        with patch.dict(os.environ, {"SUPABASE_JWT_SECRET": HS_SECRET}):
            self.assertEqual(verify_user_token(make_hs_token())["sub"], "user-1")

    def test_verify_hs256_wrong_secret_raises(self):
        with patch.dict(os.environ, {"SUPABASE_JWT_SECRET": HS_SECRET}):
            with self.assertRaises(jwt.InvalidSignatureError):
                verify_user_token(make_hs_token(secret="another-secret-that-is-at-least-32-bytes"))

    def test_verify_jwks_unreachable_raises_auth_unavailable(self):
        self.fetch.side_effect = jwt.PyJWKClientConnectionError("timed out")
        with self.assertRaises(AuthUnavailable):
            verify_user_token(make_token())

    def test_verify_without_supabase_url_raises_auth_unavailable(self):
        os.environ.pop("SUPABASE_URL")
        with self.assertRaises(AuthUnavailable):
            verify_user_token(make_token())

    def test_verify_jwks_fetched_from_supabase_url(self):
        verify_user_token(make_token())
        self.assertEqual(list(auth._jwks_clients), [f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"])


def make_app():
    app = Flask("auth-test")

    def view():
        return jsonify({"user": g.user})

    for path, decorator in (("/api", require_api_token), ("/staff", require_staff), ("/cron", require_cron_or_staff)):
        app.add_url_rule(path, path, decorator(view))
    return app


class DecoratorTestCase(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.app = make_app()
        self.client = self.app.test_client()

    def get(self, path, header=None):
        """GET `path`; returns (status, json, log text). `header` is the full Authorization value."""
        headers = {} if header is None else {"Authorization": header}
        with self.assertLogs(self.app.logger, "DEBUG") as logs:
            self.app.logger.debug("start")  # assertLogs needs at least one record
            response = self.client.get(path, headers=headers)
        return response.status_code, response.get_json(), "\n".join(logs.output)


class RequireApiTokenTests(DecoratorTestCase):
    def test_api_token_accepted_user_none(self):
        status, body, _ = self.get("/api", f"Bearer {API_TOKEN}")
        self.assertEqual((status, body), (200, {"user": None}))

    def test_api_scheme_case_insensitive(self):
        self.assertEqual(self.get("/api", f"bearer {API_TOKEN}")[0], 200)

    def test_api_valid_login_rejected(self):
        status, _, logs = self.get("/api", f"Bearer {make_token()}")
        self.assertEqual(status, 401)
        self.assertIn("token does not match REL8_API_AUTH_TOKEN", logs)
        self.fetch.assert_not_called()

    def test_api_cron_secret_rejected(self):
        self.assertEqual(self.get("/api", f"Bearer {CRON_SECRET}")[0], 401)

    def test_api_not_configured_503(self):
        os.environ.pop("REL8_API_AUTH_TOKEN")
        status, body, logs = self.get("/api", f"Bearer {API_TOKEN}")
        self.assertEqual(status, 503)
        self.assertIn("REL8_API_AUTH_TOKEN is not configured", body["error"])


class RequireStaffTests(DecoratorTestCase):
    def test_staff_valid_login_sets_user(self):
        status, body, logs = self.get("/staff", f"Bearer {make_token()}")
        self.assertEqual((status, body), (200, {"user": {"id": "user-1", "email": "staff@example.com", "name": None}}))
        self.assertIn("Authenticated user user-1", logs)

    def test_staff_login_without_email_sets_email_none(self):
        status, body, _ = self.get("/staff", f"Bearer {make_token(email=None)}")
        self.assertEqual((status, body), (200, {"user": {"id": "user-1", "email": None, "name": None}}))

    def test_staff_login_name_from_user_metadata(self) -> None:
        """Verify the friendly name comes from full_name, else name; blanks and non-strings are ignored."""
        for metadata, name in (({"full_name": " Luce M ", "name": "x"}, "Luce M"), ({"full_name": " ", "name": "Tom"}, "Tom"),
                               ({"name": 5}, None), ("text", None)):
            with self.subTest(metadata=metadata):
                status, body, _ = self.get("/staff", f"Bearer {make_token(user_metadata=metadata)}")
                self.assertEqual((status, body["user"]["name"]), (200, name))

    def test_staff_api_token_and_cron_secret_rejected(self):
        for token in (API_TOKEN, CRON_SECRET):
            with self.subTest(token=token):
                status, _, logs = self.get("/staff", f"Bearer {token}")
                self.assertEqual(status, 401)
                self.assertIn("not a login token", logs)

    def test_staff_expired_login_401(self):
        token = make_token(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)
        status, _, logs = self.get("/staff", f"Bearer {token}")
        self.assertEqual(status, 401)
        self.assertIn("login token expired", logs)

    def test_staff_invalid_logins_401_with_error_type(self):
        cases = {
            "wrong aud": (make_token(aud="other"), "InvalidAudienceError"),
            "wrong iss": (make_token(iss="https://evil.example/auth/v1"), "InvalidIssuerError"),
            "anon role": (make_token(role="anon"), "InvalidTokenError"),
            "service role": (make_token(role="service_role"), "InvalidTokenError"),
            "anonymous": (make_token(is_anonymous=True), "InvalidTokenError"),
            "other key": (make_token(key=OTHER_KEY), "InvalidSignatureError"),
            "no exp": (make_token(exp=None), "MissingRequiredClaimError"),
            "no sub": (make_token(sub=None), "MissingRequiredClaimError"),
            "hs256 no secret": (make_hs_token(), "InvalidAlgorithmError"),
        }
        for name, (token, error) in cases.items():
            with self.subTest(name):
                status, _, logs = self.get("/staff", f"Bearer {token}")
                self.assertEqual(status, 401)
                self.assertIn(f"login token invalid ({error})", logs)

    def test_staff_hs256_login_accepted_with_secret(self):
        with patch.dict(os.environ, {"SUPABASE_JWT_SECRET": HS_SECRET}):
            status, body, _ = self.get("/staff", f"Bearer {make_hs_token()}")
        self.assertEqual((status, body["user"]["id"]), (200, "user-1"))

    def test_staff_jwks_unreachable_503(self):
        self.fetch.side_effect = jwt.PyJWKClientConnectionError("timed out")
        status, body, logs = self.get("/staff", f"Bearer {make_token()}")
        self.assertEqual(status, 503)
        self.assertIn("Login could not be verified", body["error"])
        self.assertIn("Could not verify login", logs)

    def test_staff_not_configured_503(self):
        os.environ.pop("SUPABASE_URL")
        status, body, _ = self.get("/staff", f"Bearer {make_token()}")
        self.assertEqual(status, 503)
        self.assertIn("SUPABASE_URL is not configured", body["error"])

    def test_staff_alg_none_token_401(self):
        token = jwt.encode(claims(), None, algorithm="none")
        status, _, logs = self.get("/staff", f"Bearer {token}")
        self.assertEqual(status, 401)
        self.assertIn("login token invalid", logs)

    def test_staff_token_without_kid_401(self):
        token = jwt.encode(claims(), KEY, algorithm="ES256")
        self.assertEqual(self.get("/staff", f"Bearer {token}")[0], 401)

    def test_staff_alg_none_token_jwks_unreachable_still_401(self):
        self.fetch.side_effect = jwt.PyJWKClientConnectionError("timed out")
        token = jwt.encode(claims(), None, algorithm="none")
        self.assertEqual(self.get("/staff", f"Bearer {token}")[0], 401)

    def test_staff_token_without_kid_jwks_unreachable_still_401(self):
        self.fetch.side_effect = jwt.PyJWKClientConnectionError("timed out")
        token = jwt.encode(claims(), KEY, algorithm="ES256")
        self.assertEqual(self.get("/staff", f"Bearer {token}")[0], 401)

    def test_staff_junk_tokens_never_fetch_jwks(self):
        cases = {
            "alg none": (jwt.encode(claims(), None, algorithm="none"), "InvalidAlgorithmError"),
            "alg HS512": (jwt.encode(claims(), HS_SECRET + "-padding-for-hs512-key-length", algorithm="HS512"),
                          "InvalidAlgorithmError"),
            "no kid": (jwt.encode(claims(), KEY, algorithm="ES256"), "InvalidTokenError"),
            "empty kid": (make_token(kid=""), "InvalidTokenError"),
        }
        for name, (token, error) in cases.items():
            with self.subTest(name):
                status, _, logs = self.get("/staff", f"Bearer {token}")
                self.assertEqual(status, 401)
                self.assertIn(f"login token invalid ({error})", logs)
        self.fetch.assert_not_called()
        self.assertEqual(auth._jwks_clients, {})


class RequireCronOrStaffTests(DecoratorTestCase):
    def test_cron_secret_accepted_user_none(self):
        status, body, _ = self.get("/cron", f"Bearer {CRON_SECRET}")
        self.assertEqual((status, body), (200, {"user": None}))

    def test_cron_login_accepted_sets_user(self):
        status, body, _ = self.get("/cron", f"Bearer {make_token()}")
        self.assertEqual((status, body["user"]["id"]), (200, "user-1"))

    def test_cron_api_token_rejected(self):
        status, _, logs = self.get("/cron", f"Bearer {API_TOKEN}")
        self.assertEqual(status, 401)
        self.assertIn("token does not match CRON_SECRET and is not a login token", logs)

    def test_cron_only_secret_configured_still_works(self):
        os.environ.pop("SUPABASE_URL")
        self.assertEqual(self.get("/cron", f"Bearer {CRON_SECRET}")[0], 200)

    def test_cron_only_supabase_configured_accepts_login(self):
        os.environ.pop("CRON_SECRET")
        self.assertEqual(self.get("/cron", f"Bearer {make_token()}")[0], 200)

    def test_cron_nothing_configured_503(self):
        os.environ.pop("CRON_SECRET")
        os.environ.pop("SUPABASE_URL")
        status, body, _ = self.get("/cron", f"Bearer {CRON_SECRET}")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "CRON_SECRET or SUPABASE_URL is not configured.")

    def test_cron_bad_headers_401_with_reason(self):
        cases = {
            "header missing": None,
            "scheme is not Bearer": f"Basic {CRON_SECRET}",
            "token empty": "Bearer ",
            "token has leading/trailing whitespace": f"Bearer {CRON_SECRET} ",
        }
        for reason, header in cases.items():
            with self.subTest(reason):
                status, body, logs = self.get("/cron", header)
                self.assertEqual((status, body), (401, {"error": "Unauthorized."}))
                self.assertIn(f"Unauthorized request to /cron: {reason}", logs)

    def test_cron_leading_whitespace_rejected(self):
        status, _, logs = self.get("/cron", f"Bearer  {CRON_SECRET}")
        self.assertEqual(status, 401)
        self.assertIn("token has leading/trailing whitespace", logs)

    def test_cron_no_scheme_rejected(self):
        self.assertEqual(self.get("/cron", CRON_SECRET)[0], 401)


class TokenNotLoggedTests(DecoratorTestCase):
    def test_logs_never_contain_token(self):
        login = make_token()
        expired = make_token(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)
        cases = [("/api", API_TOKEN), ("/api", CRON_SECRET), ("/api", login),
                 ("/staff", login), ("/staff", expired), ("/staff", API_TOKEN), ("/staff", make_token(aud="x")),
                 ("/cron", CRON_SECRET), ("/cron", API_TOKEN), ("/cron", f" {CRON_SECRET}")]
        for path, token in cases:
            with self.subTest(path=path, token=token[:12]):
                _, _, logs = self.get(path, f"Bearer {token}")
                self.assertNotIn(token.strip(), logs)
                self.assertNotIn(token.strip().split(".")[0] if "." in token else token.strip(), logs)

    def test_logs_never_contain_token_when_jwks_unreachable(self):
        self.fetch.side_effect = jwt.PyJWKClientConnectionError("timed out")
        token = make_token()
        with self.assertLogs(self.app.logger, "DEBUG") as logs:
            self.client.get("/staff", headers={"Authorization": f"Bearer {token}"})
        text = "\n".join(logs.output) + "\n".join(str(r.exc_info) for r in logs.records)
        self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
