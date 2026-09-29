"""The job list: the run, document by document.

The shard summaries say how far along a run is. They cannot say WHICH document was slow, because
`recent` is a ten-document tail by design — four hours later the slow one has fallen off it. So
each worker also appends one line per document to a ledger, and this is what reads it back.

Two properties are under test. The first is that the ledger is a job LIST, not a log: a retried
document and a re-run under a different worker layout must each collapse to one row, or the screen
double-counts the corpus. The second is that a crash keeps the stage it died in — a
CalledProcessError in stage2_mineru is a MinerU problem and the identical exception in stage1 is
not, and that distinction previously existed only in a pod log.
"""

import time

from aosphere_core_index.service import extraction_monitor as em


def _row(label, *, product="155_Data_Privacy", gate="pass", seconds=120.0, pages=40,
         tier="first_pass", ts=None, steps=None, stage=None, error=None, **extra):
    now = time.time()
    r = {"ts": now if ts is None else ts, "product": product, "label": label,
         "gate": gate, "seconds": seconds, "pages": pages, "tier": tier,
         "started_at": (now if ts is None else ts) - seconds,
         "steps": steps or {"stage1": 20.0, "stage2_mineru": 90.0, "stage3": 10.0}}
    if stage:
        r["stage"] = stage
    if error:
        r["error"] = error
    r.update(extra)
    return r


def _shard(idx, *, current=None, shards=4):
    return {"run": "2026-08-19", "shard": idx, "shards": shards, "current": current,
            "updated_at": time.time(), "started_at": time.time() - 3600}


# ---- the stage model -------------------------------------------------------------------------

def test_a_scored_document_has_reached_the_gate():
    """Scored means it passed through extraction and was graded — so it sits at the GATE.

    Not "the last stage in the list": STAGES now ends with the two opt-in AI stages
    (stage 4 post-processing, stage 5 sub-chunks) that most documents never enter, and
    anchoring on the end of the list claimed every scored document had run them.
    """
    v = em.jobs_view([_row("Alabama__175819")], [])
    assert v["jobs"][0]["stage_idx"] == em.GATE_INDEX
    assert v["jobs"][0]["stage_label"] == "Scorecard gate"


def test_the_optional_ai_stages_come_after_the_gate():
    keys = [s["key"] for s in em.STAGES]
    assert keys[em.GATE_INDEX] == "gate"
    assert keys[em.GATE_INDEX + 1:] == ["stage4_ai", "stage5_subchunk"]
    assert all(em.STAGES[i].get("optional") for i in range(em.GATE_INDEX + 1, len(em.STAGES)))


def test_a_finished_ai_run_advances_the_document_past_the_gate():
    """The only trace stages 4-5 leave in the ledger is the time they took."""
    v = em.jobs_view([_row("Bahamas__183503", steps={
        "stage1": 20.0, "stage2_mineru": 90.0, "stage3": 10.0,
        "stage4_ai": 484.0, "stage5_subchunk": 0.3})], [])
    assert v["jobs"][0]["stage_label"] == "Stage 5 \u00b7 Sub-chunks"


def test_stage_4_alone_advances_only_to_stage_4():
    v = em.jobs_view([_row("Bahamas__183503", steps={
        "stage1": 20.0, "stage4_ai": 484.0})], [])
    assert v["jobs"][0]["stage_label"] == "Stage 4 \u00b7 AI post-processing"


def test_a_run_the_flag_refused_stays_at_the_gate():
    """A recorded ZERO means stage 4 was asked for and gated off: nothing sent, nothing
    spent, no file changed. Crediting it with the stage would overstate what ran."""
    v = em.jobs_view([_row("Belgium__163341", steps={
        "stage1": 20.0, "stage4_ai": 0.0, "stage5_subchunk": 0.0})], [])
    assert v["jobs"][0]["stage_idx"] == em.GATE_INDEX


