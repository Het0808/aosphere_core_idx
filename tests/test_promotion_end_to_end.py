"""One whole promotion, driven through main(), against a fake S3.

The unit tests each pin one rule. This one asserts they compose: that a real invocation walks
the run, copies the right keys to the right places, commits ONE manifest at the end, verifies
it, and leaves a state object the monitor can render — and that `--plan` and `--dry-run` write
nothing at all.

It also pins the two things that only show up end to end:

  * the manifest entry a promotion writes must be shaped like the one push_hybrid_s3 writes,
    or the gallery renders a promoted document differently from a published one;
  * the promotion must NOT touch index/latest. Phase 1 going live by accident is the failure
    that would matter most, and it is one line away at all times.
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

SC = json.dumps({"gate": "pass", "worst_score": 93.0,
                 "pages": {"total": 148, "states": {"3": "unvalidatable"}, "counts": {}},
                 "stage3": {"tables_total": 12, "tables_filled": 11, "tables_failed": 1},
                 "tables": []}).encode()
SC_REVIEW = SC.replace(b'"gate": "pass"', b'"gate": "review"')
SC_FAIL = SC.replace(b'"gate": "pass"', b'"gate": "fail"')

DOC_META = json.dumps([{"FILENAME": "172575", "DOCID": 172575,
                        "DOCNAME": "Data Privacy Survey dated 30th September, 2025",
                        "VERSION": 15, "SOURCEDATE": "March, 30 2025"}]).encode()


def base_objects():
    o = {
        "index/latest": b"2026-08-13-titan-hybrid",
        "corpus-src/155_Data_Privacy/Spain/Doc_metadata.json": DOC_META,
    }
    for label, sc in (("Spain__172575", SC), ("France__172576", SC_REVIEW),
                      ("Chad__172577", SC_FAIL)):
        d = f"{PREFIX}/155_Data_Privacy/{label}"
        o[f"{d}/scorecard.json"] = sc
        o[f"{d}/viewer.html"] = b"<html>viewer</html>"
        o[f"{d}/inspect.html"] = b"<html>inspect</html>"
        o[f"{d}/source.pdf"] = b"%PDF"
        o[f"{d}/03_stage3_final/index.md"] = b"# never listed"
    # An excluded product, to prove the walk skips it entirely.
    o[f"{PREFIX}/125_G20/Brazil__9003/scorecard.json"] = SC
    o[f"{PREFIX}/125_G20/Brazil__9003/viewer.html"] = b"x"
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
    """The read-only client promotion_monitor and load_names use."""

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
        """`list_level_objects` + `list_common_prefixes`, from one listing — what
        walk_run_jobs now asks for so it can see a nested 04_stage4_ai/ directory
        without a second LIST call."""
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


# ---------------- the whole thing ----------------
def test_a_promotion_copies_publishes_and_verifies(run_main):
    rc, s3 = run_main()
    assert rc == 0
    man = manifest(s3)
    assert len(man) == 2, "pass + review; the fail is held back"
    assert {d["doc_id"] for d in man} == {"172575", "172576"}
    # Three artefacts each, copied server-side.
    assert len(s3.copies) == 6
    for src, dst in s3.copies:
        assert src.startswith(f"{PREFIX}/155_Data_Privacy/")
        assert dst.startswith(f"{TARGET}/Data Privacy/")


def test_nothing_from_an_excluded_product_is_copied(run_main):
    _, s3 = run_main()
    assert not any("125_G20" in src for src, _ in s3.copies)
    assert not any("G20" in json.dumps(d) for d in manifest(s3))


def test_the_source_tree_and_pdf_are_never_read_or_copied(run_main):
    _, s3 = run_main()
    for src, _ in s3.copies:
        assert "03_stage3_final" not in src and "source.pdf" not in src


def test_index_latest_is_NOT_touched(run_main):
    """The failure that would matter most. Phase 1 must stage, never go live."""
    _, s3 = run_main()
    assert "index/latest" not in s3.puts
    assert s3.objects["index/latest"] == b"2026-08-13-titan-hybrid"


def test_the_manifest_is_written_exactly_once(run_main):
    _, s3 = run_main()
    assert s3.puts.count(f"{TARGET}/manifest.json") == 1


def test_promoting_into_the_live_version_is_refused(run_main):
    objs = base_objects()
    objs["index/latest"] = VERSION.encode()
    rc, s3 = run_main(objects=objs)
    assert rc == 2
    assert not s3.copies and not s3.puts


def test_merge_into_live_is_the_deliberate_escape_hatch(run_main):
    objs = base_objects()
    objs["index/latest"] = VERSION.encode()
    rc, s3 = run_main("--merge-into-live", objects=objs)
    assert rc == 0 and len(manifest(s3)) == 2


# ---------------- entry shape ----------------
def test_an_entry_is_shaped_like_the_publishers(run_main):
    """A promoted document must render identically to a push_hybrid_s3-published one."""
    _, s3 = run_main()
    e = next(d for d in manifest(s3) if d["doc_id"] == "172575")
    assert e["product"] == "Data Privacy"
    assert e["region"] == "Spain (MinerU)", "the display suffix is load-bearing"
    assert e["slug"] == "data-privacy-spain-172575-mineru"
    assert e["status"] == "ok" and e["gate"] == "pass" and e["worst_score"] == 93.0
    assert e["viewer"] == "Data Privacy/Spain (MinerU)/data-privacy-spain-172575-mineru-viewer.html"
    assert e["scorecard"].endswith("-scorecard.json")
    assert e["inspect"].endswith("-inspect.html")
    assert e["stats"]["pages"] == 148 and e["stats"]["tables_converted"] == 11
    # The manifest paths must resolve against the target prefix, the way S3DocStore._key does.
    for rel in (e["viewer"], e["scorecard"], e["inspect"]):
        assert f"{TARGET}/{rel}" in s3.objects


def test_a_promoted_entry_says_which_extraction_it_came_from(run_main):
    """Unanswerable from a published gallery today: a manifest carries no run id at all."""
    _, s3 = run_main()
    e = manifest(s3)[0]
    assert e["source"]["run"] == RUN and e["source"]["promotion"] == VERSION
    assert e["pdf"].startswith(f"{PREFIX}/") and e["pdf"].endswith("source.pdf")
    prov = json.loads(s3.objects[PM.provenance_key(VERSION)])
    assert prov["run"] == RUN and prov["documents"] == 2


def test_document_names_are_recovered_from_the_source_corpus(run_main):
    """Otherwise the row reads "172575" where a published row reads the document's title."""
    _, s3 = run_main()
    e = next(d for d in manifest(s3) if d["doc_id"] == "172575")
    assert e["doc_name"] == "Data Privacy Survey dated 30th September, 2025"
    assert e["doc_version"] == 15


