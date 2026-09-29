"""Stage 4 runs inside the pipeline when the host has opted in.

The flag existed and nothing honoured it. `ACI_STAGE4_AI_ENABLED=1` on a server changed
nothing at all: stage 4 was reachable only from the command line, which meant no ledger
row ever carried an AI step, and the Core Index could show the two stages while no
document could ever be in one.

The placement is the delicate part, and these tests pin its consequences rather than its
line number: the paid pass must not alter the scorecard's extraction cost, must not be
able to un-complete a document that extracted correctly, and must leave its measured
seconds somewhere the ledger will carry them.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_corpus as rc  # noqa: E402


@pytest.fixture
def job(tmp_path):
    (tmp_path / "03_stage3_final").mkdir()
    (tmp_path / "source.pdf").write_bytes(b"%PDF-1.4 fake")
    return tmp_path


def _report(**kw):
    r = {"mode": "section", "model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
         "seconds": 484.0, "sections_total": 8, "sections_accepted": 8,
         "usage": {"total_tokens": 174794, "cost_usd": 1.0092},
         "subchunk": {"seconds": 0.3, "subchunks": 25, "sections_split": 4}}
    r.update(kw)
    return r


# ---- the gate ---------------------------------------------------------------------

def test_the_gate_is_the_setting_not_a_local_copy_of_it(monkeypatch):
    """One definition, so a caller cannot spend money by reaching past it. run_stage4
    checks the same flag with force=False, which is the second half of the same rule."""
    from aosphere_core_index.config import settings
    monkeypatch.setattr(settings, "stage4_ai_enabled", False, raising=False)
    assert rc._stage4_enabled() is False
    monkeypatch.setattr(settings, "stage4_ai_enabled", True, raising=False)
    assert rc._stage4_enabled() is True


def test_the_gate_is_closed_by_default():
    """A deployment opts in. Stage 4 is the only stage that calls a paid API and the only
    one that can alter a document's text."""
    from aosphere_core_index.config import Settings
    assert Settings().stage4_ai_enabled is False


# ---- what the pipeline does with the result ---------------------------------------

def test_the_measured_seconds_reach_the_steps_dict(job, monkeypatch):
    """`steps` is the only way the timings leave here: run_one returns it, corpus_worker
    writes it onto the ledger row, and the Core Index reads that row. Without this the
    run does the work and no screen knows."""
    import aosphere_core_index.extract.ai_postprocess as ai
    monkeypatch.setattr(ai, "run_stage4", lambda *a, **k: _report())
    steps = {"stage1": 6.0, "total": 590.7}
    rc._run_stage4_in_pipeline(job, job / "source.pdf", steps, log=lambda *_: None)
    assert steps["stage4_ai"] == 484.0
    assert steps["stage5_subchunk"] == 0.3
    # and it must NOT have grown `total`, which every capacity estimate built from a
    # finished corpus reads as extraction time
    assert steps["total"] == 590.7


def test_the_cost_is_returned_not_folded_into_steps(job, monkeypatch):
    """`steps` is formatted as durations everywhere it is read (exDur on the Core Index),
    so a dollar figure hiding in there would render as "$1.0092 seconds" of AI
    post-processing. The cost comes back as the function's return value instead, which
    run_one carries onto its own result as `cost_usd` -- the field corpus_worker.finished()
    already reads for the summary-AI route."""
    import aosphere_core_index.extract.ai_postprocess as ai
    monkeypatch.setattr(ai, "run_stage4", lambda *a, **k: _report())
    steps = {"stage1": 6.0}
    cost = rc._run_stage4_in_pipeline(job, job / "source.pdf", steps, log=lambda *_: None)
    assert cost == 1.0092
    assert "cost_usd" not in steps and "cost" not in steps