def test_a_crash_is_never_dragged_forward_by_ai_timings():
    """A document that died in stage 2 cannot also have finished stage 4; the crash stage
    is the whole point of recording it."""
    v = em.jobs_view([_row("Taiwan__179120", gate="error", stage="stage2_mineru",
                           steps={"stage1": 20.0, "stage4_ai": 484.0})], [])
    assert v["jobs"][0]["stage_label"] == "Stage 2 \u00b7 Tables (MinerU)"


def test_every_ai_step_name_resolves_to_its_own_stage():
    """The map is hand-written, and a missing step name falls to index 1 -- which once made
    a document that finished stage 4 report "Stage 1 - Extract"."""
    assert em.stage_index("stage4_ai") == em.GATE_INDEX + 1
    assert em.stage_index("stage5_subchunk") == em.GATE_INDEX + 2


def test_a_crash_is_placed_at_the_stage_it_died_in():
    """The single most useful fact about a failure, and the one the exception never carries."""
    v = em.jobs_view([_row("Taiwan__179120", gate="error", stage="stage2_mineru",
                           error="CalledProcessError: mineru")], [])
    j = v["jobs"][0]
    assert j["stage"] == "stage2_mineru"
    assert j["stage_label"] == "Stage 2 · Tables (MinerU)"
    assert j["status"] == "error" and j["gate"] is None


def test_a_fallback_tier_is_placed_on_the_stage_it_re_runs():
    """mineru_full redoes Stage 2. Giving it a stage of its own would count one document twice
    in the funnel."""
    assert em.stage_index("mineru_full") == em.stage_index("stage2_mineru")
    assert em.stage_index("toc_rescue") == em.stage_index("stage1")


def test_an_unknown_stage_name_does_not_land_at_queued():
    """A document reporting something has certainly been fetched — calling it 'queued' would put
    a working document behind one that has not started."""
    assert em.stage_index("some_new_step") == 1
    assert em.stage_index(None) == 0


# ---- the job list ----------------------------------------------------------------------------

def test_the_document_in_flight_leads_the_list_and_is_not_duplicated():
    """A running document is the row an operator most wants to see. It is written to the ledger
    only once finished, so it can never appear twice."""
    cur = {"product": "155_Data_Privacy", "label": "Alabama__175819", "pages": 97,
           "started_at": time.time() - 600, "stage": "stage2_mineru",
           "stage_at": time.time() - 300}
    v = em.jobs_view([_row("Taiwan__179120")], [_shard(0, current=cur)], now=time.time())
    assert v["running"] == 1
    assert v["jobs"][0]["label"] == "Alabama__175819"
    assert v["jobs"][0]["live"] is True
    assert v["jobs"][0]["seconds"] >= 595, "a running document reports wall clock so far"
    assert v["jobs"][0]["stage_seconds"] >= 295
    assert [j["label"] for j in v["jobs"]].count("Alabama__175819") == 1


def test_a_running_document_claims_no_tier():
    """Nothing has been scored, so no tier has been ADOPTED. 'first pass' would be a claim."""
    cur = {"product": "p", "label": "X__1", "started_at": time.time(), "stage": "stage1"}
    j = em.jobs_view([], [_shard(0, current=cur)])["jobs"][0]
    assert j["tier"] is None and j["tier_label"] is None


def test_a_ledger_row_is_superseded_by_the_running_copy_of_the_same_document():
    """A retry re-runs a document that is already in the ledger. While it runs, the row must be
    the LIVE one, not the stale result it is replacing."""
    old = _row("Alabama__175819", gate="fail", ts=time.time() - 7200)
    cur = {"product": "155_Data_Privacy", "label": "Alabama__175819",
           "started_at": time.time() - 30, "stage": "stage1"}
    v = em.jobs_view([old], [_shard(0, current=cur)])
    assert len(v["jobs"]) == 1
    assert v["jobs"][0]["live"] is True


