"""The model half of the interview: what it is allowed to decide, and what it is not.

The router maps wording onto controlled values. Every test here is about a way a model
answer could become a compliance fact it has not earned:

  * active vs passive marketing turns ENTIRELY on who made the first contact, which is
    seldom stated — so it may never be committed, only offered for confirmation;
  * a value outside the vocabulary, or outside this index's jurisdictions, is dropped
    rather than passed on;
  * a hint is advisory and must carry the reason it is a hint;
  * an unparseable reply extracts NOTHING, so the interview asks.

The model is stubbed throughout: these test the contract around it, which is the part
that has to hold when the model is wrong.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from aosphere_core_index.interview import router as R  # noqa: E402
from aosphere_core_index.interview import vocabulary as V  # noqa: E402

JURISDICTIONS = ["Bahamas", "Ireland", "Jersey", "United Kingdom"]
MESSAGES = [{"role": "user", "content": "can I market my fund"}]


@pytest.fixture
def reply(monkeypatch):
    """Stub the one Bedrock call. Returns a setter for the next reply text."""
    box = {"text": "{}", "calls": []}

    def fake(system, user, max_tokens):
        box["calls"].append({"system": system, "user": user, "max_tokens": max_tokens})
        return box["text"]

    monkeypatch.setattr(R, "_converse", fake)
    return box


def _set(reply, **payload):
    reply["text"] = json.dumps(payload)


# ------------------------------------------------------------------ extraction limits

def test_active_and_passive_are_never_committed_only_offered(reply):
    """THE conservative rule. Active vs passive depends on who made the first contact,
    which the wording almost never reveals — and "we're flying out to sign up an
    investor" reveals nothing, however energetic it sounds. Committing it would turn a
    plausible reading into an unconfirmed compliance fact."""
    for value in V.HINT_ACTIVITIES:
        _set(reply, intent="permitted_activity", activity=value)
        out = R.classify(MESSAGES, JURISDICTIONS)
        assert out["slots"]["activity"] is None, f"{value} must not reach the slot"
        # The signal is not thrown away either — it comes back as a hint to confirm.
        hint = out["signals"]["activity_hint"]
        assert hint and hint["value"] == value and hint["note"].strip()


def test_a_fund_structure_the_wording_does_not_state_is_demoted_to_a_hint(reply):
    """Measured, not defensive. Asked "can I market my hedge fund to pension schemes in
    Jersey", Haiku 4.5 committed `Funds - Open-Ended` with the rationale "hedge funds are
    typically open-ended structures" — against the instruction two paragraphs above it in
    its own prompt. Typically is not a compliance fact, and the category decides which
    route through the memo answers the question."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         category="Funds - Open-Ended")
    out = R.classify([{"role": "user", "content": "can I market my hedge fund in Jersey"}],
                     JURISDICTIONS)
    assert out["slots"]["category"] is None, "the user has to confirm the structure"
    hint = out["signals"]["category_hint"]
    assert hint["value"] == "Funds - Open-Ended" and "confirm" in hint["note"]


@pytest.mark.parametrize("wording", ["our open-ended fund", "a closed ended fund",
                                     "marketing a UCITS", "we have an OEIC"])
def test_an_explicitly_stated_structure_passes_through(reply, wording):
    """The rule is about INDIRECT wording. An explicit open-/closed-ended, a UCITS or an
    OEIC is reliable, and demoting those would ask the user to confirm what they just
    said."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         category="Funds - Open-Ended")
    out = R.classify([{"role": "user", "content": f"can I market {wording} in Jersey"}],
                     JURISDICTIONS)
    assert out["slots"]["category"] == "Funds - Open-Ended"
    assert out["signals"]["category_hint"] is None


def test_the_demotion_reads_the_whole_conversation(reply):
    """The structure may have been stated three turns ago, in answer to a question the
    interview asked — demoting it then would loop on the same question forever."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         category="Funds - Closed-Ended")
    out = R.classify([{"role": "user", "content": "can I market my PE fund in Jersey"},
                      {"role": "assistant", "content": "Is it open-ended or closed-ended?"},
                      {"role": "user", "content": "closed-ended"}], JURISDICTIONS)
    assert out["slots"]["category"] == "Funds - Closed-Ended"


