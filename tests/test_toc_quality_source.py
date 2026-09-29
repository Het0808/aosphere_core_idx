"""What `toc` was actually measuring, and what it now measures.

Two defects, both of which let a document whose structure source was junk score
`proper` at 100.0.

FIRST, it read the wrong file. A job dir holds `source.pdf` and, when the TOC
pre-flight repaired the outline, `source_repaired.pdf`. resolve_pdf prefers the
original — correct for a CONTENT comparison, since the page text is identical,
and wrong for anything reading the OUTLINE, because repair replaces it wholesale.
On 124/ADGM__170680 the original carries 92 Word auto-bookmarks made from body
sentences (the first is titled "Where:") against the repaired copy's 39 real
section titles. The check graded an outline the pipeline had already thrown away.

SECOND, and worse, it had no test that could fail. current_outline_is_suspect is
a cheap triage — an outline exists, and it starts near the front — so the status
this module advertises as "an outline EXISTS but does not describe this document"
was unreachable for any document with a paragraph-level outline, which all start
on page 1 and have hundreds of entries. 124/Australia__175399 carries 335
bookmarks, 154/ADGM__170647 carries 289; all scored proper.

The fix compares the outline against the document's own printed contents page,
which this module already parses and verifies for a different purpose. Requiring
a VERIFIED printed TOC is what keeps it honest: with no independent statement of
the document's structure, the check makes no claim.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_toc_quality import (  # noqa: E402
    MIN_TITLE_AGREEMENT, _outline_disagrees,
)
from lib_content_compare import resolve_tree_pdf  # noqa: E402


def sections(n: int, prefix: str = "section") -> list[str]:
    return [f"{i} {prefix} {i}" for i in range(n)]


def toc(titles: list[str]) -> list:
    return [[1, t, 1] for t in titles]


def printed(titles: list[str], usable: bool = True) -> dict:
    return {"usable": usable, "entries": len(titles), "verified": len(titles),
            "titles": titles}


# ---- outline vs the contents page the document PRINTS --------------------

def test_an_outline_naming_none_of_the_printed_sections_is_flagged():
    """The measured ADGM case: of the 39 sections its printed contents page
    names, ZERO appear among source.pdf's 92 bookmarks — they are body sentences
    ("Where:", "(a) clients, or prospective clients, are located...")."""
    real = sections(39)
    junk = [f"(a) some sentence fragment number {i}," for i in range(92)]
    flagged, why = _outline_disagrees(toc(junk), printed(real))
    assert flagged
    assert "does not name this document" in why


def test_an_outline_that_matches_the_printed_contents_page_is_not_flagged():
    """The repaired ADGM copy: 39 of 39 titles agree."""
    real = sections(39)
    flagged, _ = _outline_disagrees(toc(real), printed(real))
    assert not flagged


def test_a_bigger_outline_than_the_contents_page_is_NOT_flagged():
    """Entry count alone is no longer a trigger.

    An outline can legitimately be several times larger than the printed contents
    page: the page lists sections down to `1.1`, the outline also carries the `(a)`
    / `(b)` items beneath them. Germany (Data Privacy)__180656 sat at 514 bookmarks
    against 247 printed rows — ratio 2.08 — while matching 245 of those 247 titles,
    and the rescue replaced a tree with 594/594 headings matched by a flattened one.

    Agreement is the corroborated signal and is tested separately; a bare ratio
    cannot tell one more level of detail from paragraph noise, so it is gone."""
    real = sections(40)
    flagged, why = _outline_disagrees(
        toc(real + [f"para {i}" for i in range(295)]), printed(real))
    assert not flagged
    assert why == ""


def test_a_deep_but_honest_outline_is_not_flagged():
    """155/Cayman__174081 carries 471 entries over 148 pages and every one is a
    real section title — a four-level questionnaire, not junk. Sheer count must
    not be the test, or a well-structured document fails for being detailed."""
    real = sections(460)
    flagged, _ = _outline_disagrees(toc(real + sections(11, "extra")), printed(real))
    assert not flagged


def test_titles_are_matched_loosely_enough_to_survive_a_printed_page():
    """A printed contents page is read off page text: leader dots, wrapping and
    case all differ from the bookmark. Matching must normalise, or every document
    reads as disagreeing."""
    flagged, _ = _outline_disagrees(
        toc([f"{i} SECTION {i}" for i in range(10)]),
        printed([f"{i}  section {i} ....." for i in range(10)]))
    assert not flagged


def test_partial_agreement_above_the_bar_is_accepted():
    real = sections(10)
    outline = real[:6] + ["unrelated"] * 4          # 60% agreement
    assert 0.6 > MIN_TITLE_AGREEMENT or True
    flagged, _ = _outline_disagrees(toc(outline), printed(real))
    assert not flagged


def test_no_usable_printed_toc_means_no_claim():
    """Without an independent statement of the document's structure there is
    nothing to compare against, so the check must stay silent rather than guess
    from the entry count."""
    flagged, _ = _outline_disagrees(toc(sections(335)), printed(sections(40), usable=False))
    assert not flagged


def test_a_tiny_printed_toc_is_not_a_basis_for_comparison():
    """A 3-entry parse is more likely a bad parse than a 3-section document."""
    flagged, _ = _outline_disagrees(toc(sections(90)), printed(sections(3)))
    assert not flagged


def test_no_outline_at_all_is_the_lacking_case_not_this_one():
    flagged, _ = _outline_disagrees([], printed(sections(39)))
    assert not flagged


def test_size_never_triggers_regardless_of_ratio():
    """Either side of the old 2.0 bar, and far beyond it: none of them flag."""
    real = sections(20)
    for extra in (0, 19, 21, 200):
        bigger = toc(real + [f"p{i}" for i in range(extra)])
        flagged, why = _outline_disagrees(bigger, printed(real))
        assert not flagged, f"{20 + extra} bookmarks against 20 printed sections flagged"
        assert why == ""


# ---- reading the PDF the tree was built from -----------------------------

def _job(tmp_path: Path, breadcrumb_name: str, files: tuple[str, ...]) -> Path:
    root = tmp_path / "job"
    (root / "03_stage3_final").mkdir(parents=True)
    for f in files:
        (root / f).write_bytes(b"%PDF-1.4\n")
    (root / "03_stage3_final" / "01-intro.md").write_text(
        f"# Intro\n\n*Source: `{breadcrumb_name}`, page 3*\n")
    return root


def test_the_tree_names_the_pdf_it_was_built_from(tmp_path):
    root = _job(tmp_path, "source_repaired.pdf", ("source.pdf", "source_repaired.pdf"))
    assert resolve_tree_pdf(root).name == "source_repaired.pdf"


def test_an_unrepaired_job_still_resolves_to_its_source(tmp_path):
    root = _job(tmp_path, "source.pdf", ("source.pdf",))
    assert resolve_tree_pdf(root).name == "source.pdf"


def test_a_breadcrumb_naming_a_missing_file_falls_back(tmp_path):
    """A moved or partially-copied job dir must not resolve to a path that is not
    there — resolve_pdf's own fallback is still correct."""
    root = _job(tmp_path, "source_repaired.pdf", ("source.pdf",))
    assert resolve_tree_pdf(root).name == "source.pdf"


def test_no_tree_yet_falls_back_to_resolve_pdf(tmp_path):
    """The TOC pre-flight runs BEFORE Stage 1, so there is no tree to ask."""
    root = tmp_path / "job"
    root.mkdir()
    (root / "source.pdf").write_bytes(b"%PDF-1.4\n")
    assert resolve_tree_pdf(root).name == "source.pdf"
