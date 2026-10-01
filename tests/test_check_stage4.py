"""Fault injection for the stage-4 validator.

A validator that never fails is worse than none: it converts "unverified" into
"verified" on the strength of nothing. So each corruption stage 4 could plausibly
introduce is applied to a real tree here, and the check must catch it. Every one of
these is a behaviour actually observed from Haiku 4.5 on the pilot document, not a
hypothetical.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_stage4 import compute_stage4, conservation_report, integrity_score  # noqa: E402

TABLE = ("<table>"
         "<tr><td>Questions</td><td>Answers</td></tr>"
         "<tr><td>(a)</td><td>Are there arrangements (“IFA”)?</td></tr>"
         "<tr><td>(b)</td><td>See answer to 5(c) above.</td></tr>"
         "</table>")
DOC = ("# 6 MARKETING\n\n## 6.1 Mutual Recognition\n\n"
       "**⚙ MinerU-extracted table** — table_006, pages 19–33, 3 rows × 2 cols\n\n"
       + TABLE + "\n")


@pytest.fixture
def job(tmp_path):
    """A job dir whose stage 4 is, to begin with, a faithful copy of stage 3."""
    (tmp_path / "03_stage3_final").mkdir()
    (tmp_path / "04_stage4_ai").mkdir()
    (tmp_path / "03_stage3_final" / "07-marketing.md").write_text(DOC)
    (tmp_path / "04_stage4_ai" / "07-marketing.md").write_text(DOC)
    (tmp_path / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md"}], "splits": [], "edits": [],
        "batches_total": 2, "batches_changed": 1, "batches_rejected": 0,
        "batches_reprojected": 0, "usage": {"total_tokens": 100, "cost_usd": 0.01},
    }))
    return tmp_path


def _write(job, text):
    (job / "04_stage4_ai" / "07-marketing.md").write_text(text)
    return compute_stage4(job)


def _kinds(r):
    return {f["kind"] for f in r["flags"]}


# ---------------------------------------------------------------- the clean case

def test_faithful_copy_passes(job):
    r = compute_stage4(job)
    assert r["passed"] and r["flags"] == [] and r["score"] > 0


def test_legitimate_respacing_passes(job):
    # A restored space and line break: no content changes, so nothing to flag.
    r = _write(job, DOC.replace("Are there arrangements", "Are there<br>arrangements"))
    assert r["passed"] and r["flags"] == []


def test_legitimate_column_split_passes(job):
    r = _write(job, DOC.replace("<td>(a)</td><td>Are there",
                                "<td>(a)</td><td>Are</td><td>there"))
    assert r["passed"]


# ---------------------------------------------------------------- Tier 1 faults

def test_paraphrase_is_caught(job):
    r = _write(job, DOC.replace("Are there arrangements", "Is there an arrangement"))
    assert not r["passed"]
    assert "content_added" in _kinds(r) or "content_removed_unexplained" in _kinds(r)


def test_altered_cross_reference_is_caught(job):
    # Observed four times in one table: "see answer to 5(c)" -> "5(a)".
    r = _write(job, DOC.replace("5(c)", "5(a)"))
    assert not r["passed"]


def test_smart_quote_normalisation_is_caught(job):
    # Invisible to an alphanumeric comparison — the row check is strict for this reason.
    r = _write(job, DOC.replace("“IFA”", '"IFA"'))
    assert not r["passed"]
    assert "row_content_drift" in _kinds(r)


def test_dropped_row_is_caught(job):
    r = _write(job, DOC.replace("<tr><td>(b)</td><td>See answer to 5(c) above.</td></tr>", ""))
    assert not r["passed"]
    assert {"content_removed_unexplained", "row_content_drift"} & _kinds(r)


def test_duplicated_row_is_caught(job):
    row = "<tr><td>(b)</td><td>See answer to 5(c) above.</td></tr>"
    r = _write(job, DOC.replace(row, row + row))
    assert not r["passed"]
    assert "content_added" in _kinds(r)


def test_invented_emphasis_is_caught(job):
    r = _write(job, DOC.replace("<td>Questions</td>", "<td><strong>Questions</strong></td>"))
    assert not r["passed"]
    assert "markup_injected" in _kinds(r)


def test_invented_colspan_is_caught(job):
    r = _write(job, DOC.replace("<td>Questions</td>", '<td colspan="2">Questions</td>'))
    assert not r["passed"]
    assert "markup_injected" in _kinds(r)


# ---------------------------------------------------------------- the one sanctioned tag
# linkify_urls() wraps a bare URL in <a href="..." target="_blank" rel="noopener
# noreferrer">, in code, after the model returns — the one markup addition this pipeline
# makes on purpose. Added after this validator was first written, which is why it used to
# score identically to a model that invented its own <a> from nothing: 0.

def test_a_linkified_url_is_not_flagged(job):
    url = "http://www.jerseyfsc.org"
    linked = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
    # Stage 3 has the bare URL as ordinary text; stage 4 has exactly what linkify_urls()
    # would produce from it. That's the whole claim under test — a verified wrap, not a
    # model-invented tag — so put the bare URL in stage 3 too, or there is nothing to verify.
    (job / "03_stage3_final" / "07-marketing.md").write_text(
        DOC.replace("Are there arrangements", f"Are there arrangements. See {url} for more"))
    r = _write(job, DOC.replace(
        "Are there arrangements", f"Are there arrangements. See {linked} for more"))
    assert "markup_injected" not in _kinds(r)


def test_a_hallucinated_link_is_still_caught(job):
    """An <a> the MODEL wrote, not the deterministic linkify step, must still fail --
    verified against what re-running linkify_urls on stage 3 would actually produce, not
    assumed from the tag shape alone."""
    fake = '<a href="http://not-a-real-source.example" target="_blank" rel="noopener noreferrer">click here</a>'
    r = _write(job, DOC.replace("Are there arrangements", f"Are there arrangements {fake}"))
    assert not r["passed"]
    assert "markup_injected" in _kinds(r)


def test_invented_emphasis_alongside_a_real_link_is_still_caught(job):
    """The excuse is narrow: a legitimate linkified URL sitting in the same file as an
    invented <strong> must still flag the <strong> — exactly Jersey/181919's own shape,
    where table_003's row picked up both a genuine link and an unexplained bold tag."""
    url = "http://www.jerseyfsc.org"
    linked = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
    (job / "03_stage3_final" / "07-marketing.md").write_text(
        DOC.replace("Are there arrangements", f"Are there arrangements. See {url}."))
    text = DOC.replace("Are there arrangements", f"Are there arrangements. See {linked}.")
    text = text.replace("<td>Questions</td>", "<td><strong>Questions</strong></td>")
    r = _write(job, text)
    assert not r["passed"]
    assert "markup_injected" in _kinds(r)
    flag = next(f for f in r["flags"] if f["kind"] == "markup_injected")
    assert "'strong'" in flag["detail"] and "'a'" not in flag["detail"]


