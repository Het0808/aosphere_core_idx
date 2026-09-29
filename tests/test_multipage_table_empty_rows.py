"""A landscape page that reconstructs to a header with NO rows must not kill stage 1.

`_recon` returns (columns, rows) or None. Truthiness is not emptiness: a ruled landscape page can
reconstruct with an empty body — a continuation page whose rules carry over, a spacer, a page of
footnotes sitting under a table. reconstruct_multipage_tables guarded the run-start case properly
(`if not r0 or len(r0[1]) < 2`) but then indexed `rq[1][0]` behind a bare `if rq:`, so such a page
raised IndexError and took the ENTIRE document down — stage 1 exits non-zero, run_corpus reports
"stage 1 failed", and nothing is extracted.

Reproduced on Marketing Restrictions - Asset Management Luxembourg 181768 (158 pages), which
failed this way on the cluster AND locally on CPU — which is how we knew it was a code bug and
not the GPU. With the guard, that document extracts 31 section files.

Because it depends on the shape of one page, it looks intermittent across a corpus: documents of
the same size both succeed and fail.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

fitz = pytest.importorskip("fitz")
import pdf2mdtree as P  # noqa: E402


def _landscape_doc(n=3):
    doc = fitz.open()
    for i in range(n):
        page = doc.new_page(width=842, height=595)          # A4 landscape
        page.insert_text((60, 80), f"Header {i}  Column A  Column B")
        page.insert_text((60, 120), f"row text for page {i}")
    return doc


def test_a_rowless_reconstruction_does_not_crash_the_document(monkeypatch):
    """The exact crash: page 1 starts a run, page 2 reconstructs with an empty body."""
    doc = _landscape_doc(3)
    calls = {"n": 0}

    def fake_recon(page, bounds=None, head_min=None, titles=None, title_exacts=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (["A", "B"], [["A", "B"], ["1", "2"], ["3", "4"]])   # a real run start
        return (["A", "B"], [])                                          # header, NO rows
    monkeypatch.setattr(P, "_recon", fake_recon)

    # Must not raise. What it returns is not the contract under test; not crashing is.
    out = P.reconstruct_multipage_tables(doc, 0, doc.page_count)
    assert isinstance(out, dict)
    doc.close()


def test_a_rowless_page_is_treated_as_a_continuation_not_a_new_table(monkeypatch):
    """With no rows there is no evidence of a DIFFERENT header, and that check is the only
    reason to end a run — so the page continues it rather than splitting the table."""
    doc = _landscape_doc(4)
    seq = [(["A", "B"], [["A", "B"], ["1", "2"], ["3", "4"]]),
           (["A", "B"], []),
           (["A", "B"], [["5", "6"], ["7", "8"]]),
           (["A", "B"], [["9", "10"], ["11", "12"]])]
    it = iter(seq * 6)
    monkeypatch.setattr(P, "_recon", lambda *a, **k: next(it, seq[-1]))
    out = P.reconstruct_multipage_tables(doc, 0, doc.page_count)
    # The rowless page must not have started its own region: at most one run begins here.
    firsts = {v.get("first") for v in out.values() if isinstance(v, dict)}
    assert len(firsts) <= 1, f"the run was split at the rowless page: {firsts}"
    doc.close()


def test_none_from_recon_is_still_handled(monkeypatch):
    """The pre-existing path: a page that does not reconstruct at all."""
    doc = _landscape_doc(3)
    monkeypatch.setattr(P, "_recon", lambda *a, **k: None)
    assert P.reconstruct_multipage_tables(doc, 0, doc.page_count) == {}
    doc.close()


@pytest.mark.skipif(
    not (Path("corpus-src/124_Marketing_Restrictions_-_Asset_Management/Luxembourg/181768.pdf")
         .exists()),
    reason="source corpus not present")
def test_the_document_that_reproduced_it_extracts():
    """End to end on the real document, which crashed on the cluster and locally."""
    doc = fitz.open("corpus-src/124_Marketing_Restrictions_-_Asset_Management/Luxembourg/181768.pdf")
    try:
        out = P.reconstruct_multipage_tables(doc, 0, doc.page_count)   # must not raise
        assert isinstance(out, dict)
    finally:
        doc.close()


def test_the_clause_table_seed_path_also_survives_empty_rows(monkeypatch):
    """The same mistake one branch further up, and it cost 24 documents to find.

    The guard before this only `continue`s when the page is NOT a clause-table seed, so a
    clause_tables product falls through with a header and no rows — and indexing row 0 there
    killed Marketing Restrictions Luxembourg 181768 even after the `rq` path was fixed.
    """
    doc = _landscape_doc(3)
    monkeypatch.setattr(P, "_recon", lambda *a, **k: (["A", "B"], []))   # header, no rows
    out = P.reconstruct_multipage_tables(doc, 0, doc.page_count, sec_pages={0, 1, 2},
                                         clause_tables=True)
    assert isinstance(out, dict)                                        # must not raise
    doc.close()
