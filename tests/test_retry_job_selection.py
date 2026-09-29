"""A retry marker must actually remove its document from the "already done" set.

The production failure this guards: 132 retry markers in S3, and a worker that printed

    already in s3://.../corpus/2026-08-21-02: 1088 document(s)
    8 shard(s), 0-0 pages of unique pages each; ... 0 job(s) to extract

then exited 0. The selection in main() is pure set algebra over three independently-built
sets of "<product>/<label>" strings:

    remote_done = done_labels(...)        # from scorecard.json keys under the run prefix
    remote_done |= permanent_labels(...)  # deterministic failures, skipped like finished ones
    remote_done -= retry_labels(...)      # ...unless a reviewer forced one back

done_labels derives its labels by SPLITTING AN S3 KEY; retry_labels reads them from a marker's
JSON BODY. Two different derivations of the same identity, and nothing checks they agree. If
they ever diverge — a trailing slash, a sanitised filename leaking in, a product whose path has
an extra segment — the subtraction silently removes nothing. No exception, no log line (both
call sites are guarded on the set being non-empty), and the run looks complete.

So these tests assert the CONTRACT rather than either function alone, using the labels this
corpus actually contains: spaces, parentheses, commas, accents, ampersands.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_worker as W  # noqa: E402

RUN = "s3://bkt/corpus/2026-08-21-02"

# Real labels from this corpus. Every one of these has broken something at least once.
DOCS = [
    ("104_Shareholding_Disclosure", "Egypt__175174"),
    ("104_Shareholding_Disclosure", "New Zealand__172150"),          # space
    ("153_ISDA_e-contracts",
     "Canada (Alberta, British Columbia, Ontario, Quebec and Canadian federal law)__69636"),
    ("155_Data_Privacy", "Aruba, Curaçao and St. Maarten__164302"),  # comma + accent
    ("160_Bank_Confidentiality_&_Outsourcing", "Bahrain__124136"),   # ampersand
    ("129_G20_-_IM", "VM__EU Member States__179002"),                # label with __ inside
    ("124_Marketing_Restrictions_-_Asset_Management",
     "Abu Dhabi Global Market (ADGM)__170680"),
]


def _ls_recursive(docs):
    """What `aws s3 ls <run>/ --recursive` prints for finished documents."""
    lines = []
    for product, label in docs:
        for tail in ("scorecard.json", "validation.json", "03_stage3_final/01-intro.md"):
            lines.append(f"2026-08-26 11:47:20        169 "
                         f"corpus/2026-08-21-02/{product}/{label}/{tail}")
    # a fallback tier's nested scorecard: NOT a document (len(rel) != 3)
    lines.append("2026-08-26 11:47:20        169 corpus/2026-08-21-02/"
                 "104_Shareholding_Disclosure/Egypt__175174/mineru_full_attempt/scorecard.json")
    return "\n".join(lines) + "\n"


def _ls_markers(docs):
    """What `aws s3 ls <run>/_retry/` prints — filenames are SANITISED, unlike the labels."""
    lines = []
    for product, label in docs:
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{product}__{label}")
        lines.append(f"2026-08-26 11:47:20        211 {safe}.json")
    return "\n".join(lines) + "\n"


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _fake_s3(done, marked=(), permanent=()):
    """Serve the three listings and the per-marker reads from one fake."""
    by_kind = {"_retry": list(marked), "_permanent": list(permanent)}

    def fake(*args, profile=None):
        if args[0] == "ls":
            uri = args[1]
            for kind, docs in by_kind.items():
                if uri.endswith(f"/{kind}/"):
                    return _Proc(out=_ls_markers(docs))
            return _Proc(out=_ls_recursive(done))
        # cp of one marker: find which document its sanitised filename belongs to
        name = args[1].rsplit("/", 1)[-1][: -len(".json")]
        kind = "_retry" if "/_retry/" in args[1] else "_permanent"
        for product, label in by_kind[kind]:
            safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{product}__{label}")
            if safe == name:
                return _Proc(out=json.dumps({"product": product, "label": label,
                                             "cause": "gpu_oom"}))
        return _Proc(rc=1, err="NoSuchKey")

    return fake


# --------------------------------------------------------------------------- contract

def test_done_and_retry_agree_on_the_label_shape(monkeypatch):
    """THE invariant. Both sides derive "<product>/<label>" by different routes — one from an
    S3 key, one from a JSON body — and the subtraction is only meaningful if they match."""
    monkeypatch.setattr(W, "_s3", _fake_s3(DOCS, marked=DOCS))
    done = W.done_labels(RUN, None)
    marked = W.retry_labels(RUN, None)
    assert done == marked, (
        "the two derivations disagree, so `remote_done -= marked` would remove nothing:\n"
        f"  only in done  : {sorted(done - marked)}\n"
        f"  only in marked: {sorted(marked - done)}")


@pytest.mark.parametrize("product,label", DOCS)
def test_every_awkward_label_round_trips(monkeypatch, product, label):
    """Per-document, so a failure names the label that broke it rather than the whole set."""
    monkeypatch.setattr(W, "_s3", _fake_s3([(product, label)], marked=[(product, label)]))
    assert W.done_labels(RUN, None) == W.retry_labels(RUN, None) == {f"{product}/{label}"}


def test_a_marked_document_is_removed_from_done(monkeypatch):
    """The selection main() performs: a retry makes a finished document eligible again."""
    marked = DOCS[:2]
    monkeypatch.setattr(W, "_s3", _fake_s3(DOCS, marked=marked))
    remote_done = W.done_labels(RUN, None)
    remote_done -= W.retry_labels(RUN, None)
    assert len(remote_done) == len(DOCS) - 2
    for product, label in marked:
        assert f"{product}/{label}" not in remote_done


def test_a_retry_overrides_a_permanent_marker(monkeypatch):
    """The documented escape hatch: "mark one for retry to force it". Order matters — the
    union with permanent happens first, the retry subtraction second."""
    doc = DOCS[0]
    monkeypatch.setattr(W, "_s3", _fake_s3(DOCS[1:], permanent=[doc], marked=[doc]))
    remote_done = W.done_labels(RUN, None)
    remote_done |= W.permanent_labels(RUN, None)
    assert f"{doc[0]}/{doc[1]}" in remote_done, "permanent alone means skipped"
    remote_done -= W.retry_labels(RUN, None)
    assert f"{doc[0]}/{doc[1]}" not in remote_done, "a retry must beat a permanent marker"


def test_a_nested_attempt_scorecard_is_not_a_document(monkeypatch):
    """A fallback tier leaves its own scorecard under <label>/mineru_full_attempt/. Counting it
    would mark a document done on the strength of an attempt whose final scorecard never landed."""
    monkeypatch.setattr(W, "_s3", _fake_s3(DOCS))
    done = W.done_labels(RUN, None)
    assert not any("attempt" in d for d in done)
    assert len(done) == len(DOCS)


def test_no_markers_leaves_the_done_set_untouched(monkeypatch):
    monkeypatch.setattr(W, "_s3", _fake_s3(DOCS))
    before = W.done_labels(RUN, None)
    assert W.retry_labels(RUN, None) == set()
    assert before - W.retry_labels(RUN, None) == before
