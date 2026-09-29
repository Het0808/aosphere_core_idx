"""MRAM documents whose content tile is "Summary" are extracted by the summary AI
pipeline, not by stages 1-3.

124_Marketing_Restrictions_-_Asset_Management holds two kinds of document. The 89
jurisdiction folders hold 76-163 page questionnaire SURVEYS, whose page-spanning tables are
the reason for every other rule in product_rules: flat depth-0 sections, clause-long table
runs, hybrid/medium effort. The Summary-tile documents are 5-10 page prose summaries, which
have none of that structure and carry their headings in coloured BARS rather than in the
text layer — see product_rules.AI_PIPELINE for the measurement and the cost.

The rule used to be matched on the jurisdiction FOLDER name ("Summary MRAM" / "SUMMARY").
It is now matched on OPINIONCONTENTTILENAME, a per-document field read from the
Doc_metadata.json that ships beside every PDF in the source corpus — a database field, not
a folder someone can rename.

The match is CONTAINS "summary", casefolded, not equality. United States files two of
these documents under one opinion, tiled "SEC Summary" and "CFTC Summary" rather than the
bare word — an exact-equality match (measured against the real corpus after the SUMMARY/
folders were merged into their jurisdictions on 2026-09-10) missed both and let them fall
through to the survey route.

These tests pin four things the rule depends on and nothing about the model itself:

  1. WHO it applies to. The rule is scoped to one product, and within it to documents whose
     own content tile reads "Summary"; the survey jurisdictions beside it must be
     untouched, or it silently re-routes 105 documents whose extraction every other rule
     here was measured against — and, now that this route costs money per document, bills
     for them too.
  2. That it beats the page-count test. A summary under the short-document bar would
     otherwise be extracted by the MinerU route the measurement rejected, and recorded
     with the page count as the reason. Nothing downstream would complain.
  3. That it is not satisfied by a CLONE. A rule pinning the route cannot be honoured by
     hard-linking a twin extracted by a different one.
  4. That ACI_SUMMARY_AI=0 SKIPS rather than falling through. Quietly re-routing to Stage 1
     would produce a plausible tree by the route this rule exists to avoid.

Nothing here calls Bedrock: extract_document is stubbed throughout, so the assertions are
about ROUTING alone and the suite costs nothing to run.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import product_rules as pr  # noqa: E402
import run_corpus as rc  # noqa: E402

MRAM = "124_Marketing_Restrictions_-_Asset_Management"


def _write_doc_metadata(pdf: Path, doc_id: str, content_tile: str | None) -> None:
    """The Doc_metadata.json sidecar the source corpus ships beside every PDF.

    `content_tile` omitted entirely when None, so a test can exercise a row that simply
    has no OPINIONCONTENTTILENAME field, not just an empty one."""
    row = {"FILENAME": doc_id, "DOCID": doc_id, "DOCNAME": "test"}
    if content_tile is not None:
        row["OPINIONCONTENTTILENAME"] = content_tile
    (pdf.parent / "Doc_metadata.json").write_text(json.dumps([row]))


# ---- 1. who the rule applies to ------------------------------------------------

@pytest.mark.parametrize("content_tile,expected", [
    ("Summary", True),
    ("summary", True),            # case is not load-bearing
    ("SUMMARY", True),
    (" Summary ", True),          # nor surrounding whitespace
    ("SEC Summary", True),        # United States: real tile, "summary" as a suffix
    ("CFTC Summary", True),       # United States: the other document under that opinion
    ("Survey", False),
    ("Summaries", False),         # "summary" is not a substring of "summaries"
    ("", False),
    (None, False),                # the field is simply absent from the row
])
def test_matches_only_documents_whose_content_tile_is_summary(tmp_path, content_tile, expected):
    pdf = tmp_path / "X.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    _write_doc_metadata(pdf, "X", content_tile)
    tile = pr.content_tile_name(pdf, "X")
    assert pr.ai_pipeline(MRAM, tile) is expected


@pytest.mark.parametrize("product", ["155_Data_Privacy", "104_Shareholding_Disclosure",
                                     "154_Marketing_Restrictions", "", None])
def test_scoped_to_one_product(product):
    """A Summary-tiled document under another product is NOT this rule's business."""
    assert pr.ai_pipeline(product, "Summary") is False


