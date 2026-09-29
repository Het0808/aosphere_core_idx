"""The model half of the interview: what is being asked, and what was already said.

Two single-purpose calls, never combined:

  * `classify` — the intent lane plus whole-conversation slot extraction. Run on the
    first turn and on any turn that answers a question.
  * `triage` (+ `explain`) — what a free-text turn IS while a question is pending: an
    answer, a question about the vocabulary, or a genuine question to answer elsewhere.

Both are prompt-and-parse only. Neither decides the interview's shape, neither writes a
regulatory conclusion, and everything they return is re-validated by `engine` against the
controlled vocabulary before it reaches the user.

Bedrock Converse in the EU (same region as the rest of the service, so the conversation
never leaves it). Deliberately a small model: this is synonym mapping and classification,
not legal reasoning. `temperature` is not sent to ANY model — see the note in
extract/ai_postprocess.py: Sonnet 5 rejects it outright, and one config every model
accepts cannot drift out of step with the model list.
"""

from __future__ import annotations

import json
import logging
import os
import re

from aosphere_core_index.interview import vocabulary as V

log = logging.getLogger(__name__)

_REGION = os.getenv("ACI_INTERVIEW_REGION") or os.getenv("ACI_BEDROCK_REGION") or "eu-west-1"
_MODEL = os.getenv("ACI_INTERVIEW_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
_TIMEOUT = float(os.getenv("ACI_INTERVIEW_TIMEOUT", "45"))

_BR = None


class RouterUnavailable(RuntimeError):
    """The model could not be reached. Raised rather than defaulted: every lane this
    module chooses sends the user somewhere different, and a guessed lane on a dead
    model is worse than a retry — it answers the wrong question convincingly."""


def _bedrock():
    global _BR
    if _BR is None:
        import boto3
        from botocore.config import Config

        _BR = boto3.client(
            "bedrock-runtime", region_name=_REGION,
            config=Config(retries={"max_attempts": 4, "mode": "adaptive"},
                          read_timeout=_TIMEOUT))
    return _BR


def _converse(system: str, user: str, max_tokens: int) -> str:
    try:
        resp = _bedrock().converse(
            modelId=_MODEL,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens},
        )
        return resp["output"]["message"]["content"][0]["text"]
    except Exception as e:  # noqa: BLE001
        log.warning("interview router call failed (%s: %s)", type(e).__name__, str(e)[:200])
        raise RouterUnavailable(str(e)[:200]) from e


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _json_object(text: str) -> dict:
    """Parse the model's JSON leniently.

    Converse has no `response_format`, so the contract is prompt-only and the reply can
    arrive fenced or with a sentence in front of it. Falling back to the outermost
    braces recovers those; a genuinely unparseable reply becomes {}, which every caller
    reads as "nothing extracted" — the interview then asks, which is the safe outcome.
    """
    s = _FENCE.sub("", (text or "").strip())
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j <= i:
            return {}
        try:
            d = json.loads(s[i:j + 1])
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


def _pick(value, allowed) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _hint(raw, allowed) -> dict | None:
    """A {value, note} hint, or None. A hint must name an allowed value AND carry a note:
    it is shown to the user as the reason to confirm, and a note-less hint would present
    a guess as a question with no way to see where it came from."""
    if not isinstance(raw, dict):
        return None
    value = _pick(raw.get("value"), allowed)
    note = raw.get("note") if isinstance(raw.get("note"), str) else ""
    note = note.strip()
    return {"value": value, "note": note} if value and note else None


# Wording that RELIABLY states a fund's structure, per the extraction rules: an explicit
# open-/closed-ended, or a UCITS/OEIC (both reliably open-ended). Everything else — hedge
# fund, private equity, SICAV, unit trust, investment trust — is a clue and must be
# confirmed, so a committed category with none of these behind it is demoted to a hint by
# `_demote_category` below.
_STRUCTURE_STATED = re.compile(
    r"\bopen[- ]?end(ed)?\b|\bclosed[- ]?end(ed)?\b|\bucits\b|\boeic\b", re.I)


