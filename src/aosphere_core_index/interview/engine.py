"""The deterministic half of the interview. Pure — no model, no I/O, no state.

Every decision that changes what the user is asked, or what the answer model is told,
lives here rather than in a prompt. The model's only job is to map wording onto the
controlled values (see `router`); if it also decided the flow, a hallucinated
"jurisdiction: Ireland" could skip the jurisdiction question outright, and the answer
would be confidently about the wrong country.

The two halves meet in `merge`: extracted values are merged over what the client already
holds, and then every cross-field rule below is re-applied. A value that survives the
merge is one the vocabulary actually permits for the rest of the scenario.
"""

from __future__ import annotations

from aosphere_core_index.interview import vocabulary as V

SLOT_KEYS = V.FIELDS
EMPTY_SLOTS: dict = {k: None for k in SLOT_KEYS}
EMPTY_SIGNALS: dict = {"product_kind": None, "category_hint": None, "activity_hint": None}


# ---- what applies, and what comes next ------------------------------------------------

def is_applicable(field: str, slots: dict) -> bool:
    """Whether a field applies at all, given what is known so far.

    An unknown activity is treated as APPLICABLE on purpose: the alternative is deciding
    a field does not apply before knowing the thing that decides it, which silently
    shortens the interview.
    """
    if field in ("jurisdiction", "activity"):
        return True
    activity = slots.get("activity")
    if field == "marketer":
        # Passporting rights turn on the marketer's status, which is only a question
        # inside the EEA and only for active marketing.
        if not V.is_eea(slots.get("jurisdiction")):
            return False
        return activity is None or activity == "Active Marketing/Selling"
    # General Marketing is firm-level promotion: no fund, no service, no audience.
    return activity is None or activity != "General Marketing"


def next_missing(slots: dict) -> str | None:
    """The next applicable field still needing a value, in interview order, or None when
    the scenario is complete."""
    if not slots.get("jurisdiction"):
        return "jurisdiction"
    if not slots.get("activity"):
        return "activity"
    if slots["activity"] != "General Marketing":
        if not slots.get("category"):
            return "category"
        if is_applicable("marketer", slots) and not slots.get("marketer"):
            return "marketer"
        if not slots.get("investor_type"):
            return "investor_type"
    return None


def options_for(field: str, slots: dict,
                jurisdictions: list[str] | None = None) -> tuple[str, ...]:
    """The canonical option list for a field. Nothing is hidden here — options that do
    not apply are returned and reported as excluded (see `exclusions_for`), so the user
    sees WHY an option is unavailable instead of wondering where it went."""
    if field == "jurisdiction":
        return tuple(jurisdictions or ())
    if field == "activity":
        return V.ACTIVITY_OPTIONS
    if field == "category":
        return V.category_options_for(slots.get("jurisdiction"), slots.get("activity"))
    if field == "investor_type":
        return V.investor_type_options_for(slots.get("jurisdiction"))
    if field == "marketer":
        return V.MARKETER_OPTIONS
    return ()


def exclusions_for(field: str, slots: dict, signals: dict) -> dict:
    """Options ruled OUT for this field, each with the reason to show the user.

    Driven by the chosen activity and by the product-kind signal — the two facts that
    make an option incoherent rather than merely unlikely.
    """
    out: dict = {}
    product_kind = signals.get("product_kind")
    if field == "activity":
        if product_kind == "fund":
            out["General Marketing"] = (
                "This question is about a fund. General Marketing is firm- or "
                "brand-level promotion that isn't tied to a specific fund.")
        if product_kind == "service":
            out["Pre-Marketing of Funds"] = (
                "Pre-marketing applies to funds, not to investment management & "
                "advisory services.")
    if field == "category":
        if slots.get("activity") == "Pre-Marketing of Funds":
            out[V.IMAS] = ("Pre-marketing applies to funds, so this service category "
                           "doesn't apply.")
        elif product_kind == "fund":
            out[V.IMAS] = ("This question is about a fund, so this service category "
                           "doesn't apply.")
    return out


def question_for(field: str, slots: dict, signals: dict) -> str:
    """The question text, tailored by a hint when there is one.

    A hint narrows the question to two named options rather than answering it: the user
    still chooses. Category hints speak the open-/closed-ended vocabulary, so they never
    tailor an EEA category question.
    """
    hint = signals.get("category_hint")
    if field == "category" and hint and not V.is_eea(slots.get("jurisdiction")):
        likely = "open-ended" if hint["value"] == "Funds - Open-Ended" else "closed-ended"
        other = "closed-ended" if likely == "open-ended" else "open-ended"
        return f'{hint["note"]} Is it {likely}, or {other}?'
    hint = signals.get("activity_hint")
    if field == "activity" and hint:
        active = hint["value"] == "Active Marketing/Selling"
        likely = "active marketing/selling" if active else "passive marketing/selling"
        other = "passive marketing/selling" if active else "active marketing/selling"
        return f'{hint["note"]} Is it {likely}, or {other}?'
    return V.QUESTION_COPY[field]


