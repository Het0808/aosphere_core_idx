#!/usr/bin/env python3
"""summary_ai_extract.py — transcribe ONE aosphere SPEED READ summary PDF with a VLM.

The MRAM "SUMMARY" documents are a different animal from the memoranda the rest of the
pipeline was built for. A memorandum is a numbered question/answer table; a summary is a
DESIGNED page — coloured bars, tinted panels, icons, bulleted prose — and its structure is
drawn rather than written. Nothing in the text layer says "this line is a heading": what
says so is the dark teal bar printed behind it.

That is why the deterministic path mis-reads them, and the failures are all of a kind.
Measured on SUMMARY__Argentina__32551 (out/corpus_pymupdf/.../01_stage1_extract/):

  * the cover's two columns interleave, so the Table of Contents lands in the middle of
    the Overview paragraph -- "Depending on the particular activities, it may be possible
    to conduct certain" / "Additional Considerations" / "general marketing activities...";
  * a bullet that wrapped becomes TWO bullets, split mid-sentence ("...addressed to the" +
    "particular recipient;");
  * every bar flattens to one level, so "1. Carrying on Business Restriction" -- a pale
    mint sub-bar -- outranks nothing and reads as a peer of "Active Marketing/Selling of
    Funds", the dark bar it sits under;
  * the coloured status squares vanish without trace. They are 20x16 embedded IMAGES, so
    no text extractor can see them, and they carry the verdict the paragraph relies on.

A vision model can see all four, because all four are visible. So this script does the
simplest thing that could work: render every page, hand the model the whole document at
once, and ask for Markdown.

WHOLE DOCUMENT IN ONE CALL, deliberately. A section's panel continues across a page break
and its bar is NOT reprinted at the top of the next page, so a page-at-a-time pass cannot
know which section it is inside. At 150 dpi an A4 page costs roughly 2.3k input tokens, so
even a 10-page summary is ~23k -- there is no context pressure to trade the correctness
away for.

WHAT IT DOES NOT DO is gate the output the way stage 4 does. Stage 4 repairs a table and
may therefore demand a character-identical stream, because every repair it allows is about
where a boundary falls. Transcription is not like that: a transcript legitimately differs
from the text layer by the joining of wrapped lines, by the reading order of the cover
(where the TEXT LAYER is the thing that is wrong), and by the bar text being promoted to a
heading. So the check here MEASURES and REPORTS instead of accepting or rejecting --
report.json carries the word coverage and, more usefully, the longest runs of words that
went missing or were added, which is what a prompt iteration actually needs to read.

Coverage is about PRESENCE, not order, for that same reason: the cover's text layer reads
in the wrong order by construction, so an order-sensitive score would condemn the one
place the model is expected to disagree with it.

The palette, measured off the PDFs with get_drawings() rather than guessed from a render:

    #005C54  (0, 0.361, 0.329)      section bar, 37pt tall, white bold serif + icon
    #369485  (0.212, 0.58, 0.522)   sub-section bar, 28pt, white bold sans   (Germany)
    #DAEEEA  (0.855, 0.933, 0.918)  sub-section bar, 28pt, teal bold sans    (Argentina)
    #E1F1EE  (0.882, 0.945, 0.933)  ALSO the tall "Speedread:" callout panel
    #F7F8F9  (0.969, 0.973, 0.976)  a very light grey intro panel

Note the collision on the last two: the same mint is used for a 28pt BAR and for a 167pt
PANEL of prose. The prompt separates them by shape (one short bold line vs paragraphs),
not by colour, because colour cannot separate them.

CHUNKING BY TITLE is the point of the heading levels, so --split (on by default) writes
the transcript out as a titled tree in the shape stage 3 already uses -- `NN-slug.md` per
`##`, a directory with 00-overview.md + README.md where a section has sub-headings, and a
"*Source: `x.pdf`, page A-B*" breadcrumb under every heading. That breadcrumb is a
contract, not decoration: chunk_eval reads the tree, check_content_localized.py audits the
line, and the review viewer navigates by it.

The page numbers in it are DERIVED FROM THE TEXT LAYER, never asked of the model -- see
attribute_pages. --split-only re-splits a transcript already on disk and calls nothing, so
tuning the tree is free.

What this buys over the deterministic path is depth. Stage 1's headings_manifest.json for
Argentina reports all nine headings at "level": 1, because its structure_source is a
"PyMuPDF 12pt-bold spine" and font weight cannot tell a dark bar from a pale one -- so
"1. Carrying on Business Restriction" is recorded as a PEER of the dark-bar section it sits
inside, and chunking by title gives nine siblings with the nesting gone. Reading the bar
colour gives real levels, and with them real parents. It also recovers sub-chunks the text
layer destroys: "Intermediaries" is teal text alone on its own line on page 4, and stage 1
glues it onto the sentence beneath ("**Intermediaries** An intermediary is not generally
required...") -- so "Additional Considerations" is one blob where the page offers four
titled parts.

    # richest document -- all three bar levels, all three square colours, Definitions
    python scripts/summary_ai_extract.py \
        "out/corpus_pymupdf/124_Marketing_Restrictions_-_Asset_Management/SUMMARY__Germany__33748/source.pdf"

    # cheap smoke test, 5 pages
    python scripts/summary_ai_extract.py \
        "out/corpus_pymupdf/124_Marketing_Restrictions_-_Asset_Management/SUMMARY__Argentina__32551/source.pdf" \
        --pages 1-3 --model sonnet

    # tune the chunk tree against a transcript already on disk, for nothing
    python scripts/summary_ai_extract.py <the same pdf> --split-only --depth 2

Every run writes the exact instruction it sent to prompt.txt beside the transcript, so a
prompt change is a diff rather than a memory.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    MODEL_ALIASES, bedrock_client, cost_usd, inference_config, resolve_model, response_text,
)
# The SAME failure reporting as the other two Bedrock call sites. Three sites reporting
# differently means a Kibana count of throttles depends on which kind of call happened to
# hit the limit -- and this route is the one that sends every page image in a single call,
# so it is the most likely to be throttled of the three.
from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    _attempts as _bedrock_attempts,
)
from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    _aws_error as _bedrock_aws_error,
)
from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    _call_fields as _bedrock_call_fields,
)

# ============================================================================
#  THE PROMPT — this is the bit to edit
# ----------------------------------------------------------------------------
#  Assembled by build_instr() as:
#      [page image] x N          every page, in order
#      INSTR                     the transcription rules
#      SQUARES        (optional, --markers tag)
#      FURNITURE_OMIT / _KEEP
#      TAIL                      "reply with the Markdown and nothing else"
#
#  Written to prompt.txt on every run. The lesson stage 4 paid for applies here
#  too: a rule that describes a SHAPE ("the section bar is 37 points tall") is
#  worse than one that describes a ROLE, because the documents are not all drawn
#  the same. Germany's sub-bar is white-on-mid-teal and Argentina's is
#  teal-on-pale-mint, and any instruction naming one colour gets the other wrong.
# ============================================================================

SYSTEM = ("You transcribe a page-designed legal PDF into Markdown. You copy what the page "
          "prints. You never summarise, never explain, and never write a word the page "
          "does not show.")

INSTR = """The images above are EVERY page of one document, in order. They are the ground truth.

Transcribe the whole document into Markdown, word for word.

This is a TRANSCRIPTION, not a summary and not a rewrite. Every word the pages print
appears in your output, spelled the way the page spells it, in the order the page reads it.
Never paraphrase, never shorten, never tidy a sentence, never correct what looks like a
mistake, never add a word of your own, and never write a note about what you did. Copy the
punctuation as printed — the curly quotes, the en dashes, the "/" in "marketing/selling".


# THE STRUCTURE IS DRAWN. READ IT OFF THE DESIGN.

This document is built out of coloured bars and tinted panels. THE BAR IS THE HEADING, and
its colour is what tells you the level. Use the colour of the bar, not your own sense of
what ought to nest under what.

A DARK TEAL BAR the full width of the panel, carrying white bold serif text and usually a
white line-drawn icon at its right-hand end — this is a top-level section. Write it as
`## Its Text`.

A LIGHTER BAR, also the full width, carrying ONE SHORT LINE of bold text — either white on
mid-teal or teal on pale mint — is a sub-section of the dark bar above it. Write it as
`### Its Text`. Those two shades mean the SAME level; the document simply uses different
ones in different places, so do not read the lighter of them as a deeper level.

A LINE OF BOLD OR TEAL TEXT ALONE ON ITS LINE, with no bar behind it and no sentence
continuing on the same line, is a labelled sub-part. Write it as `#### Its Text`.

Keep whatever number or letter the bar prints as part of the heading text — `### 1.
Carrying on Business Restriction`, `### A. UCITS`. Do not renumber, and do not drop it.

HEADINGS STAY IN THE ORDER THE PAGES PRINT THEM, start to finish. Where a top-level part
repeats the same sub-labels under each of several numbered sections — "1. A, B, C" then
"2. A, B, C" — reproduce them in that order, one numbered section finished before the
next begins. Never regroup by label across sections (every "A" heading first, then every
"B", then every "C") even where two sections' sub-headings sit side by side on the page —
see MULTI-COLUMN CONTENT below. The sequence is whichever order the pages read in, never
a grouping of your own.

A TINTED PANEL THAT HOLDS WHOLE PARAGRAPHS IS NOT A HEADING, however close its colour is
to a bar's. A bar is one short line of bold text; a panel is prose, and it is often several
paragraphs tall. This catches the pale mint "Speedread:" panels and the very light grey
intro panels: transcribe what is inside them as ordinary paragraphs, where they stand.

A SECTION CONTINUES ACROSS A PAGE BREAK and its bar is not reprinted at the top of the next
page. Where a page opens with prose and no bar, it is still inside the last section you
opened — do not invent a heading for it, and do not start a new one.


# FORMATTING INSIDE A SECTION

A paragraph is a paragraph: one blank line between paragraphs, and NO line break inside
one. These pages are justified, so nearly every paragraph wraps mid-sentence — join those
lines back into a single line. A word the wrap split in two is one word.

A bulleted list uses `-`, one item per bullet the page prints, indented two spaces for each
level of nesting. A BULLET THAT WRAPS IS STILL ONE BULLET: join it up, and never let the
tail of a wrapped bullet become a bullet of its own. The page draws its outer level with a
round bullet and sometimes an inner level with a short dash "–" — write both as `-` and let
the indentation carry the level.

Bold text is `**bold**` and italic text is `*italic*`. A bold or teal LEAD-IN followed by a
sentence on the same line stays part of that paragraph — "**Speedread:** To actively
market/sell...", "**Occasional:** there are no written rules..." — because it is a lead-in
and not a heading. Only a line with nothing after it becomes a `####`.

A word printed in teal inside a sentence is a defined term. Write it as `**bold**` and
leave it exactly where it sits.

A URL is plain text, exactly the characters printed. Do not turn it into a link, do not add
or drop a "www.", and do not repair one the page broke across a line.

Do not build a Markdown table unless the page draws a real grid of rows and columns. These
documents are prose in panels, not tables.


# MULTI-COLUMN CONTENT

A page is not always one column top to bottom — a bulleted list or a block of prose is
sometimes drawn as TWO (or more) side-by-side columns, the same as the cover. Read each of
them the same way: finish one column top to bottom, THEN start the next — never left to
right across the columns.

A SHORT BULLETED LIST is where this goes wrong most often, because two columns of brief
items look like a two-column TABLE — one row per pair — and invite reading row by row. They
are not rows; they are two independent lists that happen to sit side by side to save space.
Where the page prints:

    - brand awareness;              - product/service line awareness;
    - publishing articles;          - activities referring to an investment
                                       strategy/particular characteristics; and
    - speaking at a finance         - distribution of business cards.
      related event;

the correct reading is the LEFT column, top to bottom, in full, THEN the RIGHT column, top
to bottom:

    - brand awareness;
    - publishing articles;
    - speaking at a finance related event;
    - product/service line awareness;
    - activities referring to an investment strategy/particular characteristics; and
    - distribution of business cards.

Do NOT pair them left-right, left-right, row by row — "brand awareness", "product/service
line awareness", "publishing articles", "activities referring to..." — which is what a
two-column TABLE would read as, not what two side-by-side LISTS are. If a bulleted item and
the one beside it read as unrelated fragments of two different lists, that is column
layout, not a row; finish the left one before the right one exists at all.

This applies wherever the page draws columns, not only on the cover.


# THE COVER PAGE

Page 1 is a cover, and it is laid out in COLUMNS. Read it column by column and never
straight across the page, or the Table of Contents ends up spliced into the middle of the
Overview paragraph. Transcribe it in this order:

1. The header line, as `**SPEED READ** | CROSS-BORDER DISTRIBUTION | **<the date>**`, then
   the jurisdiction name beside the flag as `# <Jurisdiction>`.

2. The two gauges. Each is a five-band scale with the two ends labelled and ONE band
   marked by a small rounded tag above it. Give each a line of its own, exactly:
   `**<left end label> – <right end label>:** <the text in the tag>`
   for example `**SIMPLE – COMPLEX:** QUITE SIMPLE`. These readings exist ONLY as a
   drawing, so a transcript that leaves them out loses them entirely. Never guess one: if
   no tag is visible on a gauge, leave that line out.

3. The `**Overview:**` paragraph, and any paragraph beneath it, in full.

4. The Table of Contents, as `**Table of Contents**` followed by one `-` bullet per entry
   in the printed order, keeping any numbering the entry shows.

5. Whatever prose is printed below the columns, in full."""

# The squares are the one thing this pass adds that is not literal page text, so it is
# opt-out (--markers none). They are pictures: invisible to every text extractor, and
# they carry the paragraph's verdict. Asking for the COLOUR rather than its meaning is
# deliberate -- the colour is observation, the meaning would be inference, and the
# trailing "Permitted" / "Not permitted" in the prose already states the meaning.
SQUARES = """


# THE COLOURED SQUARES

Many paragraphs have a small coloured square printed to their left. It is a picture, so no
text extractor can see it, and it carries a verdict the paragraph relies on. Where a
paragraph has one, begin that paragraph with the square's colour in brackets:

    [green] **Active marketing/selling via UCITS marketing passport:** Permitted
    [amber] Workable depending on how activity structured
    [red] **Active marketing/selling a UCITS that has not been passported:** Not permitted

There are exactly THREE colours these squares are ever printed in — green, yellow and red
— never any other. The yellow one is the colour most often missed or misread as green, as
red, or as no square at all, so look carefully at every square before you decide, not only
the ones that are obviously green or red.

