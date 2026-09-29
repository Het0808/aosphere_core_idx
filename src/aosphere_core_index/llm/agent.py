"""Agentic AI Mode — an OpenAI Agents SDK agent over the cross-region index.

The agent is given tools (search_clauses, read_clauses) and reasons in a loop:
search → read → follow cross-references → compare jurisdictions → cited answer.
The model is a tool-capable Bedrock model (default Claude Haiku 4.5 via the EU
inference profile — best quality/cost), selectable per request. Runs trace to MLflow
when MLFLOW_TRACKING_URI is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import litellm

from ..config import settings

log = logging.getLogger(__name__)
from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

set_tracing_disabled(True)  # MLflow is our trace backend, not the SDK's OpenAI one

# Pin the Bedrock region so local matches prod and data stays in the EU. With NO
# region env litellm defaults to us-east-1, so we FORCE it (not setdefault).
# The value comes from settings.bedrock_region (ACI_BEDROCK_REGION) — the same one
# stage 4 and the rerankers read — rather than a literal here, so "which region do
# our AI calls use" has one answer. Every model in MODELS below is offered in
# eu-west-2 as well as eu-west-1; litellm + our Titan client both read AWS_REGION_NAME.
_BEDROCK_REGION = settings.bedrock_region
os.environ["AWS_REGION_NAME"] = _BEDROCK_REGION

# Per-call cap. Larger models (Nova Pro, DeepSeek V3) can take 20-40s/call as the
# context grows over the tool loop — give them headroom; fast models finish well under.
litellm.request_timeout = float(os.getenv("ACI_LLM_TIMEOUT", "120"))
_RUN_TIMEOUT = float(os.getenv("ACI_AGENT_TIMEOUT", "300"))  # hard wall-clock cap per run
# Some models take more search/read rounds; the wall-clock cap still bounds runaways.
_MAX_TURNS = int(os.getenv("ACI_AGENT_MAX_TURNS", "28"))
# Pin sampling low: this is a factual legal-research agent, and an unpinned (default
# ~1.0) temperature makes the multi-step tool path non-deterministic — the same
# question sometimes follows a cross-reference edge (e.g. A2.2 -> Part G) and answers
# fully, sometimes loops and misses it. Low temp makes the good path the consistent one.
_TEMP = float(os.getenv("ACI_LLM_TEMPERATURE", "0"))

# Curated to the models that reliably give correct, grounded, cited answers for this
# task — both Anthropic, served in eu-west-1 via the EU inference profile (eu.* id).
# Haiku 4.5 ≈ Sonnet quality at ~1/3 the cost (the default); Sonnet for the hardest
# questions. The cheaper Nova/GPT-OSS/Nemotron models underperformed and were dropped;
# DeepSeek R1 isn't offered in eu-west-1 and lacks Converse tool support anyway.
# Each needs Bedrock model access granted in the region. `in`/`out` = USD per 1M tokens.
MODELS: list[dict] = [
    {"id": "anthropic.claude-sonnet-4-6", "invoke": "eu.anthropic.claude-sonnet-4-6",
     "label": "Claude Sonnet 4.6 (highest quality)", "in": 3.0, "out": 15.0},
    # Cheaper-than-Sonnet alternatives to A/B for quality (bare on-demand in eu-west-1,
    # tool-calling verified). `in`/`out` are APPROXIMATE — confirm on the AWS price list.
    {"id": "minimax.minimax-m2.5", "label": "MiniMax M2.5 (agentic, low cost)", "in": 0.30, "out": 1.20},
    {"id": "qwen.qwen3-vl-235b-a22b", "label": "Qwen3 235B", "in": 0.22, "out": 0.88},
    {"id": "anthropic.claude-haiku-4-5-20251001-v1:0",
     "invoke": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
     "label": "Claude Haiku 4.5", "in": 1.0, "out": 5.0},
]
DEFAULT_MODEL = os.getenv("ACI_LLM_MODEL", "anthropic.claude-sonnet-4-6")
_ALLOWED = {m["id"] for m in MODELS}


def _invoke_id(model_id: str) -> str:
    """The id Bedrock actually accepts for this model in the configured region (the
    `eu.` inference-profile id for Nova/Claude, the bare id otherwise)."""
    for m in MODELS:
        if m["id"] == model_id:
            return m.get("invoke", model_id)
    return model_id


_SEARCH_K = int(os.getenv("ACI_SEARCH_K", "40"))  # candidate pool retrieved + reranked
# Relevance cutoff for the agent's search tool — scale depends on the reranker backend:
#   * cross-encoder ('logit'): relevant clauses score ~5-8, unrelated go negative -> ~2.
#   * LLM/cohere ('unit'): scores are [0,1]; the LLM reranker bands its relevant set >0.5,
#     so ~0.3 keeps that set plus a safety margin and drops the clearly-irrelevant tail.
# (A logit cutoff against unit scores filters out EVERYTHING -> the agent finds nothing.)
from aosphere_core_index.embeddings.reranker import score_scale as _score_scale
_SEARCH_MIN_SCORE = float(os.getenv(
    "ACI_SEARCH_MIN_SCORE", "0.3" if _score_scale() == "unit" else "2.0"))

# In-memory conversation history per chat session. SINGLE-PROCESS ONLY: with
# multiple workers a follow-up can land on another worker and silently lose
# context — move to Redis (or sticky sessions) before scaling out.
# Bounded three ways: per-session history cap, TTL, and a max session count
# (oldest evicted), so the store can't grow without limit.
_SESSIONS: dict[str, tuple[float, list]] = {}  # session_id -> (last_used, input list)
_HISTORY_CAP = 40
_SESSION_TTL = float(os.getenv("ACI_SESSION_TTL", "7200"))  # seconds
_SESSION_MAX = int(os.getenv("ACI_SESSION_MAX", "500"))


def _session_history(session_id: str | None) -> list:
    if not session_id:
        return []
    ent = _SESSIONS.get(session_id)
    if ent is None:
        return []
    ts, items = ent
    if time.monotonic() - ts > _SESSION_TTL:
        _SESSIONS.pop(session_id, None)
        return []
    return items


def _session_store(session_id: str, items: list) -> None:
    now = time.monotonic()
    for sid, (ts, _) in list(_SESSIONS.items()):  # purge expired
        if now - ts > _SESSION_TTL:
            _SESSIONS.pop(sid, None)
    while len(_SESSIONS) >= _SESSION_MAX:  # evict oldest
        _SESSIONS.pop(min(_SESSIONS, key=lambda s: _SESSIONS[s][0]), None)
    _SESSIONS[session_id] = (now, items[-_HISTORY_CAP:])

INSTRUCTIONS = """You are aosphere's data-privacy research assistant. Answer ONLY \
from information returned by your tools (clauses from the survey index, across \
jurisdictions) — never outside knowledge.

