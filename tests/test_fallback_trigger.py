"""WHY a document escalated to the fallback chain.

The chain has recorded this all along, but in two shapes and never anywhere the screen looked, so
"it fell back" arrived with no cause attached — and the cause is the half you can act on. A
document rescued because its printed TOC was lacking needs a different fix from one that lost two
thirds of its words on the first pass.

The two shapes are not interchangeable, which is the trap this module exists to avoid:

    normal path   fallback["first_attempt"]["entry_reason"] — semicolon-joined score comparisons
    short doc     fallback["reason"]                        — set when a memo skips tier 2

Reading only the first explains 4 of the current corpus's 18 fallbacks and blanks the other 14.
"""

from aosphere_core_index.service import extraction_monitor as em


def _fb(**kw):
    base = {"triggered": True, "chain": [], "adopted_tier": "mineru_full"}
    base.update(kw)
    return base


# ---- reading both entrances --------------------------------------------------------------

def test_the_normal_path_reason_is_read_from_first_attempt():
    t = em.fallback_trigger(_fb(first_attempt={"entry_reason": "completeness 35.1 < 60"}))
    assert [c["cause"] for c in t["causes"]] == ["completeness"]
    assert t["primary"]["score"] == 35.1 and t["primary"]["threshold"] == 60


def test_the_short_document_path_reason_is_read_from_reason():
    """14 of 18 real fallbacks take this path — entry_reason is None for every one of them."""
    t = em.fallback_trigger(_fb(
        reason="short document (6 pages): routed straight to MinerU, structural tiers skipped",
        first_attempt={"entry_reason": None}))
    assert t["primary"]["cause"] == "short_doc"
    assert t["primary"]["label"] == "too short for a TOC rescue"


def test_a_document_that_never_escalated_has_no_trigger():
    assert em.fallback_trigger({"triggered": False}) is None
    assert em.fallback_trigger(None) is None


# ---- the causes themselves ---------------------------------------------------------------

def test_every_clause_of_a_multi_cause_reason_survives():
    """needs_help joins them with semicolons; showing only the first hides real problems."""
    t = em.fallback_trigger(_fb(first_attempt={
        "entry_reason": "completeness 35.1 < 60; toc 65.0 < 70; sectioning 40.0 < 70"}))
    assert [c["cause"] for c in t["causes"]] == ["completeness", "toc", "sectioning"]


def test_a_scored_clause_keeps_its_numbers():
    """"TOC unusable" alone does not say whether it missed by a point or by sixty."""
    c = em.trigger_causes("toc 65.0 < 70")[0]
    assert c["score"] == 65.0 and c["threshold"] == 70.0
    assert c["detail"] is None, "the numbers are structured; repeating the raw clause is noise"


def test_a_prose_clause_keeps_its_explanation():
    """The one-chunk diagnosis carries the numbers that matter inside the sentence."""
    c = em.trigger_causes("CONTENT ALL IN ONE CHUNK — 82% of this document's words sit in a "
                          "single chunk, and 28 of 45 chunks are under 20 words")[0]
    assert c["cause"] == "one_chunk"
    assert "82%" in c["detail"], "a prose cause must keep the detail it carries"


def test_the_primary_cause_is_the_furthest_below_its_bar():
    """needs_help emits clauses in a fixed dimension order, so 'first' is an artefact of that
    order rather than the worst problem."""
    t = em.fallback_trigger(_fb(first_attempt={
        "entry_reason": "completeness 58.0 < 60; sectioning 12.0 < 70"}))
    assert t["primary"]["cause"] == "sectioning"


def test_a_prose_only_reason_still_yields_a_primary():
    """No clause has a score, so 'lowest score' cannot pick — it must not return nothing."""
    t = em.fallback_trigger(_fb(reason="short document (2 pages): routed straight to MinerU"))
    assert t["primary"] is not None and t["primary"]["cause"] == "short_doc"


def test_an_unrecognised_clause_is_kept_rather_than_dropped():
    """A new gate in needs_help must show up as itself, not vanish from the screen."""
    c = em.trigger_causes("some brand new gate fired")[0]
    assert c["cause"] == "other" and "brand new gate" in c["label"]


def test_an_empty_reason_produces_no_causes():
    assert em.trigger_causes("") == [] and em.trigger_causes(None) == []


# ---- the outcome, which is not the gate --------------------------------------------------

