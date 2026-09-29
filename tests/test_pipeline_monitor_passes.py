"""Whether a job is on its first pass or in a fallback tier, as the monitor sees it.

A fallback tier re-runs stages 1-3 into the SAME directories, so the monitor's
file-existence model cannot tell a rescue from a first pass on its own: a document in
toc_rescue looks like a fresh job that has gone backwards from stage 4 to stage 2.
These tests pin the three signals that can tell them apart — the live run heartbeat,
the scored `scorecard["fallback"]` block, and the markers a killed run leaves behind —
and, most importantly, pin the one directory that must NOT be read as a fallback.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pipeline_monitor as pm  # noqa: E402


@pytest.fixture
def corpus(tmp_path):
    """A corpus root plus a maker for job dirs under one product folder."""
    def mk(name, *, scorecard=None, markers=()):
        d = tmp_path / "124_Prod" / name
        d.mkdir(parents=True)
        (d / "corpus_meta.json").write_text(json.dumps({
            "product": "124_Prod", "jurisdiction": name.split("__")[0],
            "doc_id": name.split("__")[-1]}))
        if scorecard is not None:
            (d / "scorecard.json").write_text(json.dumps(scorecard))
        for m in markers:
            p = d / m
            p.write_text("{}") if m.endswith(".json") else p.mkdir()
        return d
    return tmp_path, mk


def _one(root):
    jobs = pm.discover_jobs(root)
    assert len(jobs) == 1
    return jobs[0]


def _fb(adopted, chain=None, reason="completeness 41 < 60"):
    return {"gate": "review", "worst_score": 72,
            "fallback": {"triggered": True, "adopted_tier": adopted,
                         "reason": reason, "chain": chain or []}}


def test_a_clean_document_reads_as_a_first_pass(corpus):
    root, mk = corpus
    mk("Chile__1", scorecard={"gate": "pass", "worst_score": 95})
    j = _one(root)
    assert j["tier"] == "first_pass"
    assert j["fallback"] is False and j["tier_live"] is False


def test_the_preflight_repair_directory_is_not_a_fallback(corpus):
    """`fallback/` holds source_repaired.pdf from the PRE-FLIGHT outline repair, which
    runs on nearly every document. Reading it as a tier would mark the whole corpus
    rescued — the exact false positive this column exists to avoid."""
    root, mk = corpus
    mk("Chile__1", scorecard={"gate": "pass", "worst_score": 93}, markers=("fallback",))
    assert _one(root)["tier"] == "first_pass"
    assert _one(root)["fallback"] is False


def test_a_live_tier_is_named_from_the_run_heartbeat(corpus):
    """The document in flight is the only one the heartbeat can speak for, and while a
    tier is running nothing on disk says so yet."""
    root, mk = corpus
    mk("Peru__9")
    (root / "_progress.json").write_text(json.dumps({
        "state": "running", "product": "124_Prod", "document": "Peru__9",
        "stage": "toc_rescue", "done": 2, "total": 9}))
    j = _one(root)
    assert j["tier"] == "toc_rescue" and j["tier_live"] is True
    assert j["fallback"] is True


def test_the_heartbeat_only_speaks_for_the_document_it_names(corpus):
    """A neighbour must not inherit the in-flight document's tier."""
    root, mk = corpus
    mk("Peru__9")
    mk("Chile__1", scorecard={"gate": "pass", "worst_score": 95})
    (root / "_progress.json").write_text(json.dumps({
        "state": "running", "product": "124_Prod", "document": "Peru__9",
        "stage": "mineru_full", "done": 2, "total": 9}))
    by_id = {j["doc_id"]: j for j in pm.discover_jobs(root)}
    assert by_id["9"]["tier"] == "mineru_full" and by_id["9"]["tier_live"] is True
    assert by_id["1"]["tier"] == "first_pass" and by_id["1"]["tier_live"] is False