Method (be decisive — at most TWO searches, then read + traverse, then answer):
1. Call search_clauses ONCE. When comparing named jurisdictions, pass them all in \
that single call (jurisdictions="Germany,France,Italy") — do NOT search each \
separately. The snippet returned is usually enough to answer directly — use it.
2. Call read_clauses for the full text, passing every clause you need at once.
3. TRAVERSE THE GRAPH: each read also returns the clause's GUIDANCE (a curated answer
— use it) and its RELATED clauses (cross-references, incl. "Part X" pointers like \
"see Part G Breach Response"). If the answer points elsewhere or the question needs \
exceptions/exemptions/full detail, do ONE more read_clauses on those RELATED clauses \
(follow the edge — do not re-search) before answering.
4. Then write the answer. Don't keep re-SEARCHING (reading related clauses is fine).

Reading clauses: each clause shows the survey QUESTION and the jurisdiction's \
ANSWER. Base your answer ONLY on the answer text. An empty, "N/A", or "no \
requirement" answer means that obligation does NOT exist — never infer an \
obligation from the question wording alone.

By question type:
- "What laws apply / are in force / main privacy laws" → read clause A1.1 ("Laws") \
  in Part A; it names the jurisdiction's data-privacy laws. (US states: also A1.3.)
- "Exemptions", "what must be included", or any list/enumerate question → read the \
  WHOLE relevant clause and list EVERY item; do not stop at the first few. Add only \
  items actually present in the answer (don't pad with ones that aren't there).
- A jurisdiction you were asked about is ALWAYS in the index — never say it is "not \
  covered" or "not in the dataset". If a search is thin, read Part A directly.

US STATES — the clauses that answer the common questions (each already scoped to the \
state):
  • A1.3 "List of relevant privacy laws" — whether the state has an omnibus law + statute.
  • A2.1 "Introduction" — HOW the state's laws reach an organisation (the jurisdictional \
basis / nexus) and their key limits.
  • A2.2 "Scope of laws: state-by-state comparison" — applicability / thresholds.
  • the topic table for the question (e.g. B2 rights, exemptions, C-series, etc.).
For an applicability/scope question, READ BOTH A2.1 and A2.2 and answer from their text. \
A1.3/A2.2 tell you whether the state has an omnibus law (give its name + any thresholds). \
A2.1 states the basis on which the state's laws apply to an organisation. If there is no \
omnibus law, say so AND give the applicability basis exactly as A2.1 describes it — quote \
the clause, do not recite a remembered legal test (the laws change; the clause is the \
source of truth).

Negative findings are valid: when the answer text shows no such law/requirement, \
state that plainly (e.g. "X does not have a standalone comprehensive privacy law"). \
Never reply that you were "unable to retrieve" or that the index lacks data.

ALERTS: search may also return regulatory ALERTS (keys like `ALERT:<id>`, shown with \
a 🔔). These are recent, time-sensitive regulatory news — not survey clauses. Read them \
like any other source (read_clauses accepts `Jurisdiction:ALERT:<id>`) and, when \
relevant, mention the development and cite it `[Jurisdiction · ALERT:<id>]`. Keep the \
authoritative answer grounded in the survey clauses; use alerts for recent context.

CURATED GUIDANCE: results marked 💡 (keys like `GUID:<id>`) are lawyer-curated Q&A \
answers — HIGH-QUALITY sources, often answering the user's exact question. Read them \
via read_clauses (`Jurisdiction:GUID:<id>`) and cite as `[Jurisdiction · GUID:<id>]`.

Cite every statement as [Jurisdiction · ClauseKey].