def test_the_funnel_counts_documents_at_or_past_each_stage():
    ledger = [_row("A__1"), _row("B__2"),
              _row("C__3", gate="error", stage="stage1", error="boom")]
    v = em.jobs_view(ledger, [])
    counts = {f["key"]: f["count"] for f in v["funnel"]}
    assert counts["queued"] == 3, "every document was at least fetched"
    assert counts["stage1"] == 3
    assert counts["stage2"] == 2, "the one that died in stage 1 never reached stage 2"
    assert counts["gate"] == 2


def test_an_empty_run_is_not_an_error():
    v = em.jobs_view([], [])
    assert v["jobs"] == [] and v["total"] == 0 and v["ledger"] is False


def test_a_run_with_no_ledger_says_so_rather_than_looking_empty():
    """Runs extracted before the workers wrote a ledger have no job list. An empty table would
    read as though the run had extracted nothing."""
    assert em.jobs_view([], [])["ledger"] is False
    assert em.jobs_view([_row("A__1")], [])["ledger"] is True


# ---- what each row carries -------------------------------------------------------------------

def test_the_adopted_tier_is_carried_with_its_label():
    j = em.jobs_view([_row("UK__85598", tier="mineru_full", fallback=True)], [])["jobs"][0]
    assert j["tier"] == "mineru_full"
    assert j["tier_label"] == "⟲ MinerU full"
    assert j["fallback"] is True


def test_a_tier_that_ran_and_lost_is_distinguishable_from_one_that_was_adopted():
    """run_one reports adopted_tier 'stage1' when a tier ran and the first pass still won. The
    screen has to be able to tell those apart, or it claims a tree that was thrown away."""
    j = em.jobs_view([_row("UK__1", tier="stage1", fallback=True)], [])["jobs"][0]
    assert j["tier"] == "stage1" and j["tier_label"] == "⟲ fallback tried"


def test_per_stage_timings_survive_to_the_row():
    """The whole point: where a slow document spent its time, without reading its scorecard."""
    j = em.jobs_view([_row("A__1", steps={"stage1": 12.0, "stage2_mineru": 300.0},
                           slowest="stage2_mineru", spp=3.2, fb_seconds=33.0)], [])["jobs"][0]
    assert j["steps"]["stage2_mineru"] == 300.0
    assert j["slowest"] == "stage2_mineru"
    assert j["spp"] == 3.2 and j["fb_seconds"] == 33.0


def test_a_failure_is_classified_on_the_row():
    """So the screen never has to parse a traceback — the same rule the failure logs use."""
    j = em.jobs_view([_row("A__1", gate="error", stage="stage2_mineru",
                           error="torch.OutOfMemoryError: CUDA out of memory")], [])["jobs"][0]
    assert j["cause"] == "gpu_oom"


# ---- deduplication: the ledger is a job list, not a log --------------------------------------

def _ledger_bytes(rows):
    import json
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


class _FakeS3:
    """Just enough ReadOnlyS3 to exercise read_ledger's merging."""

    def __init__(self, files):
        self.files = files

    def list_keys(self, prefix, suffix=None):
        return [k for k in self.files
                if k.startswith(prefix) and (suffix is None or k.endswith(suffix))]

    def get_bytes(self, key):
        return self.files[key]


def _read_with(monkeypatch, files):
    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _FakeS3(files))
    return em.read_ledger("b", "corpus/run", "eu-west-2")


def test_a_retried_document_appears_once_with_its_newest_result(monkeypatch):
    """A retry re-extracts a document already in the ledger. Two rows for one document would
    double-count the corpus and show the result that was replaced."""
    old = _row("Alabama__175819", gate="fail", ts=1000.0)
    new = _row("Alabama__175819", gate="pass", ts=2000.0)
    rows = _read_with(monkeypatch, {"corpus/run/_progress/ledger-0.jsonl":
                                    _ledger_bytes([old, new])})
    assert len(rows) == 1 and rows[0]["gate"] == "pass"


