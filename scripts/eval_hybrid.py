#!/usr/bin/env python3
"""Retrieval-only eval of the 2-pass HYBRID extraction (pdf2mdtree + MinerU
tables) for the 4 validated regions. Same pipeline as
extraction_regress.run_retrieval, but the section tree is the EXISTING hybrid
stage-3 output (out/hybrid/<doc_id>/03_stage3_final) instead of a fresh
pdf2mdtree run. Reembeds each region with Titan (Bedrock EU, needs dev1 SSO)
into an isolated data dir, runs eval_cases hit@k, and prints hybrid vs the
legacy-docx baseline (test/retrieval_baseline.json). Writes
test/eval_hybrid_result.json."""
import glob, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import extraction_regress as R

# eval region  ->  hybrid stage-3 doc-id
DOC_ID = {
    "United Kingdom": "179582",
    "Germany": "180656",
    "South Korea": "174504",
    "Shareholding Disclosure — Australia": "172274",
}
BASELINE = json.loads((HERE.parent / "test" / "retrieval_baseline.json").read_text())


def run_one(cfg, tmp: Path):
    product, region, srcfolder = cfg
    did = DOC_ID[region]
    tree = HERE.parent / "out" / "hybrid" / did / "03_stage3_final"
    legacy = glob.glob(f"data/products/{product}/{srcfolder}/artifacts/*.content.json")
    if not tree.exists() or not legacy:
        return {"error": f"missing tree={tree.exists()} legacy={bool(legacy)}"}
    safe = region.replace("/", "_").replace(" ", "_")
    dd = tmp / f"dd_{safe}"
    cj = dd / "products" / product / srcfolder / "artifacts" / f"{region}.content.json"
    cj.parent.mkdir(parents=True, exist_ok=True)
    aci = str(HERE.parent / ".venv/bin/aci")

    def run(cmd, env=None):
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    run([sys.executable, str(HERE / "tree_to_content.py"), str(cj), str(tree), region])
    run([sys.executable, str(HERE / "merge_guidance_alerts.py"), str(cj), legacy[0]])
    env = R._aci_env(dd)
    run([aci, "reembed", "--region", region, "--force"], env=env)
    run([aci, "reindex"], env=env)
    eenv = dict(env); eenv.update(ACI_VECTOR_BACKEND="memory", ACI_RERANK="0", ACI_QUERY_EXPAND="0")
    run([sys.executable, "scripts/eval_cases.py", f"--product={product}", "--k=40"], env=eenv)
    try:
        rep = json.loads(Path("test/eval_report.json").read_text())
    except Exception:
        rep = {"cases": []}
    subprocess.run(["git", "checkout", "--", "test/eval_report.json", "test/eval_report.md"],
                   capture_output=True)
    cs = [c for c in rep.get("cases", [])
          if c.get("scorable") and c.get("region_id") == region and "rank" in c]
    n = len(cs) or 1
    hk = {f"@{k}": round(100 * sum(1 for c in cs if 0 <= c["rank"] < k) / n) for k in (1, 5, 10)}
    mrr = round(sum(1 / (c["rank"] + 1) for c in cs if c["rank"] >= 0) / n, 3)
    return {"n": len(cs), **hk, "MRR": mrr}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="eval_hybrid_"))
    print(f"HYBRID retrieval eval — 4 regions, Titan reembed (dev1 SSO) -> {tmp}\n")
    print(f"  {'region':38} {'':>4} {'@1':>10} {'@5':>10} {'@10':>10} {'MRR':>12}")
    out = {}
    for cfg in R.RETRIEVAL_SET:
        region = cfg[1]
        m = run_one(cfg, tmp)
        if "error" in m:
            print(f"  ERROR {region}: {m['error']}"); continue
        b = BASELINE.get(region, {})
        out[region] = {"hybrid": m, "baseline": b}

        def cmp(k):
            hv, bv = m.get(k), b.get(k)
            if bv is None:
                return f"{hv:>4}"
            d = round(hv - bv, 3) if k == "MRR" else hv - bv
            sign = "+" if d >= 0 else ""
            return f"{hv:>4}({sign}{d})"
        warn = "  ⚠ n=0 (reembed/SSO?)" if m["n"] == 0 else ""
        print(f"  {region:38} n={m['n']:>3} "
              f"{cmp('@1'):>10} {cmp('@5'):>10} {cmp('@10'):>10} {cmp('MRR'):>12}{warn}")
    (HERE.parent / "test" / "eval_hybrid_result.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nsaved test/eval_hybrid_result.json  (hybrid vs legacy-docx baseline; Δ in parens)")


if __name__ == "__main__":
    main()
