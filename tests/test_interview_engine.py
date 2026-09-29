"""The deterministic interview: the rules that decide what the user is asked.

These carry most of the weight for this feature, because the engine is what stops a
model's guess from becoming a compliance fact. The cases below are the ones where a
mistake is INVISIBLE — a question quietly skipped, or a value from the wrong regulatory
vocabulary surviving a jurisdiction switch. Both produce a confident answer to a scenario
the user never described.

`instructions_for`-style discipline: the expected values are computed from
`vocabulary`, never copied, so a change to the option lists cannot leave these passing
against stale text.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from aosphere_core_index.interview import engine as E  # noqa: E402
from aosphere_core_index.interview import vocabulary as V  # noqa: E402

JURISDICTIONS = ["Bahamas", "Ireland", "Jersey", "Malta", "Norway", "United Kingdom"]


def slots(**kw) -> dict:
    return {**E.EMPTY_SLOTS, **kw}


# ------------------------------------------------------------------ the EEA branch

def test_the_eea_branch_is_membership_not_the_pilots_two_jurisdictions():
    """The POC had EEA_JURISDICTIONS = ["Hungary", "Ireland"] — its two pilot entries,
    not a definition. With 88 jurisdictions in the index that under-counts massively, and
    an EEA member answered with open-/closed-ended vocabulary is answered in the wrong
    regime's terms."""
    for member in ("Ireland", "Hungary", "Malta", "Norway", "Iceland", "Liechtenstein",
                   "France", "Luxembourg"):
        assert V.is_eea(member), f"{member} is in the EEA"
    # Not oversights: the UK left the EEA, the Crown Dependencies were never in it, and
    # Switzerland is an EFTA member that never joined — the one most often assumed
    # otherwise.
    for outside in ("United Kingdom", "Jersey", "Guernsey", "Isle of Man", "Gibraltar",
                    "Switzerland", "Monaco", "Bahamas", "Indonesia"):
        assert not V.is_eea(outside), f"{outside} is not in the EEA"


def test_the_collective_eu_entry_is_not_a_jurisdiction_the_interview_can_ask_about():
    """The corpus carries an "EU Member States" entry, but its document is the European
    FRAMEWORK memo — "Regulatory framework in Europe", "European Legislative Process",
    "Marketing a UCITS Fund" — and has none of the Part B sections a confirmed scenario
    routes to. Treating it as an EEA jurisdiction gave it the passporting vocabulary for
    questions its own document cannot answer clause by clause."""
    assert not V.is_eea("EU Member States")


def test_each_branch_gets_its_own_category_vocabulary():
    assert V.category_options_for("Jersey", "Active Marketing/Selling") == \
        V.NON_EEA_CATEGORY_OPTIONS
    assert V.category_options_for("Ireland", "Active Marketing/Selling") == \
        V.EEA_ACTIVE_CATEGORY_OPTIONS
    # The EEA category is a ROUTE, so it changes with the activity; the non-EEA one is a
    # fund STRUCTURE, so it does not.
    assert V.category_options_for("Ireland", "Passive Marketing/Selling") == \
        V.EEA_PASSIVE_CATEGORY_OPTIONS
    assert V.category_options_for("Ireland", "Pre-Marketing of Funds") == \
        V.EEA_PRE_MARKETING_CATEGORY_OPTIONS
    assert "Funds - Open-Ended" not in V.category_options_for("Ireland", None)


def test_investor_labels_follow_the_regime_not_just_the_wording():
    """EEA answers use MiFID II's client classification. "Professional Investor" is the
    wrong term of art for Ireland, and a prompt that used it would be asking the model
    about a classification that regime does not have."""
    assert V.investor_type_options_for("Ireland")[:2] == ("Professional Client",
                                                          "Retail Client")
    assert V.investor_type_options_for("Jersey")[:2] == ("Professional Investor",
                                                         "Retail Investor")
    for shared in ("Both", "Unknown"):
        assert shared in V.investor_type_options_for("Ireland")
        assert shared in V.investor_type_options_for("Jersey")


# ------------------------------------------------------------------ question order

def test_the_questions_are_asked_in_a_fixed_order():
    s = slots()
    assert E.next_missing(s) == "jurisdiction"
    s["jurisdiction"] = "Jersey"
    assert E.next_missing(s) == "activity"
    s["activity"] = "Active Marketing/Selling"
    assert E.next_missing(s) == "category"
    s["category"] = "Funds - Open-Ended"
    assert E.next_missing(s) == "investor_type"
    s["investor_type"] = "Professional Investor"
    assert E.next_missing(s) is None