def test_invented_bullet_glyph_is_caught(job):
    # Also punctuation-only: the model saw bullets on a page image and added them.
    r = _write(job, DOC.replace("Are there", "• Are there"))
    assert not r["passed"]
    assert "row_content_drift" in _kinds(r)


def test_missing_file_is_caught(job):
    (job / "04_stage4_ai" / "07-marketing.md").unlink()
    r = compute_stage4(job)
    assert not r["passed"] and "file_missing" in _kinds(r)


# ---------------------------------------------------------------- de-duplication

def _doc(headings: str, rows: str) -> str:
    return ("# 6 MARKETING\n\n" + headings + "\n\n"
            "**⚙ MinerU-extracted table** — table_006, pages 19–33, 3 rows × 2 cols\n\n"
            "<table><tr><td>Questions</td><td>Answers</td></tr>" + rows + "</table>\n")


ROW_A = "<tr><td>(a)</td><td>Are there arrangements (“IFA”)?</td></tr>"
LABEL_62 = "<tr><td>6.2</td><td>Funds</td></tr>"


def test_shell_removal_passes_when_the_text_survives_once(job):
    """Stage 1 wrote an EMPTY '## 6.2 Funds' shell and the table also carries the label
    row, so the text is present twice. Stage 4 promotes the row to a heading and deletes
    the shell — one copy goes, one remains. That must reconcile exactly."""
    (job / "03_stage3_final" / "07-marketing.md").write_text(
        _doc("## 6.1 Mutual Recognition\n\n## 6.2 Funds", LABEL_62 + ROW_A))
    (job / "04_stage4_ai" / "07-marketing.md").write_text(
        _doc("## 6.1 Mutual Recognition\n\n### 6.2 Funds", ROW_A))
    # A shell is only ever removed AFTER a split promotes that section, so the report
    # carries both — the split moved the row out of the table, the shell deletion took
    # the surplus heading.
    (job / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md", "shells_removed": ["6.2 Funds"]}],
        "splits": [{"file": "07-marketing.md", "ok": True, "sections": ["6.2 Funds"],
                    "reused_existing_heading": False}],
        "edits": [], "batches_total": 1, "batches_changed": 1,
        "batches_rejected": 0, "batches_reprojected": 0, "usage": {},
    }))
    r = compute_stage4(job)
    assert r["passed"], r["flags"]