NEVER narrate your own process. The reader wants the position, not an account of how you \
found it — so no "based on the information already retrieved", "as read above", "no further \
search is needed", "the survey states/addresses this exact scenario", "I searched for". \
State the position and cite the clause; the citation is the evidence that you read it.

TONE: keep it professional. Do NOT use emojis, with ONE exception — a country/region \
flag emoji (e.g. 🇫🇷, 🇩🇪, 🇪🇺) may prefix a jurisdiction name. No decorative emojis in \
headings, bullets, or prose (no 📊, 📈, 🔑, ✅, etc.)."""

# Answer format is a per-request choice: a practitioner asking a closed question wants the
# position and the clause, not an essay. Default BRIEF; the UI's "Explain" switch asks for
# the reasoning. Both modes obey the same sourcing rules above — only the shape changes.

_ANSWER_BRIEF = """

ANSWER FORMAT — BRIEF (default). Lead with the answer, then stop. Budget: about 60 words, \
90 at the very most, for a single jurisdiction.
- First line: the direct answer in ONE sentence, starting with **Yes**, **No** or the \
requirement itself, with its citation. For a closed question that line IS the answer — very \
often you should stop there.
- Then AT MOST two short bullets, and only for something the first line does not already \
imply: the operative condition, a threshold/deadline, or a genuine exception. Each cited.
- HARD LIMIT: at most THREE sentences of prose in total, plus the two bullets. Count them.
- Do not quote unless the exact words carry the obligation and paraphrase would weaken it. \
Then quote ONCE, 20 words at most, with the quotation marks inside your own sentence. NEVER \
introduce a quote or a finding by referring to the source document — no "the survey/clause \
states", "addresses this exact scenario", "provides", "confirms", "reads". The citation \
already says where it came from.
- No headings, no preamble, no restatement of the question, and no closing summary — do NOT \
add "In short", "Consequence", "Applied to your scenario" or a bolded recap of the first \
line. Saying it once is enough.
- Comparing jurisdictions: ONE line each, same shape, no per-jurisdiction headings.
- Being brief never licenses being vague: keep every figure, deadline and condition that \
changes the answer. For a list/enumerate question, give the COMPLETE list — completeness \
outranks the word budget."""

_ANSWER_EXPLAIN = """

