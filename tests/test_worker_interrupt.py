"""A SIGTERM has to stop the worker WHERE IT IS, not when the document happens to finish.

The MRAM run is the evidence. The signal landed at 08:35:28 and the worker went on
heartbeating `still in stage4_ai` for another twelve minutes — 2,000 seconds into one document,
with Bedrock's adaptive retries attempting again at 08:44:47 — because the handler only set a
flag that the loop checks BETWEEN documents. A Kubernetes termination grace period is 30 seconds
by default and minutes at most, so nothing about that wait is graceful: the kubelet SIGKILLs the
pod partway through, and the clean exit the flag was protecting never happens.

Stopping mid-document costs nothing, which is what makes it the right answer. Resume keys on the
scorecard, so a document interrupted at any point is re-extracted from the top whether it was
abandoned politely or not.

Three properties, one per question the pod's last moments have to answer:

    does it actually stop           the handler raises, out of whatever call is blocking
    what was it doing              the shard summary says interrupted, and names the document
    is it a crash                  no — exit 0, status "interrupted", not "error"
"""

import json
import os
import signal
import sys
import time
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import corpus_worker as W  # noqa: E402

from aosphere_core_index.service import extraction_monitor as em  # noqa: E402


@pytest.fixture
def prog(tmp_path, monkeypatch):
    """A ShardProgress with S3 and the environment probe stubbed out."""
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None:
                        types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(W, "env_fingerprint", lambda *a, **k: {"cuda_available": True})
    return W.ShardProgress("run1", 0, 8, 10, 500, tmp_path, "s3://b/corpus/run1", None)


@pytest.fixture(autouse=True)
def _flag_reset(monkeypatch):
    """_STOP is module state; a test that sets it must not make the next one take the
    second-signal path, which calls os._exit."""
    monkeypatch.setattr(W, "_STOP", False)
    monkeypatch.setattr(W, "_kill_children", lambda: 0)


def test_the_signal_raises_instead_of_letting_the_document_run_on():
    """The whole point: a flag is only read between documents, so a worker 2,000s into
    stage4_ai ignores it for as long as Bedrock keeps retrying."""
    with pytest.raises(W.Interrupted) as e:
        W._on_signal(signal.SIGTERM, None)
    assert e.value.signum == 15
    assert W._STOP is True


def test_the_interrupt_is_not_swallowed_by_the_pipelines_own_except_exception():
    """Stage 4 keeps going when one section fails, every marker write is guarded, and run_one's
    wrapper catches broadly to log. An Exception subclass would be absorbed by any of them and
    the worker would go back to sleep inside the call it was told to leave."""
    assert not issubclass(W.Interrupted, Exception)
    try:
        try:
            raise W.Interrupted(15)
        except Exception:                                    # noqa: BLE001
            pytest.fail("an `except Exception` swallowed the stop")
    except W.Interrupted:
        pass


def test_a_second_signal_exits_immediately_without_bookkeeping(monkeypatch):
    """If the first raise did not get us out, something is swallowing it — so the second signal
    does not try to raise again, it goes."""
    monkeypatch.setattr(W, "_STOP", True)                    # a signal already arrived
    exits = []

    def _exit(code):
        # os._exit does not return, and a stub that does would let the handler fall through to
        # the raise the second signal exists to skip.
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(W.os, "_exit", _exit)
    with pytest.raises(SystemExit):
        W._on_signal(signal.SIGTERM, None)
    assert exits == [128 + 15]


def test_the_shard_records_which_document_was_cut_off_and_where(prog):
    """The one fact the resume cannot reconstruct. Every other document is either finished (a
    scorecard in S3) or untouched; the one in flight left neither."""
    prog.start("155_Data_Privacy", "Canada__96416", 120)
    prog.stage("stage4_ai")
    prog.interrupted(signal.SIGTERM, killed=2)

    assert prog.d["status"] == "interrupted"
    assert prog.d["interrupted_document"] == "155_Data_Privacy/Canada__96416"
    assert prog.d["interrupted_stage"] == "stage4_ai"
    assert prog.d["interrupted_signal"] == 15
    assert prog.d["interrupted_children_killed"] == 2
    # `stopping` stays set as well: every existing reader keys on it, and an interruption IS a
    # stop — the status adds the reason rather than replacing the fact.
    assert prog.d["stopping"] is True
    # Nothing is left claiming to be in flight, or the screen shows a document being worked on
    # by a process that has exited.
    assert prog.d["current"] is None


