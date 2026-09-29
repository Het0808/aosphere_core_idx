"""The interview endpoints: the contract a client builds against.

Weighted towards the properties a caller cannot check for itself:

  * the interview is STATELESS, so a client's posted slots are re-validated server-side
    and cannot be used to post past a question;
  * the model's decisions never reach the client unfiltered;
  * a dead model is a 503, not a wrong lane;
  * only a product that HAS a vocabulary can be interviewed.

The router is stubbed. Bedrock is not reachable from a test run, and what matters here is
the wiring around it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aosphere_core_index.interview import engine as IE  # noqa: E402
from aosphere_core_index.interview import router as IR  # noqa: E402
from aosphere_core_index.interview import vocabulary as IV  # noqa: E402
from aosphere_core_index.service import app as APP  # noqa: E402

JURISDICTIONS = ["Bahamas", "Ireland", "Jersey", "United Kingdom"]
PRODUCTS = ["Data Privacy", IV.PRODUCT]


def _jurisdictions(product, user, selected=None):
    """Mirrors _interview_jurisdictions: narrow to the caller's filter, but never to
    nothing — a filter selecting only jurisdictions this product lacks is ignored."""
    have = set(JURISDICTIONS)
    if selected:
        narrowed = have & {x.strip() for x in selected.split(",") if x.strip()}
        if narrowed:
            have = narrowed
    return sorted(have)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(APP, "_prompt_products", lambda: list(PRODUCTS))
    monkeypatch.setattr(APP, "_interview_jurisdictions", _jurisdictions)
    # The section hints resolve against a real bundle; the index is not loaded in a test
    # run, so keep them unresolved unless a test says otherwise.
    monkeypatch.setattr(APP, "_interview_sections", lambda p, j, titles: [])
    APP.app.dependency_overrides[APP.require_access] = lambda: {"email": "sme@aosphere.com"}
    try:
        yield TestClient(APP.app)
    finally:
        APP.app.dependency_overrides.clear()


def routed(**kw) -> dict:
    """A classify() result, with everything unset unless a test sets it."""
    out = {"intent": "permitted_activity", "other_kind": None,
           "slots": dict(IE.EMPTY_SLOTS), "signals": dict(IE.EMPTY_SIGNALS),
           "rationales": {}}
    out["slots"].update(kw.pop("slots", {}))
    out["signals"].update(kw.pop("signals", {}))
    out.update(kw)
    return out


@pytest.fixture
def stub(monkeypatch):
    def _stub(result):
        monkeypatch.setattr(IR, "classify", lambda messages, jurisdictions: result)
    return _stub


ASK = [{"role": "user", "content": "can I market my fund in Jersey"}]


# ------------------------------------------------------------------ schema

def test_the_schema_offers_every_jurisdiction_the_index_holds(client):
    """The POC's six were a pilot list. A jurisdiction the tool can answer for but not
    ask about is a silent gap, so the options come from the index."""
    d = client.get("/api/interview/schema").json()
    assert [j["name"] for j in d["jurisdictions"]] == JURISDICTIONS
    assert d["fields"] == list(IV.FIELDS)
    by_name = {j["name"]: j["eea"] for j in d["jurisdictions"]}
    assert by_name["Ireland"] is True
    assert by_name["United Kingdom"] is False, "the UK left the EEA"


def test_a_selected_region_is_not_asked_about_again(client, stub):
    """Reported from the first real use: choosing a region in the sidebar IS answering
    "which jurisdiction?", and asking again asks the user to repeat themselves. One
    selected jurisdiction leaves nothing to ask."""
    stub(routed())
    d = client.post("/api/interview/route", json={
        "messages": ASK, "jurisdictions": "Jersey",
        "slots": {"jurisdiction": "Jersey"}}).json()
    assert d["next_question"]["field"] == "activity", "not jurisdiction"
    assert d["slots"]["jurisdiction"] == "Jersey"


def test_several_selected_regions_narrow_the_question_instead_of_skipping_it(client, stub):
    """"Which of these three?" is still a real question — but offering all 88 when the
    user has already narrowed to three is ignoring what they told the filter."""
    stub(routed())
    d = client.post("/api/interview/route", json={
        "messages": ASK, "jurisdictions": "Bahamas,Ireland,Jersey"}).json()
    q = d["next_question"]
    assert q["field"] == "jurisdiction"
    assert [o["value"] for o in q["options"]] == ["Bahamas", "Ireland", "Jersey"]


def test_the_filter_also_narrows_what_the_extractor_may_return(client, monkeypatch):
    """Otherwise the two disagree: the model could name a jurisdiction the interview
    cannot offer, and the value would be dropped with no explanation."""
    from aosphere_core_index.interview import router as IR

    seen = {}
    monkeypatch.setattr(IR, "classify",
                        lambda messages, jurisdictions: seen.update(js=jurisdictions)
                        or routed())
    client.post("/api/interview/route", json={"messages": ASK, "jurisdictions": "Jersey"})
    assert seen["js"] == ["Jersey"]


def test_a_filter_that_selects_nothing_this_product_has_is_ignored(client, stub):
    """A jurisdiction only Data Privacy covers selects nothing here. Falling back to
    everything keeps the interview usable; silently offering nothing would strand it."""
    stub(routed())
    d = client.post("/api/interview/route", json={
        "messages": ASK, "jurisdictions": "Atlantis"}).json()
    assert [o["value"] for o in d["next_question"]["options"]] == JURISDICTIONS


def test_the_schema_narrows_to_the_filter_too(client):
    """A client renders the option list from the schema, so it has to see the same
    narrowing the server applies."""
    d = client.get("/api/interview/schema?jurisdictions=Ireland,Jersey").json()
    assert [j["name"] for j in d["jurisdictions"]] == ["Ireland", "Jersey"]


def test_only_a_product_with_a_vocabulary_can_be_interviewed(client):
    """These are Marketing Restrictions' own terms of art. Offering "Pre-Marketing of
    Funds" for Data Privacy would be nonsense, and a silent fallback would let a caller
    think it ran an interview that does not exist."""
    assert client.get("/api/interview/schema?product=Data Privacy").status_code == 404
    assert client.get(f"/api/interview/schema?product={IV.PRODUCT}").status_code == 200


def test_a_product_absent_from_this_index_is_404_even_with_a_vocabulary(client,
                                                                       monkeypatch):
    monkeypatch.setattr(APP, "_prompt_products", lambda: ["Data Privacy"])
    assert client.get("/api/interview/schema").status_code == 404


def test_the_schema_marks_which_definitions_are_drafted(client):
    """Activity and category wording is verbatim from the definitions document; the
    investor-type and some EEA entries are ours, pending review. A reviewer has to be able
    to tell which is which."""
    d = client.get("/api/interview/schema").json()
    investor = {x["value"]: x for x in d["definitions"]["investor_type"]}
    assert investor["Professional Investor"].get("drafted") is True
    activity = {x["value"]: x for x in d["definitions"]["activity"]}
    assert "drafted" not in activity["General Marketing"]
    # And the source document's own heading, where it differs from the option label.
    assert activity["Active Marketing/Selling"]["doc_term"] == "Active Marketing"


# ------------------------------------------------------------------ route

def test_a_vague_request_starts_the_interview_rather_than_being_answered(client, stub):
    """"I want to market my fund" has no jurisdiction, no activity and no category. The
    interview exists to collect exactly that, so it must not be treated as unanswerable."""
    stub(routed())
    d = client.post("/api/interview/route",
                    json={"messages": [{"role": "user", "content": "I want help with "
                                                                   "marketing"}]}).json()
    assert d["intent"] == "permitted_activity"
    assert d["complete"] is False and d["handoff"] is None
    assert d["next_question"]["field"] == "jurisdiction"


def test_the_next_question_carries_its_options_definitions_and_exclusions(client, stub):
    """Everything a client needs to render the step without a second call — and the
    reason an option is unavailable, so it can be greyed out rather than hidden."""
    stub(routed(slots={"jurisdiction": "Jersey"}, signals={"product_kind": "fund"}))
    d = client.post("/api/interview/route", json={"messages": ASK}).json()
    q = d["next_question"]
    assert q["field"] == "activity" and q["label"] == IV.FIELD_LABELS["activity"]
    opts = {o["value"]: o for o in q["options"]}
    assert set(opts) == set(IV.ACTIVITY_OPTIONS)
    assert opts["General Marketing"]["excluded_reason"], "a fund rules this out"
    assert "definition" in opts["Pre-Marketing of Funds"]
    assert "excluded_reason" not in opts["Active Marketing/Selling"]


def test_a_client_cannot_post_its_way_past_a_question(client, stub):
    """Stateless does not mean trusting. The posted slots are re-validated against the
    vocabulary, so a category from the wrong branch is dropped and its question re-asked
    — whatever the client believed it had."""
    stub(routed(slots={"jurisdiction": "Ireland"}))
    d = client.post("/api/interview/route", json={
        "messages": ASK,
        "slots": {"jurisdiction": "Jersey", "activity": "Active Marketing/Selling",
                  "category": "Funds - Open-Ended",
                  "investor_type": "Professional Investor"}}).json()
    assert d["slots"]["jurisdiction"] == "Ireland"
    assert d["slots"]["category"] is None and d["slots"]["investor_type"] is None
    assert d["next_question"]["field"] == "category"
    assert [o["value"] for o in d["next_question"]["options"]] == \
        list(IV.EEA_ACTIVE_CATEGORY_OPTIONS), "the EEA list, not the one posted"


def test_an_invented_jurisdiction_never_reaches_the_answer_path(client, stub):
    stub(routed(slots={"jurisdiction": "Atlantis"}))
    d = client.post("/api/interview/route", json={"messages": ASK}).json()
    assert d["slots"]["jurisdiction"] is None
    assert d["next_question"]["field"] == "jurisdiction"


def test_a_complete_scenario_hands_off_instead_of_asking_again(client, stub):
    stub(routed(slots={"jurisdiction": "Jersey", "activity": "Pre-Marketing of Funds",
                       "category": "Funds - Closed-Ended",
                       "investor_type": "Professional Investor"}))
    d = client.post("/api/interview/route", json={"messages": ASK}).json()
    assert d["complete"] is True and d["next_question"] is None
    h = d["handoff"]
    assert h["jurisdiction"] == "Jersey"
    assert "Jersey" in h["query"] and h["scenario"].startswith("Assess this")
    assert h["original_question"] == ASK[0]["content"], "the user's own wording survives"
    assert "Pre-Marketing of Funds" in h["definitions"]
    assert h["section_hints"], "the memo sections this scenario is answered from"


def test_a_capability_question_is_answered_without_touching_the_interview(client, stub):
    """A question about the tool is not an answer to the pending field."""
    stub(routed(intent="capability", slots={"jurisdiction": "Jersey"}))
    d = client.post("/api/interview/route", json={
        "messages": [{"role": "user", "content": "what can you tell me?"}],
        "slots": {"jurisdiction": "Jersey"}}).json()
    assert d["capability"] and str(len(JURISDICTIONS)) in d["capability"]
    assert d["next_question"] is None and d["complete"] is False
    assert d["slots"]["jurisdiction"] == "Jersey", "state is preserved"


def test_a_carve_out_question_is_routed_to_its_own_part_of_the_memo(client, stub):
    """"What disclaimers are required?" is informational, not a permission check — it
    skips the interview but still needs a jurisdiction, and the memo answers it in its own
    section."""
    stub(routed(intent="other", other_kind="disclaimers",
                slots={"jurisdiction": "Jersey", "category": "Funds - Closed-Ended"}))
    d = client.post("/api/interview/route", json={"messages": [
        {"role": "user", "content": "what disclaimers apply to a closed-ended fund?"}]}
    ).json()
    assert d["intent"] == "other" and d["complete"] is False
    g = d["general"]
    assert g["jurisdiction"] == "Jersey" and g["needs_jurisdiction"] is False
    assert g["section_hints"][0] == "Disclaimers: Closed-Ended Fund"
    assert g["query"] == "what disclaimers apply to a closed-ended fund?"


def test_a_general_question_with_no_jurisdiction_asks_for_one_first(client, stub):
    """The corpus is per-jurisdiction: there is no answer to give until one is chosen."""
    stub(routed(intent="other", other_kind="penalties"))
    d = client.post("/api/interview/route", json={"messages": [
        {"role": "user", "content": "what are the penalties for marketing unregistered?"}]}
    ).json()
    assert d["general"]["needs_jurisdiction"] is True
    assert d["next_question"]["field"] == "jurisdiction"


def test_a_newly_set_value_is_acknowledged(client, stub):
    stub(routed(slots={"jurisdiction": "Jersey"},
                rationales={"jurisdiction": "the Channel Islands crown dependency"}))
    d = client.post("/api/interview/route", json={"messages": ASK}).json()
    assert "Jersey" in d["acknowledgement"]
    assert "crown dependency" in d["acknowledgement"]


def test_a_dead_router_is_a_503_not_a_wrong_lane(client, monkeypatch):
    def dead(*a, **k):
        raise IR.RouterUnavailable("bedrock down")

    monkeypatch.setattr(IR, "classify", dead)
    monkeypatch.setattr(IR, "triage", dead)
    assert client.post("/api/interview/route",
                       json={"messages": ASK}).status_code == 503
    assert client.post("/api/interview/triage",
                       json={"messages": ASK}).status_code == 503


# ------------------------------------------------------------------ triage

def test_a_scoping_question_comes_back_with_its_explanation(client, monkeypatch):
    monkeypatch.setattr(IR, "triage",
                        lambda m, f: {"lane": "scoping_question", "has_answer": False})
    monkeypatch.setattr(IR, "explain",
                        lambda latest, field, juris: "Open-ended means …")
    d = client.post("/api/interview/triage", json={
        "messages": [{"role": "user", "content": "does an OEIC count as open-ended?"}],
        "pending_field": "category", "jurisdiction": "Jersey"}).json()
    assert d["lane"] == "scoping_question" and d["answer"] == "Open-ended means …"


def test_the_other_lanes_carry_no_explanation(client, monkeypatch):
    """The explainer call only runs for the scoping lane, so each model call stays
    single-purpose — and an "answer" turn cannot be silently explained at instead of
    being merged."""
    for lane in ("answer", "route_onward"):
        monkeypatch.setattr(IR, "triage", lambda m, f, lane=lane: {"lane": lane,
                                                                   "has_answer": False})
        monkeypatch.setattr(IR, "explain", lambda *a: pytest.fail("must not be called"))
        d = client.post("/api/interview/triage",
                        json={"messages": ASK, "pending_field": "activity"}).json()
        assert d == {"lane": lane, "has_answer": False}


def test_an_unknown_pending_field_is_refused(client):
    """It selects the explainer's framing and gates the deterministic scoping backstop;
    a typo would silently disable both."""
    r = client.post("/api/interview/triage",
                    json={"messages": ASK, "pending_field": "jurisdiciton"})
    assert r.status_code == 400


# ------------------------------------------------------------------ handoff

def test_the_handoff_needs_no_model_at_all(client, monkeypatch):
    """A client that collected every answer by clicking options never needed the
    extractor, and should not have to pay for a model call to compose the scenario."""
    monkeypatch.setattr(IR, "classify",
                        lambda *a: pytest.fail("handoff must not call the model"))
    d = client.post("/api/interview/handoff", json={
        "slots": {"jurisdiction": "Ireland", "activity": "Active Marketing/Selling",
                  "category": "Funds - AIF via NPPR", "marketer": "Other firm",
                  "investor_type": "Professional Client"},
        "original_question": "marketing our AIF into Ireland"}).json()
    assert d["jurisdiction"] == "Ireland"
    assert "national private placement regime" in d["query"]
    assert "Marketing carried out by" in d["scenario"]
    assert d["slots"]["marketer"] == "Other firm"


def test_an_incomplete_scenario_is_refused_rather_than_half_composed(client):
    """A scenario missing the investor type would be handed over as a confident statement
    of facts that were never established."""
    r = client.post("/api/interview/handoff", json={
        "slots": {"jurisdiction": "Jersey", "activity": "Active Marketing/Selling",
                  "category": "Funds - Open-Ended"},
        "original_question": "x"})
    assert r.status_code == 400 and "Investor Type" in r.json()["detail"]


def test_a_scenario_invalid_for_its_own_jurisdiction_is_refused(client):
    """Posted directly, "Funds - Open-Ended" for Ireland is not merely odd — it is not a
    value that regime has, so it is dropped and the scenario is then incomplete."""
    r = client.post("/api/interview/handoff", json={
        "slots": {"jurisdiction": "Ireland", "activity": "Active Marketing/Selling",
                  "category": "Funds - Open-Ended", "marketer": "Other firm",
                  "investor_type": "Professional Client"},
        "original_question": "x"})
    assert r.status_code == 400 and "Product / Service Category" in r.json()["detail"]


def test_general_marketing_hands_off_with_only_the_fields_that_apply(client):
    d = client.post("/api/interview/handoff", json={
        "slots": {"jurisdiction": "Bahamas", "activity": "General Marketing"},
        "original_question": "can we put our logo on a conference banner"}).json()
    assert "Target investors" not in d["scenario"]
    assert d["slots"]["category"] is None


def test_the_section_hints_resolve_to_real_clause_keys(client, monkeypatch):
    """The hint is a list of titles; what a client can act on is a key it can read. The
    resolution is title-matched case-insensitively because the corpus carries both
    "PASSIVE MARKETING (REVERSE-ENQUIRY)" and the title-case form."""
    monkeypatch.setattr(APP, "_interview_sections",
                        lambda p, j, titles: [{"key": "S12", "title": t.upper()}
                                              for t in titles])
    d = client.post("/api/interview/handoff", json={
        "slots": {"jurisdiction": "Jersey", "activity": "Passive Marketing/Selling",
                  "category": "Funds - Open-Ended", "investor_type": "Both"},
        "original_question": "an investor emailed us"}).json()
    assert d["section_hints"] == ["Passive Marketing (Reverse-Enquiry)"]
    assert d["sections"] == [{"key": "S12", "title": "PASSIVE MARKETING (REVERSE-ENQUIRY)"}]


# ------------------------------------------------------------------ limits & gate

def test_every_interview_endpoint_sits_on_the_same_access_gate_as_search():
    """Asserted structurally: locally ACI_AUTH_ENABLED=0 makes every endpoint answer 200
    without a token, so a status-code assertion would test the environment rather than the
    code. What must hold is that these routes carry require_access — the same dependency
    /api/search uses — so that wherever auth IS enabled they refuse an unentitled user."""
    def gate_of(path, method):
        for r in APP.app.routes:
            if getattr(r, "path", None) == path and method in getattr(r, "methods", ()):
                return {d.call for d in r.dependant.dependencies}
        raise AssertionError(f"no route for {method} {path}")

    assert APP.require_access in gate_of("/api/search", "GET"), \
        "the reference gate moved; update this test"
    for path, method in [("/api/interview/schema", "GET"),
                         ("/api/interview/route", "POST"),
                         ("/api/interview/triage", "POST"),
                         ("/api/interview/handoff", "POST")]:
        assert APP.require_access in gate_of(path, method), f"{method} {path} is ungated"


def test_the_transcript_is_bounded(client, stub):
    """The whole transcript goes into a prompt on every turn: unbounded, one long
    conversation costs more per turn than the answer it is working towards."""
    stub(routed())
    long_convo = [{"role": "user", "content": "x"}] * 200
    assert client.post("/api/interview/route",
                       json={"messages": long_convo}).status_code == 422
    assert client.post("/api/interview/route", json={"messages": []}).status_code == 422
    assert client.post("/api/interview/route", json={"messages": [
        {"role": "user", "content": "x" * 5000}]}).status_code == 422
