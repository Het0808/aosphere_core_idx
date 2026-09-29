#!/usr/bin/env python3
"""retrieval_compare_ui.py — served dashboard comparing the CURRENT HYBRID
extraction pipeline (pdf2mdtree.py / hybrid_extract.py) against the LEGACY
production system (DOCX-extracted, already embedded in
data/products/_multi/multi.npz) on retrieval quality.

Legacy: pre-built vectors from the region's source DOCX, extracted by the real
production pipeline (src/aosphere_core_index/extract, embeddings/*).
Hybrid: this repo's out/<job>/03_stage3_final tree, embedded here with the SAME
model the legacy index was built with, so the two live in one comparable vector
space.

Gold: hand-curated clause-key citations from the eval workbooks in test/.

Usage:
    python scripts/retrieval_compare_ui.py [--port 8813]
Then open http://127.0.0.1:8813/ — the comparison runs once and is cached to
out/retrieval_compare/<jurisdiction-slug>.json; use "Recompute" in the page to
redo it (e.g. after a pipeline change) without restarting the server.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import openpyxl
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from aosphere_core_index.embeddings.embedder import FastEmbedEmbedder  # noqa: E402
from aosphere_core_index.embeddings.multi_index import load_multi  # noqa: E402

CACHE_DIR = ROOT / "out" / "retrieval_compare"
EVAL_XLSX = ROOT / "test" / "aosphere adv search - SD - eval cases v1.xlsx"
CLAUSE_RE = re.compile(r"\b([A-K])\s*(\d+(?:\.\d+)*)")

# Jurisdictions with BOTH a legacy-embedded region and a hybrid pipeline output
# on disk. Add an entry here once a new pair exists; nothing else has to change.
JURISDICTIONS = {
    "au-shareholding": {
        "label": "Australia — Shareholding Disclosure",
        "legacy_region": "Shareholding Disclosure — Australia",
        "hybrid_tree": ROOT / "out/baseline/australia-share/03_stage3_final",
        "gold_prefix": "AU",
    },
}


# ---------------------------------------------------------------- gold questions

def load_gold(prefix: str) -> list[dict]:
    wb = openpyxl.load_workbook(EVAL_XLSX, data_only=True, read_only=True)
    rows = []
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        for r in ws.iter_rows(values_only=True):
            q, citation = r[0], r[2]
            if not q or not citation:
                continue
            if not str(citation).strip().upper().startswith(prefix.upper()):
                continue
            keys = [f"{letter}{num}" for letter, num in CLAUSE_RE.findall(str(citation))]
            if not keys:
                continue
            rows.append({"question": str(q).strip(), "citation": str(citation).strip(),
                        "gold_keys": keys, "sheet": sheet})
    return rows


# ---------------------------------------------------------------- hybrid tree -> sections

def load_hybrid_sections(root: Path) -> list[dict]:
    """-> [{key, title, text}]. Key scheme: letter-prefixed dotted number taken
    from the file's/folder's own numeric prefix — the document's own numbering,
    the same one the legacy DOCX extractor reads, so keys land in the same
    namespace as the gold citations without any translation table."""
    sections = []
    for p in sorted(root.rglob("*.md")):
        if p.name in ("README.md", "CONVERSION_REPORT.md", "STAGE3_REPORT.md"):
            continue
        parts = p.relative_to(root).parts
        if not parts:
            continue
        m = re.match(r'^([A-Z])-', parts[0])
        if not m:
            continue
        letter = m.group(1)
        fm = re.match(r'^0*(\d+(?:\.\d+)*)-', p.stem)
        if not fm:
            continue
        key = f"{letter}{fm.group(1)}"
        text = p.read_text(errors="ignore")
        title = text.lstrip("#").split("\n", 1)[0].strip() if text.startswith("#") else p.stem
        body = re.sub(r'!\[.*?\]\(.*?\)', ' ', text)
        body = re.sub(r'[#*`_>-]', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        sections.append({"key": key, "title": title, "text": body[:4000]})
    for d in sorted(p for p in root.rglob("*") if p.is_dir() and len(p.relative_to(root).parts) == 2):
        m = re.match(r'^([A-Z])-', d.relative_to(root).parts[0])
        fm = re.match(r'^0*(\d+)-', d.name)
        if not (m and fm):
            continue
        key = f"{m.group(1)}{fm.group(1)}"
        overview = next(d.glob("00-overview.md"), None)
        src = overview if overview and overview.exists() else (d / "README.md")
        if not src.exists():
            continue
        text = src.read_text(errors="ignore")
        body = re.sub(r'!\[.*?\]\(.*?\)', ' ', text)
        body = re.sub(r'[#*`_>-]', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        sections.append({"key": key, "title": d.name, "text": body[:4000]})
    return sections


def is_descendant(child: str, parent: str) -> bool:
    if child == parent:
        return True
    if not child.startswith(parent):
        return False
    return child[len(parent)] in ".("


def relevant_hit(gold_keys, result_keys) -> bool:
    return any(is_descendant(rk, g) or is_descendant(g, rk)
               for g in gold_keys for rk in result_keys)


def rank_of_first_hit(gold_keys, ranked_keys) -> int | None:
    for i, rk in enumerate(ranked_keys, start=1):
        if any(is_descendant(rk, g) or is_descendant(g, rk) for g in gold_keys):
            return i
    return None


# ---------------------------------------------------------------- computation

def compute_comparison(jur_key: str) -> dict:
    cfg = JURISDICTIONS[jur_key]
    mi = load_multi()
    if mi is None:
        raise RuntimeError("no data/products/_multi/multi.npz on disk")
    rows = np.where(mi.row_region == cfg["legacy_region"])[0]
    if rows.size == 0:
        raise RuntimeError(f"region {cfg['legacy_region']!r} not in multi-index "
                           f"(have: {sorted(set(mi.regions))})")
    legacy_matrix = mi.matrix[rows]
    legacy_keys = [mi.keys[i] for i in rows]
    legacy_titles = [mi.titles[i] for i in rows]

    hy_sections = load_hybrid_sections(cfg["hybrid_tree"])
    emb = FastEmbedEmbedder(mi.model)  # same model as the legacy index — fair comparison
    hy_matrix = emb.embed([f"{s['title']}. {s['text']}" for s in hy_sections])
    hy_keys = [s["key"] for s in hy_sections]
    hy_titles = [s["title"] for s in hy_sections]

    gold = load_gold(cfg["gold_prefix"])
    K = 5
    q_vecs = emb.embed([g["question"] for g in gold])
    results = []
    for g, qv in zip(gold, q_vecs):
        leg_order = np.argsort(-(legacy_matrix @ qv))[:K]
        leg_top = [{"key": legacy_keys[i], "title": legacy_titles[i][:60], "score": float((legacy_matrix @ qv)[i])}
                   for i in leg_order]
        hy_order = np.argsort(-(hy_matrix @ qv))[:K]
        hy_top = [{"key": hy_keys[i], "title": hy_titles[i][:60], "score": float((hy_matrix @ qv)[i])}
                  for i in hy_order]
        results.append({
            **g,
            "legacy_top": leg_top, "hybrid_top": hy_top,
            "legacy_hit": relevant_hit(g["gold_keys"], [t["key"] for t in leg_top]),
            "hybrid_hit": relevant_hit(g["gold_keys"], [t["key"] for t in hy_top]),
            "legacy_rank": rank_of_first_hit(g["gold_keys"], [t["key"] for t in leg_top]),
            "hybrid_rank": rank_of_first_hit(g["gold_keys"], [t["key"] for t in hy_top]),
        })

    n = len(results)
    leg_hits = sum(r["legacy_hit"] for r in results)
    hy_hits = sum(r["hybrid_hit"] for r in results)
    summary = {
        "jurisdiction": jur_key, "label": cfg["label"], "n_questions": n, "k": K,
        "n_legacy_sections": len(legacy_keys), "n_hybrid_sections": len(hy_keys),
        "legacy": {"hit@k": leg_hits / n, "hits": leg_hits,
                   "mrr": sum(1 / r["legacy_rank"] for r in results if r["legacy_rank"]) / n},
        "hybrid": {"hit@k": hy_hits / n, "hits": hy_hits,
                   "mrr": sum(1 / r["hybrid_rank"] for r in results if r["hybrid_rank"]) / n},
        "both_hit": sum(1 for r in results if r["legacy_hit"] and r["hybrid_hit"]),
        "legacy_only": sum(1 for r in results if r["legacy_hit"] and not r["hybrid_hit"]),
        "hybrid_only": sum(1 for r in results if r["hybrid_hit"] and not r["legacy_hit"]),
        "neither": sum(1 for r in results if not r["legacy_hit"] and not r["hybrid_hit"]),
    }
    return {"summary": summary, "results": results}


def _cache_path(jur_key: str) -> Path:
    return CACHE_DIR / f"{jur_key}.json"


def get_or_compute(jur_key: str, force: bool = False) -> dict:
    import json
    p = _cache_path(jur_key)
    if not force and p.exists():
        return json.loads(p.read_text())
    data = compute_comparison(jur_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    return data


# ---------------------------------------------------------------- FastAPI app

app = FastAPI(title="Retrieval A/B — legacy vs hybrid")


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_page(jur_key: str, data: dict | None, error: str | None = None) -> str:
    options = "".join(
        f'<option value="{k}" {"selected" if k == jur_key else ""}>{_esc(v["label"])}</option>'
        for k, v in JURISDICTIONS.items())

    body = f'<p class="err">Error: {_esc(error)}</p>' if error else ""
    if data:
        s = data["summary"]
        rows_html = []
        for r in data["results"]:
            def side(top, hit, rank):
                cls = "hit" if hit else "miss"
                items = "".join(
                    f"<div class='row'><span class='key'>{_esc(t['key'])}</span>"
                    f"<span class='title'>{_esc(t['title'])}</span>"
                    f"<span class='score'>{t['score']:.3f}</span></div>"
                    for t in top)
                return f"<td class='{cls}'>{items}<div class='rank'>first relevant at rank: {rank or '—'}</div></td>"

            rows_html.append(
                f"<tr><td class='q'><b>{_esc(r['question'])}</b>"
                f"<div class='gold'>gold: {_esc(', '.join(r['gold_keys']))} "
                f"<span class='cite'>({_esc(r['citation'])})</span></div></td>"
                f"{side(r['legacy_top'], r['legacy_hit'], r['legacy_rank'])}"
                f"{side(r['hybrid_top'], r['hybrid_hit'], r['hybrid_rank'])}</tr>")

        body = f"""
