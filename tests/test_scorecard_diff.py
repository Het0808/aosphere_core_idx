"""Did the AI pass fix what the extraction got wrong, or break something new?

The join that answers it must survive the renames stage 5 performs, which is the whole
reason it does not use the findings' own dismissal keys: several of those embed the file
a finding sits in, and subchunking renames files as it restructures
(01-front-matter.md -> 00-front-matter.md, a sub-chunked section becomes a directory,
appendices take a letter prefix). Joined on those keys every gap in the document reads
as fixed by the AI and an identical one as introduced by it.

Measured on the four local stage-5 jobs, this join's first run:
  Jersey__181919                      review 87.1 -> pass 94.6   2 fixed, 0 introduced
  Bahamas-sonnet-nocounts             pass 91.9 -> fail 45.8     0 fixed, 7 introduced
which is the comparison neither scorecard states on its own.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib_scorecard_diff import dimension_deltas, finding_deltas, join_key  # noqa: E402


def f(kind, title, dimension="completeness", dismissed=False, **kw):
    return {"kind": kind, "title": title, "dimension": dimension,
            "dismissed": dismissed, **kw}


def sc(findings, dimensions=None, **kw):
    return {"findings": findings, "dimensions": dimensions or {}, **kw}


# ---- the join itself ---------------------------------------------------------
def test_a_finding_gone_after_the_ai_pass_is_reported_as_fixed():
    pre = sc([f("gap", "the fund must notify the regulator within 30 days")])
    d = finding_deltas(pre, sc([]))
    assert d["counts"] == {"fixed": 1, "fixed_by_relaxation": 0, "persisting": 0,
                           "introduced": 0, "before": 1, "after": 0}
    assert d["by_dimension"]["fixed"] == {"completeness": 1}


def test_a_finding_present_in_both_is_persisting_not_fixed_and_not_new():
    one = f("gap", "the fund must notify the regulator within 30 days")
    d = finding_deltas(sc([one]), sc([dict(one)]))
    assert d["counts"]["persisting"] == 1
    assert d["counts"]["fixed"] == d["counts"]["introduced"] == 0


def test_a_finding_only_after_the_ai_pass_is_reported_as_introduced():
    d = finding_deltas(sc([]), sc([f("table_lost", "table_011 was extracted but is not "
                                     "in the output", dimension="fidelity")]))
    assert d["counts"]["introduced"] == 1
    assert d["by_dimension"]["introduced"] == {"fidelity": 1}
    assert any("appear only after the AI pass" in c for c in d["caveats"])


def test_a_rename_between_the_stages_does_not_read_as_fixed_and_reintroduced():
    """The defect this join exists to avoid. Same claim, different file — one
    persisting finding, not one fixed plus one introduced."""
    pre = sc([f("gap", "the fund must notify the regulator", file="01-front-matter.md")])
    post = sc([f("gap", "the fund must notify the regulator", file="00-front-matter.md")])
    d = finding_deltas(pre, post)
    assert d["counts"]["persisting"] == 1
    assert d["counts"]["fixed"] == d["counts"]["introduced"] == 0


def test_whitespace_and_case_do_not_split_one_finding_into_two():
    pre = sc([f("gap", "The Fund   must notify")])
    post = sc([f("gap", "the fund must notify")])
    assert finding_deltas(pre, post)["counts"]["persisting"] == 1


def test_a_dismissed_finding_takes_no_part_in_the_join():
    """A reviewer already cleared it, so it is neither a problem that was fixed nor one
    that survived — counting it either way would misreport the AI pass."""
    pre = sc([f("gap", "a false positive", dismissed=True)])
    d = finding_deltas(pre, sc([]))
    assert d["counts"] == {"fixed": 0, "fixed_by_relaxation": 0, "persisting": 0,
                           "introduced": 0, "before": 0, "after": 0}


def test_a_finding_cleared_only_because_its_check_relaxed_is_not_counted_as_a_repair():
    """check_table_presence stops requiring <td> after stage 4, because the AI pass may
    legitimately turn a questionnaire table into headings and prose. A table_lost
    finding that disappears for that reason must not be sold as a fix."""
    pre = sc([f("table_lost", "table_003 was extracted but is not in the output",
                dimension="fidelity")])
    d = finding_deltas(pre, sc([]))
    assert d["counts"]["fixed"] == 0
    assert d["counts"]["fixed_by_relaxation"] == 1
    assert any("not by itself evidence of a repair" in c for c in d["caveats"])


# ---- dimension movement ------------------------------------------------------
def test_dimension_deltas_report_movement_in_both_directions():
    pre = sc([], {"completeness": {"score": 91.9, "critical": True},
                  "fidelity": {"score": 100.0, "critical": True}})
    post = sc([], {"completeness": {"score": 94.6, "critical": True},
                   "fidelity": {"score": 45.8, "critical": True}})
    d = dimension_deltas(pre, post)
    assert d["completeness"]["delta"] == 2.7
    assert d["fidelity"]["delta"] == -54.2


def test_a_dimension_measured_on_only_one_side_keeps_a_none_rather_than_a_zero():
    """"not measured" and "scored zero" are different facts, and collapsing them is how
    a regression hides — ai_postprocess is absent on every document that never ran
    stage 4."""
    d = dimension_deltas(sc([], {"toc": {"score": 100.0}}),
                         sc([], {"ai_postprocess": {"score": 0.0}}))
    assert d["toc"] == {"before": 100.0, "after": None, "delta": None, "critical": None}
    assert d["ai_postprocess"]["before"] is None and d["ai_postprocess"]["after"] == 0.0


def test_the_gates_and_scored_stages_are_carried_through():
    pre = sc([], {}, gate="review", worst_score=87.1, scored_stage=None)
    post = sc([], {}, gate="pass", worst_score=94.6, scored_stage=5)
    d = finding_deltas(pre, post)
    assert d["gates"] == {"before": "review", "after": "pass",
                          "before_score": 87.1, "after_score": 94.6}
    assert d["scored_stages"] == {"before": 3, "after": 5}


def test_join_key_separates_different_kinds_that_share_a_title():
    assert join_key(f("gap", "section 7")) != join_key(f("hollow", "section 7"))
