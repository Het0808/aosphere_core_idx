#!/usr/bin/env python3
"""pipeline_monitor.py — live dashboard over a scripts/run_corpus.py run.

Reads the same on-disk state run_corpus.py already writes — no new files,
no in-memory job store, nothing to keep in sync:

  out/corpus/_progress.json                                  overall run heartbeat
  out/corpus/<product>/<jurisdiction>__<doc_id>/corpus_meta.json   job identity (job start)
  .../01_stage1_extract/tables_manifest.json                  Stage 1 done
  .../0*_*mineru*/stage2_report.json                          Stage 2 done
  .../0*_*final*/stage3_report.json                           Stage 3 done
  .../validation.json                                         validation done
  .../scorecard.json                                          gate decided (job done)

A job's stage is inferred purely from which of those files exist, so this is
safe to run alongside a live run_corpus.py process (or point it at a finished
one) — restarting the dashboard loses nothing since there is no server-side
job state, only a re-read of disk.

Usage:
    python scripts/pipeline_monitor.py [--root out/corpus] [--port 8814]
Then open http://127.0.0.1:8814/
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# The trace graph, shared with the Core Index UI. Imported from the package rather
# than copied, which is the whole point of it living there.
from aosphere_core_index.service import summary_trace_graph  # noqa: E402
from aosphere_core_index.service import trace_graph  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent / "out" / "corpus"

STAGES = [
    {"key": "meta", "label": "Fetched / queued"},
    {"key": "stage1", "label": "Stage 1 · Extract"},
    {"key": "stage2", "label": "Stage 2 · Tables (MinerU)"},
    {"key": "stage3", "label": "Stage 3 · Combine"},
    {"key": "validate", "label": "Validation"},
    {"key": "gate", "label": "Scorecard gate"},
    # Opt-in and paid. Marked optional so a funnel row reading 0 says "nobody asked for
    # it", not "the pipeline stalled here" -- extraction ENDS at the gate.
    {"key": "stage4_ai", "label": "Stage 4 \u00b7 AI post-processing", "optional": True},
    {"key": "stage5_subchunk", "label": "Stage 5 \u00b7 Sub-chunks", "optional": True},
]

# A ROUTE that does not walk the spine does not get the spine's dots. The MRAM summaries
# are extracted by one AI call (product_rules.AI_PIPELINE): there is no Stage 1, no MinerU
# and no Stage 3, so painting those dots -- in any colour, including "skipped" -- describes
# a journey this document did not take. Three nodes is the whole path: the file arrives, the
# model reads it, the scorecard judges it.
#
# Keyed by the `route` each job reports, so a future route adds a list here and nothing
# else. The spine stays the default for everything that has no route of its own.
SUMMARY_AI_STAGES = [
    {"key": "meta", "label": "Fetched / queued"},
    {"key": "summary_ai", "label": "AI processing \u00b7 MRAM summary"},
    {"key": "gate", "label": "Scorecard gate"},
]
STAGE_SETS = {"summary_ai": SUMMARY_AI_STAGES}

GATE_STATUS = {"pass": "good", "review": "review", "fail": "fail", "unknown": "review"}

def _ai_block(job: Path, reports: list[Path]) -> dict:
    """What stages 4 and 5 did to this document -- or why they did nothing.

    Three outcomes, and the screen must not blur them: the AI RAN (model, tokens, money),
    it was DISABLED for this run (asked for, gated off, nothing sent and nothing spent), or
    it was never asked for at all. "Disabled" is only distinguishable from "never asked"
    because the disabled path still records a zero into timings.json -- that zero IS the
    evidence, so read presence of the key, never its truthiness.
    """
    steps = {}
    tp = job / "timings.json"
    if tp.exists():
        try:
            steps = (json.loads(tp.read_text()).get("steps") or {})
        except (OSError, ValueError):
            steps = {}
    ai_steps = {k: v for k, v in steps.items()
                if k in ("stage4_ai", "stage5_subchunk", "ai_total")}
    asked = "stage4_ai" in steps

    rep = _load(reports[0]) if reports else None
    if rep is None:
        # The summary-AI route (scripts/summary_ai_extract.py) never writes a
        # stage4_report.json at all -- it is not stage 4 of the normal pipeline, it
        # REPLACES stages 1-3 entirely, and it prices its own call into report.json /
        # scorecard.json's timing block instead. Without this, every summary document
        # showed "$0.00 / gated off", which reads as free when it demonstrably was not:
        # the run log for this same job printed a real dollar figure for it.
        srep = _load(job / "report.json")
        if srep is not None:
            return {"ran": True, "disabled": False, "model": srep.get("model"),
                    "mode": "summary_ai",
                    "cost_usd": round(float(srep.get("cost_usd") or 0.0), 4),
                    "tokens": int(srep.get("tokens_in") or 0) + int(srep.get("tokens_out") or 0),
                    "calls": 1, "seconds": srep.get("seconds"), "stage5_seconds": None,
                    "subchunks": 0, "sections": 0, "accepted": 0, "steps": ai_steps}
        return {"ran": False, "disabled": asked, "model": None, "mode": None,
                "cost_usd": 0.0, "tokens": 0, "seconds": ai_steps.get("stage4_ai"),
                "stage5_seconds": None, "subchunks": 0, "sections": 0, "steps": ai_steps}

    usage = rep.get("usage") or {}
    sub = rep.get("subchunk") or {}
    # Count off disk rather than trusting the report: the sub-chunks are the deliverable,
    # and a report written by an older build carries no subchunk block at all.
    files = [q for q in (job / "05_subchunks").rglob("*.md")] if (job / "05_subchunks").is_dir() else []
    return {
        "ran": True,
        "disabled": False,
        "model": rep.get("model"),
        "mode": rep.get("mode"),
        "cost_usd": round(float(usage.get("cost_usd") or 0.0), 4),
        "tokens": int(usage.get("total_tokens") or 0),
        "calls": len(rep.get("sections") or []),
        # None, not 0 -- a run that predates the timing code took an unknown time, and
        # printing "0s" for eight minutes of Bedrock calls would be a lie.
        "seconds": ai_steps.get("stage4_ai"),
        "stage5_seconds": ai_steps.get("stage5_subchunk"),
        "subchunks": max(len(files) - len(sub.get("sections") or []), 0) or len(files),
        "sections": rep.get("sections_total") or 0,
        "accepted": rep.get("sections_accepted") or 0,
        "steps": ai_steps,
    }



# ---- which PASS a job is on --------------------------------------------------
# A fallback tier RE-RUNS stages 1-3 into the same directories, so the file-existence
# model above cannot tell a first pass from a rescue: a document in toc_rescue looks
# like a fresh job that has inexplicably gone BACKWARDS from stage 4 to stage 2.
# These are the signals that can tell them apart.
#
# NOT a signal: the `fallback/` directory. It holds source_repaired.pdf written by the
# PRE-FLIGHT outline repair, which runs on nearly every document and has nothing to do
# with the fallback chain -- keying off it would mark the whole corpus "rescued".
TIER_LABEL = {
    "first_pass": "first pass",
    "toc_rescue": "\u27f2 TOC rescue",
    "mineru_full": "\u27f2 MinerU full",
    "stage1": "\u27f2 fallback tried",
    # Not a pass through the spine and not a fallback from one -- a different route
    # altogether, chosen before Stage 1 by a product rule. Named for what it IS, because
    # "first pass" would say this document took the ordinary path and any of the fallback
    # labels would say something failed. Neither is true.
    "summary_ai": "\u2605 special pass \u2014 MRAM summary",
}
# fallback_chain emits "mineru_full" on the short-document path and "mineru_fallback"
# on the normal escalation path. Same tier, so both are recognised and normalised to one
# canonical key -- otherwise a document live in the second spelling renders as a first
# pass, which is the exact blind spot this column exists to remove.
# "summary_ai" is the step name run_corpus._step ticks for the AI route, and it is the
# only live signal that route has -- every other file this monitor reads is written after
# the fact, so without it a two-minute call is invisible.
LIVE_TIERS = ("toc_rescue", "mineru_full", "mineru_fallback", "summary_ai")
TIER_ALIASES = {"mineru_fallback": "mineru_full"}

# Stages 4 and 5 leave NOTHING on disk until they finish: stage4_report.json is written
# after the last section, and the timings entry with it. Every other signal this monitor
# reads is a completion artifact, so a document part-way through a twenty-minute AI pass
# was indistinguishable from one that had stopped at the gate -- it sat on "Scorecard
# gate" for the whole run, and the trace graph pulsed nothing, because `live` is
# `status == "running"` and a scored document's status is its gate verdict.
#
# ai_postprocess._progress already writes a heartbeat into the job's own progress.json,
# explicitly so a UI could see stage 4 in flight ("so the UI picks stage 4 up without a
# server change"). Nothing was reading it here.
AI_LIVE_STAGES = {"stage4_ai": 6, "stage5_subchunk": 7}


def _ai_live(job: Path) -> str | None:
    """The AI stage this job has IN FLIGHT, per its own progress.json, or None.

    Read by KEY, not by truthiness of a duration: a stage that has recorded seconds has
    finished, which is the case `_ai_block` already covers.
    """
    prog = _load(job / "progress.json") or {}
    stage = prog.get("stage")
    if prog.get("status") == "running" and stage in AI_LIVE_STAGES:
        return stage
    return None


def _live_stage(job: Path) -> str | None:
    """The stage this job is in RIGHT NOW, per its own progress.json, or None.

    Named the way the WORKERS name it ("toc_preflight", "stage2_mineru", "stage3"), which
    is exactly what trace_graph's EXSTAGE_NODE maps -- so the graph resolves it to a node
    without this monitor knowing anything about the graph.

    `_ai_live` reads the same file and cannot stand in for this: it answers the narrower
    question "is one of the two AI stages in flight", so for a document sitting in stage 2
    it correctly returns None. The graph's `running_step || stage` then had no second
    operand to fall back to, and a document could run for forty minutes with nothing on
    the trace lit at all.
    """
    prog = _load(job / "progress.json") or {}
    return prog.get("stage") if prog.get("status") == "running" else None


def _done_stages(job: Path) -> dict[str, None]:
    """{stage: None} for every stage this job has FINISHED, per its own progress.json.

    The values are deliberately null, not a duration: mid-run there is no per-stage timing
    on disk (timings.json is written at the end), and inventing 0 would put a confident
    "0s" on the trace under every completed node. The graph only needs the KEY to paint a
    node travelled -- `ranOf` tests presence, `secOf` ignores a non-number -- so this says
    "it ran" and stays silent on how long, which is exactly what is known.

    Merged UNDER the scorecard's real timings by the caller, so a finished document is
    unaffected: its numbers win and always did.
    """
    prog = _load(job / "progress.json") or {}
    return {s: None for s in (prog.get("done") or []) if isinstance(s, str)}


def _is_summary_ai(d: Path, sc: dict | None) -> bool:
    """Was this document extracted by the summary AI pipeline?

    Two witnesses, either sufficient: the rule's own marker, written before the call, and
    the scorecard's special_mode, written after it. The marker alone would miss a job
    extracted by the CLI rather than by a corpus run; special_mode alone would miss one
    still in flight -- which is the case this whole flag exists to show.
    """
    if (d / "summary_ai_rule.json").exists():
        return True
    return ((sc or {}).get("special_mode") or {}).get("mode") == "ai_transcription"


def _fallback_state(d: Path, sc: dict | None, prog: dict | None, product: str) -> dict:
    """Which pass this job is on -- and, once scored, which tier's tree was adopted.

    Three sources, in descending order of authority:

      1. the run heartbeat (`_progress.json`) names the tier running RIGHT NOW, because
         run_corpus._step ticks each tier as it is entered. Authoritative but momentary,
         and only ever about the single document in flight.
      2. `scorecard["fallback"]` carries the whole decision once the document is scored:
         what triggered it, every tier tried, and which one won.
      3. markers left on disk catch the rest -- a run that was killed mid-tier, or a
         document the heartbeat has already moved past: rescued_by_toc.json (tier 2 was
         adopted), mineru_full_attempt/ or hybrid_attempt/ (tier 3 ran and was undone).
    """
    st = {"tier": "first_pass", "live": False, "triggered": False,
          "adopted_tier": None, "reason": None, "chain": []}

    prog = prog or {}
    # ASKED FIRST, and it is not a fallback question. The route is settled before Stage 1
    # runs, so it holds whether the document is queued, mid-call or scored -- and the
    # answer must not depend on the heartbeat, which only ever names the one document in
    # flight. summary_ai_rule.json is written the moment the route is taken; special_mode
    # is the scorecard's own record of it once the document is done.
    if _is_summary_ai(d, sc):
        live = (prog.get("state") == "running" and prog.get("document") == d.name
                and prog.get("product") == product
                and prog.get("stage") == "summary_ai")
        st.update(tier="summary_ai", live=live, triggered=False,
                  reason="product rule: extracted by the summary AI pipeline, not by "
                         "stages 1-3 (see product_rules.AI_PIPELINE)")
        return st

    if (prog.get("state") == "running" and prog.get("document") == d.name
            and prog.get("product") == product and prog.get("stage") in LIVE_TIERS):
        st.update(tier=TIER_ALIASES.get(prog["stage"], prog["stage"]), live=True, triggered=True,
                  reason="this tier is running now (run heartbeat)")
        return st

    fb = (sc or {}).get("fallback") or {}
    if fb.get("triggered"):
        _ad = fb.get("adopted_tier") or "stage1"
        st.update(tier=TIER_ALIASES.get(_ad, _ad), triggered=True,
                  adopted_tier=fb.get("adopted_tier"), reason=fb.get("reason"),
                  chain=fb.get("chain") or [])
        return st

    if (d / "rescued_by_toc.json").exists():
        st.update(tier="toc_rescue", triggered=True,
                  reason="rescued_by_toc.json on disk (tier 2 tree adopted)")
    if (d / "mineru_full_attempt").exists() or (d / "hybrid_attempt").exists():
        st.update(tier="mineru_full", triggered=True,
                  reason="mineru_full_attempt/ on disk (tier 3 ran)")
    return st


def _load(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def read_progress(root: Path) -> dict | None:
    return _load(root / "_progress.json")


def discover_jobs(root: Path) -> list[dict]:
    jobs = []
    if not root.exists():
        return jobs
    # Read once for the whole sweep: it names at most one in-flight document, and
    # re-reading it per job would just be the same file 200 times.
    prog = read_progress(root)
    for product_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for d in sorted(x for x in product_dir.iterdir() if x.is_dir()):
            meta_p = d / "corpus_meta.json"
            if not meta_p.exists():
                continue
            meta = _load(meta_p)
            if meta is None:
                continue

            sc_p = d / "scorecard.json"
            val_p = d / "validation.json"
            manifest_p = d / "01_stage1_extract" / "tables_manifest.json"
            stage2_reports = list(d.glob("0*_*mineru*/stage2_report.json"))
            stage3_reports = list(d.glob("0*_*final*/stage3_report.json"))

            sc = _load(sc_p) if sc_p.exists() else None
            # Stage 4 runs LONG AFTER the scorecard, out of band -- never in the corpus
            # run -- so neither the ledger row nor the scorecard's frozen timing block can
            # know about it. Disk is the only witness, exactly as it is for stages 1-3.
            # THE TOC RESCUE. It happens in the pre-flight, before Stage 2 is paid for, so
            # no tier runs and the document stays on its first pass -- which is exactly why
            # it went unlabelled: the run records it only as this marker in the job dir.
            pf = _load(d / "toc_preflight.json") if (d / "toc_preflight.json").exists() else None
            toc_rescued = bool(pf and pf.get("applied") is True)
            stage4_reports = list(d.glob("0*_stage4_ai/stage4_report.json"))
            ai = _ai_block(d, stage4_reports)

            route = "summary_ai" if _is_summary_ai(d, sc) else None

            if route == "summary_ai":
                # Three nodes, so three answers. The middle one is where the document sits
                # for the whole call, which is the only state the spine's ladder could not
                # express: it has no file to key off until the transcript lands.
                if sc is not None:
                    stage_idx = 2
                    gate = sc.get("gate", "unknown")
                    status = GATE_STATUS.get(gate, "review")
                else:
                    stage_idx, gate, status = 1, None, "running"
            elif sc is not None:
                stage_idx = 5
                gate = sc.get("gate", "unknown")
                status = GATE_STATUS.get(gate, "review")
            elif val_p.exists() or stage3_reports:
                stage_idx, gate, status = 4, None, "running"
            elif stage2_reports:
                stage_idx, gate, status = 3, None, "running"
            elif manifest_p.exists():
                stage_idx, gate, status = 2, None, "running"
            else:
                stage_idx, gate, status = 1, None, "running"

            # A DISABLED run is not a run: it spent nothing and changed no file, so it
            # must not advance the document past the gate.
            #
            # Guarded on the route: stages 4 and 5 are indices into the SPINE's list, and
            # this route's list has three entries. Applying them here would index past the
            # end of it -- and there is nothing to apply anyway, since the AI pass on this
            # route IS the extraction, not a paid pass over a tree it produced.
            if ai["ran"] and route is None:
                stage_idx = 7 if ai["subchunks"] else 6

            # RUNNING NOW wins over the gate verdict. The gate was decided before stage 4
            # started, so a document with an AI stage in flight is not "at the gate" -- it
            # is past it, working. Set last so it overrides both branches above.
            ai_live = _ai_live(d)
            if ai_live and route is None:
                stage_idx, status = AI_LIVE_STAGES[ai_live], "running"

            # The trace graph reads a scorecard-shaped `detail` alongside the job row. It is
            # the SAME graph the Core Index draws (service/trace_graph.py), so it wants the
            # same fields -- and this monitor already has the scorecard open, so supplying
            # them costs one dict rather than an endpoint.
            _tm = (sc or {}).get("timing") or {}
            _fb = (sc or {}).get("fallback") or {}
            detail = None if sc is None else {
                "preflight": pf,
                "gate": sc.get("gate"), "worst_score": sc.get("worst_score"),
                "dimensions": sc.get("dimensions") or sc.get("checks") or {},
                "gating_dimensions": sc.get("gating_dimensions"),
                "special_mode": sc.get("special_mode"),
                "timing": {"seconds": _tm.get("seconds"),
                           "steps": _tm.get("steps") or {},
                           "pages": _tm.get("pages")},
                "fallback": {"triggered": bool(_fb.get("triggered")),
                             "reason": _fb.get("reason"),
                             "adopted_tier": _fb.get("adopted_tier"),
                             "chain": _fb.get("chain") or [],
                             "accepted": _fb.get("accepted"),
                             "hard_fail": bool(_fb.get("hard_fail")),
                             "hard_fail_reason": _fb.get("hard_fail_reason")},
            }

            fbst = _fallback_state(d, sc, prog, product_dir.name)
            # What the document cost, straight off the scorecard (run_corpus.timing_block).
            # "Started"/"Updated" are file mtimes and cannot answer this: a job that sat
            # queued behind a 20-minute MinerU pass has an early mtime and a short run.
            tm = (sc or {}).get("timing") or {}

            mtimes = [meta_p.stat().st_mtime]
            for p in (manifest_p, val_p, sc_p, *stage2_reports, *stage3_reports):
                if p.exists():
                    mtimes.append(p.stat().st_mtime)

            jobs.append({
                "job_id": f"{product_dir.name}/{d.name}",
                "product": meta.get("product", product_dir.name),
                "jurisdiction": meta.get("jurisdiction", ""),
                "doc_id": meta.get("doc_id", d.name),
                "stage_idx": stage_idx,
                "status": status,
                "gate": gate,
                "worst_score": (sc or {}).get("worst_score"),
                # Which stage list this document's dots come from. None = the spine.
                "route": route,
                # For the summary route's own graph. Cost has no equivalent on any other
                # route -- it is the only one that spends money -- so it is carried here
                # rather than squeezed into the shared timing shape.
                "cost_usd": tm.get("cost_usd"),
                "ai_model": tm.get("model"),
                "tokens_in": tm.get("tokens_in"),
                "tokens_out": tm.get("tokens_out"),
                "weakest": (sc or {}).get("weakest_dimension"),
                "pages": tm.get("pages"),
                "tier": fbst["tier"],
                "tier_label": TIER_LABEL.get(fbst["tier"], fbst["tier"]),
                "tier_live": fbst["live"],
                "fallback": fbst["triggered"],
                "adopted_tier": fbst["adopted_tier"],
                "tier_reason": fbst["reason"],
                "toc_rescued": toc_rescued,
                "detail": detail,
                # Which step is in flight, for the trace's "running now" state. Named the
                # way the workers name it (EXSTAGE_NODE maps it), so the graph resolves it
                # to a node without this monitor knowing anything about the graph.
                # EXSTAGE_NODE maps "stage4_ai"/"stage5_subchunk" to the ai/subchunk
                # nodes already -- the graph has always been able to draw them running,
                # it was never told which one was.
                "running_step": ("summary_ai" if (route and fbst["live"])
                                 else ai_live or (fbst["tier"] if fbst["live"] else None)),
                # Where the document is RIGHT NOW. exNodeStates resolves its running node
                # from `running_step || stage`, and its CRASHED node from `stage` alone --
                # but this monitor never sent a `stage` key, so both fell through to '' and
                # neither lit. running_step only ever names an AI stage or a fallback tier,
                # which is why the graph pulsed during stage 4 and sat dark through 1-3.
                "stage": _live_stage(d),
                # exNodeStates reads `steps` and `worst`; this monitor calls the same two
                # things timing_steps and worst_score. Aliased rather than renamed, because
                # the existing columns and their tooltips read the originals.
                # MERGED, not `or`: the scorecard's timing block is frozen at gate time and
                # can never hold the AI steps, so `A or B` kept A and silently dropped every
                # stage-4/5 entry -- the two nodes the graph would then never light.
                # _done_stages FIRST so a real measured duration always overwrites its
                # placeholder null — a finished document's trace is exactly as it was.
                "steps": {**_done_stages(d), **(_tm.get("steps") or {}), **ai["steps"]},
                "worst": sc.get("worst_score") if sc else None,
                "hard_fail": bool(_fb.get("hard_fail")),
                "accepted": _fb.get("accepted"),
                "live": status == "running",
                "tier_chain": fbst["chain"],
                "seconds": tm.get("seconds"),
                "timing_steps": {**(tm.get("steps") or {}), **ai["steps"]},
                "ai": ai,
                "timing_pages": tm.get("pages"),
                "seconds_per_page": tm.get("seconds_per_page"),
                "seconds_per_crop_page": tm.get("seconds_per_crop_page"),
                "fallback_seconds": tm.get("fallback_seconds") or 0,
                # a reconstructed window must not read as a measured one
                "timing_backfilled": bool(tm.get("backfilled")),
                "started": meta_p.stat().st_mtime,
                "updated": max(mtimes),
            })
    return jobs


def build_app(root: Path) -> FastAPI:
    app = FastAPI(title="Extraction pipeline monitor")

    @app.middleware("http")
    async def _no_cache(request, call_next):
        """Never let a browser cache this page.

        The response carried no Cache-Control, ETag or Last-Modified, so browsers fell
        back to heuristic caching and kept serving the HTML from before a restart. The
        JSON kept updating -- the page polls every 3s -- but the cached markup had no
        column to render new fields into, so they were dropped silently. A stale
        dashboard that looks live is worse than one that is obviously down.
        """
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/status")
    def status() -> JSONResponse:
        jobs = discover_jobs(root)
        jobs.sort(key=lambda j: j["updated"], reverse=True)
        return JSONResponse({
            "root": str(root),
            "now": time.time(),
            "progress": read_progress(root),
            "stages": STAGES,
            "stage_sets": STAGE_SETS,
            "jobs": jobs,
        })

    return app


_PAGE_TMPL = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Extraction Pipeline Monitor</title>
<style>
:root {
  color-scheme: light;
  --surface-1: #fcfcfb; --surface-2: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
  --gridline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --seq-400: #3987e5;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
  /* The trace graph is shared with the Core Index and names its own tokens, because the
     two palettes are unrelated -- and this one has a dark mode the graph knows nothing
     about. Mapped here and again below, so the graph follows the theme for free. */
  --prog: var(--series-1); --prog-ink: #1b4f8f;
  --g-ink: var(--text-primary); --g-muted: var(--text-muted);
  --g-surface: var(--surface-1); --g-line: var(--gridline);
  --g-line-strong: var(--text-secondary); --g-faint: var(--baseline);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface-1: #1a1a19; --surface-2: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --seq-400: #3987e5;
    --good: #0ca30c; --warning: #fab219; --critical: #e66767;
    --prog: var(--series-1); --prog-ink: #8fbdf0;
    --g-ink: var(--text-primary); --g-muted: var(--text-muted);
    --g-surface: var(--surface-1); --g-line: var(--gridline);
    --g-line-strong: var(--text-secondary); --g-faint: var(--baseline);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--surface-2); }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--text-primary); }
.root { padding: 28px clamp(16px, 4vw, 40px) 60px; max-width: 1280px; margin: 0 auto; }
.hdr { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 22px; flex-wrap: wrap; }
.hdr h1 { font-size: 20px; font-weight: 700; margin: 0 0 4px; }
.hdr .sub { font-size: 13px; color: var(--text-secondary); }
.run-badge { display: inline-flex; align-items: center; gap: 7px; padding: 6px 12px; border-radius: 999px;
  background: color-mix(in oklab, var(--series-1) 14%, var(--surface-1)); border: 1px solid var(--border); font-size: 13px; font-weight: 600; }
.run-badge.idle { background: var(--surface-1); color: var(--text-muted); }
.run-badge .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--series-1); animation: pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
.hdr-right { text-align: right; font-size: 12px; color: var(--text-muted); }
.tiles { display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; margin-bottom: 22px; }
@media (max-width: 1180px) { .tiles { grid-template-columns: repeat(4, 1fr); } }
@media (max-width: 900px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.tile .label { font-size: 11.5px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .03em; margin-bottom: 6px; }
.tile .value { font-size: 26px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
.tile .value.good { color: var(--good); } .tile .value.warn { color: var(--warning); } .tile .value.crit { color: var(--critical); }
.tile .foot { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 20px; }
.card-hd { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 14px; flex-wrap: wrap; gap: 8px; }
.card-hd h2 { font-size: 14.5px; font-weight: 700; margin: 0; }
.card-hd .note { font-size: 12px; color: var(--text-muted); }
.funnel { display: flex; flex-direction: column; gap: 9px; }
.funnel-opt { opacity: .62; }
.opt-tag { margin-left: 6px; font-size: 10px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-muted); border: 1px solid var(--border); border-radius: 3px; padding: 0 4px; }
.ai-off { color: var(--text-muted); font-style: italic; }
.funnel-row { display: grid; grid-template-columns: 170px 1fr 46px; align-items: center; gap: 10px; }
.funnel-row .stage-name { font-size: 12.5px; color: var(--text-secondary); }
.funnel-track { position: relative; height: 18px; background: var(--surface-2); border: 1px solid var(--gridline); border-radius: 5px; overflow: hidden; }
.funnel-fill { position: absolute; inset: 0; width: 0%; border-radius: 4px 0 0 4px; transition: width .4s ease; background: var(--seq-400); }
.funnel-fill.done { background: var(--good); }
.funnel-row .count { font-size: 12.5px; text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
.tbl-wrap { overflow-x: auto; }
table.docs { width: 100%; border-collapse: collapse; font-size: 12.5px; min-width: 760px; }
table.docs th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .03em; color: var(--text-muted); font-weight: 600;
  padding: 0 10px 8px; border-bottom: 1px solid var(--gridline); }
table.docs td { padding: 10px 10px; border-bottom: 1px solid var(--gridline); vertical-align: middle; }
table.docs tr:last-child td { border-bottom: none; }
table.docs .docname { font-weight: 600; color: var(--text-primary); }
table.docs .jur { color: var(--text-muted); font-size: 11.5px; }
/*TRACE_CSS*/
/* The trace opens under the row it belongs to, so a document's path is read in place
   rather than on another screen. */
.trace-row > td { background: var(--surface-2); border-top: 0; padding: 4px 14px 16px; }
.docs tbody tr.has-trace { cursor: pointer; }
.docs tbody tr.has-trace:hover { background: color-mix(in oklab, var(--series-1) 6%, transparent); }
.trace-hint { font-size: 11px; color: var(--text-muted); margin: 0 0 6px 2px; }
.mini-stepper { display: flex; gap: 3px; align-items: center; }
.mini-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--baseline); flex: none; }
.mini-dot.done { background: var(--good); } .mini-dot.active { background: var(--series-1); box-shadow: 0 0 0 3px color-mix(in oklab, var(--series-1) 25%, transparent); }
.mini-dot.crit { background: var(--critical); } .mini-dot.warn { background: var(--warning); }
.badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600; white-space: nowrap; }
.badge .d { width: 6px; height: 6px; border-radius: 50%; }
.badge.running { background: color-mix(in oklab, var(--series-1) 16%, var(--surface-1)); color: var(--series-1); }
.badge.running .d { background: var(--series-1); animation: pulse 1.6s ease-in-out infinite; }
.badge.good { background: color-mix(in oklab, var(--good) 16%, var(--surface-1)); color: var(--good); } .badge.good .d { background: var(--good); }
.badge.review { background: color-mix(in oklab, var(--warning) 22%, var(--surface-1)); color: color-mix(in oklab, var(--warning) 65%, var(--text-primary)); } .badge.review .d { background: var(--warning); }
.badge.fail { background: color-mix(in oklab, var(--critical) 16%, var(--surface-1)); color: var(--critical); } .badge.fail .d { background: var(--critical); }
.badge.tier-first { background: var(--surface-2); color: var(--text-muted); border: 1px solid var(--gridline); }
.badge.tier-first .d { background: var(--baseline); }
.badge.tier-rescue { background: color-mix(in oklab, var(--warning) 20%, var(--surface-1)); color: color-mix(in oklab, var(--warning) 65%, var(--text-primary)); }
.badge.tier-rescue .d { background: var(--warning); }
.badge.tier-mineru { background: color-mix(in oklab, var(--critical) 16%, var(--surface-1)); color: var(--critical); }
.badge.tier-mineru .d { background: var(--critical); }
/* Its own colour on purpose. The warning/critical tints mean "a fallback fired"; the muted
   first-pass tint means "the ordinary path". This route is neither, and borrowing either
   palette would mislabel it at a glance -- which is the only way this column is read. */
.badge.tier-summary { background: color-mix(in oklab, var(--accent, #6aa9ff) 18%, var(--surface-1)); color: color-mix(in oklab, var(--accent, #6aa9ff) 70%, var(--text-primary)); border: 1px solid color-mix(in oklab, var(--accent, #6aa9ff) 35%, var(--gridline)); }
.badge.tier-summary .d { background: var(--accent, #6aa9ff); }
.badge.tier-live .d { animation: pulse 1.6s ease-in-out infinite; }
.tier-note { font-size: 11px; color: var(--text-muted); margin-top: 3px; }
.took { font-variant-numeric: tabular-nums; font-weight: 600; }
.took-sub { font-size: 11px; color: var(--text-muted); font-variant-numeric: tabular-nums; margin-top: 3px; }
.took-live { color: var(--series-1); font-weight: 600; font-variant-numeric: tabular-nums; }
.took-fb { color: var(--warning); }
.elapsed { font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.empty { color: var(--text-muted); font-size: 13px; padding: 20px 0; text-align: center; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 11.5px; color: var(--text-secondary); margin-top: 10px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend .d { width: 8px; height: 8px; border-radius: 50%; }
.footnote { font-size: 12px; color: var(--text-muted); }
code { font-size: 11.5px; background: var(--surface-2); padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>
<div class="root">
  <div class="hdr">
    <div>
      <h1>Extraction Pipeline Monitor</h1>
      <div class="sub">Reading <code id="root-path">out/corpus</code> · refreshes every 3s</div>
    </div>
    <div style="display:flex; align-items:center; gap:14px;">
      <div class="run-badge idle" id="run-badge"><span class="dot"></span> <span id="run-badge-text">checking…</span></div>
      <div class="hdr-right">Last polled<br><b id="last-updated" style="color:var(--text-secondary)">—</b></div>
    </div>
  </div>

  <div class="tiles">
    <div class="tile"><div class="label">Jobs discovered</div><div class="value" id="tile-total">—</div><div class="foot">have started (corpus_meta.json)</div></div>
    <div class="tile"><div class="label">Passed gate</div><div class="value good" id="tile-done">—</div><div class="foot" id="tile-done-pct"></div></div>
    <div class="tile"><div class="label">Running</div><div class="value" id="tile-active" style="color:var(--series-1)">—</div><div class="foot">stage 1–5</div></div>
    <div class="tile"><div class="label">Review</div><div class="value warn" id="tile-review">—</div><div class="foot">gate: review</div></div>
    <div class="tile"><div class="label">Failed gate</div><div class="value crit" id="tile-failed">—</div><div class="foot">gate: fail</div></div>
    <div class="tile"><div class="label">Fell back</div><div class="value" id="tile-fallback" style="color:var(--warning)">—</div><div class="foot" id="tile-fallback-foot">first pass held elsewhere</div></div>
    <div class="tile"><div class="label">Time extracted</div><div class="value" id="tile-time">—</div><div class="foot" id="tile-time-foot">summed from scorecards</div></div>
    <div class="tile"><div class="label">AI spend (stage 4)</div><div class="value" id="tile-ai">—</div><div class="foot" id="tile-ai-foot">opt-in, off by default</div></div>
  </div>

  <div class="card">
    <div class="card-hd"><h2>Pipeline funnel — jobs by stage reached</h2><div class="note">from files actually on disk</div></div>
    <div class="funnel" id="funnel"></div>
  </div>

  <div class="card">
    <div class="card-hd"><h2>Jobs</h2><div class="note">sorted by most recently updated</div></div>
    <div class="tbl-wrap"><table class="docs">
      <thead><tr><th>Document</th><th>Stage</th><th>Pass</th><th>Took</th><th>AI (4&#183;5)</th><th>Progress</th><th>Status</th><th>Started</th><th>Updated</th></tr></thead>
      <tbody id="docs-body"></tbody>
    </table></div>
    <div id="empty-msg" class="empty" style="display:none">No jobs found yet under this root — run <code>scripts/run_corpus.py</code> against it.</div>
    <div class="legend">
      <span><span class="d" style="background:var(--good)"></span>gate: pass</span>
      <span><span class="d" style="background:var(--series-1)"></span>running</span>
      <span><span class="d" style="background:var(--warning)"></span>gate: review</span>
      <span><span class="d" style="background:var(--critical)"></span>gate: fail</span>
      <span><span class="d" style="background:var(--baseline)"></span>first pass — no fallback needed</span>
      <span><span class="d" style="background:var(--accent,#6aa9ff)"></span>special pass — MRAM summary (AI route, no stage 1-3)</span>
      <span><span class="d" style="background:var(--warning)"></span>&#10226; TOC rescue (tier 2)</span>
      <span><span class="d" style="background:var(--critical)"></span>&#10226; MinerU full re-parse (tier 3)</span>
    </div>
  </div>

  <div class="footnote">Embedding / indexing / reranking are not tracked here — they run in a separate step (<code>embeddings/*</code>, <code>aci reembed</code> / <code>reindex</code>) after these jobs pass the gate.</div>
</div>

<script>
/*TRACE_JS*/
function fmtAgo(ts, now) {
  if (!ts) return '—';
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return s + 's ago';
  const m = Math.round(s/60);
  if (m < 60) return m + 'm ago';
  const h = Math.round(m/60);
  if (h < 48) return h + 'h ago';
  return Math.round(h/24) + 'd ago';
}
function fmtDur(s) {
  if (s === null || s === undefined) return '—';
  // Sub-second steps are real and worth showing: Stage 3 splices in 0.1s and Stage 5
  // splits sub-chunks in 0.3s. Rounding first reported both as "0s", which reads as
  // "did nothing" rather than "was fast".
  if (s > 0 && s < 1) return (Math.round(s * 10) / 10) + 's';
  s = Math.round(s);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
// Three outcomes, three different cells -- ran / gated off / never asked. Collapsing the
// last two into one dash is what makes a disabled run look like a broken one.
// The shared trace graph asks its host for exactly one helper (see trace_graph.py).
const exDur = fmtDur;

function aiCell(j) {
  const ai = j.ai || {};
  if (ai.ran) {
    const model = (ai.model || '').replace(/^eu\./, '').replace(/^anthropic\.claude-/, '')
                                  .replace(/-v1:0$/, '');
    const t = ai.seconds != null ? fmtDur(ai.seconds) : 'time not recorded';
    const tip = [
      `model: ${ai.model}`, `mode: ${ai.mode}`,
      `${ai.calls || 0} calls · ${(ai.tokens || 0).toLocaleString()} tokens`,
      `stage 4: ${t}`,
      ai.stage5_seconds != null ? `stage 5: ${fmtDur(ai.stage5_seconds)}` : null,
      `${ai.accepted || 0}/${ai.sections || 0} sections accepted`,
      `${ai.subchunks || 0} sub-chunks written`,
    ].filter(Boolean).join('\n').replace(/"/g, '&quot;');
    return `<td title="${tip}"><span class="took">$${(ai.cost_usd || 0).toFixed(4)}</span>` +
           `<div class="took-sub">${model} · ${ai.subchunks || 0} sub</div></td>`;
  }
  if (ai.disabled) {
    return `<td title="stage 4 was asked for and the feature flag refused it — nothing was sent to Bedrock and nothing was spent"><span class="ai-off">gated off</span><div class="took-sub">$0.00</div></td>`;
  }
  return `<td title="stage 4 was never asked for on this document"><span style="color:var(--text-muted)">—</span></td>`;
}

function fmtClock(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toTimeString().slice(0,8);
}

async function poll() {
  let data;
  try {
    const res = await fetch('/api/status');
    data = await res.json();
  } catch (e) { return; }

  document.getElementById('root-path').textContent = data.root;
  document.getElementById('last-updated').textContent = new Date(data.now * 1000).toTimeString().slice(0,8);

  const badge = document.getElementById('run-badge');
  const badgeText = document.getElementById('run-badge-text');
  if (data.progress && data.progress.state === 'running') {
    badge.className = 'run-badge';
    const doc = data.progress.document ? ' — ' + data.progress.document : '';
    badgeText.textContent = `Run in progress (${data.progress.done||0}/${data.progress.total||'?'})${doc}`;
  } else if (data.progress && data.progress.state === 'finished') {
    badge.className = 'run-badge idle';
    badgeText.textContent = `Last run finished (${data.progress.done||0}/${data.progress.total||'?'})`;
  } else {
    badge.className = 'run-badge idle';
    badgeText.textContent = 'No _progress.json found';
  }

  const jobs = data.jobs;
  const total = jobs.length;
  const done = jobs.filter(j => j.gate === 'pass').length;
  const review = jobs.filter(j => j.gate === 'review' || j.gate === 'unknown').length;
  const failed = jobs.filter(j => j.gate === 'fail').length;
  const active = jobs.filter(j => !j.gate).length;
  document.getElementById('tile-total').textContent = total;
  document.getElementById('tile-done').textContent = done;
  document.getElementById('tile-done-pct').textContent = total ? Math.round(done/total*100) + '% of discovered' : '';
  document.getElementById('tile-active').textContent = active;
  document.getElementById('tile-review').textContent = review;
  document.getElementById('tile-failed').textContent = failed;
  const fellBack = jobs.filter(j => j.fallback).length;
  const liveTier = jobs.filter(j => j.tier_live).length;
  document.getElementById('tile-fallback').textContent = fellBack;
  document.getElementById('tile-fallback-foot').textContent =
    liveTier ? liveTier + ' in a tier right now' : (total ? (total - fellBack) + ' on first pass' : '');
  const timed = jobs.filter(j => j.seconds != null);
  const totalSecs = timed.reduce((a, j) => a + j.seconds, 0);
  const totalPages = timed.reduce((a, j) => a + (j.timing_pages || 0), 0);
  document.getElementById('tile-time').textContent = timed.length ? fmtDur(totalSecs) : '—';
  document.getElementById('tile-time-foot').textContent = timed.length
    ? `${timed.length} scored · ${totalPages || '?'} pages`
    : 'no scorecard carries timing yet';

  // Money is the one number nobody should have to drill for.
  const aiRan = jobs.filter(j => j.ai && j.ai.ran);
  const aiOff = jobs.filter(j => j.ai && j.ai.disabled).length;
  const aiUsd = aiRan.reduce((a, j) => a + (j.ai.cost_usd || 0), 0);
  // In flight spends money it cannot report yet: the model, tokens and cost are written
  // with stage4_report.json at the end. Saying so beats a $0.00 that looks like an idle
  // stage while a run is half way through paying for it.
  const aiNow = jobs.filter(j => j.live && /^stage[45]/.test(j.running_step || '')).length;
  document.getElementById('tile-ai').textContent = aiRan.length ? '$' + aiUsd.toFixed(2) : '$0.00';
  document.getElementById('tile-ai-foot').textContent =
    (aiNow ? `${aiNow} running now` + (aiRan.length ? ' · ' : '') : '') + (aiRan.length
      ? `${aiRan.length} doc${aiRan.length === 1 ? '' : 's'} through AI` +
        (aiOff ? ` · ${aiOff} gated off` : '')
      : (aiNow ? '' : (aiOff ? `${aiOff} gated off — nothing spent` : 'opt-in, off by default')));

  const stages = data.stages;
  const funnelHtml = stages.map((s, idx) => {
    const atOrPast = jobs.filter(j => j.stage_idx >= idx).length;
    const pct = total ? Math.round(atOrPast/total*100) : 0;
    const cls = idx === stages.length - 1 ? 'done' : '';
    // Extraction ENDS at the gate. Stages 4-5 are opt-in, so their row is drawn as a
    // separate, dimmed tail -- otherwise "0 of 97" reads as a pipeline that died.
    const opt = s.optional ? ' funnel-opt' : '';
    const note = s.optional ? '<span class="opt-tag">opt-in</span>' : '';
    return `<div class="funnel-row${opt}"><div class="stage-name">${s.label}${note}</div>
      <div class="funnel-track"><div class="funnel-fill ${cls}" style="width:${pct}%"></div></div>
      <div class="count">${atOrPast}</div></div>`;
  }).join('');
  document.getElementById('funnel').innerHTML = funnelHtml;

  document.getElementById('empty-msg').style.display = total ? 'none' : 'block';
  document.getElementById('docs-body').innerHTML = jobs.map(j => {
    // A document on its own route walks a different set of nodes, so its dots and its
    // stage name must come from THAT list. The funnel above stays the spine's, because a
    // funnel counts documents through the common path and these never enter it.
    const jstages = (j.route && (data.stage_sets || {})[j.route]) || stages;
    const dots = jstages.map((s, idx) => {
      let cls = 'pending';
      // A stage IN FLIGHT is not a verdict. Asked before the gate branches below, or a
      // document running stage 4 after a review gate took the gate's amber and read as a
      // second opinion on a stage that has not finished.
      if (j.live && idx === j.stage_idx) cls = 'active';
      else if (j.gate === 'fail' && idx === j.stage_idx) cls = 'crit';
      else if ((j.gate === 'review' || j.gate === 'unknown') && idx === j.stage_idx) cls = 'warn';
      // A passing gate used to paint EVERY later dot done; with two opt-in stages on the
      // end that claimed an AI run that never happened.
      else if (idx < j.stage_idx || (j.gate === 'pass' && !s.optional)) cls = 'done';
      else if (s.optional && j.ai && j.ai.disabled) cls = 'pending';
      else if (idx === j.stage_idx) cls = 'active';
      return `<span class="mini-dot ${cls}" title="${s.label}"></span>`;
    }).join('');
    let badgeCls = 'running', badgeTxt = 'Running';
    if (j.gate === 'pass') { badgeCls = 'good'; badgeTxt = 'Gate: pass'; }
    else if (j.gate === 'review') { badgeCls = 'review'; badgeTxt = 'Gate: review'; }
    else if (j.gate === 'unknown') { badgeCls = 'review'; badgeTxt = 'Gate: unknown'; }
    else if (j.gate === 'fail') { badgeCls = 'fail'; badgeTxt = 'Gate: fail'; }
    const tierCls = j.tier === 'first_pass' ? 'tier-first'
                  : j.tier === 'summary_ai' ? 'tier-summary'
                  : j.tier === 'toc_rescue' ? 'tier-rescue'
                  : j.tier === 'mineru_full' ? 'tier-mineru' : 'tier-rescue';
    // Once a document is scored, saying WHICH tier won is the whole point: a tier can
    // run, score worse than the first pass and be thrown away, and "MinerU full" alone
    // would read as though its tree is the one on disk.
    let tierNote = '';
    if (j.toc_rescued) tierNote = '\u27f2 TOC rescued';
    // Said plainly, because "special pass" alone does not say what was special about it:
    // the geometry path was not tried and rejected, it was never run.
    if (j.route === 'summary_ai') tierNote = 'product rule \u00b7 no stage 1-3';
    if (j.tier_live) tierNote = 'running now';
    else if (j.fallback && j.adopted_tier === 'stage1') tierNote = 'tried, first pass held';
    else if (j.fallback && j.adopted_tier) tierNote = 'adopted';
    const tierTitle = [j.tier_reason].concat(
      (j.tier_chain || []).map(c => '· ' + c.tier + ': ' + (c.status || (c.adopted ? 'adopted' : 'rejected')))
    ).filter(Boolean).join('\n').replace(/"/g, '&quot;');
    // Finished -> the measured cost. Still running -> wall clock so far, which is the
    // only honest answer: nothing has been scored, so there is no total to report yet.
    let tookCell;
    if (j.seconds != null) {
      const steps = Object.entries(j.timing_steps || {})
        .map(([k, v]) => `${k}: ${fmtDur(v)}`).join('\n');
      const rate = [
        j.timing_pages ? `${j.timing_pages} pages` : null,
        j.seconds_per_page ? `${j.seconds_per_page}s / page` : null,
        j.seconds_per_crop_page ? `${j.seconds_per_crop_page}s / crop-page` : null,
      ].filter(Boolean).join(' · ');
      const tip = [steps, j.fallback_seconds ? `fallback tiers: ${fmtDur(j.fallback_seconds)}` : '',
                   rate, j.timing_backfilled ? '(backfilled — window approximate)' : '']
        .filter(Boolean).join('\n').replace(/"/g, '&quot;');
      const sub = j.fallback_seconds
        ? `<div class="took-sub took-fb">${fmtDur(j.fallback_seconds)} fallback</div>`
        : (j.seconds_per_page ? `<div class="took-sub">${j.seconds_per_page}s/pp</div>` : '');
      tookCell = `<td title="${tip}"><span class="took">${fmtDur(j.seconds)}</span>${sub}</td>`;
    } else if (!j.gate) {
      tookCell = `<td title="elapsed since this job started — not yet scored"><span class="took-live">${fmtDur(data.now - j.started)}</span><div class="took-sub">running</div></td>`;
    } else {
      tookCell = `<td title="this scorecard predates the timing block"><span style="color:var(--text-muted)">—</span></td>`;
    }
    // Keyed by job_id rather than by row index: the table re-sorts on every 3s poll, so an
    // index would reopen the trace of whichever document happened to land in that slot.
    const key = j.job_id, open = TRACE_OPEN.has(key);
    const trace = open
      ? `<tr class="trace-row" data-trace="${esc(key)}"><td colspan="9">
           <div class="trace-hint">Path through the pipeline — blue is the path this document took</div>
           ${exGraphFor(j)}</td></tr>`
      : '';
    return `<tr class="has-trace" data-job="${esc(key)}">
      <td><div class="docname">${j.doc_id}</div><div class="jur">${j.product} · ${j.jurisdiction}</div></td>
      <td>${(jstages[j.stage_idx] || {label: '\u2014'}).label}</td>
      <td title="${tierTitle}"><span class="badge ${tierCls} ${j.tier_live ? 'tier-live' : ''}"><span class="d"></span>${j.tier_label}</span>${tierNote ? `<div class="tier-note">${tierNote}</div>` : ''}</td>
      ${tookCell}
      ${aiCell(j)}
      <td><div class="mini-stepper">${dots}</div></td>
      <td><span class="badge ${badgeCls}"><span class="d"></span>${badgeTxt}</span></td>
      <td class="elapsed">${fmtClock(j.started)}</td>
      <td class="elapsed" title="${fmtClock(j.updated)}">${fmtAgo(j.updated, data.now)}</td>
    </tr>${trace}`;
  }).join('');
}

// ---- the trace, as the Core Index draws it -------------------------------------------
// Same nodes, same states, same colours: service/trace_graph.py is spliced into both pages
// so there is one copy of the picture. What differs is only where the inputs come from --
// there, an S3 ledger row and a detail endpoint; here, the job dir on disk.
const TRACE_OPEN = new Set();
// The same two the Core Index uses (service/web.py), so the shared graph escapes text
// identically on both pages. Written with the double-quote forms deliberately: an
// apostrophe inside a regex character class reads as an unbalanced quote to the
// escape guard in tests/test_pipeline_monitor_passes.py, which cannot parse regexes.
const esc = s => String(s == null ? "" : s).replace(/[&<>]/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
const escA = s => esc(s).replace(/"/g, "&quot;").replace(/\u0027/g, "&#39;");
function exGraphFor(j) {
  try {
    // A route of its own gets a graph of its own. Asked before the shared graph rather
    // than inside it: exNodeStates reasons entirely about a first pass and its fallbacks,
    // and none of those questions was asked of a document on this route.
    if (j.route === 'summary_ai') return sumGraph(j);
    return exGraph(j, exNodeStates(j, j.detail), null);
  } catch (e) {
    // A graph that throws must not take the table with it: the row is the thing people
    // came for. Say so rather than rendering an empty box.
    return `<div class="trace-hint">trace unavailable: ${esc(e && e.message || e)}</div>`;
  }
}
document.addEventListener('click', ev => {
  const row = ev.target.closest && ev.target.closest('tr.has-trace');
  if (!row) return;
  const key = row.getAttribute('data-job');
  if (TRACE_OPEN.has(key)) TRACE_OPEN.delete(key); else TRACE_OPEN.add(key);
  // poll() renders the table inline and keeps no copy of the payload, so re-polling is
  // both the smallest change and always correct. It is a local request against a warm
  // filesystem scan, and the 3s poll would repaint within a tick anyway.
  poll();
});

poll();
setInterval(poll, 3000);
</script>
</body>
</html>
"""

# The trace graph is the SAME code the Core Index draws (service/trace_graph.py),
# spliced in rather than copied: this monitor and that page had already drifted apart
# on the node count and the palette once each.
# TWO graphs, spliced independently. The summary AI route has its own path through the
# pipeline -- three nodes, none of them the spine's -- so it has its own file and its own
# renderer rather than a branch inside the shared one. See summary_trace_graph's docstring
# for why merging them produced a picture that was actively wrong.
PAGE = (_PAGE_TMPL.replace("/*TRACE_CSS*/", trace_graph.CSS + summary_trace_graph.CSS)
                  .replace("/*TRACE_JS*/", trace_graph.JS + summary_trace_graph.JS))



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="corpus output root (default out/corpus)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8814)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    app = build_app(root)
    print(f"Pipeline monitor -> http://{args.host}:{args.port}/  (watching {root})")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
