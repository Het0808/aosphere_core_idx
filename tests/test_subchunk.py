"""Sub-chunking: the nested per-sub-section tree, and the two gates that scope it.

The gates are the point. This behaviour is wanted for ONE product that has been through
AI post-processing, and must not fire anywhere else — a product whose sections are
ordinary prose has no numbered split points, and stage 3 alone leaves the sub-section
labels inside the tables where nothing can split on them.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pytest
from subchunk import ai_processed, run, slug, split_file, subchunk_eligible

MRAM = "124_Marketing_Restrictions_-_Asset_Management"

SECTION = """# 6. MARKETING/SELLING TO THE PUBLIC

*Source: `source_repaired.pdf`, page 19-34*

### 6.1 Mutual Recognition of Funds/Passporting Schemes

<table><tr><td>(a)</td><td>Are there arrangements?</td></tr></table>

### 6.2 Funds

<table><tr><td>(a)</td><td>Please confirm.</td></tr></table>

### 6.10 Something Tenth

<table><tr><td>(a)</td><td>Tenth.</td></tr></table>
"""


def _job(tmp_path, product=MRAM, with_stage4=True, files=None):
    job = tmp_path / product / "Bahamas__1"
    (job / "03_stage3_final").mkdir(parents=True)
    s4 = job / "04_stage4_ai"
    s4.mkdir()
    for name, body in (files or {"07-marketing.md": SECTION}).items():
        (s4 / name).write_text(body)
    (job / "corpus_meta.json").write_text(json.dumps({"product": product}))
    if with_stage4:
        (s4 / "stage4_report.json").write_text(json.dumps({"model": "haiku", "mode": "section"}))
    return job


# ---------------------------------------------------------------- the gates

def test_eligible_when_product_matches_and_ai_has_run(tmp_path):
    ok, why = subchunk_eligible(_job(tmp_path))
    assert ok and "AI-processed" in why


def test_refused_when_the_ai_pass_has_not_run(tmp_path):
    ok, why = subchunk_eligible(_job(tmp_path, with_stage4=False))
    assert not ok and "no completed stage 4" in why


def test_refused_for_any_other_product(tmp_path):
    ok, why = subchunk_eligible(_job(tmp_path, product="155_Data_Privacy"))
    assert not ok and "not in SUBCHUNK_PRODUCTS" in why


def test_refused_product_wins_even_with_ai(tmp_path):
    """Both gates are required, not either — an AI-processed document of another product
    must not be split."""
    job = _job(tmp_path, product="172_G20", with_stage4=True)
    assert subchunk_eligible(job)[0] is False
    assert run(job)["ran"] is False


def test_a_refused_run_writes_nothing(tmp_path):
    job = _job(tmp_path, product="155_Data_Privacy")
    run(job)
    assert not (job / "05_subchunks").exists()


def test_the_flag_is_the_stage4_report(tmp_path):
    job = _job(tmp_path)
    assert ai_processed(job)["model"] == "haiku"
    (job / "04_stage4_ai" / "stage4_report.json").unlink()
    assert ai_processed(job) is None


# ---------------------------------------------------------------- splitting

def test_parent_keeps_everything_above_the_first_subsection():
    parent, subs = split_file(SECTION)
    assert "# 6. MARKETING/SELLING TO THE PUBLIC" in parent
    assert "*Source:" in parent
    assert "6.1" not in parent
    assert [s["id"] for s in subs] == ["6.1", "6.2", "6.10"]


def test_each_subsection_carries_its_own_heading():
    """A sub-chunk read on its own has to say what it is."""
    _, subs = split_file(SECTION)
    assert subs[0]["body"].startswith("### 6.1 Mutual Recognition")
    assert "Are there arrangements?" in subs[0]["body"]
    assert "Please confirm" not in subs[0]["body"]


def test_a_section_with_no_subsections_is_left_whole():
    text = "# 10. LICENCE\n\n<table><tr><td>a</td></tr></table>\n"
    parent, subs = split_file(text)
    assert subs == [] and parent == text


# -------------------------------------------- a sub-heading from ANOTHER section

# Malaysia 176985, verbatim in shape. Section 8's questionnaire ends on an "8.12 Relocation
# of Investor" row whose table spans the page break into section 10's table, so stage 3's
# table_013 opens with that label and stage 4 lifts it into a heading at the top of a
# section it has nothing to do with.
FOREIGN_LABEL = """# 10 Licence

