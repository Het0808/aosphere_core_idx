"""The per-document ledger each worker appends to.

`recent` in a shard summary is a ten-document tail: it answers "what is happening now" and has to
stay small enough to rewrite after every document. The ledger answers the other question — which
document was slow, and where did the crashed one stop — and it is bounded by the corpus rather
than by time.

The property that matters most here is that a RESTART does not truncate it. S3 cannot append, so
every row is re-uploaded with the whole file; a restarted pod has an empty local ledger, and
without pulling the published one back first its next upload would replace a shard's entire
history with a single row. On spot capacity a restart is the normal case, not an exception.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def worker(monkeypatch):
    """corpus_worker is a script, not a package module — load it by path."""
    spec = importlib.util.spec_from_file_location("corpus_worker",
                                                  ROOT / "scripts" / "corpus_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["corpus_worker"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def s3(worker, monkeypatch):
    """A fake `aws s3 cp` that keeps objects in a dict, so uploads and reads are observable."""
    store: dict[str, str] = {}

    def fake_s3(*args, profile=None):
        out = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if args and args[0] == "cp":
            src, dst = args[1], args[2]
            if dst == "-":                                   # download to stdout
                if src not in store:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="NoSuchKey")
                out.stdout = store[src]
            else:                                            # upload
                store[dst] = Path(src).read_text()
        return out

    monkeypatch.setattr(worker, "_s3", fake_s3)
    monkeypatch.setattr(worker, "env_fingerprint", lambda: {})
    return store


def _prog(worker, tmp_path, shard=0, shards=4):
    return worker.ShardProgress(run="2026-08-19", shard=shard, shards=shards, jobs=10, pages=500,
                                out_root=tmp_path, results="s3://b/corpus/run", profile=None)


def _rows(store, shard=0):
    body = store.get(f"s3://b/corpus/run/_progress/ledger-{shard}.jsonl", "")
    return [json.loads(x) for x in body.splitlines() if x.strip()]


def _done(worker, p, label, **res):
    p.start("155_Data_Privacy", label, res.pop("pages", 40))
    r = {"status": "done", "gate": "pass", "worst": 80,
         "steps": {"stage1": 20.0, "stage2_mineru": 90.0, "stage3": 10.0, "total": 120.0}}
    r.update(res)
    p.finished("155_Data_Privacy", label, 40, r, 120.0, review=["viewer.html"])


def test_every_finished_document_gets_exactly_one_ledger_row(worker, s3, tmp_path):
    p = _prog(worker, tmp_path)
    for lab in ("A__1", "B__2", "C__3"):
        _done(worker, p, lab)
    assert [r["label"] for r in _rows(s3)] == ["A__1", "B__2", "C__3"]


def test_the_row_carries_what_run_one_already_computed(worker, s3, tmp_path):
    """All of this was being thrown away, so 'where did this document spend its time' could only
    be answered by opening its scorecard."""
    p = _prog(worker, tmp_path)
    _done(worker, p, "UK__85598", gate="review", worst=62, fallback=True,
          fallback_tier="mineru_full", tables=14, mineru_pages=31,
          steps={"stage1": 24.2, "stage2_mineru": 280.0, "mineru_full": 33.0, "total": 337.2})
    r = _rows(s3)[0]
    assert r["gate"] == "review" and r["worst"] == 62
    assert r["tier"] == "mineru_full" and r["fallback"] is True
    assert r["slowest"] == "stage2_mineru"
    assert r["steps"]["stage2_mineru"] == 280.0
    assert "total" not in r["steps"], "the end-to-end total is the row's `seconds`, not a step"
    assert r["fb_seconds"] == 33.0
    assert r["pages"] == 40 and r["spp"] == 3.0
    assert r["tables"] == 14 and r["mineru_pages"] == 31
    assert r["shard"] == 0 and r["started_at"] is not None


def test_a_first_pass_document_says_so_rather_than_saying_nothing(worker, s3, tmp_path):
    p = _prog(worker, tmp_path)
    _done(worker, p, "A__1")
    r = _rows(s3)[0]
    assert r["tier"] == "first_pass"
    assert "fallback" not in r, "a falsy flag is dropped rather than stored on every row"


def test_a_tier_that_ran_and_lost_is_recorded_as_such(worker, s3, tmp_path):
    """run_one reports no adopted_tier when a tier ran and the first pass still won."""
    p = _prog(worker, tmp_path)
    _done(worker, p, "A__1", fallback=True, fallback_tier=None)
    assert _rows(s3)[0]["tier"] == "stage1"


def test_a_crash_records_the_stage_it_died_in(worker, s3, tmp_path):
    """The exception names the fault but never the pipeline stage, and the two answer different
    questions: a CalledProcessError in stage2_mineru is a MinerU problem, the same exception in
    stage1 is not."""
    p = _prog(worker, tmp_path)
    p.start("155_Data_Privacy", "Taiwan__179120", 88)
    p.stage("stage1")
    p.stage("stage2_mineru")
    p.failed("155_Data_Privacy", "Taiwan__179120", "CalledProcessError: mineru exploded")
    r = _rows(s3)[0]
    assert r["stage"] == "stage2_mineru"
    assert r["gate"] == "error"
    assert r["pages"] == 88 and r["seconds"] is not None


def test_a_failures_ledger_excerpt_is_shorter_than_the_summarys(worker, s3, tmp_path):
    """The ledger holds every document in the run; 1,500 characters of traceback per failure is
    how a bounded file becomes a log. The full text is already its own object."""
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 10)
    p.failed("p", "A__1", "E: " + ("x" * 3000))
    assert len(_rows(s3)[0]["error"]) == 400
    assert len(p.d["recent"][0]["error"]) == 1500


def test_the_stage_is_cleared_between_documents(worker, s3, tmp_path):
    """Otherwise a document that crashes before reporting any stage inherits the previous
    document's, which is worse than saying nothing."""
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 10)
    p.stage("stage2_mineru")
    p.finished("p", "A__1", 10, {"status": "done", "gate": "pass", "steps": {}}, 5.0)
    p.start("p", "B__2", 10)
    p.failed("p", "B__2", "died immediately")
    assert _rows(s3)[1]["stage"] == "starting"


