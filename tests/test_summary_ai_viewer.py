"""build_review_artefacts must find a document's newest tree, not just 03_stage3_final.

The summary AI route (product_rules.AI_PIPELINE / summary_ai_extract.py) never writes
03_stage3_final -- there is no stage 1-3 on that route -- so hardcoding that one name here
silently built no viewer.html for every one of those documents. Not cosmetic:
PROMOTION_PIPELINE.md's `no_viewer` gate refuses to promote a document with no viewer, so a
summary-AI job that scored `pass` would still be unpublishable until someone noticed and ran
backfill_review_artefacts by hand.
"""
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_worker as W  # noqa: E402


def _stub_push_hybrid_s3(monkeypatch, calls):
    """The real push_hybrid_s3 imports hybrid_extract -> pdf2mdtree -> fitz -> MinerU at
    module scope (see its own docstring). Not what this test exercises: only WHICH tree and
    subtitle build_review_artefacts hands it."""
    mod = ModuleType("push_hybrid_s3")

    def build_viewer(tree_dir, pdf_path, title, subtitle, stages=None):
        calls["tree_dir"] = Path(tree_dir)
        calls["subtitle"] = subtitle
        calls["stages"] = stages
        return "<html>stub viewer</html>"

    def build_inspect(job_dir, title):
        return None

    mod.build_viewer = build_viewer
    mod.build_inspect = build_inspect
    monkeypatch.setitem(sys.modules, "push_hybrid_s3", mod)


def test_summary_ai_route_gets_a_viewer_from_04_stage4_ai(tmp_path, monkeypatch):
    """The whole point: this route has no 03_stage3_final, only 04_stage4_ai (written in the
    same stage-3 shape by summary_ai_extract.split), and that tree must still get a viewer."""
    dest = tmp_path / "job"
    (dest / "04_stage4_ai").mkdir(parents=True)
    (dest / "04_stage4_ai" / "01-overview.md").write_text("# Overview\n\npage 1\n")
    (dest / "source.pdf").write_bytes(b"%PDF-1.4 stub")

    calls = {}
    _stub_push_hybrid_s3(monkeypatch, calls)
    made = W.build_review_artefacts(dest, "124_Marketing_Restrictions_-_Asset_Management",
                                    "SUMMARY__Argentina", route="summary_ai")

    assert "viewer.html" in made, "no viewer built for the summary AI route"
    assert calls["tree_dir"] == dest / "04_stage4_ai"
    assert "summary ai" in calls["subtitle"].lower()
    assert calls["stages"] is None, "only one tree exists; the stage-switcher must not fire"
    assert (dest / "viewer.html").read_text() == "<html>stub viewer</html>"


def test_the_normal_route_is_unaffected(tmp_path, monkeypatch):
    """Regression: a stage-1-3 document must still get its viewer from 03_stage3_final,
    exactly as before this fix, with the ordinary subtitle."""
    dest = tmp_path / "job"
    (dest / "03_stage3_final").mkdir(parents=True)
    (dest / "03_stage3_final" / "01-clause.md").write_text("# Clause\n\npage 1\n")
    (dest / "source.pdf").write_bytes(b"%PDF-1.4 stub")

    calls = {}
    _stub_push_hybrid_s3(monkeypatch, calls)
    made = W.build_review_artefacts(dest, "155_Data_Privacy", "Australia__181815", route=None)

    assert "viewer.html" in made
    assert calls["tree_dir"] == dest / "03_stage3_final"
    assert "pdf2mdtree" in calls["subtitle"]


def test_a_summary_ai_job_with_stage4_and_no_stage3_prefers_stage4(tmp_path, monkeypatch):
    """max(stages) picks the newest tree generically -- no route special-case needed for
    resolving WHICH directory, only for the subtitle text."""
    dest = tmp_path / "job"
    (dest / "04_stage4_ai").mkdir(parents=True)
    (dest / "04_stage4_ai" / "x.md").write_text("# X\n")
    (dest / "source.pdf").write_bytes(b"%PDF-1.4 stub")

    calls = {}
    _stub_push_hybrid_s3(monkeypatch, calls)
    W.build_review_artefacts(dest, "124_Marketing_Restrictions_-_Asset_Management",
                             "SUMMARY", route="summary_ai")
    assert calls["tree_dir"] == dest / "04_stage4_ai"


