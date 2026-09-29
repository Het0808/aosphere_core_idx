"""Stage 1 (Extraction): source PDFs -> markdown trees (deterministic pdf2mdtree) ->
local artifacts + optional S3 upload, plus a gallery viewer over ALL extracted memos.

This is the first, independently-testable stage of the extraction pipeline
(see docs/EXTRACTION_PIPELINE.md). It does NOT embed or index — it only produces the
md-tree artifacts + a QA gallery, so extraction can be reviewed before ingestion (S2).

Artifact layout (local `--out`, mirrored to S3 under `--s3-prefix`):
    <out>/<Product>/<Region>/<slug>/            # markdown tree (+ CONVERSION_REPORT.md)
    <out>/<Product>/<Region>/<slug>-viewer.html # self-contained md ⇄ PDF viewer
    <out>/<Product>/<Region>/<slug>.zip
    <out>/manifest.json                         # per-memo QA stats
    <out>/index.html                            # GALLERY: every memo + stats + links

Usage:
    python scripts/extract_to_s3.py --source RAG-json_docx_v1_2026-06-30 --out out/extractions \
        [--products "Data Privacy,Shareholding Disclosure"] [--regions "Germany,Australia"] \
        [--limit N] [--workers 4] [--depth 3] [--force] \
        [--s3-bucket BUCKET --s3-prefix extractions/ --profile dev1 --s3-region eu-west-1]

No --s3-bucket -> LOCAL ONLY (safe dry run). Never writes to the read-only source bucket.
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, hashlib, html, json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
PDF2MD = HERE / "pdf2mdtree.py"
SOURCE_BUCKET = "aosphere-aosphere-tenant-prod-advanced-search-store"  # READ-ONLY: never write here


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


# Re-exported: the canonical implementation lives with the product taxonomy it answers
# to (regions/region_map.py), beside PRODUCTS, so "which products can we index" and
# "what is this directory called" cannot drift apart.
from aosphere_core_index.regions.region_map import product_from_dir  # noqa: E402,F401


def parse_report(report_path: Path) -> dict:
    stats = {}
    if not report_path.exists():
        return {"error": "no CONVERSION_REPORT"}
    for line in report_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"- \*\*(\w+)\*\*:\s*(.+)", line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            try:
                fv = float(v)                      # handles signed floats/ints ("-5.42", "147")
                stats[k] = int(fv) if "." not in v and fv == int(fv) else fv
            except ValueError:
                stats[k] = v                       # non-numeric (e.g. "[4, 93]", "PDF bookmark outline")
        elif line.startswith("No warnings"):
            stats["warnings_clean"] = True
    # word_delta_pct is SIGNED (negative = content loss, positive = benign inflation).
    # Keep the signed value for direction; expose abs magnitude for thresholds/badges.
    if isinstance(stats.get("word_delta_pct"), (int, float)):
        stats["word_delta_signed"] = stats["word_delta_pct"]
        stats["word_delta_pct"] = round(abs(stats["word_delta_pct"]), 2)
    return stats


_KEEPER_CACHE: dict = {}


# Products whose opinion is identified by the word "Memorandum" in its DOCNAME. Everything else
# is matched by exclusion instead (see opinions() below) — a whitelist silently drops any product
# nobody has added to it, which is the wrong default when 30+ products are being onboarded.
_MEMORANDUM_PRODUCTS = {"Shareholding Disclosure"}


def keeper_stems(region_dir: Path, product: str) -> set[str] | None:
    """From a region's Doc_metadata.json, the PDF filename stem(s) worth indexing
    — the OPINION only, one per region:
      - Data Privacy         -> DOCNAME contains "Survey"
      - Shareholding Disc.   -> DOCNAME contains "Memorandum" (the main memo)
    Dropped: "Instructions to Counsel", working .docx, superseded "DO NOT USE"
    surveys, and SD "Memorandum on EU Short-Selling" secondary memos. If several
    survive (e.g. a stale EU-wide survey alongside the jurisdiction one), prefer
    the jurisdiction-named / newest DOCID. Returns None when no metadata exists
    (keep everything, as before)."""
    key = str(region_dir)
    if key in _KEEPER_CACHE:
        return _KEEPER_CACHE[key]
    md = region_dir / "Doc_metadata.json"
    if not md.exists():
        _KEEPER_CACHE[key] = None
        return None
    try:
        entries = json.loads(md.read_text(encoding="utf-8"))
    except Exception:
        _KEEPER_CACHE[key] = None
        return None
    jur = (entries[0].get("JURISDICTIONNAME") or "").lower() if entries else ""

    def opinions(drop_shortselling: bool) -> list:
        out = []
        for e in entries:
            if str(e.get("EXTENSION", "")).lower() != "pdf":
                continue
            name = (e.get("DOCNAME") or "").lower()
            if "do not use" in name:
                continue
            if product == "Data Privacy":
                if "survey" in name:
                    out.append(e)
            elif product not in _MEMORANDUM_PRODUCTS:
                # Every other product. Their DOCNAMEs are plain DATES ("22nd June, 2026"), not
                # titles, so requiring the word "memorandum" dropped EVERY document: measured on
                # Marketing Restrictions - Asset Management, 69 of 89 jurisdictions resolved to no
                # keeper at all, and 20 "matched" only because a secondary "ELTIF Supplemental
                # Memorandum" contains the word — keeping the supplement and discarding the main
                # opinion (Czech Republic kept a 2024 supplement over the 2025 opinion; France a
                # 2023 supplement over 2026; Italy likewise).
                #
                # So identify the opinion by what it is NOT: a supplement, or instructions to
                # counsel. Measured across the same product, that leaves exactly one main opinion
                # in 88 of 89 jurisdictions.
                if "supplemental" in name or "supplement" in name:
                    continue
                if "instructions to counsel" in name:
                    continue
                out.append(e)
            elif "memorandum" in name:
                # only the DEDICATED secondary memo is "EU Short-Selling"; a main
                # memo merely amended for short-selling guidance is still the keeper
                is_ss = "eu short-selling" in name or "eu short selling" in name
                if drop_shortselling and is_ss:
                    continue
                out.append(e)
        return out

    cands = opinions(drop_shortselling=True)
    if not cands and product != "Data Privacy":
        cands = opinions(drop_shortselling=False)   # region's ONLY opinion is a short-selling memo → keep it
    if not cands:
        _KEEPER_CACHE[key] = set()
        return set()
    cands.sort(key=lambda e: ((jur in (e.get("DOCNAME") or "").lower()) if jur else False,
                              int(e.get("DOCID") or 0)), reverse=True)
    keep = {str(cands[0].get("FILENAME"))}
    _KEEPER_CACHE[key] = keep
    return keep


def discover(source: Path, products: set[str] | None, regions: set[str] | None) -> list[dict]:
    jobs = []
    dropped = 0
    for pdf in sorted(source.glob("*/*/*/*.pdf")):
        prod_dir, _date, region = pdf.parts[-4], pdf.parts[-3], pdf.parts[-2]
        product = product_from_dir(prod_dir)
        # DP source folders are "<Region> (Data Privacy)"; SD are bare "<Region>" (which may
        # legitimately contain parens, e.g. "Canada (Ontario)"). Strip ONLY the product suffix.
        region = re.sub(r"\s*\((?:Data Privacy|Shareholding Disclosure)\)\s*$", "", region).strip()
        if products and product not in products:
            continue
        if regions and region not in regions:
            continue
        # keep only the opinion PDF per Doc_metadata.json (Survey / main Memorandum)
        keepers = keeper_stems(pdf.parent, product)
        if keepers is not None and pdf.stem not in keepers:
            dropped += 1
            continue
        jobs.append({"pdf": pdf, "product": product, "region": region, "doc_id": pdf.stem,
                     "slug": slugify(f"{product}-{region}-{pdf.stem}")})
    if dropped:
        print(f"  metadata filter: kept {len(jobs)} opinion PDFs, dropped {dropped} "
              f"(Instructions to Counsel / superseded / short-selling / working files)", file=sys.stderr)
    return jobs


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def extract_one(job: dict, out: Path, depth: int, force: bool, prior: dict) -> dict:
    pdf: Path = job["pdf"]
    digest = sha1(pdf)
    dest_dir = out / job["product"] / job["region"]
    slug_dir = dest_dir / job["slug"]
    key = job["slug"]
    if not force and prior.get(key, {}).get("pdf_sha1") == digest and (dest_dir / f"{job['slug']}-viewer.html").exists():
        return {**prior[key], "status": "cached"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(PDF2MD), str(pdf), "-o", str(slug_dir), "--depth", str(depth),
           "--viewer", "--zip", "--title", f"{job['product']} — {job['region']}",
           "--subtitle", "aosphere legal memo"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {**job, "pdf": str(pdf), "status": "FAILED",
                "error": (r.stderr or r.stdout).strip()[-300:]}
    stats = parse_report(slug_dir / "CONVERSION_REPORT.md")
    return {"product": job["product"], "region": job["region"], "slug": job["slug"],
            "doc_id": job["doc_id"], "pdf": str(pdf), "pdf_sha1": digest, "status": "ok",
            "viewer": f"{job['product']}/{job['region']}/{job['slug']}-viewer.html",
            "zip": f"{job['product']}/{job['region']}/{job['slug']}.zip",
            "tree": f"{job['product']}/{job['region']}/{job['slug']}", "stats": stats}


# ---------------- gallery ----------------
_CSS = """
:root{--bg:#0f1216;--panel:#171b21;--line:#262d36;--ink:#e7ecf2;--dim:#8a95a3;--accent:#4c9ffe}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:22px 28px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg)}
h1{margin:0;font-size:18px;letter-spacing:.2px}.sub{color:var(--dim);font-size:13px;margin-top:4px}
main{padding:20px 28px;max-width:1180px}
h2{font-size:15px;margin:26px 0 6px;border-bottom:1px solid var(--line);padding-bottom:6px}
.region{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.6px;margin:16px 0 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.card a.t{color:var(--ink);text-decoration:none;font-weight:600;font-size:14px}
.card a.t:hover{color:var(--accent)}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.b{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--dim);
 font-variant-numeric:tabular-nums}
.g{color:#7ee2a8;border-color:#245239}.a{color:#ffd479;border-color:#5c4a1e}.r{color:#ff9b9b;border-color:#5c2626}
.links{margin-top:10px;font-size:12px}.links a{color:var(--accent);text-decoration:none;margin-right:14px}
.summary{display:flex;gap:20px;margin-top:8px;font-size:13px;color:var(--dim)}
.summary b{color:var(--ink)}
"""


def _badge(stats: dict) -> str:
    # Prefer signed delta: md<pdf (negative) = real content LOSS (red); md>pdf (positive)
    # = inflation from clause-number prefixes + footnote defs (benign, amber/green).
    s = stats.get("word_delta_signed")
    wd = stats.get("word_delta_pct", stats.get("word_delta", 0))
    if isinstance(s, (int, float)):
        if s <= -5:      cls, lbl = "r", f"−{abs(s):g}% missing"
        elif abs(s) < 1: cls, lbl = "g", f"Δ {s:+g}%"
        elif s < 0:      cls, lbl = "a", f"{s:+g}% short"
        else:            cls, lbl = "a", f"Δ {s:+g}% (structure)"
    else:
        cls = "g" if isinstance(wd, (int, float)) and wd < 1 else ("a" if isinstance(wd, (int, float)) and wd < 5 else "r")
        lbl = f"Δ {wd}%"
    hm, ht = stats.get("headings_matched"), stats.get("headings_total")
    hcls = "g" if hm == ht else "a"
    out = [f'<span class="b {cls}">Δ {wd}%</span>',
           f'<span class="b">{stats.get("pages","?")}pp</span>',
           f'<span class="b {hcls}">{hm}/{ht} headings</span>']
    if stats.get("tables_converted"):
        out.append(f'<span class="b">{stats["tables_converted"]} tables</span>')
    if stats.get("files_written"):
        out.append(f'<span class="b">{stats["files_written"]} files</span>')
    return "".join(out)


def build_gallery(manifest: list[dict], out: Path, s3_base: str | None) -> None:
    ok = [m for m in manifest if m.get("status") in ("ok", "cached")]
    failed = [m for m in manifest if m.get("status") == "FAILED"]
    def _sd(m): return m.get("stats", {}).get("word_delta_signed")
    loss = sum(1 for m in ok if isinstance(_sd(m), (int, float)) and _sd(m) <= -5)
    infl = sum(1 for m in ok if isinstance(_sd(m), (int, float)) and _sd(m) >= 5)
    by_prod: dict[str, dict[str, list]] = {}
    for m in ok:
        by_prod.setdefault(m["product"], {}).setdefault(m["region"], []).append(m)
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<title>Extracted memos — Stage 1 gallery</title>'
             f'<style>{_CSS}</style></head><body>',
             "<header><h1>Extracted memos — Stage 1 gallery</h1>",
             f'<div class="sub">{len(ok)} memos · {len(by_prod)} products · '
             f'<span style="color:#ff9b9b">{loss} content-loss (≥5% missing)</span> · '
             f'<span style="color:#ffd479">{infl} inflation (benign)</span> · '
             f'{len(failed)} failed</div>'
             f'<div class="sub" style="font-size:11px;margin-top:6px">Δ = extracted vs source word count · '
             f'<span style="color:#ff9b9b">red = content missing (review)</span> · '
             f'<span style="color:#ffd479">amber = extra structural words (numbering/footnotes, harmless)</span> · '
             f'<span style="color:#7ee2a8">green = tight match</span></div></header>', "<main>"]
    for product in sorted(by_prod):
        regions = by_prod[product]
        parts.append(f"<h2>{html.escape(product)} · {sum(len(v) for v in regions.values())} memos</h2>")
        parts.append('<div class="grid">')
        for region in sorted(regions):
            for m in regions[region]:
                vq, zq = quote(m["viewer"]), quote(m["zip"])   # region names have spaces/parens
                href = (s3_base.rstrip("/") + "/" + vq) if s3_base else vq
                zref = (s3_base.rstrip("/") + "/" + zq) if s3_base else zq
                parts.append(
                    f'<div class="card"><a class="t" href="{html.escape(href)}">{html.escape(region)}</a> '
                    f'<span class="b">{html.escape(str(m.get("doc_id","")))}</span>'
                    f'<div class="badges">{_badge(m.get("stats",{}))}</div>'
                    f'<div class="links"><a href="{html.escape(href)}">open viewer ↗</a>'
                    f'<a href="{html.escape(zref)}">zip</a></div></div>')
        parts.append("</div>")
    if failed:
        parts.append('<h2 style="color:#ff9b9b">Failed</h2><div class="grid">')
        for m in failed:
            parts.append(f'<div class="card"><div class="t">{html.escape(m["region"])}</div>'
                         f'<div class="sub">{html.escape(m.get("error","")[:200])}</div></div>')
        parts.append("</div>")
    parts.append("</main></body></html>")
    (out / "index.html").write_text("\n".join(parts), encoding="utf-8")


# ---------------- s3 ----------------
def upload(out: Path, bucket: str, prefix: str, profile: str | None, region: str) -> str:
    if bucket == SOURCE_BUCKET:
        sys.exit(f"REFUSING to write to the read-only source bucket {bucket!r}.")
    import boto3
    s3 = (boto3.Session(profile_name=profile) if profile else boto3.Session()).client("s3", region_name=region)
    ctypes = {".html": "text/html", ".json": "application/json", ".md": "text/markdown",
              ".zip": "application/zip", ".pdf": "application/pdf"}
    n = 0
    for f in out.rglob("*"):
        if f.is_file():
            key = f"{prefix.rstrip('/')}/{f.relative_to(out).as_posix()}"
            s3.upload_file(str(f), bucket, key,
                           ExtraArgs={"ContentType": ctypes.get(f.suffix, "application/octet-stream")})
            n += 1
    print(f"uploaded {n} objects to s3://{bucket}/{prefix}")
    return f"https://{bucket}.s3.{region}.amazonaws.com/{prefix.rstrip('/')}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="root of the PDF drop")
    ap.add_argument("--out", required=True, help="local artifacts dir")
    ap.add_argument("--products", default=None, help="CSV filter, e.g. 'Data Privacy'")
    ap.add_argument("--regions", default=None, help="CSV filter, e.g. 'Germany,Australia'")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="re-extract even if PDF hash unchanged")
    ap.add_argument("--s3-bucket", default=None, help="writable EU bucket (omit = local only)")
    ap.add_argument("--s3-prefix", default="extractions")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--s3-region", default="eu-west-1")
    a = ap.parse_args()

    source, out = Path(a.source), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prods = {x.strip() for x in a.products.split(",")} if a.products else None
    regs = {x.strip() for x in a.regions.split(",")} if a.regions else None
    jobs = discover(source, prods, regs)
    if a.limit:
        jobs = jobs[:a.limit]
    if not jobs:
        sys.exit("no PDFs matched")
    mpath = out / "manifest.json"
    prior = {m.get("slug"): m for m in json.loads(mpath.read_text())} \
        if mpath.exists() else {}
    print(f"extracting {len(jobs)} memo(s) with {a.workers} workers -> {out}"
          f"{'  (+S3 '+a.s3_bucket+')' if a.s3_bucket else '  (LOCAL ONLY)'}", flush=True)

    manifest, t0 = [], time.time()
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for res in ex.map(lambda j: extract_one(j, out, a.depth, a.force, prior), jobs):
            manifest.append(res)
            s = res.get("stats", {})
            flag = "  ⚠ HIGH Δ" if isinstance(s.get("word_delta_pct"), (int, float)) and s["word_delta_pct"] >= 5 else ""
            print(f"  [{res['status']:6}] {res['product']}/{res['region']:20} "
                  f"Δ={s.get('word_delta_pct','?')}% tables={s.get('tables_converted',0)}{flag}"
                  f"{'  '+res.get('error','') if res['status']=='FAILED' else ''}", flush=True)

    mpath.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    s3_base = None
    if a.s3_bucket:
        s3_base = upload(out, a.s3_bucket, a.s3_prefix, a.profile, a.s3_region)
    build_gallery(manifest, out, s3_base)
    if a.s3_bucket:
        upload(out, a.s3_bucket, a.s3_prefix, a.profile, a.s3_region)  # re-push incl. gallery
    ok = sum(1 for m in manifest if m["status"] in ("ok", "cached"))
    fail = sum(1 for m in manifest if m["status"] == "FAILED")
    hi = sum(1 for m in manifest if isinstance(m.get("stats", {}).get("word_delta_pct"), (int, float)) and m["stats"]["word_delta_pct"] >= 5)
    print(f"\ndone in {time.time()-t0:.0f}s: {ok} ok, {fail} failed, {hi} flagged (Δ≥5%)")
    print(f"gallery: {out/'index.html'}" + (f"  |  {s3_base}/index.html" if s3_base else ""))


if __name__ == "__main__":
    main()