def test_general_marketing_ends_the_interview_early():
    """It is firm- or brand-level promotion: there is no fund, no service and no
    audience to ask about. Asking anyway would collect values the answer must then
    ignore."""
    s = slots(jurisdiction="Jersey", activity="General Marketing")
    assert E.next_missing(s) is None
    assert not E.is_applicable("category", s)
    assert not E.is_applicable("investor_type", s)


def test_who_is_marketing_is_asked_only_for_eea_active_marketing():
    """Passporting rights turn on the marketer's status, which is only a question inside
    the EEA and only when the firm made the first approach."""
    base = dict(activity="Active Marketing/Selling", category=V.IMAS)
    assert E.next_missing(slots(jurisdiction="Ireland", **base)) == "marketer"
    # Non-EEA: never asked, whatever the activity.
    assert E.next_missing(slots(jurisdiction="Jersey", **base)) == "investor_type"
    # EEA but passive: not asked either.
    assert E.next_missing(slots(jurisdiction="Ireland", activity="Passive Marketing/Selling",
                                category=V.IMAS)) == "investor_type"


def test_an_unknown_activity_leaves_later_fields_applicable():
    """Deciding a field does not apply BEFORE knowing the thing that decides it is how an
    interview silently shortens itself."""
    s = slots(jurisdiction="Ireland")
    assert E.is_applicable("category", s) and E.is_applicable("marketer", s)


# ------------------------------------------------------------------ merging

def test_a_value_the_extractor_fails_to_re_read_is_not_wiped():
    """The extractor re-reads the whole conversation every turn. A value the user set by
    clicking an option was never in the text at all, so a null must mean "no opinion",
    not "cleared"."""
    current = slots(jurisdiction="Jersey", activity="Pre-Marketing of Funds")
    merged = E.merge_slots(current, E.EMPTY_SLOTS, E.EMPTY_SIGNALS, JURISDICTIONS)
    assert merged["jurisdiction"] == "Jersey"
    assert merged["activity"] == "Pre-Marketing of Funds"


def test_switching_between_the_branches_drops_the_other_vocabulary():
    """THE invalidation that matters. "Funds - Open-Ended" is not an answer for Ireland
    and "Professional Investor" is not its investor label — kept, they would be handed to
    the answer model as confirmed facts about a regime that has no such categories."""
    current = slots(jurisdiction="Jersey", activity="Active Marketing/Selling",
                    category="Funds - Open-Ended", investor_type="Professional Investor")
    merged = E.merge_slots(current, {"jurisdiction": "Ireland"}, E.EMPTY_SIGNALS,
                           JURISDICTIONS)
    assert merged["jurisdiction"] == "Ireland"
    assert merged["category"] is None, "the non-EEA structure is not EEA vocabulary"
    assert merged["investor_type"] is None, "EEA uses the MiFID II client labels"
    assert E.next_missing(merged) == "category", "so the question is asked again"


def test_leaving_active_marketing_clears_who_is_marketing():
    current = slots(jurisdiction="Ireland", activity="Active Marketing/Selling",
                    category=V.IMAS, marketer="EEA Passporting Entity")
    merged = E.merge_slots(current, {"activity": "Passive Marketing/Selling"},
                           E.EMPTY_SIGNALS, JURISDICTIONS)
    assert merged["marketer"] is None
    # And so does leaving the EEA.
    left_eea = E.merge_slots(current, {"jurisdiction": "Jersey"}, E.EMPTY_SIGNALS,
                             JURISDICTIONS)
    assert left_eea["marketer"] is None


def test_switching_to_general_marketing_clears_the_fields_it_does_not_have():
    current = slots(jurisdiction="Jersey", activity="Active Marketing/Selling",
                    category="Funds - Open-Ended", investor_type="Retail Investor")
    merged = E.merge_slots(current, {"activity": "General Marketing"}, E.EMPTY_SIGNALS,
                           JURISDICTIONS)
    assert merged["category"] is None and merged["investor_type"] is None


def test_an_eea_category_from_the_wrong_activity_is_dropped():
    """The EEA lists are per-activity: NPPR is an ACTIVE route and is not offered for
    passive marketing, so it cannot survive a switch to passive."""
    current = slots(jurisdiction="Ireland", activity="Active Marketing/Selling",
                    category="Funds - AIF via NPPR")
    merged = E.merge_slots(current, {"activity": "Passive Marketing/Selling"},
                           E.EMPTY_SIGNALS, JURISDICTIONS)
    assert merged["category"] is None


def test_a_jurisdiction_this_index_does_not_hold_is_refused():
    """An invented jurisdiction scopes retrieval to nothing, and an answer from an empty
    corpus is the worst failure this feature can have."""
    merged = E.merge_slots(slots(), {"jurisdiction": "Atlantis"}, E.EMPTY_SIGNALS,
                           JURISDICTIONS)
    assert merged["jurisdiction"] is None
    assert E.next_missing(merged) == "jurisdiction"