def test_a_refusal_or_failure_reports_no_cost(job, monkeypatch):
    """Nothing sent, nothing spent -- and a crash must not invent a figure either."""
    import aosphere_core_index.extract.ai_postprocess as ai
    monkeypatch.setattr(ai, "run_stage4",
                        lambda *a, **k: {"ran": False, "reason": "disabled"})
    assert rc._run_stage4_in_pipeline(job, job / "source.pdf", {}, log=lambda *_: None) is None

    def boom(*a, **k):
        raise RuntimeError("bedrock is down")
    monkeypatch.setattr(ai, "run_stage4", boom)
    assert rc._run_stage4_in_pipeline(job, job / "source.pdf", {}, log=lambda *_: None) is None


def test_the_final_verdict_prefers_the_post_ai_scorecard_when_it_exists(job):
    """scorecard.json is the --resume marker, frozen at stage 3. When stage 4/5 ran, the
    gate/worst a reviewer sees has to describe that tree, not the one from before the AI
    pass touched it."""
    import json
    sc = {"gate": "review", "worst_score": 70.0}
    (job / "scorecard_post_ai.json").write_text(json.dumps(
        {"gate": "pass", "worst_score": 95.0, "scored_stage": 5}))
    gate, worst, scored_stage = rc._final_verdict(job, sc)
    assert (gate, worst, scored_stage) == ("pass", 95.0, 5)


def test_the_final_verdict_falls_back_to_the_extraction_gate_without_stage_4(job):
    """Most documents never run stage 4/5 -- no scorecard_post_ai.json on disk means the
    extraction gate IS the verdict, at scored_stage 3."""
    sc = {"gate": "pass", "worst_score": 96.0}
    assert rc._final_verdict(job, sc) == ("pass", 96.0, 3)


def test_the_final_verdict_ignores_a_post_ai_scorecard_with_no_gate(job):
    """_score_post_ai writes nothing when stage 4 wrote no output (stage <= 3) -- but if a
    malformed or partial file ever landed on disk anyway, a scorecard with no gate must not
    silently override a real one."""
    import json
    sc = {"gate": "review", "worst_score": 62.0}
    (job / "scorecard_post_ai.json").write_text(json.dumps({"gate": None}))
    assert rc._final_verdict(job, sc) == ("review", 62.0, 3)


def test_a_paid_pass_that_fails_leaves_the_extraction_completed(job, monkeypatch):
    """The scorecard is already written and the tree already published by this point.
    A Bedrock outage must cost the document its AI output and nothing else."""
    import aosphere_core_index.extract.ai_postprocess as ai

    def boom(*a, **k):
        raise RuntimeError("bedrock is down")

    monkeypatch.setattr(ai, "run_stage4", boom)
    steps = {"stage1": 6.0}
    said = []
    rc._run_stage4_in_pipeline(job, job / "source.pdf", steps, log=said.append)
    assert steps == {"stage1": 6.0}                 # nothing invented
    assert any("FAILED" in s and "bedrock is down" in s for s in said), said


def test_a_refusal_from_the_pass_itself_is_reported_not_swallowed(job, monkeypatch):
    """run_stage4 returns ran=False with a reason rather than raising when its own gate
    declines. Saying nothing here is how a run with no credentials read as a model that
    had looked at every page and changed nothing."""
    import aosphere_core_index.extract.ai_postprocess as ai
    monkeypatch.setattr(ai, "run_stage4",
                        lambda *a, **k: {"ran": False, "reason": "stage4_ai_enabled is false"})
    steps = {}
    said = []
    rc._run_stage4_in_pipeline(job, job / "source.pdf", steps, log=said.append)
    assert steps == {}
    assert any("stage4_ai_enabled is false" in s for s in said), said


# ---- the model ---------------------------------------------------------------------