def _demote_category(category: str | None, text: str) -> tuple[str | None, dict | None]:
    """Split a fund-structure category into (committed value, hint).

    Measured, not defensive: asked "can I market my hedge fund to pension schemes in
    Jersey", Haiku 4.5 set `category = Funds - Open-Ended` outright, with the rationale
    "hedge funds are typically open-ended structures" — against the instruction two
    paragraphs above it. Typically is not a compliance fact, and the category decides which
    route through the memo answers the question, so committing it silently means the user
    never confirms the one value most likely to be wrong.

    The same shape as the Active/Passive demotion: the signal is kept, as a hint, and the
    question is asked. Narrow on purpose — an explicitly stated structure, a UCITS or an
    OEIC passes through untouched.
    """
    if category not in V.FUND_CATEGORY_HINTS or _STRUCTURE_STATED.search(text or ""):
        return category, None
    likely = "open-ended" if category == "Funds - Open-Ended" else "closed-ended"
    return None, {"value": category, "note": (
        f"From what you've described this is probably {likely}, but the fund's actual "
        f"structure is what decides it — please confirm.")}


def _transcript(messages: list[dict]) -> str:
    return "\n".join(
        f'{"User" if m.get("role") == "user" else "Assistant"}: {m.get("content", "")}'
        for m in messages)


# ---- intent + whole-conversation extraction -------------------------------------------

