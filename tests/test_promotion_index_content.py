"""Building content.json for the promoted documents: collisions, and the stale .npz.

TWO DOCUMENTS, ONE REGION. The index keys content by product+jurisdiction, so a jurisdiction
holding a superseded opinion beside the current one means one silently overwriting the other.
This is normal, not exceptional — build_hybrid_index's own comment records "on Marketing
Restrictions - Asset Management that is 17 of 88 jurisdictions". So it is resolved by the
validated rule (`keeper_stems`, the Survey/Memorandum filter) and, whatever resolves it, the
decision is REPORTED rather than swallowed: silently choosing between two legal opinions is
not a thing to do quietly.

THE STALE NPZ. `aci reembed` skips a region whose .npz already reports the target model —
right for a re-embed, exactly wrong here. A promoted region gets a new content.json while
its seeded .npz still says "titan", so reembed would skip it and the region would keep
vectors describing the PREVIOUS extraction. Silent, and the kind of thing found months
later. The content stage therefore deletes the .npz of every region it rewrites, and that
invariant is what these tests pin.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_index as PI  # noqa: E402
from aosphere_core_index.regions.region_map import qualified  # noqa: E402


def row(doc_id, jur="Kenya", gate="pass", worst=90.0,
        product="Marketing Restrictions - Asset Management",
        product_dir="124_Marketing_Restrictions_-_Asset_Management"):
    return {"job": f"{product_dir}/{jur}__{doc_id}", "doc_id": doc_id, "product": product,
            "product_dir": product_dir, "jurisdiction": jur, "gate": gate,
            "worst_score": worst, "slug": f"s-{doc_id}"}


# ---------------- collisions ----------------
def test_one_document_per_region_is_left_alone():
    rows = [row("1", "Kenya"), row("2", "Ghana")]
    kept, collisions = PI.resolve_collisions(rows, None)
    assert len(kept) == 2 and collisions == []


def test_two_documents_in_one_region_are_resolved_and_REPORTED():
    kept, collisions = PI.resolve_collisions([row("1"), row("2")], None)
    assert len(kept) == 1
    assert len(collisions) == 1
    c = collisions[0]
    assert c["region"] == qualified("Marketing Restrictions - Asset Management", "Kenya")
    assert c["kept"] in ("1", "2") and c["dropped"] == [x for x in ("1", "2")
                                                        if x != c["kept"]]
    assert c["rule"] == "tiebreak"


def test_a_passing_document_beats_a_reviewed_one():
    kept, _ = PI.resolve_collisions([row("1", gate="review"), row("2", gate="pass")], None)
    assert kept[0]["doc_id"] == "2"


def test_at_equal_gate_the_higher_score_wins():
    kept, _ = PI.resolve_collisions([row("1", worst=71.0), row("2", worst=95.0)], None)
    assert kept[0]["doc_id"] == "2"


def test_resolution_is_deterministic_whatever_the_input_order():
    a, _ = PI.resolve_collisions([row("1"), row("2"), row("3")], None)
    b, _ = PI.resolve_collisions([row("3"), row("1"), row("2")], None)
    assert a[0]["doc_id"] == b[0]["doc_id"]


def test_the_keeper_rule_wins_over_the_tiebreak_when_metadata_says_so(tmp_path, monkeypatch):
    """keeper_stems is the VALIDATED rule (DOCNAME contains Survey / Memorandum) and it is
    why the promotion reads corpus-src at all. A higher score must not override it: the
    superseded document can easily score better than the current one."""
    src = tmp_path / "124_Marketing_Restrictions_-_Asset_Management" / "Kenya"
    src.mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "extract_to_s3",
                        type(sys)("extract_to_s3"))
    sys.modules["extract_to_s3"].keeper_stems = lambda d, p: {"1"}
    kept, collisions = PI.resolve_collisions(
        [row("1", worst=71.0), row("2", worst=99.0)], tmp_path)
    assert kept[0]["doc_id"] == "1", "the keeper rule must beat the score tiebreak"
    assert collisions[0]["rule"] == "keeper_stems"


def test_a_keeper_rule_that_matches_nothing_falls_back_rather_than_dropping_everything(
        tmp_path, monkeypatch):
    """A jurisdiction whose Doc_metadata does not mention either document must still get
    one indexed — returning zero documents for it would be a silent hole."""
    (tmp_path / "124_Marketing_Restrictions_-_Asset_Management" / "Kenya").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "extract_to_s3", type(sys)("extract_to_s3"))
    sys.modules["extract_to_s3"].keeper_stems = lambda d, p: {"999"}
    kept, collisions = PI.resolve_collisions([row("1"), row("2")], tmp_path)
    assert len(kept) == 1 and collisions[0]["rule"] == "tiebreak"


def test_no_source_tree_still_resolves():
    kept, collisions = PI.resolve_collisions([row("1"), row("2")], None)
    assert len(kept) == 1 and collisions[0]["rule"] == "tiebreak"


# ---------------- the stale npz ----------------
class Prog:
    def __init__(self):
        self.d = {"stages": {}}
        self.rows = []

    def bump(self, *a, **k):
        pass

    def publish(self, *a, **k):
        pass

    def stopping(self):
        return False

    def _ledger_row(self, r):
        self.rows.append(r)


class FakeS3:
    """Serves one document's stage-3 markdown."""

    def __init__(self, md):
        self.md = md

    def get_paginator(self, _op):
        outer = self

        class _P:
            @staticmethod
            def paginate(Bucket, Prefix, **kw):              # noqa: N803
                yield {"Contents": [{"Key": k, "Size": len(v)}
                                    for k, v in outer.md.items() if k.startswith(Prefix)]}
        return _P()

    def download_file(self, bucket, key, dest):
        Path(dest).write_bytes(self.md[key])


