"""A running footer that lands outside the 6-line zone is still a running footer.

Guatemala 32221 prints "<page no> | Marketing Restrictions - Asset Management" at
y=0.96 of the page on all four of its pages. fitz emits pages 2-4's copy at line index
1, but page 1's busy masthead pushes it to index 6 -- one past zone_top -- so
detect_boilerplate_patterns counted the footer on 3 pages of 4, scored 0.75 against its
0.80 threshold, and never recognised it. It stayed in the source text, and both
sections whose page range covered those pages were charged a silent content gap whose
five "missing" tokens were the page number and the footer.

page_band_lines answers the question the line-index zone only approximates -- where is
this line PRINTED -- and admits the band lines to their own page's zone.

The second test is the guard rail on that rescue, and it is the one that matters: the
band must name individual LINES of a page, never signatures pooled across the document.
A page number signs as "#", which is also the signature of every bare number in the
body; an earlier pooled-set version of this un-gated "#" everywhere and stripped a
contents list's own "1." through "11." numbering off Czech Republic 166819's page 2.
Stripping real content does not fix a false gap, it hides one -- that document's gap
disappeared for exactly the wrong reason.
"""

import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib_content_compare import (  # noqa: E402
    detect_boilerplate_patterns,
    page_band_lines,
    pdf_page_texts,
    strip_boilerplate,
)

FOOTER = "Marketing Restrictions - Asset Management"
WIDTH, HEIGHT = 612.0, 792.0


def _doc(tmp_path: Path, name: str, masthead_lines: dict[int, int],
         body: dict[int, list[str]] | None = None, n_pages: int = 4) -> Path:
    """An n-page PDF with the footer printed at y=0.96 on every page.

    `masthead_lines` gives how many extra lines to print ABOVE the footer on a page --
    the thing that pushes it down the extracted stream without moving it on the page.
    """
    doc = fitz.open()
    for pno in range(1, n_pages + 1):
        page = doc.new_page(width=WIDTH, height=HEIGHT)
        y = 60.0
        for k in range(masthead_lines.get(pno, 0)):
            page.insert_text((60, y), f"masthead line {k} of page {pno}", fontsize=10)
            y += 14
        # The footer goes into the content stream HERE, between the masthead and the
        # body, which is what puts it mid-stream on page 1 while leaving it at y=0.96
        # on the page -- the Guatemala shape exactly. Body text follows it, so on a
        # page with a masthead it falls outside zone_top AND zone_bottom.
        page.insert_text((60, HEIGHT * 0.96), str(pno), fontsize=8)
        page.insert_text((100, HEIGHT * 0.96), FOOTER, fontsize=8)
        for line in (body or {}).get(pno, []) or [f"body line {k} of page {pno}"
                                                  for k in range(8)]:
            page.insert_text((60, y), line, fontsize=10)
            y += 14
    out = tmp_path / name
    doc.save(str(out))
    doc.close()
    return out


def test_footer_pushed_past_the_line_zone_is_still_detected(tmp_path):
    # Page 1 carries a masthead deep enough to push its footer past zone_top.
    pdf = _doc(tmp_path, "masthead.pdf", masthead_lines={1: 8})
    pages = pdf_page_texts(pdf)

    footer_line_index = [l.strip() for l in pages[1].split("\n") if l.strip()].index(FOOTER)
    assert footer_line_index >= 6, "page 1's footer must be outside the 6-line zone"

    before, _ = detect_boilerplate_patterns(pages)
    assert "marketing restrictions asset management" not in before, (
        "precondition: the line-index zone alone must miss this footer"
    )

    band = page_band_lines(pdf)
    after, _ = detect_boilerplate_patterns(pages, band_lines=band)
    assert "marketing restrictions asset management" in after
    assert before <= after, "the band may only add patterns, never remove one"

    cleaned = strip_boilerplate(pages, after, band_lines=band)
    assert not any(FOOTER in cleaned[p] for p in range(1, len(cleaned))), (
        "every copy of the footer must be stripped, page 1's included"
    )


def test_band_never_excuses_a_body_line_sharing_a_footer_signature(tmp_path):
    # Bare numbers in the body: same "#" signature as the page number in the band.
    # Padded either side so the numbering sits outside zone_top AND zone_bottom --
    # otherwise the line-index zone strips it on its own and the band is never tested.
    numbering = [f"{k}." for k in range(1, 12)]
    pad = [f"padding line {k}" for k in range(8)]
    pdf = _doc(tmp_path, "numbered.pdf", masthead_lines={1: 8},
               body={2: pad + numbering + pad})
    pages = pdf_page_texts(pdf)
    band = page_band_lines(pdf)
    patterns, _ = detect_boilerplate_patterns(pages, band_lines=band)
    assert "#" in patterns, "precondition: the page number is recognised boilerplate"

    without_band = [l.strip() for l in strip_boilerplate(pages, patterns)[2].split("\n")
                    if l.strip()]
    assert all(item in without_band for item in numbering), (
        "precondition: the line-index zone alone leaves the numbering alone, so "
        "anything this test catches is the band's doing"
    )

    cleaned = strip_boilerplate(pages, patterns, band_lines=band)
    kept = [l.strip() for l in cleaned[2].split("\n") if l.strip()]
    for item in numbering:
        assert item in kept, f"body numbering {item!r} was stripped as page furniture"
    assert FOOTER not in kept and "2" not in kept, (
        "the page's own furniture must still go"
    )