def test_removal_that_is_not_actually_duplicated_is_caught(job):
    """The same removal, but the text does NOT survive anywhere — that is deletion
    dressed up as de-duplication, and it must fail."""
    doc3 = DOC.replace("<tr><td>(a)</td>", "<tr><td>6.2</td><td>Funds</td></tr><tr><td>(a)</td>")
    (job / "03_stage3_final" / "07-marketing.md").write_text(doc3)
    (job / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md", "shells_removed": ["6.2 Funds"]}],
        "splits": [], "edits": [], "batches_total": 1, "batches_changed": 1,
        "batches_rejected": 0, "batches_reprojected": 0, "usage": {},
    }))
    r = compute_stage4(job)          # stage 4 still holds plain DOC, with no 6.2 anywhere
    assert not r["passed"]
    assert "removal_not_duplicated" in _kinds(r)


def test_a_stale_report_cannot_buy_a_pass(job):
    """A report claiming a removal that never happened must not license an unrelated
    deletion of the same size."""
    (job / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md", "shells_removed": ["Are there arrangements"]}],
        "splits": [], "edits": [], "batches_total": 1, "batches_changed": 1,
        "batches_rejected": 0, "batches_reprojected": 0, "usage": {},
    }))
    r = _write(job, DOC.replace("See answer to 5(c) above.", ""))
    assert not r["passed"]


# ---------------------------------------------------------------- Tier 2 signals

def test_high_rejection_rate_warns_without_failing(job):
    (job / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md"}], "splits": [], "edits": [],
        "batches_total": 10, "batches_changed": 3, "batches_rejected": 4,
        "batches_reprojected": 0, "usage": {},
    }))
    r = compute_stage4(job)
    assert r["passed"]                       # a rejection is the gate WORKING
    assert "high_rejection_rate" in _kinds(r)
    assert r["score"] < 100


def test_column_uniformity_regression_warns(job):
    # stage 3 rows all match a 2-column table; stage 4 leaves one short.
    r = _write(job, DOC.replace("<tr><td>(b)</td><td>See answer to 5(c) above.</td></tr>",
                                "<tr><td>(b) See answer to 5(c) above.</td></tr>"))
    assert "column_uniformity_regressed" in _kinds(r)


def test_absent_stage4_is_skipped_not_failed(tmp_path):
    """Most documents never run stage 4. The dimension must be ABSENT, not zero, or
    every one of them starts failing a gate it was never eligible for."""
    (tmp_path / "03_stage3_final").mkdir()
    r = compute_stage4(tmp_path)
    assert r["skipped"] and r["passed"] and r["score"] is None