def test_no_tree_at_all_builds_no_viewer(tmp_path, monkeypatch):
    dest = tmp_path / "job"
    dest.mkdir(parents=True)
    (dest / "source.pdf").write_bytes(b"%PDF-1.4 stub")

    calls = {}
    _stub_push_hybrid_s3(monkeypatch, calls)
    made = W.build_review_artefacts(dest, "155_Data_Privacy", "Nowhere__1", route=None)
    assert "viewer.html" not in made
    assert "tree_dir" not in calls


# ---- backfill_review_artefacts.py: same distinction, made from an S3 listing -------------

import backfill_review_artefacts as B  # noqa: E402


def test_backfill_downloads_04_stage4_ai_for_a_summary_ai_job(tmp_path, monkeypatch):
    """A summary-AI job in S3 has no 03_stage3_final at all. Downloading that (as this script
    did before) syncs nothing, builds a viewer over an empty tree, and calls it done."""
    calls = {"aws": []}

    def fake_aws(*args, profile=None):
        calls["aws"].append(args)
        if args[:2] == ("s3", "cp") and str(args[2]).endswith("/source.pdf"):
            Path(args[3]).write_bytes(b"%PDF-1.4 stub")
        class _R:
            returncode, stdout, stderr = 0, "", ""
        return _R()

    def fake_build_review_artefacts(dest, product, label, route=None):
        calls["route"] = route
        (dest / "viewer.html").write_text("<html/>")
        return ["viewer.html"]

    monkeypatch.setattr(B, "_aws", fake_aws)
    import corpus_worker as W2
    monkeypatch.setattr(W2, "build_review_artefacts", fake_build_review_artefacts)

    files = {"scorecard.json", "summary_ai_rule.json", "corpus_meta.json"}
    built, detail = B.backfill_one("bucket", "corpus/run1/", "124_MRAM/SUMMARY__Argentina",
                                   files, None, dry_run=False)

    assert built, detail
    assert calls["route"] == "summary_ai"
    tree_downloads = [a for a in calls["aws"] if a[:2] == ("s3", "cp") and "--recursive" in a]
    assert len(tree_downloads) == 1
    assert "04_stage4_ai" in tree_downloads[0][2]
    assert "03_stage3_final" not in tree_downloads[0][2]


def test_backfill_still_downloads_03_stage3_final_for_a_normal_job(tmp_path, monkeypatch):
    calls = {"aws": []}

    def fake_aws(*args, profile=None):
        calls["aws"].append(args)
        if args[:2] == ("s3", "cp") and str(args[2]).endswith("/source.pdf"):
            Path(args[3]).write_bytes(b"%PDF-1.4 stub")
        class _R:
            returncode, stdout, stderr = 0, "", ""
        return _R()

    def fake_build_review_artefacts(dest, product, label, route=None):
        calls["route"] = route
        (dest / "viewer.html").write_text("<html/>")
        return ["viewer.html"]

    monkeypatch.setattr(B, "_aws", fake_aws)
    import corpus_worker as W2
    monkeypatch.setattr(W2, "build_review_artefacts", fake_build_review_artefacts)

    files = {"scorecard.json", "corpus_meta.json"}   # no summary_ai_rule.json
    built, detail = B.backfill_one("bucket", "corpus/run1/", "155_Data_Privacy/Australia__1",
                                   files, None, dry_run=False)

    assert built, detail
    assert calls["route"] is None
    tree_downloads = [a for a in calls["aws"] if a[:2] == ("s3", "cp") and "--recursive" in a]
    assert len(tree_downloads) == 1
    assert "03_stage3_final" in tree_downloads[0][2]