def test_a_service_category_is_never_demoted(reply):
    """IMAS is not a fund structure — there is nothing to confirm about redemptions."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         category="Investment Management & Advisory Services")
    out = R.classify([{"role": "user", "content": "we manage client portfolios"}],
                     JURISDICTIONS)
    assert out["slots"]["category"] == "Investment Management & Advisory Services"


def test_only_the_two_direct_activities_can_be_committed(reply):
    for value in V.DIRECT_ACTIVITIES:
        _set(reply, intent="permitted_activity", activity=value)
        out = R.classify(MESSAGES, JURISDICTIONS)
        assert out["slots"]["activity"] == value
        assert out["signals"]["activity_hint"] is None, "a hint excludes a set value"


def test_a_jurisdiction_outside_this_index_is_dropped(reply):
    """The prompt lists the allowed names, but a model can still return a neighbour.
    Retrieval is scoped by this value, so an unindexed one answers from nothing."""
    _set(reply, intent="permitted_activity", jurisdiction="Belgium")
    assert R.classify(MESSAGES, JURISDICTIONS)["slots"]["jurisdiction"] is None
    _set(reply, intent="permitted_activity", jurisdiction="Jersey")
    assert R.classify(MESSAGES, JURISDICTIONS)["slots"]["jurisdiction"] == "Jersey"


def test_the_allowed_jurisdictions_are_injected_not_hardcoded(reply):
    """The POC named its six in the prompt, so it could not have extracted a seventh
    after the corpus grew to 88."""
    _set(reply, intent="permitted_activity")
    R.classify(MESSAGES, JURISDICTIONS)
    system = reply["calls"][0]["system"]
    for j in JURISDICTIONS:
        assert j in system, f"{j} was not offered to the extractor"
    # And the prompt says WHICH of them take the EEA vocabulary, because that decides
    # which category and investor-type lists it may draw on.
    eea_block = R._jurisdiction_context(JURISDICTIONS).rsplit("EEA JURISDICTIONS", 1)[1]
    assert "Ireland" in eea_block
    assert "United Kingdom" not in eea_block and "Jersey" not in eea_block


def test_who_is_marketing_is_ignored_outside_the_eea(reply):
    """It is an EEA-only question, so a value anywhere else is one the interview would
    never have asked for."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         marketer="EEA Passporting Entity")
    assert R.classify(MESSAGES, JURISDICTIONS)["slots"]["marketer"] is None
    _set(reply, intent="permitted_activity", jurisdiction="Ireland",
         marketer="EEA Passporting Entity")
    assert R.classify(MESSAGES, JURISDICTIONS)["slots"]["marketer"] == \
        "EEA Passporting Entity"


def test_the_investor_labels_are_validated_against_the_branch(reply):
    """"Professional Investor" is not an Irish answer and "Professional Client" is not a
    Jersey one — each would be a term of art from the wrong regime."""
    _set(reply, intent="permitted_activity", jurisdiction="Ireland",
         investor_type="Professional Investor")
    assert R.classify(MESSAGES, JURISDICTIONS)["slots"]["investor_type"] is None
    _set(reply, intent="permitted_activity", jurisdiction="Ireland",
         investor_type="Professional Client")
    assert R.classify(MESSAGES, JURISDICTIONS)["slots"]["investor_type"] == \
        "Professional Client"


def test_a_category_hint_is_suppressed_for_an_eea_jurisdiction(reply):
    """Hedge fund -> open-ended is not the EEA vocabulary, so offering it there suggests
    an option that jurisdiction's list does not contain."""
    hint = {"value": "Funds - Open-Ended", "note": "hedge funds are usually open-ended"}
    _set(reply, intent="permitted_activity", jurisdiction="Ireland", category_hint=hint)
    assert R.classify(MESSAGES, JURISDICTIONS)["signals"]["category_hint"] is None
    _set(reply, intent="permitted_activity", jurisdiction="Jersey", category_hint=hint)
    assert R.classify(MESSAGES, JURISDICTIONS)["signals"]["category_hint"] == hint


def test_a_hint_without_a_reason_is_not_a_hint(reply):
    """The note IS the reason to confirm. Without it the user sees a guess with no way to
    tell where it came from."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         category_hint={"value": "Funds - Open-Ended", "note": "  "})
    assert R.classify(MESSAGES, JURISDICTIONS)["signals"]["category_hint"] is None
    _set(reply, intent="permitted_activity",
         activity_hint={"value": "Active Marketing/Selling"})
    assert R.classify(MESSAGES, JURISDICTIONS)["signals"]["activity_hint"] is None


def test_a_rationale_is_kept_only_for_a_value_that_survived(reply):
    """A rationale for a dropped value would explain a choice the user was never shown."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         category="Funds - Open-Ended",
         rationales={"jurisdiction": "not shown, obvious",
                     "category": "a UCITS is reliably open-ended",
                     "marketer": "explains a value that was never set"})
    out = R.classify([{"role": "user", "content": "can I market a UCITS in Jersey"}],
                     JURISDICTIONS)
    assert out["rationales"]["category"] == "a UCITS is reliably open-ended"
    assert "marketer" not in out["rationales"], "the slot was never set"


