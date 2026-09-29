"""The order of the fallback tiers, and what each one is allowed to adopt.

The expensive tier (a whole-document MinerU re-parse) must never run before the cheap
one (the document's own printed contents page), must not run at all once the cheap one
has recovered the document, and must not be adopted when it scores worse than what it
replaced. Those four properties are the whole point of fallback_chain, so they are
pinned here with the tiers themselves stubbed out — no PDF, no GPU.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fallback_chain as fc  # noqa: E402

# The chain now accepts on an ABSOLUTE bar rather than escalating below a low
# threshold: a result is kept only if the words are there AND the tree divided into
# usable sections. Both halves matter, so the fixture supplies both.
THRESH = 90.0        # check_structure_profile.ACCEPT_COMPLETENESS


def sc(worst: float, gate: str = "fail", *, divided: bool = True) -> dict:
    """A scorecard whose completeness IS the worst score, plus a structure profile.

    The profile is not decoration: `result_is_acceptable` asks two questions, and a
    scorecard without a profile answers "no structure profile" and escalates no matter
    how complete it is. Omitting it made every tier fire regardless of the score under
    test, which is the opposite of what these tests pin. `divided=False` gives the blob
    shape — one chunk holding everything — for the cases that need a failing SHAPE with
    a passing word count."""
    profile = ({"pages": 40, "chunks": 25, "concentration_pct": 20.0,
                "second_chunk_words": 800, "tiny_chunks": 0}
               if divided else
               {"pages": 40, "chunks": 1, "concentration_pct": 100.0,
                "second_chunk_words": 0, "tiny_chunks": 0})
    return {"gate": gate, "worst_score": worst, "weakest_dimension": "completeness",
            "structure": profile,
            "dimensions": {"completeness": {"score": worst},
                           "placement": {"score": 100.0}, "fidelity": {"score": 100.0}}}


@pytest.fixture
def job(tmp_path):
    d = tmp_path / "job"
    for name in fc.STAGE_DIR_NAMES:
        (d / name).mkdir(parents=True)
        (d / name / "marker.txt").write_text(f"stage1 {name}")
    (d / "source.pdf").write_bytes(b"%PDF-1.4 fake")
    return d


@pytest.fixture
def calls(monkeypatch):
    """Record which tiers ran, in order, and let each test say what they return.

    One tier, since the printed-TOC tier was removed: the pre-flight settles the outline
    before Stage 2 is paid for, so a document that still escalates has no outline repair
    left to try."""
    order = []
    monkeypatch.setattr(fc, "_mineru_full",
                        lambda *a, **k: (order.append("mineru_full"), a[2], a[3],
                                         {"tier": "mineru_full"})[1:])
    return order


def test_no_fallback_when_completeness_is_fine(job, calls):
    good = sc(94.0, "pass")
    val, out = fc.run_chain(job, job / "source.pdf", {"v": 1}, good)
    assert out is good and val == {"v": 1}
    assert calls == []                       # the tier is not even considered


def test_step_hook_names_each_tier(job, monkeypatch, calls):
    seen = []

    def step(name, fn):
        seen.append(name)
        return fn()

    fc.run_chain(job, job / "source.pdf", {}, sc(20.0), step=step)
    assert seen == ["mineru_fallback"]


def test_the_chain_has_exactly_one_tier(job, calls):
    """The printed-TOC tier is gone. It sat at the head of this chain and had nothing left
    to do: the pre-flight rebuilds an untrustworthy outline BEFORE Stage 2 is paid for, so
    it either skipped ("the pre-flight already rebuilt this outline") or re-ran the same
    reader on a document the pre-flight had already failed to verify. Measured over 148
    scored documents: 30 entered the chain, it did work on none and was adopted on none."""
    _, out = fc.run_chain(job, job / "source.pdf", {}, sc(20.0))
    assert [c["tier"] for c in out["fallback"]["chain"]] == ["mineru_full"]
    assert calls == ["mineru_full"]


def test_mineru_worse_is_rolled_back_and_kept_for_inspection(job, monkeypatch):
    """run_fallback swaps its own output in and parks the previous tree in
    hybrid_attempt/. When the re-parse scores worse, that swap has to be undone."""
    def fake_run_fallback(dest, pdf, val, s, **k):
        backup = dest / "hybrid_attempt"
        backup.mkdir()
        for name in fc.STAGE_DIR_NAMES:
            (dest / name).rename(backup / name)
            (dest / name).mkdir()
            (dest / name / "marker.txt").write_text(f"mineru {name}")
        (backup / "scorecard.json").write_text(json.dumps(s))
        return {"v": "mineru"}, sc(18.0)
    monkeypatch.setattr(fc, "run_fallback", fake_run_fallback)

    val, out, rec = fc._mineru_full(job, job / "source.pdf", {"v": "prev"}, sc(52.0))
    assert rec["adopted"] is False and "NOT adopted" in rec["status"]
    assert val == {"v": "prev"} and out["worst_score"] == 52.0
    # the earlier tree is the result again...
    assert (job / "03_stage3_final/marker.txt").read_text() == "stage1 03_stage3_final"
    # ...and the rejected MinerU attempt is still on disk, not deleted, WITH the
    # scorecard that lost — so the rejection can be read back per dimension
    aside = job / "mineru_full_attempt"
    assert (aside / "03_stage3_final/marker.txt").read_text() == "mineru 03_stage3_final"
    assert json.loads((aside / "scorecard.json").read_text())["worst_score"] == 18.0
    assert json.loads((aside / "validation.json").read_text()) == {"v": "mineru"}
    assert not (job / "hybrid_attempt").exists()


def test_mineru_better_is_kept(job, monkeypatch):
    monkeypatch.setattr(fc, "run_fallback",
                        lambda *a, **k: ({"v": "mineru"}, sc(77.0, "review")))
    val, out, rec = fc._mineru_full(job, job / "source.pdf", {"v": "prev"}, sc(41.0))
    assert rec["adopted"] is True and val == {"v": "mineru"}
    assert out["worst_score"] == 77.0
    assert not (job / "mineru_full_attempt").exists()   # nothing was rolled back


def _floor(coverage_pct, pages_missing):
    """A scorecard clamped to completeness 0.0, differing only in the raw inputs."""
    s = sc(0.0)
    s["dimensions"]["completeness"]["detail"] = {"coverage_pct": coverage_pct,
                                                "pages_missing": pages_missing,
                                                "pages_checkable": 6}
    return s


def test_at_the_score_floor_the_tie_breaks_on_coverage(job, monkeypatch):
    """Both attempts clamp to 0.0; the one that actually extracted something wins."""
    empty, partial = _floor(0.0, 4), _floor(15.0, 0)
    assert fc._better(partial, empty) is True
    assert fc._better(empty, partial) is False

    monkeypatch.setattr(fc, "run_fallback", lambda *a, **k: ({"v": "mineru"}, partial))
    val, out, rec = fc._mineru_full(job, job / "source.pdf", {"v": "prev"}, empty)
    assert rec["adopted"] is True and val == {"v": "mineru"}


def test_the_floor_tiebreak_does_not_apply_above_the_floor(job):
    """Two documents tied at a real score are left alone — swapping a whole tree for a
    coverage rounding difference is what MARGIN exists to prevent."""
    a, b = sc(55.8), sc(55.8)
    a["dimensions"]["completeness"]["detail"] = {"coverage_pct": 91.0, "pages_missing": 0}
    b["dimensions"]["completeness"]["detail"] = {"coverage_pct": 90.0, "pages_missing": 0}
    assert fc._better(a, b) is False


def test_a_tiny_gain_is_not_worth_swapping_the_tree(job, monkeypatch):
    monkeypatch.setattr(fc, "run_fallback",
                        lambda *a, **k: ({"v": "mineru"}, sc(41.02)))
    _, out, rec = fc._mineru_full(job, job / "source.pdf", {"v": "prev"}, sc(41.0))
    assert rec["adopted"] is False and out["worst_score"] == 41.0


# ---------------- what the gallery is told about the rescue ----------------
def _flag(job: Path):
    """rescue_flag through a real import — it is what puts the badge on a gallery row."""
    import push_hybrid_s3
    return push_hybrid_s3.rescue_flag(job)


def test_gallery_flag_names_the_mineru_tier(job):
    (job / "scorecard.json").write_text(json.dumps({
        **sc(48.3), "fallback": {"triggered": True, "adopted_tier": "mineru_full",
                                 "first_attempt": {"gate": "fail", "worst_score": 0.0,
                                                   "completeness_score": 0.0},
                                 "chain": [{"tier": "toc_rescue", "status": "TOC rejected: only 0 entries parsed"},
                                           {"tier": "mineru_full", "adopted": True}]}}))
    f = _flag(job)
    assert f["method"] == "mineru-full"
    assert f["was"] == "fail" and f["was_score"] == 0.0
    assert f["toc_rescue"] == "TOC rejected: only 0 entries parsed"


def test_gallery_flag_prefers_the_toc_rescue_record(job):
    """A TOC-rescued document has BOTH artifacts; the printed TOC is the real story."""
    (job / "rescued_by_toc.json").write_text(json.dumps(
        {"engine": "layout", "check": {"entries": 14, "verified": 13, "offset": 0},
         "before": {"gate": "fail"}}))
    (job / "scorecard.json").write_text(json.dumps({
        **sc(70.2, "review"), "fallback": {"triggered": True, "adopted_tier": "toc_rescue"}}))
    f = _flag(job)
    assert f["method"] == "toc-outline" and f["entries"] == 14 and f["verified"] == 13


def test_no_rescue_claimed_when_every_tier_was_rejected(job):
    """The result IS the normal pipeline's — badging it "rescued" would be a lie."""
    (job / "scorecard.json").write_text(json.dumps({
        **sc(55.8), "fallback": {"triggered": True, "adopted_tier": "stage1",
                                 "chain": [{"tier": "toc_rescue", "adopted": False},
                                           {"tier": "mineru_full", "adopted": False}]}}))
    assert _flag(job) is None