def test_an_interrupted_document_is_not_counted_as_a_failure(prog):
    """A failure is a document to look at; an interruption is a node that went away. Counting
    the second as the first sends someone to read a traceback that does not exist."""
    prog.start("155_Data_Privacy", "Canada__96416", 120)
    prog.interrupted(signal.SIGTERM)
    assert prog.d["failed"] == 0
    assert prog.d["done"] == 0
    row = prog.d["recent"][0]
    assert row["gate"] == "interrupted"
    assert "error" not in row


def test_the_ledger_keeps_the_interrupted_document(prog, tmp_path):
    """`recent` is capped at ten, so on a shard that restarts and runs on it is the ledger, not
    the summary, that still says which document the eviction caught."""
    prog.start("155_Data_Privacy", "Canada__96416", 120)
    prog.interrupted(signal.SIGTERM)
    rows = [r for r in (tmp_path / "_progress" / "ledger-0.jsonl").read_text().splitlines() if r]
    assert len(rows) == 1
    assert '"gate":"interrupted"' in rows[0]
    assert "Canada__96416" in rows[0]


def test_a_signal_before_the_first_document_records_nothing_and_still_stops(prog):
    """During the 1.23 GB source sync there is nothing in flight. The shard still has to say it
    was interrupted — it just has no document to name."""
    prog.interrupted(signal.SIGTERM)
    assert prog.d["status"] == "interrupted"
    assert prog.d["interrupted_document"] is None
    assert prog.d["recent"] == []


# ---------------------------------------------------------------- what the screen then reads


def _shard(**over):
    d = {"shard": 0, "done": 3, "jobs_total": 40, "pages_done": 300, "pages_total": 4000,
         "failed": 0, "gates": {}, "recent": [], "current": None,
         "started_at": 1000.0, "updated_at": 2000.0, "stopping": False}
    d.update(over)
    return d


def test_the_screen_reads_interrupted_rather_than_inferring_staleness():
    """Before this, an evicted worker sat as `stopping` until the staleness timeout aged it into
    `stale` — the same reading a worker that crashed gets, hours later and for the wrong reason."""
    p = em.summarize([_shard(stopping=True, status="interrupted",
                             interrupted_document="155_Data_Privacy/Canada__96416",
                             interrupted_stage="stage4_ai", interrupted_at=2000.0)],
                     now=2010.0)
    row = p["shards"][0]
    assert row["state"] == "interrupted"
    assert row["interrupted_document"] == "155_Data_Privacy/Canada__96416"
    assert row["interrupted_stage"] == "stage4_ai"
    # And it is NOT counted among the workers still doing something, which is what drives the
    # ETA: extrapolating from a process that has exited is how a dead run claimed 1010 hours.
    assert p["shards_live"] == 0


def test_an_interrupted_worker_stays_interrupted_once_it_goes_quiet():
    """`stopping` is a claim to still be there, so silence outranks it. `interrupted` is the
    worker's last word on the way out, so silence is exactly what is expected next."""
    p = em.summarize([_shard(stopping=True, status="interrupted", updated_at=2000.0)],
                     now=2000.0 + em.STALE_AFTER_S + 600)
    assert p["shards"][0]["state"] == "interrupted"


def test_a_complete_shard_is_not_relabelled_by_a_stale_interrupted_flag():
    """A worker that finished its partition and was then signalled has done its job; the
    partition is complete and the screen must keep saying so."""
    p = em.summarize([_shard(done=40, jobs_total=40, stopping=True, status="interrupted")],
                     now=2010.0)
    assert p["shards"][0]["state"] == "complete"


def test_an_interrupted_job_row_does_not_claim_it_reached_the_gate():
    """The funnel counts a document at the stage it got to. An interrupted row has a stage and
    no verdict — reading it as gated would credit stage 4 with a document it never finished."""
    job = em._finished_job({"product": "155_Data_Privacy", "label": "Canada__96416",
                            "gate": "interrupted", "stage": "stage4_ai", "seconds": 2000.0})
    assert job["status"] == "interrupted"
    assert job["gate"] is None                       # no verdict was ever reached
    assert job["stage_idx"] == em.stage_index("stage4_ai")
    # and it is not a crash: nothing here for a reviewer to read.
    assert job["error"] is None


# ------------------------------------------------------------------- the whole pod, end to end


