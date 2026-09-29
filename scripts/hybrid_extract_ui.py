#!/usr/bin/env python3
"""
hybrid_extract_ui — a small local web UI over hybrid_extract.py.

Upload a PDF, watch it move through Stage 1 (extract) -> Stage 2 (MinerU
tables) -> Stage 3 (combine), then browse the result with every table
visually flagged by where it came from:

  blue card   = MinerU-extracted table (Stage 2)
  red card    = table MinerU could not produce (flagged, not dropped)
  plain text  = the deterministic extractor (Stage 1) — headings, prose,
                footnotes, formulas, page snapshots

Local only — no S3, no auth, in-memory job store. For dev/testing use.

Usage:
  python3 scripts/hybrid_extract_ui.py [--port 8811]
  then open http://127.0.0.1:8811/
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hybrid_extract as he  # noqa: E402
from check_word_coverage import compute_word_coverage  # noqa: E402
from check_content_localized import compute_content_localized  # noqa: E402
from check_numeric_integrity import compute_numeric_integrity  # noqa: E402
from check_table_placement import compute_table_placement  # noqa: E402
from check_engine_agreement import compute_engine_agreement  # noqa: E402
from check_semantic_integrity import compute_semantic_integrity  # noqa: E402
from check_heading_hierarchy import compute_heading_hierarchy  # noqa: E402
from check_table_presence import compute_table_presence  # noqa: E402
from check_footnote_integrity import compute_footnote_integrity  # noqa: E402
from check_scorecard import _significant_number, compute_scorecard  # noqa: E402
import lib_dismissals as dis
import lib_review as rev  # noqa: E402
from compare_stage_completeness import (  # noqa: E402
    final_stage, heading_coverage, table_row_completeness, text_diff,
)
from lib_content_compare import (  # noqa: E402
    StageDirectoryNotFoundError, iter_content_files, parse_page_range, resolve_stage_dir)

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, Response  # noqa: E402

MINERU_BACKENDS = ("pipeline", "vlm-engine", "hybrid-engine")
MINERU_EFFORTS = ("medium", "high")

ROOT = HERE.parent / "out" / "hybrid_extractions" / "_ui"
ROOT.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

app = FastAPI(title="hybrid_extract UI")


@app.middleware("http")
async def _no_cache(request, call_next):
    """Never let a browser cache anything here.

    The page and its JSON are regenerated on every request and change whenever a
    check is edited or a job is re-scored, so a cached copy is always wrong — and
    it is indistinguishable from "the fix didn't work", which cost real debugging
    time. Page images are the one exception: they are derived from an immutable
    source PDF, so they may be cached briefly."""
    resp = await call_next(request)
    if "/page_image/" in request.url.path or "/page_bbox_image/" in request.url.path:
        resp.headers["Cache-Control"] = "private, max-age=300"
    else:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


# ---------------- job execution ----------------
# The upload UI scores on the ONE canonical 9-check set (was a hand-kept copy).
from lib_validate import validate as _run_validation  # noqa: E402
from fallback_chain import run_chain  # noqa: E402 — ordered: TOC rescue, then MinerU


def _run_job(job_id: str) -> None:
    job = JOBS[job_id]
    root = Path(job["root"])
    pdf_path = root / "source.pdf"
    try:
        job["status"] = "stage1"
        manifest = he.run_stage1(pdf_path, root / "01_stage1_extract")
        job["manifest"] = manifest

        job["status"] = "stage2"
        stage2_report = he.run_stage2(pdf_path, manifest, root / "02_stage2_mineru_tables",
                                      job.get("mineru_backend"), job.get("mineru_effort"))
        job["stage2_report"] = stage2_report

        job["status"] = "stage3"
        stage3_report = he.run_stage3(root / "01_stage1_extract", root / "02_stage2_mineru_tables",
                                      root / "03_stage3_final", stage2_report)
        job["stage3_report"] = stage3_report

        job["status"] = "validate"
        job["validation"] = _run_validation(root)
        try:
            job["scorecard"] = compute_scorecard(root, job["validation"])
        except Exception as e:  # noqa: BLE001 — a scoring bug must not sink a good extraction
            job["scorecard"] = {"error": str(e), "gate": "unknown"}

        # Same ordered fallback chain the corpus run uses (printed-TOC rescue first,
        # whole-document MinerU only if that did not recover it) — the UI shows which
        # tier is running as the job status.
        def _tier(name, fn):
            job["status"] = name
            return fn()

        job["validation"], job["scorecard"] = run_chain(
            root, pdf_path, job["validation"], job["scorecard"],
            backend=job.get("mineru_backend"), effort=job.get("mineru_effort"),
            step=_tier)

        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 — surfaced to the UI, not swallowed
        job["status"] = "error"
        job["error"] = str(e)


BASELINE_ROOT = HERE.parent / "out" / "baseline"

# ---------------- corpus mode ----------------
# Set by --corpus-root. When present the dashboard also serves /corpus: a
# hierarchical view over a whole folder tree extracted by run_corpus.py, laid out
# <product>/<jurisdiction>__<doc_id>/. Same server, same checks, same per-document
# tabs — only the overview page is new, so every fix here applies to both views.
CORPUS_ROOT: Path | None = None


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _stage4_status(d: Path) -> dict | None:
    """Stage 4's own per-section health, for the corpus table's AI-stage column.

    None when this document never touched Stage 4 at all (ACI_STAGE4_AI_ENABLED was off, or
    the product simply has not reached it yet) — that is not a failure, and the column stays
    blank for it exactly the way "orphans" stays blank for a document with no tables.

    A `04_stage4_ai` directory with no `stage4_report.json` inside it means Stage 4 STARTED
    (the directory is created before anything is written into it) and never finished writing
    its report — killed, crashed, or wedged on a call that never returned. That is the "just
    didn't happen at all" half of the signal this column exists to give; `ok: false` on an
    individual section (a timeout, a throttle exhausting its retries, a validation error) is
    the other half, and both are reported the same way so the caller never has to ask which
    kind of red this is before deciding to look.

    A third gap `ok: false` cannot see: a section Stage 1 produced that the report never
    mentions AT ALL. bedrock.call.start records the section starting, but a process killed
    between sections leaves later ones with no entry whatsoever rather than a failed one —
    silence, not a recorded failure — so this is checked separately against what Stage 1
    actually produced, not inferred from the report alone."""
    stage4_dir = d / "04_stage4_ai"
    if not stage4_dir.exists():
        return None
    report = _read_json(stage4_dir / "stage4_report.json")
    if report is None:
        return {"status": "incomplete", "failed_sections": [],
                "reason": "Stage 4 started (04_stage4_ai exists) but never wrote its "
                          "report — crashed, was killed, or is still stuck mid-run"}
    sections = report.get("sections") or []
    failed = [{"file": s.get("file"), "reason": s.get("reason") or "failed, no reason recorded"}
              for s in sections if not s.get("ok")]
    stage1_dir = d / "01_stage1_extract"
    if stage1_dir.exists():
        reported = {s.get("file") for s in sections}
        stage1_files = {f.name for f in stage1_dir.glob("*.md") if f.name[:1].isdigit()}
        for missing in sorted(stage1_files - reported):
            failed.append({"file": missing,
                           "reason": "never attempted — absent from stage4_report.json entirely"})
    return {"status": "failed" if failed else "ok", "failed_sections": failed}


def _corpus_jobs() -> list[dict]:
    """Every corpus job on disk, newest scorecard first.

    Reads each job's SAVED scorecard.json rather than recomputing. Validation is
    the slow part of scoring, and re-running nine checks across 100+ documents on
    every page load would make the overview unusable — run_corpus.py already paid
    that cost once and wrote the answer down."""
    if not CORPUS_ROOT or not CORPUS_ROOT.exists():
        return []
    out = []
    for product in sorted(p for p in CORPUS_ROOT.iterdir() if p.is_dir()):
        for d in sorted(x for x in product.iterdir() if x.is_dir()):
            meta_p, sc_p = d / "corpus_meta.json", d / "scorecard.json"
            if not meta_p.exists():
                continue
            try:
                meta = json.loads(meta_p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            sc = None
            if sc_p.exists():
                try:
                    sc = json.loads(sc_p.read_text())
                except (OSError, json.JSONDecodeError):
                    sc = None
            # A document that went through AI post-processing is judged on what it NOW
            # IS, not on the tree stage 3 left behind. scorecard.json is the extraction
            # gate and the --resume marker and is never rewritten, so where a post-AI
            # scorecard exists it is the one that answers "is this document good".
            # Reading the wrong one made this page a stage out of date: Jersey showed
            # review 87.1 from stage 3 while its stage-5 tree scores pass 94.6, and the
            # product row took 87.1 as the corpus's worst document.
            #
            # Core Index already resolves it this way (run_corpus._final_verdict); this
            # is the same rule on the corpus overview so the two cannot disagree.
            sc_stage = 3
            post_p = d / "scorecard_post_ai.json"
            if post_p.exists():
                try:
                    post = json.loads(post_p.read_text())
                except (OSError, json.JSONDecodeError):
                    post = None
                if post and post.get("gate") is not None:
                    sc, sc_stage = post, post.get("scored_stage") or 5
            out.append({
                "job_id": f"cp-{product.name}-{d.name}",
                "root": str(d),
                "product": meta.get("product", product.name),
                "jurisdiction": meta.get("jurisdiction", ""),
                "doc_id": meta.get("doc_id", d.name),
                "label": d.name,
                # A job with a meta file but no scorecard is one the runner has
                # started and not finished — shown as "running" so a long document
                # is visibly in progress rather than silently absent.
                "done": sc is not None,
                # Which stage a still-running document is in. Read from the
                # progress.json each stage stamps (hybrid_extract.write_progress):
                # without it a 20-minute Stage 2 is indistinguishable from a hang,
                # because MinerU writes nothing until it exits.
                "progress": (_read_json(d / "progress.json") if sc is None else None),
                "gate": (sc or {}).get("gate"),
                "worst_score": (sc or {}).get("worst_score"),
                "weakest": (sc or {}).get("weakest_dimension"),
                # WHICH scorecard this row is reporting. Never inferred at render time:
                # both files have the same shape, so a reader who cannot tell them apart
                # will compare a post-AI score against a neighbour's extraction score
                # and conclude the wrong thing.
                "scored_stage": sc_stage,
                # Orphan tables, on the TREE row rather than only inside the job
                # view: a table MinerU extracted that no section took is content
                # loss, and it was previously invisible until you clicked in.
                # `lost` is the actionable one; `recovered` and `duplicate` are
                # reported so a green row still says what happened.
                "orphans": {
                    k: (((sc or {}).get("dimensions") or {}).get("completeness") or {})
                       .get("detail", {}).get(f"orphan_tables_{k}", 0)
                    for k in ("lost", "recovered", "duplicate")},
                "dimensions": {k: (v or {}).get("score")
                               for k, v in ((sc or {}).get("dimensions") or {}).items()},
                # What the document cost. run_corpus writes this into the scorecard
                # (timing_block); the tree reads it rather than re-deriving anything,
                # so an unscored or pre-timing document simply reports None.
                "seconds": ((sc or {}).get("timing") or {}).get("seconds"),
                "seconds_per_page": ((sc or {}).get("timing") or {}).get("seconds_per_page"),
                "fallback_seconds": ((sc or {}).get("timing") or {}).get("fallback_seconds") or 0,
                "timing_pages": ((sc or {}).get("timing") or {}).get("pages"),
                "tables": len((sc or {}).get("tables") or []),
                "tables_failed": sum(1 for t in ((sc or {}).get("tables") or [])
                                     if t.get("bucket") == "failed"),
                "findings": (sc or {}).get("active_finding_count"),
                "fallback": (sc or {}).get("fallback"),
                # A document scored under a DIFFERENT rule than its neighbours has to
                # say so from the corpus view too: 94.4 here and 94.4 on a 200-page
                # document did not measure the same things, and only this field
                # distinguishes them at a glance.
                "special_mode": (sc or {}).get("special_mode"),
                # Was the outline Stage 1 built the tree from fit for the job?
                # Carried here so the corpus table can show it without opening
                # 130 validation files.
                "toc_status": (((sc or {}).get("dimensions") or {}).get("toc") or {})
                              .get("detail", {}).get("status"),
                "toc_score": (((sc or {}).get("dimensions") or {}).get("toc") or {}).get("score"),
                "toc_rescuable": (((sc or {}).get("dimensions") or {}).get("toc") or {})
                                 .get("detail", {}).get("rescuable"),
                "toc_note": (((sc or {}).get("dimensions") or {}).get("toc") or {})
                            .get("detail", {}).get("explanation"),
                # Stage 4's own per-section health — green/red, and which section if red.
                # Read straight off disk like everything else here, not off the scorecard:
                # a document can gate "pass" on its stage-3 tree while Stage 4 quietly lost
                # a section repairing it, and scorecard_post_ai has no field for that.
                "stage4": _stage4_status(d),
            })
    return out


def _corpus_tree() -> dict:
    """Group corpus jobs by product folder and summarise each.

    The folder-level number is deliberately NOT a plain average of the scores. A
    folder holding a 95 and a 50 averages to a comfortable-looking 72, which hides
    exactly the document you need to look at. So each folder reports the COUNTS of
    pass/review/fail plus its WORST document's score — you can see both how much
    is healthy and how bad the worst case is."""
    jobs = _corpus_jobs()
    prog = {}
    if CORPUS_ROOT and (CORPUS_ROOT / "_progress.json").exists():
        try:
            prog = json.loads((CORPUS_ROOT / "_progress.json").read_text())
        except (OSError, json.JSONDecodeError):
            prog = {}
    def _roll_up(node: dict, children: list[dict]) -> None:
        """Give a level its own counts and scores from whatever sits under it.

        Used at BOTH the jurisdiction and the product level so the two agree by
        construction — a product's numbers are its jurisdictions' numbers summed,
        not a separately-derived figure that could drift from them."""
        node["pass"] = sum(c.get("pass", 0) for c in children)
        node["review"] = sum(c.get("review", 0) for c in children)
        node["fail"] = sum(c.get("fail", 0) for c in children)
        node["unknown"] = sum(c.get("unknown", 0) for c in children)
        node["running"] = sum(c.get("running", 0) for c in children)
        node["total"] = sum(c.get("total", 0) for c in children)
        scored = [c["worst_score"] for c in children
                  if isinstance(c.get("worst_score"), (int, float))]
        means = [(c["mean_score"], c.get("scored", 0)) for c in children
                 if isinstance(c.get("mean_score"), (int, float))]
        node["worst_score"] = min(scored) if scored else None
        # weighted by document count so a jurisdiction holding three documents
        # counts three times in its product's mean, not once
        tot_n = sum(n for _, n in means)
        node["mean_score"] = (round(sum(m * n for m, n in means) / tot_n, 1)
                              if tot_n else None)
        node["scored"] = tot_n
        # Summed, not averaged: "what did this product cost" is a total. None when
        # nothing underneath carries timing, so the column stays blank rather than
        # claiming a folder of unscored documents took 0s.
        secs = [c["seconds"] for c in children if isinstance(c.get("seconds"), (int, float))]
        node["seconds"] = round(sum(secs), 1) if secs else None
        fbs = [c.get("fallback_seconds") or 0 for c in children]
        node["fallback_seconds"] = round(sum(fbs), 1) if any(fbs) else 0
        node["verdict"] = ("fail" if node["fail"] else "review" if node["review"]
                           else "pass" if node["pass"] else "pending")

    # product -> jurisdiction -> documents
    folders: dict[str, dict] = {}
    for j in jobs:
        f = folders.setdefault(j["product"], {"product": j["product"], "jurisdictions": {}})
        jr = f["jurisdictions"].setdefault(j["jurisdiction"] or "(root)", {
            "jurisdiction": j["jurisdiction"] or "(root)", "documents": [],
            "pass": 0, "review": 0, "fail": 0, "unknown": 0, "running": 0,
        })
        jr["documents"].append(j)
        if not j["done"]:
            jr["running"] += 1
        else:
            jr[j["gate"] if j["gate"] in ("pass", "review", "fail") else "unknown"] += 1

    for f in folders.values():
        for jr in f["jurisdictions"].values():
            scored = [d["worst_score"] for d in jr["documents"]
                      if d["done"] and isinstance(d["worst_score"], (int, float))]
            jr["worst_score"] = min(scored) if scored else None
            jr["mean_score"] = round(sum(scored) / len(scored), 1) if scored else None
            jr["total"] = len(jr["documents"])
            jr["scored"] = len(scored)
            jr["verdict"] = ("fail" if jr["fail"] else "review" if jr["review"]
                             else "pass" if jr["pass"] else "pending")
            jr["documents"].sort(key=lambda d: (d["worst_score"] is None, d["worst_score"]))
        # worst-first within a product, so the jurisdiction to look at is on top
        f["jurisdictions"] = sorted(
            f["jurisdictions"].values(),
            key=lambda x: (x["worst_score"] is None,
                           x["worst_score"] if x["worst_score"] is not None else 999))
        _roll_up(f, f["jurisdictions"])
        # kept so existing consumers (and the drill-down) still see a flat list
        f["documents"] = [d for jr in f["jurisdictions"] for d in jr["documents"]]
    ordered = sorted(folders.values(),
                     key=lambda f: ({"fail": 0, "review": 1, "pass": 2, "pending": 3}[f["verdict"]],
                                    f["worst_score"] if f["worst_score"] is not None else 999))
    return {"folders": ordered, "progress": prog,
            "totals": {
                "folders": len(ordered),
                "documents": sum(f["total"] for f in ordered),
                "pass": sum(f["pass"] for f in ordered),
                "review": sum(f["review"] for f in ordered),
                "fail": sum(f["fail"] for f in ordered),
                "running": sum(f["running"] for f in ordered),
            }}


def _load_corpus_artifacts(root: Path, job: dict) -> None:
    """Read a corpus job's on-disk reports into its job entry.

    Shared by first registration and by the later upgrade of a job that was
    mid-extraction when it was first seen, so the two paths cannot load different
    sets of files and disagree about what a document contains."""
    for name, path in (("manifest", "01_stage1_extract/tables_manifest.json"),
                       ("stage2_report", "02_stage2_mineru_tables/stage2_report.json"),
                       ("stage3_report", "03_stage3_final/stage3_report.json"),
                       ("validation", "validation.json"),
                       ("scorecard", "scorecard.json")):
        p = root / path
        if p.exists():
            try:
                job[name] = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                pass


def _adopt_corpus_jobs() -> None:
    """Register corpus jobs in the shared store so the existing per-document tabs
    (inspector, page review, validation, scorecard) work on them unchanged.

    Uses the scorecard and validation the runner already wrote — nothing is
    re-validated here, so registering 129 documents costs a few file reads."""
    if not CORPUS_ROOT:
        return
    n = 0
    with JOBS_LOCK:
        for j in _corpus_jobs():
            jid = j["job_id"]
            existing = JOBS.get(jid)
            if existing is not None:
                # Already registered — but if it was registered WHILE the runner was
                # still on it, its entry says "running" and there is nothing that
                # would ever revisit it. Upgrade in place once the scorecard appears,
                # so the document page stops claiming a finished extraction is still
                # in progress. Skipped only when nothing has changed.
                if not j["done"] or existing.get("status") == "done":
                    continue
                _load_corpus_artifacts(Path(j["root"]), existing)
                existing["status"] = "done"
                n += 1
                continue
            root = Path(j["root"])
            job = {
                "id": jid, "filename": f"{j['product']} / {j['label']}",
                "created_at": root.stat().st_mtime,
                "status": "done" if j["done"] else "running",
                "error": None, "root": str(root),
                "mineru_backend": None, "mineru_effort": None,
                "manifest": None, "stage2_report": None, "stage3_report": None,
                "adopted": True, "source": "corpus",
                "product": j["product"], "jurisdiction": j["jurisdiction"],
                # needed to tell two documents of the SAME jurisdiction apart in
                # the structure panel's cohort charts (see _cohort_for)
                "doc_id": j.get("doc_id"),
            }
            _load_corpus_artifacts(root, job)
            JOBS[jid] = job
            n += 1
    if n:
        print(f"corpus: registered/updated {n} document(s) from {CORPUS_ROOT}")


def _score_existing(job_id: str) -> None:
    """Run only the checks against an extraction already on disk — no MinerU."""
    job = JOBS[job_id]
    root = Path(job["root"])
    try:
        for name, path in (("manifest", "01_stage1_extract/tables_manifest.json"),
                           ("stage2_report", "02_stage2_mineru_tables/stage2_report.json"),
                           ("stage3_report", "03_stage3_final/stage3_report.json")):
            p = root / path
            if p.exists():
                job[name] = json.loads(p.read_text())
        job["status"] = "validate"
        job["validation"] = _run_validation(root)
        try:
            job["scorecard"] = compute_scorecard(root, job["validation"])
        except Exception as e:  # noqa: BLE001
            job["scorecard"] = {"error": str(e), "gate": "unknown"}
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)


def _adopt_disk_jobs(max_recent: int = 14) -> None:
    """Register completed extractions found on disk as jobs.

    Two problems this solves. The job store is in-memory, so every restart lost
    the job list even though all the output was still on disk. And extractions
    produced outside the UI — run_baseline.py writes to out/baseline/ — were
    never visible in the dashboard at all.

    Adopting costs no GPU: stages 1-3 are already done, so only the checks run.
    They run in ONE background thread, sequentially, because validating a dozen
    documents at once would swamp the machine; the list fills in as each
    finishes, exactly as it does for a fresh upload."""
    found = []
    for base in (ROOT, BASELINE_ROOT):
        if not base.exists():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or not (d / "source.pdf").exists():
                continue
            if not (d / "03_stage3_final").exists():
                continue
            found.append((base, d))
    # Newest first, capped — this directory accumulates every run ever made and
    # re-scoring all of them on every boot would take longer than it is worth.
    found.sort(key=lambda bd: bd[1].stat().st_mtime, reverse=True)
    found = found[:max_recent]

    adopted = []
    with JOBS_LOCK:
        for base, d in found:
            job_id = d.name if base is ROOT else f"bl-{d.name}"
            if job_id in JOBS:
                continue
            JOBS[job_id] = {
                "id": job_id,
                "filename": (f"{d.name}.pdf" if base is BASELINE_ROOT else d.name),
                "created_at": d.stat().st_mtime,
                "status": "queued", "error": None, "root": str(d),
                "mineru_backend": None, "mineru_effort": None,
                "manifest": None, "stage2_report": None, "stage3_report": None,
                "adopted": True, "source": "baseline" if base is BASELINE_ROOT else "ui",
            }
            adopted.append(job_id)

    if not adopted:
        return

    def _worker():
        for jid in adopted:
            _score_existing(jid)

    threading.Thread(target=_worker, daemon=True).start()
    print(f"adopting {len(adopted)} existing extraction(s) from disk; "
          f"scoring them in the background")


def _build_page_issues(job: dict, view: str = "extraction") -> dict:
    """Every page-attributable finding, keyed by page, plus each content file's own
    page range (so a page can be placed in its section for navigation even if that
    file has no findings of its own).

    DRIVEN BY THE SCORECARD'S OWN FINDINGS LIST, and that is the point. This used to
    re-derive page issues from three checks — dropped spans and missing numbers —
    while the heat-map beside it coloured pages from a DIFFERENT and larger set (see
    check_scorecard._page_states: the section census, failed and lost tables,
    unreadable pages). The two disagreed by construction, so a page could be painted
    red and then show "No flagged issues on this page", and nothing at all reported a
    lost table, a hollow section, an inverted clause or an orphan table against the
    page it happened on.

    _collect_findings already produces all of those with page numbers attached, so the
    fix is to stop re-deriving and read them. One list, one page attribution, and the
    map and the list can no longer contradict each other.

    `view` selects WHICH scorecard — the extraction gate or the post-AI one — so the
    page review matches the scorecard on screen. It also follows that scorecard's
    stage: reading stage 3's tree while showing Scorecard 2 named files that stage 5
    had already renamed.
    """
    sc = _fresh_scorecard(job, view=view) or {}
    stage = sc.get("scored_stage") or 3
    root = Path(job["root"])
    try:
        tree_root = resolve_stage_dir(root, stage)
    except StageDirectoryNotFoundError:
        # The hardcoded 03_stage3_final fallback assumed every job has a stage 3 --
        # not true for the summary-AI route, which has no stage 1-3 at all and writes
        # straight into 04_stage4_ai. Its scorecard carries no "scored_stage" either,
        # so `stage` above defaults to 3, resolve_stage_dir finds no 03_* directory,
        # and the old fallback pointed at a path that does not exist -- file_ranges
        # came back empty and every single page read "no extracted section maps to
        # this page". Same "pick whichever stage is actually on disk" rule job_data()
        # already uses for the Document tab, which is why that tab works for these
        # jobs and this one, reading its own hardcoded guess, did not.
        available = [s for s, d in STAGE_DIRS.items() if (root / d).exists()]
        tree_root = root / STAGE_DIRS[max(available)] if available else root / "03_stage3_final"

    file_ranges = []
    for f in iter_content_files(tree_root):
        rng = parse_page_range(f.read_text(encoding="utf-8"))
        if rng:
            file_ranges.append({"file": str(f.relative_to(tree_root)), "pages": list(rng)})

    page_issues: dict[int, list[dict]] = {}

    def add(pno, source, kind, text, detail, **extra):
        page_issues.setdefault(pno, []).append(
            {"source": source, "kind": kind, "text": text, "detail": detail, **extra})

    # A finding is listed on EVERY page of its range, not only its first.
    #
    # It used to land on pages[0] alone, to stop one finding smearing across a section's
    # whole page range — which is the right rule for the heat-map, where a colour per
    # page is a claim about that page. But this list is not the heat-map: it is what a
    # reader reads while paging through the PDF hunting for the text a finding names.
    # A gap that says "5 tokens on pages 2-3 are not in this section" appeared on page 2
    # and vanished on page 3 — exactly where the reader had turned to look for it — so
    # the one finding that told you to check page 3 was invisible from page 3.
    #
    # The heat-map is unaffected: it is painted from check_scorecard._page_states, a
    # separate computation that still attributes each finding to one page, so the "53 of
    # 57 pages coloured" failure that rule exists to prevent cannot come back through
    # here. Each entry carries `anchor` (is this the page the finding is filed under)
    # and `span`, so anything that needs the old one-page attribution still has it.
    #
    # Measured over the 16-09-26 corpus: of 914 findings carrying pages, 69% span a
    # single page and 97.5% span 12 or fewer, the widest being 46. There is no run here
    # long enough to need capping.
    for f in sc.get("findings") or []:
        pages = [p for p in (f.get("pages") or []) if p]
        if not pages:
            continue
        # A gap the output already acknowledges is a different colour from a silent
        # one, and that distinction was the one thing the old builder got right.
        kind = f.get("kind")
        if kind == "gap" and f.get("severity") == "flagged":
            kind = "gap_acknowledged"
        where = f.get("file")
        detail = f.get("detail") or ""
        # check_source_fidelity.py's cell-level findings (section_boundary_leak,
        # duplicated_content, missing_cell_answer, ...) carry the actual flagged
        # text in evidence.text -- the one thing a reader paging through the PDF
        # actually needs to find the row a generic category label ("duplicated
        # content") cannot point to. Quoted and capped: this is a hunt aid, not a
        # second copy of the cell.
        cited = (f.get("evidence") or {}).get("text")
        if cited:
            snippet = cited if len(cited) <= 200 else cited[:200] + "…"
            detail = f'{detail} — "{snippet}"' if detail else f'"{snippet}"'
        lo, hi = min(pages), max(pages)
        for pno in range(lo, hi + 1):
            add(pno, f.get("dimension") or "scorecard", kind,
                f.get("title") or "", (f"{where} — " if where else "") + detail,
                severity=f.get("severity"), dimension=f.get("dimension"),
                dismissed=bool(f.get("dismissed")),
                anchor=(pno == lo), span=[lo, hi])

    return {"file_ranges": file_ranges, "scored_stage": stage, "view": view,
            "page_issues": {str(k): v2 for k, v2 in sorted(page_issues.items())}}


def _job_summary(j: dict) -> dict:
    # Every read below is defensive on purpose. This summary is built from
    # validation dicts that may have been written by a DIFFERENT producer (the
    # dashboard's own _run_validation, or run_corpus.py's saved validation.json)
    # and may predate a check being added. A single missing key used to raise
    # KeyError straight out of the endpoint, which showed up as a job page stuck
    # on "loading" with no visible error anywhere.
    v = j.get("validation") or {}
    sc = j.get("scorecard") or {}

    def _chk(name: str) -> dict:
        got = v.get(name)
        return got if isinstance(got, dict) else {}

    return {
        "id": j["id"], "filename": j["filename"], "status": j["status"],
        "created_at": j["created_at"], "error": j["error"],
        "mineru_backend": j.get("mineru_backend"), "mineru_effort": j.get("mineru_effort"),
        "gate": sc.get("gate"), "worst_score": sc.get("worst_score"),
        "weakest_dimension": sc.get("weakest_dimension"),
        "tables_total": len(j["manifest"]["tables"]) if j.get("manifest") else None,
        "tables_ok": j["stage2_report"]["tables_ok"] if j.get("stage2_report") else None,
        "tables_failed": j["stage2_report"]["tables_failed"] if j.get("stage2_report") else None,
        "validation_passed": v.get("passed"),
        "coverage_pct": _chk("word_coverage").get("coverage_adjusted_pct"),
        "silent_gaps": _chk("content_localized").get("silent_count"),
        "transpositions": len(_chk("numeric_integrity").get("transpositions") or []),
    }


# ---------------- API ----------------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...), mineru_backend: str = Form("hybrid-engine"),
                 mineru_effort: str = Form("medium")):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "only .pdf files are supported")
    if mineru_backend not in MINERU_BACKENDS:
        raise HTTPException(400, f"mineru_backend must be one of {MINERU_BACKENDS}")
    if mineru_effort not in MINERU_EFFORTS:
        raise HTTPException(400, f"mineru_effort must be one of {MINERU_EFFORTS}")
    job_id = uuid.uuid4().hex[:10]
    root = ROOT / job_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "source.pdf").write_bytes(await file.read())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "filename": file.filename, "created_at": time.time(),
            "status": "queued", "error": None, "root": str(root),
            "mineru_backend": mineru_backend, "mineru_effort": mineru_effort,
            "manifest": None, "stage2_report": None, "stage3_report": None,
        }
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/rescan")
def rescan_disk(body: dict = Body(default=None)):
    """Pick up extractions that finished AFTER the server started.

    Adoption otherwise only runs at boot, so anything still extracting at that
    moment — e.g. a run_baseline.py pass in progress — stays invisible until the
    next restart. This makes it a button instead."""
    before = set(JOBS)
    _adopt_disk_jobs((body or {}).get("max", 14))
    new = [j for j in JOBS if j not in before]
    return {"adopted": new, "count": len(new), "total": len(JOBS)}


@app.get("/api/corpus")
def api_corpus():
    """Folder tree + aggregates. Re-adopts on every call so documents the runner
    finishes while the page is open appear without a restart."""
    _adopt_corpus_jobs()
    return _corpus_tree()


@app.get("/api/jobs")
def list_jobs():
    jobs = sorted((_job_summary(j) for j in JOBS.values()), key=lambda x: -x["created_at"])
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    # A corpus document registered mid-extraction is marked "running"; re-adopting
    # here upgrades it the moment its scorecard lands, so refreshing the document
    # page is enough to see it finish — no server restart, no trip via /corpus.
    if CORPUS_ROOT and job_id.startswith("cp-"):
        _adopt_corpus_jobs()
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    out = _job_summary(j)
    out["tables"] = j["stage2_report"]["tables"] if j.get("stage2_report") else []
    return out


def _build_tree(root: Path, job_id: str, rel: str = ""):
    node = {"name": root.name if rel else "root", "dirs": [], "files": []}
    files = {}
    for e in sorted(p.name for p in root.iterdir()):
        if e == "_assets":
            continue
        full = root / e
        r = f"{rel}/{e}" if rel else e
        if full.is_dir():
            sub_node, sub_files = _build_tree(full, job_id, r)
            node["dirs"].append(sub_node)
            files.update(sub_files)
        elif e.endswith(".md"):
            text = full.read_text()
            text = re.sub(r"\]\((?:\.\./)*_assets/([^)]+)\)",
                          rf"](/api/jobs/{job_id}/assets/\1)", text)
            files[r] = text
            node["files"].append({"name": e, "path": r})
    node["files"].sort(key=lambda f: (f["name"] != "README.md", f["name"]))
    return node, files


# The tree the Document tab renders. Stage 4 is optional — it only exists for jobs that
# have been through AI post-processing — so the sidebar offers it as a switch rather
# than replacing stage 3, which stays the reference the two are compared against.
# Stage 1 is inspectable in its own right, not only as the input to Stage 3: a job
# run with --no-mineru (or one still mid-pipeline) has a complete markdown tree and
# nothing to combine it with, and Stage 1 is also where a typography or sectioning
# change has to be read BEFORE MinerU's tables land on top of it. Without 1 in this
# map such a job answered every /data.json with a 500 — `stage` fell back to the
# hardcoded 3 and _build_tree walked a directory that does not exist.
STAGE_DIRS = {1: "01_stage1_extract", 3: "03_stage3_final",
              4: "04_stage4_ai", 5: "05_subchunks"}


@app.get("/api/jobs/{job_id}/data.json")
def job_data(job_id: str, stage: int | None = None):
    """The job's markdown tree at `stage`, defaulting to the LAST stage it reached.

    Not 3. Stage 3 is the last stage every job has, not the last stage any given job
    ran: on a product in SUBCHUNK_PRODUCTS with Stage 4 enabled, the final chunking
    is Stage 5's nested per-sub-section tree — 41 files for Bahamas against Stage 3's
    19 — and defaulting to 3 showed the pre-AI tree as though it were the output.
    Stage 4's own re-headed tree was equally invisible unless you knew to click for
    it."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j["status"] != "done":
        raise HTTPException(409, f"job not finished yet (status={j['status']})")
    root = Path(j["root"])
    available = [s for s, d in STAGE_DIRS.items() if (root / d).exists()]
    if not available:
        raise HTTPException(404, "job has no stage output on disk")
    if stage is None or stage not in available:
        # The LATEST stage present. Also covers a job that stopped after Stage 1,
        # which has no stage 3 to fall back to and used to answer 500.
        stage = max(available)
    stage3_dir = root / STAGE_DIRS[stage]
    tree, files = _build_tree(stage3_dir, job_id)
    # Total page count, for the Document tab's page navigator: it needs to bound
    # "next" at the end of the actual PDF, not just at the end of whichever
    # section's Source breadcrumb happens to be open.
    total_pages = None
    stage1_report = Path(j["root"]) / "01_stage1_extract" / "stage1_report.json"
    if stage1_report.exists():
        try:
            total_pages = json.loads(stage1_report.read_text()).get("pages")
        except (OSError, json.JSONDecodeError):
            pass
    return {"tree": tree, "files": files, "total_pages": total_pages,
            "stage": stage, "stages_available": available}


