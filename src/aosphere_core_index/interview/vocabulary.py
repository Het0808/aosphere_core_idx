"""The interview's controlled vocabulary, and the definitions behind each value.

Ported from the Ask MRAM POC (`lib/scoping-definitions`). The activity and category
definitions are VERBATIM from aosphere's definitions document; entries marked
`drafted` are summaries awaiting review and are flagged in the API so a reviewer can
tell which wording is authoritative. Nothing here is inferred from the corpus.

Two things are deliberately NOT ported from the POC:

  * its six hardcoded jurisdictions. The interview offers whatever the index actually
    holds for the product (88 jurisdictions for Marketing Restrictions), because a
    jurisdiction the tool can answer for but not ask about is a silent gap.
  * its `EEA_JURISDICTIONS = ["Hungary", "Ireland"]`, which was the pilot's two EEA
    entries rather than a definition. The EEA branch now turns on actual membership
    (see EEA_JURISDICTIONS below), so Malta and Norway get the passporting vocabulary
    without a code change and the UK correctly does not.
"""

from __future__ import annotations

# The product this vocabulary belongs to. The interview is product-scoped: these terms
# are Marketing Restrictions' own, and offering them for Data Privacy would be nonsense.
PRODUCT = "Marketing Restrictions - Asset Management"

# ---- fields, in the order they are asked ---------------------------------------------
# Order is fixed and load-bearing: jurisdiction decides the vocabulary of everything
# after it, and activity decides which of the remaining fields apply at all.
FIELDS = ("jurisdiction", "activity", "category", "marketer", "investor_type")

FIELD_LABELS = {
    "jurisdiction": "Jurisdiction",
    "activity": "Marketing Activity",
    "category": "Product / Service Category",
    "marketer": "Who Is Marketing",
    "investor_type": "Investor Type",
}

QUESTION_COPY = {
    "jurisdiction": "Which jurisdiction are you asking about?",
    "activity": "What marketing activity is being carried out?",
    "category": "What is the product or service category?",
    "marketer": ("Who is carrying out the marketing — an EEA passporting entity, "
                 "or another firm?"),
    "investor_type": "Who are the target investors?",
}

# ---- the EEA branch -------------------------------------------------------------------
# EEA = the EU 27 plus Iceland, Liechtenstein and Norway. Spellings are the index's own
# (they must match a jurisdiction name from /api/regions or the branch never triggers);
# members the corpus does not cover as standalone jurisdictions are listed anyway, so
# adding one to the index is not also a code change here.
#
# The UNITED KINGDOM IS NOT IN THIS SET, and that is not an oversight: it left the EEA, so
# it uses the open-/closed-ended vocabulary. Nor are Gibraltar, Guernsey, Jersey, the Isle
# of Man, Monaco or Switzerland — Switzerland being the one most often assumed otherwise,
# as an EFTA member that never joined the EEA.
#
# The corpus's collective "EU Member States" entry is NOT here either. It is not a
# jurisdiction the interview can ask about: its document is the European framework memo
# (sections "Regulatory framework in Europe", "European Legislative Process", "Marketing a
# UCITS Fund", "Marketing an AIF", "Reverse enquiry", "Coming Developments") and carries
# none of the Part B sections the scenario routes to — no PRIVATE PLACEMENT REGIME, no
# LICENCE, no per-category disclaimers. Confirmed as a content decision, 2026-09-11.
EEA_JURISDICTIONS = frozenset({
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic", "Denmark",
    "Estonia", "Finland", "France", "Germany", "Greece", "Hungary", "Ireland", "Italy",
    "Latvia", "Lithuania", "Luxembourg", "Malta", "Netherlands", "Poland", "Portugal",
    "Romania", "Slovakia", "Slovenia", "Spain", "Sweden",
    "Iceland", "Liechtenstein", "Norway",
})


def is_eea(jurisdiction: str | None) -> bool:
    return bool(jurisdiction) and jurisdiction in EEA_JURISDICTIONS


