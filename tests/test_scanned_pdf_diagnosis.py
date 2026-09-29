"""A document with no text layer is a scan, and must say so rather than crash.

Pass 1 counts text spans across EVERY page to find the body font size, then reads
`most_common(1)[0]`. On an image-only PDF that counter is empty, so it raised IndexError — which
looks like a bug in the extractor and was recorded as one: 24 documents in a single run, every one
of them a scan. They cannot be extracted without OCR, and no amount of heading detection helps,
so the useful outcome is a diagnosis that says exactly that.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
fitz = pytest.importorskip("fitz")


def _run(pdf: Path, out: Path):
    return subprocess.run([sys.executable, str(SCRIPTS / "pdf2mdtree.py"), str(pdf),
                           "-o", str(out)], capture_output=True, text=True)


def test_an_image_only_pdf_is_diagnosed_as_a_scan(tmp_path):
    pdf = tmp_path / "scan.pdf"
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page()
        # a filled rectangle: a real block, but not a text span
        page.draw_rect(fitz.Rect(50, 50, 300, 300), fill=(0.6, 0.6, 0.6))
    doc.save(str(pdf))
    doc.close()

    r = _run(pdf, tmp_path / "out")
    assert r.returncode != 0, "a document that cannot be extracted must fail"
    combined = r.stdout + r.stderr
    assert "No text layer" in combined, combined[-400:]
    assert "SCANNED" in combined and "OCR" in combined
    assert "IndexError" not in combined, "it must not surface as a crash in our own code"


def test_the_diagnosis_is_classified_as_permanent_and_not_as_our_bug():
    """Retrying a scan changes nothing, and calling it `pipeline_bug` would send someone looking
    for a code fix that does not exist."""
    from aosphere_core_index.service.extraction_monitor import classify_failure, is_permanent
    text = ("No text layer: 34 page(s) contain no text spans at all (68 image block(s) found). "
            "This is a SCANNED document — it needs OCR")
    assert classify_failure(text) == "scanned_pdf"
    assert is_permanent("scanned_pdf") is True


def test_a_document_with_text_is_unaffected(tmp_path):
    pdf = tmp_path / "text.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "HEADING ONE", fontsize=18)
    page.insert_text((72, 140), "Body text that gives the pass a body font size to find.",
                     fontsize=10)
    doc.save(str(pdf))
    doc.close()
    r = _run(pdf, tmp_path / "out")
    assert "No text layer" not in (r.stdout + r.stderr)
