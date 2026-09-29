#!/usr/bin/env python3
"""check_toc_quality.py — is this document's TABLE OF CONTENTS fit to build a tree from?

Every other check asks whether the extracted content is right. This one asks
whether the thing Stage 1 built the structure FROM was trustworthy in the first
place, because that failure is invisible downstream: pdf2mdtree treats "the PDF
has bookmarks" as "the bookmarks describe the document", and when a publisher
bookmarks only the appendix the extractor still produces a complete, correct,
utterly flat tree. Word coverage is 99%; the document has no sections.

Curaçao is the case that motivates this. Its outline is TWO entries, both on page
37 of 40, so the real spine (A. Substantial Shareholding, B. Sensitive
Industries, ...) never becomes structure — but its printed contents page lists all
30 sections and verifies perfectly. The rescue that would fix it already exists
(rescue_outline.py); what was missing was any signal that the document needs it.
Completeness cannot supply that signal: front-matter recovery already pulls the
un-bookmarked pages in, so the document scores 92 and never qualifies.

    proper    the embedded outline plausibly describes the document — trust it
    lacking   no outline at all; structure was inferred from font size
    improper  an outline exists but does not describe the document (bookmarks
              start deep into the document, or are internal anchor names that
              appear nowhere in the text)

`printed_toc` is reported alongside the status: when a printed contents page
parses AND verifies against the document, a better structure is available and the
fallback chain can adopt it. That is what makes a bad status actionable rather
than merely bad news.

Usage:
    python scripts/check_toc_quality.py out/corpus/<product>/<job>
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (  # noqa: E402
    BOLD, DIM, GREEN, RED, YELLOW, banner, resolve_tree_pdf, run_cli,
)


def _rescue_reader():
    """rescue_outline's TOC reader, imported LAZILY.

    Reusing the rescue's own parser/verifier is the point — this check must never
    claim a printed TOC is usable that the rescue would then reject. But
    rescue_outline imports lib_validate (to re-score its attempt), and lib_validate
    imports this module, so a module-level import is a cycle. Deferring it to call
    time breaks the cycle without duplicating a single line of the reader."""
    from rescue_outline import (  # noqa: PLC0415 — deliberate, see above
        current_outline_is_suspect, find_toc_pages, parse_toc_layout, parse_toc_text, verify,
    )
    return current_outline_is_suspect, find_toc_pages, parse_toc_layout, parse_toc_text, verify

# A document this short legitimately arrives without an outline and without
# sections; demanding one would fail correct output. Matches the guard
# check_structure_profile uses for the same reason.
MIN_PAGES_TO_JUDGE = 8

# Scores per status. Anything below GATE_REVIEW (70) fails, which is the point:
# a document whose structure came from an outline that does not describe it must
# not pass, and failing is what lets the fallback chain try the printed TOC.
# `lacking` scores above `improper` because "no outline, inferred from font size"
# is a weaker claim than "an outline exists and it is wrong" — the latter actively
# misplaces section boundaries.
SCORE_PROPER = 100.0
SCORE_LACKING = 55.0
SCORE_IMPROPER = 35.0

# A verified printed TOC lifts the score, because the document is repairable and
# the chain will repair it — but not to a pass, or nothing would ever escalate.
RESCUABLE_BONUS = 10.0

# ---- OVER-SEGMENTED OUTLINES -------------------------------------------------
#
# current_outline_is_suspect is a CHEAP TRIAGE: it asks whether an outline exists
# and starts near the front of the document, and nothing else. That leaves this
# check unable to detect the second case its own status vocabulary promises — an
# outline that exists and does not describe the document — because a Word export
# that bookmarks every styled paragraph produces hundreds of entries that all
# start on page 1 and all pass. Measured on this corpus: 124/ADGM__170680 carries
# 92 bookmarks titled with body sentences ("Where:", "(a) clients, or prospective
# clients, are located in your jurisdiction"), 124/Australia__175399 carries 335,
# 154/ADGM__170647 carries 289 — and every one of them scored `proper`, 100.0.
#
# That is not a cosmetic misreport. pdf2mdtree builds the tree FROM the outline,
# so a paragraph-level outline splits the document at paragraph boundaries: it is
# what produces one-token section titles ("Into") and nodes like
# "(i) Authorised Persons or Persons Outside ADGM Exemption;" that are headings
# with no body.
#
# The test is a comparison, not a heuristic about titles. This module ALREADY
# parses and verifies the document's printed contents page — it just never
# compared the two. When the printed TOC verifies and the embedded outline names
# several times more sections than the document's own contents page does, the
# embedded outline is not the document's spine. Requiring a VERIFIED printed TOC
# is what keeps this honest: with no independent statement of the document's
# structure, this check makes no claim.

# Share of the PRINTED contents page's sections that must also appear in the
# metadata outline before the outline is believed to describe this document.
# Set loosely on purpose: a printed TOC is OCR'd from page text and its titles
# wrap, abbreviate and pick up leader dots, so a handful will always fail to
# match a clean bookmark. The signal this has to separate is not 85% against 95%
# — it is 100% against 0%.
MIN_TITLE_AGREEMENT = 0.5


def _read_printed_toc(doc, pdf_path: Path) -> dict:
    """Parse + verify the document's PRINTED contents page, read-only.

    Text first (dot leaders), then layout (column geometry) — the same order and
    the same verify(), with the SAME ARGUMENTS, as the rescue itself, so the two can
    never disagree about whether a TOC is usable.

    The arguments are the part that bit. verify() used to check each title against
    the page the contents page printed for it, so the contents page could never be
    a false hit and there was nothing to exclude. It now LOCATES each heading by
    searching forward, and `skip_pages` is what stops all of them being "found" on
    the contents page that lists them. This call did not pass it: on
    124/Philippines__167492 the rescue read 40 entries and verified 40 of 40 across
    87% of the document, while this reader — same PDF, same parser, same function —
    verified the same 40 on the contents page itself and rejected them as a 1.2%
    span. The pre-flight then left a 3-bookmark outline in place (Check1,
    OLE_LINK5, OLE_LINK6 — Word anchors), Stage 1 built a 3-section tree,
    completeness scored 0, and the fallback chain paid for a SECOND full MinerU
    pass to apply the repair the pre-flight had declined: 46 minutes against 21."""
    _, find_toc_pages, parse_toc_layout, parse_toc_text, verify = _rescue_reader()
    out = {"pages": [], "entries": 0, "verified": 0, "usable": False, "reason": "no printed TOC found"}
    try:
        pages = find_toc_pages(doc)
    except Exception as e:  # noqa: BLE001 — a malformed PDF must not sink the check
        out["reason"] = f"TOC scan failed: {e}"
        return out
    if not pages:
        return out
    out["pages"] = pages
    for parser, label in ((parse_toc_text, "text"), (parse_toc_layout, "layout")):
        try:
            entries = parser(doc, pages)
        except Exception:  # noqa: BLE001
            continue
        if not entries:
            continue
        chk = verify(doc, entries, skip_pages=pages)
        out.update({"entries": chk["entries"], "verified": chk["verified"],
                    "engine": label, "span_pct": chk.get("span_pct", 0.0),
                    "usable": bool(chk["ok"]),
                    # The titles themselves, for the agreement test below. A count
                    # cannot tell an outline that names the WRONG sections from one
                    # that names the right ones.
                    "titles": [t for _lvl, t, _pg in entries]})
        out["reason"] = ("usable: %d/%d titles verified on their page, spans %.0f%% of the document"
                         % (chk["verified"], chk["entries"], chk.get("span_pct") or 0.0)) if chk["ok"] \
            else "; ".join(chk["reasons"]) or "did not verify"
        if chk["ok"]:
            break
    return out


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _outline_disagrees(toc: list, printed: dict) -> tuple[bool, str]:
    """Compare the metadata outline against the contents page the document PRINTS.

    Two independent statements of the same thing, so they can be checked against
    each other — which is the test this module was missing. Measured on
    124/ADGM__170680: of the 39 sections its printed contents page names, ZERO
    appear in the 92-entry metadata outline, and 39 of 39 appear in the repaired
    one. No threshold tuning is needed to separate those.

    Two distinct failures, because they need different words:
      WRONG     the outline names sections this document does not have. The tree
                is then built on boundaries that exist nowhere in the text.
      TOO MANY  the outline names the right sections AND hundreds of paragraphs
                besides — a Word export that bookmarked every styled paragraph.
                The tree splits mid-clause.

    Silent unless the printed TOC verified: with no independent statement of the
    document's structure this check makes no claim."""
    if not printed.get("usable") or not toc:
        return False, ""
    titles = printed.get("titles") or []
    if len(titles) < 4:
        return False, ""
    outline_norm = {_norm_title(t) for _lvl, t, _pg in toc}
    found = sum(1 for t in titles if _norm_title(t) in outline_norm)
    agreement = found / len(titles)
    if agreement < MIN_TITLE_AGREEMENT:
        return True, (f"{len(toc)} bookmark(s), but only {found} of the {len(titles)} section(s) "
                      f"named on the document's own printed contents page appear among them — "
                      f"the outline does not name this document's sections")
    # OVER-SEGMENTATION IS NO LONGER A TRIGGER. It was a bare count — outline entries
    # against printed contents rows, over a ratio — with nothing to corroborate it, and
    # it cannot tell "bookmarked every styled paragraph" from "has one more level than
    # the contents page prints". Germany (Data Privacy)__180656 tripped it at 2.08
    # against a 2.0 bar (514 bookmarks, 247 printed rows) while agreeing with its own
    # contents page on 245 of 247 sections — a 99% match. The rescue then replaced a
    # tree with 594/594 headings matched and a full A. -> 2. -> 2.3 -> (a) hierarchy
    # with a flattened 3-level one, 91 promised sections lost their home, `sectioning`
    # fell to 60.8, and an 11-minute MinerU re-parse followed.
    #
    # Title AGREEMENT above is the corroborated test and stays: ADGM__170680 matches
    # 0 of 39, Germany matches 245 of 247. That separation needs no ratio.
    return False, ""


