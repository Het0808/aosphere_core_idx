"""Source-based regressions for the September 2026 spreadsheet failure classes."""
import json
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_source_fidelity import (GridParser, audit_source, compare_grid, finding,
                                   heading_anchors, missing_answers, tokens)
from check_scorecard import compute_scorecard
from lib_validate import CHECKS, STAGE_SCOPED


def cell(text, row=0, col=0, **kw):
    return {'text': text, 'tokens': tokens(text), 'row': row, 'col': col,
            'rowspan': 1, 'colspan': 1, 'file': 'section.md', 'table': 0, **kw}


A = 'Alpha investment conditions require careful written approval from the local supervisory authority before distribution'
B = 'Bravo marketing restrictions prohibit unsolicited communications with prospective retail investors'


def test_clean_grid_and_typographic_quotes_do_not_flag():
    src = [cell(A + ' regulator’s approval', col=0), cell(B, col=1)]
    out = [cell(A + " regulator's approval", col=0), cell(B, col=1)]
    assert compare_grid(src, out)[0] == []


@pytest.mark.parametrize('row,col,kind', [(1, 0, 'row_merge'), (0, 1, 'column_merge')])
def test_merge_preserves_every_word_but_still_flags(row, col, kind):
    flags, _ = compare_grid([cell(A), cell(B, row=row, col=col)], [cell(A+' '+B)])
    assert kind in [f[0] for f in flags]


def test_small_tail_split_is_not_excused_by_ninety_percent_coverage():
    long = ' '.join(f'word{i}' for i in range(50))
    tail = 'reporting dates language format'
    flags, _ = compare_grid([cell(long+' '+tail)], [cell(long), cell(tail, row=1)])
    assert [f[0] for f in flags] == ['cell_split']


def test_label_only_column_is_not_a_substantive_cell_split():
    flags, _ = compare_grid([cell('(a) '+A)], [cell('(a)'), cell(A, col=1)])
    assert flags == []


def test_row_to_column_transposition_is_detected():
    flags, _ = compare_grid([cell(A), cell(B, col=1)], [cell(A), cell(B, row=1)])
    assert 'row_alignment' in [f[0] for f in flags]


def test_legitimate_rowspan_still_pairs_question_and_answer():
    flags, _ = compare_grid([cell(A), cell(B, col=1)],
                           [cell(A, rowspan=2), cell(B, row=1, col=1)])
    assert flags == []


def test_duplicate_source_text_does_not_prove_a_merge():
    assert compare_grid([cell(A), cell(A, row=1)], [cell(A)])[0] == []


def test_missing_short_answer_is_not_rescued_by_another_row():
    src = [cell(A), cell('No.', col=1)]
    out = [cell(A), cell('', col=1), cell(B, row=1), cell('No.', row=1, col=1)]
    misses = missing_answers(src, out)
    assert len(misses) == 1
    assert misses[0][1]['actual_occurrences'] == 0


def test_repeated_answer_multiplicity_in_a_merged_row():
    answer = 'N/a. Please see 7.1(a) above.'
    src = [cell(A), cell(answer, col=1), cell(B, row=1), cell(answer, row=1, col=1)]
    out = [cell(A+' '+B), cell(answer, col=1)]
    misses = missing_answers(src, out)
    assert misses[0][1]['expected_occurrences'] == 2
    assert misses[0][1]['actual_occurrences'] == 1
    out[1] = cell(answer+' '+answer, col=1)
    assert missing_answers(src, out) == []


def test_parser_preserves_spans_and_inline_word_boundaries():
    parser = GridParser()
    parser.feed('<table><tr><td rowspan="2">A <b>word</b>.</td><td>B<br>C</td></tr>'
                '<tr><td colspan="2">D</td></tr></table>')
    assert parser.cells[0]['text'] == 'A word.'
    assert parser.cells[1]['tokens'] == ['b', 'c']
    assert parser.cells[2]['col'] == 1
    assert parser.cells[2]['colspan'] == 2