# ---------------- stage 4/5 (post-AI) ----------------

def test_a_document_that_ran_stage4_publishes_its_post_ai_scorecard(run_main):
    """scorecard.json is frozen at stage 3; when stage 4/5 ran, scorecard_post_ai.json is
    what the document now looks like, and the published gallery must ship (and gate on)
    THAT tree — otherwise a document the AI pass degraded still shows its stale pre-AI
    pass, and reviewers can never see stage 4/5 in the gallery at all, only on the
    pipeline monitor's per-job page."""
    objs = base_objects()
    d = f"{PREFIX}/155_Data_Privacy/Spain__172575"
    objs[f"{d}/scorecard_post_ai.json"] = json.dumps({
        "gate": "review", "worst_score": 70.0,
        "pages": {"total": 148, "states": {"3": "unvalidatable"}, "counts": {}},
        "stage3": {"tables_total": 12, "tables_filled": 11, "tables_failed": 1},
        "tables": []}).encode()
    rc, s3 = run_main(objects=objs)
    assert rc == 0

    man = manifest(s3)
    assert len(man) == 2, "still pass + review under the default gates"
    e = next(x for x in man if x["doc_id"] == "172575")
    assert e["gate"] == "review" and e["worst_score"] == 70.0, \
        "the manifest must carry the post-AI verdict, not the frozen stage-3 pass"
    assert e["scorecard"].endswith("-scorecard_post_ai.json")
    assert f"{TARGET}/{e['scorecard']}" in s3.objects
    # scorecard.json is still copied too -- it is the completion marker, and nothing here
    # should stop publishing it -- but the manifest points reviewers at the current tree.
    assert f"{TARGET}/Data Privacy/Spain (MinerU)/{e['slug']}-scorecard.json" in s3.objects


def test_a_document_with_no_post_ai_scorecard_publishes_unaffected(run_main):
    """The majority of documents never ran stage 4/5 — no extra copy, no manifest change."""
    rc, s3 = run_main()
    assert rc == 0
    assert not any(src.endswith("scorecard_post_ai.json") for src, _dst in s3.copies)
    e = next(d for d in manifest(s3) if d["doc_id"] == "172575")
    assert e["scorecard"].endswith("-scorecard.json")
    assert e["gate"] == "pass"


def test_a_missing_doc_metadata_does_not_fail_the_promotion(run_main):
    objs = {k: v for k, v in base_objects().items() if "Doc_metadata" not in k}
    rc, s3 = run_main(objects=objs)
    assert rc == 0
    assert all("doc_name" not in d for d in manifest(s3))


