"""A long AI Mode question must not travel in the URL.

AI Mode streamed over EventSource, which can only issue a GET — so the question, the
product/jurisdiction CSVs, the model, the session id AND an auth token all went into the
query string. AWS WAF's core rule set caps the WHOLE query string at 2 KB
(SizeRestrictions_QUERYSTRING) and answers 403 itself, before the request reaches the app.
The stream token carried the entire Keycloak payload — 1,840 characters on dev1 — leaving
about 150 for the question. So on dev the first (short) question worked and a longer one
403'd, with nothing in the application log, while `_Q_MAX` advertised 2000 characters.

Two things are pinned here: the question travels in a request BODY, where no such ceiling
exists; and the stream token that remains for EventSource clients is small — and shrinking
it never changes an authorization verdict.
"""

import json
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

from aosphere_core_index.service import app as A  # noqa: E402
from aosphere_core_index.service import auth  # noqa: E402

# The dev1 Keycloak access token, claim for claim: this is what used to be base64'd into a
# URL. allowed-origins alone is ~430 characters of it.
DEV1_CLAIMS = {
    "exp": 1786617853, "iat": 1786617553, "auth_time": 1786521670,
    "jti": "onrtrt:5573999e-8c1c-9465-b993-7ef031370754",
    "iss": "https://auth.dev1.aoslogin.net/realms/aosphere", "aud": "account",
    "sub": "f:894c9cdc-29e4-437f-acab-6d15fa0e2d9a:457", "typ": "Bearer",
    "azp": "ai-playground-client", "sid": "89100569-6696-476c-a916-7e8ff61ea12e", "acr": "1",
    "allowed-origins": ["https://aos-proto.replit.app", "http://localhost:8090",
                        "https://playground.dev1.aoslogin.net",
                        "https://core-index.dev1.aoslogin.net",
                        "https://b6077c47-5f19-4a9b-9ad1-6bfcf6462dbd-00-3rljh0y2yjij8.kirk.replit.dev",
                        "http://localhost:3000", "https://api-docs.dev1.aoslogin.net"],
    "realm_access": {"roles": ["offline_access", "uma_authorization"]},
    "resource_access": {"account": {"roles": ["manage-account", "view-profile"]}},
    "scope": "openid email profile", "isTermAndConditionAccepted": False,
    "Custome-claim": {"orgId": 101, "orgTypeId": 1, "roleId": 1, "orgName": "aosphere Ltd",
                      "orgEntityId": 473, "jobTitle": "Director",
                      "orgEntityName": "aosphere Limited", "firstName": "Mitul",
                      "lastName": "Patel (Admin)", "lastLogin": "2026-08-13T11:24:46.160"},
    "email_verified": True, "name": "Mitul Patel (Admin)", "remember_me": False,
    "preferred_username": "mitul.patel@example.com", "given_name": "Mitul",
    "family_name": "Patel (Admin)", "isApiClient": True, "email": "mitul.patel@example.com",
}

WAF_QUERYSTRING_LIMIT = 2048          # AWS managed rule SizeRestrictions_QUERYSTRING


@pytest.fixture
def client(monkeypatch):
    """The service with auth satisfied and the agent stubbed — this is about transport."""

    async def fake_stream(q, allowed, model=None, session_id=None, explain=False, products=None,
                      context=None):
        yield {"kind": "activity", "text": f"read {len(q)} chars"}
        yield {"kind": "answer", "answer": f"answered {len(q)} chars", "sources": []}

    import aosphere_core_index.llm.agent as agent_mod
    monkeypatch.setattr(agent_mod, "run_agent_stream", fake_stream, raising=False)
    monkeypatch.setattr(A, "_scope_ids", lambda p, j, u: None)
    # search, stubbed at the same seam — these tests are about transport, not retrieval
    monkeypatch.setattr(A, "search_all", lambda q, k=80, jurisdictions=None: [])
    monkeypatch.setattr(A, "_detected_jurisdictions", lambda q, u: [])
    from aosphere_core_index.service import registry as R
    monkeypatch.setattr(R, "did_you_mean", lambda q: None, raising=False)
    A.app.dependency_overrides[A.require_access] = lambda: dict(DEV1_CLAIMS)
    yield TestClient(A.app)
    A.app.dependency_overrides.clear()


