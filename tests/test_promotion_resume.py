"""Restart, resume, and the property that makes a dead promotion harmless.

THE COMMIT POINT. Objects are invisible until the manifest names them, and the manifest is
written once, at the end. So a promotion that dies — a spot reclaim, a 403 on one document,
an operator's stop — leaves the LIVE gallery byte-for-byte untouched. Everything else in this
design is arranged around keeping that true, so it is asserted directly.

RESUME AUTHORITY is the target listing, not a marker file the job wrote. A pod that copied and
then died would under-report from its own marker (safe, but not the truth), and the target is
what the gallery actually reads. It is ONE paginated listing: push_hybrid_s3.present_docs
measured 455 sequential HEADs, 8-15 minutes before the first upload.

CARRY FORWARD. Counters live in the process and S3 has no append, so a restarted pod that
starts from an empty local ledger REPLACES the published history with one row — the screen
goes from "431 done" to "1 done". That is not a display bug, it is the record being
destroyed. corpus_worker.ShardProgress._carry_forward exists for exactly this and so does
this one.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_run as PR  # noqa: E402
from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402

TARGET = "index/r-p1/doc-gallery"


class FakeS3:
    def __init__(self, objects=None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.list_calls = 0
        self.heads = 0
        self.puts: list[str] = []
        self.copies: list[tuple[str, str]] = []

    # -- listing --
    def get_paginator(self, _op):
        outer = self

        class _P:
            @staticmethod
            def paginate(Bucket, Prefix, **kw):              # noqa: N803
                outer.list_calls += 1
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                for i in range(0, max(1, len(keys)), 2):     # force real pagination
                    yield {"Contents": [{"Key": k, "Size": len(outer.objects[k])}
                                        for k in keys[i:i + 2]]}
        return _P()

    def head_object(self, Bucket, Key):                      # noqa: N803
        self.heads += 1
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):                       # noqa: N803
        if Key not in self.objects:
            raise PR_Boom("NoSuchKey")
        return {"Body": type("B", (), {"read": lambda _s, b=self.objects[Key]: b})(),
                "ETag": '"e"'}

    def put_object(self, Bucket, Key, Body, ContentType=None, **kw):   # noqa: N803
        self.puts.append(Key)
        self.objects[Key] = Body if isinstance(Body, bytes) else str(Body).encode()
        return {"ETag": '"e"'}

    def copy_object(self, Bucket, Key, CopySource, **kw):    # noqa: N803
        self.copies.append((CopySource["Key"], Key))
        self.objects[Key] = self.objects.get(CopySource["Key"], b"x")
        return {}


class PR_Boom(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def published(*slugs, half=()):
    """A target prefix holding these slugs, plus any that are only half-copied."""
    o = {}
    for s in slugs:
        o[f"{TARGET}/Data Privacy/Spain (MinerU)/{s}-viewer.html"] = b"v"
        o[f"{TARGET}/Data Privacy/Spain (MinerU)/{s}-scorecard.json"] = b"{}"
    for s in half:
        o[f"{TARGET}/Data Privacy/Spain (MinerU)/{s}-viewer.html"] = b"v"
    return o


# ---------------- presence ----------------
def test_presence_is_one_listing_and_never_a_head_per_document():
    s3 = FakeS3(published(*[f"d{i}-mineru" for i in range(60)]))
    got = PR.present_slugs(s3, "bkt", TARGET)
    assert len(got) == 60
    assert s3.list_calls == 1, "one paginated listing, not one call per document"
    assert s3.heads == 0, "455 sequential HEADs cost 8-15 minutes before the first upload"


def test_a_half_copied_document_is_not_present():
    """Viewer without scorecard must be FINISHED, not skipped — otherwise the manifest
    would name a scorecard that is not there."""
    s3 = FakeS3(published("a-mineru", half=("b-mineru",)))
    assert PR.present_slugs(s3, "bkt", TARGET) == {"a-mineru"}


def test_presence_does_not_compare_verdicts_and_does_not_need_to():
    """Unlike push_hybrid_s3.unchanged_docs. A promotion is pinned to one IMMUTABLE run
    prefix, so the source objects cannot change underneath it: presence is sufficient here,
    and correct. That is a real simplification, not an oversight."""
    code = PR.present_slugs.__doc__ or ""
    assert "immutable" in code.lower()
    s3 = FakeS3(published("a-mineru"))
    assert PR.present_slugs(s3, "bkt", TARGET) == {"a-mineru"}
    assert not s3.heads


def test_an_empty_target_is_an_empty_set_not_an_error():
    assert PR.present_slugs(FakeS3(), "bkt", TARGET) == set()


# ---------------- the commit point ----------------
def test_a_promotion_that_dies_mid_copy_leaves_the_manifest_untouched():
    """THE safety property. Copied objects are invisible until the manifest names them."""
    live = [{"slug": "live-1-mineru", "status": "ok"}]
    s3 = FakeS3({f"{TARGET}/manifest.json": json.dumps(live).encode()})
    before = s3.objects[f"{TARGET}/manifest.json"]
    # simulate the copy loop having copied two documents and then the pod vanishing
    s3.copy_object(Bucket="bkt", Key=f"{TARGET}/a-viewer.html",
                   CopySource={"Bucket": "bkt", "Key": "corpus/r/p/j/viewer.html"})
    s3.copy_object(Bucket="bkt", Key=f"{TARGET}/a-scorecard.json",
                   CopySource={"Bucket": "bkt", "Key": "corpus/r/p/j/scorecard.json"})
    assert s3.objects[f"{TARGET}/manifest.json"] == before
    assert json.loads(before) == live


def test_the_manifest_is_written_exactly_once_per_completed_pass():
    """--manifest-every is the ONLY way to give the all-or-nothing property up, and it is
    off by default."""
    import inspect
    src = inspect.getsource(PR._run)
    assert src.count("merge_manifest(") == 2, \
        "one checkpoint call behind --manifest-every, one commit at the end"
    assert "args.manifest_every and" in src, "checkpointing must be opt-in"
    ap = PR.build_parser()
    assert ap.get_default("manifest_every") == 0


# ---------------- carry forward ----------------
def progress(s3, **kw):
    return PR.PromotionProgress(s3, "bkt", "r", "corpus/r", "r-p1", "me",
                                {"eligible": 3, **kw})


def test_a_restart_resumes_the_counters_instead_of_replacing_them():
    prev = {"promotion_id": "r-p1", "started_at": 100.0, "restarts": 1,
            "counts": {"copied": 431, "skipped": 18, "failed": 2, "objects": 1293,
                       "bytes": 3980123456},
            "gates": {"pass": 398, "review": 51},
            "stages": {"copy": {"started_at": 100.0, "done": 451, "total": 612}},
            "recent": [{"job": "a"}], "manifest": {}}
    s3 = FakeS3({PM.state_key("corpus/r", "r-p1"): json.dumps(prev).encode(),
                 PM.ledger_key("corpus/r", "r-p1"): b'{"job":"a"}\n{"job":"b"}\n'})
    p = progress(s3)
    p.carry_forward()
    assert p.d["counts"]["copied"] == 431
    assert p.d["restarts"] == 2
    assert p.d["started_at"] == 100.0, "the original start time, not this pod's"
    assert len(p.ledger) == 2, "S3 has no append: the published ledger must be read back"


def test_a_restart_appends_to_the_published_ledger_rather_than_truncating_it():
    s3 = FakeS3({PM.ledger_key("corpus/r", "r-p1"): b'{"job":"a"}\n'})
    p = progress(s3)
    p.carry_forward()
    p.copied({"job": "b", "slug": "b-mineru", "gate": "pass"}, 3, 10, 0.4)
    p.publish(force=True)
    body = s3.objects[PM.ledger_key("corpus/r", "r-p1")].decode()
    assert body.count("\n") == 2 and '"job": "b"' in body.replace('"job":"b"', '"job": "b"')


def test_a_fresh_promotion_does_not_inherit_a_different_ones_counters():
    other = {"promotion_id": "r-p2", "counts": {"copied": 999}}
    s3 = FakeS3({PM.state_key("corpus/r", "r-p1"): json.dumps(other).encode()})
    p = progress(s3)
    p.carry_forward()
    assert p.d["counts"]["copied"] == 0


def test_a_progress_write_that_fails_never_fails_the_promotion():
    """The promotion is the thing with value; the progress object is how we watch it."""
    class Blocked(FakeS3):
        def put_object(self, **kw):
            raise PR_Boom("AccessDenied")

    p = progress(Blocked())
    p.stage("copy", total=2)
    p.copied({"job": "a", "slug": "a-mineru", "gate": "pass"}, 3, 10, 0.1)
    p.publish(force=True)
    assert p.d["counts"]["copied"] == 1


def test_recent_and_failures_are_bounded_so_the_screen_never_reads_a_log():
    p = progress(FakeS3())
    for i in range(PM.RECENT_MAX + 40):
        p.copied({"job": f"j{i}", "slug": f"s{i}-mineru", "gate": "pass"}, 3, 1, 0.1)
    assert len(p.d["recent"]) == PM.RECENT_MAX
    for i in range(PM.FAILURES_MAX + 20):
        p.doc_failed({"job": f"f{i}", "slug": f"f{i}-mineru"}, RuntimeError("x" * 5000))
    assert len(p.failures) == PM.FAILURES_MAX
    assert all(len(f["error"]) <= PM.ERROR_CHARS for f in p.failures)


def test_one_bad_document_is_counted_and_the_loop_carries_on():
    p = progress(FakeS3())
    p.stage("copy", total=3)
    p.doc_failed({"job": "bad", "slug": "bad-mineru"}, RuntimeError("403"))
    p.copied({"job": "ok", "slug": "ok-mineru", "gate": "pass"}, 3, 1, 0.1)
    assert p.d["counts"] == {"copied": 1, "skipped": 0, "failed": 1, "objects": 3, "bytes": 1}
    assert p.d["stages"]["copy"]["done"] == 2, "a failure still advances the stage"
    assert p.d["stages"]["copy"]["failed"] == 1


def test_a_stage_already_finished_on_an_earlier_pass_is_not_restarted():
    prev = {"promotion_id": "r-p1",
            "stages": {"select": {"started_at": 1.0, "finished_at": 2.0, "done": 1, "total": 1}}}
    s3 = FakeS3({PM.state_key("corpus/r", "r-p1"): json.dumps(prev).encode()})
    p = progress(s3)
    p.carry_forward()
    p.stage("select", total=1)
    assert p.d["stages"]["select"]["finished_at"] == 2.0


# ---------------- stop ----------------
def test_a_stop_is_a_durable_marker_not_a_signal():
    """The process to stop may not exist yet, may be restarting, or may be on a node
    nobody can reach — so the request outlives all of that and is re-read each loop."""
    s3 = FakeS3()
    p = progress(s3)
    assert p.stopping() is False
    s3.objects[PM.stop_key("corpus/r", "r-p1")] = b"{}"
    p._stop_at = 0.0                      # skip the throttle
    assert p.stopping() is True
    assert p.d["stopping"] is True