RED AND YELLOW ARE TELLING YOU APART BY DARKNESS, NOT JUST HUE — both can print as a
checkered or dotted texture built from two close shades, and the texture itself is not
the tell. A RED square reads DARK and SATURATED: a deep, strong orange-red throughout,
however it is textured. A YELLOW/amber square reads PALE and LIGHT by contrast: a washed-
out gold or straw yellow, never as deep or saturated as the red one. Where a page shows
both, compare them side by side before tagging either — the paler, lighter square is
yellow; the deeper, more saturated one is red. Do not let "it has a checkered pattern"
alone push you toward red — plenty of yellow squares are checkered too.

Use `[green]` for the striped green square, `[amber]` for the yellow one (yellow and gold
both count) and `[red]` for the red/orange one. Every square you see is one of these three
— report THE COLOUR YOU SEE and nothing else, do not translate it into words of your own,
and never put a tag on a paragraph that has no square beside it."""

FURNITURE_OMIT = """


# PAGE FURNITURE

Leave out the running footer that repeats on every page — the document reference, the page
number and the "Marketing Restrictions – Asset Management" running title — and leave out
the "aosphere" logo. Also leave out the cover's **Disclaimer:** block — it is aosphere's
standard subscription-service notice, not part of the document's own substance. Keep
everything else."""

FURNITURE_KEEP = """


# PAGE FURNITURE

Transcribe the running footer of each page where it appears, as its own italic line —
`*4130-0297-2520, v. 1 | 4 | Marketing Restrictions – Asset Management*` — and keep the
"aosphere" logo as the plain word `aosphere`. Leave out the cover's **Disclaimer:** block —
it is aosphere's standard subscription-service notice, not part of the document's own
substance."""

TAIL = """


Reply with the Markdown and nothing else. No code fence, no preamble, no closing remark,
and no count of what you found."""


def build_instr(markers: str, furniture: str) -> str:
    """The exact text sent after the page images. Written to prompt.txt every run."""
    parts = [INSTR]
    if markers == "tag":
        parts.append(SQUARES)
    parts.append(FURNITURE_OMIT if furniture == "omit" else FURNITURE_KEEP)
    parts.append(TAIL)
    return "".join(parts)


# ============================================================================
#  End of prompt. Plumbing below.
# ============================================================================


def _read_json(path: Path) -> dict | None:
    """A JSON file, or None. Never raises: a missing or broken corpus_meta.json means
    "this PDF is not in a job directory", which is a normal way to be called."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def parse_pages(spec: str | None, count: int) -> list[int]:
    """"1-3", "2,4,5", "1-2,7" or None -> a sorted list of 1-based page numbers.

    Out-of-range pages are dropped rather than raising: --pages 1-99 on a 5-page document
    means "all of it", which is the obviously intended reading.
    """
    if not spec or spec.strip().lower() == "all":
        return list(range(1, count + 1))
    want: set[int] = set()
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            a, _, b = piece.partition("-")
            want.update(range(int(a), int(b) + 1))
        else:
            want.add(int(piece))
    return sorted(p for p in want if 1 <= p <= count)


def render(pdf: Path, pages: list[int], dpi: int, cache: Path) -> list[tuple[int, bytes]]:
    """Every requested page as PNG bytes, cached on disk so a prompt re-run is free.

    150 dpi matches what stage 4 sends, and it is not an arbitrary match: below it the
    small teal defined terms and the tag text on the cover gauges stop being legible, which
    is precisely the detail this pass exists to read.
    """
    import fitz

    cache.mkdir(parents=True, exist_ok=True)
    out: list[tuple[int, bytes]] = []
    with fitz.open(str(pdf)) as doc:
        for p in pages:
            img = cache / f"page-{p:03d}.png"
            if img.exists():
                out.append((p, img.read_bytes()))
                continue
            data = doc[p - 1].get_pixmap(dpi=dpi).tobytes("png")
            img.write_bytes(data)
            out.append((p, data))
    return out


def pdf_text(pdf: Path, pages: list[int]) -> str:
    """The text layer of the requested pages, in the order PyMuPDF reports it.

    Reading order and all. It is the only independent record of WHICH WORDS the document
    contains, and that is the one question the coverage check asks of it.
    """
    import fitz

    with fitz.open(str(pdf)) as doc:
        return "\n".join(doc[p - 1].get_text() for p in pages)


def _page_text(pdf: Path, page: int) -> str:
    """One page's text layer. Separate from pdf_text because the per-page checks need the
    pages kept APART -- pdf_text joins them, and a phrase cannot be attributed to a page
    once they are concatenated."""
    import fitz

    with fitz.open(str(pdf)) as doc:
        return doc[page - 1].get_text() if 1 <= page <= doc.page_count else ""


def transcribe(client, model: str, images: list[tuple[int, bytes]], instr: str,
               max_tokens: int) -> tuple[str, dict, str]:
    """One call: every page image, then the instruction. Returns (markdown, usage, stop)."""
    parts: list[dict] = [{"image": {"format": "png", "source": {"bytes": data}}}
                         for _, data in images]
    parts.append({"text": instr})
    # THE CALL THAT CAN SIT SILENT FOR AN HOUR.
    #
    # This route sends every page image in ONE Converse call, against a client configured
    # read_timeout=900 with 6 adaptive attempts (see ai_postprocess.bedrock_client), so a
    # throttled or slow model is a 90-minute worst case during which this function prints
    # nothing at all. That is exactly what a wedged worker looks like from outside, and it is
    # the leading explanation for the MRAM jurisdictions that appeared stuck with nothing to
    # look at afterwards. The start event is the half that matters: it puts the model, the
    # page count and the payload size in Kibana BEFORE the wait, so a call that never returns
    # still says what it was doing.
    from aosphere_core_index.obs import log as _log

    pages_in = [p for p, _ in images]
    t0 = time.time()
    _bedrock_attempts.n = 0                                  # this call's own attempt count
    _log.event("bedrock.call.start",
               message=f"transcribe {len(images)} page image(s) via {model}",
               **{"aci.model": model, "aci.purpose": "summary_transcribe",
                  "aci.images": len(images),
                  "aci.page_first": min(pages_in) if pages_in else None,
                  "aci.page_last": max(pages_in) if pages_in else None,
                  "aci.payload_mb": round(sum(len(d) for _, d in images) / 1e6, 2),
                  "aci.max_tokens": max_tokens})
    try:
        # Registered for the duration so the heartbeat can report it: this route's single
        # call carries every page image and is the longest-running of the three.
        with _log.api_call("bedrock.converse",
                           **{"aci.api_model": model,
                              "aci.api_purpose": "summary_transcribe",
                              "aci.api_images": len(images)}):
            resp = client.converse(
                modelId=model,
                system=[{"text": SYSTEM}],
                messages=[{"role": "user", "content": parts}],
                inferenceConfig=inference_config(model, max_tokens),
            )
    except BaseException as e:
        _aws = _bedrock_aws_error(e)
        _log.exception("bedrock.call.end", e,
                       # The AWS CODE, not just the Python class: ThrottlingException and
                       # AccessDeniedException are both ClientError, and they need opposite
                       # responses.
                       message=(f"transcribe failed: {model}: "
                                f"{_aws.get('aci.aws_error_code') or type(e).__name__}"),
                       **{"aci.model": model, "aci.purpose": "summary_transcribe",
                          "aci.images": len(images),
                          **_bedrock_call_fields(t0), **_aws})
        raise
    u = resp.get("usage") or {}
    _log.event("bedrock.call.end",
               # A truncated transcription is a silently WRONG document, not a failure: the
               # scorecard measures coverage against the page text and a max_tokens stop is
               # the mechanism that loses the tail of a long one.
               level="warn" if resp.get("stopReason") == "max_tokens" else "info",
               message=f"transcribed in {round(time.time() - t0, 1)}s",
               **{"aci.model": model, "aci.purpose": "summary_transcribe",
                  "event.outcome": "success", "event.duration_s": round(time.time() - t0, 2),
                  "aci.images": len(images),
                  "aci.tokens_in": int(u.get("inputTokens") or 0),
                  "aci.tokens_out": int(u.get("outputTokens") or 0),
                  "aci.aws_request_id": (resp.get("ResponseMetadata") or {}).get("RequestId"),
                  "aci.stop_reason": resp.get("stopReason")})
    return (response_text(resp).strip(),
            {"in": int(u.get("inputTokens") or 0), "out": int(u.get("outputTokens") or 0)},
            resp.get("stopReason", ""))


# ---------------------------------------------------------------- coverage