def test_rule_membership_is_exactly_what_was_measured():
    """A new entry here needs its own before/after -- see the module docstring.

    Tighter than it looks: this route bills per document, so widening the table without a
    measurement spends money on documents nobody checked it was right for."""
    assert pr.AI_PIPELINE == {MRAM}, \
        "add the measurement to product_rules.AI_PIPELINE before widening this rule"
    assert pr.AI_PIPELINE_CONTENT_TILE == "summary"
    assert pr.AI_PIPELINE_MODULE == "summary_ai_extract"


def test_the_old_vlm_rule_is_gone():
    """The full-VLM route these documents used to take was REPLACED, not left beside the
    new one. Two live routes for one document is a coin toss decided by branch order."""
    assert not hasattr(pr, "FULL_VLM_MINERU")
    assert not hasattr(pr, "full_vlm_mineru")
    assert not hasattr(pr, "FULL_VLM_BACKEND")


def test_rule_key_is_stored_casefolded():
    """ai_pipeline() casefolds the content tile, not the constant, so a mixed-case constant
    would never match anything and the rule would silently do nothing."""
    assert pr.AI_PIPELINE_CONTENT_TILE == pr.AI_PIPELINE_CONTENT_TILE.casefold()


def test_content_tile_name_reads_the_sidecar_beside_the_pdf(tmp_path):
    pdf = tmp_path / "181919.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    _write_doc_metadata(pdf, "181919", "Summary")
    assert pr.content_tile_name(pdf, "181919") == "Summary"
    assert pr.content_tile_name(pdf, "999999") is None, "wrong doc id must not match"


def test_content_tile_name_is_none_without_a_sidecar(tmp_path):
    """Most products ship no Doc_metadata.json at all -- a missing file is a routing
    answer of "no", not an error."""
    pdf = tmp_path / "X.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    assert pr.content_tile_name(pdf, "X") is None


def test_the_surveys_keep_every_other_rule():
    """The new rule must not have disturbed the ones measured on the surveys."""
    assert pr.section_depth(MRAM) == 0
    assert pr.clause_table_runs(MRAM) is True
    assert pr.mineru_effort(MRAM) == "medium"
    assert pr.sectioning_gates(MRAM) is True


# ---- 2. it is asked before the page-count test ---------------------------------

def _fake_extract(calls):
    """Stand in for summary_ai_extract.extract_document — records that it was called,
    writes the stage dir the real one writes, and calls NOTHING."""
    def fake(pdf, dest, **kw):
        calls["ai"] = True
        calls["ai_kwargs"] = kw
        (Path(dest) / "04_stage4_ai").mkdir(parents=True, exist_ok=True)
        (Path(dest) / "transcript.md").write_text("## Stub\n")
        sc = {"gate": "pass", "worst_score": 98.1, "weakest_dimension": "completeness",
              "dimensions": {}, "special_mode": {"mode": "ai_transcription"}}
        return {"report": {}, "scorecard": sc, "cost_usd": 0.23, "seconds": 1.0,
                "timing": {"pages": 9}, "split": None}
    return fake


