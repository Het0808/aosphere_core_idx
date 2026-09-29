"""Reusable core for the chunker head-to-head eval (used by the eval UI and the
`scripts/eval_chunkers.py` CLI).

The ONLY variable across the two arms is the chunking: the same document supplied
as a PDF (chunked by MinerU) and as a DOCX (chunked by the legacy Word-style
extractor) is embedded with the SAME local bge model and scored with the SAME
citation scorer against that region's gold eval cases (question + expected clause
key). Synthetic questions (Bedrock, optional) are a supplement.
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts"))
import eval_cases as EC  # load_cases, hit_ok, _nk, section_rank  (reads test/*.xlsx)


# --- regions that actually have gold cases -----------------------------------
def list_regions() -> list[dict]:
    """(product, jurisdiction, case_count) for every region with >=1 gold case,
    so the UI only offers regions we can actually score."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for c in EC.load_cases():
        if c["expected"]:
            counts[(c["product"], c["expected"][0][0])] += 1
    out = [{"product": p, "jurisdiction": j, "cases": n} for (p, j), n in counts.items()]
    out.sort(key=lambda d: -d["cases"])
    return out


def load_gold(product: str, jurisdiction: str) -> list[dict]:
    return [c for c in EC.load_cases()
            if c["product"] == product and c["expected"] and c["expected"][0][0] == jurisdiction]


# --- chunking -----------------------------------------------------------------
def _meta(path, product, jurisdiction, ext):
    return {"DOCID": os.path.basename(path), "DOCNAME": f"{jurisdiction} — {product}",
            "JURISDICTIONNAME": jurisdiction, "JURISDICTIONID": None, "OPINIONID": None,
            "RENDERSTYLENAME": product, "EXTENSION": ext}


def chunk_docx(path, product, jurisdiction):
    from aosphere_core_index.extract.docx_extract import extract_docx
    return extract_docx(path, doc_meta=_meta(path, product, jurisdiction, "docx"), source_key="eval")


def chunk_pdf(path, product, jurisdiction, backend=None, effort=None):
    """backend/effort default to settings.mineru_backend/mineru_effort (run_mineru's
    own fallback) — None here, not a hardcoded engine, so the eval UI honors whatever
    backend is configured instead of silently pinning one."""
    from aosphere_core_index.extract.mineru_extract import blocks_to_doc, run_mineru
    blocks = run_mineru(path, backend=backend, effort=effort)
    return blocks_to_doc(blocks, doc_meta=_meta(path, product, jurisdiction, "pdf"), source_key="eval")


# --- hybrid pipeline (pdf2mdtree.py / hybrid_extract.py) arm ------------------
# A THIRD chunking source, distinct from both legacy(docx) and mineru(pdf): the
# markdown tree this repo's own 3-stage pipeline writes to
# out/<job>/03_stage3_final/. It has no ExtractedDoc of its own (that pipeline
# predates this model), so we build one here — same Section/Element shapes,
# same downstream scoring — purely from the tree already on disk. No re-run,
# no MinerU call.
import re as _re  # noqa: E402

_LETTER_RE = _re.compile(r'^([A-Z])-')
_NUM_RE = _re.compile(r'^0*(\d+(?:\.\d+)*)-')            # "01-...", "1.1-...", "2.7-..."
_LETTER_TOKEN_RE = _re.compile(r'^([a-z]+)(?:-|\.md$|$)')  # "a-...", "ii-contract.md" -> lettered/roman sub-clause
_SKIP_FILES = {"README.md", "CONVERSION_REPORT.md", "STAGE3_REPORT.md", "stage1_report.json",
              "stage3_report.json", "tables_manifest.json", "headings_manifest.json"}


def _hybrid_body_text(md_text: str) -> str:
    body = _re.sub(r'!\[.*?\]\(.*?\)', ' ', md_text)          # snapshot images
    body = _re.sub(r'\*\*\[TABLE[^\]]*\]\*\*', ' ', body)      # failure/continuation markers
    body = _re.sub(r'<!--.*?-->', ' ', body, flags=_re.S)      # HTML comments (table anchors)
    body = _re.sub(r'[#*`_>]', ' ', body)
    return _re.sub(r'\s+', ' ', body).strip()


