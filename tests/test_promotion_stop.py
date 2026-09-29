"""Stopping a promotion must not publish or cut over a half-built index.

The stop flag is a marker object in S3, polled by the stages. `stage_content`'s loop honours
it by breaking — and for a long time that was ALL it did. The caller could not tell "stopped
at 195 of 322" from "converted all 322": both return an empty `failed` list, so content was
marked complete and the run walked on to embed, publish, the gate, and CUTOVER, with an index
in which most regions still described the PREVIOUS extraction.

The verify gate does not save you here, which is the part worth remembering. Seeding has
already copied every incumbent region into the data dir, so the new version really is a
superset of the live one and `index_verify`'s assertion holds. The index is not missing
regions; it is silently carrying stale ones. Nothing downstream can see the difference.

So a stop has to fail the stage, not finish it: `finished_at` stays unset, `done("content")`
stays false, and a re-run resumes content instead of publishing what it has.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_index as PI  # noqa: E402


def row(doc_id, jur, product="Data Privacy", product_dir="155_Data_Privacy"):
    return {"job": f"{product_dir}/{jur}__{doc_id}", "doc_id": doc_id, "product": product,
            "product_dir": product_dir, "jurisdiction": jur, "gate": "pass",
            "worst_score": 95.0, "slug": f"s-{doc_id}"}


class StopAfter:
    """A progress object whose stop flag trips after N documents."""

    def __init__(self, after):
        self.after, self.seen = after, 0
        self.d = {"stages": {}}
        self.failed = None
        self.finished = []

    def stopping(self):
        self.seen += 1
        return self.seen > self.after

    def bump(self, *a, **k):
        pass

    def publish(self, *a, **k):
        pass

    def _ledger_row(self, r):
        pass

    def stage(self, key, **k):
        self.d["stages"].setdefault(key, {})

    def fail(self, key, note):
        self.failed = (key, note)

    def finish(self, key, **k):
        self.finished.append(key)
        self.d["stages"].setdefault(key, {})["finished_at"] = 1.0


def test_a_stop_midway_through_content_is_reported_not_swallowed(monkeypatch):
    # Every document would convert fine; the stop is the only thing that ends the loop.
    monkeypatch.setattr(PI, "_download_tree", lambda *a, **k: Path("/nonexistent"))
    prog = StopAfter(after=2)
    rows = [row(str(i), f"Country{i}") for i in range(6)]
    res = PI.stage_content(prog, None, "b", "corpus/r", rows,
                           Path("/tmp/nope"), Path("/tmp/nope"), None)
    assert res["stopped"] is True
    assert res["documents"] == 6
    assert res["built"] < 6          # it really did stop short


def test_a_content_stage_that_runs_to_the_end_is_not_marked_stopped(monkeypatch):
    monkeypatch.setattr(PI, "_download_tree", lambda *a, **k: None)   # every doc fails
    prog = StopAfter(after=10_000)
    rows = [row(str(i), f"Country{i}") for i in range(3)]
    res = PI.stage_content(prog, None, "b", "corpus/r", rows,
                           Path("/tmp/nope"), Path("/tmp/nope"), None)
    assert res["stopped"] is False
    assert len(res["failed"]) == 3   # failures are a different thing from a stop


def test_the_caller_refuses_to_publish_after_a_stop(monkeypatch):
    """The invariant: a stopped content stage fails the run before embed/flat/publish."""
    called = []
    # Realistic return shapes, so that if the guard is ever removed this test fails on the
    # assertion below — "publish ran" — and not on an incidental KeyError in a stub.
    shapes = {
        "stage_embed": {"regions": 1},
        "stage_flat": {"rows": 10, "regions": 1, "model": "titan", "dim": 4},
        "stage_publish": {"tail": [], "prefix": "index/v-p1"},
        "stage_index_verify": {"ok": True, "checks": []},
        "stage_cutover": {"previous": "incumbent-version"},
        "stage_vectors": {"backend": "opensearch", "index": "aci-vectors", "tail": []},
    }
    for name, shape in shapes.items():
        monkeypatch.setattr(PI, name,
                            lambda *a, _n=name, _s=shape, **k: (called.append(_n), _s)[1])
    monkeypatch.setattr(PI, "stage_seed",
                        lambda *a, **k: {"seeded": 1, "from": "incumbent-version"})
    monkeypatch.setattr(PI, "stage_content", lambda *a, **k: {
        "built": 2, "failed": [], "rewritten": ["Kenya"], "collisions": [],
        "guidance_dropped": 0, "documents": 6, "stopped": True})
    monkeypatch.setattr(PI, "_read_pointer", lambda *a, **k: "incumbent-version")

    class Cfg:
        data_dir = "/tmp/promo-data"
        trees_dir = "/tmp/promo-trees"
        src_root_local = None
        bucket = "b"
        profile = None
        embed_backend = "titan"
        bedrock_region = "eu-west-1"
        vector_backend = "opensearch"
        vector_index = "aci-vectors"
        opensearch_url = "http://localhost:9200"
        allow_content_failures = False
        seed_from = None
        set_latest = True
        reload_vectors = True

    prog = StopAfter(after=10_000)
    rc = PI.run_index_stages(prog, None, None, Cfg(), [row("1", "Kenya")],
                             "2026-08-21-02", "corpus/2026-08-21-02", "v-p1")

    assert rc == 1, "a stopped content stage must fail the run"
    assert called == [], f"nothing after content may run, but {called} did"
    assert prog.failed is not None and prog.failed[0] == "content"
    # finished_at unset -> done("content") is false -> a re-run resumes content.
    assert "content" not in prog.finished
    assert "finished_at" not in prog.d["stages"].get("content", {})


# ---------------- the pointer ----------------
"""An unreadable index/latest must not be read as "there is no incumbent".