_INTENT_PROMPT = """You are an intent router for a regulatory tool used by \
asset-management compliance staff. The tool answers questions about cross-border \
marketing and selling restrictions for investment funds and investment \
management/advisory services in specific jurisdictions.

Given the conversation so far, do the following and return them as JSON.

1) Decide the user's INTENT:
   - "permitted_activity": the user wants to DO, or is asking whether they CAN do, some \
marketing or selling ACTIVITY (or what conditions apply to it) — for funds or investment \
management/advisory services. There MUST be an actual marketing/selling/distribution \
activity that the user intends to carry out or check (marketing, promoting, advertising, \
pitching, soliciting, cold-calling, pre-marketing, distributing, selling, providing a \
service, or responding to a reverse enquiry). This INCLUDES vague or under-specified \
requests to do such an activity, where the user has not yet said which jurisdiction, \
activity, fund/service or investor type — the structured interview exists to gather those \
missing details, so route here rather than to "other". Examples that ALL count: "I want \
help with marketing", "I want to market my fund", "help me sell to investors", "can I \
promote my fund abroad", "can I cold-call investors in Jersey", "can I do a fly-in \
meeting with investors". Do NOT require a jurisdiction or any other detail to be present.
     BUT a question about the CONSEQUENCES, EFFECT or regulatory TREATMENT of a \
circumstance or fact — where the user is NOT proposing to carry out a marketing/selling \
activity — is NOT permitted_activity; classify it as "other". The mere mention of an \
"investor" or a "fund" does not make it permitted_activity; there has to be an activity \
the user wants to do or check. "I've got an investor who has relocated, how does that \
affect things?" proposes no activity, and is therefore "other".
     CARVE-OUTS — the following are ALWAYS "other", EVEN IF a marketing/selling activity \
is mentioned as context or framing. What matters is the PRIMARY ASK — the thing the user \
actually wants back. If the deliverable is INFORMATION of one of these kinds (rather than \
a can-I/may-I permission check), classify as "other" and set "other_kind":
       (i) "disclaimers" — what disclaimers, legends, risk warnings or prescribed wording \
must be used (e.g. "What are the written disclaimers for a closed-ended fund sold using \
the PPR?" — the ask is disclaimer wording, so "other").
       (ii) "definitions" — what a particular (defined) term, label or concept means \
(e.g. "what does pre-marketing mean?", "what counts as a professional investor?").
       (iii) "penalties" — the penalties, sanctions, fines, enforcement or consequences \
of breach or non-compliance.
       (iv) "licensing" — what licences, registrations, notifications or filings exist or \
are required, how such a process works, what it costs, or how long it takes.
       (v) "regulator" — who the regulator is, what a document or rulebook says, \
procedural steps, or timelines.
     Contrast: "CAN I sell my closed-ended fund to retail investors in Ireland?" IS \
permitted_activity — the deliverable is a permission/conditions answer. "WHAT DISCLAIMERS \
are required when doing so?" is "other" with other_kind "disclaimers". When a message \
contains BOTH a genuine request to do/check an activity AND an informational ask, prefer \
the carve-out only if the informational ask is clearly the primary deliverable.
   - "capability": the user is asking what this tool itself can do, what it covers, or \
how to use it — a meta question ABOUT the tool.
   - "other": any other substantive question that is NOT a request to do (or check the \
permissibility of) a marketing/selling activity.
   Set "other_kind" to one of "disclaimers" | "definitions" | "penalties" | "licensing" | \
"regulator" when the intent is "other" and one of those clearly fits; otherwise null. It \
is always null when the intent is not "other".

2) EXTRACT the five parameters below from the WHOLE conversation, mapping the user's \
wording onto the exact allowed values. Map synonyms and paraphrases — do not require \
exact wording. Use null when a parameter has not been provided or cannot be confidently \
determined.

- jurisdiction: EXACTLY one of the names in ALLOWED JURISDICTIONS below, spelled exactly \
as listed. Map colloquial names and cities onto the listed spelling ("Britain", "England" \
-> "United Kingdom"; "Éire" -> "Ireland"; "Frankfurt" -> "Germany"). If the user names a \
jurisdiction that is NOT in the list, return null — do not substitute a neighbour.

- activity: "General Marketing" | "Pre-Marketing of Funds" | "Passive Marketing/Selling" \
| "Active Marketing/Selling"
  - "General Marketing": general brand or firm-level promotion not tied to selling a \
specific fund.
  - "Pre-Marketing of Funds": testing investor appetite, sounding out, or pre-marketing a \
fund before it is established or available to subscribe.
  - "Passive Marketing/Selling": reverse solicitation — marketing or selling that follows \
the investor's OWN unsolicited first approach.
  - "Active Marketing/Selling": marketing or selling where the FIRM made the first \
approach to an investor who had not asked first.
  - CRITICAL — Active vs Passive turns ENTIRELY on ONE thing: WHO MADE THE INITIAL \
CONTACT. Firm reached out first to someone who hadn't asked = active. Investor approached \
the firm first, unsolicited, and the firm is responding = passive. Nothing else decides it.
  - Because the initial contact is rarely stated, NEVER commit "Active Marketing/Selling" \
or "Passive Marketing/Selling" to the "activity" slot — leave "activity" null for them \
and, at most, express the lean as an "activity_hint" for the user to confirm. The \
"activity" slot may ONLY ever hold "General Marketing" or "Pre-Marketing of Funds". So \
there are THREE cases:
    (a) SIGNAL — the text says (or strongly implies) who made the FIRST contact. Only \
then set "activity_hint" to the leaning value with a one-sentence note that names the \
signal and asks the user to confirm. Example: "an investor emailed me out of the blue \
asking about the fund" -> activity_hint {"value":"Passive Marketing/Selling","note":"They \
contacted you first, which usually means passive (reverse solicitation) — please \
confirm."}
    (b) EXPLICIT LABEL — the user THEMSELVES labels it "active"/"active marketing" or \
"passive"/"reverse solicitation"/"reverse enquiry". Their own label IS the signal: set \
"activity_hint" to the matching value with a note asking them to confirm, noting that \
active vs passive ultimately turns on who made first contact. Still do NOT promote it to \
the "activity" slot.
    (c) NO SIGNAL — nothing reveals who made first contact and the user gives no explicit \
label. Leave BOTH "activity" and "activity_hint" null so the user is simply asked. This \
is the DEFAULT and it is common.
  - DO NOT infer active from the mere fact that the firm is SELLING, signing up, closing, \
pitching at a meeting, travelling to meet an investor (fly-in/fly-out), running a \
roadshow, or otherwise doing energetic-sounding work. None of that reveals who made the \
first contact — all of it can perfectly well FOLLOW an investor's own unsolicited \
approach. Likewise a bare verb such as "sell", "market", "offer" or "distribute" on its \
own, or a channel named with no direction ("I might email people", "we could run a \
webinar"), gives NO signal — leave both null.
  - The MEDIUM or CHANNEL (a phone call, email, letter, brochure, website, social media, \
webinar, in-person meeting, fly-in or fly-out visit) is ORTHOGONAL to active vs passive \
and never decides it — the SAME channel can be proactive or reactive.
  - "General Marketing" and "Pre-Marketing of Funds" are NOT subject to this caution: set \
them on "activity" directly whenever the wording clearly matches. An activity_hint is \
ONLY ever Active or Passive, is advisory only, and is set ONLY when "activity" is null.

- category — the allowed values DEPEND ON THE JURISDICTION:
  - For a NON-EEA jurisdiction, or when the jurisdiction is unknown: "Funds - Open-Ended" \
| "Funds - Closed-Ended" | "Investment Management & Advisory Services"
  - For an EEA jurisdiction (see EEA JURISDICTIONS below) the category captures the fund's \
EEA marketing status (or IMAS) instead, and the valid values depend on the activity:
    - Pre-Marketing of Funds: "UCITS fund prior to using UCITS passport" | "AIF prior to \
using AIFMD passport" | "AIF prior to making PPR registration/notification"
    - Passive Marketing/Selling: "Funds - Passported AIF" | "Funds - Non-passported AIF" \
| "Funds - Passported UCITS" | "Funds - Non-passported UCITS" | "Investment Management & \
Advisory Services"
    - Active Marketing/Selling: "Funds - Passported AIF" | "Funds - AIF via NPPR" | \
"Funds - Passported UCITS" | "Funds - Non-passported UCITS" | "Funds - AIF to Retail \
investors" | "Investment Management & Advisory Services"
    - BE CONSERVATIVE for the EEA fund values: only set one when the user has clearly \
stated the fund type (AIF vs UCITS) AND its passporting/NPPR status. A bare "fund", "AIF" \
or "UCITS" with no passporting status stays null — the interview will ask. NEVER set \
"Funds - Open-Ended" or "Funds - Closed-Ended" for an EEA jurisdiction.
  - BE CONSERVATIVE generally — only set category when you are certain:
    - "Funds - Open-Ended" when the user EXPLICITLY says the fund is open-ended, or names \
a UCITS or an OEIC (these are reliably open-ended).
    - "Funds - Closed-Ended" when the user EXPLICITLY says the fund is closed-ended.
    - "Investment Management & Advisory Services" when the question is clearly about a \
SERVICE (managing portfolios, discretionary management, investment advice, arranging) \
rather than a fund.
    - Do NOT infer open- vs closed-ended from INDIRECT fund wording that does not itself \
state the structure (SICAV, unit trust, investment trust, hedge fund, private equity, real \
estate). Leave category null and use category_hint instead. We would rather ask than guess.

- investor_type: for a NON-EEA jurisdiction (or an unknown one) "Professional Investor" | \
"Retail Investor" | "Both" | "Unknown"; for an EEA jurisdiction the MiFID II labels \
"Professional Client" | "Retail Client" | "Both" | "Unknown".
  - Professional = institutional, professional, qualified, sophisticated, accredited, \
high-net-worth, eligible counterparties.
  - Retail = ordinary, individual, non-professional investors, or the general public.
  - Both = explicitly both professional and retail.
  - Unknown = the user said they don't know / it isn't specified but they still want an \
answer. Only use "Unknown" if the user signalled it; otherwise use null when investor type \
was not mentioned.

- marketer: "EEA Passporting Entity" | "Other firm"
  - ONLY relevant for an EEA jurisdiction with Active Marketing/Selling — it captures WHO \
is carrying out the marketing. Set "EEA Passporting Entity" when the user clearly says the \
marketing firm is an EEA-authorised firm relying on an EEA passport. Set "Other firm" when \
the marketer is clearly a non-EEA / third-country firm or a firm not relying on a \
passport. Otherwise leave null — this is rarely stated and the interview will ask. For a \
non-EEA jurisdiction ALWAYS leave marketer null.

3) Identify the PRODUCT KIND, only when it is clear:
   - "fund": clearly about a fund or funds (a collective investment vehicle). A fund is \
never investment management/advisory services.
   - "service": clearly about investment management or advisory services (portfolio \
management, discretionary management, investment advice, arranging) rather than a fund.
   - null: not clear which.

4) Provide a CATEGORY HINT, ONLY in these two cases, ONLY when "category" is null, and \
ONLY when the jurisdiction is non-EEA or unknown (open- vs closed-ended is not the EEA \
vocabulary, so for an EEA jurisdiction category_hint must be null):
   - the user mentions a "hedge fund" -> {"value": "Funds - Open-Ended", "note": "You \
mentioned a hedge fund, which is usually open-ended."}
   - the user mentions "private equity" or "PE" -> {"value": "Funds - Closed-Ended", \
"note": "You mentioned private equity, which is usually closed-ended."}
   Otherwise category_hint is null. A hint NEVER decides the category — it only suggests a \
likely answer for the user to confirm.

5) Provide RATIONALES for non-obvious mappings, so the tool can explain WHY a value was \
set. For each parameter you set to a NON-NULL value:
   - OBVIOUS — the user named the value directly or used clearly equivalent wording \
("Jersey" -> Jersey; "professional investors" -> Professional Investor; "an open-ended \
fund" -> Funds - Open-Ended). OMIT the field from rationales.
   - NON-OBVIOUS — you mapped a synonym, paraphrase, described scenario or inference, such \
that the user might not immediately see why ("putting up posters" -> General Marketing; \
"sounding out investors before launch" -> Pre-Marketing of Funds; "managing client \
portfolios" -> Investment Management & Advisory Services). Include a VERY SHORT \
explanation (one clause, max ~15 words, lower-case, no trailing period).
   Return this as "rationales", an object whose keys are a subset of \
{"jurisdiction","activity","category","investor_type","marketer"}. Use {} when every \
mapping was obvious or nothing was set.

Return ONLY a JSON object with exactly this shape and nothing else — no prose, no code \
fence:
{
  "intent": "permitted_activity" | "capability" | "other",
  "other_kind": "disclaimers" | "definitions" | "penalties" | "licensing" | "regulator" \
| null,
  "jurisdiction": <value or null>,
  "activity": <value or null>,
  "category": <value or null>,
  "investor_type": <value or null>,
  "marketer": <value or null>,
  "product_kind": "fund" | "service" | null,
  "category_hint": { "value": "Funds - Open-Ended" | "Funds - Closed-Ended", "note": \
<string> } | null,
  "activity_hint": { "value": "Active Marketing/Selling" | "Passive Marketing/Selling", \
"note": <string> } | null,
  "rationales": { "jurisdiction"?: <string>, "activity"?: <string>, "category"?: \
<string>, "investor_type"?: <string>, "marketer"?: <string> }
}"""

