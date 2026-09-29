"""The pre-flight's printed-TOC reader must verify a contents page the way the rescue does.

check_toc_quality._read_printed_toc and rescue_outline.rescue ask the same question of the
same PDF through the same function, so they have to agree. They stopped agreeing when
verify() changed from "is the title on the page the contents page printed for it" to "can
the title be found in the document at all". The search has to be kept off the contents
pages themselves — they list every title — and the reader was not passing skip_pages,
which defaulted to "skip nothing".

Measured on 124_Marketing_Restrictions_-_Asset_Management/Philippines__167492 (163 pages,
contents page on 2-3): the rescue verified 40 of 40 titles across 87% of the document, the
reader verified the same 40 on the contents page itself and rejected them as a 1.2% span.
So the pre-flight declined to repair a 3-bookmark outline of Word anchors (Check1,
OLE_LINK5, OLE_LINK6), Stage 1 built three sections, completeness scored 0 -- and the
fallback chain then paid for a SECOND full MinerU pass to apply the very repair the
pre-flight had turned down. 46 minutes, against 21 for the same document a run earlier.

The reader reported "no usable printed TOC" for every document in the corpus while this
held, so the pre-flight's outline-disagreement trigger was dead everywhere and every
scorecard's toc.rescuable read False.
"""

import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_toc_quality import _outline_disagrees, _read_printed_toc  # noqa: E402
from rescue_outline import (find_toc_pages, locate_titles, parse_toc_text,  # noqa: E402
                            verify)

SECTIONS = [("1. BACKGROUND TO PART B", 1), ("2. GUIDELINES FOR COMPLETION", 3),
            ("3. MARKETING RESTRICTIONS", 5), ("4. PRIVATE PLACEMENT", 7),
            ("5. SUPERVISORY POWERS", 9), ("6. APPENDIX A DEFINITIONS", 11)]


def _doc(tmp_path: Path) -> tuple[fitz.Document, Path]:
    """A cover, a printed contents page, then the sections spread over the body.

    The shape that matters: every title appears on the contents page AND, later, on
    its own body page. Locating them on the contents page is what produces the tiny
    span that fails verification."""
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)
    cover.insert_text((72, 100), "MEMORANDUM ON MARKETING RESTRICTIONS", fontsize=16)
    toc = doc.new_page(width=595, height=842)
    toc.insert_text((72, 80), "TABLE OF CONTENTS", fontsize=14)
    for i, (title, page) in enumerate(SECTIONS):
        toc.insert_text((72, 120 + i * 24), f"{title} {'.' * 40} {page}", fontsize=10)
    body = {page: title for title, page in SECTIONS}
    for n in range(1, 13):                      # 12 body pages, 2 per section
        page = doc.new_page(width=595, height=842)
        if n in body:
            page.insert_text((72, 100), body[n], fontsize=13)
        page.insert_text((72, 160), f"Body text for page {n} of the memorandum.", fontsize=10)
    out = tmp_path / "memo.pdf"
    doc.save(str(out))
    return fitz.open(str(out)), out


def test_the_reader_accepts_the_contents_page_the_rescue_would_accept(tmp_path):
    doc, path = _doc(tmp_path)
    printed = _read_printed_toc(doc, path)
    assert printed["usable"] is True, printed["reason"]
    assert printed["entries"] == printed["verified"] == len(SECTIONS)
    assert printed["span_pct"] >= 40


def test_the_titles_are_located_in_the_body_not_on_the_contents_page(tmp_path):
    """The failure this pins. Without the contents pages excluded every title is
    'found' on the page that lists them, and the TOC is rejected for its span --
    so a reader that forgets skip_pages does not report a weaker result, it
    reports the opposite one."""
    doc, path = _doc(tmp_path)
    pages = find_toc_pages(doc)
    entries = parse_toc_text(doc, pages)
    assert len(entries) == len(SECTIONS)

    unguarded = verify(doc, entries, skip_pages=())
    assert unguarded["verified"] == len(SECTIONS)      # all of them, on one page
    assert unguarded["ok"] is False
    assert unguarded["span_pct"] < 40

    guarded = verify(doc, entries, skip_pages=pages)
    assert guarded["ok"] is True
    assert _read_printed_toc(doc, path)["span_pct"] == guarded["span_pct"]


def test_an_outline_of_anchor_names_disagrees_with_the_printed_contents_page(tmp_path):
    """The pre-flight consequence: this is the comparison that repairs the outline
    BEFORE Stage 2, and it is silent unless the printed TOC verified."""
    doc, path = _doc(tmp_path)
    doc.set_toc([[1, "Check1", 3], [1, "OLE_LINK5", 9], [1, "OLE_LINK6", 9]])
    repaired = tmp_path / "anchored.pdf"
    doc.save(str(repaired))
    doc = fitz.open(str(repaired))

    printed = _read_printed_toc(doc, repaired)
    assert printed["usable"] is True
    disagrees, why = _outline_disagrees(doc.get_toc(), printed)
    assert disagrees is True
    assert "3 bookmark(s)" in why and f"0 of the {len(SECTIONS)}" in why


@pytest.mark.parametrize("fn", [verify, locate_titles])
def test_the_contents_pages_to_skip_cannot_be_omitted(fn, tmp_path):
    """Keyword-only and required, so the next caller cannot inherit the old default
    and silently get the wrong answer for a whole corpus."""
    doc, _ = _doc(tmp_path)
    entries = parse_toc_text(doc, find_toc_pages(doc))
    with pytest.raises(TypeError):
        fn(doc, entries)
