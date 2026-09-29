"""A Stage 1 that cannot read the document at all routes to MinerU, and says why.

pdf2mdtree already names both ways this happens and exits rather than emitting a tree. Those
exits used to end the document: run_stage1 turned them into a RuntimeError, the corpus loop
recorded a crash, and NO extraction was attempted -- even though a whole-document visual parse
is exactly the tool for a scan. These tests pin the routing and the reasons it records.
"""
import json
from pathlib import Path

import pytest

import sys
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import run_corpus as R  # noqa: E402

SRC = (SCRIPTS / "run_corpus.py").read_text()
# "Hand the whole document to MinerU" is now ONE nested helper with two entrances: the
# page-count route above Stage 1, and the Stage 1 bail below it. The mechanics are asserted
# against the helper, and each entrance against its own call site, so a change that breaks one
# route cannot pass by looking like the other.
ROUTE = SRC[SRC.index("def _straight_to_mineru("):SRC.index("# ---- PAGE COUNT:")]
SHORT = SRC[SRC.index("# ---- PAGE COUNT:"):SRC.index("# Stage 1 either produces a tree")]
BAIL = SRC[SRC.index("if bail is not None:"):SRC.index("# ---- PRE-FLIGHT")]


# ---- the two diagnoses pdf2mdtree actually emits ----------------------------------------------

def test_an_image_only_pdf_is_named_a_scan():
    cause, why = R.classify_stage1_failure(
        "stage 1 (pdf2mdtree.py) failed:\n"
        "No text layer: 40 page(s) contain no text spans at all (40 image block(s) found). "
        "This is a SCANNED document — it needs OCR before this pipeline can read it")
    assert cause == "scanned" and "image" in why


def test_the_more_specific_scanned_diagnosis_wins():
    """pdf2mdtree writes "effectively no text layer; this looks genuinely scanned" INSIDE the
    no-structure message, so a bare "no text layer" needle matches it too. The specific one has
    to be tested first or the reason describes the wrong half of the diagnosis."""
    cause, why = R.classify_stage1_failure(
        "No structure found: no bookmark outline, no heading-sized text, and no BOLD "
        "CAPITALISED lines to fall back on.\n  text in first 12 page(s): 210 characters"
        "  -> effectively no text layer; this looks genuinely scanned and needs OCR")
    assert cause == "scanned"
    assert "almost no text" in why, "matched the image-only rule instead of the scan rule"


def test_a_heading_detection_gap_is_not_called_a_scan():
    """The distinction matters to whoever reads it: one needs OCR, the other needs the
    extractor taught a new heading style. Saying "scanned" about a document with 46k
    characters of text sends them looking for a problem that does not exist."""
    cause, why = R.classify_stage1_failure(
        "No structure found: no bookmark outline, no heading-sized text, and no BOLD "
        "CAPITALISED lines to fall back on.\n  text in first 12 page(s): 46000 characters"
        "  -> there IS a text layer, so this is a heading-DETECTION gap")
    assert cause == "no_structure"
    assert "scan" not in why.lower()


def test_an_unrecognised_failure_still_routes_but_is_named_as_unrecognised():
    """Losing a document to an unexpected error helps nobody, so it still reaches MinerU. But
    it must not be filed under a known cause, or a genuine regression in the extractor is
    quietly papered over with a MinerU tree that looks like a success."""
    cause, why = R.classify_stage1_failure("RuntimeError: fitz exploded on page 3")
    assert cause == "unrecognised" and "does not recognise" in why


# ---- what the route actually does --------------------------------------------------------------

def test_the_route_skips_stage_2_and_stage_3():
    """Cropping tables out of a tree that does not exist is not work worth paying for, and
    MinerU writes all three stage dirs itself. This is the CHEAPEST route through the pipeline
    at one MinerU pass, not the most expensive."""
    assert "run_mineru_full" in ROUTE
    for skipped in ("he.run_stage2", "he.run_stage3", "_preflight_outline"):
        assert skipped not in ROUTE, f"the shared route still calls {skipped}"


def test_the_bail_records_why_it_was_taken():
    """"It went to MinerU" without "because Stage 1 could not read it" is the half of the
    story that makes the tier look like a failure rather than a routing decision."""
    assert 'fb_extra={"stage1_bail": bail}' in BAIL
    assert 'f"Stage 1 could not read this document: {bail[\'reason\']}"' in BAIL
    assert '"stage1_bailed.json"' in SRC