# ---- merging ---------------------------------------------------------------------------

def merge_signals(current: dict, extracted: dict) -> dict:
    return {k: (extracted.get(k) if extracted.get(k) is not None else current.get(k))
            for k in EMPTY_SIGNALS}


def merge_slots(current: dict, extracted: dict, signals: dict,
                jurisdictions: list[str] | None = None) -> dict:
    """Merge extracted values over the current ones, then re-apply every consistency rule.

    A non-null extracted value wins; a null LEAVES THE EXISTING VALUE ALONE, because the
    extractor re-reads the whole conversation each turn and a value it fails to re-extract
    (or that came from a button, and so was never in the text) must not be wiped.

    Everything after the merge is invalidation: the interview prefers re-asking a question
    to keeping a value that the rest of the scenario no longer permits.
    """
    merged = {k: (extracted.get(k) if extracted.get(k) is not None else current.get(k))
              for k in SLOT_KEYS}

    # A jurisdiction must be one this index actually holds for the product. An invented
    # one would scope retrieval to nothing and answer from an empty corpus.
    if jurisdictions is not None and merged["jurisdiction"] not in (jurisdictions or ()):
        merged["jurisdiction"] = None

    eea = V.is_eea(merged["jurisdiction"])

    if merged["activity"] == "General Marketing":
        merged["category"] = None
        merged["investor_type"] = None

    # The category vocabulary depends on jurisdiction AND activity, so a value from the
    # other branch cannot survive a switch: "Funds - Open-Ended" is not an answer for
    # Ireland, and "Funds - AIF via NPPR" is not one for passive marketing.
    if merged["category"] and merged["category"] not in V.category_options_for(
            merged["jurisdiction"], merged["activity"]):
        merged["category"] = None

    # Same for investor type, whose LABELS differ between the two branches.
    if merged["investor_type"] and merged["investor_type"] not in \
            V.investor_type_options_for(merged["jurisdiction"]):
        merged["investor_type"] = None

    if not eea or (merged["activity"] is not None
                   and merged["activity"] != "Active Marketing/Selling"):
        merged["marketer"] = None

    # Cross-field rules from the product-kind signal, applied last so they also catch a
    # value the extractor set this turn.
    if signals.get("product_kind") == "fund":
        if merged["category"] == V.IMAS:          # a fund is never a service
            merged["category"] = None
        if merged["activity"] == "General Marketing":
            merged["activity"] = None
    elif signals.get("product_kind") == "service":
        if merged["activity"] == "Pre-Marketing of Funds":   # funds only
            merged["activity"] = None
        # A service IS IMAS, so this one is filled rather than asked — unless the
        # activity is General Marketing, which has no category at all.
        if merged["activity"] != "General Marketing":
            merged["category"] = V.IMAS
    return merged


def acknowledgements(before: dict, after: dict, rationales: dict,
                     signals: dict) -> str | None:
    """A short confirmation of anything newly set this turn, or None.

    Obvious mappings are simply echoed; a non-obvious one carries its one-clause reason.
    This exists because a silently-filled slot is indistinguishable from a slot the tool
    guessed: the user has to be able to see "Jersey" was taken from their own wording.
    """
    items = []
    for f in ("jurisdiction", "activity", "category", "marketer", "investor_type"):
        val = after.get(f)
        if not val or val == before.get(f):
            continue
        reason = rationales.get(f)
        if (not reason and f == "category" and val == V.IMAS
                and signals.get("product_kind") == "service"):
            reason = "this is about an advisory/management service, not a fund"
        items.append((V.FIELD_LABELS[f], val, reason))
    if not items:
        return None

    def line(it) -> str:
        label, val, reason = it
        return f"{label}: {val}" + (f" ({reason})" if reason else "")

    if len(items) == 1:
        return f"Got it — {line(items[0])}."
    return "Got it — I've noted:\n" + "\n".join(f"• {line(i)}" for i in items)


# ---- composing the hand-off -----------------------------------------------------------

