"""A reviewer can mark one bad document for re-extraction without redoing a whole run.

Resume is "a document with a scorecard is done". That is what makes an interrupted run cheap, and
what makes a bad result sticky: re-running the same prefix skips exactly the documents someone
wants redone, and the only alternative is a new run_id, which redoes all 891. So a marker under
<run>/_retry/ overrides resume for one document.

Two properties matter beyond that. The scorecard is NOT deleted — the bad result is the evidence
of what went wrong. And the marker is cleared once the document has been re-extracted, or it would
be redone on every future run of that prefix.
"""

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_worker as W  # noqa: E402


def _ls(names):
    return "\n".join(f"2026-08-23 10:00:00        120 {n}" for n in names)


def test_a_marked_document_is_treated_as_not_done(monkeypatch):
    """The core of it: the scorecard is there, and the document is extracted anyway."""
    marker = {"product": "155_Data_Privacy", "label": "Netherlands (Data Privacy)__174642"}

    def fake_s3(*args, profile=None):
        if args[0] == "ls" and "_retry/" in args[1]:
            return types.SimpleNamespace(returncode=0, stdout=_ls(["m.json"]), stderr="")
        if args[0] == "cp" and args[2] == "-":
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(marker), stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(W, "_s3", fake_s3)
    marked = W.retry_labels("s3://b/corpus/run1", None)
    assert marked == {"155_Data_Privacy/Netherlands (Data Privacy)__174642"}


def test_an_unreadable_marker_is_skipped_not_fatal(monkeypatch):
    def fake_s3(*args, profile=None):
        if args[0] == "ls":
            return types.SimpleNamespace(returncode=0, stdout=_ls(["bad.json", "ok.json"]), stderr="")
        if args[0] == "cp":
            body = "" if "bad.json" in args[1] else json.dumps({"product": "p", "label": "l"})
            return types.SimpleNamespace(returncode=0, stdout=body, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(W, "_s3", fake_s3)
    assert W.retry_labels("s3://b/corpus/run1", None) == {"p/l"}


def test_no_markers_means_no_extra_work(monkeypatch):
    monkeypatch.setattr(W, "_s3", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="", stderr="NoSuchKey"))
    assert W.retry_labels("s3://b/corpus/run1", None) == set()


def test_the_marker_is_cleared_by_key_not_by_pattern(monkeypatch, tmp_path):
    """It must delete the marker for THIS document — labels contain spaces and parentheses."""
    calls = []
    monkeypatch.setattr(W, "_s3", lambda *a, **k: calls.append(a) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    W.clear_retry("s3://b/corpus/run1", "155_Data_Privacy",
                  "Netherlands (Data Privacy)__174642", tmp_path, None)
    rm = next(c for c in calls if c[0] == "rm")
    key = rm[1]
    assert key.startswith("s3://b/corpus/run1/_retry/")
    assert " " not in key and "(" not in key, key
    assert "155_Data_Privacy__Netherlands" in key


def test_the_service_and_the_worker_agree_on_the_key():
    """Two components writing and deleting the same object by two sanitisers would silently
    strand markers, so the shapes are pinned together."""
    from aosphere_core_index.service.extraction_monitor import _retry_key
    product, label = "155_Data_Privacy", "United States - California (Short Form)__175656"
    service_key = _retry_key("corpus/run1", product, label)
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{product}__{label}")
    assert service_key == f"corpus/run1/_retry/{safe}.json"


@pytest.mark.parametrize("product,label", [
    ("../x", "l"), ("p", "../../etc/passwd"), ("p", "a/b"),
])
def test_a_marker_cannot_be_written_outside_the_run(product, label):
    from aosphere_core_index.service.extraction_monitor import _retry_key
    with pytest.raises(ValueError):
        _retry_key("corpus/run1", product, label)


# ---------------------------------------------------------------------------
# Telling a CRASH apart from a bad score.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,cause", [
    ("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", "gpu_oom"),
    ("RuntimeError: CUDA error: out of memory", "gpu_oom"),
    ("Killed\nCommand exited with exit status 137", "host_oom"),
    ("MemoryError", "host_oom"),
    ("RuntimeError: Failed to find C compiler. Please specify via CC", "no_compiler"),
    ("DataLoader worker (pid 431) is killed by signal: Bus error", "shared_memory"),
    ("OSError: Can't load the model — offline mode is enabled", "model_missing"),
    ("ReadTimeoutError: Connection reset by peer", "timeout"),
    # was "unknown" before permanence was split out; an exception from our own code is a
    # pipeline bug, and re-running it changes nothing.
    ("IndexError: list index out of range", "pipeline_bug"),
])
def test_a_failure_is_classified_by_what_it_actually_said(text, cause):
    """A GPU that ran out of memory needs a bigger node; a document the pipeline cannot parse
    needs code. Reading one as the other wastes a run either way."""
    from aosphere_core_index.service.extraction_monitor import classify_failure
    assert classify_failure(text) == cause


