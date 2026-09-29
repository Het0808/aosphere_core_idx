"""The tree stage 4/5 leaves behind gets scored, into its own file.

Nothing measured stage 4 or 5. That is how the run in commit 1affed9 -- every section
failing with KeyError, all eight "kept as stage 3" -- still reported gate=pass: the gate
reads stages 1-3, and a stage-4 rejection count is read by nothing. Measured on Bahamas
183503, stage 3 against stage 5: completeness 99.2 -> 94.6, fidelity 91.9 -> 45.8.

Two properties are load-bearing and both are asserted here:

  * scorecard.json is NEVER rewritten. It is the --resume completion marker, its `timing`
    is extraction cost, and a paid pass that fails must leave a clean extraction clean.
    The post-AI score is a second file beside it.
  * the extraction gate is unchanged when no stage is passed. `validate(root)` and
    `validate(root, stage=None)` must stay byte-identical to what they were, or every
    stored scorecard in the corpus becomes incomparable with a fresh one.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lib_validate as lv  # noqa: E402
import run_corpus as rc  # noqa: E402


def _job(tmp_path: Path, stages=(1, 3)) -> Path:
    """A job dir with the named stage dirs, each holding one markdown file."""
    job = tmp_path / "Country__1"
    for s in stages:
        d = job / {1: "01_stage1_extract", 3: "03_stage3_final",
                   4: "04_stage4_ai", 5: "05_subchunks"}[s]
        d.mkdir(parents=True)
        (d / "01-intro.md").write_text("# Intro\n\nbody\n")
    return job


# ---- final_stage: which tree is "the document as it now stands" -----------------

@pytest.mark.parametrize("stages,want", [
    ((1, 3), 3),
    ((1, 3, 4), 4),
    ((1, 3, 4, 5), 5),
    ((1,), 3),          # nothing to score; callers treat <=3 as "skip"
])
def test_final_stage_is_the_highest_output_tree_present(tmp_path, stages, want):
    assert lv.final_stage(_job(tmp_path, stages)) == want


def test_final_stage_ignores_stage_2(tmp_path):
    """02_ is MinerU's per-table HTML/JSON, not a markdown tree — scoring it would
    compare the PDF against a directory of table fragments."""
    job = _job(tmp_path, (1, 3))
    (job / "02_stage2_mineru_tables").mkdir()
    assert lv.final_stage(job) == 3


# ---- the pipeline actually reaches the re-score --------------------------------

def test_pipeline_writes_a_post_ai_scorecard_after_stage_5(tmp_path, monkeypatch):
    """_run_stage4_in_pipeline must call the re-score once stage 4 reports success.

    Stubbed at run_stage4 and run_and_gate: this asserts the WIRING, so it needs neither
    Bedrock nor a real PDF."""
    job = _job(tmp_path, (1, 3, 4, 5))
    seen = {}

    def fake_run_stage4(*a, **kw):
        return {"ran": True, "seconds": 12.0, "sections_accepted": 1, "sections_total": 1,
                "usage": {"total_tokens": 10, "cost_usd": 0.01}, "subchunk": {"seconds": 0.3}}

    def fake_run_and_gate(root, stage=None):
        seen["stage"] = stage
        return {"passed": True}, {"gate": "review", "worst_score": 81.2,
                                  "weakest_dimension": "fidelity", "scored_stage": stage}

    monkeypatch.setattr(rc, "_stage4_enabled", lambda: True)
    monkeypatch.setitem(sys.modules, "_stub", None)
    monkeypatch.setattr(lv, "run_and_gate", fake_run_and_gate)
    import aosphere_core_index.extract.ai_postprocess as ai
    monkeypatch.setattr(ai, "run_stage4", fake_run_stage4)
    monkeypatch.setattr(ai, "resolve_model", lambda n=None: "stub-model")

    steps: dict = {}
    rc._run_stage4_in_pipeline(job, tmp_path / "src.pdf", steps, log=lambda m: None)

    assert seen["stage"] == 5, "the re-score must target the stage 5 tree, not stage 3"
    sc = json.loads((job / "scorecard_post_ai.json").read_text())
    assert sc["scored_stage"] == 5 and sc["gate"] == "review"
    assert (job / "validation_post_ai.json").exists()
    assert "scorecard_post_ai" in steps, "the seconds must reach the ledger row"


def test_post_ai_scorecard_never_overwrites_the_extraction_gate(tmp_path, monkeypatch):
    job = _job(tmp_path, (1, 3, 4, 5))
    gate = {"gate": "pass", "worst_score": 97.9, "scored_stage": None}
    (job / "scorecard.json").write_text(json.dumps(gate))

    monkeypatch.setattr(lv, "run_and_gate",
                        lambda root, stage=None: ({}, {"gate": "fail", "worst_score": 45.8,
                                                       "scored_stage": stage}))
    rc._score_post_ai(job, {}, log=lambda m: None)

    assert json.loads((job / "scorecard.json").read_text()) == gate
    assert json.loads((job / "scorecard_post_ai.json").read_text())["gate"] == "fail"


def test_no_post_ai_scorecard_when_stage_4_never_ran(tmp_path, monkeypatch):
    """A document that stopped at stage 3 has nothing extra to score, and must not get
    a second file implying it does."""
    job = _job(tmp_path, (1, 3))
    called = []
    monkeypatch.setattr(lv, "run_and_gate",
                        lambda root, stage=None: called.append(stage) or ({}, {}))
    rc._score_post_ai(job, {}, log=lambda m: None)
    assert not called and not (job / "scorecard_post_ai.json").exists()


def test_a_failed_re_score_does_not_raise(tmp_path, monkeypatch):
    """This is a report, not a gate. It must not be able to fail a finished document."""
    job = _job(tmp_path, (1, 3, 4, 5))

    def boom(root, stage=None):
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr(lv, "run_and_gate", boom)
    msgs: list[str] = []
    rc._score_post_ai(job, {}, log=msgs.append)          # must not raise
    assert any("FAILED" in m for m in msgs), "a swallowed failure still has to be said"


# ---- the extraction gate is untouched ------------------------------------------

def test_stage_none_leaves_every_check_on_its_own_default(monkeypatch):
    """The default path must pass NO stage argument at all — not stage=3.

    table_placement and heading_hierarchy default to stage 1 (they read stage 1's regions
    and outline headings), so passing 3 to everything would silently re-point them and
    change the stored gate for every document in the corpus."""
    got: dict[str, tuple] = {}

    def rec(name):
        def fn(root, **kw):
            got[name] = tuple(sorted(kw.items()))
            return {"passed": True}
        return fn

    monkeypatch.setattr(lv, "CHECKS", tuple((n, rec(n)) for n, _ in lv.CHECKS))
    lv.validate(Path("."))
    assert all(kw == () for kw in got.values()), f"a check was given a stage: {got}"


def test_stage_is_passed_only_to_the_tree_vs_pdf_checks(monkeypatch):
    got: dict[str, tuple] = {}

    def rec(name):
        def fn(root, **kw):
            got[name] = tuple(sorted(kw.items()))
            return {"passed": True}
        return fn

    monkeypatch.setattr(lv, "CHECKS", tuple((n, rec(n)) for n, _ in lv.CHECKS))
    out = lv.validate(Path("."), stage=5)

    for name, kw in got.items():
        if name in lv.STAGE_SCOPED:
            assert kw == (("stage", 5),), f"{name} should have been re-pointed"
        elif name in lv.TREE_STAGE_SCOPED:
            # Takes the scored stage as `tree_stage`, never as `stage`: it reads Stage
            # 1's headings manifest AND an output tree, and only the tree may move.
            # Passing `stage` would look for the manifest under 05_subchunks and skip.
            assert kw == (("tree_stage", 5),), f"{name} should have been re-pointed"
        else:
            assert kw == (), f"{name} must keep its own default"
    assert out["scored_stage"] == 5


def test_input_side_checks_are_not_stage_scoped():
    """Named explicitly, so adding one to STAGE_SCOPED by reflex fails here first.

    heading_hierarchy is on this list and yet IS re-pointed — via TREE_STAGE_SCOPED,
    because the thing that moves is the tree it compares against, not the manifest it
    reads. Putting it in STAGE_SCOPED would send `stage=5` to the manifest lookup and
    silently skip the check."""
    for name in ("table_placement", "heading_hierarchy", "engine_agreement",
                 "toc_quality", "stage4"):
        assert name not in lv.STAGE_SCOPED
    assert lv.STAGE_SCOPED <= {n for n, _ in lv.CHECKS}
    assert lv.TREE_STAGE_SCOPED <= {n for n, _ in lv.CHECKS}
    assert not (lv.STAGE_SCOPED & lv.TREE_STAGE_SCOPED)