# The marker tags are the one thing the model is ASKED to invent, so they are removed
# before either side is compared -- "green", "amber" and "red" are ordinary English words
# and would otherwise be counted as text the model added.
_MARKER = re.compile(r"\[(?:green|amber|red)\]")
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*|\s*```\s*$", re.IGNORECASE)


def words(text: str) -> list[str]:
    """Lowercase alphanumeric words. Markdown syntax falls out for free: `##`, `**` and
    `-` are not alphanumeric, so `**bold**` and `bold` give the same word.

    NFKD FIRST, for the reason spelled out on _key: these PDFs encode "fi" as the ligature
    U+FB01, so the text layer's "<fi>rm" and the model's "firm" are different strings. Left
    undecomposed, the first Germany run reported "firm", "firm" and "filing" as words the
    model had invented and the same three as words it had dropped -- three false diffs per
    document, from the checker rather than the model.
    """
    text = unicodedata.normalize("NFKD", _MARKER.sub(" ", text or "").lower())
    return re.findall(r"[0-9a-z]+", text)


def runs(a: list[str], b: list[str], limit: int = 12) -> tuple[list[str], list[str]]:
    """(longest runs only in `a`, longest runs only in `b`), as readable phrases.

    A count of missing words says how bad it is; a RUN says what it was. A dropped bullet
    shows up here as its own sentence, which is immediately diagnosable, where "37 words
    missing" is not.

    autojunk=False matters: on sequences this long SequenceMatcher's heuristic treats any
    word appearing in more than 1% of positions as junk, which on legal prose means "the",
    "of" and "marketing" -- and the matcher then reports spurious differences everywhere
    they occur.
    """
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    lost: list[tuple[int, str]] = []
    gained: list[tuple[int, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace") and i2 > i1:
            lost.append((i2 - i1, " ".join(a[i1:i2])))
        if tag in ("insert", "replace") and j2 > j1:
            gained.append((j2 - j1, " ".join(b[j1:j2])))
    lost.sort(key=lambda t: -t[0])
    gained.sort(key=lambda t: -t[0])
    fmt = lambda t: f"[{t[0]}] {t[1][:240]}"                              # noqa: E731
    return [fmt(t) for t in lost[:limit]], [fmt(t) for t in gained[:limit]]


def headings(md: str) -> list[str]:
    """Every `##`/`###`/`####` line, in order — the hierarchy at a glance.

    The whole point of the pass is whether the bars came through as levels, and that is
    read in ten seconds off this list rather than by scrolling the transcript.
    """
    return [ln.rstrip() for ln in (md or "").splitlines()
            if re.match(r"^#{1,6}\s", ln)]


def coverage(source: str, md: str, page_texts: dict[int, str] | None = None) -> dict:
    """How much of the text layer survived, and what did not.

    MEASURED, NOT ENFORCED. A transcript is expected to differ from the text layer in ways
    that are correct — wrapped lines joined, the cover read in column order instead of the
    text layer's broken one, bar text promoted to a heading — so there is no threshold here
    that could be both safe and useful. What the report gives instead is the runs, which
    say whether a difference is one of those or a dropped paragraph.
    """
    # THE RUNNING FURNITURE IS REMOVED FIRST, when the caller can supply the pages to find
    # it in. Left in, it dominates the measurement and hides the defects that matter: with
    # --furniture omit the prompt is TOLD to drop the footer, so Germany reported 91 words
    # "missing" -- all of them instructions being followed -- and a genuine one-word loss
    # was 1 part in 91 of a number nobody could interpret. Stripped, the same document
    # reports 2, and a single altered word doubles it.
    #
    # This is also the difference between a score that conflates COMPLIANCE with LOSS
    # (98.1) and one that measures only loss (99.96).
    if page_texts:
        shapes = furniture_lines(page_texts)
        source = "\n".join(strip_disclaimer(strip_furniture(page_texts[p], shapes), p)
                           for p in sorted(page_texts))
    a, b = words(source), words(md)
    lost, gained = runs(a, b)
    # BAG OF WORDS for the count the score uses, sequence alignment only for the runs it
    # reports. A block the model MOVED -- the cover's Table of Contents, put back in column
    # order instead of the text layer's broken order -- is a delete plus an insert to
    # SequenceMatcher and would be scored as both a loss and an invention. It is neither.
    matched = sum((collections.Counter(a) & collections.Counter(b)).values())
    return {
        "pdf_words": len(a),
        "out_words": len(b),
        "matched": matched,
        "pdf_chars": len("".join(a)),
        "out_chars": len("".join(b)),
        "missing_runs": lost,
        "added_runs": gained,
        "note": "presence, not order: `matched` is a multiset intersection, because the "
                "cover's own text-layer order is wrong and the model is expected to "
                "disagree with it there. The runs are sequence-aligned, so a MOVED block "
                "appears in both lists -- that is a reordering, not a loss.",
    }


# The typographic characters a model "tidies" without altering a single word. Split into
# the ones that appear ONLY in body text and the ones the running footer also uses --
# "Marketing Restrictions – Asset Management" carries an en dash on every page, so with
# --furniture omit a nine-page document is legitimately nine en dashes short and scoring
# them would punish following the instructions.
_QUOTES = ("“", "”", "‘", "’")          # “ ” ‘ ’ — body text only
_DASHES = ("–", "—", "…")                    # – — … — also page furniture


_SMART_TO_PLAIN = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
                   "\u2013": "-", "\u2014": "-", "\u2026": "..."}
_WSRUN = re.compile(r"\s+")


def _flat(s: str) -> str:
    """Whitespace collapsed to single spaces — the form both sides are compared in.

    Required, not cosmetic: the transcript JOINS lines the PDF wrapped, so a phrase that
    straddled a line break has a newline in the text layer and a space in the transcript.
    Comparing raw would report every such phrase as missing.
    """
    return _WSRUN.sub(" ", s or "")


def punct_losses(page_texts: dict[int, str], md: str, limit: int = 40) -> list[dict]:
    """WHERE each typographic character was rewritten — the phrase, and its page.

    A score of 0.0 says a document was re-punctuated; it does not say where, and without
    that the finding is unactionable. This walks every smart character in the text layer,
    takes the phrase around it, and asks whether the transcript holds that same phrase with
    the character FLATTENED. A hit is proof of a rewrite at a known place, not an inference
    from two counts that happen to differ.

    Deliberately confirms the flattened form rather than just noting the absence of the
    curly one: a phrase the model dropped entirely is a COMPLETENESS defect and belongs to
    that dimension. Only a phrase that is present with the character changed is a
    punctuation defect, and telling those apart is the whole point of looking.
    """
    flat_md = _flat(md)
    out: list[dict] = []
    seen: set[str] = set()
    for page in sorted(page_texts):
        text = page_texts[page]
        for i, ch in enumerate(text):
            plain = _SMART_TO_PLAIN.get(ch)
            if plain is None:
                continue
            # A window wide enough to be unambiguous, trimmed to whole words so the
            # reported phrase reads as language rather than as a slice.
            lo, hi = max(0, i - 26), min(len(text), i + 27)
            frag = _flat(text[lo:hi]).strip()
            if lo > 0 and " " in frag:
                frag = frag.split(" ", 1)[1]
            if hi < len(text) and " " in frag:
                frag = frag.rsplit(" ", 1)[0]
            if len(frag) < 8:
                continue
            rewritten = "".join(_SMART_TO_PLAIN.get(c, c) for c in frag)
            if rewritten == frag or rewritten not in flat_md:
                continue
            key = rewritten
            if key in seen:
                continue
            seen.add(key)
            out.append({"page": page, "char": ch, "became": plain,
                        "printed": frag, "written": rewritten})
            if len(out) >= limit:
                return out
    return out


def punctuation(source: str, md: str, page_texts: dict[int, str] | None = None) -> dict:
    """How many of the PDF's own typographic characters survived the transcription.

    THE COVERAGE CHECK CANNOT SEE THIS. `words` strips every non-alphanumeric character, so
    a transcript that replaced all 18 of Germany's curly quotes with straight ones scored
    100.0 on fidelity -- which is how the first run came back green while silently
    re-punctuating a legal document.

    ai_postprocess.strict_stream was written for exactly this, and its docstring records the
    pilot that motivated it: the model turned (\u201cIFA\u201d) into ("IFA"), which an
    alphanumeric-only gate waves straight through. Stage 4 can afford to REJECT such a reply
    because it still holds the original characters; a transcription has no such fallback, so
    the same finding has to be measured and shown instead.

    Scored on the QUOTES alone, with the dashes reported but not scored. The running footer
    carries an en dash on every page ("Marketing Restrictions \u2013 Asset Management") and the
    prompt is told to omit it, so a nine-page document is legitimately nine en dashes short
    -- scoring that would penalise following the instructions. Quotes appear only in body
    text, so they carry no such exemption.
    """
    def tally(text: str, chars) -> dict[str, int]:
        return {c: (text or "").count(c) for c in chars}

    src_q, out_q = tally(source, _QUOTES), tally(md, _QUOTES)
    src_d, out_d = tally(source, _DASHES), tally(md, _DASHES)
    want = sum(src_q.values())
    kept = sum(min(out_q[c], src_q[c]) for c in _QUOTES)
    return {
        "score": round(100.0 * kept / want, 1) if want else 100.0,
        "quotes_in_pdf": want, "quotes_in_output": sum(out_q.values()), "quotes_kept": kept,
        "per_char": {"pdf": {**src_q, **src_d}, "out": {**out_q, **out_d}},
        "straight_in_output": {'"': (md or "").count('"'), "'": (md or "").count("'")},
        "losses": punct_losses(page_texts or {}, md) if page_texts else [],
    }


_DIGITS = re.compile(r"\d+")


def furniture_lines(page_texts: dict[int, str], share: float = 0.6) -> set[str]:
    """The LINE shapes that repeat across pages — the running header and footer.

    Detected at line level, not word level, and with digits masked. A word-level pass gets
    two things wrong that between them flag most of the document:

      * THE PAGE NUMBER IS UNIQUE PER PAGE, so it never looks repeated and is never
        recognised as furniture -- every page then reports losing one word, its own number.
      * A FOOTER WORD THAT ALSO APPEARS IN PROSE ("management", in "Marketing Restrictions
        - Asset Management" and in "investment management") is present in the transcript, so
        it survives the "missing everywhere" test, and the page is left short by one
        OCCURRENCE that a word-set cannot subtract.

    Masking digits fixes the first -- "4130-0297-2520, v. 1 | 4 | Marketing Restrictions -
    Asset Management" becomes one shape shared by every page -- and matching whole lines
    fixes the second, because the line is removed from the page before anything is counted.

    Derived rather than hardcoded, so it holds for a document whose footer reads differently.
    A hardcoded string would fail silently the moment it stopped matching, which is the
    failure mode this whole approach is chosen to avoid.
    """
    n = len(page_texts)
    if n < 3:
        return set()                      # too few pages for "repeats across pages" to mean much
    seen: collections.Counter = collections.Counter()
    for text in page_texts.values():
        shapes = {_DIGITS.sub("#", " ".join(ln.split())) for ln in (text or "").splitlines()}
        seen.update(s for s in shapes if s)
    return {s for s, c in seen.items() if c >= max(2, int(n * share))}


def strip_furniture(text: str, shapes: set[str]) -> str:
    """A page's text with its running header/footer lines removed."""
    keep = [ln for ln in (text or "").splitlines()
            if _DIGITS.sub("#", " ".join(ln.split())) not in shapes]
    return "\n".join(keep)


# The cover's Disclaimer block is one page, once, so it never repeats across pages the way
# the footer does -- furniture_lines' repetition test cannot see it. The prompt is told to
# drop it (build_instr / FURNITURE_OMIT / FURNITURE_KEEP), so the source side of every
# word-coverage check has to drop it too, or the completeness score reports this paragraph
# as lost prose on every single run. Matched from the "Disclaimer:" heading to the next
# blank line rather than against a fixed string, so it still holds if aosphere ever
# rewords the notice itself.
#
# THREE GUARDS, the first two earned by a real miss on SUMMARY__Denmark: without the word
# boundary and the colon, "Disclaimer" matched inside "...including disclaimers" -- an
# unrelated heading deep in the body, "Form and content requirements including
# disclaimers" -- and with no blank line before the page ran out, the old
# `.*?(?:\n\s*\n|\Z)` fell through to \Z and swallowed the REST OF THE PAGE as "disclaimer
# text", so every real word after it read as invented. The colon requirement stops the
# match from ever starting on the body heading (it has no colon there), and the bounded
# `.{0,600}?` caps the damage if some other page ever DOES end mid-paragraph with no blank
# line to stop at. The third guard is the caller: the notice is always on the cover, so
# strip_disclaimer only ever runs the regex over page 1 -- every later page is returned
# untouched, which is what actually makes the other two guards belt-and-braces rather
# than the only thing standing between a stray "disclaimer" and a wrecked page.
# Shared with the scorecard's conservation checks, which have to excuse exactly the same
# paragraph or they score this pipeline's compliance as content loss. One regex, imported
# rather than restated, so the two can never drift apart.
from lib_content_compare import COVER_DISCLAIMER as _DISCLAIMER  # noqa: E402


def strip_disclaimer(text: str, page: int) -> str:
    """`text` with the cover's boilerplate Disclaimer paragraph removed, page 1 only."""
    return _DISCLAIMER.sub("", text or "") if page == 1 else (text or "")


def word_ledger(page_texts: dict[int, str], md: str) -> dict:
    """Which words went missing, WHICH PAGE they were on, and which arrived unsourced.

    The stat "words not transcribed: 2" is unactionable on its own -- it was the first
    thing asked of this scorecard by someone reading it. The words themselves are cheap to
    carry and turn the number into something checkable against the page.

    Attribution is per page, because the finding has to land somewhere in the page heat-map
    and Page Review; a document-level count cannot be clicked on.
    """
    shapes = furniture_lines(page_texts)
    out = collections.Counter(words(md))
    src_all: collections.Counter = collections.Counter()
    per_page: list[dict] = []
    for page in sorted(page_texts):
        pw = collections.Counter(words(strip_disclaimer(strip_furniture(page_texts[page], shapes), page)))
        src_all.update(pw)
        miss = pw - out
        if miss:
            per_page.append({"page": page, "words": sorted(miss.elements()),
                             "n": sum(miss.values())})
    added = out - src_all
    return {
        "missing_by_page": per_page,
        "missing_total": sum(p["n"] for p in per_page),
        "added": sorted(added.elements()),
        "added_total": sum(added.values()),
    }


# Words that are PRINTED on the page but absent from its text layer, so they arrive with no
# source and look invented. They are not: the cover's gauge labels and, on some
# jurisdictions, the status line beside a coloured square are drawn as GRAPHICS -- see the
# check in this module's docstring. The vision model reads them off the image, which is the
# entire reason this route exists, so a check that penalised it for that would be
# penalising it for working. Named rather than pattern-matched: this is a short, closed list
# of label text this template draws, and anything outside it stays a real finding.
GRAPHIC_LABELS = frozenset({
    "simple", "complex", "open", "restrictive", "medium", "quite", "very",
    "workable", "depending", "structured", "activity", "investor", "type", "how",
    "permitted", "not", "and", "or", "on", "is", "of", "the", "a",
})

# The text layer letter-spaces the cover's running head ("CROSS-BORDER DISTRIBU TION"), so
# it holds two tokens where the page shows one word and the model correctly writes one.
# A known, document-template artefact of the SOURCE, not a defect in the transcript.
SPLIT_ARTEFACTS = frozenset({"distribu", "tion", "distribution"})


def page_health(page_texts: dict[int, str], md: str) -> dict:
    """Per-page word coverage, in the shape the scorecard's page heat-map reads.

    The heat-map is the one panel that answers "WHERE did content go missing" spatially,
    and it rendered "0 pages" because this route supplied no `pages` block at all. Filled in
    from a measure this route genuinely has: every page was sent to the model, and every
    page's text layer can be compared against the transcript.

    The running furniture is excluded first -- see furniture_words -- so a page is flagged
    for losing PROSE, never for the footer the prompt was told to drop.

    'flagged' rather than 'silent' for a page that lost words. The pipeline's distinction is
    whether the loss was DETECTED, and here it is reported by construction: on this panel,
    and word for word in the completeness card. Nothing on this route can lose text
    silently, so the silent state is unreachable and says so.
    """
    out_words = collections.Counter(words(md))
    shapes = furniture_lines(page_texts)
    states: dict[str, str] = {}
    counts: collections.Counter = collections.Counter()
    detail: list[dict] = []
    for page in sorted(page_texts):
        pw = words(strip_disclaimer(strip_furniture(page_texts[page], shapes), page))
        if not words(page_texts[page]):
            # No text layer at all: a page of pure image. The model may well have read it,
            # but nothing here can check that, which is exactly "visual-only".
            st = "unvalidatable"
        elif not pw:
            st = "ok"                     # the page holds nothing BUT furniture
        else:
            missing = collections.Counter(pw) - out_words
            n = sum(missing.values())
            st = "ok" if n == 0 else "flagged"
            if n:
                detail.append({"page": page, "missing_words": n,
                               "sample": " ".join(list(missing.elements())[:12])})
        states[str(page)] = st
        counts[st] += 1
    return {
        "total": len(page_texts), "states": states, "counts": dict(counts),
        "furniture_lines_excluded": sorted(shapes),
        "state_help": {
            "ok": "every word of this page's text layer is in the transcript, once the "
                  "repeated running furniture the prompt is told to omit is set aside",
            "flagged": "this page has body words that are not in the transcript — named in "
                       "the completeness card",
            "silent": "unreachable on this route: every loss here is reported by "
                      "construction, on this panel and in the completeness card",
            "unvalidatable": "this page has no text layer to compare against, so its "
                             "transcript cannot be checked mechanically",
        },
        "pages_flagged": detail,
    }


# ---------------------------------------------------------------- the scorecard

# Written so this experiment shows up in the EXISTING corpus view with no change to any
# existing code. hybrid_extract_ui._corpus_jobs walks CORPUS_ROOT/<product>/<job>/ and
# reads corpus_meta.json + scorecard.json per job -- nothing more -- so a run that writes
# that layout is indistinguishable from a pipeline run as far as the UI is concerned:
#
#     python scripts/hybrid_extract_ui.py --corpus-root out/summary_ai --port 8811
#
# Deliberately a SEPARATE ROOT from out/corpus*. This is a side experiment on one product,
# and dropping its scorecards in among the real corpus runs would mix a transcription score
# into corpus-wide numbers that mean something different.

# The chunk tree's directory name is NOT cosmetic: hybrid_extract_ui.STAGE_DIRS maps a
# stage number to a directory name, and its /data.json endpoint -- the Document pane -- only
# builds a tree from one of those four. A tree written to "sections/" is invisible there,
# and the job then shows a scorecard with no content behind it, which is how the first
# Germany run looked in the UI. Stage 4 is the AI slot, which is what this pass is.
SPLIT_DIR = "04_stage4_ai"

GATE_PASS, GATE_REVIEW = 90.0, 70.0


def _gate(worst: float) -> str:
    return "pass" if worst >= GATE_PASS else ("review" if worst >= GATE_REVIEW else "fail")


def scorecard(cov: dict, hs: list[str], checks: dict, markers: int,
              timing: dict, doc_key: str, punct: dict,
              pages: dict | None = None, ledger: dict | None = None,
              squares_pdf: list[str] | None = None,
              squares_model: list[str] | None = None) -> dict:
    """The three things a transcription can get wrong, scored, in the corpus-view shape.

    Not the pipeline's dimensions, because they measure a different job: `placement` and
    `sectioning` are about where MinerU's tables landed in a stage-1 tree, and there is no
    such tree here. What a transcription can get wrong is narrower and all three are
    independently measurable, which is the only reason they are worth scoring:

    COMPLETENESS -- did every word arrive. Bag-of-words against the text layer, so a block
    the model moved to its correct position (the cover's Table of Contents, read in column
    order rather than the text layer's broken one) still counts as present. That is the
    whole argument of coverage(): presence, not order.

    STRUCTURE -- did every heading arrive. Scored against find_bars, which counts the bars
    in the PDF geometry without the model's help, so this is a genuine independent check
    rather than the model grading itself. A bar with no matching heading means a section
    silently folded into the one above it -- the prose is all still there, which is exactly
    why nothing else would catch it.

    FIDELITY -- did anything arrive that was never printed. The inverse of completeness,
    and the one that matters most in a legal document: a model that invents a clause is
    worse than one that drops a footer. Scored on words in the transcript that the text
    layer does not have.
    """
    import hashlib

    ledger = ledger or {"missing_by_page": [], "added": [],
                        "missing_total": 0, "added_total": 0}
    pdf_w, out_w, matched = cov["pdf_words"], cov["out_words"], cov["matched"]
    completeness = round(100.0 * matched / pdf_w, 1) if pdf_w else 0.0
    fidelity = round(100.0 * matched / out_w, 1) if out_w else 0.0
    bars, missed_bars = checks["bars_found"], len(checks["bars_not_emitted"])
    structure = round(100.0 * (bars - missed_bars) / bars, 1) if bars else 100.0

    # THE SQUARES, SCORED THE SAME WAY THE BARS ARE: find_squares reads the true colour
    # off the PDF's own embedded images, independent of anything the model wrote, so this
    # is a genuine check rather than the model grading itself -- same argument as
    # STRUCTURE, applied to the other picture-only signal this route recovers by eye.
    #
    # COMPARED PER PAGE, not as one whole-document total: a red tagged as amber on page 2
    # and an amber tagged as red on page 3 cancel out in a document-wide count -- 5 red +
    # 1 amber either way -- and the mismatch vanishes. Per page, page 2 comes up one
    # amber short and page 3 one amber over, and both are caught. This is exactly the
    # failure a whole-document count missed on a real run (SUMMARY__Malaysia), which is
    # why this is per page and not the simpler thing tried first.
    #
    # GATING, not advisory: a misread verdict square is not a typography nit like a
    # straight quote -- it is the ANSWER itself, "Permitted" vs "Not permitted", read
    # wrong. A document can have perfect words and structure and still tell the reader
    # the opposite of what the page says, which is worse than losing a paragraph.
    squares_total = len(squares_pdf) if squares_pdf is not None else 0
    squares_correct = 0
    # ONE ENTRY PER WRONG SQUARE -- {"page", "want", "got"} -- not one per page. A page
    # can hold more than one mismatch, and "extracted red, should be amber" is the whole
    # answer Page Review needs; an aggregate count ("red: 4 vs 3 tagged") makes the
    # reader do the matching themselves.
    squares_mismatches: list[dict] = []
    if squares_pdf is not None and squares_model is not None:
        want_by_page: dict[int, list[str]] = collections.defaultdict(list)
        got_by_page: dict[int, list[str]] = collections.defaultdict(list)
        for p, c in squares_pdf:
            want_by_page[p].append(c)
        for p, c in squares_model:
            got_by_page[p].append(c)
        for p in sorted(set(want_by_page) | set(got_by_page)):
            want, got = want_by_page.get(p, []), got_by_page.get(p, [])
            want_n, got_n = collections.Counter(want), collections.Counter(got)
            for name in ("red", "amber", "green"):
                squares_correct += min(want_n.get(name, 0), got_n.get(name, 0))
            # POSITION BY POSITION, not a bag comparison, so the finding can say WHICH
            # square is wrong -- "extracted red, should be amber" -- rather than only
            # that the page's colour counts disagree. Only trustworthy when the two
            # sides agree on HOW MANY squares are on the page; when they don't (a square
            # the model never tagged at all, or an extra tag with nothing beside it),
            # position stops meaning anything and the aggregate counts are all that is
            # honest to report.
            if len(want) == len(got):
                for i, (w, g) in enumerate(zip(want, got), 1):
                    if w != g:
                        squares_mismatches.append({"page": p, "index": i, "want": w, "got": g,
                                                   "detail": f"extracted {g}, should be {w}"})
            elif want_n != got_n:
                bits = [f"{name}: {want_n.get(name, 0)} on the page vs {got_n.get(name, 0)} tagged"
                        for name in ("red", "amber", "green")
                        if want_n.get(name, 0) != got_n.get(name, 0)]
                squares_mismatches.append({"page": p, "index": None, "want": None, "got": None,
                                           "detail": (f"{len(want)} square(s) on the page vs "
                                                      f"{len(got)} tagged — " + ", ".join(bits))})
    squares = (round(100.0 * squares_correct / squares_total, 1)
               if squares_total else 100.0)

    dims = {
        "completeness": {
            "score": completeness, "critical": True,
            "label": "Completeness",
            "what": "Whether every word the PDF prints reached the transcript.",
            "advice": "Read the longest runs below. A run that is the repeated page "
                      "footer or the cover's Disclaimer notice is the prompt doing as it "
                      "was told (--furniture omit). A run of body prose is a real loss: "
                      "re-run, and if it recurs the page it came from is worth looking at.",
            "from": "the PDF text layer (PyMuPDF page.get_text) against the transcript, as "
                    "a multiset of lowercased words, with the repeated running header and "
                    "footer and the cover's Disclaimer paragraph removed first — the prompt "
                    "is told to omit those, so counting them would score compliance as loss",
            "formula": "words present in both / words in the text layer",
            "caveat": "Presence, not order. The cover page's own text-layer order is wrong "
                      "-- it interleaves two columns -- so the transcript is EXPECTED to "
                      "disagree with it there, and an order-sensitive score would condemn "
                      "the one place the model is right and the text layer is not.",
            "detail": {
            "pdf_words": pdf_w, "out_words": out_w, "matched_words": matched,
            "missing_words": pdf_w - matched,
            "stats": [
                {"label": "word coverage", "value": f"{completeness}%",
                 "bad": completeness < GATE_PASS},
                {"label": "words in the text layer", "value": f"{pdf_w:,}"},
                {"label": "words transcribed", "value": f"{out_w:,}"},
                {"label": "words not transcribed", "value": f"{pdf_w - matched:,}",
                 "warn": pdf_w - matched > 0},
            # THE WORDS THEMSELVES, per page. "words not transcribed: 2" was the first
            # thing a reader asked about, because a count cannot be checked against the
            # page and a word can.
            ] + [{"label": f"not transcribed, p{pg['page']}",
                  "value": ", ".join(pg["words"][:14]),
                  "warn": any(w not in SPLIT_ARTEFACTS for w in pg["words"])}
                 for pg in ledger["missing_by_page"]
            ] + [{"label": "longest run not transcribed", "value": r}
                 for r in cov["missing_runs"][:5]],
        }},
        "structure": {
            "score": structure, "critical": True,
            "label": "Structure",
            "what": "Whether every heading the document DRAWS became a heading in the "
                    "transcript.",
            "advice": "A bar with no heading means that section was silently folded into "
                      "the one above it -- the prose is all there, just unlabelled. Check "
                      "the named bar on its page and re-run.",
            "from": "the PDF's own drawing geometry: find_bars matches the four measured "
                    "bar fills at their measured heights, independently of the model",
            "formula": "(heading bars found in the PDF - bars with no heading) / bars found",
            "caveat": "Counts bars, not correctness of nesting. A #### that should have "
                      "been a ### still counts as present -- see the known level-inversion "
                      "note in product_rules.AI_PIPELINE.",
            "detail": {
            "bars_found": bars, "bars_not_emitted": checks["bars_not_emitted"],
            "h2": sum(h.startswith("## ") for h in hs),
            "h3": sum(h.startswith("### ") for h in hs),
            "h4": sum(h.startswith("#### ") for h in hs),
            "headings": hs,
            "stats": [
                {"label": "heading bars found in the PDF", "value": str(bars)},
                {"label": "bars with no heading in the transcript",
                 "value": str(missed_bars), "bad": missed_bars > 0},
                {"label": "heading levels written",
                 "value": (f"{sum(h.startswith('## ') for h in hs)} sections / "
                           f"{sum(h.startswith('### ') for h in hs)} sub / "
                           f"{sum(h.startswith('#### ') for h in hs)} labelled")},
                {"label": "status squares captured", "value": str(markers),
                 "warn": markers == 0},
            ] + [{"label": "bar with no heading", "value": t, "bad": True}
                 for t in checks["bars_not_emitted"][:5]]
            + [{"label": "heading not located on a page", "value": h, "warn": True}
               for h in checks["unlocated_headings"][:5]],
        }},
        "fidelity": {
            "score": fidelity, "critical": True,
            "label": "Fidelity",
            "what": "Whether anything reached the transcript that the PDF never printed.",
            "advice": "The runs below are usually REORDERED text, not invented text -- a "
                      "block the model moved appears in both this list and completeness's. "
                      "A run that appears here ONLY is text with no source, and is the most "
                      "serious defect this route can have.",
            "from": "the same two word multisets as completeness, inverted",
            "formula": "words present in both / words in the transcript",
            "detail": {
            "added_words": out_w - matched,
            "stats": [
                {"label": "words not in the text layer", "value": f"{out_w - matched:,}",
                 "bad": fidelity < GATE_PASS},
            ] + ([{"label": "in the transcript, not in the text layer",
                   "value": ", ".join(ledger["added"][:14]), "warn": True}]
                 if ledger["added"] else [])
              + [{"label": "longest run not in the text layer", "value": r}
                 for r in cov["added_runs"][:5]],
        }},
    }
    # Added only when the caller actually supplied ground truth -- summary_ai_selftest's
    # scorecard() call predates this check and passes neither squares_pdf nor
    # squares_model, so it stays exactly as scored before rather than gating on data it
    # never had.
    if squares_pdf is not None and squares_model is not None:
        dims["markers"] = {
            "score": squares, "critical": True,
            "label": "Colour squares",
            "what": "Whether every [green]/[amber]/[red] verdict tag the model wrote "
                    "matches the true colour of the square printed beside it.",
            "advice": "Each mismatch below names its page. Compare that page's image "
                      "against its tags -- the usual failure is red misread as amber or "
                      "the reverse; a pale, washed-out square is amber even where it "
                      "looks textured the same way a red one does.",
            "from": "the PDF's own embedded square images (find_squares), classified by "
                    "their real pixel colour against three fixed references, independent "
                    "of anything the model wrote -- the same argument STRUCTURE makes "
                    "for heading bars, applied to this route's other picture-only signal",
            "formula": "squares whose colour the model got right, per page / squares "
                       "found in the PDF",
            "caveat": "Compared per page, not as one whole-document total: two swapped "
                      "tags elsewhere in the document would cancel out in a single count "
                      "and this catches neither -- per page it catches both.",
            "detail": {
            "squares_found": squares_total, "squares_correct": squares_correct,
            "stats": [
                {"label": "verdict squares in the PDF", "value": str(squares_total)},
                {"label": "correctly tagged", "value": str(squares_correct),
                 "bad": squares_correct < squares_total},
            ] + [{"label": f"page {m['page']}" + (f", square {m['index']}" if m['index'] else ""),
                  "value": m["detail"], "bad": True}
                 for m in squares_mismatches],
        }}
    # ADVISORY, and the reason is worth stating because the first version of this gated and
    # was wrong. Scoring 0.0 and failing the document conflates two very different defects:
    #
    #   every quotation mark missing        -- text is gone, the document is unshippable
    #   every quotation mark FLATTENED      -- U+201C became U+0022, nothing is gone
    #
    # This route only ever produces the second. The marks are all present, in the right
    # places, around the right words; it is the GLYPH that changed. A gate that cannot tell
    # that from a dropped section stops discriminating, and across 88 documents every row
    # would read `fail` -- which is the same as no gate at all.
    #
    # Still a CRITICAL FINDING, so it is loud rather than lost: the finding names every
    # rewrite with its page and phrase. ai_postprocess.strict_stream's standard -- a
    # rewritten quotation mark in a legal document is a content change -- is upheld by
    # reporting it, not by refusing to ship a document whose words are all correct. That
    # standard was written for a REPAIR pass, which can discard a bad reply for free because
    # it still holds the original characters; a transcription has no such fallback, and the
    # remedy here is a prompt change rather than a verdict.
    dims["punctuation"] = {
        "score": punct["score"], "critical": False,
        "label": "Typography (advisory)",
        "what": "Whether the exact quote GLYPHS the page prints survived. A low score does "
                "not mean quotation marks are missing -- it means curly ones (\u201c \u201d \u2019) "
                "were written as straight ones (\" \'). The marks are all there.",
        "advice": "Every rewrite is listed below with its page and its phrase. The fix is "
                  "a prompt change, not a re-run: the model is normalising deliberately, "
                  "so the same prompt will do it again. Does not affect the verdict.",
        "from": "a character count over the PDF text layer against the transcript, plus a "
                "phrase-level confirmation of each rewrite (punct_losses)",
        "formula": "curly quotes and apostrophes kept / those the PDF prints",
        "caveat": "Scored on quotes alone. En and em dashes are reported but not scored: "
                  "the running footer carries one per page and the prompt is told to omit "
                  "it, so scoring them would penalise following instructions.",
        "detail": {
        **{k: v for k, v in punct.items() if k != "score"},
        "losses": punct["losses"],
        "stats": [
            {"label": "curly quotes kept",
             "value": f"{punct['quotes_kept']} of {punct['quotes_in_pdf']}",
             "bad": punct["score"] < GATE_PASS},
            {"label": "straight quotes written instead",
             "value": str(punct["straight_in_output"]['"'] + punct["straight_in_output"]["'"]),
             "warn": punct["score"] < 100},
            {"label": "en/em dashes",
             "value": f"{sum(punct['per_char']['out'].get(c, 0) for c in _DASHES)} written / "
                      f"{sum(punct['per_char']['pdf'].get(c, 0) for c in _DASHES)} in the PDF "
                      f"(the running footer carries one per page, and is omitted)"},
        # EVERY rewrite, on its own row with its page and its phrase. A score of 0.0 says a
        # document was re-punctuated; only these rows say where, and a finding nobody can
        # locate is a finding nobody can act on.
        ] + [{"label": f"p{w['page']}  {w['char']} \u2192 {w['became']}",
               "value": w["printed"], "bad": True}
             for w in punct["losses"]],
    }}
    # Over the GATING dimensions only. min() across every dimension is what let an advisory
    # one set the verdict -- the bug this comment block exists to prevent coming back.
    gating = [k for k, v in dims.items() if v.get("critical")]
    worst_dim = min(gating, key=lambda k: dims[k]["score"])
    worst = dims[worst_dim]["score"]
    advisory_low = [k for k, v in dims.items()
                    if not v.get("critical") and v["score"] < GATE_PASS]
    # ABSOLUTE COUNTS AS FINDINGS, because the SCORES cannot see a small defect: one wrong
    # word in 4,764 is 0.021%, which rounds away at one decimal place. Found by the mutation
    # self-test (scripts/summary_ai_selftest.py), which is why this exists at all.
    #
    # Shaped exactly like the pipeline's own findings -- key / kind / title / detail / pages
    # / dismissed -- so hybrid_extract_ui.renderFinding renders them, the page heat-map
    # attributes them, and Page Review opens the page they name. A finding with no `pages`
    # cannot be clicked on, which makes it a number again.
    #
    # AND THE WORDS ARE NAMED, for the same reason.
    def _find(kind, dim, sev, title, detail, pages):
        return {"key": f"sai|{kind}|{hashlib.sha1(title.encode()).hexdigest()[:10]}",
                "kind": kind, "dimension": dim, "severity": sev, "file": None,
                "pages": pages, "title": title, "detail": detail, "dismissed": False}

    count_findings = []
    for pg in ledger["missing_by_page"]:
        real = [w for w in pg["words"] if w not in SPLIT_ARTEFACTS]
        shown = ", ".join(pg["words"][:12]) + ("…" if len(pg["words"]) > 12 else "")
        if real:
            count_findings.append(_find(
                "gap", "completeness", "advisory" if pg["n"] < 5 else "critical",
                f"{pg['n']} word(s) on page {pg['page']} not in the transcript: {shown}",
                "Compared against that page's text layer, with the repeated running header "
                "and footer already excluded. Check these words against the page image.",
                [pg["page"]]))
        else:
            # Reported, not hidden: a reader should see that the check fired and why it was
            # discounted, or the same two words get investigated again next month.
            count_findings.append(_find(
                "gap", "completeness", "advisory",
                f"page {pg['page']}: {shown} — the text layer's own letter-spacing",
                "NOT a defect in the transcript. This template letter-spaces the cover's "
                "running head, so the text layer holds \"DISTRIBU TION\" as two tokens "
                "where the page shows one word, and the model correctly writes one. The "
                "text layer is wrong here, not the transcript.",
                [pg["page"]]))

    unsourced = [w for w in ledger["added"]
                 if w not in GRAPHIC_LABELS and w not in SPLIT_ARTEFACTS]
    graphic = sorted({w for w in ledger["added"] if w in GRAPHIC_LABELS})
    if unsourced:
        count_findings.append(_find(
            "meaning", "fidelity", "critical",
            f"{len(unsourced)} word(s) in the transcript with no source on the page: "
            + ", ".join(unsourced[:12]) + ("…" if len(unsourced) > 12 else ""),
            "Words with no source are the most serious thing a transcription can produce. "
            "Check each against the page image before accepting the document.",
            []))
    if graphic:
        count_findings.append(_find(
            "engine", "fidelity", "advisory",
            f"{len(graphic)} word(s) read off the page GRAPHICS, absent from the text "
            f"layer: " + ", ".join(graphic[:12]),
            "RECOVERED, NOT INVENTED. The cover's gauge labels -- and on some "
            "jurisdictions the status line beside a coloured square -- are drawn rather "
            "than typeset, so no text extractor can see them at all. The model read them "
            "off the page image, which is the reason this route exists. They lower "
            "`fidelity` only because that dimension measures against the text layer, and "
            "the text layer does not contain them.",
            [1]))

    # Every finding through _find, so all of them carry the key/kind/title/pages the UI
    # renders and the heat-map attributes. A finding built by hand in a different shape is
    # a finding that renders blank -- which is how the first three came out.
    if punct["score"] < 100:
        loss_pages = sorted({w["page"] for w in punct.get("losses") or []})
        count_findings.append(_find(
            "meaning", "punctuation", "advisory",
            f"{punct['quotes_in_pdf'] - punct['quotes_kept']} of {punct['quotes_in_pdf']} "
            f"curly quotes/apostrophes written as STRAIGHT ones",
            "U+201C/U+201D/U+2019 became U+0022/U+0027. The marks are all present and so "
            "are the words, which is why word coverage reads 100 and this does not set the "
            "verdict. Every rewrite is listed with its page and phrase on the Typography "
            "card. Fix: strengthen the punctuation rule in the prompt. Advisory, not "
            "critical, to match the dimension itself: a rewritten glyph is not a missing "
            "quote, and this alone never sets the gate.",
            loss_pages[:8]))
    for t in checks["bars_not_emitted"]:
        count_findings.append(_find(
            "hierarchy", "structure", "critical",
            f"a heading bar with no heading in the transcript: {t!r}",
            "The PDF draws a bar here and the transcript has no heading for it, so that "
            "section's prose is folded into the section above. The words are all still "
            "present, which is why nothing but the bar geometry can find this.",
            []))
    for h in checks["unlocated_headings"]:
        count_findings.append(_find(
            "hierarchy", "structure", "advisory",
            f"{h!r} could not be located on any page",
            "Its page range is inherited from the heading before it rather than measured, "
            "so this section's *Source:* breadcrumb may point at the wrong page.",
            []))
    # THE SQUARES: one finding per WRONG square (or, when the count itself is off, one
    # per page), reusing the same comparison already scored into dims["markers"] above
    # -- see there for why gating. "extracted X, should be Y" is the whole answer this
    # finding exists to give; Page Review shows this text verbatim against the page it
    # names, which is the point of naming a square index at all.
    # THE RENDERED TREE DISAGREEING WITH THE PDF is its own failure, separate from the
    # transcript's: everything else here scores transcript.md, but 04_stage4_ai/ is what a
    # reader is actually shown. Reported as critical for the same reason a misread square
    # is -- it is the answer itself, wrong, in the copy people read.
    for m in (checks.get("square_tree_mismatches") or []):
        count_findings.append(_find(
            "meaning", "markers", "critical",
            f"page {m['page']}: the published tree says [{m['in_tree']}], the PDF's square "
            f"is {m['in_pdf']}",
            "The chunk tree under 04_stage4_ai/ is what the Doc Gallery renders, and it "
            "disagrees with the PDF here even though the transcript may not. It means the "
            "tree was split from uncorrected text -- re-run the split.",
            [m["page"]]))
    for m in squares_mismatches:
        where = f"page {m['page']}" + (f", square {m['index']}" if m['index'] else "")
        count_findings.append(_find(
            "meaning", "markers", "critical",
            f"{where}: {m['detail']}",
            "The colour is read from the PDF's own embedded square image at this "
            "position (deterministic -- not a model guess) against the [green]/[amber]/"
            "[red] tag the model wrote for the paragraph located here. Check the page "
            "image at the named position against what was tagged.",
            [m["page"]]))
    findings = count_findings
    return {
        "gate": _gate(worst), "worst_score": worst, "weakest_dimension": worst_dim,
        "gate_thresholds": {"pass": GATE_PASS, "review": GATE_REVIEW},
        "critical_dimensions": gating,
        "gating_dimensions": gating,
        "dimensions": dims, "timing": timing,
        # The heat-map panel reads this and rendered "0 pages" without it. See page_health.
        "pages": pages or {"total": 0, "states": {}, "counts": {}, "state_help": {}},
        # Empty on purpose, and READ as empty: the tables panel and the structure panel each
        # skip themselves when their key is absent or empty, which is the correct outcome --
        # this route parses no tables and builds no stage-1 chunk tree to describe.
        "tables": [],
        "findings": findings, "active_finding_count": len(findings), "dismissed_count": 0,
        "advisory_low": advisory_low, "doc_key": doc_key,
        # Says WHAT was scored, in the same slot the pipeline uses to explain a document
        # judged under different rules -- so nobody reads these numbers as a stage-1-3 gate.
        "special_mode": {
            "mode": "ai_transcription",
            "why": "This is not a pipeline extraction. One vision-model call transcribed "
                   "every page of the PDF into Markdown, reading the document's own "
                   "coloured heading bars for structure. It is scored on the three things "
                   "a transcription can get wrong -- every word present (completeness), "
                   "every heading bar present (structure, checked against the PDF geometry "
                   "independently of the model), and nothing present that was never "
                   "printed (fidelity). Stages 1-3 were not run and none of their "
                   "dimensions apply.",
        },
    }


def doc_key(pdf: Path) -> str:
    """The scorecard's document identity — the PDF's sha1, truncated as the pipeline does."""
    import hashlib

    return hashlib.sha1(pdf.read_bytes()).hexdigest()[:16]


def write_job_meta(out: Path, pdf: Path, product: str, jurisdiction: str, doc_id: str) -> None:
    """corpus_meta.json, the file the corpus view uses to decide a directory IS a job.

    `pdf_sha1` is the FULL digest, not doc_key's 16-char prefix. run_corpus builds its
    clone index off this field and looks documents up by it, so a truncated value here
    would silently never match -- every summary would look like a document never seen
    before. doc_key's prefix is for the scorecard's own identity and is not interchangeable
    with it, which is exactly the mistake this comment exists to stop being made again.
    """
    import hashlib

    (out / "corpus_meta.json").write_text(json.dumps({
        "product": product, "jurisdiction": jurisdiction, "doc_id": doc_id,
        "source_pdf": str(pdf), "pdf_sha1": hashlib.sha1(pdf.read_bytes()).hexdigest(),
        "ai_processed": True, "ai_mode": "summary_transcription",
    }, indent=2) + "\n")


# ---------------------------------------------------------------- chunking by title

# The filename shape, the breadcrumb wording and the 00-overview.md / README.md pair are
# all COPIED from pdf2mdtree.emit_item rather than reinvented, because nothing downstream
# treats them as cosmetic: chunk_eval.chunk_hybrid_tree reads 00-overview.md and falls back
# to README.md, check_content_localized.py parses the "*Source:*" line out of every content
# file, and the review viewer navigates by it. A tree written to its own conventions is a
# tree the rest of the repo cannot read.


def slug(s: str, maxlen: int = 48) -> str:
    """Title -> filename stem, as pdf2mdtree.slug does it (leading marker stripped)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"^[A-Z]\.\s*|^\d+(\.\d+)*\.?\s*|^\(\w+\)\s*", "", s)
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0]
    return s or "section"


def item_prefix(title: str, seq: int) -> str:
    """The file's ordinal — the DOCUMENT's own number or letter, LED by its running
    position among its siblings.

    "A. UCITS" as the 2nd child -> "02-A", "1. Carrying on Business Restriction" as the
    1st -> "01-01", an unnumbered heading -> just its running position.

    THE RUNNING POSITION IS NOT OPTIONAL, even though the document's own label is kept
    front and centre for a reader -- because a running number/letter is not unique
    across a whole document, only within whatever it happens to be numbering. A section
    can letter its own sub-items A, B, C under "1. Fund level requirements" and then
    letter ANOTHER A, B, C under "2. Firm Level Restrictions" a few headings later: two
    "A"s, two "B"s, two "C"s, all genuine SIBLINGS in the parsed tree because the source
    PDF draws "1./2." and "A./B./C." as the same bar level rather than nesting one under
    the other. Filenames built from the bare label alone came out
    "01-fund-level-requirements.md", "A-permitted....md", "A-regulated....md",
    "B-guidelines....md", "B-carrying....md" -- and ANY plain alphabetical file listing
    (an IDE sidebar, a directory `ls`) then sorts by that label FIRST, interleaving one
    group's A/B/C with the other's and burying the 1/2 grouping that gave them meaning.
    Leading with `seq` -- each item's own position among its true siblings, always
    available, always unique there -- fixes the sort order without removing the label
    that makes the filename recognisable against the source page.
    """
    seq_s = f"{seq:02d}"
    m = re.match(r"^([A-Z])\.\s", title)
    if m:
        return f"{seq_s}-{m.group(1)}"
    m = re.match(r"^(\d+(?:\.\d+)+)", title)
    if m:
        return f"{seq_s}-{m.group(1)}"
    m = re.match(r"^\((\w+)\)", title)
    if m:
        return f"{seq_s}-{m.group(1).lower()}"
    m = re.match(r"^(\d+)\.\s", title)
    if m:
        return f"{seq_s}-{int(m.group(1)):02d}"
    return seq_s


@dataclass
class Node:
    level: int                                       # 2 for `##`, 3 for `###`, ...
    title: str
    lines: list[str] = field(default_factory=list)   # prose directly under this heading
    kids: list["Node"] = field(default_factory=list)
    page: int = 0
    end_page: int = 0


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FRONT_MATTER = "Front matter"


def parse_tree(md: str) -> list[Node]:
    """The transcript -> a forest of `##` sections with `###`/`####` nested inside.

    A `#` heading is the jurisdiction name off the cover, not a section. It and everything
    printed before the first `##` become one "Front matter" node -- the same name and the
    same first position stage 1 gives that material, so the two trees can be compared file
    for file. The `#` line keeps its words, demoted to prose, exactly as stage 1 leaves
    "Argentina" as a bare line in 01-front-matter.md: one H1 per file, nothing lost.

    A level SKIP is handled by construction rather than by repair -- a `####` directly
    under a `##`, which happens wherever a section has teal labels but no lighter bar,
    simply nests under whatever is open.
    """
    front = Node(level=2, title=FRONT_MATTER)
    sections: list[Node] = [front]
    stack: list[Node] = [front]
    for line in (md or "").splitlines():
        m = _HEADING.match(line)
        if not m:
            stack[-1].lines.append(line)
            continue
        if len(m.group(1)) == 1:
            stack[-1].lines.append(m.group(2))       # the cover's jurisdiction name
            continue
        node = Node(level=len(m.group(1)), title=m.group(2))
        while stack and stack[-1].level >= node.level:
            stack.pop()
        if stack:
            stack[-1].kids.append(node)
        else:
            sections.append(node)
        stack.append(node)
    return sections


def flatten(nodes: list[Node]) -> list[Node]:
    """Every node, pre-order — i.e. in the order the document prints them."""
    out: list[Node] = []
    for n in nodes:
        out.append(n)
        out.extend(flatten(n.kids))
    return out


def _key(s: str) -> str:
    """Text -> comparable key: ligatures decomposed, everything but [0-9a-z] gone.

    NFKD is not optional. These PDFs encode "fi" as the LIGATURE U+FB01, so Germany's last
    section bar reads "De<fi>nitions" in the text layer while the model, reading the pixels,
    writes "Definitions". Stripping non-alphanumerics without decomposing first turns the
    former into "denitions" and the two never match. It also flattens India's stray
    intra-word spaces ("Pre -Market ing of Funds") for free.
    """
    return re.sub(r"[^0-9a-z]+", "", unicodedata.normalize("NFKD", s or "").lower())


# The bar fills, measured off the PDFs with get_drawings() rather than sampled from a
# render, mapped to the heading level each one means. Two shades of pale mint because the
# documents genuinely use both, and they mean the same thing -- exactly what the prompt
# tells the model.
_BAR_FILLS: tuple[tuple[tuple[float, float, float], int], ...] = (
    ((0.000, 0.361, 0.329), 2),   # #005C54 dark teal, white bold serif + icon
    ((0.212, 0.580, 0.522), 3),   # #369485 mid teal, white bold sans
    ((0.855, 0.933, 0.918), 3),   # #DAEEEA pale mint, teal bold sans
    ((0.882, 0.945, 0.933), 3),   # #E1F1EE the other pale mint
)
# A bar is drawn as TWO rects, an outer and an inner -- 37.0 + 32.0 for a section bar,
# 28.3 + 28.0 for a sub-section one -- and REQUIRING THAT PAIR is what separates a real bar
# from a line of a tinted panel. Measured across Argentina, Germany, India, the United
# States, the Bahamas and Italy: every one of the 90-odd real bars is drawn twice, and the
# only single-rect candidates in the whole sample were the three cover Overview panels,
# whose first prose line lands at h=27.0 and had otherwise passed every other test. Height
# alone could not reject that; the pair does, and it does so for a reason rooted in how the
# document is drawn rather than in a threshold tuned until the sample passed.
_BAR_HEIGHT = {2: (30.0, 40.0), 3: (26.0, 31.0)}
_BAR_RECTS = 2                       # the outer/inner pair; fewer is not a bar


def find_bars(pdf: Path, pages: list[int]) -> list[tuple[int, int, str, str]]:
    """Every heading bar in the document: [(page, level, key, text)], reading order.

    THE SAME SIGNAL THE MODEL IS READING, computed independently. Titles cannot simply be
    searched for in the text layer, because every section title is ALSO printed in the
    cover's Table of Contents -- a first pilot put all nine of Argentina's sections on page
    1, each matching its own TOC entry. A bar is unambiguous: it is drawn once, where the
    section actually starts.

    The pale mint is the hard case, because the design uses that same fill for a 28pt BAR
    and for the tall "Speedread:" and Overview PANELS, which are drawn as one rect per line
    of prose. Three filters in combination, and all three are needed: the fill must be one
    of the measured bar colours, the height must be in that level's window (which rejects
    the 15/17/21/25/42pt panel lines), and the rect must be drawn TWICE (which rejects the
    27pt one that survives the window). Both geometric rules say in code what the prompt
    says to the model in words -- a bar is one short line, a panel is prose.
    """
    import fitz

    out: list[tuple[int, int, str, str]] = []
    with fitz.open(str(pdf)) as doc:
        for p in pages:
            page = doc[p - 1]
            # (level, key) -> [rect count, topmost y, text]; a bar is claimed only once per
            # page, by the pair that drew it.
            cand: dict[tuple[int, str], list] = {}
            for dr in page.get_drawings():
                fill, r = dr.get("fill"), dr["rect"]
                if not fill or r.width <= 200:
                    continue
                for want, level in _BAR_FILLS:
                    if not all(abs(a - b) <= 0.06 for a, b in zip(fill, want)):
                        continue
                    lo, hi = _BAR_HEIGHT[level]
                    if not lo <= r.height <= hi:
                        break
                    # Inset, so a line of prose sitting just under a bar cannot bleed into
                    # the clip -- get_text takes any span whose box merely INTERSECTS.
                    clip = fitz.Rect(r.x0, r.y0 + 3, r.x1, r.y1 - 3)
                    text = " ".join(page.get_text("text", clip=clip).split())
                    key = _key(text)
                    if not key or len(text) > 150:
                        break
                    slot = cand.setdefault((level, key), [0, r.y0, text])
                    slot[0] += 1
                    slot[1] = min(slot[1], r.y0)
                    break
            found = [(y, lvl, k, t) for (lvl, k), (n, y, t) in cand.items()
                     if n >= _BAR_RECTS]
            out.extend((p, lvl, k, t) for _, lvl, k, t in sorted(found))
    return out


# Reference colours for find_squares, measured directly rather than guessed at: sampled
# across every SUMMARY jurisdiction PDF this product has produced (100+), the three
# verdict squares turn out to be drawn from a FIXED palette, not merely "some shade of
# red/amber/green" -- every red square rounds to (231, 107, 54), every amber to
# (226, 209, 110), every green to (112, 193, 112), regardless of jurisdiction. The other
# small images the same scan turns up (a logo mark, a background tint) sit far outside
# this cluster -- near-black, near-white, mid-grey -- so nearest-centroid with a reject
# distance comfortably under the ~115-unit gap between any two real colours tells a
# verdict square from page furniture without ever being told where either one sits.
_SQUARE_COLOURS = {"red": (231, 107, 54), "amber": (226, 209, 110), "green": (112, 193, 112)}
_SQUARE_MAX_SIDE = 40      # the real squares are 20x16; a content photo is not
_SQUARE_MAX_DIST = 60      # < half the ~115-unit gap between any two real colours
# A real duplicate placement, not two distinct squares that happen to sit close: seen on
# a real run (SUMMARY__Costa Rica) where the page's own content stream placed one icon's
# image twice, ~2pt apart, almost exactly on top of itself -- a paste artifact in how that
# one instance was authored, not a PyMuPDF quirk (confirmed off the raw content stream:
# the same `/ImageNN Do` operator twice for one visible icon). Two GENUINE squares never
# overlap this way -- each sits beside its own list item, at least a line's height apart --
# so any pair with more overlap than this is the same square counted twice, not a second
# verdict.
_SQUARE_DUP_IOU = 0.3


def find_squares_detailed(pdf: Path, pages: list[int]) -> list[dict]:
    """Every coloured verdict square, in reading order, WITH the line it sits beside:
    [{"page", "colour", "caption"}].

    THE SAME SIGNAL THE MODEL IS ASKED TO READ ([green]/[amber]/[red], see SQUARES),
    computed independently -- the same argument find_bars makes for heading bars, applied
    to the other picture-only signal this route recovers by eye. A square is a small
    embedded RASTER image, not a vector drawing, so get_images()/get_image_rects() finds
    it directly rather than needing find_bars' drawing-geometry approach; only a handful
    of distinct images are reused across the whole document -- one asset per colour -- so
    classifying each XREF once, by its own average pixel colour, classifies every place
    it is placed.

    THE CAPTION IS WHAT MAKES A SQUARE ADDRESSABLE, and it is read off the page rather
    than guessed: a square sits in the left margin at a known y, so the words to the right
    of its rect that vertically overlap it ARE the verdict line it marks. Pairing a square
    to a tag by that text is what lets a MISSING tag be placed and a wrong colour be
    corrected. Pairing by ordinal position cannot: the moment the model skips one square,
    every tag after it lines up against the wrong square, and a corrector working off that
    would rewrite correct verdicts (a red "Prohibited" relabelled amber "Workable") while
    the per-page counts still agreed.
    """
    import fitz

    def _classify(pix) -> str | None:
        n = pix.n
        if n < 3:
            return None
        samples = pix.samples
        rs, gs, bs = samples[0::n], samples[1::n], samples[2::n]
        if not rs:
            return None
        avg = (sum(rs) / len(rs), sum(gs) / len(gs), sum(bs) / len(bs))
        best, best_d = None, 1e9
        for name, ref in _SQUARE_COLOURS.items():
            d = sum((a - b) ** 2 for a, b in zip(avg, ref)) ** 0.5
            if d < best_d:
                best, best_d = name, d
        return best if best_d < _SQUARE_MAX_DIST else None

    def _iou(a, b) -> float:
        """Overlap as a fraction of the SMALLER rect -- see _SQUARE_DUP_IOU."""
        x0, y0 = max(a.x0, b.x0), max(a.y0, b.y0)
        x1, y1 = min(a.x1, b.x1), min(a.y1, b.y1)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        inter = (x1 - x0) * (y1 - y0)
        area_a = (a.x1 - a.x0) * (a.y1 - a.y0)
        area_b = (b.x1 - b.x0) * (b.y1 - b.y0)
        return inter / min(area_a, area_b) if area_a and area_b else 0.0

    out: list[dict] = []
    with fitz.open(str(pdf)) as doc:
        colour_by_xref: dict[int, str | None] = {}
        for p in pages:
            page = doc[p - 1]
            found: list[tuple[float, float, str, object]] = []
            kept_rects: list = []          # every rect kept on this page so far, any colour
            # get_images(full=True) lists the SAME xref once per reference to it in the
            # page's resource tree -- not once per placement -- so it can list one square's
            # image four times over. get_image_rects(xref) already returns every placement
            # of that xref on the page in one call, so processing an xref twice would not
            # find new squares, only COUNT THE SAME ONES AGAIN: a page with 4 real squares
            # first came back with 16, each one accounted for exactly 4 times.
            done_this_page: set[int] = set()
            for info in page.get_images(full=True):
                xref = info[0]
                if xref in done_this_page:
                    continue
                done_this_page.add(xref)
                if xref not in colour_by_xref:
                    pix = fitz.Pixmap(doc, xref)
                    colour_by_xref[xref] = (
                        _classify(pix) if pix.width <= _SQUARE_MAX_SIDE
                        and pix.height <= _SQUARE_MAX_SIDE else None)
                colour = colour_by_xref[xref]
                if colour is None:
                    continue
                for rect in page.get_image_rects(xref):
                    # SAME SQUARE, PLACED TWICE -- see _SQUARE_DUP_IOU. Checked against
                    # every rect kept on the page so far, not just this xref's own: a
                    # duplicate placement is a property of the page's content stream, not
                    # of which image it happens to be.
                    if any(_iou(rect, r) > _SQUARE_DUP_IOU for r in kept_rects):
                        continue
                    kept_rects.append(rect)
                    found.append((rect.y0, rect.x0, colour, rect))
            # The caption, per square: words to the RIGHT of the image rect whose own
            # vertical span overlaps it. The 1pt/2pt slacks absorb the sub-point
            # disagreement between an image rect and the baseline box of the text set
            # against it -- without them a square whose rect ends a hair past the first
            # glyph's x0 loses its first word.
            words = page.get_text("words")     # (x0, y0, x1, y1, word, block, line, no)
            for _, _, colour, rect in sorted(found, key=lambda f: (f[0], f[1])):
                beside = [w for w in words
                          if w[0] > rect.x1 - 1 and w[3] > rect.y0 - 2 and w[1] < rect.y1 + 2]
                beside.sort(key=lambda w: (round(w[1], 1), w[0]))
                out.append({"page": p, "colour": colour,
                            "caption": " ".join(w[4] for w in beside)})
    return out


def find_squares(pdf: Path, pages: list[int]) -> list[tuple[int, str]]:
    """Every coloured verdict square in the document, in reading order: [(page, colour)].

    The page/colour view of find_squares_detailed -- all the scorecard's per-page
    comparison consumes, and the shape it has always been handed."""
    return [(s["page"], s["colour"]) for s in find_squares_detailed(pdf, pages)]


_TAG_RE = re.compile(r"\[(green|amber|red)\]\s*")
# How much of a square's caption has to be found in the transcript. Long enough that two
# different verdicts cannot collide (the boilerplate "Prohibited without use of a local
# intermediary" is 45 characters before it starts to differ), short enough to survive the
# model's own line wrapping and the trailing words a caption picks up from the next column.
_ANCHOR_CHARS = 60
_ANCHOR_MIN = 12                     # below this a caption cannot identify anything
# A tag belongs to the caption it IMMEDIATELY precedes. Looking further back than this
# would let the previous paragraph's tag be read as this one's.
_ANCHOR_LOOKBACK = 30


def _norm_offsets(s: str) -> tuple[str, list[int]]:
    """_key(s), plus the original index each surviving character came from.

    _key alone cannot be used to EDIT the transcript: it throws away the offsets, so a
    match in normalised space cannot be turned back into a place to write. This keeps the
    mapping so a caption found in normalised text can be located in the real markdown."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKD", ch).lower():
            if c.isalnum() and c.isascii():
                out.append(c)
                idx.append(i)
    return "".join(out), idx