def test_the_npz_of_a_rewritten_region_is_DELETED_so_reembed_cannot_skip_it(tmp_path,
                                                                            monkeypatch):
    r = row("183509", "Kenya")
    qreg = qualified(r["product"], r["jurisdiction"])
    data = tmp_path / "data"
    art = PI.artifacts_dir(data, qreg)
    art.mkdir(parents=True)
    # The seeded state: a content.json AND an .npz from the previous index version.
    (art / f"{qreg}.content.json").write_text(json.dumps({"sections": [{"key": "old"}],
                                                          "answers_by_clause": {}}))
    npz = art / f"{qreg}.sections.npz"
    npz.write_bytes(b"vectors describing the PREVIOUS extraction")

    job_prefix = f"corpus/r/{r['job']}/{PI.STAGE3}"
    s3 = FakeS3({f"{job_prefix}/index.md": b"# A\n\ntext\n"})

    # tree_to_content and merge_guidance_alerts are exercised for real elsewhere; here the
    # subject is the npz, so they are stubbed to a known-good content.json.
    def fake_run(argv, env=None, cwd=None, on_line=None):
        if str(PI.TREE_TO_CONTENT) in argv:
            Path(argv[2]).write_text(json.dumps({"sections": [{"key": "A1"}, {"key": "A2"}]}))
            return 0, "wrote 2 rows"
        return 0, "guidance clause-keys: 3 attached (1 dropped)"

    monkeypatch.setattr(PI, "_run", fake_run)
    res = PI.stage_content(Prog(), s3, "bkt", "corpus/r", [r], data, tmp_path / "trees", None)

    assert not npz.exists(), \
        "the stale .npz survived — reembed would skip this region and keep old vectors"
    assert res["rewritten"] == [qreg]
    assert res["built"] == 1 and res["failed"] == []
    assert res["guidance_dropped"] == 1, "dropped curated guidance must be counted, not lost"


def test_a_region_that_converts_to_zero_sections_is_a_failure_not_a_publish(tmp_path,
                                                                            monkeypatch):
    """A content.json with no sections is a silent hole in the corpus, not a document."""
    r = row("1", "Kenya")
    data = tmp_path / "data"
    s3 = FakeS3({f"corpus/r/{r['job']}/{PI.STAGE3}/index.md": b"# x"})

    def fake_run(argv, env=None, cwd=None, on_line=None):
        Path(argv[2]).write_text(json.dumps({"sections": []}))
        return 0, ""

    monkeypatch.setattr(PI, "_run", fake_run)
    res = PI.stage_content(Prog(), s3, "bkt", "corpus/r", [r], data, tmp_path / "t", None)
    assert res["built"] == 0 and len(res["failed"]) == 1
    assert "0 sections" in res["failed"][0]["error"]


def test_a_document_with_no_markdown_in_s3_is_a_failure_with_a_reason(tmp_path):
    r = row("1", "Kenya")
    res = PI.stage_content(Prog(), FakeS3({}), "bkt", "corpus/r", [r],
                           tmp_path / "d", tmp_path / "t", None)
    assert res["built"] == 0
    assert "no 03_stage3_final markdown" in res["failed"][0]["error"]


def test_only_markdown_is_downloaded(tmp_path, monkeypatch):
    """A real tree measured 22MB, of which 552KB is .md and 21MB is page snapshots —
    and tree_to_content reads .md exclusively."""
    r = row("1", "Kenya")
    prefix = f"corpus/r/{r['job']}/{PI.STAGE3}"
    s3 = FakeS3({f"{prefix}/index.md": b"# x",
                 f"{prefix}/page-001.png": b"\x89PNG" + b"x" * 5000,
                 f"{prefix}/nested/b.md": b"# y"})
    got = PI._download_tree(s3, "bkt", f"corpus/r/{r['job']}", tmp_path, r["job"])
    files = sorted(p.name for p in got.rglob("*") if p.is_file())
    assert files == ["b.md", "index.md"]