@app.get("/api/jobs/{job_id}/stage4.json")
def job_stage4(job_id: str):
    """What Stage 4 did, and what the conservation checks make of it.

    The checks are run HERE rather than read from a stored file: they compare the stage-3
    and stage-4 trees as they are on disk right now, so re-running Stage 4 and hitting
    refresh shows the new verdict with no separate step to remember.
    """
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    from check_stage4 import stage4_dashboard
    return stage4_dashboard(Path(j["root"]))


@app.get("/api/jobs/{job_id}/viewer.html", response_class=HTMLResponse)
def job_viewer(job_id: str):
    """The same self-contained MD/PDF viewer a finished corpus run publishes to S3
    (scripts/corpus_worker.py's build_review_artefacts -> push_hybrid_s3.build_viewer),
    built fresh from whatever is on disk right now instead of read from a stored copy --
    there is no upload step here, so a job that was never pushed still gets one, and one
    re-run through stage 4 shows the tree as it stands NOW, not as it stood when a run
    last uploaded a copy.
    """
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    root = Path(j["root"])
    pdf = root / "source.pdf"
    if not pdf.exists():
        pdf = root / "source_repaired.pdf"
    # Same stage set corpus_worker.py builds from -- deliberately excludes stage 1, which
    # is this tool's own extra inspection stage and not one the production viewer offers.
    stages = {n: root / d for n, d in STAGE_DIRS.items()
             if n != 1 and (root / d).is_dir()}
    if not stages or not pdf.exists():
        raise HTTPException(404, "no extracted tree or source PDF for this job")
    sys.path.insert(0, str(HERE))
    import push_hybrid_s3 as PH
    sub = ("summary AI pipeline — direct Bedrock transcription, no stage 1-3"
          if (root / "summary_ai_rule.json").exists()
          else "extraction run — pdf2mdtree + MinerU")
    if len(stages) > 1:
        sub += f" · stages {'/'.join(str(n) for n in sorted(stages))}"
    title = f"{j['product']} — {j['jurisdiction']}"
    html = PH.build_viewer(stages[max(stages)], pdf, title, sub,
                           stages=stages if len(stages) > 1 else None)
    return HTMLResponse(html)


@app.get("/api/jobs/{job_id}/tables_detail.json")
def tables_detail(job_id: str):
    """Per-table 'what went into MinerU vs what came out' — the source page
    snapshot(s) alongside the raw HTML MinerU returned and the markdown it was
    converted to, so a failure or a bad extraction can be judged against the
    actual source page rather than taken on faith."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j["status"] != "done":
        raise HTTPException(409, f"job not finished yet (status={j['status']})")
    root = Path(j["root"])
    out = []
    for t in j["stage2_report"]["tables"]:
        d = dict(t)
        if t.get("ok"):
            tdir = root / "02_stage2_mineru_tables" / "tables" / t["table_id"]
            md_p, html_p = tdir / "table.md", tdir / "table.html"
            d["table_md"] = md_p.read_text() if md_p.exists() else ""
            d["table_html"] = html_p.read_text() if html_p.exists() else ""
        d["asset_urls"] = [f"/api/jobs/{job_id}/assets/page-{p:03d}.png" for p in t["pages"]]
        out.append(d)
    return {"tables": out}


@app.get("/api/jobs/{job_id}/validation.json")
def job_validation(job_id: str, view: str = "extraction"):
    """`view` mirrors the scorecard endpoint's, for the same reason page_issues does.

    The Validation tab always served validation.json — the stage-3 gate — while the
    Scorecard tab beside it defaulted to Scorecard 2. So the per-check numbers a reader
    was reading described a different tree from the verdict above them, and a check whose
    answer depends on the stage (outline_coverage asks whether the SHIPPED tree still
    carries every outline heading) reported the pre-AI answer under a post-AI heading.
    """
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j["status"] != "done":
        raise HTTPException(409, f"job not finished yet (status={j['status']})")
    if view == "post_ai" and j.get("root"):
        p = Path(j["root"]) / "validation_post_ai.json"
        if p.exists():
            try:
                out = json.loads(p.read_text())
                out["view"] = "post_ai"
                return out
            except (OSError, ValueError):
                pass            # fall through to the extraction one rather than 500
    out = dict(j.get("validation") or {})
    out["view"] = "extraction"
    return out


@app.get("/api/jobs/{job_id}/page_issues.json")
def page_issues(job_id: str, view: str = "extraction"):
    """`view` mirrors the scorecard endpoint's, so the page list always describes the
    same document as the scorecard on screen — and the same tree, since Scorecard 2
    reads stage 5 whose files stage 3's names no longer match."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j["status"] != "done":
        raise HTTPException(409, f"job not finished yet (status={j['status']})")
    return _build_page_issues(j, view=view)


@app.get("/api/jobs/{job_id}/page_image/{page_no}")
def page_image(job_id: str, page_no: int):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    pdf_path = Path(j["root"]) / "source.pdf"
    doc = he.fitz.open(str(pdf_path))
    try:
        if page_no < 1 or page_no > doc.page_count:
            raise HTTPException(404, "page out of range")
        pix = doc[page_no - 1].get_pixmap(matrix=he.fitz.Matrix(1.6, 1.6))
        png_bytes = pix.tobytes("png")
    finally:
        doc.close()
    return Response(content=png_bytes, media_type="image/png")


_GEO_COLOR = {"confident": (0.24, 0.86, 0.46), "uncertain": (0.94, 0.66, 0.24)}
_PDF2MD_BLUE = (0.31, 0.56, 0.97)
_MISSING_RED = (1.0, 0.42, 0.42)


@app.get("/api/jobs/{job_id}/page_bbox_image/{page_no}")
def page_bbox_image(job_id: str, page_no: int):
    """Same page render as page_image, with Rule B's geometry drawn on top:
    blue = pdf2mdtree's detected table placeholder bbox; green/amber = the
    MinerU block(s) matched to it (green = confident IoU match, amber =
    uncertain — low overlap or a possible split/merge); red = no MinerU block
    overlapped the placeholder at all (geometrically MISSING)."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    pdf_path = Path(j["root"]) / "source.pdf"
    tables = (j.get("stage2_report") or {}).get("tables", [])
    doc = he.fitz.open(str(pdf_path))
    try:
        if page_no < 1 or page_no > doc.page_count:
            raise HTTPException(404, "page out of range")
        page = doc[page_no - 1]
        for t in tables:
            if page_no not in t.get("pages", []) or not t.get("bbox"):
                continue
            page.draw_rect(he.fitz.Rect(*t["bbox"]), color=_PDF2MD_BLUE, width=1.6)
            status = t.get("match_status")
            # A MinerU bbox is only meaningful on the page it was measured on.
            # A cross-page recovery (a merged page-spanning table whose rows sit
            # on a neighbour) stores that neighbour's box here, and drawing it on
            # THIS page put a dashed rectangle over unrelated content — the
            # "uncertain match" box that visibly didn't line up with anything.
            # mineru_bbox_pages is written alongside; when it's absent (output
            # from before that existed) fall back to the old behaviour.
            _bb_pages = t.get("mineru_bbox_pages") or []
            for _i, mb in enumerate(t.get("mineru_bboxes") or []):
                if not mb:
                    continue
                if _i < len(_bb_pages) and _bb_pages[_i] not in (None, page_no):
                    continue
                page.draw_rect(he.fitz.Rect(*mb), color=_GEO_COLOR.get(status, _MISSING_RED),
                               width=1.6, dashes="[3 2] 0")
            if status == "missing":
                page.draw_rect(he.fitz.Rect(*t["bbox"]), color=_MISSING_RED, width=2.2)
        pix = page.get_pixmap(matrix=he.fitz.Matrix(1.6, 1.6))
        png_bytes = pix.tobytes("png")
    finally:
        doc.close()
    return Response(content=png_bytes, media_type="image/png")


# Metrics compared across a product's jurisdictions. Each is (key, label, and
# whether a HIGH value is the suspicious direction) — used only for wording, the
# deviation itself is symmetric.
_COHORT_METRICS = (
    ("chunks", "chunks"),
    ("mean_chunk_words", "mean words per chunk"),
    ("median_chunk_words", "median words per chunk"),
    ("max_chunk_words", "largest chunk"),
    ("concentration_pct", "words in largest chunk (%)"),
    ("chunks_per_page", "chunks per page"),
    ("words_per_page", "words per page"),
    ("headings_total", "headings"),
    ("max_depth", "nesting depth"),
    ("tiny_chunks", "tiny chunks"),
)


def _cohort_for(job_id: str) -> dict:
    """This document's structural profile against its PRODUCT's other
    jurisdictions.

    Computed here, at request time, rather than stored in scorecard.json: a
    cohort statistic is a property of the GROUP, so freezing it into one
    member's file makes it wrong for every sibling re-extracted afterwards —
    silently, with nothing on screen to say so. Recomputing costs a dict lookup
    over jobs already in memory.

    Uses MEDIAN and MAD (median absolute deviation), not mean and stdev.
    Cohorts here are small — most products hold three jurisdictions — and the
    thing being looked for is one broken document; a mean is dragged toward that
    document and hides exactly what it should expose. Measured on this corpus:
    108_netalytics+ holds a 46k-word single-chunk document, and the cohort mean
    of ~15k describes none of its three members."""
    me = JOBS.get(job_id) or {}
    product = me.get("product")
    my_prof = ((me.get("scorecard") or {}).get("structure")) or {}
    if not product or not my_prof.get("chunks"):
        return {"available": False, "reason": "no structure profile for this document"}

    peers = []
    for jid, j in JOBS.items():
        if j.get("product") != product or j.get("status") != "done":
            continue
        prof = ((j.get("scorecard") or {}).get("structure")) or {}
        if prof.get("chunks"):
            peers.append({"job_id": jid, "jurisdiction": j.get("jurisdiction") or j.get("id"),
                          "doc_id": j.get("doc_id") or (j.get("id") or "")[-6:],
                          "is_self": jid == job_id, "profile": prof})
    if len(peers) < 2:
        return {"available": False, "product": product, "cohort_size": len(peers),
                "reason": "need at least 2 extracted jurisdictions in this product to compare"}

    metrics = []
    for key, label in _COHORT_METRICS:
        vals = [p["profile"].get(key) for p in peers]
        vals = [v for v in vals if isinstance(v, (int, float))]
        mine = my_prof.get(key)
        if len(vals) < 2 or not isinstance(mine, (int, float)):
            continue
        med = statistics.median(vals)
        # MAD scaled to be comparable to a standard deviation for a normal
        # distribution, so the z-like number reads on a familiar scale.
        mad = statistics.median([abs(v - med) for v in vals]) * 1.4826
        ratio = (mine / med) if med else None
        z = ((mine - med) / mad) if mad else None
        metrics.append({
            "key": key, "label": label, "value": mine,
            "median": round(med, 2), "ratio": (round(ratio, 2) if ratio is not None else None),
            "z": (round(z, 1) if z is not None else None),
            "peers": sorted(vals),
        })

    # An outlier is called on RATIO, not on z: with three documents the MAD is
    # frequently 0 (two identical siblings), which makes z infinite or undefined
    # and would flag on noise. A 2x deviation from the cohort median is a plain,
    # explainable bar that behaves sensibly at n=3.
    outliers = [m for m in metrics
                if m["ratio"] is not None and (m["ratio"] >= 2.0 or m["ratio"] <= 0.5)
                and m["key"] in ("mean_chunk_words", "median_chunk_words",
                                 "chunks_per_page", "words_per_page", "concentration_pct")]
    return {
        "available": True, "product": product, "cohort_size": len(peers),
        "metrics": metrics, "outlier_metrics": outliers,
        "is_outlier": bool(outliers),
        # Small cohorts cannot support a strong claim; the UI says so rather than
        # implying a rigour the sample size does not have.
        "confidence": "low" if len(peers) < 4 else ("medium" if len(peers) < 8 else "good"),
        # A jurisdiction name is NOT unique inside a product — 155_Data_Privacy
        # holds two Angola documents and two Argentina ones, so a bare
        # jurisdiction label makes a chart where the same name appears twice with
        # different bars and no way to tell which is which. Carry the doc_id and
        # let the UI disambiguate only when it has to.
        "siblings": sorted(
            [{"jurisdiction": p["jurisdiction"], "doc_id": p["doc_id"],
              "is_self": p["is_self"],
              "chunks": p["profile"].get("chunks"),
              "mean_chunk_words": p["profile"].get("mean_chunk_words"),
              "concentration_pct": p["profile"].get("concentration_pct")} for p in peers],
            key=lambda x: -(x["mean_chunk_words"] or 0)),
    }


def _fresh_scorecard(j: dict, view: str = "extraction") -> dict | None:
    """This job's scorecard, re-read from disk when the file is newer than the copy
    held in memory.

    The corpus overview reads each job's saved scorecard.json; this endpoint used to
    return only the in-memory copy captured when the server registered the corpus. A
    document re-extracted while the server was up therefore showed its NEW verdict in
    the corpus table and its OLD one on its own page — the two views disagreeing about
    the same document, which is worse than either being wrong on its own. Comparing
    mtimes keeps the in-memory cache (page loads stay cheap) without letting it go
    stale behind a re-run."""
    # Per-view cache keys. The post-AI scorecard is a different file with the same
    # shape, so sharing one cache slot would serve whichever view was asked for first
    # under both names — the exact confusion these two files exist to prevent.
    ck, mk = ("scorecard", "_sc_mtime") if view == "extraction" else ("scorecard_post_ai",
                                                                     "_sc_post_mtime")
    fname = "scorecard.json" if view == "extraction" else "scorecard_post_ai.json"
    sc = j.get(ck)
    root = j.get("root")
    if not root:
        return sc
    p = Path(root) / fname
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return sc
    if j.get(mk) == mtime and sc is not None:
        return sc
    try:
        sc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return j.get(ck)
    j[ck], j[mk] = sc, mtime
    return sc


@app.get("/api/jobs/{job_id}/scorecard.json")
def job_scorecard(job_id: str, view: str = "extraction"):
    """The at-a-glance verdict: five scored dimensions, a gate set by the WORST
    of them, per-page health states for the heat-map, and per-table outcomes —
    plus the helper text explaining each number (see check_scorecard.HELP).

    `cohort` is attached here rather than baked into the stored scorecard
    because it describes this document's PRODUCT, not this document — see
    _cohort_for.

    `view` picks WHICH TREE the scores describe:
      "extraction" (default)  scorecard.json      — stages 1-3, the gate
      "post_ai"               scorecard_post_ai.json — the stage 4/5 tree

    Two files, not one, because the extraction gate must stay exactly what it was: it
    is the --resume completion marker, its timing is extraction cost, and a paid pass
    that fails must not un-complete a document that extracted cleanly. `views` tells
    the page which of them exist, so the toggle appears only when there is a second
    one to toggle to."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j["status"] != "done":
        raise HTTPException(409, f"job not finished yet (status={j['status']})")
    views = ["extraction"]
    if (Path(j["root"]) / "scorecard_post_ai.json").exists():
        views.append("post_ai")
    if view not in views:
        view = "extraction"
    sc = (_fresh_scorecard(j, view=view)
          or {"gate": "unknown", "error": "scorecard not computed"})
    return {**sc, "cohort": _cohort_for(job_id), "view": view, "views": views}


@app.get("/api/jobs/{job_id}/stage_diff.json")
def job_stage_diff(job_id: str, from_stage: int = 3):
    """Recovered/regressed text, heading coverage against the source outline, and
    table row counts against MinerU's own count -- between `from_stage` (default
    3, the extraction tree) and whatever later stage is actually on disk.

    A CROSS-CHECK, not a dimension: it never writes a score or touches the
    gate. It exists because none of the scored dimensions can tell "recovered"
    from "regressed" -- completeness only ever reports what's missing NOW, not
    what changed relative to an earlier stage -- and because table_id is not a
    stable key across a tree (see compare_stage_completeness.py's module
    docstring), so a naive by-id diff can misattribute one table's rows to
    another. `available: false` when only `from_stage` exists on disk, which is
    most documents (stage 4/5 are opt-in)."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    root = Path(j["root"])
    to_stage = final_stage(root)
    if to_stage == from_stage:
        return {"available": False, "reason": f"only stage {to_stage} is on disk"}
    return {
        "available": True, "from_stage": from_stage, "to_stage": to_stage,
        "text": text_diff(root, from_stage, to_stage),
        "headings": heading_coverage(root, from_stage, to_stage),
        "tables": table_row_completeness(root, from_stage, to_stage),
    }


def _recompute_scorecard(j: dict, view: str = "extraction") -> dict:
    """Re-score from the CACHED validation for the given view — cheap (no PDF
    re-read, no MinerU), so dismissing a finding updates the verdict immediately.

    Two views, two caches: Scorecard 1 rescoring from `j["validation"]` (the
    stage-3 validation this dashboard already ran and holds in memory) never
    touched Scorecard 2 at all, because both wrote to the same `j["scorecard"]`
    slot `job_scorecard` never reads when `view=post_ai`. Dismissing a finding
    while looking at Scorecard 2 (the default view) silently rescored the
    OTHER scorecard instead — the one on screen never changed. See
    `job_validation`'s `view` handling, which this mirrors.

    Scorecard 2 is also written back to scorecard_post_ai.json, the same file
    and format run_corpus._score_post_ai saves — a dismissal made here must
    survive a server restart and show up in the corpus overview, which reads
    that file straight off disk (_corpus_jobs, by design, never recomputes).
    Scorecard 1 is NOT written back to scorecard.json: that file is the
    --resume completion marker and its `timing` is extraction cost, so it must
    stay exactly what extraction left — see _score_post_ai's own docstring for
    why a second file exists at all rather than a rewrite of the first."""
    root = Path(j["root"])
    if view == "post_ai":
        p = root / "validation_post_ai.json"
        if p.exists():
            try:
                validation_post_ai = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                validation_post_ai = None
            j["scorecard_post_ai"] = compute_scorecard(root, validation_post_ai)
            try:
                (root / "scorecard_post_ai.json").write_text(
                    json.dumps(j["scorecard_post_ai"], indent=2))
            except OSError:
                pass  # in-memory copy is still correct; disk write is best-effort
            return j["scorecard_post_ai"]
        # No post-AI tree to score against -- fall back to extraction below.
    j["scorecard"] = compute_scorecard(root, j.get("validation"))
    return j["scorecard"]


@app.post("/api/jobs/{job_id}/dismiss")
def dismiss_finding(job_id: str, body: dict = Body(...)):
    """Mark one finding as a false positive. It leaves the active score and the
    heat-map but is still returned (flagged `dismissed`) and can be restored —
    dismissals are recorded, never deleted. Keyed by source-PDF hash, so the
    dismissal survives re-running the same document as a new job."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    key = (body or {}).get("key")
    if not key:
        raise HTTPException(400, "missing 'key'")
    root = Path(j["root"])
    dkey = dis.doc_key(root / "source.pdf")
    dis.dismiss(root, dkey, key, label=(body or {}).get("label", ""),
                reason=(body or {}).get("reason", ""))
    return _recompute_scorecard(j, view=(body or {}).get("view", "extraction"))


@app.get("/api/jobs/{job_id}/review")
def get_review(job_id: str):
    """This document's human verdict + notes, if any."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    root = Path(j["root"])
    return rev.for_doc(root, dis.doc_key(root / "source.pdf"))


@app.post("/api/jobs/{job_id}/review/override")
def set_review_override(job_id: str, body: dict = Body(...)):
    """Set or clear a reviewer's verdict. The computed gate is never overwritten —
    both are returned so the UI shows the disagreement rather than hiding it."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    root = Path(j["root"])
    dkey = dis.doc_key(root / "source.pdf")
    computed = ((j.get("scorecard") or {}).get("gate")) or ""
    if (body or {}).get("clear"):
        rev.clear_override(root, dkey, author=(body or {}).get("author", ""))
    else:
        try:
            rev.set_override(root, dkey, (body or {}).get("verdict", ""),
                             (body or {}).get("reason", ""),
                             author=(body or {}).get("author", ""),
                             computed_gate=computed)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {"review": rev.for_doc(root, dkey), "computed_gate": computed}