@pytest.mark.parametrize("setting,expect", [
    ("haiku", "haiku-4-5"), ("sonnet", "sonnet-4-6"), ("sonnet5", "sonnet-5"),
    ("SONNET5", "sonnet-5"), (" sonnet5 ", "sonnet-5"),
    # Anything that is not an alias is a MODEL ID and goes through verbatim -- that is what
    # lets a model released tomorrow be selected without a code change. The cost is that a
    # typo is indistinguishable from a new id and fails at Bedrock, which is the better
    # failure: loud, and before any work. See ai_postprocess.resolve_model.
    ("eu.anthropic.claude-opus-4-6-v1:0", "opus-4-6"),
    ("scribble", "scribble"),
    # ...but an EMPTY setting is not a model id, and must not be sent as one.
    ("", "sonnet-5"),
])
def test_the_model_comes_from_the_setting(job, monkeypatch, setting, expect):
    from aosphere_core_index.config import settings
    import aosphere_core_index.extract.ai_postprocess as ai
    seen = {}
    monkeypatch.setattr(settings, "stage4_ai_model", setting, raising=False)
    monkeypatch.setattr(ai, "run_stage4",
                        lambda *a, **k: seen.update(models=k.get("models")) or _report())
    rc._run_stage4_in_pipeline(job, job / "source.pdf", {}, log=lambda *_: None)
    assert expect in seen["models"][0], seen


def test_the_model_is_logged_every_run(job, monkeypatch):
    """This is the number that turns into money, and a silently-defaulted model is exactly
    how a comparison run got billed on one model while being read as the other."""
    import aosphere_core_index.extract.ai_postprocess as ai
    monkeypatch.setattr(ai, "run_stage4", lambda *a, **k: _report())
    said = []
    rc._run_stage4_in_pipeline(job, job / "source.pdf", {}, log=said.append)
    assert any("model=" in s for s in said), said


def test_the_default_table_model_is_sonnet_4_6():
    """Chosen deliberately over the cheaper option: haiku broke the column structure this
    pass exists to repair. A corpus run multiplies it -- roughly $160 against $50 over 150
    documents -- which is the number to check before enabling the flag corpus-wide."""
    from aosphere_core_index.config import Settings
    assert Settings().stage4_ai_model == "sonnet4.6"


def test_the_default_text_model_is_haiku():
    """The text pass reproduces prose close to verbatim rather than repairing a table's
    column structure, so it does not need the table pass's model -- and can run cheaper."""
    from aosphere_core_index.config import Settings
    assert Settings().stage4_text_ai_model == "haiku"


def test_sonnet_5_has_a_price_so_a_paid_run_cannot_report_zero():
    """cost_usd returns 0.0 for a model PRICES does not know, and a $0.0000 on a run that
    spent real money is worse than a wrong number. The rate itself is carried over from
    Sonnet 4.6 and unconfirmed -- the AWS pricing API is denied to the invoke-only role --
    so the report also names any model billed at an unknown rate."""
    from aosphere_core_index.extract.ai_postprocess import PRICES, SONNET5, cost_usd
    assert SONNET5 in PRICES
    assert cost_usd(SONNET5, 137_624, 43_602) > 1.0


# ---- choosing a model without touching code --------------------------------------

def test_a_short_alias_resolves_and_a_model_id_passes_through():
    from aosphere_core_index.extract.ai_postprocess import HAIKU, SONNET5, resolve_model
    assert resolve_model("haiku") == HAIKU
    assert resolve_model("sonnet5") == SONNET5
    assert resolve_model("SONNET-5") == SONNET5              # case and separators
    assert resolve_model(" haiku4.5 ") == HAIKU              # whitespace
    # the whole point: an id this build has never heard of is usable as-is
    assert resolve_model("eu.anthropic.claude-opus-9") == "eu.anthropic.claude-opus-9"


def test_an_empty_setting_is_not_treated_as_a_model_id(monkeypatch):
    """"" and None both fall through to settings.stage4_ai_model -- this must hold even
    when THAT is empty, not merely happen to pass because the current default is not.
    Pinned explicitly rather than relying on the default's own value, which is a separate
    fact tested in test_the_default_table_model_is_sonnet_4_6 and can change independently."""
    from aosphere_core_index.config import settings
    from aosphere_core_index.extract.ai_postprocess import SONNET5, resolve_model
    monkeypatch.setattr(settings, "stage4_ai_model", "", raising=False)
    assert resolve_model("") == SONNET5
    assert resolve_model(None) == SONNET5
    assert resolve_model("   ") == SONNET5