def _key_sort(key: str):
    m = _re.match(r'^([A-Z])(\d+(?:\.\d+)*)?$', key)
    letter, num = m.groups()
    parts = tuple(int(p) for p in num.split(".")) if num else ()
    return (letter, parts)


def chunk_hybrid_tree(stage3_dir, product, jurisdiction) -> "ExtractedDoc":  # noqa: F821
    """Build an ExtractedDoc from a 03_stage3_final tree, walked to ARBITRARY
    depth — a numbered node (folder or file, e.g. "1.1-requirements-for-...")
    keys off its own self-contained dotted number regardless of how deep it
    sits; a lettered/roman node ("(a)", "(ii) Contract") keys off its parent
    plus a parenthesized suffix, matching the legacy DOCX key scheme's own
    "(a)"/"(ii)" sub-clause markers. Missing a depth here previously dropped
    whole clauses out of the index silently — verified against
    united-kingdom-dp: "E1.1" (a 3-levels-deep folder) held the exact content
    several gold questions expect and was invisible to a 2-levels-only walk."""
    from aosphere_core_index.extract.model import Element, ExtractedDoc, Section

    root = Path(stage3_dir)
    nodes: dict[str, dict] = {}       # key -> {title, text}
    dir_key: dict[Path, str] = {}     # directory -> its assigned key, for lettered children's parent lookup

    all_dirs = sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts))
    for d in all_dirs:
        parts = d.relative_to(root).parts
        if len(parts) == 1:
            m = _LETTER_RE.match(parts[0])
            if m:
                dir_key[d] = m.group(1)
            continue
        if not _LETTER_RE.match(parts[0]):
            continue  # not under a lettered clause section (e.g. a glossary/annex folder) — not in scope
        # numbered nodes are self-contained: "1.1-..." already encodes its full path from the
        # letter root, so it only needs the letter prefix, not a join with its immediate parent.
        letter = _LETTER_RE.match(parts[0]).group(1)
        nm = _NUM_RE.match(d.name)
        if nm:
            dir_key[d] = f"{letter}{nm.group(1)}"
            continue
        lm = _LETTER_TOKEN_RE.match(d.name)
        parent_key = dir_key.get(d.parent)
        if lm and parent_key:
            dir_key[d] = f"{parent_key}({lm.group(1)})"

    for d, key in dir_key.items():
        overview = next(d.glob("00-overview.md"), None)
        src = overview if overview and overview.exists() else (d / "README.md")
        if not src.exists():
            continue
        text = src.read_text(errors="ignore")
        title = text.lstrip("#").split("\n", 1)[0].strip() if text.startswith("#") else d.name
        nodes.setdefault(key, {"title": title, "text": text})

    for p in sorted(root.rglob("*.md")):
        if p.name in _SKIP_FILES or p.name == "00-overview.md":
            continue
        parts = p.relative_to(root).parts
        if not _LETTER_RE.match(parts[0]):
            continue
        letter = _LETTER_RE.match(parts[0]).group(1)
        nm = _NUM_RE.match(p.stem)
        if nm:
            key = f"{letter}{nm.group(1)}"
        else:
            lm = _LETTER_TOKEN_RE.match(p.stem)
            parent_key = dir_key.get(p.parent)
            if not (lm and parent_key):
                continue
            key = f"{parent_key}({lm.group(1)})"
        text = p.read_text(errors="ignore")
        title = text.lstrip("#").split("\n", 1)[0].strip() if text.startswith("#") else p.stem
        nodes[key] = {"title": title, "text": text}  # a leaf file always wins over a folder stub

    if not nodes:
        raise ValueError(f"no letter-prefixed sections found under {stage3_dir}")

    def _parent_of(key: str) -> str | None:
        if key.endswith(")"):
            return key.rsplit("(", 1)[0]
        num = key[1:]
        return key.rsplit(".", 1)[0] if "." in num else (key[:1] if num else None)

    def _level_of(key: str) -> int:
        base, _, tail = key.partition("(")
        num = base[1:]
        lvl = (num.count(".") + 2) if num else 1
        return lvl + key.count("(")

    def _sort_key(key: str):
        base = key.split("(", 1)[0]
        m = _re.match(r'^([A-Z])(\d+(?:\.\d+)*)?$', base)
        letter, num = m.groups() if m else (key[:1], None)
        parts = tuple(int(p) for p in num.split(".")) if num else ()
        subs = tuple(_re.findall(r'\(([a-z]+)\)', key))
        return (letter, parts, subs)

    ordered_keys = sorted(nodes, key=_sort_key)
    sections = []
    for key in ordered_keys:
        n = nodes[key]
        body = _hybrid_body_text(n["text"])
        sections.append(Section(
            id=key, key=key, title=n["title"] or key, level=_level_of(key),
            parent_id=_parent_of(key), part_letter=key[:1],
            elements=[Element(kind="body", text=body)] if body else [],
        ))

    return ExtractedDoc(
        doc_id=root.parent.name, title=f"{jurisdiction} — {product} (hybrid)",
        jurisdiction=jurisdiction, jurisdiction_id=None, opinion_id=None,
        product=product, source_key="eval-hybrid", sections=sections, footnotes={},
    )