ANSWER FORMAT — EXPLAIN (the user asked for the reasoning). Still lead with the direct \
answer in one sentence, then develop it.
- Quote the operative wording of the clauses you rely on, and say how it applies to the \
question as asked.
- Cover what a practitioner would need next: conditions, thresholds, deadlines, exceptions, \
and the related clauses you traversed.
- Use short headings and bullets where they aid scanning; keep prose tight — explaining \
means adding substance, not repeating the answer in different words.
- Where the survey text is silent or ambiguous on the question, say so explicitly rather \
than reasoning past it."""


# Shared invariants, appended to every assembled prompt regardless of product (AOSNG-3442).
#
# EMPTY ON PURPOSE, for now. The grounding and citation rules currently live at the TOP of
# INSTRUCTIONS, and that is where they must stay: the measurement recorded below found that an
# instruction at the END of a ~1,500-word prompt did not hold. Moving the invariants down here
# would put the one thing that must not fail in the position already known to be weakest.
#
# So this block exists as the seam decided in AOSNG-3442 — shared rules, not per-product
# editable — and is deliberately unpopulated until there is guidance that is genuinely product-
# agnostic AND safe to state late. Keeping it empty also keeps assembly byte-identical to the
# previous single-string prompt, which is what makes the restructure verifiable.
STANDARD_RULES = ""


def _prompt_store():
    """The per-product prompt store (AOSNG-3442).

    Import is local so agent.py carries no import-time dependency on a storage backend. With
    the NullBackend installed — which is the default until a backend is configured — this
    resolves every request to the built-in prompt, so the feature is inert rather than
    half-working in an environment that has nowhere to store overrides."""
    from aosphere_core_index.llm.prompt_store import ResolverAdapter
    return ResolverAdapter()


def mode_of(explain: bool) -> str:
    """The two answer modes the UI offers, named as the UI names them.

    The "Explain" checkbox is the whole of it: off is a summary, on is the reasoning. Prompts
    are stored per mode, so this is the name that ends up in a storage key and on a screen.
    """
    return "explain" if explain else "summary"


def _resolve_prompt(products: list[str] | None, explain: bool = False):
    """Which master prompt this request uses, and why. Never raises."""
    from aosphere_core_index.llm.product_prompt import resolve
    return resolve(products, _prompt_store(), mode_of(explain))


def instructions_for(explain: bool = False, product_prompt: str | None = None,
                     owns_format: bool = True) -> str:
    """Full system prompt: product prompt + answer style + shared invariants.

    `product_prompt` is the resolved per-product master prompt for THIS MODE, or None for the
    default. It arrives already resolved — this function performs no I/O and knows nothing
    about storage, so the prompt text a request uses stays a pure function of its arguments
    and remains testable without S3 or a network.

    A mode override replaces the answer-format block as well as the product half. That is the
    point of storing a prompt per mode: the built-in blocks below say "about 60 words", "no
    headings", "at most THREE sentences", so appending one to a summary prompt that asks for a
    labelled structure would contradict it, and the built-in block — being later in the
    prompt — is the one the model would follow. An override owns the format for its mode.

    With no override and an empty STANDARD_RULES this returns exactly what the previous
    single-string version returned, byte for byte.
    """
    fmt = _ANSWER_EXPLAIN if explain else _ANSWER_BRIEF
    if product_prompt:
        return product_prompt + ("" if owns_format else fmt) + STANDARD_RULES
    return INSTRUCTIONS + fmt + STANDARD_RULES


# The system prompt alone did not hold. Measured on the Argentina netting question, a
# format section at the end of a ~1,500-word prompt still produced 153 words, a quote
# lead-in and a recap: the answering habit sits closer to the model than the instruction
# does. Repeating the contract in the USER turn — the last thing read before it writes,
# and countable rather than a word budget — is what actually lands.

_STYLE_NOTE_BRIEF = ("\n\n(Answer BRIEFLY: the verdict in ONE sentence with its citation, "
                     "then at most two cited bullets. Three sentences of prose maximum. No "
                     "quote lead-ins, no headings, no closing recap. Keep every figure and "
                     "condition that changes the answer, and give any list in full.)")
_STYLE_NOTE_EXPLAIN = ("\n\n(EXPLAIN this one: lead with the verdict in one sentence, then "
                       "give the reasoning — the operative wording, conditions, thresholds, "
                       "exceptions and related clauses — each cited.)")


# With an override in force, the built-in note above would be the LAST thing the model reads
# and would override the very format the SME wrote. But dropping the note entirely gives back
# the position that was measured to matter, so what goes in instead is a pointer with no shape
# of its own: it re-asserts "follow your format instructions" at the strongest position
# without saying what that format is.
_STYLE_NOTE_CUSTOM = ("\n\n(Answer in exactly the shape your instructions require — the "
                      "order, the labels and the limits they set, and nothing else.)")


def _style_note(explain: bool = False, custom: bool = False) -> str:
    """The format reminder appended to the USER turn.

    `custom` says a per-product override is in force for this mode, in which case the
    built-in contract must not be restated — see _STYLE_NOTE_CUSTOM.
    """
    if custom:
        return _STYLE_NOTE_CUSTOM
    return _STYLE_NOTE_EXPLAIN if explain else _STYLE_NOTE_BRIEF


# `ctx.sources` only records what THIS turn read. A follow-up usually answers from clauses
# read in an earlier turn (that is the point of session history), so the answer arrived full
# of [Jurisdiction · Key] citations and the "Sources the agent read" strip was empty — the
# reader could see a citation but had nothing to click. The citations in the answer are the
# authoritative list, so harvest anything the tool log missed.
_CITE_BLOCK = re.compile(r"\[([^\[\]]{3,240})\]")
_FLAGS = "🇦🇧🇨🇩🇪🇫🇬🇭🇮🇯🇰🇱🇲🇳🇴🇵🇶🇷🇸🇹🇺🇻🇼🇽🇾🇿 "
_JUR_NAME_MAX = 70   # "United States - States and Territories" is 38


def _bare_jurisdiction(name: str) -> str:
    """'Shareholding Disclosure — Argentina' -> 'Argentina'. The read log records the
    QUALIFIED region id while the model cites the bare name, so dedupe on the bare one or
    the same clause is listed twice under two spellings."""
    return (name.split("—")[-1] if "—" in name else name).strip().casefold()


def _clause_key(text: str) -> str:
    """The clause key out of a cited reference. Keys never contain a space, so a trailing
    sub-reference the model added ('A6.2 §6.2.2(a)') is dropped — it is not a key, and left
    intact it both breaks the chip's lookup and defeats dedupe against the read log."""
    tok = text.strip().split()
    return tok[0].rstrip(".,;:") if tok else ""


def _existing_key_in(sections, key: str) -> str | None:
    """The nearest key present in `sections` at or above a cited reference.

    The model cites the numbering it reads INSIDE a clause — `A3.3(c)(ii)` — while the index
    holds `A3.3`, so a chip built from the citation verbatim 404s when clicked. Only
    parenthesised groups are stripped: `A3.4` must not be answered with `A3`, which would
    turn a wrong key into a confident link to the wrong clause. None when nothing matches, so
    a hallucinated key becomes no chip rather than a dead one.
    """
    if key.startswith(("ALERT:", "GUID:")):
        return key
    candidate = key
    while candidate:
        if candidate in sections:
            return candidate
        if candidate.endswith(")") and "(" in candidate:
            candidate = candidate[:candidate.rindex("(")].strip()
        else:
            return None
    return None


def _existing_key(jurisdiction: str, key: str) -> str | None:
    """`_existing_key_in` against the live index. Returns the key unchanged when the index
    cannot be consulted — a visible chip beats silently dropping a citation."""
    from aosphere_core_index.service.registry import get_bundle

    try:
        sections = get_bundle(jurisdiction).sections_by_key
    except Exception:                      # no index / unknown region — don't guess
        return key
    return _existing_key_in(sections, key)


