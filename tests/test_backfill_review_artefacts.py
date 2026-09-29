"""A REBUILT page must be able to say what the document actually is.

backfill_review_artefacts was written for one gap -- 123 documents that finished before the
worker built viewers at all -- and both of its defaults encode that: it selects only jobs
MISSING an artefact, and it downloads only the one tree the viewer draws. Those defaults quietly
became wrong as the page grew tabs that read other files:

  * a frozen inspect.html cannot pick up a new tab, so "only where absent" means every
    already-published document keeps the old page forever, and
  * the Stage 4 tab's numbers come from 04_stage4_ai/stage4_report.json and from a conservation
    diff of the stage-3 and stage-4 trees, while Scorecard 2 comes from scorecard_post_ai.json --
    none of which were being downloaded, so the rebuilt page rendered fully and said the AI pass
    had not run.

These tests pin the selection and the download set, which is where both faults lived.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_review_artefacts as B  # noqa: E402


def _fake_s3(tmp_path, monkeypatch, captured):
    """Stands in for the `aws` CLI: records every cp, and creates the destination for the ones
    whose source exists in a local 'bucket'. Only the ARGUMENTS matter to these tests."""
    bucket = tmp_path / "bucket"

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_aws(*args, profile=None):
        if args[:2] == ("s3", "cp"):
            src, dest = args[2], args[3]
            captured.append(src)
            rel = src.split("/job/", 1)[1]
            s, d = bucket / rel, Path(dest)
            if s.is_dir():
                d.mkdir(parents=True, exist_ok=True)
                for f in s.iterdir():
                    (d / f.name).write_bytes(f.read_bytes())
            elif s.is_file():
                d.parent.mkdir(parents=True, exist_ok=True)
                d.write_bytes(s.read_bytes())
        return R()

    monkeypatch.setattr(B, "_aws", fake_aws)
    return bucket


def test_every_stage_tree_is_downloaded_not_just_the_first(tmp_path, monkeypatch):
    """A stage-4/5 document must arrive with ALL its trees.

    build_review_artefacts hands the viewer the NEWEST stage present, and stage4_dashboard's
    conservation check diffs stage 3 against stage 4 -- so fetching 03_stage3_final alone
    produced a viewer of the AI pass's input and a Stage 4 tab that denied the pass happened."""
    bucket = _fake_s3(tmp_path, monkeypatch, cap := [])
    for d in ("03_stage3_final", "04_stage4_ai", "05_subchunks"):
        (bucket / d).mkdir(parents=True)
        (bucket / d / "01-clause.md").write_text("# Clause\n")
    (bucket / "source.pdf").write_bytes(b"%PDF-1.4 stub")

    monkeypatch.setattr(B, "ARTEFACTS", ("viewer.html", "inspect.html"))
    listing = {"scorecard.json", "03_stage3_final/", "04_stage4_ai/", "05_subchunks/"}
    built, detail = B.backfill_one("b", "corpus/run/", "prod/job", listing, None, dry_run=True)

    assert built, detail
    trees = [c for c in cap if c.rstrip("/").endswith(("03_stage3_final", "04_stage4_ai",
                                                       "05_subchunks"))]
    assert len(trees) == 3, f"only fetched {trees}"


def test_the_post_ai_scorecard_and_route_marker_are_fetched(tmp_path, monkeypatch):
    """Scorecard 2 and the summary-AI guard both read a file beside the job. Neither is
    required to exist -- but both must be ASKED for, or the page falls back to a verdict and
    a reason that are not this document's."""
    bucket = _fake_s3(tmp_path, monkeypatch, cap := [])
    (bucket / "03_stage3_final").mkdir(parents=True)
    (bucket / "03_stage3_final" / "01-clause.md").write_text("# Clause\n")
    (bucket / "source.pdf").write_bytes(b"%PDF-1.4 stub")

    B.backfill_one("b", "corpus/run/", "prod/job", {"scorecard.json", "03_stage3_final/"},
                   None, dry_run=True)

    for name in ("scorecard_post_ai.json", "summary_ai_rule.json"):
        assert any(c.endswith(name) for c in cap), f"{name} was never downloaded"


@pytest.mark.parametrize("rebuild, expected", [(False, ["p/no-page"]),
                                               (True, ["p/has-page", "p/no-page"])])
def test_rebuild_selects_documents_that_already_have_a_page(monkeypatch, capsys, rebuild,
                                                            expected):
    """Default stays "only what is missing"; --rebuild is what lets a CHANGED page reach the
    documents that already have one -- which, for a frozen page, is the only way it can."""
    jobs = {"p/has-page": {"scorecard.json", "viewer.html", "inspect.html"},
            "p/no-page": {"scorecard.json"}}
    monkeypatch.setattr(B, "job_dirs", lambda *a, **k: jobs)
    argv = ["backfill", "--run", "r", "--plan"] + (["--rebuild"] if rebuild else [])
    monkeypatch.setattr(sys, "argv", argv)

    B.main()

    listed = [ln.split()[0] for ln in capsys.readouterr().out.splitlines()
              if ln.startswith("      p/")]
    assert sorted(listed) == expected
