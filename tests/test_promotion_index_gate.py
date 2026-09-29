"""The gate between publishing an index version and pointing at it.

This stage exists for one failure above all others: **flipping index/latest to a version
that is missing regions the live one has DELETES those jurisdictions from search.** Nothing
errors, nothing logs, the corpus just gets smaller — and the way to get there is ordinary,
because `aci reindex` builds multi.npz from whatever happens to be in the data dir. A
promotion that forgot to seed produces exactly that index.

So the promotion seeds from the incumbent and this asserts the result is a superset. The
other checks are the two silent partial loads (2,000 of 84,197 and 11,000 of 77,939) turned
into arithmetic that can be done BEFORE the cutover rather than discovered after it.

The stage runs while nothing a reader can see has changed, which is why a failure has
nothing to roll back. That ordering is asserted too.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import numpy as np  # noqa: E402

import promote_index as PI  # noqa: E402
from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402

VERSION = "2026-08-21-02-p1"
CURRENT = "2026-08-13-titan-hybrid"
LIVE = ["Spain", "France", "Shareholding Disclosure — Australia"]
NEW = ["Marketing Restrictions - Asset Management — Kenya"]


class Boom(Exception):
    def __init__(self, code="NoSuchKey"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)

    def get_paginator(self, _op):
        outer = self

        class _P:
            @staticmethod
            def paginate(Bucket, Prefix, **kw):              # noqa: N803
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                yield {"Contents": [{"Key": k, "Size": len(outer.objects[k])} for k in keys]}
        return _P()

    def get_object(self, Bucket, Key):                       # noqa: N803
        if Key not in self.objects:
            raise Boom()
        return {"Body": type("B", (), {"read": lambda _s, b=self.objects[Key]: b})()}

    def put_object(self, Bucket, Key, Body, **kw):           # noqa: N803
        self.objects[Key] = Body if isinstance(Body, bytes) else str(Body).encode()
        return {}


class Prog:
    """Just enough of PromotionProgress for the stages under test."""

    def __init__(self):
        self.d = {"stages": {}}

    def bump(self, *a, **k):
        pass

    def publish(self, *a, **k):
        pass

    def stopping(self):
        return False

    def _ledger_row(self, row):
        pass


def build_data_dir(tmp_path: Path, regions, rows=None, version=VERSION, sections=3):
    """A data dir shaped exactly the way settings.region_artifacts resolves it."""
    from aosphere_core_index.regions.region_map import split_region
    rows = len(regions) * 10 if rows is None else rows
    for qreg in regions:
        product, jur = split_region(qreg)
        art = tmp_path / "products" / product / jur / "artifacts"
        art.mkdir(parents=True, exist_ok=True)
        (art / f"{qreg}.content.json").write_text(json.dumps(
            {"sections": [{"key": f"A{i}"} for i in range(sections)]}))
        (art / f"{qreg}.sections.npz").write_bytes(b"npz-not-empty")
    multi = tmp_path / "products" / "_multi"
    multi.mkdir(parents=True, exist_ok=True)
    np.savez(multi / "multi.npz", matrix=np.zeros((rows, 4), dtype="float32"))
    (multi / "manifest.json").write_text(json.dumps({
        "version": version, "model": "titan", "rows": rows, "dim": 4,
        "count": len(regions), "regions": len(regions),
        "jurisdictions": [{"name": r, "region": "Europe"} for r in regions]}))
    return tmp_path


def s3_with_live(regions=LIVE, published=None):
    objs = {
        "index/latest": CURRENT.encode(),
        f"index/{CURRENT}/products/_multi/manifest.json": json.dumps(
            {"jurisdictions": [{"name": r} for r in regions]}).encode(),
    }
    for qreg in (published if published is not None else regions + NEW):
        objs[f"index/{VERSION}/products/x/{qreg}/artifacts/{qreg}.content.json"] = b"{}"
        objs[f"index/{VERSION}/products/x/{qreg}/artifacts/{qreg}.sections.npz"] = b"z"
    return FakeS3(objs)


def verify(tmp_path, regions=None, **kw):
    regions = LIVE + NEW if regions is None else regions
    data = build_data_dir(tmp_path, regions, **{k: v for k, v in kw.items()
                                                if k in ("rows", "version", "sections")})
    s3 = kw.get("s3") or s3_with_live()
    return PI.stage_index_verify(Prog(), s3, "bkt", VERSION, kw.get("current", CURRENT),
                                 data, kw.get("promoted", NEW))


def failed(res):
    return [c["check"] for c in res["checks"] if not c["ok"]]


# ---------------- THE superset check ----------------
def test_a_superset_of_the_live_index_passes(tmp_path):
    res = verify(tmp_path)
    assert res["ok"], failed(res)


def test_an_index_MISSING_A_LIVE_REGION_is_refused(tmp_path):
    """The failure this whole stage exists for. Flipping to this index would delete
    "France" from search with no error anywhere."""
    res = verify(tmp_path, regions=["Spain", "Shareholding Disclosure — Australia"] + NEW)
    assert not res["ok"]
    assert "incumbent regions are all present" in failed(res)
    detail = next(c["detail"] for c in res["checks"]
                  if c["check"] == "incumbent regions are all present")
    assert "France" in detail and "DISAPPEAR" in detail


def test_an_index_containing_ONLY_the_promoted_regions_is_refused(tmp_path):
    """What an unseeded data dir produces — the ordinary way to reach the bad state."""
    res = verify(tmp_path, regions=NEW)
    assert not res["ok"]
    assert "incumbent regions are all present" in failed(res)


def test_an_unreadable_incumbent_manifest_refuses_rather_than_flipping_blind(tmp_path):
    """Not knowing what is live is not the same as nothing being live."""
    s3 = s3_with_live()
    del s3.objects[f"index/{CURRENT}/products/_multi/manifest.json"]
    res = verify(tmp_path, s3=s3)
    assert not res["ok"]
    assert "refusing to flip blind" in next(
        c["detail"] for c in res["checks"]
        if c["check"] == "incumbent regions are all present")


def test_a_first_ever_index_version_has_no_incumbent_to_be_a_superset_of(tmp_path):
    res = verify(tmp_path, regions=NEW, current=None, promoted=NEW)
    assert res["ok"], failed(res)


# ---------------- rows ----------------
def test_a_row_count_that_disagrees_with_the_matrix_is_refused(tmp_path):
    """The check both silent partial loads needed. `count` in the manifest has always been
    the JURISDICTION count, so this question was previously unanswerable."""
    data = build_data_dir(tmp_path, LIVE + NEW, rows=40)
    man = data / "products" / "_multi" / "manifest.json"
    m = json.loads(man.read_text())
    m["rows"] = 999999
    man.write_text(json.dumps(m))
    res = PI.stage_index_verify(Prog(), s3_with_live(), "bkt", VERSION, CURRENT, data, NEW)
    assert not res["ok"] and "rows match multi.npz" in failed(res)


def test_a_manifest_with_no_row_count_is_refused(tmp_path):
    data = build_data_dir(tmp_path, LIVE + NEW)
    man = data / "products" / "_multi" / "manifest.json"
    m = json.loads(man.read_text())
    del m["rows"]
    man.write_text(json.dumps(m))
    res = PI.stage_index_verify(Prog(), s3_with_live(), "bkt", VERSION, CURRENT, data, NEW)
    assert not res["ok"] and "manifest has a row count" in failed(res)


def test_a_manifest_naming_a_different_version_is_refused(tmp_path):
    """ACI_INDEX_VERSION not reaching `aci reindex` would publish a version whose own
    manifest cannot say which version it is — which a pod's readiness check needs."""
    res = verify(tmp_path, version="some-other-version")
    assert not res["ok"] and "manifest names this version" in failed(res)


