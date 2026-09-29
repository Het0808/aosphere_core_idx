"""Where the monitor puts a document while stages 4 and 5 are RUNNING.

Every other signal the monitor reads is a completion artifact — tables_manifest.json,
stage2_report.json, scorecard.json — and that model breaks down on the two AI stages,
because they write nothing at all until they finish: stage4_report.json and the timings
entry both land after the last section. A document half way through a twenty-minute AI
pass therefore looked exactly like one that had stopped at the gate. It sat on "Scorecard
gate" for the whole run, and the trace graph pulsed no node, because `live` is
`status == "running"` and a scored document's status is its gate verdict.

The heartbeat was already there: ai_postprocess._progress writes the running stage into
the job's own progress.json, and says in its docstring that it does so "so the UI picks
stage 4 up without a server change". Nothing read it. These tests pin that it is read,
and — the other half of the bug — that a FINISHED or ABSENT heartbeat never advances a
document past the gate it actually reached.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pipeline_monitor as pm  # noqa: E402

GATE_IDX = 5
AI_IDX = 6
SUBCHUNK_IDX = 7


@pytest.fixture
def scored(tmp_path):
    """A scored job — gate decided, extraction over — plus a maker for its heartbeat."""
    d = tmp_path / "124_Prod" / "Japan__172122"
    d.mkdir(parents=True)
    (d / "corpus_meta.json").write_text(json.dumps(
        {"product": "124_Prod", "jurisdiction": "Japan", "doc_id": "172122"}))
    (d / "scorecard.json").write_text(json.dumps({"gate": "review", "worst_score": 81.3}))

    def heartbeat(**kw):
        (d / "progress.json").write_text(json.dumps(kw))
    return tmp_path, d, heartbeat


def _one(root):
    jobs = pm.discover_jobs(root)
    assert len(jobs) == 1
    return jobs[0]


def test_stage4_in_flight_moves_the_document_off_the_gate(scored):
    root, _, heartbeat = scored
    heartbeat(stage="stage4_ai", status="running", detail={"ok": 13, "failed": 0})
    j = _one(root)
    assert j["stage_idx"] == AI_IDX
    assert j["status"] == "running"
    # Both of these, or the graph draws nothing: exNodeStates needs `live` before it will
    # look at `running_step` at all.
    assert j["live"] is True
    assert j["running_step"] == "stage4_ai"


def test_the_running_step_is_the_name_the_graph_maps(scored):
    """The seam that broke, pinned across BOTH files.

    EXSTAGE_NODE in service/trace_graph.py keys on the worker's own stage names, and
    `exNodeStates` pulses a node only when `EXSTAGE_NODE[j.running_step]` resolves. So
    the monitor must pass "stage4_ai" through UNTRANSLATED — a friendlier label here
    resolves to no node and pulses nothing, silently, which is the whole bug.

    Text-level on the graph side because there is no JS engine here, the same rule
    tests/test_web_pipeline_trace_nodes.py works under."""
    root, _, heartbeat = scored
    heartbeat(stage="stage4_ai", status="running")
    step = _one(root)["running_step"]

    graph = (Path(__file__).resolve().parent.parent
             / "src/aosphere_core_index/service/trace_graph.py").read_text(encoding="utf-8")
    mapping = re.search(r"const EXSTAGE_NODE=\{(.*?)\};", graph, re.S).group(1)
    pairs = dict(re.findall(r"(\w+)\s*:\s*'(\w+)'", mapping))
    assert pairs.get(step) == "ai", f"{step!r} resolves to {pairs.get(step)!r}, not the ai node"
    assert pairs.get("stage5_subchunk") == "subchunk"
    # The pulse is what "running" LOOKS like, and it hangs off exactly one state class.
    assert re.search(r"\.s-run[^}]*animation:exgpulse", graph, re.S)


def test_stage5_in_flight_reaches_the_subchunk_stage(scored):
    root, _, heartbeat = scored
    heartbeat(stage="stage5_subchunk", status="running")
    j = _one(root)
    assert j["stage_idx"] == SUBCHUNK_IDX
    assert j["running_step"] == "stage5_subchunk"
    assert j["live"] is True


def test_a_finished_heartbeat_leaves_the_document_at_its_gate(scored):
    """The heartbeat is not deleted when stage 4 ends — it is updated to done. Reading
    presence rather than status would strand every completed document on stage 4 forever,
    and `ai.ran` (which reads stage4_report.json) is what answers "did it run"."""
    root, _, heartbeat = scored
    heartbeat(stage="stage4_ai", status="done", detail={"ok": 18, "failed": 0})
    j = _one(root)
    assert j["stage_idx"] == GATE_IDX
    assert j["status"] == "review"
    assert j["live"] is False
    assert j["running_step"] is None


def test_a_corpus_stage_heartbeat_is_not_an_ai_stage(scored):
    """A fallback tier re-running stages 1-3 also writes `status: running` here. That is
    the fallback column's business (`_fallback_state`), and must not be read as an AI
    stage — which would put a document that is back in MinerU on "Stage 4"."""
    root, _, heartbeat = scored
    heartbeat(stage="stage2_mineru", status="running")
    j = _one(root)
    assert j["stage_idx"] == GATE_IDX
    assert j["running_step"] != "stage4_ai"


def test_no_heartbeat_at_all_is_not_a_running_stage(scored):
    root, _, _ = scored
    j = _one(root)
    assert j["stage_idx"] == GATE_IDX
    assert j["live"] is False
    assert j["running_step"] is None


def test_an_unreadable_heartbeat_does_not_take_the_row_down(scored):
    """One truncated json — a heartbeat caught mid-write — used to be enough to break the
    whole sweep, since discover_jobs builds every row in one pass."""
    root, d, _ = scored
    (d / "progress.json").write_text('{"stage": "stage4_ai", "status": "run')
    j = _one(root)
    assert j["stage_idx"] == GATE_IDX
    assert j["live"] is False
