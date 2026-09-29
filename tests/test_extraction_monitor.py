"""The extraction monitor tells you what a GPU run is really doing — including when it isn't.

On spot capacity a silent worker is the normal case, not an exception: the node is reclaimed, the
pod is rescheduled, and for a while that shard writes nothing. A screen that showed eight workers
because eight shards exist would be actively misleading, so liveness is derived only from whether
a shard's own summary has moved recently, and never inferred from the shard existing.

The other property under test is cheapness. Progress comes from one small object per shard, so the
screen can poll a 17-hour run without touching a single scorecard — the gallery loading every
scorecard is what took the service down once already.
"""

import time

import pytest

from aosphere_core_index.service import extraction_monitor as em


def _shard(idx, *, done=0, jobs=100, pages_done=0, pages_total=5000, gates=None,
           updated_ago=5, started_ago=3600, current=None, failed=0, stopping=False):
    now = time.time()
    return {"run": "2026-08-19", "shard": idx, "shards": 8,
            "done": done, "jobs_total": jobs, "extracted": done, "cloned": 0, "failed": failed,
            "pages_done": pages_done, "pages_total": pages_total, "gates": gates or {},
            "current": current, "recent": [],
            "started_at": now - started_ago, "updated_at": now - updated_ago,
            "stopping": stopping}


def test_no_shards_is_idle_not_an_error():
    """Before the first worker starts there is nothing to read, and that is not a failure."""
    p = em.summarize([])
    assert p["state"] == "idle"
    assert p["percent"] == 0.0 and p["eta_seconds"] is None


def test_totals_and_percentage_are_summed_across_shards():
    p = em.summarize([_shard(0, done=10, pages_done=500), _shard(1, done=6, pages_done=300)])
    assert p["done"] == 16
    assert p["pages_done"] == 800 and p["pages_total"] == 10_000
    assert p["percent"] == 8.0
    assert p["shards_live"] == 2


def test_a_silent_shard_is_reported_stale_rather_than_running():
    """A reclaimed spot node stops writing. The screen must say so."""
    p = em.summarize([_shard(0, done=5, pages_done=200),
                      _shard(1, done=2, pages_done=90, updated_ago=em.STALE_AFTER_S + 60)])
    states = {s["shard"]: s["state"] for s in p["shards"]}
    assert states == {0: "running", 1: "stale"}
    assert p["shards_live"] == 1, "a stale shard must not be counted as live"


def test_a_long_document_is_not_mistaken_for_a_stuck_worker():
    """A 281-page memorandum legitimately takes ~40 minutes on one GPU. Calling that stuck would
    cry wolf on precisely the biggest documents in the corpus."""
    p = em.summarize([_shard(0, done=1, pages_done=281, updated_ago=35 * 60,
                             current={"product": "155_Data_Privacy", "label": "Alaska__175822",
                                      "pages": 281, "started_at": time.time() - 35 * 60})])
    assert p["shards"][0]["state"] == "running"
    assert p["shards"][0]["current_seconds"] >= 35 * 60 - 5


def test_progress_rate_and_eta_come_from_pages_not_documents():
    """Documents run 4 to 286 pages here, so a documents-per-hour rate gives a useless ETA.

    One shard, 3,000 of 6,000 pages in one hour -> 3,000 pages/hour -> about an hour left."""
    p = em.summarize([_shard(0, done=20, pages_done=3000, pages_total=6000, started_ago=3600)])
    assert 2900 <= p["pages_per_hour"] <= 3100
    assert 3400 <= p["eta_seconds"] <= 3800


def test_eta_is_withheld_until_there_is_enough_signal():
    """An ETA extrapolated from the first few seconds is noise presented as fact."""
    p = em.summarize([_shard(0, done=0, pages_done=0, started_ago=20, updated_ago=1)])
    assert p["pages_per_hour"] is None and p["eta_seconds"] is None


def test_gates_are_merged_and_failures_surface():
    p = em.summarize([_shard(0, done=9, gates={"pass": 7, "review": 2}, failed=1),
                      _shard(1, done=5, gates={"pass": 3, "fail": 2})])
    assert p["gates"] == {"pass": 10, "review": 2, "fail": 2}
    assert p["failed"] == 1