def chunk_hybrid_pdf(path, product, jurisdiction, mineru_backend=None, mineru_effort=None,
                     log=print) -> "ExtractedDoc":  # noqa: F821
    """Run the actual 3-stage pipeline (pdf2mdtree.py -> hybrid_extract.py stages
    1-3) on an uploaded PDF, right here, then hand the resulting 03_stage3_final
    tree to chunk_hybrid_tree(). Slow — stage 2 shells out to MinerU, same cost as
    the mineru(pdf) arm — but it means the hybrid arm no longer needs a
    pre-existing out/ job; any PDF can be scored live."""
    import tempfile

    import hybrid_extract as he  # scripts/ is on sys.path (see _REPO insert above)

    tmp = Path(tempfile.mkdtemp(prefix="eval_hybrid_"))
    log(f"hybrid: stage 1 (pdf2mdtree) -> {tmp}")
    manifest = he.run_stage1(path, tmp / "01_stage1_extract", 3)
    log("hybrid: stage 2 (MinerU tables) — can take minutes…")
    s2 = he.run_stage2(path, manifest, tmp / "02_stage2_mineru_tables",
                       mineru_backend, mineru_effort, tmp / "01_stage1_extract")
    log("hybrid: stage 3 (splice) …")
    he.run_stage3(tmp / "01_stage1_extract", tmp / "02_stage2_mineru_tables",
                 tmp / "03_stage3_final", s2)
    log(f"hybrid: pipeline done -> {tmp}")
    return chunk_hybrid_tree(tmp / "03_stage3_final", product, jurisdiction)


def list_hybrid_dirs() -> list[dict]:
    """Every 03_stage3_final tree on disk this session's pipeline has produced,
    with the (product, jurisdiction) it was run for — read from corpus_meta.json
    when the job was run via run_corpus.py, else guessed from the job folder name
    for ad-hoc baseline runs (out/baseline/<name>/03_stage3_final has no
    corpus_meta.json — those predate the corpus runner)."""
    out = []
    for meta_path in sorted(_REPO.glob("out/**/corpus_meta.json")):
        job_dir = meta_path.parent
        stage3 = job_dir / "03_stage3_final"
        if not stage3.exists():
            continue
        try:
            import json
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        out.append({"path": str(stage3), "product": meta.get("product", ""),
                    "jurisdiction": meta.get("jurisdiction", ""),
                    "label": f"{meta.get('jurisdiction', '?')} — {meta.get('product', '?')} "
                             f"({job_dir.name})"})
    for stage3 in sorted(_REPO.glob("out/baseline/*/03_stage3_final")):
        job_dir = stage3.parent
        if (job_dir / "corpus_meta.json").exists():
            continue  # already listed above
        out.append({"path": str(stage3), "product": "", "jurisdiction": job_dir.name,
                    "label": f"{job_dir.name} (baseline, product unknown)"})
    return out


