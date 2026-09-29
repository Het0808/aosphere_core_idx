"""A marker read that fails must SAY so, not report "no markers".

This is the bug that cost a day. `retry_labels` and `permanent_labels` each made TWO S3 calls —
`ls` to enumerate the markers and `cp` to read each one's JSON — and swallowed every failure of
either: one `return set()` and three bare `continue`s. Both call sites in main() are guarded on
the returned set being non-empty:

    marked = retry_labels(...)
    if marked:
        remote_done -= marked
        print(f"  {len(marked)} document(s) marked for re-extraction: ...")

so an empty set printed NOTHING. The worker then reported "0 job(s) to extract" and exited
successfully while 132 retry markers sat in S3 — and the log's silence was indistinguishable
from a run with nothing queued.

The two calls need different IAM actions (ListBucket vs GetObject), so `ls` succeeding proves
nothing about `cp`: a listing that works from inside the container while the reads return
nothing is exactly the shape this took.
"""

import json

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_worker as W  # noqa: E402


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


LISTING = (
    "2026-08-24 16:33:04        169 104_Shareholding_Disclosure__Egypt__175174.json\n"
    "2026-08-24 16:33:05        168 104_Shareholding_Disclosure__India__172436.json\n"
)


def _marker(product, label):
    return json.dumps({"product": product, "label": label})


def test_markers_are_read_and_reported(monkeypatch, capsys):
    def fake(*args, profile=None):
        if args[0] == "ls":
            return _Proc(out=LISTING)
        name = args[1].rsplit("/", 1)[-1]
        return _Proc(out=_marker("104_Shareholding_Disclosure",
                                 name[len("104_Shareholding_Disclosure__"):-len(".json")]))
    monkeypatch.setattr(W, "_s3", fake)
    got = W.retry_labels("s3://b/corpus/run", None)
    assert got == {"104_Shareholding_Disclosure/Egypt__175174",
                   "104_Shareholding_Disclosure/India__172436"}
    assert "_retry: 2 marker(s) read" in capsys.readouterr().out


def test_a_denied_object_read_is_reported_not_swallowed(monkeypatch, capsys):
    """The exact failure: `ls` succeeds, every `cp` is denied. Silence here is what made a
    permissions problem look like an empty queue."""
    def fake(*args, profile=None):
        if args[0] == "ls":
            return _Proc(out=LISTING)
        return _Proc(rc=1, err="fatal error: An error occurred (AccessDenied) when calling "
                              "the GetObject operation: Access Denied")
    monkeypatch.setattr(W, "_s3", fake)
    got = W.retry_labels("s3://b/corpus/run", None)
    assert got == set(), "nothing readable, so nothing is returned"
    out = capsys.readouterr().out
    assert "2 marker(s) listed, 0 usable" in out
    assert "2 unreadable" in out
    assert "AccessDenied" in out, "the reason must reach the log"


def test_a_failed_listing_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None: _Proc(
        rc=1, err="An error occurred (AccessDenied) when calling the ListObjectsV2 operation"))
    assert W.permanent_labels("s3://b/corpus/run", None) == set()
    out = capsys.readouterr().out
    assert "could not LIST _permanent" in out and "AccessDenied" in out


def test_an_absent_prefix_stays_quiet(monkeypatch, capsys):
    """An empty _retry/ is the normal case for most runs and must not print a warning."""
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None: _Proc(rc=0, out=""))
    assert W.retry_labels("s3://b/corpus/run", None) == set()
    assert capsys.readouterr().out == ""


def test_a_malformed_marker_is_counted_and_named(monkeypatch, capsys):
    def fake(*args, profile=None):
        if args[0] == "ls":
            return _Proc(out=LISTING)
        return _Proc(out="{not json")
    monkeypatch.setattr(W, "_s3", fake)
    assert W.retry_labels("s3://b/corpus/run", None) == set()
    out = capsys.readouterr().out
    assert "2 unparseable" in out


# ---------------------------------------------------------------------------
# WRITES. Same class of bug, worse consequence: clear_retry's two mutations had
# their return codes discarded entirely, so a denied delete left the marker in
# place and the document was re-extracted on every subsequent run — the exact
# loop the one-retry cap exists to prevent, and nothing said a word.
# ---------------------------------------------------------------------------

def test_a_failed_marker_delete_warns_that_the_document_will_repeat(monkeypatch, capsys, tmp_path):
    def fake(*args, profile=None):
        if args[0] == "rm":
            return _Proc(rc=1, err="An error occurred (AccessDenied) when calling DeleteObject")
        return _Proc()
    monkeypatch.setattr(W, "_s3", fake)
    W.clear_retry("s3://b/corpus/run", "104_Shareholding_Disclosure", "Egypt__175174",
                  tmp_path, None)
    out = capsys.readouterr().out
    assert "drop _retry/" in out and "AccessDenied" in out
    assert "WILL BE RETRIED AGAIN" in out, "an unbounded retry loop must be stated, not implied"


def test_a_failed_retried_record_warns_the_cap_is_blind(monkeypatch, capsys, tmp_path):
    """Marker removed but no _retried record: the document stops looping, but the one-retry
    cap has lost its memory of having tried."""
    def fake(*args, profile=None):
        if args[0] == "cp":
            return _Proc(rc=1, err="An error occurred (AccessDenied) when calling PutObject")
        return _Proc()
    monkeypatch.setattr(W, "_s3", fake)
    W.clear_retry("s3://b/corpus/run", "104_Shareholding_Disclosure", "Egypt__175174",
                  tmp_path, None)
    out = capsys.readouterr().out
    assert "record _retried/" in out and "AccessDenied" in out
    assert "one-retry cap will not see it" in out


def test_a_clean_clear_retry_says_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None: _Proc())
    W.clear_retry("s3://b/corpus/run", "p", "l__1", tmp_path, None)
    assert capsys.readouterr().out == "", "the happy path must stay quiet"


def test_a_failed_permanent_marker_warns_it_will_be_reattempted(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None: _Proc(
        rc=1, err="An error occurred (AccessDenied) when calling PutObject"))
    W.mark_permanent("s3://b/corpus/run", "179_Private_Wealth_-_Tax", "Austria__176029",
                     "no_structure", "No structure found", tmp_path, None)
    out = capsys.readouterr().out
    assert "record _permanent/" in out and "AccessDenied" in out
    assert "re-attempted on every future run" in out
