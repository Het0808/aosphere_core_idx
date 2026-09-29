"""Audit extraction coverage: how much of each source docx reaches ExtractedDoc.

Compares the RAW docx (every w:p / table cell anywhere in word/document.xml —
including content nested in content-controls w:sdt and text boxes w:txbxContent)
against what the extractor captures. Reports per-doc coverage, the structural
reasons for any loss, and sample dropped paragraphs.

Audits the extractor (pre-reshape), so the per-state table reduction does NOT
count as loss.

  .venv/bin/python scripts/audit_extraction.py [Jurisdiction ...] [--sample N] [--show K] [--backend legacy|mineru]
"""
import glob
import json
import re
import sys
import zipfile
from collections import Counter

from lxml import etree

from aosphere_core_index.config import settings
from aosphere_core_index.extract import extract_document
from aosphere_core_index.regions.region_map import qualified

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("\xa0", " ")).strip().lower()


def _p_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{W}t"))


def raw_units(path: str) -> tuple[list[str], dict]:
    """Every paragraph (anywhere) + table-cell-row text in the doc, with structural tags."""
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    units, struct = [], Counter()
    for p in body.iter(f"{W}p"):
        txt = norm(_p_text(p))
        if not txt:
            continue
        anc = {etree.QName(a).localname for a in p.iterancestors()}
        if "sdt" in anc or "sdtContent" in anc:
            struct["in_content_control(w:sdt)"] += 1
        if "txbxContent" in anc:
            struct["in_textbox"] += 1
        units.append(txt)
    return units, dict(struct)


def extracted_units(path: str, meta: dict, backend: str) -> list[str]:
    doc = extract_document(path, doc_meta=meta, source_key="audit", backend=backend)
    out = []
    for s in doc.sections:
        out.append(norm(s.title))
        for e in s.elements:
            for line in e.text.split("\n"):  # tables are multi-line
                if norm(line):
                    out.append(norm(line))
    return out


def audit(jur: str, show: int, backend: str = "legacy") -> dict:
    base = settings.region_source(jur)
    metas = json.load(open(base / "Doc_metadata.json"))
    meta = next((m for m in metas if str(m.get("EXTENSION", "")).lower() == "docx"), metas[0])
    path = str(base / f"{meta['FILENAME']}.docx")
    raw, struct = raw_units(path)
    ext = extracted_units(path, meta, backend)
    ext_blob = " \n ".join(ext)
    # a raw paragraph is "covered" if its text appears within the extracted blob
    missing = [u for u in raw if u not in ext_blob and len(u) > 12]
    raw_chars = sum(len(u) for u in raw)
    miss_chars = sum(len(u) for u in missing)
    cov = 1 - (miss_chars / raw_chars) if raw_chars else 1.0
    print(f"\n=== {jur} ===")
    print(f"  raw paragraphs: {len(raw)} | extracted units: {len(ext)} | "
          f"char coverage: {cov*100:.1f}% | missing paras: {len(missing)}")
    if struct:
        print(f"  structural: {struct}")
    for m in missing[:show]:
        print(f"    DROPPED: {m[:140]}")
    return {"jur": jur, "coverage": cov, "missing_paras": len(missing),
            "raw_paras": len(raw), "struct": struct}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--show=")), 8))
    sample = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--sample=")), 0)
    backend = next((a.split("=")[1] for a in sys.argv if a.startswith("--backend=")), "legacy")
    if args:
        jurs = args
    else:
        # data/products/<product>/<jurisdiction>/source/Doc_metadata.json
        found = glob.glob(str(settings.products_dir / "*" / "*" / "source" / "Doc_metadata.json"))
        jurs = sorted({qualified(p.split("/")[-4], p.split("/")[-3]) for p in found})
        if sample:
            jurs = jurs[::max(1, len(jurs) // sample)][:sample]
    results = []
    for j in jurs:
        try:
            results.append(audit(j, show, backend))
        except Exception as e:  # noqa: BLE001
            print(f"\n=== {j} === ERROR {type(e).__name__}: {str(e)[:120]}")
    if len(results) > 1:
        avg = sum(r["coverage"] for r in results) / len(results)
        worst = sorted(results, key=lambda r: r["coverage"])[:8]
        print(f"\n==== {len(results)} docs | avg char coverage {avg*100:.1f}% ====")
        print("worst:", ", ".join(f"{r['jur']}={r['coverage']*100:.0f}%" for r in worst))
        sdt = [r["jur"] for r in results if r["struct"].get("in_content_control(w:sdt)")]
        tbx = [r["jur"] for r in results if r["struct"].get("in_textbox")]
        print(f"docs with content-control text: {len(sdt)} | with textbox text: {len(tbx)}")


if __name__ == "__main__":
    main()
