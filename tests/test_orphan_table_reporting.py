"""Orphan tables must be COUNTED in the scorecard, recovered or not.

An orphan is a table MinerU extracted correctly that no Stage 1 region claimed. The
loss it causes can be invisible to every other signal: word coverage is a bag of
words, so a question whose wording mirrors a parallel question elsewhere still counts
as covered, and the per-section diff only fires if the span lands inside a section's
page range. Measured on Bahrain__169749: 3,543 characters of section 7.5 absent from
the tree, completeness 98.6, ZERO silent spans, gate PASS.

So the numbers below are the only thing that says so, and a recovered orphan is
reported separately — nothing is lost there, but which region adopted it is worth an
eye.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_scorecard as cs  # noqa: E402

LOST = {"page": 48, "rows": 3, "chars": 3543, "preview": "7.5 Investment Management"}
GOT = {"table_id": "table_010", "page": 85, "rows": 4, "chars": 2395,
       "preview": "7.5 Investment Management"}


def test_summary_separates_lost_from_recovered():
    s = cs.orphan_summary({"orphan_blocks": [LOST], "orphans_recovered": [GOT]})
    assert (s["lost_count"], s["lost_chars"]) == (1, 3543)
    assert (s["recovered_count"], s["recovered_chars"]) == (1, 2395)
    assert s["lost_pages"] == [48]
    assert s["recovered_into"] == ["table_010"]


def test_summary_is_empty_and_safe_without_a_stage2_report():
    for arg in (None, {}, {"orphan_blocks": [], "orphans_recovered": []}):
        s = cs.orphan_summary(arg)
        assert s["lost_count"] == 0 and s["recovered_count"] == 0
        assert s["lost_chars"] == 0 and s["recovered_chars"] == 0


def _detail(orphans):
    _score, detail = cs._score_completeness({}, {}, orphans=orphans)
    return detail


def test_the_counts_reach_the_completeness_detail():
    d = _detail(cs.orphan_summary({"orphan_blocks": [LOST], "orphans_recovered": [GOT]}))
    assert d["orphan_tables_lost"] == 1 and d["orphan_chars_lost"] == 3543
    assert d["orphan_tables_recovered"] == 1 and d["orphan_chars_recovered"] == 2395


def test_a_lost_orphan_is_a_bad_stat_and_a_recovered_one_is_not():
    lost = _detail(cs.orphan_summary({"orphan_blocks": [LOST]}))
    row = next(s for s in lost["stats"] if "LOST" in s["label"])
    assert row.get("bad") is True, "lost content must read as a defect"
    assert "3543" in row["value"] and "48" in row["value"]

    got = _detail(cs.orphan_summary({"orphans_recovered": [GOT]}))
    row = next(s for s in got["stats"] if "RECOVERED" in s["label"])
    assert not row.get("bad"), "a recovered orphan lost nothing — not a defect"
    assert row.get("warn") is True and "table_010" in row["value"]


def test_neither_stat_appears_when_there_are_no_orphans():
    d = _detail(cs.orphan_summary(None))
    assert not [s for s in d["stats"] if "orphan" in s["label"].lower()]


def test_scoring_is_unchanged_by_orphans():
    """Reported, deliberately not scored: where the per-section diff already caught
    the loss it has charged for it once, and charging twice would make the number
    mean less. The stat and the finding are what surface it."""
    base, _ = cs._score_completeness({}, {})
    with_lost, _ = cs._score_completeness(
        {}, {}, orphans=cs.orphan_summary({"orphan_blocks": [LOST]}))
    assert with_lost == base


def test_findings_distinguish_the_two_cases():
    s2 = {"orphan_blocks": [LOST], "orphans_recovered": [GOT]}
    f = cs._collect_findings({}, {}, None, [], stage2=s2,
                             orphans=cs.orphan_summary(s2, tree_text=""))
    lost = [x for x in f if x["kind"] == "orphan_table"]
    got = [x for x in f if x["kind"] == "orphan_recovered"]
    assert len(lost) == 1 and len(got) == 1
    assert lost[0]["severity"] == "silent", "unseen content loss is the actionable case"
    assert got[0]["severity"] == "advisory", "recovered content is not a defect"
    assert lost[0]["dimension"] == got[0]["dimension"] == "completeness"
    assert lost[0]["pages"] == [48] and got[0]["pages"] == [85]
    assert "table_010" in got[0]["title"]
    assert lost[0]["key"] != got[0]["key"]


def test_no_orphan_findings_without_a_stage2_report():
    assert not [x for x in cs._collect_findings({}, {}, None, [])
                if x["kind"].startswith("orphan_")]


def test_an_orphan_whose_text_is_already_in_the_tree_is_not_reported_as_lost():
    """"Unclaimed by a region" and "absent from the output" are different questions.
    Conflating them made the scorecard claim 3 lost tables on the MRAM corpus when the
    real number was 0 -- MinerU emits a block covering a whole page's table area, and
    its text is already emitted by the one or two regions that DID match. Adopting
    such a block would duplicate, not recover."""
    tree = ("Regime will not need to apply MiFID II requirements. "
            "A non-EU OFI which is not subject to the Licensing Requirement is exempt. ")
    dup = {"page": 78, "rows": 2, "chars": 2011, "preview": tree}
    s = cs.orphan_summary({"orphan_blocks": [dup]}, tree_text=tree)
    assert (s["lost_count"], s["duplicate_count"]) == (0, 1)
    assert s["duplicate_chars"] == 2011

    d = _detail(s)
    assert d["orphan_tables_lost"] == 0 and d["orphan_tables_duplicate"] == 1
    row = next(x for x in d["stats"] if "DUPLICATE" in x["label"])
    assert not row.get("bad"), "nothing was lost, so this must not read as a defect"

    f = cs._collect_findings({}, {}, None, [], stage2={"orphan_blocks": [dup]}, orphans=s)
    kinds = [x["kind"] for x in f if x["kind"].startswith("orphan")]
    assert kinds == ["orphan_duplicate"]
    assert next(x for x in f if x["kind"] == "orphan_duplicate")["severity"] == "advisory"


def test_a_genuinely_absent_orphan_is_still_reported_as_lost():
    s = cs.orphan_summary({"orphan_blocks": [LOST]}, tree_text="entirely unrelated prose")
    assert (s["lost_count"], s["duplicate_count"]) == (1, 0)
    f = cs._collect_findings({}, {}, None, [], stage2={"orphan_blocks": [LOST]}, orphans=s)
    assert [x["kind"] for x in f if x["kind"].startswith("orphan")] == ["orphan_table"]
