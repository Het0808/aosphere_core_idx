"""The guided interview in the AI Mode chat: what the screen must not get wrong.

Source-level assertions on web.py, in the style this repo already uses for that file.
They pin the handful of properties where a mistake would be either invisible or costly:

  * the interview must not hijack a question it does not apply to;
  * choosing from a list must not cost a model call;
  * a free-text turn must be triaged before it is treated as an answer, and a useful aside
    must not throw the collected answers away;
  * the confirmed scenario must be shown before an authoritative-looking answer is
    generated from it.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src/aosphere_core_index/service/web.py"
RAW = WEB.read_text(encoding="utf-8")


def _no_comments(src: str) -> str:
    return "\n".join("" if ln.lstrip().startswith(("//", "#")) else ln
                     for ln in src.split("\n"))


SRC = _no_comments(RAW)


def body_of(fn: str) -> str:
    i = SRC.index(f"function {fn}(")
    return SRC[i:SRC.find("\n}", i)]


def test_the_interview_runs_outright_only_for_this_product_alone():
    """Same rule as the master prompt (AOSNG-3442): exactly ONE product in scope. A
    question spanning products is broader than this vocabulary, so it is not silently run
    through an interview about funds — it is asked about first (below)."""
    b = body_of("ivApplies")
    assert "enabledP.size===1" in b and "IV_PRODUCT" in b


def test_an_ambiguous_product_scope_asks_instead_of_staying_silent():
    """The filter starts with EVERY product selected, so gating on "exactly Marketing
    Restrictions" left the interview invisible by DEFAULT: a user asking "can I market my
    fund in Jersey" got the hedging answer and never learned a guided scoping existed."""
    assert "enabledP.size>1" in body_of("ivAmbiguous")
    b = body_of("sendChat")
    assert "ivApplies() || ivAmbiguous()" in b


def test_the_product_question_is_only_asked_for_a_marketing_question():
    """Otherwise "what is a DPO?", asked with the default filter, would be interrupted to
    choose a product it has nothing to do with."""
    b = body_of("ivStart")
    assert 'd.intent!=="permitted_activity"' in b
    assert "streamAnswer(q, null, null)" in b, "anything else takes the plain path"
    assert "ivAskProduct(d)" in b


def test_choosing_the_product_costs_no_second_model_call():
    """The classification that decided to ask is kept and reused — the answer to "which
    product?" does not change what the question says."""
    b = body_of("ivAskProduct")
    assert "ivApply(routed)" in b
    assert 'ivPost("route"' not in b


def test_choosing_a_product_narrows_the_sidebar_too():
    """So the rest of the session — the answer's scope, the master prompt, a follow-up —
    agrees with what was just chosen in the chat."""
    b = body_of("ivPickProduct")
    assert "enabledP=new Set([name])" in b
    assert "renderTree()" in b and "updateRbtn()" in b


def test_choosing_another_product_answers_plainly():
    b = body_of("ivAskProduct")
    assert "name!==IV_PRODUCT" in b and "ivReset()" in b
    assert "streamAnswer(_ivQuestion, null, null)" in b


def test_the_product_name_is_not_duplicated_in_the_page():
    """It is a storage key, a filter value and an API argument. Two spellings is one
    silent mismatch away from an interview that never triggers."""
    assert RAW.count('"Marketing Restrictions - Asset Management"') == 1, \
        "declare it once (IV_PRODUCT) and reference it"


def test_a_selected_region_is_never_asked_about_again():
    """Reported from the first real use. Selecting a region in the sidebar is the same
    statement as answering "which jurisdiction?" — asking again asks the user to repeat
    themselves."""
    b = body_of("ivStart")
    assert "ivSyncFilter()" in b
    assert "jurisdiction:pinned" in b.replace(" ", "")
    assert "from your jurisdiction filter" in b, \
        "and it must SAY where the value came from — the server only acknowledges what " \
        "the extractor set, so this one would appear out of nowhere"
    # ONE selection is an answer; several is a narrower question, not a skip.
    assert "enabledJ.size===1" in body_of("ivPinnedJ")


def test_the_filter_travels_on_every_interview_call():
    """It narrows both the options offered and what the extractor may return, so the two
    cannot disagree."""
    assert "jurisdictions:ivFilterJ()" in body_of("ivPost")
    f = body_of("ivFilterJ")
    # Nothing selected is the unrestricted state, and selecting every jurisdiction the
    # product covers is the same statement -- updateRbtn labels both "All jurisdictions".
    # Neither may send a filter. (Before the filter redesign the default was everything
    # selected and the ceiling was a flat ALLJ; it is now per-product.)
    assert "enabledJ.size&&enabledJ.size<mj" in f.replace(" ", ""), \
        "everything selected means no filter, and neither does nothing selected"
    assert "prodJurisdictions(selectedProduct)" in f, \
        "the ceiling is what THIS product covers, not every jurisdiction in the index"


def test_moving_the_filter_to_another_region_discards_the_scenario():
    """What was collected describes a scenario in a jurisdiction the user is no longer
    asking about."""
    b = body_of("ivSyncFilter")
    assert "pinned!==_ivSlots.jurisdiction" in b and "ivReset()" in b
    assert "ivSyncFilter()" in body_of("sendChat"), "checked on every turn"


def test_a_clicked_option_advances_without_the_extractor():
    """Choosing from a list needs no wording mapped. Routing a click through /route would
    spend a Bedrock call and ~1.5s per step to be told what the page already knows."""
    b = body_of("ivPick")
    assert 'ivPost("next"' in b
    assert '"route"' not in b


