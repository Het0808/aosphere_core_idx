"""Where the Doc Library lives: inside the index version it describes.

A single flat "doc-gallery/" prefix is one mutable copy shared by every index version. Flip
the index pointer and the scorecards still describe the PREVIOUS extraction; roll the index
back and the gallery stays rolled forward. Nesting it under index/<version>/doc-gallery/ ties
the two together, so the verdict a reader sees belongs to the content being searched.

Reader and writer must agree on that resolution, or the tab goes blank in a way nothing else
would catch — so both are pinned here, including the fallbacks, which is where this kind of
change actually goes wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lib_gallery as G  # noqa: E402
import push_hybrid_s3 as W  # noqa: E402
from aosphere_core_index.service import doc_gallery as R  # noqa: E402
from aosphere_core_index.service import promotion_monitor as P  # noqa: E402

VERSION = "2026-08-13-titan-hybrid-r2"
NESTED = f"index/{VERSION}/doc-gallery"


class _S3:
    """Minimal stand-in for the pointer read."""

    def __init__(self, body=VERSION, raise_exc=None):
        self.body, self.raise_exc, self.asked = body, raise_exc, []

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3's own casing
        self.asked.append((Bucket, Key))
        if self.raise_exc:
            raise self.raise_exc
        return {"Body": type("B", (), {"read": lambda _self: self.body.encode()})()}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ACI_DOC_GALLERY_PREFIX", "ACI_INDEX_VERSION", "ACI_INDEX_POINTER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(R, "_store", None, raising=False)


# ---------------- writer ----------------
def test_writer_follows_the_index_pointer(monkeypatch):
    s3 = _S3()
    assert W.gallery_prefix(s3) == NESTED
    assert s3.asked == [(W.BUCKET, "index/latest")], "must read the same pointer as the data mount"


def test_writer_honours_an_explicit_prefix(monkeypatch):
    monkeypatch.setenv("ACI_DOC_GALLERY_PREFIX", "index/pinned/doc-gallery")
    s3 = _S3()
    assert W.gallery_prefix(s3) == "index/pinned/doc-gallery"
    assert s3.asked == [], "an explicit prefix must not need S3 at all"


def test_writer_honours_a_pinned_version(monkeypatch):
    monkeypatch.setenv("ACI_INDEX_VERSION", "2026-07-29-titan-hybrid")
    s3 = _S3()
    assert W.gallery_prefix(s3) == "index/2026-07-29-titan-hybrid/doc-gallery"
    assert s3.asked == []


def test_writer_falls_back_rather_than_publishing_nowhere(monkeypatch):
    """An unreadable pointer must not send objects to "index//doc-gallery"."""
    assert W.gallery_prefix(_S3(raise_exc=RuntimeError("no creds"))) == W.LEGACY_PREFIX


# ---------------- reader ----------------
def test_reader_resolves_the_same_prefix_as_the_writer(monkeypatch):
    calls = {}

    class _Boto:
        @staticmethod
        def client(_svc, region_name=None):
            calls["region"] = region_name
            return _S3()

    monkeypatch.setitem(sys.modules, "boto3", _Boto)
    assert R._gallery_prefix("bucket", "eu-west-1") == NESTED
    assert calls["region"] == "eu-west-1"


def test_reader_explicit_prefix_wins(monkeypatch):
    monkeypatch.setenv("ACI_DOC_GALLERY_PREFIX", "doc-gallery")
    assert R._gallery_prefix("bucket", "eu-west-1") == "doc-gallery"


def test_reader_keeps_serving_when_the_pointer_is_unreadable(monkeypatch):
    class _Boto:
        @staticmethod
        def client(_svc, region_name=None):
            return _S3(raise_exc=RuntimeError("denied"))

    monkeypatch.setitem(sys.modules, "boto3", _Boto)
    assert R._gallery_prefix("bucket", "eu-west-1") == R._LEGACY_GALLERY_PREFIX


def test_reader_and_writer_agree(monkeypatch):
    """The one that matters: disagreement means an empty tab, not an error."""
    class _Boto:
        @staticmethod
        def client(_svc, region_name=None):
            return _S3()

    monkeypatch.setitem(sys.modules, "boto3", _Boto)
    assert R._gallery_prefix(W.BUCKET, "eu-west-1") == W.gallery_prefix(_S3())


# ---------------- promotion: the THIRD resolver ----------------
# A promotion writes the gallery for an index version. That makes three implementations of
# one string — publisher, reader, promotion target — and a writer that publishes where the
# reader does not look fails SILENTLY: the only symptom is an empty tab.
def test_the_promotion_target_is_the_prefix_the_reader_reads():
    assert P.promotion_target(VERSION) == NESTED
    assert R.version_prefix(VERSION) == NESTED


def test_all_three_resolvers_agree(monkeypatch):
    """publisher == reader == promotion target, for the same version."""
    monkeypatch.setenv("ACI_INDEX_VERSION", VERSION)

    class _Boto:
        @staticmethod
        def client(_svc, region_name=None):
            return _S3()

    monkeypatch.setitem(sys.modules, "boto3", _Boto)
    monkeypatch.setenv("ACI_INDEX_POINTER", "index/latest")
    assert (W.gallery_prefix(_S3())
            == P.promotion_target(VERSION)
            == R._gallery_prefix(W.BUCKET, "eu-west-1")
            == NESTED)


def test_the_publisher_helpers_are_re_exported_not_reimplemented():
    """push_hybrid_s3 must BE lib_gallery's implementations, not a second copy of them.

    They were moved out because push_hybrid_s3 cannot be imported without fitz/MinerU (it
    pulls hybrid_extract -> pdf2mdtree at module scope), and a promotion pod has neither.
    Two copies of the slug rule is the failure this whole file exists to prevent.
    """
    assert W.gallery_prefix is G.gallery_prefix
    assert W.gslug is G.gslug
    assert W.doc_name_fields is G.doc_name_fields
    assert W.rescue_flag is G.rescue_flag
    assert W.BUCKET == G.BUCKET and W.LEGACY_PREFIX == G.LEGACY_PREFIX


def test_the_promotion_and_the_publisher_build_the_same_slug():
    """promotion_monitor cannot import scripts/, so it carries the one-line slug rule.
    That is the only duplication tolerated here, and this is what keeps it honest."""
    for product, jur, doc in (("Data Privacy", "Spain", "172575"),
                              ("Shareholding Disclosure", "United States - California", "9"),
                              ("Marketing Restrictions - Asset Management",
                               "Aruba, Curaçao and St. Maarten", "183509"),
                              ("Data Privacy",
                               "Canada (Alberta, British Columbia, Ontario and Quebec)", "1")):
        assert P._gslug(product, jur, doc) == G.gslug(product, jur, doc)


def test_a_promotion_refuses_to_target_the_legacy_flat_prefix():
    """The legacy prefix is a FALLBACK for an unreadable pointer, never a deliberate target:
    it is one mutable copy shared by every version."""
    assert P.promotion_target(VERSION) != G.LEGACY_PREFIX
    assert not P.valid_version("")


def test_local_store_is_untouched_by_any_of_this(monkeypatch, tmp_path):
    """No bucket configured -> a directory, no pointer read, no credentials."""
    monkeypatch.delenv("ACI_DOC_GALLERY_BUCKET", raising=False)
    monkeypatch.setenv("ACI_DOC_GALLERY_DIR", str(tmp_path))
    assert isinstance(R.store(), R.LocalDocStore)
