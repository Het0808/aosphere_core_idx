#!/usr/bin/env python3
"""product_rules.py — per-product extraction overrides.

Documents inside one product are written to a shared template, so a rule that is
wrong in general can be exactly right for one product. This is where such a rule
lives, keyed by the product directory name, so it is one place to read rather
than a flag someone has to remember to pass.

Only add a rule here with a reason and the document it was measured on. A rule
that silently changes the shape of an extraction is worse than no rule.

    SECTION_DEPTH   how many levels of NESTING the tree may build.
                    0 = flat: every level-1 section becomes ONE file holding
                    everything beneath it. 3 = the pipeline default.

                    This maps to pdf2mdtree's --depth, and the number of outline
                    LEVELS that become their own node is depth + 1 (a level-1
                    heading is a node at depth 0; with --depth 3 a level-4
                    heading still gets its own file and only level 5 collapses).
                    check_heading_hierarchy needs that same number to know what
                    the tree was TRYING to build, which is why Stage 1 now
                    records it — see build_depth().
"""
from __future__ import annotations

import json

DEFAULT_SECTION_DEPTH = 3

# ---- 124 Marketing Restrictions (Asset Management) ---------------------------
# FLAT, level-1 sections only: 1..11 plus the appendix, each as a single chunk.
#
# Why: this product prints its subsections as ROWS INSIDE a page-spanning
# questionnaire table. Stage 1 can only cut a table at a page boundary, and two
# subsections routinely start on the same page (8.9 and 8.10 both begin on p60 of
# ADGM__170680, 8.11 and 8.12 both on p64), so the subsection boundary is not
# reachable. MinerU then re-merges the pages Stage 1 did manage to split, and the
# combine step correctly refuses to render the same rows twice — so the
# subsections arrive as headings with no body. Measured on ADGM__170680: 5 of 39
# sections published empty, 4 of them under section 8.
#
# Collapsing to level 1 removes the failure rather than papering over it: there is
# no subsection node left to be empty, and the rows stay with the section that
# owns them. Measured on Australia__181814 at depth 0: 22 files, ZERO hollow
# sections, against 12 subsections of section 8 that a depth-3 build cannot fill.
#
# The cost is real and should be stated: a search can no longer reach "8.9
# Arranging" as its own node, and section 1 arrives as one 11,000-word file
# because its Executive Summary is 22 pages. This is the right trade only while
# the subsection rows cannot be split — see the note in check_content_localized
# about cutting the MERGED table on its heading rows, which would make depth 3
# viable again and make this rule unnecessary.
SECTION_DEPTH = {
    "124_Marketing_Restrictions_-_Asset_Management": 0,
}

# ---- CLAUSE-LONG TABLE RUNS ---------------------------------------------------
# Trust the PAGE GROUPING over Stage 1's own dry-run stitch, for this product only.
#
# reconstruct_multipage_tables validates a multi-page run by stitching the rows
# itself and rejecting the run if any stitched cell exceeds MAX_STITCHED_CELL.
# Those stitched rows are then DISCARDED — MinerU renders the real table in Stage
# 2 and all that survives is which pages belong to one run. So on a document where
# the stitch degenerates, a correct page grouping is vetoed by the quality of an
# artifact nothing downstream ever reads.
#
# Measured on Australia__181814, the three question-table clauses, every other
# condition passing (>=3 rows, 100% of rows keyed):
#
#   clause 6  p54-82    36 rows   maxcell 42,089   REJECTED
#   clause 7  p83-105   89 rows   maxcell 10,848   REJECTED
#   clause 8  p106-129  99 rows   maxcell  4,469   accepted -> ONE 100-row table
#
# Clause 8 passed not because it is more genuinely one table but because its pages
# happen to share a column geometry (4 near-identical bound sets across 24 pages).
# Clause 6 has 9 distinct geometries across 29 pages — some pages' left column
# starts at x=354 or x=128 instead of x=56 — so one canonical bound set puts their
# text right of the first boundary, every band reads as a blank-first-cell
# continuation, and twenty of them accrete into a single 42k cell.
#
# Scoped to this product because the veto is doing real work elsewhere: it is the
# only thing stopping two genuinely different adjacent tables from being merged
# into one run, and nothing here establishes that relaxing it is safe in general.
# In this product every clause IS one questionnaire table by construction, so the
# grouping is known good and the stitch is the only thing in doubt.
#
# The row-count and keyed-ratio conditions still apply — this relaxes ONE test.
CLAUSE_TABLE_RUNS = {
    "124_Marketing_Restrictions_-_Asset_Management",
}


