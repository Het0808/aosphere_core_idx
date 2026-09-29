# Guided interview API (Marketing Restrictions)

## Problem

A Marketing Restrictions question is usually asked in a form the corpus cannot answer
definitively:

> *"Can I market my fund in Jersey?"*

The Part B memo has no answer to that, and it is right not to. It carries a **different
route for each scenario**: passive marketing (reverse enquiry) is a separate section from
active marketing; a closed-ended fund has different disclaimers from an open-ended one;
inside the EEA the question is not open- vs closed-ended at all but whether the fund is
passported, going via NPPR, or being pre-marketed before notification — and for active
marketing, whether the marketer holds an EEA passport. Answered as asked, the model either
hedges ("it depends on…") or silently picks one route and sounds certain about it.

The interview collects the five facts that select the route, then hands the answer path a
scenario it can be definitive about.

| Field | Decides |
|---|---|
| Jurisdiction | which memo, and which branch of the vocabulary |
| Marketing activity | which sections apply, and whether the remaining fields apply at all |
| Product / service category | which route through the memo (structure, or EEA passporting status) |
| Who is marketing | EEA + active marketing only — passporting rights turn on the marketer |
| Investor type | the audience-specific conditions |

## Where it came from

Extracted from the **Ask MRAM POC** (`MRAM-AI-Assistant`): the routing prompt and the
guided-interview state machine, which lived in `artifacts/api-server/src/routes/dev-intent.ts`,
`dev-scoping.ts`, `lib/scoping-definitions/` and (as React state) `pages/Development.tsx`.
The definitions are verbatim; the conservative rules are verbatim; the flow is the same.

Four things changed in the port, each because core-index is not the POC:

1. **88 jurisdictions, not 6.** The POC named Bahamas, Jersey, Indonesia, UK, Hungary and
   Ireland in its prompt, so it could not have extracted a seventh. The option list now
   comes from the index, and the jurisdiction spellings are core-index's own
   (`United Kingdom`, not `UK`).
2. **The EEA branch is membership, not a pilot list.** `EEA_JURISDICTIONS = ["Hungary",
   "Ireland"]` became the actual EEA — the EU 27 plus Iceland, Liechtenstein and Norway.
   Around 22 of 88 take the EEA branch locally; the exact count depends on which members
   the deployed corpus carries as standalone jurisdictions, and
   `scripts/try_interview.py --eea` prints the split for any environment.

   Three exclusions are deliberate rather than oversights: the **United Kingdom** (left the
   EEA), **Switzerland** (EFTA, never joined — the one most often assumed otherwise) and
   the corpus's collective **`EU Member States`** entry, whose document is the European
   *framework* memo and carries none of the Part B sections a confirmed scenario routes
   to. Gibraltar, Guernsey, Jersey, the Isle of Man and Monaco are likewise not EEA.
3. **Section routing.** The POC's corpus was a flat chunk store. Core-index holds the memo
   section by section, and the sections are named after the very things the interview
   establishes — consistently across all 88 jurisdictions. A completed interview therefore
   *names the clauses that answer it* (see below).
4. **Bedrock in the EU**, not OpenAI, and Claude Haiku 4.5 by default
   (`ACI_INTERVIEW_MODEL`). Converse has no JSON mode, so the JSON contract is prompt-only
   and parsed leniently.

## Shape

```
interview/vocabulary.py   the controlled values and their definitions — data, no logic
interview/engine.py       the deterministic interview — pure, no model, no I/O
interview/router.py       the two model calls — prompt and parse only
service/app.py            four endpoints
```

**The model maps wording; the engine decides everything else.** That split is the whole
design. A model that also drove the flow could talk itself into skipping a question, and
the cost of a wrongly-skipped question is a confident answer to a scenario the user never
described. So every value the model returns is re-validated against the vocabulary, and
every "what next" decision is taken from the validated values by pure code.

The API is **stateless**. The client holds the transcript and the slots and posts them back
each turn; the server validates, merges and says what to ask next. Posted slots are not
trusted — a category from the wrong branch is dropped and its question re-asked, so a
client cannot post its way past a question. (AI Mode's in-process session store already
carries a single-process caveat; an interview is long-lived enough to land on another pod
mid-flow.)

## Endpoints

All on `require_access`, the same gate as `/api/search`.

### `GET /api/interview/schema`