def test_the_recorded_shape_is_the_same_for_both_entrances():
    """Every consumer that explains "why is this document on the MinerU tier" reads
    fallback.reason / adopted_tier / chain. A second entrance with its own vocabulary would
    blank all of them, which is exactly why both now go through one helper."""
    for field in ("triggered=True", 'adopted_tier="mineru_full"', "chain=chain", "hard_fail"):
        assert field in ROUTE
    # and each entrance supplies its own reason and chain rather than inventing fields
    assert "_straight_to_mineru(" in BAIL and "_straight_to_mineru(" in SHORT


def test_stage1_only_does_not_secretly_pay_for_mineru():
    """--stage1-only exists to find out how much MinerU a corpus would need before committing
    to it. A route that runs MinerU anyway would answer the opposite question."""
    assert "if stage1_only:" in ROUTE
    assert ROUTE.index("if stage1_only:") < ROUTE.index("run_mineru_full")


# ---- the page count is a fact about the SOURCE, so it is asked first --------------------------

def test_the_page_count_is_asked_before_stage_1():
    """It was asked inside the fallback chain, i.e. after Stage 2 had already been paid for.
    Nothing the extraction produces can change a page count, so a short document was building
    a tree that the re-parse then replaced -- 341.6s across this corpus's 14 short documents,
    all 14 of which adopted MinerU's tree."""
    assert "pdf_page_count" in SHORT and "SHORT_DOC_MAX_PAGES" in SHORT
    assert SRC.index("# ---- PAGE COUNT:") < SRC.index('_step("stage1", he.run_stage1')


def test_the_page_count_route_skips_stage_1_as_well():
    """The difference from the bail route: there, Stage 1 ran and reported it could not read the
    document. Here it is never started, because the answer is already known."""
    assert "he.run_stage1" not in SHORT
    assert "_straight_to_mineru(" in SHORT


def test_the_kill_switch_still_wins_over_the_page_count():
    """DISABLE_MINERU_FALLBACK exists to iterate on Stage 1 without paying for MinerU. A route
    that fires before the switch is read would send every short document to MinerU anyway."""
    assert "DISABLE_MINERU_FALLBACK" in SHORT


def test_a_short_pdf_never_reaches_stage_1(tmp_path, monkeypatch):
    """The routing itself, run for real. Validation and scoring are stubbed because this test is
    about which functions the router calls, not about what they return."""
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "memo.pdf"
    doc = fitz.open()
    for i in range(6):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 96), f"Section {i + 1}", fontsize=18)
        page.insert_text((72, 130), "Body text that gives this page a real text layer.",
                         fontsize=11)
    doc.save(str(pdf))
    doc.close()
    assert fitz.open(str(pdf)).page_count == 6

    called = []
    monkeypatch.setattr(R.he, "run_stage1", lambda *a, **k: called.append("stage1"))
    monkeypatch.setattr(R.he, "run_stage2", lambda *a, **k: called.append("stage2") or {})
    monkeypatch.setattr(R.he, "run_stage3", lambda *a, **k: called.append("stage3"))
    monkeypatch.setattr(R, "_preflight_outline", lambda *a, **k: called.append("preflight"))
    monkeypatch.setattr(R, "run_mineru_full", lambda *a, **k: called.append("mineru_full") or {})
    monkeypatch.setattr(R, "run_chain",
                        lambda dest, p, val, sc, **k: called.append("run_chain") or (val, sc))
    monkeypatch.setattr(R, "_validate", lambda dest: {"passed": True})
    monkeypatch.setattr(R, "compute_scorecard", lambda dest, val: {
        "gate": "pass", "worst_score": 99.8,
        "dimensions": {"completeness": {"score": 99.8}},
        "structure": {"chunks": 1}})

    dest = tmp_path / "out"
    res = R.run_one({"label": "memo", "jurisdiction": "T", "doc_id": "memo", "pdf": pdf},
                    "TESTPROD", dest, False, True)

    assert called == ["mineru_full"], (
        "a 6-page document must reach MinerU without Stage 1, the pre-flight, Stage 2, Stage 3 "
        f"or the chain being started; it called {called}")
    assert res["fallback_tier"] == "mineru_full"
    assert "6 pages" in res["fallback_reason"] and "before Stage 1" in res["fallback_reason"]
    marker = json.loads((dest / "short_document.json").read_text())
    assert marker["pages"] == 6 and marker["decided"] == "before Stage 1"