def test_an_earlier_worker_layouts_ledger_is_kept_not_discarded(monkeypatch):
    """Unlike the shard SUMMARIES, an old layout's ledger holds real documents this version
    extracted. Dropping it would empty the job list of everything a previous attempt finished."""
    files = {"corpus/run/_progress/ledger-0.jsonl": _ledger_bytes([_row("A__1", ts=1000.0)]),
             "corpus/run/_progress/ledger-7.jsonl": _ledger_bytes([_row("B__2", ts=900.0)])}
    rows = _read_with(monkeypatch, files)
    assert {r["label"] for r in rows} == {"A__1", "B__2"}


def test_the_same_document_in_two_shard_ledgers_collapses_to_the_newest(monkeypatch):
    """A re-run with fewer workers repartitions the corpus, so two ledgers can hold one
    document. The newest row describes the tree actually on disk."""
    files = {"corpus/run/_progress/ledger-0.jsonl":
             _ledger_bytes([_row("A__1", gate="pass", ts=2000.0)]),
             "corpus/run/_progress/ledger-6.jsonl":
             _ledger_bytes([_row("A__1", gate="review", ts=1000.0)])}
    rows = _read_with(monkeypatch, files)
    assert len(rows) == 1 and rows[0]["gate"] == "pass"


def test_a_half_written_final_line_does_not_lose_the_whole_shard(monkeypatch):
    """The ledger is re-uploaded per document, so a read can land mid-PUT. One unparseable line
    must cost one row, not the shard's entire history."""
    body = _ledger_bytes([_row("A__1"), _row("B__2")]) + b'{"product":"x","lab'
    rows = _read_with(monkeypatch, {"corpus/run/_progress/ledger-0.jsonl": body})
    assert {r["label"] for r in rows} == {"A__1", "B__2"}


def test_rows_come_back_newest_first(monkeypatch):
    files = {"corpus/run/_progress/ledger-0.jsonl":
             _ledger_bytes([_row("A__1", ts=1000.0), _row("B__2", ts=3000.0),
                            _row("C__3", ts=2000.0)])}
    assert [r["label"] for r in _read_with(monkeypatch, files)] == ["B__2", "C__3", "A__1"]


def test_a_shard_that_has_written_nothing_is_skipped(monkeypatch):
    class _Boom(_FakeS3):
        def get_bytes(self, key):
            if key.endswith("ledger-1.jsonl"):
                raise RuntimeError("NoSuchKey")
            return super().get_bytes(key)

    files = {"corpus/run/_progress/ledger-0.jsonl": _ledger_bytes([_row("A__1")]),
             "corpus/run/_progress/ledger-1.jsonl": b""}
    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _Boom(files))
    rows = em.read_ledger("b", "corpus/run", "eu-west-2")
    assert [r["label"] for r in rows] == ["A__1"]


# ---- the drill-down --------------------------------------------------------------------------

def test_job_detail_reads_exactly_one_scorecard(monkeypatch):
    """The one place this module reads a scorecard at all — and it must be ONE, on demand."""
    import json
    sc = {"gate": "review", "worst_score": 62,
          "timing": {"seconds": 304.2, "steps": {"stage2_mineru": 280.0, "stage1": 24.2},
                     "slowest_step": "stage2_mineru", "pages": 97, "seconds_per_page": 3.14},
          "fallback": {"triggered": True, "reason": "headings below threshold",
                       "adopted_tier": "mineru_full",
                       "chain": [{"tier": "toc_rescue", "adopted": False, "status": "rejected"},
                                 {"tier": "mineru_full", "adopted": True}]}}
    reads = []

    class _S3(_FakeS3):
        def get_bytes(self, key):
            reads.append(key)
            return json.dumps(sc).encode()

    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _S3({}))
    d = em.job_detail("b", "corpus/run", "eu-west-2", "155_Data_Privacy", "Alabama__175819")
    # Four bounded objects for the one document the reviewer opened, and nothing else. The rule
    # this guards is "never in bulk" -- 891 scorecards per page load is what took the service down
    # -- not "exactly one key", so the post-AI scorecard, the pre-flight record and stage 4's own
    # report all join it on the same on-demand path.
    assert reads == ["corpus/run/155_Data_Privacy/Alabama__175819/scorecard.json",
                     "corpus/run/155_Data_Privacy/Alabama__175819/scorecard_post_ai.json",
                     "corpus/run/155_Data_Privacy/Alabama__175819/toc_preflight.json",
                     "corpus/run/155_Data_Privacy/Alabama__175819/04_stage4_ai/stage4_report.json"]
    assert d["gate"] == "review" and d["worst_score"] == 62
    assert d["timing"]["slowest_step"] == "stage2_mineru"
    assert d["fallback"]["adopted_tier"] == "mineru_full"
    assert len(d["fallback"]["chain"]) == 2


