"""Appendix integrity: an appendix the memo PRINTS that never became a chunk.

These memos head each appendix with a bare ALL-CAPS "APPENDIX n" label over the title,
and neither line is a markdown heading. Where the outline also missed the title, the
whole appendix is absorbed into the one above it and every other check stays clean —
the text is all still in the tree. This is the only check that can see it.

The three discriminations below are the check: each one exists because a real document
in the corpus would otherwise be scored wrong.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pytest
from check_appendix_integrity import compute_appendix_integrity, scan_file

MRAM = "124_Marketing_Restrictions_-_Asset_Management"

_LOREM = ("Disclaimer wording that runs well past the prose floor so the label above it "
          "reads as a division of this document and not as a stray trailing label. ")


def _job(tmp_path, files, product=MRAM):
    job = tmp_path / product / "Jurisdiction__1"
    s3 = job / "03_stage3_final"
    s3.mkdir(parents=True)
    for name, body in files.items():
        (s3 / name).write_text(body)
    (job / "corpus_meta.json").write_text(json.dumps({"product": product}))
    return job


# ------------------------------------------------- what a swallowed division looks like

def test_label_with_prose_after_it_is_a_swallowed_division():
    """Malta 181816 and Iceland 181135: appendices 3 and 4 inside the appendix-2 file."""
    owned, buried = scan_file(
        "# Appendix 2 Disclaimers: Closed-Ended Fund\n\nAPPENDIX 2\n\n"
        + _LOREM + "\n\n**APPENDIX 3**\n\n**DISCLAIMERS: IMAS**\n\n" + _LOREM)
    assert owned == {"app2"}
    assert [k for k, _ in buried] == ["app3"]


def test_the_label_opening_its_own_file_is_owned_not_buried():
    owned, buried = scan_file(
        "# Appendix 3 Disclaimers: IMAS\n\n*Source: `s.pdf`, page 5*\n\n"
        "APPENDIX 3\n\n**DISCLAIMERS: IMAS**\n\n" + _LOREM)
    assert owned == {"app3"} and buried == []


# ------------------------------------------------------------- the three discriminations

def test_a_label_stranded_at_the_end_of_a_file_is_not_a_missed_division():
    """The ORPHAN case. The label fell one paragraph short and sits at the bottom of the
    section BEFORE the one it names — pdf2mdtree's --group-labels pass moves it. The
    appendix itself has its own chunk, so flagging it would be wrong. Scoring this as a
    defect flagged 116 of 350 trees on disk."""
    owned, buried = scan_file("# 11 Penalties/Sanctions\n\n" + _LOREM + "\n\nAPPENDIX 1\n")
    assert buried == []


def test_a_footnote_body_after_an_orphaned_label_is_not_appendix_content():
    """Uruguay 175072: "APPENDIX 2" at the end of the appendix-1 file, followed only by a
    horizontal rule and `[^26]: Restatement on Rules of the Securities Market...`."""
    owned, buried = scan_file(
        "# 1 Disclaimers: Open-Ended Fund\n\n" + _LOREM + "\n\nAPPENDIX 2\n\n---\n\n"
        "[^26]: Restatement on Rules of the Securities Market of the Central Bank of "
        "Uruguay, article 222, and a good deal of further citation text besides.\n")
    assert buried == []


def test_a_title_case_appendix_is_a_reference_not_a_division():
    """Malaysia 176985's appendix 5 quotes the Malaysian SC's "Foreign Funds Guidelines"
    verbatim — that instrument's own Appendix 1-4, title case. Four false positives if
    the match is case-insensitive."""
    owned, buried = scan_file(
        '# 5 Extract from the "Guidelines for the Offering of Foreign Funds"\n\n'
        + _LOREM + "\n\nAppendix 1\n\n" + _LOREM + "\n\nAppendix 2\n\n" + _LOREM)
    assert buried == []


def test_a_cross_reference_mid_sentence_is_never_a_division():
    owned, buried = scan_file(
        "# 2 Disclaimers\n\nPlease refer to section H of Appendix 1 that will apply "
        "equally to ELTIFs, and see Appendix 2.A for examples.\n\n" + _LOREM)
    assert owned == set() and buried == []


# ------------------------------------------------------------------------- the job gate

def test_a_swallowed_appendix_fails_the_check(tmp_path):
    job = _job(tmp_path, {
        "13-disclaimers-open-ended-fund.md":
            "# Appendix 1 Disclaimers\n\nAPPENDIX 1\n\n" + _LOREM,
        "14-disclaimers-closed-ended-fund.md":
            "# Appendix 2 Disclaimers\n\nAPPENDIX 2\n\n" + _LOREM
            + "\n\n**APPENDIX 3**\n\n**DISCLAIMERS: IMAS**\n\n" + _LOREM,
    })
    r = compute_appendix_integrity(job)
    assert not r["passed"]
    assert r["swallowed_count"] == 1
    assert r["swallowed"][0]["label"] == "app3"
    assert r["swallowed"][0]["absorbed_into"] == "14-disclaimers-closed-ended-fund.md"
    assert r["score"] == pytest.approx(66.7, abs=0.1)


def test_every_division_having_a_chunk_passes(tmp_path):
    job = _job(tmp_path, {
        "13-a.md": "# Appendix 1 Disclaimers\n\nAPPENDIX 1\n\n" + _LOREM,
        "14-b.md": "# Appendix 2 Disclaimers\n\nAPPENDIX 2\n\n" + _LOREM,
    })
    r = compute_appendix_integrity(job)
    assert r["passed"] and r["score"] == 100.0 and r["swallowed"] == []


def test_a_document_printing_no_divisions_is_not_penalised(tmp_path):
    job = _job(tmp_path, {"02-background.md": "# 1 Background\n\n" + _LOREM})
    r = compute_appendix_integrity(job)
    assert r["passed"] and r["score"] == 100.0


def test_families_are_keyed_separately(tmp_path):
    """Guernsey 174507 prints both a SCHEDULE 1 and an APPENDIX 1; they are different
    divisions and an owned APPENDIX 1 must not excuse a swallowed SCHEDULE 1."""
    owned, buried = scan_file(
        "# 1. Controlled Investments\n\nSCHEDULE 1\n\n" + _LOREM
        + "\n\n**SCHEDULE 2**\n\n**Designated Jurisdictions**\n\n" + _LOREM)
    assert owned == {"sch1"} and [k for k, _ in buried] == ["sch2"]


# ------------------------------------------------------------------- the product scope

def test_any_other_product_is_not_scored(tmp_path):
    """The ALL-CAPS-label convention is 124's. Another product must score None and pass
    rather than be judged against a shape its documents do not use."""
    job = _job(tmp_path, {
        "01-a.md": "# Appendix 1\n\nAPPENDIX 1\n\n" + _LOREM
                   + "\n\n**APPENDIX 2**\n\n" + _LOREM,
    }, product="155_Data_Privacy")
    r = compute_appendix_integrity(job)
    assert r["passed"] and r["score"] is None and r["available"] is False
    assert "not in APPENDIX_CHUNK_PRODUCTS" in r["reason"]