def test_a_restart_appends_to_the_published_ledger_rather_than_replacing_it(worker, s3, tmp_path):
    """The property this whole design turns on. A restarted pod has an empty local ledger, so
    without pulling the published one back its next upload truncates the shard's history."""
    p1 = _prog(worker, tmp_path / "run1")
    _done(worker, p1, "A__1")
    _done(worker, p1, "B__2")
    assert len(_rows(s3)) == 2

    p2 = _prog(worker, tmp_path / "run2")            # a fresh pod: new emptyDir, same prefix
    _done(worker, p2, "C__3")
    assert [r["label"] for r in _rows(s3)] == ["A__1", "B__2", "C__3"]
    assert p2.d["restarts"] == 1


def test_a_restart_with_no_published_ledger_is_not_an_error(worker, s3, tmp_path):
    """The first worker of a brand-new run has nothing to carry forward."""
    p = _prog(worker, tmp_path)
    _done(worker, p, "A__1")
    assert [r["label"] for r in _rows(s3)] == ["A__1"]


def test_each_shard_writes_its_own_ledger(worker, s3, tmp_path):
    """One object per shard, never one shared file — concurrent workers would clobber each other."""
    a, b = _prog(worker, tmp_path / "a", shard=0), _prog(worker, tmp_path / "b", shard=3)
    _done(worker, a, "A__1")
    _done(worker, b, "B__2")
    assert [r["label"] for r in _rows(s3, 0)] == ["A__1"]
    assert [r["label"] for r in _rows(s3, 3)] == ["B__2"]


def test_the_summary_stays_capped_while_the_ledger_grows(worker, s3, tmp_path):
    """The whole point of having both: `recent` must not become the job list."""
    p = _prog(worker, tmp_path)
    for i in range(25):
        _done(worker, p, f"D__{i}")
    assert len(p.d["recent"]) == p.RECENT
    assert len(_rows(s3)) == 25


