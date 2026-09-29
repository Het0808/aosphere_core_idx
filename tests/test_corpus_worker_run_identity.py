"""The results prefix names the run, and resume is always on against it.

There is no --force and no re-extract mode: a document with a scorecard under the configured
prefix is skipped, one without is extracted. A new prefix therefore re-extracts the whole corpus
and the same prefix continues an interrupted one, which is the same rule read two ways rather
than two code paths that can disagree.

The local scratch directory has to follow the prefix for that to hold. Kubernetes reuses the pod
(and its emptyDir) under restartPolicy: OnFailure, so a fixed /work/out would let a previous
run's trees satisfy the local skip and silently suppress work the new prefix has never done —
a full re-extraction that quietly extracted nothing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_worker as W  # noqa: E402


@pytest.mark.parametrize("results, expect_id", [
    ("s3://bucket/corpus/2026-08-19", "2026-08-19"),
    ("s3://bucket/corpus/2026-08-19/", "2026-08-19"),          # a trailing slash is not a run
    ("s3://bucket/baseline-v2", "baseline-v2"),
    ("s3://bucket", "bucket"),
])
def test_the_prefix_leaf_is_the_run_id(results, expect_id):
    run_id, out = W.resolve_run(results, None)
    assert run_id == expect_id
    assert out == Path("/work/out") / expect_id


def test_two_prefixes_never_share_a_scratch_directory():
    """The invariant that makes "new prefix = full re-extract" true on a restarted pod."""
    _a_id, a = W.resolve_run("s3://bucket/corpus/2026-08-19", None)
    _b_id, b = W.resolve_run("s3://bucket/corpus/2026-09-01", None)
    assert a != b


def test_an_explicit_out_dir_wins(tmp_path):
    """An operator pinning the scratch dir (a mounted volume, a laptop run) keeps control."""
    run_id, out = W.resolve_run("s3://bucket/corpus/2026-08-19", str(tmp_path))
    assert run_id == "2026-08-19"
    assert out == tmp_path.resolve()


def test_resume_reads_scorecards_only(monkeypatch):
    """The resume set is built from scorecard keys — not trees, and not any other file.

    Listing whole trees would be ~76 GB of keys to page through, and treating a job dir as done
    because some other artifact landed would skip a document whose scoring never finished."""
    listing = "\n".join([
        "2026-08-19 10:00:00     1024 corpus/run1/155_Data_Privacy/Austria__1/scorecard.json",
        "2026-08-19 10:00:00   999999 corpus/run1/155_Data_Privacy/Austria__1/source.pdf",
        "2026-08-19 10:00:00     2048 corpus/run1/155_Data_Privacy/Belgium__2/validation.json",
        "2026-08-19 10:00:00     1024 corpus/run1/104_Shareholding/Poland__3/scorecard.json",
        "2026-08-19 10:00:00      512 corpus/run1/104_Shareholding/Poland__3/03_stage3_final/x.md",
    ])

    class _R:
        returncode, stdout, stderr = 0, listing, ""

    monkeypatch.setattr(W, "_s3", lambda *a, **k: _R())
    done = W.done_labels("s3://bucket/corpus/run1", None)
    assert done == {"155_Data_Privacy/Austria__1", "104_Shareholding/Poland__3"}
    assert "155_Data_Privacy/Belgium__2" not in done, \
        "a job with artifacts but no scorecard is NOT done"


def test_a_fallback_attempt_scorecard_does_not_count_as_finished(monkeypatch):
    """Observed in the first real run: the fallback tiers leave their own scorecards behind.

        .../Abu Dhabi Global Market__183256/mineru_full_attempt/scorecard.json
        .../Barbados__183141/hybrid_attempt/scorecard.json

    Those are working notes from a tier, not the verdict — run_corpus writes the real one at the
    job root, last. Counting a nested attempt would mark a document finished whose chain was
    interrupted before it ever produced a final scorecard, and resume would then skip it for the
    rest of the corpus's life."""
    listing = "\n".join([
        "2026-08-19 17:12:59  19395 corpus/run1/180_PWPB/AbuDhabi__183256/mineru_full_attempt/scorecard.json",
        "2026-08-19 17:12:59  22586 corpus/run1/180_PWPB/AbuDhabi__183256/scorecard.json",
        "2026-08-19 17:13:52  21381 corpus/run1/180_PWPB/Barbados__183141/hybrid_attempt/scorecard.json",
    ])

    class _R:
        returncode, stdout, stderr = 0, listing, ""

    monkeypatch.setattr(W, "_s3", lambda *a, **k: _R())
    done = W.done_labels("s3://bucket/corpus/run1", None)
    assert done == {"180_PWPB/AbuDhabi__183256"}
    assert "180_PWPB/Barbados__183141" not in done, \
        "an attempt scorecard is not a finished document"


