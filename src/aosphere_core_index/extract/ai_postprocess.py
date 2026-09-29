"""Stage 4 — AI post-processing of MinerU tables.

Stage 3 splices MinerU's table HTML into the stage-1 tree. MinerU is good at finding
table geometry and bad at two things inside a cell: it drops the space between two
words that sat either side of a line break ("registrationThe", "t.Marketing"), and it
loses a hard line break so two logically separate lines run together. `realign_table_spacing`
recovers what it can deterministically by diffing the cell against the PDF text layer;
this stage hands the rest to a vision model, showing it the rendered page so it can SEE
where the break belongs rather than guess.

The whole design rests on one invariant, enforced mechanically rather than by prompting:

    THE CONTENT STREAM MAY NOT CHANGE.

Strip the tags, drop every non-alphanumeric character, lowercase what is left, and the
before and after must be byte-identical. That admits exactly the repairs we want, because
every one of them is about WHERE A BOUNDARY FALLS, not about what the words are:

    "registrationThe"        -> "registration The"       both -> registrationthe   OK
    "a<br>b" from "ab"       -> inserted <br>            both -> ab                OK
    one row split into two   -> inserted </tr><tr>       stream unchanged          OK
    cell boundary moved      -> moved </td><td>          stream unchanged          OK
    a word paraphrased       -> stream differs                                     REJECTED
    a row dropped            -> stream differs                                     REJECTED

A word-level check cannot express this: splitting "registrationThe" into two words
changes the word multiset by construction, so a multiset gate would reject the fixes
this stage exists to make. The character stream is both stricter (it catches a single
altered letter) and more permissive in the only dimension that matters (whitespace and
tag placement). A model physically cannot smuggle a paraphrase past it.
"""

from __future__ import annotations

import collections
import concurrent.futures
import contextvars
import html as _html
import json
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# The one cap, imported rather than repeated: a limit written down twice is two limits.
# Guarded, and every other obs import in this file is lazy for the same reason: the package
# not importing is a case this pipeline survives (run_corpus carries a whole no-op shim for
# it), and a module-level import made that survivable case fatal for all of stage 4.
try:
    from ..obs.log import MAX_STR as _LOG_MAX
except Exception:                                            # noqa: BLE001
    _LOG_MAX = 1_000

# THE DEFAULT `log` FOR THIS STAGE, and not `print`.
#
# Stage 4 fans sections over a thread pool, so the human-readable lines and the structured
# events are written to one stdout by several threads at once. `print` is two unlocked
# writes (text, then newline), so a JSON event can land INSIDE one -- gluing itself onto the
# tail of a text line, where Filebeat can no longer parse it as JSON and the event reaches
# Elasticsearch with no `event.action` to find it by. write_line takes the same lock `event`
# does, which is what makes the two writers safe together. Guarded like _LOG_MAX above: obs
# failing to import is a case this pipeline survives.
try:
    from ..obs.log import write_line as _say
except Exception:                                            # noqa: BLE001
    _say = print

# ============================================================================
#  THE PROMPT — this is the bit to edit
# ----------------------------------------------------------------------------
#  These two strings are everything the model is told. The rest of this file is
#  plumbing around them. Nothing needs rebuilding: the next run reads whatever
#  is here.
#
#  repair_section() assembles the request as:
#      [page image] x N   (every page the section's tables came from, in order)
#      SECTION_INSTR
#      the section's whole markdown
#
#  Earned the hard way, and worth reading before adding a rule: instructions
#  that describe a SHAPE — how many columns a table has, what a row looks like —
#  fix one section and break another, because these tables are not all the same
#  shape. A two-column worked example fixed section 3's misplaced cell and
#  doubled section 5's rows in the same run; a general sentence saying the same
#  thing did nothing at all. State the goal and the prohibitions; leave the
#  shape to the page images.
# ============================================================================

SYSTEM_GROUNDTRUTH = "You correct the structure of an HTML table that was extracted from a PDF."

SECTION_INSTR = """The images above are the pages of one section of a legal PDF, in order.
They are the ground truth.

I need to repair the things like boldeness, page breakss, line breaks, indentations, bullets points which are merged togather, and other OCR errors in the HTML table extracted from those pages.

Below is the markdown extracted from those pages. Its tables are damaged: one table is
often broken into several, columns are inconsistent, rows are split or run together, some
words are fused where a line wrapped, and some line breaks are lost.

Dont rebuild it, just fix the structures of it so it matches the pages again. A section is normally ONE continuous table — if the
markdown has it in pieces, join them.

Every piece of text stays in the column it came from. Never append one cell's text onto a
cell in a different column. so if you merge a split table, the first column's text stays in the first column, the second column's text stays in the second column, and so on. Check every join against the page images first, and be clear about what may be joined: a table the extractor broke into pieces, and a cell the extractor broke across a page break. Two cells sitting side by side on the same row are never joined.

AN EMPTY CELL IS CONTENT. Where a row has text in one column and nothing beside it, look
at the page image again to be sure — and where the page really is blank there, the
extractor got it right. Leave that row exactly as it came: the same number of <td>s, the
empty one still empty. Do not delete an empty cell, do not collapse its row into a single
full-width cell, and do not add colspan to the cell next to it. No cell becomes a spanning
cell that was not already one, and no cell that already spans is taken apart.

THE COLUMN TITLES SIT OVER THE COLUMNS THEY NAME. The "Questions | Answers" row is the
table's own heading, and the extractor often leaves it too narrow: "Questions" ends up
covering the number column alone, so "Answers" slides left and stands over the question
text with nothing above the answers at all. The number and the question it belongs to are
BOTH under Questions — whether the rows beneath split them across two cells or keep them
in one — and only the answer is under Answers. Where that row is out of line, widen the
"Questions" cell with a colspan so it covers every column its questions occupy. That row
is the ONE place a cell may become a spanning cell; everywhere else the rule above holds.


Where a row is a numbered sub-section label for example like (6.1, 6.2, 7.1, 7.3, 8.11), copy it out of the
table, write it as a markdown heading on its own line — ### 6.2 Funds — and start a new
table beneath it. Do this for EVERY one of them, with no exceptions.

A label row carries the number and a short title and nothing else — the number may sit
alone in its own narrow cell, with the title in the cell beside it, or the two may share
one cell. A number written inside a sentence is a CROSS-REFERENCE, not a label ("See
answer in 6.2(b) above", "see section 8.3 below"): leave it in its cell, untouched, and
never promote it to a heading.

The label rows of one section all share that section's own number, and they run in order
without a gap — 6.1, 6.2, 6.3. That is your check. Before you reply, run through the ones
you have promoted: if your lowest is 6.2, or you have 6.1 and 6.3 but no 6.2, look at the
page images again for the one you passed over. Do not write the check out, just fix it.
The one most often missed is the FIRST — it is the top row of the first table, so there is
nothing above it to split off, and a bare "6.1" in a narrow cell reads like data rather
than a label. It gets its own ### heading like all the others.

A label row bearing a DIFFERENT section's number is not yours to promote; it is spillover,
and the rule further down applies to it instead.

Keep the tables as HTML — <table>, <tr>, <td>, exactly the markup style the input uses.
Do NOT convert them into markdown pipe tables.

Leave a URL as plain text, exactly the characters the cell has, in the cell it sits in. Do
not wrap it in <a>, do not shorten it, do not add or drop a "www.", and do not repair a
URL that the page broke across a line. The links are made afterwards, from the characters
you leave behind — a URL you retype lands inside an attribute, where none of the checks
on this output can see that it is now dead.

Reproduce the "*Source: `...`, page A–B*" line exactly as given, in the same place. It is
not prose and not part of any table — it records which PDF pages this section came from,
and the review tool uses it to open the right page. A section without it cannot be
navigated. The same applies to every "**⚙ MinerU-extracted table**" line: keep them.

A table cell can CONTINUE ACROSS A PAGE BREAK. When a question or an answer runs off the
bottom of one page and carries on at the top of the next, that is ONE cell, not two rows
and not two sections. The extractor often breaks it there, so the tail of the sentence
ends up looking like a stray row or like the start of the next section.

Read the RULING to decide, not the reading. A row continues only where the page leaves it
open: the sentence breaks off part-way at the foot of one page and picks up part-way at
the top of the next. Where the page closes the row with a rule and opens a fresh ruled row
at the top of the next page, that row is FINISHED — what follows is a new row, however
much it reads as the continuation of the question above it.

A NEW SECTION begins only at its own numbered heading (for example "7. PRIVATE PLACEMENT
REGIME"). Everything printed ABOVE that heading, even on the same page, still belongs to
the previous section. The last page of a section and the first page of the next are
frequently the same sheet of paper, so use the heading, not the page, to decide.

The same rule cuts the other way: where the NEXT section's own heading appears BELOW your
last row on a shared page, stop there. Two short sections routinely share one page, and
that page can show your section's last table AND the next section's complete, legible
heading and content both at once. A next section that reads perfectly well in the image
is still not yours to include -- everything from its heading onward belongs in a
different file, however complete and well-formed it looks. Your reply ends at your own
section's last row.

FRONT MATTER STOPS AT THE NEXT SECTION, WHATEVER FORM IT TAKES. Front matter is
unnumbered -- if this section is the document's front matter, the page images showing
ANY of the following ends your content right there: a table of contents beginning
partway through ("PART B: CONTENTS", "TABLE OF CONTENTS", or similar), OR a numbered
heading such as "1. BACKGROUND" or "1 Introduction". Both share the page with front
matter routinely. Reply with the front matter text only -- never adopt a numbered
section's own heading as yours, and never reply with that section's content, however
completely the page shows it.

Where a cell has clearly been TRUNCATED — it ends mid-sentence, or a question ends without
the "If so, please..." part that the page shows — read the missing words off the page image
and append them to that same cell, exactly as printed. Copy them character for character;
do not paraphrase, re-word, or write anything the page does not show. Only ever add text
that is visibly printed on one of the pages above.

A ROW IS NEVER FOLDED INTO ANOTHER ROW. Every row the page rules stays its own <tr>, with
its own cells beside it. Where a question is followed by its own sub-parts — (i), (ii),
(iii), or (a), (b), or bullets — and the page rules each of them as a row in its own right
with its own answer alongside, those are ROWS, not the tail of the question that
introduces them. Keep every one of them as a row. Never pull them up into the cell of the
question above, and never run their answers together into one answer cell: the answer
printed beside (iii) belongs beside (iii) and nowhere else. A lead-in line — "please
ensure that your analysis covers the following:" — introduces the rows beneath it; it does
not absorb them, and it keeps its own row too, empty cell beside it and all.

That rule separates ROWS from each other. It never separates a marker from its own words:
"(i)" and "criminal sanctions;" are one row, together, with their answer beside them — and
that holds whether the page prints the "(i)" in a narrow cell of its own or in the same
cell as the words. A row carrying a marker and nothing else says nothing, and a row of
words whose marker has been taken away cannot be read. Splitting one is never the repair.

A SUB-ITEM'S OWN LABEL STAYS IN WHATEVER COLUMN THE PAGE ACTUALLY PRINTS IT IN. A row's
leftmost column holds only THAT row's own main-level label -- (a), (b), (c) -- exactly
where the page shows one, and stays genuinely empty on every row underneath it; never fill
it with a roman numeral or sub-letter just because the column has room. Where the page
nests a sub-item's own numbering -- (i), (ii) -- inside the SAME cell as its parent's
question text, rather than ruling it a fresh cell of its own, keep it there, wrapped in
its own <blockquote> one level deeper than its parent -- <blockquote> works the same way
inside a table cell as anywhere else, and is the same nesting a definitions entry uses.
And where the page prints a parent's intro sentence and its first sub-item
sharing ONE ruled row with ONE answer cell, reproduce that as one row -- do not invent a
second row that splits them apart with a blank answer cell the page never shows.

Never delete a row and never merge two rows into one, except when removing spillover from
another section as described below.

Clean up section boundaries by removing out-of-scope content from document chunks.

Chunks often contain "spillover" content from adjacent sections. For example, a chunk designated for Section 3 might accidentally include the end of Section 2's tables at the top, or the title and beginning of Section 4 at the bottom.

Instructions:

Identify extraneous content: Review the target chunk and locate any text, titles, or tables that belong to neighboring sections.

Verify against ground truth: You must cross-reference the text with the original source images to confirm the exact visual boundaries of the target section.

Clean the chunk: Delete any foreign content so that the final output contains only the information belonging to the target section.

A FOOTNOTE AT THE FOOT OF A PAGE IS NOT SPILLOVER. A page sometimes prints a small numbered
note at the bottom -- "1  The affiliation procedure is described...", "2  The registration
procedure is described..." -- explaining a superscript number attached to a word inside a
cell above it ("...FinSA;1", "...Beraterregister).2"). That note belongs to the table it
annotates, not to whichever section happens to print next. Do not delete it as if it were
spillover. Keep the marker number exactly as printed and keep the footnote text with it,
appended after the table's closing </table> tag, in the order the page prints them, before
the next section's own heading begins. This is what keeps the footnote attached to its own
table rather than orphaned as trailing text a sub-chunk split later drops. The way to tell a
footnote from real spillover: its number matches a superscript reference inside THIS
table, and it is short -- a sentence or two, not another section's heading or rows.

Reply with the whole section: its heading, its *Source:* line, its ⚙ badge lines, its
### sub-headings, and its <table> blocks. Nothing else."""


# A section with no <table> in it at all -- plain prose, as the extractor saw it. Same
# ground truth, same word-preservation discipline as SECTION_INSTR. Most of it stays
# paragraphs: line-level OCR damage, spillover, bold/underline/indentation restored from
# the page images. The one exception is a definitions/interpretation list the page prints
# as a genuine two-column table that the extractor missed entirely -- that IS a table
# shape to repair, in the one place this pass is allowed to introduce <table> markup.
TEXT_SECTION_INSTR = """The images above are the pages of one section of a legal PDF, in
order. They are the ground truth. This section is plain prose -- it has no tables.

Below is the markdown extracted from those pages. Repair OCR damage: two words fused
together where a line wrapped ("registrationThe" -> "registration The"), a missing space
after punctuation ("t.Marketing" -> "t. Marketing"), and a bullet, numbered item, or
paragraph break the extractor lost so two lines run together that the page prints apart.

THE PAGE IMAGES ARE ALSO THE GROUND TRUTH FOR FORMATTING -- bold, underline, indentation,
and table structure the extractor routinely drops. A DEFINITIONS or INTERPRETATION
section is where this matters most: reproduce exactly what the page shows for every
entry, not only the ones that already look wrong.

- BOLD. Wrap a bold word or phrase -- most often the defined term itself -- in
  **like this**. Strip ** from anything the page does NOT show as bold: match the page,
  not the extractor's guess.

- ITALIC. Wrap an italicised word or phrase in *like this* -- common for a block of
  wording reproduced verbatim for the reader to quote, a document or defined-term title,
  or a foreign-language term. The extractor drops italics as readily as it drops bold and
  underline; strip * from anything the page does NOT show in italics, the same as the
  BOLD rule.

- UNDERLINE. Wrap an underlined word or phrase in <u>like this</u>. Bold and underline are
  SEPARATE, independent observations about the same word -- most defined terms are bold
  and NOT underlined, so never underline a term just because it is bold, or just because
  it is a defined term at all. If you cannot point to a specific reason a term is
  underlined, leave it bold-only; blanket-underlining every entry is the single most
  common way this goes wrong.

  The one specific reason that DOES apply: a line reading "the underlined words in the
  above [Executive Summary / section] have the following meanings", followed by a
  glossary, names exactly which words are underlined. For those named terms only, find
  EVERY occurrence anywhere in the prose above that line -- not just the first, not just
  where it is defined -- and wrap each one, even where the underline stroke itself is
  faint. This licence is scoped to exactly those terms; it says nothing about any other
  list in the section (a 1.3-style definitions list gets no underline from this rule
  unless the page independently shows one).

- A DEFINITIONS LIST THAT IS ACTUALLY A TABLE ON THE PAGE -- ONLY where THIS section IS the
  document's own numbered Definitions/Interpretation section, the one whose OWN heading
  reads "Definitions", "Interpretation", or the like (for example "1.3 Definitions"). A
  "DEFINED TERMS:" label is NOT a heading of that kind -- it is a sub-label some OTHER
  section (an Executive Summary, a guidance section, any section that is not itself the
  document's Definitions section) prints partway through its own prose, introducing a
  glossary scoped to that section alone. Leave a "DEFINED TERMS:" list exactly as prose,
  indented per the rule below, never as a table -- however identical its two-column
  term/meaning layout looks to the real thing. Only the section whose OWN heading says
  Definitions gets table treatment; the giveaway there is the page itself: the defined
  term in one column, its meaning in a second, ruled or aligned like any other table here.
  Reproduce that as a real table, one row per term:

      <table><tr><td><strong>Fund</strong></td><td>A collective investment undertaking...</td></tr>
      ...</table>

  Bold inside a cell is <strong>, not ** -- markdown is not re-read inside a <table>, so
  ** there would render as two literal asterisks. Use <em> for italics the same way.
  Outside a table, ** is correct. A multi-line entry or lettered sub-clauses -- (a), (b),
  (c) -- stay in the SAME cell as their term, separated by <br>; never give a sub-clause
  its own row. Only build a table where the page truly shows two columns; a section that
  is genuinely one bold word per paragraph stays as paragraphs.

  A cell's own content can be NESTED just as deep as any outline elsewhere -- a bullet
  under the term, its own sub-bullets, a lettered or roman-numeral list one level deeper
  still. Keep every level: wrap each nested level in its own <blockquote>...</blockquote>,
  one level of <blockquote> nesting per level of depth the page shows at that point --
  <blockquote> works the same way inside a <table> cell as anywhere else -- so a
  third-level roman numeral sits inside three nested <blockquote>s and reads visibly
  deeper than the bullet above it. Never flatten a nested entry into one indent level for
  everything.

- INDENTATION ELSEWHERE. Outside a definitions table, the extractor commonly flattens an
  ENTIRE outline to the left margin, not just one isolated list: a lettered top-level
  label ("a) Client Categories"), its own intro sentence one level deeper ("Under FinSA,
  a distinction must be made between"), and a further lettered list one level deeper
  again ("a. retail clients...", "b. professional clients..."). Treat this as the shape
  of the WHOLE section where it applies, not a single list to spot in passing -- every
  paragraph and list in it gets nested according to where it actually sits under its own
  parent. Reproduce every level with a markdown blockquote, one ">" per level of nesting
  -- "> " for the first level, ">> " for the second, ">>> " for a third, and so on as deep
  as the page actually goes. An intro sentence sitting between a top-level label and its
  own lettered list keeps its OWN middle depth; do not collapse it to either neighbour's
  level. Match the DEPTH the page shows at each point -- never one uniform level for
  everything, never an invented one.

- LINE BREAKS WITHIN AN ENTRY. Most entries run the term and definition together with no
  break ("Fund" means ...), but the page sometimes breaks them onto separate lines.
  Decide per entry, from its own page image -- never one rule for all of them.

- ORDER. Where the extractor printed a definition BEFORE its term, swap the two back into
  the page's own order: term, then definition. This reorders only the two pieces of ONE
  entry -- never move an entry's position in the list, merge two entries, or drop or
  duplicate anything while doing it.

You may ADD **, *...*, <u>...</u>, "> "/">> ", or
<table>/<tr>/<td>/<br>/<strong>/<em>/<blockquote> ONLY to reproduce formatting the page
actually shows -- doing so is not a word-preservation violation, because it touches no
word. Nothing else may be added: NEVER add, remove,
reword, paraphrase or reorder a word; NEVER change capitalisation, spelling, punctuation
or quotation marks (curly quotes stay curly); NEVER invent a bullet, number, or heading
the page does not show. If a line looks wrong but the page does not clearly show what it
should be, leave it alone.

Reproduce the "*Source: `...`, page A–B*" line exactly, in the same place. Keep every
markdown heading exactly as it is, with one exception: where the extractor built the
heading from only the SECOND line of a two-line page title and left the FIRST line -- a
bare label like "APPENDIX 2" -- stranded on its own right after the *Source:* line, fold
it into the heading instead of leaving it orphaned: put the label where the page itself
puts it, leading the title, replacing the heading's bare number with it since the label
already carries that same number -- "# 2 Disclaimers: Closed-Ended Fund" becomes
"# Appendix 2 Disclaimers: Closed-Ended Fund" -- and delete the orphaned line. Do this
only for that exact orphaned-label shape. A label mentioned mid-sentence later ("see
Appendix 1.A above") is a cross-reference, not a stray title, and is never touched.

A SECTION IS BOUNDED BY HEADINGS ON BOTH SIDES -- its own, and the next one's -- not by
the page. A numbered heading ("7. PRIVATE PLACEMENT REGIME") and an unnumbered label that
names a section just as definitively ("APPENDIX 4") count equally as hard boundaries.
Two short sections routinely share one page, so a single ground-truth image can show
your last paragraph AND the whole of the next section, heading and all -- complete,
legible, reading perfectly well on its own. That completeness is not licence to include
it: everything above your own heading belongs to the PREVIOUS file, and everything from
the NEXT heading onward belongs to a DIFFERENT one, however well-formed either looks.
Verify boundaries against the page images and remove only content proven to belong
elsewhere; never remove anything that is genuinely yours. Your reply holds exactly one
numbered section heading -- your own, at the top -- and ends at your own last paragraph.

FRONT MATTER STOPS AT THE NEXT SECTION, WHATEVER FORM IT TAKES. Front matter is
unnumbered -- if this section is the document's front matter, the page images showing
ANY of the following ends your content right there: a table of contents beginning
partway through ("PART B: CONTENTS", "TABLE OF CONTENTS", or similar), OR a numbered
heading such as "1. BACKGROUND" or "1 Introduction". Both share the page with front
matter routinely. Reply with the front matter text only -- never adopt a numbered
section's own heading as yours, and never reply with that section's content, however
completely the page shows it.

Reply with the whole section: its heading, its *Source:* line, and its corrected prose --
including any <table> you produced for a definitions list. Nothing else."""