# ---- STAGE-4 SUB-CHUNKING -----------------------------------------------------
# Which products get their AI-post-processed sections split into a nested tree, one
# file per numbered sub-section (6.1, 6.2, 6.3 beneath section 6).
#
# Scoped to this product, and deliberately not a default. It is only meaningful where
# a section IS one questionnaire table carrying numbered sub-sections inside it, which
# is true here by construction and is what stage 4 lifts out into `###` headings. A
# product whose sections are ordinary prose has no such split points, and running this
# over it would produce one folder holding one file.
#
# Gated on the AI pass having actually run as well — see subchunk_eligible(). Stage 3
# leaves those labels inside the tables, so there is nothing to split without stage 4.
SUBCHUNK_PRODUCTS = {
    "124_Marketing_Restrictions_-_Asset_Management",
}


# ---- MINERU EFFORT ------------------------------------------------------------
# MEDIUM, and pinned here deliberately rather than left to the global default, because
# `high` is actively WRONG for this product and a future default change must not
# silently pick it up.
#
# Measured on Australia__181814, identical Stage 1 (12 clause-long regions) through
# three backends, reading MinerU's own raw output:
#
#   backend/effort     blocks  tables  words in table HTML  words as loose text  coverage
#   hybrid / medium       728      35               40,749                  447     98.9%
#   hybrid / HIGH         977      43               25,598                9,027     72.6%
#   vlm-engine            977      43               25,598                9,038     72.6%
#
# hybrid/high and vlm-engine are the SAME output to within 11 words — `high` is not a
# refinement of the geometry path, it routes to the VLM behaviour. And that behaviour
# fails on this document: it classifies table rows as prose wherever the ruling is
# irregular (10-sources-of-law 12 rows -> 5, 13-marketing-selling 55 -> 43), spilling
# 9,000 words out of the tables while reporting failed=0. The one perfectly regular
# 24-page table survives on all three, which is why a spot check would miss this.
#
# hybrid/medium reads the ruled geometry and keeps the grid even when the rules are
# ragged, which is exactly what this product's questionnaire tables need.
MINERU_EFFORT = {
    "124_Marketing_Restrictions_-_Asset_Management": "medium",
}


def mineru_effort(product: str | None, override: str | None = None) -> str | None:
    """Effort for this product. An explicit CLI override always wins."""
    return override or MINERU_EFFORT.get((product or "").strip())


def section_depth(product: str | None) -> int:
    """Nesting depth to build this product's trees at."""
    return SECTION_DEPTH.get((product or "").strip(), DEFAULT_SECTION_DEPTH)


def clause_table_runs(product: str | None) -> bool:
    """Should a multi-page run survive a degenerate dry-run stitch? See above."""
    return (product or "").strip() in CLAUSE_TABLE_RUNS


# Products whose EMBEDDED OUTLINE is authoritative, so the printed-contents-page
# disagreement check does not apply to them.
#
# Data Privacy and Shareholding Disclosure bookmark at a finer grain than their printed contents
# page lists — 509 bookmarks against 243 printed sections on a 361-page DP survey — and the
# outline-vs-printed check reads that as "paragraph-level, not section-level" and marks the
# structure improper. Measured consequence: every DP and SD document was routed into toc_rescue
# and then the whole-document MinerU tier, when the FIRST pass had already produced the accepted
# result (Netherlands 174642 scored pass 94.4 with toc 100 before the check changed; the same tree
# now scores toc 45). It is a regression on the two products that were already correct.
#
# The finer bookmarks are how these documents are written, not a defect in them. Their outline is
# trusted; other products keep the check, which is what catches a Word export that bookmarked
# every styled paragraph.
OUTLINE_AUTHORITATIVE = {"155_Data_Privacy", "104_Shareholding_Disclosure"}


def outline_is_authoritative(product: str | None) -> bool:
    """True when this product's embedded outline should be trusted over its printed contents page."""
    return bool(product) and product in OUTLINE_AUTHORITATIVE


