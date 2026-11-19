"""Authentication: shared bearer tokens for machines (Vapi, Vercel Cron) and Supabase Auth logins for staff."""

import hmac
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from albert import config

_jwks_clients: dict[str, jwt.PyJWKClient] = {}  # per SUPABASE_URL; caches Supabase's public keys per process


class AuthUnavailable(Exception):
    """A login token couldn't be checked because Supabase's signing keys couldn't be loaded."""


def _supabase_auth_url() -> str | None:
    url = config.env("SUPABASE_URL")
    return f"{url.rstrip('/')}/auth/v1" if url else None


def _display_name(claims: dict) -> str | None:
    """Friendly name from the login's Supabase user metadata (`full_name` or `name`), if set."""
    metadata = claims.get("user_metadata")
    for key in ("full_name", "name"):
        value = metadata.get(key) if isinstance(metadata, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()[:config.CALL_NOTE_AUTHOR_MAX_CHARS]
    return None


def verify_user_token(token: str) -> dict:
    """Verify a Supabase Auth access token and return its claims.

    Raises jwt.PyJWTError if the token is invalid, AuthUnavailable if the keys can't be loaded.
    """
    issuer = _supabase_auth_url()
    if not issuer:
        raise AuthUnavailable("SUPABASE_URL is not configured.")
    # Reject junk before any key lookup, so bad tokens can't trigger JWKS fetches (and get 401, not 503).
    header = jwt.get_unverified_header(token)
    algorithm = header.get("alg")
    if algorithm not in ("ES256", "RS256", "HS256"):
        raise jwt.InvalidAlgorithmError(f"Unsupported algorithm {algorithm!r}.")
    if algorithm != "HS256" and not header.get("kid"):
        raise jwt.InvalidTokenError("Token has no key id (kid).")
    if algorithm == "HS256":
        # Legacy Supabase projects sign with a shared secret instead of published keys.
        secret = config.env("SUPABASE_JWT_SECRET")
        if not secret:
            raise jwt.InvalidAlgorithmError("HS256 token but SUPABASE_JWT_SECRET is not configured.")
        key, algorithms = secret, ["HS256"]
    else:
        jwks_url = config.env("SUPABASE_URL").rstrip("/") + config.SUPABASE_JWKS_PATH
        client = _jwks_clients.get(jwks_url)
        if client is None:  # one client per URL, so its key cache survives between requests
            client = _jwks_clients[jwks_url] = jwt.PyJWKClient(jwks_url, cache_keys=True, timeout=5)
        try:
            key = client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as e:
            raise AuthUnavailable(str(e)) from e
        algorithms = ["ES256", "RS256"]
    claims = jwt.decode(
        token,
        key,
        algorithms=algorithms,
        audience=config.SUPABASE_JWT_AUDIENCE,
        issuer=issuer,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )
    # Only real, signed-in users: not anonymous sign-ins and not service/anon API keys.
    if claims.get("role") != "authenticated" or claims.get("is_anonymous"):
        raise jwt.InvalidTokenError("Not a signed-in user.")
    return claims


def require_bearer(*env_vars: str, users: bool = False):
    """Require `Authorization: Bearer <token>`: one of the env var tokens, or (users=True) a Supabase login.

    Fails closed (503) if nothing that could authorize the request is configured.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")  # the request itself is logged in app.py
            expected = [token for token in (config.env(name) for name in env_vars) if token]
            accepts_users = users and _supabase_auth_url() is not None
            if not expected and not accepts_users:
                names = " or ".join(env_vars + (("SUPABASE_URL",) if users else ()))
                current_app.logger.error("%s is not configured; refusing %s", names, request.path)
                return jsonify({"error": f"{names} is not configured."}), 503
            scheme, _, provided = auth_header.partition(" ")
            # compare_digest for every candidate (no short-circuit) to keep timing independent of which one matched.
            matches = [hmac.compare_digest(provided.encode(), token.encode()) for token in expected]
            if scheme.lower() == "bearer" and any(matches):
                g.user = None
                return view(*args, **kwargs)

            reason = None
            if not auth_header:
                reason = "header missing"
            elif scheme.lower() != "bearer":
                reason = "scheme is not Bearer"
            elif not provided:
                reason = "token empty"
            elif provided != provided.strip():
                reason = "token has leading/trailing whitespace"
            elif accepts_users and provided.count(".") == 2:  # looks like a JWT
                try:
                    claims = verify_user_token(provided)
                except AuthUnavailable:
                    current_app.logger.exception("Could not verify login for %s", request.path)
                    return jsonify({"error": "Login could not be verified right now. Please try again."}), 503
                except jwt.ExpiredSignatureError:
                    reason = "login token expired"
                except jwt.PyJWTError as e:
                    reason = f"login token invalid ({type(e).__name__})"
                else:
                    g.user = {"id": claims["sub"], "email": claims.get("email"), "name": _display_name(claims)}
                    current_app.logger.info("Authenticated user %s for %s", claims["sub"], request.path)
                    return view(*args, **kwargs)
            elif not env_vars:
                reason = "not a login token"
            else:
                reason = f"token does not match {' or '.join(env_vars)}" + (" and is not a login token" if accepts_users else "")
            # Say why, without logging the token itself.
            current_app.logger.warning("Unauthorized request to %s: %s", request.path, reason)
            return jsonify({"error": "Unauthorized."}), 401
        return wrapper
    return decorator


# Machines (Vapi server URL, tools): shared secret token only.
require_api_token = require_bearer("REL8_API_AUTH_TOKEN")
# Staff in the browser: Supabase login only.
require_staff = require_bearer(users=True)
# Vercel Cron sends `Authorization: Bearer $CRON_SECRET`; staff trigger it from the page with their login.
require_cron_or_staff = require_bearer("CRON_SECRET", users=True)
# Vapi notify_team tool; staff send the Debug tab's test email through it.
require_api_token_or_staff = require_bearer("REL8_API_AUTH_TOKEN", users=True)