# ============================================================================
#  End of prompts. Plumbing below.
# ============================================================================


# ---------------------------------------------------------------- content stream

_TAG = re.compile(r"<[^>]+>")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WS = re.compile(r"\s+")


def content_stream(html: str) -> str:
    """The invariant: tags gone, entities resolved, only lowercase alphanumerics kept.

    Everything the model is ALLOWED to touch — whitespace, <br>, cell and row boundaries,
    entity spelling (&amp; vs &) — vanishes here. Everything it is FORBIDDEN to touch
    survives. Comparing two streams is therefore the whole safety argument.
    """
    # Tags first, then entities: unescaping first would turn a literal "&lt;td&gt;" in
    # the text into something _TAG would then eat as markup.
    text = _TAG.sub(" ", html or "")
    text = _html.unescape(text)
    return _NON_ALNUM.sub("", text.lower())


def _cell_streams(html: str) -> list[str]:
    """Per-cell streams, reading order — used only to describe HOW a batch changed."""
    return [content_stream(m.group(1)) for m in re.finditer(r"<t[dh][^>]*>(.*?)</t[dh]>",
                                                            html or "", re.DOTALL | re.IGNORECASE)]


VERDICT_CLEAN = "clean"          # stream identical, nothing changed at all
VERDICT_SPACING = "spacing"      # stream identical, only whitespace/<br> moved
VERDICT_STRUCTURAL = "structural"  # stream identical, cell/row boundaries moved too
VERDICT_REJECT = "reject"        # stream differs — content was mutated
VERDICT_UNGATED = "ungated"      # gate off: applied anyway, and what it WOULD have said


def strict_stream(html: str) -> str:
    """The gate proper: tags and whitespace removed, EVERYTHING else kept byte-exact.

    `content_stream` above answers "are these the same words". This answers the stronger
    question the user actually asked — "is this the same text" — and the difference is
    not academic. A first pilot run against the real document showed the model happily:

        (“IFA”)     -> ("IFA")        smart quotes normalised
        <br>        -> <br>•\t        a bullet glyph and tab invented from the page image
        Commission  -> <strong>Commission</strong>

    Every one of those leaves the alphanumeric stream untouched, so an alnum-only gate
    waves them through. In a legal document a rewritten quotation mark is a content
    change. Keeping punctuation, symbols and case in the compared stream costs nothing —
    no legitimate respacing repair touches any of them — and closes the hole.
    """
    return _WS.sub("", _html.unescape(_TAG.sub(" ", html or "")))


# The only markup the model is permitted to INTRODUCE. Anything else it emits must have
# been in the input already, or the reply is discarded: repairing a table means moving a
# boundary, never inventing emphasis, colspans or list markup.
_STRUCTURAL_TAGS = {"tr", "td", "th", "br"}
_TAG_NAME = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")


def markup_signature(html: str) -> set[tuple[str, str]]:
    """{(tag name, attribute name)} present in the markup; ("td", "") for a bare cell."""
    sig = set()
    for m in _TAG_NAME.finditer(html or ""):
        name = m.group(1).lower()
        attrs = re.findall(r"([a-zA-Z-]+)\s*=", m.group(2) or "")
        sig.add((name, ""))
        sig.update((name, a.lower()) for a in attrs)
    return sig


_SPAN_ATTR = re.compile(r"<t[dh]\b[^>]*\b(?:col|row)span\s*=", re.I)


def _span_count(html: str) -> int:
    return len(_SPAN_ATTR.findall(html or ""))


def _empty_cells(html: str) -> int:
    return sum(1 for c in _cell_streams(html) if not c)


def _cell_counts(html: str) -> list[int]:
    return [len(_cell_streams(row)) for row in split_rows(html)]


_COLSPAN = re.compile(r"<t[dh]\b[^>]*\bcolspan\s*=\s*[\"\']?(\d+)", re.I)
_DECLARED_COLS = re.compile(r"(\d+)\s*(?:rows?\s*[x×]\s*)?(\d+)\s*cols?", re.I)


def _row_width(row: str) -> int:
    """A row's width in COLUMNS, counting a colspan cell as the columns it covers."""
    spans = [int(n) for n in _COLSPAN.findall(row)]
    return len(_cell_streams(row)) - len(spans) + sum(spans)


def table_width(table_html: str, badge: str = "") -> int:
    """How many columns the table really has.

    NOT the modal cell count, which was the first thing tried and is actively wrong: in
    table_006 twenty-one rows have two cells and only twelve have three, because the
    merged-label defect is the MAJORITY case. Taking the mode there measures the damage
    and then enshrines it — every correct 3-cell row looks like the anomaly, and the
    repair that widens a broken row gets rejected for exceeding "the table width".

    Stage 2 already counted the columns properly and printed the answer in the badge
    ("33 rows × 3 cols"), so that is used when present. Failing that, the widest row wins,
    counting a colspan cell as the columns it spans — damage removes cells, it does not
    add them, so the maximum is the honest estimate and the mode is not.
    """
    m = _DECLARED_COLS.search(badge or "")
    if m:
        return int(m.group(2))
    rows = split_rows(table_html)
    return max((_row_width(r) for r in rows), default=0)


def verdict(before: str, after: str, width: int | None = None,
            structural_gate: bool = True) -> tuple[str, str]:
    """Classify an edit. Four independent checks, all of which must pass to accept.

    Only accept/reject matters for safety; the spacing-vs-structural split is reporting
    detail, so a reviewer can skim the boundary-moving edits (which deserve a look)
    apart from the pure spacing ones (which cannot go wrong).
    """
    # 1. Text identity, punctuation and case included.
    sb, sa = strict_stream(before), strict_stream(after)
    if sb != sa:
        # Report the drift over the alnum stream when the words themselves changed, and
        # over the strict stream when only punctuation moved — the distinction is the
        # first thing a reviewer wants to know.
        cb, ca = content_stream(before), content_stream(after)
        kind = "content" if cb != ca else "punctuation/case"
        return VERDICT_REJECT, f"{kind}: " + _describe_drift(*( (cb, ca) if cb != ca else (sb, sa) ))

    # 2. No invented markup.
    introduced = markup_signature(after) - markup_signature(before)
    illegal = {t for t in introduced if t[0] not in _STRUCTURAL_TAGS or t[1]}
    if illegal:
        return VERDICT_REJECT, f"introduced markup: {sorted(illegal)}"

    # 2b. …and none REMOVED either, for span attributes specifically.
    #
    # This rule was missing and it cost real damage: told "this table has 7 columns,
    # every row must have 7 cells", the model dutifully rewrote the full-width banner
    # row <td colspan="7">What we are seeking to draw out…</td> as one text cell plus
    # SIX EMPTY ONES. The content stream is identical, the row still covers 7 columns,
    # and every other check passed — but the row now renders as a narrow squeezed
    # column instead of a banner. 29 of the document's 36 span attributes were
    # destroyed this way. A colspan row is already correct; nothing may take it apart.
    if structural_gate and _span_count(after) < _span_count(before):
        return VERDICT_REJECT, (f"span attribute removed "
                                f"({_span_count(before)} -> {_span_count(after)})")

    # 2c. No padding. Splitting a merged label REDISTRIBUTES existing text, which is the
    #     repair we want; adding empty cells to make a row "the right length" is not a
    #     repair at all, and is how a spanning row gets flattened.
    if structural_gate and _empty_cells(after) > _empty_cells(before):
        return VERDICT_REJECT, (f"empty cells added "
                                f"({_empty_cells(before)} -> {_empty_cells(after)})")

    # 3. Row widths. Splitting a merged label ADDS cells; nothing may ever remove one,
    #    and no row may end up covering more columns than the table has.
    #
    #    An earlier version allowed only cell counts already present in the batch, which
    #    was too rigid in a way that mattered: splitting "6.2 Funds" into "6.2 | Funds"
    #    produces a 2-cell row in a 3-column table, and 2 appeared nowhere in the input,
    #    so the correct repair was rejected. With padding now forbidden (2c) a short row
    #    staying short IS the right answer, so the bound is simply the table width.
    if width and any(_row_width(r) > width for r in split_rows(after)):
        return VERDICT_REJECT, f"row wider than the table ({width} columns)"
    if structural_gate and sum(_cell_counts(after)) < sum(_cell_counts(before)):
        return VERDICT_REJECT, (f"cells merged: {sum(_cell_counts(before))} -> "
                                f"{sum(_cell_counts(after))}")

    # 4. Rows may be added (a merged row split apart) but never lost.
    if len(split_rows(after)) < len(split_rows(before)):
        return VERDICT_REJECT, "row count fell"

    if before == after:
        return VERDICT_CLEAN, "no change"
    if _cell_streams(before) != _cell_streams(after):
        return VERDICT_STRUCTURAL, "cell/row boundaries moved"
    return VERDICT_SPACING, "whitespace/<br> only"


def _describe_drift(sb: str, sa: str) -> str:
    """Where the two streams first diverge, with a little context each side. Makes a
    rejection diagnosable from the report alone instead of needing a re-run."""
    i = 0
    while i < min(len(sb), len(sa)) and sb[i] == sa[i]:
        i += 1
    return (f"diverges at char {i}: "
            f"expected …{sb[max(0, i - 25):i + 25]!r}, got …{sa[max(0, i - 25):i + 25]!r} "
            f"(len {len(sb)} -> {len(sa)})")


# ---------------------------------------------------------------- reprojection

_TOKEN = re.compile(r"(<[^>]+>)|(&[#0-9a-zA-Z]+;)|(\s+)|(.)", re.DOTALL)


def _units(html: str) -> list[tuple[str, str]]:
    """Content units of a fragment: (rendered character, exact source text).

    An entity is ONE unit — `&amp;` renders as "&" but must be written back as "&amp;",
    so the rendered form is what we compare and the source form is what we emit.
    Whitespace and tags are not units; they are exactly what the model may move.
    """
    out = []
    for m in _TOKEN.finditer(html or ""):
        tag, ent, ws, ch = m.groups()
        if tag or ws:
            continue
        if ent:
            out.append((_html.unescape(ent), ent))
        else:
            out.append((ch, ch))
    return out


def reproject(before: str, after: str) -> str | None:
    """Rebuild the model's answer out of the ORIGINAL characters. None if it cannot be.

    The model is good at deciding WHERE a space or a <br> belongs and unreliable about
    reproducing text verbatim — the pilot showed it silently normalising (“IFA”) to
    ("IFA") in a document where the quotation marks are part of a legal definition.
    Rather than trust its text and check it afterwards, we keep only its layout: walk
    its output, pass tags and whitespace through untouched, and for every content unit
    emit the original document's character instead of the model's.

    The result cannot differ from the input by a single character of content — that is
    true by construction, not by inspection — while still gaining the model's spacing
    and line-break judgement. Reprojection is refused outright if the model's units do
    not line up one-for-one with the original's, or if any mismatch touches an
    alphanumeric character, since that means it did more than re-punctuate and its
    layout can no longer be trusted to align.
    """
    ub, ua = _units(before), _units(after)
    if len(ub) != len(ua):
        return None
    for (cb, _), (ca, _) in zip(ub, ua):
        if cb != ca and (cb.isalnum() or ca.isalnum()):
            return None

    src = iter(ub)
    parts: list[str] = []
    for m in _TOKEN.finditer(after or ""):
        tag, ent, ws, ch = m.groups()
        if tag or ws:
            parts.append(tag or ws)
            continue
        parts.append(next(src)[1])
    return "".join(parts)


# ---------------------------------------------------------------- row splitting

_ROW = re.compile(r"<tr\b.*?</tr>", re.DOTALL | re.IGNORECASE)


def split_rows(table_html: str) -> list[str]:
    return _ROW.findall(table_html or "")


@dataclass
class Batch:
    """Consecutive rows from ONE page, sent to the model together with that page image."""
    table_id: str
    page: int | None
    start: int              # index of the first row within the table
    rows: list[str]
    header: str | None = None   # the table's first row, resent as column context

    @property
    def html(self) -> str:
        return "\n".join(self.rows)


def plan_batches(rows: list[str], pages: list[int | None], table_id: str,
                 max_rows: int = 6, max_chars: int = 9000,
                 group_by_page: bool = False) -> list[Batch]:
    """Group consecutive rows into batches bounded by row count and size.

    Page boundaries used to force a split, because each request carried that page's
    rendered image and a 19-page table cannot ship 19 images per call. With image
    grounding gone the constraint goes with it: batches are now bounded only by size,
    which means fewer, larger calls. The page is still recorded per batch — the review
    report is far easier to check against the PDF with it than without.
    """
    header = rows[0] if rows else None
    batches: list[Batch] = []
    cur: list[str] = []
    cur_page: int | None = None
    cur_start = 0

    def flush():
        if cur:
            batches.append(Batch(table_id, cur_page, cur_start, list(cur),
                                 header if cur_start > 0 else None))

    for i, (row, page) in enumerate(zip(rows, pages)):
        too_big = cur and (len(cur) >= max_rows
                           or sum(len(r) for r in cur) + len(row) > max_chars)
        if cur and ((group_by_page and page != cur_page) or too_big):
            flush()
            cur, cur_start = [], i
        if not cur:
            cur_start, cur_page = i, page
        cur.append(row)
    flush()
    return batches


# ---------------------------------------------------------------- page attribution

def page_streams(pdf_path: Path, pages: list[int]) -> dict[int, str]:
    """{1-based page number: content stream of that page's text layer}."""
    import fitz

    out: dict[int, str] = {}
    with fitz.open(str(pdf_path)) as doc:
        for p in pages:
            if 1 <= p <= doc.page_count:
                out[p] = content_stream(doc[p - 1].get_text())
    return out