@pytest.mark.parametrize("adopted,expected", [
    ("toc_rescue", "toc_rescue"),
    ("mineru_full", "mineru_full"),
    ("stage1", "stage1"),
])
def test_a_scored_document_reports_the_tier_that_was_adopted(corpus, adopted, expected):
    """Which tier WON is the point: a tier can run, score worse than the first pass and
    be discarded, and naming the tier alone would imply its tree is the one on disk."""
    root, mk = corpus
    mk("Chile__1", scorecard=_fb(adopted))
    j = _one(root)
    assert j["tier"] == expected
    assert j["adopted_tier"] == adopted and j["fallback"] is True


def test_a_rejected_chain_is_still_reported_as_having_fallen_back(corpus):
    """adopted_tier == stage1 means every tier lost. The document still cost the time,
    so it must not be filed alongside the documents that never escalated."""
    root, mk = corpus
    mk("Chile__1", scorecard=_fb("stage1", chain=[
        {"tier": "toc_rescue", "adopted": False},
        {"tier": "mineru_full", "adopted": False}]))
    j = _one(root)
    assert j["fallback"] is True
    assert j["tier_label"] == pm.TIER_LABEL["stage1"]
    assert len(j["tier_chain"]) == 2


@pytest.mark.parametrize("marker,expected", [
    ("rescued_by_toc.json", "toc_rescue"),
    ("mineru_full_attempt", "mineru_full"),
    ("hybrid_attempt", "mineru_full"),
])
def test_a_run_killed_mid_tier_is_recovered_from_disk(corpus, marker, expected):
    """No scorecard and a heartbeat that has moved on — the markers are all that is
    left, and they are why a stopped run does not read as a clean first pass."""
    root, mk = corpus
    mk("Chile__1", markers=(marker,))
    j = _one(root)
    assert j["tier"] == expected and j["fallback"] is True
    assert j["tier_reason"]


def test_the_scorecard_outranks_the_disk_markers(corpus):
    """A rescued document keeps rescued_by_toc.json forever. Once scored, the
    scorecard's own account of the chain is the better answer."""
    root, mk = corpus
    mk("Chile__1", scorecard=_fb("stage1"), markers=("rescued_by_toc.json",))
    assert _one(root)["adopted_tier"] == "stage1"


def test_every_job_carries_a_renderable_label(corpus):
    """The page reads tier_label directly, so an unmapped tier must not reach it."""
    root, mk = corpus
    mk("A__1", scorecard={"gate": "pass"})
    mk("B__2", scorecard=_fb("mineru_full"))
    mk("C__3", markers=("rescued_by_toc.json",))
    for j in pm.discover_jobs(root):
        assert j["tier_label"] in pm.TIER_LABEL.values()


# ---------------------------------------------------------------- the Took column

def test_a_scored_job_exposes_what_it_cost(corpus):
    """The column reads the scorecard's timing block — mtimes cannot answer this, since
    a job queued behind a long MinerU pass has an early mtime and a short run."""
    root, mk = corpus
    mk("Chile__1", scorecard={"gate": "review", "worst_score": 84.3, "timing": {
        "seconds": 1212.0, "steps": {"stage2_mineru": 1177.6}, "pages": 137,
        "seconds_per_page": 8.85, "seconds_per_crop_page": 10.33,
        "fallback_seconds": 0}})
    j = _one(root)
    assert j["seconds"] == 1212.0
    assert j["timing_pages"] == 137 and j["seconds_per_page"] == 8.85
    assert j["timing_steps"] == {"stage2_mineru": 1177.6}


def test_fallback_cost_is_carried_so_the_column_can_show_it(corpus):
    root, mk = corpus
    mk("Guatemala__2", scorecard={"gate": "pass", "timing": {
        "seconds": 1018.0, "fallback_seconds": 503.0}})
    assert _one(root)["fallback_seconds"] == 503.0


def test_a_backfilled_timing_is_flagged_not_passed_off_as_measured(corpus):
    root, mk = corpus
    mk("Nigeria__3", scorecard={"gate": "review", "timing": {
        "seconds": 1424.8, "backfilled": True}})
    assert _one(root)["timing_backfilled"] is True


def test_a_scorecard_without_timing_reports_no_cost_rather_than_zero(corpus):
    """A pre-timing scorecard must render as "—", not as a document that took 0s."""
    root, mk = corpus
    mk("Old__4", scorecard={"gate": "pass", "worst_score": 91.0})
    j = _one(root)
    assert j["seconds"] is None
    assert j["fallback_seconds"] == 0


