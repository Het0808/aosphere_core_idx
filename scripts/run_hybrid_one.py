#!/usr/bin/env python3
"""
run_hybrid_one — run ONE document through the 2-pass hybrid extraction
(Stage 1 deterministic pdf2mdtree --defer-tables  ->  Stage 2 MinerU tables  ->
Stage 3 combine) with LIVE, monitorable progress and clear error surfacing.

This is a thin monitoring wrapper around scripts/hybrid_extract.py. The stock
pipeline runs MinerU with captured output (silent for minutes); here MinerU is
streamed line-by-line so you can watch model load + per-page progress and see
immediately if something stalls or errors.

Run under the MAIN venv (it has PyMuPDF for Stage 1 + the aosphere package).
The `mineru` CLI is a self-contained script that runs under .venv-mineru; make
it findable from the main venv once with:
    ln -sf "$PWD/.venv-mineru/bin/mineru" .venv/bin/mineru

Then:
    .venv/bin/python scripts/run_hybrid_one.py <pdf-or-docid> [-o out/hybrid]

Examples:
    .venv/bin/python scripts/run_hybrid_one.py \\
        "RAG-json_docx_v1_2026-06-30/155_Data_Privacy/2026-06-30/United States - Colorado/175652.pdf"
    .venv/bin/python scripts/run_hybrid_one.py 175652              # resolve by doc-id

Prerequisite (one-time): MinerU models must be downloaded to the local cache
(settings.mineru_model_cache = data/.mineru_models) or Stage 2 fails with
"local_models_config is None":
    HF_HOME="$PWD/data/.mineru_models" .venv-mineru/bin/mineru-models-download -m all
"""
from __future__ import annotations

import argparse
import glob
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SOURCE = REPO / "RAG-json_docx_v1_2026-06-30"

# ---- ANSI (no external dep; degrade to plain if not a tty) ----
_TTY = sys.stdout.isatty()
def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _TTY else s
BOLD = lambda s: _c("1", s)
DIM = lambda s: _c("2", s)
GREEN = lambda s: _c("32", s)
RED = lambda s: _c("31", s)
YELLOW = lambda s: _c("33", s)
CYAN = lambda s: _c("36", s)

_T0 = time.time()
def _elapsed():
    return f"{time.time() - _T0:6.1f}s"

def log(msg):
    print(f"{DIM('['+_elapsed()+']')} {msg}", flush=True)

def banner(msg):
    line = "─" * max(4, 72 - len(msg))
    print(f"\n{BOLD(CYAN('▶ ' + msg))} {DIM(line)}", flush=True)


def resolve_pdf(arg: str) -> Path:
    p = Path(arg)
    if p.is_file():
        return p.resolve()
    # treat as a doc-id or region name; glob the source drop
    hits = glob.glob(str(SOURCE / "**" / f"{arg}.pdf"), recursive=True)
    if not hits:
        hits = [h for h in glob.glob(str(SOURCE / "**" / "*.pdf"), recursive=True)
                if arg.lower() in h.lower()]
    if not hits:
        sys.exit(RED(f"✗ could not resolve a PDF for '{arg}' (not a file, no match under {SOURCE.name}/)"))
    if len(hits) > 1:
        log(YELLOW(f"⚠ {len(hits)} matches for '{arg}'; using the first:"))
        for h in hits[:5]:
            log(DIM("    " + h))
    return Path(hits[0]).resolve()


def preflight(pdf: Path):
    banner("Preflight")
    import fitz
    doc = fitz.open(str(pdf)); n = doc.page_count; doc.close()
    log(f"PDF        : {pdf}")
    log(f"pages      : {n}")
    # MinerU CLI must sit next to this python (the .venv-mineru venv)
    cli = Path(sys.executable).parent / "mineru"
    if cli.exists():
        log(GREEN(f"mineru CLI : {cli}"))
    else:
        from shutil import which
        onpath = which("mineru")
        if onpath:
            log(YELLOW(f"mineru CLI : {onpath} (on PATH, not in this venv — ok if it's the right one)"))
        else:
            sys.exit(RED("✗ 'mineru' CLI not found next to this python nor on PATH.\n"
                         "  Run under the MinerU venv:  .venv-mineru/bin/python scripts/run_hybrid_one.py ..."))
    # config + model cache
    sys.path.insert(0, str(REPO / "src"))
    from aosphere_core_index.config import settings
    cache = settings.mineru_model_cache.resolve()
    log(f"backend    : {settings.mineru_backend}   effort: {settings.mineru_effort}   timeout: {settings.mineru_timeout_s}s")
    log((GREEN if cache.exists() else YELLOW)(f"model cache: {cache} {'(present)' if cache.exists() else '(MISSING — MinerU may try to download)'}"))
    return n


