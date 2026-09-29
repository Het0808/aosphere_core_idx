"""A failure must not be visible ONLY in the pod log.

The gap this closes was found from a real report: a ReadTimeoutError was in the container log
that ArgoCD shows, and could not be found in Elasticsearch at all. The reason is the split
between the two streams -- every structured event goes to STDOUT, while an uncaught exception
prints its traceback to STDERR and nothing else is emitted. A log agent harvesting stdout (or
simply a person searching by field) therefore sees a run that stops mid-sentence: the last
event is whatever the worker happened to be doing, so a crash reads as a stall.

Three properties, one per way a failure used to disappear:

    the process dies          -> run.crash, with error.type, before the traceback
    a thread dies             -> thread.crash, and the process keeps going
    a fallback is taken       -> the fallback says so, rather than changing behaviour quietly
"""
import json
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _run(body: str) -> tuple[list[dict], str, int]:
    """Run a snippet in its own process; return (json events on stdout, stderr, exit code)."""
    script = f"import sys\nsys.path.insert(0, {str(ROOT / 'src')!r})\n" + body
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       timeout=60)
    events = []
    for line in r.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events, r.stderr, r.returncode


def test_an_uncaught_exception_emits_a_json_event_before_it_dies():
    """The exact case from the report: a Bedrock read timeout escaping to the top."""
    events, stderr, code = _run("""
from botocore.exceptions import ReadTimeoutError
from aosphere_core_index.obs import log as L
L.install_crash_handlers()
L.bind_service("aosphere-extract", **{"aci.run_id": "r1", "aci.shard": 3})
raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.eu-west-2.amazonaws.com")
""")
    crash = [e for e in events if e["event.action"] == "run.crash"]
    assert crash, "a crash emitted no event — this is the whole bug"
    c = crash[0]
    assert c["error.type"] == "ReadTimeoutError"          # searchable by field in Kibana
    assert c["log.level"] == "error"
    assert c["aci.fatal"] is True
    assert c["aci.run_id"] == "r1" and c["aci.shard"] == 3  # the bound context survives
    assert "ReadTimeoutError" in c["error.stack_trace"]
    # and stderr still gets its traceback: nothing that reads the pod log loses anything
    assert "ReadTimeoutError" in stderr
    assert code != 0


def test_a_dying_thread_is_reported_and_the_process_carries_on():
    """The worse half. Stage 4 fans sections over a ThreadPoolExecutor and the heartbeat runs
    on its own thread, so a thread dying quietly leaves a worker that LOOKS alive and quietly
    does less than it should."""
    events, _stderr, code = _run("""
import threading
from aosphere_core_index.obs import log as L
L.install_crash_handlers()
L.bind_service("aosphere-extract", **{"aci.run_id": "r1"})
def boom():
    raise RuntimeError("section thread died")
t = threading.Thread(target=boom, name="stage4-worker-1"); t.start(); t.join()
L.event("run.end", message="still here")
""")
    crash = [e for e in events if e["event.action"] == "thread.crash"]
    assert crash, "a thread died with no event"
    assert crash[0]["error.type"] == "RuntimeError"
    assert crash[0]["aci.thread"] == "stage4-worker-1"
    assert crash[0]["aci.fatal"] is False
    # the process really did survive, which is why this needs its own event
    assert [e for e in events if e["event.action"] == "run.end"]
    assert code == 0


def test_a_clean_exit_is_not_reported_as_a_crash():
    """sys.exit(0) after a graceful stop is the worker's NORMAL ending — the interrupt path
    uses it. Logging that as a crash would make every eviction look like a failure."""
    events, _stderr, code = _run("""
import sys
from aosphere_core_index.obs import log as L
L.install_crash_handlers()
L.event("run.end", message="done")
sys.exit(0)
""")
    assert [e for e in events if e["event.action"] == "run.end"]
    assert not [e for e in events if e["event.action"] == "run.crash"]
    assert code == 0


def test_installing_twice_does_not_log_the_crash_twice():
    """The hooks CHAIN, so a second install would make one crash arrive two or three times
    and a Kibana count of failures would read high."""
    events, _stderr, _code = _run("""
from aosphere_core_index.obs import log as L
L.install_crash_handlers()
L.install_crash_handlers()
L.install_crash_handlers()
raise ValueError("once")
""")
    assert len([e for e in events if e["event.action"] == "run.crash"]) == 1


def test_the_worker_installs_them_before_anything_can_fail():
    """A handler nobody installs is not a handler. The worker's entry point must arm these
    before the source sync, the S3 listing or the first document."""
    src = (SCRIPTS / "corpus_worker.py").read_text()
    assert "_log.install_crash_handlers()" in src
    i_install = src.index("_log.install_crash_handlers()")
    # before the work starts: the sync, the resume listing and the document loop. The CALL
    # sites, not the definitions -- both functions are defined earlier in the file.
    for later in ("pull_sources(args.source", "done_labels(args.results",
                  "for i, (product, job) in enumerate"):
        assert i_install < src.index(later), f"crash handlers are armed after {later}"