def _with_cited_sources(answer: str, sources: list[dict],
                        resolve=_existing_key) -> list[dict]:
    """`sources` plus every clause the answer cites that is not already there.

    `resolve` maps a cited (jurisdiction, key) to the key the index really has, so every
    chip opens something. Injectable so the parsing can be tested without an index.
    """
    out = list(sources)
    have = {(_bare_jurisdiction(str(s.get("jurisdiction", ""))), s.get("key")) for s in out}
    for block in _CITE_BLOCK.findall(answer or ""):
        jur = ""
        # one bracket may carry several refs: "[Argentina · A3.3(c); Argentina · A6.2]"
        for part in block.split(";"):
            if "·" in part:
                left, _, right = part.rpartition("·")
                name = left.strip().lstrip(_FLAGS).strip()
                if len(name) > _JUR_NAME_MAX:
                    continue          # prose with an interpunct, not a citation
                jur = name or jur
                key = _clause_key(right)
            else:
                key = _clause_key(part)   # "[Argentina · A3.3; A6.2]" — jurisdiction carries
            if not jur or not key:
                continue
            key = resolve(jur, key)
            if not key:
                continue                  # cites nothing this index holds
            ident = (_bare_jurisdiction(jur), key)
            if ident not in have:
                have.add(ident)
                out.append({"jurisdiction": jur, "key": key, "cited_only": True})
    return out


@dataclass
class AgentCtx:
    """Per-run state: clauses read (for citation) + an activity log the UI shows.

    `allowed` is the ENFORCED region scope (the user's product/jurisdiction
    selection ∩ entitlements). Tools resolve every region name against it and
    never search outside it — scope is a tool-layer guarantee, not a prompt
    request the model may ignore. If `queue` is set, activities are also pushed
    live for streaming to the UI.
    """

    sources: list[dict] = field(default_factory=list)
    activities: list[dict] = field(default_factory=list)
    queue: object = None  # asyncio.Queue | None
    allowed: list[str] | None = None  # enforced region-identity scope; None = all

    def emit(self, icon: str, text: str) -> None:
        act = {"icon": icon, "text": text}
        self.activities.append(act)
        if self.queue is not None:
            self.queue.put_nowait({"kind": "activity", **act})


def _resolve_scope(ctx: AgentCtx, requested: list[str] | None) -> list[str] | None:
    """Requested region names -> enforced identities.

    Names are resolved against ctx.allowed (bare 'France' matches the selected
    product's France). With no request, the whole allowed scope applies. A request
    that resolves to nothing inside the scope falls back to the scope — the agent
    can NARROW the user's selection, never escape it."""
    from aosphere_core_index.regions.region_map import match_identities
    from aosphere_core_index.service.registry import get_multi

    pool = ctx.allowed if ctx.allowed is not None else list(get_multi().regions)
    if not requested:
        return list(ctx.allowed) if ctx.allowed is not None else None
    resolved: list[str] = []
    for name in requested:
        resolved += match_identities(name, pool)
    resolved = list(dict.fromkeys(resolved))
    if resolved:
        return resolved
    return list(ctx.allowed) if ctx.allowed is not None else None


@function_tool
def search_clauses(
    wrapper: RunContextWrapper[AgentCtx], query: str, jurisdictions: str = ""
) -> str:
    """Search the legal index across jurisdictions. `jurisdictions` is an optional
    comma-separated filter (e.g. 'France,Germany'); empty = all in scope."""
    from aosphere_core_index.service.registry import search_all

    requested = [j.strip() for j in jurisdictions.split(",") if j.strip()] or None
    juris = _resolve_scope(wrapper.context, requested)
    scope = (f" in {jurisdictions}" if requested
             else f" across {len(juris)} selected regions" if juris
             else " across all regions")
    wrapper.context.emit("🔍", f"Searching “{query}”{scope}")
    hits = [h for h in search_all(query, k=_SEARCH_K, jurisdictions=juris)
            if h["score"] >= _SEARCH_MIN_SCORE]  # threshold out unrelated content
    wrapper.context.emit("→", f"Found {len(hits)} relevant clauses (≥{_SEARCH_MIN_SCORE})")
    if not hits:
        return "No sufficiently relevant clauses found. Consider rephrasing the query."
    def _fmt(h):
        tag = "🔔 ALERT " if h.get("kind") == "alert" else ""
        return (f"{tag}[{h['jurisdiction']} · {h['key']}] {h['title']} "
                f"(score {h['score']}) — {h['snippet'][:280]}")
    return "\n".join(_fmt(h) for h in hits)


# Element kinds a clause READ must leave out. Everything else is content: 'body' and
# 'answer' are the same thing from two producers, and 'bullet'/'summary'/'readernote'/
# 'table' all carry substance the agent is expected to quote.
_SKIP_ELEMENT_KINDS = frozenset()


