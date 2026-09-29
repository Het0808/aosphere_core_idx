"""Imported exports must become real local gallery jobs, without invented stages."""
import json
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import import_exported_chunks as imp
import lib_validate
from aosphere_core_index.service import doc_gallery as gallery
from aosphere_core_index.service import local_extraction as local
from check_scorecard import compute_scorecard
from build_inspect import _total_pages


def pair(tmp_path):
    tree = tmp_path/'chunks'/'Country'/'Memorandum__123'
    tree.mkdir(parents=True)
    (tree/'body.md').write_text('# Body\n*Source: `original.pdf`, page 1*\nSome content.')
    pdf = tmp_path/'original.pdf'
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((40,50), 'Some content.')
        doc.save(pdf)
    return tree, pdf


def fake_gate(job):
    return ({'passed': True}, {'gate': 'review', 'worst_score': 98.2,
                              'active_finding_count': 1, 'dimensions': {},
                              'pages': {'total': 1}, 'findings': []})


def test_import_is_discoverable_with_score_pdf_and_nested_chunks(tmp_path, monkeypatch):
    tree, pdf = pair(tmp_path)
    nested = tree/'nested'
    nested.mkdir()
    (nested/'child.md').write_text('child')
    monkeypatch.setattr(imp, 'run_and_gate', fake_gate)
    corpus = tmp_path/'corpus'
    job = imp.import_pair(tree, pdf, corpus)
    assert (job/'source.pdf').read_bytes() == pdf.read_bytes()
    assert (job/'03_stage3_final/nested/child.md').read_text() == 'child'
    assert not (job/'01_stage1_extract').exists()
    assert _total_pages(job) == 1
    rows = local.discover_jobs(corpus)
    assert len(rows) == 1 and rows[0]['worst_score'] == 98.2
    store = gallery.LocalRunDocStore(corpus)
    entry = store.manifest()[0]
    assert entry['gate'] == 'review' and entry['worst_score'] == 98.2
    assert entry['viewer'].endswith('/viewer.html')
    assert entry['inspect'].endswith('/inspect.html')
    assert json.loads(store.viewer_bytes(entry['scorecard']))['worst_score'] == 98.2


def test_repeat_import_preserves_originals_and_other_jobs(tmp_path, monkeypatch):
    tree, pdf = pair(tmp_path)
    before = (tree/'body.md').read_bytes(), pdf.read_bytes()
    monkeypatch.setattr(imp, 'run_and_gate', fake_gate)
    corpus = tmp_path/'corpus'
    job = imp.import_pair(tree, pdf, corpus)
    monkeypatch.setattr(imp, 'run_and_gate', lambda *_: pytest.fail('unchanged import should be reused'))
    assert imp.import_pair(tree, pdf, corpus) == job
    assert ((tree/'body.md').read_bytes(), pdf.read_bytes()) == before
    (tree/'body.md').write_text('changed input')
    with pytest.raises(ValueError, match='Refusing to overwrite'):
        imp.import_pair(tree, pdf, corpus)


def test_rescore_refreshes_frozen_pages_and_keeps_previous_copy(tmp_path, monkeypatch):
    tree, pdf = pair(tmp_path)
    monkeypatch.setattr(imp, 'run_and_gate', fake_gate)
    job = imp.import_pair(tree, pdf, tmp_path/'corpus')
    (job/'inspect.html').write_text('old scorecard')
    imp.import_pair(tree, pdf, tmp_path/'corpus', rescore=True)
    assert not (job/'inspect.html').exists()
    assert (job/'inspect.html.previous').read_text() == 'old scorecard'


def test_import_skips_only_missing_extraction_manifests(tmp_path, monkeypatch):
    (tmp_path/'corpus_meta.json').write_text(json.dumps({'import_kind': 'exported_chunks'}))
    called=[]
    def fn(name):
        return lambda *a, **k: called.append(name) or {'passed': True}
    names=['table_placement','heading_hierarchy','word_coverage','source_fidelity']
    monkeypatch.setattr(lib_validate,'CHECKS',tuple((n,fn(n)) for n in names))
    result=lib_validate.validate(tmp_path)
    assert called == ['word_coverage','source_fidelity']
    assert result['table_placement']['skipped']
    assert result['heading_hierarchy']['available'] is False


def test_imported_page_count_is_real_and_missing_geometry_is_not_scored(tmp_path):
    tree,pdf=pair(tmp_path)
    job=tmp_path/'job'
    job.mkdir()
    (job/'source.pdf').write_bytes(pdf.read_bytes())
    (job/'corpus_meta.json').write_text(json.dumps({'import_kind':'exported_chunks','pages':1}))
    sc=compute_scorecard(job, {'word_coverage':{'coverage_adjusted_pct':95}})
    assert sc['pages']['total']==1
    assert sc['dimensions']['fidelity']['score'] is None
    assert sc['input_kind']=='exported_chunks'


def test_imported_snapshots_are_embedded_without_stage_one(tmp_path):
    from build_inspect import _assets
    assets=tmp_path/'03_stage3_final'/'_assets'
    assets.mkdir(parents=True)
    (assets/'page-001.png').write_bytes(b'png-test-data')
    payload=_assets(tmp_path)
    assert payload['page-001.png'].startswith('data:image/png;base64,')