# ---------------------------------------------------------------- removal accounting
# Two faults found by running the validator over a real 9-chunk stage-4 run. Both
# reported clean documents as corrupted, or would have let a real deletion through.

def _split_job(job, *, reused):
    """Stage 3 holds a label row; stage 4 promotes it to a heading.

    reused=True models stage 1 having ALREADY written that heading above the table: the
    split drops its own heading, so the label leaves the file entirely (one of two copies
    goes). reused=False means the split writes the heading itself, so the label only
    MOVES out of the table and the file keeps every character.
    """
    head3 = "## 6.2 Funds" if reused else "## 6.1 Mutual Recognition"
    head4 = "## 6.2 Funds" if reused else "## 6.1 Mutual Recognition\n\n### 6.2 Funds"
    (job / "03_stage3_final" / "07-marketing.md").write_text(_doc(head3, LABEL_62 + ROW_A))
    (job / "04_stage4_ai" / "07-marketing.md").write_text(_doc(head4, ROW_A))
    (job / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md"}],
        "splits": [{"file": "07-marketing.md", "ok": True, "sections": ["6.2 Funds"],
                    "reused_existing_heading": reused}],
        "edits": [], "batches_total": 1, "batches_changed": 1, "batches_rejected": 0,
        "batches_reprojected": 0, "usage": {},
    }))
    return compute_stage4(job)


def test_promoted_label_is_row_accounted_even_when_no_heading_was_reused(job):
    """The label always leaves the TABLES, whether or not it leaves the FILE.

    Conflating the two reported 08-private-placement-regime and 09-marketing-activities
    as corrupted on a run where nothing was actually wrong.
    """
    r = _split_job(job, reused=False)
    assert r["passed"], r["flags"]
    assert "row_content_drift" not in _kinds(r)


def test_promoted_label_accounted_when_a_heading_was_reused(job):
    r = _split_job(job, reused=True)
    assert r["passed"], r["flags"]


def test_overdeclared_removal_is_caught(job):
    """Counter subtraction clamps at zero, so an over-declaring report would absorb an
    unrelated deletion of the same size and pass. Declaring a removal that did not
    happen is itself a fault."""
    (job / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md",
                   "shells_removed": ["6.2 Funds", "6.3 Something Else Entirely"]}],
        "splits": [], "edits": [], "batches_total": 1, "batches_changed": 1,
        "batches_rejected": 0, "batches_reprojected": 0, "usage": {},
    }))
    r = compute_stage4(job)          # stage 4 is a faithful copy — nothing was removed
    assert not r["passed"]
    assert "removal_overdeclared" in _kinds(r)


def test_only_the_first_section_of_a_reused_split_is_a_file_removal(job):
    """A split with two sections and a reused heading deletes ONE label, not both.

    Stage 1 wrote '## 7.1 Alpha' above the table, so that label existed twice and one
    copy goes. '7.2 Beta' existed only as a row and merely moves into its own heading.
    Counting both as file removals over-declared by ~160 characters on the real document
    — enough to have absorbed a genuine deletion.
    """
    rows = ("<tr><td>7.1</td><td>Alpha</td></tr>"
            "<tr><td>7.2</td><td>Beta</td></tr>" + ROW_A)
    (job / "03_stage3_final" / "07-marketing.md").write_text(_doc("## 7.1 Alpha", rows))
    (job / "04_stage4_ai" / "07-marketing.md").write_text(
        _doc("## 7.1 Alpha\n\n### 7.2 Beta", ROW_A))
    (job / "04_stage4_ai" / "stage4_report.json").write_text(json.dumps({
        "files": [{"file": "07-marketing.md"}],
        "splits": [{"file": "07-marketing.md", "ok": True,
                    "sections": ["7.1 Alpha", "7.2 Beta"],
                    "reused_existing_heading": True}],
        "edits": [], "batches_total": 1, "batches_changed": 1, "batches_rejected": 0,
        "batches_reprojected": 0, "usage": {},
    }))
    r = compute_stage4(job)
    assert r["passed"], r["flags"]


