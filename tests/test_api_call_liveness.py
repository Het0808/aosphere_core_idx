"""While an API call is in flight, can anyone tell whether it is alive?

A Bedrock converse can legitimately run for minutes, and between `bedrock.call.start` and
`bedrock.call.end` the process used to say nothing about it at all. Twelve minutes of silence
reads identically whether the model is still thinking, the connection died under us, or the
pod is wedged — which is exactly the ambiguity the MRAM run was debugged through.

Two halves, and they answer different questions:

    the heartbeat names the open call   -> a person can SEE it is still waiting, and for how long
    TCP keepalive on the client         -> the SOCKET notices a dead peer instead of waiting out
                                           the 900s read timeout on a connection that is gone
"""
import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from aosphere_core_index.extract import ai_postprocess as A  # noqa: E402
from aosphere_core_index.obs import log as L  # noqa: E402
from aosphere_core_index.obs import probe as P  # noqa: E402


class _SlowBedrock:
    """Stands in for a call that takes far longer than one heartbeat interval."""

    def __init__(self, seconds=0.35, fail=False):
        self.seconds, self.fail = seconds, fail

    def converse(self, **_kw):
        time.sleep(self.seconds)
        if self.fail:
            from botocore.exceptions import ReadTimeoutError
            raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.eu-west-2.amazonaws.com")
        return {"output": {"message": {"content": [{"text": "<tr><td>x</td></tr>"}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5}}


def _batch():
    return types.SimpleNamespace(header="", html="<tr><td>x</td></tr>", rows=["x"])


def _events(capsys):
    out = []
    for line in capsys.readouterr().out.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def test_every_beat_names_the_open_call_and_how_long_it_has_waited(capsys):
    """The number is the point: climbing beat by beat means the call is still open, and the
    absence of beats means the worker itself is gone. One filter separates them."""
    L.bind(**{"aci.stage": "stage4_ai", "aci.stage_started_at": time.time()})
    hb = P.Heartbeat(interval=0.1, watch=None, scratch="/tmp").start()
    try:
        A.call_model(_SlowBedrock(0.35), "test-model", _batch())
    finally:
        hb.stop()
        time.sleep(0.15)
        L.unbind("aci.stage", "aci.stage_started_at")

    beats = [e for e in _events(capsys) if e["event.action"] == "stage.heartbeat"]
    assert beats, "no heartbeat fired during the call"
    live = [b for b in beats if "aci.api_oldest_s" in b]
    assert live, "a beat during an open call must say a call is open"
    assert live[0]["aci.api"] == "bedrock.converse"
    assert live[0]["aci.api_model"] == "test-model"
    assert live[0]["aci.api_inflight"] == 1
    assert "call(s) open" in live[0]["message"]
    # the age is real, not a constant
    assert all(b["aci.api_oldest_s"] >= 0 for b in live)


def test_a_finished_call_stops_being_reported_as_open(capsys):
    """The failure mode of the fix itself. A call left bound would make every later beat
    claim a call is in flight — a permanently 'waiting' worker that is doing nothing of
    the sort, which is worse than the silence it replaced."""
    L.bind(**{"aci.stage": "stage4_ai", "aci.stage_started_at": time.time()})
    A.call_model(_SlowBedrock(0.01), "test-model", _batch())
    hb = P.Heartbeat(interval=0.1, watch=None, scratch="/tmp").start()
    time.sleep(0.25)
    hb.stop()
    time.sleep(0.15)
    L.unbind("aci.stage", "aci.stage_started_at")

    beats = [e for e in _events(capsys) if e["event.action"] == "stage.heartbeat"]
    assert beats, "no heartbeat fired"
    assert not any("aci.api_oldest_s" in b for b in beats), \
        "a beat after the call ended still claims one is open"


def test_a_call_that_FAILS_also_stops_being_reported_as_open(capsys):
    """The unbind has to be in a finally: the interesting calls are the ones that fail, and
    those are exactly the ones whose state would otherwise stick."""
    L.bind(**{"aci.stage": "stage4_ai", "aci.stage_started_at": time.time()})
    from botocore.exceptions import ReadTimeoutError
    try:
        A.call_model(_SlowBedrock(0.01, fail=True), "test-model", _batch())
    except ReadTimeoutError:
        pass
    hb = P.Heartbeat(interval=0.1, watch=None, scratch="/tmp").start()
    time.sleep(0.25)
    hb.stop()
    time.sleep(0.15)
    L.unbind("aci.stage", "aci.stage_started_at")

    beats = [e for e in _events(capsys) if e["event.action"] == "stage.heartbeat"]
    assert not any("aci.api_oldest_s" in b for b in beats)


def test_the_client_does_not_pretend_keepalive_is_protecting_it():
    """tcp_keepalive=True sets SO_KEEPALIVE and nothing else, so the timers are the OS's --
    first probe after 7200s by default. A line dying mid-call is found by read_timeout at
    900s and a pooled connection killed by a NAT idle timeout (~350s) is reused dead long
    before the OS looks. Enabling it here would be cover, not detection; it belongs with
    tuned pod sysctls or not at all."""
    captured = {}

    class _Client:
        meta = types.SimpleNamespace(events=types.SimpleNamespace(register=lambda *a: None))

    class _Session:
        def client(self, *_a, **kw):
            captured.update(kw)
            return _Client()

    import boto3
    orig = boto3.Session
    boto3.Session = lambda *a, **kw: _Session()
    try:
        A.bedrock_client()
    finally:
        boto3.Session = orig

    cfg = captured["config"]
    assert not cfg.tcp_keepalive
    # the timeouts that DO the work are still in place
    assert cfg.read_timeout == 900 and cfg.connect_timeout == 30


# ------------------------------------------------- six at once, which is what is deployed


def test_all_six_concurrent_calls_are_counted_and_the_OLDEST_is_the_one_reported():
    """The deployment runs ACI_STAGE4_AI_WORKERS=6. Five healthy calls finishing in seconds
    and one open for minutes: a count alone says "busy", the newest says "fine", and only the
    oldest says which section to go and look at. The single context slot this replaced
    reported the newest — the one least likely to be the problem.

    A barrier rather than sleeps: the property is "six open AT ONCE", and timing a heartbeat
    to land inside that window makes the test about scheduling luck instead.
    """
    import threading

    sections = ["14-disclaimers.md"] + [f"{n}-quick.md" for n in range(15, 20)]
    all_open = threading.Barrier(len(sections) + 1)
    release = threading.Event()
    entered = []
    entered_lock = threading.Lock()

    def one(section, delay):
        time.sleep(delay)                       # staggers the START times, not the overlap
        with L.context(**{"aci.section": section}):
            with L.api_call("bedrock.converse", **{"aci.api_model": "sonnet"}):
                with entered_lock:
                    entered.append(section)
                all_open.wait(timeout=10)
                release.wait(timeout=10)

    threads = [threading.Thread(target=one, args=(s_, 0.05 * i), name=f"stage4-{i}")
               for i, s_ in enumerate(sections)]
    for t in threads:
        t.start()
    try:
        all_open.wait(timeout=10)               # every call is now open, provably
        snap = L.inflight()
    finally:
        release.set()
        for t in threads:
            t.join(timeout=10)

    assert snap["aci.api_inflight"] == 6, "not every concurrent call was counted"
    # the OLDEST — the first to enter, not the last
    assert snap["aci.api_section"] == entered[0] == "14-disclaimers.md"
    assert snap["aci.api_oldest_s"] >= 0
    # and nothing is left behind once they finish
    assert L.inflight() == {}


def test_the_beat_reports_the_open_call_and_its_age_climbs(capsys):
    """The heartbeat half, kept separate from the concurrency half above so neither depends
    on the other's timing. A climbing age is what says "still alive" rather than "hung"."""
    L.bind(**{"aci.stage": "stage4_ai", "aci.stage_started_at": time.time()})
    hb = P.Heartbeat(interval=0.15, watch=None, scratch="/tmp").start()
    try:
        with L.context(**{"aci.section": "14-disclaimers.md"}):
            with L.api_call("bedrock.converse", **{"aci.api_model": "sonnet"}):
                time.sleep(0.7)
    finally:
        hb.stop()
        time.sleep(0.2)
        L.unbind("aci.stage", "aci.stage_started_at")

    live = [b for b in _events(capsys)
            if b["event.action"] == "stage.heartbeat" and "aci.api_oldest_s" in b]
    assert len(live) >= 2, "not enough beats landed inside the call to compare ages"
    assert live[-1]["aci.api_oldest_s"] > live[0]["aci.api_oldest_s"]
    assert live[0]["aci.api_section"] == "14-disclaimers.md"
    assert "call(s) open" in live[0]["message"]


def test_a_worker_thread_does_not_wipe_the_stage_from_the_heartbeat(capsys):
    """Found by the test above. A section worker binds its own section and, on the way out,
    restores its own EMPTY context -- which cleared the mirror the beat reads, so every beat
    said "still in None" while the stage was running perfectly well."""
    import threading

    L.bind(**{"aci.stage": "stage4_ai", "aci.stage_started_at": time.time()})

    def worker():
        with L.context(**{"aci.section": "15-scope.md"}):
            time.sleep(0.05)

    t = threading.Thread(target=worker, name="stage4-1")
    t.start()
    t.join()

    assert L.snapshot().get("aci.stage") == "stage4_ai", \
        "a worker thread erased the main thread's context from the heartbeat's view"
    L.unbind("aci.stage", "aci.stage_started_at")