def compute_toc_quality(out_root: Path, pdf: str | None = None) -> dict:
    # The OUTLINE is what this check judges, and the TOC pre-flight replaces the
    # outline wholesale when it repairs a document — so this must read the PDF
    # Stage 1 built the tree from, not whichever copy resolve_pdf prefers. Reading
    # the wrong one made this check grade an outline the pipeline had already
    # discarded (see resolve_tree_pdf).
    pdf_path = Path(pdf).resolve() if pdf else resolve_tree_pdf(Path(out_root))
    doc = fitz.open(str(pdf_path))
    try:
        npages = doc.page_count
        toc = doc.get_toc()
        current_outline_is_suspect, *_ = _rescue_reader()
        suspect, outline_note = current_outline_is_suspect(doc)
        printed = _read_printed_toc(doc, pdf_path)

        if npages < MIN_PAGES_TO_JUDGE:
            return {"pdf": str(pdf_path), "pages": npages, "status": "not_judged",
                    "score": None, "outline_entries": len(toc), "outline_note": outline_note,
                    "printed_toc": printed, "passed": True,
                    "detail": f"document is only {npages} page(s) — too short to require an outline"}

        # Not every product's outline should be judged against its printed contents page: DP and
        # SD bookmark at a finer grain by design, and treating that as "improper" sent both
        # products through a rescue they had already passed without.
        try:
            from product_rules import outline_is_authoritative, product_for_job
            authoritative = outline_is_authoritative(product_for_job(Path(out_root)))
        except Exception:                                    # noqa: BLE001
            authoritative = False

        overseg, overseg_note = (False, "") if authoritative else _outline_disagrees(toc, printed)
        if overseg:
            # The triage passed it and the document itself disagrees. Scored as
            # `improper` because that is what it is — an outline that exists and
            # does not describe the document's sections — and because the remedy
            # is the same one `improper` already triggers: adopt the printed
            # contents page instead.
            status, score = "improper", SCORE_IMPROPER
            outline_note = overseg_note
            detail = (f"an outline EXISTS but is not this document's section spine "
                      f"({overseg_note}). Building a tree from it splits the document at "
                      f"paragraph boundaries")
        elif not suspect:
            status, score = "proper", SCORE_PROPER
            detail = f"the embedded outline describes this document ({outline_note})"
        elif not toc:
            status, score = "lacking", SCORE_LACKING
            detail = ("this PDF has NO bookmark outline, so section structure was inferred "
                      "from font size rather than read from the document")
        else:
            status, score = "improper", SCORE_IMPROPER
            detail = (f"an outline EXISTS but does not describe this document ({outline_note}) — "
                      f"the sections it names are not the document's own spine")

        if status != "proper" and printed["usable"]:
            score = min(score + RESCUABLE_BONUS, SCORE_PROPER - 1)
            detail += (f". Its PRINTED contents page IS usable ({printed['reason']}), so a "
                       f"better structure is available and the fallback chain can adopt it")
        elif status != "proper":
            detail += f". No usable printed contents page either ({printed['reason']})"

        return {"pdf": str(pdf_path), "pages": npages, "status": status, "score": round(score, 1),
                "outline_entries": len(toc), "outline_note": outline_note,
                "printed_toc": printed, "rescuable": bool(printed["usable"]),
                "detail": detail,
                # `passed` is the boolean validation.json reports; the SCORE above is
                # what the gate reads (see check_scorecard.CRITICAL_DIMENSIONS).
                "passed": status == "proper"}
    finally:
        doc.close()


STATUS_COLOR = {"proper": GREEN, "lacking": YELLOW, "improper": RED, "not_judged": DIM}


def print_report(report: dict) -> None:
    banner("TOC quality")
    st = report["status"]
    print(f"{BOLD('status')}          : {STATUS_COLOR[st](st.upper())}")
    print(f"{BOLD('score')}           : {report['score']}")
    print(f"{BOLD('pages')}           : {report['pages']}")
    print(f"{BOLD('outline')}         : {report['outline_note']}")
    p = report["printed_toc"]
    print(f"{BOLD('printed TOC')}     : pages {p.get('pages') or '-'}  {p.get('reason')}")
    print(f"\n{report['detail']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--pdf", default=None)
    args = ap.parse_args()
    print_report(compute_toc_quality(Path(args.out_root), args.pdf))


if __name__ == "__main__":
    run_cli(main)