# ---- activity -------------------------------------------------------------------------
ACTIVITY_OPTIONS = (
    "General Marketing",
    "Pre-Marketing of Funds",
    "Passive Marketing/Selling",
    "Active Marketing/Selling",
)

# The only two an extractor may COMMIT. Active vs passive turns entirely on who made the
# first contact, which is seldom stated, so those two are never set from wording — only
# offered as a hint for the user to confirm. See router.INTENT_PROMPT.
DIRECT_ACTIVITIES = ("General Marketing", "Pre-Marketing of Funds")
HINT_ACTIVITIES = ("Active Marketing/Selling", "Passive Marketing/Selling")

# ---- category -------------------------------------------------------------------------
IMAS = "Investment Management & Advisory Services"

NON_EEA_CATEGORY_OPTIONS = ("Funds - Open-Ended", "Funds - Closed-Ended", IMAS)

# The EEA category identifies the fund's ROUTE (passport / NPPR / pre-notification),
# not its redemption structure, so the options depend on the activity.
EEA_PRE_MARKETING_CATEGORY_OPTIONS = (
    "UCITS fund prior to using UCITS passport",
    "AIF prior to using AIFMD passport",
    "AIF prior to making PPR registration/notification",
)
EEA_PASSIVE_CATEGORY_OPTIONS = (
    "Funds - Passported AIF", "Funds - Non-passported AIF",
    "Funds - Passported UCITS", "Funds - Non-passported UCITS", IMAS,
)
EEA_ACTIVE_CATEGORY_OPTIONS = (
    "Funds - Passported AIF", "Funds - AIF via NPPR",
    "Funds - Passported UCITS", "Funds - Non-passported UCITS",
    "Funds - AIF to Retail investors", IMAS,
)

FUND_CATEGORY_HINTS = ("Funds - Open-Ended", "Funds - Closed-Ended")


def _dedup(values) -> tuple[str, ...]:
    out: list[str] = []
    for v in values:
        if v not in out:
            out.append(v)
    return tuple(out)


def eea_category_options_for(activity: str | None) -> tuple[str, ...]:
    """EEA category options for an activity. General Marketing has none (it ends the
    interview); an unknown activity returns the union so a category volunteered early
    is still recognised and can be validated later."""
    if activity == "General Marketing":
        return ()
    if activity == "Pre-Marketing of Funds":
        return EEA_PRE_MARKETING_CATEGORY_OPTIONS
    if activity == "Passive Marketing/Selling":
        return EEA_PASSIVE_CATEGORY_OPTIONS
    if activity == "Active Marketing/Selling":
        return EEA_ACTIVE_CATEGORY_OPTIONS
    return _dedup([*EEA_PRE_MARKETING_CATEGORY_OPTIONS,
                   *EEA_PASSIVE_CATEGORY_OPTIONS,
                   *EEA_ACTIVE_CATEGORY_OPTIONS])


def category_options_for(jurisdiction: str | None,
                         activity: str | None) -> tuple[str, ...]:
    return (eea_category_options_for(activity) if is_eea(jurisdiction)
            else NON_EEA_CATEGORY_OPTIONS)


ALL_CATEGORIES = _dedup([*NON_EEA_CATEGORY_OPTIONS, *eea_category_options_for(None)])

# ---- who is marketing (EEA + active only) ---------------------------------------------
MARKETER_OPTIONS = ("EEA Passporting Entity", "Other firm")

# ---- investor type --------------------------------------------------------------------
# The EEA uses MiFID II's client classification, so the LABELS differ. Keeping one list
# for both would put "Professional Investor" on an Irish answer, which is the wrong term
# of art for that regime.
NON_EEA_INVESTOR_TYPE_OPTIONS = ("Professional Investor", "Retail Investor",
                                 "Both", "Unknown")
EEA_INVESTOR_TYPE_OPTIONS = ("Professional Client", "Retail Client", "Both", "Unknown")