def test_run_corpus_installs_them_too():
    """The local CLI path emits the same events and had the same hole."""
    assert "_log.install_crash_handlers()" in (SCRIPTS / "run_corpus.py").read_text()


# ------------------------------------------------- fallbacks that used to change behaviour


def test_a_progress_upload_that_stops_reaching_S3_says_so(tmp_path, monkeypatch):
    """The shard summary is how the monitor knows a worker is alive. If the PUT is denied,
    updated_at stops moving, the screen calls the worker stale and then dead -- while the
    worker is healthy and nothing anywhere says which is true."""
    import corpus_worker as W

    calls = {"rc": 0}
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None:
                        types.SimpleNamespace(returncode=calls["rc"], stdout="",
                                              stderr="AccessDenied: not authorized"))
    monkeypatch.setattr(W, "env_fingerprint", lambda *a, **k: {})
    events = []
    monkeypatch.setattr(W._log, "event",
                        lambda action, **kw: events.append((action, kw.get("level"))))

    p = W.ShardProgress("run1", 0, 1, 5, 100, tmp_path, "s3://b/corpus/run1", None)
    calls["rc"] = 1                                  # the PUT starts failing
    p.publish()
    assert ("progress.publish_failing", "error") in events

    # ...and it must not repeat on every heartbeat, or one broken permission is thousands
    # of identical lines
    before = len(events)
    p.publish(); p.publish(); p.publish()
    assert len(events) == before

    calls["rc"] = 0                                  # and recovery is worth one line
    p.publish()
    assert any(a == "progress.publish_recovered" for a, _ in events)


def test_stage4_falls_back_to_one_worker_not_three_and_says_so(monkeypatch):
    """A failed settings import used to TRIPLE Bedrock concurrency silently -- the opposite
    of what a host setting ACI_STAGE4_AI_WORKERS=1 asked for."""
    src = (ROOT / "src" / "aosphere_core_index" / "extract" / "ai_postprocess.py").read_text()
    i = src.index("could not read stage4_ai_workers")
    window = src[i - 400:i]
    assert "workers = 1" in window, "the fallback must be the SAFE value"
    assert "workers = 3" not in window


def test_an_unclassifiable_failure_reports_that_the_classifier_broke(monkeypatch):
    """"unknown, retryable" is the safe answer and an indistinguishable one: it also means a
    deterministically-failing document is re-attempted on every future run."""
    import corpus_worker as W

    events = []
    monkeypatch.setattr(W._log, "event", lambda action, **kw: events.append(action))
    monkeypatch.setitem(sys.modules, "aosphere_core_index.service.extraction_monitor", None)

    cause, permanent = W.classify("CUDA out of memory")
    assert (cause, permanent) == ("unknown", False)
    assert "classify.unavailable" in events


def test_a_signal_handler_can_log_while_a_log_is_already_in_progress():
    """A signal handler runs on the MAIN thread, wherever that thread happens to be --
    including inside the logger's own critical section. corpus_worker's SIGTERM handler logs
    `run.signal` as its first act, so with a non-reentrant lock the handler deadlocked
    against a write the same thread was already doing: the interrupt bookkeeping never ran,
    the shard was never marked interrupted, and the pod left only when the watchdog gave up.

    Subprocess with a timeout, because the failure mode is a hang rather than an exception.
    """
    events, _stderr, code = _run("""
import os, signal, time
from aosphere_core_index.obs import log as L

def handler(signum, frame):
    L.event("run.signal", message="signal while the main thread was mid-write")

signal.signal(signal.SIGTERM, handler)
with L._write_lock:                       # exactly where event() spends its time
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.2)
L.event("run.end", message="main thread carried on")
""")
    actions = [e["event.action"] for e in events]
    assert "run.signal" in actions, "the handler could not log — this is the deadlock"
    assert "run.end" in actions, "the main thread never recovered the lock"
    assert code == 0


def test_the_fallback_shim_carries_every_name_the_pipeline_reads_on_it():
    """run_corpus swaps in a no-op _log when the obs package will not import, and then
    truncates error text with _log.MAX_STR. A missing name there is an AttributeError raised
    while REPORTING a failure -- the shim exists precisely to stop that."""
    src = (SCRIPTS / "run_corpus.py").read_text()
    shim = src[src.index("    class _log:"):src.index("    _probe = None")]
    for name in sorted(set(__import__("re").findall(r"_log\.([a-zA-Z_]+)", src))):
        assert name in shim, f"run_corpus reads _log.{name} but the shim does not define it"


def test_stage_four_survives_the_obs_package_not_importing():
    """Every obs import in ai_postprocess is lazy for one reason: the package failing to
    import is a case this pipeline survives. A module-level one made that survivable case
    fatal for the whole of stage 4."""
    events, stderr, code = _run("""
import sys
sys.modules['aosphere_core_index.obs.log'] = None      # the scenario the shim exists for
import aosphere_core_index.extract.ai_postprocess as A
print("imported, cap =", A._LOG_MAX)
""")
    assert code == 0, f"ai_postprocess no longer imports without obs: {stderr[-400:]}"