# ---------------------------------------------------------------- absence vs split
# A real loss found by hand and missed by two earlier versions of this check: the trailing
# line of question 6.3(f), which stage 1 left at the top of section 7's chunk. Stage 4
# correctly removed it as foreign to section 7 — and nothing re-homed it into section 6,
# so it left the document. Both earlier rules passed it silently.

LONG_A = "the requirements in your jurisdiction which apply to a web page targeted at " * 3
LONG_B = "investors located in your jurisdiction by writing it in the local language " * 3


def _tree(job, before, after):
    for name, text in before.items():
        (job / "03_stage3_final" / name).write_text(text)
    for name, text in after.items():
        (job / "04_stage4_ai" / name).write_text(text)


def test_a_short_deleted_cell_is_flagged(job):
    """Under the "half the windows may fail" rule a 72-character cell yielded ONE window,
    so it could only be flagged at zero matches — and its 40 characters of boilerplate
    matched a near-identical sentence elsewhere. It passed. It must not now."""
    lost = "If so, please provide recommended wording in Section C of Appendix 3 here"
    _tree(job,
          {"08.md": f"<table><tr><td>{lost}</td><td></td></tr></table>"},
          # the near-identical sentence that used to rescue it, in another section
          {"07.md": "<table><tr><td>If so, please provide recommended wording in "
                    "Section C of Appendix 1 instead</td></tr></table>",
           "08.md": "<table><tr><td>unrelated</td></tr></table>"})
    r = conservation_report(job)
    hit = [m for m in r["missing_cells"] if "recommendedwording" in m["text"]]
    assert hit, "the deleted cell was not flagged"
    assert hit[0]["likely"] == "deleted"


def test_a_cell_split_across_rows_is_labelled_split_not_deleted(job):
    """Splitting a merged question/answer cell breaks the window straddling the cut, so
    the whole cell is absent while every word survives. Reported, but as a split."""
    merged = LONG_A + LONG_B
    _tree(job,
          {"09.md": f"<table><tr><td>{merged}</td><td>ans</td></tr></table>"},
          {"09.md": f"<table><tr><td>{LONG_A}</td><td>ans</td></tr>"
                    f"<tr><td>{LONG_B}</td><td>ans2</td></tr></table>"})
    r = conservation_report(job)
    hit = [m for m in r["missing_cells"] if m["chars"] > 200]
    assert hit and hit[0]["likely"] == "split"


def test_a_deletion_costs_score_and_a_split_does_not(job):
    assert integrity_score([{"likely": "split"}] * 6, []) == 100.0
    assert integrity_score([{"likely": "deleted"}], []) == 88.0


def test_deduplication_is_not_reported_as_loss(job):
    """Stage 4 removes text stage 3 held twice. Counting occurrences flagged 57 cells on
    the real document for this reason; absence of the whole cell does not."""
    dup = "King & Co acknowledges that aosphere Limited will publish the Memorandum"
    _tree(job,
          {"01.md": f"{dup}\n\n<table><tr><td>{dup}</td></tr></table>"},
          {"01.md": f"<table><tr><td>{dup}</td></tr></table>"})
    r = conservation_report(job)
    assert r["missing_count"] == 0


# ---- which sub-section a finding sits in ------------------------------------------
# "09-marketing-activities.md, 5 cells missing" sends a reviewer through 227 cells and
# twenty pages. "8.5 Internet/Social Media" sends them to one row.

def _tbl(*rows):
    return "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                               for r in rows) + "</table>"


