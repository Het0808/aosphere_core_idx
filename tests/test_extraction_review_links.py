"""A run's extractions are viewable from the monitor, and a link is only offered when real.

A run writes to corpus/<run>/…, which the Doc Library does not read — it serves
index/<version>/doc-gallery/. So a document that has just finished is published nowhere, and
linking the monitor to the gallery would give a 404 for exactly the documents someone wants to
look at while a run is going. The worker therefore builds the same viewer and inspection page
beside each extraction, and the service streams them so the bucket stays private.

Offering a link that 404s is worse than offering none, so availability is checked: newer runs
carry it in the progress object, older ones are probed with a HEAD.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from aosphere_core_index.service import extraction_monitor as em

PRESENT = {"corpus/run1/155_DP/Germany__1/viewer.html": b"<!DOCTYPE html><title>viewer</title>",
           "corpus/run1/155_DP/Germany__1/inspect.html": b"<!DOCTYPE html><title>inspect</title>"}


class _S3:
    def __init__(self, **kw): pass
    def exists(self, key): return key in PRESENT
    def get_bytes(self, key):
        if key not in PRESENT:
            raise FileNotFoundError(key)
        return PRESENT[key]


@pytest.fixture(autouse=True)
def _s3(monkeypatch):
    monkeypatch.setattr(em, "ReadOnlyS3", _S3)


@pytest.mark.parametrize("kind", ["viewer", "inspect"])
def test_a_published_artefact_is_streamed(kind):
    html = em.review_html("b", "corpus/run1", "eu-west-1", "155_DP", "Germany__1", kind)
    assert html and html.startswith(b"<!DOCTYPE html>")


def test_a_document_without_one_returns_none_not_an_error():
    """Runs extracted before the worker built these have no viewer; the caller says so."""
    assert em.review_html("b", "corpus/run1", "eu-west-1", "155_DP", "Austria__2") is None


def test_only_the_two_known_kinds_are_served():
    with pytest.raises(ValueError):
        em.review_html("b", "corpus/run1", "eu-west-1", "155_DP", "Germany__1", "scorecard.json")


@pytest.mark.parametrize("product,label", [
    ("../../index", "Germany__1"),
    ("155_DP", "../../../etc/passwd"),
    ("155_DP", "Germany__1/.."),
])
def test_a_segment_cannot_escape_the_run(product, label):
    with pytest.raises(ValueError):
        em.review_html("b", "corpus/run1", "eu-west-1", product, label)
    with pytest.raises(ValueError):
        em.review_available("b", "corpus/run1", "eu-west-1", product, label)


def test_availability_is_reported_per_artefact():
    """The screen renders `view` and `scorecard` independently, so it needs both answers."""
    assert em.review_available("b", "corpus/run1", "eu-west-1", "155_DP", "Germany__1") == \
        {"viewer": True, "inspect": True}
    assert em.review_available("b", "corpus/run1", "eu-west-1", "155_DP", "Austria__2") == \
        {"viewer": False, "inspect": False}


def test_availability_uses_head_not_a_download(monkeypatch):
    """A viewer is ~7MB. Asking whether it exists must not fetch it."""
    fetched = []

    class _Counting(_S3):
        def get_bytes(self, key):
            fetched.append(key)
            return super().get_bytes(key)

    monkeypatch.setattr(em, "ReadOnlyS3", _Counting)
    em.review_available("b", "corpus/run1", "eu-west-1", "155_DP", "Germany__1")
    assert fetched == [], "review_available must not download anything"