def make_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 40), '1 PREVIOUS SECTION')
    page.insert_text((40, 300), '2 LICENCE')
    # Empty footer precedes headings in PDF storage order in the real documents.
    page.insert_text((40, 760), ' ')
    for x in (40, 300, 560):
        page.draw_line((x, 80), (x, 200))
    for y in (80, 140, 200):
        page.draw_line((40, y), (560, y))
    for text, box in [(A, (45, 85, 295, 135)), (B, (305, 85, 555, 135)),
                      (B, (45, 145, 295, 195)), (A, (305, 145, 555, 195))]:
        page.insert_textbox(box, text, fontsize=10)
    doc.save(path)
    doc.close()


def test_pdf_boundary_leak_and_blank_line_anchor_regression(tmp_path):
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    tree = tmp_path/'tree'
    tree.mkdir()
    (tree/'previous.md').write_text('# 1 PREVIOUS SECTION\n\n*Source: `source.pdf`, page 1*\n')
    (tree/'licence.md').write_text('### 2 LICENCE\n\n*Source: `source.pdf`, page 1*\n'
                                 f'<table><tr><td>{A}</td><td>{B}</td></tr>'
                                 f'<tr><td>{B}</td><td>{A}</td></tr></table>')
    report = audit_source(pdf, tree)
    leaks = [f for f in report['findings'] if f['kind']=='section_boundary_leak']
    assert leaks and all(f['file']=='licence.md' for f in leaks)
    assert all(280 < f['evidence']['heading_y'] < 310 for f in leaks)
    assert report['passed'] is False


def test_heading_anchor_does_not_start_at_blank_footer():
    class Page:
        def get_text(self, _):
            return {'blocks': [{'lines': [
                {'bbox': (0, 750, 10, 760), 'spans': [{'text': ' '}]},
                {'bbox': (0, 100, 10, 110), 'spans': [{'text': '10.'}]},
                {'bbox': (30, 100, 80, 110), 'spans': [{'text': 'LICENCE'}]},
            ]}]}
    anchors = heading_anchors([Page()], [{'file': 'licence.md', 'title': '10 LICENCE', 'pages': (1,1)}])
    assert anchors == [(1, 100, 'licence.md')]


def test_source_check_is_registered_and_stage_scoped():
    assert 'source_fidelity' in dict(CHECKS)
    assert 'source_fidelity' in STAGE_SCOPED


@pytest.mark.parametrize('stage', [None, 5])
def test_local_defect_forces_review_even_when_aggregate_scores_pass(tmp_path, stage):
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 1}))
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'source_fidelity': {'findings': [finding('cell_split', 's.md', [1], 'split')]}}
    report = compute_scorecard(tmp_path, validation, stage=stage)
    assert report['gate'] == 'review'
    assert report['passed'] is False
    assert report['source_fidelity']['review_count'] == 1


def test_advisory_layout_and_dismissals_do_not_force_review(tmp_path, monkeypatch):
    make_pdf(tmp_path/'source.pdf')
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 1}))
    f = finding('cell_split', 's.md', [1], 'split')
    monkeypatch.setattr('check_scorecard.dis.load_for_doc', lambda *args: {f['key']: {'reason': 'verified'}})
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'source_fidelity': {'findings': [f, finding('boxed_layout_as_text', 's.md', [1],
                                                            'layout', confidence='advisory')]}}
    report = compute_scorecard(tmp_path, validation)
    assert report['gate'] == 'pass'
    assert report['source_fidelity']['review_count'] == 0


def test_near_identical_question_elsewhere_does_not_hide_a_split():
    # Liechtenstein 4.1 repeats the same question for PPR and AIFMD passport.
    lead = 'By way of example please consider whether any additional local conditions have been imposed in order for an AIF to be marketed sold into your jurisdiction via the'
    tail = 'in respect of i reporting dates language format'
    src = lead+' private placement regime '+tail
    similar = lead+' AIFMD marketing passport '+tail
    flags, _ = compare_grid([cell(src)], [cell(lead+' private placement regime in respect of'),
                                           cell('i reporting dates language format', row=1),
                                           cell(similar, row=8)])
    assert [f[0] for f in flags] == ['cell_split']