def test_a_failed_ledger_upload_never_aborts_the_run(worker, monkeypatch, tmp_path):
    """Best effort, exactly like publish(): losing a progress row must not cost a GPU hour."""
    monkeypatch.setattr(worker, "env_fingerprint", lambda: {})
    monkeypatch.setattr(worker, "_s3",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network")))
    p = worker.ShardProgress(run="r", shard=0, shards=1, jobs=1, pages=1, out_root=tmp_path,
                             results="s3://b/corpus/run", profile=None)
    _done(worker, p, "A__1")               # must not raise
    assert p.d["done"] == 1


# ---- the fallback announcement ---------------------------------------------------------------

def test_entering_the_chain_is_announced_live_with_its_reason(worker, s3, tmp_path):
    """The tier takes minutes, so the reason has to be published when the chain DECIDES, not
    when the document finishes — otherwise the answer arrives after the wait it explains."""
    p = _prog(worker, tmp_path)
    p.start("155_Data_Privacy", "Bermuda__166524", 93)
    p.fallback({"reason": "completeness 35.1 < 60; toc 65.0 < 70",
                "first_attempt": {"toc_score": 65.0, "toc_rescuable": True}})
    cur = p.d["current"]
    assert cur["fallback"]["reason"] == "completeness 35.1 < 60; toc 65.0 < 70"
    assert cur["fallback"]["first"]["toc_rescuable"] is True
    assert cur["fallback"]["at"] is not None


def test_the_trigger_reaches_the_ledger_row(worker, s3, tmp_path):
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 40)
    p.fallback({"reason": "toc 55.0 < 70"})
    p.finished("p", "A__1", 40, {"status": "done", "gate": "pass", "steps": {},
                                 "fallback": True, "fallback_tier": "mineru_full",
                                 "fallback_reason": "toc 55.0 < 70",
                                 "fallback_hard_fail": None,
                                 "fallback_accepted": True}, 12.0)
    r = _rows(s3)[0]
    assert r["trigger_reason"] == "toc 55.0 < 70"
    assert r["accepted"] is True and "hard_fail" not in r


def test_a_hard_fail_is_recorded_on_the_row(worker, s3, tmp_path):
    """Distinct from the gate: it says the pipeline is out of options, so the run should not
    simply be repeated."""
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 93)
    p.finished("p", "A__1", 93, {"status": "done", "gate": "fail", "steps": {},
                                 "fallback": True, "fallback_tier": "toc_rescue",
                                 "fallback_reason": "completeness 35.1 < 60",
                                 "fallback_hard_fail": True,
                                 "fallback_accepted": False}, 1594.0)
    r = _rows(s3)[0]
    assert r["hard_fail"] is True and r["accepted"] is False


def test_the_live_trigger_is_cleared_between_documents(worker, s3, tmp_path):
    """Otherwise the next document inherits the previous one's escalation reason."""
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 10)
    p.fallback({"reason": "toc 55.0 < 70"})
    p.finished("p", "A__1", 10, {"status": "done", "gate": "pass", "steps": {}}, 5.0)
    p.start("p", "B__2", 10)
    assert (p.d["current"] or {}).get("fallback") is None
    p.finished("p", "B__2", 10, {"status": "done", "gate": "pass", "steps": {}}, 5.0)
    assert "trigger_reason" not in _rows(s3)[1]


def test_a_fallback_announcement_with_no_document_in_flight_is_ignored(worker, s3, tmp_path):
    """Belt and braces: progress bookkeeping must never raise into an extraction run."""
    p = _prog(worker, tmp_path)
    p.fallback({"reason": "toc 55.0 < 70"})          # must not raise
    assert p.d["current"] is None


# ---- live stage timings ----------------------------------------------------------------------

