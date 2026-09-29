#!/usr/bin/env python3
"""Import PDF/Markdown pairs into Browse run without re-extracting their content.

Creates real validation.json and scorecard.json under the configured local corpus.
Existing unrelated jobs are never overwritten. Re-run with --rescore to refresh
scores for an unchanged import. Changed sources require a new corpus destination.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from lib_validate import run_and_gate

PRODUCT = '124_Marketing_Restrictions_-_Asset_Management'


def fingerprint(tree, pdf):
    digest = hashlib.sha256()
    for file in [pdf, *sorted(p for p in tree.rglob('*') if p.is_file())]:
        name = 'source.pdf' if file == pdf else file.relative_to(tree).as_posix()
        digest.update(name.encode())
        digest.update(b'\0')
        with file.open('rb') as stream:
            for block in iter(lambda: stream.read(1024*1024), b''):
                digest.update(block)
        digest.update(b'\0')
    return digest.hexdigest()


def write_json(path, value):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def import_pair(tree, pdf, corpus, *, product=PRODUCT, rescore=False):
    import fitz
    country = tree.parent.name
    kind, sep, doc_id = tree.name.rpartition('__')
    if not sep or not doc_id.isdigit():
        raise ValueError(f'Expected Document__numericID: {tree}')
    if Path(product).name != product or product in {'.', '..'}:
        raise ValueError('Product must be a single directory name')
    job = corpus/product/f'{country}__{doc_id}'
    source_id = fingerprint(tree, pdf)
    if job.exists():
        meta_path = job/'corpus_meta.json'
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        if meta.get('import_kind') != 'exported_chunks' or meta.get('source_fingerprint') != source_id:
            raise ValueError(f'Refusing to overwrite unrelated or changed import: {job}')
        if (job/'scorecard.json').exists() and not rescore:
            return job
    else:
        job.mkdir(parents=True)
        shutil.copy2(pdf, job/'source.pdf')
        shutil.copytree(tree, job/'03_stage3_final')
        with fitz.open(pdf) as document:
            pages = len(document)
        write_json(job/'corpus_meta.json', {
            'product': product, 'jurisdiction': country, 'doc_id': doc_id,
            'doc_name': kind, 'import_kind': 'exported_chunks', 'pages': pages,
            'source_chunks': str(tree.resolve()), 'source_pdf': str(pdf.resolve()),
            'source_fingerprint': source_id, 'started_at': time.time(),
        })
    start = time.monotonic()
    validation, scorecard = run_and_gate(job)
    scorecard['timing'] = {'seconds': round(time.monotonic()-start, 3),
                           'pages': json.loads((job/'corpus_meta.json').read_text())['pages'],
                           'steps': {'validation': round(time.monotonic()-start, 3)}}
    write_json(job/'validation.json', validation)
    write_json(job/'scorecard.json', scorecard)
    # Frozen review pages must match the newly written scorecard on an explicit rescore.
    if rescore:
        for name in ('viewer.html', 'inspect.html'):
            cached = job/name
            if cached.exists():
                cached.replace(job/(name+'.previous'))
    return job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chunks', type=Path, required=True)
    parser.add_argument('--pdfs', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--product', default=PRODUCT)
    parser.add_argument('--rescore', action='store_true')
    args = parser.parse_args()
    if not args.chunks.is_dir() or not args.pdfs.is_dir():
        parser.error('Both input directories must exist')
    imported, errors = [], []
    for pdf in sorted(args.pdfs.glob('*/*.pdf')):
        tree = args.chunks/pdf.parent.name/pdf.stem
        if not tree.is_dir() or not any(tree.rglob('*.md')):
            errors.append(f'Missing Markdown export: {tree}')
            continue
        print(f'Importing and scoring {tree.parent.name}/{tree.name}', flush=True)
        try:
            job = import_pair(tree, pdf, args.corpus, product=args.product, rescore=args.rescore)
            sc = json.loads((job/'scorecard.json').read_text())
            imported.append(str(job))
            print(f"  {sc['gate']} · {sc['worst_score']} · {sc['active_finding_count']} findings", flush=True)
        except Exception as exc:
            errors.append(f'{tree}: {exc}')
            print(f'  ERROR: {exc}', flush=True)
    args.corpus.mkdir(parents=True, exist_ok=True)
    write_json(args.corpus/'_export_import.json', {'jobs': imported, 'errors': errors})
    print(f'{len(imported)} documents available in Browse run → local; {len(errors)} errors.')
    return int(bool(errors) or not imported)


if __name__ == '__main__':
    raise SystemExit(main())