def test_a_model_with_no_built_in_price_can_be_priced_from_the_environment(monkeypatch):
    """Otherwise choosing an unknown model silently reports $0.0000 on a paid run."""
    from aosphere_core_index.config import settings
    from aosphere_core_index.extract.ai_postprocess import UNPRICED, cost_usd
    UNPRICED.clear()
    assert cost_usd("eu.anthropic.made-up", 1_000_000, 0) == 0.0
    assert "eu.anthropic.made-up" in UNPRICED, "an unpriced model must be recorded"

    monkeypatch.setattr(settings, "stage4_ai_price_in", 2.0, raising=False)
    monkeypatch.setattr(settings, "stage4_ai_price_out", 10.0, raising=False)
    assert cost_usd("eu.anthropic.made-up", 1_000_000, 1_000_000) == 12.0


def test_a_configured_price_overrides_a_built_in_one(monkeypatch):
    """The reason to set a rate by hand is that the built-in one is absent OR WRONG --
    SONNET5's is carried over from Sonnet 4.6 and unconfirmed. An override that lost to a
    stale hard-coded number would be useless for the second case."""
    from aosphere_core_index.config import settings
    from aosphere_core_index.extract.ai_postprocess import SONNET5, cost_usd
    assert cost_usd(SONNET5, 1_000_000, 0) == 3.0            # the built-in rate
    monkeypatch.setattr(settings, "stage4_ai_price_in", 7.5, raising=False)
    monkeypatch.setattr(settings, "stage4_ai_price_out", 30.0, raising=False)
    assert cost_usd(SONNET5, 1_000_000, 0) == 7.5


def test_half_a_price_is_ignored_rather_than_half_applied(monkeypatch):
    """A run priced at $0 for output would read as almost free."""
    from aosphere_core_index.config import settings
    from aosphere_core_index.extract.ai_postprocess import SONNET5, cost_usd
    monkeypatch.setattr(settings, "stage4_ai_price_in", 7.5, raising=False)
    monkeypatch.setattr(settings, "stage4_ai_price_out", None, raising=False)
    assert cost_usd(SONNET5, 1_000_000, 0) == 3.0            # falls back to the table


def test_nothing_defaults_to_a_hardcoded_model_behind_the_config():
    """run_stage4 and repair_batch used to default to (HAIKU, HAIKU, SONNET), so a caller
    that passed no model got Haiku whatever ACI_STAGE4_AI_MODEL said -- which is how
    comparison runs came to be billed on one model while being read as another."""
    src = (Path(__file__).resolve().parents[1]
           / "src/aosphere_core_index/extract/ai_postprocess.py").read_text()
    assert "models=(HAIKU, HAIKU, SONNET)" not in src
    assert "models=(HAIKU,)" not in src


# ---------------------------------------------------------------- stage 4 section health
#
# A document can gate PASS on stages 1-3 while stage 4 quietly loses a section repairing
# it: the extraction scorecard describes the stage-3 tree and is written BEFORE stage 4
# runs, so nothing it carries can say a paid pass timed out halfway. These pin the signal
# the Extraction tab's "AI stage" column reads.

def _job(tmp_path, *, report=None, stage1_files=(), summary_route=False, stage4_dir=True):
    job = tmp_path / "124_X" / "Belgium__1"
    (job / "01_stage1_extract").mkdir(parents=True)
    for name in stage1_files:
        (job / "01_stage1_extract" / name).write_text("#\n")
    if summary_route:
        (job / "summary_ai_rule.json").write_text('{"product": "124_X"}')
    if stage4_dir:
        (job / "04_stage4_ai").mkdir()
    if report is not None:
        import json as _json
        (job / "04_stage4_ai" / "stage4_report.json").write_text(_json.dumps(report))
    return job


