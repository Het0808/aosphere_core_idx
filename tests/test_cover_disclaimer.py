"""The cover Disclaimer the summary-AI prompt is told to omit is not a content gap.

These documents carry a boilerplate notice on the cover — "Disclaimer: This document
contains a high level summary of information contained in aosphere Limited's proprietary
subscription services…" — and the summary pipeline's prompt is instructed to leave it
out. Boilerplate detection cannot excuse it: that works by finding lines that RECUR
across pages, and this one is printed once. So every conservation check read the notice
as source text the tree had dropped and charged the document for doing as it was told —
15 of the 22 summary-route documents in the 16-09-26 corpus, Guatemala 32221 among them,
which was FAILING partly on that.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from lib_content_compare import (  # noqa: E402
    COVER_DISCLAIMER, strip_cover_disclaimer, summary_ai_route,
)

NOTICE = ("Disclaimer: \nThis document contains a high level summary of information "
          "contained in aosphere Limited's proprietary subscription services.\n\n")
BODY = "Marketing Restrictions\n\nActive marketing is prohibited without a licence.\n"


def _pages(*texts):
    """pdf_page_texts is 1-indexed — pages[0] is a placeholder, pages[1] is page 1."""
    return ["", *texts]


def test_the_cover_notice_is_removed_from_page_one():
    out = strip_cover_disclaimer(_pages(NOTICE + BODY), True)
    assert "high level summary" not in out[1]
    assert "Active marketing is prohibited" in out[1]


def test_only_page_one_is_touched():
    """The notice is a COVER notice. A later page keeping the word must not be eaten."""
    out = strip_cover_disclaimer(_pages(BODY, NOTICE + BODY), True)
    assert "high level summary" in out[2]


def test_a_full_route_document_keeps_it():
    """Malta 181816's front matter carries the notice, and its tree keeps it. Stripping it
    there would stop the checks noticing if it ever went missing for real."""
    out = strip_cover_disclaimer(_pages(NOTICE + BODY), False)
    assert "high level summary" in out[1]


def test_body_prose_mentioning_a_disclaimer_survives():
    """The colon is required and the run stops at the first blank line — without both, a
    stray "disclaimer" in body prose eats the rest of the page."""
    prose = "The disclaimer wording in marketing materials must be prominent.\n\nMore.\n"
    out = strip_cover_disclaimer(_pages(prose), True)
    assert out[1] == prose


def test_the_run_is_bounded():
    """600 characters, so a cover with no blank line after the notice cannot take the
    whole page with it."""
    out = strip_cover_disclaimer(_pages("Disclaimer: " + ("x " * 2000) + "\n\ntail"), True)
    assert "tail" in out[1]
    assert len(out[1]) > 2000        # the bounded run left the overflow behind


def test_the_regex_is_shared_with_the_summary_pipeline():
    """One source of truth: if these drift, the pipeline omits one paragraph and the
    scorecard excuses a different one."""
    import summary_ai_extract
    assert summary_ai_extract._DISCLAIMER is COVER_DISCLAIMER


def test_the_route_is_read_from_the_rule_file(tmp_path):
    """Evidence of the route ACTUALLY taken, written by the router at extraction time."""
    assert not summary_ai_route(tmp_path)
    (tmp_path / "summary_ai_rule.json").write_text(json.dumps({"module": "summary_ai_extract"}))
    assert summary_ai_route(tmp_path)