def investor_type_options_for(jurisdiction: str | None) -> tuple[str, ...]:
    return (EEA_INVESTOR_TYPE_OPTIONS if is_eea(jurisdiction)
            else NON_EEA_INVESTOR_TYPE_OPTIONS)


# ---- definitions ----------------------------------------------------------------------
# `doc_term` is the heading the definition appears under in the source document where it
# differs from the option label — the interview says "Active Marketing/Selling", the
# document says "Active Marketing", and a reader checking the source needs the bridge.
# `drafted` marks wording that is ours, not the document's.

ACTIVITY_DEFINITIONS = [
    {"value": "General Marketing", "definition": (
        "Marketing that does not specifically reference a particular Fund or service, "
        "for example: using your company's name or logo; providing general information "
        "about the OFI (e.g. history, business divisions, key persons); handing out "
        "branded items or displaying a banner at an event; publishing articles/giving "
        "interviews (e.g. thought leadership pieces or macroeconomic analysis/market "
        "outlook); raising awareness of product lines without referencing a specific "
        "Fund or service; discussing the characteristics of a Fund or investment "
        "strategy without mentioning a specific Fund or service; attending or "
        "presenting at a finance-related event or speaking on a panel to discuss "
        "general trends and themes rather than specific Funds/IMAS; socialising with "
        "prospective investors; and distributing business cards.")},
    {"value": "Pre-Marketing of Funds", "doc_term": "Pre-marketing", "definition": (
        "Any promotional activity which may be undertaken before triggering the need "
        "for registration or notification with the regulators in a particular "
        "jurisdiction. If you wish to pre-market a Fund cross-border, use this section "
        "to check if such activities are permitted in the target jurisdiction.")},
    {"value": "Passive Marketing/Selling", "doc_term": "Passive Marketing",
     "definition": (
        "Marketing or selling following an unsolicited approach by an investor. May "
        "also be known as “reverse-enquiry” or “reverse-solicitation”.")},
    {"value": "Active Marketing/Selling", "doc_term": "Active Marketing", "definition": (
        "Marketing or selling activities which were not initiated by an unsolicited "
        "investor approach.")},
]

CATEGORY_DEFINITIONS = [
    {"value": "Funds - Open-Ended", "doc_term": "Open-Ended Fund", "definition": (
        "A Fund with or without legal personality which offers an investor the "
        "opportunity to regularly purchase or sell its units or shares.")},
    {"value": "Funds - Closed-Ended", "doc_term": "Closed-Ended Fund", "definition": (
        "A Fund with a fixed number of units or shares which does not offer an investor "
        "the opportunity to regularly purchase or sell its units or shares.")},
    {"value": IMAS, "definition": (
        "Includes Arranging, Execution of Orders on Behalf of Clients, Investment "
        "Advice and Portfolio Management.")},
]

