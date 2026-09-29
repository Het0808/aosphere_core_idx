"""--seed-gallery-from-latest — carrying the Doc Gallery forward across a product-scoped run.

Phase 2's `seed` stage already protects the SEARCH index: an unseeded build produces a
`multi.npz` containing only the promoted regions, and cutting over to it deletes every other
jurisdiction from search, so `index_verify` asserts the new version is a superset of the live
one. The Doc Gallery had no equivalent. A promotion mints a fresh `doc-gallery/` prefix and
writes rows only for what it promotes, so a run over one product (or one that only found a
handful of eligible documents) publishes a manifest containing ONLY those rows -- and cutting
over drops every other product from the gallery, even though search still answers for it
because that half IS seeded. Confirmed against a real bucket: the live manifest had 452
entries across three products, and a run containing only one product's job dirs would have
produced a 194-entry manifest with the rest gone.

`--seed-gallery-from-latest` closes it: read the live manifest, carry forward every entry this
run's OWN plan is not about to overwrite, copy its artefacts into the new prefix, and merge it
into the same commit as the freshly promoted entries.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_run as PR  # noqa: E402
from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402

RUN = "2026-08-21-02"
PREFIX = f"corpus/{RUN}"
VERSION = f"{RUN}-p1"
TARGET = f"index/{VERSION}/doc-gallery"
PREV_VERSION = "2026-08-13-titan-hybrid"
PREV_TARGET = f"index/{PREV_VERSION}/doc-gallery"

SC = json.dumps({"gate": "pass", "worst_score": 93.0,
                 "pages": {"total": 148, "states": {"3": "unvalidatable"}, "counts": {}},
                 "stage3": {"tables_total": 12, "tables_filled": 11, "tables_failed": 1},
                 "tables": []}).encode()
SC_REVIEW = SC.replace(b'"gate": "pass"', b'"gate": "review"')

DOC_META = json.dumps([{"FILENAME": "172575", "DOCID": 172575,
                        "DOCNAME": "Data Privacy Survey dated 30th September, 2025",
                        "VERSION": 15, "SOURCEDATE": "March, 30 2025"}]).encode()


def _incumbent_entry(product, region, slug, doc_id):
    return {
        "product": product, "region": f"{region} (MinerU)", "slug": slug, "doc_id": doc_id,
        "status": "ok", "gate": "pass", "worst_score": 91.0, "stats": {},
        "viewer": f"{product}/{region} (MinerU)/{slug}-viewer.html",
        "scorecard": f"{product}/{region} (MinerU)/{slug}-scorecard.json",
        "inspect": f"{product}/{region} (MinerU)/{slug}-inspect.html",
        "source": {"run": "2026-08-13", "job": "x", "promotion": PREV_VERSION},
    }


def base_objects():
    """One promotable Data Privacy region for THIS run, plus an incumbent gallery that also
    holds Shareholding Disclosure (a product this run never touches) and a SECOND Data
    Privacy region (`Italy`) this run's plan does not redo -- the two cases that must both be
    carried forward, and the one (`Spain`, redone this run) that must NOT be duplicated."""
    o = {"index/latest": PREV_VERSION.encode(),
         "corpus-src/155_Data_Privacy/Spain/Doc_metadata.json": DOC_META}
    for label, sc in (("Spain__172575", SC), ("France__172576", SC_REVIEW)):
        d = f"{PREFIX}/155_Data_Privacy/{label}"
        o[f"{d}/scorecard.json"] = sc
        o[f"{d}/viewer.html"] = b"<html>viewer</html>"
        o[f"{d}/inspect.html"] = b"<html>inspect</html>"
        o[f"{d}/source.pdf"] = b"%PDF"

    incumbent = [
        _incumbent_entry("Shareholding Disclosure", "Germany",
                         "shareholding-disclosure-germany-9001-mineru", "9001"),
        _incumbent_entry("Data Privacy", "Italy", "data-privacy-italy-9002-mineru", "9002"),
        # Same product AND the same slug this run is about to redo -- must be replaced, not
        # duplicated. gslug's shape is product-jurisdiction-doc_id-mineru; Spain's doc id
        # matches the run's own so the slugs collide exactly.
        _incumbent_entry("Data Privacy", "Spain", "data-privacy-spain-172575-mineru", "OLD"),
    ]
    o[f"{PREV_TARGET}/manifest.json"] = json.dumps(incumbent).encode()
    for e in incumbent:
        for f in ("viewer", "scorecard", "inspect"):
            o[f"{PREV_TARGET}/{e[f]}"] = f"<html>{e['slug']}-{f}</html>".encode()
    return o


class Boom(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.puts: list[str] = []
        self.copies: list[tuple[str, str]] = []
        self.etags: dict[str, str] = {}

    def _etag(self, key):
        return self.etags.setdefault(key, f'"{len(self.etags)}"')

    def get_paginator(self, _op):
        outer = self

        class _P:
            @staticmethod
            def paginate(Bucket, Prefix, Delimiter=None, **kw):   # noqa: N803
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                if Delimiter is None:
                    yield {"Contents": [{"Key": k, "Size": len(outer.objects[k])}
                                        for k in keys]}
                    return
                contents, prefixes = [], set()
                for k in keys:
                    rest = k[len(Prefix):]
                    if Delimiter in rest:
                        prefixes.add(Prefix + rest.split(Delimiter, 1)[0] + Delimiter)
                    else:
                        contents.append({"Key": k, "Size": len(outer.objects[k])})
                yield {"Contents": contents,
                       "CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)]}
        return _P()

    def get_object(self, Bucket, Key):                       # noqa: N803
        if Key not in self.objects:
            raise Boom("NoSuchKey")
        return {"Body": type("B", (), {"read": lambda _s, b=self.objects[Key]: b})(),
                "ETag": self._etag(Key)}

    def head_object(self, Bucket, Key):                      # noqa: N803
        if Key not in self.objects:
            raise Boom("404")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None, **cond):   # noqa: N803
        if "IfNoneMatch" in cond and Key in self.objects:
            raise Boom("PreconditionFailed")
        if "IfMatch" in cond and cond["IfMatch"] != self._etag(Key):
            raise Boom("PreconditionFailed")
        self.puts.append(Key)
        self.objects[Key] = Body if isinstance(Body, bytes) else str(Body).encode()
        self.etags[Key] = f'"{len(self.etags) + 1000}"'
        return {"ETag": self.etags[Key]}

    def copy_object(self, Bucket, Key, CopySource, **kw):    # noqa: N803
        src = CopySource["Key"]
        if src not in self.objects:
            raise Boom("NoSuchKey")
        self.copies.append((src, Key))
        self.objects[Key] = self.objects[src]
        return {}


class FakeRO:
    def __init__(self, s3):
        self.s3, self.bucket = s3, "bkt"

    def list_common_prefixes(self, prefix):
        out = set()
        for k in self.s3.objects:
            if k.startswith(prefix):
                rest = k[len(prefix):]
                if "/" in rest:
                    out.add(prefix + rest.split("/", 1)[0] + "/")
        return sorted(out)

    def list_level_objects(self, prefix):
        return [{"key": k, "size": len(v)} for k, v in sorted(self.s3.objects.items())
                if k.startswith(prefix) and "/" not in k[len(prefix):]]

    def list_level_with_prefixes(self, prefix):
        return self.list_level_objects(prefix), self.list_common_prefixes(prefix)

    def list_keys(self, prefix, suffix=None):                # pragma: no cover
        raise AssertionError("never list a run recursively")

    def get_bytes(self, key):
        return self.s3.objects[key]


@pytest.fixture
def run_main(monkeypatch):
    def go(*argv, objects=None):
        s3 = FakeS3(objects if objects is not None else base_objects())
        monkeypatch.setattr(PR, "_client", lambda *a, **k: s3)
        monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3",
                            lambda bucket=None, region=None: FakeRO(s3))
        monkeypatch.setattr(PM, "ReadOnlyS3", lambda bucket=None, region=None: FakeRO(s3))
        rc = PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1", *argv])
        return rc, s3
    return go


def manifest(s3):
    return json.loads(s3.objects[f"{TARGET}/manifest.json"])


def test_without_the_flag_other_products_are_dropped(run_main):
    """The bug this feature exists to fix, pinned as a regression test."""
    rc, s3 = run_main()
    assert rc == 0
    slugs = {d["slug"] for d in manifest(s3)}
    assert "shareholding-disclosure-germany-9001-mineru" not in slugs
    assert "data-privacy-italy-9002-mineru" not in slugs


def test_seed_carries_an_untouched_product_forward(run_main):
    rc, s3 = run_main("--seed-gallery-from-latest")
    assert rc == 0
    e = next(d for d in manifest(s3) if d["slug"] == "shareholding-disclosure-germany-9001-mineru")
    assert e["product"] == "Shareholding Disclosure"
    for rel in (e["viewer"], e["scorecard"], e["inspect"]):
        assert f"{TARGET}/{rel}" in s3.objects


def test_seed_carries_a_same_product_region_this_run_does_not_redo(run_main):
    """A product IN scope can still have a region absent from this run's plan; that region's
    old entry must survive exactly like a product outside scope entirely, because the search
    index (seeded separately) keeps its old content live either way."""
    _, s3 = run_main("--seed-gallery-from-latest")
    slugs = {d["slug"] for d in manifest(s3)}
    assert "data-privacy-italy-9002-mineru" in slugs


def test_seed_does_not_duplicate_a_region_this_run_redoes(run_main):
    _, s3 = run_main("--seed-gallery-from-latest")
    man = manifest(s3)
    spain = [d for d in man if d["slug"] == "data-privacy-spain-172575-mineru"]
    assert len(spain) == 1
    assert spain[0]["doc_id"] == "172575", "the FRESH copy must win, not the carried one"


def test_seed_preserves_the_carried_entrys_own_provenance(run_main):
    """MetadataDirective=COPY: a carried entry did not come from this run, and overwriting
    its source/promotion metadata would misattribute who produced it."""
    _, s3 = run_main("--seed-gallery-from-latest")
    e = next(d for d in manifest(s3) if d["slug"] == "shareholding-disclosure-germany-9001-mineru")
    assert e["source"]["promotion"] == PREV_VERSION


def test_seed_is_still_one_manifest_commit(run_main):
    _, s3 = run_main("--seed-gallery-from-latest")
    assert s3.puts.count(f"{TARGET}/manifest.json") == 1


def test_plan_with_seed_flag_writes_nothing(run_main):
    rc, s3 = run_main("--seed-gallery-from-latest", "--plan")
    assert rc == 0
    assert not s3.puts and not s3.copies


def test_no_incumbent_version_is_not_an_error(run_main):
    objs = base_objects()
    del objs["index/latest"]
    del objs[f"{PREV_TARGET}/manifest.json"]
    rc, s3 = run_main("--seed-gallery-from-latest", objects=objs)
    assert rc == 0
    slugs = {d["slug"] for d in manifest(s3)}
    assert "shareholding-disclosure-germany-9001-mineru" not in slugs


def test_a_promotion_without_the_flag_still_reports_complete(run_main):
    """Regression: `gallery_seed` is a declared phase-1 stage now, so summarize()'s
    all-phase-1-stages-complete check needs a finished row here even when the flag is off."""
    _, s3 = run_main()
    state = json.loads(s3.objects[PM.state_key(PREFIX, VERSION)])
    s = PM.summarize(state, state["updated_at"] + 1)
    assert s["state"] == "complete" and s["percent"] == 100.0
