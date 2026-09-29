"""pull_sources() must bring Doc_metadata.json down with the PDFs, not just the PDFs.

product_rules.content_tile_name() reads Doc_metadata.json from beside the PDF on the
worker's local disk to route 124_Marketing_Restrictions_-_Asset_Management's summary
documents (see product_rules.AI_PIPELINE). The sync used to be `--exclude "*" --include
"*.pdf"`, which downloads the PDFs and nothing else -- verified against the live
corpus-src bucket on 2026-09-10: that filter pulled 194 PDFs and 0 of the 89
Doc_metadata.json sidecars that exist there, so content_tile_name() found nothing to
read and every summary document silently fell through to the survey route.

This does not re-verify `aws s3 sync`'s own filter semantics (done by hand against the
real bucket -- a bare "Doc_metadata.json" pattern does not match a nested
"Australia/Doc_metadata.json" key, "*Doc_metadata.json" does). It pins the ARGUMENTS
pull_sources passes to `_s3`, which is the part a future edit could silently regress.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_worker as W  # noqa: E402


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_sync_includes_the_metadata_sidecar_alongside_pdfs(tmp_path, monkeypatch):
    calls = {}

    def fake(*args, profile=None):
        calls["args"] = args
        return _Proc()

    monkeypatch.setattr(W, "_s3", fake)
    W.pull_sources("s3://bucket/corpus-src", tmp_path, None)

    args = calls["args"]
    assert args[0] == "sync"
    assert "--exclude" in args and args[args.index("--exclude") + 1] == "*"
    includes = [args[i + 1] for i, a in enumerate(args) if a == "--include"]
    assert "*.pdf" in includes, "the PDF include was dropped"
    assert "*Doc_metadata.json" in includes, \
        "Doc_metadata.json is not synced -- content_tile_name() will find nothing beside the PDF"


def test_bare_filename_pattern_would_not_have_caught_this(tmp_path, monkeypatch):
    """Regression guard on the fix itself: the wrong-looking-right fix is the bare
    filename, which `aws s3 sync` does not match against a nested key. If this ever
    changes back to "Doc_metadata.json" without the wildcard, this test still passes
    (it only inspects the call), which is why the real bucket check above matters --
    this test exists to document that distinction, not to substitute for it."""
    calls = {}

    def fake(*args, profile=None):
        calls["args"] = args
        return _Proc()

    monkeypatch.setattr(W, "_s3", fake)
    W.pull_sources("s3://bucket/corpus-src", tmp_path, None)
    includes = [calls["args"][i + 1] for i, a in enumerate(calls["args"]) if a == "--include"]
    assert "Doc_metadata.json" not in includes, \
        "the bare filename does not match a nested key -- use a leading wildcard"


def test_sync_failure_still_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "_s3", lambda *a, profile=None: _Proc(rc=1, err="denied"))
    try:
        W.pull_sources("s3://bucket/corpus-src", tmp_path, None)
        assert False, "must exit on a failed sync"
    except SystemExit as e:
        assert "denied" in str(e)