def test_each_stage_is_closed_off_as_the_next_one_begins(worker, s3, tmp_path):
    """run_one builds the same breakdown but only writes it to the scorecard at the very end, so
    without this a document in flight could say which stage it was in and nothing about the ones
    already done — for up to 40 minutes on the largest documents here."""
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 100)
    p.stage("stage1")
    p.d["current"]["stage_at"] -= 12          # pretend stage1 ran for 12s
    p.stage("stage2_mineru")
    steps = p.d["current"]["steps"]
    assert 11.9 <= steps["stage1"] <= 12.6
    assert "stage2_mineru" not in steps, "the stage in flight has no final duration yet"
    assert p.d["current"]["stage"] == "stage2_mineru"


def test_a_stage_that_runs_twice_accumulates_rather_than_overwrites(worker, s3, tmp_path):
    """A fallback tier re-runs stages under their own names; assigning would report only the
    last occurrence."""
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 10)
    p.stage("stage1")
    p.d["current"]["stage_at"] -= 5
    p.stage("stage2_mineru")
    p.d["current"]["stage_at"] -= 3
    p.stage("stage1")
    p.d["current"]["stage_at"] -= 4
    p.stage("stage3")
    assert 8.9 <= p.d["current"]["steps"]["stage1"] <= 9.6, "5s + 4s"


def test_the_placeholder_starting_stage_is_never_recorded(worker, s3, tmp_path):
    """`starting` is a marker, not a pipeline stage — it would appear as a phantom step."""
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 10)
    p.stage("stage1")
    assert "starting" not in (p.d["current"].get("steps") or {})


def test_live_steps_do_not_leak_into_the_next_document(worker, s3, tmp_path):
    p = _prog(worker, tmp_path)
    p.start("p", "A__1", 10)
    p.stage("stage1")
    p.stage("stage2_mineru")
    p.finished("p", "A__1", 10, {"status": "done", "gate": "pass", "steps": {}}, 5.0)
    p.start("p", "B__2", 10)
    assert (p.d["current"].get("steps") or {}) == {}


# ------------------------------------------------- stage 4 health survives to the screen
#
# The "AI stage" column in the Core Index Extraction tab is fed from the ledger, not from a
# per-document scorecard read (891 of those per page load is what took the service down). So
# the field has to survive every hop: run_one's result -> corpus_worker's row -> the ledger
# JSONL -> extraction_monitor._finished_job. A break anywhere in that chain shows up as a
# column of dashes on a run that did record the answer, which is indistinguishable from a run
# extracted before the field existed.

def test_stage_4_health_reaches_the_ledger_and_the_screen(worker, s3, tmp_path):
    from aosphere_core_index.service import extraction_monitor as em
    p = _prog(worker, tmp_path)
    _done(worker, p, "Belgium__163341", stage4_status="failed",
          stage4_failed_sections=["09-marketing-selling-to-the-public.md",
                                  "15-disclaimers-open-ended-fund.md"])
    row = _rows(s3)[0]
    assert row["stage4_status"] == "failed"
    assert row["stage4_failed_sections"] == ["09-marketing-selling-to-the-public.md",
                                             "15-disclaimers-open-ended-fund.md"]
    job = em._finished_job(row)
    assert job["stage4_status"] == "failed"
    assert len(job["stage4_failed_sections"]) == 2


def test_a_document_stage_4_never_touched_writes_no_stage_4_field_at_all(worker, s3, tmp_path):
    """Absent, not "ok" — the screen has to tell "stage 4 did not apply here" from "it ran and
    every section came back", and _row() drops Nones precisely so absence stays meaningful."""
    from aosphere_core_index.service import extraction_monitor as em
    p = _prog(worker, tmp_path)
    _done(worker, p, "Malaysia__176985")
    row = _rows(s3)[0]
    assert "stage4_status" not in row
    assert em._finished_job(row)["stage4_status"] is None


def test_an_old_ledger_row_reads_as_blank_not_green(worker, s3, tmp_path):
    """Every run already in the bucket predates this field. Defaulting it to "ok" would put a
    green tick on 891 documents nobody checked."""
    from aosphere_core_index.service import extraction_monitor as em
    job = em._finished_job({"product": "p", "label": "l", "gate": "pass", "worst": 91.0})
    assert job["stage4_status"] is None and job["stage4_failed_sections"] == []
