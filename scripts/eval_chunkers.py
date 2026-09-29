"""Head-to-head chunker eval: MinerU-on-PDF vs legacy-on-DOCX, scored on the
project's gold eval cases (and optionally synthetic questions), using the SAME
embedder for both so the ONLY variable is the chunking.

What it does, for one region (jurisdiction + product):
  1. chunk the PDF with MinerU (hybrid-engine/medium by default) -> ExtractedDoc
  2. chunk the DOCX with the legacy Word-style extractor           -> ExtractedDoc
  3. build a bge (local, offline) section index over EACH doc
  4. load that region's gold eval cases (question + expected clause key)
  5. for every question, retrieve top-k from each index and score citation
     hit@1/3/5/10/20 + MRR (family match via eval_cases.hit_ok)
  6. optional: synthetic questions per clause (Bedrock Haiku if available)
  7. write test/eval_chunkers.<region>.{md,json} and print a side-by-side table

Fairness: identical embedder + identical scorer for both arms; the expected
clause keys are the canonical convention BOTH chunkers target, and the gold
questions are human-written (chunker-neutral). Pass --pdf OR --docx alone to
run a single arm (e.g. a legacy baseline before the PDF is sourced).

Usage:
  .venv/bin/python scripts/eval_chunkers.py \
      --product "Shareholding Disclosure" --jurisdiction "Australia" \
      --docx "/path/AU.docx" --pdf "/path/AU.pdf" [--k 20] [--mineru-backend hybrid-engine] \
      [--synth 4]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

os.environ.setdefault("ACI_EMBED_BACKEND", "bge")   # local, offline, same for both arms
os.environ.setdefault("ACI_OFFLINE", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import eval_cases as EC  # load_cases, hit_ok, _nk, section_rank

from aosphere_core_index.embeddings.embedder import make_embedder
from aosphere_core_index.embeddings.section_index import build_section_index
from aosphere_core_index.config import settings as _settings

_ROOT = "test"


# --- chunking -----------------------------------------------------------------
def chunk_docx(path, product, jurisdiction):
    from aosphere_core_index.extract.docx_extract import extract_docx
    meta = {"DOCID": os.path.basename(path), "DOCNAME": f"{jurisdiction} — {product}",
            "JURISDICTIONNAME": jurisdiction, "JURISDICTIONID": None, "OPINIONID": None,
            "RENDERSTYLENAME": product, "EXTENSION": "docx"}
    return extract_docx(path, doc_meta=meta, source_key="eval")


def chunk_pdf(path, product, jurisdiction, backend, effort):
    from aosphere_core_index.extract.mineru_extract import blocks_to_doc, run_mineru
    meta = {"DOCID": os.path.basename(path), "DOCNAME": f"{jurisdiction} — {product}",
            "JURISDICTIONNAME": jurisdiction, "JURISDICTIONID": None, "OPINIONID": None,
            "RENDERSTYLENAME": product, "EXTENSION": "pdf"}
    blocks = run_mineru(path, backend=backend, effort=effort)
    return blocks_to_doc(blocks, doc_meta=meta, source_key="eval")


def tree_summary(doc):
    levels = defaultdict(int)
    for s in doc.sections:
        levels[s.level] += 1
    parts = [s.key for s in doc.sections if s.level == 1][:12]
    return {"sections": len(doc.sections),
            "levels": dict(sorted(levels.items())),
            "footnotes": len(doc.footnotes),
            "top_level_keys": parts}


# --- scoring ------------------------------------------------------------------
def score(doc, cases, emb, k):
    """Build an index over doc, retrieve for each case, return per-case ranks."""
    idx = build_section_index(doc, emb)
    have = {EC._nk(s.key) for s in doc.sections}   # for coverage: is the gold key even present?
    rows = []
    for c in cases:
        exp = [s for (_j, s) in c["expected"]]
        qv = emb.embed([c["question"]])[0]
        hits = idx.search(qv, k)
        keys = [h[0].split("#", 1)[0] for h in hits]
        ok, rank = EC.hit_ok(exp, keys)
        srank = EC.section_rank(exp, keys)
        key_present = any(EC._nk(e) == nk or nk.startswith(EC._nk(e) + ".") or EC._nk(e).startswith(nk + ".")
                          for e in exp for nk in have)
        rows.append({"q": c["question"], "expected": exp, "rank": rank,
                     "section_rank": srank, "key_present": key_present, "top": keys[:5]})
    return idx, rows


def agg(rows, k):
    n = len(rows) or 1
    def at(field, kk):
        return 100 * sum(1 for r in rows if 0 <= r.get(field, -1) < kk) / n
    mrr = sum(1 / (r["rank"] + 1) for r in rows if r["rank"] >= 0) / n
    cov = 100 * sum(1 for r in rows if r["key_present"]) / n
    return {"n": n, "hit@1": at("rank", 1), "hit@3": at("rank", 3), "hit@5": at("rank", 5),
            "hit@10": at("rank", 10), f"hit@{k}": at("rank", k), "MRR": mrr,
            "sec@5": at("section_rank", 5), "sec@10": at("section_rank", 10),
            "key_coverage": cov}


# --- synthetic questions (optional, Bedrock) ----------------------------------
def synth_questions(doc, product, per_clause, jurisdiction):
    """Generate `per_clause` NL questions for each substantive clause, tagged with
    that clause's key. Returns eval-case-shaped dicts, or [] if Bedrock is absent."""
    try:
        import boto3
        client = boto3.Session(profile_name=os.getenv("ACI_BEDROCK_PROFILE"),
                               region_name=os.getenv("AWS_REGION") or _settings.bedrock_region
                               ).client("bedrock-runtime")
        model = os.getenv("ACI_SYNTH_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    except Exception as e:  # noqa: BLE001
        print(f"  [synth] Bedrock unavailable ({type(e).__name__}); skipping synthetic questions")
        return []
    # substantive leaf-ish clauses with real text (skip the root + heading-only)
    clauses = [s for s in doc.sections
               if s.level >= 2 and sum(len(e.text) for e in s.elements) > 200][:60]
    cases = []
    sysp = (f"You write the natural-language questions a real user would ask that a specific clause of a "
            f"{product} legal memo answers. Return ONLY a JSON list of {per_clause} short question strings. "
            f"No preamble.")
    for s in clauses:
        body = " ".join(e.text for e in s.elements)[:1500]
        try:
            r = client.converse(modelId=model, system=[{"text": sysp}],
                                messages=[{"role": "user", "content": [{"text": f"Clause: {s.title}\n\n{body}"}]}],
                                inferenceConfig={"maxTokens": 400, "temperature": 0.3})
            txt = r["output"]["message"]["content"][0]["text"]
            qs = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
        except Exception:  # noqa: BLE001
            continue
        for q in qs[:per_clause]:
            if isinstance(q, str) and q.strip():
                cases.append({"question": q.strip(), "expected": [(jurisdiction, s.key)]})
    print(f"  [synth] generated {len(cases)} questions from {len(clauses)} clauses")
    return cases


# --- main ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", required=True)
    ap.add_argument("--jurisdiction", required=True)
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--docx", default=None)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--mineru-backend", default="hybrid-engine")
    ap.add_argument("--mineru-effort", default="medium")
    ap.add_argument("--synth", type=int, default=0, help="synthetic questions per clause (0=off)")
    args = ap.parse_args()

    emb = make_embedder()
    print(f"embedder: {emb.name}")

    gold = [c for c in EC.load_cases()
            if c["product"] == args.product and c["expected"]
            and c["expected"][0][0] == args.jurisdiction]
    print(f"gold cases for {args.product} / {args.jurisdiction}: {len(gold)}")
    if not gold and not args.synth:
        print("no gold cases and no --synth; nothing to score."); return

    arms = {}   # name -> (doc, tree_summary)
    if args.docx:
        print(f"\nchunking DOCX (legacy): {args.docx}")
        d = chunk_docx(args.docx, args.product, args.jurisdiction)
        arms["legacy(docx)"] = d; print("  ", tree_summary(d))
    if args.pdf:
        print(f"\nchunking PDF (mineru {args.mineru_backend}/{args.mineru_effort}): {args.pdf}")
        t0 = time.monotonic()
        d = chunk_pdf(args.pdf, args.product, args.jurisdiction, args.mineru_backend, args.mineru_effort)
        arms["mineru(pdf)"] = d; print(f"   ({time.monotonic()-t0:.0f}s)", tree_summary(d))
    if not arms:
        print("provide --pdf and/or --docx"); return

    synth = synth_questions(next(iter(arms.values())), args.product, args.synth, args.jurisdiction) if args.synth else []

    report = {"product": args.product, "jurisdiction": args.jurisdiction, "k": args.k,
              "embedder": emb.name, "trees": {n: tree_summary(d) for n, d in arms.items()},
              "gold": {}, "synth": {}}
    for label, cases in [("gold", gold), ("synth", synth)]:
        if not cases:
            continue
        print(f"\n=== scoring {label}: {len(cases)} questions ===")
        for name, doc in arms.items():
            _idx, rows = score(doc, cases, emb, args.k)
            a = agg(rows, args.k)
            report[label][name] = {"agg": a, "rows": rows}
            print(f"  {name:14} hit@1 {a['hit@1']:.0f}%  hit@5 {a['hit@5']:.0f}%  "
                  f"hit@10 {a['hit@10']:.0f}%  hit@{args.k} {a[f'hit@{args.k}']:.0f}%  "
                  f"MRR {a['MRR']:.2f}  keycov {a['key_coverage']:.0f}%")

    _write_report(report, args)


