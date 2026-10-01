"""Prose rendered as a table is a finding, not a footnote.

Russian Federation 89570 p9-14: the PDF prints "Counsel's Guidance on General Marketing" as
a heading with paragraphs under it -- no ruled lines, no columns -- and the exported chunk
lays it out as a two-column table, heading in one cell and its text in the other. Nothing
was lost, and the validator did see it, but only as the advisory `table_without_source_grid`
that is also what a perfectly good BORDERLESS table (term | definition printed side by side)
produces. Advisory means no score, no gate and no place in Issues Requiring Attention, so
the 26 real cases across six executive summaries read the same as 155 harmless ones.

What tells them apart is the page. In a borderless table the cells of a row are printed side
by side; in prose the second cell starts BELOW the first, from the same left margin.
"""
import json
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_source_fidelity import audit_source, layout_verdict  # noqa: E402

P1 = ('In respect of General Marketing Activities that take place at a fly-in event or where relevant remotely '
      'counsel recommend that the following precautions be observed persons should be specifically invited '
      'or addressed by name and or company and a record of those invited should be kept')
P2 = ('An OFI may not market or provide IMAS cross-border in Russia as such activities require a Local Licence '
      'which cannot be granted to an OFI unless the Limited Number Exclusion applies and the following '
      'conditions have been satisfied in full before any communication is made')
H1, H2 = 'Counsel Guidance on General Marketing', 'General Licensing Restriction'


def stacked_page(page, heading, text, y=100):
    """Prose: the heading on its own line, its paragraph beneath it from the same margin."""
    page.insert_text((50, y), heading, fontsize=11)
    page.insert_textbox((50, y + 8, 550, y + 150), text, fontsize=9)


def columns_page(page, term, text, y=100):
    """A borderless table: the term on the left, its definition beside it on the same line."""
    page.insert_text((50, y + 10), term, fontsize=9)
    page.insert_textbox((300, y, 560, y + 150), text, fontsize=9)


def table(*rows):
    return '<table>' + ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows) + '</table>'


def run(tmp_path, draw, rows, pages=1):
    doc = fitz.open()
    for n in range(pages):
        draw(doc.new_page(width=600, height=800), n)
    doc.save(tmp_path / 'source.pdf')
    doc.close()
    tree = tmp_path / 'tree'
    tree.mkdir()
    rng = f'{1}–{pages}' if pages > 1 else '1'
    (tree / '1.md').write_text(f'### 2.8 Definitions\n\n*Source: `source.pdf`, page {rng}*\n\n' + table(*rows))
    return audit_source(tmp_path / 'source.pdf', tree)['findings']


def kinds(findings):
    return sorted(f['kind'] for f in findings if f['kind'] in ('prose_as_table', 'table_without_source_grid'))


def test_headings_with_paragraphs_under_them_laid_out_as_a_table_are_a_finding(tmp_path):
    found = run(tmp_path, lambda pg, n: stacked_page(pg, H2, P2), [[H2, P2]])
    (f,) = [x for x in found if x['kind'] == 'prose_as_table']
    assert f['severity'] == 'review'                       # NOT advisory: it counts
    assert f['pages'] == [1, 1] and f['file'] == '1.md'
    assert 'no side-by-side columns' in f['detail'] and 'table structure is invented' in f['detail']
    assert 'table_without_source_grid' not in kinds(found)  # replaced, not doubled


def test_a_curly_apostrophe_in_the_pdf_does_not_hide_the_heading():
    # The PDF prints Counsel\u2019s; the tree has Counsel's. The search is retried with the
    # quote style swapped. (A stand-in page: the base PDF fonts cannot print a curly quote.)
    class Page:
        def search_for(self, phrase):
            return [fitz.Rect(50, 100, 200, 112)] if phrase.startswith('Counsel\u2019s Guidance') else []
    from check_source_fidelity import _first_rect
    assert _first_rect(Page(), "Counsel's Guidance on General Marketing") is not None
    assert _first_rect(Page(), 'No such words on the page') is None


def test_a_borderless_table_printed_in_columns_stays_advisory(tmp_path):
    found = run(tmp_path, lambda pg, n: columns_page(pg, 'Limited Number Exclusion', P2), [['Limited Number Exclusion', P2]])
    assert kinds(found) == ['table_without_source_grid']
    (f,) = [x for x in found if x['kind'] == 'table_without_source_grid']
    assert f['severity'] == 'advisory'


def test_a_summary_running_over_several_pages_is_one_finding_not_one_per_page(tmp_path):
    texts = [(H1, P1), (H2, P2)]
    found = run(tmp_path, lambda pg, n: stacked_page(pg, *texts[n]), [[H1, P1], [H2, P2]], pages=2)
    prose = [x for x in found if x['kind'] == 'prose_as_table']
    assert len(prose) == 1
    assert prose[0]['pages'] == [1, 2]


def test_a_table_whose_cells_cannot_be_located_is_never_called_prose(tmp_path):
    # One-column table: no neighbouring pair to judge, so the verdict is "unknown", and
    # unknown stays on the advisory path.
    found = run(tmp_path, lambda pg, n: stacked_page(pg, H2, P2), [[P2]])
    assert 'prose_as_table' not in kinds(found)


def test_layout_verdict_directly(tmp_path):
    doc = fitz.open()
    stacked_page(doc.new_page(width=600, height=800), H2, P2)
    columns_page(doc.new_page(width=600, height=800), 'Limited Number Exclusion', P2)
    doc.save(tmp_path / 'two.pdf')
    doc.close()
    doc = fitz.open(tmp_path / 'two.pdf')              # pages stay valid only on a settled document
    prose, cols = doc[0], doc[1]
    cell = lambda r, c, t: {'row': r, 'col': c, 'text': t}
    assert layout_verdict(prose, [cell(0, 0, H2), cell(0, 1, P2)]) == 'prose'
    assert layout_verdict(cols, [cell(0, 0, 'Limited Number Exclusion'), cell(0, 1, P2)]) == 'columns'
    assert layout_verdict(prose, [cell(0, 0, 'words that are nowhere on this page at all'), cell(0, 1, P2)]) == 'unknown'


def test_an_identical_finding_keeps_the_same_key_so_dismissals_hold(tmp_path):
    a = run(tmp_path, lambda pg, n: stacked_page(pg, H2, P2), [[H2, P2]])
    other = tmp_path / 'again'
    other.mkdir()
    b = run(other, lambda pg, n: stacked_page(pg, H2, P2), [[H2, P2]])
    assert [f['key'] for f in a if f['kind'] == 'prose_as_table'] == [f['key'] for f in b if f['kind'] == 'prose_as_table']