def test_the_product_kind_signal_rules_out_incoherent_combinations():
    fund = {**E.EMPTY_SIGNALS, "product_kind": "fund"}
    merged = E.merge_slots(slots(jurisdiction="Jersey"),
                           {"activity": "General Marketing", "category": V.IMAS},
                           fund, JURISDICTIONS)
    assert merged["category"] is None, "a fund is never an advisory service"
    assert merged["activity"] is None, "naming a fund rules out firm-level promotion"

    service = {**E.EMPTY_SIGNALS, "product_kind": "service"}
    merged = E.merge_slots(slots(jurisdiction="Jersey"),
                           {"activity": "Pre-Marketing of Funds"}, service, JURISDICTIONS)
    assert merged["activity"] is None, "pre-marketing is a fund-only concept"
    assert merged["category"] == V.IMAS, "a service IS IMAS — filled, not asked"


def test_the_exclusions_carry_the_reason_to_show_the_user():
    """An option that silently vanishes reads as a bug; one greyed out with a reason
    teaches the vocabulary."""
    fund = {**E.EMPTY_SIGNALS, "product_kind": "fund"}
    ex = E.exclusions_for("activity", slots(jurisdiction="Jersey"), fund)
    assert "General Marketing" in ex and ex["General Marketing"].strip()
    ex = E.exclusions_for("category", slots(activity="Pre-Marketing of Funds"),
                          E.EMPTY_SIGNALS)
    assert V.IMAS in ex and "Pre-marketing" in ex[V.IMAS]


def test_a_hint_narrows_the_question_without_answering_it():
    """A hedge fund is USUALLY open-ended — usually is not a compliance fact, so the hint
    re-asks with both options named rather than filling the slot."""
    signals = {**E.EMPTY_SIGNALS, "category_hint": {
        "value": "Funds - Open-Ended", "note": "You mentioned a hedge fund, which is "
                                               "usually open-ended."}}
    q = E.question_for("category", slots(jurisdiction="Jersey"), signals)
    assert "open-ended" in q and "closed-ended" in q
    # The open/closed vocabulary is not the EEA's, so it must not tailor an EEA question.
    assert E.question_for("category", slots(jurisdiction="Ireland"), signals) == \
        V.QUESTION_COPY["category"]


# ------------------------------------------------------------------ the hand-off

def test_the_scenario_states_the_facts_and_does_not_re_ask_the_question():
    """The answer prompt already frames the task. A second determination clause here gives
    the model two subtly different questions to answer."""
    s = slots(jurisdiction="Jersey", activity="Active Marketing/Selling",
              category="Funds - Closed-Ended", investor_type="Professional Investor")
    scenario = E.compose_scenario(s)
    assert scenario.startswith("Assess this permitted-activity scenario:")
    for banned in ("is it permitted", "permitted?", "?"):
        assert banned not in scenario.lower(), "the scenario states, it does not ask"
    for fact in ("Jersey", "closed-ended fund", "professional investors"):
        assert fact in scenario


def test_the_pre_marketing_query_does_not_name_the_fund_twice():
    """"pre-marketing of a fund" already names the object, so appending the category read
    "pre-marketing of a fund OF an open-ended fund" — the POC's phrasing table had the same
    bug. This is the RETRIEVAL wording, so it is what the corpus is matched against."""
    s = slots(jurisdiction="Jersey", activity="Pre-Marketing of Funds",
              category="Funds - Open-Ended", investor_type="Professional Investor")
    q = E.compose_query(s)
    assert "of a fund of" not in q
    assert "is pre-marketing of an open-ended fund permitted" in q
    # The scenario is a labelled list, not a sentence, so it keeps the fuller phrase.
    assert "pre-marketing of a fund" in E.compose_scenario(s)
    # And an activity with no category clause still reads correctly.
    assert "is general marketing permitted" in E.compose_query(
        slots(jurisdiction="Jersey", activity="General Marketing"))


def test_the_scenario_omits_fields_the_interview_never_asked():
    """General Marketing has no category or investor type. Emitting "—" or a stale
    value would invite the model to reason about an audience the user never named."""
    s = slots(jurisdiction="Bahamas", activity="General Marketing")
    scenario = E.compose_scenario(s)
    assert "Product / service" not in scenario and "Target investors" not in scenario


def test_an_unknown_investor_type_is_left_out_rather_than_asserted():
    s = slots(jurisdiction="Jersey", activity="Active Marketing/Selling",
              category=V.IMAS, investor_type="Unknown")
    assert "Target investors" not in E.compose_scenario(s)
    assert "targeting" not in E.compose_query(s)


