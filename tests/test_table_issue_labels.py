"""A merged CELL is not a merged ROW.

The detector reports where the pieces of a PDF cell ended up and names the finding after the
direction: row_merge for cells stacked in a column that came out as one, column_merge for cells
side by side. Read as a label, "row merge" claimed the whole row had collapsed, when on the
corpus every one of those findings is a single column's cells sharing a cell and the answers
beside them intact (Botswana__183055 p26, Morocco__168888 p35, United States__170841 p69 ...).

The display name now says what happened to the table:

    MERGED_CELL   cells of ONE column (or one row, or a block) came out as one cell
    MERGED_ROW    the same rows were merged in TWO OR MORE columns -- the whole row structure
    SPLIT_CELL    one cell came out as several

Only the name and wording change. The detector's kind, the finding key (so a reviewer's
dismissal keeps matching), the severity and the cost are exactly what they were.
"""
import hashlib
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_source_fidelity import audit_source, label_flags  # noqa: E402

TXT = {
    'a': 'Alpha investment conditions require careful written approval from the local supervisory authority before distribution',
    'b': 'Bravo marketing restrictions prohibit unsolicited communications with prospective retail investors in this jurisdiction',
    'c': 'Charlie disclosure obligations demand that every prospectus includes a prominent risk warning for all recipients',
    'd': 'Delta record keeping duties oblige managers to retain complete client correspondence for at least five years',
}