def install_mineru_streaming():
    """Monkeypatch so ONLY the MinerU subprocess streams live (everything else,
    e.g. the Stage-1 pdf2mdtree call, keeps its captured behavior)."""
    import aosphere_core_index.extract.mineru_extract as mx
    _orig = mx.subprocess.run

    def _run(cmd, **kw):
        is_mineru = cmd and "mineru" in str(cmd[0]).lower() and kw.get("capture_output")
        if not is_mineru:
            return _orig(cmd, **kw)
        kw.pop("capture_output", None); kw.pop("text", None)
        log(DIM("    $ " + " ".join(str(c) for c in cmd)))
        buf = []
        p = mx.subprocess.Popen(cmd, env=kw.get("env"),
                                stdout=mx.subprocess.PIPE, stderr=mx.subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in p.stdout:
            line = line.rstrip("\n")
            buf.append(line)
            print(f"      {DIM('│')} {line}", flush=True)
        p.wait()
        out = "\n".join(buf)
        cp = mx.subprocess.CompletedProcess(cmd, p.returncode, stdout=out, stderr="\n".join(buf[-60:]))
        if kw.get("check") and p.returncode:
            raise mx.subprocess.CalledProcessError(p.returncode, cmd, output=out, stderr=cp.stderr)
        return cp

    mx.subprocess.run = _run


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", help="path to a PDF, or a doc-id / region substring to resolve under the source drop")
    ap.add_argument("-o", "--out", default=str(REPO / "out" / "hybrid"), help="output root (default: out/hybrid)")
    ap.add_argument("--depth", type=int, default=None,
                    help="override the product rule's nesting depth")
    ap.add_argument("--backend", default=None, help="override MinerU backend")
    ap.add_argument("--effort", default=None, help="override MinerU effort")
    args = ap.parse_args()

    pdf = resolve_pdf(args.pdf)
    npages = preflight(pdf)

    # import the pipeline (adds src to path) + turn on live MinerU streaming
    sys.path.insert(0, str(HERE))
    import hybrid_extract as he
    install_mineru_streaming()

    out_root = Path(args.out).resolve() / he.slug_stem(pdf)
    stage1_dir = out_root / "01_stage1_extract"
    stage2_dir = out_root / "02_stage2_mineru_tables"
    stage3_dir = out_root / "03_stage3_final"
    out_root.mkdir(parents=True, exist_ok=True)
    log(f"output     : {out_root}")

    # ---- Stage 1 ----
    banner("Stage 1 · deterministic extract (tables deferred)")
    t = time.time()
    try:
        manifest = he.run_stage1(pdf, stage1_dir, args.depth)
    except Exception as e:
        print(RED(f"✗ Stage 1 failed: {e}")); traceback.print_exc(); sys.exit(2)
    tables = manifest.get("tables", [])
    table_pages = sorted({pg for tb in tables for pg in tb.get("pages", [])})
    log(GREEN(f"✓ Stage 1 done in {time.time()-t:.1f}s — {len(tables)} table(s) deferred across {len(table_pages)} page(s)"))
    if tables:
        for tb in tables[:12]:
            pg = tb.get("pages", [])
            span = f"p{pg[0]}" if len(pg) == 1 else f"p{pg[0]}–{pg[-1]}"
            log(DIM(f"    {tb['table_id']:14} {span:10} src={tb.get('source','?')}"))
        if len(tables) > 12:
            log(DIM(f"    … and {len(tables)-12} more"))

    # ---- Stage 2 ----
    banner("Stage 2 · MinerU table extraction (LIVE output below)")
    if not tables:
        log(YELLOW("no deferred tables — skipping MinerU (Stage 2 no-op)"))
    log(f"MinerU on {len(table_pages)} page(s), backend={args.backend or 'default'} effort={args.effort or 'default'} …")
    t = time.time()
    try:
        s2 = he.run_stage2(pdf, manifest, stage2_dir, args.backend, args.effort)
    except Exception as e:
        print(RED(f"✗ Stage 2 crashed: {e}")); traceback.print_exc(); sys.exit(3)
    log(GREEN(f"✓ Stage 2 done in {time.time()-t:.1f}s — {s2['tables_ok']} ok, ") +
        (RED if s2['tables_failed'] else GREEN)(f"{s2['tables_failed']} failed"))
    for st in s2.get("tables", []):
        if st.get("ok"):
            log(GREEN(f"    ✓ {st['table_id']:14} {st.get('rows','?')}×{st.get('cols','?')}"))
        else:
            log(RED(f"    ✗ {st['table_id']:14} {st.get('reason','?')[:80]}"))

    # ---- Stage 3 ----
    banner("Stage 3 · combine (splice tables into tree)")
    t = time.time()
    try:
        s3 = he.run_stage3(stage1_dir, stage2_dir, stage3_dir, s2)
    except Exception as e:
        print(RED(f"✗ Stage 3 failed: {e}")); traceback.print_exc(); sys.exit(4)
    log(GREEN(f"✓ Stage 3 done in {time.time()-t:.1f}s — {s3['tables_filled']} filled, ") +
        (RED if s3['tables_failed'] else GREEN)(f"{s3['tables_failed']} failed"))

    # ---- summary ----
    banner("Summary")
    ok = s3["tables_filled"]; fail = s3["tables_failed"]; total = s3["tables_total"]
    pct = (100 * ok / total) if total else 100
    log(f"document   : {pdf.name}  ({npages} pages)")
    log(f"tables     : {total} detected → {GREEN(str(ok)+' filled')} / {(RED if fail else DIM)(str(fail)+' failed')}  ({pct:.0f}% success)")
    if s3.get("failed_table_ids"):
        log(YELLOW("failed ids : " + ", ".join(s3["failed_table_ids"])))
        log(DIM("             (see 02_stage2_mineru_tables/tables/<id>/status.json for the reason)"))
    log(f"total time : {time.time()-_T0:.1f}s")
    log(BOLD("output tree:"))
    log(f"    final    → {stage3_dir}")
    log(f"    stage1   → {stage1_dir}")
    log(f"    stage2   → {stage2_dir}  (MinerU raw + per-table html/md/status)")
    log(f"    summary  → {out_root/'PIPELINE_SUMMARY.md'}")
    print()
    print(GREEN(BOLD("✓ extraction complete — inspect the final tree, then push to S3 for testing.")))


if __name__ == "__main__":
    main()