def test_job_detail_prefers_the_post_ai_verdict_when_it_exists(monkeypatch):
    """scorecard.json is the --resume marker, frozen at stage 3. When stage 4/5 ran, the
    tree it left behind is what actually ships, and the gate/worst/dimensions a reviewer
    sees have to describe THAT, not the extraction gate from before the AI pass ran."""
    import json
    sc = {"gate": "review", "worst_score": 70.0, "dimensions": {"completeness": {"score": 70.0}},
          "timing": {"seconds": 120.0}, "fallback": {}}
    sc_post = {"gate": "pass", "worst_score": 95.0, "scored_stage": 5,
              "dimensions": {"completeness": {"score": 95.0}}}
    files = {
        "corpus/run/155_Data_Privacy/Alabama__175819/scorecard.json": json.dumps(sc).encode(),
        "corpus/run/155_Data_Privacy/Alabama__175819/scorecard_post_ai.json":
            json.dumps(sc_post).encode(),
    }
    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _FakeS3(files))
    d = em.job_detail("b", "corpus/run", "eu-west-2", "155_Data_Privacy", "Alabama__175819")
    assert d["gate"] == "pass" and d["worst_score"] == 95.0
    assert d["dimensions"]["completeness"]["score"] == 95.0
    assert d["scored_stage"] == 5
    # The extraction gate is not discarded -- "the AI pass moved this from review to pass"
    # is a fact the screen can state precisely because the old number travels alongside.
    assert d["extraction_gate"] == {"gate": "review", "worst_score": 70.0}
    # Fallback/timing describe how stage 3 was REACHED, which stage 4/5 does not change --
    # scorecard_post_ai.json carries neither, so these still have to come from stage 3's.
    assert d["timing"]["seconds"] == 120.0


def test_job_detail_falls_back_to_the_extraction_gate_when_stage_4_never_ran(monkeypatch):
    """Most documents never run stage 4/5 -- absence of scorecard_post_ai.json must read as
    exactly that, not as an error, and the extraction gate is the only verdict there is."""
    import json
    sc = {"gate": "pass", "worst_score": 96.0, "dimensions": {}}
    files = {"corpus/run/155_Data_Privacy/Alabama__175819/scorecard.json": json.dumps(sc).encode()}
    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _FakeS3(files))
    d = em.job_detail("b", "corpus/run", "eu-west-2", "155_Data_Privacy", "Alabama__175819")
    assert d["gate"] == "pass" and d["worst_score"] == 96.0
    assert d["scored_stage"] == 3
    assert d["extraction_gate"] is None


def test_job_detail_reports_whether_the_preflight_repaired_the_outline(monkeypatch):
    """The one thing the scorecard cannot say, and the trace needs to draw.

    The pre-flight rewrites the bookmark outline, re-extracts on the repaired PDF and gets out of
    the way, so afterwards a document it repaired and one it left alone are indistinguishable in
    the scores -- the re-scored `toc` dimension reads healthy either way. _preflight_outline writes
    toc_preflight.json ONLY when it applied a repair (or errored trying), so the file is the whole
    signal: present means Stage 1 ran twice."""
    import json
    sc = {"gate": "pass", "timing": {"steps": {"stage1": 7.1, "toc_preflight": 8.4}}}
    pf = {"applied": True, "trigger": "outline_disagrees_with_printed_toc",
          "trigger_detail": "92 bookmarks from p3", "engine": "prune",
          "toc_pages": [2, 3], "entries": 41, "verified": 38}

    class _S3(_FakeS3):
        def get_bytes(self, key):
            if key.endswith("toc_preflight.json"):
                return json.dumps(pf).encode()
            return json.dumps(sc).encode()

    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _S3({}))
    d = em.job_detail("b", "corpus/run", "eu-west-2", "p", "l")
    assert d["preflight"]["applied"] is True
    assert d["preflight"]["trigger"] == "outline_disagrees_with_printed_toc"
    assert d["preflight"]["entries"] == 41


