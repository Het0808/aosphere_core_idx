"""The Doc Gallery's extraction-scorecard roll-up (product / jurisdiction / document).

Runs against a temp LocalDocStore, so it exercises the real manifest + scorecard
reading path rather than a stubbed tree.
"""

import json

import pytest

from aosphere_core_index.service import doc_gallery as dg


def _doc(product, region, slug, gate=None, worst=None, scorecard=None, stats=None):
    d = {"product": product, "region": region, "slug": slug, "doc_id": slug[-6:],
         "status": "ok", "viewer": f"{slug}-viewer.html", "stats": stats or {}}
    if gate:
        d.update(gate=gate, worst_score=worst, scorecard=f"{slug}-scorecard.json")
    if scorecard is not None:
        d["scorecard"] = f"{slug}-scorecard.json"
    return d


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    """A manifest with: a failing + a passing MinerU doc in one jurisdiction, a
    review doc in another, and an unscored (pre-scorecard) original."""
    man = [
        _doc("repoAnalytics", "Anguilla (MinerU)", "repo-anguilla-1-mineru", "fail", 0.0),
        _doc("repoAnalytics", "Anguilla (MinerU)", "repo-anguilla-2-mineru", "pass", 96.0),
        _doc("repoAnalytics", "Armenia (MinerU)", "repo-armenia-1-mineru", "review", 80.0),
        _doc("repoAnalytics", "Anguilla", "repo-anguilla-orig",
             stats={"pages": 12, "tables_converted": 3}),
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    (tmp_path / "repo-anguilla-1-mineru-scorecard.json").write_text(json.dumps({
        "gate": "fail", "worst_score": 0.0, "weakest_dimension": "completeness",
        "active_finding_count": 51, "dismissed_count": 2,
        "dimensions": {"completeness": {"label": "Completeness", "score": 0.0,
                                        "critical": True,
                                        "detail": {"coverage_pct": 42.999,
                                                   "files_scanned": 54,
                                                   "files_with_silent_gap": 24}}},
        "tables": [{"bucket": "failed"}, {"bucket": "clean"}, {"bucket": "clean"}],
        "pages": {"total": 38, "counts": {"silent": 15, "ok": 23}},
        "headings": {"total": 81, "matched": 77},
    }))
    for slug, gate, worst in (("repo-anguilla-2-mineru", "pass", 96.0),
                              ("repo-armenia-1-mineru", "review", 80.0)):
        (tmp_path / f"{slug}-scorecard.json").write_text(json.dumps({
            "gate": gate, "worst_score": worst, "weakest_dimension": "fidelity",
            "active_finding_count": 0, "dimensions": {"fidelity": {"label": "Fidelity"}},
            "tables": [],
        }))
    # one doc also carries the frozen six-tab inspection page
    man[0]["inspect"] = "repo-anguilla-1-mineru-inspect.html"
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    (tmp_path / "repo-anguilla-1-mineru-inspect.html").write_text("<html>tabs</html>")
    monkeypatch.setattr(dg, "_store", dg.LocalDocStore(tmp_path))
    dg._sc_summary_cache.clear()
    yield tmp_path
    dg._store = None
    dg._sc_summary_cache.clear()


def test_verdict_is_the_weakest_document_not_the_average(gallery):
    f = dg.gallery_tree()["folders"][0]
    # 0 and 96 in one jurisdiction, 80 in another: a mean would read 58.7 and look
    # merely mediocre — the verdict has to come from the 0.
    assert f["verdict"] == "fail"
    assert f["worst_score"] == 0.0
    assert f["mean_score"] == 58.7
    assert (f["pass"], f["review"], f["fail"]) == (1, 1, 1)


def test_folder_counts_are_the_sum_of_its_jurisdictions(gallery):
    f = dg.gallery_tree()["folders"][0]
    for k in ("pass", "review", "fail", "total"):
        assert f[k] == sum(j[k] for j in f["jurisdictions"]), k


def test_unscored_doc_is_left_out_of_the_scorecard(gallery):
    """A doc published without a scorecard has no verdict to show — in a scorecard
    table it is a row of dashes, so it is excluded rather than counted as a failure."""
    f = dg.gallery_tree()["folders"][0]
    anguilla = next(j for j in f["jurisdictions"] if j["jurisdiction"] == "Anguilla")
    # the MinerU variants group under the bare jurisdiction; the unscored original
    # published there is not among them
    assert anguilla["total"] == 2
    assert [d["mineru"] for d in anguilla["documents"]] == [True, True]
    assert anguilla["scored"] == 2
    assert anguilla["mean_score"] == 48.0   # (0 + 96) / 2
    assert dg.gallery_tree()["totals"]["documents"] == 3   # not 4


def test_worst_document_sorts_first(gallery):
    f = dg.gallery_tree()["folders"][0]
    assert [j["jurisdiction"] for j in f["jurisdictions"]] == ["Anguilla", "Armenia"]
    assert f["jurisdictions"][0]["documents"][0]["worst_score"] == 0.0


def test_document_row_carries_the_scorecard_numbers(gallery):
    f = dg.gallery_tree()["folders"][0]
    d = f["jurisdictions"][0]["documents"][0]
    assert d["weakest"] == "Completeness"
    assert (d["tables"], d["tables_failed"], d["findings"]) == (3, 1, 51)
    assert d["mineru"] is True


def test_document_row_carries_the_extraction_detail(gallery):
    """The row shows the report card itself — there is no inline expansion."""
    d = dg.gallery_tree()["folders"][0]["jurisdictions"][0]["documents"][0]
    assert d["dims"] == [{"label": "Completeness", "score": 0.0, "critical": True}]
    assert (d["pages"], d["pages_silent"]) == (38, 15)
    assert (d["headings_matched"], d["headings_total"]) == (77, 81)
    assert (d["coverage"], d["sections"], d["sections_silent"]) == (43.0, 54, 24)
    assert d["dismissed"] == 2


def test_unscored_original_still_browsable_in_the_card_list(gallery):
    """Excluded from the scorecard table, but the card view still lists it with the
    manifest stats it does have."""
    cards = {d["slug"]: d for d in dg.gallery_list()}
    orig = cards["repo-anguilla-orig"]
    assert orig["pages"] == 12
    assert orig["tables"] == 3           # tables_converted
    assert orig["gate"] is None and orig["has_scorecard"] is False


def test_inspection_page_is_served_only_where_published(gallery):
    """The scorecard link opens the frozen dashboard page when a doc has one; docs
    published before that wiring fall back to the scorecard-JSON render."""
    assert dg.inspect_html("repo-anguilla-1-mineru") == b"<html>tabs</html>"
    assert dg.inspect_html("repo-anguilla-2-mineru") is None      # no "inspect" key
    assert dg.inspect_html("no-such-slug") is None
    rows = {d["slug"]: d for d in dg.gallery_list()}
    assert rows["repo-anguilla-1-mineru"]["has_inspect"] is True
    assert rows["repo-anguilla-2-mineru"]["has_inspect"] is False


def test_variant_filters_before_the_roll_up(gallery):
    t = dg.gallery_tree("mineru")
    assert t["totals"]["documents"] == 3
    # variants count SCORED docs, so the buttons never offer an empty view
    assert t["totals"]["variants"] == {"all": 3, "mineru": 3, "original": 0}
    o = dg.gallery_tree("original")
    assert o["totals"]["documents"] == 0 and o["folders"] == []