def test_an_unscored_job_has_no_cost_yet(corpus):
    """Still running: the page falls back to elapsed-since-started, so the API must not
    invent a total."""
    root, mk = corpus
    mk("Running__5")
    assert _one(root)["seconds"] is None


# ---------------------------------------------------------------- the served page

def test_javascript_string_escapes_survive_the_python_string():
    """PAGE is a plain triple-quoted string, so a lone \\n inside it is converted to a
    REAL newline when the module is imported. Landing one inside a single-quoted JS
    literal breaks the string, the whole script throws SyntaxError, and the page renders
    its static shell while never once calling /api/status — a dashboard that looks alive
    and shows nothing. Escapes must reach the browser as escapes."""
    script = pm.PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    for i, line in enumerate(script.split("\n"), 1):
        # Comments are skipped: an apostrophe in prose ("Tier 2's rescue") is not a broken
        # literal, and the shared trace graph brought a lot of prose with it. The check is
        # about CODE lines, where an odd count really does mean a literal left open.
        if line.lstrip().startswith(("//", "*", "/*")):
            continue
        # An ESCAPED apostrophe is legal inside a single-quoted literal (MinerU\\'s), so it
        # must not be counted as a delimiter -- otherwise every such line reads as broken.
        bare = line.replace("\\'", "")
        # a quoted literal must open and close on its own line
        assert bare.count("'") % 2 == 0, f"unbalanced ' on script line {i}: {line.strip()!r}"
    assert "join('\\n')" in script, "newline joins must be escaped, not literal breaks"


def test_the_page_still_declares_every_column_it_renders():
    """The row builder and the header have to agree, or cells shift under the wrong
    heading."""
    header = pm.PAGE.split("<thead><tr>", 1)[1].split("</tr></thead>", 1)[0]
    for col in ("Document", "Stage", "Pass", "Took", "Progress", "Status"):
        assert f"<th>{col}</th>" in header, f"missing column: {col}"


def test_the_second_spelling_of_the_mineru_tier_is_recognised_live(corpus):
    """A document escalating normally ticks "mineru_fallback", not "mineru_full". If the
    monitor does not know that name it renders a document mid-rescue as a first pass."""
    root, mk = corpus
    mk("India__9")
    (root / "_progress.json").write_text(json.dumps({
        "state": "running", "product": "124_Prod", "document": "India__9",
        "stage": "mineru_fallback", "done": 1, "total": 2}))
    j = _one(root)
    assert j["tier_live"] is True and j["fallback"] is True
    assert j["tier"] == "mineru_full", "both spellings normalise to one canonical tier"
    assert j["tier_label"] == pm.TIER_LABEL["mineru_full"]


# ---- stages 4 and 5: opt-in, paid, and run out of band ----------------------------
# Stage 4 never runs inside the corpus run, so neither the ledger nor the scorecard's
# frozen timing block can witness it. Disk is the only witness, and three outcomes must
# stay distinguishable: it RAN, it was GATED OFF for that run, or it was never asked for.

def _ai(root, name, *, report=None, steps=None, subchunks=()):
    """A scored job, optionally carrying a stage 4 report and/or AI timings."""
    d = root / "124_Prod" / name
    d.mkdir(parents=True)
    (d / "corpus_meta.json").write_text(json.dumps({
        "product": "124_Prod", "jurisdiction": "X", "doc_id": name.split("__")[-1]}))
    (d / "scorecard.json").write_text(json.dumps({"gate": "pass", "worst_score": 90}))
    if steps is not None:
        (d / "timings.json").write_text(json.dumps({"steps": steps}))
    if report is not None:
        (d / "04_stage4_ai").mkdir()
        (d / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps(report))
    for rel in subchunks:
        p = d / "05_subchunks" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x")
    return d


REPORT = {"mode": "section", "model": "eu.anthropic.claude-sonnet-4-6",
          "sections": [{}] * 8, "sections_total": 8, "sections_accepted": 8,
          "usage": {"input_tokens": 134394, "output_tokens": 40400,
                    "total_tokens": 174794, "cost_usd": 1.0092}}