def attribute_rows(rows: list[str], candidates: list[int],
                   streams: dict[int, str], probe: int = 40) -> list[int | None]:
    """Which page each row came from, by locating the row's text in the page text layer.

    Deliberately independent of MinerU's internals: the final rows are the product of
    stitching and continuation-merging, so mapping them back through MinerU's per-page
    blocks would mean re-deriving that logic. Searching the text layer asks the simpler
    question — where do these words physically appear in the PDF? A row that cannot be
    located (heavily mangled, or pure punctuation) inherits the previous row's page,
    which is right far more often than not: rows arrive in reading order.
    """
    found: list[int | None] = []
    last: int | None = candidates[0] if candidates else None
    for row in rows:
        s = content_stream(row)
        hit = None
        if len(s) >= 12:
            # Probe from a few offsets: a row whose opening words MinerU mangled may
            # still match further in, and a long row's tail can land on the next page.
            for off in (0, len(s) // 3, 2 * len(s) // 3):
                frag = s[off:off + probe]
                if len(frag) < 12:
                    continue
                for p in candidates:
                    if frag in streams.get(p, ""):
                        hit = p
                        break
                if hit is not None:
                    break
        if hit is None:
            hit = last
        else:
            last = hit
        found.append(hit)
    return found


# ---------------------------------------------------------------- the model

SYSTEM = """You repair OCR damage inside one HTML table fragment from a legal document.

Fix ONLY these three things:
1. Two words run together because a line break was dropped: "registrationThe" -> "registration The".
2. A missing space after punctuation: "t.Marketing" -> "t. Marketing".
3. A row where the extractor merged a LABEL into the next cell. If the table has 3
   columns and a row reads
       <tr><td>(g) Do the Arrangements apply?</td><td>N/A</td></tr>
   the label belongs in its own cell:
       <tr><td>(g)</td><td>Do the Arrangements apply?</td><td>N/A</td></tr>
   Split off ONLY the leading label — "(a)", "(iv)", "6.2". Never merge two cells.

   This is the ONLY reason to change a row's cells, and it only ever REDISTRIBUTES
   text that is already there.

   A row that uses colspan is ALREADY CORRECT — leave it exactly as it is:
       <tr><td colspan="7">What we are seeking to draw out in this section…</td></tr>
   That single cell spans all 7 columns on purpose; it is a full-width banner, not a
   short row. Do NOT rewrite it as 7 cells, do NOT remove the colspan, do NOT add
   empty cells beside it. The same goes for rowspan.

You may also insert <br> where a hard line break was clearly lost, such as before a new
list item or a new numbered point. Judge that from the text itself.

ABSOLUTE RULES — a violation makes your whole answer worthless and it will be discarded:
- NEVER add, remove or change a word. Not one character of any word.
- NEVER change a punctuation character. Curly quotes stay curly: (“IFA”) must come back
  as (“IFA”), NOT ("IFA"). Dashes, apostrophes and brackets stay exactly as given.
- NEVER change capitalisation, spelling or grammar, even when clearly wrong.
- NEVER add a character that is not already in the HTML. In particular: do NOT add
  bullet glyphs (• - *), tabs, or numbering of your own.
- NEVER reorder content. Cells and rows stay in the order given.
- NEVER add a tag other than <br>, <tr>, <td>. No <strong>, <em>, <p>, <ul>, and no
  attributes of any kind — no colspan, no rowspan.
- NEVER delete a row, and never merge two rows into one.
- Keep every HTML entity as it is (&amp; stays &amp;, &#x27; stays &#x27;).
- If a fragment looks wrong but the text does not clearly show what it should be, LEAVE
  IT ALONE. Returning the input unchanged is always acceptable.

Your output must differ from the input ONLY by spaces, <br> tags, and cell boundaries.

Reply with the corrected <tr>...</tr> rows and NOTHING else."""


# Any language tag, not just ```html: the sub-section pass asks for JSON and Haiku
# duly answers in a ```json fence, which an html-only pattern left behind as a
# stray "json" line — the parse then failed and the split silently found nothing.
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*|\s*```\s*$", re.IGNORECASE)


def _clean_reply(text: str) -> str:
    """Model output -> bare rows. Strips a stray markdown fence and any chatter that
    landed outside the rows; if no <tr> survives, returns "" so the caller rejects."""
    text = _FENCE.sub("", text or "").strip()
    rows = split_rows(text)
    return "\n".join(rows)


# HOW MANY ATTEMPTS THE CALL IN FLIGHT HAS COST. botocore retries inside converse(), so a
# call that finally raises may have been three round trips -- and from the outside a slow
# first attempt and three throttled ones look identical. Thread-local because botocore is
# synchronous: the retry handler runs on the thread that made the call, and stage 4 makes
# several at once.
_attempts = threading.local()

# WHAT THIS THREAD'S CALL IN FLIGHT HAS PUT ON THE WIRE.
#
# Filled by the before-send hook in bedrock_client, read back by _call_fields, so a call's
# own end event can say how many bytes it actually shipped and when they left. Thread-local
# for the same reason _attempts is: botocore is synchronous, the hook runs on the thread
# that made the call, and stage 4 makes several at once.
_wire = threading.local()


def _wire_reset() -> None:
    """Start of a call: forget the previous one's bytes on this thread.

    Without this a call whose request never reached the wire -- no credentials, a signing
    failure, a connection refused before send -- would report the PREVIOUS call's size as
    its own, which is worse than reporting nothing: a payload figure that is confidently
    wrong is one nobody re-checks."""
    _wire.bytes = 0
    _wire.requests = 0
    _wire.sent_at = None
    _wire.last_sent_at = None


class _UploadLedger:
    """Every byte this process has handed to Bedrock, and how many requests carried them.

    THE COUNT IS OF REQUESTS, NOT OF CALLS, and the difference is the point. botocore
    retries inside converse(), and a retried request re-uploads the entire payload -- 15
    page images and all. A run that looks like 87 calls can be 130 uploads, and the gap
    between those two numbers is what a throttled region costs in bandwidth. Nothing else
    in the pipeline counts it, because nothing else sees the individual attempt.

    Locked rather than relying on the GIL: `+=` on an int is read-modify-write, and with
    six section workers the lost updates are not theoretical."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.bytes = 0
        self.requests = 0

    def add(self, n: int) -> tuple[int, int]:
        with self._lock:
            self.bytes += n
            self.requests += 1
            return self.bytes, self.requests

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.bytes, self.requests


_uploads = _UploadLedger()


def _mb(n: int | float | None) -> float | None:
    """Bytes as MB, to two places. MB (10^6), not MiB -- it is the unit AWS bills and
    quotes payload limits in, so the number here is comparable to theirs without a
    conversion nobody remembers to do."""
    return None if n is None else round(float(n) / 1_000_000, 2)


def _aws_error(exc: BaseException) -> dict:
    """The AWS error CODE, not just the Python class.

    Every modelled Bedrock failure arrives as one class -- ClientError -- so `error.type`
    reads "ClientError" for a throttle, a model timeout, an access denial and a validation
    error alike. That is the difference between "we are being rate limited, slow down" and
    "this role cannot call Bedrock at all", and it was not in the index: only the retry hook
    dug the code out, and only for attempts that were retried rather than for the failure
    that ended the call.

    The socket-level ones (ReadTimeoutError, ConnectTimeoutError) carry no response and no
    code; their class name is already the answer, so they simply return nothing here and
    `error.type` stands on its own.

    The REQUEST ID is the other half. It is the handle AWS support and CloudWatch start
    from, and it was logged on success and dropped on failure -- backwards, since a
    successful call is the one nobody needs to look up.

    Total by construction. This runs INSIDE the handler that is reporting a failure, so
    anything it raises replaces a useful Bedrock error with an unrelated one and loses the
    original: `Error` arriving as a string, a non-numeric HTTPStatusCode and a metadata block
    that is not a dict each did exactly that before this was guarded. A missing field is a
    field missing from one record; an exception here is the whole reason gone."""
    out: dict = {}
    try:
        resp = getattr(exc, "response", None)
        if not isinstance(resp, dict):
            return out
        err = resp.get("Error")
        if isinstance(err, dict):
            code, msg = err.get("Code"), err.get("Message")
            if code:
                out["aci.aws_error_code"] = str(code)
            if msg:
                out["aci.aws_error_message"] = str(msg)
        meta = resp.get("ResponseMetadata")
        if isinstance(meta, dict):
            req = meta.get("RequestId")
            if req:
                out["aci.aws_request_id"] = str(req)
            # The HTTP status separates a 429 (throttle, back off) from a 5xx (Bedrock's
            # problem) from a 4xx (ours), without anyone memorising which code is which.
            try:
                status = meta.get("HTTPStatusCode")
                if status is not None:
                    out["aci.http_status"] = int(status)
            except (TypeError, ValueError):
                pass
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _call_fields(t0: float) -> dict:
    """The fields every bedrock.call.end carries, win or lose.

    started_at as a FIELD and not only as the start event's timestamp: one document then
    answers "when did this begin, how long did it take, how many attempts" without joining
    two records in Kibana.

    THE UPLOAD FIGURES, and what they can and cannot say. `aci.wire_bytes` is the real
    serialized request body, summed over every attempt this call made, so a call retried
    twice honestly reports three payloads' worth of upload. `aci.prep_s` is the time from
    entering the call to the first request leaving the client -- rendering page images,
    base64-encoding them, signing -- which is the only part of the wall clock that is
    ours rather than Bedrock's.

    There is deliberately NO upload-rate field. boto3 sends the body in one blocking write
    and hands back a parsed response; the only interval anything can measure spans the
    upload, the model's thinking and the download together, so bytes divided by it is not a
    throughput and publishing it as one would invite exactly the wrong conclusion about a
    slow call. Size and duration sit side by side here instead, unmixed."""
    n = int(getattr(_wire, "bytes", 0) or 0)
    sent_at = getattr(_wire, "sent_at", None)
    last_sent = getattr(_wire, "last_sent_at", None)
    return {"aci.started_at": round(t0, 3),
            "aci.retry_count": int(getattr(_attempts, "n", 0) or 0),
            "event.duration_s": round(time.time() - t0, 2),
            # None, not 0, when nothing reached the wire -- a call that died in signing
            # uploaded nothing, and "0 bytes" reads as a payload that was measured.
            "aci.wire_bytes": n or None,
            "aci.wire_mb": _mb(n) or None,
            "aci.wire_requests": int(getattr(_wire, "requests", 0) or 0) or None,
            "aci.prep_s": round(sent_at - t0, 2) if sent_at else None,
            # The answer to "is the wait genuinely on Bedrock": the interval between the
            # last request leaving this client and the response landing. On a call with
            # no retries prep_s + wait_s is essentially the whole duration; where it is
            # not, the remainder is time spent inside botocore's back-off between
            # attempts, which is neither ours nor a server delay.
            "aci.wait_s": round(time.time() - last_sent, 2) if last_sent else None}


def bedrock_client(region: str | None = None, profile: str | None = None):
    import boto3
    from botocore.config import Config

    from ..config import settings
    from ..obs import log as _log

    # Region comes from settings (ACI_BEDROCK_REGION), not a literal default. Both
    # call sites in this module call bedrock_client() with no arguments, so a literal
    # here WAS the region for every stage 4 run -- it read eu-west-1 while config said
    # eu-west-2, and nothing reconciled the two.
    region = region or settings.bedrock_region
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    # 120s was not enough and it cost a table: table_010 is 97 rows over 19 page images,
    # and grounded mode sends the whole thing in ONE call, so a single request can run for
    # minutes. It timed out, fell back to stage 3, and the failure looked like a model
    # problem rather than a client setting. Batched mode's short calls never hit this.
    # NO tcp_keepalive HERE, deliberately. It looks like the answer to "was the connection
    # still alive" and is not: botocore's tcp_keepalive=True sets SO_KEEPALIVE and nothing
    # else, so the timers come from the OS, where the first probe is sent after
    # net.ipv4.tcp_keepalive_time -- 7200 seconds by default. Every failure it would need to
    # catch happens long before that: a line dying mid-call is found by read_timeout at 900s,
    # and a pooled connection killed by a NAT or LB idle timeout (~350s) is reused dead while
    # the OS still believes it is fine. Turning it on would buy nothing and read like cover.
    # It is worth having only alongside tuned sysctls in the pod (keepalive_time down to
    # ~60s, intvl ~10, probes ~3), which is a kubelet allowlist change, not a code change.
    client = session.client(
        "bedrock-runtime", region_name=region,
        config=Config(retries={"max_attempts": 3, "mode": "adaptive"},
                      connect_timeout=30, read_timeout=900))

    # WHY A RETRY HOOK AND NOT JUST CALL TIMING
    #
    # 900s read_timeout x 3 adaptive attempts is still a legitimate 45-minute silence on ONE
    # call, and adaptive mode sleeps between attempts without saying so. From outside the
    # process that is indistinguishable from a hang — it is the leading candidate for what
    # Canada and New Zealand were doing on the MRAM run, and nothing recorded it either way.
    # Dropped from 6 attempts (a 90-minute worst case) once bedrock.call.start/end below made
    # that silence observable instead of just shorter — six was headroom for a failure mode
    # nothing could see; three is enough to ride out a single throttle without doubling the
    # cost of one that is genuinely stuck.
    #
    # `needs-retry` fires once per attempt with the response or the exception that caused it,
    # so each throttle, timeout and 5xx becomes its own line. Returning None is botocore's
    # "no opinion": the handler observes and never changes what the retry policy decides.
    def _on_retry(response=None, attempts=None, caught_exception=None, **_kw):
        try:
            status, code, msg, req_id = None, None, None, None
            if response and isinstance(response, tuple):
                if response[0] is not None:
                    status = getattr(response[0], "status_code", None)
                # An HTTP-level retry carries NO exception object — botocore hands back the
                # parsed response instead. Without digging the AWS error code out of it,
                # `error.type` would be empty on exactly the retries that matter most
                # (ThrottlingException is a 429, not a raised exception), and one field
                # would mean "the error" on some records and nothing on others.
                if len(response) > 1 and isinstance(response[1], dict):
                    err = response[1].get("Error") or {}
                    code, msg = err.get("Code"), err.get("Message")
                    req_id = (response[1].get("ResponseMetadata") or {}).get("RequestId")
            if status in (None, 200) and caught_exception is None:
                return None                  # the successful attempt, not a retry
            # Read back by _call_fields, so the call's OWN record says what it cost.
            _attempts.n = int(attempts or 0)
            _log.event("bedrock.retry", level="warn",
                       message=f"attempt {attempts}: {code or status or 'retry'}",
                       **{"aci.attempt": attempts, "aci.http_status": status,
                          "aci.region": region,
                          # THE HANDLE AWS ITSELF USES. Without it, "Bedrock timed out on
                          # us" is an unprovable claim: a support case or a CloudWatch
                          # lookup starts from this id and nothing else in the record
                          # identifies the individual request.
                          "aci.aws_request_id": req_id,
                          # ONE field for "what went wrong", whichever way it arrived.
                          "error.type": (type(caught_exception).__name__
                                         if caught_exception else code),
                          "error.message": (str(caught_exception)[:_log.MAX_STR]
                                            if caught_exception else
                                            (str(msg)[:_log.MAX_STR] if msg else None))})
        except Exception:                                    # noqa: BLE001
            pass
        return None

    # WHAT ACTUALLY WENT UP THE WIRE, AND WHEN.
    #
    # Stage 4 sends page images: a grounded section is every one of its pages as a 150 dpi
    # PNG, and JSON has no way to carry bytes, so Converse takes them base64-encoded --
    # roughly 4/3 of the file on disk. Nothing in the call site can know the real figure.
    # The images are counted before the call, the text is counted before the call, and the
    # number that matters -- what this pod pushed at Bedrock -- exists only after botocore
    # has serialized, encoded and signed the request. `before-send` is the last event
    # before the socket write and is handed the prepared request, so it is the one place
    # the true size can be read.
    #
    # PER ATTEMPT, which is the whole reason this belongs on the client and not at the call
    # site. botocore's retries happen inside converse(); a throttled 15 MB section is
    # uploaded again in full, and from the call site's side that is invisible. Three
    # bedrock.request.sent events under one bedrock.call.start is what 45 MB spent on one
    # section looks like.
    #
    # It also covers the call sites that send no images at all -- table repair, sub-chunk
    # boundaries -- for free, because it is a property of the client rather than of any
    # one caller. A hook is the only shape that does not need every future call site to
    # remember to measure itself.
    def _on_before_send(request=None, **_kw):
        try:
            body = getattr(request, "body", None)
            if isinstance(body, str):
                n = len(body.encode("utf-8", "ignore"))
            elif isinstance(body, (bytes, bytearray, memoryview)):
                n = len(body)
            else:
                # A streaming or file-like body has no length without consuming it, and
                # consuming it here would send an empty request. Measuring nothing is the
                # only safe answer; Converse does not use one today.
                return None
            total, count = _uploads.add(n)
            _wire.bytes = int(getattr(_wire, "bytes", 0) or 0) + n
            _wire.requests = int(getattr(_wire, "requests", 0) or 0) + 1
            if getattr(_wire, "sent_at", None) is None:
                _wire.sent_at = time.time()                  # FIRST attempt's departure
            # The LATEST attempt's departure, which is what the outstanding wait is
            # measured from. On a retried call these two differ by however long the
            # failed attempts and the adaptive back-off took.
            _wire.last_sent_at = time.time()
            # Onto the in-flight registry, so the heartbeat can say how many megabytes are
            # sitting open across the pod while nothing else is being logged at all. This
            # is the "how much is uploading right now" half: the call's own end event
            # cannot answer it, because it does not exist until the call is over.
            #
            # AND THE PHASE FLIP, which is the fact this event exists to record. Up to
            # here the time is OURS -- rendering page images, base64-encoding them,
            # signing. From here it is Bedrock's. Without the flip, a heartbeat saying
            # "call open 20 minutes" cannot distinguish a pod that is still building a
            # 40 MB request from one that handed it over nineteen minutes ago and has
            # heard nothing, and those two have nothing in common but the number.
            _now = time.time()
            _log.note(**{"aci.api_bytes": int(getattr(_wire, "bytes", 0) or 0),
                         "aci.api_phase": "sent",
                         "aci.api_wire_bytes": n,
                         # Reset on a retry: the previous attempt's wait is over, and the
                         # question is always how long the request CURRENTLY outstanding
                         # has been unanswered.
                         "_sent_at": _now})
            _log.event("bedrock.request.sent",
                       # Deliberately worded as a handoff, not as a delivery. This fires
                       # at the last point before the HTTP layer writes the body, which
                       # is as close to "it has gone" as botocore exposes -- there is no
                       # event between the socket write completing and the response
                       # arriving. For a payload this size the write is seconds and the
                       # wait that follows is minutes, so the marker is worth having; it
                       # just must not be read as an acknowledgement from AWS.
                       message=f"request {count} handed to the socket: {_mb(n)} MB "
                               f"— now waiting on Bedrock "
                               f"({_mb(total)} MB uploaded this process)",
                       **{"aci.wire_bytes": n, "aci.wire_mb": _mb(n),
                          "aci.region": region,
                          "aci.api_phase": "sent",
                          # Attempt 1 is the first send; the retry hook has not fired yet
                          # when it does, so _attempts.n is still 0 there.
                          "aci.attempt": int(getattr(_attempts, "n", 0) or 0) + 1,
                          "aci.call_wire_bytes": int(getattr(_wire, "bytes", 0) or 0),
                          # THE RUNNING TOTAL. One field, on every request, so "how much
                          # has this pod uploaded so far" is a single Kibana column and
                          # not a sum over records that may have been dropped.
                          "aci.upload_total_bytes": total,
                          "aci.upload_total_mb": _mb(total),
                          "aci.upload_total_requests": count})
        except Exception:                                    # noqa: BLE001
            pass
        # None is botocore's "no opinion": returning anything else here REPLACES the
        # response and the request is never sent at all.
        return None

    try:
        client.meta.events.register("needs-retry.bedrock-runtime.*", _on_retry)
        client.meta.events.register("before-send.bedrock-runtime.*", _on_before_send)
    except Exception:                                        # noqa: BLE001
        pass                                                 # never fail a run over a hook
    return client


def call_model(client, model_id: str, batch: Batch, note: str | None = None,
               width: int | None = None, system: str | None = None) -> tuple[str, dict]:
    """One repair attempt. `note` carries the previous attempt's rejection reason back
    to the model, which is the only thing that makes a retry more than a re-roll."""
    parts: list[dict] = []
    if width and (system or SYSTEM) is SYSTEM:
        parts.append({"text": f"This table has {width} columns. A row whose cells cover "
                              f"FEWER than {width} columns may have a merged label to "
                              f"split out. A row using colspan already covers all "
                              f"{width} — leave it untouched."})
    if batch.header:
        parts.append({"text": f"Column header row, for context only — do NOT return it:\n{batch.header}"})
    if note:
        parts.append({"text": f"Your previous attempt was DISCARDED because it changed the "
                              f"content: {note}\nReturn the SAME words, only respaced."})
    parts.append({"text": f"Repair these rows:\n{batch.html}"})

    from ..obs import log as _log

    _t0 = time.time()
    _attempts.n = 0                                          # this call's own attempt count
    _wire_reset()                                            # and its own upload figures
    _log.event("bedrock.call.start",
               message=f"table repair via {model_id}",
               **{"aci.model": model_id, "aci.purpose": "table_repair",
                  "aci.rows": len(batch.rows) if getattr(batch, "rows", None) else None,
                  "aci.retry_note": bool(note)})
    # SO THE HEARTBEAT CAN SEE IT. start/end bracket the call; between them a 900s converse
    # says nothing, and "no events for 12 minutes" reads the same whether the call is
    # running, the connection is dead, or the pod is wedged. Registered here, every beat
    # carries the open call and its age -- and, with several running at once, the OLDEST of
    # them, which is the one worth looking at.
    try:
        with _log.api_call("bedrock.converse", **{"aci.api_model": model_id,
                                                  "aci.api_purpose": "table_repair"}):
            resp = client.converse(
                modelId=model_id,
                system=[{"text": system or SYSTEM}],
                messages=[{"role": "user", "content": parts}],
                inferenceConfig=inference_config(model_id, 8192),
            )
    except BaseException as _e:
        _aws = _aws_error(_e)
        _log.exception("bedrock.call.end", _e,
                       # The AWS CODE in the message, not just the Python class: a throttle
                       # and an access denial are both ClientError, and reading one as the
                       # other sends someone to the wrong fix.
                       message=(f"{model_id} failed: "
                                f"{_aws.get('aci.aws_error_code') or type(_e).__name__}"),
                       **{"aci.model": model_id, "aci.purpose": "table_repair",
                          **_call_fields(_t0), **_aws})
        raise
    u = resp.get("usage") or {}
    _log.event("bedrock.call.end",
               message=f"{model_id} ok in {round(time.time() - _t0, 1)}s",
               **{"aci.model": model_id, "aci.purpose": "table_repair",
                  "event.outcome": "success", **_call_fields(_t0),
                  "aci.tokens_in": int(u.get("inputTokens") or 0),
                  "aci.tokens_out": int(u.get("outputTokens") or 0),
                  "aci.aws_request_id": (resp.get("ResponseMetadata") or {}).get("RequestId"),
                  "aci.stop_reason": resp.get("stopReason")})
    return response_text(resp), {
        "model": model_id,
        "in": int(u.get("inputTokens") or 0),
        "out": int(u.get("outputTokens") or 0),
    }


HAIKU = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "eu.anthropic.claude-sonnet-4-6"
# Cross-region inference profile, verified against Converse in eu-west-1. The bare
# "anthropic.claude-sonnet-5" is rejected there (on-demand is not offered for it) and the
# "-v1:0" suffix does not exist for this model, so the eu. prefix is the only form.
SONNET5 = "eu.anthropic.claude-sonnet-5"

# Sonnet 5 REJECTS `temperature` — Converse fails outright with "ValidationException: The
# model returned the following errors: `temperature` is deprecated for this model", and the
# stage-4 pass then keeps stage 3 for every section. That is not a soft failure: on
# 2026-09-08 a cluster run reported gate=pass, 178s and $0.00 with 0 of 8 sections
# processed, because the extraction gate measures stages 1-3 and nothing downstream reads
# stage 4's rejection count.
#
# It is not sent to ANY model, rather than being suppressed for Sonnet 5 alone. The
# parameter is optional everywhere it is still accepted, and one config that every model
# takes cannot drift out of step with the model list the way a per-model exemption can.
#
# The cost of this is real and is accepted deliberately: omitting temperature is NOT the
# same as sending 0. Each model applies its own default, which is not deterministic, so a
# repair may come back differently on a retry. The character-exact reprojection gate is
# what makes that safe — a re-roll that changes content is rejected rather than kept — so
# the exposure is extra rejections and tokens, never altered text.
def response_text(resp: dict) -> str:
    """The assistant's text from a Converse response — NOT necessarily the first block.

    A reasoning model returns its thinking alongside the answer, so Sonnet 5 replies with
    `[reasoningContent, text]` and `content[0]["text"]` raises KeyError. It does that only
    when the task warrants thinking, so the failure is by PROMPT, not by model: a one-line
    probe returns a single text block and looks fine, while the real stage-4 prompt does
    not. That is why it survived the temperature fix — every section failed with
    `KeyError: 'text'`, each was caught per-section and reported "kept as stage 3", and the
    run still finished gate=pass having done nothing.
    """
    blocks = ((resp.get("output") or {}).get("message") or {}).get("content") or []
    for b in blocks:
        if "text" in b:
            return b["text"]
    raise RuntimeError(
        f"no text block in the Converse response (blocks: {[sorted(b) for b in blocks]}, "
        f"stopReason={resp.get('stopReason')!r})")


def inference_config(model_id: str, max_tokens: int) -> dict:
    """Converse's inferenceConfig — the ONE place a model's limits are applied."""
    return {"maxTokens": max_tokens}


# USD per 1M tokens, per the MODELS table in llm/agent.py. Image tokens arrive inside
# inputTokens, so the page grounding is priced automatically — no separate image term.
#
# SONNET5's rate is CARRIED OVER from Sonnet 4.6 and is NOT confirmed: the AWS pricing
# API is denied to the bedrock-dev1-invoke-only role, so it could not be read from the
# authoritative source. Every dollar figure this module reports for Sonnet 5 -- the run
# log, stage4_report.json, the Stage 4 tab, the ledger -- is therefore an estimate at that
# rate. Confirm it against the Bedrock pricing page and correct this line.
PRICES = {HAIKU: (1.0, 5.0), SONNET: (3.0, 15.0), SONNET5: (3.0, 15.0)}

# Short names for the models kept above, so the common choice does not mean pasting a
# Bedrock inference-profile id. NOT a whitelist: anything not listed here is passed to
# Bedrock verbatim, which is how a new model is used without touching this file.
MODEL_ALIASES = {
    "haiku": HAIKU, "haiku4.5": HAIKU, "haiku-4-5": HAIKU,
    "sonnet": SONNET, "sonnet4.6": SONNET, "sonnet-4-6": SONNET,
    "sonnet5": SONNET5, "sonnet-5": SONNET5,
}


def resolve_model(name: str | None = None) -> str:
    """A model name from a human or an env var -> the id to send to Bedrock.

    Three ways in, in order:

      "sonnet5"                       -> a short alias, resolved from MODEL_ALIASES
      "eu.anthropic.claude-opus-4-6"  -> anything else, passed through UNCHANGED
      None / ""                       -> whatever ACI_STAGE4_AI_MODEL says

    Pass-through is the point: a model released next month is selected by setting an env
    var, not by editing this file. The cost of that is that a TYPO is indistinguishable
    from a new model id, so "sonet5" is sent to Bedrock and the call fails there rather
    than here. That is the better failure: it is loud and it happens before any work,
    whereas silently correcting a typo to a default would bill a run on a model nobody
    chose -- which is exactly how earlier comparison runs came to be billed on Haiku while
    being read as Sonnet.
    """
    if not name:
        try:
            from aosphere_core_index.config import settings
            name = settings.stage4_ai_model
        except Exception:                                    # noqa: BLE001
            return SONNET5
    key = str(name or "").strip()
    if not key:
        # An empty setting is not a model id. Reached when ACI_STAGE4_AI_MODEL is set to
        # the empty string, which otherwise resolved to "" and was sent to Bedrock.
        return SONNET5
    return MODEL_ALIASES.get(key.lower(), key)


def resolve_text_model(name: str | None = None) -> str:
    """Same resolution as resolve_model, for the TEXT-section pass specifically.

    A text-only section is plain prose reproduced close to verbatim, not a table whose
    column structure breaks under a weak model -- the failure mode resolve_model's default
    was raised against. Haiku is the default here deliberately: cheaper, and the text pass
    does not need Sonnet's structural judgement. Reads ACI_STAGE4_TEXT_AI_MODEL when `name`
    is not given, independent of ACI_STAGE4_AI_MODEL, so a host can run one model on tables
    and a different, cheaper one on text without the two settings fighting over one name.
    """
    if not name:
        try:
            from aosphere_core_index.config import settings
            name = settings.stage4_text_ai_model
        except Exception:                                    # noqa: BLE001
            return HAIKU
    key = str(name or "").strip()
    if not key:
        return HAIKU
    return MODEL_ALIASES.get(key.lower(), key)


# Models billed at a rate this table does not know. cost_usd cannot raise -- it is summed
# in the middle of a paid run -- but a $0.0000 on a run that spent real money is worse than
# a wrong number, so the models are recorded and the report carries them.
UNPRICED: set[str] = set()


def _configured_price() -> tuple[float, float] | None:
    """The rate from ACI_STAGE4_AI_PRICE_IN / _OUT, USD per 1M tokens, or None.

    Exists so a model this file has never heard of can still be costed -- and so a rate
    that turns out to be wrong (SONNET5's is carried over and unconfirmed) can be corrected
    without a code change. Set BOTH or neither; one alone is ignored rather than half
    applied, because a run priced at $0 for output would read as almost free.
    """
    try:
        from aosphere_core_index.config import settings
        pin, pout = settings.stage4_ai_price_in, settings.stage4_ai_price_out
    except Exception:                                        # noqa: BLE001
        return None
    if pin is None or pout is None:
        return None
    try:
        return float(pin), float(pout)
    except (TypeError, ValueError):
        return None


def cost_usd(model_id: str, tok_in: int, tok_out: int) -> float:
    """USD for one call. The configured rate WINS over the built-in table.

    That order is deliberate: the reason to set a rate by hand is that the built-in one is
    absent or wrong, and an override that lost to a stale hard-coded number would be
    useless for the second case.
    """
    rate = _configured_price() or PRICES.get(model_id)
    if rate is None:
        UNPRICED.add(model_id)
        rate = (0.0, 0.0)
    return (tok_in * rate[0] + tok_out * rate[1]) / 1_000_000


@dataclass
class BatchResult:
    batch: Batch
    html: str                    # what we are keeping (original when rejected)
    verdict: str
    reason: str = ""
    model: str = ""
    attempts: int = 0
    changed: bool = False
    reprojected: bool = False    # the model's text was re-derived from the original
    rejected: list[dict] = field(default_factory=list)
    # EVERY attempt is billed, including the ones the gate threw away, so usage
    # accumulates across the retry ladder rather than recording only the winner.
    usage: list[dict] = field(default_factory=list)


def repair_batch(client, batch: Batch, width: int | None = None,
                 models=None, system: str | None = None,
                 structural_gate: bool = True, content_gate: bool = True) -> BatchResult:
    """Try each model in turn; keep the first reply that passes the gate.

    `models=None` means one attempt with the configured model. The old default was a
    hard-coded ladder (haiku, haiku, sonnet), which every real caller already overrode with
    a single model -- so the ladder was dead, and keeping it as a default meant a caller
    who passed nothing silently got Haiku no matter what ACI_STAGE4_AI_MODEL said.

    A rejection is never applied and never fatal — the batch simply keeps its stage-3
    rows. The escalation exists because the two failure modes differ: Haiku retried with
    the reason usually fixes an over-eager edit, and Sonnet catches the genuinely hard
    pages, but the document is correct either way.
    """
    res = BatchResult(batch=batch, html=batch.html, verdict=VERDICT_CLEAN)
    note = None
    models = tuple(models) if models else (resolve_model(),)
    for i, model in enumerate(models, 1):
        try:
            raw, used = call_model(client, model, batch, note, width, system)
            res.usage.append(used)
            reply = _clean_reply(raw)
        except Exception as e:  # noqa: BLE001 — a Bedrock failure must not lose the table
            res.rejected.append({"model": model,
                                 "error": f"{type(e).__name__}: {e}"[:_LOG_MAX]})
            continue
        res.attempts = i
        if not reply:
            note = "you returned no <tr> rows at all"
            res.rejected.append({"model": model, "reason": note})
            continue
        # Prefer the model's LAYOUT over its TEXT: rebuild the answer from the original
        # characters where that is possible, so a re-punctuated reply is healed instead
        # of thrown away. When it is not possible the raw reply still faces the gate,
        # which rejects it with a reason worth reporting.
        if content_gate:
            healed = reproject(batch.html, reply)
            if healed is not None:
                res.reprojected = healed != reply
                reply = healed
        v, why = verdict(batch.html, reply, width, structural_gate)
        if not content_gate:
            # Full freedom: whatever the model returns is applied, including a rewrite
            # of the text itself. The verdict is still COMPUTED and recorded, so the
            # report says exactly what changed and what the gate would have refused —
            # the check is gone, the visibility is not.
            res.html, res.model = reply, model
            res.verdict = VERDICT_UNGATED if v == VERDICT_REJECT else v
            res.reason = why
            res.changed = reply != batch.html
            return res
        if v == VERDICT_REJECT:
            note = why
            res.rejected.append({"model": model, "reason": why})
            continue
        res.html, res.verdict, res.reason, res.model = reply, v, why, model
        res.changed = v in (VERDICT_SPACING, VERDICT_STRUCTURAL)
        return res
    res.verdict = VERDICT_REJECT
    res.reason = note or "every attempt failed"
    res.model = ""
    return res


# ---------------------------------------------------------------- stage assembly

_TABLE_BLOCK = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
# The badge stage 3 writes immediately above each spliced table, e.g.
#   **⚙ MinerU-extracted table** — table_003, pages 13–14, 6 rows × 2 cols
_BADGE = re.compile(r"MinerU-extracted table\*\*\s*—\s*(table_\d+)", re.IGNORECASE)


def _tables_in(text: str) -> list[tuple[str | None, re.Match]]:
    """Each <table> block in a stage-3 markdown file, paired with the table_id from the
    badge above it (None when a table carries no badge)."""
    out = []
    for m in _TABLE_BLOCK.finditer(text):
        badges = _BADGE.findall(text[:m.start()])
        out.append((badges[-1] if badges else None, m))
    return out


def _progress(job_dir: Path, status: str, **detail) -> None:
    """Heartbeat in the same file the other stages write, so the UI picks stage 4 up
    without a server change. Mirrors hybrid_extract.write_progress and, like it, never
    raises — a broken heartbeat must not fail the run."""
    import time
    try:
        path = Path(job_dir) / "progress.json"
        try:
            cur = json.loads(path.read_text())
        except (OSError, ValueError):
            cur = {"done": []}
        cur.setdefault("done", [])
        if status == "done" and "stage4_ai" not in cur["done"]:
            cur["done"].append("stage4_ai")
        stages = list(cur.get("stages") or [])
        if "stage4_ai" not in stages:
            stages.append("stage4_ai")
        cur.update({"stage": "stage4_ai", "status": status, "at": time.time(),
                    "stages": stages, "detail": detail or cur.get("detail") or {}})
        path.write_text(json.dumps(cur, indent=1))
    except Exception:  # noqa: BLE001
        pass


def run_stage4(stage3_dir, stage4_dir, pdf_path, only: list[str] | None = None,
               client=None, models=None, text_model: str | None = None, log=_say,
               subchunk: bool = False, repair: bool = True,
               system: str | None = None, structural_gate: bool = True,
               content_gate: bool = True, mode: str = "section",
               subchunk_after: bool = True, force: bool = False) -> dict:
    """Copy stage 3, repair the tables in `only` (filename prefixes), report.

    Non-destructive by construction: stage 3 is copied, never edited, so the tree can be
    diffed against its input and the whole stage discarded by deleting one directory.

    Two modes. `grounded` (the default) sends each table to Haiku 4.5 in ONE call with
    that table's rendered pages attached, and is what the measurements settled on — on
    chunk 07 it produced uniform 3-column rows and its own 6.1/6.2/6.3 split for $0.076,
    against $0.287 and a worse result for batched Sonnet with no images. `batched` is the
    earlier row-batched path with the reprojection gate, kept because it is the only mode
    that can promise character-exact output and is still the right tool where that
    matters more than structure.
    """
    # The deployment gate, checked before anything is read or any request is made.
    # Stage 4 is the only stage that calls a paid API and the only one that can alter a
    # document's text, so a host opts in rather than inheriting it. The gate sits on the
    # pass ITSELF, not just the CLI flag, so a programmatic caller — a server, a
    # scheduled job — cannot spend money where it was never enabled.
    #
    # Imported here rather than at module scope so importing this module never depends
    # on the settings environment: the conservation checks and the tests import it freely.
    if not force:
        from aosphere_core_index.config import settings

        if not settings.stage4_ai_enabled:
            log("stage 4: DISABLED (set ACI_STAGE4_AI_ENABLED=1 to enable, or pass "
                "force=True for a deliberate local run)")
            _record_timings(Path(stage4_dir).parent, stage4_ai=0.0, stage5_subchunk=0.0)
            return {"ran": False, "disabled": True, "mode": mode, "seconds": 0.0,
                    "model": None, "reason": "stage4_ai_enabled is false",
                    "sections": [], "sections_total": 0, "sections_accepted": 0,
                    "sections_kept_stage3": 0, "headings_added": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                              "cost_usd": 0.0, "by_model": {}}}

    models = tuple(models) if models else (resolve_model(),)
    if mode not in ("section", "batched"):
        # Silently falling through ran the WRONG implementation: the CLI still asked for
        # "grounded" after that mode was deleted, so --stage4-ai quietly used the old
        # row-batched path instead of section mode — different prompt, different gates,
        # different report keys, and no error to notice it by.
        raise ValueError(f"unknown stage 4 mode {mode!r}; expected 'section' or 'batched'")

    if mode == "section":
        return run_stage4_section(stage3_dir, stage4_dir, pdf_path, only=only,
                                  client=client,
                                  model=models[0] if models else resolve_model(),
                                  text_model=text_model or resolve_text_model(),
                                  log=log, subchunk_after=subchunk_after)
    stage3_dir, stage4_dir = Path(stage3_dir), Path(stage4_dir)
    job_dir = stage4_dir.parent
    _progress(job_dir, "running")
    if stage4_dir.exists():
        shutil.rmtree(stage4_dir)
    shutil.copytree(stage3_dir, stage4_dir)

    manifest_path = stage3_dir / "tables_manifest.json"
    pages_by_id: dict[str, list[int]] = {}
    if manifest_path.exists():
        for t in json.loads(manifest_path.read_text()).get("tables", []):
            pages_by_id[t["table_id"]] = t.get("pages") or []

    if client is None:
        client = bedrock_client()

    results: list[BatchResult] = []
    per_file: list[dict] = []
    edits: list[dict] = []
    splits: list[dict] = []
    dup_headings: list[dict] = []
    extra_usage: list[dict] = []

    targets = sorted(p for p in stage4_dir.glob("*.md")
                     if only is None or any(p.name.startswith(pre) for pre in only))

    for md in targets:
        text = md.read_text()
        tables = _tables_in(text)
        if not tables:
            continue
        file_stats = {"file": md.name, "tables": 0, "batches": 0, "changed": 0, "rejected": 0}
        emitted: set[str] = set()      # sub-sections this file re-emitted with content
        labels: dict[str, str] = {}    # their heading text, for the duplicate check
        # Right to left: rewriting from the end keeps every earlier match's offsets valid.
        for table_id, m in reversed(tables):
            pages = pages_by_id.get(table_id or "", [])
            rows = split_rows(m.group(0))
            if not rows:
                continue
            file_stats["tables"] += 1
            streams = page_streams(Path(pdf_path), pages) if pages else {}
            attributed = attribute_rows(rows, pages, streams) if pages else [None] * len(rows)
            batches = plan_batches(rows, attributed, table_id or md.stem)
            badge_line = text[:m.start()].rstrip().rsplit("\n", 1)[-1]
            width = table_width(m.group(0), badge_line)
            log(f"  {md.name} {table_id}: {len(rows)} rows, {width} cols, "
                f"{len(batches)} batches over pages {pages[:1]}–{pages[-1:]}")

            new_rows: list[str] = []
            if repair:
                for b in batches:
                    r = repair_batch(client, b, width, models=models, system=system,
                                     structural_gate=structural_gate,
                                     content_gate=content_gate)
                    results.append(r)
                    file_stats["batches"] += 1
                    if r.verdict == VERDICT_REJECT:
                        file_stats["rejected"] += 1
                    elif r.changed:
                        file_stats["changed"] += 1
                        edits.append({"file": md.name, "table": table_id, "page": b.page,
                                      "row": b.start, "verdict": r.verdict, "model": r.model,
                                      "before": b.html, "after": r.html})
                    new_rows.append(r.html)
            else:
                new_rows = rows

            rebuilt = _rebuild_table(m.group(0), rows, new_rows)

            # Sub-chunking runs on the REPAIRED table: the column fix turns "6.2 Funds"
            # from a merged 1-cell row into "6.2 | Funds", which is what makes the label
            # recognisable in the first place.
            if subchunk:
                bounds, u = find_subsections(client, rebuilt, models=models[:1])
                extra_usage.extend(u)
                if bounds:
                    candidate = split_table(rebuilt, bounds)
                    ok, why = verify_split(rebuilt, candidate, bounds)
                    splits.append({"file": md.name, "table": table_id, "ok": ok,
                                   "reason": why,
                                   "sections": [f"{b['id']} {b['title']}" for b in bounds]})
                    if ok:
                        # Stage 1 usually already wrote a heading for the FIRST
                        # sub-section, right above this table. Reuse it rather than
                        # emitting a second one saying the same thing.
                        candidate, reused = drop_duplicate_lead_heading(
                            candidate, preceding_heading(text[:m.start()]))
                        rebuilt = candidate
                        file_stats["subsections"] = len(bounds)
                        splits[-1]["reused_existing_heading"] = reused
                        emitted.update(b["id"] for b in bounds)
                        labels.update({b["id"]: f"{b['id']} {b['title']}".strip()
                                       for b in bounds})
                    else:
                        log(f"    split REFUSED for {table_id}: {why}")

            text = text[:m.start()] + rebuilt + text[m.end():]
        if emitted:
            text, gone = remove_empty_shells(text, emitted, labels)
            if gone:
                file_stats["shells_removed"] = gone

        # A sub-section promoted out of the table may already exist as a stage-1 heading
        # elsewhere in the file — usually an empty shell sitting AFTER the table it should
        # have introduced. Reported, never silently deleted: those shells sometimes carry
        # orphan prose, and deciding where that prose belongs is not this stage's job.
        seen: dict[str, list[int]] = {}
        for i, line in enumerate(text.splitlines(), 1):
            hm = re.match(r"^#{2,4}\s+(\d+\.\d+)\b", line)
            if hm:
                seen.setdefault(hm.group(1), []).append(i)
        for sec, lines in seen.items():
            if len(lines) > 1:
                dup_headings.append({"file": md.name, "section": sec, "lines": lines})

        md.write_text(text)
        per_file.append(file_stats)

    all_usage = [u for r in results for u in r.usage] + extra_usage
    tok_in = sum(u["in"] for u in all_usage)
    tok_out = sum(u["out"] for u in all_usage)
    by_model: dict[str, dict] = {}
    if True:
        for u in all_usage:
            d = by_model.setdefault(u["model"], {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
            d["calls"] += 1
            d["in"] += u["in"]
            d["out"] += u["out"]
            d["usd"] += cost_usd(u["model"], u["in"], u["out"])

    report = {
        "files": per_file,
        "batches_total": len(results),
        "batches_changed": sum(1 for r in results if r.changed),
        "batches_unchanged": sum(1 for r in results if r.verdict == VERDICT_CLEAN),
        "batches_rejected": sum(1 for r in results if r.verdict == VERDICT_REJECT),
        "spacing_fixes": sum(1 for r in results if r.verdict == VERDICT_SPACING),
        "batches_reprojected": sum(1 for r in results if r.reprojected),
        "batches_ungated": sum(1 for r in results if r.verdict == VERDICT_UNGATED),
        "ungated": [{"table": r.batch.table_id, "row": r.batch.start,
                     "reason": r.reason} for r in results if r.verdict == VERDICT_UNGATED],
        "structural_fixes": sum(1 for r in results if r.verdict == VERDICT_STRUCTURAL),
        "rejections": [{"table": r.batch.table_id, "row": r.batch.start,
                        "page": r.batch.page, "reason": r.reason,
                        "attempts": [x.get("reason") or x.get("error") for x in r.rejected]}
                       for r in results if r.verdict == VERDICT_REJECT],
        "usage": {"input_tokens": tok_in, "output_tokens": tok_out,
                  "total_tokens": tok_in + tok_out,
                  "cost_usd": round(sum(d["usd"] for d in by_model.values()), 4),
                  # Present only when something was billed at a rate PRICES does not
                  # know, so a $0.0000 can never be mistaken for a free run.
                  **({"unpriced_models": sorted(UNPRICED)} if UNPRICED else {}),
                  "by_model": {k: {**v, "usd": round(v["usd"], 4)} for k, v in by_model.items()}},
        "edits": edits,
        "splits": splits,
        "duplicate_headings": dup_headings,
    }
    (stage4_dir / "stage4_report.json").write_text(json.dumps(report, indent=2))
    _mark_ai_processed(stage4_dir.parent, report)
    (stage4_dir / "STAGE4_REPORT.md").write_text(_report_md(report))
    (stage4_dir / "STAGE4_DIFF.html").write_text(_diff_html(report))
    _progress(job_dir, "done", changed=report["batches_changed"],
              rejected=report["batches_rejected"], cost_usd=report["usage"]["cost_usd"])
    return report


def _rebuild_table(original: str, rows: list[str], new_rows: list[str]) -> str:
    """Put repaired rows back inside the original <table> wrapper.

    Reassembles from the wrapper rather than string-replacing each row, because a repair
    may SPLIT one row into two — the row list is no longer 1:1 with the input, so
    positional replacement would corrupt the table.
    """
    head = original[:original.lower().index("<tr")] if "<tr" in original.lower() else "<table>"
    tail = "</table>"
    return head + "".join(new_rows) + tail


def _report_md(r: dict) -> str:
    u = r["usage"]
    lines = ["# Stage 4 — AI post-processing report", "",
             f"- **batches_total**: {r['batches_total']}",
             f"- **batches_changed**: {r['batches_changed']}",
             f"- **spacing_fixes**: {r['spacing_fixes']}",
             f"- **structural_fixes**: {r['structural_fixes']}",
             f"- **batches_unchanged**: {r['batches_unchanged']}",
             f"- **batches_reprojected**: {r.get('batches_reprojected', 0)} "
             f"(model re-punctuated; original characters restored)",
             f"- **batches_rejected**: {r['batches_rejected']} "
             f"(content-gate failures; stage-3 rows kept)", "",
             "## Cost", "",
             f"- **input tokens**: {u['input_tokens']:,}",
             f"- **output tokens**: {u['output_tokens']:,}",
             f"- **total tokens**: {u['total_tokens']:,}",
             f"- **cost**: ${u['cost_usd']:.4f}", ""]
    for model, d in u["by_model"].items():
        lines.append(f"  - `{model}` — {d['calls']} calls, "
                     f"{d['in']:,} in / {d['out']:,} out, ${d['usd']:.4f}")
    lines += ["", "## Per file", ""]
    for f in r["files"]:
        lines.append(f"- **{f['file']}** — {f['tables']} tables, {f['batches']} batches, "
                     f"{f['changed']} changed, {f['rejected']} rejected")
    if r["rejections"]:
        lines += ["", "## Rejected (left as stage 3)", ""]
        for x in r["rejections"]:
            lines.append(f"- {x['table']} row {x['row']} p{x['page']}: {x['reason']}")
    return "\n".join(lines) + "\n"


def _diff_html(r: dict) -> str:
    """Every accepted edit, before over after, for eyeball review."""
    esc = _html.escape
    rows = []
    for e in r["edits"]:
        badge = "structural" if e["verdict"] == VERDICT_STRUCTURAL else "spacing"
        rows.append(
            f"<section><h3>{esc(e['file'])} · {esc(str(e['table']))} · row {e['row']} · "
            f"page {e['page']} <span class='b {badge}'>{badge}</span> "
            f"<span class='m'>{esc(e['model'].split('.')[-1])}</span></h3>"
            f"<div class=g><pre class=before>{esc(e['before'])}</pre>"
            f"<pre class=after>{esc(e['after'])}</pre></div></section>")
    u = r["usage"]
    return (
        "<!doctype html><meta charset=utf-8><title>Stage 4 diff</title><style>"
        "body{font:13px/1.5 ui-monospace,Menlo,monospace;margin:2rem;background:#fbfbfa;color:#1a1a18}"
        "h1{font-size:1.3rem}section{margin:1.5rem 0;border-top:1px solid #ddd;padding-top:.8rem}"
        "h3{font-size:.8rem;font-weight:600;color:#555;margin:0 0 .5rem}"
        ".g{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}"
        "pre{white-space:pre-wrap;word-break:break-word;padding:.6rem;border-radius:5px;margin:0}"
        ".before{background:#fff0f0;border:1px solid #f3c9c9}"
        ".after{background:#eefaf0;border:1px solid #bfe3c8}"
        ".b{border-radius:3px;padding:.05rem .35rem;font-size:.7rem}"
        ".spacing{background:#e3edfb;color:#24467d}.structural{background:#fbeede;color:#7d5324}"
        ".m{color:#999;font-weight:400}"
        "</style>"
        f"<h1>Stage 4 — {len(r['edits'])} accepted edits</h1>"
        f"<p>{r['spacing_fixes']} spacing · {r['structural_fixes']} structural · "
        f"{r['batches_rejected']} rejected · {u['total_tokens']:,} tokens · "
        f"${u['cost_usd']:.4f}</p>" + "".join(rows))


# ---------------------------------------------------------------- sub-chunking

SUBCHUNK_SYSTEM = """You are given the rows of one HTML table from a legal memorandum,
numbered. The table covers several numbered sub-sections of the document (6.1, 6.2, 6.3
— or 7.1, 8.2, and so on). Some rows are GROUP LABEL rows: they announce a sub-section
rather than asking a question, e.g.

    row 1: 6.1 | Mutual Recognition of Funds/Passporting Schemes |
    row 18: 6.2 Funds
    row 24: 6.3 | Investment Management & Advisory Services |

Identify every group label row. Reply with JSON only, no prose, no code fence:

    [{"row": 1, "id": "6.1"}, {"row": 18, "id": "6.2"}, {"row": 24, "id": "6.3"}]

Rules:
- "id" MUST be the sub-section number exactly as printed in that row.
- Do NOT return the title text. It is read from the row itself.
- A row that asks a question, or that starts with a letter marker like "(a)", is NOT a
  group label. Neither is the "Questions | Answers" header row.
- If there are no group label rows, reply []."""


def _row_preview(row: str, limit: int = 90) -> str:
    return " | ".join(_TAG.sub("", c)[:limit].strip() for c in
                      re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I))


def _title_from_row(row: str, sec_id: str) -> str:
    """The heading text for a label row: everything in the row except its number.

    Taken from the row rather than from the model on purpose. The model is shown a
    TRUNCATED preview of each row, so asking it to echo the title back caps the heading
    at the preview length and silently drops the tail of a long one — "…in respect of
    Single Investor Vehicles," lost its remainder that way in testing. Reading the title
    off the row makes truncation and hallucination both impossible, and lets the verifier
    demand an exact match instead of settling for a prefix.
    """
    text = _html.unescape(_TAG.sub(" ", row))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^" + re.escape(sec_id) + r"\b[\s.:]*", "", text).strip()


def find_subsections(client, table_html: str, models=None) -> tuple[list[dict], list[dict]]:
    """Ask the model which rows open a new sub-section. Returns (boundaries, usage).

    The model only ever chooses ROW NUMBERS and reads back the section number it sees;
    the title and the split itself are ordinary string work. Each answer is checked
    against the row it names — the id must actually occur there — so a hallucinated
    heading cannot enter the document. A boundary that fails is dropped, not guessed at.
    """
    from ..obs import log as _log

    rows = split_rows(table_html)
    listing = "\n".join(f"row {i}: {_row_preview(r)}" for i, r in enumerate(rows))
    usage: list[dict] = []
    for model in (tuple(models) if models else (resolve_model(),)):
        # THE FOURTH BEDROCK CALL SITE, and until now the one with NO events at all — not
        # even a generic failure line. "no sub-chunking is a fine outcome" is true when the
        # model genuinely found no boundaries; it is not true when Bedrock throttled or
        # denied the call, and this `except Exception: continue` could not tell the two
        # apart from the caller's side, let alone from Kibana's. Same three-part contract
        # as the other calls: start before the wait, the AWS code and request id on
        # failure, registered so the heartbeat can see it while it is open.
        _attempts.n = 0
        _wire_reset()
        _t0 = time.time()
        _log.event("bedrock.call.start", message=f"find subsections via {model}",
                   **{"aci.model": model, "aci.purpose": "subchunk_boundaries",
                      "aci.rows": len(rows)})
        try:
            with _log.api_call("bedrock.converse", **{"aci.api_model": model,
                                                      "aci.api_purpose": "subchunk_boundaries"}):
                resp = client.converse(
                    modelId=model,
                    system=[{"text": SUBCHUNK_SYSTEM}],
                    messages=[{"role": "user", "content": [{"text": listing}]}],
                    inferenceConfig=inference_config(model, 2048),
                )
            u = resp.get("usage") or {}
            usage.append({"model": model, "in": int(u.get("inputTokens") or 0),
                          "out": int(u.get("outputTokens") or 0)})
            raw = response_text(resp)
            found = json.loads(_FENCE.sub("", raw).strip())
        except Exception as _e:  # noqa: BLE001 — no sub-chunking is a fine outcome
            _aws = _aws_error(_e)
            _log.exception("bedrock.call.end", _e,
                           message=(f"find subsections failed: {model}: "
                                    f"{_aws.get('aci.aws_error_code') or type(_e).__name__}"),
                           **{"aci.model": model, "aci.purpose": "subchunk_boundaries",
                              **_call_fields(_t0), **_aws})
            continue
        _log.event("bedrock.call.end", message=f"{model} ok in {round(time.time() - _t0, 1)}s",
                   **{"aci.model": model, "aci.purpose": "subchunk_boundaries",
                      "event.outcome": "success", **_call_fields(_t0)})
        out = []
        for b in found if isinstance(found, list) else []:
            try:
                i = int(b["row"])
                bid = str(b["id"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= i < len(rows)):
                continue
            # The section number must really be in the row the model pointed at.
            if not bid or content_stream(bid) not in content_stream(rows[i]):
                continue
            out.append({"row": i, "id": bid, "title": _title_from_row(rows[i], bid)})
        out.sort(key=lambda b: b["row"])
        return [b for j, b in enumerate(out) if j == 0 or b["row"] > out[j - 1]["row"]], usage
    return [], usage


def split_table(table_html: str, boundaries: list[dict], heading: str = "###",
                badge: str = "", repeat_header: bool = False) -> str:
    """One table -> a heading + table per sub-section. Deterministic, no model involved.

    The group label row becomes the heading and is dropped from the table, which is what
    removes the duplication: the label was previously emitted twice, once as a stage-1
    heading and once as a row. Its text is not lost — it moves into the heading, so the
    file's content stream is unchanged and the same gate still applies.

    `repeat_header` carries the "Questions | Answers" row into every sub-table. It reads
    better in isolation and helps a retrieval chunk stand alone, but it genuinely copies
    text, so it is off by default: the point of this pass was to REMOVE duplication.
    """
    rows = split_rows(table_html)
    if not boundaries:
        return table_html
    first = boundaries[0]["row"]
    # Rows above the first label — in practice the "Questions | Answers" header — ride
    # with the first sub-table rather than becoming a stranded one-row table of their own.
    lead = rows[:first]
    parts: list[str] = []
    for j, b in enumerate(boundaries):
        end = boundaries[j + 1]["row"] if j + 1 < len(boundaries) else len(rows)
        body = rows[b["row"] + 1:end]          # the label row itself becomes the heading
        block = f"{heading} {b['id']} {b['title']}".rstrip()
        if body:
            inner = (lead if j == 0 else (lead[:1] if repeat_header and lead else [])) + body
            block += (f"\n\n{badge}" if badge else "") + "\n\n<table>" + "".join(inner) + "</table>"
        parts.append(block)
    return "\n\n".join(parts)


def verify_split(table_html: str, markdown: str, boundaries: list[dict]) -> tuple[bool, str]:
    """Check a split kept everything. Returns (ok, reason).

    The reading-order gate used for cell repair cannot be reused here: promoting a label
    row to a heading legitimately moves its text above the "Questions | Answers" header
    row, so the linear stream changes order by design. The invariant that DOES hold is
    sharper, and is checked directly —

      * every non-label row survives, byte-identical, in its original order; and
      * every label row's text reappears exactly once, in its heading.

    Together those say no row was dropped, duplicated, reworded or resequenced, which is
    the whole claim a split needs to make.
    """
    rows = split_rows(table_html)
    label_idx = {b["row"] for b in boundaries}
    expected = [r for i, r in enumerate(rows) if i not in label_idx]
    got = split_rows(markdown)
    if got != expected:
        if len(got) != len(expected):
            return False, f"row count changed: {len(expected)} -> {len(got)}"
        i = next(k for k in range(len(got)) if got[k] != expected[k])
        return False, f"row {i} altered: {expected[i][:80]!r} -> {got[i][:80]!r}"

    headings = re.findall(r"^#{2,4}\s+(.*)$", markdown, re.M)
    if len(headings) != len(boundaries):
        return False, f"expected {len(boundaries)} headings, found {len(headings)}"
    for b, h in zip(boundaries, headings):
        want = content_stream(rows[b["row"]])
        if content_stream(h) != want:
            return False, (f"heading {h!r} does not carry its label row's text "
                           f"({want[:60]!r})")
    return True, "ok"


# ---------------------------------------------------------------- heading reconciliation

_HEADING = re.compile(r"^(#{2,4})\s+(\d+\.\d+)\b(.*)$", re.M)


def preceding_heading(text: str) -> tuple[str, str] | None:
    """(section id, full heading text) of the last heading before `text` ends."""
    ms = list(_HEADING.finditer(text))
    if not ms:
        return None
    m = ms[-1]
    return m.group(2), f"{m.group(2)}{m.group(3)}".strip()


def drop_duplicate_lead_heading(split_md: str, existing: tuple[str, str] | None) -> tuple[str, bool]:
    """Drop the split's first heading when stage 1 already wrote the same one above.

    Only when the two say the SAME thing, compared on content. A stage-1 heading that
    merely shares a number ("6.2 Funds" vs "6.2 Funds of One") is not a duplicate, and
    dropping ours there would lose the distinction — so it is kept and the file carries
    both, which the report flags.
    """
    if not existing:
        return split_md, False
    m = _HEADING.search(split_md)
    if not m or m.group(2) != existing[0]:
        return split_md, False
    ours = f"{m.group(2)}{m.group(3)}".strip()
    if content_stream(ours) != content_stream(existing[1]):
        return split_md, False
    return split_md[:m.start()].lstrip("\n") + split_md[m.end():].lstrip("\n"), True


def remove_empty_shells(text: str, ids: set[str], labels: dict[str, str]) -> tuple[str, list[str]]:
    """Delete stage-1 headings that now have a real home and nothing of their own.

    A shell is a heading followed only by blank lines before the next heading or the end
    of the file — the empty "## 6.2 Funds" that stage 1 left behind after its content was
    swallowed by the table above it. Two conditions before anything is deleted: the
    section must have been re-emitted with its content elsewhere in this file, and the
    shell's text must match that section's label exactly, so what is removed is provably
    a duplicate and not the only copy of something.

    A shell holding orphan prose is never touched; the prose has to go somewhere and this
    stage cannot say where.
    """
    removed: list[str] = []
    out = text
    for m in reversed(list(_HEADING.finditer(text))):
        sec = m.group(2)
        if sec not in ids:
            continue
        heading_text = f"{sec}{m.group(3)}".strip()
        if content_stream(heading_text) != content_stream(labels.get(sec, "")):
            continue
        nxt = _HEADING.search(text, m.end())
        body = text[m.end():nxt.start() if nxt else len(text)]
        if body.strip():
            continue                      # carries prose — leave it alone
        # Only remove this occurrence if the section still appears elsewhere.
        if len(re.findall(rf"^#{{2,4}}\s+{re.escape(sec)}\b", out, re.M)) < 2:
            continue
        out = out[:m.start()] + out[(nxt.start() if nxt else len(out)):]
        removed.append(heading_text)
    return out, removed


# ---------------------------------------------------------------- trailing duplicates

# A fragment must carry this much text before its absence from the prose can be called
# safe. Stage 1's leftovers include lines as short as "services;" — eight characters that
# occur inside a table by coincidence as often as by duplication. Those are REPORTED as
# candidates and left in place; only substantial matches are deleted.
MIN_DUP_CHARS = 20


def prune_trailing_duplicates(text: str, min_chars: int = MIN_DUP_CHARS) -> tuple[str, dict]:
    """Delete leftover prose after the last table that the tables already contain.

    Stage 1 writes a page snapshot for each page it judges to be a table, but its
    exclusion zone does not cover the whole page, so stray text lines leak out as prose.
    MinerU then captures that same text properly inside the table, and stage 3 splices
    the table in without removing the leak. The result is the run of broken fragments at
    the end of a chunk — "Services to the public in your jurisdiction permitted without",
    "Arranging Deals in Capital Markets Instruments:" twice — where 12 of chunk 07's 14
    trailing lines were already present verbatim inside its tables.

    Three conditions, all required, so "100% sure" means what it says:

      * the line sits AFTER the last table — the leftover region, not body prose;
      * it is not a heading, and it carries at least `min_chars` of real text; and
      * its content appears, exactly, inside a table in this same file.

    A line failing only the length test is returned as a candidate rather than removed.
    Headings are never touched: an empty stage-1 shell may be a duplicate, but deciding
    that is heading reconciliation's job, not this one.
    """
    tables = _TABLE_BLOCK.findall(text)
    if not tables:
        return text, {"removed": [], "candidates": []}
    table_text = content_stream("".join(tables))
    last = text.rfind("</table>")
    if last < 0:
        return text, {"removed": [], "candidates": []}
    head, tail = text[:last + len("</table>")], text[last + len("</table>"):]

    kept, removed, candidates = [], [], []
    for line in tail.splitlines():
        s = content_stream(line)
        if not s or line.lstrip().startswith("#"):
            kept.append(line)
            continue
        if s in table_text:
            if len(s) >= min_chars:
                removed.append(line.strip())
                continue
            candidates.append(line.strip())
        kept.append(line)

    if not removed:
        # Nothing to do — return the text byte-identical rather than reflowed. A step
        # that rewrites whitespace on every file it merely inspects turns a no-op into a
        # diff, and makes stage 4's output impossible to compare cleanly with stage 3.
        return text, {"removed": [], "candidates": candidates}
    out = head + "\n".join(kept)
    # Collapse the runs of blank lines that deleting alternate lines leaves behind.
    out = re.sub(r"\n{3,}", "\n\n", out).rstrip() + "\n"
    return out, {"removed": removed, "candidates": candidates}


# ---------------------------------------------------------------- section mode

# One call per SECTION, not per table. A section of these memoranda is one Q&A table with
# numbered sub-headings inside it; stage 1 detects it as several tables because it breaks
# across pages, and per-table calls can never put it back together — each call sees one
# fragment and none of the others. Chunk 04 is the plain case: section 3's table runs
# (a)-(d) on pages 13-14 and (e)-(f) on page 15, detected as table_003 and table_004, so
# no per-table prompt however worded could merge them.
#
# The prompt is deliberately short. The previous one grew to four numbered tasks and a
# rules block trying to specify structure procedurally, and the instruction that mattered
# most — lift the sub-section labels out — was applied to section 7 and skipped for
# sections 6 and 8. Given the pages and the whole section at once, the model has what it
# needs to work the shape out; what it needs from us is the goal and the one prohibition.
def _table_pages(stage_dir: Path) -> dict[str, list[int]]:
    """{table_id: [page numbers]} from stage 1's manifest — which pages each table
    came from, and therefore which page images ground its repair."""
    mp = Path(stage_dir) / "tables_manifest.json"
    if not mp.exists():
        return {}
    return {t["table_id"]: (t.get("pages") or [])
            for t in json.loads(mp.read_text()).get("tables", [])}


# The section's own page range, off the breadcrumb stage 3 writes. Same shape the review
# tool navigates by, so if this stops matching the viewer stops working too.
_CRUMB_PAGES = re.compile(r"^\*Source:[^*]*?page\s+(\d+)(?:\s*[\u2013-]\s*(\d+))?\*", re.M)


def _section_pages(text: str, pages_by_id: dict[str, list[int]]) -> list[int]:
    """Every page of this section, as page images to send as ground truth.

    The union of two sources, and it has to be both:

    THE TABLES MANIFEST says which pages each table was found on. Alone it is not the
    section: MinerU attributes a page SHARED by two sections to one of them, and it is
    always the later one, because that is where the next table starts. So the last page of
    every section was missing -- exactly the page a table's tail continues onto, and
    exactly where the next section's heading sits. On Bahamas that dropped page 34 from
    section 6, whose first printed line is "If so, please provide recommended wording in
    Section C of Appendix 3 of this Memorandum." -- the tail of 6.3's last question. The
    prompt tells the model it may only add text "visibly printed on one of the pages
    above", so it could not restore that line and was right not to: the page was never
    sent. No prompt wording can fix a page the model was not given.

    THE BREADCRUMB gives the section's real range, lo..hi. It also covers the sections with
    no tables at all, which had a manifest entry for nothing and were therefore repaired
    with ZERO ground truth -- 8 of 16 sections on this document.

    Kept as a union rather than replacing one with the other: a breadcrumb narrower than
    the table's own pages would otherwise silently drop ground truth that used to be sent.
    """
    seen: set[int] = set()
    for tid, _ in _tables_in(text):
        seen.update(pages_by_id.get(tid or "", []))
    m = _CRUMB_PAGES.search(text)
    if m:
        lo = int(m.group(1))
        seen.update(range(lo, int(m.group(2) or lo) + 1))
    return sorted(seen)


# The badge stage 3 writes above each table:
#   **⚙ MinerU-extracted table** — table_005, pages 15–18, 31 rows × 3 cols
# Split into the part the model may see (id + pages) and the part it may not (the counts).
_BADGE_LINE = re.compile(
    r"^(\*\*\u2699 MinerU-extracted table\*\*\s*\u2014\s*table_\d+,\s*pages?\s*[^,\n]*)"
    r"(,[^\n]*)?$", re.M)


def _hide_mineru_counts(text: str) -> tuple[str, dict[str, str]]:
    """Take MinerU's row/column COUNTS out of what the model is shown.

    MinerU's "31 rows × 3 cols" is a guess, and on table_005 it drove the model to
    restructure a 2-column question/answer table into 3 columns: stage 3 had 29 rows of
    two cells with the label glued into the question and 2 rows of three, and the model
    normalised all of them to the declared three. It was following the badge, which is
    exactly why the badge should not be making that claim -- the PAGES are the ground
    truth here, and the counts are a second opinion from the tool whose output is being
    corrected.

    The id and the pages STAY. They are how a table is identified and how its page images
    are looked up, and the pages are what the review tool references.

    Returns the text to send and the original badge lines, keyed by the text the model will
    see, so _restore_mineru_counts can put them back afterwards. Nothing is lost from the
    file on disk: this hides the claim for one API call, it does not rewrite stage 3.
    """
    originals: dict[str, str] = {}

    def _cut(m: re.Match) -> str:
        keep, claims = m.group(1), m.group(2) or ""
        if claims:
            originals[keep] = keep + claims
        return keep

    return _BADGE_LINE.sub(_cut, text), originals


def _restore_mineru_counts(text: str, originals: dict[str, str]) -> str:
    """Put the counts back, so the file on disk keeps the badge stage 3 wrote.

    Downstream reads that line: _tables_in maps table_005 to its pages through it, and
    table_width consults the declared columns in batched mode. A section returned without
    it would quietly lose both.
    """
    for keep, full in originals.items():
        if keep in text and full not in text:
            text = text.replace(keep, full, 1)
    return text


# ------------------------------------------------------------------------ links

# A bare URL in a cell is a URL the reader has to retype: these files are rendered by
# marked.js, which leaves "https://…" alone unless something wrapped it. So wrap it here.
#
# Deterministically, and NOT by asking the model — deliberately. An href lives inside a
# tag, and `content_stream` strips tags before any gate sees them, so a model that drops a
# "www." or invents a path segment writes a dead link that nothing in this file can catch.
# The one place the stream invariant does not reach is exactly the place a wrong character
# does the most damage. Built from the cell's own text instead, the destination is
# whatever the page printed, by construction.
# A numbered section file, as stage 1 names them -- "07-marketing-selling…".
# README.md and the three *_REPORT.md files sit in the same directory and are not content.
_SECTION_FILE = re.compile(r"\d+-")
# The non-content markdown every stage directory carries beside its sections. Named
# rather than pattern-matched because the pattern that used to do this job ("does the
# filename start with a digit") ALSO excluded content: a nested tree files its clauses
# under "A-substantial-shareholding/", and its appendix sub-sections are "A-background.md",
# "B-manager-has-investment-and-voting-discretion.md" -- real content, no leading digit.
_NON_SECTION_MD = frozenset({"README.md", "CONVERSION_REPORT.md",
                             "STAGE3_REPORT.md", "STAGE4_REPORT.md"})


def section_files(stage_dir) -> list[Path]:
    """Every CONTENT section file in a stage tree, flat or nested, in tree order.

    Products do not agree on tree SHAPE. 124_Marketing_Restrictions is flat -- every
    section a top-level "07-marketing-selling….md" -- while 104_Shareholding_Disclosure
    nests by Part: "A-substantial-shareholding/03-disclosure-thresholds/3.1-thresholds.md".
    A top-level `glob("*.md")` sees ONE content file on the nested shape (its front matter)
    and silently skips the other 123, which is what it did: Australia 172274 ran Stage 4
    over 1 of 124 sections, reported "1/1 sections accepted", and cost $0.01 -- a pass that
    reads as a clean success at every level except the document.

    Walks instead, and identifies a file by its path RELATIVE to the stage dir. Two
    sections may share a basename across a nested tree (each Part has its own
    "00-overview.md"), so the basename is not an identity: keying the report by it would
    have one Part's overview overwrite another's.
    """
    stage_dir = Path(stage_dir)
    return sorted((p for p in stage_dir.rglob("*.md")
                   if p.name not in _NON_SECTION_MD
                   and "_assets" not in p.relative_to(stage_dir).parts),
                  key=lambda p: p.relative_to(stage_dir).as_posix())


def section_key(stage_dir, md) -> str:
    """How a section is named in the report and the logs: its path below the stage dir.

    Equal to the bare filename on a flat tree, so a flat product's report is byte-identical
    to what it was before the walk above replaced the top-level glob."""
    return Path(md).relative_to(Path(stage_dir)).as_posix()
_TAG_OR_TEXT = re.compile(r"(<[^>]+>)|([^<]+)")
_A_OPEN = re.compile(r"<a[\s>]", re.IGNORECASE)
_A_CLOSE = re.compile(r"</a\s*>", re.IGNORECASE)
# The optional tail rejoins a URL that WRAPPED in the PDF. The text layer returns the wrap
# as a space — the page prints ".../IFA-Fund-Manager-Registration-IFR-Form-B.pdf" and the
# cell holds ".../IFA- Fund-Manager-Registration-IFR-Form-B.pdf" — and two of the Bahamas
# document's URLs are like that. Only a break after "-" or "/" counts, which is where a
# line wraps, and the continuation has to still look like a URL (it contains a "." or "/").
_URL = re.compile(r"""https?://[^\s<>"'`]+(?:(?<=[-/]) [^\s<>"'`]*[./][^\s<>"'`]*)?""")
_URL_TAIL = re.compile(r"""[)\].,;:'"]+$""")


def linkify_urls(text: str) -> tuple[str, int]:
    """Wrap every bare URL in `text` in an <a>. Returns the new text and how many.

    Three things it will not touch, because wrapping them breaks something:
      - a URL already inside an <a> — the model sometimes writes its own, unprompted
        and inconsistently, and this has to be idempotent across those;
      - the target of a markdown link, `](https://…)`, which marked.js already handles;
      - a URL in a code span, where the point is to show the characters.

    Trailing punctuation stays OUTSIDE the anchor: "(https://x/a.pdf)." must not put the
    ")." in the href, and must not lose it from the sentence either. The visible text is
    byte-for-byte what came in — the only place a wrapped URL is rejoined is the href.
    """
    n = 0

    def wrap(m: re.Match) -> str:
        nonlocal n
        raw = m.group(0)
        before = m.string[max(0, m.start() - 2):m.start()]
        if before.endswith("](") or before.endswith("`"):
            return raw
        tail_m = _URL_TAIL.search(raw)
        tail = tail_m.group(0) if tail_m else ""
        shown = raw[:len(raw) - len(tail)] if tail else raw
        # `shown` holds at most the single wrap-point space the regex allowed, and that
        # space is by construction preceded by "-" or "/", so dropping every space is
        # exactly the rejoin and nothing else.
        href = shown.replace(" ", "")
        if not re.fullmatch(r"https?://\S{4,}", href):
            return raw
        n += 1
        return (f'<a href="{href}" target="_blank" rel="noopener noreferrer">{shown}</a>'
                f"{tail}")

    out: list[str] = []
    in_anchor = False
    for tag, chunk in _TAG_OR_TEXT.findall(text):
        if tag:
            if _A_OPEN.match(tag):
                in_anchor = True
            elif _A_CLOSE.match(tag):
                in_anchor = False
            out.append(tag)
        elif in_anchor:
            out.append(chunk)
        else:
            out.append(_URL.sub(wrap, chunk))
    return "".join(out), n


def _page_png(assets: Path, page: int, pdf: Path | None) -> bytes | None:
    """The page image, rendering it from the PDF if stage 2 never did.

    Stage 2 renders only the pages it CROPS for tables -- 59 of 74 on Bahamas -- so asking
    for a section's whole page range asks for pages that were never rendered. Rendered here
    on demand, once, at the same 150 dpi: local, deterministic and free, and the alternative
    is sending the model a section with holes in its ground truth.
    """
    img = assets / f"page-{page:03d}.png"
    if img.exists():
        return img.read_bytes()
    if pdf is None or not Path(pdf).exists():
        return None
    try:
        import fitz
        with fitz.open(str(pdf)) as doc:
            if not 1 <= page <= doc.page_count:
                return None
            data = doc[page - 1].get_pixmap(dpi=150).tobytes("png")
        assets.mkdir(parents=True, exist_ok=True)
        img.write_bytes(data)                 # cached: the next section reuses it
        return data
    except Exception:                         # noqa: BLE001 — a page is not worth failing on
        return None


def repair_section(client, text: str, pages: list[int], assets: Path,
                   model: str = HAIKU, max_tokens: int = 32000,
                   pdf: Path | None = None, log=None,
                   instr: str = SECTION_INSTR) -> tuple[str, dict, str]:
    """One section, one call: all its pages, all its content, the whole markdown.

    `instr` picks the prompt: SECTION_INSTR for a section with tables, TEXT_SECTION_INSTR
    for plain prose. Everything else -- page grounding, hiding MinerU's counts, the
    reasoning-block-safe reply parse -- is identical either way.
    """
    from ..obs import log as _log

    parts: list[dict] = []
    absent: list[int] = []
    # PER PAGE, not just a total. A section whose payload is 40 MB is either thirty
    # ordinary pages or one page that rendered as a scanned photograph, and the two want
    # different fixes -- so the sizes travel alongside the page numbers rather than being
    # collapsed into a sum nobody can decompose.
    sent_pages: list[int] = []
    sizes: list[int] = []
    # Timed on its own, because it is not part of either half the call's own timing
    # measures. _page_png RENDERS a page stage 2 never cropped -- fitz at 150 dpi, on this
    # thread, for every missing page in the section -- and that happens before the Bedrock
    # call exists at all. Folded into the call it would be invisible; left out entirely,
    # a section that spent two minutes rendering and forty seconds in Bedrock reports as
    # a slow model.
    _render_t0 = time.time()
    _rendered = 0
    for p in pages:
        _cached = (assets / f"page-{p:03d}.png").exists()
        data = _page_png(assets, p, pdf)
        if data is None:
            # SAY SO. Silently skipping a page is what let section 6 be repaired without
            # page 34 -- the page carrying the tail of its last question -- while the log
            # reported a confident "15 page images".
            absent.append(p)
            continue
        sent_pages.append(p)
        sizes.append(len(data))
        _rendered += 0 if _cached else 1
        parts.append({"image": {"format": "png", "source": {"bytes": data}}})
    render_s = round(time.time() - _render_t0, 2)
    if absent and log:
        log(f"     WARNING: no page image for {absent} — repaired without that ground truth")
    img_bytes = sum(sizes)
    # MinerU's row/column counts are hidden for the duration of the call and put back on
    # the way out -- see _hide_mineru_counts for the table this cost.
    sent, badge_claims = _hide_mineru_counts(text)
    parts.append({"text": instr + "\n\n" + sent})
    # An ESTIMATE, and named one. Converse carries the PNGs base64-encoded, which is 4/3
    # of the file on disk plus JSON framing, so this is what the call is about to weigh
    # give or take a few hundred bytes of envelope. It exists because it is available
    # BEFORE the call -- the exact figure arrives from the before-send hook, by which time
    # the request is already gone, and a call that hangs never produces one at all. This
    # is the number the heartbeat has to work with while a section sits open for twenty
    # minutes.
    text_bytes = len((instr + sent).encode("utf-8", "ignore"))
    payload_est = int(img_bytes * 4 / 3) + text_bytes
    # One event carrying the page-by-page breakdown, separate from call.start so that the
    # start event stays the same shape it is at every other call site. Emitted before the
    # call for the same reason call.start is: a payload that is never answered for is
    # still a payload that was built.
    _log.event("bedrock.payload",
               message=(f"{len(sizes)} page image(s), {_mb(img_bytes)} MB on disk, "
                        f"~{_mb(payload_est)} MB encoded "
                        f"({_rendered} rendered here in {render_s}s)"),
               **{"aci.model": model, "aci.purpose": "section_repair",
                  "aci.images": len(sizes),
                  "aci.image_pages": sent_pages,
                  "aci.image_sizes": sizes,
                  "aci.image_bytes": img_bytes,
                  "aci.image_mb": _mb(img_bytes),
                  "aci.image_bytes_max": max(sizes) if sizes else 0,
                  "aci.image_page_largest": (sent_pages[sizes.index(max(sizes))]
                                             if sizes else None),
                  "aci.pages_absent": len(absent) or None,
                  # How many of them this call had to rasterise itself, and what that
                  # cost. Stage 2 renders only the pages it crops for tables, so the
                  # first section over a page range pays for the rest and every later
                  # section reads them from the cache -- which is why one section can
                  # look far slower than its neighbours for no reason visible in the
                  # model's timing.
                  "aci.images_rendered": _rendered,
                  "aci.images_cached": len(sizes) - _rendered,
                  "aci.render_s": render_s,
                  "aci.text_bytes": text_bytes,
                  "aci.payload_bytes_est": payload_est,
                  "aci.payload_mb_est": _mb(payload_est)})
    if log:
        log(f"     payload: {len(sizes)} image(s) {_mb(img_bytes)} MB + "
            f"{text_bytes:,} chars of text → ~{_mb(payload_est)} MB encoded")
    # THE CALL STAGE 4 ACTUALLY SPENDS ITS TIME IN.
    #
    # One section, one call, every page image in it — and run_stage4_section runs
    # stage4_ai_workers of these CONCURRENTLY, so a stalled stage 4 is some number of these
    # sitting open at once with nothing else to see. The start event is the half that
    # matters: it names the section and the model BEFORE the wait, so a call that never
    # returns still says what it was doing. Without it, "stuck in stage 4" has no evidence
    # at all beyond a heartbeat saying the stage has been running a long time, which a large
    # document does legitimately.
    _t0 = time.time()
    _attempts.n = 0
    _wire_reset()
    _log.event("bedrock.call.start",
               message=(f"repair section via {model}: {len(sizes)} image(s), "
                        f"~{_mb(payload_est)} MB"),
               **{"aci.model": model, "aci.purpose": "section_repair",
                  "aci.images": len(parts) - 1, "aci.pages_absent": len(absent) or None,
                  "aci.max_tokens": max_tokens,
                  "aci.chars_sent": len(sent),
                  "aci.image_bytes": img_bytes, "aci.image_mb": _mb(img_bytes),
                  "aci.render_s": render_s, "aci.images_rendered": _rendered,
                  "aci.payload_bytes_est": payload_est,
                  "aci.payload_mb_est": _mb(payload_est)})
    try:
        # Registered for the duration — this is the call stage 4 runs several of at once, so
        # it is the one the heartbeat most needs to be able to count and age.
        #
        # aci.api_bytes is the ESTIMATE until the before-send hook overwrites it with the
        # real serialized size, which it does on the same thread a moment later. Seeded
        # here rather than left absent so that a call which dies before reaching the wire
        # still tells the heartbeat roughly what it was carrying.
        with _log.api_call("bedrock.converse", **{"aci.api_model": model,
                                                  "aci.api_purpose": "section_repair",
                                                  "aci.api_images": len(parts) - 1,
                                                  "aci.api_bytes": payload_est}):
            resp = client.converse(
                modelId=model,
                system=[{"text": SYSTEM_GROUNDTRUTH}],
                messages=[{"role": "user", "content": parts}],
                inferenceConfig=inference_config(model, max_tokens),
            )
    except BaseException as _e:
        _aws = _aws_error(_e)
        _log.exception("bedrock.call.end", _e,
                       message=(f"section repair failed: {model}: "
                                f"{_aws.get('aci.aws_error_code') or type(_e).__name__}"),
                       **{"aci.model": model, "aci.purpose": "section_repair",
                          "aci.images": len(parts) - 1,
                          "aci.image_bytes": img_bytes, "aci.image_mb": _mb(img_bytes),
                          "aci.render_s": render_s, "aci.images_rendered": _rendered,
                          "aci.payload_bytes_est": payload_est,
                          **_call_fields(_t0), **_aws})
        raise
    u = resp.get("usage") or {}
    # Built once, so the human-readable message and the fields cannot disagree about how
    # the time was split.
    _f = _call_fields(_t0)
    _log.event("bedrock.call.end",
               # max_tokens here is a section that comes back plausible and SHORT — the
               # mechanism behind an accepted-but-lossy section.
               level="warn" if resp.get("stopReason") == "max_tokens" else "info",
               # THE SPLIT, spelled out: how long we spent building the request, how big
               # it was, and how long Bedrock then took. "Slow section" means something
               # different for each of the three.
               message=(f"section repaired in {_f['event.duration_s']}s "
                        f"— {_f.get('aci.prep_s')}s prep, "
                        f"{_f.get('aci.wire_mb')} MB up ({len(sizes)} image(s), "
                        f"{render_s}s rendering), "
                        f"{_f.get('aci.wait_s')}s waiting on Bedrock"),
               **{"aci.model": model, "aci.purpose": "section_repair",
                  "event.outcome": "success", **_f,
                  "aci.images": len(parts) - 1,
                  "aci.image_bytes": img_bytes, "aci.image_mb": _mb(img_bytes),
                  "aci.render_s": render_s, "aci.images_rendered": _rendered,
                  "aci.payload_bytes_est": payload_est,
                  "aci.tokens_in": int(u.get("inputTokens") or 0),
                  "aci.tokens_out": int(u.get("outputTokens") or 0),
                  "aci.aws_request_id": (resp.get("ResponseMetadata") or {}).get("RequestId"),
                  "aci.stop_reason": resp.get("stopReason")})
    body = _FENCE.sub("", response_text(resp)).strip()
    body = _restore_mineru_counts(body, badge_claims)
    return body, {"model": model, "in": int(u.get("inputTokens") or 0),
                  "out": int(u.get("outputTokens") or 0)}, resp.get("stopReason", "")


def _cell_content(text: str) -> str:
    """Every table CELL's text in a section, concatenated. Nothing else."""
    return "".join(content_stream(c)
                   for tbl in _TABLE_BLOCK.findall(text or "")
                   for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tbl, re.S | re.I))


def accept_section(before: str, after: str) -> tuple[bool, str]:
    """Keep the model's section? Yes, unless it returned nothing usable.

    This used to block any loss of cell text, and that guarantee could not survive the
    two requirements it now sits between: "no cell content may go missing" and "delete
    the content of other sections that stage 1 dragged into this chunk". Telling those
    apart mechanically needs a signal we do not have — on chunk 04, section 5's rows sit
    INSIDE the same table as section 3's (e) and (f), and its heading is a bare text line
    after both tables, so neither position nor markup separates them.

    So the loss is measured and reported rather than enforced. `cell_chars_lost` in
    stage4_report.json says exactly how much cell text each section shed, and stage 3
    remains untouched on disk, so any section can be compared or reverted. Validation is
    deferred, not abandoned.
    """
    if not after or not split_rows(after):
        return False, "model returned no rows"
    lost = collections.Counter(_cell_content(before)) - collections.Counter(_cell_content(after))
    gained = collections.Counter(_cell_content(after)) - collections.Counter(_cell_content(before))
    n_lost, n_gained = sum(lost.values()), sum(gained.values())
    if not n_lost and not n_gained:
        return True, "cell text unchanged"
    return True, f"cell text {n_lost} lost / {n_gained} added — reported, not enforced"


def _prose_content(text: str) -> str:
    """A text-only section's whole content, ignoring markup -- the same content_stream
    measure `_cell_content` applies to table cells, generalised to a section with no
    cells to isolate it to."""
    return content_stream(text)


def accept_text_section(before: str, after: str) -> tuple[bool, str]:
    """Keep the model's text-only section? Same policy as accept_section: reject only
    empty output, and report drift rather than enforce it, for the reason accept_section
    already gives -- spillover removal legitimately drops text that belongs to a
    neighbouring section, so a hard content gate cannot tell that apart from a mistake.

    One thing IS enforced, mechanically rather than by prompt: which section this is.
    Two sections sharing a page (front matter running up to page 3, the real "1.
    BACKGROUND" starting partway down the same page) can each hand the model a ground
    -truth image showing the OTHER section's heading, and nothing in the word-preservation
    check catches the model adopting it -- a wholesale content swap still passes as
    "n lost / n gained". A section's own number is the one thing spillover-trimming never
    legitimately changes, so a before/after mismatch here is not drift, it is the model
    having answered a different section than the one it was asked to repair."""
    if not after.strip():
        return False, "model returned no text"
    before_num, after_num = _heading_number(before), _heading_number(after)
    if before_num != after_num:
        return False, (f"heading section number changed ({before_num!r} -> {after_num!r}) "
                       "— looks like a neighbouring section's content, not a repair")
    lost = collections.Counter(_prose_content(before)) - collections.Counter(_prose_content(after))
    gained = collections.Counter(_prose_content(after)) - collections.Counter(_prose_content(before))
    n_lost, n_gained = sum(lost.values()), sum(gained.values())
    if not n_lost and not n_gained:
        return True, "text unchanged"
    return True, f"text {n_lost} lost / {n_gained} added — reported, not enforced"


def _mark_ai_processed(job_dir: Path, report: dict) -> None:
    """Record on the job that it has been through the AI pass, and with what.

    stage4_report.json existing is already an implicit flag, but a consumer deciding
    whether a document is eligible for AI-only behaviour should not have to open a stage
    directory to find out. This puts it where the corpus already keeps per-document
    facts, so the UI listing and the sub-chunker read the same source. Never raises — a
    missing flag must not fail a completed run.
    """
    try:
        mp = Path(job_dir) / "corpus_meta.json"
        meta = json.loads(mp.read_text()) if mp.exists() else {}
        meta["ai_processed"] = True
        meta["ai_model"] = report.get("model")
        meta["ai_mode"] = report.get("mode")
        meta["ai_cost_usd"] = (report.get("usage") or {}).get("cost_usd")
        meta["ai_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        mp.write_text(json.dumps(meta, indent=2))
    except Exception:  # noqa: BLE001
        pass


def _run_subchunk(job_dir: Path, log=_say) -> dict:
    """Split the AI output into per-sub-section files, straight after stage 4.

    Chained here rather than left to the caller because it is not an optional extra: the
    sub-section labels only become separable BECAUSE stage 4 lifted them out of the tables
    into headings, so every completed AI pass wants this and nothing else can produce it.
    Stage 4 itself stays explicit — nothing runs it automatically — but once it has run,
    stage 5 follows.

    The splitter keeps its own product gate, so this is a no-op for a product that is not
    listed. Never raises: a finished, verified stage 4 must not be discarded because the
    split that comes after it failed.
    """
    try:
        repo = Path(__file__).resolve().parents[3]
        if str(repo / "scripts") not in sys.path:
            sys.path.insert(0, str(repo / "scripts"))
        from subchunk import run as run_subchunk

        r = run_subchunk(job_dir)
        if r.get("ran"):
            log(f"  stage 5: {r['sections_split']} section(s) -> "
                f"{r['subchunks_total']} sub-chunks")
        else:
            log(f"  stage 5: skipped ({r.get('reason')})")
        return r
    except Exception as e:  # noqa: BLE001
        log(f"  stage 5: FAILED ({type(e).__name__}: {e})")
        return {"ran": False, "reason": f"{type(e).__name__}: {e}"[:_LOG_MAX]}


def _record_timings(job_dir: Path, **steps) -> None:
    """Merge stage 4/5 wall clock into the job's timings.json.

    Stages 1-3 record themselves there and the scorecard, the gallery and the pipeline
    monitor all read it, so anything that costs time has to land in the same file or it
    is invisible to every existing consumer. MERGED rather than written: stage 4 runs
    long after run_corpus wrote the stage 1-3 entries, and must not erase them.
    """
    try:
        path = Path(job_dir) / "timings.json"
        try:
            cur = json.loads(path.read_text())
        except (OSError, ValueError):
            cur = {}
        cur.setdefault("steps", {}).update({k: round(v, 1) for k, v in steps.items()})
        # `total` covers stages 1-3 only; the AI passes are opt-in and priced separately,
        # so they are summed on their own rather than folded into a number that every
        # capacity estimate built from a finished corpus already treats as extraction.
        cur["steps"]["ai_total"] = round(sum(v for k, v in cur["steps"].items()
                                             if k.startswith(("stage4", "stage5"))), 1)
        path.write_text(json.dumps(cur, indent=2))
    except Exception:  # noqa: BLE001 — a missing timing must not fail a finished run
        pass


_ANY_HEADING = re.compile(r"^#{1,6}\s+.*$", re.M)
# Reads the same leading digit subchunk.py's own _H1 keys the stage-5 split off of, so
# "this is still the same section" means the same thing in both places. "Appendix N" is
# read the same way stage 5 does, and folding an orphaned "APPENDIX N" label into the
# heading (see TEXT_SECTION_INSTR) never changes N, so that legitimate rewrite still
# passes this check.
_H1_NUM = re.compile(r"(?m)^#\s+(?:Appendix\s+)?(\d+)\.?\s", re.I)


def _heading_number(text: str) -> str | None:
    """The section number a text section's own first heading claims, or None for an
    unnumbered one (front matter, "Section Intentionally Left Blank", ...)."""
    m = _H1_NUM.search(text)
    return m.group(1) if m else None


# Every top-level heading in a reply, not just the first. _heading_number answers "WHICH
# section is this?" and search() is right for that; this answers "is there more than one?",
# which is a different question and the one nothing was asking.
_H1_LINE = re.compile(r"(?m)^#\s+.*$")
_TRAILING_RULE = re.compile(r"(?:\n\s*(?:-{3,}|\*{3,}|_{3,})\s*)+$")


def _trim_foreign_sections(before: str, after: str) -> tuple[str, str | None]:
    """Cut a reply back to its own section when the model carried on into the next one.

    Two short sections routinely share a page, so a section's ground-truth images can show
    the whole of its neighbour -- heading and all -- and the boundary rule in
    TEXT_SECTION_INSTR is the only thing telling the model not to transcribe it. Measured
    on the 77-document MRAM run: the rule loses on 7 of 1036 sections, five of them the
    same short "Disclaimers: Closed-Ended Fund" appendix whose successor starts on its own
    last page.

    accept_text_section cannot see it. Its identity check reads the FIRST heading number,
    and in this failure the first heading is CORRECT -- the reply opens with its own
    section and simply continues past the end of it, so "2" == "2" and the check passes
    while a whole foreign appendix rides along underneath.

    Every top-level heading after a section's own is, by that same boundary rule, another
    file's. Stage 3 never emits two (879 of 879 section files across 56 documents carry
    exactly one), so a second one is always the model's addition and never content this
    section is entitled to keep. Cut at it rather than rejecting the whole reply: the
    repair ABOVE the cut is the legitimate work this call was paid for, and rejecting
    would throw it away along with the contamination.
    """
    heads = list(_H1_LINE.finditer(after))
    if len(heads) < 2:
        return after, None
    own = _heading_number(before)
    for m in heads[1:]:
        if _heading_number(m.group(0)) != own:
            # The horizontal rule the model writes BETWEEN two sections belongs to the
            # boundary, not to the section above it -- leaving it behind would end the
            # file on a separator with nothing after it.
            kept = _TRAILING_RULE.sub("", after[:m.start()].rstrip())
            return kept.rstrip() + "\n", m.group(0).strip()
    return after, None


def _has_real_content(text: str, min_chars: int = MIN_DUP_CHARS) -> bool:
    """Whether a section has any body text once its heading(s) and *Source:* crumb are
    stripped out. A bare heading with nothing under it -- an empty shell stage 1
    sometimes leaves behind -- is not something a model can repair, and sending it
    anyway would spend an AI call on nothing."""
    stripped = _CRUMB_PAGES.sub("", _ANY_HEADING.sub("", text))
    return len(content_stream(stripped)) >= min_chars


def _repair_one_section(client, md: Path, pages_by_id: dict[str, list[int]], assets: Path,
                        model: str, text_model: str, pdf_path, log, prune_duplicates: bool,
                        key: str | None = None
                        ) -> tuple[dict, dict | None, str | None]:
    """One section end to end: read, (maybe) dedupe, call the model, accept, decide.

    Runs on a worker thread — self-contained, touches only this file, and returns rather
    than writes, so the caller can write files back in a fixed order regardless of which
    thread finished first. `to_write` is None exactly when the caller should leave the
    file untouched: a crash before the model answered must not overwrite stage 3 with a
    half-formed edit.

    `key` is how this section is NAMED in the report and the logs -- its path below the
    stage dir on a nested tree, so two Parts' "00-overview.md" stay distinguishable. It
    falls back to the basename, which is the same string on a flat tree.
    """
    key = key or md.name
    text = md.read_text()
    is_table_section = bool(_tables_in(text))
    kind = "table" if is_table_section else "text"

    if not is_table_section and not _has_real_content(text):
        # A bare heading (± a *Source:* line) and nothing else -- an empty shell, not a
        # section. Skip the call rather than pay for a model to look at nothing; the file
        # is left exactly as stage 3 wrote it (to_write=None).
        log(f"  {key}: text section, no body content — skipped")
        return {"file": key, "ok": True, "kind": kind,
                "reason": "no body content — skipped"}, None, None

    instr = SECTION_INSTR if is_table_section else TEXT_SECTION_INSTR
    accept_fn = accept_section if is_table_section else accept_text_section
    section_model = model if is_table_section else text_model

    # Off by default. A repeated line is not evidence of a leak in these documents: they
    # repeat themselves by nature, and the "is this text already in a table" test flags
    # genuine repetition as duplication. Available for a document where the stage-1 leak
    # is known to be the problem.
    pruned = {"removed": [], "candidates": []}
    if prune_duplicates:
        text, pruned = prune_trailing_duplicates(text)
    pages = _section_pages(text, pages_by_id)
    n_tables = len(_tables_in(text))
    log(f"  {key}: {kind} section, {n_tables} table(s), {len(pages)} page images…")
    try:
        # WHICH SECTION, bound in this worker thread. The contextvar is per-thread, so six
        # concurrent repairs each carry their own -- and log.api_call copies it onto the
        # registry entry, which is what lets a heartbeat say "oldest call: 15-scope.md,
        # open 22 minutes" instead of only "six calls open".
        from ..obs import log as _sec_log
        with _sec_log.context(**{"aci.section": key}):
            after, used, stop = repair_section(client, text, pages, assets, section_model,
                                               pdf=pdf_path, log=log, instr=instr)
    except Exception as e:  # noqa: BLE001 — a failure keeps stage 3
        # SAY WHY. The reason went into the report and the log said only "FAILED", so a
        # run with no AWS credentials failed all 8 sections in four seconds and reported
        # "0/8 accepted, $0.0000" -- indistinguishable on screen from a model that had
        # read every page and declined to change anything. The reason is the whole
        # difference between "the prompt did not work" and "nothing ran".
        # 200 was enough for the short rejection reasons this was written for and useless
        # for an AWS error: AccessDeniedException spends its first ~200 characters on the
        # exception name, "when calling the Converse operation" and a 100-character IAM
        # ARN, so the cap deleted the action, the resource and the reason -- every run
        # without Bedrock access read as "REJECTED — ...is not authorized to " and
        # stopped there.
        reason = f"{type(e).__name__}: {e}"[:_LOG_MAX]
        log(f"     -> FAILED, kept as stage 3 — {reason}"[:_LOG_MAX + 40])
        return {"file": key, "ok": False, "kind": kind, "reason": reason}, None, None

    # BEFORE the accept check, because the accept check is what this slips past: it reads
    # the reply's FIRST heading, which in this failure is the right one. Trimming here
    # means accept_fn measures drift against the section the model was actually asked for,
    # so a reply that was clean apart from the spillover now reports as clean.
    after, foreign = _trim_foreign_sections(text, after)
    if foreign:
        log(f"     trimmed a foreign section from the reply: {foreign[:80]}")

    ok, why = accept_fn(text, after)
    rec = {"file": key, "ok": ok, "reason": why, "stop_reason": stop,
           "pages": len(pages), "kind": kind,
           # Recorded even when None, so "this run trimmed nothing" is readable from the
           # report rather than inferred from the absence of a key.
           "foreign_section_trimmed": foreign,
           # Per-section attribution -- which model actually touched this file and what it
           # cost -- so a report with more than one model in play (a table pass on one
           # model, a text pass on another) can be read section by section rather than
           # only as one blended total.
           "model": used["model"], "tokens_in": used["in"], "tokens_out": used["out"],
           "cost_usd": round(cost_usd(used["model"], used["in"], used["out"]), 4),
           "tables_before": n_tables, "tables_after": after.count("<table"),
           "rows_before": sum(len(split_rows(t)) for t in _TABLE_BLOCK.findall(text)),
           "rows_after": sum(len(split_rows(t)) for t in _TABLE_BLOCK.findall(after)),
           "headings_added": len([h for h in re.findall(r"(?m)^#{2,4} .*$", after)
                                  if h not in re.findall(r"(?m)^#{2,4} .*$", text)]),
           "cell_chars_before": len(_cell_content(text)),
           "cell_chars_after": len(_cell_content(after)),
           "cell_chars_lost": max(0, len(_cell_content(text)) - len(_cell_content(after))),
           "duplicates_pruned": len(pruned["removed"])}
    log(f"     -> {rec['tables_after']} table(s), {rec['rows_after']} rows, "
        f"+{rec['headings_added']} headings, {'accepted' if ok else 'REJECTED'}: {why}")
    return rec, used, (after if ok else text)


def run_stage4_section(stage3_dir, stage4_dir, pdf_path=None, only: list[str] | None = None,
                       client=None, model: str = HAIKU, text_model: str | None = None,
                       log=_say, prune_duplicates: bool = False,
                       subchunk_after: bool = True) -> dict:
    """Stage 4 at section granularity — the default.

    Every section file gets an AI pass now, not just the ones with tables: a section with
    tables goes to SECTION_INSTR under `model`, a plain-prose section goes to
    TEXT_SECTION_INSTR under `text_model` -- a separate, usually cheaper model, since text
    reproduction does not need the structural judgement the table pass is chosen for -- and
    the two run CONCURRENTLY across a thread pool -- the calls are independent Bedrock
    requests, so nothing is gained by making a text section wait behind a table section
    or vice versa. Sized by settings.stage4_ai_workers (ACI_STAGE4_AI_WORKERS).
    """
    if text_model is None:
        text_model = resolve_text_model()
    stage3_dir, stage4_dir = Path(stage3_dir), Path(stage4_dir)
    started = time.time()
    _progress(stage4_dir.parent, "running")
    if stage4_dir.exists():
        shutil.rmtree(stage4_dir)
    shutil.copytree(stage3_dir, stage4_dir)

    pages_by_id = _table_pages(stage3_dir)
    assets = stage4_dir / "_assets"
    if client is None:
        client = bedrock_client()

    try:
        from aosphere_core_index.config import settings
        workers = max(1, int(settings.stage4_ai_workers))
    except Exception as e:                                   # noqa: BLE001
        # 1, not 3. This fallback used to raise concurrency to three times the code's own
        # default the moment settings failed to import -- silently, so a host that had
        # deliberately set ACI_STAGE4_AI_WORKERS=1 would fan three calls per pod at Bedrock
        # and nothing said why. Fall back to the SAFE value and say that it happened.
        workers = 1
        from ..obs import log as _log
        _log.event("stage4.settings_unreadable", level="warn",
                   message=f"could not read stage4_ai_workers ({type(e).__name__}): "
                           f"falling back to 1",
                   **{"error.type": type(e).__name__, "error.message": str(e),
                      "aci.stage4_workers": 1})

    # Only real section files ("07-marketing-selling…") go through the AI pass — the same
    # filter the URL-linking pass below uses. README.md and the *_REPORT.md files stage 3
    # writes into the same directory are not content, and now that a table is no longer
    # required to enter this loop, nothing else would have kept them out of it.
    # `only` matches the relative path as well as the basename, so a caller can name one
    # file ("3.1-thresholds.md") or a whole Part ("A-substantial-shareholding/").
    targets = [p for p in section_files(stage4_dir)
               if only is None or any(section_key(stage4_dir, p).startswith(pre)
                                      or p.name.startswith(pre) for pre in only)]

    usage: list[dict] = []
    results: dict[Path, tuple[dict, dict | None, str | None]] = {}

    # THE UPLOAD SEQUENCE, counted.
    #
    # A section's own bedrock.call.start/end says what that call did. What no single call
    # can say is whether the SET of them finished: with the pool swallowing a worker, a
    # SIGTERM landing mid-stage, or a pod evicted at section 40 of 60, the surviving
    # evidence is forty ordinary successful calls and nothing at all to say that twenty
    # more were meant to follow. "Did the run send everything it had?" is then answered by
    # counting records in Kibana and hoping none were dropped -- which is not an answer.
    #
    # So the total is declared up front and reconciled at the end, and the end event
    # carries aci.upload_complete as a plain boolean. Absence of that event is itself the
    # signal: a stage 4 with a start and no end did not finish, and no arithmetic is
    # needed to see it.
    from ..obs import log as _log

    _up_t0 = time.time()
    _up_bytes0, _up_reqs0 = _uploads.snapshot()
    _log.event("stage4.upload.start",
               message=f"{len(targets)} section(s) to send, {workers} worker(s)",
               **{"aci.sections_total": len(targets), "aci.stage4_workers": workers,
                  "aci.model": model, "aci.text_model": text_model})
    _done = 0
    _done_lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(targets) or 1)) as pool:
        # ctx.run, NOT a bare submit. A new thread starts at the contextvar's default --
        # an EMPTY context -- so every event a section worker emits lost aci.run_id,
        # aci.stage, aci.shard, aci.product and service.name, and carried only the
        # aci.section it binds for itself. The events shipped and indexed fine; they were
        # simply invisible to every Kibana filter scoped to a run, which is how a whole
        # stage's worth of bedrock.call.start/end/retry could look like it never happened.
        # A fresh copy per submit, because one Context cannot be entered concurrently.
        futures = {pool.submit(contextvars.copy_context().run,
                               _repair_one_section, client, md, pages_by_id, assets,
                               model, text_model, pdf_path, log, prune_duplicates,
                               section_key(stage4_dir, md)): md
                   for md in targets}
        for fut in concurrent.futures.as_completed(futures):
            results[futures[fut]] = fut.result()
            # ONE LINE PER SECTION FINISHED, carrying the running upload total. This is
            # the "how far through is it" signal: n of N, and how many megabytes that has
            # cost so far. A stage that stops emitting these has stopped making progress,
            # which the heartbeat alone cannot distinguish from one long call.
            try:
                with _done_lock:
                    _done += 1
                    n = _done
                _b, _r = _uploads.snapshot()
                _log.event("stage4.upload.progress",
                           message=(f"{n}/{len(targets)} section(s) done, "
                                    f"{_mb(_b - _up_bytes0)} MB uploaded"),
                           **{"aci.sections_done": n,
                              "aci.sections_total": len(targets),
                              "aci.sections_remaining": len(targets) - n,
                              "aci.section": results[futures[fut]][0].get("file"),
                              "aci.upload_bytes": _b - _up_bytes0,
                              "aci.upload_mb": _mb(_b - _up_bytes0),
                              "aci.upload_requests": _r - _up_reqs0,
                              "event.duration_s": round(time.time() - _up_t0, 1)})
            except Exception:                                # noqa: BLE001
                pass                                         # never fail a run over a count

    # Written back in the original, deterministic file order -- concurrency changes which
    # section finishes first, not what the finished tree looks like or what the report says.
    sections: list[dict] = []
    for md in targets:
        rec, used, to_write = results[md]
        sections.append(rec)
        if used:
            usage.append(used)
        if to_write is not None:
            md.write_text(to_write)

    # Bare URLs -> <a href>, over EVERY section file rather than only the ones the loop
    # above just wrote. Cheap and idempotent, so re-running it here is simpler than
    # threading a "did this file change" flag out of a concurrent loop; a rejected
    # section's stage-3 text still gets linked. Runs before the report and before stage 5,
    # so both see the linked text.
    urls_by_file: dict[str, int] = {}
    for md in section_files(stage4_dir):
        linked, k = linkify_urls(md.read_text())
        if k:
            md.write_text(linked)
            urls_by_file[section_key(stage4_dir, md)] = k
    for rec in sections:
        rec["urls_linked"] = urls_by_file.get(rec["file"], 0)
    if urls_by_file:
        log(f"  URLs linked: {sum(urls_by_file.values())} "
            f"across {len(urls_by_file)} file(s)")

    tok_in = sum(u["in"] for u in usage)
    tok_out = sum(u["out"] for u in usage)
    by_model: dict[str, dict] = {}
    for u in usage:
        d = by_model.setdefault(u["model"], {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        d["calls"] += 1
        d["in"] += u["in"]
        d["out"] += u["out"]
        d["usd"] += cost_usd(u["model"], u["in"], u["out"])

    # THE RECONCILIATION. Declared N at the top, accounted for N here -- and
    # aci.upload_complete says whether those two numbers match rather than leaving it to
    # be worked out from the other fields. A stage4.upload.start with no matching end is
    # the case this is really for: the stage died partway, and no count of successful
    # calls can reveal that on its own.
    #
    # "Skipped" is a third outcome and is kept separate from both. An empty-shell section
    # deliberately makes no call, so folding it into failures would report a healthy run
    # as incomplete, and folding it into successes would claim an upload that never
    # happened.
    _skipped = sum(1 for r in sections if r.get("reason") == "no body content — skipped")
    _failed = sum(1 for r in sections if not r.get("ok"))
    _ok = len(sections) - _failed - _skipped
    _b, _r = _uploads.snapshot()
    _complete = len(sections) == len(targets) and _failed == 0
    _log.event("stage4.upload.end",
               level="info" if _complete else "warn",
               message=(f"{'complete' if _complete else 'INCOMPLETE'}: "
                        f"{_ok} sent and answered, {_failed} failed, {_skipped} skipped "
                        f"of {len(targets)} — {_mb(_b - _up_bytes0)} MB in "
                        f"{_r - _up_reqs0} request(s)"),
               **{"event.outcome": "success" if _complete else "failure",
                  "aci.upload_complete": _complete,
                  "aci.sections_total": len(targets),
                  "aci.sections_done": len(sections),
                  "aci.sections_ok": _ok,
                  "aci.sections_failed": _failed,
                  "aci.sections_skipped": _skipped,
                  "aci.upload_bytes": _b - _up_bytes0,
                  "aci.upload_mb": _mb(_b - _up_bytes0),
                  # Requests, not calls: a retried section uploaded its images twice, and
                  # the gap between this and sections_ok is what a throttled region cost.
                  "aci.upload_requests": _r - _up_reqs0,
                  "aci.upload_total_bytes": _b,
                  "aci.upload_total_mb": _mb(_b),
                  "event.duration_s": round(time.time() - _up_t0, 1)})
    log(f"  upload: {_ok}/{len(targets)} section(s) answered, "
        f"{_mb(_b - _up_bytes0)} MB in {_r - _up_reqs0} request(s)"
        + ("" if _complete else f" — INCOMPLETE, {_failed} failed"))

    elapsed = round(time.time() - started, 1)
    # One string when both passes used the same model (the common case), a descriptive
    # pair when they did not -- a single "model" field can't honestly claim one model did
    # both jobs once tables and text run under different ones.
    report_model = model if model == text_model else f"{model} (tables) + {text_model} (text sections)"
    report = {
        "mode": "section", "model": report_model,
        "models_used": {"table_sections": model, "text_sections": text_model},
        "seconds": elapsed,
        "started_at": round(started, 1), "sections": sections,
        "sections_total": len(sections),
        "sections_accepted": sum(1 for s in sections if s.get("ok")),
        "sections_kept_stage3": sum(1 for s in sections if not s.get("ok")),
        "sections_with_tables": sum(1 for s in sections if s.get("kind") == "table"),
        "sections_text_only": sum(1 for s in sections if s.get("kind") == "text"),
        "headings_added": sum(s.get("headings_added") or 0 for s in sections),
        "urls_linked": sum(urls_by_file.values()),
        "urls_linked_by_file": urls_by_file,
        "usage": {"input_tokens": tok_in, "output_tokens": tok_out,
                  "total_tokens": tok_in + tok_out,
                  "cost_usd": round(sum(d["usd"] for d in by_model.values()), 4),
                  # Present only when something was billed at a rate PRICES does not
                  # know, so a $0.0000 can never be mistaken for a free run.
                  **({"unpriced_models": sorted(UNPRICED)} if UNPRICED else {}),
                  "by_model": {k: {**v, "usd": round(v["usd"], 4)} for k, v in by_model.items()}},
    }
    (stage4_dir / "stage4_report.json").write_text(json.dumps(report, indent=2))
    (stage4_dir / "STAGE4_REPORT.md").write_text(
        "# Stage 4 — AI post-processing (section mode)\n\n"
        + f"- **model**: `{model}`\n"
        + f"- **sections**: {report['sections_total']} "
          f"({report['sections_accepted']} accepted, {report['sections_kept_stage3']} kept)\n"
        + f"- **headings added**: {report['headings_added']}\n"
        + f"- **URLs linked**: {report['urls_linked']}\n"
        + f"- **cost**: ${report['usage']['cost_usd']:.4f} "
          f"({report['usage']['total_tokens']:,} tokens)\n\n## Sections\n\n"
        + "\n".join(f"- {'✓' if s.get('ok') else '✗'} {s['file']} — "
                    f"{s.get('tables_before')}→{s.get('tables_after')} tables, "
                    f"{s.get('rows_before')}→{s.get('rows_after')} rows, "
                    f"+{s.get('headings_added', 0)} headings — {s['reason']}"
                    for s in sections) + "\n")
    _mark_ai_processed(stage4_dir.parent, report)
    _progress(stage4_dir.parent, "done", accepted=report["sections_accepted"],
              cost_usd=report["usage"]["cost_usd"])
    # After the report and the ai_processed flag are on disk — the splitter's gate reads
    # both to decide eligibility, so it cannot run before they exist.
    if subchunk_after:
        t5 = time.time()
        report["subchunk"] = _run_subchunk(stage4_dir.parent, log=log)
        report["subchunk"]["seconds"] = round(time.time() - t5, 1)
        (stage4_dir / "stage4_report.json").write_text(json.dumps(report, indent=2))
    _record_timings(stage4_dir.parent, stage4_ai=elapsed,
                    stage5_subchunk=(report.get("subchunk") or {}).get("seconds") or 0.0)
    return report


# Sections whose loss is not worth a red flag. A cover/disclaimer page failing to get an AI
# pass costs nothing regulatory, and flagging it drowns the sections that do matter under
# noise the reviewer learns to ignore — which is worse than not flagging anything at all.
_IGNORED_SECTION_RE = re.compile(r"front-matter", re.IGNORECASE)


def stage4_failed_sections_from_report(report: dict, known_files: set[str] | None = None) -> list[dict]:
    """The `failed_sections` list a parsed stage4_report.json yields on its own — the half of
    stage4_section_health that needs no filesystem access, so a caller reading the report from
    S3 (the Core Index Extraction tab's one-document drill-down) gets the same answer a local
    disk read would, without a LIST call to enumerate Stage 1's section files.

    `known_files`, when given, is the FULL set of section files Stage 1 actually produced —
    passing it catches a section missing from the report entirely (killed mid-run), which
    `sections[].ok` alone cannot see. Omitted, only sections the report DOES mention are
    checked (still catches every ok:false — the killed-mid-run gap just isn't checked)."""
    sections = report.get("sections") or []
    failed = [{"file": s.get("file"), "reason": s.get("reason") or "failed, no reason recorded"}
              for s in sections
              if not s.get("ok") and not _IGNORED_SECTION_RE.search(s.get("file") or "")]
    if known_files is not None:
        reported = {s.get("file") for s in sections}
        for missing in sorted(known_files - reported):
            if _IGNORED_SECTION_RE.search(missing):
                continue
            failed.append({"file": missing,
                           "reason": "never attempted — absent from stage4_report.json entirely"})
    return failed


def stage4_section_health(job_dir: Path) -> dict | None:
    """Stage 4's own per-section health, for a green/red "did this document's AI pass
    actually finish" signal — the pipeline monitor's job list and the Core Index Extraction
    tab both read this rather than each growing their own copy of the same disk-reading
    logic.

    None when Stage 4 does not apply to this document at all:
      * it never ran here (no 04_stage4_ai/ at all — most documents, ACI_STAGE4_AI_ENABLED
        is opt-in);
      * it is the summary-AI product-rule route (`summary_ai_rule.json` present). That route
        writes ITS OWN 04_stage4_ai/ (see summary_ai_extract.extract_document) as the primary
        extraction, not a repair pass over a Stage 1-3 tree — it has no stage4_report.json in
        this shape, and treating its absence as "started and never finished" would flag every
        summary-AI document as stuck for a report it was never going to write.
    None is "not applicable", not "healthy" — the caller shows blank for it, the same rule
    orphan-table and route badges already follow for a document with nothing to report.

    A `04_stage4_ai` directory with no `stage4_report.json` inside it means Stage 4 STARTED
    (the directory is created before anything is written into it) and never finished writing
    its report — killed, crashed, or wedged on a call that never returned. That is the "just
    didn't happen at all" half of the signal this function exists to give; `ok: false` on an
    individual section (a timeout, a throttle that exhausted its retries, a validation error)
    is the other half, and both come back through the same `failed_sections` list so a caller
    never has to ask which kind of red this is before deciding to look.

    A third gap `ok: false` cannot see: a section Stage 1 produced that the report never
    mentions AT ALL. A process killed between sections leaves later ones with no entry
    whatsoever rather than a failed one — silence, not a recorded failure — so this is
    checked separately against what Stage 1 actually produced, not inferred from the report
    alone.

    Front matter is excluded from `failed_sections` either way (see _IGNORED_SECTION_RE):
    losing it is not the kind of thing this signal exists to catch."""
    job_dir = Path(job_dir)
    if (job_dir / "summary_ai_rule.json").exists():
        return None
    stage4_dir = job_dir / "04_stage4_ai"
    if not stage4_dir.exists():
        return None
    try:
        report = json.loads((stage4_dir / "stage4_report.json").read_text())
    except (OSError, ValueError):
        return {"status": "incomplete", "failed_sections": [],
                "reason": "Stage 4 started (04_stage4_ai exists) but never wrote its "
                          "report — crashed, was killed, or is still stuck mid-run"}
    stage1_dir = job_dir / "01_stage1_extract"
    # Walked, and keyed the same way the report keys its sections (path below the stage
    # dir) — the two sets are differenced against each other, so a top-level-only listing
    # here against a walked report would call every nested section "never attempted".
    known_files = ({section_key(stage1_dir, p) for p in section_files(stage1_dir)}
                   if stage1_dir.exists() else None)
    failed = stage4_failed_sections_from_report(report, known_files)
    return {"status": "failed" if failed else "ok", "failed_sections": failed}