def test_a_demoted_category_takes_its_rationale_with_it(reply):
    """Same rule, and the case that actually occurred: the model committed a structure
    from "hedge fund" AND explained it. Keeping the explanation for a value that is now
    only a hint would tell the user the tool had decided something it had not."""
    _set(reply, intent="permitted_activity", jurisdiction="Jersey",
         category="Funds - Open-Ended",
         rationales={"category": "hedge funds are typically open-ended structures"})
    out = R.classify([{"role": "user", "content": "can I market my hedge fund in Jersey"}],
                     JURISDICTIONS)
    assert out["slots"]["category"] is None
    assert "category" not in out["rationales"]


def test_an_unparseable_reply_extracts_nothing_rather_than_guessing(reply):
    """Converse has no JSON mode, so the contract is prompt-only. Nothing extracted means
    the interview asks — the safe outcome."""
    for text in ("I'm sorry, I can't help with that.", "", "[]", "{oops"):
        reply["text"] = text
        out = R.classify(MESSAGES, JURISDICTIONS)
        assert all(v is None for v in out["slots"].values())
        assert out["intent"] == "other"


def test_a_fenced_or_prefaced_json_reply_is_still_read(reply):
    """A small model wraps JSON in a code fence often enough that treating it as
    unparseable would throw away good extractions."""
    reply["text"] = '```json\n{"intent": "permitted_activity", "jurisdiction": "Jersey"}\n```'
    assert R.classify(MESSAGES, JURISDICTIONS)["slots"]["jurisdiction"] == "Jersey"
    reply["text"] = 'Here is the JSON:\n{"intent": "capability"}'
    assert R.classify(MESSAGES, JURISDICTIONS)["intent"] == "capability"


def test_an_unknown_intent_falls_to_other_not_to_the_interview(reply):
    """"other" answers the question asked; "permitted_activity" starts a five-question
    interview. Defaulting to the interview on a garbled reply is the more annoying and
    the more presumptuous of the two."""
    _set(reply, intent="permitted-activity")          # near-miss spelling
    assert R.classify(MESSAGES, JURISDICTIONS)["intent"] == "other"


def test_the_carve_out_kind_is_only_returned_for_the_other_lane(reply):
    _set(reply, intent="other", other_kind="disclaimers")
    assert R.classify(MESSAGES, JURISDICTIONS)["other_kind"] == "disclaimers"
    _set(reply, intent="permitted_activity", other_kind="disclaimers")
    assert R.classify(MESSAGES, JURISDICTIONS)["other_kind"] is None
    _set(reply, intent="other", other_kind="something else")
    assert R.classify(MESSAGES, JURISDICTIONS)["other_kind"] is None


def test_the_whole_conversation_is_sent_not_only_the_latest_turn(reply):
    """The jurisdiction may have been named three messages ago. Re-asking for something
    the user already said is the fastest way to make a guided interview feel broken."""
    _set(reply, intent="permitted_activity")
    R.classify([{"role": "user", "content": "we target professional investors"},
                {"role": "assistant", "content": "Which jurisdiction?"},
                {"role": "user", "content": "Jersey"}], JURISDICTIONS)
    sent = reply["calls"][0]["user"]
    assert "professional investors" in sent and "Jersey" in sent


# ------------------------------------------------------------------ triage

def test_a_definition_question_cannot_boot_the_user_out_of_the_interview(reply):
    """The triage model flips between lanes on definitional questions that also sketch a
    scenario. The cost is asymmetric — a wrong "answer"/"route_onward" abandons the
    pending field — so a clear definition question is FORCED into the scoping lane."""
    _set(reply, lane="route_onward")
    out = R.triage([{"role": "user",
                     "content": "what are professional investors? I'm talking to a bank"}],
                   pending_field="investor_type")
    assert out["lane"] == "scoping_question"


def test_the_backstop_does_not_fire_on_a_plain_answer(reply):
    """It needs a question mark AND a definition stem AND one of the interview's own
    terms, so a bare value stays an answer."""
    for latest in ("Jersey?", "professional investors", "an open-ended fund",
                   "I don't know"):
        _set(reply, lane="answer")
        out = R.triage([{"role": "user", "content": latest}],
                       pending_field="investor_type")
        assert out["lane"] == "answer", latest


