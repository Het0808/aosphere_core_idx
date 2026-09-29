"""The manifest merge — the one that has already destroyed data once.

From scripts/push_hybrid_s3.py's own comment, at the branch this file exists to keep out:

    A PARTIAL run must not drop the rows it is not republishing. Getting this wrong emptied
    the manifest: each 25-document batch stripped every -mineru row and wrote back only its
    own, so 455 published documents ended up as 5 manifest entries and the Doc Library would
    have listed five.

A promotion is ALWAYS partial by construction — a product allow-list and a gate filter — so
the full-replace branch must be unreachable here under every flag, `--force` included. That
is asserted by brute force over the flag combinations rather than by reading the code, because
the whole failure mode is a branch someone reintroduces while meaning well.

Three more properties are pinned:

  * only documents that were actually COPIED get rows. A row pointing at a key that is not
    there is a guaranteed 404, and doc_gallery._doc_row carries `has_viewer` precisely
    because a reviewer cannot tell that apart from a broken viewer;
  * the write is CONDITIONAL. Minutes pass between the read and the write, so a concurrent
    writer must cause a refusal, not silent loss;
  * it happens ONCE, AT THE END. Objects are invisible until the manifest names them, so a
    promotion that dies leaves the live gallery byte-for-byte untouched. That is the single
    most important safety property in the design, and it is tested in test_promotion_resume.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_run as PR  # noqa: E402

TARGET = "index/2026-08-21-02-p1/doc-gallery"
KEY = f"{TARGET}/manifest.json"


class Boom(Exception):
    """Stands in for botocore's ClientError, which is what the code actually inspects."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """A manifest object with an ETag, and a scripted sequence of write outcomes."""

    def __init__(self, manifest=None, fail_puts=(), mutate_between=None):
        self.body = None if manifest is None else json.dumps(manifest).encode()
        self.etag = '"e0"' if manifest is not None else None
        self.fail_puts = list(fail_puts)
        self.mutate_between = mutate_between
        self.puts: list[dict] = []
        self.gets = 0

    class _NoSuchKey(Exception):
        pass

    def get_object(self, Bucket, Key):                       # noqa: N803
        self.gets += 1
        if self.body is None:
            raise Boom("NoSuchKey")
        return {"Body": type("B", (), {"read": lambda _s, b=self.body: b})(),
                "ETag": self.etag}

    def put_object(self, Bucket, Key, Body, ContentType=None, **cond):   # noqa: N803
        self.puts.append({"key": Key, "body": json.loads(Body), "cond": cond})
        if self.fail_puts and self.fail_puts.pop(0):
            if self.mutate_between:
                self.body = json.dumps(self.mutate_between).encode()
                self.etag = '"e-moved"'
            raise Boom("PreconditionFailed")
        self.body, self.etag = Body, '"e-new"'
        return {"ETag": self.etag}


def rows(n, prefix="old"):
    return [{"slug": f"{prefix}-{i}-mineru", "product": "Data Privacy", "status": "ok"}
            for i in range(n)]


def entries(n, prefix="new"):
    return [{"slug": f"{prefix}-{i}-mineru", "product": "Data Privacy", "status": "ok",
             "gate": "pass"} for i in range(n)]


# ---------------- THE incident ----------------
def test_455_plus_5_is_460_and_never_5():
    """The exact shape of the incident, in the numbers it happened in."""
    s3 = FakeS3(rows(455))
    merged = PR.merge_manifest(s3, "bkt", TARGET, entries(5))
    assert len(merged) == 460
    assert len(s3.puts[-1]["body"]) == 460