def _read_alert(ctx: AgentCtx, jurisdiction: str, clause_key: str, bundle) -> str:
    """Read an alert (ALERT:<id>) — title, impact, summary, attachment text, and the
    clauses it maps to — and record it as a source. Alerts are time-sensitive
    regulatory news, not clauses."""
    aid = clause_key.split(":", 1)[1]
    rec = bundle.content.get("alerts_by_id", {}).get(aid)
    if not rec:
        ctx.emit("🔔", f"Reading alert {jurisdiction} · {clause_key} (not found)")
        return f"Not found: {jurisdiction} {clause_key}"
    ctx.emit("🔔", f"Reading alert {jurisdiction} · {clause_key} {rec.get('title', '')[:40]}")
    if not any(s["jurisdiction"] == jurisdiction and s["key"] == clause_key for s in ctx.sources):
        ctx.sources.append({"jurisdiction": jurisdiction, "key": clause_key,
                            "title": rec.get("title", ""),
                            "region": bundle.content.get("doc", {}).get("region")})
    parts = [f"[{jurisdiction} · {clause_key}] ALERT: {rec.get('title', '')}",
             f"Impact: {rec.get('impact', '')} | Date: {rec.get('date', '')}",
             rec.get("summary", "")]
    att = rec.get("attachment_text", "")
    if att:
        parts.append("ATTACHMENT:\n" + att[:6000])
    if rec.get("mapped"):
        parts.append("Related clauses: " + ", ".join(f"{jurisdiction}:{m[0]}" for m in rec["mapped"]))
    return "\n".join(p for p in parts if p)


def _read_guidance(ctx: AgentCtx, jurisdiction: str, clause_key: str, bundle) -> str:
    """Read a standalone curated-guidance entry (GUID:<id>) and record it as a
    source. These are lawyer-curated Q&As that no clause anchors — expert content
    in their own right."""
    gid = clause_key.split(":", 1)[1]
    rec = bundle.content.get("guidance_by_id", {}).get(gid)
    if not rec:
        ctx.emit("💡", f"Reading guidance {jurisdiction} · {clause_key} (not found)")
        return f"Not found: {jurisdiction} {clause_key}"
    ctx.emit("💡", f"Reading guidance {jurisdiction} · {rec.get('subject', '')[:40]}")
    if not any(s["jurisdiction"] == jurisdiction and s["key"] == clause_key for s in ctx.sources):
        ctx.sources.append({"jurisdiction": jurisdiction, "key": clause_key,
                            "title": rec.get("question") or rec.get("subject", ""),
                            "region": bundle.content.get("doc", {}).get("region")})
    parts = [f"[{jurisdiction} · {clause_key}] CURATED GUIDANCE",
             f"Subject: {rec.get('subject', '')} | Question: {rec.get('question', '')}",
             rec.get("answer", "")]
    if rec.get("linked"):
        parts.append("Related clauses: " + ", ".join(f"{jurisdiction}:{k}" for k in rec["linked"]))
    return "\n".join(p for p in parts if p)


def _read_one(ctx: AgentCtx, jurisdiction: str, clause_key: str) -> str:
    """Read one clause's full text (with its sub-clauses) and record it as a source."""
    from aosphere_core_index.embeddings.section_index import strip_scope_notes
    from aosphere_core_index.service.registry import get_bundle

    # Resolve the (possibly bare) name to an in-scope identity: 'Australia' with a
    # Shareholding Disclosure selection reads 'Shareholding Disclosure — Australia',
    # not the Data Privacy doc. Unknown names fail below with a clear message.
    resolved = _resolve_scope(ctx, [jurisdiction])
    if resolved and jurisdiction not in resolved:
        jurisdiction = resolved[0]
    bundle = get_bundle(jurisdiction)
    if clause_key.startswith("ALERT:"):
        return _read_alert(ctx, jurisdiction, clause_key, bundle)
    if clause_key.startswith("GUID:"):
        return _read_guidance(ctx, jurisdiction, clause_key, bundle)
    sec = bundle.sections_by_key.get(clause_key)
    if sec is None:
        ctx.emit("📖", f"Reading {jurisdiction} · {clause_key} (not found)")
        return f"Not found: {jurisdiction} {clause_key}"
    ctx.emit("📖", f"Reading {jurisdiction} · {clause_key} {sec['title'][:40]}")
    if not any(s["jurisdiction"] == jurisdiction and s["key"] == clause_key for s in ctx.sources):
        region = bundle.content.get("doc", {}).get("region")
        ctx.sources.append({"jurisdiction": jurisdiction, "key": clause_key,
                            "title": sec["title"], "region": region})
    import re as _re
    sections = bundle.content["sections"]
    idx = {s["key"]: i for i, s in enumerate(sections)}
    parts, lvl = [], sec["level"]
    read_keys, refs = [], set()
    for s in sections[idx[clause_key]:]:
        if s is not sec and s["level"] <= lvl:
            break
        # Include every element that carries text — tables carry the answers, and
        # content.json is already reduced to this jurisdiction's rows at export.
        # Strip editorial "Do not address in this Part …" scope-notes: they state
        # what a Part omits, not what the law requires, and mislead the agent.
        #
        # An ALLOW-LIST of kinds silently starved the agent. The docx export writes
        # 'body'; the extraction pipeline writes 'answer' (plus 'bullet'), so on an
        # extracted index this read returned 0 characters for a clause whose prose was
        # 1,405 — the agent could see nothing but tables, and said so confidently.
        # A deny-list keeps that from happening again when a new element kind appears:
        # anything with text is content unless we know it is not.
        cells = [strip_scope_notes(e["text"]) for e in s["elements"]
                 if e["kind"] not in _SKIP_ELEMENT_KINDS and e["text"]]
        body = " ".join(c for c in cells if c)
        if body:
            parts.append(f"[{s['key']}] {s['title']}: {body}")
        read_keys.append(s["key"])
        refs.update(s.get("cites_out", []))
        # Part-level pointers ("see Part G Breach Response") aren't [A-K]\d refs, so
        # the edge isn't pre-built — recover it from the prose so the agent can follow.
        refs.update(_re.findall(r"\bPart ([A-K])\b", body))
    cap = int(os.getenv("ACI_READ_CHARS", "12000"))
    text = f"[{jurisdiction} · {clause_key}] {sec['title']}\n" + ("\n".join(parts)[:cap] or sec["title"])

    # Graph neighbourhood: curated guidance attached to these clauses + outgoing
    # references — so the agent can traverse to related content for a full answer.
    by_clause = bundle.content.get("answers_by_clause", {})
    guid = []
    for k in read_keys:
        for a in by_clause.get(k, []):
            ans = (a.get("answer") or "").strip()
            if ans:
                guid.append(f"[{k}] {a.get('subject', '')}: {ans}")
    if guid:
        text += "\n\nGUIDANCE (curated answers for this clause):\n" + "\n".join(guid)[:4000]
    # Sibling clauses (same parent) are natural neighbours — the DPO section's
    # "Notification" clause sits beside "When to appoint one", so a "notify the
    # regulator about the DPO" question is answered by a sibling, not the clause
    # that matched. Templates routinely omit the explicit cross-reference, so
    # surface siblings too (with titles, so the agent can pick the right facet).
    parent = sec.get("parent_key")
    if parent:
        refs.update(s["key"] for s in sections
                    if s.get("parent_key") == parent and s["key"] not in read_keys)
    by_key = {s["key"]: s for s in sections}
    related = sorted(r for r in refs if r not in read_keys)
    if related:
        def _label(r: str) -> str:
            t = by_key.get(r, {}).get("title", "")
            return f"{jurisdiction}:{r}" + (f" ({t})" if t else "")
        text += ("\n\nRELATED clauses (read these too if relevant — for exceptions/"
                 "exemptions/detail, or a sibling that answers a different facet of "
                 f"the question): {', '.join(_label(r) for r in related)}")
    return text


