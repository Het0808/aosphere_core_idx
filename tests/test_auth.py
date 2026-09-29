"""Auth unit tests: role extraction, admin gating, entitlements, stream tokens.

No Keycloak needed — everything here is pure claim/dict logic plus the in-memory
stream-token store.
"""

import pytest

pytest.importorskip("jwt")
pytest.importorskip("fastapi")

from aosphere_core_index.service import auth


@pytest.fixture(autouse=True)
def _no_realm(monkeypatch):
    """Auth disabled by default (no realm configured), as in local/offline dev."""
    for var in ("KEYCLOAK_URL", "KEYCLOAK_REALM", "KEYCLOAK_AUDIENCE",
                "ACI_AUTH_ENABLED", "ACI_REQUIRE_ADMIN", "ACI_ADMIN_ROLE_IDS",
                "ACI_ADMIN_ROLES", "ACI_ROLE_CLAIM_KEY"):
        monkeypatch.delenv(var, raising=False)


# ---- role extraction ---------------------------------------------------------

def test_extract_role_id_variants():
    assert auth.extract_role_id({"Custom-claim": {"roleId": 1}}) == 1
    assert auth.extract_role_id({"custom_claim": {"roleId": "7"}}) == 7
    assert auth.extract_role_id({"roleId": 3}) == 3
    assert auth.extract_role_id({"anything": {"roleId": 9}}) == 9
    assert auth.extract_role_id({"roleId": "not-a-number"}) is None
    assert auth.extract_role_id({}) is None


def test_is_admin_when_auth_disabled():
    assert auth.is_admin({"anonymous": True}) is True


def test_is_admin_by_role_id(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "aosphere")
    monkeypatch.setenv("ACI_ADMIN_ROLE_IDS", "1,42")
    assert auth.is_admin({"Custom-claim": {"roleId": 42}}) is True
    assert auth.is_admin({"Custom-claim": {"roleId": 5}}) is False


def test_is_admin_by_role_name(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "aosphere")
    assert auth.is_admin({"realm_access": {"roles": ["Admin"]}}) is True
    assert auth.is_admin({"realm_access": {"roles": ["viewer"]}}) is False


# ---- entitlements ------------------------------------------------------------

def test_entitled_jurisdictions_absent_means_all():
    assert auth.entitled_jurisdictions({}) is None


def test_entitled_jurisdictions_list_and_csv():
    assert auth.entitled_jurisdictions({"jurisdictions": ["France", "Germany"]}) == [
        "France", "Germany"]
    assert auth.entitled_jurisdictions({"regions": "France, Germany"}) == [
        "France", "Germany"]


# ---- stream tokens -----------------------------------------------------------

def test_stream_token_roundtrip():
    tok = auth.issue_stream_token({"preferred_username": "gaurang", "roleId": 1})
    claims = auth.redeem_stream_token(tok)
    assert claims and claims["preferred_username"] == "gaurang"


def test_stream_token_unknown_is_none():
    assert auth.redeem_stream_token("nope") is None


def test_stream_token_expires(monkeypatch):
    tok = auth.issue_stream_token({"u": 1})
    # exp is a wall-clock time.time() (stateless/cross-pod token), so advance time.time —
    # not monotonic — past the TTL to simulate expiry.
    real = auth.time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + auth._STREAM_TOKEN_TTL + 1)
    assert auth.redeem_stream_token(tok) is None


# ---- email allowlist ----------------------------------------------------------

def _kc(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "aosphere")


def test_token_email_claims():
    assert auth.token_email({"email": "A.User@Aosphere.com "}) == "a.user@aosphere.com"
    assert auth.token_email({"preferred_username": "b@x.com"}) == "b@x.com"
    assert auth.token_email({"preferred_username": "not-an-email"}) is None
    assert auth.token_email({}) is None


def test_allowlist_grants_access(monkeypatch):
    # Access = admin AND (when a list is set) email on the list (see auth.is_allowed).
    _kc(monkeypatch)
    monkeypatch.setenv("ACI_ALLOWED_EMAILS", "gaurang.patel@aosphere.com, Ext.User@partner.com")
    adm = {"realm_access": {"roles": ["admin"]}}
    assert auth.is_allowed({**adm, "email": "gaurang.patel@aosphere.com"}) is True
    assert auth.is_allowed({**adm, "email": "EXT.USER@PARTNER.COM"}) is True   # case-insensitive
    assert auth.is_allowed({**adm, "email": "stranger@partner.com"}) is False  # admin, not listed
    assert auth.is_allowed({"email": "gaurang.patel@aosphere.com"}) is False    # listed, not admin
    assert auth.is_allowed({**adm}) is False                                    # admin, no email claim


def test_allowlist_unset_stays_admin_only(monkeypatch):
    _kc(monkeypatch)
    monkeypatch.delenv("ACI_ALLOWED_EMAILS", raising=False)
    assert auth.is_allowed({"email": "anyone@aosphere.com"}) is False     # fail-closed
    assert auth.is_allowed({"realm_access": {"roles": ["admin"]}}) is True  # admins unaffected


def test_admin_still_bound_by_allowlist(monkeypatch):
    # With a list set, admin alone is NOT sufficient — the admin's email must be on it.
    _kc(monkeypatch)
    monkeypatch.setenv("ACI_ALLOWED_EMAILS", "only.this@aosphere.com")
    assert auth.is_allowed({"email": "other@aosphere.com",
                            "realm_access": {"roles": ["admin"]}}) is False
    assert auth.is_allowed({"email": "only.this@aosphere.com",
                            "realm_access": {"roles": ["admin"]}}) is True


def test_no_domain_wildcards(monkeypatch):
    _kc(monkeypatch)
    monkeypatch.setenv("ACI_ALLOWED_EMAILS", "@aosphere.com")
    assert auth.is_allowed({"email": "someone@aosphere.com"}) is False    # exact match only


def test_allowed_when_auth_disabled():
    # no realm configured (autouse fixture) -> local/offline dev unaffected
    assert auth.is_allowed({"anonymous": True}) is True