@app.post("/api/jobs/{job_id}/review/note")
def add_review_note(job_id: str, body: dict = Body(...)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    root = Path(j["root"])
    dkey = dis.doc_key(root / "source.pdf")
    if (body or {}).get("delete_id"):
        rev.delete_note(root, dkey, body["delete_id"])
    else:
        try:
            rev.add_note(root, dkey, (body or {}).get("text", ""),
                         author=(body or {}).get("author", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
    return rev.for_doc(root, dkey)


@app.post("/api/jobs/{job_id}/restore")
def restore_finding(job_id: str, body: dict = Body(...)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    key = (body or {}).get("key")
    if not key:
        raise HTTPException(400, "missing 'key'")
    root = Path(j["root"])
    dis.restore(root, dis.doc_key(root / "source.pdf"), key)
    return _recompute_scorecard(j, view=(body or {}).get("view", "extraction"))


@app.get("/api/jobs/{job_id}/geometry.json")
def job_geometry(job_id: str):
    """Per-table Rule B geometry summary for the dashboard: bbox, matched
    MinerU bbox(es), IoU, and match status — everything already computed in
    Stage 2 (see hybrid_extract.run_stage2's geo_match), just surfaced here."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j["status"] != "done":
        raise HTTPException(409, f"job not finished yet (status={j['status']})")
    tables = (j.get("stage2_report") or {}).get("tables", [])
    out = [{"table_id": t["table_id"], "pages": t["pages"], "bbox": t.get("bbox"),
            "mineru_bboxes": t.get("mineru_bboxes"), "match_method": t.get("match_method"),
            "match_status": t.get("match_status"), "match_iou": t.get("match_iou"),
            "match_note": t.get("match_note"), "ok": t.get("ok")} for t in tables]
    placement = ((j.get("validation") or {}).get("table_placement")) or {}
    return {"tables": out, "placement_flags": placement.get("flags", [])}


@app.get("/api/jobs/{job_id}/assets/{name}")
def job_asset(job_id: str, name: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    p = Path(j["root"]) / "01_stage1_extract" / "_assets" / name
    if ".." in name or not p.exists():
        raise HTTPException(404, "unknown asset")
    return FileResponse(p)


# ---------------- pages ----------------
INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>hybrid_extract lab</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0f1117; color:#e6e6e6; font-family:-apple-system,Segoe UI,sans-serif;
         max-width:900px; margin:40px auto; padding:0 20px; }
  h1 { font-size:1.4rem; }
  .sub { color:#9aa0ac; margin-top:-8px; }
  .card { background:#181b24; border:1px solid #2a2f3a; border-radius:10px; padding:20px; margin:20px 0; }
  #drop { border:2px dashed #3a4150; border-radius:10px; padding:30px; text-align:center; cursor:pointer; }
  #drop.hover { border-color:#4f8ff7; background:#141824; }
  button { background:#4f8ff7; color:#fff; border:none; padding:8px 16px; border-radius:6px;
           cursor:pointer; font-size:0.95rem; }
  button:disabled { opacity:0.5; cursor:default; }
  table { width:100%; border-collapse:collapse; margin-top:10px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid #262b36; font-size:0.9rem; }
  th { color:#9aa0ac; font-weight:600; }
  tr:hover td { background:#141824; }
  a { color:#4f8ff7; text-decoration:none; }
  .badge { display:inline-block; padding:2px 8px; border-radius:20px; font-size:0.75rem; font-weight:600; }
  .b-queued, .b-stage1, .b-stage2, .b-stage3, .b-validate { background:#3a3410; color:#f0c33c; }
  .b-done { background:#123a1f; color:#3ddc76; }
  .b-error { background:#3a1414; color:#ff6b6b; }
  .v-ok { color:#3ddc76; }
  .v-bad { color:#ff6b6b; }
  .g-pass { background:#123a1f; color:#3ddc76; }
  .g-review { background:#3a3410; color:#f0a83c; }
  .g-fail { background:#3a1414; color:#ff6b6b; }
  .g-unknown { background:#1d2230; color:#7d8494; }
  .opt-row { display:flex; gap:20px; margin-top:14px; flex-wrap:wrap; }
  .opt-field { display:flex; flex-direction:column; gap:4px; }
  .opt-field label { font-size:0.78rem; color:#9aa0ac; text-transform:uppercase; letter-spacing:0.04em; }
  .opt-field select { background:#141721; color:#e6e6e6; border:1px solid #2a2f3a; border-radius:6px;
                      padding:6px 10px; font-size:0.9rem; }
  .opt-hint { font-size:0.76rem; color:#7d8494; margin-top:2px; max-width:320px; }
  .model-tag { font-size:0.72rem; color:#7d8494; }
.stage-strip{display:inline-flex;gap:3px}
.sc{font-size:.62rem;padding:1px 5px;border-radius:3px;border:1px solid #333;color:#888}
.sc-done{background:#14361f;border-color:#1f5c33;color:#5fd68a}
.sc-now{background:#3a2f10;border-color:#7a6318;color:#ffd257;animation:scpulse 1.4s ease-in-out infinite}
.sc-todo{opacity:.45}
@keyframes scpulse{0%,100%{opacity:1}50%{opacity:.45}}
@media (prefers-reduced-motion:reduce){.sc-now{animation:none}}
</style></head>
<body>
<h1>hybrid_extract lab</h1>
<p class="sub">Stage 1 (extract) &rarr; Stage 2 (MinerU tables) &rarr; Stage 3 (combine) &rarr; validate (source vs. extracted). Local only, no S3.</p>
<div class="card">
  <div id="drop">Drop a PDF here or click to choose one<br><input id="file" type="file" accept=".pdf" style="display:none"></div>
  <div class="opt-row">
    <div class="opt-field">
      <label for="backend">MinerU backend</label>
      <select id="backend">
        <option value="hybrid-engine" selected>hybrid-engine (default — rules + MinerU table model)</option>
        <option value="vlm-engine">vlm-engine (pure vision-language model)</option>
        <option value="pipeline">pipeline (classic OCR/layout pipeline)</option>
      </select>
    </div>
    <div class="opt-field">
      <label for="effort">Effort</label>
      <select id="effort">
        <option value="medium" selected>medium (default)</option>
        <option value="high">high (slower, hybrid-engine only)</option>
      </select>
      <div class="opt-hint">Effort only affects hybrid-engine — the other backends ignore it.</div>
    </div>
  </div>
  <div style="margin-top:12px; text-align:right;"><button id="go" disabled>Run pipeline</button></div>
</div>
<div class="card">
  <div style="display:flex; align-items:center; gap:12px; margin-bottom:4px;">
    <h3 style="margin:0">Jobs</h3>
    <button id="rescan" style="background:#181b24;color:#9aa0ac;border:1px solid #262b36;
            padding:5px 12px;border-radius:6px;font-size:0.8rem;"
            title="Pick up extractions finished on disk since the server started (e.g. a baseline run)">
      Rescan disk</button>
    <span id="rescan-note" class="model-tag"></span>
  </div>
  <table id="jobs"><thead><tr><th>File</th><th>Model</th><th>Status</th><th>Verdict</th><th>Tables</th><th>Validation</th><th></th></tr></thead><tbody></tbody></table>
</div>
<script>
const drop = document.getElementById('drop'), fileInput = document.getElementById('file'), go = document.getElementById('go');
const backendSel = document.getElementById('backend'), effortSel = document.getElementById('effort');
let chosen = null;
drop.onclick = () => fileInput.click();
fileInput.onchange = () => { chosen = fileInput.files[0]; drop.textContent = chosen ? chosen.name : 'Drop a PDF here'; go.disabled = !chosen; };
['dragover','dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.toggle('hover', ev==='dragover'); }));
drop.addEventListener('drop', e => { chosen = e.dataTransfer.files[0]; drop.textContent = chosen.name; go.disabled = !chosen; });

go.onclick = async () => {
  go.disabled = true; go.textContent = 'Uploading...';
  const fd = new FormData();
  fd.append('file', chosen);
  fd.append('mineru_backend', backendSel.value);
  fd.append('mineru_effort', effortSel.value);
  const r = await fetch('/api/upload', { method: 'POST', body: fd });
  go.textContent = 'Run pipeline';
  if (!r.ok) { alert('upload failed: ' + await r.text()); return; }
  chosen = null; fileInput.value = ''; drop.textContent = 'Drop a PDF here or click to choose one';
  refresh();
};

const STATUS_LABEL = { queued:'queued', stage1:'Stage 1: extract', stage2:'Stage 2: MinerU', stage3:'Stage 3: combine', validate:'Validating', done:'done', error:'error' };
async function refresh() {
  const r = await fetch('/api/jobs'); const { jobs } = await r.json();
  const tbody = document.querySelector('#jobs tbody'); tbody.innerHTML = '';
  for (const j of jobs) {
    const tr = document.createElement('tr');
    const tables = j.tables_total == null ? '-' : `${j.tables_ok ?? 0} ok / ${j.tables_failed ?? 0} failed / ${j.tables_total} total`;
    let validation = '-';
    if (j.validation_passed != null) {
      const cls = j.validation_passed ? 'v-ok' : 'v-bad';
      validation = `<span class="${cls}">${j.coverage_pct?.toFixed(1)}% cov, ${j.silent_gaps} gap(s), ${j.transpositions} transp.</span>`;
    }
    const model = `<span class="model-tag">${j.mineru_backend || '-'}${j.mineru_effort ? ' / ' + j.mineru_effort : ''}</span>`;
    const verdict = j.gate
      ? `<span class="badge g-${j.gate}" title="Gate = the worst of five dimensions${j.weakest_dimension ? ', here: ' + j.weakest_dimension : ''}">${j.gate.toUpperCase()}${j.worst_score != null ? ' ' + j.worst_score : ''}</span>`
      : '-';
    const link = j.status === 'done' ? `<a href="/jobs/${j.id}">view &rarr;</a>` : (j.status === 'error' ? j.error : '');
    tr.innerHTML = `<td>${j.filename}</td><td>${model}</td><td><span class="badge b-${j.status}">${STATUS_LABEL[j.status] || j.status}</span></td><td>${verdict}</td><td>${tables}</td><td>${validation}</td><td>${link}</td>`;
    tbody.appendChild(tr);
  }
  if (jobs.some(j => !['done','error'].includes(j.status))) setTimeout(refresh, 1500);
}
document.getElementById('rescan').onclick = async () => {
  const note = document.getElementById('rescan-note');
  note.textContent = 'scanning…';
  const r = await fetch('/api/rescan', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
  const d = await r.json();
  note.textContent = d.count ? `picked up ${d.count} — scoring in background` : 'nothing new on disk';
  refresh();
};
refresh();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    # In corpus mode the folder tree IS the landing page — the upload-driven job
    # list is not what this server is for.
    if CORPUS_ROOT:
        return CORPUS_HTML
    return INDEX_HTML


CORPUS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>corpus — extraction scorecard</title>
<style>
 :root{color-scheme:dark}
 body{background:#0d1017;color:#c9d1d9;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:28px 32px}
 h1{font-size:19px;margin:0 0 4px;font-weight:600}
 .sub{color:#7d8494;font-size:12.5px;margin-bottom:20px}
 .bar{display:flex;gap:18px;flex-wrap:wrap;background:#131722;border:1px solid #1f2733;border-radius:8px;padding:12px 16px;margin-bottom:8px}
 .bar b{font-size:17px;font-weight:600}
 .kv{display:flex;flex-direction:column}
 .kv span{color:#7d8494;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
 .run{background:#1a2030;border:1px solid #2a3550;border-radius:8px;padding:9px 14px;margin-bottom:18px;font-size:12.5px;color:#8fb0e0}
 .run-fallback{background:#241b3a;border-color:#5b3fa0;animation:fallback-pulse 1.6s ease-in-out infinite}
 @keyframes fallback-pulse{0%,100%{box-shadow:0 0 0 0 rgba(180,140,242,.55)}50%{box-shadow:0 0 0 5px rgba(180,140,242,0)}}
 .stage-tag{background:#0d1017;border:1px solid #2a3346;border-radius:5px;padding:2px 8px;font-size:11.5px;color:#8b93a1}
 .stage-tag-fallback{background:#2a1f47;border-color:#7a5cc4;color:#d8c6ff;font-weight:600}
 table{width:100%;border-collapse:collapse}
 th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:#7d8494;padding:7px 10px;border-bottom:1px solid #1f2733;font-weight:600}
 td{padding:8px 10px;border-bottom:1px solid #161c26;vertical-align:middle}
 tr.folder{cursor:pointer}
 tr.folder:hover td{background:#141a24}
 tr.juris{cursor:pointer}
 tr.juris td{background:#111620;font-size:13px}
 tr.juris:hover td{background:#161d2a}
 tr.juris td:first-child{padding-left:22px;color:#c9d1d9}
 tr.doc td{background:#0f131b;font-size:13px}
 tr.doc td:first-child{padding-left:52px;color:#8b93a1}
 .pill{display:inline-block;padding:1px 9px;border-radius:11px;font-size:11px;font-weight:600;letter-spacing:.3px}
 .p-pass{background:#123a1f;color:#3ddc76}
 .p-review{background:#2a2210;color:#f0a83c}
 .p-fail{background:#3a1418;color:#ff8080}
 .p-pending{background:#1b2436;color:#8fb0e0}
 .p-mineru-fallback{background:#241b3a;color:#b48cf2}
 /* TOC quality. Every badge carries its own word, so colour is redundant. */
 .rt{display:inline-block;padding:1px 7px;border-radius:4px;font-size:10.5px;
     font-weight:600;letter-spacing:.2px;white-space:nowrap;cursor:help}
 .rt-direct{background:#1b2029;color:#7d8494}
 .rt-toc{background:#111c2c;color:#63b3ff}
 .rt-mineru{background:#231129;color:#e879f9}
 .rt-kept{background:#2a2210;color:#f5b942}
 .rt-short{background:#241d12;color:#f5b942;border:1px solid #6b5320}
 .rtmix{font-size:10.5px;color:#8b93a1;cursor:help}
 .toc{display:inline-block;padding:1px 7px;border-radius:4px;font-size:10.5px;
      font-weight:600;letter-spacing:.2px;white-space:nowrap;cursor:help}
 .toc-proper{background:#123a1f;color:#35c46a}
 .toc-lacking{background:#2a2210;color:#f5b942}
 .toc-improper{background:#3a1418;color:#ff5c5c}
 .toc-none{background:#1e2129;color:#5c6470}
 .tocmix{font-size:10.5px;color:#8b93a1;cursor:help}
 .cnt{font-variant-numeric:tabular-nums;color:#8b93a1;font-size:12.5px}
 .score{font-variant-numeric:tabular-nums;font-weight:600}
 .caret{display:inline-block;width:12px;color:#5c6470}
 a{color:#8fb0e0;text-decoration:none}
 a:hover{text-decoration:underline}
 .muted{color:#5c6470}
</style></head><body>
<h1>Corpus extraction scorecard</h1>
<div class="sub">One row per product folder. A folder's verdict is its <b>weakest</b> document &mdash;
 an average would hide the one document you need to look at. Click a folder to see its documents.</div>
<style>
td.took { font-variant-numeric: tabular-nums; white-space: nowrap; }
td.took b { font-weight: 600; }
.took-fb { font-size: 10.5px; color: #d9a441; }
.took-rate { font-size: 10.5px; color: #6b7280; }
</style>
<div class="bar" id="totals"></div>
<div class="run" id="running" style="display:none"></div>
<table><thead><tr>
  <th style="width:38%">product / jurisdiction / document</th><th>verdict</th><th>pass</th><th>review</th><th>fail</th>
  <th>worst</th><th>mean</th><th title="Tables MinerU extracted that no section claimed. lost = the text is NOT in the tree (a defect); rec = placed into the nearest region in its OWN section; dup = the text is already in the tree, so adopting would duplicate">orphans</th><th title="How this document got here: straight through the normal pipeline, or rescued by a fallback tier">route</th><th title="Wall clock this document cost to extract, from its scorecard. Folder rows are the SUM of what is underneath, never an average.">took</th><th>tables</th><th>failed</th><th>findings</th><th title="Stage 4 AI post-processing, per section: green if every section came back, red if one timed out, errored, or was never attempted at all — hover a red pill to see which section">AI stage</th>
</tr></thead><tbody id="rows"></tbody></table>
<script>
// Deep-linkable: #<product>||<product>|~|<jurisdiction>||... opens those rows on load,
// so a link to "the AI-stage failures in Belgium" can be shared/screenshotted directly
// rather than describing which rows to click.
const open = new Set(location.hash ? decodeURIComponent(location.hash.slice(1)).split('||').filter(Boolean) : []);
// Quotes MUST be escaped: these values land in HTML attributes, and one raw quote
// ends the attribute early and takes the element's behaviour with it.
// Cost, at a glance. Seconds up to a minute, then m/h — a corpus mixes 57s memos
// with 26-minute clause-table documents and one unit cannot read well for both.
function dur(s){
  if(s === null || s === undefined) return '';
  s = Math.round(s);
  if(s < 60) return s + 's';
  const m = Math.floor(s/60), r = s % 60;
  if(m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  return `${Math.floor(m/60)}h ${m % 60}m`;
}
function tookCell(n){
  if(n.seconds === null || n.seconds === undefined) return '<td class="cnt muted"></td>';
  // Fallback time is called out because it is the half of a rescued document's cost
  // that the total alone hides.
  const fb = n.fallback_seconds
    ? `<div class="took-fb">+${dur(n.fallback_seconds)} fallback</div>` : '';
  const rate = (!n.fallback_seconds && n.seconds_per_page)
    ? `<div class="took-rate">${n.seconds_per_page}s/pp</div>` : '';
  return `<td class="took" title="${escAttr(String(n.timing_pages || '?'))} pages">`
       + `<b>${dur(n.seconds)}</b>${fb}${rate}</td>`;
}
function escAttr(s){
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function pill(v){ return `<span class="pill p-${v}">${v}</span>`; }
// ONE question per column: how did this document get here? A document either came
// straight out of the normal deterministic pipeline, or a fallback tier rescued it.
// (The TOC verdict that TRIGGERED a rescue is detail, and lives in the document's own
// scorecard panel — showing it here as well meant two columns saying overlapping
// things, which is what made the table hard to read.)
const ROUTE = {
  toc_rescue:  ['rt-toc',    '\u27F2 TOC rescue',  "the normal extraction failed, and the outline was rebuilt from this PDF's own printed table of contents"],
  mineru_full: ['rt-mineru', '\u27F2 MinerU',      'the normal extraction failed and the printed contents page could not fix it, so the whole document was re-parsed by MinerU'],
  stage1:      ['rt-kept',   '\u27F2 none kept',   'a fallback ran but no tier scored better, so this is still the ordinary Stage 1 result'],
};
// Which stage a running document is in. Stage 2 shells out to MinerU as one
// subprocess that writes nothing until it exits, so a 20-minute pass is otherwise
// indistinguishable from a hang — this is the only signal there is.
const STAGE_LABEL = { stage1: 'extract', stage2_mineru: 'MinerU tables', stage3: 'combine' };
function stageChips(pr){
  if(!pr || !pr.stage) return '<span class="pill p-pending">running</span>';
  const done = new Set(pr.done || []);
  const chips = (pr.stages || []).map(st => {
    const cls = done.has(st) ? 'sc-done' : (st === pr.stage ? 'sc-now' : 'sc-todo');
    return `<span class="sc ${cls}">${STAGE_LABEL[st] || st}</span>`;
  }).join('');
  const d = pr.detail || {};
  const bits = [];
  if(d.regions != null) bits.push(`${d.regions} region(s)`);
  if(d.pages != null) bits.push(`${d.pages} page(s)`);
  if(d.backend) bits.push(d.backend + (d.effort ? '/' + d.effort : ''));
  const mins = pr.at ? Math.round((Date.now()/1000 - pr.at)/60) : null;
  const since = mins != null ? ` &middot; ${mins}m in ${STAGE_LABEL[pr.stage]||pr.stage}` : '';
  return `<span class="stage-strip" title="${bits.join(', ')}${since.replace(/&middot;/g,'-')}">${chips}</span>`;
}

// Label for the special-rule chip. Keyed on the MODE, not on the page count: the
// "{n}p rule" wording belongs to short_document, where the page count IS the rule
// ("6 pages, under the 8-page bar"). Reusing special_mode for the per-product rule
// made that chip read "88p rule" on an 88-page document — implying a rule about page
// count that does not exist.
const SPECIAL_MODE_CHIP = {
  short_document:             sm => `${sm.pages}p rule`,
  product_rule_flat_sections: sm => 'product rule: flat sections',
};
function specialModeChip(sm){
  const f = SPECIAL_MODE_CHIP[sm.mode];
  return f ? f(sm) : String(sm.mode || 'special rule').replace(/_/g, ' ');
}

// Orphan tables on the TREE row. A table MinerU extracted that no section took is
// content loss and used to be invisible here -- you had to open the job view. Three
// states, because they are three different claims: LOST is a defect; RECOVERED was
// rescued and only needs a glance at which region took it; DUPLICATE is text that is
// already in the tree, where adopting the block would duplicate rather than recover
// (all three orphans left on the MRAM corpus turned out to be this).
function orphanBadge(doc) {
  const o = doc.orphans || {};
  const b = [];
  if (o.lost)      b.push('<span class="pill" style="background:#3a1414;color:#ff6b6b" title="MinerU extracted these; no section took them and their text is NOT in the tree">' + o.lost + ' lost</span>');
  if (o.recovered) b.push('<span class="pill" style="background:#0f2417;color:#5ad18b" title="Unclaimed, but exactly one region held the page, so adopted there">' + o.recovered + ' rec</span>');
  if (o.duplicate) b.push('<span class="pill" style="background:#26262e;color:#8b8f9a" title="Unclaimed, but the text is already in the tree - adopting would duplicate, nothing lost">' + o.duplicate + ' dup</span>');
  return b.join(' ');
}

// WHICH scorecard this row's verdict came from. A document that went through AI
// post-processing is judged on what it now is, so its row reports Scorecard 2 — and
// must say so, because both scorecards have the same shape and an unlabelled 94.6 from
// stage 5 sitting beside an unlabelled 94.6 from stage 3 invites the wrong comparison.
// Stage-3 rows carry no badge: that is the default and labelling every row would be
// noise.
function scStageBadge(doc){
  if(!doc.done || !doc.scored_stage || doc.scored_stage <= 3) return '';
  return ` <span class="pill" style="background:#132033;color:#6fa8dc" title="${escAttr(
    'Scorecard 2 — this document went through AI post-processing, so the verdict '
    + 'shown is the stage ' + doc.scored_stage + ' tree scored against the PDF, not the '
    + 'stage 3 extraction. The extraction gate is still on the document page under '
    + '"Scorecard 1".')}">SC2</span>`;
}

function routeBadge(doc){
  if(!doc.done) return '<span class="muted">-</span>';
  const sm = doc.special_mode;
  const fb = doc.fallback;
  // The special rule comes FIRST, because it changes what the score means — not just
  // how the document got here. A short document is gated on word coverage alone, so
  // its number is not comparable with a full structural score beside it.
  if (sm && sm.mode === 'short_document') {
    const t = `SPECIAL RULE \u2014 ${sm.pages}-page document (bar is ${sm.threshold}).`
      + `\n\nToo short to have a structure, so it goes straight to MinerU and is judged on`
      + ` WORD COVERAGE alone. Gated on: ${(sm.gated_on||[]).join(', ')}.`
      + ((sm.not_gating||[]).length ? ` Shown but NOT gating: ${(sm.not_gating||[]).join(', ')}.` : '')
      + `\n\nThe score is therefore not like-for-like with a full structural score.`;
    return `<span class="rt rt-short" title="${escAttr(t)}">\u26A1 &lt;${sm.threshold}p \u2192 MinerU</span>`;
  }
  if(!fb || !fb.triggered) return '<span class="rt rt-direct" title="Passed first time \u2014 no fallback was needed">direct</span>';
  const m = ROUTE[fb.adopted_tier] || ['rt-kept', '\u27F2 fallback', ''];
  const fa = fb.first_attempt || {};
  let t = m[2];
  if(fa.weakest_dimension) t += `\n\nIt escalated because ${fa.weakest_dimension} scored ${fa.worst_score}.`;
  return `<span class="rt ${m[0]}" title="${escAttr(t)}">${m[1]}</span>`;
}
function groupRoute(node){
  const docs = (node.documents || (node.jurisdictions||[]).flatMap(j => j.documents||[])).filter(d => d.done);
  if(!docs.length) return '';
  const short = docs.filter(d => (d.special_mode || {}).mode === 'short_document');
  const resc = docs.filter(d => d.fallback && d.fallback.triggered && !short.includes(d));
  const shortChip = short.length
    ? ` <span class="rt rt-short" style="padding:0 5px" title="${escAttr(short.length + ' short document(s) \u2014 scored on word coverage alone')}">\u26A1${short.length}</span>` : '';
  if(!resc.length && !short.length) return `<span class="rtmix" title="All ${docs.length} passed first time">${docs.length} direct</span>`;
  return `<span class="rtmix" title="${escAttr(resc.length + ' of ' + docs.length + ' needed a fallback')}">`
       + `<span class="rt rt-direct" style="padding:0 5px">${docs.length - resc.length}</span> `
       + (resc.length ? `<span class="rt rt-toc" style="padding:0 5px">\u27F2 ${resc.length}</span>` : '')
       + shortChip + '</span>';
}
// Stage 4's own per-section health. `null` (never touched Stage 4) stays blank rather than
// reading as either colour \u2014 the same rule orphanBadge follows for a document with no tables.
// `failed_sections` covers both halves of "a section didn't work": ok:false (a timeout, a
// throttle that exhausted its retries, a validation error \u2014 the reason string says which) and
// a section Stage 1 produced that the report never mentions at all (killed mid-run).
function aiStageBadge(doc){
  const s = doc.stage4;
  if(!s) return '<span class="muted">-</span>';
  if(s.status === 'ok') return '<span class="pill p-pass" title="Stage 4: every section came back">ok</span>';
  if(s.status === 'incomplete') return `<span class="pill p-fail" title="${escAttr(s.reason)}">stuck</span>`;
  const files = s.failed_sections.map(x => x.file).join(', ');
  const detail = s.failed_sections.map(x => x.file + ': ' + x.reason).join('\\n');
  return `<span class="pill p-fail" title="${escAttr(detail)}">${s.failed_sections.length} section${s.failed_sections.length===1?'':'s'}: ${files}</span>`;
}
// Folder/jurisdiction rollup: which documents underneath have an AI-stage problem, so a
// row can be flagged before anyone opens a single document to find out.
function groupStage4(node){
  const docs = (node.documents || (node.jurisdictions||[]).flatMap(j => j.documents||[])).filter(d => d.done);
  const bad = docs.filter(d => d.stage4 && d.stage4.status !== 'ok');
  if(!bad.length) return '';
  return `<span class="pill p-fail" title="${escAttr(bad.map(d => d.doc_id).join(', ') + ' had an AI-stage problem')}">${bad.length} AI fail</span>`;
}
function num(v){ return (v===null||v===undefined) ? '<span class="muted">-</span>' : v; }
function score(v){
  if(v===null||v===undefined) return '<span class="muted">-</span>';
  const c = v>=90 ? '#3ddc76' : v>=70 ? '#f0a83c' : '#ff8080';
  return `<span class="score" style="color:${c}">${v.toFixed(1)}</span>`;
}
async function load(){
  const r = await fetch('/api/corpus'); const d = await r.json();
  const t = d.totals;
  document.getElementById('totals').innerHTML = `
    <div class="kv"><span>folders</span><b>${t.folders}</b></div>
    <div class="kv"><span>documents</span><b>${t.documents}</b></div>
    <div class="kv"><span>pass</span><b style="color:#3ddc76">${t.pass}</b></div>
    <div class="kv"><span>review</span><b style="color:#f0a83c">${t.review}</b></div>
    <div class="kv"><span>fail</span><b style="color:#ff8080">${t.fail}</b></div>
    <div class="kv"><span>in progress</span><b style="color:#8fb0e0">${t.running}</b></div>`;
  const p = d.progress || {};
  const rb = document.getElementById('running');
  const STAGE_LABEL = {starting:'starting', stage1:'Stage 1 &middot; extract', stage2:'Stage 2 &middot; MinerU tables',
    stage3:'Stage 3 &middot; combine', validating:'validating (9 checks)',
    toc_rescue:'⟲ COMPLETENESS TOO LOW — rebuilding the outline from the printed contents page',
    mineru_fallback:'⟲ THE PRINTED TOC DID NOT FIX IT — reparsing entire document via MinerU',
    rescoring:'rescoring the MinerU fallback attempt'};
  if(p.state === 'running'){
    rb.style.display='block';
    const isFallback = p.stage === 'toc_rescue' || p.stage === 'mineru_fallback' || p.stage === 'rescoring';
    rb.className = 'run' + (isFallback ? ' run-fallback' : '');
    const stageTag = STAGE_LABEL[p.stage] || p.stage || '';
    rb.innerHTML = `extracting <b>${p.product||''} / ${p.document||''}</b>`
      + (stageTag ? ` &nbsp;&middot;&nbsp; <span class="stage-tag${isFallback?' stage-tag-fallback':''}">${stageTag}</span>` : '')
      + ` &nbsp;&middot;&nbsp; ${p.done||0} of ${p.total||0} documents complete`;
  } else if(p.state === 'finished'){
    rb.className = 'run';
    rb.style.display='block'; rb.innerHTML = `run finished &mdash; ${p.done||0} documents`;
  } else { rb.style.display='none'; }

  let h = '';
  for(const f of d.folders){
    const fOpen = open.has(f.product);
    // Key carried in a data-* attribute, never interpolated into an inline
    // onclick: folder names contain quotes/&/() and JSON.stringify's own double
    // quotes terminate the attribute, silently killing the handler. One delegated
    // listener below reads these instead.
    h += `<tr class="folder" data-key="${escAttr(f.product)}">
      <td><span class="caret">${fOpen?'&#9662;':'&#9656;'}</span><b>${f.product}</b>
          <span class="cnt"> &nbsp;${f.jurisdictions.length} folder${f.jurisdictions.length===1?'':'s'} &middot; ${f.total} doc${f.total===1?'':'s'}</span></td>
      <td>${pill(f.verdict)}</td>
      <td class="cnt">${f.pass}</td><td class="cnt">${f.review}</td><td class="cnt">${f.fail}</td>
      <td>${score(f.worst_score)}</td><td>${score(f.mean_score)}</td><td class="cnt"></td>
      <td>${groupRoute(f)}</td>
      ${tookCell(f)}
      <td class="cnt"></td><td class="cnt"></td><td class="cnt"></td><td>${groupStage4(f)}</td></tr>`;
    if(!fOpen) continue;
    for(const jr of f.jurisdictions){
      // Printable separator, NOT a control character. A \\u0000 here does not
      // survive a round-trip through an HTML attribute — dataset.key comes back
      // mangled, so the key added to `open` never matches on the next render and
      // the row appears to ignore every click.
      const key = f.product + '|~|' + jr.jurisdiction;
      const jOpen = open.has(key);
      h += `<tr class="juris" data-key="${escAttr(key)}">
        <td><span class="caret">${jOpen?'&#9662;':'&#9656;'}</span>${jr.jurisdiction}
            <span class="cnt"> &nbsp;${jr.total} doc${jr.total===1?'':'s'}</span></td>
        <td>${pill(jr.verdict)}</td>
        <td class="cnt">${jr.pass}</td><td class="cnt">${jr.review}</td><td class="cnt">${jr.fail}</td>
        <td>${score(jr.worst_score)}</td><td>${score(jr.mean_score)}</td><td class="cnt"></td>
        <td>${groupRoute(jr)}</td>
        ${tookCell(jr)}
        <td class="cnt"></td><td class="cnt"></td><td class="cnt"></td><td>${groupStage4(jr)}</td></tr>`;
      if(!jOpen) continue;
      for(const doc of jr.documents){
        const g = doc.done ? (doc.gate||'pending') : 'pending';
        // How this document got here is now the `route` column's job (routeBadge),
        // so the pill that used to repeat it beside the doc id is gone: two controls
        // saying overlapping things is what made this table hard to scan. That pill
        // also described the trigger as a completeness score, which stopped being
        // true the moment TOC quality could escalate a document on its own.
        h += `<tr class="doc"><td><a href="/jobs/${doc.job_id}" target="_blank">${doc.doc_id}</a></td>
          <td>${doc.done?pill(g)+scStageBadge(doc):stageChips(doc.progress)}</td>
          <td class="cnt"></td><td class="cnt"></td><td class="cnt"></td>
          <td>${score(doc.worst_score)}</td>
          <td class="cnt muted">${doc.weakest||''}</td>
          <td>${orphanBadge(doc)}</td>
          <td>${routeBadge(doc)}</td>
          ${tookCell(doc)}
          <td class="cnt">${num(doc.tables)}</td>
          <td class="cnt" style="color:${doc.tables_failed?'#ff8080':'#5c6470'}">${num(doc.tables_failed)}</td>
          <td class="cnt">${num(doc.findings)}</td>
          <td>${aiStageBadge(doc)}</td></tr>`;
      }
    }
  }
  document.getElementById('rows').innerHTML = h || '<tr><td colspan="14" class="muted">nothing extracted yet</td></tr>';
}
function tog(p){ open.has(p) ? open.delete(p) : open.add(p); load(); }
// Delegated once on the table body, so rows rebuilt by the 5s refresh keep working
// without rebinding anything. A click on the document link is left alone.
document.getElementById('rows').addEventListener('click', (e) => {
  if (e.target.closest('a')) return;
  const tr = e.target.closest('tr[data-key]');
  if (tr) tog(tr.dataset.key);
});
load(); setInterval(load, 5000);
</script></body></html>
"""


@app.get("/corpus", response_class=HTMLResponse)
def corpus_page():
    return CORPUS_HTML


JOB_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>hybrid_extract — {filename}</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing:border-box; }}
  body {{ background:#0f1117; color:#e6e6e6; font-family:-apple-system,Segoe UI,sans-serif; margin:0; display:flex; height:100vh; }}
  #sidebar {{ width:280px; flex:none; background:#141721; border-right:1px solid #262b36; padding:16px; overflow-y:auto; }}
  #sidebar h3 {{ font-size:0.85rem; color:#9aa0ac; text-transform:uppercase; letter-spacing:0.05em; margin:20px 0 8px; }}
  #sidebar h3:first-child {{ margin-top:0; }}
  #tree a {{ display:flex; align-items:center; gap:5px; padding:4px 6px; border-radius:4px; font-size:0.88rem; color:#c8ccd4; text-decoration:none; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  #tree a:hover {{ background:#1d2230; }}
  #tree a.active {{ background:#20304a; color:#7fb0ff; }}
  /* The folder LABEL sits flush with its sibling files — it is a level-0 entry, the
     same as any section file. Indenting the whole .dir block moved the label in with
     its children, so a folder read as one level deeper than the files beside it.
     Only the children are indented; nesting compounds naturally. */
  #tree .dir {{ margin-left:0; }}
  #tree .dir > a, #tree .dir > .dir {{ margin-left:14px; }}
  #tree .fname {{ overflow:hidden; text-overflow:ellipsis; }}
  #tree .tag {{ flex:none; font-size:0.78rem; }}
  #tree .tag-mineru {{ color:#4f8ff7; }}
  #tree .tag-fail {{ color:#ff6b6b; }}
  #tree .tag-fallback {{ color:#f0a83c; }}
  #tree .tag-gap {{ color:#f0a83c; }}
  #tables-panel div {{ font-size:0.82rem; padding:5px 6px; border-radius:4px; cursor:pointer; margin-bottom:2px; }}
  #tables-panel div:hover {{ background:#1d2230; }}
  .t-ok::before {{ content:"\\2713 "; color:#3ddc76; }}
  .t-fail::before {{ content:"\\2715 "; color:#ff6b6b; }}
  #tables-panel .fallback-note {{ color:#f0a83c; }}
  #main {{ flex:1; overflow-y:auto; padding:32px 48px; }}
  #jobhead {{ display:flex; align-items:center; gap:14px; margin-bottom:14px; }}
  #back {{ display:inline-flex; align-items:center; gap:6px; flex:none; background:#181b24;
            color:#9aa0ac; border:1px solid #2a2f3a; border-radius:7px; padding:6px 13px;
            font-size:0.85rem; text-decoration:none; }}
  #back:hover {{ background:#20304a; color:#7fb0ff; border-color:#2f4a72; }}
  #jobname {{ font-size:0.92rem; color:#c8ccd4; font-weight:600; overflow:hidden;
              text-overflow:ellipsis; white-space:nowrap; }}
  #tabs {{ display:flex; gap:8px; margin-bottom:20px; flex-wrap:wrap; }}
  #tabs button {{ background:#181b24; color:#9aa0ac; border:1px solid #262b36; padding:7px 16px;
                  border-radius:8px; cursor:pointer; font-size:0.88rem; }}
  #tabs button.active {{ background:#20304a; color:#7fb0ff; border-color:#2f4a72; }}
  #legend {{ position:sticky; top:0; background:#141721; border:1px solid #262b36; border-radius:8px;
             padding:10px 16px; font-size:0.82rem; color:#9aa0ac; margin-bottom:24px; z-index:5; }}
  #legend span {{ margin-right:18px; }}
  #legend .sw {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:middle; }}
  /* Extracted text beside the pages it came from, so a gap or a mangled table can
     be checked against the source without leaving the tab. Collapsible because on
     a prose-only section the pages add nothing and the text wants the width. */
  #doc-split {{ display:grid; grid-template-columns:minmax(0,1fr) 420px; gap:20px; align-items:start; }}
  #doc-split.collapsed {{ grid-template-columns:minmax(0,1fr) 92px; }}
  #doc-pdf {{ position:sticky; top:12px; border:1px solid #2a2f3a; border-radius:8px;
              background:#12151d; max-height:calc(100vh - 90px); display:flex; flex-direction:column; }}
  #doc-pdf-head {{ display:flex; justify-content:space-between; align-items:center; gap:8px;
                   padding:7px 10px; border-bottom:1px solid #2a2f3a; font-size:0.78rem; color:#9aa0ac; }}
  #doc-pdf-toggle {{ background:#1a1e28; color:#9aa0ac; border:1px solid #2a2f3a; border-radius:5px;
                     padding:2px 8px; font-size:0.72rem; cursor:pointer; }}
  #doc-pdf-toggle:hover {{ color:#e6e6e6; }}
  #doc-pdf-nav {{ display:flex; align-items:center; justify-content:center; gap:8px;
                 padding:8px 10px; border-bottom:1px solid #2a2f3a; }}
  #pr-pager {{ float:right; display:inline-flex; align-items:center; gap:5px; }}
  #pr-pdf-prev, #pr-pdf-next {{ background:#1a1e28; color:#c9d1d9; border:1px solid #2a2f3a;
    border-radius:4px; cursor:pointer; font-size:0.72rem; padding:1px 7px; }}
  #pr-pdf-prev:hover:not(:disabled), #pr-pdf-next:hover:not(:disabled) {{ background:#232838; color:#fff; }}
  #pr-pdf-prev:disabled, #pr-pdf-next:disabled {{ opacity:.35; cursor:default; }}
  #pr-pdf-pos {{ font-size:0.7rem; color:#9aa0ac; min-width:74px; text-align:center; }}
  #pr-pdf-goto {{ width:46px; background:#0d1017; color:#c9d1d9; border:1px solid #2a2f3a;
    border-radius:4px; font-size:0.7rem; padding:1px 4px; }}
  #doc-pdf-prev, #doc-pdf-next {{ background:#1a1e28; color:#c9d1d9; border:1px solid #2a2f3a;
                 border-radius:5px; width:28px; height:26px; font-size:0.85rem; cursor:pointer; }}
  #doc-pdf-prev:hover:not(:disabled), #doc-pdf-next:hover:not(:disabled) {{ background:#232838; color:#fff; }}
  #doc-pdf-prev:disabled, #doc-pdf-next:disabled {{ opacity:.35; cursor:default; }}
  #doc-pdf-pos {{ font-size:0.78rem; color:#9aa0ac; min-width:88px; text-align:center;
                 font-variant-numeric:tabular-nums; }}
  #doc-pdf-goto {{ width:52px; background:#0d1017; color:#c9d1d9; border:1px solid #2a2f3a;
                 border-radius:5px; padding:2px 5px; font-size:0.78rem; text-align:center; }}
  #doc-split.collapsed #doc-pdf-nav, #doc-split.collapsed #doc-pdf-body,
  #doc-split.collapsed #doc-pdf-label {{ display:none; }}
  #doc-pdf-body {{ overflow-y:auto; padding:10px; display:flex; flex-direction:column; align-items:center; }}
  #doc-pdf-body img {{ max-width:100%; border-radius:5px; border:1px solid #2a2f3a; background:#fff; }}
  .pdf-pno {{ font-size:0.72rem; color:#7d8494; margin-bottom:3px; }}
  .pdf-empty {{ color:#5c6470; font-size:0.8rem; padding:8px 2px; }}
  @media (max-width:1180px) {{ #doc-split {{ grid-template-columns:minmax(0,1fr); }}
                               #doc-pdf {{ position:static; max-height:none; }} }}
  #content {{ max-width:820px; line-height:1.6; }}
  #content h1, #content h2, #content h3 {{ color:#f2f2f2; }}
  #content table {{ border-collapse:collapse; width:100%; margin:12px 0; }}
  #content th, #content td {{ border:1px solid #2a2f3a; padding:6px 10px; font-size:0.88rem; text-align:left; }}
  #content th {{ background:#1a1e28; }}
  #content img {{ max-width:100%; border-radius:6px; border:1px solid #2a2f3a; }}
  .mineru-block {{ border-left:4px solid #4f8ff7; background:#10192b; padding:4px 16px 12px; border-radius:0 8px 8px 0; margin:16px 0; }}
  .mineru-block > p:first-child {{ color:#7fb0ff; font-size:0.82rem; margin-bottom:4px; }}
  .failed-block {{ border-left:4px solid #ff6b6b; background:#2a1414; padding:8px 16px; border-radius:0 8px 8px 0; margin:16px 0; color:#ffb3b3; }}
  .gap-marker-block {{ border-left:4px solid #f0a83c; background:#2a2210; padding:8px 16px; border-radius:0 8px 8px 0; margin:16px 0; color:#f0cc8f; }}
  a {{ color:#4f8ff7; }}
  .fn-ref {{ font-size:0.68em; vertical-align:super; line-height:0; }}
  .fn-ref a {{ text-decoration:none; padding:0 1px; }}
  .fn-ref a:hover {{ text-decoration:underline; }}
  .fn-ref.fn-dangling a {{ color:#f0a83c; }}
  .fn-def {{ font-size:0.82rem; color:#9aa0ac; margin:3px 0; padding-left:2px;
             scroll-margin-top:70px; }}
  .fn-def b {{ color:#c8ccd4; }}
  .fn-def:target {{ background:#20304a; border-radius:4px; padding:4px 6px; }}
  .fn-def.fn-missing {{ color:#f0a83c; }}
  .fn-def.fn-uncited {{ border-left:2px solid #f0a83c; padding-left:7px; }}
  .fn-tag {{ font-size:0.68rem; color:#f0a83c; background:#2a2210; border-radius:3px;
             padding:1px 5px; margin-left:5px; white-space:nowrap; }}
  .fn-back {{ text-decoration:none; margin-right:5px; opacity:0.55; }}
  .fn-back:hover {{ opacity:1; }}
  sup.fn-ref:target a {{ background:#20304a; border-radius:3px; }}
  #inspector-grid {{ display:flex; flex-direction:column; gap:20px; max-width:1100px; }}
  .insp-card {{ border-radius:10px; border:1px solid #262b36; background:#141721; overflow:hidden; }}
  .insp-card.insp-ok {{ border-left:4px solid #4f8ff7; }}
  .insp-card.insp-fail {{ border-left:4px solid #ff6b6b; }}
  .insp-card h4 {{ margin:0; padding:12px 18px; background:#181b24; font-size:0.92rem; display:flex;
                   align-items:center; gap:12px; }}
  .insp-src {{ font-size:0.72rem; color:#7d8494; font-weight:400; }}
  .insp-badge {{ margin-left:auto; font-size:0.78rem; padding:2px 10px; border-radius:20px; }}
  .insp-badge.ok {{ background:#123a1f; color:#3ddc76; }}
  .insp-badge.fail {{ background:#3a1414; color:#ff6b6b; }}
  .insp-cols {{ display:grid; grid-template-columns:1fr 1fr; gap:0; }}
  .insp-in, .insp-out {{ padding:14px 18px; }}
  .insp-in {{ border-right:1px solid #262b36; }}
  .insp-label {{ font-size:0.72rem; text-transform:uppercase; letter-spacing:0.05em; color:#7d8494; margin-bottom:8px; }}
  .insp-in img {{ max-width:100%; border-radius:6px; border:1px solid #2a2f3a; margin-bottom:8px; display:block; }}
  .insp-out table {{ border-collapse:collapse; width:100%; font-size:0.82rem; }}
  .insp-out th, .insp-out td {{ border:1px solid #2a2f3a; padding:5px 8px; }}
  .insp-out th {{ background:#1a1e28; }}
  .insp-fail-text {{ color:#ff6b6b; font-size:0.88rem; }}
  .insp-out details {{ margin-top:10px; font-size:0.78rem; color:#9aa0ac; }}
  .insp-out pre {{ white-space:pre-wrap; word-break:break-word; background:#0f1117; border:1px solid #262b36;
                   border-radius:6px; padding:10px; max-height:240px; overflow:auto; }}
  #val-banner {{ display:flex; align-items:center; gap:16px; padding:18px 22px; border-radius:10px; margin-bottom:26px; }}
  #val-banner.ok {{ background:#0f2417; border:1px solid #1f5c33; }}
  #val-banner.bad {{ background:#2a1414; border:1px solid #5c2020; }}
  .val-banner-icon {{ font-size:1.9rem; flex:none; }}
  .val-banner-title {{ font-size:1.12rem; font-weight:600; }}
  #val-banner.ok .val-banner-title {{ color:#3ddc76; }}
  #val-banner.bad .val-banner-title {{ color:#ff8080; }}
  .val-banner-sub {{ color:#9aa0ac; font-size:0.85rem; margin-top:3px; }}
  .val-heading {{ font-size:0.8rem; text-transform:uppercase; letter-spacing:0.06em; color:#7d8494; margin:0 0 12px; font-weight:600; }}
  #val-issues {{ margin-bottom:28px; }}
  .val-issue {{ border-radius:8px; padding:13px 16px; margin-bottom:10px; border-left:4px solid; background:#161414; }}
  .val-issue-red {{ background:#2a1414; border-left-color:#ff6b6b; }}
  .val-issue-amber {{ background:#2a2210; border-left-color:#f0a83c; }}
  .val-issue.clickable {{ cursor:pointer; }}
  .val-issue.clickable:hover {{ filter:brightness(1.18); }}
  .val-issue-head {{ display:flex; align-items:center; gap:10px; font-size:0.88rem; flex-wrap:wrap; }}
  .val-issue-tag {{ font-size:0.66rem; font-weight:700; letter-spacing:0.05em; padding:2px 7px; border-radius:4px; background:rgba(255,255,255,0.12); flex:none; }}
  .val-issue-title {{ font-weight:600; color:#f2f2f2; }}
  .val-issue-pages {{ color:#9aa0ac; font-size:0.8rem; }}
  .val-issue-link {{ margin-left:auto; color:#7fb0ff; font-size:0.8rem; flex:none; }}
  .val-issue-body {{ color:#d8b9b9; font-size:0.84rem; margin-top:6px; }}
  .val-issue-snippet {{ font-family:ui-monospace,SFMono-Regular,monospace; font-size:0.78rem; color:#aab0bc; margin-top:9px;
                         line-height:1.6; background:#0f1117; border-radius:6px; padding:9px 12px; word-break:break-word; }}
  .val-issue-snippet mark {{ background:#ff6b6b; color:#1a0505; padding:1px 4px; border-radius:3px; font-weight:700; }}
  .val-clean {{ display:flex; align-items:center; gap:10px; color:#3ddc76; font-size:0.9rem; padding:14px 18px;
                background:#0f2417; border:1px solid #1f5c33; border-radius:8px; }}
  details.val-advanced {{ margin-bottom:12px; border:1px solid #262b36; border-radius:8px; }}
  details.val-advanced summary {{ cursor:pointer; padding:12px 16px; font-size:0.86rem; color:#c8ccd4; list-style:none; }}
  details.val-advanced summary::-webkit-details-marker {{ display:none; }}
  details.val-advanced summary::before {{ content:"\\25B8  "; color:#7d8494; }}
  details.val-advanced[open] summary::before {{ content:"\\25BE  "; }}
  details.val-advanced .val-advanced-body {{ padding:2px 16px 16px; border-top:1px solid #1d2230; }}
  .val-stat-row {{ display:flex; gap:22px; margin:12px 0 16px; flex-wrap:wrap; }}
  .val-stat {{ font-size:0.85rem; color:#9aa0ac; }}
  .val-stat b {{ color:#e6e6e6; font-weight:600; }}
  .val-words {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .val-words span {{ background:#181b24; border:1px solid #262b36; border-radius:20px; padding:3px 10px; font-size:0.78rem; }}
  .val-words .n {{ color:#7d8494; margin-right:5px; }}
  .val-ack-item {{ font-size:0.84rem; padding:6px 0; }}
  .val-ack-item a {{ cursor:pointer; }}
  #pr-layout {{ display:flex; gap:0; height:80vh; margin:0 -48px 0 -48px; }}
  #pr-nav {{ width:225px; flex:none; overflow-y:auto; padding:0 12px 16px 48px; border-right:1px solid #262b36; }}
  /* extracted content for the selected node, so the tree, what we produced, the
     source page and the findings can all be read side by side */
  #pr-content-col {{ flex:1.05; min-width:0; overflow-y:auto; padding:0 16px 16px; border-right:1px solid #262b36; }}
  #pr-content-col .pr-colhead {{ position:sticky; top:0; background:#0f1117; padding:2px 0 8px; }}
  #pr-content-body {{ font-size:0.84rem; line-height:1.55; }}
  #pr-content-body table {{ border-collapse:collapse; width:100%; margin:10px 0; font-size:0.76rem; }}
  #pr-content-body th, #pr-content-body td {{ border:1px solid #2a2f3a; padding:4px 6px; text-align:left; }}
  #pr-content-body th {{ background:#1a1e28; }}
  #pr-content-body h1, #pr-content-body h2, #pr-content-body h3 {{ font-size:0.95rem; color:#f2f2f2; }}
  #pr-content-body img {{ max-width:100%; border-radius:4px; border:1px solid #2a2f3a; }}
  #pr-page-col {{ flex:1.25; min-width:0; overflow:auto; background:#0a0b0f; padding:12px; }}
  #pr-page-col img {{ max-width:100%; height:fit-content; border:1px solid #2a2f3a; border-radius:4px; box-shadow:0 4px 24px rgba(0,0,0,0.4); }}
  #pr-issues-col {{ width:310px; flex:none; overflow-y:auto; padding:0 48px 16px 14px; border-left:1px solid #262b36; }}
  .pr-colhead {{ font-size:0.7rem; text-transform:uppercase; letter-spacing:0.06em; color:#7d8494;
                 margin-bottom:8px; font-weight:600; }}
  .pr-dirname {{ font-size:0.78rem; color:#7d8494; margin:14px 0 4px; padding-left:2px; }}
  .pr-dirname:first-child {{ margin-top:0; }}
  .pr-dir {{ margin-left:10px; }}
  .pr-file {{ font-size:0.8rem; color:#c8ccd4; font-weight:600; margin:8px 0 2px; }}
  .pr-page-item {{ display:flex; align-items:center; justify-content:space-between; padding:5px 10px; margin-left:8px;
                   border-radius:5px; font-size:0.84rem; cursor:pointer; color:#c8ccd4; text-decoration:none; }}
  .pr-page-item:hover {{ background:#1d2230; }}
  .pr-page-item.active {{ background:#20304a; color:#7fb0ff; }}
  .pr-count {{ color:#ff6b6b; font-size:0.72rem; background:rgba(255,107,107,0.15); padding:1px 6px; border-radius:10px; }}
  /* ---- scorecard ---- */
  #score-view {{ max-width:1080px; }}
  /* Duplicated content. Amber rather than red: this is "look at this", not "this is
     broken" — a legitimately repeated boilerplate answer can trip it too. */
  .dup-hit {{ background:#3a2c0c; box-shadow: inset 3px 0 0 #f0a83c; }}
  .dup-hit::after {{ content:" \2398 dup"; color:#f0a83c; font-size:0.68rem;
                    letter-spacing:0.04em; vertical-align:super; }}
  .dup-banner {{ background:#2a2210; border:1px solid #6b5417; color:#f0c274;
                border-radius:8px; padding:9px 12px; margin-bottom:12px;
                font-size:0.8rem; line-height:1.45; }}
  .gate {{ display:flex; align-items:center; gap:20px; padding:22px 26px; border-radius:12px; margin-bottom:8px; }}
  .gate-pass {{ background:#0f2417; border:1px solid #1f5c33; }}
  .gate-review {{ background:#2a2210; border:1px solid #6b5417; }}
  .gate-fail {{ background:#2a1414; border:1px solid #5c2020; }}
  .gate-unknown {{ background:#181b24; border:1px solid #2a2f3a; }}
  .gate-verdict {{ font-size:1.7rem; font-weight:700; letter-spacing:0.04em; flex:none; }}
  .gate-pass .gate-verdict {{ color:#3ddc76; }}
  .gate-review .gate-verdict {{ color:#f0a83c; }}
  .gate-fail .gate-verdict {{ color:#ff6b6b; }}
  .gate-unknown .gate-verdict {{ color:#7d8494; }}
  .gate-sub {{ color:#c8ccd4; font-size:0.92rem; }}
  .gate-note {{ color:#7d8494; font-size:0.8rem; margin-top:5px; }}
  .dim-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(330px, 1fr)); gap:14px; margin:20px 0 26px; }}
  .dim {{ background:#141721; border:1px solid #262b36; border-radius:10px; padding:15px 17px; }}
  .dim-head {{ display:flex; align-items:baseline; gap:10px; }}
  .dim-name {{ font-weight:600; font-size:1rem; }}
  .dim-tag {{ font-size:0.63rem; font-weight:700; letter-spacing:0.06em; padding:2px 6px;
              border-radius:4px; text-transform:uppercase; flex:none; }}
  .dim-tag-gate {{ background:#20304a; color:#7fb0ff; }}
  .dim-tag-adv {{ background:#26262e; color:#8b8f9a; }}
  .dim.advisory {{ opacity:0.88; }}
  .dim-score {{ margin-left:auto; font-size:1.5rem; font-weight:700; font-variant-numeric:tabular-nums; }}
  .s-good {{ color:#3ddc76; }} .s-warn {{ color:#f0a83c; }} .s-bad {{ color:#ff6b6b; }} .s-na {{ color:#7d8494; }}
  .dim-bar {{ height:6px; border-radius:3px; background:#232734; margin:10px 0 12px; overflow:hidden; }}
  .dim-bar > div {{ height:100%; border-radius:3px; }}
  .bar-good {{ background:#3ddc76; }} .bar-warn {{ background:#f0a83c; }} .bar-bad {{ background:#ff6b6b; }}
  .dim-what {{ color:#c8ccd4; font-size:0.85rem; margin-bottom:10px; }}
  .dim-stats {{ display:flex; flex-direction:column; gap:3px; margin-bottom:11px; }}
  .dim-stat {{ display:flex; justify-content:space-between; gap:10px; font-size:0.82rem; color:#9aa0ac; }}
  .dim-stat b {{ color:#e6e6e6; font-variant-numeric:tabular-nums; }}
  .dim-stat.bad b {{ color:#ff6b6b; }}
  .dim-stat.warn b {{ color:#f0a83c; }}
  .struct-table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:0.82rem; }}
  .struct-table th {{ text-align:left; color:#7d838f; font-weight:600; padding:4px 8px;
                      border-bottom:1px solid #262b36; }}
  .struct-table td {{ padding:4px 8px; border-bottom:1px solid #1b1f27; color:#9aa0ac; }}
  .struct-table td.num, .struct-table th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .struct-table tr.struct-off td {{ color:#f0a83c; }}
  .struct-table tr.struct-off td:first-child {{ font-weight:600; }}
  /* ---- structure charts ----
     Three colours only, validated against this surface (#141721): siblings are
     RECESSIVE context (#63748f), this document is the vivid SUBJECT (#4f8ff7),
     and outlier status is amber (#f0a83c, reserved). Every pair clears the
     normal-vision separation floor, so the encoding survives colour-blindness —
     and identity is never colour-alone: the current row is also labelled and
     marked with a caret. */
  /* the name column has to fit "<jurisdiction> · <doc id>" un-truncated, since
     a product can hold two documents of the same jurisdiction and the id is the
     only thing telling them apart */
  .sv-row {{ display:grid; grid-template-columns:minmax(200px,22%) 1fr 84px; align-items:center;
             gap:10px; padding:3px 0; font-size:0.8rem; }}
  /* jurisdiction may ellipsis; the doc id never does — two documents of the
     same jurisdiction are told apart ONLY by that id, so it is the one part
     that must survive a narrow column */
  .sv-name {{ display:flex; gap:5px; align-items:baseline; min-width:0; color:#9aa0ac; }}
  .sv-j {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; }}
  .sv-id {{ flex:none; color:#7d8494; font-size:0.72rem; font-variant-numeric:tabular-nums; }}
  .sv-row.is-self .sv-name {{ color:#e6e6e6; font-weight:600; }}
  .sv-track {{ position:relative; background:#1b1f27; border-radius:3px; height:14px; }}
  .sv-bar {{ height:100%; border-radius:3px; background:#63748f; }}
  .sv-row.is-self .sv-bar {{ background:#4f8ff7; }}
  .sv-row.is-out  .sv-bar {{ background:#f0a83c; }}
  .sv-val {{ text-align:right; color:#c8ccd4; font-variant-numeric:tabular-nums; }}
  /* median reference line — recessive, dashed, sits above the fills */
  .sv-med {{ position:absolute; top:-3px; bottom:-3px; width:0; border-left:1px dashed #7d8494; }}
  .sv-medlab {{ font-size:0.72rem; color:#7d8494; margin:2px 0 8px 180px; }}
  /* distribution histogram */
  .sh-wrap {{ display:flex; align-items:flex-end; gap:2px; height:64px; margin-top:6px; }}
  .sh-col {{ flex:1; display:flex; flex-direction:column; justify-content:flex-end; height:100%; }}
  .sh-bar {{ background:#4f8ff7; border-radius:3px 3px 0 0; min-height:2px; }}
  .sh-x {{ display:flex; gap:2px; margin-top:4px; }}
  .sh-x span {{ flex:1; text-align:center; font-size:0.66rem; color:#7d8494;
                white-space:nowrap; overflow:hidden; }}
  .sh-note {{ font-size:0.75rem; color:#7d8494; margin-top:6px; }}
  .dim-advice {{ font-size:0.81rem; color:#9fd0a8; background:#101e14; border-left:3px solid #2f6b3f;
                 border-radius:0 5px 5px 0; padding:8px 11px; line-height:1.5; }}
  .dim-caveat {{ font-size:0.78rem; color:#b8a678; margin-top:7px; line-height:1.5; }}
  .dim-from {{ font-size:0.75rem; color:#6e7482; margin-top:8px; line-height:1.45; }}
  .panel {{ background:#141721; border:1px solid #262b36; border-radius:10px; padding:16px 18px; margin-bottom:20px; }}
  .panel h4 {{ margin:0 0 4px; font-size:0.95rem; }}
  .panel-sub {{ color:#7d8494; font-size:0.8rem; margin-bottom:14px; }}
  .rv-set {{ border-radius:8px; padding:11px 13px; border-left:4px solid currentColor; }}
  .rv-pass {{ background:#0f2417; color:#35c46a; }}
  .rv-review {{ background:#241d12; color:#f5b942; }}
  .rv-fail {{ background:#2b1013; color:#ff5c5c; }}
  .rv-set-top {{ font-size:0.87rem; font-weight:650; }}
  .rv-differs {{ background:#111c2c; color:#63b3ff; font-size:0.7rem; padding:1px 7px;
       border-radius:4px; margin-left:8px; font-weight:600; }}
  .rv-agrees {{ color:#7d8494; font-size:0.72rem; margin-left:8px; font-weight:500; }}
  .rv-reason {{ color:#c3c9d4; font-size:0.81rem; line-height:1.5; margin-top:6px; font-weight:400; }}
  .rv-meta {{ color:#6e7482; font-size:0.72rem; margin-top:6px; }}
  .rv-none {{ color:#6e7482; font-size:0.79rem; padding:8px 0; }}
  .rv-form {{ margin-top:11px; display:flex; flex-direction:column; gap:7px; }}
  .rv-row {{ display:flex; gap:7px; align-items:center; }}
  .rv-form textarea, .rv-form input, .rv-form select {{ background:#0f1218; color:#c3c9d4;
       border:1px solid #262b36; border-radius:6px; padding:7px 9px; font:inherit; font-size:0.8rem; }}
  .rv-form textarea {{ resize:vertical; width:100%; }}
  .rv-btn {{ background:#1b2029; color:#a8aeba; border:1px solid #2d3340; border-radius:6px;
       padding:5px 11px; font-size:0.75rem; cursor:pointer; margin-left:8px; }}
  .rv-btn:hover {{ background:#232a35; }}
  .rv-primary {{ background:#152742; color:#63b3ff; border-color:#2b4570; margin-left:0; }}
  .rv-err {{ color:#ff5c5c; font-size:0.75rem; }}
  .rv-sep {{ margin-top:14px; color:#7d8494; font-size:0.72rem; text-transform:uppercase; letter-spacing:.6px; }}
  .rv-note {{ background:#12151c; border-radius:6px; padding:8px 11px; margin-top:7px; }}
  .rv-note-sys {{ opacity:.65; font-style:italic; }}
  .rv-note-text {{ color:#c3c9d4; font-size:0.8rem; line-height:1.5; }}
  .special-mode {{ background:#241d12; border:1px solid #6b5320; border-left:4px solid #f5b942;
       border-radius:8px; padding:12px 14px; margin:12px 0 0; }}
  .sm-title {{ color:#f5b942; font-weight:700; font-size:0.86rem; letter-spacing:.3px; }}
  .sm-why {{ color:#ddd0b4; font-size:0.8rem; line-height:1.55; margin-top:6px; }}
  .sm-facts {{ display:flex; flex-wrap:wrap; gap:16px; margin-top:9px; color:#b9a97f; font-size:0.75rem; }}
  /* Extraction cost. Fallback tiers are amber in the step bars so a rescued document
     shows at a glance which half of its runtime was the rescue. */
  .timing-card {{ border:1px solid #2a3140; background:#141922; border-radius:10px; padding:12px 14px; margin:12px 0; }}
  .tm-head {{ display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }}
  .tm-title {{ font-size:0.7rem; letter-spacing:.5px; color:#8fb0e0; font-weight:700; }}
  .tm-total {{ font-size:1.25rem; font-weight:700; font-variant-numeric:tabular-nums; }}
  .tm-slow {{ font-size:0.72rem; color:#8b93a1; }}
  .tm-facts {{ display:flex; gap:16px; flex-wrap:wrap; margin:8px 0 4px; font-size:0.72rem; color:#a8b0bd; }}
  .tm-facts b {{ color:#dbe3ee; }}
  .tm-warn {{ color:#d9a441; }} .tm-warn b {{ color:#f0c069; }}
  .tm-steps {{ margin-top:8px; display:flex; flex-direction:column; gap:3px; }}
  .tm-step {{ display:grid; grid-template-columns:130px 1fr 58px 38px; align-items:center; gap:8px; font-size:0.72rem; }}
  .tm-k {{ color:#a8b0bd; }}
  .tm-bar {{ height:7px; background:#1e2430; border-radius:3px; overflow:hidden; }}
  .tm-bar i {{ display:block; height:100%; background:#3987e5; }}
  .tm-step-fb .tm-bar i {{ background:#d9a441; }}
  .tm-step-fb .tm-k {{ color:#d9a441; }}
  .tm-v, .tm-p {{ text-align:right; font-variant-numeric:tabular-nums; color:#dbe3ee; }}
  .tm-p {{ color:#6b7280; }}
  .tm-note {{ margin-top:8px; font-size:0.7rem; color:#8b93a1; }}
  /* Extraction path: one line, above everything. Each chip states its outcome in
     WORDS as well as colour, and carries the full reason in its tooltip. */
  .fl-strip {{ display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin-bottom:12px;
       padding:8px 11px; background:#12151c; border:1px solid #262b36; border-radius:9px; }}
  .fl-lab {{ font-size:0.66rem; text-transform:uppercase; letter-spacing:.7px; color:#6e7482;
       margin-right:3px; }}
  .fl-chip {{ font-size:0.73rem; color:#8b93a1; padding:2px 8px; border-radius:5px;
       background:#181c24; border:1px solid #262b36; cursor:help; white-space:nowrap; }}
  .fl-chip b {{ color:#5c6470; font-weight:700; margin-right:3px; }}
  .fl-chip i {{ font-style:normal; font-weight:700; margin-left:5px; text-transform:uppercase;
       font-size:0.66rem; letter-spacing:.3px; }}
  .fl-sep {{ color:#3d4757; font-size:0.8rem; }}
  .fl-ok    {{ border-color:#1f5c37; }} .fl-ok    i {{ color:#35c46a; }}
  .fl-adopt {{ border-color:#2b4570; background:#111c2c; }} .fl-adopt i {{ color:#63b3ff; }}
  .fl-adopt b, .fl-adopt {{ color:#c3c9d4; }}
  .fl-esc   i {{ color:#f5b942; }}
  .fl-rej   i {{ color:#f5b942; }}
  .fl-skip  {{ opacity:.5; }} .fl-skip i {{ color:#7d8494; }}
  .fl-won {{ font-size:0.74rem; color:#8b93a1; margin-left:2px; }}
  .fl-won b {{ color:#c3c9d4; }}
  .fl-flag {{ font-size:0.7rem; color:#f5b942; background:#241d12; border:1px solid #6b5320;
       padding:2px 7px; border-radius:5px; cursor:help; }}
  .failwhy {{ background:#2b1013; border:1px solid #7a2430; border-left:4px solid #ff5c5c;
       border-radius:9px; padding:13px 15px; margin:12px 0 0; }}
  .failwhy-t {{ color:#ff5c5c; font-weight:750; font-size:0.9rem; letter-spacing:.3px; }}
  .failwhy-b {{ color:#f0d6d6; font-size:0.84rem; line-height:1.55; margin-top:7px; }}
  .failwhy-m {{ color:#c49a9a; font-size:0.75rem; line-height:1.5; margin-top:8px; }}
  .toc-verdict {{ font-size:0.92rem; font-weight:700; letter-spacing:.3px; padding:9px 12px;
       border-radius:7px; border-left:4px solid currentColor; }}
  .toc-v-pass {{ background:#0f2417; color:#35c46a; }}
  .toc-v-fail {{ background:#2b1013; color:#ff5c5c; }}
  .toc-v-fixed {{ background:#111c2c; color:#63b3ff; }}
  .toc-why {{ color:#a8aeba; font-size:0.81rem; line-height:1.55; margin-top:9px; }}
  .toc-rescued {{ margin-top:11px; background:#111c2c; border-left:3px solid #63b3ff;
       border-radius:7px; padding:10px 12px; color:#bcd2ea; font-size:0.8rem; line-height:1.55; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:16px; margin-bottom:13px; }}
  .legend-item {{ display:flex; align-items:center; gap:6px; font-size:0.79rem; color:#9aa0ac; cursor:help; }}
  .legend-sw {{ width:11px; height:11px; border-radius:2px; flex:none; }}
  .pr-span {{ font-size:0.72rem; color:#8d94a2; letter-spacing:.02em; margin:2px 0 4px; }}
  .pr-carried {{ font-size:0.7rem; color:#8d94a2; font-weight:400; margin-left:8px; }}
  .heat {{ display:flex; flex-wrap:wrap; gap:3px; }}
  .heat-cell {{ width:17px; height:17px; border-radius:3px; cursor:pointer; position:relative; }}
  .heat-cell:hover {{ outline:2px solid #7fb0ff; outline-offset:1px; }}
  .h-ok {{ background:#1f5c33; }}
  .h-flagged {{ background:#8a6a1c; }}
  .h-silent {{ background:#a33; }}
  .h-unvalidatable {{ background:#333846; }}
  .strip {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .chip {{ font-size:0.76rem; padding:3px 9px; border-radius:20px; cursor:pointer; border:1px solid transparent;
           font-variant-numeric:tabular-nums; }}
  .chip:hover {{ border-color:#7fb0ff; }}
  .c-confident {{ background:#123a1f; color:#3ddc76; }}
  .c-uncertain {{ background:#2a2210; color:#f0a83c; }}
  .c-unverified {{ background:#1b2436; color:#8fb0e0; }}
  .c-reclassified {{ background:#1d2230; color:#9aa0ac; }}
  .c-continuation {{ background:#1b2436; color:#8fb0e0; }}
  /* content verified present but no table of its own — same family as the other
     "not wrong, not fully verified" buckets, deliberately not the failure red */
  .c-absorbed {{ background:#1b2436; color:#8fb0e0; }}
  .c-failed {{ background:#3a1414; color:#ff6b6b; }}
  .calib {{ font-size:0.79rem; color:#7d8494; border-top:1px solid #262b36; padding-top:14px; line-height:1.6; }}
  .dim-formula {{ font-size:0.74rem; color:#8b90a0; font-family:ui-monospace,SFMono-Regular,monospace;
                  background:#0f1117; border:1px solid #232734; border-radius:5px;
                  padding:7px 9px; margin-top:8px; line-height:1.55; word-break:break-word; }}
  .find {{ display:flex; gap:12px; align-items:flex-start; padding:11px 13px; border-radius:8px;
           margin-bottom:7px; border-left:3px solid; background:#161821; }}
  .find-silent {{ border-left-color:#ff6b6b; }}
  .find-flagged {{ border-left-color:#f0a83c; }}
  .find-review {{ border-left-color:#f0a83c; }}
  .find-advisory {{ border-left-color:#4a5060; }}
  .find.is-dismissed {{ opacity:0.55; border-left-color:#4a5060; background:#131520; }}
  .find-main {{ flex:1; min-width:0; }}
  .find-top {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:3px; }}
  .find-kind {{ font-size:0.63rem; font-weight:700; letter-spacing:0.05em; padding:2px 6px;
                border-radius:4px; background:rgba(255,255,255,0.09); text-transform:uppercase; }}
  .find-where {{ font-size:0.75rem; color:#7d8494; }}
  .find-title {{ font-size:0.86rem; color:#e6e6e6; word-break:break-word; }}
  .find-detail {{ font-size:0.78rem; color:#9aa0ac; margin-top:3px; line-height:1.5; }}
  .find-btn {{ flex:none; background:#1d2230; color:#9aa0ac; border:1px solid #2f3542;
               border-radius:6px; padding:5px 11px; font-size:0.77rem; cursor:pointer; }}
  .find-btn:hover {{ background:#252b3a; color:#e6e6e6; }}
  .find-btn.restore {{ color:#7fb0ff; border-color:#2f4a72; }}
  .find-none {{ color:#3ddc76; font-size:0.86rem; padding:12px 14px; background:#0f2417;
                border:1px solid #1f5c33; border-radius:8px; }}
  .flow {{ display:flex; align-items:stretch; gap:6px; margin-top:14px; flex-wrap:wrap; }}
  .flow-step {{ flex:1; min-width:140px; border-radius:6px; padding:10px 12px;
                border:1px solid #262b36; background:#141721; }}
  .flow-name {{ font-weight:700; font-size:0.9rem; }}
  .flow-sub {{ font-size:0.68rem; color:#8892a4; margin:2px 0 7px; }}
  .flow-body {{ font-size:0.78rem; color:#c8ccd4; line-height:1.5; }}
  .flow-arr {{ align-self:center; color:#4a5262; font-size:1.3rem; }}
  .flow-ai {{ border-color:#2f6fd0; }}
  .flow-ok {{ border-color:#4ac97e; }}
  .flow-warn {{ border-color:#f0a83c; }}
  .ai-find-ok {{ background:#152417; border-color:#4ac97e; }}
  .ai-tbl {{ width:100%; border-collapse:collapse; margin-top:12px; font-size:0.82rem; }}
  .ai-tbl th {{ text-align:left; color:#9aa0ac; font-weight:600; padding:6px 10px;
                border-bottom:1px solid #262b36; }}
  .ai-tbl td {{ padding:6px 10px; border-bottom:1px solid #1b1f28; }}
  .ai-flag {{ background:#ff6b6b; color:#141721; border-radius:9px; padding:1px 8px; font-weight:700; }}
  .ai-find {{ margin:7px 0; padding:8px 11px; border-radius:5px; border-left:3px solid; }}
  .ai-find-miss {{ background:#241618; border-color:#ff6b6b; }}
  .ai-find-move {{ background:#241f14; border-color:#f0a83c; }}
  .ai-tag {{ font-size:0.68rem; font-weight:700; letter-spacing:0.04em; }}
  /* The sub-section a finding sits in, given weight because it is the part that
     turns a finding into somewhere to look. */
  .ai-sub {{ font-size:0.74rem; }}
  .ai-txt {{ font-family:ui-monospace,Menlo,monospace; font-size:0.76rem; color:#c8ccd4;
             margin-top:4px; word-break:break-all; }}
  .stage-btn {{ font:inherit; font-size:0.7rem; margin-left:4px; padding:2px 7px; cursor:pointer;
                border:1px solid #3a4252; background:#232a36; color:#9aa4b5; border-radius:4px; }}
  .stage-btn.on {{ background:#2f6fd0; border-color:#2f6fd0; color:#fff; }}
  /* Which tree the scorecard below describes. Sits above the gate, because a verdict
     read against the wrong tree is worse than no verdict. */
  .sc-viewswitch {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap;
                   margin-bottom:14px; padding:9px 12px; background:#131722;
                   border:1px solid #1f2733; border-radius:8px; }}
  .sc-viewswitch .stage-btn {{ margin-left:0; font-size:0.74rem; padding:4px 10px; }}
  .sc-viewnote {{ color:#7d8494; font-size:0.76rem; }}
</style></head>
<body>
<div id="sidebar">
  <h3>Files <span id="stage-switch"></span></h3>
  <div id="tree"></div>
  <h3>Tables</h3>
  <div id="tables-panel"></div>
</div>
<div id="main">
  <div id="jobhead">
    <a id="back" href="/" title="Back to all documents (or press Escape)">&#8592; All documents</a>
    <span id="jobname">{filename}</span>
  </div>
  <div id="tabs">
    <button id="tab-score" class="active">Scorecard</button>
    <button id="tab-doc">Document</button>
    <button id="tab-insp">MinerU Inspector</button>
    <button id="tab-val">Validation</button>
    <button id="tab-ai">Stage 4 &middot; AI</button>
    <button id="tab-pr">Page Review</button>
  </div>
  <div id="score-view">
    <div id="score-body">Loading&hellip;</div>
  </div>
  <div id="doc-view" style="display:none">
    <div id="legend">
      <span><span class="sw" style="background:#4f8ff7"></span>MinerU-extracted table (Stage 2)</span>
      <span><span class="sw" style="background:#ff6b6b"></span>Extraction failed (flagged, not dropped)</span>
      <span><span class="sw" style="background:#f0a83c"></span>Possible content gap (Test 2 vs. source PDF)</span>
      <span><span class="sw" style="background:#666"></span>Plain text = deterministic extractor (Stage 1)</span>
    </div>
    <div id="doc-split">
      <div id="content">Loading&hellip;</div>
      <div id="doc-pdf">
        <div id="doc-pdf-head">
          <span id="doc-pdf-label">source pages</span>
          <button id="doc-pdf-toggle" title="hide/show the source pages">hide</button>
        </div>
        <div id="doc-pdf-nav">
          <button id="doc-pdf-prev" title="previous page (&larr;)">&larr;</button>
          <span id="doc-pdf-pos">&ndash;</span>
          <button id="doc-pdf-next" title="next page (&rarr;)">&rarr;</button>
          <input id="doc-pdf-goto" type="number" min="1" title="jump to page">
        </div>
        <div id="doc-pdf-body"><div class="pdf-empty">select a section</div></div>
      </div>
    </div>
  </div>
  <div id="ai-view" style="display:none">
    <div id="ai-body">Loading&hellip;</div>
  </div>
  <div id="inspector-view" style="display:none">
    <p style="color:#9aa0ac; font-size:0.88rem; margin-top:0;">Every table Stage 1 flagged, with exactly what was
      cropped and passed to MinerU on the left and what MinerU returned on the right — so a bad or missing
      extraction can be checked against the actual source page.</p>
    <div id="inspector-grid">Loading&hellip;</div>
  </div>
  <div id="validation-view" style="display:none">
    <p style="color:#9aa0ac; font-size:0.88rem; margin-top:0;">Source PDF vs. extracted tree, checked three ways:
      whole-document word coverage, per-section location of any gap, and numeric/percentage integrity.</p>
    <div id="validation-body">Loading&hellip;</div>
  </div>
  <div id="pagereview-view" style="display:none">
    <p style="color:#9aa0ac; font-size:0.88rem; margin:0 0 14px;">Every finding on the scorecard that can be placed on a
      page &mdash; the same list, page by page, so this and the heat-map can never disagree. Follows the scorecard view
      you have selected. Pick a page: the original PDF renders in the middle, everything found on it lists on the right.</p>
    <div id="pr-layout">
      <div id="pr-nav">Loading&hellip;</div>
      <div id="pr-content-col">
        <div class="pr-colhead">Extracted content</div>
        <div id="pr-content-body"><span style="color:#7d8494">select a page</span></div>
      </div>
      <div id="pr-page-col">
        <div class="pr-colhead">Source PDF page
          <span id="pr-pager">
            <button id="pr-pdf-prev" title="previous page (&larr;)">&larr;</button>
            <span id="pr-pdf-pos">&ndash;</span>
            <button id="pr-pdf-next" title="next page (&rarr;)">&rarr;</button>
            <input id="pr-pdf-goto" type="number" min="1" title="jump to page">
          </span>
        </div>
        <div id="pr-page-body"><span style="color:#7d8494">select a page</span></div>
      </div>
      <div id="pr-issues-col"></div>
    </div>
  </div>
</div>
<script>
const JOB_ID = "{job_id}";
let DATA = null;

let FN_DEFINED = new Set(), FN_CITED = new Set();
function indexFootnotes(files) {{
  // Resolution must be WHOLE-TREE: a body lives in whichever section the PDF put
  // it, so a reference is routinely defined in a different file. Marking per file
  // called half the references broken on one page (48 refs, 24 local bodies).
  FN_DEFINED = new Set(); FN_CITED = new Set();
  for (const text of Object.values(files || {{}})) {{
    for (const m of text.matchAll(/^\\[\\^([^\\]]+)\\]:/gm)) FN_DEFINED.add(m[1]);
    for (const m of text.matchAll(/\\[\\^([^\\]]+)\\](?!:)/g)) FN_CITED.add(m[1]);
  }}
}}

function renderFootnotes(mdText) {{
  // marked.js has no footnote support, so [^n] rendered as literal text here
  // while the standalone viewer (assets/viewer_template.html) has linked them all
  // along. Ported, plus: ids are namespaced per file (the same footnote number
  // appears in many sections, and duplicate DOM ids would jump to the wrong one),
  // refs get a back-link target, and BOTH failure directions are marked in place:
  // a reference with no body anywhere (fn-dangling) and a body nothing cites
  // (fn-uncited, i.e. the inline marker was lost).
  mdText = mdText.replace(/\\[\\^(\\d+)\\]:\\s?(.*)/g, (m, n, body) => {{
    const noBody = /footnote body not present/i.test(body);
    const uncited = !FN_CITED.has(n);          // nothing in the prose cites it
    const cls = 'fn-def' + (noBody ? ' fn-missing' : '') + (uncited ? ' fn-uncited' : '');
    const tag = uncited
      ? '<span class="fn-tag" title="This footnote body exists but nothing in the text cites it — its inline marker (a ~5pt superscript) was lost during extraction">marker lost</span>'
      : '';
    return `<p class="${{cls}}" id="fn-${{n}}">`
         + `<a class="fn-back" href="#fnref-${{n}}" title="back to the reference">&#8617;</a>`
         + `<b>${{n}}.</b> ${{body}} ${{tag}}</p>`;
  }});
  mdText = mdText.replace(/\\[\\^(\\d+)\\]/g, (m, n) => {{
    const has = FN_DEFINED.has(n);             // resolved anywhere in the tree
    return `<sup class="fn-ref${{has ? '' : ' fn-dangling'}}" id="fnref-${{n}}">`
         + `<a href="#fn-${{n}}"${{has ? '' : ' title="no body for this footnote anywhere in the tree"'}}>${{n}}</a></sup>`;
  }});
  return mdText;
}}

function anchorPattern(words) {{
  // words come only from our own tokenizer ([A-Za-z0-9'-] only), so none of
  // them can contain a regex metacharacter that needs escaping. Separator
  // allows any punctuation/whitespace the tree's real formatting inserted
  // between words that a plain space-joined match would miss (e.g. a comma).
  return new RegExp(words.join('[^a-zA-Z0-9]+'), 'i');
}}

function insertGapMarkers(dataFiles, clResults) {{
  for (const r of (clResults || [])) {{
    // Acknowledged gaps already carry their own [TABLE EXTRACTION FAILED]
    // marker inline — an extra one here would just be a duplicate.
    if (r.acknowledged || !r.dropped || !r.dropped.length) continue;
    let text = dataFiles[r.file];
    if (text === undefined) continue;
    for (const d of r.dropped) {{
      const snippet = d.tokens.slice(0, 20).join(' ') + (d.tokens.length > 20 ? ' \\u2026' : '');
      const marker = '\\n\\n> \\u26A0 **[POSSIBLE CONTENT GAP \\u2014 ' + d.tokens.length + ' word(s)]** the source PDF has content here that this section doesn\\u2019t: \\u201c' + snippet + '\\u201d\\n\\n';
      const beforeWords = (d.before || '').split(' ').filter(Boolean).slice(-5);
      const afterWords = (d.after || '').split(' ').filter(Boolean).slice(0, 5);
      let insertAt = -1;
      if (beforeWords.length) {{
        const m = text.match(anchorPattern(beforeWords));
        if (m) {{
          const brk = text.indexOf('\\n\\n', m.index + m[0].length);
          insertAt = brk === -1 ? text.length : brk;
        }}
      }}
      if (insertAt === -1 && afterWords.length) {{
        const m = text.match(anchorPattern(afterWords));
        if (m) insertAt = m.index;
      }}
      if (insertAt === -1) {{
        text = text + marker;  // couldn't anchor precisely — still surface it, just at the end
      }} else {{
        text = text.slice(0, insertAt) + marker + text.slice(insertAt);
      }}
    }}
    dataFiles[r.file] = text;
  }}
}}

function renderTree(node, container, prefix) {{
  // Files and folders INTERLEAVED, in name order. Rendering every file and then every
  // folder pushed the sub-chunked sections into a block at the very bottom, so section
  // 6's folder sat below section 16's file — nowhere near its place in the document.
  const entries = [
    ...node.files.map(f => ({{kind: 'file', name: f.name, node: f}})),
    ...node.dirs.map(d => ({{kind: 'dir', name: d.name, node: d}})),
  ].sort((a, b) => {{
    if (a.name === 'README.md') return -1;
    if (b.name === 'README.md') return 1;
    return a.name.localeCompare(b.name, undefined, {{numeric: true}});
  }});
  for (const e of entries) {{
    if (e.kind === 'dir') {{
      const h = document.createElement('div');
      h.className = 'dir';
      const label = document.createElement('div');
      label.textContent = '\\uD83D\\uDCC1 ' + e.node.name;
      label.style.cssText = 'font-size:0.82rem;color:#7d8494;margin:6px 0 2px;';
      h.appendChild(label);
      renderTree(e.node, h, prefix);
      container.appendChild(h);
      continue;
    }}
    const f = e.node;
    const a = document.createElement('a');
    const text = DATA.files[f.path] || '';
    let tags = '';
    if (text.includes('MinerU-extracted table')) tags += '<span class="tag tag-mineru" title="Contains a MinerU-extracted table">\\u2699</span>';
    if (text.includes('TABLE EXTRACTION FAILED')) tags += '<span class="tag tag-fail" title="Contains a failed table extraction">\\u26A0</span>';
    if (text.includes('recovered via') && text.includes('fallback')) tags += '<span class="tag tag-fallback" title="Contains a table recovered via the snapshot-fallback path \\u2014 worth double-checking placement/content">\\uD83D\\uDD0E</span>';
    if (text.includes('POSSIBLE CONTENT GAP')) tags += '<span class="tag tag-gap" title="Test 2 found content in the source PDF missing from this section">\\uD83D\\uDCDD</span>';
    a.innerHTML = tags + '<span class="fname">' + f.name + '</span>';
    a.dataset.path = f.path; a.href = '#' + f.path;
    a.onclick = () => showFile(f.path);
    container.appendChild(a);
  }}
}}

function annotate(container) {{
  const kids = Array.from(container.children);
  for (let i = 0; i < kids.length; i++) {{
    const el = kids[i];
    if (el.tagName === 'P' && el.textContent.trim().startsWith('\\u2699 MinerU-extracted table')) {{
      const next = kids[i+1];
      const wrapper = document.createElement('div');
      wrapper.className = 'mineru-block';
      el.replaceWith(wrapper);
      wrapper.appendChild(el);
      if (next && next.tagName === 'TABLE') wrapper.appendChild(next);
    }} else if (el.tagName === 'BLOCKQUOTE' && el.textContent.includes('TABLE EXTRACTION FAILED')) {{
      el.classList.add('failed-block');
    }} else if (el.tagName === 'BLOCKQUOTE' && el.textContent.includes('POSSIBLE CONTENT GAP')) {{
      el.classList.add('gap-marker-block');
    }}
  }}
}}

// The source pages for a section come from the file's OWN `*Source: `x.pdf`, page
// A–B*` breadcrumb — the same line every check uses to know which PDF pages a file
// is answerable for. Reading it here means "which section are we looking at" can
// never disagree with what the validators compared against. Once a section is
// opened, Prev/Next browse the WHOLE source PDF freely — the breadcrumb only picks
// where the view starts.
let pdfTotalPages = null;   // filled from DATA.total_pages once the tree loads
// Which pipeline stage the Document tab is showing. Stage 4 (AI post-processing) only
// exists for jobs that have been through it, so the switcher is hidden otherwise.
// null on first load = "whichever stage this job actually finished at", decided by
// the server from what is on disk. Hardcoding 3 here made the first fetch ask for
// the pre-AI tree on every job, so a document that had been through Stage 4 and 5
// still opened on Stage 3 and the sub-chunks were never what anyone saw.
let CUR_STAGE = null;
const STAGE_LABEL = {{1: 'Stage 1 — extract', 3: 'Stage 3 — combined',
                      4: 'Stage 4 — AI', 5: 'Stage 5 — sub-chunks (final)'}};

function renderStageSwitch(available) {{
  const el = document.getElementById('stage-switch');
  if (!el) return;
  if (available.length < 2) {{ el.textContent = '(' + (STAGE_LABEL[CUR_STAGE] || '') + ')'; return; }}
  el.innerHTML = available.map(s =>
    `<button class="stage-btn${{s === CUR_STAGE ? ' on' : ''}}" data-stage="${{s}}">${{STAGE_LABEL[s]}}</button>`
  ).join('');
  el.querySelectorAll('.stage-btn').forEach(b => b.onclick = () => {{
    const s = Number(b.dataset.stage);
    if (s === CUR_STAGE) return;
    CUR_STAGE = s;
    load();          // re-fetch the tree for the chosen stage and repaint
  }});
}}
let pdfCurrentPage = null;

function sourceRangeOf(text) {{
  const m = (text || '').match(/^\\*Source:.*?page\\s+(\\d+)(?:\\s*[\\u2013-]\\s*(\\d+))?\\*/m);
  if (!m) return null;
  const a = parseInt(m[1], 10);
  const b = m[2] ? parseInt(m[2], 10) : a;
  return (a >= 1 && b >= a) ? [a, b] : null;
}}

function setDocPdfPage(p) {{
  const lo = 1, hi = pdfTotalPages || Infinity;
  p = Math.max(lo, Math.min(hi, p || 1));
  pdfCurrentPage = p;
  const body = document.getElementById('doc-pdf-body');
  body.innerHTML = `<div class="pdf-pno">page ${{p}}</div>` +
    `<img src="/api/jobs/${{JOB_ID}}/page_image/${{p}}" alt="page ${{p}}" ` +
    `onerror="this.replaceWith(Object.assign(document.createElement('div'),` +
    `{{className:'pdf-empty',textContent:'page ${{p}} could not be rendered'}}))">`;
  const pos = document.getElementById('doc-pdf-pos');
  pos.textContent = pdfTotalPages ? `page ${{p}} of ${{pdfTotalPages}}` : `page ${{p}}`;
  document.getElementById('doc-pdf-prev').disabled = p <= lo;
  document.getElementById('doc-pdf-next').disabled = pdfTotalPages ? p >= hi : false;
  const goto = document.getElementById('doc-pdf-goto');
  goto.value = p;
  if (pdfTotalPages) goto.max = pdfTotalPages;
}}

function renderDocPdf(text) {{
  const label = document.getElementById('doc-pdf-label');
  const range = sourceRangeOf(text);
  if (!range) {{
    label.textContent = 'source pages';
    // No breadcrumb to anchor on — leave the viewer where it was rather than
    // resetting to page 1, so browsing stays smooth across sections that don't
    // carry one (a synthetic overview node, for instance).
    if (pdfCurrentPage === null) setDocPdfPage(1);
    return;
  }}
  const [first, last] = range;
  label.textContent = first === last ? `page ${{first}}` : `pages ${{first}}\\u2013${{last}}`;
  setDocPdfPage(first);
}}

// Duplicated content, marked in place so it is visible while READING the extraction
// rather than only as a `uniqueness` number. A region that repeats its own long cells
// means two representations of the same table were mixed — the failure that took Saudi
// Arabia's uniqueness from 86.9 to 0.0 while its coverage went UP, because duplicated
// text counts as covered.
//
// The length floor is what makes this usable: a questionnaire repeats "No." and "Yes"
// on every row, so only cells of real length are candidates. 40 normalised characters
// is about a sentence.
const DUP_MIN_CHARS = 40;

function markDuplicates(container) {{
  const norm = t => (t || '').replace(/[^a-z0-9]/gi, '').toLowerCase();
  const nodes = Array.from(container.querySelectorAll('td, th, p, li'));
  const seen = new Map();
  nodes.forEach(n => {{
    const k = norm(n.textContent);
    if (k.length >= DUP_MIN_CHARS) seen.set(k, (seen.get(k) || 0) + 1);
  }});
  let hits = 0;
  const groups = new Map();
  nodes.forEach(n => {{
    const k = norm(n.textContent);
    if (k.length < DUP_MIN_CHARS || (seen.get(k) || 0) < 2) return;
    if (!groups.has(k)) groups.set(k, groups.size + 1);
    n.classList.add('dup-hit');
    n.dataset.dupGroup = groups.get(k);
    n.title = `Duplicated content \u2014 this text appears ${{seen.get(k)}} times in this section (group ${{groups.get(k)}})`;
    hits++;
  }});
  return {{ hits: hits, groups: groups.size }};
}}

function withDupBanner(container) {{
  const r = markDuplicates(container);
  if (!r.hits) return;
  const b = document.createElement('div');
  b.className = 'dup-banner';
  b.innerHTML = `\u26A0 ${{r.hits}} duplicated block(s) in ${{r.groups}} group(s) \u2014 highlighted below. `
              + `Repeated long text usually means one table was merged from two sources.`;
  container.insertBefore(b, container.firstChild);
}}

function showFile(path) {{
  document.querySelectorAll('#tree a').forEach(a => a.classList.toggle('active', a.dataset.path === path));
  const content = document.getElementById('content');
  const raw = DATA.files[path] || '*(empty)*';
  content.innerHTML = marked.parse(renderFootnotes(raw));
  annotate(content);
  withDupBanner(content);
  renderDocPdf(raw);
}}

function renderTables(tables) {{
  const panel = document.getElementById('tables-panel');
  panel.innerHTML = '';
  for (const t of tables) {{
    const div = document.createElement('div');
    div.className = t.ok ? 't-ok' : 't-fail';
    const detail = t.ok ? `${{t.rows}}x${{t.cols}}` : (t.reason || 'failed');
    const fallback = (t.source || '').startsWith('snapshot_fallback')
      ? ' <span class="fallback-note">\\uD83D\\uDD0E fallback</span>' : '';
    div.innerHTML = `${{t.table_id}} (p.${{t.pages.join('-')}}) — ${{detail}}${{fallback}}`;
    div.onclick = () => {{
      for (const [path, text] of Object.entries(DATA.files)) {{
        if (text.includes(t.table_id)) {{ showFile(path); break; }}
      }}
    }};
    panel.appendChild(div);
  }}
}}

function escapeHtml(s) {{
  // Quotes MUST be escaped too: this output is interpolated into HTML ATTRIBUTES
  // (data-*, title=…) as well as text nodes, and an unescaped quote there
  // terminates the attribute and silently breaks the element.
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}}

const GEO_BADGE_STYLE = {{
  confident: 'background:#123a1f;color:#3ddc76',
  uncertain: 'background:#2a2210;color:#f0a83c',
  missing: 'background:#3a1414;color:#ff6b6b',
}};
function geoBadge(t) {{
  if (t.match_method !== 'geometric_iou' || !t.match_status) return '';
  const style = GEO_BADGE_STYLE[t.match_status] || '';
  const pct = t.match_iou != null ? ` ${{Math.round(t.match_iou * 100)}}%` : '';
  const title = t.match_note ? ` title="${{escapeHtml(t.match_note)}}"` : '';
  return ` <span class="insp-badge" style="${{style}}"${{title}}>&#128295; ${{t.match_status}}${{pct}}</span>`;
}}

let inspLoaded = false, valLoaded = false, prLoaded = false, scoreLoaded = false;
let aiLoaded = false;
function setTab(tab) {{
  document.getElementById('score-view').style.display = tab === 'score' ? '' : 'none';
  document.getElementById('doc-view').style.display = tab === 'doc' ? '' : 'none';
  document.getElementById('inspector-view').style.display = tab === 'insp' ? '' : 'none';
  document.getElementById('validation-view').style.display = tab === 'val' ? '' : 'none';
  document.getElementById('ai-view').style.display = tab === 'ai' ? '' : 'none';
  document.getElementById('pagereview-view').style.display = tab === 'pr' ? '' : 'none';
  document.getElementById('tab-score').classList.toggle('active', tab === 'score');
  document.getElementById('tab-doc').classList.toggle('active', tab === 'doc');
  document.getElementById('tab-insp').classList.toggle('active', tab === 'insp');
  document.getElementById('tab-val').classList.toggle('active', tab === 'val');
  document.getElementById('tab-ai').classList.toggle('active', tab === 'ai');
  document.getElementById('tab-pr').classList.toggle('active', tab === 'pr');
  if (tab === 'score' && !scoreLoaded) {{ scoreLoaded = true; loadScorecard(); }}
  if (tab === 'insp' && !inspLoaded) {{ inspLoaded = true; loadInspector(); }}
  if (tab === 'val' && !valLoaded) {{ valLoaded = true; loadValidation(); }}
  // Always reload: the checks run against the trees on disk, so a Stage 4 re-run should
  // be one refresh away rather than needing the page reopened.
  if (tab === 'ai') {{ aiLoaded = true; loadStage4(); }}
  if (tab === 'pr' && !prLoaded) {{ prLoaded = true; loadPageReview(); }}
}}
document.addEventListener('keydown', e => {{
  if (e.key !== 'Escape') return;
  const el = document.activeElement;
  if (el && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) return;
  window.location.href = '/';
}});
document.getElementById('tab-score').onclick = () => setTab('score');
document.getElementById('tab-doc').onclick = () => setTab('doc');
document.getElementById('doc-pdf-toggle').onclick = () => {{
  const split = document.getElementById('doc-split');
  const hidden = split.classList.toggle('collapsed');
  document.getElementById('doc-pdf-toggle').textContent = hidden ? 'show' : 'hide';
}};
document.getElementById('pr-pdf-prev').onclick = () => stepPRPdf(-1);
document.getElementById('pr-pdf-next').onclick = () => stepPRPdf(1);
document.getElementById('pr-pdf-goto').onchange = (e) => setPRPdfPage(parseInt(e.target.value, 10));
document.getElementById('doc-pdf-prev').onclick = () => setDocPdfPage(pdfCurrentPage - 1);
document.getElementById('doc-pdf-next').onclick = () => setDocPdfPage(pdfCurrentPage + 1);
document.getElementById('doc-pdf-goto').addEventListener('change', (e) => {{
  const v = parseInt(e.target.value, 10);
  if (v) setDocPdfPage(v);
}});
// Left/right browse the source PDF while the Document tab is open — skipped while
// typing anywhere (the goto box included) so arrow keys still move a text cursor.
document.addEventListener('keydown', (e) => {{
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  const step = e.key === 'ArrowLeft' ? -1 : 1;
  // Page Review takes the arrows when it is the open tab, on the same terms as the
  // Document tab: browsing the source PDF is the point of both panes.
  if (document.getElementById('pagereview-view').style.display !== 'none') {{
    if (prPdfPage !== null) {{ stepPRPdf(step); e.preventDefault(); }}
    return;
  }}
  if (document.getElementById('doc-view').style.display === 'none') return;
  if (document.getElementById('doc-split').classList.contains('collapsed')) return;
  setDocPdfPage(pdfCurrentPage + step);
  e.preventDefault();
}});
document.getElementById('tab-insp').onclick = () => setTab('insp');
document.getElementById('tab-val').onclick = () => setTab('val');
document.getElementById('tab-ai').onclick = () => setTab('ai');

// ---- Stage 4 -- AI post-processing ------------------------------------------
// Two questions, side by side and never merged: what did the AI change, and what do the
// conservation checks say is wrong with it. The score is integrity ONLY -- it credits no
// structural work, because a score that mixed the two would let a well-restructured
// section hide a lost cell.
async function loadStage4() {{
  const body = document.getElementById('ai-body');
  body.innerHTML = 'Loading&hellip;';
  let d;
  try {{
    const r = await fetch(`/api/jobs/${{JOB_ID}}/stage4.json`);
    d = await r.json();
  }} catch (e) {{ body.innerHTML = '<p class="muted">could not load stage 4 data</p>'; return; }}
  if (!d.ran) {{
    const off = d.enabled === false;
    body.innerHTML = `<div class="panel"><h4>${{off ? 'Stage 4 disabled' : 'Stage 4 not run'}}</h4>
      <div class="panel-sub">${{escapeHtml(d.reason || '')}}</div>
      ${{off ? `<div class="val-clean" style="margin-top:12px;color:#f0c33c;background:#141721;border-color:#262b36">
        Nothing was sent to Bedrock and nothing was spent for this document. Stages 4 and 5
        are opt-in per host \u2014 set <code>ACI_STAGE4_AI_ENABLED=1</code> to allow them.</div>` : ''}}
    </div>`;
    return;
  }}
  const u = d.usage || {{}};
  const bad = d.missing_total + d.moved_total;
  const col = d.score >= 90 ? '#4ac97e' : (d.score >= 70 ? '#f0c33c' : '#ff6b6b');

  const fmtS = s => (s >= 60 ? `${{Math.floor(s/60)}}m ${{Math.round(s%60)}}s` : `${{(s||0).toFixed(1)}}s`);
  // One row per section: which model actually touched it and what that call cost. Older
  // sections from before this was tracked (or ones a model call never reached, like an
  // empty shell that was skipped) show em-dashes rather than a fabricated number.
  const sectionRows = (d.sections||[]).map(s => `
    <tr>
      <td>${{escapeHtml(s.file||'')}}</td>
      <td>${{escapeHtml(s.kind||'—')}}</td>
      <td><code>${{escapeHtml(s.model||'—')}}</code></td>
      <td>${{s.tokens_in!=null ? (s.tokens_in+s.tokens_out).toLocaleString() : '—'}}</td>
      <td>${{s.cost_usd!=null ? '$'+s.cost_usd.toFixed(4) : '—'}}</td>
      <td>${{s.ok ? '✓' : '✗'}}</td>
    </tr>`).join('');
  let h = `<div class="panel"><h4>This run</h4>
    <div class="panel-sub">What the AI stages cost in time and money, next to what stages 1-3
      cost. Both AI stages are opt-in per host and priced separately, so they are not folded
      into the extraction total.</div>
    <table class="ai-tbl"><tbody>
      <tr><td>model</td><td><code>${{escapeHtml(d.model || '\u2014')}}</code> &middot; mode ${{escapeHtml(d.mode || '\u2014')}}</td></tr>
      <tr><td>stage 4 &middot; AI</td><td>${{fmtS(d.seconds)}} &middot; ${{(u.total_tokens||0).toLocaleString()}} tokens &middot; <b>$${{(u.cost_usd||0).toFixed(4)}}</b></td></tr>
      <tr><td>stage 5 &middot; sub-chunks</td><td>${{fmtS(d.stage5_seconds)}} &middot; ${{d.sections_split||0}} section(s) split into ${{d.subchunks||0}} sub-chunks &middot; no model, no cost</td></tr>
      <tr><td>AI stages total</td><td>${{fmtS(d.ai_total_seconds)}} &middot; $${{(u.cost_usd||0).toFixed(4)}}</td></tr>
      <tr><td class="muted">stages 1-3 (extraction)</td><td class="muted">${{fmtS(d.extraction_seconds)}} &middot; local, no cost</td></tr>
    </tbody></table></div>

  <div class="panel"><h4>Cost by section</h4>
    <div class="panel-sub">Each section's own model and spend. Sections run concurrently and a
      table section can go to a different model than a text section, so the run-level total
      above is a sum across whatever is in this table, not one uniform rate.</div>
    <table class="ai-tbl"><thead><tr><th>section</th><th>kind</th><th>model</th><th>tokens</th><th>cost</th><th>ok</th></tr></thead>
    <tbody>${{sectionRows || '<tr><td colspan="6" class="muted">no sections recorded</td></tr>'}}</tbody></table>
  </div>

  <div class="panel"><h4>Integrity score</h4>
    <div class="panel-sub">100 &minus; ${{'12'}}&times;(cells missing from the whole document)
      &minus; ${{'5'}}&times;(cells that changed column role). Structural work earns no credit here
      &mdash; this measures only whether the output is safe to keep.</div>
    <div style="display:flex;gap:26px;align-items:baseline;margin-top:12px">
      <div style="font-size:2.6rem;font-weight:700;color:${{col}}">${{d.score}}</div>
      <div class="panel-sub" style="margin:0">
        <b>${{d.missing_total}}</b> cell(s) missing document-wide &middot;
        <b>${{d.moved_total}}</b> cell(s) changed column &middot;
        <b>${{d.headings_added}}</b> sub-headings created<br>
        <span style="color:#9aa0ac">see &ldquo;This run&rdquo; above for model, time and cost</span>
      </div>
    </div>
    ${{bad === 0 ? '<div class="val-clean" style="margin-top:14px">No conservation failures &mdash; every cell is still somewhere in the document, and none changed column.</div>' : ''}}
  </div>`;

  const P = d.pipeline || {{}};
  const ai = P.ai||{{}}, det = P.detect||{{}}, rep = P.repair||{{}}, rec = P.recheck||{{}};
  const step = (name, sub, body, tone) =>
    `<div class="flow-step flow-${{tone}}"><div class="flow-name">${{name}}</div>
     <div class="flow-sub">${{sub}}</div><div class="flow-body">${{body}}</div></div>`;
  h += `<div class="panel"><h4>Pipeline</h4>
    <div class="panel-sub">Each stage and what it changed. Stage 3 is never edited, so every
      step below is reversible by deleting one directory.</div>
    <div class="flow">
      ${{step('Stage 3', 'deterministic input', `${{ai.tables_before||0}} tables<br>${{ai.rows_before||0}} rows`, 'plain')}}
      <div class="flow-arr">&rarr;</div>
      ${{step('AI', 'section mode &middot; Haiku', `${{ai.accepted||0}}/${{ai.sections||0}} sections<br>${{ai.tables_after||0}} tables, ${{ai.rows_after||0}} rows<br>+${{ai.headings||0}} sub-headings`, 'ai')}}
      <div class="flow-arr">&rarr;</div>
      ${{step('Detect', 'check_column_roles', `${{det.moved||0}} column moves<br>${{det.missing||0}} missing cells`, (det.moved||det.missing) ? 'warn' : 'ok')}}
      <div class="flow-arr">&rarr;</div>
      ${{step('Repair', 'deterministic move', rep.ran ? `${{rep.repaired||0}} moved back<br>${{rep.declined||0}} declined` : 'not run', rep.repaired ? 'ok' : 'plain')}}
      <div class="flow-arr">&rarr;</div>
      ${{step('Re-check', 'same check, after repair', `${{rec.remaining_moved||0}} moves left<br>${{rec.remaining_missing||0}} missing left`, (rec.remaining_moved||rec.remaining_missing) ? 'warn' : 'ok')}}
    </div>`;

  if (rep.ran) {{
    h += `<div style="margin-top:16px"><b>Repaired</b> &mdash; text moved back to the column it
      came from. Each was verified to be a pure move: the document's characters are unchanged,
      only their cell differs. A repair that would have altered content was rolled back.</div>`;
    if (!(rep.applied||[]).length) h += `<div class="panel-sub">none</div>`;
    for (const r of rep.applied||[]) h += `<div class="ai-find ai-find-ok">
      <span class="ai-tag">MOVED BACK</span> <span class="muted">${{escapeHtml(r.file||'')}} &middot;
      answer &rarr; question</span><div class="ai-txt">${{escapeHtml(r.text||'')}}&hellip;</div></div>`;
    h += `<div style="margin-top:16px"><b>Declined</b> &mdash; detected, not repaired. There is no
      unambiguous destination for these, and guessing would turn a visible defect into a silent
      one. They stay flagged below.</div>`;
    const byWhy = {{}};
    for (const dcl of rep.declined_detail||[]) byWhy[dcl.why] = (byWhy[dcl.why]||0) + 1;
    for (const k in byWhy) h += `<div class="ai-find ai-find-move">
      <span class="ai-tag">DECLINED &times;${{byWhy[k]}}</span> <span class="muted">${{escapeHtml(k)}}</span></div>`;
  }}
  h += `</div>`;

  h += `<div class="panel"><h4>What Stage 4 did</h4>
    <div class="panel-sub">Per section. "cells" is the character count of all table cell text;
      a drop is only a loss if the checks below say the text is nowhere else in the document.</div>
    <table class="ai-tbl"><thead><tr><th>section</th><th>tables</th><th>rows</th>
      <th>sub-headings</th><th>cell chars</th><th>findings</th></tr></thead><tbody>`;
  for (const s of d.sections) {{
    const n = (s.missing||[]).length + (s.moved||[]).length;
    const flag = n ? `<span class="ai-flag">${{n}}</span>` : '<span class="muted">&mdash;</span>';
    const delta = (s.cell_chars_lost || 0) ? ` <span class="muted">(&minus;${{s.cell_chars_lost}})</span>` : '';
    h += `<tr><td><a href="#${{escapeHtml(s.file)}}" onclick="setTab('doc');showFile('${{escapeHtml(s.file)}}')">${{escapeHtml(s.file)}}</a></td>
      <td>${{s.tables_before}} &rarr; ${{s.tables_after}}</td>
      <td>${{s.rows_before}} &rarr; ${{s.rows_after}}</td>
      <td>${{s.headings_added || 0}}</td>
      <td>${{s.cell_chars_before}} &rarr; ${{s.cell_chars_after}}${{delta}}</td>
      <td>${{flag}}</td></tr>`;
  }}
  h += `</tbody></table></div>`;

  if (bad) {{
    h += `<div class="panel"><h4>Validation findings &mdash; ${{bad}}</h4>
      <div class="panel-sub"><b>Missing</b>: cell text that is in Stage 3 and in no Stage 4 file
      anywhere. Checked across the WHOLE tree, so a row moved out of the wrong chunk into its
      own is not a loss. <b>Moved column</b>: text that survived but changed side of the table
      &mdash; question text sitting in an answer cell. Nothing is added or removed by this, which
      is why no content check can see it.</div>`;
    for (const s of d.sections) {{
      const ms = s.missing || [], mv = s.moved || [];
      if (!ms.length && !mv.length) continue;
      h += `<div style="margin-top:16px"><b>${{escapeHtml(s.file)}}</b>`;
      // WHICH sub-section, first thing after the tag. "09-marketing-activities.md, 5
      // missing" is 227 cells and twenty pages to search; "8.5 Internet/Social Media"
      // is one row. `likely` comes with it because a split and a deletion want very
      // different amounts of attention.
      for (const m of ms) h += `<div class="ai-find ai-find-miss">
        <span class="ai-tag">MISSING</span>${{m.subsection
          ? ` <b class="ai-sub">${{escapeHtml(m.subsection)}}</b>` : ''}}
        <span class="muted">${{m.role}} cell, ${{m.chars}} chars${{
          m.likely ? ' &middot; likely ' + m.likely : ''}}${{
          m.windows ? ` (${{m.windows_found}}/${{m.windows}} fragments still present)` : ''}}</span>
        <div class="ai-txt">${{escapeHtml(m.text)}}&hellip;</div></div>`;
      for (const m of mv) h += `<div class="ai-find ai-find-move">
        <span class="ai-tag">MOVED</span>${{m.subsection
          ? ` <b class="ai-sub">${{escapeHtml(m.subsection)}}</b>` : ''}}
        <span class="muted">${{m.from_role}} &rarr; ${{m.to_role}}, ${{m.chars}} chars</span>
        <div class="ai-txt">${{escapeHtml(m.text)}}&hellip;</div></div>`;
      h += `</div>`;
    }}
    h += `</div>`;
  }}
  body.innerHTML = h;
}}
document.getElementById('tab-pr').onclick = () => setTab('pr');

function jumpToFile(path) {{
  setTab('doc');
  if (DATA && DATA.files[path] !== undefined) showFile(path);
}}

function wordChips(obj, limit) {{
  const entries = Object.entries(obj).slice(0, limit);
  if (!entries.length) return '<span style="color:#7d8494">none</span>';
  return '<div class="val-words">' + entries.map(([w, c]) =>
    `<span><span class="n">${{c}}&times;</span>${{escapeHtml(w)}}</span>`).join('') + '</div>';
}}

function highlightSnippet(before, tokens, after, max) {{
  const shown = tokens.slice(0, max).join(' ') + (tokens.length > max ? ' \\u2026' : '');
  return `\\u2026${{escapeHtml(before)}} <mark>${{escapeHtml(shown)}}</mark> ${{escapeHtml(after)}}\\u2026`;
}}

async function loadValidation() {{
  const body = document.getElementById('validation-body');
  const r = await fetch(`/api/jobs/${{JOB_ID}}/validation.json?view=${{SC_VIEW}}`);
  const v = await r.json();
  if (!v || !v.word_coverage) {{ body.innerHTML = '<p style="color:#9aa0ac">No validation data for this job.</p>'; return; }}

  const wc = v.word_coverage, cl = v.content_localized, ni = v.numeric_integrity;
  const tp = v.table_placement || {{}};
  const placementFlags = tp.skipped ? [] : (tp.flags || []);
  const silentResults = (cl.results || []).filter(x => !x.acknowledged && x.dropped.length);
  const ackResults = (cl.results || []).filter(x => x.acknowledged && x.dropped.length);
  const transpositions = ni.transpositions || [];
  const silentSpanCount = silentResults.reduce((n, x) => n + x.dropped.length, 0);
  const issueCount = transpositions.length + silentSpanCount + placementFlags.length;

  // ---- banner ----
  let html = '';
  if (issueCount === 0) {{
    html += `<div id="val-banner" class="ok"><div class="val-banner-icon">&#10003;</div><div>`
          + `<div class="val-banner-title">No issues found</div>`
          + `<div class="val-banner-sub">${{wc.coverage_adjusted_pct.toFixed(1)}}% word coverage &middot; 0 content gaps &middot; 0 number transpositions &middot; 0 placement flags</div>`
          + `</div></div>`;
  }} else {{
    const parts = [];
    if (transpositions.length) parts.push(`${{transpositions.length}} possible number error(s)`);
    if (silentSpanCount) parts.push(`${{silentSpanCount}} content gap(s) across ${{silentResults.length}} file(s)`);
    if (placementFlags.length) parts.push(`${{placementFlags.length}} table(s) with uncertain section placement`);
    html += `<div id="val-banner" class="bad"><div class="val-banner-icon">&#9888;</div><div>`
          + `<div class="val-banner-title">${{issueCount}} issue(s) need review</div>`
          + `<div class="val-banner-sub">${{parts.join(' &middot; ')}} &mdash; ${{wc.coverage_adjusted_pct.toFixed(1)}}% word coverage overall</div>`
          + `</div></div>`;
  }}

  // ---- issues to review: placement first (structural), then numeric transpositions, then content gaps ----
  html += '<div id="val-issues">';
  if (issueCount > 0) {{
    html += '<div class="val-heading">Issues to review</div>';
    for (const f of placementFlags) {{
      html += `<div class="val-issue val-issue-red"><div class="val-issue-head">`
            + `<span class="val-issue-tag">PLACEMENT</span>`
            + `<span class="val-issue-title">${{escapeHtml(f.table_id)}}</span>`
            + `<span class="val-issue-pages">page ${{f.pages.join(', ')}}</span></div>`
            + `<div class="val-issue-body">${{escapeHtml(f.detail)}}</div></div>`;
    }}
    for (const t of transpositions) {{
      html += `<div class="val-issue val-issue-red"><div class="val-issue-head">`
            + `<span class="val-issue-tag">NUMBER</span>`
            + `<span class="val-issue-title">${{escapeHtml(t.missing)}} &rarr; ${{escapeHtml(t.extra)}}</span>`
            + `<span class="val-issue-pages">page ${{t.missing_pages.join(', ')}}</span></div>`
            + `<div class="val-issue-body">"${{escapeHtml(t.missing)}}" is missing from the tree on the page(s) above, but "${{escapeHtml(t.extra)}}" `
            + `appears instead with the same digits &mdash; looks like a possible digit transposition. Verify by hand.</div></div>`;
    }}
    for (const res of silentResults) {{
      for (const d of res.dropped) {{
        html += `<div class="val-issue val-issue-amber clickable" onclick="jumpToFile(${{JSON.stringify(res.file)}})">`
              + `<div class="val-issue-head"><span class="val-issue-tag">GAP</span>`
              + `<span class="val-issue-title">${{escapeHtml(res.file.split('/').pop())}}</span>`
              + `<span class="val-issue-pages">pages ${{res.pages[0]}}-${{res.pages[1]}} &middot; ${{d.tokens.length}} token(s) dropped</span>`
              + `<span class="val-issue-link">View in document &rarr;</span></div>`
              + `<div class="val-issue-snippet">${{highlightSnippet(d.before, d.tokens, d.after, 24)}}</div></div>`;
      }}
    }}
  }} else {{
    html += '<div class="val-clean">&#10003; Word coverage, section-by-section content, number integrity, and table placement all check out.</div>';
  }}
  html += '</div>';

  // ---- acknowledged gaps: already flagged by a TABLE EXTRACTION FAILED/PENDING marker ----
  if (ackResults.length) {{
    html += `<details class="val-advanced"><summary>Acknowledged gaps (already flagged in the document) &mdash; ${{ackResults.length}} file(s)</summary>`
          + '<div class="val-advanced-body">';
    for (const res of ackResults) {{
      html += `<div class="val-ack-item">&#9989; <a onclick="jumpToFile(${{JSON.stringify(res.file)}})">${{escapeHtml(res.file)}}</a> `
            + `<span style="color:#7d8494">pages ${{res.pages[0]}}-${{res.pages[1]}}, ${{res.dropped.length}} span(s) &mdash; already covered by a failed/pending table marker</span></div>`;
    }}
    html += '</div></details>';
  }}

  // ---- advanced: word-level coverage detail ----
  html += '<details class="val-advanced"><summary>Word-level coverage detail (Test 1)</summary><div class="val-advanced-body">';
  html += '<div class="val-stat-row">'
        + `<span class="val-stat"><b>${{wc.pdf_word_occurrences}}</b> pdf words</span>`
        + `<span class="val-stat"><b>${{wc.md_word_occurrences}}</b> tree words</span>`;
  if (wc.boilerplate_lines && wc.boilerplate_lines.length) {{
    html += `<span class="val-stat"><b>${{wc.boilerplate_word_occurrences}}</b> excluded as recurring header/footer</span>`;
  }}
  html += '</div>';
  html += `<div style="margin-bottom:8px" class="val-stat"><b>Missing</b> &mdash; in PDF, not in tree:</div>${{wordChips(wc.missing_words_real, 30)}}`;
  html += `<div style="margin:14px 0 8px" class="val-stat"><b>Extra</b> &mdash; in tree, not in PDF (possible duplication):</div>${{wordChips(wc.extra_words, 30)}}`;
  html += '</div></details>';

  // ---- advanced: all numeric findings ----
  const missingNums = Object.entries(ni.missing || {{}});
  html += '<details class="val-advanced"><summary>All numeric findings (Test 3)</summary><div class="val-advanced-body">';
  html += `<div class="val-stat-row"><span class="val-stat"><b>${{ni.pdf_numeric_total}}</b> numeric tokens in PDF</span><span class="val-stat"><b>${{ni.md_numeric_total}}</b> in tree</span></div>`;
  if (missingNums.length) {{
    html += '<div style="margin-bottom:8px" class="val-stat"><b>Missing numbers</b> (with page hints):</div><div class="val-words">'
          + missingNums.slice(0, 30).map(([w, info]) => `<span><span class="n">${{info.count}}&times;</span>${{escapeHtml(w)}} <span style="color:#7d8494">p.${{info.pages.slice(0,5).join(',')}}</span></span>`).join('')
          + '</div>';
  }} else {{
    html += '<div class="val-stat">no missing numbers</div>';
  }}
  html += '</div></details>';

  // ---- advanced: relocated/reworded (Test 2 supporting detail) ----
  html += '<details class="val-advanced"><summary>Relocated &amp; reworded spans (Test 2 supporting detail)</summary><div class="val-advanced-body">';
  html += `<p class="val-stat">${{cl.files_scanned}} files scanned. ${{cl.total_relocated_spans}} span(s) found relocated elsewhere in the same/sibling file `
        + `(e.g. a footnote definition moved from its inline reference) &mdash; not a real loss. ${{cl.total_changed_spans}} span(s) reworded/reordered.</p>`;
  html += '</div></details>';

  body.innerHTML = html;
}}

// Every kind check_scorecard._collect_findings can emit, so nothing arrives here
// unlabelled. The page review used to know four of these and silently showed the
// bare source name for the rest -- which it never saw anyway, because it only read
// three checks. Red = content is missing or wrong; amber = flagged, advisory, or a
// "look closer" hint.
const PR_KIND_LABEL = {{
  gap: 'CONTENT GAP', gap_acknowledged: 'GAP (FLAGGED)',
  missing_number: 'NUMBER', missing_word: 'WORD',
  hollow: 'EMPTY SECTION', section_missing: 'SECTION NOT BUILT', build_rule: 'BUILD RULE',
  found_elsewhere: 'FILED ELSEWHERE', text_absent: 'TEXT ABSENT',
  table: 'TABLE FAILED', table_lost: 'TABLE LOST', placement: 'TABLE MISPLACED',
  hierarchy: 'WRONG PARENT', unreadable: 'UNREADABLE PAGE',
  meaning: 'MEANING CHANGED', negation_shortfall: 'NEGATION COUNT',
  number: 'NUMBER ABSENT', transposition: 'DIGITS TRANSPOSED',
  orphan_table: 'ORPHAN TABLE LOST', orphan_duplicate: 'ORPHAN (DUPLICATE)',
  orphan_recovered: 'ORPHAN RECOVERED',
  fn_dangling: 'FOOTNOTE NO BODY', fn_orphan: 'FOOTNOTE NO MARKER',
  fn_duplicate: 'FOOTNOTE DUPLICATE', fn_gap: 'FOOTNOTE MISSING',
  engine: 'PARSER DISAGREEMENT',
}};
const PR_KIND_CLASS = {{
  gap: 'val-issue-red', gap_acknowledged: 'val-issue-amber',
  missing_number: 'val-issue-red', missing_word: 'val-issue-amber',
  hollow: 'val-issue-red', section_missing: 'val-issue-red', build_rule: 'val-issue-amber',
  found_elsewhere: 'val-issue-amber', text_absent: 'val-issue-red',
  table: 'val-issue-amber', table_lost: 'val-issue-red', placement: 'val-issue-red',
  hierarchy: 'val-issue-red', unreadable: 'val-issue-red',
  meaning: 'val-issue-red', negation_shortfall: 'val-issue-amber',
  number: 'val-issue-amber', transposition: 'val-issue-red',
  orphan_table: 'val-issue-red', orphan_duplicate: 'val-issue-amber',
  orphan_recovered: 'val-issue-amber',
  fn_dangling: 'val-issue-amber', fn_orphan: 'val-issue-amber',
  fn_duplicate: 'val-issue-amber', fn_gap: 'val-issue-amber',
  engine: 'val-issue-amber',
}};

let pageIssuesData = {{}}, prFileRanges = [];

function buildNestedNav(fileRanges, issuePagesSet) {{
  const root = {{}};
  for (const fr of fileRanges) {{
    const pages = [];
    for (let p = fr.pages[0]; p <= fr.pages[1]; p++) if (issuePagesSet.has(p)) pages.push(p);
    if (!pages.length) continue;
    const parts = fr.file.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {{
      node.dirs = node.dirs || {{}};
      node.dirs[parts[i]] = node.dirs[parts[i]] || {{}};
      node = node.dirs[parts[i]];
    }}
    node.files = node.files || [];
    node.files.push({{ name: parts[parts.length - 1], path: fr.file, pages }});
  }}
  return root;
}}

function renderNav(node, container) {{
  for (const [dirName, sub] of Object.entries(node.dirs || {{}})) {{
    const wrap = document.createElement('div'); wrap.className = 'pr-dir';
    const label = document.createElement('div'); label.className = 'pr-dirname'; label.textContent = '\\uD83D\\uDCC1 ' + dirName;
    wrap.appendChild(label);
    renderNav(sub, wrap);
    container.appendChild(wrap);
  }}
  for (const f of (node.files || [])) {{
    const fdiv = document.createElement('div'); fdiv.className = 'pr-file'; fdiv.textContent = f.name;
    container.appendChild(fdiv);
    for (const p of f.pages) {{
      const a = document.createElement('a'); a.className = 'pr-page-item';
      a.dataset.page = p; a.dataset.file = f.path || '';
      const count = (pageIssuesData[p] || []).length;
      a.innerHTML = `<span>Page ${{p}}</span><span class="pr-count">${{count}}</span>`;
      a.onclick = () => loadPRPage(p, f.path);
      container.appendChild(a);
    }}
  }}
}}

// The PDF page shown in Page Review, browsable independently of the issue list.
//
// The nav only lists pages something was FLAGGED on, so before this there was no
// way to look at the page before or after one — and that is exactly what you need
// to judge a gap at a section boundary, or to see where a table actually starts.
// The issue column keeps tracking whichever page is displayed, so stepping onto a
// clean page correctly says "no flagged issues" rather than going stale.
let prPdfPage = null;

function setPRPdfPage(p) {{
  const hi = pdfTotalPages || Infinity;
  p = Math.max(1, Math.min(hi, p || 1));
  prPdfPage = p;
  document.getElementById('pr-page-body').innerHTML =
    `<img src="/api/jobs/${{JOB_ID}}/page_image/${{p}}" alt="page ${{p}}" ` +
    `onerror="this.replaceWith(Object.assign(document.createElement('div'),` +
    `{{className:'pdf-empty',textContent:'page ${{p}} could not be rendered'}}))">`;
  const pos = document.getElementById('pr-pdf-pos');
  if (pos) pos.textContent = pdfTotalPages ? `${{p}} / ${{pdfTotalPages}}` : `page ${{p}}`;
  const prev = document.getElementById('pr-pdf-prev');
  const next = document.getElementById('pr-pdf-next');
  if (prev) prev.disabled = p <= 1;
  if (next) next.disabled = pdfTotalPages ? p >= hi : false;
  const goto = document.getElementById('pr-pdf-goto');
  if (goto) {{ goto.value = p; if (pdfTotalPages) goto.max = pdfTotalPages; }}
  renderPRIssues(p);
}}

function stepPRPdf(delta) {{ setPRPdfPage((prPdfPage || 1) + delta); }}

function renderPRIssues(pno) {{
  const issues = pageIssuesData[pno] || [];
  const col = document.getElementById('pr-issues-col');
  if (!col) return;
  if (!issues.length) {{
    col.innerHTML = `<div class="val-heading">Page ${{pno}}</div>`
      + '<p style="color:#9aa0ac; font-size:0.85rem;">No flagged issues on this page.</p>';
    return;
  }}
  // A finding is listed on every page of its range, so say which range — otherwise the
  // same issue re-read on page 5 of 4-10 looks like a second, identical one.
  const carried = issues.filter(i => i.anchor === false).length;
  let html = `<div class="val-heading">Page ${{pno}} &mdash; ${{issues.length}} issue(s)`
           + (carried ? `<span class="pr-carried">${{carried}} spanning this page</span>` : '')
           + `</div>`;
  for (const is of issues) {{
    const sp = is.span || [];
    const spans = sp.length === 2 && sp[1] > sp[0];
    html += `<div class="val-issue ${{PR_KIND_CLASS[is.kind] || 'val-issue-amber'}}"><div class="val-issue-head">`
          + `<span class="val-issue-tag">${{PR_KIND_LABEL[is.kind] || is.source}}</span>`
          + `<span class="val-issue-title">${{escapeHtml(is.text)}}</span></div>`
          + (spans ? `<div class="pr-span">pages ${{sp[0]}}&ndash;${{sp[1]}}`
                   + (is.anchor === false ? ` &middot; also on this page` : '')
                   + `</div>` : '')
          + `<div class="val-issue-snippet">${{escapeHtml(is.detail)}}</div></div>`;
  }}
  col.innerHTML = html;
}}

function loadPRPage(pno, file) {{
  document.querySelectorAll('#pr-nav .pr-page-item').forEach(a => {{
    const hit = Number(a.dataset.page) === pno && (!file || a.dataset.file === file);
    a.classList.toggle('active', hit);
    if (hit && !file) file = a.dataset.file;
  }});
  // Reached from the heat-map there is no file, so fall back to whichever
  // section's declared page range contains this page.
  if (!file) {{
    const hit = (prFileRanges || []).find(fr => pno >= fr.pages[0] && pno <= fr.pages[1]);
    file = hit && hit.file;
  }}
  const cbody = document.getElementById('pr-content-body');
  if (file && DATA && DATA.files[file] !== undefined) {{
    cbody.innerHTML = `<div style="color:#7d8494;font-size:0.74rem;margin-bottom:8px">${{escapeHtml(file)}}</div>`
                    + marked.parse(renderFootnotes(DATA.files[file]));
    annotate(cbody);
    withDupBanner(cbody);
  }} else {{
    cbody.innerHTML = '<span style="color:#7d8494">no extracted section maps to this page</span>';
  }}
  setPRPdfPage(pno);   // also renders this page's issue column
}}

async function loadPageReview() {{
  const nav = document.getElementById('pr-nav');
  // Same scorecard the dimension panel is showing, so the two can never describe
  // different documents.
  const r = await fetch(`/api/jobs/${{JOB_ID}}/page_issues.json?view=${{SC_VIEW}}`);
  const data = await r.json();
  // The file names in data.file_ranges come from whichever stage produced the score
  // being shown (data.scored_stage) -- NOT necessarily the stage DATA.files was last
  // built from. CUR_STAGE and SC_VIEW are two independently-switchable selections (a
  // manual Stage 1/3/4/5 switch on the Document tab, and the Scorecard 1/2 toggle),
  // and nothing kept them in sync -- so a document scored at stage 5 (sub-chunks) but
  // still showing stage 3 in the Document tab named files here that DATA.files did not
  // have, and every one of them read "no extracted section maps to this page" even
  // though the section genuinely existed, just under stage 5's tree. Loading the tree
  // the score actually describes is what "the page review matches the scorecard on
  // screen" already promised; this is what makes that promise hold for the FILES too,
  // not just the verdict.
  if (data.scored_stage != null && data.scored_stage !== CUR_STAGE) {{
    CUR_STAGE = data.scored_stage;
    await load();
  }}
  pageIssuesData = {{}};
  prFileRanges = data.file_ranges || [];
  for (const [pStr, issues] of Object.entries(data.page_issues || {{}})) pageIssuesData[Number(pStr)] = issues;
  const issuePages = new Set(Object.keys(pageIssuesData).map(Number));
  if (!issuePages.size) {{ nav.innerHTML = '<p style="color:#3ddc76; font-size:0.85rem;">&#10003; No findings on any page (stage ' + (data.scored_stage || 3) + ').</p>'; return; }}
  nav.innerHTML = '';
  renderNav(buildNestedNav(data.file_ranges || [], issuePages), nav);
  loadPRPage(Math.min(...issuePages));
}}

const GATE_TEXT = {{
  pass:    {{ icon: '\\u2713', title: 'PASS',   sub: 'Every dimension scored at or above the pass bar.' }},
  review:  {{ icon: '\\u26A0', title: 'REVIEW', sub: 'Usable, but at least one dimension needs a human look before you trust this extraction.' }},
  fail:    {{ icon: '\\u2715', title: 'FAIL',   sub: 'At least one dimension is bad enough that this extraction should not be used as-is.' }},
  unknown: {{ icon: '?',       title: 'UNKNOWN', sub: 'Not enough data to score this job.' }},
}};

function scoreClass(s, pass, review) {{
  if (s == null) return 's-na';
  return s >= pass ? 's-good' : (s >= review ? 's-warn' : 's-bad');
}}

function renderReview(sc, rv) {{
  // A reviewer's judgement, kept visibly SEPARATE from the measurement. The computed
  // gate is always shown next to an override, because an override that hides what the
  // tool said is worse than none: the disagreement is the useful part.
  const ov = (rv || {{}}).override;
  const notes = ((rv || {{}}).notes) || [];
  const computed = sc.gate;
  const differs = ov && ov.verdict && ov.verdict !== computed;
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : '';

  const banner = ov && ov.verdict ? `
    <div class="rv-set rv-${{ov.verdict}}">
      <div class="rv-set-top">Reviewer verdict: <b>${{ov.verdict.toUpperCase()}}</b>
        ${{differs ? `<span class="rv-differs">overrides the computed <b>${{escapeHtml(computed)}}</b></span>`
                   : `<span class="rv-agrees">agrees with the computed verdict</span>`}}</div>
      <div class="rv-reason">${{escapeHtml(ov.reason || '')}}</div>
      <div class="rv-meta">${{ov.author ? escapeHtml(ov.author) + ' · ' : ''}}${{when(ov.at)}}
        <button class="rv-btn" onclick="clearOverride()">clear override</button></div>
    </div>`
    : `<div class="rv-none">No reviewer verdict &mdash; this document shows the computed gate
        (<b>${{escapeHtml(computed)}}</b>) only.</div>`;

  const noteList = notes.length ? notes.map(n => `
    <div class="rv-note${{n.system ? ' rv-note-sys' : ''}}">
      <div class="rv-note-text">${{escapeHtml(n.text)}}</div>
      <div class="rv-meta">${{n.author ? escapeHtml(n.author) + ' · ' : ''}}${{when(n.at)}}
        ${{n.system ? '' : `<button class="rv-btn" onclick="delNote('${{n.id}}')">delete</button>`}}</div>
    </div>`).join('') : '<div class="rv-none">No notes yet.</div>';

  return `<div class="panel"><h4>Reviewer verdict &amp; notes
      <span class="dim-tag dim-tag-adv" title="Recorded alongside the computed score, never replacing it">human</span></h4>
    <div class="panel-sub">Your judgement on this document, stored against the source PDF so it survives
      re-extraction. The computed score is never overwritten &mdash; both are kept, so a later reader can
      see what the tool concluded and what a person decided.</div>
    ${{banner}}
    <div class="rv-form">
      <div class="rv-row">
        <select id="rv-verdict">
          <option value="pass">pass</option><option value="review">review</option><option value="fail">fail</option>
        </select>
        <input id="rv-author" placeholder="your name (optional)">
        <button class="rv-btn rv-primary" onclick="saveOverride()">set verdict</button>
      </div>
      <textarea id="rv-reason" rows="2" placeholder="Why? A reason is required &mdash; an override with no argument behind it tells the next reader nothing."></textarea>
      <span id="rv-err" class="rv-err"></span>
    </div>
    <div class="rv-sep">Notes</div>
    ${{noteList}}
    <div class="rv-form">
      <textarea id="rv-note" rows="2" placeholder="Add a note &mdash; e.g. 'the table on p12 is a graphic, not an extraction failure'"></textarea>
      <button class="rv-btn" onclick="saveNote()">add note</button>
    </div></div>`;
}}
// Redraw via loadScorecard(), NOT load(): load() guards the scorecard behind a
// `scoreLoaded` flag and so re-renders it exactly once per page load. Calling it after
// saving a note stored the note correctly and then repainted nothing, which reads as
// "the button does nothing". The dismiss/restore buttons already call loadScorecard()
// directly for this same reason.
async function rvPost(path, body) {{
  const r = await fetch(`/api/jobs/${{JOB_ID}}/review/${{path}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(body)}});
  if (!r.ok) {{
    const e = document.getElementById('rv-err');
    if (e) e.textContent = ((await r.json().catch(() => ({{}}))).detail) || 'could not save';
    return false;
  }}
  return true;
}}
async function saveOverride() {{
  const err = document.getElementById('rv-err'); if (err) err.textContent = '';
  const ok = await rvPost('override', {{
    verdict: document.getElementById('rv-verdict').value,
    reason:  document.getElementById('rv-reason').value,
    author:  document.getElementById('rv-author').value}});
  if (ok) loadScorecard();
}}
async function clearOverride() {{ if (await rvPost('override', {{clear: true}})) loadScorecard(); }}
async function saveNote() {{
  const t = document.getElementById('rv-note').value;
  if (!t.trim()) return;
  const a = document.getElementById('rv-author');
  if (await rvPost('note', {{text: t, author: a ? a.value : ''}})) loadScorecard();
}}
async function delNote(id) {{ if (await rvPost('note', {{delete_id: id}})) loadScorecard(); }}

// Label for the special-rule chip, keyed on the MODE not the page count: "{{n}}p rule"
// belongs to short_document, where the page count IS the rule. Reusing special_mode for
// the per-product rule made this read "88p rule" on an 88-page document. Duplicated from
// the corpus template on purpose — each page renders its own inline script and this
// single-file dashboard has no shared bundle to put it in.
function specialModeChip(sm){{
  if (sm.mode === 'short_document') return `${{sm.pages}}p rule`;
  if (sm.mode === 'product_rule_flat_sections') return 'product rule: flat sections';
  return String(sm.mode || 'special rule').replace(/_/g, ' ');
}}

function renderFlow(sc) {{
  // The route this document took, as ONE line at the top of the page. Three tiers are
  // always drawn — a tier that never ran is as informative as the one that won, since
  // "the rescue was never tried" and "it was tried and rejected" are different facts.
  // Detail lives in the tooltips: this is the answer at a glance, not the argument.
  const fb = sc.fallback || {{}};
  const chain = fb.chain || [];
  const of = (t) => chain.find(c => c.tier === t) || null;
  const fa = fb.first_attempt || {{}};

  const first = !fb.triggered
    ? ['fl-ok', 'passed', `Scored ${{sc.worst_score}} first time \u2014 no rescue needed.`]
    : ['fl-esc', 'failed', fa.weakest_dimension
        ? `${{fa.weakest_dimension}} scored ${{fa.worst_score}} \u2014 escalated.`
        : `Scored ${{fa.worst_score}} \u2014 escalated.`];
  const tier = (name) => {{
    if (!fb.triggered) return ['fl-skip', 'not needed', 'The first attempt was good enough.'];
    const c = of(name);
    if (!c) return ['fl-skip', 'not reached', 'An earlier tier already recovered the document.'];
    if (c.adopted) return ['fl-adopt', 'ADOPTED',
      (c.worst_before != null ? `${{c.worst_before}} \u2192 ${{c.worst_after}}. ` : '') + (c.status || '')];
    const st = (c.status || '').toLowerCase();
    const skipped = st.startsWith('skipped') || st.includes('not needed') || st.includes('no printed toc');
    return [skipped ? 'fl-skip' : 'fl-rej', skipped ? 'skipped' : 'rejected', c.status || ''];
  }};
  const steps = [first, tier('toc_rescue'), tier('mineru_full')];
  const NAMES = ['Normal pipeline', 'TOC rescue', 'MinerU re-parse'];

  const chips = steps.map((st, i) =>
    `<span class="fl-chip ${{st[0]}}" title="${{escAttrJ(NAMES[i] + ' \u2014 ' + st[1] + '. ' + st[2])}}">
       <b>${{i + 1}}</b> ${{NAMES[i]}} <i>${{st[1]}}</i></span>`
  ).join('<span class="fl-sep">\u203A</span>');

  const RESULT = {{toc_rescue: 'TOC rescue', mineru_full: 'MinerU re-parse',
                  stage1: 'Stage 1 (both rescues rejected)'}};
  const won = fb.triggered ? (RESULT[fb.adopted_tier] || String(fb.adopted_tier || '?'))
                           : 'normal pipeline';
  const sm = sc.special_mode;
  return `<div class="fl-strip">
    <span class="fl-lab">extraction path</span>
    ${{chips}}
    <span class="fl-won">\u2192 <b>${{won}}</b></span>
    ${{sm ? `<span class="fl-flag" title="${{escAttrJ(sm.why || '')}}">\u26A1 ${{specialModeChip(sm)}}</span>` : ''}}
  </div>`;
}}
// Attribute-safe escaping for tooltip text assembled from scorecard strings.
function escAttrJ(x) {{ return escapeHtml(String(x || '')).replace(/"/g, '&quot;'); }}

function renderTocPanel(sc) {{
  // Whether this document's table of contents was fit to build a tree from, stated
  // plainly and ALWAYS shown — a pass is as much a fact worth seeing as a failure.
  //
  // The original verdict is read from fallback.first_attempt, not from the live `toc`
  // dimension, whenever a rescue ran: the TOC rescue REPLACES the PDF's outline, so
  // the re-scored dimension reads "proper" afterwards and the failure that caused the
  // rescue would otherwise vanish from the scorecard entirely.
  const dim = (sc.dimensions || {{}}).toc;
  if (!dim) return '';
  const d = dim.detail || {{}};
  if (d.available === false)
    return `<div class="panel"><h4>Table of contents</h4>
      <div class="panel-sub">${{escapeHtml(d.reason || 'not judged for this document')}}</div></div>`;

  const fb = (sc.fallback && sc.fallback.triggered) ? sc.fallback : null;
  const fa = fb ? (fb.first_attempt || {{}}) : {{}};
  // The headline is the document's CURRENT state, not its history: if a rescue fixed
  // it, this document passes, and saying "FAILED" about output that no longer exists
  // is simply wrong. The original failure is still shown — as the reason the rescue
  // ran — in the note below.
  const rescuedOk = !!(fb && fb.adopted_tier && fb.adopted_tier !== 'stage1'
                       && fa.toc_status && fa.toc_status !== 'proper');
  const rescueTried = !!(fb && fa.toc_status && fa.toc_status !== 'proper');
  const scoreNow = dim.score;
  const scoreThen = fa.toc_score;
  const failed = d.status !== 'proper';
  const why = failed ? (d.explanation || '') : (rescuedOk ? (fa.toc_reason || '') : (d.explanation || ''));

  const head = failed
    ? `<div class="toc-verdict toc-v-fail">\u2716 FAILED the TOC check &mdash; ${{escapeHtml(String(d.status || '').toUpperCase())}}</div>`
    : rescuedOk
      ? `<div class="toc-verdict toc-v-fixed">\u2713 PASSES after rescue &mdash; the outline was REBUILT from the printed contents page</div>`
      : `<div class="toc-verdict toc-v-pass">\u2713 PASSED the TOC check &mdash; PROPER</div>`;

  const facts = `<div class="dim-stats" style="margin-top:10px">
    <div class="dim-stat"><span>bookmark entries in the PDF</span><b>${{d.outline_entries != null ? d.outline_entries : '\u2014'}}</b></div>
    <div class="dim-stat"><span>where the outline starts</span><b>${{escapeHtml(String(d.outline_note || '\u2014'))}}</b></div>
    <div class="dim-stat"><span>printed contents page</span><b>${{d.printed_toc_pages && d.printed_toc_pages.length ? 'page ' + d.printed_toc_pages.join(', ') : 'none found'}}</b></div>
    <div class="dim-stat"><span>printed contents usable?</span><b>${{d.printed_toc_usable ? 'yes \u2014 ' + d.printed_toc_verified + '/' + d.printed_toc_entries + ' verified' : 'no'}}</b></div>
    <div class="dim-stat"><span>TOC score</span><b>${{scoreNow}}</b></div>
  </div>`;

  let rescueNote = '';
  if (rescueTried) {{
    const tier = fb.adopted_tier;
    const adopted = (fb.chain || []).find(c => c.adopted) || {{}};
    const TIER = {{toc_rescue: 'its own PRINTED table of contents', mineru_full: 'a full MinerU re-parse',
                  stage1: 'nothing \u2014 every tier scored worse, so the ordinary Stage 1 result was kept'}};
    rescueNote = `<div class="toc-rescued">
      <b>\u27F2 ${{rescuedOk ? 'This document was RESCUED by the fallback chain.'
                            : 'The fallback chain ran but nothing was adopted \u2014 this is still the Stage 1 result.'}}</b>
      It scored <b>${{scoreThen}}</b> on TOC (${{escapeHtml(String(fa.toc_status||''))}}), which failed the gate and
      triggered a rescue from ${{TIER[tier] || escapeHtml(String(tier||''))}}.
      ${{adopted.worst_before != null ? `Overall score went <b>${{adopted.worst_before}} \u2192 ${{adopted.worst_after}}</b>.` : ''}}
      The TOC dimension now reads <b>${{escapeHtml(String(d.status||''))}}</b> because the outline was rebuilt \u2014
      that is the repaired document being measured, not the original.</div>`;
  }} else if (failed) {{
    rescueNote = `<div class="toc-rescued">This failed the TOC check but was <b>not</b> rescued
      ${{d.rescuable ? '\u2014 a usable printed contents page exists, so a rescue should have been attempted.'
                    : '\u2014 no usable printed contents page was found, so there is nothing better to rebuild the outline from.'}}</div>`;
  }}

  return `<div class="panel"><h4>Table of contents
      <span class="dim-tag dim-tag-crit" title="Sets the verdict: a tree built from an outline that does not describe the document is unusable however complete its text is">gating</span></h4>
    <div class="panel-sub">Was the outline Stage 1 built this tree from actually fit for the job? Every other
      check compares the output against the PDF and is blind to this: when the outline names the wrong
      sections the text is still complete, just never divided into the document's real spine.</div>
    ${{head}}
    <div class="toc-why">${{escapeHtml(why)}}</div>
    ${{facts}}
    ${{rescueNote}}</div>`;
}}

function renderDimension(key, d, th) {{
  const s = d.score, cls = scoreClass(s, th.pass, th.review);
  const barCls = cls === 's-good' ? 'bar-good' : (cls === 's-warn' ? 'bar-warn' : 'bar-bad');
  const stats = (d.detail.stats || []).map(st => {{
    const k = st.bad ? 'bad' : (st.warn ? 'warn' : '');
    return `<div class="dim-stat ${{k}}"><span>${{escapeHtml(st.label)}}</span><b>${{escapeHtml(String(st.value))}}</b></div>`;
  }}).join('');
  const naReason = d.detail.reason ? `<div class="dim-caveat">Not scored: ${{escapeHtml(d.detail.reason)}}</div>` : '';
  const tag = d.critical
    ? '<span class="dim-tag dim-tag-gate" title="This dimension can set the verdict">sets verdict</span>'
    : '<span class="dim-tag dim-tag-adv" title="Reported, but never changes the verdict \\u2014 number-level and duplication noise is expected in this corpus">advisory</span>';
  // Numbers first, explanation on demand: the card used to open with all of "what to do",
  // "limit of this check", "measured from" and the formula ALREADY expanded, which made
  // eight cards a wall of text before a reviewer had even compared the eight scores. Score
  // + stats stay visible (that's the number a reviewer scans for); everything that explains
  // HOW it was reached collapses behind one click, same pattern as the Dismissed findings.
  return `<div class="dim${{d.critical ? '' : ' advisory'}}">
    <div class="dim-head"><span class="dim-name">${{escapeHtml(d.label)}}</span>${{tag}}
      <span class="dim-score ${{cls}}">${{s == null ? 'n/a' : s.toFixed(1)}}</span></div>
    <div class="dim-bar"><div class="${{barCls}}" style="width:${{s == null ? 0 : s}}%"></div></div>
    <div class="dim-what">${{escapeHtml(d.what)}}</div>
    <div class="dim-stats">${{stats}}</div>
    ${{naReason}}
    <details class="val-advanced dim-details"><summary>What to do, and how this is measured</summary>
      <div class="val-advanced-body">
        <div class="dim-advice"><b>What to do:</b> ${{escapeHtml(d.advice)}}</div>
        ${{d.caveat ? `<div class="dim-caveat"><b>Limit of this check:</b> ${{escapeHtml(d.caveat)}}</div>` : ''}}
        <div class="dim-from"><b>Measured from:</b> ${{escapeHtml(d.from)}}</div>
        ${{d.formula ? `<div class="dim-formula"><b>score =</b> ${{escapeHtml(d.formula)}}</div>` : ''}}
      </div>
    </details>
  </div>`;
}}

const STRUCT_FLAG_LABEL = {{
  no_structure: 'NO SECTION STRUCTURE', concentrated: 'CONTENT ALL IN ONE CHUNK',
  shredded: 'SHREDDED INTO FRAGMENTS', many_tiny: 'MOSTLY EMPTY SECTIONS',
}};

// Document SHAPE, plus how that shape compares to the other jurisdictions of the
// same product. Deliberately outside the dim-grid and never scored: an unusual
// shape is a reason to look, not proof of a defect. Products legitimately mix
// document types (114_DRV_Repo holds both German 1997/2005 opinions and a much
// shorter England & Wales one), so letting this move the verdict would train
// people to ignore the verdict.
function renderStructure(s, c) {{
  if (!s || !s.chunks) return '';
  const row = (label, value, extra='') =>
    `<div class="dim-stat ${{extra}}"><span>${{escapeHtml(label)}}</span><b>${{escapeHtml(String(value))}}</b></div>`;

  let flags = '';
  for (const f of (s.flags || [])) {{
    flags += `<div class="val-issue val-issue-red" style="margin:6px 0">
      <div class="val-issue-head">${{escapeHtml(STRUCT_FLAG_LABEL[f.kind] || f.kind)}}</div>
      <div class="val-issue-body">${{escapeHtml(f.detail)}}</div></div>`;
  }}

  // Chunk-size distribution. The single clearest picture of "did this document
  // get sectioned?": an even spread across buckets vs everything piled in one.
  let dist = '';
  const bk = (s.size_buckets || []).filter(b => true);
  if (bk.length) {{
    const peak = Math.max(...bk.map(b => b.n), 1);
    const cols = bk.map(b => {{
      // An EMPTY bucket renders no bar at all. A min-height stub would draw a
      // 2px mark where there is no data, which reads as "a little" instead of
      // "none" — precisely the distinction this chart exists to show.
      const bar = b.n
        ? `<div class="sh-bar" style="height:${{Math.max(3, Math.round(100 * b.n / peak))}}%"></div>`
        : '';
      return `<div class="sh-col" title="${{b.n}} chunk(s) of ${{b.lo}}\\u2013${{b.hi == null ? '\\u221e' : b.hi}} words">${{bar}}</div>`;
    }}).join('');
    const xs = bk.map(b => `<span>${{b.hi == null ? (b.lo/1000)+'k+' : (b.hi >= 1000 ? (b.hi/1000)+'k' : b.hi)}}</span>`).join('');
    const p = s.percentiles || {{}};
    dist = `<div style="margin-top:14px">
      <div class="panel-sub"><b>Chunk-size distribution</b> &mdash; how the document's words are spread
        across its sections. A healthy document fills several buckets; a collapsed one puts everything
        in the rightmost.</div>
      <div class="sh-wrap">${{cols}}</div>
      <div class="sh-x">${{xs}}</div>
      <div class="sh-note">words per chunk (bucket upper bound)${{
        p.p50 != null ? ` &nbsp;&middot;&nbsp; median ${{p.p50}}w, p10 ${{p.p10}}w, p90 ${{p.p90}}w` : ''}}</div>
    </div>`;
  }}

  let cohort = '';
  if (c && c.available) {{
    const conf = {{low:'only a few jurisdictions extracted \\u2014 treat as a hint',
                  medium:'a moderate cohort', good:'a reasonable cohort'}}[c.confidence] || '';
    // Sibling comparison: one bar per jurisdiction on a SHARED scale, with the
    // cohort median drawn as a reference line. This is the view the whole panel
    // exists for -- an outlier is visible without reading a single number.
    // Two documents of the same jurisdiction can sit in one product, so a bare
    // jurisdiction name would label two different bars identically. Append the
    // doc id ONLY for the names that actually repeat — adding it everywhere
    // would clutter the common case for no gain.
    const nameCount = {{}};
    for (const x of (c.siblings || [])) nameCount[x.jurisdiction] = (nameCount[x.jurisdiction] || 0) + 1;
    const labelOf = x => ({{
      j: x.jurisdiction || '?',
      id: (nameCount[x.jurisdiction] > 1 && x.doc_id) ? String(x.doc_id) : '',
    }});

    const barsFor = (key, label, unit='') => {{
      const m = (c.metrics || []).find(x => x.key === key);
      if (!m) return '';
      const rows = (c.siblings || []).map(x => {{
        const v = x[key] ?? 0;
        return {{lab: labelOf(x), v, self: x.is_self}};
      }}).sort((a,b) => b.v - a.v);
      const peak = Math.max(...rows.map(r => r.v), m.median, 1);
      const medPct = 100 * m.median / peak;
      const out = rows.map(r => {{
        const off = m.median && (r.v / m.median >= 2 || r.v / m.median <= 0.5);
        const full = r.lab.j + (r.lab.id ? ' \\u00b7 ' + r.lab.id : '');
        return `<div class="sv-row ${{r.self ? 'is-self' : ''}} ${{r.self && off ? 'is-out' : ''}}">
          <div class="sv-name" title="${{escapeHtml(full)}}"
            ><span class="sv-j">${{r.self ? '\\u25b8 ' : ''}}${{escapeHtml(r.lab.j)}}</span
            >${{r.lab.id ? `<span class="sv-id">${{escapeHtml(r.lab.id)}}</span>` : ''}}</div>
          <div class="sv-track"><div class="sv-bar" style="width:${{Math.max(1, 100*r.v/peak)}}%"></div>
            <div class="sv-med" style="left:${{medPct}}%" title="product median ${{m.median}}"></div></div>
          <div class="sv-val">${{r.v}}${{unit}}</div></div>`;
      }}).join('');
      return `<div style="margin-top:14px"><div class="panel-sub"><b>${{escapeHtml(label)}}</b>
          &mdash; one bar per jurisdiction, shared scale. Dashed line = product median (${{m.median}}${{unit}}).</div>
        ${{out}}<div class="sv-medlab">\\u2503 dashed = product median</div></div>`;
    }};

    const rows = (c.metrics || []).map(m => {{
      const off = m.ratio != null && (m.ratio >= 2 || m.ratio <= 0.5);
      return `<tr class="${{off ? 'struct-off' : ''}}">
        <td>${{escapeHtml(m.label)}}</td>
        <td class="num">${{escapeHtml(String(m.value))}}</td>
        <td class="num">${{escapeHtml(String(m.median))}}</td>
        <td class="num">${{m.ratio == null ? '\\u2014' : '\\u00d7' + m.ratio}}</td></tr>`;
    }}).join('');

    cohort = `
      <div class="panel-sub" style="margin-top:16px">Compared against the <b>${{c.cohort_size}}</b>
        extracted jurisdiction(s) of <b>${{escapeHtml(c.product || '')}}</b>. Jurisdictions in one product
        follow a shared template, so their extracted shape should match; a large deviation usually means
        heading detection behaved differently on this one. Measured against the cohort <b>median</b>
        (not the mean \\u2014 with three documents, one broken member drags a mean toward itself and hides
        exactly what this is meant to expose). <i>${{escapeHtml(conf)}}</i>.</div>
      ${{c.is_outlier ? `<div class="dim-caveat" style="border-color:#f0a83c;color:#f0a83c">
         <b>&#9888; Outlier:</b> ${{c.outlier_metrics.map(m => escapeHtml(m.label) + ' \\u00d7' + m.ratio).join(', ')}}
         vs this product's median \\u2014 worth opening alongside a sibling to compare.</div>` : ''}}
      ${{barsFor('mean_chunk_words', 'Mean words per chunk', 'w')}}
      ${{barsFor('chunks', 'Chunks (sections)')}}
      ${{barsFor('concentration_pct', 'Share of words in the largest chunk', '%')}}
      <div class="panel-sub" style="margin-top:16px"><b>All metrics</b> &mdash; the same comparison in full.</div>
      <table class="struct-table"><thead><tr>
        <th>metric</th><th class="num">this doc</th><th class="num">product median</th><th class="num">ratio</th>
      </tr></thead><tbody>${{rows}}</tbody></table>`;
  }} else if (c && c.reason) {{
    cohort = `<div class="dim-caveat" style="margin-top:12px">No cohort comparison: ${{escapeHtml(c.reason)}}</div>`;
  }}

  return `<div class="panel"><h4>Structure &amp; shape
      <span class="dim-tag dim-tag-adv" title="Never changes the verdict">advisory</span></h4>
    <div class="panel-sub">What this document's extracted tree looks like \\u2014 the one view that can
      catch a document where every word is present but the section structure collapsed, which the
      coverage checks are blind to by construction.</div>
    ${{flags}}
    <div class="dim-stats" style="margin-top:10px">
      ${{row('pages', s.pages ?? '\\u2014')}}
      ${{row('chunks (sections)', s.chunks)}}
      ${{row('chunks per page', s.chunks_per_page ?? '\\u2014')}}
      ${{row('words per chunk (mean / median)', s.mean_chunk_words + ' / ' + s.median_chunk_words)}}
      ${{row('largest chunk', s.max_chunk_words + 'w')}}
      ${{row('words in the largest chunk', s.concentration_pct + '%', s.concentration_pct >= 80 ? 'bad' : '')}}
      ${{row('tiny chunks (<20w)', s.tiny_chunks, s.tiny_chunks > s.chunks/2 ? 'warn' : '')}}
      ${{row('headings', s.headings_total)}}
      ${{row('nesting depth', s.max_depth)}}
    </div>
    ${{dist}}
    ${{cohort}}</div>`;
}}

const PAGE_STATE_LABEL = {{ ok:'clean', flagged:'flagged loss', silent:'SILENT loss', unvalidatable:'visual-only' }};

const FIND_KIND_LABEL = {{
  gap: 'content gap', placement: 'placement', table: 'table failed',
  number: 'number', transposition: 'transposition',
  unreadable: 'UNREADABLE PAGE', engine: 'engine disagreement',
  meaning: 'MEANING CHANGED', hierarchy: 'wrong nesting',
  fn_dangling: 'FOOTNOTE HAS NO BODY', fn_orphan: 'FOOTNOTE MARKER LOST',
  fn_gap: 'FOOTNOTE MISSING FROM SEQUENCE', fn_duplicate: 'FOOTNOTE BODY DUPLICATED — VERIFY',
  table_lost: 'TABLE LOST AFTER EXTRACTION',
  // MinerU extracted the table; no Stage 1 region claimed it. Lost is shouted
  // because nothing else in the scorecard may catch it (Bahrain__169749: 3,543
  // chars gone, completeness 98.6, zero silent spans). Recovered is lower-case
  // because nothing was lost -- the only open question is which region took it.
  orphan_table: 'TABLE IN NO SECTION',
  orphan_recovered: 'orphan table recovered',
  orphan_duplicate: 'orphan table (duplicate — nothing lost)',
}};

function renderFinding(f) {{
  const sevCls = f.dismissed ? 'find-advisory' : ('find-' + (f.severity || 'advisory'));
  const where = [f.file ? f.file.split('/').pop() : null,
                 (f.pages && f.pages.length) ? 'p' + f.pages.join('–') : null]
                .filter(Boolean).join(' \\u00b7 ');
  const dismissedNote = f.dismissed && f.dismissal
    ? `<div class="find-detail">Dismissed as a false positive${{f.dismissal.reason ? ': ' + escapeHtml(f.dismissal.reason) : ''}} \\u2014 excluded from the score, kept on record.</div>`
    : '';
  // Data attributes + one delegated listener (see wireFindingButtons) rather than
  // inline onclick: the previous version interpolated JSON.stringify(title) — whose
  // own double quotes terminated the onclick="..." attribute — so Dismiss silently
  // never fired for any finding.
  const btn = f.dismissed
    ? `<button class="find-btn restore" data-act="restore" data-key="${{escapeHtml(f.key)}}">Restore</button>`
    : `<button class="find-btn" data-act="dismiss" data-key="${{escapeHtml(f.key)}}"`
      + ` data-title="${{escapeHtml(f.title)}}">Dismiss</button>`;
  return `<div class="find ${{sevCls}}${{f.dismissed ? ' is-dismissed' : ''}}">
    <div class="find-main">
      <div class="find-top">
        <span class="find-kind">${{FIND_KIND_LABEL[f.kind] || f.kind}}</span>
        ${{f.severity === 'silent' ? '<span class="find-kind" style="background:#3a1414;color:#ff6b6b">silent</span>' : ''}}
        ${{f.severity === 'advisory' ? '<span class="find-kind" style="background:#26262e;color:#8b8f9a">advisory</span>' : ''}}
        <span class="find-where">${{escapeHtml(where)}}</span>
      </div>
      <div class="find-title">${{escapeHtml(f.title)}}</div>
      <div class="find-detail">${{escapeHtml(f.detail || '')}}</div>
      ${{dismissedNote}}
    </div>${{btn}}</div>`;
}}

function wireFindingButtons(container) {{
  container.querySelectorAll('button[data-act]').forEach(b => {{
    b.onclick = () => (b.dataset.act === 'dismiss'
      ? dismissFinding(b.dataset.key, b.dataset.title || '')
      : restoreFinding(b.dataset.key));
  }});
}}

async function dismissFinding(key, title) {{
  const reason = prompt('Why is this a false positive? (optional \\u2014 recorded with the dismissal)', '');
  if (reason === null) return;   // cancelled
  // view: SC_VIEW -- so the server rescores whichever scorecard is actually on
  // screen. Without it, dismissing while on Scorecard 2 (the default) silently
  // rescored Scorecard 1 instead, and the finding just sat there un-dismissed.
  await fetch(`/api/jobs/${{JOB_ID}}/dismiss`, {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ key, label: title, reason, view: SC_VIEW }}),
  }});
  loadScorecard();   // re-render: the score and heat-map update immediately
}}

async function restoreFinding(key) {{
  await fetch(`/api/jobs/${{JOB_ID}}/restore`, {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ key, view: SC_VIEW }}),
  }});
  loadScorecard();
}}

// Which TREE the scorecard describes. "extraction" is stages 1-3 (the gate); "post_ai"
// is the stage 4/5 tree the Document tab now opens on by default. Both exist as separate
// files, and until this switch the page showed the stage-5 tree beside stage-3 scores
// with nothing saying they described different documents.
//
// Defaults to post_ai: when stage 4/5 ran, that tree is what actually ships, and a
// reviewer opening a document should see how it reads NOW, not how it read before the AI
// pass touched it. Safe on every other document too -- job_scorecard falls back to
// "extraction" server-side when `view` isn't in this job's `views`, and this JS syncs
// back to whatever the server actually served (see the `if (sc.view) SC_VIEW = ...` below).
let SC_VIEW = 'post_ai';
// User-facing names only -- the internal key stays "post_ai" (view=post_ai, scorecard_post_ai.json)
// everywhere else in this file and in the API; only the LABEL a reviewer reads changes here.
const SC_VIEW_LABEL = {{extraction: 'Scorecard 1 \\u00b7 Extraction (stages 1\\u20133)',
                        post_ai: 'Scorecard 2 \\u00b7 Post-AI (stage 4/5)'}};

function renderScoreViewSwitch(sc) {{
  const views = sc.views || ['extraction'];
  if (views.length < 2) return '';
  const stage = sc.scored_stage ? ` (stage ${{sc.scored_stage}})` : '';
  return `<div class="sc-viewswitch">` + views.map(v =>
    `<button class="stage-btn${{v === SC_VIEW ? ' on' : ''}}" data-scview="${{v}}">${{SC_VIEW_LABEL[v] || v}}</button>`
  ).join('') + `<span class="sc-viewnote">${{
    SC_VIEW === 'post_ai'
      ? 'Scorecard 2 \\u2014 scoring the tree stage 4/5 left behind' + stage + '. NOT the extraction gate.'
      : 'Scorecard 1 \\u2014 the extraction gate: stages 1\\u20133 only. The AI pass is not measured here.'
  }}</span></div>`;
}}

// A cross-check alongside the scored dimensions, not one of them: recovered/regressed
// text (matched by its own content, since stage 5's file layout is not stage 3's),
// heading coverage against the source outline, and table row counts against MinerU's
// own count. See /api/jobs/{{id}}/stage_diff.json and compare_stage_completeness.py.
function renderStageDiff(diff) {{
  if (!diff || !diff.available) return '';
  const t = diff.text || {{}}, h = diff.headings || {{}}, tb = diff.tables || {{}};
  const esc = escapeHtml;

  const spanRow = (v) => `<div class="val-issue-body">
    <span class="val-issue-pages">[${{v.token_count}} tok]</span>
    <b>${{esc(v.file)}}</b>: ${{esc(v.text.length > 140 ? v.text.slice(0, 140) + '\\u2026' : v.text)}}</div>`;
  let textHtml = `<div class="panel-sub">
    ${{t.dropped_spans_at_from ?? '?'}} dropped span(s) at stage ${{diff.from_stage}},
    ${{t.dropped_spans_at_to ?? '?'}} at stage ${{diff.to_stage}} &mdash;
    <b>${{(t.recovered || []).length}}</b> recovered,
    <b>${{(t.regressed || []).length}}</b> regressed,
    ${{(t.still_missing || []).length}} still missing.</div>`;
  if ((t.regressed || []).length) {{
    textHtml += `<div class="val-issue val-issue-red"><div class="val-issue-head">
      <span class="val-issue-title">REGRESSED &mdash; present at stage ${{diff.from_stage}}, missing at stage ${{diff.to_stage}}</span></div>`
      + t.regressed.slice(0, 12).map(spanRow).join('') + '</div>';
  }}
  if ((t.recovered || []).length) {{
    textHtml += `<div class="val-issue" style="background:#132a1c;border-left-color:#3ddc76"><div class="val-issue-head">
      <span class="val-issue-title">RECOVERED &mdash; missing at stage ${{diff.from_stage}}, present at stage ${{diff.to_stage}}</span></div>`
      + t.recovered.slice(0, 12).map(spanRow).join('') + '</div>';
  }}
  if (!(t.regressed || []).length && !(t.recovered || []).length) {{
    textHtml += '<div class="find-none">&#10003; nothing moved between missing and present</div>';
  }}

  let headHtml;
  if (h.error) {{
    headHtml = `<div class="panel-sub">${{esc(h.error)}} &mdash; skipped</div>`;
  }} else {{
    const mf = h.missing_at_from || [], mt = h.missing_at_to || [];
    headHtml = `<div class="panel-sub">${{h.outline_count ?? 0}} headings in the source outline.</div>`;
    if (!mf.length && !mt.length) {{
      headHtml += '<div class="find-none">&#10003; every outline heading present at both stages</div>';
    }} else {{
      const list = (label, items) => !items.length ? '' :
        `<div class="val-issue val-issue-amber"><div class="val-issue-head">
          <span class="val-issue-title">${{label}}</span></div>` +
        items.map(hd => `<div class="val-issue-body">${{esc(hd.title)}} (p${{hd.page}})</div>`).join('') + '</div>';
      headHtml += list(`MISSING at stage ${{diff.from_stage}}`, mf) + list(`MISSING at stage ${{diff.to_stage}}`, mt);
    }}
  }}

  let tableHtml;
  if (tb.error) {{
    tableHtml = `<div class="panel-sub">${{esc(tb.error)}} &mdash; skipped</div>`;
  }} else {{
    const rows = tb.tables || [];
    const bad = rows.filter(x => x.missing_at_from || x.missing_at_to || x.row_delta_to_vs_from);
    tableHtml = `<div class="panel-sub">${{tb.mineru_table_count ?? 0}} tables MinerU extracted.</div>`;
    if (!bad.length) {{
      tableHtml += '<div class="find-none">&#10003; every table present at both stages with an unchanged row count</div>';
    }} else {{
      tableHtml += '<div class="dim-stats">' + bad.map(row => {{
        const fr = row.missing_at_from ? 'MISSING' : row.rows_at_from;
        const to = row.missing_at_to ? 'MISSING' : row.rows_at_to;
        const big = row.missing_at_from || row.missing_at_to
          || Math.abs(row.row_delta_to_vs_from || 0) > 5;
        const pages = row.pages[0] === row.pages[1] ? `p${{row.pages[0]}}` : `p${{row.pages[0]}}\\u2013${{row.pages[1]}}`;
        return `<div class="dim-stat ${{big ? 'bad' : 'warn'}}">
          <span>${{esc(row.table_id)}} (${{pages}}) &mdash; MinerU ${{row.mineru_rows}} rows</span>
          <b>stage${{diff.from_stage}}=${{fr}} &rarr; stage${{diff.to_stage}}=${{to}}</b></div>`;
      }}).join('') + '</div>';
    }}
  }}

  return `<div class="panel"><h4>Completeness diff &mdash; stage ${{diff.from_stage}} vs stage ${{diff.to_stage}}</h4>
    <div class="panel-sub">A cross-check alongside the dimensions above, not one of them: it never
      writes a score. Text is matched by its own content rather than by file, since stage
      ${{diff.to_stage}}'s layout is not stage ${{diff.from_stage}}'s; headings and tables are checked
      against the source outline and MinerU's own count rather than against each other.</div>
    <div style="margin-top:12px"><b style="color:#e6e6e6">Text</b>${{textHtml}}</div>
    <div style="margin-top:14px"><b style="color:#e6e6e6">Headings</b>${{headHtml}}</div>
    <div style="margin-top:14px"><b style="color:#e6e6e6">Tables</b>${{tableHtml}}</div>
  </div>`;
}}

async function loadScorecard() {{
  const body = document.getElementById('score-body');
  const r = await fetch(`/api/jobs/${{JOB_ID}}/scorecard.json?view=${{SC_VIEW}}`);
  const sc = await r.json();
  if (sc.view) SC_VIEW = sc.view;   // server falls back to extraction if the file is gone
  // Fetched alongside the scorecard, never merged into it: a reviewer's verdict is a
  // separate record and must stay distinguishable from what was measured.
  let REVIEW = {{}};
  try {{ REVIEW = await (await fetch(`/api/jobs/${{JOB_ID}}/review`)).json(); }} catch (e) {{ REVIEW = {{}}; }}
  if (sc.error) {{ body.innerHTML = `<div class="val-issue val-issue-red">Scorecard failed: ${{escapeHtml(sc.error)}}</div>`; return; }}

  // A cross-check the scored dimensions structurally cannot do: which text moved from
  // missing to present (or the reverse) between stage 3 and whatever stage 4/5 left
  // behind, matched by content rather than file path or table_id. Fetched up front (not
  // where it renders) so its position in the page can be decided independently of when
  // the data arrives. Scorecard 2 (post_ai) only -- the comparison it draws is inherently
  // about the AI pass, and has nothing to say on Scorecard 1's own page. Best-effort: this
  // must never take the scorecard down with it.
  let diffHtml = '';
  if (SC_VIEW === 'post_ai') {{
    try {{
      const dr = await fetch(`/api/jobs/${{JOB_ID}}/stage_diff.json?from_stage=3`);
      diffHtml = renderStageDiff(await dr.json());
    }} catch (e) {{ /* the scorecard above is the thing people came for */ }}
  }}

  const th = sc.gate_thresholds || {{ pass: 90, review: 70 }};
  const g = GATE_TEXT[sc.gate] || GATE_TEXT.unknown;
  const weak = sc.weakest_dimension ? (sc.dimensions[sc.weakest_dimension] || {{}}).label : null;

  const critNames = (sc.critical_dimensions || []).map(k => (sc.dimensions[k] || {{}}).label || k).join(', ');
  // One click from the verdict to the tree it was measured against -- the Core Index
  // scorecard page has the same "Open viewer" button for the same reason: a reviewer
  // reading a gate/score number almost always wants to go look at the actual content next.
  // Opens the SAME self-contained md/PDF viewer a finished run publishes to S3 (built
  // fresh, on demand, from this job's tree right now) -- not this dashboard's own
  // Document tab, which is a different, richer page with its own tabs and controls.
  let html = `<div style="margin:0 0 14px"><button onclick="window.open('/api/jobs/${{JOB_ID}}/viewer.html','_blank')">\U0001F4C4 Open viewer</button></div>`
    + renderScoreViewSwitch(sc) + renderFlow(sc) + `<div class="gate gate-${{sc.gate}}">
    <div class="gate-verdict">${{g.icon}} ${{g.title}}</div>
    <div><div class="gate-sub">${{escapeHtml(g.sub)}}</div>
      <div class="gate-note">${{(sc.gate_reasons || []).map(escapeHtml).join(' ')}} ${{weak ? `Lowest <b>verdict-setting</b> dimension: <b>${{escapeHtml(weak)}}</b> at ${{sc.worst_score}}.` : ''}}
      The score-based verdict is the WORST of ${{escapeHtml(critNames)}} \\u2014 never an average, because a 99%
      extraction that drops one risk-disclosure table is a failed extraction. Number-level and
      aggregate duplication findings are <b>advisory</b>. Source-based structural findings require review even when percentages pass, so
      expected %/footnote noise can't drown out a genuinely missing paragraph or table.${{
      sc.dismissed_count ? ` <b>${{sc.dismissed_count}} finding(s) dismissed</b> as false positives and excluded from these scores &mdash; listed under Findings, restorable.` : ''}}</div></div></div>`;

  // A verdict reached under a DIFFERENT rule than its neighbours must say so, or the
  // number is misleading: 96 here and 96 on a 200-page document did not measure the
  // same things. Rendered directly under the gate, before any dimension.
  const sm = sc.special_mode;
  if (sm) {{
    const notGating = (sm.not_gating || []).map(k => (sc.dimensions[k] || {{}}).label || k).join(', ');
    html += `<div class="special-mode">
      <div class="sm-title">\u26A1 SPECIAL RULE APPLIED &mdash; ${{escapeHtml(String(sm.mode || '').replace(/_/g, ' '))}}</div>
      <div class="sm-why">${{escapeHtml(sm.why || '')}}</div>
      <div class="sm-facts">
        <span><b>${{sm.pages}}</b> pages (bar is ${{sm.threshold}})</span>
        <span>verdict set by: <b>${{escapeHtml((sm.gated_on || []).join(', '))}}</b></span>
        ${{notGating ? `<span>shown but NOT gating: <b>${{escapeHtml(notGating)}}</b></span>` : ''}}
      </div></div>`;
  }}

  // What the document COST, next to the verdict it was spent earning. The scorecard
  // already carries it (run_corpus.timing_block) -- this tab fetched the whole object
  // and rendered everything except the field answering "what did this take".
  const tm = sc.timing;
  if (tm && tm.seconds != null) {{
    const dfmt = (x) => {{
      if (x == null) return '\u2014';
      x = Math.round(x);
      if (x < 60) return x + 's';
      const m = Math.floor(x / 60), r = x % 60;
      return m < 60 ? (r ? `${{m}}m ${{r}}s` : `${{m}}m`) : `${{Math.floor(m/60)}}h ${{m % 60}}m`;
    }};
    const steps = Object.entries(tm.steps || {{}})
      .filter(([, v]) => v >= 0.5)
      .map(([k, v]) => {{
        const pct = tm.seconds ? Math.round(v / tm.seconds * 100) : 0;
        const isFb = (k === 'toc_rescue' || k === 'mineru_full');
        return `<div class="tm-step${{isFb ? ' tm-step-fb' : ''}}">
          <span class="tm-k">${{escapeHtml(k)}}</span>
          <span class="tm-bar"><i style="width:${{pct}}%"></i></span>
          <span class="tm-v">${{dfmt(v)}}</span><span class="tm-p">${{pct}}%</span></div>`;
      }}).join('');
    const facts = [
      tm.pages ? `<span><b>${{tm.pages}}</b> pages</span>` : '',
      tm.seconds_per_page ? `<span><b>${{tm.seconds_per_page}}</b>s / page</span>` : '',
      tm.mineru_crop_pages != null ? `<span><b>${{tm.mineru_crop_pages}}</b> crop pages to MinerU</span>` : '',
      tm.seconds_per_crop_page ? `<span><b>${{tm.seconds_per_crop_page}}</b>s / crop page</span>` : '',
      tm.fallback_seconds ? `<span class="tm-warn"><b>${{dfmt(tm.fallback_seconds)}}</b> in fallback tiers</span>` : '',
      tm.cloned_from ? `<span class="tm-warn">cloned from <b>${{escapeHtml(String(tm.cloned_from))}}</b> \u2014 paid none of this cost</span>` : '',
    ].filter(Boolean).join('');
    html += `<div class="timing-card">
      <div class="tm-head"><span class="tm-title">\u23F1 EXTRACTION COST</span>
        <span class="tm-total">${{dfmt(tm.seconds)}}</span>
        <span class="tm-slow">slowest: <b>${{escapeHtml(String(tm.slowest_step || '\u2014'))}}</b></span></div>
      <div class="tm-facts">${{facts}}</div>
      ${{steps ? `<div class="tm-steps">${{steps}}</div>` : ''}}
      ${{tm.backfilled ? `<div class="tm-note">Durations were measured at extraction time, but this
        block was reconstructed from timings.json afterwards \u2014 the start/finish window is
        approximate.</div>` : ''}}</div>`;
  }}

  // WHY it failed, stated before anything else. A reader opening a failed document
  // should not have to find the weakest dimension, expand it, and read a formula to
  // learn what went wrong \u2014 the reason is the first thing they came for.
  if (sc.gate === 'fail') {{
    const wk = sc.weakest_dimension;
    const d  = (sc.dimensions[wk] || {{}});
    const det = d.detail || {{}};
    const reason = det.reason || det.explanation
      || (det.stats || []).filter(x => x.bad).map(x => `${{x.label}}: ${{x.value}}`).join(' \u00b7 ')
      || `${{d.label || wk}} scored ${{d.score}}`;
    const fbk = sc.fallback || {{}};
    const tried = fbk.triggered
      ? (fbk.hard_fail
          ? 'Every fallback tier was tried and none produced a usable result.'
          : `Rescued via ${{escapeHtml(String(fbk.adopted_tier || ''))}}, and it still failed this test.`)
      : 'No fallback was triggered.';
    html += `<div class="failwhy">
      <div class="failwhy-t">\u2716 THIS DOCUMENT FAILED &mdash; ${{escapeHtml((d.label || wk || '').toUpperCase())}}</div>
      <div class="failwhy-b">${{escapeHtml(reason)}}</div>
      <div class="failwhy-m">Set by the weakest verdict-setting dimension: <b>${{escapeHtml(d.label || wk || '?')}}</b>
        at <b>${{d.score}}</b> (a document passes at ${{(sc.gate_thresholds||{{}}).pass ?? 90}}).
        ${{escapeHtml(tried)}}</div>
    </div>`;
  }}

  // Which tier produced this tree. A reader comparing two scores has to know whether
  // one of them came from the normal deterministic extraction or from a rescue — the
  // rescued tree is structured unlike its neighbours, so the number is not like-for-like.
  const fbk = (sc.fallback && sc.fallback.triggered) ? sc.fallback : null;
  if (fbk) {{
    const TIER_TEXT = {{
      toc_rescue: 'The outline was rebuilt from its own <b>printed table of contents</b> and re-extracted.',
      mineru_full: 'The printed contents page could not fix it either, so the <b>whole document was re-parsed by MinerU</b>, which supplied the hierarchy as well.',
      stage1: 'No fallback tier scored better, so <b>this is the ordinary Stage 1 result</b> \u2014 both rescues were tried and rejected.'}};
    const fa0 = fbk.first_attempt || {{}};
    const rows = (fbk.chain || []).map(c =>
      `<li><b>${{escapeHtml(c.tier || '')}}</b> \u2014 ${{escapeHtml(c.status || '')}}${{
        c.adopted ? ' <b>&#10003; adopted</b>' : ''}}</li>`).join('');
    // Name the dimension that ACTUALLY failed, not completeness. Escalation used to
    // have exactly one trigger (completeness < 60), so hard-coding it here was true;
    // it no longer is. A TOC-triggered document reads "scored 92.1 on completeness
    // (fail) \u2014 low enough to redo the document", which is self-contradicting: 92.1 is
    // a comfortable pass, and completeness was never the problem. The weakest
    // dimension of the FIRST attempt is the honest answer.
    const trigLabel = (sc.dimensions[fa0.weakest_dimension] || {{}}).label
                      || fa0.weakest_dimension || 'the gate';
    const alsoOk = (fa0.weakest_dimension && fa0.weakest_dimension !== 'completeness'
                    && fa0.completeness_score != null)
      ? ` Completeness itself was fine at <b>${{fa0.completeness_score}}</b> \u2014 every word was
          present, the document simply had no usable structure.` : '';
    html += `<div class="dim-caveat" style="margin:10px 0 0">
      <b>\u27F2 Fallback ran.</b> The deterministic extraction failed on
      <b>${{escapeHtml(String(trigLabel))}}</b> at
      <b>${{fa0.worst_score != null ? fa0.worst_score : '?'}}</b>, which set the verdict to
      ${{escapeHtml(fa0.gate || '?')}} \u2014 low enough to redo the document.${{alsoOk}}
      ${{TIER_TEXT[fbk.adopted_tier] || escapeHtml(String(fbk.adopted_tier || ''))}}
      Tiers are tried cheapest-first and each is kept only if it scores better than the one before.
      <ul style="margin:6px 0 0 18px">${{rows}}</ul></div>`;
  }}

  if ((sc.advisory_low || []).length) {{
    const names = sc.advisory_low.map(k => (sc.dimensions[k] || {{}}).label || k).join(', ');
    html += `<div class="dim-caveat" style="margin:10px 0 0">Advisory dimension(s) scoring low
      (<b>${{escapeHtml(names)}}</b>) &mdash; worth a glance, but deliberately not affecting the verdict.</div>`;
  }}

  // Verdict-setting dimensions first, so the cards that matter read top-left.
  const ordered = Object.entries(sc.dimensions).sort((a, b) => (b[1].critical ? 1 : 0) - (a[1].critical ? 1 : 0));
  html += '<div class="dim-grid">';
  for (const [key, d] of ordered) html += renderDimension(key, d, th);
  html += '</div>';
  html += diffHtml;

  // ---- individual findings, dismissable ----
  // Right under the Completeness diff, always: the diff tells a reviewer TEXT recovered or
  // regressed between stage 3 and whatever stage 4/5 left behind, and a finding is the same
  // kind of claim at a finer grain (one specific gap, one specific table) -- the two belong
  // read together, not separated by the TOC/Review panels in between.
  const findings = sc.findings || [];
  const active = findings.filter(f => !f.dismissed);
  const gone = findings.filter(f => f.dismissed);
  html += `<div class="panel"><h4>Findings &mdash; ${{active.length}} open</h4>
    <div class="panel-sub">Each one is an individual claim you can judge. <b>Dismiss</b> marks it a
      false positive: it leaves the score and the page heat-map, but it is kept on record and can be
      restored at any time &mdash; nothing is deleted. Dismissals are stored against the source PDF,
      so re-running the same document keeps them.</div>`;
  if (!active.length) {{
    html += '<div class="find-none">&#10003; No open findings.</div>';
  }} else {{
    for (const f of active) html += renderFinding(f);
  }}
  if (gone.length) {{
    html += `<details class="val-advanced" style="margin-top:12px"><summary>Dismissed &mdash; ${{gone.length}} (kept on record, click to review or restore)</summary>
      <div class="val-advanced-body">` + gone.map(renderFinding).join('') + '</div></details>';
  }}
  html += '</div>';

  html += renderTocPanel(sc);
  html += renderReview(sc, REVIEW);

  // ---- page heat-map ----
  const pages = sc.pages || {{ states: {{}}, counts: {{}}, total: 0 }};
  const order = ['ok', 'flagged', 'silent', 'unvalidatable'];
  html += `<div class="panel"><h4>Page health &mdash; ${{pages.total}} pages</h4>
    <div class="panel-sub">One square per PDF page, in order. Click any page to open it in Page Review
      alongside everything flagged on it. A finding is attributed to a single page (the first of its
      section's range) rather than smeared across the whole range.</div><div class="legend">`;
  for (const st of order) {{
    const n = pages.counts[st] || 0;
    html += `<div class="legend-item" title="${{escapeHtml((pages.state_help || {{}})[st] || '')}}">
      <span class="legend-sw h-${{st}}"></span>${{PAGE_STATE_LABEL[st]}} &middot; <b>${{n}}</b></div>`;
  }}
  html += '</div><div class="heat">';
  for (let p = 1; p <= pages.total; p++) {{
    const st = pages.states[String(p)] || 'ok';
    html += `<div class="heat-cell h-${{st}}" title="Page ${{p}} \\u2014 ${{PAGE_STATE_LABEL[st]}}"
              onclick="gotoPage(${{p}})"></div>`;
  }}
  html += '</div>';
  if ((pages.counts.unvalidatable || 0) > 0) {{
    html += `<div class="dim-caveat" style="margin-top:12px">${{escapeHtml(pages.state_help.unvalidatable)}}</div>`;
  }}
  html += '</div>';

  // ---- table strip ----
  if ((sc.tables || []).length) {{
    const buckets = {{}};
    for (const t of sc.tables) buckets[t.bucket] = (buckets[t.bucket] || 0) + 1;
    html += `<div class="panel"><h4>Tables &mdash; ${{sc.tables.length}} detected</h4>
      <div class="panel-sub">${{Object.entries(buckets).map(([b, n]) => `${{n}} ${{b}}`).join(' &middot; ')}}.
        Click a table to inspect what was cropped, what MinerU returned, and the bbox overlay.</div>
      <div class="strip">`;
    for (const t of sc.tables) {{
      const iou = t.match_iou != null ? ` ${{Math.round(t.match_iou * 100)}}%` : '';
      const tip = t.reason || t.bucket;
      html += `<span class="chip c-${{t.bucket}}" title="${{escapeHtml(tip)}}" onclick="setTab('insp')">
        ${{escapeHtml(t.table_id.replace('table_', 'T'))}} p${{t.pages[0]}}${{iou}}</span>`;
    }}
    html += '</div></div>';
  }}

  html += `<div class="calib"><b>How to read these numbers.</b> Every score is a
    <i>conservation</i> check &mdash; not "is this correct?" (unanswerable without a human answer key)
    but "was anything lost, moved, or duplicated?", which the source PDF alone can answer.
    Losses the output <i>declares</i> (a failure marker, a page snapshot) are penalised at
    ${{Math.round((1 - 0.25) * 100)}}% less than silent ones, so the pipeline is never rewarded for
    hiding a failure.${{sc.calibrated ? '' : ' <b>Thresholds are provisional defaults, not yet calibrated against a human-labelled gold set</b> \\u2014 read a score as "where to look first", not as a measurement.'}}</div>`;

  // Last on the page, deliberately: an unusual SHAPE is a reason to look, not proof of a
  // defect (products legitimately mix document types), so it reads as background after
  // everything that actually scored the document, not competing with it at the top.
  html += renderStructure(sc.structure || {{}}, sc.cohort || {{}});

  body.innerHTML = html;
  wireFindingButtons(body);
  body.querySelectorAll('[data-scview]').forEach(b => b.onclick = () => {{
    if (b.dataset.scview === SC_VIEW) return;
    SC_VIEW = b.dataset.scview;
    loadScorecard();   // re-fetch: the whole panel is derived from the chosen view
    // The page review is derived from the same scorecard, so it has to follow the
    // switch or it goes on listing the other stage's findings against this one's
    // pages. Only if it has already been opened — otherwise it loads on first use.
    if (prLoaded) loadPageReview();
    // Validation is per-check detail for the SAME tree the verdict describes, so it
    // follows too — otherwise the numbers under Scorecard 2 are stage 3's.
    if (valLoaded) loadValidation();
  }});
}}

async function gotoPage(p) {{
  // Claim prLoaded BEFORE setTab so setTab doesn't also kick off loadPageReview:
  // its own tail-call to loadPRPage(lowest issue page) would otherwise race and
  // override the page the user actually clicked.
  const needLoad = !prLoaded;
  prLoaded = true;
  setTab('pr');
  if (needLoad) await loadPageReview();
  loadPRPage(p);
}}

async function loadInspector() {{
  const grid = document.getElementById('inspector-grid');
  const r = await fetch(`/api/jobs/${{JOB_ID}}/tables_detail.json`);
  const {{ tables }} = await r.json();
  grid.innerHTML = '';
  for (const t of tables) {{
    const card = document.createElement('div');
    card.className = 'insp-card ' + (t.ok ? 'insp-ok' : 'insp-fail');
    const imgs = t.asset_urls.map(u => `<img src="${{u}}">`).join('');
    const SRC_NOTES = {{
      snapshot_fallback_anchored: '\\uD83D\\uDD0E anchored fallback \\u2014 extractor located this region but didn\\'t trust it enough to render; verify placement/content',
      snapshot_fallback_unanchored: '\\uD83D\\uDD0E whole-page fallback, no anchor \\u2014 may be misplaced if a new section starts on this page',
    }};
    const srcNote = SRC_NOTES[t.source] ? `(${{SRC_NOTES[t.source]}})` : '';
    const badge = t.ok
      ? `<span class="insp-badge ok">&#10003; ${{t.rows}} rows &times; ${{t.cols}} cols</span>`
      : `<span class="insp-badge fail">&#10007; failed</span>`;
    const output = t.ok
      ? marked.parse(t.table_md) + `<details><summary>raw MinerU HTML</summary><pre>${{escapeHtml(t.table_html)}}</pre></details>`
      : `<p class="insp-fail-text">${{escapeHtml(t.reason || 'MinerU produced nothing usable.')}}</p>`;
    const geoImgs = t.pages.map(p => `<img src="/api/jobs/${{JOB_ID}}/page_bbox_image/${{p}}">`).join('');
    card.innerHTML = `
      <h4>${{t.table_id}} &mdash; page(s) ${{t.pages.join(', ')}} <span class="insp-src">${{srcNote}}</span>${{badge}}${{geoBadge(t)}}</h4>
      <div class="insp-cols">
        <div class="insp-in"><div class="insp-label">Passed to MinerU (source page)</div>${{imgs}}</div>
        <div class="insp-out"><div class="insp-label">MinerU output</div>${{output}}</div>
      </div>
      <details class="val-advanced"><summary>Geometry (Rule B: pdf2mdtree bbox vs. matched MinerU block)</summary>
        <div class="val-advanced-body">
          <p style="color:#9aa0ac; font-size:0.8rem; margin:10px 0;">
            <span style="color:#4f8ff7">&#9632;</span> pdf2mdtree placeholder bbox &nbsp;
            <span style="color:#3ddc76">&#9632;</span> MinerU block (confident match) &nbsp;
            <span style="color:#f0a83c">&#9632;</span> uncertain match &nbsp;
            <span style="color:#ff6b6b">&#9632;</span> no overlapping MinerU block (missing)
          </p>
          ${{geoImgs}}
        </div>
      </details>`;
    grid.appendChild(card);
  }}
}}

async function load() {{
  // Scorecard is the landing tab — kick it off first so the verdict paints
  // without waiting on the (much larger) full-tree data.json fetch below.
  if (!scoreLoaded) {{ scoreLoaded = true; loadScorecard(); }}
  const jr = await fetch(`/api/jobs/${{JOB_ID}}`);
  const job = await jr.json();
  renderTables(job.tables || []);
  // No stage param on the first load — the server picks the job's latest.
  const sq = CUR_STAGE == null ? '' : `?stage=${{CUR_STAGE}}`;
  const dr = await fetch(`/api/jobs/${{JOB_ID}}/data.json${{sq}}`);
  DATA = await dr.json();
  CUR_STAGE = DATA.stage;
  renderStageSwitch(DATA.stages_available || [CUR_STAGE]);
  pdfTotalPages = DATA.total_pages || null;   // bounds Prev/Next in the Document tab
  try {{
    const vr = await fetch(`/api/jobs/${{JOB_ID}}/validation.json`);
    const validation = await vr.json();
    insertGapMarkers(DATA.files, (validation.content_localized || {{}}).results);
  }} catch (e) {{ /* validation not ready — document view still works without inline markers */ }}
  indexFootnotes(DATA.files);
  // renderTree APPENDS, and load() is no longer a once-per-page call — the stage
  // switcher re-runs it — so the container has to be emptied first or every switch
  // stacks another copy of the whole file tree under the previous one.
  const treeEl = document.getElementById('tree');
  treeEl.innerHTML = '';
  renderTree(DATA.tree, treeEl);
  const readme = Object.keys(DATA.files).find(p => p === 'README.md') || Object.keys(DATA.files)[0];
  if (readme) showFile(readme);
}}
load();
</script>
</body></html>"""


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j["status"] != "done":
        return HTMLResponse(
            f"<body style='background:#0f1117;color:#e6e6e6;font-family:sans-serif;padding:40px'>"
            f"Job {job_id} is still running (status: {j['status']}). "
            f"<a href='/' style='color:#4f8ff7'>Back</a>, refresh in a few seconds.</body>")
    return HTMLResponse(JOB_HTML.format(filename=j["filename"], job_id=job_id))


def main():
    import uvicorn
    global CORPUS_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8811)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-adopt", action="store_true",
                    help="skip picking up completed extractions already on disk")
    ap.add_argument("--adopt-max", type=int, default=14,
                    help="how many of the most recent on-disk extractions to adopt")
    ap.add_argument("--corpus-root", default=None,
                    help="serve the hierarchical corpus view at /corpus over a tree "
                         "produced by run_corpus.py (e.g. out/corpus)")
    args = ap.parse_args()
    if args.corpus_root:
        CORPUS_ROOT = Path(args.corpus_root).resolve()
        CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
        _adopt_corpus_jobs()
        print(f"corpus view: http://{args.host}:{args.port}/corpus   (root {CORPUS_ROOT})")
    elif not args.no_adopt:
        _adopt_disk_jobs(args.adopt_max)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