# ---- REPAIR BY REBUILDING, NOT BY PRUNING ------------------------------------
# Products whose bad outline must be repaired from the PRINTED CONTENTS PAGE rather than by
# deleting its prose entries in place (rescue_outline's `prune` engine, --no-prune to skip).
#
# Both engines fix "an outline made of body text". They differ in what they do with the LEVELS
# of the entries that survive, and for a FLAT product that difference is the whole ballgame:
# SECTION_DEPTH 0 emits one file per LEVEL-1 heading, so a wrong level is a wrong chunk.
#
#   prune  deletes prose entries and keeps the embedded outline's own nesting (_legal_levels only
#          shifts levels and caps jumps; it does not re-derive hierarchy). This product's
#          embedded outlines put "6.1", "8.9" at LEVEL 1, as siblings of "6.", "8." — so 35 of
#          52 surviving entries came out level 1.
#   text   parses the contents page the document PRINTS and rebuilds, so levels come from the
#          printed structure: the numbered clauses at level 1, their "6.1"/"8.9" children at 2.
#
# Measured on Australia__183509, same PDF, same day:
#   prune          335 -> 52 entries, levels {1:35, 2:13, 3:3, 4:1}
#                  34 files, 37 deferred tables, uniqueness 71.8
#   text (rebuild)  41 entries, 41 verified, monotonic, span 96.7%, levels {1:18, 2:23}
#                  28 files, 16 deferred tables, uniqueness 90.5
# The level-1 boundaries in the prune build cut INSIDE the page-spanning questionnaire tables,
# which is the exact failure SECTION_DEPTH 0 exists to prevent: more than twice the deferred
# regions, and the same table content emitted under both "6." and "6.1".
#
# This is a RESTORATION, not a new behaviour. The whole product was extracted on a branch where
# the prune engine did not exist (no prune_prose_entries in rescue_outline.py at f2da755), so
# every repair went through the rebuild and Bermuda__166524 scored pass 92.1 with levels
# {1:16, 2:23}. 27 of this product's 105 documents have a prose-heavy embedded outline and would
# otherwise all switch engines, including ADGM__170680 — the document the printed-TOC rescue was
# written for.
#
# NOT A REVERT OF a781419 ("an outline made of body text is not a structure"), which added prune
# FOR THIS DOCUMENT FAMILY. The defect it fixed — Australia's Executive Summary emerging as 73
# pseudo-sections named after sentence fragments — is fixed by the rebuild too: the archived
# engine=text build has no sentence-fragment section and no separate summary file at all, because
# "1.4 Executive Summary" comes back as a level-2 child of "1. Background". Same 73 -> 1 outcome,
# reached by rebuilding rather than deleting. That commit measured prune against the UNREPAIRED
# outline (110 files -> 42), never against the rebuild (23), so the levels were never compared.
#
# The rebuild is not strictly better: it fragments the cover title into junk top-level files
# ("04-into.md", "05-australia.md") where prune starts cleanly at "1. Background". That is
# front-matter noise and it does not split a table; wrong clause levels do, which is why the
# trade goes this way here. Bermuda carries the same cover fragments and still scores pass 92.1.
#
# The better end-state is to re-derive levels from the clause numbering after pruning, so prune
# keeps its own fix AND lands the right hierarchy — at which point this rule can be retired. That
# touches every product prune runs on (36 documents corpus-wide as of 2026-08-21, up from the 3
# a781419 measured), so it needs its own before/after rather than riding along with this one.
#
# Scoped to this product because the trade is only clearly right where the printed contents page
# is reliable AND the tree is flat. Elsewhere prune is cheaper and keeps offsets the rebuild has
# to re-derive.
REBUILD_OUTLINE_FROM_PRINTED_TOC = {
    "124_Marketing_Restrictions_-_Asset_Management",
}


def rebuild_outline_from_printed_toc(product: str | None) -> bool:
    """True when a bad outline must be REBUILT from the printed contents page, not pruned."""
    return bool(product) and product in REBUILD_OUTLINE_FROM_PRINTED_TOC