def _write_report(report, args):
    reg = f"{args.jurisdiction}".replace(" ", "_")
    L = [f"# Chunker eval — {args.product} / {args.jurisdiction}", "",
         f"Embedder `{report['embedder']}` (same for both arms) · k={args.k}", "",
         "## Chunk trees", "", "| Arm | sections | footnotes | levels |", "|---|---|---|---|"]
    for n, t in report["trees"].items():
        L.append(f"| {n} | {t['sections']} | {t['footnotes']} | {t['levels']} |")
    for label in ("gold", "synth"):
        if not report[label]:
            continue
        L += ["", f"## Retrieval — {label} questions", "",
              f"| Arm | n | hit@1 | hit@5 | hit@10 | hit@{args.k} | MRR | key-coverage |",
              "|---|---|---|---|---|---|---|---|"]
        for n, r in report[label].items():
            a = r["agg"]
            L.append(f"| {n} | {a['n']} | {a['hit@1']:.0f}% | {a['hit@5']:.0f}% | {a['hit@10']:.0f}% | "
                     f"{a[f'hit@{args.k}']:.0f}% | {a['MRR']:.2f} | {a['key_coverage']:.0f}% |")
        arms = list(report[label])
        if len(arms) == 2:
            x, y = arms
            dh = report[label][y]["agg"][f"hit@{args.k}"] - report[label][x]["agg"][f"hit@{args.k}"]
            L += ["", f"**Δ hit@{args.k} ({y} − {x}) = {dh:+.0f} pts**  ·  "
                  f"Δ MRR = {report[label][y]['agg']['MRR'] - report[label][x]['agg']['MRR']:+.2f}"]
    md = "\n".join(L)
    open(f"{_ROOT}/eval_chunkers.{reg}.md", "w").write(md)
    json.dump(report, open(f"{_ROOT}/eval_chunkers.{reg}.json", "w"), default=str, indent=1)
    print("\n" + md)
    print(f"\nsaved -> {_ROOT}/eval_chunkers.{reg}.md + .json")


if __name__ == "__main__":
    main()
