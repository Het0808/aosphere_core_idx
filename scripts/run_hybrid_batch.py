#!/usr/bin/env python3
"""
run_hybrid_batch — run the 2-pass hybrid extraction (Stage 1 pdf2mdtree
--defer-tables -> Stage 2 MinerU tables -> Stage 3 combine) over MANY documents,
one at a time, with per-doc LIVE monitoring and an aggregate report.

Each document is delegated to scripts/run_hybrid_one.py in its OWN subprocess
(process isolation — a crash or hang on one doc never takes down the batch), and
its streamed, monitored output is passed through so you can watch MinerU work.
Already-completed docs are skipped (resumable); a per-doc watchdog kills a hung
MinerU so the batch keeps moving.

Run under the MAIN venv (needs the `mineru` symlink — see run_hybrid_one.py):

  # everything (keeper-filtered: one opinion per jurisdiction, matches the index corpus)
  .venv/bin/python scripts/run_hybrid_batch.py --all

  # a product / region slice, capped
  .venv/bin/python scripts/run_hybrid_batch.py --all --product "Data Privacy" --limit 10
  .venv/bin/python scripts/run_hybrid_batch.py --all --region "Germany,United Kingdom"

  # specific docs (doc-id, region substring, or a PDF path — any mix)
  .venv/bin/python scripts/run_hybrid_batch.py 172099 175652 "United Kingdom"

  --force re-runs docs that already have output; --timeout caps each doc (s).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SOURCE = REPO / "RAG-json_docx_v1_2026-06-30"
RUNNER = HERE / "run_hybrid_one.py"
sys.path.insert(0, str(HERE))
import extract_to_s3 as E  # discover() + keeper_stems() — the canonical opinion set
import push_hybrid_s3 as PH  # build_viewer() + S3 constants (used by --push)

_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _TTY else s
BOLD = lambda s: _c("1", s); DIM = lambda s: _c("2", s)
GREEN = lambda s: _c("32", s); RED = lambda s: _c("31", s)
YELLOW = lambda s: _c("33", s); CYAN = lambda s: _c("36", s)


def slug_stem(pdf) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(pdf).stem).strip("-") or "document"


def resolve_jobs(args) -> list[dict]:
    """Return [{pdf, product, region, doc_id}, …]. Explicit tokens win; else --all
    (optionally filtered by --product / --region), keeper-filtered by discover()."""
    if args.docs:
        alljobs = E.discover(SOURCE, None, None)
        picked, seen = [], set()
        for tok in args.docs:
            p = Path(tok)
            if p.is_file():
                matches = [{"pdf": p.resolve(), "product": "?", "region": p.stem, "doc_id": p.stem}]
            else:
                matches = [j for j in alljobs
                           if tok == j["doc_id"] or tok.lower() in j["region"].lower()]
            if not matches:
                print(YELLOW(f"⚠ no match for '{tok}' — skipping"))
            for j in matches:
                if str(j["pdf"]) not in seen:
                    seen.add(str(j["pdf"])); picked.append(j)
        return picked
    if not args.all:
        sys.exit(RED("✗ specify documents (doc-id / region / path) or --all"))
    products = set(s.strip() for s in args.product.split(",")) if args.product else None
    regions = set(s.strip() for s in args.region.split(",")) if args.region else None
    jobs = E.discover(SOURCE, products, regions)
    if args.limit:
        jobs = jobs[:args.limit]
    return jobs


def run_one(job, out_root: Path, args) -> dict:
    pdf = Path(job["pdf"])
    root = out_root / slug_stem(pdf)
    report_path = root / "03_stage3_final" / "stage3_report.json"

    if report_path.exists() and not args.force:
        r = json.loads(report_path.read_text())
        r["status"] = "skipped"
        return r

    cmd = [sys.executable, str(RUNNER), str(pdf), "-o", str(out_root), "--depth", str(args.depth)]
    if args.backend: cmd += ["--backend", args.backend]
    if args.effort: cmd += ["--effort", args.effort]

    t0 = time.time()
    killed = {"v": False}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    def _kill():
        killed["v"] = True
        proc.terminate()
    wd = threading.Timer(args.timeout, _kill); wd.start()
    try:
        for line in proc.stdout:
            print("    " + line.rstrip())
        proc.wait()
    finally:
        wd.cancel()
    secs = round(time.time() - t0, 1)

    if killed["v"]:
        return {"status": "timeout", "seconds": secs}
    if report_path.exists():
        r = json.loads(report_path.read_text())
        r["status"] = "ok" if proc.returncode == 0 else "error"
        r["seconds"] = secs
        return r
    return {"status": "error", "reason": f"no stage3 report (exit {proc.returncode})", "seconds": secs}


def _s3_setup():
    """boto3 client + the current gallery manifest (fetched once, mutated per-doc)."""
    import boto3
    from botocore.config import Config
    cfg = Config(connect_timeout=10, read_timeout=90, retries={"max_attempts": 4})
    s3 = boto3.Session(profile_name=PH.PROFILE, region_name=PH.REGION).client("s3", config=cfg)
    man = json.loads(s3.get_object(Bucket=PH.BUCKET, Key=f"{PH.PREFIX}/manifest.json")["Body"].read())
    return s3, man


def publish_doc(s3, man, job, out_root: Path, viewers_dir: Path) -> tuple[int, int]:
    """Build a self-contained viewer from this doc's Stage-3 tree and push it +
    an updated manifest entry ("<region> (MinerU)"). Mutates `man` in place."""
    stem = slug_stem(job["pdf"])
    s3dir = out_root / stem / "03_stage3_final"
    rp = s3dir / "stage3_report.json"
    rep = json.loads(rp.read_text()) if rp.exists() else {}
    product, region, pdf = job.get("product", "?"), job["region"], Path(job["pdf"])
    title = f"{product} — {region} (MinerU)"
    html = PH.build_viewer(s3dir, pdf, title, "2-pass hybrid: pdf2mdtree + MinerU tables")
    vpath = viewers_dir / f"{stem}-mineru-viewer.html"
    vpath.write_text(html, encoding="utf-8")
    gslug = re.sub(r"[^a-z0-9]+", "-", f"{product}-{region}-{stem}".lower()).strip("-") + "-mineru"
    r = region + " (MinerU)"
    vkey = f"{PH.PREFIX}/{product}/{r}/{gslug}-viewer.html"
    s3.put_object(Bucket=PH.BUCKET, Key=vkey, Body=vpath.read_bytes(),
                  ContentType="text/html; charset=utf-8")
    stats = PH.doc_display_stats(pdf, s3dir, rep)
    man[:] = [d for d in man if d.get("slug") != gslug]
    man.append({"product": product, "region": r, "slug": gslug, "doc_id": stem,
                "pdf": str(pdf), "status": "ok",
                "viewer": vkey.split(PH.PREFIX + "/", 1)[1], "stats": stats})
    s3.put_object(Bucket=PH.BUCKET, Key=f"{PH.PREFIX}/manifest.json",
                  Body=json.dumps(man, ensure_ascii=False).encode(), ContentType="application/json")
    return rep.get("tables_filled", 0), rep.get("tables_total", 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docs", nargs="*", help="doc-id / region substring / PDF path (omit with --all)")
    ap.add_argument("--all", action="store_true", help="run every keeper-filtered opinion PDF")
    ap.add_argument("--product", default=None, help="CSV filter for --all, e.g. 'Data Privacy'")
    ap.add_argument("--region", default=None, help="CSV exact-region filter for --all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("-o", "--out", default=str(REPO / "out" / "hybrid"))
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--backend", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--force", action="store_true", help="re-run docs that already have output")
    ap.add_argument("--timeout", type=int, default=2400, help="per-doc watchdog seconds (default 2400)")
    ap.add_argument("--push", action="store_true",
                    help="after each doc finishes, push its viewer to S3 (view online as the batch runs)")
    args = ap.parse_args()

    jobs = resolve_jobs(args)
    if not jobs:
        sys.exit(RED("✗ no documents resolved"))
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    s3 = man = viewers_dir = None
    if args.push:
        try:
            s3, man = _s3_setup()
            viewers_dir = out_root / "_mineru_viewers"; viewers_dir.mkdir(exist_ok=True)
            print(DIM(f"  --push: S3 ready ({len(man)} docs in manifest); each doc publishes as it finishes"))
        except Exception as e:
            sys.exit(RED(f"✗ --push: S3 setup failed ({e}).\n"
                         "  Ensure dev1 creds:  aws sso login --sso-session tools1"))

    print(BOLD(CYAN(f"\n═══ Hybrid batch: {len(jobs)} document(s) → {out_root} ═══")))
    tally = {"ok": 0, "skipped": 0, "error": 0, "timeout": 0}
    tot_tables = tot_filled = tot_failed = 0
    results = []
    t_batch = time.time()

    for i, job in enumerate(jobs, 1):
        label = f"{job.get('product','?')} / {job['region']} ({job['doc_id']})"
        print(BOLD(f"\n┏━ [{i}/{len(jobs)}] {label} ") + DIM("━" * max(2, 60 - len(label))))
        try:
            r = run_one(job, out_root, args)
        except KeyboardInterrupt:
            print(RED("\n✗ interrupted by user")); break
        except Exception as e:
            r = {"status": "error", "reason": repr(e)}
        r.update(doc_id=job["doc_id"], region=job["region"], product=job.get("product", "?"))
        results.append(r)
        tally[r["status"]] = tally.get(r["status"], 0) + 1
        total, filled, failed = r.get("tables_total", 0), r.get("tables_filled", 0), r.get("tables_failed", 0)
        tot_tables += total; tot_filled += filled; tot_failed += failed
        color = {"ok": GREEN, "skipped": DIM, "error": RED, "timeout": RED}.get(r["status"], YELLOW)
        secs = r.get("seconds", 0)
        pushed = ""
        if args.push and r["status"] in ("ok", "skipped") and (out_root / slug_stem(job["pdf"]) / "03_stage3_final").exists():
            try:
                pf, pt = publish_doc(s3, man, job, out_root, viewers_dir)
                pushed = GREEN(f"  → pushed ({pf}/{pt})")
            except Exception as e:
                pushed = RED(f"  → push FAILED: {str(e)[:60]}")
        print(color(f"┗━ {r['status'].upper():8}") +
              f"  tables {filled}/{total} filled" +
              (RED(f" · {failed} failed") if failed else "") +
              DIM(f"  · {secs}s") + pushed +
              (RED("  · " + r.get("reason", "")) if r.get("reason") else "") +
              DIM(f"   [running: {tally['ok']} ok, {tally['skipped']} skip, {tally['error']+tally['timeout']} bad]"))

    # ---- aggregate report ----
    batch_secs = round(time.time() - t_batch, 1)
    report = {
        "documents": len(results),
        "ok": tally["ok"], "skipped": tally["skipped"],
        "error": tally["error"], "timeout": tally["timeout"],
        "tables_total": tot_tables, "tables_filled": tot_filled, "tables_failed": tot_failed,
        "success_pct": round(100 * tot_filled / tot_tables, 1) if tot_tables else 0.0,
        "seconds": batch_secs,
        "results": sorted(results, key=lambda r: -r.get("tables_failed", 0)),
    }
    (out_root / "BATCH_REPORT.json").write_text(json.dumps(report, indent=2))
    md = ["# Hybrid extraction — batch report", "",
          f"- documents: **{report['documents']}**  (ok {report['ok']}, skipped {report['skipped']}, "
          f"error {report['error']}, timeout {report['timeout']})",
          f"- tables: **{tot_filled}/{tot_tables} filled** ({report['success_pct']}%), {tot_failed} failed",
          f"- wall time: {batch_secs}s", "",
          "| doc | region | status | filled/total | failed | secs |",
          "| --- | --- | --- | --- | --- | --- |"]
    for r in report["results"]:
        md.append(f"| {r['doc_id']} | {r['region']} | {r['status']} | "
                  f"{r.get('tables_filled',0)}/{r.get('tables_total',0)} | "
                  f"{r.get('tables_failed',0)} | {r.get('seconds',0)} |")
    (out_root / "BATCH_REPORT.md").write_text("\n".join(md) + "\n")

    print(BOLD(CYAN(f"\n═══ Done in {batch_secs}s ═══")))
    print(f"  documents : {report['documents']}  " +
          GREEN(f"({report['ok']} ok") + f", {report['skipped']} skipped, " +
          (RED if report['error']+report['timeout'] else DIM)(f"{report['error']+report['timeout']} failed") + ")")
    print(f"  tables    : {GREEN(f'{tot_filled}/{tot_tables} filled')} ({report['success_pct']}%), "
          + (RED if tot_failed else DIM)(f"{tot_failed} failed"))
    print(f"  report    : {out_root/'BATCH_REPORT.md'}")
    if report["error"] + report["timeout"]:
        bad = [r["doc_id"] for r in report["results"] if r["status"] in ("error", "timeout")]
        print(YELLOW(f"  check     : {', '.join(bad)}"))


if __name__ == "__main__":
    main()