def test_free_text_is_triaged_before_it_is_treated_as_an_answer():
    """The three lanes need three handlings, and mis-laning is what loses the user's
    place in the interview."""
    b = body_of("ivTyped")
    assert 'ivPost("triage"' in b
    assert 'lane==="scoping_question"' in b and 'lane==="route_onward"' in b


def test_an_aside_parks_the_interview_instead_of_abandoning_it():
    """Losing five collected answers to a useful aside is the thing that makes a guided
    flow untrustworthy — the POC's own "pick up where you left off"."""
    b = body_of("ivTyped")
    assert "_ivSuspended={" in b, "the slots are saved before the aside is answered"
    assert "ivresume" in b
    r = body_of("ivResume")
    assert "_ivSlots=_ivSuspended.slots" in r and "_ivSuspended=null" in r


def test_a_scoping_aside_leaves_the_answers_untouched_and_re_asks():
    """A question about what a term means is not an answer to the pending field."""
    b = body_of("ivTyped")
    scoping = b[b.index('lane==="scoping_question"'):b.index('lane==="route_onward"')]
    assert "_ivSlots" not in scoping, "a scoping aside must not write a slot"
    assert "ivReask" in scoping
    # …unless it ALSO stated a value, which must still reach the extractor.
    assert "hasAnswer" in scoping and "ivExtract" in scoping


def test_re_asking_costs_no_model_call():
    """The answers have not changed, so there is nothing to re-extract."""
    assert 'ivPost("next"' in body_of("ivReask")


def test_the_confirmed_scenario_is_shown_before_the_answer_is_generated():
    """The last chance to catch a wrong answer before an authoritative-looking reply is
    built on it — which is why the slots are echoed rather than only used."""
    b = body_of("ivAnswer")
    assert b.index("ivScenarioCard") < b.index("streamAnswer"), \
        "the scenario must be on screen before the stream starts"
    card = body_of("ivScenarioCard")
    assert "ivedit" in card and "ivRestart" in card, "and it must be correctable"


def test_the_scenario_travels_as_slots_not_as_prose():
    """The server re-validates them and composes the confirmed-facts block itself, so the
    page cannot put arbitrary text behind "these facts are CONFIRMED"."""
    b = body_of("ivAnswer")
    assert "h.slots" in b
    for composed in ("scenario", "definitions", "answer_context"):
        assert composed not in b, f"the page must not send a composed {composed}"
    stream = body_of("streamAnswer")
    assert "body.interview=interview" in stream


def test_the_answered_jurisdiction_overrides_the_sidebar_filter():
    """The user was asked and answered; that is more specific than whatever was selected.
    The server does the same with it, so the two cannot disagree."""
    assert "body.jurisdictions=jurisdiction" in body_of("streamAnswer")


def test_a_dead_router_still_answers_the_question():
    """The router being down is not the answer path being down. Stranding the user in a
    dead interview would make a scoping failure look like a total outage."""
    b = body_of("ivStart")
    assert "r.status===503" in b and "streamAnswer(q, null, null)" in b
    assert "catch" in b and b.count("streamAnswer") >= 2, "and the same on any failure"


def test_a_ruled_out_option_is_shown_with_its_reason():
    """An option that silently vanishes reads as a bug; one greyed out with a reason
    teaches the vocabulary."""
    b = body_of("ivRenderQuestion")
    assert "excluded_reason" in b and "ivopt out" in b.replace('"', "").replace("${out}", " out")
    assert "disabled" in b
    assert "ivwhy" in b, "the reason is rendered, not only a tooltip"
    assert ".filter(o=>!o.excluded_reason)" not in b, "excluded options must not be dropped"


def test_the_question_text_comes_from_the_server_not_a_local_table():
    """The activity question arrives pre-tailored ("…Is it active marketing/selling, or
    passive?") when the wording gave a signal. A local string table would silently discard
    that and re-ask the generic question."""
    b = body_of("ivRenderQuestion")
    assert "esc(q.question)" in b
    assert "QUESTION_COPY" not in SRC, "the page must not carry its own copy of the questions"


def test_option_values_ride_in_data_attributes_not_inline_handlers():
    """Values carry spaces, ampersands and slashes — "Investment Management & Advisory
    Services", "Active Marketing/Selling". An inline onclick with such a value terminates
    the attribute and silently kills the handler."""
    b = body_of("ivRenderQuestion")
    assert "data-ivval=" in b and "escA(o.value)" in b
    assert "onclick=" not in b


def test_the_interview_state_is_cleared_when_the_scope_moves_away():
    """A half-collected scenario belongs to a question that is no longer being asked."""
    b = body_of("sendChat")
    assert "ivReset()" in b
    assert b.index("ivApplies()") < b.index("ivReset()")


def test_a_section_chip_opens_this_product_s_memo():
    """"Jersey" exists under two products and the clause endpoint resolves a bare name by
    taking the first match, so the qualified id is what must be used."""
    b = body_of("ivScenarioCard")
    assert "h.region" in b
    assert re.search(r"openClause\(region", b), "not the bare jurisdiction name"


def test_the_interview_never_writes_a_slot_the_server_did_not_return():
    """Every merge is the server's: it applies the invalidation rules (an EEA switch drops
    a non-EEA category, and so on). A page that kept its own merged copy would drift."""
    b = body_of("ivApply")
    assert "_ivSlots=d.slots" in b


def test_capability_and_general_lanes_do_not_start_an_interview():
    """A question about the tool, or an informational carve-out, is not five questions."""
    b = body_of("ivApply")
    assert "d.capability" in b and "_ivPending=null" in b
    assert "d.general" in b and "needs_jurisdiction" in b