Same shape of bug as the stop above: two different facts collapsed into one value, and
every downstream check agreeing. `_read_pointer` returned None both when index/latest was
absent and when S3 could not be asked at all — an expired SSO token, a network blip, a
missing permission. None then means "first index version", so seed copies nothing, the
gate WAIVES its superset check, and cutover points index/latest at a version holding only
the promoted regions. Every jurisdiction outside the promotion vanishes from search, and
the run reports success.
"""


class Boom(Exception):
    """A read failure that is NOT a 404 — an expired token looks like this."""

    response = {"Error": {"Code": "ExpiredToken"},
                "ResponseMetadata": {"HTTPStatusCode": 400}}


class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"},
                "ResponseMetadata": {"HTTPStatusCode": 404}}


class PointerS3:
    def __init__(self, exc=None, body=b"live-version"):
        self.exc, self.body = exc, body

    def get_object(self, Bucket, Key):                       # noqa: N803
        if self.exc:
            raise self.exc
        return {"Body": type("B", (), {"read": lambda _s: self.body})()}


def test_a_readable_pointer_is_returned():
    assert PI._read_pointer(PointerS3(), "b") == "live-version"


def test_a_genuinely_absent_pointer_is_None_so_a_first_index_still_works():
    assert PI._read_pointer(PointerS3(exc=Missing()), "b") is None


def test_an_unreadable_pointer_RAISES_rather_than_looking_like_no_incumbent():
    with pytest.raises(RuntimeError) as e:
        PI._read_pointer(PointerS3(exc=Boom()), "b")
    # The message has to name the consequence, not just the error.
    assert "only the promoted regions" in str(e.value)
    assert "ExpiredToken" in str(e.value)


def test_promote_run_and_promote_index_agree_on_the_pointer():
    """Two callers, one rule — they read the same pointer for the same decision."""
    import promote_run as PR
    assert PR.read_latest(PointerS3(exc=Missing()), "b") is None
    with pytest.raises(RuntimeError):
        PR.read_latest(PointerS3(exc=Boom()), "b")
