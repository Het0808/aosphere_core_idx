"""Verifying a stitched table's continuation page when MinerU leaves only a stub.

When MinerU merges a table across pages it renders the rows with the neighbouring page's
table and leaves an empty stub behind. The merge check clips that stub and measures how
much of its text is present in the tree's cells — but the stub is routinely just the
table's REPRINTED HEADER ROW, a two-word strip. A 6-word window test and a whole-cell
test both score 0 on two words, so the check reported "the merge appears to have DROPPED
these rows" for tables that were completely intact.

Hungary Data Privacy 180716: five such stubs, each a 19pt strip holding "Term Meaning",
each scored 0% and credited as a failure — fidelity 62.1 and a failed document, while
77-89% of every one of those pages was present in the tree's cells.

A continuation stub means "this table carries on from here", so the region to measure is
the rest of the page in the table's own column span. What these tests pin is that
widening does not become a licence to pass anything: a page whose rows really are absent
must still fail, and a page with nothing to read must report unverifiable rather than
either verdict.
"""

import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_table_presence import (  # noqa: E402
    CONT_COVERAGE_THRESHOLD, MIN_REGION_TOKENS, _verify_continuations,
)
from lib_content_compare import tokenize  # noqa: E402

HEADER = "Term Meaning"
ROWS = [
    "Controller the natural or legal person which determines the purposes of processing",
    "Processor a person which processes personal data on behalf of the controller",
    "Supervisory authority the independent public authority responsible for monitoring",
]
STUB_BBOX = [48.0, 70.0, 548.0, 90.0]          # the 19pt header strip, as MinerU leaves it


def _job(tmp_path, rows=ROWS, header=HEADER):
    """A one-page continuation: header strip at the top, table rows below it."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 84), header, fontsize=10)
    y = 140.0
    for row in rows:
        page.insert_text((50, y), row, fontsize=9)
        y += 40
    job = tmp_path / "job"
    job.mkdir()
    doc.save(str(job / "source.pdf"))
    doc.close()
    return job


def _report():
    return {"tables": [{"table_id": "table_004", "pages": [1], "continuation": True,
                        "bbox": list(STUB_BBOX), "ok": False}]}


def _cells(texts):
    return [tokenize(t) for t in texts]


def test_the_stub_alone_is_too_small_to_measure(tmp_path):
    """The premise: the stub holds fewer words than any coverage measure needs."""
    job = _job(tmp_path)
    doc = fitz.open(str(job / "source.pdf"))
    stub_tokens = tokenize(doc[0].get_text("text", clip=fitz.Rect(*STUB_BBOX)))
    doc.close()
    assert len(stub_tokens) < MIN_REGION_TOKENS


def test_intact_rows_verify_after_widening(tmp_path):
    """The Hungary case: rows present in the tree's cells -> the merge kept them."""
    job = _job(tmp_path)
    out = _verify_continuations(_report(), job, _cells(ROWS))
    assert len(out) == 1
    rec = out[0]
    assert rec["verified"] is True
    assert rec["coverage"] >= CONT_COVERAGE_THRESHOLD
    assert "rest of page" in rec["region"]      # says which region it actually read


def test_genuinely_dropped_rows_still_fail(tmp_path):
    """Widening must not become a licence to pass: unrelated cells -> still a failure."""
    job = _job(tmp_path)
    out = _verify_continuations(_report(), job, _cells([
        "wholly unrelated content about maritime insurance premiums and salvage",
        "another cell concerning shipping tonnage and port dues entirely",
    ]))
    assert out[0]["verified"] is False
    assert "DROPPED" in out[0]["detail"]


def test_a_page_with_nothing_to_read_is_unverifiable_not_failed(tmp_path):
    """No rows below the stub either -> `None`, which credits as an unverifiable merge
    (0.75), not a failure (0.25). A check that cannot see anything must not accuse."""
    job = _job(tmp_path, rows=[])
    out = _verify_continuations(_report(), job, _cells(ROWS))
    assert out[0]["verified"] is None
    assert out[0]["coverage"] is None
    assert "too little to verify" in out[0]["detail"]


def test_a_stub_that_is_already_big_enough_is_measured_as_is(tmp_path):
    """Only a too-small region is widened, so a real region keeps its own verdict."""
    job = _job(tmp_path)
    report = _report()
    report["tables"][0]["bbox"] = [48.0, 70.0, 548.0, 300.0]     # covers header + rows
    out = _verify_continuations(report, job, _cells(ROWS))
    assert out[0]["verified"] is True
    assert out[0]["region"] == "stub bbox"


def test_a_region_without_a_bbox_stays_unverifiable(tmp_path):
    """Pre-existing behaviour, kept: a multi-page comparison table has no bbox to clip."""
    job = _job(tmp_path)
    report = {"tables": [{"table_id": "t", "pages": [1], "continuation": True,
                          "bbox": None, "ok": False}]}
    out = _verify_continuations(report, job, _cells(ROWS))
    assert out[0]["verified"] is None
    assert "no bbox" in out[0]["detail"]