def test_the_definitions_handed_over_are_only_the_selected_ones():
    """The full vocabulary contains both branches. An answer about Jersey with
    "Funds - Passported AIF" in front of it is being invited to reason about a regime
    that does not apply there."""
    s = slots(jurisdiction="Jersey", activity="Passive Marketing/Selling",
              category="Funds - Open-Ended", investor_type="Both")
    block = V.relevant_definitions_block(s)
    assert "Passive Marketing/Selling" in block and "Open-Ended" in block
    assert "Passported AIF" not in block and "NPPR" not in block


def test_imas_carries_its_component_definitions():
    """The IMAS definition is written in terms of four other terms, so an IMAS scenario is
    unanswerable without them."""
    block = V.relevant_definitions_block(slots(category=V.IMAS))
    for component in ("Arranging", "Investment Advice", "Portfolio Management",
                      "Execution of Orders on Behalf of Clients"):
        assert component in block


def test_the_original_question_survives_the_hand_off():
    """The controlled vocabulary has no slot for the channel, the strategy or the odd
    constraint — which is exactly the nuance a definitive answer has to address."""
    original = "we're flying out to Jersey to pitch our credit fund at a roadshow"
    h = E.handoff(slots(jurisdiction="Jersey", activity="Active Marketing/Selling",
                        category="Funds - Closed-Ended",
                        investor_type="Professional Investor"), original)
    assert h["original_question"] == original
    assert h["query"] != original and "Jersey" in h["query"]


def test_the_scenario_routes_to_the_memo_sections_that_answer_it():
    """Core-index holds the Part B memo section by section, named after the very things
    the interview establishes. A completed interview therefore NAMES the clauses."""
    passive = E.section_hints(slots(jurisdiction="Jersey",
                                    activity="Passive Marketing/Selling"))
    assert passive == ("Passive Marketing (Reverse-Enquiry)",)
    active = E.section_hints(slots(jurisdiction="Jersey",
                                   activity="Active Marketing/Selling"))
    assert "Private Placement Regime" in active and "Marketing Activities" in active
    # The EEA memos carry two sections the others do not, both bearing on a passport or
    # NPPR route.
    eea = E.section_hints(slots(jurisdiction="Ireland",
                                activity="Active Marketing/Selling"))
    assert any("AIFMD" in t for t in eea) and any("Prospectus" in t for t in eea)
    assert not any("AIFMD" in t for t in active)


def test_a_carve_out_question_routes_to_its_own_section_most_specific_first():
    """"What disclaimers apply?" is not a permission question, and the memo answers it in
    its own section — the one for THAT category, before the catch-all."""
    hints = E.section_hints(slots(jurisdiction="Jersey",
                                  activity="Active Marketing/Selling",
                                  category="Funds - Closed-Ended"),
                            other_kind="disclaimers")
    assert hints[0] == "Disclaimers: Closed-Ended Fund"
    assert "Disclaimers: Other" in hints
    assert E.section_hints(slots(jurisdiction="Jersey"), other_kind="penalties")[0] == \
        "Penalties/Sanctions"
    # A definitions question is answered from this package's vocabulary, not the memo.
    assert E.section_hints(slots(jurisdiction="Jersey"), other_kind="definitions") == ()


def test_acknowledgements_explain_a_non_obvious_mapping_and_stay_quiet_otherwise():
    """A silently-filled slot is indistinguishable from a guess."""
    before = slots()
    after = slots(jurisdiction="Jersey", activity="Pre-Marketing of Funds")
    msg = E.acknowledgements(before, after, {"activity": "sounding out investors "
                                                         "before launch"}, E.EMPTY_SIGNALS)
    assert "Jersey" in msg and "sounding out investors before launch" in msg
    assert E.acknowledgements(after, after, {}, E.EMPTY_SIGNALS) is None


def test_the_service_to_imas_rule_explains_itself():
    """It is the one value the interview fills without asking, so it is the one that most
    needs to say why."""
    signals = {**E.EMPTY_SIGNALS, "product_kind": "service"}
    msg = E.acknowledgements(slots(), slots(category=V.IMAS), {}, signals)
    assert "service" in msg and "not a fund" in msg


@pytest.mark.parametrize("field", V.FIELDS)
def test_every_field_has_a_question_and_options(field):
    s = slots(jurisdiction="Ireland", activity="Active Marketing/Selling")
    assert E.question_for(field, s, E.EMPTY_SIGNALS).strip()
    assert E.options_for(field, s, JURISDICTIONS), f"{field} has no options"
