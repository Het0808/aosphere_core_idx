"""An outline made of body text is not a structure, and must not become one.

A PDF's bookmark outline is supposed to describe the document's sections. Some instead dump
body text into it. Marketing Restrictions - Asset Management Australia 181814 has 335 entries
of which 84% are whole sentences and sub-bullets:

    (a) for a Fund that is a Body Corporate: (i) Sufficient Equivalent Relief (to …
    An exemption is available where the Financial Service is the provision of Gene…

Stage 1 believed them, so the Executive Summary came out as 73 pseudo-sections named after
sentence fragments — `09-however-it-has-not-typically-covered-the-actual.md` — instead of one
section holding the summary.

Nothing failed. Every word was present, so completeness and fidelity stayed green (review
84.4) and only a person reading the tree could see it. The old trust test asked just two
questions — is there an outline, and does it start too deep into the document — and this
outline passed both.

Pruning the prose is the repair, not rebuilding from a printed contents page: the real
headings ("1. Background", "1.4 Executive Summary") are already there and correct, along with
whatever page offsets and nesting the embedded outline got right. Measured on that document,
pruning also RECOVERS 491 words in the summary, because a sentence turned into a folder name
was truncated to fit and the tail was lost.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rescue_outline as ro  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent / "out" / "corpus"


@pytest.mark.parametrize("title", [
    "1. Background",
    "1.4 Executive Summary",
    "Definitions",
    "A. Substantial shareholding",
    "Appendix 1",
    "1.4.",                                  # a numbered heading's trailing dot
    "Which data breaches must be notified",
])
def test_headings_are_recognised(title):
    assert ro._heading_like(title), title


@pytest.mark.parametrize("title", [
    "(a) for a Fund that is a Body Corporate: (i) Sufficient Equivalent Relief",
    "An exemption is available where the Financial Service is the provision of advice",
    "however it has not typically covered the actual provision of the service",
    "(c) if the advice relates to the acquisition, or possible acquisition, of a product",
    "the client should, before acting on the advice, consider its appropriateness,",
    "This relief is relevant to both wholesale and retail clients.",
])
def test_body_text_is_not_a_heading(title):
    assert not ro._heading_like(title), title


def test_prose_share_and_pruning():
    toc = [[1, "1. Background", 3], [2, "1.1 Introduction", 3],
           [2, "1.4 Executive Summary", 5],
           [3, "An exemption is available where the Financial Service is provided", 6],
           [3, "(a) the advice has been prepared without taking account of anything", 6]]
    assert ro.prose_share(toc) == pytest.approx(0.4)
    kept = [t for _l, t, _p in toc if ro._heading_like(t)]
    assert kept == ["1. Background", "1.1 Introduction", "1.4 Executive Summary"]


def test_pruning_refuses_to_leave_a_useless_skeleton():
    """If almost nothing survives, the outline is not "prose around real headings" — it is
    something else, and replacing it with three bookmarks would be worse than leaving it."""
    toc = [[1, "Some long sentence that is clearly body text and not a heading at all", 1]
           for _ in range(50)] + [[1, "1. Background", 1]]
    assert ro.prose_share(toc) > 0.9
    assert ro.prune_prose_entries(toc) == [], "too few headings to be worth pruning to"


def test_a_prose_outline_is_suspect_and_a_healthy_one_is_not():
    class _Doc:
        def __init__(self, toc, pages=100):
            self._toc, self.page_count = toc, pages

        def get_toc(self):
            return self._toc

    healthy = [[1, f"{i}. Section {i}", i + 1] for i in range(1, 30)]
    ok, why = ro.current_outline_is_suspect(_Doc(healthy))
    assert ok is False, why

    # 10+ words, so unambiguously a sentence — a 9-word capitalised phrase with no trailing
    # punctuation reads as a heading, which is the conservative call the detector should make.
    prose = healthy + [[2, f"An exemption is available where the Financial Service number {i} "
                           f"is provided from offshore", 5] for i in range(60)]
    ok, why = ro.current_outline_is_suspect(_Doc(prose))
    assert ok is True
    assert "body text" in why and "heading" in why


def test_an_outline_of_only_headings_is_never_pruned():
    """The corpus is mostly healthy; this path must cost those documents nothing."""
    toc = [[1, f"{i}. Section", i] for i in range(1, 40)]
    assert ro.prose_share(toc) == 0.0
    assert ro.prune_prose_entries(toc) == toc


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus not present")
def test_the_reported_document_end_to_end():
    """MRAM Australia 181814: the document that prompted this."""
    pytest.importorskip("fitz")
    import fitz
    job = CORPUS / "124_Marketing_Restrictions_-_Asset_Management" / "Australia__181814"
    # The PRISTINE input, which is what this test is about. A repair promotes its result over
    # source.pdf IN PLACE and keeps the untouched original beside it, so after any run that
    # rescued this document source.pdf holds a repaired outline whose prose share is ~0 and the
    # 335-entry outline the assertions describe is only in source.pdf_original.
    pdf = next((p for p in (job / "source.pdf_original", job / "source.pdf") if p.exists()), None)
    if pdf is None:
        pytest.skip("MRAM Australia not extracted here")
    doc = fitz.open(str(pdf))
    try:
        toc = doc.get_toc()
        assert ro.prose_share(toc) > 0.8, "84% of this outline is body text"
        kept = ro.prune_prose_entries(toc)
        assert 30 < len(kept) < 80, f"{len(toc)} entries pruned to {len(kept)}"
        assert any("Executive Summary" in t for _l, t, _p in kept), \
            "the real heading must survive pruning"
        assert not any(t.startswith("An exemption is available") for _l, t, _p in kept)
        suspect, why = ro.current_outline_is_suspect(doc)
        assert suspect and "body text" in why
    finally:
        doc.close()