def _anchor_squares(md: str, squares: list[dict]) -> list[dict]:
    """Pair each square to the tag on its own caption: [{"square", "colour", "at", "end"}].

    `colour` is the tag already written there, or None where the model wrote none; `at` is
    where that tag's colour word starts (or where one must be inserted), None when the
    caption could not be found in the transcript at all.

    ANCHORED ON THE CAPTION, NEVER ON ORDINAL POSITION -- see find_squares_detailed. The
    captions are consumed left to right so a verdict line repeated verbatim in several
    sections (the norm in these documents, not the exception) takes a different occurrence
    each time, in the order both the page and the transcript put them in."""
    nmd, imap = _norm_offsets(md)
    out: list[dict] = []
    cur = 0
    for sq in squares:
        cap = _key(sq["caption"])[:_ANCHOR_CHARS]
        at = -1
        if len(cap) >= _ANCHOR_MIN:
            at = nmd.find(cap, cur)
            if at < 0:
                # Not ahead of the last anchor. Reading order is the assumption, not a
                # guarantee -- a caption the model moved still deserves its correction.
                at = nmd.find(cap)
        if at < 0:
            out.append({"square": sq, "colour": None, "at": None, "end": None})
            continue
        cur = at + len(cap)
        orig = imap[at]
        window = md[max(0, orig - _ANCHOR_LOOKBACK):orig]
        tags = list(_TAG_RE.finditer(window))
        if tags and tags[-1].end() >= len(window) - 2:
            base = max(0, orig - _ANCHOR_LOOKBACK)
            out.append({"square": sq, "colour": tags[-1].group(1),
                        "at": base + tags[-1].start(1), "end": base + tags[-1].end(1)})
        else:
            out.append({"square": sq, "colour": None, "at": orig, "end": orig})
    return out