def test_a_trusted_outline_leaves_no_preflight_record_rather_than_failing_the_read(monkeypatch):
    """Absence is the answer, not an error: most of the corpus has a healthy outline and pays
    nothing here, and a missing object must not take the whole drill-down down with it."""
    import json
    sc = {"gate": "pass", "timing": {"steps": {"stage1": 7.1, "toc_preflight": 0.3}}}

    class _S3(_FakeS3):
        def get_bytes(self, key):
            if key.endswith("toc_preflight.json"):
                raise RuntimeError("NoSuchKey")
            return json.dumps(sc).encode()

    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _S3({}))
    d = em.job_detail("b", "corpus/run", "eu-west-2", "p", "l")
    assert d["preflight"] is None and d["gate"] == "pass"


def test_job_detail_is_none_when_there_is_no_scorecard(monkeypatch):
    """A crashed or still-running document has none, and the screen must say so rather than 500."""
    class _S3(_FakeS3):
        def get_bytes(self, key):
            raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(em, "ReadOnlyS3", lambda **kw: _S3({}))
    assert em.job_detail("b", "corpus/run", "eu-west-2", "p", "l") is None


def test_job_detail_refuses_a_traversing_path():
    import pytest
    for bad in ("../secrets", "a/b"):
        with pytest.raises(ValueError):
            em.job_detail("b", "corpus/run", "eu-west-2", bad, "label")
        with pytest.raises(ValueError):
            em.job_detail("b", "corpus/run", "eu-west-2", "product", bad)


# ---- what a RUNNING document can already show ------------------------------------------------
# Everything below is available before the document finishes. It used to wait on the scorecard,
# which run_one writes last — so for the 40 minutes the largest documents take, the screen knew
# almost nothing at exactly the point the answer was most wanted.

def _live(**cur):
    base = {"product": "155_Data_Privacy", "label": "Alabama__175819", "pages": 281,
            "started_at": time.time() - 2400, "stage": "stage2_mineru",
            "stage_at": time.time() - 1200}
    base.update(cur)
    return _shard(0, current=base)


def test_stages_already_finished_are_reported_while_the_document_runs():
    """The worker closes each stage off as the next begins, so a document deep into Stage 2
    still shows what Stage 1 cost."""
    j = em.jobs_view([], [_live(steps={"stage1": 1180.5, "toc_preflight": 49.2})])["jobs"][0]
    assert j["steps"]["stage1"] == 1180.5
    assert j["slowest"] == "stage1"


def test_the_stage_in_flight_is_sized_but_marked_as_still_running():
    """It has no final duration, so it is named separately — a still-growing number shown
    beside measured ones would be read as measured."""
    j = em.jobs_view([], [_live(steps={"stage1": 100.0})])["jobs"][0]
    assert j["running_step"] == "stage2_mineru"
    assert j["stage_seconds"] >= 1195
    assert j["passes"][0]["steps"]["stage2_mineru"] >= 1195, "sized so its pass can be drawn"


def test_a_running_document_reports_its_rate_so_far():
    j = em.jobs_view([], [_live()])["jobs"][0]
    assert j["spp"] is not None and j["spp"] > 0