EEA_CATEGORY_DEFINITIONS = [
    {"value": "UCITS fund prior to using UCITS passport", "drafted": True,
     "definition": (
        "Pre-marketing a UCITS fund in the target jurisdiction before the UCITS "
        "marketing passport notification for that jurisdiction has been made.")},
    {"value": "AIF prior to using AIFMD passport", "drafted": True, "definition": (
        "Pre-marketing an AIF by an EEA AIFM before an AIFMD marketing passport "
        "notification covering the target jurisdiction has been made.")},
    {"value": "AIF prior to making PPR registration/notification", "drafted": True,
     "definition": (
        "Pre-marketing an AIF before making the registration or notification required "
        "under the target jurisdiction's private placement regime (where one is "
        "available).")},
    {"value": "Funds - Passported AIF", "doc_term": "Passported AIF", "definition": (
        "An alternative investment fund (AIF) where a notification letter has been "
        "filed with the home Member State regulator and as a result such AIF can be "
        "marketed in other EEA jurisdictions pursuant to the AIFMD marketing passport. "
        "An EEA AIF being marketed by or on behalf of an EEA AIFM to Professional "
        "Investors must use the AIFMD marketing passport. The local national private "
        "placement regimes are therefore not applicable.")},
    {"value": "Funds - Non-passported AIF", "doc_term": "Non-Passported AIF",
     "definition": (
        "An alternative investment fund (AIF) where a notification letter has not been "
        "filed with the home Member State regulator in respect of the host Member State "
        "or Member States that the AIF is being marketed into.")},
    {"value": "Funds - Passported UCITS", "doc_term": "Passported UCITS Fund",
     "definition": (
        "A UCITS Fund where a notification letter has been filed with the home Member "
        "State regulator and as a result such UCITS Fund can be marketed in other EEA "
        "jurisdictions pursuant to the passport.")},
    {"value": "Funds - Non-passported UCITS", "doc_term": "Non-Passported UCITS Fund",
     "definition": (
        "A UCITS Fund where a notification letter has not been filed with the home "
        "Member State regulator in respect of the host Member State or Member States "
        "that the UCITS Fund is being marketed into.")},
    {"value": "Funds - AIF via NPPR", "drafted": True, "definition": (
        "An AIF marketed under the target jurisdiction's national private placement "
        "regime (NPPR), where such a regime is available.")},
    {"value": "Funds - AIF to Retail investors", "drafted": True, "definition": (
        "An AIF intended to be marketed to retail investors in the target jurisdiction, "
        "which typically triggers additional authorisation or approval requirements.")},
]

INVESTOR_TYPE_DEFINITIONS = [
    {"value": "Professional Investor", "drafted": True, "definition": (
        "Institutional, professional, qualified, sophisticated, accredited, "
        "high-net-worth or eligible-counterparty investors — those who meet a "
        "regulatory threshold and need less protection than the general public.")},
    {"value": "Retail Investor", "drafted": True, "definition": (
        "Ordinary, individual, non-professional investors, or members of the general "
        "public, who receive the highest level of regulatory protection.")},
    {"value": "Both", "drafted": True, "definition": (
        "The target investors include both professional and retail investors.")},
    {"value": "Unknown", "drafted": True, "definition": (
        "The investor type has not been determined, but an answer is still wanted. The "
        "analysis is run without committing to a specific investor classification.")},
]

EEA_INVESTOR_TYPE_DEFINITIONS = [
    {"value": "Professional Client", "doc_term": "Professional Investor", "definition": (
        "A “professional client” as defined in Annex II of MiFID II. The "
        "following categories of client are per se professional: (i) investment firms, "
        "credit institutions and insurance companies; (ii) other authorised or regulated "
        "financial institutions; (iii) UCITS Funds and UCITS ManCos; (iv) pension funds "
        "and their management companies; (v) commodity and commodity derivatives "
        "dealers; (vi) locals (own account dealers on derivative exchanges); (vii) other "
        "institutional investors that are authorised or regulated; (viii) large "
        "undertakings meeting two of the following criteria — balance sheet total of "
        "€20 million, net turnover of €40 million, own funds of €2 "
        "million; (ix) national and regional governments, public bodies that manage debt, "
        "Central Banks, and international and supranational institutions such as the "
        "World Bank, the IMF, the ECB and the EIB; and (x) other institutional investors "
        "whose main activity is to invest in MiFID 2 Financial Instruments, including "
        "entities dedicated to the securitisation of assets or other financing "
        "transactions. Clients other than those listed above may be treated as "
        "professional on request where certain criteria are met (such clients are "
        "referred to as elective professional clients).")},
    {"value": "Retail Client", "doc_term": "Retail Investors",
     "definition": "An investor that is not a Professional Client."},
]

MARKETER_DEFINITIONS = [
    {"value": "EEA Passporting Entity", "definition": (
        "A MiFID II Investment Firm, CRD Credit Institution, UCITS ManCo or EEA AIFM "
        "authorised in another EEA Member State with appropriately scoped permissions "
        "and which has exercised its right to passport into the jurisdiction on the "
        "basis of its home Member State authorisation.")},
    {"value": "Other firm", "drafted": True, "definition": (
        "A firm that is not relying on an EEA passport for the activity — for "
        "example a non-EEA (third-country) firm, or an EEA firm without passport rights "
        "covering the activity.")},
]