@function_tool
def read_clauses(wrapper: RunContextWrapper[AgentCtx], refs: str) -> str:
    """Read the full text of one or more clauses in ONE call. `refs` is a semicolon-
    separated list of 'Jurisdiction:ClauseKey' pairs, e.g.
    'Germany:H1.4.4; France:H1.4; Italy:H1.4' (a single 'Germany:H1.4.4' is fine too).
    Always pass every clause you need here at once — never read clauses across calls."""
    out, n = [], 0
    for raw in refs.split(";"):
        # normalise "·" separators and any spaces around colons
        raw = ":".join(p.strip() for p in raw.replace("·", ":").split(":")).strip(":")
        if not raw:
            continue
        # Alert/guidance refs are "Jurisdiction:ALERT:<id>" / "Jurisdiction:GUID:<id>"
        # — keep the compound key intact (rpartition would split at the last colon).
        up = raw.upper()
        for tag in (":ALERT:", ":GUID:"):
            if tag in up:
                i = up.index(tag)
                jur, key = raw[:i], raw[i + 1:]
                break
        else:
            jur, _, key = raw.rpartition(":")
        jur, key = jur.strip(), key.strip()
        if not jur or not key:
            continue
        out.append(_read_one(wrapper.context, jur, key))
        n += 1
    if not out:
        return "No valid 'Jurisdiction:ClauseKey' refs parsed. Example: 'Germany:H1.4.4; France:H1.4'."
    return "\n\n".join(out)


def resolve_model(model: str | None) -> str:
    return model if model in _ALLOWED else DEFAULT_MODEL


def _scope_note(jurisdictions: list[str] | None) -> str:
    """Compact scope line for the seed. Scope is ENFORCED by the tools, so this is
    informational — it tells the model which product/jurisdiction names are valid
    without dumping 100+ qualified identities into the context (which the model
    then mistypes into tool calls)."""
    if not jurisdictions:
        return ""
    from aosphere_core_index.regions.region_map import split_region

    by_product: dict[str, list[str]] = {}
    for ident in jurisdictions:
        p, j = split_region(ident)
        by_product.setdefault(p, []).append(j)
    parts = []
    for p, js in by_product.items():
        names = ", ".join(sorted(js)) if len(js) <= 15 else f"{len(js)} jurisdictions"
        parts.append(f"{p} ({names})")
    return ("\n\n(Scope — searches and reads are restricted to: " + "; ".join(parts) +
            ". Use bare jurisdiction names like 'France' in tool calls.)")


def _context_note(context: str | None) -> str:
    """Confirmed scenario facts for the seed, from a guided interview (see
    interview/engine.answer_context).

    Placed in the USER turn, after the question and before the scope line — NOT in the
    system prompt. Two reasons. The system prompt is the SME-authored master prompt for
    this product (AOSNG-3442) and appending to it would silently edit what an SME wrote.
    And these facts belong to ONE question: a follow-up in the same session carries its
    own turn, so a scenario stated here cannot leak into a later, different one.
    """
    return f"\n\n{context.strip()}" if context and context.strip() else ""