_INTENTS = ("permitted_activity", "capability", "other")
_OTHER_KINDS = ("disclaimers", "definitions", "penalties", "licensing", "regulator")
_PRODUCT_KINDS = ("fund", "service")
_RATIONALE_MAX = 160


def _jurisdiction_context(jurisdictions: list[str]) -> str:
    """The jurisdiction list, and which of them take the EEA branch.

    Injected rather than hardcoded in the prompt: the POC named its six, which meant its
    router could not have extracted a seventh even after the corpus gained 82 more.
    """
    eea = [j for j in jurisdictions if V.is_eea(j)]
    return ("ALLOWED JURISDICTIONS (use these spellings exactly):\n"
            + ", ".join(jurisdictions)
            + "\n\nEEA JURISDICTIONS (these take the EEA category, investor-type and "
              "who-is-marketing vocabulary; every other jurisdiction above does not):\n"
            + (", ".join(eea) if eea else "(none in this index)"))


def classify(messages: list[dict], jurisdictions: list[str]) -> dict:
    """Intent + slot extraction over the WHOLE conversation.

    Whole-conversation, not just the latest turn: the user's jurisdiction may have been
    named three messages ago, and re-asking for something they already said is the fastest
    way to make a guided interview feel broken.

    Returns raw-but-VALIDATED values — every value is one the vocabulary allows. Cross-field
    consistency is `engine.merge_slots`' job, not this function's.
    """
    transcript = _transcript(messages)
    text = _converse(
        _INTENT_PROMPT + "\n\n" + _jurisdiction_context(jurisdictions),
        transcript, 1024)
    d = _json_object(text)

    intent = d.get("intent") if d.get("intent") in _INTENTS else "other"
    jurisdiction = _pick(d.get("jurisdiction"), jurisdictions)
    eea = V.is_eea(jurisdiction)
    slots = {
        "jurisdiction": jurisdiction,
        # Only the two committable activities are accepted here. An Active/Passive value
        # is demoted to a hint below — see case (a)-(c) in the prompt.
        "activity": _pick(d.get("activity"), V.DIRECT_ACTIVITIES),
        # Both vocabularies are accepted at extraction time; merge_slots drops whatever is
        # invalid for the jurisdiction and activity that are finally chosen. A fund
        # STRUCTURE the wording does not actually state is demoted to a hint below.
        "category": _pick(d.get("category"), V.ALL_CATEGORIES),
        "investor_type": _pick(d.get("investor_type"),
                               V.investor_type_options_for(jurisdiction)),
        # Who is marketing is an EEA-only question, so a value for anywhere else is a
        # value the interview would never have asked for.
        "marketer": _pick(d.get("marketer"), V.MARKETER_OPTIONS) if eea else None,
    }

    # A hint and a committed value are mutually exclusive. Prefer the model's own hint;
    # failing that, demote an Active/Passive value it committed against instructions,
    # rather than dropping the signal entirely.
    slots["category"], demoted_category = _demote_category(slots["category"], transcript)

    demoted = _pick(d.get("activity"), V.HINT_ACTIVITIES)
    activity_hint = None
    if not slots["activity"]:
        activity_hint = _hint(d.get("activity_hint"), V.HINT_ACTIVITIES)
        if activity_hint is None and demoted:
            activity_hint = {"value": demoted, "note": (
                "This looks like active outreach, but active vs passive depends on who "
                "made first contact — please confirm."
                if demoted == "Active Marketing/Selling" else
                "This looks like passive (reverse) solicitation, but active vs passive "
                "depends on who made first contact — please confirm.")}

    rationales = {}
    raw_rat = d.get("rationales")
    if isinstance(raw_rat, dict):
        for k in ("jurisdiction", "activity", "category", "investor_type", "marketer"):
            v = raw_rat.get(k)
            # Only for a slot that actually holds a value: a rationale for a dropped
            # value would explain a choice the user was never shown.
            if slots.get(k) and isinstance(v, str) and v.strip():
                rationales[k] = v.strip()[:_RATIONALE_MAX]

    return {
        "intent": intent,
        "other_kind": (_pick(d.get("other_kind"), _OTHER_KINDS)
                       if intent == "other" else None),
        "slots": slots,
        "signals": {
            "product_kind": _pick(d.get("product_kind"), _PRODUCT_KINDS),
            # Hedge-fund / PE hints point at open- vs closed-ended, which is not the EEA
            # vocabulary — suppressed there rather than shown as an unusable suggestion.
            # A demoted category takes precedence over the model's own hint: it is the
            # value the model was confident enough to commit, so it is the better
            # suggestion to put in front of the user.
            "category_hint": (None if eea else
                              (demoted_category
                               or _hint(d.get("category_hint"), V.FUND_CATEGORY_HINTS))),
            "activity_hint": activity_hint,
        },
        "rationales": rationales,
    }