# Lookups span BOTH vocabularies per field: the values are unique across them (IMAS
# appears only in the non-EEA category list; the MiFID labels only in the EEA investor
# list), so an exact-value lookup is unambiguous and a mid-interview jurisdiction switch
# can still explain the value the user had already chosen.
OPTION_DEFINITIONS = {
    "activity": ACTIVITY_DEFINITIONS,
    "category": [*CATEGORY_DEFINITIONS, *EEA_CATEGORY_DEFINITIONS],
    "investor_type": [*INVESTOR_TYPE_DEFINITIONS, *EEA_INVESTOR_TYPE_DEFINITIONS],
    "marketer": MARKETER_DEFINITIONS,
}


def option_definition(field: str, value: str) -> dict | None:
    """One option's definition, or None. `jurisdiction` has no definitions — its values
    are the index's own jurisdiction names, not controlled terms."""
    for d in OPTION_DEFINITIONS.get(field, ()):
        if d["value"] == value:
            return d
    return None


# Not selectable, but they are how a described scenario gets mapped onto an option
# ("we run segregated managed accounts" -> IMAS), so the explainer must see them.
CONTEXT_TERMS = [
    {"term": "Private Placement", "definition": (
        "Marketing or selling Funds cross-border to investors by targeting a limited "
        "sub-set or number of investors as opposed to via a public offering.")},
    {"term": "Reverse enquiry / reverse solicitation", "definition": (
        "Another name for Passive Marketing/Selling: marketing or selling that follows "
        "an unsolicited approach made by the investor.")},
    {"term": "IMAS (Investment Management & Advisory Services)", "definition": (
        "Includes Arranging, Execution of Orders on Behalf of Clients, Investment "
        "Advice and Portfolio Management.")},
    {"term": "Investment Advice", "definition": (
        "The provision of personal recommendations to a client, either upon its request "
        "or at the initiative of the OFI, in respect of one or more transactions "
        "relating to MiFID 2 Financial Instruments.")},
    {"term": "Arranging", "definition": (
        "Making arrangements for another person to buy, sell or subscribe for a Fund or "
        "Investment Management and Advisory Services or passing execution instructions "
        "to a third party. This may include the activity of reception and transmission "
        "of orders as defined under MiFID 2.")},
    {"term": "Portfolio Management", "definition": (
        "Managing portfolios in accordance with mandates given by clients either "
        "collectively for a Fund or individually on a discretionary client-by-client "
        "basis where such portfolios include one or more MiFID 2 Financial "
        "Instruments.")},
    {"term": "Execution of Orders on Behalf of Clients", "definition": (
        "Agreeing to conclude agreements to buy or sell one or more MiFID 2 Financial "
        "Instruments on behalf of clients.")},
    {"term": "Fund", "definition": (
        "A collective investment undertaking, with or without legal personality, which "
        "raises capital from a number of investors with a view to investing it in "
        "accordance with a defined investment policy for the benefit of those "
        "investors.")},
    {"term": "OFI (Offshore Financial Institution)", "definition": (
        "A bank, asset manager or other offshore financial institution (e.g. "
        "distributor) which is marketing or providing financial products or services and "
        "is headquartered in a different jurisdiction to your jurisdiction.")},
    {"term": "Segregated Managed Account", "definition": (
        "A contractual arrangement between an investment manager and investor where the "
        "investment manager makes discretionary investments into financial instruments "
        "or assets through an account owned by the investor.")},
    {"term": "Single Investor Vehicle", "definition": (
        "A vehicle which makes investments into financial instruments or assets and "
        "which is prevented by law or its constitution from raising capital from more "
        "than one investor.")},
    {"term": "UCITS Fund", "definition": (
        "A Fund which qualifies as an undertaking for collective investment in "
        "transferable securities under the UCITS IV Directive (2009/65/EC) and which has "
        "been authorised as such in an EEA Member State.")},
    {"term": "Private Placement Regime", "definition": (
        "Marketing/selling a fund into an EEA jurisdiction via a national private "
        "placement regime that has been implemented in that jurisdiction. "
        "Marketing/selling via a private placement regime may be an option where a "
        "marketing passport is not available. This will be the case, for example, where "
        "an EEA AIFM is marketing a non-EEA AIF or where a non-EEA AIFM is marketing an "
        "EEA AIF or a non-EEA AIF.")},
    # Added after a live run: asked "it's a hedge fund, what does that mean here?", the
    # explainer answered "a type of Closed-Ended Fund" — inventing a mapping the
    # definitions do not contain, and contradicting the router's own category hint, which
    # says a hedge fund is usually OPEN-ended. Neither term appears in the source
    # document, so the explainer had nothing to ground on and reasoned from the label.
    # These two entries give it the same heuristic the hint uses, in the same hedged
    # wording: a structural clue to be confirmed, never a category on its own.
    {"term": "Hedge fund", "definition": (
        "Not a category in itself: a hedge fund is USUALLY an Open-Ended Fund, because "
        "it normally offers periodic subscriptions and redemptions. It is a clue to the "
        "category, not the answer — the fund's actual structure has to be confirmed.")},
    {"term": "Private equity fund", "definition": (
        "Not a category in itself: a private equity fund is USUALLY a Closed-Ended Fund, "
        "because it normally has a fixed number of units or shares and no regular "
        "redemptions. It is a clue to the category, not the answer — the fund's actual "
        "structure has to be confirmed.")},
    {"term": "Third Country Firm", "definition": (
        "A firm in a third country that would be: (i) a credit institution providing "
        "investment services or performing investment activities; or (ii) an investment "
        "firm, if its head office or registered office were located in a Member "
        "State.")},
]


