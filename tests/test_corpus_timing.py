"""What a document cost to extract, recorded where the cost can still be found.

Wall clock used to live only in timings.json — which nothing reads, and which a CLONE
never receives (it is not in run_corpus.ARTIFACTS). So the scorecard, the artifact every
consumer actually opens, could not answer "what did this document cost". These tests pin
the shape of the block that now carries it, and the one case where inheriting a number
would actively lie: a hard-linked clone reporting the GPU hours its twin paid.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_corpus as rc  # noqa: E402


STEPS = {"stage1": 12.0, "toc_preflight": 13.0, "stage2_mineru": 1178.0,
         "stage3": 4.0, "validation": 2.0, "scorecard": 3.0}


def _block(steps=None, *, started=1000.0, finished=2212.0, **kw):
    kw.setdefault("pages", 137)
    kw.setdefault("tables", 14)
    kw.setdefault("mineru_crop_pages", 118)
    return rc.timing_block(steps or STEPS, started=started, finished=finished, **kw)


def test_the_total_is_wall_clock_not_the_sum_of_steps():
    """Steps are timed individually and do not tile the run — the gap between them is
    real time too, so a summed total would under-report every document."""
    b = _block()
    assert b["seconds"] == 1212.0
    assert b["seconds"] > sum(STEPS.values()) - 1000  # sanity: not the step sum


def test_steps_are_ranked_slowest_first():
    """Stage 2 dominates so heavily that any other ordering buries the only step whose
    cost ever moves."""
    b = _block()
    assert list(b["steps"])[0] == "stage2_mineru"
    assert b["slowest_step"] == "stage2_mineru"
    assert list(b["steps"].values()) == sorted(b["steps"].values(), reverse=True)


def test_cost_is_priced_per_cropped_page_not_per_document():
    """MinerU is billed by the page it is handed, and crop pages — not document pages —
    are what a corpus estimate has to multiply."""
    b = _block()
    assert b["seconds_per_crop_page"] == round(1178 / 118, 2)
    assert b["seconds_per_page"] == round(1212.0 / 137, 2)


def test_a_document_with_no_tables_does_not_divide_by_zero():
    b = _block(mineru_crop_pages=0, pages=None)
    assert b["seconds_per_crop_page"] is None
    assert b["seconds_per_page"] is None


def test_fallback_tiers_are_summed_out_of_the_total():
    """"What did escalating cost us" is a question the total alone cannot answer."""
    b = _block(dict(STEPS, toc_rescue=41.0, mineru_full=903.0), finished=3156.0)
    assert b["fallback_seconds"] == 944.0
    assert b["seconds"] == 2156.0


def test_a_document_that_never_escalated_reports_no_fallback_cost():
    assert _block()["fallback_seconds"] == 0


def test_the_window_is_recorded_so_a_document_can_be_matched_to_a_log():
    b = _block()
    assert b["started_at"] == 1000.0 and b["finished_at"] == 2212.0


# ---------------------------------------------------------------- the clone case

@pytest.fixture
def twin(tmp_path):
    """A finished job dir whose scorecard already carries a real extraction cost."""
    d = tmp_path / "twin"
    for sub in rc.ARTIFACTS:
        if not sub.endswith(".json"):
            (d / sub).mkdir(parents=True)
    d.mkdir(exist_ok=True)
    (d / "validation.json").write_text("{}")
    (d / "scorecard.json").write_text(json.dumps({
        "gate": "review", "worst_score": 84.3,
        "timing": _block()}))
    return d


def _run_clone(tmp_path, twin_dir):
    pdf = tmp_path / "src.pdf"
    pdf.write_bytes(b"%PDF-1.4\nnot a real pdf, never parsed on this path\n")
    dest = tmp_path / "dest"
    job = {"pdf": pdf, "jurisdiction": "Peru", "doc_id": "999", "label": "Peru__999"}
    sha = rc.pdf_sha1(pdf)
    out = rc.run_one(job, "124_Prod", dest, False, False, None, None, None,
                     hash_idx={sha: twin_dir})
    return out, json.loads((dest / "scorecard.json").read_text())


def test_a_clone_is_recognised_and_does_not_re_extract(tmp_path, twin):
    out, _ = _run_clone(tmp_path, twin)
    assert out["status"] == "duplicate"


def test_a_clone_does_not_inherit_the_twins_extraction_cost(tmp_path, twin):
    """The failure this guards: 53 hard-linked clones each reporting 1212s would put
    18 GPU-hours into a capacity estimate that only ever spent 20 minutes."""
    _, sc = _run_clone(tmp_path, twin)
    assert sc["timing"]["seconds"] < 30, "a clone is a hard-link, not an extraction"
    assert sc["timing"]["steps"] == {}
    assert sc["timing"]["slowest_step"] is None


def test_a_clone_still_records_what_the_extraction_originally_cost(tmp_path, twin):
    """The twin's number is not deleted — it is relabelled, so the real cost of the
    tree on disk is still answerable, just not double-counted."""
    _, sc = _run_clone(tmp_path, twin)
    assert sc["timing"]["extraction_seconds"] == 1212.0
    assert sc["timing"]["cloned_from"] == "twin"


def test_a_real_extraction_is_not_marked_as_a_clone():
    assert _block()["cloned_from"] is None


# ---------------------------------------------------------------- the page count

def test_page_count_comes_from_the_stage1_report(tmp_path):
    """Not from the tables manifest: that carries only source_pdf/tables/footnote_ids,
    which is why every timings.json ever written recorded "pages": null."""
    (tmp_path / "01_stage1_extract").mkdir(parents=True)
    (tmp_path / "01_stage1_extract" / "stage1_report.json").write_text(
        json.dumps({"pages": 93, "paragraphs": 349}))
    assert rc.stage1_pages(tmp_path) == 93


@pytest.mark.parametrize("report", [None, {}, {"pages": 0}, {"pages": None}, "not json"])
def test_a_missing_or_useless_page_count_is_None_not_a_crash(tmp_path, report):
    """A document scored before the report existed must still produce a timing block."""
    (tmp_path / "01_stage1_extract").mkdir(parents=True)
    if report is not None:
        p = tmp_path / "01_stage1_extract" / "stage1_report.json"
        p.write_text(report if isinstance(report, str) else json.dumps(report))
    assert rc.stage1_pages(tmp_path) is None


def test_both_spellings_of_the_mineru_tier_count_as_fallback():
    """fallback_chain names this tier "mineru_full" when a short document skips straight
    to it, and "mineru_fallback" when a normal document escalates into it. They are the
    same tier; counting only one under-reports the cost of every escalated document.
    India 169716 reported 33s of fallback against a real 86s because of this."""
    short = _block(dict(STEPS, mineru_full=52.9))
    normal = _block(dict(STEPS, mineru_fallback=52.9))
    assert short["fallback_seconds"] == normal["fallback_seconds"] == 52.9


def test_a_document_that_ran_both_tiers_sums_them():
    b = _block(dict(STEPS, toc_rescue=33.4, mineru_fallback=52.9))
    assert b["fallback_seconds"] == 86.3