# ---- triage of a mid-interview turn ----------------------------------------------------

_TRIAGE_PROMPT = """You are triaging a single user turn during a structured intake \
interview. A compliance user is being asked, one field at a time, to pin down up to five \
parameters for a cross-border marketing/selling check: Jurisdiction, Marketing Activity, \
Product/Service Category, Who Is Marketing (EEA active marketing only — whether the \
marketer is an EEA passporting entity or another firm), and Investor Type.

Classify the user's LATEST message into exactly one lane:

- "answer": the user is replying to the interview — giving or choosing a value for a field \
(e.g. "Jersey", "professional investors", "an open-ended fund", "I don't know"), \
confirming, or otherwise directly continuing the flow.

- "scoping_question": the user is asking what one of the scoping TERMS means, or asking \
which option their described situation MAPS TO. These are vocabulary/classification \
questions about how to fill in a field. Examples:
  - "what is general marketing?"
  - "I met an investor at a conference, is that reverse enquiry?"
  - "is trading securities on behalf of a client IMAS?"
  - "does an OEIC count as open-ended?"
  - "what are professional investors? I'm talking to a bank, what are they likely to be?"
  - "would a pension fund count as a professional investor or retail?"

- "route_onward": the user is asking a genuine regulatory question — whether a specific \
activity is permitted/allowed in a jurisdiction or what conditions apply — or an \
unrelated/general question. Examples:
  - "can I cold-call investors in Jersey?"
  - "is reverse enquiry allowed in the Bahamas?"
  - "what are the registration timelines for the UK?"

Decisive test: a "scoping_question" asks what a term means or which option fits a \
scenario. A "route_onward" question asks whether something is permitted/allowed or what \
the rules are. When the user is simply supplying a value, it is "answer". A described \
scenario ("I'm talking to a bank", "I met them at a conference") does NOT make a turn \
route_onward — if the underlying ask is what a term means or which option the scenario \
maps to, it is scoping_question. Prefer scoping_question over route_onward whenever the \
question is about one of the five fields' vocabulary; exiting the interview by mistake is \
far more costly than answering a definition inline.

When (and only when) the lane is "scoping_question", also set "has_answer": this is a \
MIXED turn that, alongside the definition question, ALSO states a concrete value for one \
of the five fields — a jurisdiction, a marketing activity, a product/category or fund \
attribute ("it's a hedge fund", "an open-ended fund"), an investor type, or who is doing \
the marketing ("we're an EEA AIFM using our passport"). Set "has_answer": true for such \
mixed turns (e.g. "it's a hedge fund, what is that?" both states the product AND asks for \
a definition). Set "has_answer": false when the message is only a question with no \
concrete value stated. For the other lanes "has_answer" is irrelevant; return false.

Return ONLY a JSON object, no prose and no code fence: {"lane": "answer" | \
"scoping_question" | "route_onward", "has_answer": true | false}"""