def _section(title: str, defs) -> str:
    lines = []
    for d in defs:
        alias = (f' (also called "{d["doc_term"]}")'
                 if d.get("doc_term") and d["doc_term"] != d["value"] else "")
        lines.append(f'- {d["value"]}{alias}: {d["definition"]}')
    return title + "\n" + "\n".join(lines)


def definitions_prompt_block(jurisdiction: str | None = None) -> str:
    """The vocabulary as prompt context for the scoping explainer.

    DIVIDED by pathway once the jurisdiction is known, so the model cannot offer EEA
    passporting vocabulary for Jersey (or open-/closed-ended for Ireland). While the
    jurisdiction is still unknown both sets are included, because either could apply.
    """
    eea = is_eea(jurisdiction)
    known = bool(jurisdiction)
    show_non_eea = not eea            # a non-EEA jurisdiction, or none chosen yet
    show_eea = eea or not known

    parts = [_section("MARKETING ACTIVITY options:", ACTIVITY_DEFINITIONS)]
    if show_non_eea:
        parts += ["", _section(
            "CATEGORY options (non-EEA jurisdictions):", CATEGORY_DEFINITIONS)]
    if show_eea:
        parts += ["", _section(
            "CATEGORY options (EEA jurisdictions only; which options are offered "
            "depends on the marketing activity):", EEA_CATEGORY_DEFINITIONS)]
        # Only ever asked for the EEA, so it is omitted entirely elsewhere rather than
        # shown as an option the interview will never reach.
        parts += ["", _section(
            "WHO IS MARKETING options (EEA jurisdictions only, asked for Active "
            "Marketing/Selling):", MARKETER_DEFINITIONS)]
    if show_non_eea:
        parts += ["", _section("INVESTOR TYPE options (non-EEA jurisdictions):",
                               INVESTOR_TYPE_DEFINITIONS)]
    if show_eea:
        # Both/Unknown are shared. When the non-EEA list is also present they are
        # already defined there; when it is not, include them so the EEA pathway is
        # self-contained.
        shared = [d for d in INVESTOR_TYPE_DEFINITIONS
                  if d["value"] in ("Both", "Unknown")]
        defs = (EEA_INVESTOR_TYPE_DEFINITIONS if show_non_eea
                else [*EEA_INVESTOR_TYPE_DEFINITIONS, *shared])
        note = ("Both and Unknown from the list above are also available"
                if show_non_eea else "Both and Unknown are also available")
        parts += ["", _section(
            f"INVESTOR TYPE options (EEA jurisdictions only; {note}):", defs)]
    parts += ["", "CONTEXT TERMS (not selectable options, for mapping a scenario only):",
              "\n".join(f'- {t["term"]}: {t["definition"]}' for t in CONTEXT_TERMS)]
    return "\n".join(parts)