def test_a_document_that_went_through_ai_reports_the_model_and_the_money(tmp_path):
    _ai(tmp_path, "Bahamas__1", report=REPORT,
        steps={"stage1": 6.0, "stage4_ai": 484.0, "stage5_subchunk": 0.3},
        subchunks=("06-funds/00-section.md", "06-funds/01-6.1-a.md"))
    ai = _one(tmp_path)["ai"]
    assert ai["ran"] and not ai["disabled"]
    assert ai["model"] == "eu.anthropic.claude-sonnet-4-6" and ai["mode"] == "section"
    assert (ai["cost_usd"], ai["tokens"], ai["calls"]) == (1.0092, 174794, 8)
    assert (ai["seconds"], ai["stage5_seconds"]) == (484.0, 0.3)


def test_a_finished_ai_run_advances_the_document_past_the_gate(tmp_path):
    _ai(tmp_path, "Bahamas__1", report=REPORT, subchunks=("06-funds/00-section.md",))
    assert pm.STAGES[_one(tmp_path)["stage_idx"]]["key"] == "stage5_subchunk"


def test_stage_4_without_subchunks_stops_at_stage_4(tmp_path):
    _ai(tmp_path, "Bahamas__1", report=REPORT)
    assert pm.STAGES[_one(tmp_path)["stage_idx"]]["key"] == "stage4_ai"


def test_a_run_the_flag_refused_is_reported_as_gated_off_not_as_a_run(tmp_path):
    # The disabled path spends nothing and writes no report -- it records ZERO seconds.
    # That zero is the only evidence the stage was asked for at all, so it is read by
    # PRESENCE. Read by truthiness it would vanish and look like "never asked".
    _ai(tmp_path, "Belgium__2", steps={"stage1": 6.0, "stage4_ai": 0.0,
                                       "stage5_subchunk": 0.0})
    job = _one(tmp_path)
    assert job["ai"]["disabled"] and not job["ai"]["ran"]
    assert job["ai"]["cost_usd"] == 0.0
    # and it must NOT be credited with reaching a stage it never entered
    assert pm.STAGES[job["stage_idx"]]["key"] == "gate"


def test_a_document_never_offered_to_ai_is_not_reported_as_disabled(tmp_path):
    _ai(tmp_path, "Denmark__3", steps={"stage1": 6.0, "total": 6.0})
    job = _one(tmp_path)
    assert not job["ai"]["ran"] and not job["ai"]["disabled"]
    assert pm.STAGES[job["stage_idx"]]["key"] == "gate"


def test_an_ai_run_that_predates_the_timing_code_reports_no_time_rather_than_zero(tmp_path):
    # Eight minutes of Bedrock calls printed as "0s" would be a lie; the honest answer
    # for a report written before _record_timings existed is "not recorded".
    _ai(tmp_path, "Bahamas__4", report=REPORT, steps={"stage1": 6.0})
    ai = _one(tmp_path)["ai"]
    assert ai["ran"] and ai["seconds"] is None and ai["stage5_seconds"] is None


def test_the_ai_timings_reach_the_took_tooltip(tmp_path):
    # The scorecard's timing block is frozen at gate time and can never hold these, so
    # they are merged in from timings.json or the column simply omits the paid stages.
    _ai(tmp_path, "Bahamas__5", report=REPORT,
        steps={"stage1": 6.0, "stage4_ai": 484.0, "stage5_subchunk": 0.3})
    assert _one(tmp_path)["timing_steps"]["stage4_ai"] == 484.0


def test_the_two_ai_stages_are_flagged_optional_so_an_empty_funnel_row_reads_right(tmp_path):
    # 0 of 150 at these stages means "nobody asked", not "the pipeline died here".
    opt = [s["key"] for s in pm.STAGES if s.get("optional")]
    assert opt == ["stage4_ai", "stage5_subchunk"]
    assert [s["key"] for s in pm.STAGES].index("gate") < pm.STAGES.index(
        next(s for s in pm.STAGES if s["key"] == "stage4_ai"))