*Source: `source_repaired.pdf`, page 84–94*

### 8.12 Relocation of Investor

<table><tr><td>(a)</td><td>What restrictions might apply?</td></tr></table>

<table><tr><td>Questions</td><td>Answers</td></tr><tr><td>Type of licence?</td><td>CMSL.</td></tr></table>
"""


def test_a_subheading_from_another_section_is_not_a_split_point():
    """`### 8.12` under `# 10 Licence` belongs to section 8. Splitting on it put the whole
    of section 10 inside 01-8.12-relocation-of-investor.md and left the parent holding
    nothing but its breadcrumb."""
    parent, subs = split_file(FOREIGN_LABEL)
    assert subs == []
    assert parent == FOREIGN_LABEL                   # nothing moved, nothing lost
    assert "Type of licence?" in parent              # section 10's own content stayed put


def test_a_foreign_label_does_not_cost_the_section_its_real_subsections():
    """Bahamas 183503: a stray 7.5 leads section 8's own 8.1..8.13. The stray is refused
    as a split point and falls into the parent; the real sub-sections still split."""
    text = ("# 8 Marketing Activities\n\n"
            "### 7.5 Investment Management\n\n<table><tr><td>strays here</td></tr></table>\n\n"
            "### 8.1 Written Materials\n\n<table><tr><td>a</td></tr></table>\n\n"
            "### 8.2 Telephone\n\n<table><tr><td>b</td></tr></table>\n")
    parent, subs = split_file(text)
    assert [s["id"] for s in subs] == ["8.1", "8.2"]
    assert "### 7.5 Investment Management" in parent
    assert "strays here" in parent
    # Every line still accounted for, which is the whole reason a refused heading is kept
    # rather than dropped. Compared non-blank: split_file rstrips each chunk, so the blank
    # line between them is the one thing that does not survive.
    def lines(s):
        return [ln for ln in s.splitlines() if ln.strip()]
    assert lines(parent) + [ln for s in subs for ln in lines(s["body"])] == lines(text)


def test_an_unnumbered_section_still_splits_on_everything():
    """Front matter has no own-number to check against, so the rule cannot apply and the
    old behaviour has to stand."""
    text = ("# Front matter\n\n### 1.1 Introduction\n\nx\n\n### 2.1 Elsewhere\n\ny\n")
    _, subs = split_file(text)
    assert [s["id"] for s in subs] == ["1.1", "2.1"]


def test_an_appendix_splits_on_its_own_restarted_numbering():
    """Appendices restart at 1, and `# Appendix 2` is the H1 shape stage 4 writes."""
    text = ("# Appendix 2 Disclaimers: Closed-Ended Fund\n\n"
            "### 2.1 Scope\n\nx\n\n### 8.12 Relocation of Investor\n\ny\n")
    _, subs = split_file(text)
    assert [s["id"] for s in subs] == ["2.1"]


def test_files_are_ordinal_prefixed_so_they_sort_correctly(tmp_path):
    """Section 8 runs to 8.13, and "8.10" sorts before "8.2" as text — the numeric
    prefix is what keeps the tree in document order."""
    job = _job(tmp_path)
    run(job)
    names = sorted(p.name for p in (job / "05_subchunks" / "06-marketing").glob("*.md"))
    assert names == ["00-section.md",                # the section's own heading, first
                     "01-6.1-mutual-recognition-of-funds-passporting-schemes.md",
                     "02-6.2-funds.md",
                     "03-6.10-something-tenth.md"]


def test_a_split_section_has_exactly_one_top_level_entry(tmp_path):
    """The parent file used to sit beside its folder under the same name, which read as
    a duplicate in the file tree rather than as a section and its parts."""
    job = _job(tmp_path)
    run(job)
    top = sorted(p.name for p in (job / "05_subchunks").iterdir()
                 if p.name != "SUBCHUNK_REPORT.json")
    assert top == ["06-marketing"]                   # the folder only, no sibling .md


def test_stage4_is_left_untouched(tmp_path):
    """Written to its own directory: both conservation checks glob *.md at the top of a
    stage dir, so moving files into folders would drop them from the checks entirely."""
    job = _job(tmp_path)
    before = sorted(p.name for p in (job / "04_stage4_ai").glob("*.md"))
    run(job)
    assert sorted(p.name for p in (job / "04_stage4_ai").glob("*.md")) == before


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _strip_crumbs(s: str) -> str:
    """Drop every *Source:* line. They are NAVIGATION, not content.

    The split now writes one into each sub-chunk -- narrowed to the pages that sub-section
    covers, so the viewer can jump to 6.2 rather than to the top of section 6 -- which
    adds characters the original section never had. Excluded from both sides so this test
    keeps measuring the thing it is for: whether any of the DOCUMENT went missing.
    """
    return re.sub(r"^\*Source:.*?\*\s*$", "", s, flags=re.M)


def test_no_content_is_lost_by_the_split(tmp_path):
    """Compared as a character multiset, not a sequence: the parent file and the folder
    are separate files now, so their relative order on disk says nothing about loss."""
    job = _job(tmp_path)
    run(job)
    rebuilt = "".join(p.read_text() for p in (job / "05_subchunks").rglob("*.md"))
    assert sorted(_norm(_strip_crumbs(SECTION))) == sorted(_norm(_strip_crumbs(rebuilt)))


def test_every_subsection_body_appears_intact(tmp_path):
    job = _job(tmp_path)
    run(job)
    rebuilt = _norm("".join(p.read_text() for p in (job / "05_subchunks").rglob("*.md")))
    for probe in ("aretherearrangements", "pleaseconfirm", "tenth"):
        assert probe in rebuilt


def test_report_records_what_was_split(tmp_path):
    job = _job(tmp_path)
    r = run(job)
    assert r["sections_split"] == 1 and r["subchunks_total"] == 3
    assert json.loads((job / "05_subchunks" / "SUBCHUNK_REPORT.json").read_text())["ran"]


def test_dry_run_writes_nothing(tmp_path):
    job = _job(tmp_path)
    r = run(job, dry_run=True)
    assert r["ran"] and not (job / "05_subchunks").exists()


@pytest.mark.parametrize("title,expected", [
    ("Investment Management &amp; Advisory Services", "investment-management-advisory-services"),
    ("Requests for Proposals (RFPs)", "requests-for-proposals-rfps"),
    ("", "section"),
])
def test_slug(title, expected):
    assert slug(title) == expected


# ---------------------------------------------------------------- section-number naming
# Stage 1 numbers files by position in the tree, which runs one ahead of the document's
# own numbering — its chunk 09 is section 8 — so a folder prefixed 09 sat above files
# named 8.1, 8.2. Stage 5 renames onto the real section number so the two agree.

from subchunk import section_prefix  # noqa: E402
import re  # noqa: E402
import subchunk as sub  # noqa: E402


def test_prefix_is_the_documents_section_number_not_the_chunk_ordinal():
    assert section_prefix("# 8 MARKETING ACTIVITIES\n", 9, "09-marketing-activities") == "08"
    assert section_prefix("# 6. MARKETING/SELLING\n", 7, "07-marketing-selling") == "06"


def test_a_section_with_no_number_falls_back_to_zero():
    assert section_prefix("# Front Matter\n", 1, "01-front-matter") == "00"


def test_disclaimers_take_an_A_prefix_because_their_numbering_restarts():
    """"1" is both BACKGROUND and Disclaimers: Open-Ended Fund, so a bare section number
    is not unique. A sorts after digits, which also keeps the appendices last."""
    assert section_prefix("# 1 Disclaimers: Open-Ended Fund\n", 13,
                          "13-disclaimers-open-ended-fund") == "A1"
    assert section_prefix("# 4 Disclaimers: Other\n", 16, "16-disclaimers-other") == "A4"


def test_the_renamed_tree_stays_in_document_order():
    names = ["00", "01", "02", "08", "11", "A1", "A4"]
    assert sorted(names) == names       # letters after digits, so appendices land last


def test_folder_prefix_matches_its_childrens_section(tmp_path):
    job = _job(tmp_path, files={"09-marketing.md":
                                "# 8 MARKETING ACTIVITIES\n\n### 8.1 Written Materials\n\n"
                                "<table><tr><td>a</td></tr></table>\n"})
    run(job)
    folder = next(p for p in (job / "05_subchunks").iterdir() if p.is_dir())
    assert folder.name == "08-marketing"
    assert any(p.name.startswith("01-8.1-") for p in folder.glob("*.md"))


# ---------------------------------------------------------------- the stage 4 -> 5 chain
# Stage 4 must never start on its own; stage 5 must always follow when it does. The
# sub-section labels only become separable BECAUSE stage 4 lifted them into headings, so
# nothing else can produce a stage 5 and every completed AI pass wants one.

CHAIN_IN = ("# 8 MARKETING ACTIVITIES\n\n"
            "<table><tr><td>Questions</td><td>Answers</td></tr>"
            "<tr><td>8.1</td><td>Written Materials</td></tr>"
            "<tr><td>(a)</td><td>Any requirement?</td></tr></table>\n")
CHAIN_OUT = ("# 8 MARKETING ACTIVITIES\n\n### 8.1 Written Materials\n\n"
             "<table><tr><td>Questions</td><td>Answers</td></tr>"
             "<tr><td>(a)</td><td>Any requirement?</td></tr></table>\n")


class _Echo:
    def converse(self, **kw):
        return {"output": {"message": {"content": [{"text": CHAIN_OUT}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "end_turn"}


def _stage4_job(tmp_path, product=MRAM):
    job = tmp_path / product / "Doc"
    s3 = job / "03_stage3_final"
    (s3 / "_assets").mkdir(parents=True)
    (s3 / "09-marketing.md").write_text(CHAIN_IN)
    (s3 / "tables_manifest.json").write_text(json.dumps({"tables": []}))
    (job / "corpus_meta.json").write_text(json.dumps({"product": product}))
    return job


def _run_stage4(job, **kw):
    """force=True: these are deliberate test runs, and the deployment flag
    (ACI_STAGE4_AI_ENABLED) is off by default so a host cannot spend money by accident.
    The gate itself is covered separately below."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from aosphere_core_index.extract import ai_postprocess as ap
    kw.setdefault("force", True)
    return ap.run_stage4(job / "03_stage3_final", job / "04_stage4_ai", None,
                         client=_Echo(), log=lambda *a: None, **kw)