_ACTIVITY_PHRASE = {
    "General Marketing": "general marketing",
    "Pre-Marketing of Funds": "pre-marketing of a fund",
    "Passive Marketing/Selling": "passive marketing/selling (reverse solicitation)",
    "Active Marketing/Selling": "active marketing or selling",
}
_CATEGORY_PHRASE = {
    "Funds - Open-Ended": "an open-ended fund",
    "Funds - Closed-Ended": "a closed-ended fund",
    V.IMAS: "investment management & advisory services",
    "Funds - Passported AIF": "an AIF marketed under an AIFMD passport",
    "Funds - Non-passported AIF": "a non-passported AIF",
    "Funds - AIF via NPPR": ("an AIF marketed under the national private placement "
                             "regime (NPPR)"),
    "Funds - AIF to Retail investors": "an AIF marketed to retail investors",
    "Funds - Passported UCITS": "a UCITS fund marketed under a UCITS passport",
    "Funds - Non-passported UCITS": "a non-passported UCITS fund",
    "UCITS fund prior to using UCITS passport": ("a UCITS fund prior to using the UCITS "
                                                 "passport"),
    "AIF prior to using AIFMD passport": "an AIF prior to using the AIFMD passport",
    "AIF prior to making PPR registration/notification": ("an AIF prior to making a PPR "
                                                          "registration/notification"),
}
_MARKETER_PHRASE = {
    "EEA Passporting Entity": "an EEA passporting entity",
    "Other firm": "a firm that is not an EEA passporting entity",
}
_INVESTOR_PHRASE = {
    "Professional Investor": "professional investors",
    "Retail Investor": "retail investors",
    "Professional Client": "professional clients",
    "Retail Client": "retail clients",
    "Both": "both professional and retail investors",
}


def _phrase(table: dict, value: str) -> str:
    return table.get(value, value.lower())


def _applies(field: str, slots: dict) -> bool:
    return bool(is_applicable(field, slots) and slots.get(field))


def compose_query(slots: dict) -> str:
    """A readable permission question composed from the confirmed values — the RETRIEVAL
    wording, in the vocabulary the memo itself uses."""
    activity = slots.get("activity") or ""
    has_category = _applies("category", slots) and activity != "General Marketing"
    # "pre-marketing of a fund" already names the object, so the category clause below
    # would read "pre-marketing of a fund OF an open-ended fund" — the POC's own phrasing
    # table had this. Drop the object when the category is about to supply it.
    phrase = ("pre-marketing" if activity == "Pre-Marketing of Funds" and has_category
              else _phrase(_ACTIVITY_PHRASE, activity))
    q = f'In {slots.get("jurisdiction") or ""}, is {phrase}'
    if has_category:
        q += f' of {_phrase(_CATEGORY_PHRASE, slots["category"])}'
    if _applies("marketer", slots):
        q += f' by {_phrase(_MARKETER_PHRASE, slots["marketer"])}'
    q += " permitted"
    if _applies("investor_type", slots) and slots["investor_type"] != "Unknown":
        q += f', targeting {_phrase(_INVESTOR_PHRASE, slots["investor_type"])}'
    return q + "? If so, what conditions, restrictions or requirements apply?"


def compose_scenario(slots: dict, facets: dict | None = None) -> str:
    """The confirmed facts as a labelled list, NOT as prose and NOT as a second question.

    The answer prompt already says to decide whether the activity is permitted; repeating
    the determination here in different words gives the model two subtly different
    questions to answer. This states only what was confirmed, and only the fields that
    apply — so the model neither has to reconstruct the scenario from a long interview
    transcript nor guess at a field the interview deliberately never asked.
    """
    facets = facets or {}
    lines = [f'- Jurisdiction: {slots.get("jurisdiction") or "—"}',
             f'- Marketing activity: '
             f'{_phrase(_ACTIVITY_PHRASE, slots.get("activity") or "—")}']
    if _applies("category", slots) and slots.get("activity") != "General Marketing":
        lines.append(f'- Product / service: {_phrase(_CATEGORY_PHRASE, slots["category"])}')
    if _applies("marketer", slots):
        lines.append('- Marketing carried out by: '
                     f'{_phrase(_MARKETER_PHRASE, slots["marketer"])}')
    if _applies("investor_type", slots) and slots["investor_type"] != "Unknown":
        lines.append(f'- Target investors: '
                     f'{_phrase(_INVESTOR_PHRASE, slots["investor_type"])}')
    if facets.get("investment_strategy"):
        lines.append(f'- Investment strategy: {facets["investment_strategy"]}')
    if facets.get("fund_structure"):
        lines.append(f'- Fund structure: {facets["fund_structure"]}')
    return "Assess this permitted-activity scenario:\n" + "\n".join(lines)


# ---- routing into the memo's own sections ---------------------------------------------
# NOT from the POC — its corpus was a flat chunk store. Core-index holds the Part B memo
# section by section, and those sections are named after exactly the things the interview
# establishes, consistently across all 88 jurisdictions. So a completed interview names
# the clauses that answer it, and the answer path can be pointed at them instead of
# rediscovering them by similarity. Titles are matched case-insensitively because the
# corpus carries both "PASSIVE MARKETING (REVERSE-ENQUIRY)" and the title-case form.

