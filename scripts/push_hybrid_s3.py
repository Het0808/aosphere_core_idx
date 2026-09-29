#!/usr/bin/env python3
"""
push_hybrid_s3 — build a self-contained viewer from each hybrid Stage-3 tree and
publish it to the Doc Gallery S3 bucket as a "<region> (MinerU)" variant, so the
2-pass (pdf2mdtree + MinerU) output can be browsed in the app for testing,
side-by-side with the existing extraction.

Reuses the same viewer template as pdf2mdtree --viewer (PDF embedded as base64;
page-snapshot images become PDF-page buttons client-side; MinerU HTML tables
render inline). Idempotent: re-publishing replaces the prior -mineru entries.

  .venv/bin/python scripts/push_hybrid_s3.py 172099 175652 179582
  .venv/bin/python scripts/push_hybrid_s3.py --all-in out/hybrid        # every doc under out/hybrid
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SOURCE = REPO / "RAG-json_docx_v1_2026-06-30"
TPL = REPO / "assets" / "viewer_template.html"
sys.path.insert(0, str(HERE))
import extract_to_s3 as E
from check_scorecard import compute_scorecard
from hybrid_extract import clean_mineru_text, strip_filled_snapshots  # footnote LaTeX + redundant snapshots
from lib_validate import validate as _validate  # the ONE canonical nine-check set (shared with run_corpus + the dashboard)

# The naming rules live in lib_gallery so a PROMOTION can resolve the prefix and build a
# slug without importing this module — which pulls in hybrid_extract -> pdf2mdtree -> fitz
# -> MinerU at module scope. Re-exported here because run_corpus, corpus_worker and
# tests/test_gallery_prefix.py all reach for them by this module's name.
from lib_gallery import (  # noqa: E402
    BUCKET, LEGACY_PREFIX, PROFILE, REGION, doc_name_fields, gallery_prefix, gslug,
    rescue_flag, rescue_from_scorecard, rescue_from_toc,
)

__all__ = ["BUCKET", "LEGACY_PREFIX", "PROFILE", "REGION", "doc_name_fields",
           "gallery_prefix", "gslug", "rescue_flag", "rescue_from_scorecard",
           "rescue_from_toc"]


def slug_stem(pdf) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(pdf).stem).strip("-") or "document"


def _page_of(md_text: str):
    m = re.search(r"page (\d+)", md_text)
    return int(m.group(1)) if m else None


def build_viewer(tree_dir: Path, pdf_path: Path, title: str, subtitle: str,
                 stages: dict[int, Path] | None = None,
                 current_stage: int | None = None) -> str:
    """The self-contained MD/PDF viewer for one document.

    `stages` adds the document's OTHER trees ({3: dir, 4: dir, 5: dir}) so the page can
    switch between the deterministic extraction and what AI post-processing made of it.
    Without it the viewer shows exactly the one tree it is handed, which is what left a
    reader unable to see the AI output at all.
    """
    files = {}

    def build_node(dirpath: Path, rel="", root_name=None):
        node = {"name": root_name or dirpath.name, "dirs": [], "files": []}
        for e in sorted(dirpath.iterdir(), key=lambda p: p.name):
            if e.name == "_assets":
                continue
            r = f"{rel}/{e.name}" if rel else e.name
            if e.is_dir():
                node["dirs"].append(build_node(e, r))
            elif e.name.endswith(".md"):
                txt = strip_filled_snapshots(clean_mineru_text(e.read_text(encoding="utf-8", errors="ignore")))
                files[r] = {"content": txt, "page": _page_of(txt)}
                node["files"].append({"name": e.name, "path": r})
        node["files"].sort(key=lambda f: (f["name"] != "README.md", f["name"]))
        return node

    if stages:
        # Every stage is built from `stages`, and tree_dir is not consulted: labelling
        # whatever directory the caller happened to pass as `current_stage` is how a
        # stage-3 tree gets served under a Stage 5 pill.
        if current_stage not in stages:
            # Highest by default — the document as the pipeline finished it, which is what
            # someone opening an AI-post-processed extraction is asking to see.
            current_stage = max(stages)
        by_stage = {}
        for n, d in sorted(stages.items()):
            files = {}                       # build_node writes into the enclosing name
            by_stage[str(n)] = {"tree": build_node(d, root_name=title), "files": files}
        cur = by_stage[str(current_stage)]
        payload = {"tree": cur["tree"], "files": cur["files"], "stage": current_stage,
                   "available": sorted(stages), "stages": by_stage}
    else:
        tree = build_node(tree_dir, root_name=title)
        payload = {"tree": tree, "files": files}
    data = json.dumps(payload).replace("</", "<\\/")
    b64 = base64.b64encode(pdf_path.read_bytes()).decode()
    html = TPL.read_text(encoding="utf-8")
    html = (html.replace("__DOC_TITLE__", title)
                .replace("__DOC_SUB__", subtitle)
                .replace("__DATA_JSON__", data)
                .replace("__PDF_B64__", b64))
    return html


def doc_display_stats(pdf_path, stage3_dir, rep: dict) -> dict:
    """Accurate gallery-card stats for a hybrid doc: real page count + the pages
    that STILL carry a snapshot after MinerU + snapshot de-dup (i.e. failed
    tables), not the misleading failed-table count. Drives the pp / tables / img
    badges."""
    import fitz
    try:
        npages = fitz.open(str(pdf_path)).page_count
    except Exception:
        npages = None
    snap = set()
    for md in Path(stage3_dir).rglob("*.md"):
        txt = strip_filled_snapshots(clean_mineru_text(md.read_text(encoding="utf-8", errors="ignore")))
        for m in re.finditer(r"page-0*(\d+)\.png", txt):
            snap.add(int(m.group(1)))
    return {"pages": npages,
            "tables_converted": rep.get("tables_filled", 0),
            "tables_total": rep.get("tables_total", 0),
            "tables_failed": rep.get("tables_failed", 0),
            "pages_snapshotted": str(sorted(snap))}


def doc_scorecard(job_root: Path) -> dict:
    """The scorecard the Doc Gallery card carries — the SAME nine checks + roll-up
    the dev dashboard and run_corpus.py use. If the job already has an on-disk
    `scorecard.json` (a `run_corpus` job, `--corpus-in`), reuse it verbatim so the
    gallery shows exactly what the dashboard scored; otherwise (a legacy `out/hybrid`
    job) recompute it here at publish time."""
    disk = Path(job_root) / "scorecard.json"
    if disk.exists():
        try:
            return json.loads(disk.read_text())
        except (OSError, json.JSONDecodeError):
            pass  # unreadable -> recompute below
    try:
        return compute_scorecard(job_root, _validate(job_root))
    except Exception as e:  # noqa: BLE001 — a crashed check must not sink the publish
        return {"gate": "unknown", "worst_score": None,
               "error": f"scorecard computation failed: {type(e).__name__}: {e}"}


def resolve(args):
    """(stem, product, region, pdf, stage3_dir, report) per doc.

    Corpus mode (--corpus-in): the canonical `run_corpus` layout
    out/corpus/<product>/<jurisdiction>__<docid>/, whose corpus_meta.json carries
    the identity and whose scorecard.json is reused as-is at publish. Legacy mode
    (default / --all-in): the flat out/hybrid/<slug_stem>/ layout, identity from
    the source corpus via extract_to_s3.discover."""
    if getattr(args, "corpus_in", None):
        root = Path(args.corpus_in).resolve()
        docs = []
        for prod_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
            product = E.product_from_dir(prod_dir.name)   # "155_Data_Privacy" -> "Data Privacy"
            for job in sorted(d for d in prod_dir.iterdir() if d.is_dir()):
                s3 = job / "03_stage3_final"
                meta = job / "corpus_meta.json"
                if not s3.exists() or not meta.exists():
                    print(f"⚠ {job.name}: no stage-3 tree / corpus_meta — skipping"); continue
                m = json.loads(meta.read_text())
                rp = s3 / "stage3_report.json"
                rep = json.loads(rp.read_text()) if rp.exists() else {}
                # Prefer the job's LOCAL source.pdf copy (run_corpus writes it); corpus_meta's
                # source_pdf may be an absolute path from the machine that ran the extraction.
                pdf = job / "source.pdf"
                if not pdf.exists():
                    pdf = Path(m.get("source_pdf", ""))
                if not pdf.exists():
                    print(f"⚠ {job.name}: source PDF not found — skipping"); continue
                docs.append((m["doc_id"], product, m["jurisdiction"], pdf, s3, rep))
        return docs

    alljobs = {j["doc_id"]: j for j in E.discover(SOURCE, None, None)}
    out_root = Path(args.all_in).resolve() if args.all_in else Path("out/hybrid").resolve()
    if args.all_in:
        stems = [d.name for d in out_root.iterdir()
                 if d.is_dir() and (d / "03_stage3_final").exists()]
    else:
        stems = args.docs
    docs = []
    for stem in stems:
        s3 = out_root / stem / "03_stage3_final"
        if not s3.exists():
            print(f"⚠ no stage-3 tree for '{stem}' under {out_root} — run the extraction first"); continue
        j = alljobs.get(stem)
        if not j:
            print(f"⚠ '{stem}' not in the source corpus — skipping"); continue
        rep = {}
        rp = out_root / stem / "03_stage3_final" / "stage3_report.json"
        if rp.exists():
            rep = json.loads(rp.read_text())
        docs.append((stem, j["product"], j["region"], Path(j["pdf"]), s3, rep))
    return docs


def build_inspect(job_dir: Path, title: str) -> str | None:
    """The extraction dashboard's six-tab document page (Scorecard / Document / MinerU
    Inspector / Validation / Stage 4 · AI / Page Review), frozen into one self-contained
    file — see scripts/build_inspect.py.

    Best-effort: a job dir missing its validation/stage2 reports (or an environment
    without the dashboard's imports) still publishes its viewer and scorecard, it just
    gets no deep-dive page."""
    try:
        from build_inspect import build_inspect_page
        return build_inspect_page(Path(job_dir), title)
    except Exception as e:  # noqa: BLE001 — never sink a publish over the extra page
        print(f"  ! inspect page skipped ({type(e).__name__}: {e})")
        return None


def publish_local_one(gallery_root: Path, product: str, region: str, doc_id: str, job_dir: Path,
                      slug_suffix: str = "") -> str | None:
    """Publish ONE run_corpus job into a local DocStore dir (out/extractions): build the
    self-contained viewer, write its scorecard, and upsert the manifest entry (with
    gate/worst_score). Lets run_corpus auto-wire a finished doc straight into the Doc
    Library — no separate publish step. `product` should already be the clean label
    (E.product_from_dir); `region` the bare jurisdiction. Returns the gate (or None)."""
    gallery_root, job_dir = Path(gallery_root), Path(job_dir)
    s3dir = job_dir / "03_stage3_final"
    pdf = job_dir / "source.pdf"
    rp = s3dir / "stage3_report.json"
    rep = json.loads(rp.read_text()) if rp.exists() else {}
    r = region + " (MinerU)"
    # slug_suffix keeps a second variant of the SAME document (e.g. a rescue awaiting
    # review) beside the original instead of replacing it. The slug is internal; the
    # row still shows the real doc id, told apart by the "rescued" flag.
    gslug_ = gslug(product, region, f"{doc_id}{slug_suffix}")
    vrel, srel = f"{product}/{r}/{gslug_}-viewer.html", f"{product}/{r}/{gslug_}-scorecard.json"
    (gallery_root / vrel).parent.mkdir(parents=True, exist_ok=True)
    (gallery_root / vrel).write_text(
        build_viewer(s3dir, pdf, f"{product} — {region} (MinerU)", "2-pass hybrid: pdf2mdtree + MinerU tables"),
        encoding="utf-8")
    sc = doc_scorecard(job_dir)
    (gallery_root / srel).write_text(json.dumps(sc, ensure_ascii=False))
    entry = {"product": product, "region": r, "slug": gslug_, "doc_id": doc_id,
             "pdf": str(pdf), "status": "ok", "viewer": vrel, "scorecard": srel,
             "gate": sc.get("gate"), "worst_score": sc.get("worst_score"),
             "stats": doc_display_stats(pdf, s3dir, rep)}
    entry.update(doc_name_fields(doc_id))
    rescued = rescue_flag(job_dir)
    if rescued:
        entry["rescued"] = rescued
    inspect = build_inspect(job_dir, f"{product} / {region} · {doc_id}")
    if inspect:
        irel = f"{product}/{r}/{gslug_}-inspect.html"
        (gallery_root / irel).write_text(inspect, encoding="utf-8")
        entry["inspect"] = irel
    mp = gallery_root / "manifest.json"
    man = json.loads(mp.read_text()) if mp.exists() else []
    man = [d for d in man if d.get("slug") != gslug_]   # idempotent upsert
    man.append(entry)
    mp.write_text(json.dumps(man, ensure_ascii=False))
    return sc.get("gate")


def _s3_client():
    """A client with bounded timeouts. Made fresh at the point of use, never held across a
    long phase — see the note in main()."""
    import boto3
    from botocore.config import Config
    cfg = Config(connect_timeout=10, read_timeout=90, retries={"max_attempts": 4})
    return boto3.Session(profile_name=PROFILE, region_name=REGION).client("s3", config=cfg)


_gslug = gslug   # historical name, kept for the readers inside this module


def _verdict(sc: dict) -> tuple:
    return (sc.get("gate"), round(float(sc.get("worst_score") or 0), 1),
            len(sc.get("findings") or []))


def present_docs(docs, s3, bucket, prefix) -> set:
    """Stems whose viewer object already EXISTS, found with ONE listing.

    The verdict-comparing check below needs a GET per document — 455 sequential round trips,
    8-15 minutes before the first upload, which is where a 10-minute run went with nothing
    published. When the question is only "did this document ever get uploaded" (resuming a
    run that died part-way), presence is enough and costs one paginated list.
    """
    keys = set()
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix + "/"}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys.update(o["Key"] for o in page.get("Contents", []))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    out = set()
    for stem, product, region, *_ in docs:
        vkey = f"{prefix}/{product}/{region} (MinerU)/{_gslug(product, region, stem)}-viewer.html"
        if vkey in keys:
            out.add(stem)
    return out


def unchanged_docs(docs, local_out, s3=None, bucket=None, prefix=None) -> set:
    """Stems already published WITH THE SAME VERDICT, so a republish can skip them.

    Every run rebuilds all 469 viewers (~3MB of HTML each) before uploading a byte, so an
    interruption — a killed session, an expired SSO token — costs the whole pass. It has cost
    it twice. Comparing the published gate/score/finding-count against the job's own scorecard
    makes a republish proportional to what actually moved: after a re-score, that is the 71
    documents whose verdict changed rather than all of them.
    """
    skip = set()
    for stem, product, region, pdf, s3dir, rep in docs:
        want = _verdict(doc_scorecard(Path(s3dir).parent))
        gslug_ = _gslug(product, region, stem)
        rel = f"{product}/{region} (MinerU)/{gslug_}-scorecard.json"
        try:
            if local_out:
                f = Path(local_out) / rel
                if not f.exists() or not (Path(local_out) / rel.replace(
                        "-scorecard.json", "-viewer.html")).exists():
                    continue
                have = _verdict(json.loads(f.read_text()))
            else:
                body = s3.get_object(Bucket=bucket, Key=f"{prefix}/{rel}")["Body"].read()
                have = _verdict(json.loads(body))
        except Exception:      # noqa: BLE001 — not published, or unreadable -> publish it
            continue
        if have == want:
            skip.add(stem)
    return skip


def _publish_local(built, root: Path):
    """Local equivalent of the S3 push: write each viewer + scorecard.json into a
    DocStore dir (out/extractions) the dev app reads, and upsert the manifest entry
    (with gate/worst_score/scorecard) per-slug — so a run_corpus doc shows up in the
    Doc Library with its gate/scorecard, no S3 credentials needed."""
    mp = root / "manifest.json"
    man = json.loads(mp.read_text()) if mp.exists() else []
    for stem, product, region, pdf, s3dir, vpath, rep in built:
        r = region + " (MinerU)"
        gslug_ = gslug(product, region, stem)
        vrel, srel = f"{product}/{r}/{gslug_}-viewer.html", f"{product}/{r}/{gslug_}-scorecard.json"
        (root / vrel).parent.mkdir(parents=True, exist_ok=True)
        (root / vrel).write_bytes(vpath.read_bytes())
        sc = doc_scorecard(s3dir.parent)
        (root / srel).write_text(json.dumps(sc, ensure_ascii=False))
        entry = {"product": product, "region": r, "slug": gslug_, "doc_id": stem,
                 "pdf": str(pdf), "status": "ok", "viewer": vrel, "scorecard": srel,
                 "gate": sc.get("gate"), "worst_score": sc.get("worst_score"),
                 "stats": doc_display_stats(pdf, s3dir, rep)}
        entry.update(doc_name_fields(stem))
        rescued = rescue_flag(s3dir.parent)
        if rescued:
            entry["rescued"] = rescued
        inspect = build_inspect(s3dir.parent, f"{product} / {region} · {stem}")
        if inspect:
            irel = f"{product}/{r}/{gslug_}-inspect.html"
            (root / irel).write_text(inspect, encoding="utf-8")
            entry["inspect"] = irel
        man = [d for d in man if d.get("slug") != gslug_]   # idempotent upsert
        man.append(entry)
        print(f"  local: {r}  scorecard {sc.get('gate','?')} @ {sc.get('worst_score','?')}")
    mp.write_text(json.dumps(man, ensure_ascii=False))
    print(f"\nlocal publish: {len(built)} doc(s) -> {mp} ({len(man)} entries)")


def backfill_inspect(gallery_root: Path) -> None:
    """Add the six-tab inspection page to docs already published without one.

    Rebuilding their viewers is unnecessary (and slow), so this walks the manifest
    instead: each entry records the absolute path of the job's source.pdf, whose
    parent IS the job dir the dashboard's payload builders read."""
    mp = gallery_root / "manifest.json"
    man = json.loads(mp.read_text())
    todo = [d for d in man if d.get("scorecard") and not d.get("inspect")]
    print(f"{len(todo)} of {len(man)} published doc(s) have no inspection page")
    done = 0
    for d in todo:
        job_dir = Path(d.get("pdf", "")).parent
        if not (job_dir / "scorecard.json").exists():
            print(f"  ⚠ {d['slug']}: job dir gone ({job_dir}) — skipping"); continue
        html = build_inspect(job_dir, f"{d['product']} / {d['region']} · {d.get('doc_id','')}")
        if not html:
            continue
        irel = f"{d['product']}/{d['region']}/{d['slug']}-inspect.html"
        (gallery_root / irel).parent.mkdir(parents=True, exist_ok=True)
        (gallery_root / irel).write_text(html, encoding="utf-8")
        d["inspect"] = irel
        done += 1
        print(f"  {d['slug']}  {len(html)//1024}KB")
    mp.write_text(json.dumps(man, ensure_ascii=False))
    print(f"\nbackfilled {done} inspection page(s) into {gallery_root}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docs", nargs="*", help="doc-id stems under out/hybrid")
    ap.add_argument("--all-in", default=None, help="publish every completed doc under this dir")
    ap.add_argument("--corpus-in", default=None,
                    help="publish a run_corpus tree (out/corpus/<product>/<jur>__<id>/); reuses each job's on-disk scorecard.json")
    ap.add_argument("--local-out", default=None,
                    help="publish into a LOCAL DocStore dir (e.g. out/extractions) the dev app reads, instead of S3 — no creds needed")
    ap.add_argument("--out", default="out/hybrid")
    ap.add_argument("--dry-run", action="store_true", help="build viewers locally, do NOT push to S3")
    ap.add_argument("--batch", type=int, default=0, metavar="N",
                    help="publish at most N documents this run (after --skip-unchanged has "
                         "removed what is already current). One 143-document run holds ~400MB "
                         "of built viewers and loses everything if a single request stalls; "
                         "small batches make progress durable and each run short.")
    ap.add_argument("--skip-present", action="store_true",
                    help="coarse resume: skip documents whose viewer is already in the target, "
                         "by presence alone (ONE listing, no per-document GET). Use to finish "
                         "an interrupted run; use --skip-unchanged when verdicts may have moved")
    ap.add_argument("--skip-unchanged", action="store_true",
                    help="skip documents already published with the same gate/score/findings "
                         "— makes a republish proportional to what moved, and a killed run "
                         "resumable instead of starting the 469-viewer build from scratch")
    ap.add_argument("--backfill-inspect", default=None, metavar="GALLERY_DIR",
                    help="only add the six-tab inspection page to docs already published in "
                         "this local DocStore dir (e.g. out/extractions); viewers untouched")
    args = ap.parse_args()

    if args.backfill_inspect:
        backfill_inspect(Path(args.backfill_inspect).resolve())
        return

    docs = resolve(args)
    if not docs:
        sys.exit("✗ no completed hybrid docs to publish")

    prefix = None
    if args.skip_present and not args.local_out and not args.dry_run:
        s3 = _s3_client()
        prefix = gallery_prefix(s3)
        have = present_docs(docs, s3, BUCKET, prefix)
        if have:
            docs = [d for d in docs if d[0] not in have]
            print(f"skipping {len(have)} document(s) already present; {len(docs)} to publish")
        if not docs:
            print("nothing to publish — every document is already present")
            return
        del s3

    if args.skip_unchanged:
        # A SHORT-LIVED client, discarded before the build. Reusing one across the ~10-minute
        # viewer build hung the very first upload: its SSO credentials come due for refresh
        # meanwhile, and the refresh blocked indefinitely — 0 objects and 0 CPU, past every
        # configured timeout. The upload phase makes its own client, below.
        s3 = prefix = None
        if not args.dry_run and not args.local_out:
            s3 = _s3_client()
            prefix = gallery_prefix(s3)
        skip = unchanged_docs(docs, args.local_out, s3, BUCKET, prefix)
        if skip:
            docs = [d for d in docs if d[0] not in skip]
            print(f"skipping {len(skip)} document(s) already published with this verdict; "
                  f"{len(docs)} to publish")
        if not docs:
            print("nothing to publish — every document is already current")
            return
        del s3
    if args.batch:
        docs = docs[:args.batch]
        print(f"this run: {len(docs)} document(s)")

    local_dir = Path(args.out).resolve() / "_mineru_viewers"
    local_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for stem, product, region, pdf, s3dir, rep in docs:
        title = f"{product} — {region} (MinerU)"
        sub = "2-pass hybrid: pdf2mdtree + MinerU tables"
        html = build_viewer(s3dir, pdf, title, sub)
        vpath = local_dir / f"{stem}-mineru-viewer.html"
        vpath.write_text(html, encoding="utf-8")
        built.append((stem, product, region, pdf, s3dir, vpath, rep))
        print(f"  built {region} (MinerU): {len(html)//1024}KB  "
              f"tables {rep.get('tables_filled','?')}/{rep.get('tables_total','?')} filled, "
              f"{rep.get('tables_failed','?')} failed")

    if args.dry_run:
        print(f"\n[dry-run] {len(built)} viewer(s) in {local_dir} — not pushed.")
        return

    if args.local_out:
        _publish_local(built, Path(args.local_out).resolve())
        return

    s3 = _s3_client()                    # fresh: see the note in the skip block above
    prefix = gallery_prefix(s3)
    print(f"publishing gallery -> s3://{BUCKET}/{prefix}/")
    try:
        _o = s3.get_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json")
        man, man_etag = json.loads(_o["Body"].read()), _o["ETag"]
    except s3.exceptions.NoSuchKey:
        man, man_etag = [], None      # first publish for this index version
    if args.skip_unchanged or args.skip_present or args.batch:
        # A PARTIAL run must not drop the rows it is not republishing. Getting this wrong
        # emptied the manifest: each 25-document batch stripped every -mineru row and wrote
        # back only its own, so 455 published documents ended up as 5 manifest entries and the
        # Doc Library would have listed five.
        mine = {_gslug(p_, r_, st) for st, p_, r_, *_ in built}
        man = [d for d in man if d.get("slug") not in mine]
    else:
        man = [d for d in man if not d.get("slug", "").endswith("-mineru")]  # idempotent

    for stem, product, region, pdf, s3dir, vpath, rep in built:
        r = region + " (MinerU)"
        gslug_ = gslug(product, region, stem)
        vkey = f"{prefix}/{product}/{r}/{gslug_}-viewer.html"
        s3.put_object(Bucket=BUCKET, Key=vkey, Body=vpath.read_bytes(),
                      ContentType="text/html; charset=utf-8")
        stats = doc_display_stats(pdf, s3dir, rep)
        sc = doc_scorecard(s3dir.parent)
        skey = f"{prefix}/{product}/{r}/{gslug_}-scorecard.json"
        s3.put_object(Bucket=BUCKET, Key=skey, Body=json.dumps(sc, ensure_ascii=False).encode(),
                      ContentType="application/json")
        entry = {"product": product, "region": r, "slug": gslug_, "doc_id": stem,
                 "pdf": str(pdf), "status": "ok",
                 "viewer": vkey.split(prefix + "/", 1)[1],
                 "scorecard": skey.split(prefix + "/", 1)[1],
                 "gate": sc.get("gate"), "worst_score": sc.get("worst_score"), "stats": stats}
        entry.update(doc_name_fields(stem))
        rescued = rescue_flag(s3dir.parent)
        if rescued:
            entry["rescued"] = rescued
        inspect = build_inspect(s3dir.parent, f"{product} / {region} · {stem}")
        if inspect:
            ikey = f"{prefix}/{product}/{r}/{gslug_}-inspect.html"
            s3.put_object(Bucket=BUCKET, Key=ikey, Body=inspect.encode("utf-8"),
                          ContentType="text/html; charset=utf-8")
            entry["inspect"] = ikey.split(prefix + "/", 1)[1]
        man.append(entry)
        print(f"  pushed {r}  ({rep.get('tables_filled','?')}/{rep.get('tables_total','?')} tables)  "
              f"scorecard: {sc.get('gate','?')} @ {sc.get('worst_score','?')}")

    # CONDITIONAL. The manifest is read at the top of this function and written at the
    # bottom, and everything between is minutes of uploads — so another writer (a promotion,
    # or a second push) can commit in that window and this write would silently discard it.
    # IfMatch turns that from data loss into a refusal the operator can act on. See the
    # 455 -> 5 note in the skip block above for what losing manifest rows looks like.
    try:
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json",
                      Body=json.dumps(man, ensure_ascii=False).encode(),
                      ContentType="application/json",
                      **({"IfMatch": man_etag} if man_etag else {"IfNoneMatch": "*"}))
    except Exception as e:  # noqa: BLE001 — botocore's error class varies by client
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise
        print(f"\n✗ manifest changed under us while publishing ({code}). The documents ARE "
              f"uploaded; the manifest was NOT rewritten, so the gallery still shows the "
              f"previous set. Re-run to merge them in.")
        raise SystemExit(4) from e
    print(f"\nmanifest updated: {len(man)} docs. Refresh the Doc Library → '(MinerU)' cards carry the gate + scorecard.")


if __name__ == "__main__":
    main()
