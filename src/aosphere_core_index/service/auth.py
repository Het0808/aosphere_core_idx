"""Keycloak JWT authorization — mirrors aosphere-ai-playground's auth.

The API is protected by the same Keycloak realm as the playground: clients obtain
an access token via OIDC and send it as `Authorization: Bearer <token>`. We verify
the token's signature (RS256, via the realm JWKS) + expiry, then check the audience.

Config comes from the SAME env vars the playground backend uses, so one realm/client
covers both services:
  KEYCLOAK_URL       e.g. https://auth.dev1.aoslogin.net
  KEYCLOAK_REALM     e.g. aosphere
  KEYCLOAK_AUDIENCE  the API client id the token must be issued for
  KEYCLOAK_CLIENT_ID the browser OIDC client (defaults to KEYCLOAK_AUDIENCE)
  ACI_AUTH_ENABLED   set to 0/false to force-disable (local / offline dev)
  ACI_ALLOWED_EMAILS when set, access requires BOTH admin AND an email on this list
                     (comma-separated, case-insensitive). Admin alone is not enough;
                     unset = admin-only.

Auth is ON by default once KEYCLOAK_URL+REALM are configured, and a no-op otherwise
(so the offline container and local dev keep working without a realm).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from functools import lru_cache
from typing import Any, Optional

import jwt
from fastapi import HTTPException, Request
from jwt import InvalidTokenError, PyJWKClient


def _kc_url() -> str:
    return (os.getenv("KEYCLOAK_URL", "") or "").rstrip("/")


def _kc_realm() -> str:
    return os.getenv("KEYCLOAK_REALM", "")


def _kc_audience() -> Optional[str]:
    return os.getenv("KEYCLOAK_AUDIENCE") or None


def _kc_client() -> str:
    return os.getenv("KEYCLOAK_CLIENT_ID") or _kc_audience() or ""


def _require_admin() -> bool:
    """Whether the platform is admin-only (default yes)."""
    return os.getenv("ACI_REQUIRE_ADMIN", "1").strip().lower() not in ("0", "false", "no")


def _admin_role_ids() -> set[int]:
    raw = os.getenv("ACI_ADMIN_ROLE_IDS", "1")
    return {int(r.strip()) for r in raw.split(",") if r.strip().lstrip("-").isdigit()}


def _admin_role_names() -> set[str]:
    raw = os.getenv("ACI_ADMIN_ROLES", "admin")
    return {r.strip().lower() for r in raw.split(",") if r.strip()}


def auth_enabled() -> bool:
    """On once the realm is configured; ACI_AUTH_ENABLED=0 force-disables."""
    if os.getenv("ACI_AUTH_ENABLED", "").strip().lower() in ("0", "false", "no"):
        return False
    return bool(_kc_url() and _kc_realm())


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(f"{_kc_url()}/realms/{_kc_realm()}/protocol/openid-connect/certs")


def verify_token(token: str) -> dict[str, Any]:
    """Verify a Keycloak access token's signature + expiry, then check audience."""
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        decoded = jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            options={"verify_exp": True, "verify_aud": False},
        )
    except InvalidTokenError as e:
        raise ValueError(f"Invalid token: {e}")
    # Keycloak populates `aud` inconsistently; check it manually with an azp fallback.
    audience = _kc_audience()
    if audience:
        tok_aud = decoded.get("aud")
        valid = audience in tok_aud if isinstance(tok_aud, (list, tuple)) else tok_aud == audience
        if not valid and decoded.get("azp") == audience:
            valid = True
        if not valid:
            raise ValueError(f"Invalid audience: expected {audience!r}")
    return decoded


def _extract_token(request: Request) -> Optional[str]:
    """Bearer token from the Authorization header. The raw access token is
    deliberately NOT accepted as a query parameter — URLs end up in proxy/access
    logs and browser history. SSE clients use a short-lived stream token instead
    (see issue_stream_token / the `stream_token` query param)."""
    header = request.headers.get("authorization")
    if header:
        parts = header.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
    return None


# ---- Stream tokens ---------------------------------------------------------
# EventSource can't set headers, so /api/agent/stream authenticates with a
# short-lived token minted by POST /api/agent/stream-token (which itself requires
# the real bearer token in a header). Leaking one in a log exposes a ~TTL window,
# not the user's JWT.
#
# STATELESS by design: the token is an HMAC-signed {claims, exp} blob, so ANY
# replica can validate it with the shared secret. It used to be a random opaque
# token held in an in-memory dict (single-process scope), which 401'd in multi-pod
# deploys — minted on pod A, redeemed on pod B ("Invalid or expired stream token").
# Uses wall-clock exp (not monotonic) so it's comparable across processes. Set
# ACI_STREAM_SECRET to the SAME value on every pod; without it a per-process key is
# used (correct for a single instance only).
_STREAM_TOKEN_TTL = float(os.getenv("ACI_STREAM_TOKEN_TTL", "60"))
_log = logging.getLogger(__name__)
_fallback_secret: Optional[bytes] = None