<div class="summary">
  <div class="card"><div class="big">{s['legacy']['hit@k']:.0%}</div><div class="lbl">Legacy hit@{s['k']}</div></div>
  <div class="card"><div class="big">{s['hybrid']['hit@k']:.0%}</div><div class="lbl">Hybrid hit@{s['k']}</div></div>
  <div class="card"><div class="big">{s['legacy']['mrr']:.3f}</div><div class="lbl">Legacy MRR</div></div>
  <div class="card"><div class="big">{s['hybrid']['mrr']:.3f}</div><div class="lbl">Hybrid MRR</div></div>
  <div class="card"><div class="big">{s['both_hit']}</div><div class="lbl">Both hit</div></div>
  <div class="card"><div class="big">{s['legacy_only']}</div><div class="lbl">Legacy-only</div></div>
  <div class="card"><div class="big">{s['hybrid_only']}</div><div class="lbl">Hybrid-only</div></div>
  <div class="card"><div class="big">{s['neither']}</div><div class="lbl">Neither</div></div>
</div>
<p class="meta">{s['n_questions']} gold questions &middot; legacy index {s['n_legacy_sections']} sections
&middot; hybrid index {s['n_hybrid_sections']} sections &middot; same embedding model</p>
<table>
<tr><th>Question</th><th>Legacy top-{s['k']}</th><th>Hybrid top-{s['k']}</th></tr>
{''.join(rows_html)}
</table>"""

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Retrieval A/B — legacy vs hybrid</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 0; padding: 24px;
       background: Canvas; color: CanvasText; }}
h1 {{ font-size: 20px; }}
.toolbar {{ display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }}
select, button {{ font-size: 14px; padding: 6px 10px; }}
.err {{ color: #e74c3c; font-weight: 600; }}
.meta {{ font-size: 12px; opacity: 0.7; }}
.summary {{ display: flex; gap: 16px; margin: 16px 0 20px; flex-wrap: wrap; }}
.card {{ border: 1px solid color-mix(in srgb, CanvasText 20%, transparent);
         border-radius: 8px; padding: 12px 16px; min-width: 130px; }}
.card .big {{ font-size: 26px; font-weight: 700; }}
.card .lbl {{ font-size: 12px; opacity: 0.7; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid color-mix(in srgb, CanvasText 15%, transparent);
          padding: 8px; vertical-align: top; text-align: left; }}
th {{ position: sticky; top: 0; background: Canvas; }}
td.q {{ width: 26%; }}
.gold {{ font-size: 12px; opacity: 0.75; margin-top: 4px; }}
.cite {{ font-style: italic; }}
td.hit {{ background: color-mix(in srgb, #2ecc71 12%, transparent); }}
td.miss {{ background: color-mix(in srgb, #e74c3c 10%, transparent); }}
.row {{ display: flex; gap: 6px; padding: 2px 0;
        border-bottom: 1px dashed color-mix(in srgb, CanvasText 10%, transparent); }}
.row .key {{ font-weight: 700; min-width: 46px; }}
.row .title {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.row .score {{ opacity: 0.6; font-family: monospace; }}
.rank {{ font-size: 11px; opacity: 0.7; margin-top: 4px; }}
</style></head><body>
<h1>Retrieval quality — Legacy (production DOCX index) vs Hybrid (pdf2mdtree/hybrid_extract)</h1>
<div class="toolbar">
  <form method="get" style="display:flex; gap:10px;">
    <select name="jur" onchange="this.form.submit()">{options}</select>
    <noscript><button type="submit">Load</button></noscript>
  </form>
  <form method="post" action="/recompute">
    <input type="hidden" name="jur" value="{jur_key}">
    <button type="submit">Recompute</button>
  </form>
</div>
{body}
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index(jur: str = "au-shareholding") -> str:
    if jur not in JURISDICTIONS:
        jur = next(iter(JURISDICTIONS))
    try:
        data = get_or_compute(jur)
        return render_page(jur, data)
    except Exception as e:  # noqa: BLE001 — surface any failure in the page itself
        return render_page(jur, None, error=f"{type(e).__name__}: {e}")


@app.post("/recompute", response_class=HTMLResponse)
def recompute(jur: str) -> str:
    try:
        data = get_or_compute(jur, force=True)
        return render_page(jur, data)
    except Exception as e:  # noqa: BLE001
        return render_page(jur, None, error=f"{type(e).__name__}: {e}")


@app.get("/api/compare")
def api_compare(jur: str = "au-shareholding") -> JSONResponse:
    if jur not in JURISDICTIONS:
        return JSONResponse({"error": f"unknown jurisdiction {jur!r}"}, status_code=404)
    try:
        return JSONResponse(get_or_compute(jur))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8813)
    args = ap.parse_args()
    print(f"Retrieval A/B -> http://{args.host}:{args.port}/  (legacy vs hybrid)")
    uvicorn.run(app, host=args.host, port=args.port)
