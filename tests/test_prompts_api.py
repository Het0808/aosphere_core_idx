"""The prompt-management endpoints (AOSNG-3442).

These endpoints hand an admin the text that decides what the assistant asserts about regulated
content, so the tests are weighted towards refusal and towards the ways a save could appear to
work without working:

  * admin-only, on the same gate as the reindex endpoint
  * an empty prompt is a DELETE, never an empty system prompt
  * with no storage configured, writes fail 503 rather than silently vanishing
  * a product must exist in the index, not merely be a safe string

`effective` is asserted against `instructions_for` rather than against a copy of the text, so
these tests cannot drift from what the agent actually assembles.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aosphere_core_index.llm import prompt_store as PS  # noqa: E402
from aosphere_core_index.llm.agent import instructions_for  # noqa: E402
from aosphere_core_index.service import app as APP  # noqa: E402

PRODUCTS = ["Data Privacy", "Shareholding Disclosure", "160_Bank_Confidentiality_&_Outsourcing"]


class _Memory(PS.NullBackend):
    """An in-memory backend, so write behaviour is testable before S3 exists.

    Keyed by (product, mode): the two answer modes are stored separately, and a test that
    conflated them would not notice one overwriting the other.
    """

    def __init__(self):
        self.recs, self.puts, self.deletes = {}, [], []

    def get(self, product, mode="summary"):
        return self.recs.get((product, mode))

    def put(self, product, prompt, by, mode="summary"):
        rec = PS.PromptRecord(product, prompt, by, 1_700_000_000.0, mode)
        self.recs[(product, mode)] = rec
        self.puts.append((product, prompt, by, mode))
        return rec

    def delete(self, product, mode="summary"):
        self.recs.pop((product, mode), None)
        self.deletes.append((product, mode))

    def overrides(self):
        out = {}
        for (p, m) in self.recs:
            out.setdefault(p, set()).add(m)
        return out


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(APP, "_prompt_products", lambda: list(PRODUCTS))
    APP.app.dependency_overrides[APP.require_admin] = lambda: {
        "email": "sme@aosphere.com", "sub": "abc"}
    try:
        yield TestClient(APP.app)
    finally:
        APP.app.dependency_overrides.clear()
        PS.set_backend(PS.NullBackend())


@pytest.fixture
def mem(monkeypatch):
    b = _Memory()
    monkeypatch.setattr(PS, "_backend", b)
    return b


# ------------------------------------------------------------------ authorisation

def test_every_endpoint_sits_on_the_same_admin_gate_as_reindex():
    """Asserted structurally, not by expecting a 403.

    Locally ACI_AUTH_ENABLED=0, so EVERY endpoint answers 200 without a token — the existing
    /api/admin/reindex included. A status-code assertion here would therefore be testing the
    environment rather than the code, and would pass just as happily if the dependency were
    dropped. What must hold is that these routes carry require_admin, the same dependency the
    reindex endpoint uses, so that wherever auth IS enabled they refuse a non-admin.
    """
    def gate_of(path, method):
        for r in APP.app.routes:
            if getattr(r, "path", None) == path and method in getattr(r, "methods", ()):
                return {d.call for d in r.dependant.dependencies}
        raise AssertionError(f"no route for {method} {path}")

    reindex_gate = gate_of("/api/admin/reindex", "POST")
    assert APP.require_admin in reindex_gate, "the reference gate moved; update this test"
    for path, method in [("/api/prompts", "GET"), ("/api/prompts/_default", "GET"),
                         ("/api/prompts/{product:path}", "GET"),
                         ("/api/prompts/{product:path}", "PUT"),
                         ("/api/prompts/{product:path}", "DELETE")]:
        assert APP.require_admin in gate_of(path, method), \
            f"{method} {path} is not admin-gated"


# ------------------------------------------------------------------ reads

def test_the_listing_marks_which_products_and_modes_carry_an_override(client, mem):
    mem.put("Data Privacy", "DP", "sme@aosphere.com", "explain")
    d = client.get("/api/prompts").json()
    assert [p["product"] for p in d["products"]] == PRODUCTS, \
        "the listing must use the same product source and order as the filter"
    by_p = {p["product"]: p for p in d["products"]}
    assert by_p["Data Privacy"]["modes"] == ["explain"], \
        "the screen must show WHICH answer was customised, not just that one was"
    assert by_p["Data Privacy"]["has_override"] is True
    assert by_p["Shareholding Disclosure"]["modes"] == []
    assert d["modes"] == ["summary", "explain"]


def test_a_product_with_no_override_reports_the_default_as_effective(client, mem):
    d = client.get("/api/prompts/Data Privacy").json()
    assert d["override"] is None and d["is_default"] is True
    assert d["effective"] == instructions_for(False), \
        "effective must be what the agent would actually assemble"
    assert d["updated_by"] is None and d["updated_at"] is None
    assert d["mode"] == "summary", "summary is the default mode, matching the Explain toggle"


def test_an_override_is_reflected_in_effective_not_just_returned(client, mem):
    mem.put("Data Privacy", "DP OVERRIDE", "sme@aosphere.com")
    d = client.get("/api/prompts/Data Privacy").json()
    assert d["override"] == "DP OVERRIDE" and d["is_default"] is False
    assert d["effective"] == instructions_for(False, "DP OVERRIDE")
    assert d["effective"].startswith("DP OVERRIDE")


def test_a_product_name_with_an_ampersand_round_trips(client, mem):
    """Real corpus names carry &, spaces, parentheses and commas."""
    name = "160_Bank_Confidentiality_&_Outsourcing"
    assert client.put(f"/api/prompts/{name}", json={"prompt": "P"}).status_code == 200
    assert client.get(f"/api/prompts/{name}").json()["override"] == "P"


# ------------------------------------------------------------------ writes

def test_a_save_records_who_made_it(client, mem):
    d = client.put("/api/prompts/Data Privacy", json={"prompt": "NEW"}).json()
    assert d["override"] == "NEW"
    assert d["updated_by"] == "sme@aosphere.com", "audit is required, not optional, here"
    assert mem.puts == [("Data Privacy", "NEW", "sme@aosphere.com", "summary")]


def test_a_whitespace_only_prompt_deletes_rather_than_installing_an_empty_prompt(client, mem):
    """THE one that matters. An empty system prompt would strip the grounding and citation
    rules, leaving the assistant free to answer from outside knowledge."""
    mem.put("Data Privacy", "OLD", "someone")
    d = client.put("/api/prompts/Data Privacy", json={"prompt": "   \n\t "}).json()
    assert d["is_default"] is True and d["override"] is None
    assert mem.deletes == [("Data Privacy", "summary")], "it must DELETE, not store whitespace"
    assert not any(p[1].strip() == "" for p in mem.puts), "no empty prompt may ever be stored"


def test_delete_restores_the_default(client, mem):
    mem.put("Data Privacy", "OLD", "someone")
    d = client.delete("/api/prompts/Data Privacy").json()
    assert d["is_default"] is True
    assert d["effective"] == instructions_for(False)


def test_an_absurdly_long_prompt_is_refused(client, mem):
    """It becomes a system prompt on every request for the product, so a pasted document
    would be charged per question."""
    r = client.put("/api/prompts/Data Privacy", json={"prompt": "x" * 20001})
    assert r.status_code == 422
    assert mem.puts == []


def test_an_empty_json_prompt_is_refused_by_validation(client, mem):
    assert client.put("/api/prompts/Data Privacy", json={"prompt": ""}).status_code == 422


# ------------------------------------------------------------------ no storage

def test_reads_work_with_no_storage_configured(client):
    """Deliberate: the screen must be usable and truthful before the backend lands."""
    assert client.get("/api/prompts").status_code == 200
    d = client.get("/api/prompts/Data Privacy").json()
    assert d["is_default"] is True and d["effective"] == instructions_for(False)


def test_writes_fail_loudly_with_no_storage_configured(client):
    """A save that silently vanished is the worst outcome for this feature: an SME would
    believe a prompt was in force while the default kept answering."""
    r = client.put("/api/prompts/Data Privacy", json={"prompt": "NEW"})
    assert r.status_code == 503
    assert "no prompt store" in r.json()["detail"]
    assert client.delete("/api/prompts/Data Privacy").status_code == 503


# ------------------------------------------------------------------ product validation

def test_a_product_absent_from_the_index_is_404_not_stored(client, mem):
    """Storing an override for a mistyped product would write a prompt nothing ever reads,
    and the SME would have no way to tell."""
    assert client.get("/api/prompts/Nonexistent Product").status_code == 404
    assert client.put("/api/prompts/Nonexistent Product", json={"prompt": "x"}).status_code == 404
    assert mem.puts == []


@pytest.mark.parametrize("bad", ["..", "../secrets", "a\x00b"])
def test_an_unsafe_product_name_is_refused(bad, mem):
    """The name becomes a storage key, so it is validated rather than trusted."""
    assert not PS.valid_product(bad)


@pytest.mark.parametrize("good", PRODUCTS + ["Marketing Restrictions - Asset Management",
                                             "Canada (Alberta, British Columbia)",
                                             "Aruba, Curaçao and St. Maarten"])
def test_real_product_names_are_accepted(good):
    assert PS.valid_product(good)


# ------------------------------------------------------------------ the two answer modes

def test_each_mode_is_saved_read_and_cleared_separately(client, mem):
    """The point of two prompts. An SME customising the explaining answer must not disturb the
    summary one, and clearing either must leave the other in force."""
    client.put("/api/prompts/Data Privacy?mode=summary", json={"prompt": "SUM"})
    client.put("/api/prompts/Data Privacy?mode=explain", json={"prompt": "EXP"})
    assert client.get("/api/prompts/Data Privacy?mode=summary").json()["override"] == "SUM"
    assert client.get("/api/prompts/Data Privacy?mode=explain").json()["override"] == "EXP"

    client.delete("/api/prompts/Data Privacy?mode=explain")
    assert client.get("/api/prompts/Data Privacy?mode=explain").json()["is_default"] is True
    assert client.get("/api/prompts/Data Privacy?mode=summary").json()["override"] == "SUM", \
        "clearing the explaining answer must not revert the summary one"


def test_the_default_mode_is_summary_matching_the_explain_toggle_being_off(client, mem):
    """The UI has one checkbox: off is a summary. A request without ?mode must mean the same,
    or a save from the un-toggled screen would land on the wrong prompt."""
    client.put("/api/prompts/Data Privacy", json={"prompt": "NO MODE GIVEN"})
    assert mem.puts[-1][3] == "summary"
    assert client.get("/api/prompts/Data Privacy?mode=summary").json()["override"] \
        == "NO MODE GIVEN"
    assert client.get("/api/prompts/Data Privacy?mode=explain").json()["is_default"] is True


def test_each_mode_reports_its_own_default_to_diff_against(client, mem):
    """The two defaults differ — the summary default carries the brief answer contract, the
    explain default the explaining one. Sending one for both would make the UI's diff lie."""
    s = client.get("/api/prompts/Data Privacy?mode=summary").json()
    e = client.get("/api/prompts/Data Privacy?mode=explain").json()
    assert s["default_prompt"] == instructions_for(False)
    assert e["default_prompt"] == instructions_for(True)
    assert s["default_prompt"] != e["default_prompt"]