# A group label printed alone on the line ABOVE a heading -- "APPENDIX 2" over
# "DISCLAIMERS: CLOSED-ENDED FUND" -- is part of that heading, not a paragraph of the
# section before it. See pdf2mdtree's --group-labels.
#
# Measured on this product: 385 of 385 such labels are followed by their title on the same
# page, 383 of them were being emitted as the last line of the PREVIOUS appendix's content,
# and 334 headings across 89 of its 105 documents carried no appendix label at all -- so an
# appendix chunk said nothing about which appendix it was, and its heading claimed a number
# ("2 Disclaimers: ...") that body section 2 already owned.
#
# Scoped to this product because that is the only place the shape has been measured. The
# rule reads on any document the same way -- a bare "APPENDIX 2" line is not ambiguous --
# but "reads plausibly" is not evidence, and the other products print their annexes
# differently enough that turning this on for them is a change nobody has looked at. Add a
# product here once its labels have actually been counted.
#
# 154_Marketing_Restrictions WAS counted, and is deliberately NOT here -- the rule makes it
# worse. That product prints "APPENDIX 1" as a level-1 heading in its own right, with the
# descriptive title as a level-2 child beneath it, so folding the label into that child and
# dropping the now-duplicate parent removes the appendix's OWN node and the child reattaches
# to the preceding level-1 section. A/B on its three corpus documents, --depth 3, the flag
# the only difference, counting top-level appendix sections:
#
#   Abu Dhabi Global Market (ADGM)__170647    4 -> 0   all four land under "... Sanctions"
#   Argentina__176514                         4 -> 0   likewise
#   Australia__181815                         5 -> 5   improves: 20-appendix-1 -> ...-products
#
# Australia is the one document there shaped like 124 -- an empty label heading -- which is
# why it gains. The other two lose the structure the rule exists to protect: an appendix
# chunk filed under "Part A Survey Questions: Sanctions" cites worse than a stranded label
# reads. Note also that the stranded labels visible in out/corpus/154's stage 1 output are
# STALE and not evidence for turning this on: that run matched 19 of 43 headings, current
# code matches 29 of 29 and already gives each appendix its own node.
#
# Enabling it here needs the drop of the duplicate heading to be conditional on that heading
# having no content of its own. That is a change to shared logic and would need 124's A/B
# re-run to show it still folds there.
GROUP_LABEL_HEADINGS = {
    "124_Marketing_Restrictions_-_Asset_Management",
}


def group_label_headings(product: str | None) -> bool:
    """True when a "APPENDIX n" / "SCHEDULE n" line above a heading joins that heading."""
    return bool(product) and product in GROUP_LABEL_HEADINGS


def product_for_job(job_dir, up: int = 3) -> str | None:
    """The product a job belongs to, read from its own corpus_meta.json.

    Asked for by name rather than inferred from the path: a job dir can be copied,
    renamed or suffixed for an experiment ("Australia__181814 [FLAT depth-0]") and
    the meta file still records what it actually is.

    Walks UP a few levels because sub-runs write into a subdirectory of the job and
    inherit its product: rescue_outline builds into <job>/fallback/, so a lookup
    that only checked the immediate directory found nothing and silently fell back
    to the pipeline default — rebuilding a flat product's document at depth 3, which
    is the exact failure this lookup exists to prevent."""
    from pathlib import Path
    d = Path(job_dir).resolve()
    for _ in range(max(up, 1)):
        try:
            return (json.loads((d / "corpus_meta.json").read_text()) or {}).get("product")
        except (OSError, ValueError):
            if d.parent == d:
                break
            d = d.parent
    return None


def expected_build(product: str | None) -> dict:
    """What a tree for this product SHOULD have been built with."""
    return {"build_depth": section_depth(product),
            "clause_tables": clause_table_runs(product)}


def build_matches_rule(product: str | None, build_depth, clause_tables) -> list[str]:
    """-> a list of human-readable mismatches between how a tree was BUILT and the
    rule for its product. Empty when they agree, or when the build recorded nothing
    (a tree from before Stage 1 started writing build_depth — unknowable, not wrong).

    This exists because the rule is applied by whichever entry point ran the
    extraction, and a document rebuilt through a different one comes out with the
    default shape and no complaint. The scorecard can catch that after the fact
    because Stage 1 records what it did."""
    if build_depth is None:
        return []
    want = expected_build(product)
    out = []
    if build_depth != want["build_depth"]:
        out.append(f"built at depth {build_depth}, but {product or 'this product'} "
                   f"expects depth {want['build_depth']}")
    if bool(clause_tables) != want["clause_tables"]:
        out.append("clause-long table runs were "
                   + ("NOT used, but this product expects them"
                      if want["clause_tables"] else "used, but this product does not expect them"))
    return out