def _route(monkeypatch, tmp_path, content_tile, pages, *, jurisdiction="SUMMARY",
          backend=None, stage1_only=False):
    """Run run_one far enough to see WHICH route it picks, with every extractor stubbed.

    Stubs the page count, both parses and the scoring, so the assertion is about routing
    alone: the test needs no GPU and spends no money."""
    calls = {}

    def fake_full(pdf, dest, *, backend=None, effort=None):
        calls["backend"], calls["effort"] = backend, effort
        (Path(dest) / "01_stage1_extract").mkdir(parents=True, exist_ok=True)
        return {}

    def fake_stage1(*a, **kw):
        # Recorded, not raised: run_one CLASSIFIES a Stage 1 exception into a routing
        # decision rather than letting it out, so an assert here would be swallowed and
        # the test would pass for the wrong reason.
        calls["stage1"] = True
        raise RuntimeError("stubbed Stage 1")

    import summary_ai_extract as sai
    monkeypatch.setattr(sai, "extract_document", _fake_extract(calls))
    monkeypatch.setattr(rc, "run_mineru_full", fake_full)
    monkeypatch.setattr(rc.he, "run_stage1", fake_stage1)
    # The no_structure branch would otherwise run a real printed-TOC rescue.
    monkeypatch.setattr(rc, "_stage1_from_printed_toc", lambda *a, **kw: None)
    monkeypatch.setattr(rc, "pdf_page_count", lambda p: pages)
    monkeypatch.setattr(rc, "_validate", lambda dest: {})
    monkeypatch.setattr(rc, "compute_scorecard", lambda dest, val: {"gate": "pass"})
    monkeypatch.setattr(rc, "result_is_acceptable", lambda sc: (True, None))
    monkeypatch.setattr(rc, "stage1_pages", lambda dest: pages)

    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    _write_doc_metadata(src, "X", content_tile)
    dest = tmp_path / "job"
    job = {"label": f"{jurisdiction}__X", "jurisdiction": jurisdiction,
           "doc_id": "X", "pdf": src}
    res = rc.run_one(job, MRAM, dest, stage1_only, True, backend, None)
    return res, dest, calls


def test_long_summary_still_takes_the_ai_route(monkeypatch, tmp_path):
    """The page-count test would NOT have caught this one -- 40 pages is over the bar."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 40)
    assert res["status"] == "done"
    assert res["route"] == "summary_ai"
    assert calls.get("ai") is True
    assert (dest / "summary_ai_rule.json").exists()
    assert not (dest / "short_document.json").exists()
    assert "backend" not in calls, "a summary reached MinerU"


def test_short_summary_is_recorded_as_the_rule_not_the_page_count(monkeypatch, tmp_path):
    """Under the 10-page bar the page-count test would route to MinerU. The rule must
    answer first, or the document is extracted by the route the measurement rejected."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 5)
    assert calls.get("ai") is True
    assert (dest / "summary_ai_rule.json").exists()
    assert not (dest / "short_document.json").exists()
    assert res["route"] == "summary_ai"


def test_stage1_is_never_reached_by_a_summary(monkeypatch, tmp_path):
    """The whole point: these documents carry their structure in bars, so the geometry
    path must not run at all -- not run and be discarded."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 9)
    assert calls.get("stage1") is None, "a summary reached Stage 1"
    assert calls.get("ai") is True


def test_the_route_carries_the_documents_identity(monkeypatch, tmp_path):
    """product/jurisdiction/doc_id are passed through, or the scorecard and corpus_meta
    the pipeline writes cannot be tied back to the document."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 9, jurisdiction="Germany")
    kw = calls["ai_kwargs"]
    assert kw["product"] == MRAM
    assert kw["jurisdiction"] == "Germany"
    assert kw["doc_id"] == "X"


def test_cost_is_reported_on_the_result(monkeypatch, tmp_path):
    """This is the only route that spends money, so the amount must leave run_one --
    a corpus run that cannot total its own spend cannot be budgeted."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 9)
    assert res["cost_usd"] == 0.23
    assert json.loads((dest / "timings.json").read_text())["cost_usd"] == 0.23


def test_a_survey_document_is_untouched(monkeypatch, tmp_path):
    """The 89 survey documents must still reach Stage 1, and must never be billed --
    whatever their own content tile name is (or however the folder they sit in is
    spelled), so long as it is not "Summary"."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Survey", 130, jurisdiction="Australia")
    assert calls.get("stage1") is True, "a survey document skipped Stage 1"
    assert calls.get("ai") is None, "a survey document was sent to the AI pipeline"
    assert not (dest / "summary_ai_rule.json").exists()
    assert calls.get("backend") is None, "a survey document was forced onto the VLM"


def test_a_document_with_no_metadata_sidecar_is_untouched(monkeypatch, tmp_path):
    """Most products ship no Doc_metadata.json at all -- absence must read as "not a
    summary", not crash the routing decision."""
    res, dest, calls = _route(monkeypatch, tmp_path, None, 130, jurisdiction="Australia")
    assert calls.get("stage1") is True
    assert calls.get("ai") is None