The vocabulary, once. Fields, question copy, every option list, all definitions (with
`drafted` marking wording that is ours rather than the definitions document's), the
context terms, and the jurisdictions with an `eea` flag each. With this, picking an
option, showing a definition and following the branch need **no model call at all**.

### `POST /api/interview/route`

`{messages, slots, signals}` → intent, extracted slots, and either the next question or a
hand-off.

```jsonc
{
  "intent": "permitted_activity",         // | capability | other
  "other_kind": null,                     // disclaimers|definitions|penalties|licensing|regulator
  "slots":  {"jurisdiction": "Ireland", "activity": null, ...},
  "signals": {"product_kind": "fund", "category_hint": null, "activity_hint": {...}},
  "acknowledgement": "Got it — Jurisdiction: Ireland.",
  "next_question": {
    "field": "activity",
    "question": "…Is it active marketing/selling, or passive marketing/selling?",
    "options": [{"value": "General Marketing",
                 "definition": "…",
                 "excluded_reason": "This question is about a fund. General Marketing is
                                     firm- or brand-level promotion…"}]
  },
  "complete": false,
  "handoff": null
}
```

Excluded options are **returned, not filtered**: an option that silently vanishes reads as
a bug, while one greyed out with a reason teaches the vocabulary.

### `POST /api/interview/next`

`{slots, signals, original_question}` → the next question, or the hand-off. **No model
call.**

This is what a *clicked option* uses, and it is separate from `/route` deliberately:
choosing from a list needs no wording mapped, so routing a click through the extractor
would spend a Bedrock call and ~1.5s per step to be told what the client already knows —
five times per interview. `/route` is for free text; this is for choices. The posted slots
are re-validated either way.

### `POST /api/interview/triage`

What a free-text turn IS while a question is pending. Three lanes, three handlings:

| Lane | Handling |
|---|---|
| `answer` | post it to `/route`; the interview advances |
| `scoping_question` | `answer` explains the term from the controlled definitions; state untouched, same field re-asked. `has_answer: true` means the turn also stated a value, so send it to `/route` as well |
| `route_onward` | a genuine regulatory question — suspend the interview (keep the slots), answer the aside, resume |

Mis-laning is asymmetric: a wrong `answer`/`route_onward` abandons the pending field. So a
turn that clearly asks what one of the interview's own terms means, while a field is
pending, is **forced** into the scoping lane by a deterministic backstop (question mark +
definition stem + an interview term, and no permission wording).

### `POST /api/interview/handoff`

`{slots, original_question}` → the confirmed scenario. **No model call** — the same
composition `/route` returns on completion, exposed for a client that collected every
answer by clicking options.

```jsonc
{
  "jurisdiction": "Ireland",
  "query": "In Ireland, is active marketing or selling of an AIF marketed under the
            national private placement regime (NPPR) by an EEA passporting entity
            permitted, targeting professional clients? If so, what conditions…",
  "scenario": "Assess this permitted-activity scenario:\n- Jurisdiction: Ireland\n…",
  "definitions": "Definitions of the parameters selected for this check: …",
  "section_hints": ["Marketing Activities", "Private Placement Regime", …],
  "sections": [{"key": "S14", "title": "PRIVATE PLACEMENT REGIME"}, …],
  "original_question": "we want to pitch our private equity fund to pension schemes…"
}
```

Pass `query` (or `scenario`) to `/api/agent` with `jurisdictions=<jurisdiction>` and
`products=Marketing Restrictions - Asset Management`. Four separate things are handed over
because they do four different jobs:

- **`query`** — the retrieval wording, in the memo's own vocabulary.
- **`scenario`** — the LLM-facing statement of confirmed facts. Not a second question: the
  answer prompt already frames the task, and a repeated determination clause gives the
  model two subtly different questions to answer.
- **`definitions`** — only the values actually selected, so an answer about Jersey never
  has "Funds - Passported AIF" in front of it.
- **`original_question`** — carried through unchanged, because it holds the detail the
  controlled vocabulary has no slot for (the channel, the strategy, the odd constraint),
  which is exactly the nuance a definitive answer has to address.

## Section routing

The memo's own sections are what the interview selects:

| Interview value | Sections |
|---|---|
| Passive Marketing/Selling | `PASSIVE MARKETING (REVERSE-ENQUIRY)` |
| Active Marketing/Selling | `MARKETING ACTIVITIES`, `MARKETING/SELLING TO THE PUBLIC`, `PRIVATE PLACEMENT REGIME` |
| Pre-Marketing of Funds | `MARKETING ACTIVITIES`, `PRIVATE PLACEMENT REGIME` |
| any EEA jurisdiction | + `SPECIFIC ISSUES RELATING TO THE IMPLEMENTATION OF AIFMD`, `PROSPECTUS REGULATION (PR3)` |
| `other_kind: disclaimers` | `Disclaimers: <category>` first, then `Disclaimers: Other` |
| `other_kind: penalties` | `PENALTIES/SANCTIONS` |
| `other_kind: licensing` | `LICENCE` |
| `other_kind: regulator` | `SOURCES OF LAW; GUIDANCE; COMPETENT REGULATOR` |

`other_kind` is the one addition to the POC's routing contract. Its prompt already
described five carve-outs that are informational rather than permission questions
(disclaimers, definitions, penalties, licensing, regulator facts) but did not say *which*
one had fired — and each has its own memo section, so returning it turns a carve-out into
a retrieval target. A `definitions` question maps to no section on purpose: it is answered
from this package's vocabulary, not the memo.

Titles are matched case-insensitively (the corpus carries both `PASSIVE MARKETING
(REVERSE-ENQUIRY)` and the title-case form) and resolve to real `{key, title}` pairs for
that jurisdiction. A jurisdiction that simply lacks a section yields nothing rather than a
dead key, and an unavailable bundle costs a hint, never the answer.

**Hints, not filters.** Retrieval still searches the whole jurisdiction: narrowing to these
outright would answer a cross-referenced question from half the memo.

## The conservative rules that must not be relaxed

- **Active vs passive is never extracted.** It turns *entirely* on who made the initial
  contact, which wording almost never reveals — "we're flying out to sign up a big
  investor" says nothing about who reached out first, however energetic it sounds. The
  extractor may only ever commit `General Marketing` or `Pre-Marketing of Funds`; Active
  and Passive are offered as a **hint** that re-asks with both options named. A hint is
  advisory by construction, which is what makes model variance here harmless.
- **Indirect fund wording never sets the category.** SICAV, unit trust, investment trust,
  hedge fund, private equity: none of them state the structure. Hedge fund and private
  equity produce a hint; the rest produce nothing. This one needed a **deterministic
  backstop**, added after a live run: asked *"can I market my hedge fund to pension schemes
  in Jersey"*, Haiku 4.5 committed `Funds - Open-Ended` outright with the rationale "hedge
  funds are typically open-ended structures" — against the instruction two paragraphs above
  it in its own prompt. Typically is not a compliance fact, and the category decides which
  route answers the question, so a committed structure with no explicit open-/closed-ended,
  UCITS or OEIC behind it in the conversation is demoted to a hint (`_demote_category`),
  the same way an Active/Passive value is.
- **The explainer cannot decide permission.** It is grounded only in the controlled
  definitions and is barred from saying whether anything is permitted — a verdict reached
  there would bypass retrieval and citations entirely.
- **A dead model is a 503, not a lane.** Every lane sends the user somewhere different; a
  guessed lane on a dead model answers the wrong question convincingly.

## Verified behaviour (live, Haiku 4.5, eu-west-1)

| Input | Result |
|---|---|
| "I want help with marketing" | `permitted_activity`, nothing extracted → interview starts |
| "an investor in **Britain** emailed me out of the blue" | `United Kingdom`; passive **hint** |
| "we're heading over to Jersey to sign up a big investor for our hedge fund" | `Jersey`, `product_kind=fund`, category **hint** open-ended; activity left unset |
| "we provide discretionary portfolio management … Indonesia" | `Indonesia` + `IMAS`, rationale: *"discretionary portfolio management is an investment management service, not a fund"* |
| "written disclaimers for a closed-ended fund sold in the Bahamas using the PPR" | `other` / `disclaimers` → `Disclaimers: Closed-Ended Fund` |
| "we're an EEA AIFM marketing our passported AIF into Ireland to professional clients" | `Ireland`, `Funds - Passported AIF`, `Professional Client` |
| "marketing a UCITS in **Malta**" | `Malta` on the EEA branch; category left unset (bare UCITS, no passporting status) |
| "I've got an investor who has relocated, how does that affect things?" | `other` — no activity proposed |
| "does an OEIC count as open-ended?" (mid-interview) | `scoping_question`, explained from the definitions |
| "can I cold-call investors in Jersey?" (mid-interview) | `route_onward` |

A full live walk-through of *"we want to pitch our private equity fund to pension schemes
in Ireland"* took **three** questions (activity → category → who is marketing; jurisdiction
and investor type came from the wording, the latter with the rationale *"pension schemes
are professional/institutional investors under MiFID II"*) and produced five resolved
clause keys, including Ireland's two EEA-only sections.

## How to test it

**The whole flow, scripted** — four endpoints in the order a client calls them, answering
each question by picking the first option that is not ruled out:

```bash
scripts/try_interview.py                                   # against localhost:8000
scripts/try_interview.py --q "selling a UCITS in Malta"    # the EEA branch
scripts/try_interview.py --q "what are the penalties for marketing unregistered in Indonesia?"
scripts/try_interview.py --answer                          # also run the agent (a real run)
scripts/try_interview.py --jurisdictions Jersey             # filter applied: no jurisdiction question
scripts/try_interview.py --jurisdictions Bahamas,Ireland   # narrowed, still asked
scripts/try_interview.py --base https://<dev-url>
```

It prints what was extracted, which options were greyed out and why, the composed query,
the confirmed scenario and the clause keys it resolved to — so a wrong branch is visible
without reading logs.

**In the UI** (the local stack, `ACI_LOCAL_DATA=./data-r5 docker compose up -d`):

1. open the app, click **AI Mode**;
2. open the filter button (*"All products · all jurisdictions ▾"*) and uncheck everything
   under **Products** except *Marketing Restrictions - Asset Management*. This is the gate —
   with any other product also selected the question takes the plain path, by design;
3. ask something under-specified, e.g. *"can I market my hedge fund to pension schemes in
   Jersey"*. Expect: Jersey and Professional Investor picked up from the wording and
   acknowledged, General Marketing greyed out with its reason, and a category question
   tailored to *"probably open-ended … please confirm"*;
4. answer by clicking, or by typing. Worth trying while a question is pending:
   *"does an OEIC count as open-ended?"* (explained inline, question re-asked),
   *"can I cold-call investors in Jersey?"* (the interview parks itself and offers
   **Pick up where you left off**), and *"what can you ask about?"* (capability, inline);
5. at the end, check the scenario card, then the agent's activity list — it should read the
   sections the card named.

**The tests**: `.venv/bin/pytest tests/ -q -k interview` (the model is stubbed; no AWS
needed). The full suite is `.venv/bin/pytest tests/ -q`.

**When the router 503s locally**, the SSO session has almost certainly lapsed — the
container reads the mounted `~/.aws`, so `aws sso login --sso-session tools1` on the host
fixes it with no rebuild. The UI degrades to a plain answer rather than stranding you, so a
503 shows up as *"couldn't run the guided scoping just now"* rather than an error.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ACI_INTERVIEW_MODEL` | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` | the routing model |
| `ACI_INTERVIEW_REGION` | `ACI_BEDROCK_REGION`, else `eu-west-1` | Bedrock region |
| `ACI_INTERVIEW_TIMEOUT` | `45` | per-call read timeout, seconds |

`temperature` is **not sent to any model**, following `extract/ai_postprocess.py`: Sonnet 5
rejects it outright, and one config every model accepts cannot drift out of step with the
model list. The cost is that the model's own default sampling applies, so extraction is not
bit-deterministic — absorbed by design, because the values that vary at the margin are
hints the user confirms, not committed slots.

## Known rough edges

- The scoping explainer is more confident about **indirect fund wording** than the
  extractor is allowed to be: asked "is a SICAV open-ended?" it answers "yes, by
  definition", while the extractor deliberately refuses to infer a category from "SICAV".
  It only *explains* — the interview still asks — but the two speak with different
  confidence. Adding SICAV, unit trust and investment trust to `CONTEXT_TERMS` would settle
  it, and that is a content decision for the SMEs, not a code one.
- Investor-type definitions and several EEA category definitions are `drafted` (ours,
  pending review). They are flagged in `/api/interview/schema` so a reviewer can see which
  wording is authoritative — the activity and non-EEA category definitions are verbatim
  from the definitions document.
- Facet detection (the POC's channel/attribute nudges: telephone, fly-in, fly-out, internet
  & social media, fund structure, investment strategy) is **not** ported. `handoff` accepts
  a `facets` dict and threads it into the scenario, so it can be added without changing the
  contract.

## In AI Mode

The interview drives the AI Mode chat, and is what turns the hand-off from a payload into
an answer.

**When it runs.** Three product states, three behaviours — because the filter starts with
*every* product selected, so gating on "exactly Marketing Restrictions" alone left the
interview invisible by default:

| Product filter | Behaviour |
|---|---|
| **exactly** Marketing Restrictions | the interview, straight away |
| Marketing Restrictions **and others** (including the all-selected default) | classify first; if the question really is a permitted-activity one, **ask which product** before collecting five answers about funds. Choosing Marketing Restrictions narrows the sidebar and continues — reusing the classification, so it costs no second model call. Choosing another narrows the sidebar and answers plainly |
| not in scope at all | the plain path, and no router call |

The product question is asked **only** for a permitted-activity question. Anything else — a
definition, a capability question, a Data Privacy question that happened to be asked with
everything selected — takes the plain path untouched, which is what keeps *"what is a
DPO?"* from being interrupted to choose a product it has nothing to do with.

The cost of the middle row is one Haiku classification (~1.2s, ~$0.015) before an answer
that is itself a multi-call Sonnet agent loop — but it is paid on every question asked with
the default filter, including ones that turn out not to be about marketing at all. Narrowing
the product filter to Marketing Restrictions avoids it entirely.

**The jurisdiction question is not asked when it has already been answered.** Selecting a
region in the sidebar is the same statement as answering *"which jurisdiction are you asking
about?"*, so the filter is treated as an answer, not a hint. It travels on every interview
call (`jurisdictions`, a CSV) and narrows both the options offered and the jurisdictions the
extractor may return, so the two can never disagree:

| Sidebar filter | What happens |
|---|---|
| none (all jurisdictions) | the question is asked, with everything the product covers |
| **one** jurisdiction | the slot is **filled from the filter** and the question is skipped — the chat says *"Using Jersey from your jurisdiction filter"*, because the server's acknowledgement only covers what the extractor set and this value would otherwise appear from nowhere |
| several | the extractor may still resolve one from the wording; if not, the question is asked with **only those** options |

Several selected cannot be auto-resolved, and that is a property of the vocabulary rather
than a gap: the whole branch — category options, investor labels, whether *who is marketing*
is asked at all — turns on whether that ONE jurisdiction is in the EEA, so "Ireland and
Jersey" has no single answer. Naming one in the question is what collapses it.

Moving the filter to a **different** region discards what was collected: it described a
scenario in a jurisdiction the user is no longer asking about.

**The turn.** `sendChat` decides; `streamAnswer` streams. Splitting them is what lets the
interview hand a composed query to the same answer path the plain case uses — one
streaming implementation, not two.

```
type a question ──► /api/interview/route ──► question card (options as pills)
                                    │              │
                                    │              ├─ click an option ──► /api/interview/next  (no model call)
                                    │              └─ type something ───► /api/interview/triage
                                    │                                        ├─ answer          ──► /route
                                    │                                        ├─ scoping_question ─► explain inline, re-ask
                                    │                                        └─ route_onward     ─► park, answer, "pick up where you left off"
                                    └─ complete ──► scenario card ──► /api/agent/stream
```

**How the scenario reaches the model.** `POST /api/agent/stream` takes an `interview`
field — the confirmed **slots**, not prose. The server re-validates them, composes the
confirmed-facts block itself (`interview/engine.answer_context`), resolves the section
keys, and threads the result into the agent's seed. Three consequences worth stating:

- what the model is told is "CONFIRMED" is always a scenario the interview could have
  produced. A client cannot put arbitrary text behind that phrase;
- an **incomplete** scenario is ignored rather than half-stated — "these facts are
  confirmed" over a partial list invites the model to fill the gaps itself, which is the
  exact failure the interview exists to prevent;
- the block goes in the **user turn**, not the system prompt. The system prompt is the
  SME-authored master prompt for this product, and appending to it would silently edit what
  an SME wrote; and these facts belong to one question, so a follow-up in the same session
  cannot inherit them.

The answered jurisdiction also **replaces** the sidebar filter, server-side and client-side
alike: the user was asked which jurisdiction and said so, which is more specific than
whatever happened to be selected — and leaving the scope wide lets the agent answer from a
neighbour's memo while sounding just as certain.

**What the user sees before the answer.** A scenario card: the five confirmed values, the
memo sections the answer will come from (each a clickable clause key, opening *this
product's* memo via the qualified region id), and a **Change an answer** button. It is the
last chance to catch a wrong answer before an authoritative-looking reply is generated from
it, which is why the slots are echoed rather than only used.

**Measured, on the local stack.** *"In Jersey, is active marketing or selling of a
closed-ended fund permitted, targeting professional investors?"* with the confirmed scenario
attached: the agent searched once, then read `S14 PRIVATE PLACEMENT REGIME`,
`S15 MARKETING ACTIVITIES` and `S13 MARKETING/SELLING TO THE PUBLIC` — exactly the three
sections `section_hints` predicted — and answered *"Yes … permitted under the private
placement regime [Jersey · S14 7.1(a)]"*, with the open-/closed-ended distinction addressed
explicitly from the clause. That is the difference the interview is for: the same question
asked cold has no definitive answer.

**Degradation.** If the router is unavailable (503, or any failure), the page says so in one
line and answers the question as asked rather than stranding the user in a dead interview —
the router being down is not the answer path being down.
