"""Can anyone tell what stage 4 is actually pushing at Bedrock, and how far through it is?

Stage 4 sends page images: a grounded section is every one of its pages as a 150 dpi PNG,
base64-encoded into a Converse request that can run to tens of megabytes. Three questions
had no answer anywhere in the log, and each of them is a different wrong conclusion:

  how big is it          -- a section that takes 20 minutes is a different problem at
                            2 MB than at 40 MB, and the call site cannot know the figure:
                            it exists only once botocore has encoded and signed the body
  has it gone yet        -- "call open 20 minutes" is us still building the request or
                            Bedrock not answering one we sent, and those share nothing
  did they all go        -- a stage killed at section 40 of 60 leaves forty perfectly
                            healthy call.end events and nothing at all to say that twenty
                            more were meant to follow

What pins them is, respectively: the before-send hook (the only place the true wire size
is knowable), the phase flip on the in-flight registry, and a declared total reconciled at
the end.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from aosphere_core_index.extract import ai_postprocess as A  # noqa: E402
from aosphere_core_index.obs import log as L  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    L.unbind(*L.snapshot().keys())
    yield
    L.unbind(*L.snapshot().keys())


def _events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if line.startswith("{")]


def _by(events, action):
    return [e for e in events if e.get("event.action") == action]


# ---------------------------------------------------------------- the wire size


def _client_to_nowhere(monkeypatch):
    """A real bedrock-runtime client whose requests are signed and then go nowhere.

    A stub object with a .converse method cannot test any of this: the hook under test is
    botocore's, and everything it measures -- serialization, base64, signing -- is the part
    a stub skips. Pointing a genuine client at a closed port exercises the whole client
    stack up to the socket write and no further.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAnotreal")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "notreal")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "")
    import boto3
    from botocore.config import Config

    real_client = boto3.Session.client

    def _fast(self, service, **kw):
        kw["endpoint_url"] = "http://127.0.0.1:1"
        kw["config"] = Config(retries={"max_attempts": 1},
                              connect_timeout=1, read_timeout=1)
        return real_client(self, service, **kw)

    monkeypatch.setattr(boto3.Session, "client", _fast)
    return A.bedrock_client(region="eu-west-2")