def test_the_worker_stops_mid_document_and_leaves_S3_saying_why(tmp_path, monkeypatch):
    """The MRAM case, run for real: a signal arrives while run_one is inside stage 4 with
    nothing to report for the next ten minutes. main() has to be out of there in seconds, with
    the shard summary already written — it is the only thing that outlives the pod."""
    import os
    import threading
    import time

    import run_corpus as R

    root = tmp_path / "src" / "155_Data_Privacy"
    root.mkdir(parents=True)
    pdf = root / "Canada.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 300_000)
    job = {"label": "Canada__96416", "pdf": pdf}

    monkeypatch.setattr(W, "_s3", lambda *a, profile=None:
                        types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(W, "env_fingerprint", lambda *a, **k: {"cuda_available": False})
    monkeypatch.setattr(W, "page_count", lambda _p: 120)
    monkeypatch.setattr(R, "discover", lambda _r: {"155_Data_Privacy": [job]})
    monkeypatch.setattr(R, "hash_index", lambda _o: {})
    monkeypatch.setattr(R, "bind_document", lambda *a, **k: None)
    monkeypatch.setattr(R, "unbind_document", lambda *a, **k: None)

    def stage4_that_never_returns(job, product, dest, *a, progress_cb=None, **kw):
        progress_cb("stage4_ai")
        time.sleep(600)                                      # a Bedrock retry, as logged
        raise AssertionError("the worker was not interrupted")

    monkeypatch.setattr(R, "run_one", stage4_that_never_returns)
    monkeypatch.setattr(sys, "argv",
                        ["corpus_worker.py", "--root", str(tmp_path / "src"),
                         "--results", "s3://b/corpus/run1", "--out", str(tmp_path / "out"),
                         "--shards", "1", "--shard", "0"])

    timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    t0 = time.time()
    try:
        with pytest.raises(SystemExit) as exit_:
            W.main()
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # Seconds, not the ten minutes the document had left. Anything longer and the kubelet's
    # grace period expires and SIGKILL takes the bookkeeping below with it.
    assert time.time() - t0 < 30
    # An eviction is not a crash: a non-zero exit restarts the pod into the same reclaim.
    assert exit_.value.code == 0

    shard = json.loads((tmp_path / "out" / "_progress" / "shard-0.json").read_text())
    assert shard["status"] == "interrupted"
    assert shard["interrupted_document"] == "155_Data_Privacy/Canada__96416"
    assert shard["interrupted_stage"] == "stage4_ai"
    assert shard["current"] is None
    assert shard["failed"] == 0


def test_the_mineru_child_is_killed_rather_than_left_holding_the_gpu(monkeypatch):
    """Kubernetes signals PID 1, not its descendants. A MinerU subprocess twelve minutes into a
    page pass never hears about the eviction, so without this it holds the GPU for the whole
    termination grace period while the parent is trying to leave."""
    import subprocess

    pytest.importorskip("psutil")
    monkeypatch.undo()                                       # the autouse stub replaces this
    monkeypatch.setattr(W, "_STOP", False)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        with pytest.raises(W.Interrupted) as e:
            W._on_signal(signal.SIGTERM, None)
        assert e.value.children_killed >= 1
        assert child.wait(timeout=10) != 0                   # killed, not still running
    finally:
        if child.poll() is None:
            child.kill()


# ------------------------------------------- the unwind is not guaranteed to reach main()


def test_the_handler_records_the_interruption_itself(prog, monkeypatch):
    """Not main()'s except clause. Stage 4 can swallow the unwind into a blocking pool
    shutdown (see the next test), and the record has to survive that — it is written in the
    handler, which runs the instant the signal is delivered whatever the stack is doing."""
    monkeypatch.setattr(W, "_PROG", prog)
    monkeypatch.setattr(W, "_arm_watchdog", lambda signum: None)
    prog.start("155_Data_Privacy", "Canada__96416", 165)
    prog.stage("stage4_ai")

    with pytest.raises(W.Interrupted):
        W._on_signal(signal.SIGTERM, None)

    assert prog.d["status"] == "interrupted"
    assert prog.d["interrupted_document"] == "155_Data_Privacy/Canada__96416"
    assert prog.d["interrupted_stage"] == "stage4_ai"


def test_recording_it_twice_does_not_write_the_document_twice(prog):
    """The handler writes it, then main()'s except clause calls the same method on the way out.
    A second ledger row for one document would make the run's own job list disagree with itself
    about how many documents there were."""
    prog.start("155_Data_Privacy", "Canada__96416", 165)
    prog.interrupted(signal.SIGTERM)
    prog.interrupted(signal.SIGTERM)
    assert len(prog.d["recent"]) == 1


def test_a_pool_that_will_not_shut_down_does_not_keep_the_pod_alive(tmp_path):
    """THE case from the MRAM log, and the one a raise alone does not fix.

    Stage 4 fans its sections out over a ThreadPoolExecutor, and `with ThreadPoolExecutor(...)`
    calls shutdown(wait=True) on the way out. So the exception escapes the Bedrock call in the
    main thread and then parks in __exit__ until the worker thread's own 900s call returns —
    measured at 25s and still going, with nothing left to do but wait. The watchdog is what
    makes the pod actually leave.

    Run as a subprocess because the thing under test is os._exit: in-process it would take the
    test session with it."""
    import subprocess

    script = """
import concurrent.futures, os, signal, sys, threading, time
sys.path.insert(0, %r)
import corpus_worker as W
signal.signal(signal.SIGTERM, W._on_signal)
threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(time.sleep, 300)      # one Bedrock call, mid-retry
        fut.result()
except W.Interrupted:
    pass
time.sleep(300)                                  # whatever else might block the exit
""" % str(SCRIPTS)
    env = {**os.environ, "ACI_INTERRUPT_GRACE_S": "3"}
    t0 = time.time()
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       timeout=60, env=env)
    elapsed = time.time() - t0

    assert elapsed < 30, f"the pod was still alive {elapsed:.0f}s after SIGTERM"
    # Zero, not 143: an eviction is not a crash, and a non-zero exit restarts the pod into the
    # same reclaim it was just told about.
    assert r.returncode == 0
    assert "exiting now" in r.stdout