def _attribute_tag_pages(md: str, streams: dict[int, str], pages: list[int]) -> list[int]:
    """Page number for every [colour] tag in `md`, in reading order. Shared by
    attribute_squares and correct_square_colours so the two can never disagree about where
    a tag sits -- see attribute_squares for why this search exists at all.

    SEARCHED FORWARD FROM THE LAST HIT, NEVER FROM PAGE ONE, AND NEVER THE SAME OCCURRENCE
    TWICE. Verdict captions are boilerplate reused verbatim across sections -- "Workable
    depending on how activity structured" printed on pages 2, 3 AND 4 of one document
    (SUMMARY__Costa Rica) is typical, not an outlier. Forward-only search alone still gets
    this wrong: once page 2 matches for the first tag, it KEEPS matching for the second and
    third too, because the phrase is still sitting right there on page 2 -- nothing about
    "forward from last" ever moves `last` past a page that still matches. So each page's
    own occurrences of a snippet are counted (`streams[p].count(...)`) and consumed one at
    a time; once page 2's one real occurrence has paid for one tag, the second tag's search
    is no longer satisfied by page 2 and moves on to page 3, exactly as the document does.
    """
    used: dict[tuple[int, str], int] = {}
    out: list[int] = []
    last = pages[0]
    for m in re.finditer(r"\[(green|amber|red)\]\s*([^\n]{0,80})", md):
        snippet = _key(m.group(2))[:40]
        hit = None
        if len(snippet) >= 12:
            for p in pages:
                if p < last:
                    continue
                seen = streams[p].count(snippet)
                if seen and used.get((p, snippet), 0) < seen:
                    used[(p, snippet)] = used.get((p, snippet), 0) + 1
                    hit = p
                    break
        last = hit or last
        out.append(last)
    return out