_ACTIVITY_SECTIONS = {
    "Passive Marketing/Selling": ("Passive Marketing (Reverse-Enquiry)",),
    "Active Marketing/Selling": ("Marketing Activities", "Marketing/Selling to the Public",
                                 "Private Placement Regime"),
    "Pre-Marketing of Funds": ("Marketing Activities", "Private Placement Regime"),
    "General Marketing": ("Marketing Activities",),
}
# The EEA memos carry two sections the non-EEA ones do not, and both bear on a
# passporting or NPPR route.
_EEA_SECTIONS = ("Specific Issues Relating to the Implementation of AIFMD",
                 "Prospectus Regulation (PR3)")
_DISCLAIMER_SECTIONS = {
    "Funds - Open-Ended": ("Disclaimers: Open-Ended Fund",),
    "Funds - Closed-Ended": ("Disclaimers: Closed-Ended Fund",),
    V.IMAS: ("Disclaimers: Investment Management & Advisory Services",),
}
# The router's "other" lane carve-outs, each of which has its own memo section. A
# definitions question is absent on purpose: those are answered from this package's own
# vocabulary, not from the memo.
_OTHER_KIND_SECTIONS = {
    "disclaimers": ("Disclaimers: Other",),
    "penalties": ("Penalties/Sanctions",),
    "licensing": ("Licence",),
    "regulator": ("Sources of Law; Guidance; Competent Regulator",),
}


def section_hints(slots: dict, other_kind: str | None = None) -> tuple[str, ...]:
    """Memo section titles this scenario is answered from, most specific first.

    A HINT, not a filter: retrieval still searches the whole jurisdiction. Narrowing to
    these outright would answer a cross-referenced question from half the memo.
    """
    out: list[str] = []

    def add(titles) -> None:
        for t in titles:
            if t not in out:
                out.append(t)

    if other_kind:
        if other_kind == "disclaimers":
            # The category's OWN disclaimer section first: the memo carries a separate
            # one per category, and "Disclaimers: Other" is the catch-all, not the answer.
            cat = slots.get("category")
            add(_DISCLAIMER_SECTIONS.get(cat, ()) if cat
                else [t for ts in _DISCLAIMER_SECTIONS.values() for t in ts])
        add(_OTHER_KIND_SECTIONS.get(other_kind, ()))
    add(_ACTIVITY_SECTIONS.get(slots.get("activity") or "", ()))
    if V.is_eea(slots.get("jurisdiction")):
        add(_EEA_SECTIONS)
    return tuple(out)


def handoff(slots: dict, original_question: str,
            facets: dict | None = None, other_kind: str | None = None) -> dict:
    """Everything the answer path needs, and nothing it has to re-derive.

    `original_question` is carried through UNCHANGED alongside the composed query: it
    holds the user's own detail (the channel, the fund's strategy, the odd constraint)
    which the controlled vocabulary has no slot for, and which is exactly the nuance a
    definitive answer has to address.
    """
    return {
        "jurisdiction": slots.get("jurisdiction"),
        "original_question": original_question,
        "query": compose_query(slots),
        "scenario": compose_scenario(slots, facets),
        "definitions": V.relevant_definitions_block(slots),
        "section_hints": list(section_hints(slots, other_kind)),
        "slots": {k: slots.get(k) for k in SLOT_KEYS},
    }


def answer_context(slots: dict, sections: list[dict] | None = None) -> str:
    """The confirmed scenario as a block for the ANSWERING model's seed.

    This is what makes the interview worth running. Without it the agent receives only
    `compose_query`'s sentence and has to re-derive the vocabulary from the corpus; with
    it, the scenario is stated as settled fact, the selected terms carry the same meanings
    the user was shown while choosing them, and the memo's own sections are named.

    Composed SERVER-SIDE from validated slots rather than accepted as text from the
    client. The difference matters: the slots are re-checked against the vocabulary on the
    way in, so what reaches the seed is always a scenario the interview could actually
    have produced.

    The section line says "start with" and not "only": the answer routinely turns on a
    cross-reference out of those sections, and an instruction to read nothing else would
    cut it off.
    """
    parts = [
        "The question below has been scoped by a guided interview. These facts are "
        "CONFIRMED — answer for THIS scenario, and do not re-ask or re-open them:",
        "",
        compose_scenario(slots),
    ]
    definitions = V.relevant_definitions_block(slots)
    if definitions:
        parts += ["", definitions]
    if sections:
        refs = ", ".join(f'{s["key"]} ({s["title"]})' for s in sections)
        parts += ["", f"(This jurisdiction's memo covers this in: {refs}. Start with those "
                      f"clauses; follow cross-references out of them as usual.)"]
    return "\n".join(parts)