def test_the_specific_cause_wins_over_the_generic_one():
    """An OOM traceback often also mentions a timeout or a file; order must not flip the verdict."""
    from aosphere_core_index.service.extraction_monitor import classify_failure
    text = ("Connection reset by peer\nNo such file or directory\n"
            "torch.OutOfMemoryError: CUDA out of memory")
    assert classify_failure(text) == "gpu_oom"


def test_causes_are_counted_for_the_screen():
    from aosphere_core_index.service.extraction_monitor import failure_summary
    fails = [{"cause": "gpu_oom"}, {"cause": "gpu_oom"}, {"cause": "unknown"}]
    assert failure_summary(fails) == {"gpu_oom": 2, "unknown": 1}


# ---------------------------------------------------------------------------
# ONE retry per document. A fault the retry cannot change would otherwise be paid
# for on every run of the prefix.
# ---------------------------------------------------------------------------

def test_a_second_retry_is_refused_unless_forced(monkeypatch):
    from aosphere_core_index.service import extraction_monitor as em

    class _S3:
        def __init__(self, **kw): pass
        def exists(self, key): return "/_retried/" in key      # already retried once

    monkeypatch.setattr(em, "ReadOnlyS3", _S3)
    res = em.mark_retry("b", "corpus/run1", "eu-west-1", "155_DP", "Germany__1")
    assert res["queued"] is False and "already retried" in res["reason"]


def test_forcing_it_writes_the_marker_anyway(monkeypatch):
    from aosphere_core_index.service import extraction_monitor as em
    put = {}

    class _S3:
        def __init__(self, **kw): pass
        def exists(self, key): return True

    class _Client:
        def put_object(self, Bucket, Key, Body, ContentType):
            put.update(bucket=Bucket, key=Key, body=json.loads(Body))

    monkeypatch.setattr(em, "ReadOnlyS3", _S3)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: _Client()))
    res = em.mark_retry("b", "corpus/run1", "eu-west-1", "155_DP", "Germany__1", force=True)
    assert res["queued"] is True
    assert put["body"]["forced"] is True
    assert put["key"].startswith("corpus/run1/_retry/")


def test_the_first_retry_needs_no_force(monkeypatch):
    from aosphere_core_index.service import extraction_monitor as em
    put = {}

    class _S3:
        def __init__(self, **kw): pass
        def exists(self, key): return False                     # never retried

    class _Client:
        def put_object(self, Bucket, Key, Body, ContentType):
            put.update(key=Key, body=json.loads(Body))

    monkeypatch.setattr(em, "ReadOnlyS3", _S3)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: _Client()))
    assert em.mark_retry("b", "corpus/run1", "eu-west-1", "155_DP", "Germany__1")["queued"] is True
    assert put["body"]["forced"] is False