def test_labels_containing_spaces_are_recognised_as_finished(monkeypatch):
    """Caught by the first real run against S3, and it would have wrecked resume.

    `aws s3 ls --recursive` prints "<date> <time> <size> <key>", and most jurisdictions in this
    corpus have spaces in their names. Reading the key as the last whitespace-separated field
    truncated "Abu Dhabi Global Market__183256/scorecard.json" to "Market__183256/scorecard.json",
    so the document never matched a work-list label — and on EKS every restart would re-extract
    most of the corpus at full GPU price while reporting resume as working."""
    listing = "\n".join([
        "2026-08-19 17:12:59  22586 corpus/run1/180_PWPB/Abu Dhabi Global Market__183256/scorecard.json",
        "2026-08-19 17:13:52  20067 corpus/run1/180_PWPB/Barbados__183141/scorecard.json",
        "2026-08-19 17:14:10  20067 corpus/run1/155_DP/United States - California (Short Form)__175656/scorecard.json",
    ])

    class _R:
        returncode, stdout, stderr = 0, listing, ""

    monkeypatch.setattr(W, "_s3", lambda *a, **k: _R())
    assert W.done_labels("s3://bucket/corpus/run1", None) == {
        "180_PWPB/Abu Dhabi Global Market__183256",
        "180_PWPB/Barbados__183141",
        "155_DP/United States - California (Short Form)__175656",
    }


def test_a_failed_listing_does_not_silently_mean_done(monkeypatch):
    """If the LIST fails, the safe reading is "nothing is finished" — re-extracting a document
    costs GPU time, whereas treating the corpus as complete would end the run having done
    nothing and look like success."""
    class _R:
        returncode, stdout, stderr = 1, "", "AccessDenied"

    monkeypatch.setattr(W, "_s3", lambda *a, **k: _R())
    assert W.done_labels("s3://bucket/corpus/run1", None) == set()


def test_push_job_mirrors_rather_than_adds(monkeypatch, tmp_path):
    """A retry commonly changes a document's file layout — stage 4 numbers it differently,
    or drops/adds a file. job_dir is regenerated from scratch on every run, so it is always
    this document's complete, correct state; without --delete, `aws s3 sync` only ever adds
    or overwrites, so a file the new run no longer produces stays at dest forever — the
    exact shape of the bug seen on Japan 172122, where a pre-retry
    "06-marketing-selling-to-the-public.md" sat next to the retry's own
    "07-marketing-selling-to-the-public.md" because nothing ever told S3 the old one was
    gone."""
    calls = []

    class _R:
        returncode, stdout, stderr = 0, "", ""

    def _fake_s3(*args, **kwargs):
        calls.append(args)
        return _R()

    monkeypatch.setattr(W, "_s3", _fake_s3)
    ok = W.push_job(tmp_path, "s3://bucket/corpus/run1", "124_Marketing", None)
    assert ok
    (cmd,) = calls
    assert cmd[0] == "sync"
    assert "--delete" in cmd