def attribute_squares(md: str, pdf: Path, pages: list[int]) -> list[tuple[int, str]]:
    """Every [colour] tag the MODEL wrote, located to the page its paragraph sits on.

    The other half of the find_squares comparison: find_squares reads the truth off the
    PDF's own images, page by page; this reads what the model CLAIMED, page by page, so
    the two can be compared per page rather than merely as one whole-document total --
    which matters, because two swapped tags (a red one read as amber, an amber one read
    as red elsewhere) cancel out in a document-wide count and vanish. Per page they do not.

    LOCATED BY THE SQUARE'S OWN CAPTION where that caption is in the transcript, because
    then the page is not inferred at all -- the square is on the page the PDF puts it on,
    and the tag is the one written against its caption. The text search below is the
    fallback for a tag no square claimed (the model wrote one where the page has none, or
    the caption was paraphrased past recognition), so every tag is still counted somewhere
    and a spurious tag cannot vanish from the comparison by going unattributed.

    The fallback mirrors the search attribute_pages runs for a bar-less heading: the tag
    carries no page, so the words right after it are searched for in each page's own text
    layer, falling back to the previous tag's page. See _attribute_tag_pages for why that
    search must be forward-only AND occurrence-aware.
    """
    matches = list(_TAG_RE.finditer(md))
    anchored: dict[int, int] = {}                      # tag start offset -> square's page
    for a in _anchor_squares(md, find_squares_detailed(pdf, pages)):
        if a["colour"] is not None:
            anchored[a["at"]] = a["square"]["page"]
    if len(anchored) == len(matches):
        return [(anchored[m.start(1)], m.group(1)) for m in matches]
    streams = _page_streams(pdf, pages)
    fallback = _attribute_tag_pages(md, streams, pages)
    return [(anchored.get(m.start(1), fallback[i]), m.group(1))
            for i, m in enumerate(matches)]


