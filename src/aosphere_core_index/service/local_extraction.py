"""Extraction monitoring, read from a local `scripts/run_corpus.py` output tree instead of S3.

The Extraction tab has always assumed a GPU fleet publishing progress to S3
(`extraction_monitor.py`): shards, a worker ledger, a retry queue, published viewer.html
pages. None of that exists for a local, sequential `run_corpus.py` run — it is one process
writing one tree on disk, with no workers to be stale, no queue to retry into, and (by
default) no Stage 4/5 AI pass. This module does not invent those concepts; it answers the
question the tab actually needs answered locally: which stage did each document reach, and
what did its scorecard say.

The stage-from-disk inference mirrors `scripts/pipeline_monitor.py` (the standalone local
dashboard `run_corpus.py` already ships) — same files, same rule: a job's stage is whichever
of `01_stage1_extract/tables_manifest.json`, a stage2 report, a stage3 report, `validation.json`
or `scorecard.json` exists. Safe to read while a run is still writing; a poll is just a re-read
of disk, nothing is cached.

Enabled only when ACI_LOCAL_CORPUS_DIR names a directory — never inferred from one merely
existing, so a deployed pod with a stray local corpus on ephemeral disk cannot silently
switch the tab into local mode. Every function here is pure filesystem I/O; nothing imports
boto3 or aosphere_core_index.aws, and nothing here reaches the network.

One exception, isolated on purpose: `ensure_viewer()`/`ensure_inspect()` build the same
self-contained viewer.html / inspect.html the S3 fleet's
`corpus_worker.build_review_artefacts` writes into a finished job dir, by calling those
exact functions (`scripts/push_hybrid_s3.build_viewer` / `build_inspect`) against the
local tree instead of reimplementing them. inspect.html in particular is the whole
Scorecard / Document / MinerU Inspector / Validation / Cross-check / Page Review
dashboard, frozen into one file by `scripts/build_inspect.py` — reusing it rather than
building a second scorecard UI is the entire point of calling it from here. That import
chain reaches PyMuPDF, so it is deferred to inside each function, never at module load —
importing this module must stay cheap even in a serving image with no extraction extras
installed, and a document whose viewer/inspect page cannot be built there just keeps
reading "no viewer" / falls back to the bare scorecard, the same as one that predates
the S3 worker learning to build these.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Same 8-entry vocabulary and order as extraction_monitor.STAGES, so a stage_idx computed here
# means the same thing whether the tab is reading S3 or a local tree.
STAGES = [
    {"key": "queued", "label": "Fetched / queued"},
    {"key": "stage1", "label": "Stage 1 · Extract"},
    {"key": "stage2", "label": "Stage 2 · Tables (MinerU)"},
    {"key": "stage3", "label": "Stage 3 · Combine"},
    {"key": "validate", "label": "Validation"},
    {"key": "gate", "label": "Scorecard gate"},
    {"key": "stage4_ai", "label": "Stage 4 · AI post-processing", "optional": True},
    {"key": "stage5_subchunk", "label": "Stage 5 · Sub-chunks", "optional": True},
]
GATE_INDEX = 5
GATE_STATUS = {"pass": "pass", "review": "review", "fail": "fail", "error": "error"}

# run_corpus.py has no run_id level (that is an S3-only concept, one prefix per GPU launch) —
# a local tree IS one run, so this is the one synthetic id the tab's run-picker ever sees here.
LOCAL_RUN_ID = "local"

# A local run with no live process for this long reads as stalled rather than running — generous
# because a large document can spend 20+ minutes in one MinerU pass on CPU (see the Japan/Jersey
# runs this module was built against: 1233s for one 92-page document).
STALE_AFTER_S = 30 * 60

# Only these locations may be read back through the raw-artifact endpoint. Not every file under
# a job dir is meant to be served to a browser (e.g. mineru_raw/ can hold large intermediate
# PDFs) -- this allowlist is the traversal guard's second half: even a validated in-tree path is
# refused if it is not one of these.
_ARTIFACT_ALLOWED_NAMES = {
    "scorecard.json", "validation.json", "toc_preflight.json", "corpus_meta.json",
    "scorecard_post_ai.json", "validation_post_ai.json", "timings.json",
}
_ARTIFACT_ALLOWED_DIRS = ("01_stage1_extract", "02_stage2_mineru_tables", "03_stage3_final",
                          "04_stage4_ai", "05_subchunks")


def configured_root() -> Path | None:
    """The local corpus root, if ACI_LOCAL_CORPUS_DIR names one — else None (S3/"not
    configured" territory, decided by the caller). Never defaulted from run_corpus.py's own
    default output path: local mode is opt-in, not inferred."""
    raw = os.getenv("ACI_LOCAL_CORPUS_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _load(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:                                          # noqa: BLE001
        return None


def _safe_segment(seg: str) -> str:
    """One path component, never a traversal or an absolute path."""
    if not seg or seg in (".", "..") or "/" in seg or "\\" in seg or "\x00" in seg:
        raise ValueError(f"invalid path segment: {seg!r}")
    return seg


def read_progress(root: Path) -> dict | None:
    """The run-level heartbeat run_corpus.py writes (`_progress.json`), or None."""
    return _load(root / "_progress.json")


def discover_jobs(root: Path) -> list[dict[str, Any]]:
    """One row per `<product>/<jurisdiction>__<doc_id>` directory under root."""
    jobs: list[dict[str, Any]] = []
    if not root.is_dir():
        return jobs
    for product_dir in sorted(p for p in root.iterdir()
                              if p.is_dir() and not p.name.startswith("_")):
        for d in sorted(x for x in product_dir.iterdir() if x.is_dir()):
            meta = _load(d / "corpus_meta.json")
            if meta is None:
                continue                                       # not a job dir (or job not yet started)

            sc_p, val_p = d / "scorecard.json", d / "validation.json"
            manifest_p = d / "01_stage1_extract" / "tables_manifest.json"
            stage2_reports = list(d.glob("0*_*mineru*/stage2_report.json"))
            stage3_reports = list(d.glob("0*_*final*/stage3_report.json"))
            sc = _load(sc_p) if sc_p.exists() else None
            pf = _load(d / "toc_preflight.json") if (d / "toc_preflight.json").exists() else None

            # THE FINAL VERDICT, not the extraction gate: scorecard.json is frozen at
            # stage 3 (the --resume marker), but when stage 4/5 ran, that tree is what
            # actually ships. Same rule extraction_monitor._finished_job and
            # run_corpus._final_verdict apply for the identical question on the S3 side —
            # this corpus is read by the same pipeline, so the two must not disagree.
            sc_post = _load(d / "scorecard_post_ai.json")
            post_ai = bool(sc_post and sc_post.get("gate") is not None)
            final = sc_post if post_ai else sc
            # The summary-AI product route (a single vision-model call transcribing the
            # whole PDF) writes ITS extraction straight into 04_stage4_ai/ -- that IS its
            # primary tree, not a repair pass over 03_stage3_final, and it never writes a
            # stage4_report.json by design (see pipeline_monitor._ai_block and
            # hybrid_extract_ui._is_summary_ai for the same check on the other two
            # readers of this exact layout). Reading the directory's mere presence as an
            # incomplete repair pass here mislabelled every one of these documents
            # "Stage 4 never finished" although they scored cleanly stages ago.
            is_summary_ai = (d / "summary_ai_rule.json").exists() or (
                ((sc or {}).get("special_mode") or {}).get("mode") == "ai_transcription")
            stage4_dir = d / "04_stage4_ai"
            stage4_report = (_load(stage4_dir / "stage4_report.json")
                             if stage4_dir.is_dir() and not is_summary_ai else None)
            timings = _load(d / "timings.json") or {}
            ai_steps = {k: v for k, v in (timings.get("steps") or {}).items()
                       if k in ("stage4_ai", "stage5_subchunk")}

            if final is not None:
                gate = final.get("gate", "unknown")
                status = GATE_STATUS.get(gate, "review")
                # Past the gate, working: stage 5 (sub-chunked) if it ran, else stage 4.
                stage_idx = (7 if (d / "05_subchunks").is_dir() else 6) if post_ai else GATE_INDEX
            elif val_p.exists() or stage3_reports:
                stage_idx, gate, status = 4, None, "running"
            elif stage2_reports:
                stage_idx, gate, status = 3, None, "running"
            elif manifest_p.exists():
                stage_idx, gate, status = 2, None, "running"
            else:
                stage_idx, gate, status = 1, None, "running"

            tm = (sc or {}).get("timing") or {}
            stage2_rep = _load(stage2_reports[0]) if stage2_reports else None
            stage3_rep = _load(stage3_reports[0]) if stage3_reports else None
            tables = (stage3_rep or {}).get("tables_total")
            mineru_pages = (stage2_rep or {}).get("tables_attempted")

            mtimes = [d.stat().st_mtime]
            for p in (manifest_p, val_p, sc_p, d / "scorecard_post_ai.json",
                     *stage2_reports, *stage3_reports):
                if p.exists():
                    mtimes.append(p.stat().st_mtime)

            fb = (sc or {}).get("fallback") or {}
            jobs.append({
                "product": meta.get("product", product_dir.name),
                "jurisdiction": meta.get("jurisdiction", ""),
                "doc_id": str(meta.get("doc_id", d.name)),
                "label": d.name,
                "gate": None if final is None else gate,
                "status": status,
                "worst": (final or {}).get("worst_score"),
                "worst_score": (final or {}).get("worst_score"),
                # 5 only once a post-AI scorecard genuinely exists (checked above, not
                # merely asked for); every document that never ran stage 4/5 stays 3.
                "scored_stage": (final or {}).get("scored_stage") or (5 if post_ai else 3),
                # The extraction (stage 1-3) verdict, so a row can show SC1 *and* SC2 —
                # always from the ORIGINAL scorecard.json, which stage 4/5 never rewrite.
                "gate_extraction": None if sc is None else sc.get("gate", "unknown"),
                "worst_extraction": (sc or {}).get("worst_score"),
                "stage_idx": stage_idx,
                "stage_label": STAGES[stage_idx]["label"],
                "tier": "first_pass", "tier_label": "first pass",
                "route": None, "fallback": bool(fb.get("triggered")),
                "hard_fail": bool(fb.get("hard_fail")),
                "toc_rescued": bool(pf and pf.get("applied") is True),
                "cloned": False,
                # Only the paid pass ever sets this -- None (not $0.00) on every document
                # that never ran it, which is most of them.
                "cost_usd": ((stage4_report or {}).get("usage") or {}).get("cost_usd"),
                "stage4_status": (None if is_summary_ai
                                  else "ok" if stage4_report is not None
                                  else "incomplete" if stage4_dir.is_dir() else None),
                "stage4_failed_sections": [],
                "seconds": tm.get("seconds"),
                "pages": tm.get("pages"),
                "spp": tm.get("seconds_per_page"),
                # _done_stages first so a real measured duration always overwrites its
                # placeholder, then stage4/5's own timing merged in last -- scorecard.json
                # is frozen before either ever ran, so it has no entry for them at all.
                "steps": {**(tm.get("steps") or {}), **ai_steps},
                "fb_seconds": tm.get("fallback_seconds") or 0,
                "tables": tables, "mineru_pages": mineru_pages,
                "review": [], "error": None, "cause": None,
                "live": status == "running",
                "started": meta.get("started_at") or d.stat().st_mtime,
                "updated": max(mtimes),
            })
    return jobs


def list_runs(root: Path) -> list[dict[str, Any]]:
    """The tab's run picker expects a list; a local tree is always exactly one "run"
    (there is no run_id level in `run_corpus.py`'s output — that is a fleet-launch concept),
    so this returns one synthetic entry, or none when the configured path does not exist."""
    if not root.is_dir():
        return []
    jobs = discover_jobs(root)
    prog = read_progress(root)
    last_activity = max([j["updated"] for j in jobs] + [prog.get("ts", 0.0) if prog else 0.0],
                        default=0.0)
    return [{"run": LOCAL_RUN_ID, "local": True, "workers_reported": 0,
             "documents": len(jobs), "last_activity": last_activity or None}]


def _run_state(jobs: list[dict], prog: dict | None, now: float) -> tuple[str, float | None]:
    """-> (state, quiet_seconds). No shard heartbeat exists locally, only the run-level one
    `run_corpus.py` writes — freshness of THAT is the only live/stalled signal available."""
    if not jobs:
        return "idle", None
    done = sum(1 for j in jobs if j["gate"] is not None)
    if done == len(jobs):
        return "complete", None
    last_beat = (prog or {}).get("ts")
    if last_beat:
        quiet = now - last_beat
        return ("running" if quiet <= STALE_AFTER_S else "stalled"), round(quiet)
    # No heartbeat file at all (an old / manually-run corpus) — infer from the newest mtime
    # instead, so a finished-looking tree does not read as permanently "running".
    quiet = now - max(j["updated"] for j in jobs)
    return ("running" if quiet <= STALE_AFTER_S else "stalled"), round(quiet)


def progress(root: Path, now: float | None = None) -> dict[str, Any]:
    """Aggregate view for the tab's headline tiles — the local equivalent of
    extraction_monitor.summarize(), built from the job list rather than shard summaries."""
    now = time.time() if now is None else now
    jobs = discover_jobs(root)
    prog = read_progress(root)
    state, quiet_seconds = _run_state(jobs, prog, now)

    done = sum(1 for j in jobs if j["gate"] is not None)
    failed = sum(1 for j in jobs if j["gate"] == "error")
    gates: dict[str, int] = {}
    for j in jobs:
        if j["gate"]:
            gates[j["gate"]] = gates.get(j["gate"], 0) + 1

    started_ts = [j["started"] for j in jobs if j.get("started")]
    updated_ts = [j["updated"] for j in jobs if j.get("updated")]
    last_activity = max(updated_ts, default=None)
    elapsed = (max(updated_ts, default=now) - min(started_ts)) if started_ts else 0.0

    recent = sorted(jobs, key=lambda j: j["updated"], reverse=True)[:20]
    recent_rows = [{"product": j["product"], "label": j["label"], "gate": j["gate"],
                    "worst": j["worst"], "seconds": j["seconds"], "error": j["error"],
                    "cloned": False, "cause": None, "review": []} for j in recent]

    pct = round(done / len(jobs) * 100, 1) if jobs else 0.0
    return {
        "local": True,
        "run": LOCAL_RUN_ID,
        "state": state,
        "quiet_seconds": quiet_seconds,
        "last_activity": last_activity,
        "version": {"percent": pct if jobs else None, "docs_done": done,
                    "docs_total": len(jobs) or None, "pages_done": None, "pages_total": None,
                    "resumed_from": 0},
        "done": done, "jobs_total": len(jobs), "percent": pct,
        "pages_total": 0, "pages_done": 0,
        "cloned": 0, "failed": failed,
        "gates": gates, "recent": recent_rows,
        "elapsed_seconds": round(elapsed), "eta_seconds": None, "pages_per_hour": None,
        # No shard/worker concept locally -- a sequential run has one process, not a fleet.
        "shards": [], "shards_live": 0, "superseded_workers": 0,
    }


def jobs_view(root: Path) -> dict[str, Any]:
    """The job list + stage funnel — the local equivalent of extraction_monitor.jobs_view()."""
    jobs = discover_jobs(root)
    funnel = [{"key": st["key"], "label": st["label"], "optional": bool(st.get("optional")),
              "count": sum(1 for j in jobs if j["stage_idx"] >= i)}
             for i, st in enumerate(STAGES)]
    return {"local": True, "run": LOCAL_RUN_ID, "stages": STAGES, "jobs": jobs,
            "funnel": funnel, "total": len(jobs), "running": sum(1 for j in jobs if j["live"]),
            "cost_usd_total": None, "stage4_seconds_total": None, "ledger": bool(jobs)}


def _job_dir(root: Path, product: str, label: str) -> Path:
    d = (root / _safe_segment(product) / _safe_segment(label)).resolve()
    if root.resolve() not in d.parents and d != root.resolve():
        raise ValueError("path escapes the configured local corpus root")
    return d


def _shallow_listing(d: Path, depth: int = 2) -> list[str]:
    """Relative paths of files under d, one or two levels deep — enough to show what stage2's
    per-table structural output (table.html/table.md/status.json under tables/<id>/) actually
    holds, without walking a whole extraction tree into one response."""
    out: list[str] = []
    if not d.is_dir():
        return out
    for p in sorted(d.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(d)
        if len(rel.parts) > depth:
            continue
        out.append(str(rel))
    return out


def job_detail(root: Path, product: str, label: str) -> dict[str, Any] | None:
    """Everything known about one document, read on demand — scored or not.

    Unlike the S3 side (a finished document only, since a scorecard is the one thing a run
    always eventually writes for it), this also answers for a document still mid-pipeline,
    because a local run is small enough that watching it work is the point."""
    try:
        d = _job_dir(root, product, label)
    except ValueError:
        return None
    meta = _load(d / "corpus_meta.json")
    if meta is None:
        return None

    sc = _load(d / "scorecard.json")
    sc_post = _load(d / "scorecard_post_ai.json")
    post_ai = bool(sc_post and sc_post.get("gate") is not None)
    final = sc_post if post_ai else sc
    val = _load(d / "validation.json")
    # validation_post_ai.json is stage 4/5's own re-check of the tree it left behind —
    # present only when the run wrote one, same optional-file rule as the scorecard.
    val_post = _load(d / "validation_post_ai.json") if post_ai else None
    pf = _load(d / "toc_preflight.json")
    tm = (final or {}).get("timing") or {}
    fb = (final or {}).get("fallback") or {}
    stage_files = {
        "01_stage1_extract": _shallow_listing(d / "01_stage1_extract", depth=1),
        "02_stage2_mineru_tables": _shallow_listing(d / "02_stage2_mineru_tables" / "tables",
                                                    depth=2),
        "03_stage3_final": _shallow_listing(d / "03_stage3_final", depth=1),
        "04_stage4_ai": _shallow_listing(d / "04_stage4_ai", depth=1),
        "05_subchunks": _shallow_listing(d / "05_subchunks", depth=2),
    }
    return {
        "local": True, "product": product, "label": label,
        "jurisdiction": meta.get("jurisdiction", ""), "doc_id": str(meta.get("doc_id", label)),
        "preflight": pf,
        "gate": (final or {}).get("gate"), "worst_score": (final or {}).get("worst_score"),
        "dimensions": (final or {}).get("dimensions") or (final or {}).get("checks") or {},
        # 3 unless a post-AI scorecard genuinely exists -- same rule discover_jobs uses,
        # so the funnel/job-list numbers and this drill-down cannot disagree about which
        # tree a document's numbers describe.
        "scored_stage": (final or {}).get("scored_stage") or (5 if post_ai else 3),
        # The extraction gate ALONGSIDE the final one when stage 4/5 ran, so "the AI pass
        # moved this from review to pass" (or the reverse) is a fact this panel states,
        # not one a reader has to reconstruct from two separately-fetched documents.
        "extraction_gate": ({"gate": sc.get("gate"), "worst_score": sc.get("worst_score")}
                           if post_ai and sc else None),
        "validation": val_post if val_post is not None else val,
        "timing": {"seconds": tm.get("seconds"), "steps": tm.get("steps") or {},
                   "pages": tm.get("pages"),
                   "seconds_per_page": tm.get("seconds_per_page"),
                   "fallback_seconds": tm.get("fallback_seconds")},
        "fallback": {"triggered": bool(fb.get("triggered")), "reason": fb.get("reason"),
                    "adopted_tier": fb.get("adopted_tier"), "chain": fb.get("chain") or [],
                    "hard_fail": bool(fb.get("hard_fail")),
                    "hard_fail_reason": fb.get("hard_fail_reason")},
        # Which structural artifacts exist, so a caller (or a future validator UI) knows what
        # it can fetch via read_artifact() -- including, per table, the HTML with its
        # rowspan/colspan intact, not just the flattened markdown.
        "stage_files": stage_files,
    }


def read_artifact(root: Path, product: str, label: str, rel_path: str) -> tuple[bytes, str] | None:
    """One structural artifact's raw bytes -- table.html/table.md/status.json under stage2,
    the stage3 markdown tree, scorecard.json -- exactly as MinerU/run_corpus wrote it, no
    flattening. None on anything outside the allowlisted stage dirs/files, a missing file, or
    a path that resolves outside this one job's directory."""
    try:
        d = _job_dir(root, product, label)
    except ValueError:
        return None
    try:
        rel = Path(_safe_segment_path(rel_path))
    except ValueError:
        return None
    top = rel.parts[0] if rel.parts else ""
    if top not in _ARTIFACT_ALLOWED_DIRS and str(rel) not in _ARTIFACT_ALLOWED_NAMES:
        return None
    target = (d / rel).resolve()
    if d.resolve() not in target.parents and target != d.resolve():
        return None
    if not target.is_file():
        return None
    ext = target.suffix.lower()
    content_type = {".json": "application/json", ".html": "text/html; charset=utf-8",
                   ".md": "text/markdown; charset=utf-8"}.get(ext, "text/plain; charset=utf-8")
    return target.read_bytes(), content_type


def _safe_segment_path(rel_path: str) -> str:
    """A relative path with no traversal component anywhere in it."""
    if not rel_path or rel_path.startswith("/") or "\x00" in rel_path:
        raise ValueError("invalid path")
    parts = Path(rel_path).parts
    if not parts or any(p in (".", "..") for p in parts):
        raise ValueError("invalid path")
    return rel_path


# The local trees worth offering in the viewer's stage switcher -- every stage that
# is a walkable tree of .md files, same set as hybrid_extract_ui.py's own STAGE_DIRS,
# minus Stage 1: raw pdf2mdtree output before MinerU's tables are folded in, not a
# tree a reviewer asked to compare against. Every document with a Stage 1 tree also
# has Stage 3 or later (confirmed against the local corpus), so dropping it here
# loses no document's only tree.
# 02_stage2_mineru_tables is deliberately not one of them either: it holds per-table
# fragments (tables/<id>/table.html), not a walkable section tree, so build_viewer
# (which walks a tree of .md files) has nothing to build FROM there. Its content is
# already inlined into 03_stage3_final's markdown by hybrid_extract.run_stage3, which
# is why opening the viewer already shows MinerU's tables (rowspan/colspan intact,
# embedded as raw HTML) without a separate tab.
#
# 4 and 5 were missing here for a while after AI post-processing/sub-chunking were
# added -- ensure_viewer's subtitle ("stages N/N/N") and its stage dict both come
# from this SAME mapping, so a document that had genuinely been through Stage 4 and
# 5 still only ever offered Stage 1/3 in the switcher, silently. build_viewer's
# `stages` mechanism is generic (it just walks whatever directories it is given),
# so no other change is needed to make 4/5 show up.
_VIEWER_STAGE_DIRS = {3: "03_stage3_final", 4: "04_stage4_ai", 5: "05_subchunks"}


def viewer_inputs_available(root: Path, product: str, label: str) -> bool:
    """Cheap existence check only — called once per scored document on every manifest()
    read, so it must never open the PDF or the template. True means ensure_viewer() has
    something to build FROM, not that anything has been built yet."""
    try:
        d = _job_dir(root, product, label)
    except ValueError:
        return False
    if (d / "viewer.html").is_file():
        return True
    if not (d / "source.pdf").is_file():
        return False
    return any((d / name).is_dir() for name in _VIEWER_STAGE_DIRS.values())


def ensure_viewer(root: Path, product: str, label: str) -> bool:
    """Build `<job>/viewer.html` if it is missing and the inputs exist for it.

    True means a viewer is on disk by the time this returns — freshly built, or already
    there from a previous call — False means there is nothing to build from, or the build
    failed for any reason. Best-effort, exactly like
    corpus_worker.build_review_artefacts: a document with no viewer is still a
    successfully extracted document, never a request that should 500."""
    try:
        d = _job_dir(root, product, label)
    except ValueError:
        return False
    out = d / "viewer.html"
    if out.is_file():
        return True
    pdf = d / "source.pdf"
    stages = {n: d / name for n, name in _VIEWER_STAGE_DIRS.items() if (d / name).is_dir()}
    if not pdf.is_file() or not stages:
        return False
    meta = _load(d / "corpus_meta.json") or {}
    jurisdiction = meta.get("jurisdiction", "")
    title = f"{meta.get('product', product)} — {jurisdiction}" if jurisdiction else product
    subtitle = "local extraction run — pdf2mdtree + MinerU"
    if len(stages) > 1:
        subtitle += f" · stages {'/'.join(str(n) for n in sorted(stages))}"
    try:
        _ph = _import_push_hybrid_s3()
        html = _ph.build_viewer(stages[max(stages)], pdf, title, subtitle,
                                stages=stages if len(stages) > 1 else None,
                                current_stage=max(stages))
        out.write_text(html, encoding="utf-8")
        return True
    except Exception:                                            # noqa: BLE001
        return False


def _import_push_hybrid_s3():
    """Deferred: this reaches PyMuPDF via push_hybrid_s3 -> hybrid_extract, which must
    never load merely from importing local_extraction — see the module docstring."""
    repo_scripts = Path(__file__).resolve().parents[3] / "scripts"
    if str(repo_scripts) not in sys.path:
        sys.path.insert(0, str(repo_scripts))
    import push_hybrid_s3 as _ph                                 # noqa: E402
    return _ph


def inspect_inputs_available(root: Path, product: str, label: str) -> bool:
    """Cheap existence check for ensure_inspect() — same gate as the viewer: the six-tab
    page's Document/MinerU-Inspector tabs need a stage tree, and its embedded PDF needs
    source.pdf. A job only ever reaches the manifest once it is scored (see
    LocalRunDocStore.manifest), so scorecard.json itself is not checked here again."""
    return viewer_inputs_available(root, product, label)


def ensure_inspect(root: Path, product: str, label: str) -> bool:
    """Build `<job>/inspect.html` if missing — the SAME six-tab dashboard page
    (Scorecard / Document / MinerU Inspector / Validation / Cross-check / Page Review)
    scripts/build_inspect.py freezes for the S3 flow, called through
    push_hybrid_s3.build_inspect exactly as corpus_worker.build_review_artefacts does.
    No scorecard UI is written here — this function only decides WHETHER to build and
    where to write; every tab's rendering is build_inspect_page's, unchanged.

    True once a page is on disk (built now or already there); False if there is
    nothing to build from or the build failed — best-effort, same contract as
    ensure_viewer: a document with no inspect page still has its plain scorecard."""
    try:
        d = _job_dir(root, product, label)
    except ValueError:
        return False
    out = d / "inspect.html"
    if out.is_file():
        return True
    if not inspect_inputs_available(root, product, label):
        return False
    meta = _load(d / "corpus_meta.json") or {}
    jurisdiction = meta.get("jurisdiction", "")
    title = f"{meta.get('product', product)} — {jurisdiction}" if jurisdiction else product
    try:
        html = _import_push_hybrid_s3().build_inspect(d, title)
        if not html:
            return False
        out.write_text(html, encoding="utf-8")
        return True
    except Exception:                                            # noqa: BLE001
        return False