def test_the_worker_records_the_retry_it_performed(monkeypatch, tmp_path):
    """That record is what makes the next request a deliberate one."""
    calls = []
    monkeypatch.setattr(W, "_s3", lambda *a, **k: calls.append(a) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    W.clear_retry("s3://b/corpus/run1", "155_DP", "Germany (DP)__1", tmp_path, None)
    ops = [(c[0], c[1] if c[0] == "rm" else c[2]) for c in calls]
    assert any(op == "cp" and "/_retried/" in dest for op, dest in ops), ops
    assert any(op == "rm" and "/_retry/" in dest for op, dest in ops), ops


# ---------------------------------------------------------------------------
# A failure our own pipeline diagnosed will happen again. Re-running it is a loop,
# not a retry — and since a crash writes no scorecard, resume would re-attempt it
# on EVERY future run of the prefix.
# ---------------------------------------------------------------------------

STAGE1_NO_STRUCTURE = (
    "RuntimeError: stage 1 (pdf2mdtree.py) failed: No structure found: no bookmark outline, no "
    "heading-sized text, and no BOLD CAPITALISED lines to fall back on. text in first 12 page(s): "
    "49167 characters -> there IS a text layer, so this is a heading-DETECTION gap, not a scanned "
    "document."
)


@pytest.mark.parametrize("text,cause", [
    (STAGE1_NO_STRUCTURE, "no_structure"),
    ("IndexError: list index out of range", "pipeline_bug"),
    ("KeyError: 'headings'", "pipeline_bug"),
    ("fitz.FileDataError: cannot open broken document", "bad_source"),
])
def test_a_diagnosed_failure_is_permanent(text, cause):
    from aosphere_core_index.service.extraction_monitor import classify_failure, is_permanent
    assert classify_failure(text) == cause
    assert is_permanent(cause) is True


@pytest.mark.parametrize("text", [
    "torch.OutOfMemoryError: CUDA out of memory",
    "Killed\nexit status 137",
    "ReadTimeoutError: Connection reset by peer",
    "RuntimeError: Failed to find C compiler",
    "DataLoader worker killed by signal: Bus error",
    "something nobody has seen before",
])
def test_an_environmental_failure_stays_retryable(text):
    """Wrongly calling something permanent silently drops a document, so the default leans the
    other way: anything unrecognised is worth one more attempt."""
    from aosphere_core_index.service.extraction_monitor import classify_failure, is_permanent
    assert is_permanent(classify_failure(text)) is False


def test_the_worker_and_the_service_agree_on_permanence():
    """The worker decides whether to record a marker; the screen labels the row. Two rules would
    eventually disagree about the same failure."""
    cause, permanent = W.classify(STAGE1_NO_STRUCTURE)
    assert (cause, permanent) == ("no_structure", True)
    cause, permanent = W.classify("CUDA out of memory")
    assert (cause, permanent) == ("gpu_oom", False)


def test_a_permanent_failure_is_recorded_where_resume_can_see_it(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(W, "_s3", lambda *a, **k: calls.append(a) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    W.mark_permanent("s3://b/corpus/run1", "155_DP", "Ghana__9", "no_structure",
                     STAGE1_NO_STRUCTURE, tmp_path, None)
    cp = next(c for c in calls if c[0] == "cp")
    assert "/_permanent/" in cp[2]
    body = json.loads(Path(cp[1]).read_text())
    assert body["cause"] == "no_structure" and body["label"] == "Ghana__9"
    assert "heading-DETECTION gap" in body["error"], "keep the diagnosis with the marker"


def test_previously_permanent_documents_are_skipped(monkeypatch):
    """The point of the marker: the next run does not spend a GPU slot on it again."""
    rec = {"product": "155_DP", "label": "Ghana__9", "cause": "no_structure"}

    def fake_s3(*args, profile=None):
        if args[0] == "ls" and "_permanent/" in args[1]:
            return types.SimpleNamespace(returncode=0, stdout=_ls(["p.json"]), stderr="")
        if args[0] == "cp" and args[2] == "-":
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(rec), stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(W, "_s3", fake_s3)
    assert W.permanent_labels("s3://b/corpus/run1", None) == {"155_DP/Ghana__9"}