async def run_agent_async(
    query: str, jurisdictions: list[str] | None = None, model: str | None = None,
    explain: bool = False, *, products: list[str] | None = None,
    context: str | None = None,
) -> dict:
    """Run the agent and return {answer, sources, model}. `explain` asks for the
    reasoning; the default is a brief, lead-with-the-answer reply.

    `products` is the request's PRODUCT scope, carried separately from `jurisdictions`
    because it decides which master prompt applies (AOSNG-3442). It cannot be recovered
    downstream: the API flattens (products x jurisdictions) into qualified region ids before
    the agent is reached, so the count — and "exactly one product" is the whole rule — is
    already lost by then."""
    chosen = resolve_model(model)
    resolved = _resolve_prompt(products, explain)
    agent = Agent[AgentCtx](
        name="aosphere-core-index", instructions=instructions_for(explain, resolved.prompt,
                                                   resolved.owns_format),
        tools=[search_clauses, read_clauses],
        model=LitellmModel(model=f"bedrock/converse/{_invoke_id(chosen)}"),
        model_settings=ModelSettings(temperature=_TEMP),
    )
    ctx = AgentCtx(allowed=jurisdictions)
    seed = query + _context_note(context) + _scope_note(jurisdictions) + _style_note(
        explain, custom=not resolved.is_default and resolved.owns_format)
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, seed, context=ctx, max_turns=_MAX_TURNS), timeout=_RUN_TIMEOUT
        )
    except (asyncio.TimeoutError, TimeoutError):
        return {"answer": f"The model '{chosen}' timed out. Try a faster model (e.g. Amazon Nova "
                          f"Pro/Lite).", "sources": ctx.sources, "model": chosen, "error": True}
    except Exception as e:
        if "MaxTurns" in type(e).__name__:  # hit the tool-round cap
            return {"answer": f"'{chosen}' needed more than {_MAX_TURNS} tool rounds. Raise "
                              f"ACI_AGENT_MAX_TURNS or try a more decisive model.",
                    "sources": ctx.sources, "activities": ctx.activities, "model": chosen, "error": True}
        raise
    return {"answer": result.final_output,
            "sources": _with_cited_sources(result.final_output, ctx.sources),
            "activities": ctx.activities, "model": chosen}


async def run_agent_stream(query: str, jurisdictions: list[str] | None = None,
                           model: str | None = None, session_id: str | None = None,
                           explain: bool = False, *,
                           products: list[str] | None = None,
                           context: str | None = None):
    """Async generator of events for SSE: {kind: 'activity'|'answer'|'error', ...}.

    Activities (searches / clause reads) are pushed live as the agent works, then
    a final 'answer' event with the cited answer + sources. If session_id is given,
    prior turns are prepended so follow-up questions keep context. `explain` asks for
    the reasoning; the default is a brief, lead-with-the-answer reply.
    """
    chosen = resolve_model(model)
    q: asyncio.Queue = asyncio.Queue()
    ctx = AgentCtx(queue=q, allowed=jurisdictions)
    resolved = _resolve_prompt(products, explain)
    agent = Agent[AgentCtx](
        name="aosphere-core-index", instructions=instructions_for(explain, resolved.prompt,
                                                   resolved.owns_format),
        tools=[search_clauses, read_clauses],
        model=LitellmModel(model=f"bedrock/converse/{_invoke_id(chosen)}"),
        model_settings=ModelSettings(temperature=_TEMP),
    )
    seed = query + _context_note(context) + _scope_note(jurisdictions) + _style_note(
        explain, custom=not resolved.is_default and resolved.owns_format)
    history = _session_history(session_id)
    agent_input = history + [{"role": "user", "content": seed}]
    yield {"kind": "activity", "icon": "🧠", "text": f"Thinking with {chosen}…"}
    task = asyncio.create_task(Runner.run(agent, agent_input, context=ctx, max_turns=_MAX_TURNS))
    loop = asyncio.get_event_loop()
    start = loop.time()
    while not task.done():
        try:
            yield await asyncio.wait_for(q.get(), timeout=0.4)
        except asyncio.TimeoutError:
            pass
        if loop.time() - start > _RUN_TIMEOUT:
            task.cancel()
            yield {"kind": "error", "text": f"'{chosen}' timed out — try a faster model."}
            return
    while not q.empty():
        yield q.get_nowait()
    try:
        result = task.result()
        if session_id:  # persist conversation for follow-ups (bounded, TTL'd)
            _session_store(session_id, result.to_input_list())
        yield {"kind": "answer", "answer": result.final_output,
               "sources": _with_cited_sources(result.final_output, ctx.sources),
               "model": chosen}
    except Exception as e:
        name = type(e).__name__
        if "Timeout" in name:
            msg = f"'{chosen}' was too slow to respond. Try a faster model (e.g. Nova Lite, GPT-OSS 120B)."
        elif "MaxTurns" in name:
            msg = f"'{chosen}' needed more than {_MAX_TURNS} tool rounds — try a more decisive model."
        else:
            log.exception("agent run failed (model=%s)", chosen)  # full error+traceback to logs
            msg = "AI Mode hit an internal error. Please try again."
        yield {"kind": "error", "text": msg, "sources": ctx.sources}
