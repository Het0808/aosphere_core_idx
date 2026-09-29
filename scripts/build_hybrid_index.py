#!/usr/bin/env python3
"""Batch-build the hybrid index inputs for the whole corpus.

For every extracted doc under out/hybrid/<doc_id>/03_stage3_final/, convert the
stage-3 markdown tree -> keyed content.json (tree_to_content.py) and merge the
legacy curated guidance/alerts on top (merge_guidance_alerts.py), writing into an
ISOLATED data dir (default data-hybrid/) so the live legacy index under data/ is
left untouched for A/B comparison and rollback.

Guidance source = the untouched legacy content.json under --legacy-data (data/).
Never writes into --legacy-data. After this, embed + index with:

    eval $(aws configure export-credentials --profile dev1 --format env)
    ACI_DATA_DIR=data-hybrid ACI_EMBED_BACKEND=titan ACI_BEDROCK_REGION=eu-west-2 \\
        .venv/bin/aci reembed --force
"""
import argparse, json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(REPO / "src"))
import extract_to_s3 as E
from aosphere_core_index.regions.region_map import qualified, split_region

SOURCE = REPO / "RAG-json_docx_v1_2026-06-30"
TREE_TO_CONTENT = HERE / "tree_to_content.py"
MERGE = HERE / "merge_guidance_alerts.py"


def artifacts_dir(data_dir: Path, qreg: str) -> Path:
    """<data_dir>/products/<product>/<jurisdiction>/artifacts — mirrors settings.region_artifacts."""
    product, jur = split_region(qreg)
    return data_dir / "products" / product / jur / "artifacts"


def find_tree(doc_id: str, hybrid_root: Path, corpus_root: Path) -> Path | None:
    """The stage-3 tree for a document, in either extraction layout.

    The single-document UI writes  <hybrid_root>/<doc_id>/03_stage3_final
    while a corpus run writes      <corpus_root>/<product>/<jurisdiction>__<doc_id>/03_stage3_final
    (run_corpus.py mirrors the source tree so the hierarchy survives on disk). Both are
    the same artifact, so accept either and prefer the corpus run when both exist — that
    is the one produced by the current pipeline."""
    for cand in sorted(corpus_root.glob(f"*/*__{doc_id}/03_stage3_final")):
        return cand
    flat = hybrid_root / doc_id / "03_stage3_final"
    return flat if flat.exists() else None


