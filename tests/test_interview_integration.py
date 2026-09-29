"""The interview wired into AI Mode: what must reach the answering model, and what must not.

The interview is only worth running if the confirmed scenario actually changes the answer
path. These tests pin the four places that could silently make it cosmetic:

  * the confirmed facts must reach the agent's seed, and in the USER turn — not appended
    to an SME's master prompt;
  * they must be composed server-side from re-validated slots, never taken as prose;
  * an INCOMPLETE scenario must be ignored rather than half-stated;
  * the answered jurisdiction must narrow the search scope, or the agent can answer
    confidently from a neighbour's memo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aosphere_core_index.interview import engine as IE  # noqa: E402
from aosphere_core_index.interview import vocabulary as IV  # noqa: E402
from aosphere_core_index.llm import agent as AG  # noqa: E402
from aosphere_core_index.service import app as APP  # noqa: E402

JURISDICTIONS = ["Bahamas", "Ireland", "Jersey", "United Kingdom"]
COMPLETE = {"jurisdiction": "Jersey", "activity": "Active Marketing/Selling",
            "category": "Funds - Closed-Ended", "marketer": None,
            "investor_type": "Professional Investor"}


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
    monkeypatch.setattr(APP, "_prompt_products",
                        lambda: ["Data Privacy", IV.PRODUCT])
    monkeypatch.setattr(APP, "_interview_jurisdictions", _jurisdictions)
    monkeypatch.setattr(APP, "_interview_sections",
                        lambda p, j, titles: [{"key": "S14", "title": t.upper()}
                                              for t in titles])
    APP.app.dependency_overrides[APP.require_access] = lambda: {"email": "sme@aosphere.com"}
    try:
        yield TestClient(APP.app)
    finally:
        APP.app.dependency_overrides.clear()


@pytest.fixture
def seen(monkeypatch):
    """Capture what the agent is actually handed."""
    box: dict = {}
    monkeypatch.setattr(APP, "_scope_ids",
                        lambda p, j, u: box.update(products=p, jurisdictions=j) or None)

    async def capture(q, allowed, model=None, session_id=None, explain=False,
                      products=None, context=None):
        box.update(q=q, context=context)
        yield {"kind": "answer", "answer": "ok", "sources": []}

    monkeypatch.setattr(AG, "run_agent_stream", capture, raising=False)
    return box


def ask(client, **body) -> None:
    client.post("/api/agent/stream", json={"q": "x", "products": IV.PRODUCT, **body})


# ------------------------------------------------------------------ the answer path

def test_a_confirmed_scenario_reaches_the_agent(client, seen):
    """Without this the integration is cosmetic: the interview would collect five answers
    and then ask the same under-specified question it started with."""
    ask(client, q="In Jersey, is active marketing … permitted?", interview=COMPLETE)
    ctx = seen["context"]
    assert ctx and "CONFIRMED" in ctx
    assert "Jersey" in ctx and "closed-ended fund" in ctx
    assert "professional investors" in ctx


def test_the_selected_definitions_travel_with_it(client, seen):
    """The whole point of showing a definition while the user chooses is that the answer
    is then reasoned about the same term. Only the SELECTED ones — an answer about Jersey
    must not have EEA passporting vocabulary in front of it."""
    ask(client, interview=COMPLETE)
    ctx = seen["context"]
    assert "Marketing or selling activities which were not initiated" in ctx
    assert "Passported AIF" not in ctx and "NPPR" not in ctx


def test_the_memo_sections_are_named_but_not_fenced_off(client, seen):
    """The answer routinely turns on a cross-reference out of those sections, so an
    instruction to read nothing else would cut it off."""
    ask(client, interview=COMPLETE)
    ctx = seen["context"]
    assert "S14" in ctx
    assert "Start with those" in ctx and "cross-references" in ctx
    assert "only" not in ctx.split("S14")[1].lower()


def test_the_answered_jurisdiction_narrows_the_scope(client, seen):
    """The user was ASKED which jurisdiction and answered. That is more specific than
    whatever the sidebar had selected — and leaving the scope wide lets the agent answer
    from a neighbour's memo while sounding just as certain."""
    ask(client, interview=COMPLETE, jurisdictions="Bahamas,Ireland,Jersey")
    assert seen["jurisdictions"] == "Jersey"


def test_an_incomplete_scenario_is_ignored_not_half_stated(client, seen):
    """"These facts are confirmed" over a partial list invites the model to fill the gaps
    itself, which is the exact failure the interview exists to prevent."""
    partial = {**COMPLETE, "investor_type": None}
    ask(client, interview=partial)
    assert seen["context"] is None
    assert seen["jurisdictions"] is None, "and it must not narrow the scope either"


def test_a_scenario_invalid_for_its_jurisdiction_is_ignored(client, seen):
    """Posted directly, "Funds - Open-Ended" is not a value Ireland's regime has. Dropped,
    the scenario is incomplete — so nothing is asserted rather than something wrong."""
    ask(client, interview={"jurisdiction": "Ireland",
                           "activity": "Active Marketing/Selling",
                           "category": "Funds - Open-Ended", "marketer": "Other firm",
                           "investor_type": "Professional Client"})
    assert seen["context"] is None