_LANES = ("answer", "scoping_question", "route_onward")

# Deterministic backstop for the scoping lane. The triage model is unreliable on
# definitional questions that also sketch a scenario ("what are professional investors?
# I'm talking to a bank…") — it flips between "answer" and "scoping_question" run to run.
# The cost is asymmetric: a wrong "answer" or "route_onward" boots the user out of the
# interview and loses the state. So a turn that is clearly ASKING what a term means, while
# a field is pending, is FORCED into the scoping lane. Narrow on purpose: it needs a
# question mark AND an interrogative/definition stem AND one of the interview's own terms,
# so plain values ("Jersey?", "professional investors") are untouched.
_ASKS_DEFINITION = re.compile(
    r"\b(what|who)\s+(is|are|does|do|would|should|counts?)\b|\bmean(s|ing)?\b(?!\s+(for|to)\b)"
    r"|\bcount(s)?\s+as\b|\bdefinition\b", re.I)
_INTERVIEW_TERM = re.compile(
    r"\b(jurisdiction|general marketing|pre[- ]?market|passive|active|"
    r"reverse[- ](enquiry|solicitation)|open[- ]?ended|closed[- ]?ended|oeic|ucits|aif\b|"
    r"nppr|ppr\b|passport|imas\b|investment management|advisory|professional|retail|"
    r"accredited|institutional|investor type|investor(s)?\b|client(s)?\b|marketer|"
    r"passporting)\b", re.I)