def correct_square_colours(md: str, pdf: Path,
                           pages: list[int]) -> tuple[str, list[dict]]:
    """Reconcile the transcript's [colour] tags against the PDF's own squares.

    Two corrections, each justified by ONE square's own caption and nothing else:

      colour  the model named the shade wrong -- every mismatch seen in practice
              (SUMMARY__Malaysia, SUMMARY__Denmark, SUMMARY__Ecuador) was the right
              paragraph tagged the wrong shade. The tag is overwritten in place.
      missing the model wrote no tag beside a square at all -- consistently the first
              green "The following activities are permitted..." item on page 2
              (SUMMARY__Paraguay, SUMMARY__Dominican Republic, SUMMARY__South Africa).
              The tag is inserted at that caption.

    ANCHORED PER SQUARE, NOT PAIRED BY POSITION, and that is the whole safety argument.
    The previous rule -- correct a page only where its tag count already equalled its
    square count -- looked safe and was not: a tag the model never wrote could be masked
    into a matching count by another tag drifting onto that page, and the correction then
    ran against a sequence shifted by one, rewriting a red "Prohibited" to amber
    "Workable" with every count still agreeing. Anchoring removes the failure rather than
    guarding it: a square is corrected only where ITS caption was found and the tag sits
    against THAT caption, so a square the model skipped shifts nothing -- the squares
    around it still anchor to their own captions.

    A square whose caption cannot be found in the transcript is left alone and stays a
    markers finding for a human, exactly as before this function existed.
    """
    anchors = _anchor_squares(md, find_squares_detailed(pdf, pages))

    corrections: list[dict] = []
    edits: list[tuple[int, int, str]] = []     # (start, end, replacement)
    for a in anchors:
        sq, at = a["square"], a["at"]
        if at is None:                          # caption not in the transcript: not ours to touch
            continue
        if a["colour"] is None:
            edits.append((at, at, f"[{sq['colour']}] "))
            corrections.append({"page": sq["page"], "kind": "inserted", "from": None,
                                "to": sq["colour"], "caption": sq["caption"][:80]})
        elif a["colour"] != sq["colour"]:
            edits.append((a["at"], a["end"], sq["colour"]))
            corrections.append({"page": sq["page"], "kind": "recoloured",
                                "from": a["colour"], "to": sq["colour"],
                                "caption": sq["caption"][:80]})

    # Applied back to front so an earlier edit cannot move a later one's offsets.
    for start, end, text in sorted(edits, key=lambda e: -e[0]):
        md = md[:start] + text + md[end:]
    return md, corrections


def attribute_pages(sections: list[Node], pdf: Path, pages: list[int]) -> dict:
    """Fill in page/end_page for every node. Returns what could not be matched.

    DERIVED IN CODE, NEVER ASKED OF THE MODEL. Every consumer of the breadcrumb treats the
    range as fact -- the viewer jumps to it, ai_postprocess._section_pages sends exactly
    those pages to stage 4 as its ground truth, check_content_localized.py audits the line
    -- and a page number a model wrote is a page number a model can invent. Same reasoning
    as _hide_mineru_counts, which keeps MinerU's row counts away from the model for the
    length of a call.

    Two different questions, so two different methods:

    A `##` OR `###` HEADING SITS ON A BAR, and find_bars has already found every bar in the
    document in reading order. So these are ALIGNED against that sequence rather than
    searched for: a forward-only pointer walks the bar list as it walks the headings, which
    is what keeps the second "2. Public Offer Rule" in a document that has two from
    resolving to the first one's page.

    A `####` HEADING HAS NO BAR -- it is teal text alone on a line -- so it is searched for
    in the text layer, but only WITHIN the page range of the nearest bar-level heading
    above it. That bound is what makes the search safe: unbounded, "Intermediaries" is
    fine, but any title that also appears in the cover's Table of Contents comes back as
    page 1.

    Falling back to the previous heading's page is right far more often than not, because
    headings arrive in reading order -- but it is a guess, so every one is counted and
    reported.
    """
    bars = find_bars(pdf, pages)
    flat = flatten(sections)
    streams = _page_streams(pdf, pages)

    # ---- bar-level headings, aligned against the bars in order
    unlocated: list[str] = []
    i = 0
    for node in flat:
        if node.level > 3:
            continue
        if node.title == FRONT_MATTER:
            node.page = pages[0]                 # the cover, by definition
            continue
        key = _key(node.title)
        j = next((k for k in range(i, len(bars)) if bars[k][2] == key), None)
        if j is None:
            unlocated.append(f"{'#' * node.level} {node.title}")
        else:
            node.page, i = bars[j][0], j + 1

    # ---- deeper headings, searched inside their own section's pages
    for idx, node in enumerate(flat):
        if node.level <= 3:
            continue
        lo, hi = _ancestor_range(flat, idx, pages)
        key = _key(node.title)
        hit = None
        # Under 4 characters a title is not evidence of anything, so it inherits instead.
        if len(key) >= 4:
            hit = next((q for q in pages if lo <= q <= hi and key in streams[q]), None)
        if hit is None:
            unlocated.append(f"{'#' * node.level} {node.title}")
        node.page = hit or lo

    # ---- anything still unplaced inherits the previous heading in reading order
    cursor = pages[0]
    for node in flat:
        node.page = cursor = (node.page or cursor)

    # A section's range runs to where the next thing OUTSIDE its subtree starts, so a
    # parent spans all its children. The last page of one section and the first of the next
    # are usually the same sheet, so this over-includes by design: ai_postprocess treats a
    # breadcrumb NARROWER than the truth as lost ground truth, never the other way round.
    last = pages[-1]
    for idx, node in enumerate(flat):
        node.end_page = next((n.page for n in flat[idx + 1:] if n.level <= node.level), last)

    # The cross-check, and the reason find_bars is worth its length: a bar the model never
    # turned into a heading is a heading it MISSED, and that is invisible in the transcript
    # itself -- the prose is all there, just unlabelled and folded into the section above.
    #
    # AT ANY LEVEL, which is what this dimension's own caveat promises ("counts bars, not
    # correctness of nesting -- a #### that should have been a ### still counts as
    # present"). Filtering to level <= 3 broke that promise and reported a heading that was
    # written, correct and in the right place as a bar with NO heading: the United Kingdom's
    # "A. Restrictions on marketing a fund" sits inside "### 2. Firm level requirements", so
    # #### is the RIGHT depth for it even though the bar behind it is painted in the
    # level-3 style. The bar fill says how the line is drawn, not how deep it belongs, and
    # scoring the two as if they were the same thing marked four documents down for nesting
    # they had got right.
    emitted = {_key(n.title) for n in flat}
    return {
        "bars_found": len(bars),
        "bars_not_emitted": [t for _, _, k, t in bars if k not in emitted],
        "unlocated_headings": unlocated,
    }


def _page_streams(pdf: Path, pages: list[int]) -> dict[int, str]:
    """{page: its text layer as a comparable key} — see _key for why NFKD matters."""
    import fitz

    with fitz.open(str(pdf)) as doc:
        return {p: _key(doc[p - 1].get_text()) for p in pages}


def _ancestor_range(flat: list[Node], idx: int, pages: list[int]) -> tuple[int, int]:
    """The page range of the nearest bar-level heading above `flat[idx]`.

    Computed from the pages already assigned rather than from end_page, which is not filled
    in yet at this point -- and deliberately so: end_page depends on where the deep
    headings land, which is the very thing this bounds.
    """
    anc = next((k for k in range(idx, -1, -1) if flat[k].level <= 3), None)
    if anc is None:
        return pages[0], pages[-1]
    lo = flat[anc].page or pages[0]
    nxt = next((n.page for n in flat[anc + 1:] if n.level <= flat[anc].level and n.page), None)
    return lo, (nxt or pages[-1])


def crumb(pdf_name: str, start: int, end: int) -> str:
    """*Source: `x.pdf`, page 3–4* — the exact shape _CRUMB_PAGES matches.

    `max` guards the one way this can go wrong: a heading whose title could not be found
    inherits a later page than its successor, which would otherwise print "page 5–4" and
    parse to an empty range.
    """
    end = max(start, end)
    rng = f"page {start}" if end == start else f"page {start}–{end}"
    return f"*Source: `{pdf_name}`, {rng}*"


def _flat_body(node: Node) -> str:
    """A node's prose plus every descendant's, headings and all, as one body.

    What --depth running out means: the sub-headings stay in the file as headings instead
    of becoming files of their own. Nothing is dropped by choosing a shallower split.
    """
    parts = ["\n".join(node.lines).strip("\n")]
    for kid in node.kids:
        parts.append("#" * kid.level + " " + kid.title)
        parts.append(_flat_body(kid))
    return "\n\n".join(p for p in parts if p.strip()).strip("\n")


def _emit(node: Node, rel: Path, pdf_name: str, seq: int, depth: int) -> dict:
    base = f"{item_prefix(node.title, seq)}-{slug(node.title)}"
    body = "\n".join(node.lines).strip("\n")
    rec: dict = {"title": node.title, "level": node.level, "page": node.page,
                 "end_page": max(node.page, node.end_page)}
    rel.mkdir(parents=True, exist_ok=True)
    if node.kids and depth > 0:
        d = rel / base
        d.mkdir(parents=True, exist_ok=True)
        index = [f"# {node.title}", "", crumb(pdf_name, node.page, node.end_page), ""]
        if body.strip():
            # The section's OWN prose, printed above its first sub-heading. Once the
            # section becomes a directory this is the only place it can live, and it is
            # content like any other -- the "Speedread:" panel and the status line under a
            # dark bar are both this.
            (d / "00-overview.md").write_text(
                f"# {node.title} — Overview\n\n"
                f"{crumb(pdf_name, node.page, node.kids[0].page)}\n\n{body}\n")
            index.append("- [Overview](00-overview.md)")
            rec["overview_words"] = len(words(body))
        rec["kids"] = []
        for s2, kid in enumerate(node.kids, 1):
            kr = _emit(kid, d, pdf_name, s2, depth - 1)
            index.append(f"- [{kid.title}]({Path(kr['file']).name})")
            rec["kids"].append(kr)
        (d / "README.md").write_text("\n".join(index) + "\n")
        rec["file"] = f"{base}/README.md"
    else:
        text = _flat_body(node)
        (rel / f"{base}.md").write_text(
            f"# {node.title}\n\n{crumb(pdf_name, node.page, node.end_page)}\n\n{text}\n")
        rec["file"] = f"{base}.md"
        rec["words"] = len(words(text))
    return rec


def split(md: str, pdf: Path, pages: list[int], dest: Path, depth: int = 3) -> dict:
    """transcript.md -> a titled tree under `dest`, in the stage-3 shape.

    Rebuilt from scratch each time: a section renamed by a prompt change would otherwise
    leave its old file behind, and a stale chunk nothing links to is worse than no chunk.
    """
    sections = parse_tree(md)
    checks = attribute_pages(sections, pdf, pages)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    tree = [_emit(n, dest, pdf.name, i, depth) for i, n in enumerate(sections, 1)]
    flat = flatten(sections)
    return {
        "dir": str(dest), "depth": depth, "nodes": len(flat),
        "sections": sum(n.level == 2 for n in flat), "tree": tree,
        "square_tree_mismatches": verify_tree_squares(dest, pdf, pages), **checks,
    }


def verify_tree_squares(dest: Path, pdf: Path, pages: list[int]) -> list[dict]:
    """Re-read the tree just written and check every square still carries its true colour.

    THE TREE IS WHAT A READER ACTUALLY SEES -- the Doc Gallery and the corpus view render
    04_stage4_ai/, not transcript.md -- but the scorecard scores the transcript, so a tree
    that disagrees with it is invisible to every number on the page. That is not
    hypothetical: correcting a transcript WITHOUT re-running split left Ecuador's rendered
    "Funds and IMAS: Prohibited..." reading [red] while the transcript beside it, the
    scorecard, and the PDF's own square all said amber.

    Ordering is the whole risk this guards: correct_square_colours must run BEFORE split,
    or the tree is built from uncorrected text. Checking the bytes on disk catches that
    whichever way the mistake is made -- a reordering here, or a tree patched out of band.
    """
    md = "\n".join(f.read_text() for f in sorted(dest.rglob("*.md")))
    out: list[dict] = []
    for a in _anchor_squares(md, find_squares_detailed(pdf, pages)):
        sq = a["square"]
        if a["at"] is None or a["colour"] == sq["colour"]:
            continue
        out.append({"page": sq["page"], "in_tree": a["colour"], "in_pdf": sq["colour"],
                    "caption": sq["caption"][:80]})
    return out