def test_a_finished_shard_reads_complete_and_the_run_follows():
    done = [_shard(i, done=100, jobs=100, pages_done=5000) for i in range(2)]
    p = em.summarize(done)
    assert {s["state"] for s in p["shards"]} == {"complete"}
    assert p["state"] == "complete"
    assert p["percent"] == 100.0


def test_a_worker_draining_on_a_spot_notice_reads_stopping_and_stays_live():
    """It is still working — finishing the document in flight — so it must not read as complete."""
    p = em.summarize([_shard(0, done=4, pages_done=200, stopping=True)])
    assert p["shards"][0]["state"] == "stopping"
    assert p["shards_live"] == 1 and p["state"] == "running"


def test_recent_completions_are_newest_first_across_all_shards():
    now = time.time()
    a = _shard(0, done=1)
    b = _shard(1, done=1)
    a["recent"] = [{"ts": now - 300, "product": "p", "label": "old", "gate": "pass"}]
    b["recent"] = [{"ts": now - 5, "product": "p", "label": "new", "gate": "review"}]
    p = em.summarize([a, b])
    assert [r["label"] for r in p["recent"]] == ["new", "old"]


# ---------------------------------------------------------------------------
# Multiple versions in production: each run is a version, versions are resumed,
# and a version may be relaunched with a different number of workers.
# ---------------------------------------------------------------------------

def _versioned(idx, *, shards=8, done=0, jobs=10, pages_done=0, pages_total=500,
               corpus_docs=891, corpus_pages=61214, remaining_docs=None,
               remaining_pages=None, updated_ago=5):
    """A shard summary that also carries the version's totals."""
    now = time.time()
    return {"run": "2026-08-19", "shard": idx, "shards": shards,
            "done": done, "jobs_total": jobs, "extracted": done, "cloned": 0, "failed": 0,
            "pages_done": pages_done, "pages_total": pages_total, "gates": {}, "current": None,
            "recent": [], "started_at": now - 3600, "updated_at": now - updated_ago,
            "stopping": False,
            "corpus_docs": corpus_docs, "corpus_pages": corpus_pages,
            "remaining_docs": corpus_docs if remaining_docs is None else remaining_docs,
            "remaining_pages": corpus_pages if remaining_pages is None else remaining_pages}


def test_a_fresh_version_reports_the_same_attempt_and_version_progress():
    """Nothing done before, so the two percentages agree."""
    p = em.summarize([_versioned(0, pages_done=6121, pages_total=30607),
                      _versioned(1, pages_done=0, pages_total=30607)])
    assert p["version"]["pages_total"] == 61214
    assert p["version"]["resumed_from"] == 0
    assert p["version"]["percent"] == 10.0
    assert p["percent"] == 10.0


def test_a_resumed_version_is_not_reported_as_starting_from_zero():
    """The case that would mislead an operator most.

    A version 95% extracted is relaunched; only 45 documents / 3,000 pages remain. The attempt has
    barely begun, but the VERSION is nearly done — and "is this version ready" is the question
    people actually ask the screen."""
    p = em.summarize([_versioned(0, done=1, jobs=45, pages_done=100, pages_total=3000,
                                 remaining_docs=45, remaining_pages=3000)])
    assert p["percent"] < 5, "the attempt has hardly started"
    assert p["version"]["percent"] > 95, "but the version is nearly complete"
    assert p["version"]["resumed_from"] == 891 - 45
    assert p["version"]["docs_done"] == 891 - 45 + 1


def test_version_progress_is_withheld_rather_than_faked_for_older_runs():
    """Progress objects written before version tracking have no corpus totals. Showing 0% there
    would be a confident lie; None lets the screen say "unknown"."""
    p = em.summarize([_shard(0, done=3, pages_done=120)])
    assert p["version"]["percent"] is None
    assert p["version"]["pages_total"] is None