# Permission and rules questions belong onward, not here.
_IS_PERMISSION = re.compile(
    r"\b(permitted|allowed|legal|can i|could i|may i|am i allowed|register|registration|"
    r"licen[cs]e)\b|\bmean(s|ing)?\s+(for|to)\b", re.I)


def _asks_definition(latest: str) -> bool:
    return bool("?" in latest and _ASKS_DEFINITION.search(latest)
                and _INTERVIEW_TERM.search(latest) and not _IS_PERMISSION.search(latest))


_HAS_ANSWER_PROMPT = (
    'The user message below is a definition/classification question asked during an '
    'intake interview about cross-border marketing (fields: Jurisdiction, Marketing '
    'Activity, Category, Investor Type, Who Is Marketing). Does the message ALSO state a '
    'concrete value for one of those fields (e.g. "it\'s a hedge fund", "we target '
    'professional investors", "Jersey")? Asking what a term means is NOT stating a value. '
    'Return ONLY JSON: {"has_answer": true | false}')


def triage(messages: list[dict], pending_field: str | None) -> dict:
    """What the latest turn IS. Returns {"lane", "has_answer"}."""
    latest = (messages[-1].get("content") if messages else "") or ""
    d = _json_object(_converse(
        _TRIAGE_PROMPT,
        f"Conversation so far:\n{_transcript(messages)}\n\n"
        f"Latest user message to classify:\n{latest}", 256))
    lane = d.get("lane") if d.get("lane") in _LANES else "answer"
    has_answer = d.get("has_answer") is True

    if lane != "scoping_question" and pending_field and _asks_definition(latest):
        lane = "scoping_question"
        # A forced lane means the model never judged has_answer (it only does so for
        # scoping turns), so ask separately. Without this, a mixed turn ("we target
        # professional investors — what are they?") is tagged a pure aside and its stated
        # value is dropped on the floor.
        try:
            j = _json_object(_converse(_HAS_ANSWER_PROMPT, latest, 64))
            has_answer = j.get("has_answer") is True
        except RouterUnavailable:
            # Safe default for value preservation: keep the turn visible to the extractor
            # rather than risk losing something the user actually said.
            has_answer = True
    return {"lane": lane, "has_answer": has_answer}


