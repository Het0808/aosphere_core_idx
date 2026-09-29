#!/usr/bin/env python3
"""Audit exported Markdown folders against matching jurisdiction/document PDFs.

Usage: python scripts/audit_exported_chunks.py --chunks Extracted_Content_9_Jurisdictions \
    --pdfs Source_PDFs_9_Jurisdictions --output out/exported_validation
Inputs are read-only. No extraction stages or external services are required.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from check_source_fidelity import audit_source


def audit_corpus(chunks, pdfs, output):
    output.mkdir(parents=True, exist_ok=True)
    documents, unmatched = [], []
    for country in sorted(chunks.iterdir()):
        if not country.is_dir():
            continue
        for tree in sorted(country.iterdir()):
            if not tree.is_dir() or not any(tree.rglob('*.md')):
                continue
            relative = tree.relative_to(chunks)
            pdf = pdfs / relative.with_suffix('.pdf')
            if not pdf.is_file():
                unmatched.append(str(relative))
                continue
            print(f'Auditing {relative}', flush=True)
            try:
                report = audit_source(pdf, tree)
            except Exception as exc:
                report = {'passed': False, 'error': str(exc), 'findings': []}
            target = output / country.name / (tree.name + '.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, indent=2, ensure_ascii=False))
            documents.append({'document': str(relative), 'report': str(target.relative_to(output)),
                              'passed': report['passed'], 'error': report.get('error'),
                              'coverage': report.get('coverage'),
                              'review_count': report.get('review_count', 0),
                              'counts': dict(Counter(f['kind'] for f in report['findings']))})
    matched = {d['document'] for d in documents}
    missing_chunks = [str(p.relative_to(pdfs).with_suffix('')) for p in sorted(pdfs.rglob('*.pdf'))
                      if str(p.relative_to(pdfs).with_suffix('')) not in matched]
    summary = {'documents': documents, 'unmatched_chunks': unmatched, 'unmatched_pdfs': missing_chunks,
               'scope': 'Source layout audit; not a replacement for the existing content checks.'}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2))
    lines = ['# Exported chunk validation', '', summary['scope'], '',
             '| Document | Review findings | Findings by type |', '|---|---:|---|']
    for d in documents:
        counts = ', '.join(f'{k}: {v}' for k, v in d['counts'].items()) or 'None'
        lines.append(f"| [{d['document']}]({d['report'].replace(' ', '%20')}) | {d['review_count']} | {d['error'] or counts} |")
    lines += ['', 'Counts are candidates for human review, not measured precision or recall.',
              'Extractor annotations and boxed cover forms are advisory. Missing geometry is explicitly unassessed.',
              'See each JSON report for page, file, source cell text, coordinates and coverage.', '',
              f'Unmatched chunk folders: {unmatched}', f'Unmatched PDFs: {missing_chunks}', '']
    (output / 'README.md').write_text('\n'.join(lines))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chunks', required=True, type=Path)
    parser.add_argument('--pdfs', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.chunks.is_dir() or not args.pdfs.is_dir():
        parser.error('Both input directories must exist')
    summary = audit_corpus(args.chunks, args.pdfs, args.output)
    print(f"Audited {len(summary['documents'])} documents; summary: {args.output / 'README.md'}")
    return int(not summary['documents'] or bool(summary['unmatched_chunks']) or
               bool(summary['unmatched_pdfs']) or any(d['error'] or not d['passed'] for d in summary['documents']))


if __name__ == '__main__':
    raise SystemExit(main())