def test_relaunching_a_version_with_fewer_workers_ignores_the_old_shard_objects():
    """8 workers yesterday, 4 today because that is the spot capacity available.

    shard-4..7 from the 8-way run still sit in S3 and describe a DIFFERENT partition. Counting
    them would double the document totals and invent four workers that are permanently stale."""
    old8 = [_versioned(i, shards=8, done=10, jobs=10, pages_done=500, updated_ago=86400)
            for i in range(8)]
    new4 = [_versioned(i, shards=4, done=2, jobs=30, pages_done=200, updated_ago=10)
            for i in range(4)]
    p = em.summarize(old8 + new4)
    assert len(p["shards"]) == 4, "only the current layout is shown"
    assert p["superseded_workers"] == 8
    assert p["done"] == 8 and p["jobs_total"] == 120
    assert {r["state"] for r in p["shards"]} == {"running"}


def test_the_newest_layout_wins_regardless_of_worker_count_direction():
    """Scaling UP must work the same way as scaling down."""
    old2 = [_versioned(i, shards=2, done=5, jobs=5, updated_ago=7200) for i in range(2)]
    new6 = [_versioned(i, shards=6, done=1, jobs=20, updated_ago=15) for i in range(6)]
    p = em.summarize(old2 + new6)
    assert len(p["shards"]) == 6 and p["superseded_workers"] == 2


def test_a_shard_index_outside_its_declared_layout_is_dropped():
    """Defensive: a stray shard-9 in a 4-worker layout is not a worker, it is debris."""
    live, sup = em.current_topology([_versioned(i, shards=4) for i in range(4)]
                                    + [_versioned(9, shards=4)])
    assert sorted(s["shard"] for s in live) == [0, 1, 2, 3]
    assert sup == []


def test_versions_are_listed_cheaply_and_ordered_by_real_activity(monkeypatch):
    """Ordering by name would put "baseline-v2" above "2026-08-19" and bury the live run.

    The listing must also stay SCOPED. Walking <root>/ recursively and filtering in Python
    examined every object in the corpus area to find a handful — measured 80,935 keys for 18
    progress objects, 28 seconds — and a finished document adds ~150 objects, so the screen got
    slower every hour a run progressed. A delimiter listing plus one small per-run listing is
    1 + N calls instead.
    """
    from datetime import datetime, timezone

    def at(mins):
        return datetime.fromtimestamp(1_760_000_000 - mins * 60, tz=timezone.utc)

    per_run = {
        "corpus/2026-08-19/_progress/": [
            {"key": "corpus/2026-08-19/_progress/shard-0.json", "size": 800, "last_modified": at(600)},
            {"key": "corpus/2026-08-19/_progress/shard-1.json", "size": 800, "last_modified": at(590)},
            {"key": "corpus/2026-08-19/_progress/notes.json", "size": 10, "last_modified": at(1)},
        ],
        "corpus/baseline-v2/_progress/": [
            {"key": "corpus/baseline-v2/_progress/shard-0.json", "size": 800, "last_modified": at(3)},
        ],
        "corpus/_internal/_progress/": [
            {"key": "corpus/_internal/_progress/shard-0.json", "size": 800, "last_modified": at(1)},
        ],
    }
    asked: list[str] = []

    class _S3:
        def __init__(self, **kw): pass

        def list_common_prefixes(self, prefix):
            asked.append(prefix)
            return ["corpus/2026-08-19/", "corpus/baseline-v2/", "corpus/_internal/"]

        def list_objects(self, prefix, suffix=None):
            asked.append(prefix)
            return per_run.get(prefix, [])

    monkeypatch.setattr(em, "ReadOnlyS3", _S3)
    runs = em.list_runs("bucket", "corpus", "eu-west-1")

    assert [r["run"] for r in runs] == ["baseline-v2", "2026-08-19"], "newest activity first"
    assert runs[1]["workers_reported"] == 2, "notes.json is not a worker"
    assert all(not r["run"].startswith("_") for r in runs), "internal prefixes are not runs"

    # The performance contract: nothing is ever listed at the root without a _progress/ scope, so
    # the cost cannot grow with the number of documents extracted.
    assert asked[0] == "corpus/", "the run list should start from a delimiter listing"
    assert all(a == "corpus/" or a.endswith("/_progress/") for a in asked), asked
    assert "corpus/_internal/_progress/" not in asked, "an internal prefix should not be fetched"