def test_no_rescue_claimed_for_an_ordinary_extraction(job):
    (job / "scorecard.json").write_text(json.dumps(sc(94.0, "pass")))
    assert _flag(job) is None


def _sound_structure(completeness: float) -> dict:
    """A scorecard whose HIERARCHY is beyond reproach and whose CONTENT is not —
    the shape `structure_recovered` was built to stop on, and the shape it was
    getting wrong."""
    s = sc(completeness)
    s["dimensions"].update({"toc": {"score": 100.0, "detail": {}},
                            "sectioning": {"score": 100.0}})
    return s


def test_a_sound_hierarchy_does_not_excuse_missing_content(job, calls):
    """Jersey__181919: toc 100, sectioning 100, completeness 46.6. Tier 3 recorded
    "not needed: structure is sound" on the same scorecard that recorded hard_fail
    "completeness 46.6 is below 90" -- the chain declined its last remaining tier and
    then called the document unrecoverable. Structure being sound is a reason not to
    REPLACE the hierarchy; it is not a reason to stop looking for the content."""
    fc.run_chain(job, job / "source.pdf", {"v": "stage1"}, _sound_structure(46.6))
    assert "mineru_full" in calls, "the expensive tier is the only one left to try"


def test_the_forced_run_says_why_it_overrode_the_structural_test(job, calls):
    _, out = fc.run_chain(job, job / "source.pdf", {"v": "stage1"},
                          _sound_structure(46.6))
    entry = out["fallback"]["first_attempt"]["entry_reason"]
    assert "completeness 46.6" in entry
    assert out["fallback"]["chain"][-1]["tier"] == "mineru_full"


def test_a_sound_hierarchy_still_stops_the_tier_above_the_entry_bar(job, calls):
    """The ADGM__170680 case the structural gate exists for: the cheap tier has
    already brought the document up, so replacing its hierarchy can only cost
    sectioning. Above CHAIN_ENTRY_COMPLETENESS the structural test still rules."""
    fc.run_chain(job, job / "source.pdf", {"v": "stage1"}, _sound_structure(80.2))
    assert "mineru_full" not in calls


def test_the_structural_skip_is_unchanged_at_the_boundary(job, calls):
    fc.run_chain(job, job / "source.pdf", {"v": "stage1"},
                 _sound_structure(fc.CHAIN_ENTRY_COMPLETENESS))
    assert "mineru_full" not in calls, "the bar is BELOW 60, not at it"