def test_a_permission_question_still_routes_onward(reply):
    """"Can I …" is the question the answer path exists for; answering it from the
    definitions block would bypass retrieval entirely."""
    _set(reply, lane="route_onward")
    for latest in ("can I cold-call professional investors in Jersey?",
                   "is reverse solicitation permitted in the Bahamas?"):
        out = R.triage([{"role": "user", "content": latest}], pending_field="activity")
        assert out["lane"] == "route_onward", latest


def test_the_backstop_only_applies_while_a_field_is_pending(reply):
    _set(reply, lane="answer")
    out = R.triage([{"role": "user", "content": "what is general marketing?"}],
                   pending_field=None)
    assert out["lane"] == "answer"


def test_a_mixed_turn_keeps_its_stated_value(monkeypatch):
    """"we target professional investors — what are they?" both answers the pending field
    and asks a definition. Tagged as a pure aside, the value is dropped on the floor."""
    calls = []

    def fake(system, user, max_tokens):
        calls.append(system)
        if "triaging" in system:
            return json.dumps({"lane": "answer"})       # wrong lane, forced below
        return json.dumps({"has_answer": True})

    monkeypatch.setattr(R, "_converse", fake)
    out = R.triage([{"role": "user",
                     "content": "we target professional investors — what are they?"}],
                   pending_field="investor_type")
    assert out["lane"] == "scoping_question" and out["has_answer"] is True
    assert len(calls) == 2, "the forced lane judges has_answer with its own call"


def test_a_forced_lane_keeps_the_turn_visible_when_the_judge_is_unavailable(monkeypatch):
    """Value preservation is the safe default: losing something the user said is worse
    than re-reading a turn that stated nothing."""
    def fake(system, user, max_tokens):
        if "triaging" in system:
            return json.dumps({"lane": "answer"})
        raise R.RouterUnavailable("bedrock down")

    monkeypatch.setattr(R, "_converse", fake)
    out = R.triage([{"role": "user", "content": "does an OEIC count as open-ended?"}],
                   pending_field="category")
    assert out["lane"] == "scoping_question" and out["has_answer"] is True


def test_the_explainer_sees_only_the_pathway_that_applies(monkeypatch):
    """It must not offer Jersey a passporting option, nor Ireland an open-ended one:
    both are options that jurisdiction's list does not contain."""
    seen = {}

    def fake(system, user, max_tokens):
        seen["system"] = system
        return "an open-ended fund is one you can regularly buy into."

    monkeypatch.setattr(R, "_converse", fake)
    R.explain("does an OEIC count as open-ended?", "category", "Jersey")
    assert "Open-Ended" in seen["system"] and "Passported AIF" not in seen["system"]
    R.explain("is our AIF passported?", "category", "Ireland")
    assert "Passported AIF" in seen["system"] and "WHO IS MARKETING" in seen["system"]


def test_the_explainer_is_barred_from_deciding_permission(monkeypatch):
    """A verdict reached here would bypass retrieval and citations entirely."""
    seen = {}

    def fake(system, user, max_tokens):
        seen["system"] = system
        return "text"

    monkeypatch.setattr(R, "_converse", fake)
    R.explain("what is pre-marketing?", "activity", None)
    system = seen["system"].lower()
    assert "do not give any regulatory conclusion" in system
    assert "use only the definitions provided" in system


def test_a_dead_model_raises_rather_than_choosing_a_lane(monkeypatch):
    """Every lane sends the user somewhere different. A guessed lane on a dead model
    answers the wrong question convincingly; a 503 asks them to retry."""
    def dead(*a, **k):
        raise R.RouterUnavailable("bedrock down")

    monkeypatch.setattr(R, "_converse", dead)
    with pytest.raises(R.RouterUnavailable):
        R.classify(MESSAGES, JURISDICTIONS)
    with pytest.raises(R.RouterUnavailable):
        R.triage(MESSAGES, "activity")


def test_temperature_is_never_sent_to_bedrock():
    """Asserted on the call itself, not the config helper. Sonnet 5 rejects `temperature`
    outright (see extract/ai_postprocess.py), and this module is one model-id env var away
    from being pointed at it — at which point every interview turn would 503."""
    sent = {}

    class FakeBedrock:
        def converse(self, **kw):
            sent.update(kw)
            return {"output": {"message": {"content": [{"text": "{}"}]}}}

    R._BR = FakeBedrock()
    try:
        R._converse("sys", "user", 128)
    finally:
        R._BR = None
    assert sent["inferenceConfig"] == {"maxTokens": 128}
