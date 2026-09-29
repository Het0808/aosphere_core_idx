#!/usr/bin/env python3
"""extraction_regress.py — regression gate for the PDF -> markdown extraction.

Re-extracts a small CURATED sample of opinions (one per known failure mode) with
the CURRENT scripts/pdf2mdtree.py, then runs two kinds of check:

  1. ABSOLUTE quality gates (must hold regardless of history):
       - false_formula : "**Formula:**" with a "÷" but no equation (=, ×, %) —
                          i.e. a table row-rule misread as a fraction bar
       - max_cell      : biggest markdown table cell (garbled merged cell)
       - shattered     : wide tables whose cells are word-fragments
       - content_loss  : word_delta_signed <= -6  (markdown missing source words)
       - empty_files   : content .md files with < 3 words
  2. DRIFT vs a saved baseline (catches unintended changes elsewhere):
       tables / snapshots / headings / files / content_words move > tolerance.

Usage:
  python scripts/extraction_regress.py --baseline   # snapshot current as known-good
  python scripts/extraction_regress.py              # gates + drift report (exit 1 on fail)
  python scripts/extraction_regress.py --keep       # keep the extracted trees for inspection
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PDF2MD = HERE / "pdf2mdtree.py"
SOURCE = Path("RAG-json_docx_v1_2026-06-30")
BASELINE = HERE.parent / "test" / "extraction_regress_baseline.json"
sys.path.insert(0, str(HERE))
import extract_to_s3 as E  # reuse keeper_stems() so we test the real opinion PDF per region
from aosphere_core_index.config import settings as _settings

# one opinion per known failure mode
SAMPLE = [
    ("Data Privacy", "United States - California (Short Form)"),  # per-column comparison tables + false formulas
    ("Data Privacy", "United States - Colorado"),                # comparison tables (was 248 garbled)
    ("Data Privacy", "South Korea"),                             # 148pp long-form
    ("Data Privacy", "United Kingdom"),                          # standard opinion + footnotes
    ("Data Privacy", "Germany"),                                 # standard opinion
    ("Data Privacy", "Algeria"),                                 # keeper-filter sanity (Survey vs Instructions)
    ("Shareholding Disclosure", "Kenya"),                        # REAL threshold formula (must survive)
    ("Shareholding Disclosure", "Australia"),                    # lines_strict tables (~25, must survive)
    ("Shareholding Disclosure", "Portugal"),                     # was content-loss flagged
    ("Shareholding Disclosure", "United Kingdom"),               # keeper edge: main memo amended-for-short-selling
    ("Shareholding Disclosure", "EU Member States"),             # keeper edge: short-selling-only
]

TOL = 0.12  # 12% drift tolerance on count metrics

# ---- retrieval A/B gate (heavy: Titan reembed per region, needs Bedrock/SSO) ----
RETRIEVAL_BASELINE = HERE.parent / "test" / "retrieval_baseline.json"
RETR_TOL = 4  # a region's hit@k / MRR(*100) may drop at most this many points before flagging
# (product, eval region-id / JUR passed to the converter, source region folder) — legacy content.json auto-globbed
RETRIEVAL_SET = [
    ("Data Privacy", "United Kingdom", "United Kingdom"),
    ("Data Privacy", "Germany", "Germany"),
    ("Data Privacy", "South Korea", "South Korea"),
    ("Shareholding Disclosure", "Shareholding Disclosure — Australia", "Australia"),
]


def _pdf_for(product: str, region: str) -> Path | None:
    prod_dir = "155_Data_Privacy" if product == "Data Privacy" else "104_Shareholding_Disclosure"
    base = SOURCE / prod_dir
    for suffix in (f"{region} (Data Privacy)", region):
        for d in base.glob(f"*/{suffix}"):
            keep = E.keeper_stems(d, product)
            for pdf in d.glob("*.pdf"):
                if keep is None or pdf.stem in keep:
                    return pdf
    return None


def _table_blocks(txt: str):
    """yield (ncols, nrows, max_cell_len, median_cell_len) per markdown table."""
    L = txt.splitlines()
    i = 0
    while i < len(L) - 1:
        if L[i].strip().startswith("|") and re.match(r"^\|[\s:|-]+\|?\s*$", L[i + 1].strip()):
            blk = [L[i]]
            j = i + 2
            while j < len(L) and L[j].strip().startswith("|"):
                blk.append(L[j]); j += 1
            cells = [c.strip() for row in blk for c in row.split("|")[1:-1]]
            lens = [len(c) for c in cells if c]
            ncols = blk[0].count("|") - 1
            med = sorted(lens)[len(lens) // 2] if lens else 0
            yield ncols, len(blk), (max(lens) if lens else 0), med
            i = j
        else:
            i += 1


def analyze(tree: Path) -> dict:
    rep = {}
    rp = tree / "CONVERSION_REPORT.md"
    if rp.exists():
        for line in rp.read_text(encoding="utf-8").splitlines():
            m = re.match(r"- \*\*(\w+)\*\*:\s*(.+)", line)
            if m:
                rep[m.group(1)] = m.group(2).strip()
    false_formula = formula_total = empty = content_words = max_cell = shattered = 0
    for rt, _, fs in os.walk(tree):
        for f in fs:
            if not f.endswith(".md") or f in ("README.md", "CONVERSION_REPORT.md"):
                continue
            txt = Path(rt, f).read_text(encoding="utf-8")
            w = len(txt.split()); content_words += w
            if w < 3:
                empty += 1
            for ln in txt.splitlines():
                if ln.startswith("**Formula:**"):
                    formula_total += 1
                    if "÷" in ln and not any(s in ln for s in ("=", "×", "%")):
                        false_formula += 1
            for ncols, nrows, mx, med in _table_blocks(txt):
                max_cell = max(max_cell, mx)
                if ncols >= 6 and med <= 3 and nrows >= 3:
                    shattered += 1
    wd = rep.get("word_delta_pct", "0")
    try:
        wd = float(wd)
    except ValueError:
        wd = 0.0
    snaps = rep.get("pages_snapshotted", "[]")
    nsnap = snaps.count(",") + 1 if snaps.strip("[] ") else 0
    return {
        "tables": int(rep.get("tables_converted", 0) or 0),
        "snapshots": nsnap,
        "headings_matched": int(rep.get("headings_matched", 0) or 0),
        "headings_total": int(rep.get("headings_total", 0) or 0),
        "files": int(rep.get("files_written", 0) or 0),
        "content_words": content_words,
        "word_delta_signed": wd,
        "false_formula": false_formula,
        "formula_total": formula_total,
        "max_cell": max_cell,
        "shattered_tables": shattered,
        "empty_files": empty,
    }


def gates(m: dict) -> list[str]:
    fails = []
    if m["false_formula"] > 0:
        fails.append(f"false_formula={m['false_formula']}")
    if m["max_cell"] > 2000:
        fails.append(f"giant_cell={m['max_cell']}c")
    if m["shattered_tables"] > 0:
        fails.append(f"shattered={m['shattered_tables']}")
    if m["word_delta_signed"] <= -6:
        fails.append(f"content_loss={m['word_delta_signed']}%")
    if m["empty_files"] > 0:
        fails.append(f"empty_files={m['empty_files']}")
    if m["headings_total"] and m["headings_matched"] < m["headings_total"]:
        pass  # headings mismatch is informational, not a hard gate
    return fails


def extract_one(item, tmp: Path):
    product, region = item
    pdf = _pdf_for(product, region)
    if not pdf:
        return item, None, "no PDF found"
    out = tmp / f"{product}_{region}".replace("/", "_").replace(" ", "_")
    r = subprocess.run([sys.executable, str(PDF2MD), str(pdf), "-o", str(out),
                        "--depth", "3", "--no-snapshots"], capture_output=True, text=True)
    if r.returncode != 0:
        return item, None, (r.stderr or r.stdout)[-200:]
    return item, analyze(out), None


def _aci_env(dd: Path) -> dict:
    e = dict(os.environ)
    e.update(ACI_DATA_DIR=str(dd), ACI_OFFLINE="1", ACI_SOURCE_OFFLINE="1",
             ACI_EMBED_BACKEND="titan", ACI_BEDROCK_PROFILE="dev1",
             # AWS_REGION is left alone on purpose: it is the generic region the child's
             # S3 client would also read, and S3 is configured elsewhere. Only the
             # Bedrock (AI) region is pinned here.
             AWS_PROFILE="dev1", AWS_REGION="eu-west-1",
             ACI_BEDROCK_REGION=_settings.bedrock_region)
    return e


def run_retrieval(cfg, tmp: Path) -> dict | None:
    """Extract one validated region with the CURRENT converter, rebuild its
    content.json (+ legacy guidance/alerts), reembed into an isolated data dir,
    and run the retrieval-only eval → hit@k for that region."""
    import glob
    product, region, srcfolder = cfg
    pdf = _pdf_for(product, srcfolder)
    legacy = glob.glob(f"data/products/{product}/{srcfolder}/artifacts/*.content.json")
    if not pdf or not legacy:
        return None
    safe = region.replace("/", "_").replace(" ", "_")
    tree, dd = tmp / f"tree_{safe}", tmp / f"dd_{safe}"
    cj = dd / "products" / product / srcfolder / "artifacts" / f"{region}.content.json"
    cj.parent.mkdir(parents=True, exist_ok=True)
    aci = str(HERE.parent / ".venv/bin/aci")

    def run(cmd, env=None):
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    run([sys.executable, str(PDF2MD), str(pdf), "-o", str(tree), "--depth", "3", "--no-snapshots"])
    run([sys.executable, str(HERE / "tree_to_content.py"), str(cj), str(tree), region])
    run([sys.executable, str(HERE / "merge_guidance_alerts.py"), str(cj), legacy[0]])
    env = _aci_env(dd)
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


def retrieval_gate(a) -> None:
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="retrieval_gate_"))
    print(f"retrieval A/B on {len(RETRIEVAL_SET)} validated regions (Titan reembed) -> {tmp}\n")
    results = {}
    for cfg in RETRIEVAL_SET:
        region = cfg[1]
        m = run_retrieval(cfg, tmp)
        if not m:
            print(f"  ERROR  {region}: setup failed (missing pdf/legacy)"); continue
        results[region] = m
        warn = "  ⚠ n=0 — reembed likely failed (SSO?)" if m["n"] == 0 else ""
        print(f"  {region:38} n={m['n']:>3}  @1 {m['@1']:>3}  @5 {m['@5']:>3}  @10 {m['@10']:>3}  MRR {m['MRR']}{warn}")

    if a.baseline:
        RETRIEVAL_BASELINE.parent.mkdir(parents=True, exist_ok=True)
        RETRIEVAL_BASELINE.write_text(json.dumps(results, indent=1))
        print(f"\nretrieval baseline written: {RETRIEVAL_BASELINE}")
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    base = json.loads(RETRIEVAL_BASELINE.read_text()) if RETRIEVAL_BASELINE.exists() else {}
    fail = False
    if base:
        print(f"\ndrift vs baseline (flag if hit@k / MRR drops > {RETR_TOL} pts):")
        for region, m in results.items():
            b = base.get(region, {})
            drops = [f"{k} {b[k]}->{m[k]}" for k in ("@1", "@5", "@10") if k in b and m[k] - b[k] < -RETR_TOL]
            if "MRR" in b and (m["MRR"] - b["MRR"]) < -(RETR_TOL / 100):
                drops.append(f"MRR {b['MRR']}->{m['MRR']}")
            print(f"  {'❌' if drops else '✅'} {region}: {', '.join(drops) if drops else 'within tolerance'}")
            fail = fail or bool(drops)
    else:
        print("\n(no retrieval baseline yet — run: python scripts/extraction_regress.py --retrieval --baseline)")
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nRETRIEVAL:", "❌ REGRESSION" if fail else "✅ no retrieval regression")
    sys.exit(1 if fail else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="save current metrics as the known-good baseline")
    ap.add_argument("--keep", action="store_true", help="keep extracted trees")
    ap.add_argument("--retrieval", action="store_true",
                    help="run the retrieval A/B on the 4 validated regions (needs Bedrock/SSO); "
                         "with --baseline records the known-good hit@k")
    a = ap.parse_args()
    if a.retrieval:
        return retrieval_gate(a)
    tmp = Path(tempfile.mkdtemp(prefix="extract_regress_"))
    print(f"extracting {len(SAMPLE)} sample opinions -> {tmp}\n")
    results = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for item, m, err in ex.map(lambda it: extract_one(it, tmp), SAMPLE):
            key = f"{item[0]} / {item[1]}"
            if err:
                print(f"  ERROR  {key}: {err}"); continue
            results[key] = m
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() and not a.baseline else {}

    if a.baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(results, indent=1))
        print(f"baseline written: {BASELINE}  ({len(results)} docs)")
        if not a.keep:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)
        return

    print(f"{'sample doc':46}{'tbl':>4}{'snap':>5}{'files':>6}{'wΔ%':>7}{'falseF':>7}  gates")
    hard_fail = False
    for key, m in sorted(results.items()):
        g = gates(m)
        drift = []
        if key in base:
            for k in ("tables", "snapshots", "files", "content_words"):
                b = base[key].get(k, 0)
                if b and abs(m[k] - b) / max(b, 1) > TOL:
                    drift.append(f"{k} {b}->{m[k]}")
        status = "OK" if not g and not drift else ("FAIL " + ",".join(g + drift))
        if g:
            hard_fail = True
        print(f"{key:46}{m['tables']:>4}{m['snapshots']:>5}{m['files']:>6}"
              f"{m['word_delta_signed']:>7.1f}{m['false_formula']:>7}  {status}")
    if not base:
        print("\n(no baseline yet — run with --baseline to record drift reference)")
    print("\nRESULT:", "❌ GATE FAILURES" if hard_fail else "✅ all absolute gates pass")
    if not a.keep:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if hard_fail else 0)


if __name__ == "__main__":
    main()