def _explainer_prompt(pending_label: str | None, jurisdiction: str | None) -> str:
    where = f' and is currently being asked for the "{pending_label}" field' \
        if pending_label else ""
    return f"""You are a scoping assistant for a cross-border marketing/selling \
compliance tool. The user is part-way through an intake interview{where}. They have paused \
to ask a scoping/classification question.

Your ONLY job is to:
1. Explain, in plain language, the relevant scoping term(s).
2. Where the user's described scenario clearly maps onto one of the controlled-vocabulary \
OPTIONS below, say which option it corresponds to.

Hard rules:
- Use ONLY the definitions provided below. Do not invent terms or thresholds.
- Do NOT give any regulatory conclusion about whether an activity is permitted, allowed or \
restricted, or what conditions/registrations apply — that is decided by a separate \
downstream check, not here.
- If the scenario does not clearly map to a single option, say so briefly and describe what \
would distinguish the options, without guessing.
- Be concise: 2-4 short sentences. No headings, no lists unless essential.

CONTROLLED VOCABULARY AND DEFINITIONS:
{V.definitions_prompt_block(jurisdiction)}"""


def explain(latest: str, pending_field: str | None, jurisdiction: str | None) -> str:
    """Answer a vocabulary question from the controlled definitions ONLY.

    Grounded in `definitions_prompt_block`, which is divided by pathway, so the explainer
    cannot offer Jersey a passporting option. It is also barred from saying whether
    anything is permitted: that is the answer path's job, and a permission verdict reached
    here would bypass retrieval entirely.
    """
    label = V.FIELD_LABELS.get(pending_field) if pending_field else None
    out = _converse(_explainer_prompt(label, jurisdiction), latest, 1024).strip()
    return out or "I couldn't generate an explanation for that."