def census_levels(build_depth: int | None) -> int | None:
    """How many outline LEVELS the tree was trying to give their own node.

    None when Stage 1 did not record a depth — an older tree, where the caller
    must keep whatever default it already used rather than guess and start
    reporting sections as missing that were never expected to exist."""
    return None if build_depth is None else build_depth + 1

# ---- DOES `sectioning` SET THE VERDICT? ---------------------------------------
# ADVISORY EVERYWHERE EXCEPT 124. `sectioning` asks "did the tree build the sections
# the document promises", comparing the PDF's outline against the nodes on disk. That
# question is only answerable when the expected node count is known, which is true for
# 124 (a product rule fixes the build to flat depth-0, so every level-1 outline entry
# must become exactly one file) and guesswork elsewhere, where a depth-3 build
# legitimately collapses deeper levels.
#
# Measured cost of letting it gate everywhere — Germany (Data Privacy)__180656, 328
# pages, extracted cleanly on the first pass:
#
#   completeness 94.3   placement 99.0   fidelity 98.0   toc 100.0   sectioning 60.8
#
# `sectioning 60.8` alone failed the document AND tripped the fallback entry rule
# (sectioning < 70), so a full MinerU re-parse ran for 10.9 minutes. The re-parse then
# promoted a tree whose `fidelity` is not scored at all and whose `toc` reads 45.0
# against a pre-flight that had verified 247 of 247 entries — i.e. the document was
# judged worse, re-extracted, and came back measured on FEWER dimensions than it
# started with. Germany__74039 (6 pages) was dragged through the same chain while
# already passing at 97.6.
#
# So outside 124 it is still COMPUTED and shown — it is useful description, and the
# section map it drives is worth reading — it just does not set the verdict and does
# not trigger the chain.
SECTIONING_GATES = {
    "124_Marketing_Restrictions_-_Asset_Management",
}


def sectioning_gates(product: str | None) -> bool:
    """May `sectioning` set the verdict / trigger the fallback chain for this product?"""
    return product in SECTIONING_GATES


