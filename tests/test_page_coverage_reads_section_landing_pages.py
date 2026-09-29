"""A section's landing page is tree content, and page coverage has to read it.

check_page_coverage walked iter_content_files, which deliberately skips README.md and
00-section.md — they are not DIFF targets. That is the right rule for a diff and the
wrong one here: this check asks whether the tree accounts for a page ANYWHERE, and a
stage-5 00-section.md carries the section's own prose, its deferred MinerU table, and
the only `*Source: ..., page N-M*` line declaring that section's range.

Marketing Restrictions Hungary 167023, stage 5: section 7's 00-section.md holds a
100-row table spanning pages 47-63 and declares "page 47–64"; no other file declares
48-52. Skipping it took the declaration and the text out at once, so page 52 — whose
content is in the shipped tree, on the page the reader is looking at — was reported as
accounted for nowhere and charged to completeness as a silent loss. Germany 179874 lost
pages 63-68 the same way (8 of 8 fingerprint words present in one 84KB landing page).
"""
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_page_coverage import compute_page_coverage  # noqa: E402

# Distinctive enough to survive _page_fingerprint's rarest-word sampling, and long
# enough to clear MIN_PAGE_TOKENS.
PAGE_WORDS = {
    1: "Preamble jurisdictional memorandum introducing the questionnaire apparatus wholesale",
    2: "Sequestration abatement hypothecation covenanting subrogation indemnification novation",
    3: "Escheatment defeasance estoppel laches replevin detinue trover subrogee",
    4: "Chattel usufruct emphyteusis hereditament remainderman reversioner tenancy",
}


def _job(tmp_path: Path, landing_body: str, leaf_declares: str) -> Path:
    root = tmp_path / "job"
    (root / "05_subchunks" / "02-regime").mkdir(parents=True)
    doc = fitz.open()
    for pno in sorted(PAGE_WORDS):
        page = doc.new_page()
        for i, word in enumerate(PAGE_WORDS[pno].split()):
            page.insert_text((60, 90 + i * 16), f"{word} clause paragraph provision", fontsize=11)
    doc.save(root / "source.pdf")
    doc.close()

    (root / "05_subchunks" / "01-front.md").write_text(
        f"# Front\n\n*Source: `source.pdf`, page {leaf_declares}*\n\n{PAGE_WORDS[1]}\n",
        encoding="utf-8")
    (root / "05_subchunks" / "02-regime" / "00-section.md").write_text(
        landing_body, encoding="utf-8")
    (root / "05_subchunks" / "02-regime" / "01-detail.md").write_text(
        f"# Detail\n\n*Source: `source.pdf`, page 4–4*\n\n{PAGE_WORDS[4]}\n", encoding="utf-8")
    return root


def _landing(pages: str, body_pages: tuple[int, ...]) -> str:
    body = "\n\n".join(PAGE_WORDS[p] for p in body_pages)
    return f"# 2 Regime\n\n*Source: `source.pdf`, page {pages}*\n\n{body}\n"


def test_a_page_only_the_landing_page_declares_is_not_missing(tmp_path):
    root = _job(tmp_path, _landing("2–3", (2,)), leaf_declares="1–1")
    r = compute_page_coverage(root, stage=5)
    # Page 3 is declared ONLY by 00-section.md and its words appear nowhere else.
    assert r["pages_missing"] == []
    assert r["coverage_pct"] == 100.0


def test_a_page_only_the_landing_page_carries_the_text_of_is_not_missing(tmp_path):
    # No page range at all on the landing page: presence alone has to carry it.
    root = _job(tmp_path, f"# 2 Regime\n\n{PAGE_WORDS[2]}\n{PAGE_WORDS[3]}\n",
                leaf_declares="1–1")
    r = compute_page_coverage(root, stage=5)
    assert r["pages_missing"] == []


def test_a_page_genuinely_in_no_file_is_still_missing(tmp_path):
    """The fix must not blind the check. Page 3 is in nothing at all."""
    root = _job(tmp_path, _landing("2–2", (2,)), leaf_declares="1–1")
    r = compute_page_coverage(root, stage=5)
    assert r["pages_missing"] == [3]


@pytest.mark.parametrize("declared", ["2–3", "2-3"])
def test_both_dash_forms_of_the_landing_page_range_are_read(tmp_path, declared):
    root = _job(tmp_path, _landing(declared, (2,)), leaf_declares="1–1")
    assert compute_page_coverage(root, stage=5)["pages_missing"] == []