# ------------------------------- an interrupted document must be REDONE, never quietly skipped


@pytest.fixture
def doc(tmp_path, monkeypatch):
    """The document in flight, as the loop registers it."""
    dest = tmp_path / "155_Data_Privacy" / "Canada__96416"
    dest.mkdir(parents=True)
    d = {"dest": dest, "product": "155_Data_Privacy", "label": "Canada__96416",
         "results": "s3://b/corpus/run1", "profile": None, "uploading": False}
    monkeypatch.setattr(W, "_DOC", d)
    return d


def test_a_document_extracted_but_not_uploaded_is_not_skipped_by_the_restarted_pod(doc):
    """The silent-loss window. run_one writes the scorecard, the signal lands before the upload
    finishes — and resume's LOCAL rule ("a scorecard means done") then skips the document on the
    restarted pod, which keeps its emptyDir. It is never uploaded and never extracted again:
    missing from the corpus, with nothing anywhere saying so."""
    (doc["dest"] / "scorecard.json").write_text('{"gate": "pass"}')

    undone = W._disown_current_document()

    assert "local scorecard" in undone
    assert not (doc["dest"] / "scorecard.json").exists()


def test_a_half_uploaded_document_does_not_read_as_finished_in_S3(doc, monkeypatch):
    """`aws s3 sync` uploads in whatever order it likes and scorecard.json is small, so a
    killed upload can leave the marker in S3 with the 152 MB of tree behind it missing. The
    remote rule would then read a truncated document as complete."""
    removed = []
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None: (removed.append(a) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr="")))
    doc["uploading"] = True
    (doc["dest"] / "scorecard.json").write_text('{"gate": "pass"}')

    undone = W._disown_current_document()

    assert "remote scorecard" in undone
    assert removed == [("rm", "s3://b/corpus/run1/155_Data_Privacy/Canada__96416/scorecard.json",
                        "--only-show-errors")]


def test_a_finished_upload_is_left_alone(doc, monkeypatch):
    """The other half of the same rule. Once push_job has returned, S3 holds the whole document
    — deleting that scorecard would discard a finished extraction and buy back its GPU hour for
    nothing."""
    calls = []
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None: (calls.append(a) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr="")))
    doc["uploading"] = False                                 # push_job returned
    (doc["dest"] / "scorecard.json").write_text('{"gate": "pass"}')

    W._disown_current_document()

    assert calls == [], "a completed upload must not have its scorecard deleted"


def test_disowning_between_documents_does_nothing(monkeypatch):
    """A signal arriving with no document in flight has nothing to undo, and must not reach for
    the PREVIOUS document's scorecard — that one is finished and uploaded."""
    monkeypatch.setattr(W, "_DOC", None)
    assert W._disown_current_document() == []


def test_a_run_whose_workers_were_all_signalled_reads_interrupted_not_stalled():
    """`stalled` is there to report silence — workers gone, nobody knows why, the normal shape
    of a spot reclaim nobody asked for. A run stopped on purpose (the Stop Extract Worker
    workflow) announces itself on the way out, and reading that as stalled sends an operator
    looking for a fault that does not exist."""
    p = em.summarize([_shard(stopping=True, status="interrupted"),
                      _shard(shard=1, done=40, jobs_total=40)],          # finished its partition
                     now=2010.0)
    assert p["state"] == "interrupted"


def test_a_genuinely_silent_run_still_reads_stalled():
    """The distinction only works if it stays narrow: one worker that simply stopped reporting,
    with no interruption recorded, is still the case the screen must flag."""
    p = em.summarize([_shard(updated_at=1000.0)], now=1000.0 + em.STALE_AFTER_S + 600)
    assert p["state"] == "stalled"