def test_a_short_survey_still_takes_the_page_count_route(monkeypatch, tmp_path):
    """The page-count test must keep working for everything the rule does not name."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Survey", 5, jurisdiction="Argentina")
    assert (dest / "short_document.json").exists()
    assert not (dest / "summary_ai_rule.json").exists()
    assert calls.get("ai") is None
    assert calls["backend"] is None, "the short-document route must not force a backend"


def test_rule_survives_the_fallback_kill_switch(monkeypatch, tmp_path):
    """DISABLE_MINERU_FALLBACK turns off the fallback CHAIN. This route is not a
    fallback -- nothing failed -- so it must still apply."""
    monkeypatch.setenv("DISABLE_MINERU_FALLBACK", "1")
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 5)
    assert calls.get("ai") is True
    assert (dest / "summary_ai_rule.json").exists()


# ---- 4. the kill switch skips, it does not fall through ------------------------

def test_disabling_the_route_skips_rather_than_re_routing(monkeypatch, tmp_path):
    """ACI_SUMMARY_AI=0 must not quietly hand these documents to Stage 1. That would
    produce a plausible tree by the route the rule exists to avoid, and the only trace
    would be the absence of a file nobody checks for."""
    monkeypatch.setenv("ACI_SUMMARY_AI", "0")
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 9)
    assert res["status"] == "skipped_summary_ai"
    assert calls.get("ai") is None, "the route ran despite being disabled"
    assert calls.get("stage1") is None, "a disabled summary fell through to Stage 1"
    assert calls.get("backend") is None, "a disabled summary fell through to MinerU"


def test_stage1_only_does_not_bill(monkeypatch, tmp_path):
    """--stage1-only is for iterating on Stage 1. There is no Stage 1 on this route, so
    it must skip -- and above all must not pay for a transcription nobody asked for."""
    res, dest, calls = _route(monkeypatch, tmp_path, "Summary", 9, stage1_only=True)
    assert res["status"] == "skipped_summary_ai"
    assert calls.get("ai") is None


# ---- 3. not satisfied by a clone -----------------------------------------------

def test_a_rule_bound_document_is_never_cloned(monkeypatch, tmp_path):
    """hash_idx offers a finished twin of the same bytes. Taking it would file a tree
    built by another route under a document the rule pins to the AI pipeline."""
    twin = tmp_path / "twin"
    (twin / "01_stage1_extract").mkdir(parents=True)
    (twin / "scorecard.json").write_text(json.dumps({"gate": "pass", "worst_score": 99}))
    (twin / "corpus_meta.json").write_text(json.dumps({"product": MRAM}))

    calls = {}
    monkeypatch.setattr(rc, "clone_job",
                        lambda s, d: calls.setdefault("cloned", True))
    import summary_ai_extract as sai
    monkeypatch.setattr(sai, "extract_document", _fake_extract(calls))
    monkeypatch.setattr(rc, "run_mineru_full",
                        lambda pdf, dest, *, backend=None, effort=None:
                        calls.setdefault("backend", backend) or {})
    monkeypatch.setattr(rc, "pdf_page_count", lambda p: 5)
    monkeypatch.setattr(rc, "_validate", lambda dest: {})
    monkeypatch.setattr(rc, "compute_scorecard", lambda dest, val: {"gate": "pass"})
    monkeypatch.setattr(rc, "result_is_acceptable", lambda sc: (True, None))
    monkeypatch.setattr(rc, "stage1_pages", lambda dest: 5)

    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    _write_doc_metadata(src, "X", "Summary")
    sha = rc.pdf_sha1(src)
    job = {"label": "SUMMARY__X", "jurisdiction": "SUMMARY",
           "doc_id": "X", "pdf": src}
    res = rc.run_one(job, MRAM, tmp_path / "job", False, True, None, None,
                     hash_idx={sha: twin})

    assert "cloned" not in calls, "the rule was satisfied by a clone of another route"
    assert res["status"] != "duplicate"
    assert calls.get("ai") is True, "the document did not reach the AI pipeline"