@pytest.mark.parametrize('country,doc,page,kind', [
    ('Liechtenstein', '180334', 15, 'cell_split'),
    ('Liechtenstein', '180334', 42, 'row_merge'),
    ('Mauritius', '183013', 87, 'cell_split'),
    ('Cayman Islands', '183333', 46, 'row_merge'),
])
def test_supplied_source_page_regressions(country, doc, page, kind):
    from check_source_fidelity import read_chunks
    root = Path(__file__).resolve().parents[1]
    pdf = root/'Source_PDFs_9_Jurisdictions'/country/f'Memorandum__{doc}.pdf'
    tree = root/'Extracted_Content_9_Jurisdictions'/country/f'Memorandum__{doc}'
    if not pdf.exists() or not tree.exists():
        pytest.skip('User-supplied corpus is not installed; synthetic controls still run')
    _, output = read_chunks(tree)
    output = [c for c in output if c['pages'] and c['pages'][0] <= page <= c['pages'][1]]
    flags = []
    with fitz.open(pdf) as source:
        for table in source[page-1].find_tables(strategy='lines_strict').tables:
            cells = [cell(text, row=r, col=c) for r, row in enumerate(table.extract())
                     for c, text in enumerate(row) if text]
            flags.extend(compare_grid(cells, output)[0])
    assert kind in [f[0] for f in flags]


def test_ragged_columns_account_for_rowspan_and_colspan():
    from check_source_fidelity import ragged_tables
    good = [cell(A, rowspan=2, pages=(1,1)), cell(B, col=1, pages=(1,1)),
            cell(B, row=1, col=1, pages=(1,1))]
    assert ragged_tables(good) == []
    bad = [cell(A, colspan=2, pages=(1,1)), cell(B, row=1, colspan=3, pages=(1,1))]
    assert ragged_tables(bad)[0]['evidence']['row_widths'] == {0: 2, 1: 3}


def test_partial_duplicate_inside_a_larger_chunk_is_reported(tmp_path):
    body = ' '.join(f'word{i}' for i in range(60))
    pdf = tmp_path/'source.pdf'
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_textbox((40, 50, 550, 700), body)
        doc.save(pdf)
    tree = tmp_path/'tree'
    tree.mkdir()
    (tree/'one.md').write_text(f'# One\n*Source: `source.pdf`, page 1*\n<table><tr><td>{body}</td></tr></table>')
    (tree/'two.md').write_text(f'# Two\n*Source: `source.pdf`, page 1*\nExtra introduction\n<table><tr><td>{body}</td></tr></table>')
    report = audit_source(pdf, tree)
    assert any(f['kind'] == 'duplicated_content' for f in report['findings'])


def test_batch_reports_unmatched_inputs_and_errors(tmp_path):
    from audit_exported_chunks import audit_corpus
    chunks, pdfs, output = tmp_path/'chunks', tmp_path/'pdfs', tmp_path/'report'
    (chunks/'Country'/'Memorandum__1').mkdir(parents=True)
    (chunks/'Country'/'Memorandum__1'/'body.md').write_text('# Body')
    (pdfs/'Country').mkdir(parents=True)
    make_pdf(pdfs/'Country'/'Summary__2.pdf')
    report = audit_corpus(chunks, pdfs, output)
    assert report['unmatched_chunks'] == ['Country/Memorandum__1']
    assert report['unmatched_pdfs'] == ['Country/Summary__2']


def test_source_check_error_cannot_become_a_pass(tmp_path):
    make_pdf(tmp_path/'source.pdf')
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 1}))
    report = compute_scorecard(tmp_path, {'word_coverage': {'coverage_adjusted_pct': 100},
                                         'source_fidelity': {'passed': False, 'error': 'unreadable PDF'}})
    assert report['gate'] == 'review'
    assert 'could not be checked' in report['gate_reasons'][0]