def tree_summary(doc) -> dict:
    levels: dict[int, int] = defaultdict(int)
    for s in doc.sections:
        levels[s.level] += 1
    return {"sections": len(doc.sections), "footnotes": len(doc.footnotes),
            "levels": {str(k): v for k, v in sorted(levels.items())},
            "top_level_keys": [s.key for s in doc.sections if s.level == 1][:14]}


# --- scoring ------------------------------------------------------------------
# One retrieval to the deepest level; every hit@level is derived from the true
# rank, so hit@1..hit@40 all come from a single search.
LEVELS = [1, 5, 10, 15, 20, 25, 30, 40]
MAXK = LEVELS[-1]


def score(doc, cases, emb, k=MAXK):
    from aosphere_core_index.embeddings.section_index import build_section_index
    idx = build_section_index(doc, emb)
    have = {EC._nk(s.key) for s in doc.sections}
    rows = []
    for c in cases:
        exp = [s for (_j, s) in c["expected"]]
        qv = emb.embed([c["question"]])[0]
        keys = [h[0].split("#", 1)[0] for h in idx.search(qv, k)]
        ok, rank = EC.hit_ok(exp, keys)
        srank = EC.section_rank(exp, keys)
        present = any(EC._nk(e) == nk or nk.startswith(EC._nk(e) + ".") or EC._nk(e).startswith(nk + ".")
                      for e in exp for nk in have)
        rows.append({"q": c["question"], "expected": exp, "rank": rank, "section_rank": srank,
                     "key_present": present, "top": keys[:8]})
    return rows


def agg(rows, levels=LEVELS):
    """hit@each-level (from the true rank), MRR, section-hit@10, key-coverage.
    Works on a pooled row set too, so the overall/across-jurisdiction score reuses it."""
    n = len(rows) or 1
    def at(field, kk):
        return round(100 * sum(1 for r in rows if 0 <= r.get(field, -1) < kk) / n)
    out = {"n": len(rows),
           "MRR": round(sum(1 / (r["rank"] + 1) for r in rows if r["rank"] >= 0) / n, 3),
           "sec@10": at("section_rank", 10),
           "key_coverage": round(100 * sum(1 for r in rows if r["key_present"]) / n)}
    for L in levels:
        out[f"hit@{L}"] = at("rank", L)
    return out


