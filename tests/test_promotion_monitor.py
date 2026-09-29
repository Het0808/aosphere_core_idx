"""The promotion screen's derived state — pure, and the part that has been got wrong before.

Extraction run 2026-08-20-08 sat dead for ninety-nine hours reading `stopping`, with an ETA
of 1010 hours. The worker had announced a drain and then been reclaimed, and the screen kept
honouring the announcement because it checked the FLAG before it checked whether anything had
written since. A flag records an intention at one instant; only `updated_at` is evidence that
the process is still there to act on it. That ordering is asserted here, on purpose, because
it is invisible in the code unless you know to look for it.

The other one worth pinning: Phase 2's stages are declared from day one so an operator can see
that "in the gallery" is not "searchable". If they counted toward `percent`, a COMPLETED
Phase-1 promotion would read 33% and look broken.

No I/O anywhere in this file. summarize() takes a dict and a clock.
"""
import pytest

pytest.importorskip("numpy")  # promotion_monitor -> doc_gallery -> config

from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402

NOW = 1_760_000_000.0


def state(**over):
    d = {"schema": PM.SCHEMA, "promotion_id": "r-p1", "run": "r", "version": "r-p1",
         "run_prefix": "corpus/r", "target_prefix": "index/r-p1/doc-gallery",
         "started_at": NOW - 600, "updated_at": NOW - 5, "stopping": False,
         "stages": {}, "counts": {"copied": 0, "skipped": 0, "failed": 0},
         "recent": [], "gates": {}}
    d.update(over)
    return d


def rows(st, now=NOW):
    return {r["key"]: r for r in PM.stage_rows(st, now)}


def all_phase1_done(**over):
    stages = {k: {"started_at": NOW - 500, "finished_at": NOW - 10, "done": 1, "total": 1}
              for k in PM.PHASE1_STAGES}
    return state(stages=stages, **over)


# ---------------- nothing at all ----------------
def test_no_state_is_idle_not_broken():
    s = PM.summarize(None, NOW)
    assert s["state"] == "idle"
    assert s["percent"] == 0.0
    assert s["eta_seconds"] is None
    assert s["current_stage"] is None


def test_every_declared_stage_is_reported_even_before_anything_runs():
    r = rows(None)
    assert list(r) == [m["key"] for m in PM.STAGES]
    assert all(r[k]["state"] == "pending" for k in PM.PHASE1_STAGES)


# ---------------- the ladder, in order ----------------
def test_a_finished_stage_reads_complete_however_long_ago_it_wrote():
    """Rule 3 before the staleness rule: a finished stage is not stale, it is finished."""
    st = state(updated_at=NOW - 10 * 3600,
               stages={"select": {"started_at": NOW - 10 * 3600,
                                  "finished_at": NOW - 10 * 3600, "done": 1, "total": 1}})
    assert rows(st)["select"]["state"] == "complete"


def test_silence_outranks_a_stopping_flag():
    """THE 2026-08-20-08 LESSON. A process that announced a drain and then died must read
    `stale`, not `stopping` — otherwise it counts as live forever."""
    st = state(stopping=True, updated_at=NOW - PM.PROMOTION_STALE_AFTER_S - 60,
               stages={"copy": {"started_at": NOW - 4000, "done": 431, "total": 612}})
    assert rows(st)["copy"]["state"] == "stale"
    assert PM.summarize(st, NOW)["state"] == "stalled"


def test_a_live_promotion_that_announced_a_drain_reads_stopping():
    st = state(stopping=True, updated_at=NOW - 3,
               stages={"copy": {"started_at": NOW - 400, "done": 10, "total": 612}})
    assert rows(st)["copy"]["state"] == "stopping"
    assert PM.summarize(st, NOW)["state"] == "stopping"


def test_a_failed_stage_beats_everything_including_full_counters():
    st = state(stages={"manifest": {"started_at": NOW - 50, "done": 1, "total": 1,
                                    "state": "failed"}})
    assert rows(st)["manifest"]["state"] == "failed"
    assert PM.summarize(st, NOW)["state"] == "failed"


def test_the_staleness_threshold_is_five_minutes_not_extractions_forty_five():
    """A promotion's unit of work is one GET and three server-side copies. Extraction's
    45 minutes is generous because a 281-page MinerU document really takes that long;
    nothing here legitimately takes five."""
    assert PM.PROMOTION_STALE_AFTER_S == 5 * 60
    st = state(updated_at=NOW - 4 * 60,
               stages={"copy": {"started_at": NOW - 500, "done": 5, "total": 10}})
    assert rows(st)["copy"]["state"] == "running"
    st = state(updated_at=NOW - 6 * 60,
               stages={"copy": {"started_at": NOW - 500, "done": 5, "total": 10}})
    assert rows(st)["copy"]["state"] == "stale"


def test_the_conclusion_always_travels_with_its_evidence():
    """A screen that reports only its verdict is trusted exactly as far as its threshold
    is — and the extraction monitor's was 45x too generous for two days."""
    s = PM.summarize(state(updated_at=NOW - 42), NOW)
    assert s["quiet_seconds"] == pytest.approx(42, abs=0.5)
    assert s["stale_after_seconds"] == PM.PROMOTION_STALE_AFTER_S