def test_a_second_pass_in_flight_is_split_from_the_completed_first():
    """The case the screen handled worst: the stage ticks back to work already done. Grouped by
    pass, a finished first pass and a running rescue are unmistakable."""
    j = em.jobs_view([], [_live(
        stage="toc_rescue", stage_at=time.time() - 40,
        steps={"stage1": 6.4, "stage2_mineru": 41.0, "stage3": 0.4, "validation": 5.1},
        fallback={"at": time.time() - 42, "reason": "completeness 41.2 < 60"})])["jobs"][0]
    keys = [p["key"] for p in j["passes"]]
    assert keys == ["first", "toc_rescue"]
    first, resc = j["passes"]
    assert abs(first["seconds"] - 52.9) < 0.1, "the completed first pass keeps its own total"
    assert resc["composite"] is True and resc["seconds"] >= 39
    assert j["running_step"] == "toc_rescue"


def test_the_first_passs_scores_are_available_the_moment_the_chain_escalates():
    """Long before this document writes a scorecard of its own — previously a running document
    showed no numbers at all, even though the pipeline had computed a full set."""
    j = em.jobs_view([], [_live(stage="toc_rescue", fallback={
        "at": time.time(), "reason": "completeness 41.2 < 60",
        "first": {"gate": "fail", "worst_score": 41.2, "weakest_dimension": "completeness",
                  "toc_score": 65.0, "toc_status": "lacking"}})])["jobs"][0]
    assert j["first_attempt"]["worst_score"] == 41.2
    assert j["first_attempt"]["toc_status"] == "lacking"


def test_a_document_with_no_completed_stage_yet_is_not_an_error():
    """The first seconds of a document: nothing has closed off, and the row must still render."""
    j = em.jobs_view([], [_live(stage="stage1", stage_at=time.time(), steps=None)])["jobs"][0]
    assert j["steps"] == {} and j["slowest"] is None
    assert j["passes"][0]["key"] == "first"


def test_a_finished_row_is_unaffected_by_any_of_this():
    j = em.jobs_view([_row("A__1")], [])["jobs"][0]
    assert j["live"] is False and j.get("running_step") is None


def test_the_funnel_row_itself_says_whether_a_stage_is_opt_in():
    """The screen renders from the funnel alone. Without the flag travelling on the row, an
    opt-in stage sitting at 0 is indistinguishable from a stage the whole run failed to
    reach — and the second reading is alarming rather than merely informative."""
    v = em.jobs_view([_row("Alabama__175819")], [])
    by_key = {f["key"]: f for f in v["funnel"]}
    assert by_key["stage4_ai"]["optional"] and by_key["stage5_subchunk"]["optional"]
    assert not by_key["gate"]["optional"] and not by_key["stage1"]["optional"]
    # and the opt-in rows must genuinely read zero for a document that never entered them
    assert by_key["stage4_ai"]["count"] == 0


# ---- what the run has spent on the two paid passes ----------------------------------------
# Summary-AI and stage 4 are the only routes that cost money, and both are opt-in, so most
# runs have priced nothing at all. That absence has to read as "nothing to report" rather
# than a confident "$0.00 spent" — the same distinction pages_per_hour already draws.

def test_the_run_total_is_none_when_nothing_has_been_priced():
    v = em.jobs_view([_row("Alabama__175819")], [])
    assert v["cost_usd_total"] is None


def test_the_run_total_sums_cost_across_jobs():
    v = em.jobs_view([_row("A__1", cost_usd=1.0092), _row("A__2", cost_usd=0.34),
                      _row("A__3")], [])
    assert v["cost_usd_total"] == round(1.0092 + 0.34, 4)


def test_an_unpriced_job_does_not_pull_the_total_toward_zero():
    """A row with no `cost_usd` at all is a document that never ran a paid pass — it must
    be excluded from the sum, not counted as $0."""
    v = em.jobs_view([_row("A__1", cost_usd=2.5), _row("A__2")], [])
    assert v["cost_usd_total"] == 2.5


def test_the_ai_time_total_is_seconds_inside_stage_4_alone():
    """`seconds` on the row is the WHOLE document (stages 1-3 included) — the AI-time total
    has to come from `steps.stage4_ai` specifically, or it would answer "how long did the
    run take" rather than "how long did the paid pass take"."""
    v = em.jobs_view([_row("A__1", steps={"stage1": 20.0, "stage4_ai": 484.0}),
                      _row("A__2", steps={"stage1": 6.0})], [])
    assert v["stage4_seconds_total"] == 484.0


