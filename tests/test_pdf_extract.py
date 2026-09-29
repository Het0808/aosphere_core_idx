"""chunk_text's table-safety guard + the legacy/mineru dispatch."""

from aosphere_core_index.extract import pdf_extract
from aosphere_core_index.extract.pdf_extract import chunk_text, extract_pdf_text_via


def test_oversized_plain_paragraph_still_hard_splits():
    long_prose = "word " * 400  # ~2000 chars, no pipe rows
    chunks = chunk_text(long_prose, target=1000, overlap=150)
    assert len(chunks) > 1


def test_oversized_markdown_table_kept_as_one_chunk():
    rows = "\n".join(f"| Row {i} | value {i} | detail {i} |" for i in range(60))
    table = "| Col A | Col B | Col C |\n|---|---|---|\n" + rows
    assert len(table) > 1000
    chunks = chunk_text(table, target=1000, overlap=150)
    assert chunks == [table.strip()]  # not sliced mid-row


def test_oversized_html_table_kept_as_one_chunk():
    # Real bug found in the actual pipeline: MinerU renders complex tables as
    # a single-line raw HTML <table> (not pipe rows) — verified against a
    # real alert attachment where one ran ~4000 chars with no newlines at all.
    row = "<tr><td>Applicability</td><td>Sub-industries</td><td>Detail text here</td></tr>"
    table = "<table>" + row * 20 + "</table>"
    assert len(table) > 1000
    chunks = chunk_text(table, target=1000, overlap=150)
    assert chunks == [table.strip()]  # not sliced mid-tag


def test_extract_pdf_text_via_legacy_delegates(monkeypatch):
    monkeypatch.setattr(pdf_extract, "extract_pdf_text", lambda path: f"flat:{path}")
    assert extract_pdf_text_via("a.pdf", "legacy") == "flat:a.pdf"
