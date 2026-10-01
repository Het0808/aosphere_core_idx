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


@pytest.mark.parametrize('pdf_answer,tree_answer', [
    # Spain__176285 p57: the URL wraps mid-word, so the PDF reads "procedures22.p" / "df".
    ('https://www.cnmv.es/DocPortal/IIC/UCITSandFIAsnotificationprocedures22.p\ndf',
     'https://www.cnmv.es/DocPortal/IIC/UCITSandFIAsnotificationprocedures22.pdf'),
    # Netherlands__156999 p26: a real hyphen falls at the line end and is joined away.
    ('https://www.afm.nl/en/sector/aifm/aanmelden-of-afmelden-europees-\npaspoort.',
     'https://www.afm.nl/en/sector/aifm/aanmelden-of-afmelden-europees-paspoort.'),
])
def test_an_answer_broken_across_pdf_lines_is_not_missing(pdf_answer, tree_answer):
    src = [cell(A), cell(pdf_answer, col=1)]
    out = [cell(A), cell(tree_answer, col=1)]
    assert missing_answers(src, out) == []


def test_line_wrap_tolerance_still_binds_to_whole_words():
    # "No" must not be found inside "not" / "cannot" just because spaces are ignored.
    src = [cell(A), cell('No.', col=1)]
    out = [cell(A), cell('It is not possible and cannot be done', col=1)]
    assert missing_answers(src, out)[0][1]['actual_occurrences'] == 0


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


def test_templated_question_repeated_across_sections_is_not_a_boundary_leak(tmp_path):
    """A source row that genuinely belongs to `previous.md` also fuzzy-matches a
    NEAR-DUPLICATE row that is `licence.md`'s own, separately correct content --
    the same templated question asked once per sub-topic, one word changed each
    time. The row has a real home (previous.md, where it actually matches), so the
    coincidental match against licence.md's unrelated-but-similar content must not
    be flagged: two legitimate answers must not read as one leaked cell."""
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    tree = tmp_path/'tree'
    tree.mkdir()
    A_variant = A.replace('distribution', 'publication')      # ~92% token overlap with A
    (tree/'previous.md').write_text(
        '# 1 PREVIOUS SECTION\n\n*Source: `source.pdf`, page 1*\n'
        f'<table><tr><td>{A}</td><td>{B}</td></tr><tr><td>{B}</td><td>{A}</td></tr></table>')
    (tree/'licence.md').write_text(
        '### 2 LICENCE\n\n*Source: `source.pdf`, page 1*\n'
        f'<table><tr><td>{A_variant}</td><td>unrelated licence answer text here</td></tr></table>')
    report = audit_source(pdf, tree)
    leaks = [f for f in report['findings'] if f['kind']=='section_boundary_leak']
    assert not any(f['file']=='licence.md' for f in leaks)


def test_leftover_duplicate_is_still_flagged_even_once_absorbed_elsewhere(tmp_path):
    """The opposite of the templated-question case above: a repair pass folds a
    leaked row into a longer sentence in its rightful section (measured on UK
    176767: a 19-token orphaned clause became part of a 62-token merged question),
    but leaves the bare, unabsorbed row behind in the wrong section too. That
    leftover is still a real leak -- growth in the rightful home is what tells the
    two cases apart, not merely having a match there at all."""
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    tree = tmp_path/'tree'
    tree.mkdir()
    merged = ('(c) A longer question that swallows the orphaned clause: ' + A)
    (tree/'previous.md').write_text(
        '# 1 PREVIOUS SECTION\n\n*Source: `source.pdf`, page 1*\n'
        f'<table><tr><td>{merged}</td><td>answer text unrelated to A</td></tr></table>')
    (tree/'licence.md').write_text(
        '### 2 LICENCE\n\n*Source: `source.pdf`, page 1*\n'
        f'<table><tr><td>{A}</td><td>{B}</td></tr></table>')
    report = audit_source(pdf, tree)
    leaks = [f for f in report['findings'] if f['kind']=='section_boundary_leak']
    assert any(f['file']=='licence.md' for f in leaks)


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
def test_a_finding_that_does_not_touch_any_scored_dimension_no_longer_overrides_the_gate(tmp_path, stage):
    # 'duplicate_chunk' isn't section_boundary_leak/duplicated_content/missing_cell_answer/
    # one of the table_shape_defects kinds, so it doesn't feed Placement, Uniqueness,
    # Completeness or Fidelity -- it used to force review anyway, purely by existing.
    # The gate is the numeric threshold alone now (worst_score >= 90), so a finding
    # with nowhere to land in the scored dimensions no longer moves it; it still
    # surfaces in source_fidelity.review_count for a reader to see.
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 1}))
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'source_fidelity': {'findings': [finding('duplicate_chunk', 's.md', [1], 'dup')]}}
    report = compute_scorecard(tmp_path, validation, stage=stage)
    assert report['gate'] == 'pass'
    assert report['passed'] is True
    assert report['source_fidelity']['review_count'] == 1