# ---------------- progress ----------------
def test_the_state_object_renders_as_a_finished_promotion(run_main):
    _, s3 = run_main()
    state = json.loads(s3.objects[PM.state_key(PREFIX, VERSION)])
    s = PM.summarize(state, state["updated_at"] + 1)
    assert s["state"] == "complete" and s["percent"] == 100.0
    assert s["counts"]["copied"] == 2 and s["counts"]["failed"] == 0
    assert s["gates"] == {"pass": 1, "review": 1}
    assert {r["state"] for r in s["stages"] if r["phase"] == 1} == {"complete"}
    assert {r["state"] for r in s["stages"] if r["phase"] == 2} == {"not_implemented"}


def test_the_plan_is_recorded_so_the_screen_can_show_what_was_held_back(run_main):
    _, s3 = run_main()
    plan = json.loads(s3.objects[PM.plan_key(PREFIX, VERSION)])
    e = PM.eligibility(plan)
    assert e["eligible"] == 2 and e["excluded"] == {"skip/gate": 1}


# ---------------- resume ----------------
def test_a_second_run_skips_what_is_already_published(run_main):
    _, s3 = run_main()
    copies_after_first = len(s3.copies)
    PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1"])
    # main() built a NEW client via the patched factory, so the same fake was reused.
    assert len(s3.copies) == copies_after_first, "already-published documents were re-copied"
    state = json.loads(s3.objects[PM.state_key(PREFIX, VERSION)])
    assert state["restarts"] == 1 and state["counts"]["skipped"] == 2


def test_force_re_copies(run_main):
    _, s3 = run_main()
    n = len(s3.copies)
    PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1", "--force"])
    assert len(s3.copies) == n + 6


# ---------------- the read-only modes ----------------
def test_plan_writes_absolutely_nothing(run_main):
    rc, s3 = run_main("--plan")
    assert rc == 0
    assert not s3.puts and not s3.copies


def test_dry_run_proves_the_read_path_without_writing(run_main):
    rc, s3 = run_main("--dry-run")
    assert rc == 0
    assert not s3.copies
    assert f"{TARGET}/manifest.json" not in s3.objects


def test_probe_write_fails_fast_when_the_target_is_not_writable(run_main, monkeypatch):
    """--plan never calls PutObject, so a green dry run says nothing about the IAM."""
    class Denied(FakeS3):
        def put_object(self, Bucket, Key, Body, ContentType=None, **cond):   # noqa: N803
            if Key.startswith("index/"):
                raise Boom("AccessDenied")
            return super().put_object(Bucket, Key, Body, ContentType, **cond)

    s3 = Denied(base_objects())
    monkeypatch.setattr(PR, "_client", lambda *a, **k: s3)
    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3",
                        lambda bucket=None, region=None: FakeRO(s3))
    monkeypatch.setattr(PM, "ReadOnlyS3", lambda bucket=None, region=None: FakeRO(s3))
    with pytest.raises(Boom):
        PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1", "--probe-write"])
    assert not s3.copies, "the copy loop must not have started"


# ---------------- limits and refusals ----------------
def test_limit_promotes_a_subset_and_resume_finishes_the_rest(run_main):
    _, s3 = run_main("--limit", "1")
    assert len(manifest(s3)) == 1
    PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1"])
    assert len(manifest(s3)) == 2, "the second pass must MERGE, not replace"


def test_an_unknown_product_is_refused_before_any_work(run_main):
    rc, s3 = run_main("--products", "G20")
    assert rc == 2 and not s3.copies


def test_an_unknown_gate_is_refused(run_main):
    rc, _ = run_main("--gate", "brilliant")
    assert rc == 2


def test_an_illegal_version_is_refused_before_any_work(run_main):
    rc, s3 = run_main("--version", "Not A Version")
    assert rc == 2 and not s3.copies


def test_one_failing_document_does_not_sink_the_promotion(monkeypatch):
    """A document that is fine at plan time and fails during the copy — a 403 on one key,
    an object deleted underneath us, a transient error that outlived the retries. It is
    counted, recorded, and the loop carries on; only the good ones get manifest rows."""
    class OneBadCopy(FakeS3):
        def copy_object(self, Bucket, Key, CopySource, **kw):    # noqa: N803
            if "Spain__172575" in CopySource["Key"]:
                raise Boom("AccessDenied")
            return super().copy_object(Bucket, Key, CopySource, **kw)

    s3 = OneBadCopy(base_objects())
    monkeypatch.setattr(PR, "_client", lambda *a, **k: s3)
    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3",
                        lambda bucket=None, region=None: FakeRO(s3))
    monkeypatch.setattr(PM, "ReadOnlyS3", lambda bucket=None, region=None: FakeRO(s3))
    rc = PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1"])

    assert rc == 1, "a non-zero exit reports the failure to the Job"
    man = manifest(s3)
    assert {d["doc_id"] for d in man} == {"172576"}, \
        "a failed copy must not get a manifest row pointing at a missing key"
    fails = json.loads(s3.objects[PM.failures_key(PREFIX, VERSION)])
    assert len(fails) == 1 and "172575" in fails[0]["job"]
    assert "AccessDenied" in fails[0]["error"]
    state = json.loads(s3.objects[PM.state_key(PREFIX, VERSION)])
    assert state["counts"] == {"copied": 1, "skipped": 0, "failed": 1,
                               "objects": 3, "bytes": state["counts"]["bytes"]}
    # The stage still advanced for both documents, so the screen does not stall at 1 of 2.
    assert state["stages"]["copy"]["done"] == 2
    assert state["stages"]["copy"]["failed"] == 1