def discover_from_corpus(corpus_root: Path, source_root: Path,
                         only_product: str | None) -> list[dict]:
    """Jobs from an extraction run, for products the legacy source tree does not contain.

    E.discover walks <product>/<date>/<region>/*.pdf in RAG-json_docx_v1_*, which only holds Data
    Privacy and Shareholding Disclosure. Everything else arrives as an extraction run instead, so
    identity comes from each job's corpus_meta.json.

    The keeper filter still applies. A jurisdiction routinely holds a superseded opinion beside
    the current one, and the index keys content by product+jurisdiction, so indexing both means
    one silently overwriting the other — on Marketing Restrictions - Asset Management that is 17
    of 88 jurisdictions."""
    jobs, dropped = [], []
    for prod_dir in sorted(p for p in corpus_root.iterdir()
                           if p.is_dir() and not p.name.startswith("_")):
        if only_product and prod_dir.name != only_product:
            continue
        product = E.product_from_dir(prod_dir.name)
        for job in sorted(d for d in prod_dir.iterdir() if d.is_dir()):
            meta = job / "corpus_meta.json"
            if not (job / "03_stage3_final").exists() or not meta.exists():
                continue
            m = json.loads(meta.read_text())
            did, jur = str(m["doc_id"]), m["jurisdiction"]
            src_dir = source_root / prod_dir.name / jur
            keepers = E.keeper_stems(src_dir, product) if src_dir.exists() else None
            if keepers is not None and did not in keepers:
                dropped.append(f"{jur}/{did}")
                continue
            jobs.append({"doc_id": did, "product": product, "region": jur})
    if dropped:
        print(f"  keeper filter: kept {len(jobs)}, dropped {len(dropped)} superseded/secondary "
              f"document(s): {', '.join(dropped[:6])}{' …' if len(dropped) > 6 else ''}")
    return jobs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-data", default="data-hybrid", help="isolated ACI_DATA_DIR to build into")
    ap.add_argument("--legacy-data", default="data", help="live data dir with legacy content.json (guidance source, read-only)")
    ap.add_argument("--hybrid-root", default="out/hybrid", help="root of per-doc stage-3 trees")
    ap.add_argument("--corpus-root", default="out/corpus",
                    help="root of corpus-run stage-3 trees (<product>/<jurisdiction>__<id>/)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--from-corpus", action="store_true",
                    help="discover documents from --corpus-root instead of the legacy DP/SD source "
                         "tree, so a product absent from RAG-json_docx_v1_* can be onboarded")
    ap.add_argument("--source-root", default="corpus-src",
                    help="with --from-corpus: source tree holding each jurisdiction's "
                         "Doc_metadata.json, used to keep only the current opinion")
    ap.add_argument("--only-product", default=None,
                    help="with --from-corpus: restrict to one product directory name")
    ap.add_argument("docs", nargs="*", help="optional doc-id filter (default: every discovered opinion)")
    args = ap.parse_args()

    out_data = Path(args.out_data).resolve()
    legacy_data = Path(args.legacy_data).resolve()
    hybrid_root = Path(args.hybrid_root).resolve()
    corpus_root = Path(args.corpus_root).resolve()
    assert out_data != legacy_data, "refusing to build into the legacy data dir"

    if args.from_corpus:
        jobs = discover_from_corpus(corpus_root, Path(args.source_root).resolve(),
                                    args.only_product)
    else:
        jobs = E.discover(SOURCE, None, None)
    if args.docs:
        want = set(args.docs); jobs = [j for j in jobs if j["doc_id"] in want]
    if args.limit:
        jobs = jobs[:args.limit]

    ok = skipped = merged_ok = no_legacy = 0
    for j in jobs:
        did, product, region = j["doc_id"], j["product"], j["region"]
        qreg = qualified(product, region)
        tree = find_tree(did, hybrid_root, corpus_root)
        if tree is None:
            print(f"  ⚠ {did} {qreg}: no stage-3 tree — SKIP"); skipped += 1; continue
        art = artifacts_dir(out_data, qreg); art.mkdir(parents=True, exist_ok=True)
        cj = art / f"{qreg}.content.json"

        # 1. tree -> keyed content.json (product-aware)
        r = subprocess.run([sys.executable, str(TREE_TO_CONTENT), str(cj), str(tree), qreg, product],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ✗ {did} {qreg}: tree_to_content failed: {(r.stderr or r.stdout).strip()[-200:]}")
            skipped += 1; continue
        nsec = len(json.loads(cj.read_text()).get("sections", []))

        # 2. merge legacy curated guidance/alerts (read-only source under legacy-data)
        leg = artifacts_dir(legacy_data, qreg) / f"{qreg}.content.json"
        if leg.exists():
            m = subprocess.run([sys.executable, str(MERGE), str(cj), str(leg)], capture_output=True, text=True)
            note = (m.stdout.strip().splitlines() or ["(merge: no output)"])[-1] if m.returncode == 0 \
                else f"MERGE FAILED: {(m.stderr or m.stdout).strip()[-120:]}"
            merged_ok += (m.returncode == 0)
        else:
            note = "no legacy content.json — clause bodies only"; no_legacy += 1
        print(f"  ✓ {qreg:44s} sec={nsec:<4} {note}")
        ok += 1

    print(f"\nbuilt {ok} content.json (skipped {skipped}); guidance merged {merged_ok}, "
          f"without-legacy {no_legacy}  → {out_data}")
    print(f"next: ACI_DATA_DIR={args.out_data} ACI_EMBED_BACKEND=titan ACI_BEDROCK_REGION=eu-west-2 "
          f".venv/bin/aci reembed --force")


if __name__ == "__main__":
    main()