def test_a_finding_names_the_subsection_it_sits_under():
    from check_stage4 import _cell_subsections
    text = _tbl(["8.5", "Internet/Social Media"],
                ["(a) Please outline the requirements", "N/A"],
                ["8.12", "Relocation of Investor"],
                ["(a) What restrictions if any", "N/A"])
    subs = _cell_subsections(text)
    assert subs[0] == "8.5"                        # the label cell starts its section
    assert subs[1] == "8.5 Internet/Social Media"  # title arrives in the next cell
    assert subs[2] == subs[3] == "8.5 Internet/Social Media"
    assert subs[-1] == "8.12 Relocation of Investor"


def test_the_attribution_lines_up_cell_for_cell_with_the_content_check():
    """They are zipped by INDEX. Iterate differently and a finding is attributed to the
    wrong sub-section, which is worse than not attributing it at all."""
    from check_stage4 import _cell_subsections, _cells_with_columns
    text = _tbl(["6.1", "Mutual Recognition"], ["q", "a"], ["x", "y", "z"])
    assert len(_cell_subsections(text)) == len(_cells_with_columns(text))


def test_a_label_and_title_in_one_cell_is_understood():
    """A repaired row carries them together rather than in adjacent cells."""
    from check_stage4 import _cell_subsections
    assert _cell_subsections(_tbl(["6.3 Investment Management", "N/A"]))[0] \
        == "6.3 Investment Management"


def test_an_entity_in_a_title_is_unescaped():
    """Or it reads "Investment Management &amp; Advisory" on screen."""
    from check_stage4 import _cell_subsections
    subs = _cell_subsections(_tbl(["6.3", "Investment Management &amp; Advisory"]))
    assert subs[1] == "6.3 Investment Management & Advisory"


def test_cells_before_any_subsection_are_attributed_to_none():
    """Not to the previous file's last label, and not guessed."""
    from check_stage4 import _cell_subsections
    assert _cell_subsections(_tbl(["Questions", "Answers"]))[0] is None


def test_the_presence_check_carries_the_subsection_onto_each_finding():
    from check_stage4 import check_cell_presence
    before = {"09.md": _tbl(["8.5", "Internet/Social Media"],
                            ["please outline the requirements in your jurisdiction that "
                             "apply to an OFI marketing a Fund by way of the internet", "N/A"])}
    found = check_cell_presence({"09.md": before["09.md"]}, {"09.md": "<table></table>"},
                                min_chars=40)
    assert found, "the long cell is gone from stage 4 and must be reported"
    assert found[0]["subsection"] == "8.5 Internet/Social Media"


# ------------------------------------------------------- the summary-AI route's 04_ dir
#
# build_inspect.py now calls stage4_dashboard for EVERY published document, so a directory
# named 04_stage4_ai has to be read for what it is rather than for what it is called. The
# summary-AI route writes its chunk tree there in the same stage-3 shape and never writes
# a stage4_report.json -- it replaces stages 1-3 outright and prices its own call into
# report.json. Reading the directory alone made every summary document's Stage 4 tab claim
# a run that never happened: no model, no tokens, $0.0000, integrity 100.

def test_a_04_dir_without_a_report_is_not_a_stage_4_run(tmp_path):
    from check_stage4 import stage4_dashboard

    job = tmp_path / "SUMMARY__Ecuador"
    (job / "04_stage4_ai").mkdir(parents=True)
    (job / "04_stage4_ai" / "02-general-marketing.md").write_text("# General Marketing\n")
    (job / "summary_ai_rule.json").write_text(json.dumps({"module": "summary_ai_extract"}))

    d = stage4_dashboard(job)
    assert d["ran"] is False
    assert "summary-AI route" in d["reason"]
    assert "score" not in d, "a route that never ran stage 4 must not carry an integrity score"


def test_a_04_dir_without_a_report_and_without_the_marker_still_is_not_a_run(tmp_path):
    from check_stage4 import stage4_dashboard

    job = tmp_path / "Somewhere__1"
    (job / "04_stage4_ai").mkdir(parents=True)
    d = stage4_dashboard(job)
    assert d["ran"] is False
    assert "stage4_report.json" in d["reason"]