# ---------------- per-region artifacts ----------------
def test_a_region_with_zero_sections_is_refused(tmp_path):
    """Five regions once published as EMPTY — invisible rather than obviously broken."""
    res = verify(tmp_path, sections=0)
    assert not res["ok"] and "no region has zero sections" in failed(res)


def test_a_region_missing_its_npz_is_refused(tmp_path):
    data = build_data_dir(tmp_path, LIVE + NEW)
    from aosphere_core_index.regions.region_map import split_region
    product, jur = split_region("France")
    (data / "products" / product / jur / "artifacts" / "France.sections.npz").unlink()
    res = PI.stage_index_verify(Prog(), s3_with_live(), "bkt", VERSION, CURRENT, data, NEW)
    assert not res["ok"] and "every region has readable artifacts" in failed(res)


def test_a_promoted_region_that_did_not_make_it_into_the_index_is_refused(tmp_path):
    """Otherwise the gallery offers documents the index cannot answer about."""
    res = verify(tmp_path, regions=LIVE, promoted=NEW)
    assert not res["ok"] and "promoted regions are in the index" in failed(res)


def test_an_underpopulated_published_prefix_is_refused(tmp_path):
    res = verify(tmp_path, s3=s3_with_live(published=[]))
    assert not res["ok"] and "published object count is plausible" in failed(res)


# ---------------- ordering ----------------
def test_the_gate_sits_between_publish_and_cutover():
    """So a failure has nothing to roll back. If these ever reorder, the stage stops being
    a gate and becomes a report."""
    keys = [m["key"] for m in PM.STAGES]
    assert keys.index("publish") < keys.index("index_verify") < keys.index("cutover")
    assert keys.index("cutover") < keys.index("vectors")


def test_seeding_comes_before_content_because_content_overwrites_into_it():
    keys = [m["key"] for m in PM.STAGES]
    assert keys.index("seed") < keys.index("content") < keys.index("embed")


def test_the_cutover_writes_exactly_one_object(tmp_path):
    s3 = s3_with_live()
    before = set(s3.objects)
    res = PI.stage_cutover(Prog(), s3, "bkt", VERSION, CURRENT)
    assert set(s3.objects) - before == set(), "no new keys"
    assert s3.objects["index/latest"] == VERSION.encode()
    assert res == {"pointer": "index/latest", "was": CURRENT, "now": VERSION}