def make_pdf(path):
    """One ruled 2 x 2 table: row 1 = a | b, row 2 = c | d."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 40), '1 LICENCE')
    for x in (40, 300, 560):
        page.draw_line((x, 80), (x, 200))
    for y in (80, 140, 200):
        page.draw_line((40, y), (560, y))
    for key, box in (('a', (43, 83, 297, 137)), ('b', (303, 83, 557, 137)),
                     ('c', (43, 143, 297, 197)), ('d', (303, 143, 557, 197))):
        page.insert_textbox(box, TXT[key], fontsize=8)
    doc.save(path)
    doc.close()


def table(*rows):
    def cell(c):
        if isinstance(c, tuple):                       # (text, colspan)
            return f'<td colspan="{c[1]}">{c[0]}</td>'
        return f'<td>{c}</td>'
    return '<table>' + ''.join('<tr>' + ''.join(cell(c) for c in r) + '</tr>' for r in rows) + '</table>'


def audit(tmp_path, body):
    make_pdf(tmp_path / 'source.pdf')
    tree = tmp_path / 'tree'
    tree.mkdir()
    (tree / '1.md').write_text('### 1 LICENCE\n\n*Source: `source.pdf`, page 1*\n\n' + body)
    return [f for f in audit_source(tmp_path / 'source.pdf', tree)['findings']
            if f['kind'] in ('row_merge', 'column_merge', 'block_merge', 'cell_split')]


A, B, C, D = (TXT[k] for k in 'abcd')


def test_cells_stacked_in_one_column_are_a_merged_cell_not_a_merged_row(tmp_path):
    # a and c (the whole first column) share one cell; b and d, beside them, are intact.
    (f,) = audit(tmp_path, table([A + ' ' + C, B], ['', D]))
    assert f['kind'] == 'row_merge'                       # what the detector found: unchanged
    assert f['tag'] == 'MERGED_CELL'
    assert f['title'] == 'merged cell'
    assert 'MERGED_CELL' in f['detail'] and 'not a merged row' in f['detail']
    assert 'MERGED_ROW' not in f['detail']


def test_the_same_rows_merged_in_both_columns_are_a_merged_row(tmp_path):
    # Row 1 and row 2 became ONE row: question cells merged and answer cells merged.
    found = audit(tmp_path, table([A + ' ' + C, B + ' ' + D]))
    assert sorted(f['kind'] for f in found) == ['row_merge', 'row_merge']
    assert {f['tag'] for f in found} == {'MERGED_ROW'}
    assert all('whole row structure' in f['detail'] for f in found)


def test_cells_side_by_side_are_a_merged_cell_that_spans_columns(tmp_path):
    # a and b, the two cells of the first row, came out as ONE cell spanning both columns.
    (f,) = audit(tmp_path, table([(A + ' ' + B, 2)], [C, D]))
    assert f['kind'] == 'column_merge'
    assert f['tag'] == 'MERGED_CELL'
    assert 'cell spans/merges across columns' in f['detail']


def test_one_cell_cut_in_two_is_a_split_cell(tmp_path):
    words = A.split()
    h = len(words) // 2
    found = audit(tmp_path, table([' '.join(words[:h]), B], [' '.join(words[h:]), D], [C, '']))
    split = [f for f in found if f['kind'] == 'cell_split']
    assert len(split) == 1
    assert split[0]['tag'] == 'SPLIT_CELL'
    assert split[0]['title'] == 'split cell'
    assert 'SPLIT_CELL' in split[0]['detail']


def test_a_clean_table_gets_no_merge_or_split_finding(tmp_path):
    assert audit(tmp_path, table([A, B], [C, D])) == []


def test_naming_changes_nothing_the_scoring_or_a_dismissal_depends_on(tmp_path):
    (f,) = audit(tmp_path, table([A + ' ' + C, B], ['', D]))
    # The key hashes kind / file / pages / evidence only -- never the tag, title or wording.
    identity = repr((f['kind'], f['file'], f['pages'], f['evidence']))
    assert f['key'] == 'source-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
    assert f['severity'] == 'review' and f['dimension'] == 'source_fidelity'
    assert 'tag' not in f['evidence']


def test_the_explanation_still_cites_the_source_cells(tmp_path):
    (f,) = audit(tmp_path, table([A + ' ' + C, B], ['', D]))
    assert 'Source: ' in f['detail']
    assert 'row 1, column 1' in f['detail'] and 'row 2, column 1' in f['detail']


# ---------------------------------------------------------------- label_flags on its own
def src(*cells):
    """Source cells long enough for the detector to see (MIN_CELL_TOKENS or more words)."""
    return [{'row': r, 'col': c, 'tokens': ['w'] * 10} for r, c in cells]


@pytest.mark.parametrize('flags, source, expect', [
    # three stacked cells, one column -> merged cell
    ([('row_merge', [0, 1, 2], [0])], src((0, 0), (1, 0), (2, 0)), ['MERGED_CELL']),
    # the same two rows merged in columns 0 and 1 -> merged row, for both findings
    ([('row_merge', [0, 1], [0]), ('row_merge', [2, 3], [1])],
     src((0, 0), (1, 0), (0, 1), (1, 1)), ['MERGED_ROW', 'MERGED_ROW']),
    # different rows merged in each column -> two merged cells, not a merged row
    ([('row_merge', [0, 1], [0]), ('row_merge', [2, 3], [1])],
     src((0, 0), (1, 0), (1, 1), (2, 1)), ['MERGED_CELL', 'MERGED_CELL']),
    # two columns of a THREE-column table merged over the same rows: the third is intact,
    # so the row structure did not collapse -- two merged cells
    ([('row_merge', [0, 1], [0]), ('row_merge', [2, 3], [1])],
     src((0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 2)), ['MERGED_CELL', 'MERGED_CELL']),
    # the third column holds only short answers the detector cannot see: it does not
    # count against calling the rows merged
    ([('row_merge', [0, 1], [0]), ('row_merge', [2, 3], [1])],
     src((0, 0), (1, 0), (0, 1), (1, 1)) + [{'row': 0, 'col': 2, 'tokens': ['No']},
                                            {'row': 1, 'col': 2, 'tokens': ['No']}],
     ['MERGED_ROW', 'MERGED_ROW']),
    ([('column_merge', [0, 1], [0])], src((0, 0), (0, 1)), ['MERGED_CELL']),
    # a 2 x 2 block that is ALL of those rows collapsed into one cell
    ([('block_merge', [0, 1, 2, 3], [0])], src((0, 0), (0, 1), (1, 0), (1, 1)), ['MERGED_ROW']),
    # a block that is only part of its rows is a merged cell
    ([('block_merge', [0, 1, 2, 3], [0])],
     src((0, 0), (0, 1), (1, 0), (1, 1), (0, 2), (1, 2)), ['MERGED_CELL']),
    ([('cell_split', [0], [0, 1])], src((0, 0)), ['SPLIT_CELL']),
])
def test_label_flags(flags, source, expect):
    assert [tag for tag, _ in label_flags(flags, source)] == expect


def test_findings_that_are_not_merges_or_splits_keep_their_wording():
    assert label_flags([('row_alignment', [0, 1], [0, 1])], src((0, 0), (0, 1))) == [(None, None)]