# ---------------------------------------------------------------------------
# LIVENESS. The screen's job is to describe what is happening NOW, and the only
# evidence a worker still exists is that it wrote something recently. Flags the
# worker set on its way out describe an intention, not a running process.
# ---------------------------------------------------------------------------

def test_a_worker_that_announced_stopping_and_then_died_does_not_stay_live():
    """The real failure: run 2026-08-20-08 read RUNNING with 8/8 workers live, 99h after the
    last worker wrote anything, because `stopping` was tested before staleness."""
    p = em.summarize([_shard(0, done=4, jobs=100, stopping=True,
                             updated_ago=99 * 3600, started_ago=100 * 3600)])
    assert p["shards"][0]["state"] == "stale"
    assert p["shards_live"] == 0
    assert p["state"] == "stalled"


def test_a_run_whose_workers_vanished_unfinished_is_stalled_not_complete():
    """Spot capacity reclaimed mid-run. Calling this 'complete' hides unfinished work."""
    p = em.summarize([_shard(0, done=10, jobs=100, updated_ago=99 * 3600),
                      _shard(1, done=20, jobs=100, updated_ago=99 * 3600)])
    assert p["state"] == "stalled" and p["shards_live"] == 0


def test_every_shard_finishing_its_partition_is_still_complete():
    """Staleness must not demote a run that genuinely finished — silence is expected there."""
    p = em.summarize([_shard(0, done=100, jobs=100, updated_ago=99 * 3600),
                      _shard(1, done=100, jobs=100, updated_ago=99 * 3600)])
    assert {r["state"] for r in p["shards"]} == {"complete"}
    assert p["state"] == "complete"


def test_a_stalled_run_reports_no_eta():
    """Nothing is working on it, so there is nothing to extrapolate from."""
    p = em.summarize([_shard(0, done=10, jobs=100, pages_done=500, pages_total=5000,
                             updated_ago=99 * 3600, started_ago=100 * 3600)])
    assert p["eta_seconds"] is None


def test_a_dead_runs_elapsed_clock_stops_at_its_last_activity():
    """Measuring to now() kept the wall clock of an abandoned run climbing forever, which also
    decayed pages/hour toward zero the longer the corpse sat there."""
    p = em.summarize([_shard(0, done=10, jobs=100, pages_done=500,
                             started_ago=100 * 3600, updated_ago=99 * 3600)])
    assert 3500 < p["elapsed_seconds"] < 3700, p["elapsed_seconds"]   # ~1h of real work, not 100h


def test_a_live_run_still_measures_elapsed_to_now():
    p = em.summarize([_shard(0, done=10, jobs=100, pages_done=500,
                             started_ago=3600, updated_ago=5)])
    assert 3590 < p["elapsed_seconds"] <= 3605
    assert p["state"] == "running" and p["eta_seconds"] is not None


def test_last_activity_is_reported_so_a_stalled_run_can_be_dated():
    p = em.summarize([_shard(0, done=1, updated_ago=99 * 3600)])
    assert p["last_activity"] == pytest.approx(time.time() - 99 * 3600, abs=5)


def test_the_silence_behind_the_state_is_published_not_just_the_verdict():
    """A screen that reports only its conclusion is trusted exactly as far as its threshold is
    right — and this one's was 45x too generous for two days. The gap since the last beat is what
    lets an operator see a dead worker before the threshold agrees."""
    p = em.summarize([_shard(0, done=1, updated_ago=880)])
    assert p["state"] == "running", "880s is still inside the staleness threshold"
    assert p["quiet_seconds"] == pytest.approx(880, abs=5), "...but the silence is visible anyway"


def test_quiet_seconds_is_measured_from_the_freshest_worker_not_the_stalest():
    """One live worker among seven corpses is a live run, and its beat is the run's beat."""
    p = em.summarize([_shard(0, done=1, updated_ago=30),
                      _shard(1, done=1, updated_ago=99 * 3600)])
    assert p["quiet_seconds"] == pytest.approx(30, abs=5)


def test_a_run_with_no_shards_reports_no_silence_rather_than_zero():
    """Zero would read as 'beating right now', which is the opposite of 'nothing has ever run'."""
    assert em.summarize([])["quiet_seconds"] is None