# --- synthetic questions (optional, Bedrock) ----------------------------------
def synth_questions(doc, product, jurisdiction, per_clause):
    if not per_clause:
        return [], "off"
    try:
        import boto3

        from aosphere_core_index.config import settings
        client = boto3.Session(profile_name=os.getenv("ACI_BEDROCK_PROFILE"),
                               region_name=os.getenv("AWS_REGION") or settings.bedrock_region
                               ).client("bedrock-runtime")
        model = os.getenv("ACI_SYNTH_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    except Exception as e:  # noqa: BLE001
        return [], f"Bedrock unavailable ({type(e).__name__})"
    import json as _json
    clauses = [s for s in doc.sections if s.level >= 2
               and sum(len(e.text) for e in s.elements) > 200][:60]
    sysp = (f"You write the natural-language questions a real user would ask that a specific clause of a "
            f"{product} legal memo answers. Return ONLY a JSON list of {per_clause} short question strings.")
    cases = []
    for s in clauses:
        body = " ".join(e.text for e in s.elements)[:1500]
        try:
            r = client.converse(modelId=model, system=[{"text": sysp}],
                                messages=[{"role": "user", "content": [{"text": f"Clause: {s.title}\n\n{body}"}]}],
                                inferenceConfig={"maxTokens": 400, "temperature": 0.3})
            txt = r["output"]["message"]["content"][0]["text"]
            qs = _json.loads(txt[txt.index("["):txt.rindex("]") + 1])
        except Exception:  # noqa: BLE001
            continue
        for q in qs[:per_clause]:
            if isinstance(q, str) and q.strip():
                cases.append({"question": q.strip(), "expected": [(jurisdiction, s.key)]})
    return cases, f"generated {len(cases)} from {len(clauses)} clauses (from legacy/mineru arm)"


# --- orchestration ------------------------------------------------------------
def run_eval(product, jurisdiction, docx_path=None, pdf_path=None, hybrid_dir=None,
             hybrid_pdf_path=None, synth=0, mineru_backend=None, mineru_effort=None,
             log=print) -> dict:
    from aosphere_core_index.embeddings.embedder import make_embedder
    emb = make_embedder()
    gold = load_gold(product, jurisdiction)
    log(f"gold cases {product}/{jurisdiction}: {len(gold)} | embedder {emb.name}")

    arms = {}
    timings = {}
    if docx_path:
        t = time.monotonic(); arms["legacy(docx)"] = chunk_docx(docx_path, product, jurisdiction)
        timings["legacy(docx)"] = round(time.monotonic() - t, 1); log("legacy chunked")
    if pdf_path:
        from aosphere_core_index.config import settings
        resolved_backend = mineru_backend or settings.mineru_backend
        resolved_effort = mineru_effort or settings.mineru_effort
        t = time.monotonic()
        arms["mineru(pdf)"] = chunk_pdf(pdf_path, product, jurisdiction, mineru_backend, mineru_effort)
        timings["mineru(pdf)"] = round(time.monotonic() - t, 1)
        log(f"mineru chunked ({resolved_backend}"
            f"{'/' + resolved_effort if resolved_backend.startswith('hybrid') else ''})")
    if hybrid_pdf_path:  # a fresh live run beats a cached tree lookup when both are given
        t = time.monotonic()
        arms["hybrid(pdf2mdtree)"] = chunk_hybrid_pdf(hybrid_pdf_path, product, jurisdiction,
                                                       mineru_backend, mineru_effort, log=log)
        timings["hybrid(pdf2mdtree)"] = round(time.monotonic() - t, 1)
        log(f"hybrid pipeline ran live on the upload "
            f"({len(arms['hybrid(pdf2mdtree)'].sections)} sections)")
    elif hybrid_dir:
        t = time.monotonic()
        arms["hybrid(pdf2mdtree)"] = chunk_hybrid_tree(hybrid_dir, product, jurisdiction)
        timings["hybrid(pdf2mdtree)"] = round(time.monotonic() - t, 1)
        log(f"hybrid tree loaded from {hybrid_dir} "
            f"({len(arms['hybrid(pdf2mdtree)'].sections)} sections)")

    synth_cases, synth_note = synth_questions(next(iter(arms.values())), product, jurisdiction, synth) if arms else ([], "off")

    report = {"product": product, "jurisdiction": jurisdiction, "k": MAXK, "levels": LEVELS,
              "embedder": emb.name, "timings_s": timings, "synth_note": synth_note,
              "trees": {n: tree_summary(d) for n, d in arms.items()},
              "gold": {}, "synth": {}}
    if pdf_path:
        report["mineru_backend"] = resolved_backend
        report["mineru_effort"] = resolved_effort if resolved_backend.startswith("hybrid") else None
    for label, cases in [("gold", gold), ("synth", synth_cases)]:
        if not cases:
            continue
        for name, doc in arms.items():
            rows = score(doc, cases, emb, MAXK)
            report[label][name] = {"agg": agg(rows), "rows": rows}
            a = report[label][name]["agg"]
            log(f"{label} {name}: hit@1 {a['hit@1']}% hit@10 {a['hit@10']}% MRR {a['MRR']} cov {a['key_coverage']}%")
    return report
