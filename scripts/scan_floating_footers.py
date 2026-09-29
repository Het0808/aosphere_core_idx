#!/usr/bin/env python3
"""scan_floating_footers.py — which documents have a footer/doc-control line
that repeats verbatim across pages but at NO fixed y-position?

pdf2mdtree.py's ORIGINAL running-header/footer detector bins candidate lines
by (round(y/8), text) — i.e. it assumes a repeating line always lands at the
same height. A footer/doc-control block anchored to the END of a page's own
content (not to a fixed y) breaks that assumption: it lands in a different
band on every page whose body runs a different length, so no single band
ever accumulates enough hits, and the line is never recognized as
boilerplate. That is the exact root cause behind footer text getting
mistaken for a footnote body (a bare page-number digit sitting right next to
it then reads as a footnote marker) — see the `repeated_text` fix in
pdf2mdtree.py.

This scans a corpus's SOURCE PDFs (cheap — text + layout only, no MinerU) and
flags any document where a line signature repeats across enough pages to
count as boilerplate BY TEXT, but never once by POSITION. Those are exactly
the documents pdf2mdtree's fix changes the output of; anything not flagged
here re-extracts byte-identical to before.

Usage:
    python scripts/scan_floating_footers.py out/corpus
    python scripts/scan_floating_footers.py out/corpus --json affected.json
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf2mdtree import band_key, normspace  # noqa: E402
from lib_content_compare import BOLD, DIM, GREEN, YELLOW, banner  # noqa: E402


def find_floating_footers(pdf_path: Path, min_hits: int = 4, threshold_frac: float = 0.2) -> list[dict]:
    doc = fitz.open(str(pdf_path))
    npages = doc.page_count
    band_counts = collections.Counter()
    text_bands = collections.defaultdict(set)     # text -> set of bands it appeared in
    text_pages = collections.defaultdict(set)      # text -> set of pages it appeared on
    for pno in range(npages):
        H = doc[pno].rect.height
        dd = doc[pno].get_text('dict')
        for b in dd['blocks']:
            if b['type'] != 0:
                continue
            for l in b['lines']:
                t = ''.join(s['text'] for s in l['spans'])
                y = l['bbox'][1]
                if (y < 95 or y > H - 95) and normspace(t):
                    bk = band_key(t)
                    band = round(y / 8)
                    band_counts[(band, bk)] += 1
                    text_bands[bk].add(band)
                    text_pages[bk].add(pno)
    doc.close()
    if npages == 0:
        return []

    threshold = max(min_hits, threshold_frac * npages)

    floating = []
    for bk, pages in text_pages.items():
        if not any(c.isalpha() for c in bk):
            continue  # bare-digit signatures are ambiguous (page number vs footnote marker) — not evidence either way
        if len(pages) < threshold:
            continue  # not repeated enough to be boilerplate by ANY measure
        # A signature spread across several bands (this text landed at
        # several different y's across the document) is only a REAL problem
        # if at least one of those bands, on its OWN, never accumulates
        # enough hits to cross the threshold — pdf2mdtree's original check
        # tests the CURRENT line's own band in isolation, so an instance
        # sitting in a thin band is invisible to it even though the text as
        # a whole is obviously boilerplate. If every band this text uses
        # independently clears the threshold, every instance is already
        # caught the old way and there's nothing for the new check to fix.
        bands = text_bands[bk]
        thin_bands = [band for band in bands if band_counts[(band, bk)] < threshold]
        if not thin_bands:
            continue
        floating.append({"signature": bk, "pages": len(pages), "bands_seen": len(bands),
                         "thin_bands": len(thin_bands)})
    return floating


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus_root")
    ap.add_argument("--json", default=None)
    ap.add_argument("--min-hits", type=int, default=4)
    args = ap.parse_args()

    root = Path(args.corpus_root).resolve()
    banner("Scanning for floating (position-inconsistent) footers")
    affected = []
    scanned = 0
    for product_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for job_dir in sorted(p for p in product_dir.iterdir() if p.is_dir()):
            pdf_path = job_dir / "source.pdf"
            if not pdf_path.exists():
                continue
            scanned += 1
            try:
                hits = find_floating_footers(pdf_path, min_hits=args.min_hits)
            except Exception as e:  # noqa: BLE001 — one bad PDF must not sink the scan
                print(f"  {YELLOW('ERROR')} {product_dir.name}/{job_dir.name}: {e}")
                continue
            if hits:
                key = f"{product_dir.name}/{job_dir.name}"
                affected.append({"job": key, "root": str(job_dir), "signatures": hits})
                sigs = ", ".join(f"{h['signature']!r} ({h['pages']}pp)" for h in hits[:3])
                print(f"  {YELLOW('FLOATING FOOTER')}  {key:<55} {DIM(sigs)}")

    print(f"\n{BOLD(f'{len(affected)} of {scanned} document(s) affected')}")
    if not affected:
        print(GREEN("no floating footers found — nothing needs re-extraction"))
    if args.json:
        Path(args.json).write_text(json.dumps(affected, indent=2))
        print(DIM(f"\naffected list written to {args.json}"))


if __name__ == "__main__":
    main()