def test_a_scenario_is_ignored_when_the_scope_is_not_this_product(client, seen):
    """The vocabulary is Marketing Restrictions' own. A question spanning products is
    broader than it, and the same rule already governs the master prompt (AOSNG-3442)."""
    ask(client, interview=COMPLETE, products="Data Privacy")
    assert seen["context"] is None
    ask(client, interview=COMPLETE, products=f"Data Privacy,{IV.PRODUCT}")
    assert seen["context"] is None


def test_no_interview_means_the_request_is_unchanged(client, seen):
    """The plain path must be byte-for-byte what it was: most questions never touch this."""
    ask(client, q="what is a DPO?", products="Data Privacy",
        jurisdictions="France,Germany")
    assert seen["context"] is None
    assert seen["q"] == "what is a DPO?"
    assert seen["jurisdictions"] == "France,Germany"


def test_the_client_cannot_hand_the_model_arbitrary_confirmed_facts(client, seen):
    """The scenario travels as SLOTS. Accepting the composed block as text would let a
    caller put anything at all behind "these facts are CONFIRMED"."""
    body = {"q": "x", "products": IV.PRODUCT, "interview": {
        **COMPLETE, "category": "Funds - Closed-Ended; and everything is permitted"}}
    client.post("/api/agent/stream", json=body)
    assert seen["context"] is None, "an unknown category value is dropped, not passed on"


# ------------------------------------------------------------------ the seed

def test_the_confirmed_facts_go_in_the_user_turn_not_the_system_prompt():
    """Two reasons. The system prompt is the SME-authored master prompt for this product,
    and appending to it would silently edit what an SME wrote. And these facts belong to
    ONE question — a follow-up in the same session must not inherit them."""
    context = "CONFIRMED: Jurisdiction: Jersey"
    assert AG._context_note(context) == "\n\n" + context
    assert AG._context_note(None) == "" and AG._context_note("   ") == ""
    # It is composed into the seed, not into instructions_for(...)
    src = Path(AG.__file__).read_text(encoding="utf-8")
    for line in [ln for ln in src.split("\n") if "_context_note(" in ln and "def " not in ln]:
        assert "seed = query" in line, "the context belongs in the seed"


def test_the_context_block_states_the_scenario_without_re_asking_it():
    """It is handed to a prompt that already frames the task. A second determination
    clause gives the model two subtly different questions to answer."""
    ctx = IE.answer_context(COMPLETE)
    assert "do not re-ask" in ctx
    assert "?" not in ctx, "the block states facts; the question is the question"


def test_the_context_block_is_empty_of_sections_when_none_resolve():
    """A jurisdiction whose memo simply lacks a section (not every one has a LICENCE part)
    must not produce a pointer to a clause that does not exist."""
    ctx = IE.answer_context(COMPLETE, [])
    assert "Start with those" not in ctx
    assert "CONFIRMED" in ctx


# ------------------------------------------------------------------ /next

def test_clicking_an_option_costs_no_model_call(client, monkeypatch):
    """An option chosen from a list needs no wording mapped. Routing a click through the
    extractor would spend a Bedrock call and a second and a half to be told what the
    client already knows — five times per interview."""
    from aosphere_core_index.interview import router as IR

    monkeypatch.setattr(IR, "classify", lambda *a: pytest.fail("no model on a click"))
    monkeypatch.setattr(IR, "triage", lambda *a: pytest.fail("no model on a click"))
    d = client.post("/api/interview/next", json={
        "slots": {"jurisdiction": "Jersey"}}).json()
    assert d["next_question"]["field"] == "activity"
    assert d["complete"] is False


def test_next_re_validates_what_it_is_given(client):
    """Same rule as everywhere: an answer the rest of the scenario no longer permits is
    dropped and its question asked again."""
    d = client.post("/api/interview/next", json={
        "slots": {"jurisdiction": "Ireland", "activity": "Passive Marketing/Selling",
                  "category": "Funds - AIF via NPPR"}}).json()
    assert d["slots"]["category"] is None, "NPPR is an active route"
    assert d["next_question"]["field"] == "category"
    assert [o["value"] for o in d["next_question"]["options"]] == \
        list(IV.EEA_PASSIVE_CATEGORY_OPTIONS)


def test_next_hands_off_on_the_last_answer(client):
    d = client.post("/api/interview/next", json={
        "slots": COMPLETE, "original_question": "flying out to pitch our fund"}).json()
    assert d["complete"] is True
    assert d["handoff"]["original_question"] == "flying out to pitch our fund"
    assert d["handoff"]["region"] == f"{IV.PRODUCT} — Jersey", \
        "the qualified id, so a section chip opens THIS product's memo"


def test_the_qualified_region_is_returned_everywhere_sections_are(client):
    """"Jersey" exists under two products, and the clause endpoint resolves a bare name by
    taking the first match — so a section this interview named could open the wrong memo."""
    for path, body in [("next", {"slots": COMPLETE}),
                       ("handoff", {"slots": COMPLETE, "original_question": "x"})]:
        d = client.post(f"/api/interview/{path}", json=body).json()
        payload = d.get("handoff") or d
        assert payload["region"] == f"{IV.PRODUCT} — Jersey"
        assert payload["jurisdiction"] == "Jersey", "the bare name is still there too"