def test_a_stage_4_that_never_ran_is_blank_not_green(tmp_path):
    """Most documents never enter stage 4 (it is opt-in). Reporting those as "ok" would
    make the column meaningless — a green that means "nothing happened"."""
    from aosphere_core_index.extract.ai_postprocess import stage4_section_health
    assert stage4_section_health(_job(tmp_path, stage4_dir=False)) is None


def test_the_summary_ai_route_is_not_judged_by_a_report_it_never_writes(tmp_path):
    """That route writes its OWN 04_stage4_ai as the primary extraction — there is no
    section-repair report to be missing, so "no report" must not read as "stuck"."""
    from aosphere_core_index.extract.ai_postprocess import stage4_section_health
    assert stage4_section_health(_job(tmp_path, summary_route=True)) is None


def test_a_directory_with_no_report_is_stuck_not_ok(tmp_path):
    """04_stage4_ai is created before anything is written into it, so a directory with no
    report means stage 4 started and never finished — killed, crashed, or still wedged."""
    from aosphere_core_index.extract.ai_postprocess import stage4_section_health
    h = stage4_section_health(_job(tmp_path))
    assert h["status"] == "incomplete" and h["failed_sections"] == []


def test_a_failed_section_carries_its_reason(tmp_path):
    from aosphere_core_index.extract.ai_postprocess import stage4_section_health
    report = {"sections": [{"file": "09-marketing.md", "ok": False,
                            "reason": "ReadTimeoutError: Read timeout on endpoint URL"},
                           {"file": "10-licence.md", "ok": True}]}
    h = stage4_section_health(_job(tmp_path, report=report))
    assert h["status"] == "failed"
    assert h["failed_sections"] == [{"file": "09-marketing.md",
                                     "reason": "ReadTimeoutError: Read timeout on endpoint URL"}]


def test_a_section_the_report_never_mentions_is_a_failure_too(tmp_path):
    """`ok: false` cannot see a process killed BETWEEN sections: the ones it never reached
    have no entry at all. Silence is the failure mode this catches."""
    from aosphere_core_index.extract.ai_postprocess import stage4_section_health
    report = {"sections": [{"file": "09-marketing.md", "ok": True}]}
    h = stage4_section_health(_job(tmp_path, report=report,
                                   stage1_files=("09-marketing.md", "10-licence.md")))
    assert h["status"] == "failed"
    assert [s["file"] for s in h["failed_sections"]] == ["10-licence.md"]
    assert "never attempted" in h["failed_sections"][0]["reason"]


def test_front_matter_is_ignored_on_both_paths(tmp_path):
    """Losing a cover page is not what this signal is for, and flagging it drowns the
    sections that matter — so it counts neither as a failed section nor as a missing one."""
    from aosphere_core_index.extract.ai_postprocess import stage4_section_health
    report = {"sections": [{"file": "01-front-matter.md", "ok": False, "reason": "timed out"},
                           {"file": "09-marketing.md", "ok": True}]}
    h = stage4_section_health(_job(tmp_path, report=report,
                                   stage1_files=("01-front-matter.md", "09-marketing.md")))
    assert h == {"status": "ok", "failed_sections": []}


def test_the_report_only_half_is_what_the_s3_drill_down_uses(tmp_path):
    """job_detail reads the report out of S3 and has no cheap way to list Stage 1's files,
    so it calls the same function without them — every ok:false still lands, the
    killed-mid-run gap simply is not checked."""
    from aosphere_core_index.extract.ai_postprocess import stage4_failed_sections_from_report
    report = {"sections": [{"file": "01-front-matter.md", "ok": False, "reason": "timed out"},
                           {"file": "09-marketing.md", "ok": False, "reason": "throttled"}]}
    assert stage4_failed_sections_from_report(report) == [
        {"file": "09-marketing.md", "reason": "throttled"}]