@pytest.mark.parametrize('stage', [None, 5])
def test_a_boundary_leak_finding_costs_placement_at_every_stage(tmp_path, stage):
    # section_boundary_leak DOES feed Placement (see _score_placement's
    # boundary_leaks term), so it costs real, visible points instead of only
    # forcing the gate down blindly -- no separate override needed. CELL_DEFECT_PENALTY
    # is deliberately conservative for now (1.0/defect, capped at 10 -> -10 max), so
    # this term alone cannot cross the gate by itself; see test_scorecard_dimensions.py
    # for the exact arithmetic pinned per-call.
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    # Placement only gates past SHORT_DOC_MAX_PAGES (below that, short docs gate
    # on completeness alone) -- long enough here for the leak's own hit to count.
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 10}))
    leaks = [finding('section_boundary_leak', 's.md', [p], 'leak') for p in (1, 2, 3)]
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'table_placement': {'flags': [], 'tables_total': 1},
                  'source_fidelity': {'findings': leaks}}
    report = compute_scorecard(tmp_path, validation, stage=stage)
    assert report['dimensions']['placement']['score'] == 97.0     # 100 - 3*1.0
    assert report['source_fidelity']['review_count'] == 3


@pytest.mark.parametrize('stage', [None, 5])
def test_a_table_shape_defect_costs_fidelity_at_every_stage(tmp_path, stage):
    # inconsistent_table_columns/cell_split/row_merge/column_merge/block_merge are
    # recomputed fresh against whichever tree is being audited, so unlike Fidelity's
    # own stale Stage-2 bucket credit (nulled post-stage, see _score_fidelity), this
    # signal survives into the post-AI (stage 5) re-score and still costs real,
    # visible points -- the scorecard a reviewer actually opens, not just the
    # extraction gate.
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 10}))
    defects = [finding('inconsistent_table_columns', 's.md', [p], 'ragged') for p in (1, 2, 3)]
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'source_fidelity': {'findings': defects}}
    report = compute_scorecard(tmp_path, validation, stage=stage)
    assert report['dimensions']['fidelity']['score'] == 97.0       # 100 - 3*1.0
    assert report['dimensions']['fidelity']['critical'] is True
    assert report['source_fidelity']['review_count'] == 3


def test_a_pile_up_of_shape_defects_is_capped_not_unbounded(tmp_path):
    """CELL_DEFECT_CAP=10 -> at most -10 from this term, so a document with many
    genuine findings in one dimension does not get driven to zero by volume alone
    (see the 4.0/defect, uncapped-in-practice behaviour this replaced: it drove an
    8-defect real document, India__181685, from pass(93.3) to fail(68.0))."""
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 10}))
    defects = [finding('inconsistent_table_columns', 's.md', [p], 'ragged') for p in range(15)]
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'source_fidelity': {'findings': defects}}
    report = compute_scorecard(tmp_path, validation)
    assert report['dimensions']['fidelity']['score'] == 90.0       # 100 - min(15,10)*1.0


@pytest.mark.parametrize('kind', ['row_alignment', 'table_as_text', 'prose_as_table'])
def test_the_other_table_shape_kinds_also_cost_fidelity(tmp_path, kind):
    """row_alignment and table_as_text share compare_grid's cell-boundary-disagreement
    template with inconsistent_table_columns/cell_split/row_merge/column_merge/
    block_merge -- easy to miss when first wiring the group in, so pinned separately."""
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 10}))
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'source_fidelity': {'findings': [finding(kind, 's.md', [1], 'shape')]}}
    report = compute_scorecard(tmp_path, validation)
    assert report['dimensions']['fidelity']['score'] < 100


@pytest.mark.parametrize('stage', [None, 5])
def test_a_missing_cell_answer_costs_completeness_at_every_stage(tmp_path, stage):
    # missing_cell_answer is a source-cited content loss -- a short answer the
    # source's own question expects that the output row does not have -- so it
    # belongs to Completeness's own question ("is all the source content
    # present?"), recomputed fresh at every stage the same way the others are.
    pdf = tmp_path/'source.pdf'
    make_pdf(pdf)
    stage1 = tmp_path/'01_stage1'
    stage1.mkdir()
    (stage1/'stage1_report.json').write_text(json.dumps({'pages': 10}))
    missing = [finding('missing_cell_answer', 's.md', [p], 'missing') for p in (1, 2, 3)]
    validation = {'word_coverage': {'coverage_adjusted_pct': 100},
                  'source_fidelity': {'findings': missing}}
    report = compute_scorecard(tmp_path, validation, stage=stage)
    assert report['dimensions']['completeness']['score'] == 97.0   # 100 - 3*1.0
    assert report['source_fidelity']['review_count'] == 3


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