# ---- EXTRACTED BY THE SUMMARY AI PIPELINE, NOT BY STAGES 1-3 -------------------
# Scoped to ONE FOLDER inside a product, not the whole product — the first rule here that
# needs that granularity, hence the {product: {jurisdiction}} shape rather than a flat set.
# 124's survey documents and the summaries filed beside them are different documents
# written to different templates; a rule right for one is wrong for the other.
#
# These are 5-10 page SUMMARIES of a jurisdiction's marketing restrictions, not the 76-163
# page questionnaire surveys the rest of the product holds. Nothing that makes the normal
# route correct applies to them: no page-spanning questionnaire table to keep intact, no
# clause numbering to preserve, and at 10 pages no hierarchy worth reconstructing.
#
# WHAT THEY DO HAVE is structure that is DRAWN rather than written. A summary is built out
# of coloured bars: a dark teal bar (#005C54) is a section, a lighter teal or pale mint one
# (#369485 / #DAEEEA) is a sub-section, and a paragraph's verdict is a small coloured
# square to its left — an EMBEDDED IMAGE, so no text extractor can see it at all. Nothing
# in the text layer says "this line is a heading"; what says so is the bar printed behind
# it. That is why the geometry path cannot get these documents right in principle, not just
# in practice, and it is the whole reason this rule exists.
#
# SUPERSEDES the full-VLM-MinerU route this rule used to name (removed 2026-09-10). That
# measurement stands and is kept here because it is what ruled out the NORMAL route, and a
# future document type may need it again — on both documents in Summary MRAM, 2026-09-04,
# normal route against a forced full VLM parse:
#
#   document            route            gate    worst  completeness  uniqueness  sectioning
#   Argentina  (5pp)    hybrid full      pass     93.8          93.8        53.7       100.0
#   Argentina  (5pp)    VLM full         pass     98.5          98.5        65.0       100.0
#   Australia (10pp)    hybrid 1/2/3     review   78.8          78.8        24.5       100.0
#   Australia (10pp)    VLM full         review   80.0          97.2        74.2        80.0
#
# The VLM route recovered content (Australia completeness +18.4, uniqueness +49.7) but paid
# for it in structure: sectioning 100.0 -> 80.0, because full-MinerU derives structure from
# the PDF's bookmark outline and ignores section_depth(). Australia stayed at `review`
# either way, just for a different reason — so that route never made these documents pass.
#
# THE AI PIPELINE replaces it because it addresses the structure defect rather than trading
# against it: it reads the bars. Measured on SUMMARY__Germany__33748 (9pp), 2026-09-10:
#
#   completeness  98.1   4,854 text-layer words -> 4,764 written; the 91 short are exactly
#                        the nine running footers the prompt is told to omit
#   structure    100.0   all 10 heading bars found in the PDF geometry got a heading, at
#                        three real levels (6 x ## / 4 x ### / 2 x ####); 20 status squares
#                        captured, which the text layer cannot represent at all
#   fidelity     100.0   ONE word in the transcript that is not in the text layer
#   typography     0.0   0 of 18 curly quotes kept their exact glyph — ADVISORY, see below
#
# IT COSTS MONEY, unlike every other rule in this module: one Bedrock call per document,
# ~$0.026 per page, $0.23 for Germany's 9 pages, so roughly $15-20 for all 88 summaries at
# Sonnet 5's rate (which product_rules cannot confirm — see ai_postprocess.PRICES). A
# corpus run that reaches this rule is a corpus run that spends. ACI_SUMMARY_AI=0 turns the
# route off and makes the documents SKIP rather than silently taking a route that was
# measured as wrong for them.
#
# KNOWN OPEN DEFECT, recorded so it is not rediscovered: the model writes the PDF's curly
# quotes and apostrophes as straight ones (U+201C/U+201D/U+2019 -> U+0022/U+0027). The
# marks are all PRESENT, in the right places around the right words -- it is the glyph that
# changed -- which is why word coverage reads 100 and cannot see it. See
# summary_ai_extract.punct_losses, which locates every rewrite by page and phrase.
#
# Reported as a critical FINDING but scored ADVISORY, so it does not set the verdict. That
# is deliberate and the reasoning is on the dimension itself: a gate that scores a
# flattened quote the same as a dropped section stops discriminating, and across 88
# documents every row would read `fail`. ai_postprocess.strict_stream's standard -- a
# rewritten quotation mark in a legal document is a content change -- was written for a
# REPAIR pass, which can discard a bad reply for free because it still holds the original
# characters. A transcription has no such fallback. Fix is a prompt change, not a code one.
#
# Matched on the document's own metadata, not the jurisdiction FOLDER it happens to sit
# in. The folder was the signal until 2026-09-10: the source tree was renamed "Summary
# MRAM" -> "SUMMARY" on 2026-09-09 and grew from 2 documents to 88 while extractions
# already on disk carried the old name, which needed a code patch (keeping both names) to
# survive. OPINIONCONTENTTILENAME is a field on the opinion's content tile in the source
# database (see docs/MSSQL_ANALYSIS.md, table OpinionContentTileDetails) — nobody renames
# a database row by accident the way a folder gets renamed.
#
# The match is CONTAINS, not exact equality — the tile name is not always the bare word.
# United States files two of these documents under one opinion, tiled "SEC Summary" and
# "CFTC Summary" rather than plain "Summary"; an exact-equality match (the original form
# of this rule) missed both and let them fall through to the Stage 1-3 survey route.
# Measured against the real 124 corpus after the SUMMARY/ folders were merged into their
# jurisdictions on 2026-09-10: 86 of 88 summary documents carry the bare tile "Summary",
# the United States pair carry the two above, and every one of the 106 survey documents'
# tiles is simply absent (None) — nothing else in that corpus contains the word.
AI_PIPELINE = {
    "124_Marketing_Restrictions_-_Asset_Management",
}
AI_PIPELINE_CONTENT_TILE = "summary"

# The module that owns the route. Named rather than imported here so product_rules stays
# import-free of anything heavy -- it is read by run_corpus at routing time.
AI_PIPELINE_MODULE = "summary_ai_extract"


