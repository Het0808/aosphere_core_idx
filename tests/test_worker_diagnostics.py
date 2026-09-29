"""A cluster run has to be diagnosable from S3 alone.

The first GPU run failed and told us almost nothing: the failure detail was truncated at 200
characters — before the MinerU stderr appended to it — and the per-process counters reset on pod
restart, so two workers reported done=0 while S3 held 88 scorecards against 75 counted. The real
reasons were in Kibana, which we could not reach, so the run was effectively undebuggable.

Three properties fix that, and each is tested here: the full failure text survives, a restart
carries its counters forward, and the environment a worker actually got is recorded.
"""

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import corpus_worker as W  # noqa: E402


@pytest.fixture
def prog(tmp_path, monkeypatch):
    """A ShardProgress with S3 and the environment probe stubbed out."""
    calls = []

    def fake_s3(*args, profile=None):
        calls.append(args)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(W, "_s3", fake_s3)
    monkeypatch.setattr(W, "env_fingerprint", lambda *a, **k: {"cuda_available": True})
    p = W.ShardProgress("run1", 0, 8, 10, 500, tmp_path, "s3://b/corpus/run1", None)
    p._calls = calls
    return p


def test_the_full_failure_text_survives_not_just_its_first_200_chars(prog):
    """The MinerU stderr is appended AFTER the exception line, so a short cap drops exactly the
    part worth reading."""
    err = ("CalledProcessError: Command '['/usr/local/bin/mineru', '-p', '…']' returned non-zero "
           "exit status 1." + "\n--- traceback ---\n" + "x" * 300
           + "\n--- mineru stderr ---\nCUDA out of memory. Tried to allocate 2.00 GiB")
    prog.failed("155_Data_Privacy", "Italy__96416", err)
    kept = prog.d["recent"][0]["error"]
    assert "CUDA out of memory" in kept, "the reason must survive the summary"
    assert len(kept) <= 1500


def test_each_failure_is_written_where_s3_can_be_read_without_the_cluster(prog, tmp_path):
    prog.failed("155_Data_Privacy", "Italy (Data Privacy)__96416", "boom\nCUDA out of memory")
    wrote = [c for c in prog._calls if c[0] == "cp" and "_failures/" in c[2]]
    assert wrote, "a per-failure object must be uploaded"
    body = Path(wrote[0][1]).read_text()
    assert "CUDA out of memory" in body and "155_Data_Privacy" in body
    assert "cuda_available" in body, "the environment travels with the failure"
    # the label has spaces and parentheses; the key must not
    assert " " not in wrote[0][2].rsplit("/", 1)[-1]


def test_a_restarted_worker_carries_its_counters_forward(tmp_path, monkeypatch):
    """Otherwise a restart republishes done=0 and the screen calls a live worker idle."""
    prior = json.dumps({"done": 34, "extracted": 30, "cloned": 4, "failed": 6,
                        "pages_done": 488, "gates": {"fail": 34}, "recent": [{"label": "old"}]})

    def fake_s3(*args, profile=None):
        if args[0] == "cp" and args[2] == "-":          # reading the previous summary
            return types.SimpleNamespace(returncode=0, stdout=prior, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(W, "_s3", fake_s3)
    monkeypatch.setattr(W, "env_fingerprint", lambda *a, **k: {})
    p = W.ShardProgress("run1", 3, 8, 152, 7000, tmp_path, "s3://b/corpus/run1", None)
    assert p.d["done"] == 34 and p.d["failed"] == 6 and p.d["pages_done"] == 488
    assert p.d["gates"] == {"fail": 34}
    assert p.d["restarts"] == 1


def test_a_first_start_has_nothing_to_carry(prog):
    assert prog.d["done"] == 0 and prog.d.get("restarts") in (None, 0)


def test_the_environment_is_recorded_on_the_summary(prog):
    assert prog.d["env"] == {"cuda_available": True}


def test_the_fingerprint_reports_the_facts_a_gpu_failure_turns_on():
    """Runs for real: it must never raise, and must name what it could not determine."""
    env = W.env_fingerprint()
    assert isinstance(env, dict)
    assert "cpu_count" in env
    # Either it found torch and answered the CUDA question, or it said why it could not.
    assert ("cuda_available" in env) or ("torch_error" in env) or ("probe_error" in env)
    # The C toolchain MUST be reported: triton JIT-compiles a C file for every CUDA kernel, so a
    # container with a GPU and no compiler fails every document on MinerU's GPU path — and it is
    # invisible on Apple/MPS, where triton is never invoked at all.
    assert "cc" in env


def test_the_stage_in_flight_is_recorded(prog):
    """run_corpus emits stage transitions and the worker used to discard them, so a worker 20
    minutes into one silent MinerU subprocess looked the same as one stuck in stage 1."""
    prog.start("155_Data_Privacy", "Netherlands (Data Privacy)__174642", 361)
    prog.stage("stage2_mineru")
    cur = prog.d["current"]
    assert cur["stage"] == "stage2_mineru"
    assert cur["stage_at"] >= cur["started_at"]


def test_a_stage_with_no_document_in_flight_is_ignored(prog):
    """Transitions can arrive around the edges of a document; they must never raise."""
    prog.d["current"] = None
    prog.stage("stage3")            # must not raise
    assert prog.d["current"] is None


def test_the_heartbeat_only_beats_while_a_document_is_in_flight(prog):
    before = len(prog._calls)
    prog.d["current"] = None
    prog.heartbeat()
    assert len(prog._calls) == before, "an idle worker should not republish"
    prog.start("155_Data_Privacy", "Spain__172575", 354)
    n = len(prog._calls)
    prog.heartbeat()
    assert len(prog._calls) > n, "a busy worker must refresh updated_at"