def test_a_toc_rescued_document_is_labelled_from_its_marker(corpus):
    """The only trace of the rescue in the job dir is toc_preflight.json, because the
    pre-flight is not a tier -- so this marker is the whole signal."""
    root, mk = corpus
    d = mk("Austria__137944", scorecard={"gate": "pass", "worst_score": 91})
    (d / "toc_preflight.json").write_text(json.dumps(
        {"applied": True, "trigger": "suspect_outline", "entries": 41, "verified": 39}))
    assert _one(root)["toc_rescued"] is True


def test_a_preflight_that_errored_is_not_a_rescue(corpus):
    """_preflight_outline writes the marker with applied:false when it raised. Reading
    presence rather than the flag would report a failed repair as a successful one."""
    root, mk = corpus
    d = mk("Belgium__163341", scorecard={"gate": "pass", "worst_score": 88})
    (d / "toc_preflight.json").write_text(json.dumps(
        {"applied": False, "error": "RuntimeError: boom"}))
    assert _one(root)["toc_rescued"] is False


def test_a_trusted_outline_writes_no_marker_and_is_not_a_rescue(corpus):
    root, mk = corpus
    mk("Denmark__148204", scorecard={"gate": "pass", "worst_score": 95})
    assert _one(root)["toc_rescued"] is False


# ---- the trace graph, shared with the Core Index -----------------------------------

def test_the_monitor_draws_the_shared_graph_not_a_copy_of_it():
    """One picture, two screens. A second copy would drift, and this session proved how
    quietly: the node count, the CSS min-width and the SVG arrowhead colours each fell out
    of step with the code they were meant to match, and every one of those failures left
    the JS parsing and the tests green."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "pipeline_monitor.py").read_text()
    assert "from aosphere_core_index.service import trace_graph" in src
    assert "/*TRACE_CSS*/" in src and "/*TRACE_JS*/" in src
    # and the placeholders must actually be substituted
    assert "/*TRACE_CSS*/" not in pm.PAGE and "/*TRACE_JS*/" not in pm.PAGE
    assert "const EXG_STEPS=[" in pm.PAGE and "function exGraph(" in pm.PAGE


def test_the_graph_gets_the_inputs_it_reads(corpus):
    """exNodeStates reads `steps`, `worst` and a scorecard-shaped `detail`; this monitor
    natively calls the first two timing_steps and worst_score."""
    root, mk = corpus
    d = mk("Bahamas__1", scorecard={"gate": "pass", "worst_score": 91,
                                    "timing": {"steps": {"stage1": 6.0, "stage3": 0.1},
                                               "seconds": 590.7},
                                    "fallback": {"triggered": False}})
    (d / "toc_preflight.json").write_text(json.dumps({"applied": True, "entries": 40}))
    j = _one(root)
    assert j["steps"]["stage1"] == 6.0 and j["worst"] == 91
    assert j["detail"]["preflight"]["applied"] is True
    assert j["detail"]["timing"]["steps"]["stage3"] == 0.1


def test_the_ai_steps_are_merged_into_the_graph_input_not_dropped(corpus):
    """The scorecard's timing block is frozen at gate time and can never hold the AI
    steps, so `timing_steps or ai_steps` kept the first and silently discarded every
    stage-4/5 entry — the two nodes the graph would then never light."""
    root, mk = corpus
    d = mk("Bahamas__2", scorecard={"gate": "pass", "worst_score": 90,
                                    "timing": {"steps": {"stage1": 6.0}}})
    (d / "timings.json").write_text(json.dumps(
        {"steps": {"stage1": 6.0, "stage4_ai": 484.0, "stage5_subchunk": 0.3}}))
    steps = _one(root)["steps"]
    assert steps["stage1"] == 6.0
    assert steps["stage4_ai"] == 484.0 and steps["stage5_subchunk"] == 0.3


def test_a_sub_second_step_is_not_reported_as_zero():
    """Stage 3 splices in 0.1s and Stage 5 splits sub-chunks in 0.3s. fmtDur rounded
    first, so both read "0s" — "did nothing" rather than "was fast" — and the shared
    graph asks its host for exactly this formatter."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "pipeline_monitor.py").read_text()
    assert "if (s > 0 && s < 1) return (Math.round(s * 10) / 10) + 's';" in src
    assert "const exDur = fmtDur;" in src