def report_checks(info: dict) -> None:
    """The two things a chunk tree can get wrong, said out loud rather than left in JSON.

    Both are recoverable by re-running, and neither shows up in the transcript: a section
    whose bar the model skipped still has all its prose, just folded silently into the
    section above it.
    """
    for t in info["bars_not_emitted"]:
        print(f"  WARNING: a bar the transcript has no heading for: {t!r} — its prose is "
              f"folded into the section above")
    for h in info["unlocated_headings"]:
        print(f"  WARNING: {h!r} is not on any bar or page found — page range inherited")
    for m in info.get("square_tree_mismatches") or []:
        print(f"  WARNING: the WRITTEN TREE says [{m['in_tree']}] where the PDF's own square "
              f"on p{m['page']} is {m['in_pdf']}: {m['caption']!r} — the rendered document "
              f"disagrees with the source, so split ran on uncorrected text")


def print_tree(tree: list[dict], indent: int = 0) -> None:
    for rec in tree:
        pages = (f"p{rec['page']}" if rec["page"] == rec["end_page"]
                 else f"p{rec['page']}-{rec['end_page']}")
        n = rec.get("words", rec.get("overview_words", 0))
        print(f"           {'  ' * indent}{rec['file']:<52} {pages:>8}  {n:>5}w")
        print_tree(rec.get("kids") or [], indent + 1)


# ---------------------------------------------------------------- the pass itself

def extract_document(pdf: Path, dest: Path, *, model: str | None = None,
                     pages: list[int] | None = None, dpi: int = 150,
                     max_tokens: int = 32000, markers: str = "tag",
                     furniture: str = "omit", do_split: bool = True, depth: int = 3,
                     region: str | None = None, profile: str | None = None,
                     product: str = "", jurisdiction: str = "", doc_id: str = "",
                     log=print) -> dict:
    """Transcribe one summary PDF into `dest`, score it, and return the scorecard.

    THE ONE ENTRY POINT. main() and run_corpus both come through here, so a routed corpus
    document and a hand-run one cannot drift apart -- which they would the moment the
    corpus route grew its own copy of the render/call/score sequence.

    Writes into `dest` the same job shape the corpus view reads: transcript.md, the
    04_stage4_ai/ tree, prompt.txt, report.json, scorecard.json, corpus_meta.json and the
    page renders. Raises on a Bedrock failure -- the caller decides whether one document
    failing should stop a corpus.

    SPENDS MONEY. Roughly $0.026 per page at Sonnet 5's (unconfirmed) rate; a 9-page
    summary came to $0.23. There is no internal cap: calling this IS the authorisation,
    exactly as run_stage4(force=True) treats being called.
    """
    import fitz

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with fitz.open(str(pdf)) as doc:
        count = doc.page_count
    pages = pages or list(range(1, count + 1))

    instr = build_instr(markers, furniture)
    (dest / "prompt.txt").write_text(SYSTEM + "\n\n" + ("-" * 78) + "\n\n" + instr)

    images = render(pdf, pages, dpi, dest / "pages")
    model = resolve_model(model)
    log(f"  summary-ai: {len(images)} of {count} pages at {dpi} dpi, model {model}")

    client = bedrock_client(region, profile)
    t0 = time.time()
    md, usage, stop = transcribe(client, model, images, instr, max_tokens)
    secs = round(time.time() - t0, 1)
    md = _FENCE.sub("", md).strip()

    # A truncated transcript is the one failure that READS as a success -- the Markdown
    # looks fine and simply stops -- so it is said loudly rather than left in the JSON.
    if stop == "max_tokens":
        log(f"  summary-ai: WARNING stopReason=max_tokens — the transcript is CUT SHORT. "
            f"Re-run with max_tokens={max_tokens * 2}.")

    # CORRECT WHAT CAN BE CORRECTED, before anything is saved: the model places a square
    # tag reliably and names its colour unreliably, so the colour is overwritten with the
    # PDF's own true one wherever position makes that unambiguous (see
    # correct_square_colours). This is why the saved transcript, not just the scorecard,
    # is the one built from find_squares -- a reader should not have to know a correction
    # tool exists to get the right answer.
    square_corrections: list[dict] = []
    if markers != "none":
        md, square_corrections = correct_square_colours(md, pdf, pages)
        if square_corrections:
            log(f"  summary-ai: reconciled {len(square_corrections)} square(s) against the "
                f"PDF's own images: "
                + ", ".join(f"p{c['page']} {c['from']}->{c['to']}" if c["kind"] == "recoloured"
                            else f"p{c['page']} +{c['to']}"
                            for c in square_corrections))

    (dest / "transcript.md").write_text(md + "\n")
    source_text = pdf_text(pdf, pages)
    page_texts = {p: _page_text(pdf, p) for p in pages}
    cov, hs = coverage(source_text, md, page_texts), headings(md)
    info = split(md, pdf, pages, dest / SPLIT_DIR, depth) if do_split else None
    checks = info if info else attribute_pages(parse_tree(md), pdf, pages)
    punct = punctuation(source_text, md, page_texts)
    health = page_health(page_texts, md)
    ledger = word_ledger(page_texts, md)
    cost = round(cost_usd(model, usage["in"], usage["out"]), 4)

    timing = {"seconds": secs, "pages": len(pages),
              "seconds_per_page": round(secs / len(pages), 2) if pages else None,
              "steps": {"summary_ai": secs}, "slowest_step": "summary_ai",
              "fallback_seconds": 0}
    report = {
        "pdf": str(pdf), "pages": pages, "page_count": count, "dpi": dpi, "model": model,
        "markers": markers, "furniture": furniture, "seconds": secs, "stop_reason": stop,
        "tokens_in": usage["in"], "tokens_out": usage["out"], "cost_usd": cost,
        "headings": hs, "coverage": cov, "punctuation": punct, "split": info,
        "page_health": health, "word_ledger": ledger,
        "square_corrections": square_corrections,
    }
    (dest / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

    markers_n = sum(md.count(t) for t in ("[green]", "[amber]", "[red]"))
    squares_pdf = find_squares(pdf, pages) if markers != "none" else None
    squares_model = attribute_squares(md, pdf, pages) if markers != "none" else None
    sc = scorecard(cov, hs, checks, markers_n, timing, doc_key(pdf), punct, health,
                   ledger, squares_pdf, squares_model)
    # The cost belongs ON the scorecard, next to the gate it was spent earning -- the same
    # argument run_corpus.timing_block makes for wall clock.
    sc["timing"] = dict(timing, cost_usd=cost, tokens_in=usage["in"],
                        tokens_out=usage["out"], model=model)
    write_job_meta(dest, pdf, product or "", jurisdiction or "", doc_id or "")
    (dest / "scorecard.json").write_text(json.dumps(sc, indent=2, ensure_ascii=False))

    log(f"  summary-ai: {secs}s, {usage['in']} in / {usage['out']} out, ${cost:.4f}, "
        f"gate {sc['gate'].upper()} worst {sc['worst_score']} ({sc['weakest_dimension']})")
    return {"report": report, "scorecard": sc, "cost_usd": cost, "seconds": secs,
            "timing": timing, "split": info}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", help="a SUMMARY source.pdf")
    # NOT `choices`: a Bedrock model id must be accepted verbatim, or a model released
    # after this script was written could not be run without editing it.
    ap.add_argument("--model", default=None, metavar="NAME",
                    help="a short alias (" + " | ".join(sorted(MODEL_ALIASES)) + ") or any "
                         "Bedrock model id. Default: ACI_STAGE4_AI_MODEL.")
    ap.add_argument("--pages", default=None, metavar="SPEC",
                    help="'1-3', '2,4,5' or 'all' (default: all)")
    ap.add_argument("--dpi", type=int, default=150,
                    help="page render resolution (default 150, as stage 4 uses)")
    ap.add_argument("--max-tokens", type=int, default=32000,
                    help="output cap. A 9-page summary runs to roughly 12k (default 32000)")
    ap.add_argument("--markers", default="tag", choices=("tag", "none"),
                    help="'tag' prefixes a paragraph carrying a coloured square with "
                         "[green]/[amber]/[red]; 'none' drops them, and with them the only "
                         "record of a verdict that is drawn rather than written")
    ap.add_argument("--furniture", default="omit", choices=("omit", "keep"),
                    help="the repeated running footer. 'omit' leaves it out, which DOES "
                         "show up in missing_runs — that is expected, not a failure")
    # OFF BY DEFAULT. The transcription is the thing being tested, and it has to be right
    # before a tree built on top of it means anything: chunking a transcript whose headings
    # are wrong just distributes the error into files. --split when the transcript reads
    # correctly, or --split-only later against one already on disk.
    ap.add_argument("--split", action="store_true",
                    help="also write the titled chunk tree under 04_stage4_ai/ (default: "
                         "transcript.md only — get the extraction right first). REQUIRED "
                         "to see the content in the corpus view: its Document pane reads "
                         "STAGE_DIRS and ignores a plain transcript.md")
    ap.add_argument("--split-only", action="store_true",
                    help="re-split the transcript.md already in the out dir and call "
                         "NOTHING. The split is the part that needs iterating, and "
                         "re-paying for the transcription to tune it would be absurd")
    ap.add_argument("--depth", type=int, default=3, metavar="N",
                    help="how many heading levels may become directories (default 3: ## "
                         "then ### then ####). Deeper headings stay inline, losing nothing")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="default: out/summary_ai/<document folder name>")
    # None -> bedrock_client falls back to settings.bedrock_region (ACI_BEDROCK_REGION),
    # so the AI region is one setting rather than a literal repeated per entry point.
    ap.add_argument("--region", default=None)
    ap.add_argument("--profile", default=None, help="AWS profile")
    ap.add_argument("--dry-run", action="store_true",
                    help="render the pages and write prompt.txt, call nothing, spend "
                         "nothing — for reading the prompt exactly as it would be sent")
    args = ap.parse_args()

    pdf = Path(args.pdf).resolve()
    if not pdf.exists():
        print(f"no such PDF: {pdf}", file=sys.stderr)
        return 2

    import fitz
    with fitz.open(str(pdf)) as doc:
        count = doc.page_count
    pages = parse_pages(args.pages, count)
    if not pages:
        print(f"--pages {args.pages!r} selects nothing of {count} pages", file=sys.stderr)
        return 2

    # <root>/<product>/<job>/ -- the layout hybrid_extract_ui walks, so the run appears in
    # the corpus view with nothing to wire up. Taken from the source job's own directory
    # names where the PDF sits in one, so a document keeps the identity it already has.
    src_meta = _read_json(pdf.parent / "corpus_meta.json") or {}
    product = src_meta.get("product") or pdf.parent.parent.name
    job = pdf.parent.name if src_meta else pdf.stem
    out = Path(args.out) if args.out else \
        HERE.parent / "out" / "summary_ai" / product / job
    out.mkdir(parents=True, exist_ok=True)

    # Before the prompt is built and before a page is rendered: --split-only touches
    # neither, so it cannot cost anything even by accident.
    if args.split_only:
        tr = out / "transcript.md"
        if not tr.exists():
            print(f"no transcript to split at {tr} — run without --split-only first",
                  file=sys.stderr)
            return 2
        info = split(tr.read_text(), pdf, pages, out / SPLIT_DIR, args.depth)
        rp = out / "report.json"
        if rp.exists():                       # keep ONE report; the split is part of it
            report = json.loads(rp.read_text())
            report["split"] = info
            rp.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"{pdf.parent.name}: re-split {tr.name} -> {info['sections']} sections, "
              f"{info['nodes']} nodes, depth {info['depth']}")
        report_checks(info)
        print_tree(info["tree"])
        return 0

    if args.dry_run:
        # Rendered and written, nothing called: the prompt exactly as it would be sent.
        instr = build_instr(args.markers, args.furniture)
        (out / "prompt.txt").write_text(SYSTEM + "\n\n" + ("-" * 78) + "\n\n" + instr)
        images = render(pdf, pages, args.dpi, out / "pages")
        print(f"{job}: {len(images)} of {count} pages at {args.dpi} dpi, "
              f"markers={args.markers}, furniture={args.furniture}")
        print(f"  prompt -> {out / 'prompt.txt'}  ({len(instr)} chars)")
        print("  dry run: nothing called, nothing spent")
        return 0

    try:
        res = extract_document(
            pdf, out, model=args.model, pages=pages, dpi=args.dpi,
            max_tokens=args.max_tokens, markers=args.markers, furniture=args.furniture,
            do_split=args.split, depth=args.depth, region=args.region,
            profile=args.profile, product=product,
            jurisdiction=src_meta.get("jurisdiction") or job,
            doc_id=src_meta.get("doc_id") or job)
    except Exception as e:                                   # noqa: BLE001
        print(f"  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    sc, cov, hs = res["scorecard"], res["report"]["coverage"], res["report"]["headings"]
    info = res["split"]
    print(f"  words    {cov['pdf_words']} in the text layer -> {cov['out_words']} written")
    for h in hs:
        print(f"           {h}")
    if cov["missing_runs"]:
        print("  longest runs NOT transcribed:")
        for r in cov["missing_runs"][:6]:
            print(f"           {r}")
    if info:
        print(f"  chunks   {info['sections']} sections, {info['nodes']} nodes, "
              f"depth {info['depth']}")
        report_checks(info)
        print_tree(info["tree"])
    print(f"  gate     {sc['gate'].upper()} — worst {sc['worst_score']} "
          f"({sc['weakest_dimension']}):  "
          + "  ".join(f"{k} {v['score']}" for k, v in sc["dimensions"].items()))
    for f in sc["findings"]:
        print(f"  {f['severity'].upper()}: {f['title']}")
    print(f"  -> {out / 'transcript.md'}")
    print(f"  -> {out / 'report.json'}")
    print(f"  -> {out / 'scorecard.json'}  (corpus view: "
          f"hybrid_extract_ui.py --corpus-root out/summary_ai)")
    if info:
        print(f"  -> {out / SPLIT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