def test_a_long_pdf_is_left_on_the_normal_route(tmp_path, monkeypatch):
    """The guard has to be a floor, not a ceiling: at 12 pages nothing about this route applies
    and Stage 1 must run."""
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "long.pdf"
    doc = fitz.open()
    for i in range(12):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 96), f"Section {i + 1}", fontsize=18)
    doc.save(str(pdf))
    doc.close()

    called = []
    monkeypatch.setattr(R.he, "run_stage1",
                        lambda *a, **k: called.append("stage1") or {"tables": []})
    monkeypatch.setattr(R, "_preflight_outline", lambda *a, **k: None)
    monkeypatch.setattr(R, "run_mineru_full", lambda *a, **k: called.append("mineru_full") or {})
    res = R.run_one({"label": "long", "jurisdiction": "T", "doc_id": "long", "pdf": pdf},
                    "TESTPROD", tmp_path / "out", True, True)   # stage1_only
    assert "stage1" in called and "mineru_full" not in called
    assert res["status"] == "stage1"


# ---- the last chance before giving up ----------------------------------------------------------

def test_no_structure_tries_the_printed_contents_page_first():
    """"No structure found" means no outline AND no heading-shaped text. A document that
    PRINTS its contents page still states its own structure, and building the outline from it
    keeps the document on the normal one-MinerU path for ~1.5s of work."""
    src = SRC[SRC.index("    bail = None"):SRC.index("if bail is not None:")]
    assert '_stage1_from_printed_toc' in src
    assert 'if cause == "no_structure":' in src, "the rebuild is tried for the wrong causes"
    assert src.index('cause == "no_structure"') < src.index("_stage1_from_printed_toc")


def test_a_scan_does_not_waste_time_on_a_printed_contents_page():
    """There is no text to parse a contents page out of."""
    src = SRC[SRC.index("    bail = None"):SRC.index("if bail is not None:")]
    guard = src[src.index('if cause == "no_structure":'):]
    assert "scanned" not in guard.split("_stage1_from_printed_toc")[0]


def test_the_rebuild_asks_the_product_rule_which_engine_to_use():
    """Third call site of rescue(); one that forgets undoes the other two."""
    fn = SRC[SRC.index("def _stage1_from_printed_toc"):SRC.index("def _preflight_outline")]
    assert "rebuild_outline_from_printed_toc" in fn and "no_prune=" in fn


def test_the_rebuild_records_a_distinct_trigger():
    """It is not the ordinary pre-flight: that outline was repaired because it looked wrong,
    this one was BUILT because Stage 1 had nothing to work with."""
    fn = SRC[SRC.index("def _stage1_from_printed_toc"):SRC.index("def _preflight_outline")]
    assert '"stage1_found_no_structure"' in fn


def test_the_rebuild_never_raises():
    """A last chance that throws is just a different way to lose the document."""
    fn = SRC[SRC.index("def _stage1_from_printed_toc"):SRC.index("def _preflight_outline")]
    assert "except Exception" in fn and "return None" in fn.split("except Exception")[1]


# ---- the needles are matched against the REAL message, not one copied from a comment ----------

def test_a_genuinely_image_only_pdf_classifies_as_scanned(tmp_path):
    """The whole route hangs on matching text pdf2mdtree emits. A test that asserts against a
    string pasted from a comment proves only that the paste was faithful, so this builds an
    actual image-only PDF and runs the real Stage 1 over it."""
    fitz = pytest.importorskip("fitz")
    import hybrid_extract as he

    pdf = tmp_path / "scanned.pdf"
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page(width=595, height=842)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 300, 400))
        pix.set_rect(pix.irect, (240, 240, 240))
        page.insert_image(fitz.Rect(50, 50, 545, 742), pixmap=pix)
    doc.save(str(pdf))
    doc.close()
    assert sum(len(fitz.open(str(pdf))[i].get_text("text")) for i in range(3)) == 0

    with pytest.raises(Exception) as excinfo:
        he.run_stage1(pdf, tmp_path / "out", 3, False)
    cause, why = R.classify_stage1_failure(str(excinfo.value))
    assert cause == "scanned", (
        "pdf2mdtree's message for an image-only PDF no longer matches the needles this route "
        f"keys on: {str(excinfo.value)[:200]}")
    assert "image" in why