def ai_pipeline(product: str | None, content_tile: str | None) -> bool:
    """True when this document is extracted by the summary AI pipeline, not by stages 1-3.

    `content_tile` is OPINIONCONTENTTILENAME for this one document (see
    content_tile_name()) — a per-document database field, so the route follows the
    document wherever its folder sits or gets renamed to.

    Matched by CONTAINS, casefolded — "SEC Summary" and "CFTC Summary" both qualify,
    not just the bare "Summary" tile most of these documents carry. See AI_PIPELINE.
    """
    if (product or "").strip() not in AI_PIPELINE:
        return False
    return AI_PIPELINE_CONTENT_TILE in str(content_tile or "").strip().casefold()


def content_tile_name(pdf, doc_id) -> str | None:
    """OPINIONCONTENTTILENAME for one document, read from the Doc_metadata.json beside its PDF.

    The source corpus ships one Doc_metadata.json per folder, listing every document in
    it, keyed by FILENAME and DOCID (see lib_docnames.py, which indexes a whole tree of
    these for the gallery's display name). This reads just the one row for `doc_id`, since
    a routing decision is made for one document at a time.
    """
    from pathlib import Path
    meta = Path(pdf).parent / "Doc_metadata.json"
    try:
        rows = json.loads(meta.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None
    for r in (rows if isinstance(rows, list) else [rows]):
        if not isinstance(r, dict):
            continue
        fname, did = r.get("FILENAME"), r.get("DOCID")
        if (fname and str(fname) == str(doc_id)) or (did and str(did) == str(doc_id)):
            return r.get("OPINIONCONTENTTILENAME")
    return None


# ---- APPENDIX CHUNK INTEGRITY -------------------------------------------------
# Which products are checked for an appendix that never became a chunk of its own.
#
# The memo prints its appendix divisions as a bare ALL-CAPS label over the title:
#
#     **APPENDIX 3**
#     **DISCLAIMERS: INVESTMENT MANAGEMENT & ADVISORY SERVICES**
#
# Neither line is a markdown heading, so the split depends entirely on the outline or
# the font heuristic having found the TITLE. Where it did not, the whole appendix stays
# inside the appendix above it and nothing downstream notices: measured over the 54-job
# 16-09-26 corpus, Iceland, Malta, Kazakhstan, Philippines and Singapore each lost one
# or two appendices this way and all five gated PASS at 93-97.
#
# Product-scoped because the ALL-CAPS-label-over-title convention is 124's, and the
# discriminator leans on it. 154's Australia__181815 shows the same shape, but the
# label there is title-case in places and its trees were not audited here, so it is
# left out rather than guessed at. A product whose documents quote an external
# instrument with its own appendices (124's Malaysia quotes the Malaysian SC's
# "Foreign Funds Guidelines", Appendices 1-4, in title case) is exactly what the
# case-sensitivity below exists to keep out.
APPENDIX_CHUNK_PRODUCTS = {
    "124_Marketing_Restrictions_-_Asset_Management",
}


def appendix_chunk_checked(product: str | None) -> bool:
    """True when a swallowed "APPENDIX n" division is scored for this product."""
    return bool(product) and product in APPENDIX_CHUNK_PRODUCTS


# ---- OUTLINE TEXT COVERAGE ----------------------------------------------------
# Which products are checked for an outline heading whose TEXT reaches no heading in
# the shipped tree.
#
# Distinct from heading_hierarchy's census, which asks whether each promised section
# became a NODE and is capped at census_levels() depth. This asks the weaker, deeper
# question — does the title appear as a heading ANYWHERE in the tree — so it sees the
# sub-sections the census never promises. Belgium 163341 ships without "4.5 Provision
# of Non-Core Services by an AIFM", "4.6 Small AIFMs" or "4.7 Sub-funds"; its census
# promises 17 sections and reports 0 missing.
#
# Product-scoped for the same reason the appendix check is: measured over every tree on
# disk, MRAM flags 9% of jobs (27 headings over 279), while repoAnalytics, SLAnalytics,
# netalytics-IFI, E-Signatures and ICMA flag 100% of theirs, 12-22 headings each. Those
# are flat single-heading trees where the question means something different, and they
# have not been audited. Turning a product on here needs that audit first.
OUTLINE_COVERAGE_PRODUCTS = {
    "124_Marketing_Restrictions_-_Asset_Management",
}


def outline_coverage_checked(product: str | None) -> bool:
    """True when an outline heading absent from the tree is scored for this product."""
    return bool(product) and product in OUTLINE_COVERAGE_PRODUCTS