def test_a_hard_fail_is_surfaced_separately_from_the_gate():
    """Every tier ran and none cleared the bar. That instructs a reviewer to LOOK, whereas a
    bad gate alone reads as 'this document scored badly' — and the run would just retry it."""
    t = em.fallback_trigger(_fb(first_attempt={"entry_reason": "completeness 35.1 < 60"},
                                accepted=False, hard_fail=True,
                                hard_fail_reason="completeness 63.9 is below 90"))
    assert t["hard_fail"] is True
    assert t["hard_fail_reason"] == "completeness 63.9 is below 90"
    assert t["accepted"] is False


def test_the_first_pass_state_is_preserved_after_a_toc_rescue_rewrites_it():
    """The rescue REPLACES the outline, so the re-scored `toc` dimension reads healthy and the
    original failure is unrecoverable from the final scorecard. first_attempt is the only record
    that the document was rescued for its TOC at all."""
    t = em.fallback_trigger(_fb(first_attempt={
        "entry_reason": "toc 55.0 < 70", "toc_score": 55.0, "toc_status": "lacking",
        "toc_rescuable": False, "weakest_dimension": "completeness", "worst_score": 99.2}))
    assert t["first"]["toc_score"] == 55.0
    assert t["first"]["toc_status"] == "lacking"
    assert t["first"]["toc_rescuable"] is False


# ---- what reaches the job row ------------------------------------------------------------

def _row(**kw):
    base = {"ts": 1000.0, "product": "p", "label": "L__1", "gate": "pass", "seconds": 10.0}
    base.update(kw)
    return base


def test_a_finished_row_carries_its_trigger():
    j = em.jobs_view([_row(tier="mineru_full", fallback=True,
                           trigger_reason="toc 55.0 < 70; sectioning 40.0 < 70")], [])["jobs"][0]
    assert j["trigger"]["primary"]["cause"] == "sectioning"
    assert len(j["trigger"]["causes"]) == 2


def test_a_row_that_never_fell_back_has_no_trigger():
    j = em.jobs_view([_row(tier="first_pass")], [])["jobs"][0]
    assert j["trigger"] is None and j["hard_fail"] is False


def test_a_hard_fail_reaches_the_row():
    j = em.jobs_view([_row(gate="fail", tier="toc_rescue", fallback=True, hard_fail=True,
                           trigger_reason="completeness 35.1 < 60")], [])["jobs"][0]
    assert j["hard_fail"] is True


# ---- the LIVE case: is it re-running, and why ---------------------------------------------

def _shard(cur):
    import time
    return {"run": "r", "shard": 0, "shards": 1, "current": cur,
            "updated_at": time.time(), "started_at": time.time() - 100}


def test_a_document_in_a_fallback_tier_is_marked_as_a_second_pass():
    """Without this the stage ticks BACKWARDS to work the document already finished, and a
    rescue is indistinguishable from a stall."""
    import time
    j = em.jobs_view([], [_shard({"product": "p", "label": "L__1", "stage": "toc_rescue",
                                  "started_at": time.time() - 60})])["jobs"][0]
    assert j["live"] is True and j["fallback"] is True
    assert j["running_tier"] == "toc_rescue"
    assert j["running_tier_label"] == "⟲ TOC rescue"


def test_mineru_fallback_and_mineru_full_are_the_same_tier_to_the_screen():
    """run_corpus announces the tier under two different step names."""
    import time
    for stage in ("mineru_fallback", "mineru_full"):
        j = em.jobs_view([], [_shard({"product": "p", "label": "L__1", "stage": stage,
                                      "started_at": time.time()})])["jobs"][0]
        assert j["running_tier"] == "mineru_full", stage


def test_a_live_row_reports_why_it_escalated_as_soon_as_the_chain_decides():
    """The worker announces the trigger before the tier runs, because the tier is the part that
    takes minutes — waiting for the scorecard would mean the answer arrives after the wait."""
    import time
    j = em.jobs_view([], [_shard({
        "product": "p", "label": "L__1", "stage": "toc_rescue", "started_at": time.time(),
        "fallback": {"at": time.time(), "reason": "completeness 41.2 < 60; toc 65.0 < 70"}})])["jobs"][0]
    assert j["trigger"]["primary"]["cause"] == "completeness"
    assert j["trigger"]["primary"]["score"] == 41.2