# ---------------- phase 2 ----------------
def test_phase_two_stages_are_declared_but_do_not_count_against_progress():
    s = PM.summarize(all_phase1_done(), NOW)
    assert s["percent"] == 100.0, "a finished Phase 1 must not read 33%"
    assert s["state"] == "complete"
    r = rows(all_phase1_done())
    assert {r[m["key"]]["state"] for m in PM.STAGES if m["phase"] == 2} == {"not_implemented"}
    assert all(not r[m["key"]]["implemented"] for m in PM.STAGES if m["phase"] == 2)


def test_a_phase_two_stage_that_starts_reporting_joins_the_percentage():
    """The extensibility hook: Phase 2 needs no change to summarize(), only records."""
    st = all_phase1_done()
    st["stages"]["content"] = {"started_at": NOW - 30, "done": 40, "total": 320}
    r = rows(st)
    assert r["content"]["implemented"] and r["content"]["state"] == "running"
    assert PM.summarize(st, NOW)["percent"] < 100.0


@pytest.mark.parametrize("meta", PM.STAGES, ids=[m["key"] for m in PM.STAGES])
def test_summarize_is_driven_by_the_stage_table_not_by_hardcoded_keys(meta):
    """Adding a stage must be an edit to STAGES and nothing else."""
    st = state(stages={meta["key"]: {"started_at": NOW - 20, "done": 2, "total": 4}})
    r = rows(st)[meta["key"]]
    assert r["state"] == "running" and r["percent"] == 50.0
    assert r["label"] == meta["label"] and r["phase"] == meta["phase"]


# ---------------- rate, ETA, and the clock ----------------
def test_no_eta_is_offered_when_nothing_is_live():
    st = all_phase1_done()
    st["updated_at"] = NOW - 99 * 3600
    assert PM.summarize(st, NOW)["eta_seconds"] is None


def test_the_clock_stops_at_the_last_write_for_a_promotion_that_is_not_running():
    """Extending elapsed to now for a dead process is how an ETA reaches 1010 hours."""
    st = all_phase1_done()
    st["started_at"], st["updated_at"] = NOW - 3600, NOW - 3000
    assert PM.summarize(st, NOW)["elapsed_seconds"] == pytest.approx(600, abs=1)


def test_progress_may_go_DOWN_between_polls_and_that_is_not_a_bug():
    """Resume RE-DERIVES the plan rather than reading plan.json back, because a run only
    ever grows: a second attempt legitimately sees more finished documents, so copy.total
    can increase and the percentage can fall. Anything that assumes monotonic percent will
    read this as a bug later."""
    a = state(stages={"copy": {"started_at": NOW - 100, "done": 400, "total": 400}})
    b = state(stages={"copy": {"started_at": NOW - 100, "done": 400, "total": 700}})
    assert PM.summarize(a, NOW)["percent"] > PM.summarize(b, NOW)["percent"]


def test_a_requested_promotion_is_not_the_same_as_no_promotion():
    """"waiting for a promoter" is a real status; reporting it as absent sends an operator
    hunting for a job that was never going to exist."""
    assert PM.summarize(None, NOW)["state"] == "idle"


# ---------------- identity ----------------
@pytest.mark.parametrize("v, ok", [
    ("2026-08-21-02-p1", True), ("baseline-v2-p1", True), ("a", True),
    ("", False), ("Has-Capitals", False), ("has space", False), ("-leading", False),
    ("has/slash", False), ("has:colon", False), ("has+plus", False), ("x" * 60, False),
])
def test_a_version_must_be_legal_as_an_opensearch_index_suffix(v, ok):
    """It names index/<v>/, index/<v>/doc-gallery/ AND aci-vectors-<v>. A version that
    publishes to S3 happily and then fails at indices.create forty minutes later is the
    worst possible moment to find out, so it is rejected at mint time."""
    assert PM.valid_version(v) is ok


def test_the_default_version_is_the_run_plus_an_attempt():
    """The run id is the only string that already identifies the CONTENT; the attempt
    suffix lets a second promotion exist without colliding with the first one's prefix."""
    assert PM.default_version("2026-08-21-02") == "2026-08-21-02-p1"
    assert PM.default_version("2026-08-21-02", 2) == "2026-08-21-02-p2"
    assert PM.valid_version(PM.default_version("2026-08-21-02", 2))


def test_progress_lives_under_the_run_so_a_403_on_the_target_still_leaves_a_record():
    rp = "corpus/2026-08-21-02"
    for key in (PM.state_key(rp, "v"), PM.plan_key(rp, "v"), PM.ledger_key(rp, "v"),
                PM.failures_key(rp, "v"), PM.lease_key(rp, "v"), PM.request_key(rp, "v"),
                PM.stop_key(rp, "v")):
        assert key.startswith(f"{rp}/_promotion/v/")
    # `_`-prefixed names cannot be addressed as runs — the guard that protects _progress.
    from aosphere_core_index.service.doc_gallery import valid_run
    assert not valid_run(PM.PROMOTION_DIR)