def _code_of(fn) -> str:
    """A function's source with its docstring removed.

    The docstrings here NAME the branch that must not exist, so a naive substring search
    over the source would match the warning instead of the code.
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)):
        tree.body = tree.body[1:]
    return ast.unparse(ast.Module(body=tree.body, type_ignores=[]))


def test_a_promotion_can_never_take_the_full_replace_branch():
    """Asserted over the module's CODE, because the failure mode is a branch someone
    reintroduces while meaning well — and by then the data is gone."""
    code = _code_of(PR.merge_manifest)
    assert "-mineru" not in code, \
        "the -mineru full-replace filter is what emptied the manifest"
    assert "endswith" not in code, "the merge is keyed on slugs, never on a slug suffix"
    assert "mine" in code, "the merge must be an upsert keyed on THIS run's slugs"


def test_pre_existing_rows_survive_every_flag_combination():
    """--force re-copies documents; it must never widen what the manifest write removes."""
    existing = rows(455)
    keep = {d["slug"] for d in existing}
    for n_new in (1, 5, 455):
        s3 = FakeS3(existing)
        merged = PR.merge_manifest(s3, "bkt", TARGET, entries(n_new))
        assert keep <= {d["slug"] for d in merged}, "a pre-existing row was dropped"


# ---------------- upsert semantics ----------------
def test_re_promoting_the_same_documents_upserts_rather_than_duplicating():
    s3 = FakeS3(rows(455) + entries(5))
    merged = PR.merge_manifest(s3, "bkt", TARGET, entries(5))
    assert len(merged) == 460, "re-promotion must not append a second copy"


def test_the_new_row_wins_when_a_slug_is_promoted_again():
    """A re-extraction's verdict is the current one; the stale row must not survive."""
    old = [{"slug": "new-0-mineru", "gate": "fail", "status": "ok"}]
    s3 = FakeS3(old)
    merged = PR.merge_manifest(s3, "bkt", TARGET, entries(1))
    assert [d for d in merged if d["slug"] == "new-0-mineru"][0]["gate"] == "pass"
    assert len(merged) == 1


def test_only_copied_documents_get_rows():
    """The caller passes the entries copy_doc actually returned. A failed copy contributes
    nothing, so the manifest cannot name a key that is not there."""
    s3 = FakeS3(rows(10))
    merged = PR.merge_manifest(s3, "bkt", TARGET, entries(2))
    assert len(merged) == 12
    assert {d["slug"] for d in merged if d["slug"].startswith("new")} == \
        {"new-0-mineru", "new-1-mineru"}


# ---------------- conditional write ----------------
def test_an_existing_manifest_is_written_with_ifmatch():
    s3 = FakeS3(rows(3))
    PR.merge_manifest(s3, "bkt", TARGET, entries(1))
    assert s3.puts[-1]["cond"] == {"IfMatch": '"e0"'}


def test_a_first_publish_is_written_with_ifnonematch():
    """Two promotions racing to create the same version must not both think they made it."""
    s3 = FakeS3(None)
    merged = PR.merge_manifest(s3, "bkt", TARGET, entries(2))
    assert merged == entries(2)
    assert s3.puts[-1]["cond"] == {"IfNoneMatch": "*"}


def test_a_concurrent_writer_causes_a_re_merge_not_a_silent_overwrite():
    """Whoever committed in the window keeps their rows."""
    theirs = rows(455) + [{"slug": "theirs-mineru", "status": "ok"}]
    s3 = FakeS3(rows(455), fail_puts=[True], mutate_between=theirs)
    merged = PR.merge_manifest(s3, "bkt", TARGET, entries(1))
    slugs = {d["slug"] for d in merged}
    assert "theirs-mineru" in slugs, "the concurrent writer's row was discarded"
    assert "new-0-mineru" in slugs
    assert len(s3.puts) == 2 and s3.gets == 2


def test_it_gives_up_rather_than_writing_blind():
    s3 = FakeS3(rows(3), fail_puts=[True, True, True], mutate_between=rows(4))
    with pytest.raises(RuntimeError, match="refusing to write blind"):
        PR.merge_manifest(s3, "bkt", TARGET, entries(1))
    assert len(s3.puts) == 3, "exactly the configured number of attempts, then stop"


def test_an_unexpected_s3_error_is_not_mistaken_for_a_missing_manifest():
    """AccessDenied on the manifest must not be read as "first publish" and then produce a
    manifest containing only this promotion's rows."""
    class Denied(FakeS3):
        def get_object(self, Bucket, Key):                   # noqa: N803
            raise Boom("AccessDenied")

    with pytest.raises(Boom):
        PR.merge_manifest(Denied(rows(455)), "bkt", TARGET, entries(1))


def test_a_manifest_that_is_not_a_list_is_refused_rather_than_replaced():
    s3 = FakeS3({"not": "a list"})
    with pytest.raises(RuntimeError, match="not a JSON list"):
        PR.merge_manifest(s3, "bkt", TARGET, entries(1))
    assert not s3.puts


def test_the_manifest_is_the_only_object_this_function_writes():
    s3 = FakeS3(rows(2))
    PR.merge_manifest(s3, "bkt", TARGET, entries(1))
    assert {p["key"] for p in s3.puts} == {KEY}