def test_a_document_on_its_first_pass_is_not_marked_as_fallback():
    import time
    j = em.jobs_view([], [_shard({"product": "p", "label": "L__1", "stage": "stage2_mineru",
                                  "started_at": time.time()})])["jobs"][0]
    assert j["fallback"] is False and j["running_tier"] is None and j["trigger"] is None


# ---- passes: a tier is a whole re-extraction, not a stage inside one ----------------------

def test_a_tier_is_split_out_as_its_own_pass():
    """run_corpus records a tier as ONE step beside the stages, so listed flat `toc_rescue`
    reads as a peer of `stage1` — a cheap re-shuffle next to the real work. It is the reverse:
    the rescue re-runs stages 1-3 INCLUDING MinerU."""
    steps = {"toc_rescue": 796.7, "stage2_mineru": 782.6, "stage1": 7.2,
             "validation": 7.0, "stage3": 0.5, "scorecard": 0.0}
    passes = em.timing_passes(steps)
    assert [p["key"] for p in passes] == ["first", "toc_rescue"]
    first, resc = passes
    assert first["composite"] is False
    assert resc["composite"] is True
    # The rescue duplicated the whole extraction — the number that makes the point.
    assert abs(first["seconds"] - 797.3) < 0.05
    assert resc["seconds"] == 796.7


def test_the_first_pass_keeps_only_its_own_stages():
    """stage2_mineru in the flat list is the FIRST pass's MinerU. The tier's own MinerU run is
    inside the tier's total, which is exactly the confusion this grouping removes."""
    passes = em.timing_passes({"toc_rescue": 100.0, "stage2_mineru": 80.0, "stage1": 5.0})
    first = passes[0]
    assert set(first["steps"]) == {"stage2_mineru", "stage1"}
    assert "toc_rescue" not in first["steps"]


def test_a_tiers_inner_breakdown_is_used_when_the_run_recorded_one():
    """rescue_outline times its own stages, so a 13-minute tier can say how much was MinerU
    rather than staying an opaque lump."""
    chain = [{"tier": "toc_rescue",
              "steps": {"stage2_mineru": 770.0, "stage1": 20.0, "stage3": 6.7}}]
    passes = em.timing_passes({"toc_rescue": 796.7, "stage1": 7.2}, chain)
    resc = next(p for p in passes if p["key"] == "toc_rescue")
    assert resc["steps"]["stage2_mineru"] == 770.0, "the tier's own MinerU run must be visible"


def test_a_run_without_inner_timing_still_gets_the_pass_and_an_explanation():
    """Every existing run predates the instrumentation; the total plus what it contains is
    still far better than a bare step name."""
    passes = em.timing_passes({"toc_rescue": 796.7, "stage1": 7.2}, chain=[])
    resc = next(p for p in passes if p["key"] == "toc_rescue")
    assert resc["steps"] == {}
    assert "including MinerU" in resc["note"]


def test_both_mineru_step_names_map_to_one_tier():
    """run_corpus announces tier 3 as `mineru_fallback`; the chain records it as `mineru_full`."""
    for name in ("mineru_full", "mineru_fallback"):
        passes = em.timing_passes({name: 300.0, "stage1": 5.0})
        assert [p["key"] for p in passes] == ["first", "mineru_full"], name


def test_a_document_that_never_escalated_has_exactly_one_pass():
    passes = em.timing_passes({"stage1": 7.2, "stage2_mineru": 90.0, "stage3": 0.5})
    assert len(passes) == 1 and passes[0]["key"] == "first"


def test_the_end_to_end_total_is_not_counted_as_a_stage():
    """timings.json carries `total`; adding it to the first pass would double the figure."""
    passes = em.timing_passes({"stage1": 5.0, "stage2_mineru": 10.0, "total": 15.0})
    assert passes[0]["seconds"] == 15.0 and "total" not in passes[0]["steps"]


def test_passes_are_numbered_in_the_order_they_ran():
    passes = em.timing_passes({"stage1": 5.0, "toc_rescue": 100.0, "mineru_full": 200.0})
    assert [(p["ordinal"], p["key"]) for p in passes] == [
        (1, "first"), (2, "toc_rescue"), (3, "mineru_full")]


def test_no_steps_at_all_is_not_an_error():
    passes = em.timing_passes(None)
    assert len(passes) == 1 and passes[0]["seconds"] == 0