def _stream_secret() -> bytes:
    s = os.getenv("ACI_STREAM_SECRET")
    if s:
        return s.encode()
    global _fallback_secret
    if _fallback_secret is None:
        _fallback_secret = secrets.token_bytes(32)
        if auth_enabled():
            _log.warning("ACI_STREAM_SECRET not set: stream tokens use a per-process key. "
                         "Multi-pod AI Mode needs it set to the same value on every pod.")
    return _fallback_secret


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def issue_stream_token(user: dict[str, Any]) -> str:
    """Mint a short-lived HMAC-signed token carrying the (already verified) claims."""
    body = _b64(json.dumps({"u": _stream_claims(user), "exp": time.time() + _STREAM_TOKEN_TTL},
                           separators=(",", ":")).encode())
    sig = _b64(hmac.new(_stream_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def redeem_stream_token(token: str) -> Optional[dict[str, Any]]:
    """Claims for a valid (signature + not-expired) stream token, else None.
    Stateless: any replica sharing ACI_STREAM_SECRET can validate it."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64(hmac.new(_stream_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if time.time() > float(payload.get("exp", 0)):
        return None
    return payload.get("u")


async def get_current_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency: authenticate the request. No-op (anonymous) when auth is
    disabled, so local/offline dev is unaffected."""
    if not auth_enabled():
        return {"anonymous": True}
    token = _extract_token(request)
    if not token:
        st = request.query_params.get("stream_token")
        if st:
            claims = redeem_stream_token(st)
            if claims is not None:
                return claims
            raise HTTPException(status_code=401, detail="Invalid or expired stream token")
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        claims = verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return claims


_ROLE_CLAIM_KEYS = ("Custome-claim", "Custom-claim", "custom-claim", "customClaim",
                    "custom_claim", "claims")


def extract_role_id(decoded: dict[str, Any]):
    """The integer roleId carried in the token (custom claim), wherever it lives — same
    detection as ai-playground, which handles Keycloak mapper naming variations."""
    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    configured = (os.getenv("ACI_ROLE_CLAIM_KEY") or "").strip()
    containers = ([configured] if configured else []) + list(_ROLE_CLAIM_KEYS)
    for key in containers:
        obj = decoded.get(key)
        if isinstance(obj, dict) and "roleId" in obj:
            return as_int(obj.get("roleId"))
    if "roleId" in decoded:
        return as_int(decoded.get("roleId"))
    for v in decoded.values():
        if isinstance(v, dict) and "roleId" in v:
            return as_int(v.get("roleId"))
    return None


def _token_role_names(decoded: dict[str, Any]) -> set[str]:
    """Keycloak realm + client role names on the token (realm_access / resource_access)."""
    names: set[str] = set()
    ra = decoded.get("realm_access")
    if isinstance(ra, dict):
        names.update(str(r).lower() for r in ra.get("roles", []) or [])
    res = decoded.get("resource_access")
    if isinstance(res, dict):
        for client in res.values():
            if isinstance(client, dict):
                names.update(str(r).lower() for r in client.get("roles", []) or [])
    return names


def is_admin(user: dict[str, Any]) -> bool:
    """True if the user is an aosphere admin. When auth is disabled (local/offline) or
    admin-gating is turned off, everyone is treated as admin."""
    if not auth_enabled() or not _require_admin():
        return True
    if extract_role_id(user) in _admin_role_ids():
        return True
    return bool(_token_role_names(user) & _admin_role_names())


async def require_admin(request: Request) -> dict[str, Any]:
    """FastAPI dependency: authenticate AND require an aosphere admin (403 otherwise)."""
    user = await get_current_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="aosphere administrator access required")
    return user


# ---- Email allowlist ---------------------------------------------------------
# The platform used to sit behind a VPN and be admin-only. To open it to named
# stakeholders without the VPN, ACI_ALLOWED_EMAILS grants access to an exact list
# of email addresses (comma-separated, case-insensitive). Identity still comes
# from a VERIFIED Keycloak token — the allowlist only decides authorization, so
# nobody can get in by merely claiming an email. Fail-closed: with the variable
# unset, access stays admin-only exactly as before.


def _allowed_emails() -> frozenset[str]:
    raw = os.getenv("ACI_ALLOWED_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def token_email(user: dict[str, Any]) -> str | None:
    """The user's email from standard OIDC claims. `email` is authoritative;
    `preferred_username` is accepted only when it is itself an email address
    (some realms map usernames to emails)."""
    email = user.get("email")
    if isinstance(email, str) and "@" in email:
        return email.strip().lower()
    username = user.get("preferred_username")
    if isinstance(username, str) and "@" in username:
        return username.strip().lower()
    return None


def is_allowed(user: dict[str, Any]) -> bool:
    """Access requires BOTH: the user is an admin AND — when an allowlist is configured —
    their VERIFIED token email is on it. Admin alone is NOT sufficient once
    ACI_ALLOWED_EMAILS is set, so a restricted environment stays locked to that exact list
    even if admin-gating is loose. With no allowlist configured, falls back to admin-only.
    (Email is trustworthy only if Keycloak token validation is actually enforced.)"""
    if not is_admin(user):
        return False
    allowed = _allowed_emails()
    if not allowed:
        return True  # no allowlist -> admin-only, unchanged
    email = token_email(user)
    return email is not None and email in allowed


# ---- Stream token size -------------------------------------------------------
# A stream token travels in a URL query string, and a WAF caps the WHOLE query string
# (AWS's SizeRestrictions_QUERYSTRING: 2KB). Carrying the full Keycloak payload —
# allowed-origins, jti/iss/aud/sid, name parts, scope — minted a ~1,840 character token,
# leaving ~150 characters for the question: AI Mode answered short questions and 403'd on
# long ones, from the WAF, before the request reached the app. So carry only the claims the
# authorization path reads.

_STREAM_CLAIM_KEYS = ("email", "preferred_username", "anonymous", "roleId",
                      "realm_access", "resource_access",
                      "jurisdictions", "regions", "entitled_jurisdictions")


def _stream_claims(user: dict[str, Any]) -> dict[str, Any]:
    """`user` reduced to the authorization-relevant claims — but only when the reduction is
    provably equivalent. extract_role_id() will find a roleId in a dict under ANY name, so
    the small copy is used only if it yields the same admin / access / entitlement verdicts;
    otherwise the full claims are carried and the token stays large. Shrinking must never be
    able to turn an entitled user into a 403."""
    small = {k: user[k] for k in _STREAM_CLAIM_KEYS if k in user}
    configured = (os.getenv("ACI_ROLE_CLAIM_KEY") or "").strip()
    for key in (*([configured] if configured else []), *_ROLE_CLAIM_KEYS):
        obj = user.get(key)
        if isinstance(obj, dict) and "roleId" in obj:
            small[key] = {"roleId": obj["roleId"]}   # the org block's only read field
    equivalent = (is_admin(small) == is_admin(user)
                  and is_allowed(small) == is_allowed(user)
                  and entitled_jurisdictions(small) == entitled_jurisdictions(user))
    return small if equivalent else dict(user)


async def require_access(request: Request) -> dict[str, Any]:
    """FastAPI dependency: authenticate AND require platform access — an aosphere
    admin, or an email on ACI_ALLOWED_EMAILS (403 otherwise)."""
    user = await get_current_user(request)
    if not is_allowed(user):
        raise HTTPException(status_code=403, detail="access to the Core Index has not "
                                                    "been granted for this account")
    return user


def entitled_jurisdictions(user: dict[str, Any]) -> Optional[list[str]]:
    """Jurisdictions this user is entitled to, or None for 'all'.

    Subscriptions are per-region, so once Keycloak carries an entitlement claim
    (e.g. a `jurisdictions` / `regions` string array mapped onto the token) we
    restrict results to it. Until that claim exists this returns None (no
    restriction) — the hook is here so enforcement is a config change, not a rewrite.
    """
    for key in ("jurisdictions", "regions", "entitled_jurisdictions"):
        val = user.get(key)
        if isinstance(val, (list, tuple)) and val:
            return [str(v) for v in val]
        if isinstance(val, str) and val.strip():
            return [s.strip() for s in val.split(",") if s.strip()]
    return None


def entitled_products(user: dict[str, Any]) -> Optional[list[str]]:
    """Products this user is entitled to, or None for 'all'.

    Same hook pattern as entitled_jurisdictions: reads a Keycloak claim once it
    is mapped onto the token. Values may be renderStyle ids or product names —
    the entity-search layer handles both.
    """
    for key in ("products", "productIds", "entitled_products"):
        val = user.get(key)
        if isinstance(val, (list, tuple)) and val:
            return [str(v) for v in val]
        if isinstance(val, str) and val.strip():
            return [s.strip() for s in val.split(",") if s.strip()]
    return None


def public_config() -> dict[str, Any]:
    """Non-secret OIDC config the browser UI needs to start a login flow."""
    return {
        "auth_enabled": auth_enabled(),
        "keycloak_url": _kc_url(),
        "realm": _kc_realm(),
        "client_id": _kc_client(),
    }