def test_the_default_is_readable_without_naming_a_product(client, mem):
    """The screen's first row shows the default, which belongs to the MODE and not to any
    product — so it must be readable with no product named, and per mode."""
    for mode, explain in [("summary", False), ("explain", True)]:
        d = client.get(f"/api/prompts/_default?mode={mode}").json()
        assert d["default_prompt"] == instructions_for(explain)
        assert d["mode"] == mode and d["product"] is None
        assert d["read_only"] is True, "the UI hides its editor on this flag"
    assert client.get("/api/prompts/_default").json()["mode"] == "summary"
    assert client.get("/api/prompts/_default?mode=brief").status_code == 400


def test_the_default_route_is_not_swallowed_by_the_product_route(client, mem):
    """`{product:path}` would match "_default" too, so the two routes' ORDER is what makes this
    endpoint reachable at all. A product literally named _default is a 404, not this."""
    assert client.get("/api/prompts/_default").json()["product"] is None
    paths = [r.path for r in APP.app.routes if getattr(r, "path", "").startswith("/api/prompts")]
    assert paths.index("/api/prompts/_default") < paths.index("/api/prompts/{product:path}")


def test_the_default_cannot_be_written_through_its_own_route(client, mem):
    """It is read-only because there is nowhere to put it: the store holds overrides only, and
    an edit here would change every product's answer at once."""
    assert client.put("/api/prompts/_default",
                      json={"prompt": "x"}).status_code in (404, 405), \
        "writing the default must not be routed anywhere"
    assert client.delete("/api/prompts/_default").status_code in (404, 405)
    assert mem.puts == [] and mem.deletes == []


def test_an_override_replaces_the_whole_prompt_for_its_mode(client, mem):
    """A mode override owns the answer format too, so `effective` is the override plus the
    shared invariants — not the override with a built-in format block appended that would
    contradict it."""
    d = client.put("/api/prompts/Data Privacy?mode=summary", json={"prompt": "CUSTOM"}).json()
    assert d["effective"] == instructions_for(False, "CUSTOM")
    assert "ANSWER FORMAT — BRIEF" not in d["effective"]


@pytest.mark.parametrize("bad", ["brief", "Summary", "verbose", "", "explain "])
def test_an_unknown_mode_is_refused(bad, client, mem):
    """Silently defaulting a typo'd mode to summary would let someone save the explaining
    prompt over the summary one without noticing."""
    r = client.get(f"/api/prompts/Data Privacy?mode={bad}")
    assert r.status_code == 400, f"{bad!r} should not be accepted"
    assert client.put(f"/api/prompts/Data Privacy?mode={bad}",
                      json={"prompt": "x"}).status_code == 400
    assert mem.puts == []