def test_stage5_runs_automatically_after_stage4(tmp_path):
    job = _stage4_job(tmp_path)
    rep = _run_stage4(job)
    assert rep["subchunk"]["ran"] is True
    assert (job / "05_subchunks" / "08-marketing").is_dir()


def test_stage4_writes_the_ai_processed_flag(tmp_path):
    job = _stage4_job(tmp_path)
    _run_stage4(job)
    meta = json.loads((job / "corpus_meta.json").read_text())
    assert meta["ai_processed"] is True and meta["ai_mode"] == "section"


def test_the_chain_still_respects_the_product_gate(tmp_path):
    """Stage 4 may run for any product; stage 5 must not."""
    job = _stage4_job(tmp_path, product="155_Data_Privacy")
    rep = _run_stage4(job)
    assert rep["subchunk"]["ran"] is False
    assert not (job / "05_subchunks").exists()
    assert (job / "04_stage4_ai" / "stage4_report.json").exists()   # stage 4 still ran


def test_the_chain_can_be_turned_off(tmp_path):
    job = _stage4_job(tmp_path)
    rep = _run_stage4(job, subchunk_after=False)
    assert "subchunk" not in rep and not (job / "05_subchunks").exists()


def test_a_failing_stage5_does_not_lose_stage4(tmp_path, monkeypatch):
    """A finished, verified stage 4 must survive a broken split."""
    job = _stage4_job(tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from aosphere_core_index.extract import ai_postprocess as ap
    monkeypatch.setattr(ap, "_run_subchunk",
                        lambda *a, **k: {"ran": False, "reason": "boom"})
    rep = _run_stage4(job)
    assert rep["sections_accepted"] == 1
    assert (job / "04_stage4_ai" / "09-marketing.md").exists()


# ---------------------------------------------------------------- the deployment flag
# Stage 4 is the only stage that calls a paid API and the only one that can alter a
# document's text, so it is gated on ACI_STAGE4_AI_ENABLED and the gate sits on the pass
# itself — a programmatic caller on a host where it was never enabled must not spend
# money. `force=True` is the deliberate-local-run escape hatch the CLI uses.

def _run_gated(job, **kw):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    import importlib
    import aosphere_core_index.config as cfg
    importlib.reload(cfg)
    import aosphere_core_index.extract.ai_postprocess as ap
    importlib.reload(ap)
    return ap.run_stage4(job / "03_stage3_final", job / "04_stage4_ai", None,
                         client=_Echo(), log=lambda *a: None, **kw)


def test_stage4_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ACI_STAGE4_AI_ENABLED", raising=False)
    job = _stage4_job(tmp_path)
    r = _run_gated(job)
    assert r["disabled"] is True
    assert not (job / "04_stage4_ai").exists()      # nothing written, nothing spent
    assert r["usage"]["cost_usd"] == 0.0


def test_the_flag_enables_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_STAGE4_AI_ENABLED", "1")
    job = _stage4_job(tmp_path)
    r = _run_gated(job)
    assert not r.get("disabled") and r["sections_accepted"] == 1


def test_an_explicit_zero_still_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_STAGE4_AI_ENABLED", "0")
    assert _run_gated(_stage4_job(tmp_path))["disabled"] is True


def test_force_overrides_the_flag_for_a_deliberate_run(tmp_path, monkeypatch):
    """`hybrid_extract.py --stage4-ai` takes this path: asking on the command line IS
    the opt-in, so it must not be blocked by a host setting."""
    monkeypatch.delenv("ACI_STAGE4_AI_ENABLED", raising=False)
    r = _run_gated(_stage4_job(tmp_path), force=True)
    assert not r.get("disabled") and r["sections_accepted"] == 1


def test_a_disabled_run_does_not_trigger_stage5(tmp_path, monkeypatch):
    monkeypatch.delenv("ACI_STAGE4_AI_ENABLED", raising=False)
    job = _stage4_job(tmp_path)
    _run_gated(job)
    assert not (job / "05_subchunks").exists()


# ---- the navigation breadcrumb -----------------------------------------------------
# The review tool navigates ONLY by "*Source: `x.pdf`, page A-B*", line-anchored (see the
# regex in hybrid_extract_ui). split_file left the parent's breadcrumb in 00-section.md
# and gave the children nothing, so 25 of the 41 files in a real split had no page
# reference and clicking 6.2 left the PDF pane wherever it already was.

VIEWER_RX = re.compile(r"^\*Source:.*?page\s+(\d+)(?:\s*[–-]\s*(\d+))?\*", re.M)


def test_the_parent_breadcrumb_is_parsed_the_way_the_viewer_parses_it():
    got = sub._parent_source("# 6 X\n\n*Source: `source_repaired.pdf`, page 19–34*\n")
    assert got == ("source_repaired.pdf", 19, 34)
    # a single page, no range
    assert sub._parent_source("*Source: `a.pdf`, page 7*")[1:] == (7, 7)
    assert sub._parent_source("# 6 X\nno breadcrumb here") is None


def test_a_written_breadcrumb_is_one_the_viewer_can_read():
    """Writing a line the viewer's regex misses is the same as writing nothing."""
    out = sub._with_source("### 6.2 Funds\n\nbody\n", "source_repaired.pdf", 20, 25)
    m = VIEWER_RX.search(out)
    assert m and (m.group(1), m.group(2)) == ("20", "25")
    assert out.splitlines()[0] == "### 6.2 Funds"      # heading stays first


def test_a_single_page_subsection_is_written_without_a_range():
    m = VIEWER_RX.search(sub._with_source("### 8.2 Telephone\n", "a.pdf", 49, 49))
    assert (m.group(1), m.group(2)) == ("49", None)


def test_an_existing_breadcrumb_is_not_duplicated():
    body = "### 6.2 Funds\n\n*Source: `a.pdf`, page 20*\n"
    assert sub._with_source(body, "a.pdf", 99, 99).count("*Source:") == 1


def test_labels_are_located_in_order_and_only_inside_the_section(tmp_path):
    """Two guards, both load-bearing. The document's PRINTED CONTENTS PAGE lists every
    number, so an unrestricted search puts all of them on page 2 -- which is exactly what
    an early version of this check reported. And matching in order stops a stray mention
    claiming a heading that comes later."""
    fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")
    pdf = tmp_path / "d.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "Contents\n6.1 ...\n6.2 ...\n6.3 ...")  # p1
    doc.new_page().insert_text((72, 100), "6.1 Mutual Recognition")               # p2
    doc.new_page().insert_text((72, 100), "6.2 Funds")                            # p3
    doc.new_page().insert_text((72, 100), "6.3 Advisory")                         # p4
    doc.save(str(pdf)); doc.close()
    # the section's own range excludes the contents page
    assert sub._locate_subsections(pdf, ["6.1", "6.2", "6.3"], 2, 4) == {"6.1": 2, "6.2": 3, "6.3": 4}
    # unrestricted, the contents page would claim them all -- which is why lo/hi exists
    assert sub._locate_subsections(pdf, ["6.1", "6.2", "6.3"], 1, 4)["6.1"] == 1


def test_a_label_is_not_matched_inside_a_longer_number(tmp_path):
    fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")
    pdf = tmp_path / "d.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "16.25 something else")
    doc.new_page().insert_text((72, 100), "6.2 Funds")
    doc.save(str(pdf)); doc.close()
    assert sub._locate_subsections(pdf, ["6.2"], 1, 2) == {"6.2": 2}


def test_a_missing_pdf_still_leaves_every_subchunk_navigable(tmp_path):
    """Landing on the right SECTION beats landing nowhere: an unlocatable label falls back
    to the parent's range rather than to no breadcrumb at all."""
    assert sub._locate_subsections(tmp_path / "absent.pdf", ["6.1"], 1, 5) == {}


def test_a_breadcrumb_the_model_deleted_is_recovered_from_stage_3(tmp_path):
    """Stage 4 REWRITES each section, and an earlier prompt had it dropping the breadcrumb
    from 8 of the 20 files it touched. Stage 3 is the input it was handed and always
    carries one, so a whole section must not become unnavigable because a model deleted a
    line -- which is otherwise invisible until someone notices the PDF pane not moving.
    """
    stripped = re.sub(r"^\*Source:.*?\*\s*$", "", SECTION, flags=re.M)
    assert "*Source:" not in stripped                      # the model's damage
    job = _job(tmp_path, files={"07-marketing.md": stripped})
    # stage 3 still has the original, breadcrumb intact
    (job / "03_stage3_final" / "07-marketing.md").write_text(SECTION)

    rep = run(job)
    sec = next(s for s in rep["sections"] if s["file"] == "07-marketing.md")
    assert sec.get("source_recovered_from_stage3") is True
    for rel in sec.get("files", []):
        body = (job / "05_subchunks" / rel).read_text()
        assert VIEWER_RX.search(body), f"{rel} has no breadcrumb the viewer can read"


def test_without_stage_3_to_fall_back_on_nothing_is_invented(tmp_path):
    """No breadcrumb anywhere means no page is KNOWN. Writing a guessed one would send
    the viewer confidently to the wrong page, which is worse than not moving."""
    stripped = re.sub(r"^\*Source:.*?\*\s*$", "", SECTION, flags=re.M)
    job = _job(tmp_path, files={"07-marketing.md": stripped})
    rep = run(job)
    sec = next(s for s in rep["sections"] if s["file"] == "07-marketing.md")
    assert "source_recovered_from_stage3" not in sec
    for rel in sec.get("files", []):
        assert "*Source:" not in (job / "05_subchunks" / rel).read_text()


def test_two_sections_reducing_to_one_name_raise_instead_of_overwriting(tmp_path):
    """The stage-5 name is derived — a prefix read off the heading, a stem off the
    filename — so two sections CAN land on one name, and the write is blind: the second
    would overwrite the first and a whole section would leave stage 5 with no trace.
    That is the same silent shape as an appendix going missing, so it must be loud.
    """
    job = _job(tmp_path, files={
        "13-disclaimers.md": "# 1 Disclaimers\n\n*Source: `s.pdf`, page 9*\n\nbody\n",
        "14-disclaimers.md": "# 1 Disclaimers\n\n*Source: `s.pdf`, page 9*\n\nbody\n",
    })
    with pytest.raises(ValueError, match="name collision"):
        run(job)


def test_distinct_sections_sharing_a_number_are_not_a_collision(tmp_path):
    """Two sections legitimately carry the same number — an appendix restarts at 1 while
    a body section 1 already exists. Different stems, different names, no clash."""
    r = run(_job(tmp_path, files={
        "02-background.md": "# 1 Background\n\n*Source: `s.pdf`, page 3*\n\nbody\n",
        "13-disclaimers-open-ended-fund.md":
            "# 1 Disclaimers\n\n*Source: `s.pdf`, page 9*\n\nbody\n",
    }), dry_run=True)
    assert sorted(x["renamed"] for x in r["sections"]) == [
        "01-background", "A1-disclaimers-open-ended-fund"]


# ------------------------------------------- back matter is decided by the printed label

def test_an_appendix_whose_title_is_not_disclaimers_still_sorts_as_back_matter():
    """United Kingdom 176767. "# Appendix 5 Exempt Person under the Exemption Order" has
    no disclaimer/finsa marker in its filename, so the old rule filed it as body section
    05 — immediately before 05-passive-marketing, in the middle of the tree. The tail of
    the tree ended at A4 and appendix 5 was nowhere a reader would look."""
    assert section_prefix("# Appendix 5 Exempt Person under the Exemption Order\n",
                          17, "17-exempt-person-under-the-exemption-order") == "A5"


def test_the_bare_printed_label_under_the_heading_is_read_too():
    """Stage 3 keeps the page's own "APPENDIX 1" line below a heading that carries no
    appendix word: "# 1. Controlled Investments" over "SCHEDULE 1"."""
    assert section_prefix("# APPENDIX 1\n\n*Source: `s.pdf`, page 13*\n\nbody\n",
                          13, "13-appendix-1") == "A1"


def test_a_schedule_is_not_an_appendix():
    """Guernsey 174507 prints both a SCHEDULE 1 and an APPENDIX 1. Same number, different
    divisions — one letter each, or they collide and one file overwrites the other."""
    sched = section_prefix("# 1. Controlled Investments\n\nSCHEDULE 1\n\nbody\n",
                           2, "01-controlled-investments")
    appx = section_prefix("# 1. Disclaimers\n\nAPPENDIX 1\n\nbody\n",
                          3, "01-disclaimers-open-ended-fund")
    assert (sched, appx) == ("S1", "A1")


def test_the_label_is_recovered_from_stage_3_when_stage_4_deleted_it():
    """Russian Federation 89570. Stage 4's prompt folds an orphaned "APPENDIX 2" into the
    heading and deletes the line; there it deleted without folding, so stage 4 reads
    "# 2 Sources of Law" and only the deterministic tree still knows."""
    s4 = "# 2 Sources of Law\n\n*Source: `s.pdf`, page 16*\n\nbody\n"
    s3 = "# 2 Sources of Law\n\n*Source: `s.pdf`, page 16*\n\nAPPENDIX 2\n\nbody\n"
    assert section_prefix(s4, 5, "05-sources-of-law") == "02"
    assert section_prefix(s4, 5, "05-sources-of-law", s3) == "A2"


def test_a_reused_section_number_means_the_numbering_restarted():
    """Dubai 181097's "# 5 Recognised Jurisdictions" says appendix nowhere and carries no
    printed label, but section 5 is already Passive Marketing — the appendices restart at
    1, so a repeat is the reset."""
    seen = set()
    assert section_prefix("# 5 Passive Marketing\n\nbody\n", 6, "06-passive", None, seen) == "05"
    assert section_prefix("# 5 Recognised Jurisdictions\n\nbody\n", 17,
                          "17-recognised-jurisdictions", None, seen) == "A5"


def test_an_ordinary_body_section_is_untouched():
    seen = set()
    assert section_prefix("# 6 Marketing/Selling to the Public\n\nbody\n",
                          7, "07-marketing-selling-to-the-public", None, seen) == "06"
    assert section_prefix("# Front Matter\n\nbody\n", 1, "01-front-matter", None, seen) == "00"


def test_a_cross_reference_is_not_a_division_label():
    """"see Appendix 2.A for examples" mid-sentence must not make a section back matter."""
    body = ("# 6 Marketing/Selling to the Public\n\n"
            "Please refer to section H of Appendix 1, and see Appendix 2.A for examples.\n")
    assert section_prefix(body, 7, "07-marketing-selling-to-the-public") == "06"


def test_the_disclaimer_marker_still_covers_an_unnumbered_heading():
    """Belgium 163341 heads them "# DISCLAIMERS: OPEN-ENDED FUND" — no number to fold a
    label into, and no printed label survives stage 4 either."""
    assert section_prefix("# DISCLAIMERS: OPEN-ENDED FUND\n\nbody\n",
                          15, "15-disclaimers-open-ended-fund") == "A15"
