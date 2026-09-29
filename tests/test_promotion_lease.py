"""One promotion per target version, enforced by an object.

Two writers merging into one `index/<v>/doc-gallery/manifest.json` is the
455-documents-became-5 incident in its concurrent form — and that one was a SINGLE process
getting the merge wrong. `promotion_id == version` is what makes this enforceable at all:
"a second concurrent promotion of this version" and "the same promotion" become the same
thing, which the lease can then refuse. A deliberate second attempt mints `<run>-p2`.

Released by WRITING `released_at`, never by deleting. The promotion role has no DeleteObject
anywhere, on purpose, so "free" has to be a state of the object rather than its absence —
and `lease_state` has to agree with the writer about that.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_run as PR  # noqa: E402
from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402

KEY = PM.lease_key("corpus/r", "r-p1")


class Boom(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Honours IfNoneMatch, which is what makes acquisition a real compare-and-swap."""

    def __init__(self, existing=None):
        self.objects = {}
        if existing is not None:
            self.objects[KEY] = json.dumps(existing).encode()
        self.puts = []

    def put_object(self, Bucket, Key, Body, ContentType=None, **cond):   # noqa: N803
        if "IfNoneMatch" in cond and Key in self.objects:
            raise Boom("PreconditionFailed")
        self.puts.append(Key)
        self.objects[Key] = Body
        return {"ETag": '"e"'}

    def get_object(self, Bucket, Key):                       # noqa: N803
        if Key not in self.objects:
            raise Boom("NoSuchKey")
        return {"Body": type("B", (), {"read": lambda _s, b=self.objects[Key]: b})(),
                "ETag": '"e"'}


def held(age=5.0, host="pod-a"):
    now = time.time()
    return {"holder": f"{host}:1", "host": host, "pid": 1, "acquired_at": now - age,
            "heartbeat_at": now - age, "ttl_s": PM.LEASE_TTL_S}


# ---------------- acquisition ----------------
def test_a_free_lease_is_acquired_with_a_conditional_put():
    s3 = FakeS3()
    ok, why = PR.Lease(s3, "bkt", KEY).acquire()
    assert ok and why == ""
    assert s3.puts == [KEY]


def test_a_second_promoter_is_refused_and_told_who_holds_it():
    s3 = FakeS3(held(host="pod-a"))
    ok, why = PR.Lease(s3, "bkt", KEY).acquire()
    assert not ok
    assert "pod-a" in why
    assert s3.puts == [], "a refused promoter must write nothing at all"


def test_an_abandoned_lease_is_not_silently_stolen():
    """A stale heartbeat is evidence, not permission: the holder may be paused, not dead."""
    s3 = FakeS3(held(age=PM.LEASE_TTL_S + 60))
    ok, why = PR.Lease(s3, "bkt", KEY).acquire()
    assert not ok and "--force-lease" in why
    assert s3.puts == []


def test_an_abandoned_lease_can_be_taken_over_deliberately_and_it_is_recorded():
    s3 = FakeS3(held(age=PM.LEASE_TTL_S + 60, host="dead-pod"))
    ok, why = PR.Lease(s3, "bkt", KEY).acquire(force=True)
    assert ok and "dead-pod" in why
    rec = json.loads(s3.objects[KEY])
    assert rec["broken_from"] == "dead-pod" and rec["broken_at"] > 0, "must be auditable"


def test_forcing_does_not_let_you_steal_a_LIVE_lease():
    s3 = FakeS3(held(age=5.0))
    ok, why = PR.Lease(s3, "bkt", KEY).acquire(force=True)
    assert not ok and "held by" in why


def test_a_released_lease_is_free_immediately_without_waiting_out_the_ttl():
    s3 = FakeS3()
    first = PR.Lease(s3, "bkt", KEY)
    assert first.acquire()[0]
    first.release()
    ok, _ = PR.Lease(s3, "bkt", KEY).acquire()
    assert ok, "release must not require the next promoter to wait out the TTL"


# ---------------- the monitor's reading of it ----------------
def read_state(s3, now=None):
    import aosphere_core_index.service.promotion_monitor as M

    class _RO:
        def __init__(self, bucket=None, region=None):
            self.bucket = bucket

        def get_bytes(self, key):
            return s3.objects[key]

    orig = M.ReadOnlyS3
    M.ReadOnlyS3 = _RO
    try:
        return M.lease_state("bkt", "corpus", "eu", "r", "r-p1", now)
    finally:
        M.ReadOnlyS3 = orig


def test_the_monitor_and_the_writer_agree_on_held():
    s3 = FakeS3()
    PR.Lease(s3, "bkt", KEY).acquire()
    assert read_state(s3)["state"] == "held"


def test_the_monitor_reads_a_released_lease_as_free_not_as_held():
    """The writer releases by WRITING released_at — there is no DeleteObject. If the reader
    did not know that, a released lease would read `held` for the whole TTL."""
    s3 = FakeS3()
    lease = PR.Lease(s3, "bkt", KEY)
    lease.acquire()
    lease.release()
    assert read_state(s3)["state"] == "free"


def test_the_monitor_reads_a_stale_heartbeat_as_expired():
    s3 = FakeS3(held(age=PM.LEASE_TTL_S + 60))
    st = read_state(s3)
    assert st["state"] == "expired" and st["age_seconds"] > PM.LEASE_TTL_S


def test_no_lease_object_reads_as_nobody_holding_one():
    assert read_state(FakeS3()) is None


# ---------------- heartbeat ----------------
def test_the_heartbeat_is_throttled_so_it_does_not_become_a_write_per_document():
    s3 = FakeS3()
    lease = PR.Lease(s3, "bkt", KEY)
    lease.acquire()
    n = len(s3.puts)
    for _ in range(50):
        lease.beat(every_s=3600)
    assert len(s3.puts) == n, "50 documents must not be 50 lease writes"
    lease.beat(every_s=0)
    assert len(s3.puts) == n + 1


def test_a_failed_heartbeat_does_not_kill_the_promotion():
    class Flaky(FakeS3):
        def put_object(self, **kw):
            if self.puts:
                raise Boom("SlowDown")
            return super().put_object(**kw)

    s3 = Flaky()
    lease = PR.Lease(s3, "bkt", KEY)
    assert lease.acquire()[0]
    lease.beat(every_s=0)                 # must not raise
    assert lease.held


def test_a_promotion_id_IS_its_version_so_two_of_them_collide_by_construction():
    assert PM.lease_key("corpus/r", "r-p1") != PM.lease_key("corpus/r", "r-p2")
    assert PM.promotion_target("r-p1") != PM.promotion_target("r-p2")
    assert PM.lease_key("corpus/r", "r-p1").startswith(
        PM.promotion_root("corpus/r", "r-p1"))
