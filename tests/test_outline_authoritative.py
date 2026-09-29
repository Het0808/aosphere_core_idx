"""Data Privacy and Shareholding Disclosure bookmark finer than their printed contents page — by
design, not by defect.

check_toc_quality cross-reads the embedded outline against the contents page the document prints,
and calls a large disagreement "paragraph-level, not section-level" — status improper, score 45.
That is right for a Word export that bookmarked every styled paragraph. It is wrong for DP and SD,
which bookmark at clause level while printing a section-level contents page: 509 bookmarks against
243 printed sections on a 361-page DP survey.

The consequence was a regression on the two products that were already correct. improper scores 45,
below the fallback chain's `toc < 70` entry condition, so every DP and SD document was routed into
toc_rescue and then the whole-document MinerU tier — when the FIRST pass had already produced the
accepted result. Measured on Netherlands 174642: scored pass 94.4 with toc 100 before the check
changed; the same tree then scored toc 45 and went through both fallback tiers.

So their outline is authoritative. Every other product keeps the check.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from product_rules import OUTLINE_AUTHORITATIVE, outline_is_authoritative  # noqa: E402


@pytest.mark.parametrize("product", ["155_Data_Privacy", "104_Shareholding_Disclosure"])
def test_the_two_products_whose_outline_is_trusted(product):
    assert outline_is_authoritative(product) is True
    assert product in OUTLINE_AUTHORITATIVE


@pytest.mark.parametrize("product", [
    "124_Marketing_Restrictions_-_Asset_Management",   # genuinely needs the check (ADGM 170680)
    "117_diligence",
    "172_G20_-_Eligible_Collateral",
    "104_Shareholding_Disclosure copy",                # a near-miss name must NOT be exempted
])
def test_every_other_product_keeps_the_check(product):
    assert outline_is_authoritative(product) is False


@pytest.mark.parametrize("product", [None, ""])
def test_an_unknown_product_keeps_the_check(product):
    """A job whose product cannot be read must get the conservative behaviour, not the exemption."""
    assert outline_is_authoritative(product) is False


CORPUS = Path("out/corpus")


@pytest.mark.skipif(not (CORPUS / "155_Data_Privacy").exists(), reason="corpus not present")
def test_a_dp_document_scores_proper_end_to_end():
    """The regression itself: this tree scored toc 100 (pass 94.4), then 45 after the check
    changed, which is what sent it through the fallback tiers."""
    from check_toc_quality import compute_toc_quality
    jobs = sorted(CORPUS.glob("155_Data_Privacy/*__174642"))
    if not jobs:
        pytest.skip("Netherlands DP 174642 not extracted here")
    q = compute_toc_quality(jobs[0])
    assert q["status"] == "proper", q.get("detail")
    assert q["score"] >= 70, "must sit above the fallback chain's toc<70 entry condition"


@pytest.mark.skipif(not (CORPUS / "124_Marketing_Restrictions_-_Asset_Management").exists(),
                    reason="corpus not present")
def test_a_product_that_needs_the_check_still_fails_it():
    """The negative case, so the exemption cannot quietly become global."""
    from check_toc_quality import compute_toc_quality
    jobs = sorted(CORPUS.glob("124_Marketing_Restrictions_-_Asset_Management/*__170680"))
    if not jobs:
        pytest.skip("MRAM ADGM 170680 not extracted here")
    # The document's OWN outline, named explicitly. Left to resolve_tree_pdf this reads
    # source_repaired.pdf once the pre-flight has run on this job — 39 bookmarks that
    # agree with the printed contents page 39/39 — so the check correctly answered
    # "proper" about the repaired tree and the assertion below read as a regression in
    # the check. The negative case is about the outline the document SHIPPED with.
    q = compute_toc_quality(jobs[0], pdf=str(jobs[0] / "source.pdf"))
    assert q["status"] == "improper", "this document's outline really does disagree with its TOC"
    assert q["printed_toc"]["usable"] is True, (
        "and its printed contents page is what the repair reads — the pre-flight's "
        "disagreement trigger is silent unless this verifies")