# The IMAS definition is written in terms of these four, so a scenario about IMAS is
# unanswerable without them.
_IMAS_COMPONENTS = ("IMAS (Investment Management & Advisory Services)", "Arranging",
                    "Execution of Orders on Behalf of Clients", "Investment Advice",
                    "Portfolio Management")


def relevant_definitions_block(slots: dict) -> str:
    """Definitions of ONLY the values the user actually chose — the grounding vocabulary
    handed to the answering model.

    Only what was selected, deliberately: the full block includes both pathways' options,
    and an answer that has "Funds - Passported AIF" in front of it while answering about
    Jersey is being invited to reason about a regime that does not apply there.
    """
    lines: list[str] = []
    seen: set[str] = set()

    def push(label: str, field: str, value: str | None) -> None:
        if not value:
            return
        d = option_definition(field, value)
        if d is None or f"{label}:{value}" in seen:
            return
        seen.add(f"{label}:{value}")
        lines.append(f'- {label} — {value}: {d["definition"]}')

    push("Marketing Activity", "activity", slots.get("activity"))
    push("Product / Service Category", "category", slots.get("category"))
    push("Investor Type", "investor_type", slots.get("investor_type"))
    push("Who Is Marketing", "marketer", slots.get("marketer"))

    if slots.get("category") == IMAS:
        for term in _IMAS_COMPONENTS:
            t = next((c for c in CONTEXT_TERMS if c["term"] == term), None)
            if t is None or f"ctx:{term}" in seen:
                continue
            seen.add(f"ctx:{term}")
            lines.append(f'- {t["term"]}: {t["definition"]}')

    if not lines:
        return ""
    return "\n".join([
        "Definitions of the parameters selected for this check (use these as the "
        "meaning of the terms; rely on the source document for the rules):", *lines])


def capability_text(n_jurisdictions: int) -> str:
    """What the interview can do, for the `capability` intent.

    Built from the vocabulary rather than written out, so it cannot drift from the
    questions actually asked — the POC's version hardcoded a jurisdiction list, which
    stopped being true the moment the corpus grew.
    """
    return (
        "This tool answers questions about cross-border marketing and selling "
        "restrictions for investment funds and investment management & advisory "
        "services.\n\n"
        "Ask whether a specific activity is permitted in a given jurisdiction, and I'll "
        "resolve these parameters with you:\n\n"
        f"• Jurisdiction — {n_jurisdictions} jurisdictions are covered\n"
        "• Marketing activity — " + ", ".join(ACTIVITY_OPTIONS) + "\n"
        "• Product / service category — open-ended funds, closed-ended funds, or "
        "investment management & advisory services (for EEA jurisdictions, the fund's "
        "passporting or private-placement status instead)\n"
        "• Investor type — professional or retail (EEA jurisdictions use the MiFID II "
        "labels: professional client or retail client)\n\n"
        "For EEA jurisdictions with active marketing, I'll also ask who is carrying out "
        "the marketing (an EEA passporting entity, or another firm).\n\n"
        'For example: "Can I cold-call professional investors about an open-ended fund '
        'in Jersey?"')