def test_the_wire_size_is_measured_after_encoding_not_before(capsys, monkeypatch):
    """The logged size is the SERIALIZED body, not the bytes the caller handed over.

    This is the whole reason the measurement lives in a botocore hook. JSON cannot carry
    bytes, so Converse takes the PNG base64-encoded at roughly 4/3 of its size on disk --
    a 30 MB section of page images is ~40 MB on the wire. A figure taken at the call site
    would understate every upload this stage makes by a quarter, consistently, in the
    direction that makes a payload problem look smaller than it is.
    """
    client = _client_to_nowhere(monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300_000
    capsys.readouterr()
    with pytest.raises(Exception):
        client.converse(
            modelId=A.HAIKU, system=[{"text": "s"}],
            messages=[{"role": "user", "content": [
                {"image": {"format": "png", "source": {"bytes": png}}},
                {"text": "repair this"}]}],
            inferenceConfig={"maxTokens": 16})

    sent = _by(_events(capsys), "bedrock.request.sent")
    assert sent, "a request left the client and nothing recorded its size"
    n = sent[0]["aci.wire_bytes"]
    assert n > len(png), \
        f"{n} is not larger than the {len(png)} raw bytes — the base64 inflation was missed"
    assert n < len(png) * 1.6, f"{n} is implausibly larger than 4/3 of {len(png)}"
    assert sent[0]["aci.wire_mb"] == round(n / 1_000_000, 2)


def test_every_attempt_is_counted_not_every_call(capsys, monkeypatch):
    """A retried request re-uploads the whole payload, and the log says so.

    botocore retries inside converse(), so from the call site one section is one call no
    matter how many times its images went up. Counting requests rather than calls is what
    makes the bandwidth cost of a throttled region visible at all.
    """
    client = _client_to_nowhere(monkeypatch)
    png = b"\x89PNG" + b"\x00" * 50_000
    capsys.readouterr()
    for _ in range(3):
        with pytest.raises(Exception):
            client.converse(modelId=A.HAIKU, system=[{"text": "s"}],
                            messages=[{"role": "user", "content": [
                                {"image": {"format": "png", "source": {"bytes": png}}}]}],
                            inferenceConfig={"maxTokens": 16})

    sent = _by(_events(capsys), "bedrock.request.sent")
    assert len(sent) >= 3, f"three calls produced only {len(sent)} request records"
    # The running total only ever climbs, and the last one is the sum of the parts.
    totals = [e["aci.upload_total_bytes"] for e in sent]
    assert totals == sorted(totals), f"the cumulative total went backwards: {totals}"
    assert totals[-1] == sum(e["aci.wire_bytes"] for e in sent) + (
        totals[0] - sent[0]["aci.wire_bytes"])


def test_a_body_with_no_length_is_not_guessed_at(capsys, monkeypatch):
    """A streaming body is skipped rather than measured wrong.

    Reading a file-like body to length it would consume it and send an empty request --
    a diagnostic that breaks the thing it measures. Nothing is logged instead.
    """
    client = _client_to_nowhere(monkeypatch)
    capsys.readouterr()

    class _Unsized:
        def read(self, *_a):
            raise AssertionError("the hook consumed a streaming body")

    hooks = client.meta.events
    hooks.emit("before-send.bedrock-runtime.Converse",
               request=type("R", (), {"body": _Unsized()})())
    assert not _by(_events(capsys), "bedrock.request.sent")


# ---------------------------------------------------------------- sent, or still ours?


def test_an_open_call_says_whether_it_is_still_being_built_or_is_waiting(capsys):
    """The phase flip: "preparing" is our time, "sent" is Bedrock's.

    Without it a 20-minute heartbeat means either, and the two have nothing in common.
    """
    seen = {}
    ready, go = threading.Event(), threading.Event()

    def _call():
        with L.api_call("bedrock.converse", **{"aci.api_bytes": 1_000}):
            seen["preparing"] = L.inflight()
            L.note(**{"aci.api_phase": "sent", "_sent_at": time.time() - 42.0})
            ready.set()
            go.wait(2)
            seen["sent"] = L.inflight()

    t = threading.Thread(target=_call)
    t.start()
    ready.wait(2)
    go.set()
    t.join(3)

    assert seen["preparing"]["aci.api_phase"] == "preparing"
    assert "aci.api_waiting_s" not in seen["preparing"], \
        "a request that has not left cannot have been waiting on a response"
    assert seen["sent"]["aci.api_phase"] == "sent"
    assert seen["sent"]["aci.api_waiting_s"] >= 42.0
    assert seen["sent"]["aci.api_sent"] == 1
    # The registry's bookkeeping must never reach an event.
    assert not [k for k in seen["sent"] if k.startswith("_")]


def test_bytes_in_flight_are_totalled_across_workers_not_taken_from_the_oldest():
    """Six concurrent sections at 15 MB is 90 MB on one uplink, and that is the number
    that explains a slow pod when no single call looks unusual."""
    n = 6
    ready, go = threading.Event(), threading.Event()
    arrived, snap = [], {}
    lock = threading.Lock()

    def _worker(i):
        with L.api_call("bedrock.converse", **{"aci.api_bytes": 15_000_000}):
            with lock:
                arrived.append(i)
                if len(arrived) == n:
                    ready.set()
            go.wait(3)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    assert ready.wait(3), "workers never all arrived"
    snap = L.inflight()
    go.set()
    for t in threads:
        t.join(3)

    assert snap["aci.api_inflight"] == n
    assert snap["aci.api_inflight_bytes"] == 15_000_000 * n
    assert snap["aci.api_inflight_mb"] == 90.0
    assert L.inflight() == {}, "the registry leaked an entry"


def test_note_reaches_this_threads_call_and_no_other():
    """The hook runs on the calling thread and is handed no handle to anything of ours,
    so the thread IS the handle. One worker's payload size must not land on another's
    entry, or the heartbeat reports the wrong section's megabytes."""
    ready, go = threading.Event(), threading.Event()
    n = 4
    arrived = []
    lock = threading.Lock()

    def _worker(i):
        with L.api_call("bedrock.converse", **{"aci.api_purpose": f"w{i}"}):
            L.note(**{"aci.api_bytes": (i + 1) * 1_000})
            with lock:
                arrived.append(i)
                if len(arrived) == n:
                    ready.set()
            go.wait(3)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    assert ready.wait(3)
    with L._inflight_lock:
        recs = list(L._inflight.values())
    go.set()
    for t in threads:
        t.join(3)

    got = {r["aci.api_purpose"]: r["aci.api_bytes"] for r in recs}
    assert got == {f"w{i}": (i + 1) * 1_000 for i in range(n)}, \
        f"a note() landed on the wrong thread's call: {got}"


def test_note_on_a_thread_with_no_open_call_is_a_no_op():
    """The hook fires for every bedrock-runtime request, including ones this module did
    not make. It must not invent a registry entry, or the heartbeat reports a call that
    nothing is waiting on."""
    L.note(**{"aci.api_bytes": 999})
    assert L.inflight() == {}


# ---------------------------------------------------------------- did they all go?


def _sections(tmp_path, n):
    d = tmp_path / "stage3"
    d.mkdir()
    for i in range(n):
        (d / f"{i:02d}-section.md").write_text(
            f"## {i} Heading\n\nSome genuine body content for section {i}, long enough "
            f"that the empty-shell check does not skip it before any call is made.\n")
    return d


def test_the_upload_sequence_declares_its_total_and_reconciles_it(
        capsys, tmp_path, monkeypatch):
    """start says how many are coming, end says how many arrived, and the boolean says
    whether those matched. Counting call.end records in Kibana is not a substitute: it
    cannot distinguish "all sixty succeeded" from "forty succeeded and the pod died"."""
    stage3 = _sections(tmp_path, 5)

    class _Ok:
        def converse(self, **kw):
            body = kw["messages"][0]["content"][-1]["text"]
            head = [ln for ln in body.splitlines() if ln.startswith("## ")]
            return {"output": {"message": {"content": [
                        {"text": (head[0] if head else "## x") + "\n\nSome genuine body "
                                 "content for that section, returned unchanged.\n"}]}},
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                    "stopReason": "end_turn"}

    capsys.readouterr()
    A.run_stage4_section(stage3, tmp_path / "stage4", client=_Ok(),
                         log=lambda *_a, **_k: None, subchunk_after=False)
    ev = _events(capsys)

    start = _by(ev, "stage4.upload.start")
    end = _by(ev, "stage4.upload.end")
    progress = _by(ev, "stage4.upload.progress")
    assert len(start) == 1 and start[0]["aci.sections_total"] == 5
    assert len(end) == 1
    assert end[0]["aci.upload_complete"] is True
    assert end[0]["aci.sections_ok"] == 5
    assert end[0]["aci.sections_failed"] == 0
    # One per section, and the counter never repeats or skips — a gap here is a section
    # whose result was dropped on the way out of the pool.
    assert sorted(e["aci.sections_done"] for e in progress) == [1, 2, 3, 4, 5]
    assert all(e["aci.sections_total"] == 5 for e in progress)
    assert all(e["aci.sections_remaining"] == 5 - e["aci.sections_done"] for e in progress)


def test_a_failed_section_makes_the_sequence_report_itself_incomplete(
        capsys, tmp_path):
    """The point of the boolean. A run where Bedrock refused every call used to end with
    a cheerful summary line and no single field saying the uploads did not happen."""
    stage3 = _sections(tmp_path, 3)

    class _Denied:
        def converse(self, **_kw):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "AccessDeniedException",
                                         "Message": "not authorized"}}, "Converse")

    capsys.readouterr()
    A.run_stage4_section(stage3, tmp_path / "stage4", client=_Denied(),
                         log=lambda *_a, **_k: None, subchunk_after=False)
    end = _by(_events(capsys), "stage4.upload.end")
    assert len(end) == 1
    assert end[0]["aci.upload_complete"] is False
    assert end[0]["aci.sections_failed"] == 3
    assert end[0]["aci.sections_ok"] == 0
    assert end[0]["log.level"] == "warn", \
        "a stage that uploaded nothing it meant to must be findable by level alone"