def test_a_retry_after_a_failure_picks_up_only_what_failed(monkeypatch):
    """The Job's backoffLimit exists for this: the retry resumes from the target listing."""
    class Flaky(FakeS3):
        fail = True

        def copy_object(self, Bucket, Key, CopySource, **kw):    # noqa: N803
            if Flaky.fail and "Spain__172575" in CopySource["Key"]:
                raise Boom("SlowDown")
            return super().copy_object(Bucket, Key, CopySource, **kw)

    s3 = Flaky(base_objects())
    monkeypatch.setattr(PR, "_client", lambda *a, **k: s3)
    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3",
                        lambda bucket=None, region=None: FakeRO(s3))
    monkeypatch.setattr(PM, "ReadOnlyS3", lambda bucket=None, region=None: FakeRO(s3))
    argv = ["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1"]
    assert PR.main(argv) == 1
    Flaky.fail = False
    n = len(s3.copies)
    assert PR.main(argv) == 0
    assert len(s3.copies) == n + 3, "only the failed document is re-copied"
    assert {d["doc_id"] for d in manifest(s3)} == {"172575", "172576"}, \
        "the retry MERGED into the manifest the first pass wrote"


def test_a_drained_promotion_does_not_report_its_copy_stage_as_complete(monkeypatch):
    """Stopping is not finishing. A promotion stopped half-way that reported `complete`
    with 1 of 2 documents copied would be the same class of lie as a dead worker reading
    `running` — so the stage keeps its counters, gains a note, and the derived state says
    `stopping`. What WAS copied is still committed: draining gracefully is the point.
    """
    s3 = FakeS3(base_objects())
    monkeypatch.setattr(PR, "_client", lambda *a, **k: s3)
    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3",
                        lambda bucket=None, region=None: FakeRO(s3))
    monkeypatch.setattr(PM, "ReadOnlyS3", lambda bucket=None, region=None: FakeRO(s3))
    # The stop marker is already there, so the loop breaks before its first document.
    s3.objects[PM.stop_key(PREFIX, VERSION)] = b"{}"

    assert PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1"]) == 0
    state = json.loads(s3.objects[PM.state_key(PREFIX, VERSION)])
    copy = state["stages"]["copy"]
    assert "finished_at" not in copy, "a drained stage must not be marked finished"
    assert copy["done"] < copy["total"]
    assert "stopped early" in copy["note"]

    s = PM.summarize(state, state["updated_at"] + 1)
    assert s["state"] == "stopping"
    assert s["percent"] < 100.0, "a promotion stopped half-way must not read 100%"


def test_what_was_copied_before_a_drain_is_still_committed(monkeypatch):
    """The whole point of a graceful stop: finish the document in flight, commit, exit."""
    s3 = FakeS3(base_objects())
    monkeypatch.setattr(PR, "_client", lambda *a, **k: s3)
    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3",
                        lambda bucket=None, region=None: FakeRO(s3))
    monkeypatch.setattr(PM, "ReadOnlyS3", lambda bucket=None, region=None: FakeRO(s3))

    # Ask for the stop only after the first document has been copied.
    real = PR.PromotionProgress.stopping
    seen = {"n": 0}

    def after_one(self):
        seen["n"] += 1
        self._stop_at = 0.0
        if seen["n"] > 1:
            s3.objects[PM.stop_key(PREFIX, VERSION)] = b"{}"
        return real(self)

    monkeypatch.setattr(PR.PromotionProgress, "stopping", after_one)
    assert PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1"]) == 0
    assert len(manifest(s3)) == 1, "the document copied before the stop is committed"
    # And re-running the same version finishes the rest, merging rather than replacing.
    del s3.objects[PM.stop_key(PREFIX, VERSION)]
    monkeypatch.setattr(PR.PromotionProgress, "stopping", real)
    assert PR.main(["--run", RUN, "--bucket", "bkt", "--region", "eu-west-1"]) == 0
    assert len(manifest(s3)) == 2