def _events(resp):
    return [json.loads(line[5:]) for line in resp.text.splitlines()
            if line.startswith("data:")]


def test_a_question_too_long_for_a_url_streams_fine_in_a_body(client):
    """The reported failure, as a test: a question the WAF would have blocked in a URL."""
    q = "does a controller need a processor agreement " * 40          # ~1,760 chars
    assert len(q) > WAF_QUERYSTRING_LIMIT - 500, "must exceed what a URL had left for it"

    r = client.post("/api/agent/stream", json={"q": q, "model": "m", "session": "s"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    kinds = [e["kind"] for e in _events(r)]
    assert "answer" in kinds and kinds[-1] == "done"
    assert f"answered {len(q)} chars" in r.text, "the whole question must reach the agent"


def test_the_body_carries_no_query_string_at_all(client):
    """Not merely 'shorter': the URL is fixed-length whatever the question and scope."""
    r = client.post("/api/agent/stream",
                    json={"q": "x" * 1900, "products": "Data Privacy",
                          "jurisdictions": ",".join(f"Jurisdiction {i}" for i in range(200)),
                          "model": "anthropic.claude-sonnet-4-6", "session": "abcdef"})
    assert r.status_code == 200
    assert r.request.url.query == b"", "nothing may leak into the query string"


def test_the_body_form_still_enforces_the_documented_limit(client):
    """_Q_MAX is the app's contract; a body must not become an unbounded input."""
    r = client.post("/api/agent/stream", json={"q": "x" * (A._Q_MAX + 1)})
    assert r.status_code == 422


def test_scope_and_session_survive_the_move_to_a_body(client, monkeypatch):
    """The fields must reach _scope_ids/run_agent_stream, not be silently dropped."""
    seen = {}
    monkeypatch.setattr(A, "_scope_ids",
                        lambda p, j, u: seen.update(products=p, jurisdictions=j) or None)

    async def capture(q, allowed, model=None, session_id=None, explain=False, products=None,
                      context=None):
        seen.update(q=q, model=model, session_id=session_id)
        yield {"kind": "answer", "answer": "ok", "sources": []}

    import aosphere_core_index.llm.agent as agent_mod
    monkeypatch.setattr(agent_mod, "run_agent_stream", capture, raising=False)

    client.post("/api/agent/stream", json={"q": "hello", "products": "Data Privacy",
                                           "jurisdictions": "France,Germany",
                                           "model": "m1", "session": "sid-9"})
    assert seen == {"products": "Data Privacy", "jurisdictions": "France,Germany",
                    "q": "hello", "model": "m1", "session_id": "sid-9"}


def test_the_get_form_is_kept_for_eventsource_clients(client):
    """The GET is still the documented EventSource route — it must not have been removed."""
    r = client.get("/api/agent/stream", params={"q": "short question", "session": "s"})
    assert r.status_code == 200
    assert [e["kind"] for e in _events(r)][-1] == "done"


# ---- the token that remains on the GET path ----------------------------------

def test_the_stream_token_leaves_room_for_a_real_question(monkeypatch):
    """A URL-borne token must be a small fraction of the WAF's budget, not most of it.

    Note what this does NOT claim: even at this size, a _Q_MAX (2000-character) question
    cannot fit in a query string once percent-encoded. The GET form can never honour the
    documented limit — only the POST form can, which is why the UI uses it and the GET
    docstring points there. What is pinned here is that the EventSource path is usable for
    ordinary questions instead of failing above ~150 characters.
    """
    monkeypatch.setenv("ACI_STREAM_SECRET", "s" * 32)
    token = auth.issue_stream_token(DEV1_CLAIMS)
    assert len(token) < 600, f"token is {len(token)} chars, it travels in a query string"
    assert len(token) < WAF_QUERYSTRING_LIMIT // 4, "token must not eat the URL budget"

    q = "does a controller need a processor agreement in this jurisdiction " * 15  # ~990
    url = f"q={quote(q)}&model={quote('anthropic.claude-sonnet-4-6')}" \
          f"&session=abcdef01&stream_token={quote(token)}"
    assert len(url) < WAF_QUERYSTRING_LIMIT, f"{len(q)}-char question -> {len(url)}B URL"


def test_shrinking_preserves_the_authorization_verdicts(monkeypatch):
    """The claims dropped must be ones nothing reads. Same admin/access/entitlements."""
    monkeypatch.setenv("ACI_STREAM_SECRET", "s" * 32)
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "aosphere")
    monkeypatch.setenv("ACI_ADMIN_ROLE_IDS", "1")
    claims = dict(DEV1_CLAIMS, jurisdictions=["Data Privacy - France"])

    small = auth.redeem_stream_token(auth.issue_stream_token(claims))

    assert auth.is_admin(small) == auth.is_admin(claims) is True
    assert auth.is_allowed(small) == auth.is_allowed(claims) is True
    assert auth.entitled_jurisdictions(small) == ["Data Privacy - France"]
    assert auth.token_email(small) == "mitul.patel@example.com"     # allowlist still works
    assert "allowed-origins" not in small and "jti" not in small    # and the bulk is gone


def test_a_roleid_under_an_unexpected_key_falls_back_to_full_claims(monkeypatch):
    """extract_role_id finds roleId in a dict under ANY name. Where pruning would change
    the verdict, the full claims must be carried — a smaller URL is never worth a 403."""
    monkeypatch.setenv("ACI_STREAM_SECRET", "s" * 32)
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.example")
    monkeypatch.setenv("KEYCLOAK_REALM", "aosphere")
    monkeypatch.setenv("ACI_ADMIN_ROLE_IDS", "42")
    claims = {"email": "a@b.com", "some_future_mapper": {"roleId": 42}}
    assert auth.is_admin(claims) is True

    small = auth.redeem_stream_token(auth.issue_stream_token(claims))

    assert auth.is_admin(small) is True, "an admin must not lose admin by being shrunk"
    assert small["some_future_mapper"] == {"roleId": 42}


def test_the_ui_does_not_put_the_question_in_a_url():
    """The page itself is the regression surface: it must POST, not open an EventSource."""
    from aosphere_core_index.service.web import PAGE

    assert 'authFetch("/api/agent/stream",{method:"POST"' in PAGE
    assert "new EventSource(`/api/agent/stream?" not in PAGE
    assert "stream_token=" not in PAGE, "no auth token may be built into a URL"


# ---- the same defect in the primary search path ------------------------------

def test_search_filters_travel_in_a_body_too(client):
    """A partial jurisdiction filter is a CSV of up to _JURIS_CSV_MAX characters. In a URL,
    percent-encoded commas cost 3 bytes each and the WAF 403s the request past 2 KB — so
    narrowing the filter broke SEARCH the same way a long question broke AI Mode."""
    csv = ",".join(f"Jurisdiction {i}" for i in range(150))
    assert len(quote(csv)) > WAF_QUERYSTRING_LIMIT, "the premise: this cannot fit in a URL"

    r = client.post("/api/search", json={"q": "processor agreement", "k": 5,
                                         "min_score": 0.0, "jurisdictions": csv})

    assert r.status_code == 200, r.text
    assert r.request.url.query == b""
    assert "results" in r.json()


def test_the_ui_posts_its_search():
    from aosphere_core_index.service.web import PAGE

    assert 'authFetch("/api/search",{method:"POST"' in PAGE
    assert "/api/search?q=" not in PAGE, "the query must not go back into the URL"