def test_the_ai_time_total_is_none_when_stage_4_never_ran():
    v = em.jobs_view([_row("A__1")], [])
    assert v["stage4_seconds_total"] is None


# ---- which tree the row's gate/worst describe ---------------------------------------------
# run_one overrides gate/worst with the post-AI verdict when stage 4/5 ran (see
# run_corpus.run_one), so `scored_stage` on the row is what tells a reader whether they are
# looking at the extraction gate or the final, AI-processed verdict.

def test_scored_stage_passes_through_from_the_ledger_row():
    j = em._finished_job(_row("A__1", scored_stage=5))
    assert j["scored_stage"] == 5


def test_scored_stage_defaults_to_3_when_the_row_predates_the_field():
    """Every ledger row written before this field existed was extraction-gate-only by
    construction -- absence must read as 3, not as unknown."""
    j = em._finished_job(_row("A__1"))
    assert j["scored_stage"] == 3


# ---- the TOC rescue: a property of the document, not a tier ------------------------
# The rescue used to be a fallback tier and was labelled as one. It now happens in the
# pre-flight, BEFORE Stage 2 is paid for, so no tier runs and the document stays on its
# first pass -- and it stopped being labelled at the same time it stopped being a tier.
# Measured on out/corpus: 103 of 150 documents had their outline rebuilt from the printed
# contents page, and all 103 read as "first pass" with nothing to say so.

def test_a_toc_rescued_document_is_labelled():
    v = em.jobs_view([_row("Austria__137944", toc_rescued=True)], [])
    assert v["jobs"][0]["toc_rescued"] is True


def test_a_document_on_its_own_bookmarks_is_not_labelled():
    assert em.jobs_view([_row("Belgium__163341")], [])["jobs"][0]["toc_rescued"] is False


def test_the_rescue_does_not_claim_a_tier_ran():
    """It happens before Stage 2, so nothing was extracted twice and the pass is still
    the first one. Reporting it as a tier would overstate what the run did."""
    j = em.jobs_view([_row("Austria__137944", toc_rescued=True)], [])["jobs"][0]
    assert j["toc_rescued"] is True
    assert j["tier"] == "first_pass" and j["fallback"] is False


def test_a_finished_row_carries_both_verdicts():
    """SC1 and SC2 in one row, so the monitor can show the AI pass's EFFECT without opening a
    scorecard. The worker records the stage-3 pair beside the final one (corpus_worker.finished);
    this pins that the API passes both through under names the screen reads."""
    row = {"product": "124_MRAM", "label": "Jersey__181919", "gate": "pass", "worst": 94.6,
           "scored_stage": 5, "gate_extraction": "review", "worst_extraction": 87.1,
           "stage": "stage5_subchunk", "steps": {"total": 479.0}}
    job = em._finished_job(row)

    assert (job["gate"], job["worst"]) == ("pass", 94.6), "the final verdict must stay the row's"
    assert (job["gate_extraction"], job["worst_extraction"]) == ("review", 87.1)
    assert job["scored_stage"] == 5


def test_a_row_written_before_both_verdicts_were_recorded_says_so():
    """Absence is not zero and not a guess: a row from an older worker has no extraction verdict
    to show, and the screen must be able to tell that from 'stage 4/5 never ran' (scored_stage 3,
    where gate/worst ARE the extraction verdict)."""
    old = em._finished_job({"product": "p", "label": "l", "gate": "pass", "worst": 94.6,
                            "scored_stage": 5, "stage": "stage5_subchunk"})
    assert old["gate_extraction"] is None and old["worst_extraction"] is None

    no_ai = em._finished_job({"product": "p", "label": "l", "gate": "pass", "worst": 91.6,
                              "stage": "scorecard_gate"})
    assert no_ai["scored_stage"] == 3, "absence of the field means the extraction gate"
