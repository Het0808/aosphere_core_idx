"""Progress of a GPU extraction run, read back from S3 for the monitoring screen.

The workers (scripts/corpus_worker.py) each publish ONE small summary object to
<run>/_progress/shard-<i>.json after every document. This module reads those and adds them up.

It deliberately never reads scorecards. A run is ~891 documents whose scorecards are ~20KB each,
so aggregating them per page load would be ~18MB of GETs for a number that fits in a sentence —
the same mistake that made the gallery load every scorecard and take the service down. Eight
bounded objects cost eight GETs and answer everything the screen asks.

A worker that dies takes its summary with it, frozen at the last document. That is a feature: the
screen can show a shard as STALE (nothing written for a while) rather than pretending eight
workers are alive when five are. Nothing here infers liveness from anything else, because on spot
a silent shard is the normal case, not an error.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from ..aws.s3_readonly import ReadOnlyS3

# A shard is stale when its summary has not moved for longer than this. Generous on purpose: a
# 281-page document legitimately takes ~40 minutes on one GPU, and calling that "stuck" would cry
# wolf on the largest documents in the corpus.
STALE_AFTER_S = 45 * 60


def _shard_keys(s3: ReadOnlyS3, prefix: str) -> list[str]:
    return sorted(k for k in s3.list_keys(f"{prefix.rstrip('/')}/_progress/", ".json")
                  if "/shard-" in k)


def list_runs(bucket: str, root: str, region: str) -> list[dict[str, Any]]:
    """Every extraction version under `root`, most recently active first.

    ONE list call answers this for all versions at once: the progress objects live at
    <run>/_progress/shard-<i>.json, and the list response already carries LastModified, so the
    screen can order versions by real activity instead of guessing from their names. Sorting by
    name would put "baseline-v2" above "2026-08-19" and an operator hunting for the run that is
    live right now would have to read every one.

    Each entry is cheap metadata only — name, how many workers reported, when it last moved. The
    per-version detail (percentages, gates, current documents) costs a GET per shard and is only
    paid when a version is actually selected."""
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    root = root.rstrip("/")
    runs: dict[str, dict[str, Any]] = {}
    # DELIMITER first, then one scoped listing per run. Listing <root>/ recursively and filtering
    # in Python walked EVERY object in the bucket's corpus area to find a handful: measured 80,935
    # keys examined to locate 18 progress objects, and a finished run adds ~150 objects per
    # document, so the screen got slower every hour a run progressed. This is 1 + N tiny calls
    # instead, where N is the number of runs.
    for cp in s3.list_common_prefixes(f"{root}/"):
        name = cp.rstrip("/").rsplit("/", 1)[-1]
        if not name or name.startswith("_"):
            continue
        entry = {"run": name, "prefix": f"{root}/{name}",
                 "workers_reported": 0, "last_activity": 0.0}
        for obj in s3.list_objects(f"{root}/{name}/_progress/", ".json"):
            if not obj["key"].rsplit("/", 1)[-1].startswith("shard-"):
                continue
            lm = obj.get("last_modified")
            entry["workers_reported"] += 1
            entry["last_activity"] = max(entry["last_activity"],
                                         lm.timestamp() if hasattr(lm, "timestamp") else 0.0)
        runs[name] = entry
    return sorted(runs.values(), key=lambda r: r["last_activity"], reverse=True)


def progress(bucket: str, prefix: str, region: str, now: float | None = None) -> dict[str, Any]:
    """Aggregate every shard summary under a run prefix into one view for the screen."""
    now = time.time() if now is None else now
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    shards: list[dict[str, Any]] = []
    for key in _shard_keys(s3, prefix):
        try:
            shards.append(json.loads(s3.get_bytes(key)))
        except Exception:                                        # noqa: BLE001
            continue                                             # a half-written PUT: skip it
    return summarize(shards, now=now)


def current_topology(shards: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """-> (the summaries describing the CURRENT shard layout, the superseded ones).

    Re-running a version with a different worker count is a normal production move: 8 workers
    yesterday, 4 today because that is the spot capacity available. The old shard-4..7 objects do
    not disappear, and they describe a DIFFERENT partition of the corpus — adding them to today's
    four would double-count documents and invent four permanently "stale" workers on the screen.

    The layout each worker declares (its `shards` value) identifies the generation, so the group
    whose activity is newest is the live one and the rest are history. Shard indices outside the
    declared layout are dropped for the same reason."""
    gens: dict[int, list[dict[str, Any]]] = {}
    for s in shards:
        gens.setdefault(int(s.get("shards") or 1), []).append(s)
    if not gens:
        return [], []
    newest = max(gens.values(), key=lambda g: max((x.get("updated_at") or 0) for x in g))
    n = int(newest[0].get("shards") or 1)
    live = [s for s in newest if int(s.get("shard") or 0) < n]
    superseded = [s for g in gens.values() if g is not newest for s in g]
    return live, superseded


def summarize(shards: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """Pure aggregation over shard summaries — no I/O, so this is what the tests exercise."""
    now = time.time() if now is None else now
    if not shards:
        return {"state": "idle", "shards": [], "done": 0, "jobs_total": 0, "pages_total": 0,
                "pages_done": 0, "percent": 0.0, "gates": {}, "recent": [],
                "eta_seconds": None, "pages_per_hour": None, "elapsed_seconds": 0,
                "last_activity": None, "quiet_seconds": None}

    shards, superseded = current_topology(shards)
    if not shards:
        return {"state": "idle", "shards": [], "done": 0, "jobs_total": 0, "pages_total": 0,
                "pages_done": 0, "percent": 0.0, "gates": {}, "recent": [],
                "eta_seconds": None, "pages_per_hour": None, "elapsed_seconds": 0,
                "last_activity": None, "quiet_seconds": None,
                "superseded_workers": len(superseded)}

    tot = {k: sum(int(s.get(k) or 0) for s in shards)
           for k in ("done", "extracted", "cloned", "failed", "jobs_total",
                     "pages_total", "pages_done")}
    # Version-level totals are identical in every shard (the partition is deterministic), so they
    # are taken with max(), never summed.
    ver = {k: max((int(s.get(k) or 0) for s in shards), default=0)
           for k in ("corpus_docs", "corpus_pages", "remaining_docs", "remaining_pages")}
    gates: dict[str, int] = {}
    for s in shards:
        for g, n in (s.get("gates") or {}).items():
            gates[g] = gates.get(g, 0) + int(n)

    started = min((s.get("started_at") or now) for s in shards)
    last_activity = max((s.get("updated_at") or 0.0) for s in shards)

    rows = []
    for s in sorted(shards, key=lambda x: int(x.get("shard") or 0)):
        idle_for = now - (s.get("updated_at") or now)
        cur = s.get("current")
        # ORDER MATTERS. A worker that finished its partition reads complete however long ago it
        # last wrote. After that, SILENCE outranks whatever flag the worker last managed to set:
        # `stopping` used to be tested FIRST, so a worker that announced a spot drain and then
        # died stayed "stopping" — and counted as live — forever. Run 2026-08-20-08 read RUNNING
        # with 8/8 workers live and an ETA of 1010 hours, 99 hours after the last worker wrote
        # anything. A flag records an INTENTION at one instant; only updated_at is evidence that
        # the process is still there to act on it.
        if int(s.get("done") or 0) >= int(s.get("jobs_total") or 0) and s.get("jobs_total"):
            state = "complete"
        elif s.get("status") == "interrupted":
            # ABOVE staleness, unlike `stopping` below it, and for the opposite reason. A
            # `stopping` worker is claiming it is still there; only updated_at can support
            # that, so silence outranks the claim. `interrupted` is the worker's LAST word —
            # written by the signal handler as the process leaves — so it is a fact about a
            # worker that is already gone, and ageing it into "stale" would lose the one thing
            # it says: this shard stopped because the node went away, not because it died.
            state = "interrupted"
        elif idle_for > STALE_AFTER_S:
            state = "stale"
        elif s.get("stopping"):
            state = "stopping"
        else:
            state = "running"
        rows.append({"shard": s.get("shard"), "state": state,
                     # What was in flight when the signal landed, and where it had got to.
                     # Only the interrupted shard has these; they are the one thing the resume
                     # cannot work out for itself, since the document left no scorecard.
                     "interrupted_document": s.get("interrupted_document"),
                     "interrupted_stage": s.get("interrupted_stage"),
                     "interrupted_at": s.get("interrupted_at"),
                     "done": s.get("done") or 0, "jobs_total": s.get("jobs_total") or 0,
                     "failed": s.get("failed") or 0,
                     "pages_done": s.get("pages_done") or 0,
                     "pages_total": s.get("pages_total") or 0,
                     "current": cur, "idle_seconds": round(idle_for),
                     "current_seconds": (round(now - cur["started_at"])
                                         if cur and cur.get("started_at") else None)})

    # Only a shard genuinely still reporting counts as live — see the state ladder above.
    live = [r for r in rows if r["state"] in ("running", "stopping")]

    # A dead run's clock STOPS at its last activity. Measuring to now() instead meant the wall
    # clock of a run nobody was working on kept climbing, which overstates how long the work took
    # and silently decays pages/hour toward zero the longer the corpse sits there.
    elapsed = max(0.0, (now if live else (last_activity or now)) - started)

    # Rate from PAGES, not documents: documents run 4 to 286 pages, so a document-per-hour rate
    # swings wildly with whatever happens to be in flight and gives a useless ETA.
    pph = (tot["pages_done"] / elapsed * 3600) if elapsed > 60 and tot["pages_done"] else None
    remaining = max(0, tot["pages_total"] - tot["pages_done"])
    # An ETA is a claim that something is still working on it. With no live worker there is
    # nothing to extrapolate from, and "1010h remaining" on an abandoned run is worse than blank.
    eta = (remaining / pph * 3600) if (pph and live) else None

    recent = sorted((r for s in shards for r in (s.get("recent") or [])),
                    key=lambda r: r.get("ts") or 0, reverse=True)[:20]
    # A failed row carries the exception text; classifying it HERE means the screen never has to
    # parse a traceback, and the same rule is used for the run's failure logs. A crash writes no
    # scorecard and no viewer, which is what makes "failed with no viewer" the shape to look at.
    for r in recent:
        if r.get("error"):
            r["cause"] = classify_failure(r["error"])

    # Two different percentages, because they answer two different questions. The ATTEMPT figure is
    # how far this launch has got through the work it picked up; the VERSION figure is how much of
    # the corpus this version has extracted in total, counting everything earlier attempts already
    # finished. After a resume they diverge sharply — a run with 40 documents left starting from
    # scratch is 0% of its attempt but 95% of its version, and only the second is the answer to
    # "is this version ready".
    done_before_pages = max(0, ver["corpus_pages"] - ver["remaining_pages"])
    done_before_docs = max(0, ver["corpus_docs"] - ver["remaining_docs"])
    ver_pages_done = done_before_pages + tot["pages_done"]
    ver_percent = (round(ver_pages_done / ver["corpus_pages"] * 100, 1)
                   if ver["corpus_pages"] else None)

    return {
        "version": {
            # None when the progress objects predate version tracking, so the screen can say so
            # rather than showing a confident 0%.
            "percent": ver_percent,
            "docs_done": done_before_docs + tot["done"],
            "docs_total": ver["corpus_docs"] or None,
            "pages_done": ver_pages_done,
            "pages_total": ver["corpus_pages"] or None,
            "resumed_from": done_before_docs,
        },
        "superseded_workers": len(superseded),
        # A run with no live worker is NOT automatically complete. Distinguishing them is the
        # whole point: "complete" means every shard finished its partition; "stalled" means the
        # workers are gone and the work is not done — the normal outcome when spot capacity is
        # reclaimed mid-run, and the state an operator most needs to see.
        # `interrupted` sits between complete and stalled, and it is the difference between
        # "the workers vanished and we do not know why" and "we told them to stop and they said
        # so on the way out". Every shard that is not finished announced its own interruption,
        # which is a stop with an explanation — not the silence `stalled` is there to report.
        "state": ("running" if live
                  else "complete" if rows and all(r["state"] == "complete" for r in rows)
                  else "interrupted" if rows and all(r["state"] in ("complete", "interrupted")
                                                     for r in rows)
                  else "stalled" if rows else "idle"),
        # When the run last moved, so the screen can date a stalled run instead of implying now.
        "last_activity": last_activity or None,
        # HOW LONG SINCE ANY WORKER SPOKE — the evidence behind `state`, not just its verdict.
        # A screen that reports only its conclusion ("running") is trusted exactly as far as its
        # threshold is right, and this one's was 45x too generous for two days without anyone
        # noticing. Publishing the silence lets an operator see a dead worker at 14 minutes
        # instead of waiting for the threshold to agree. Computed here rather than in the browser
        # because the client's clock is not the clock these timestamps were written against.
        "quiet_seconds": round(now - last_activity) if last_activity else None,
        "run": shards[0].get("run"),
        "shards": rows,
        "shards_live": len(live),
        "percent": round(tot["pages_done"] / tot["pages_total"] * 100, 1) if tot["pages_total"] else 0.0,
        "gates": gates,
        "recent": recent,
        "elapsed_seconds": round(elapsed),
        "pages_per_hour": round(pph) if pph else None,
        "eta_seconds": round(eta) if eta else None,
        **tot,
    }



# ---------------------------------------------------------------------------
# The JOB LIST: every document the run has finished, not just the last ten.
#
# `recent` in each shard summary is a capped tail by design — it answers "what is happening now"
# and it must stay small enough to rewrite after every document. It cannot answer "which document
# was slow", because four hours later the slow one has fallen off the end.
#
# So the workers also append one line per document to <run>/_progress/ledger-<i>.jsonl. That is
# bounded by the CORPUS rather than by time (~400 bytes x 891 documents), holds only what run_one
# already computed, and still costs one GET per shard — the screen gets the whole run's timings
# without reading a single scorecard, which is the rule this whole module exists to keep.
# ---------------------------------------------------------------------------

# The six stages a document passes through, in order. This is the pipeline as run_corpus reports
# it, not as the directories happen to be named, because the screen has to place a document that
# is IN one of them right now.
STAGES = [
    {"key": "queued", "label": "Fetched / queued"},
    {"key": "stage1", "label": "Stage 1 \u00b7 Extract"},
    {"key": "stage2", "label": "Stage 2 \u00b7 Tables (MinerU)"},
    {"key": "stage3", "label": "Stage 3 \u00b7 Combine"},
    {"key": "validate", "label": "Validation"},
    {"key": "gate", "label": "Scorecard gate"},
    # Opt-in, and off unless ACI_STAGE4_AI_ENABLED is set — so most documents never reach
    # these and the funnel counts for them stay at the gate. Listed anyway: a stage that
    # spends money needs to be visible whether it ran, was skipped, or was disabled.
    {"key": "stage4_ai", "label": "Stage 4 \u00b7 AI post-processing", "optional": True},
    {"key": "stage5_subchunk", "label": "Stage 5 \u00b7 Sub-chunks", "optional": True},
]

# run_corpus._step names -> the stage above. The fallback tiers RE-RUN earlier stages, so they map
# to the stage they redo: a document in mineru_full is doing Stage 2 work again, and showing it as
# a seventh stage would make the funnel count the same document twice.
STAGE_INDEX = {
    "starting": 0, "fetch": 0,
    "stage1": 1, "toc_preflight": 1, "toc_rescue": 1,
    "stage2_mineru": 2, "mineru_full": 2, "mineru_fallback": 2, "hybrid": 2,
    "stage3": 3,
    "validation": 4,
    "scorecard": 5, "publish": 5,
    # The summary AI route has no stages 1-4 to be mid-way through -- the product rule
    # sends it straight to one AI call, which IS the extraction. Landing it at the gate
    # rather than the default (Stage 1) is what stops a live document on this route from
    # reading as stalled on a stage it will never enter.
    "summary_ai": 5,
    "stage4_ai": 6,
    "stage5_subchunk": 7,
}

# The two vocabularies differ on purpose -- STAGES keys name the screen's columns
# ("validate"), STAGE_INDEX keys name the pipeline's steps ("validation") -- so this cannot
# be derived from STAGES. The drift it invites already bit once: a step name absent from the
# map falls to the default index 1, so a document that finished stage 4 reported
# "Stage 1 - Extract".
#
# That invariant is pinned in tests/test_extraction_jobs.py, NOT by an assert here. This
# module is imported inside the extraction route bodies, above their try/except, so a
# module-level assert would turn a funnel-labelling mistake into an HTTP 500 on every
# extraction endpoint -- taking out the whole Extraction tab to report a cosmetic bug.


# Which pass produced the tree that was kept. Same vocabulary the local pipeline monitor uses, so
# the two screens describe a fallback identically.
TIER_LABEL = {
    "first_pass": "first pass",
    "toc_rescue": "\u27f2 TOC rescue",
    "mineru_full": "\u27f2 MinerU full",
    "stage1": "\u27f2 fallback tried",
    # Not a tier that WON a comparison -- the product rule decides this before Stage 1
    # would even run, so there is nothing else it could have been. See product_rules.AI_PIPELINE.
    "summary_ai": "Summary PDF",
}

GATE_STATUS = {"pass": "pass", "review": "review", "fail": "fail", "error": "error"}


# The stage names that mean "this document is on a SECOND pass". A document in one of these has
# gone back through work it already did, which is why a live row showing "Stage 1 · Extract" for
# the second time reads as a stall unless the screen says otherwise.
FALLBACK_STAGES = frozenset({"toc_rescue", "mineru_full", "mineru_fallback", "hybrid"})

TIER_OF_STAGE = {"toc_rescue": "toc_rescue", "mineru_full": "mineru_full",
                 "mineru_fallback": "mineru_full", "hybrid": "mineru_full"}


# Where extraction proper finishes. Everything after it in STAGES is opt-in.
GATE_INDEX = next(i for i, s in enumerate(STAGES) if s["key"] == "gate")


def stage_index(name: str | None) -> int:
    """Where a stage name sits in STAGES. Unknown names land at Stage 1 rather than at 0, because
    a document that is reporting *something* has certainly been fetched."""
    if not name:
        return 0
    return STAGE_INDEX.get(name, 1)


def _ledger_keys(s3: ReadOnlyS3, prefix: str) -> list[str]:
    return sorted(k for k in s3.list_keys(f"{prefix.rstrip('/')}/_progress/", ".jsonl")
                  if "/ledger-" in k)


def read_ledger(bucket: str, prefix: str, region: str) -> list[dict[str, Any]]:
    """Every finished document in the run, newest first, deduplicated.

    Two things legitimately produce the same document twice, and both must collapse to one row:
    a RETRY re-extracts a document that is already in the ledger, and a re-run with a different
    worker count leaves the old shard's ledger in place holding documents the new layout also
    covers. The newest row wins in both cases, because it describes the tree currently on disk.

    Unlike the shard summaries, the old topology's ledgers are NOT discarded — they are real
    documents this version extracted, and dropping them would empty the job list of everything a
    previous attempt finished."""
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for key in _ledger_keys(s3, prefix):
        try:
            body = s3.get_bytes(key).decode("utf-8", errors="replace")
        except Exception:                                    # noqa: BLE001
            continue                                         # a shard that has written nothing yet
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue                                     # a half-flushed final line: skip it
            k = (row.get("product") or "", row.get("label") or "")
            if not k[1]:
                continue
            prev = best.get(k)
            if prev is None or (row.get("ts") or 0) >= (prev.get("ts") or 0):
                best[k] = row
    return sorted(best.values(), key=lambda r: r.get("ts") or 0, reverse=True)


# ---------------------------------------------------------------------------
# WHY a document escalated to the fallback chain.
#
# The chain records this already, but in two different shapes and never anywhere the screen
# looks, so "it fell back" arrived with no cause attached — and the cause is the actionable
# half. A document rescued because its printed TOC was lacking is a different problem from one
# rescued because Stage 1 dropped two thirds of the words.
#
#   normal path  fallback["first_attempt"]["entry_reason"], built by mineru_fallback.needs_help
#                as a semicolon-joined list: "completeness 35.1 < 60; toc 65.0 < 70"
#   short doc    fallback["reason"], set by run_chain when a memo skips tier 2 outright
#
# Reading only one of them explains 4 of the 18 fallbacks in the current corpus and leaves the
# other 14 blank, which is why this reads both.
# ---------------------------------------------------------------------------

# Ordered: first match wins, so the specific shapes are checked before the score comparisons.
TRIGGER_CAUSES = (
    ("short_doc", ("short document",), "too short for a TOC rescue"),
    ("one_chunk", ("content all in one chunk",), "content all in one chunk"),
    ("outline_shape", ("outline has only",), "outline too small to use"),
    ("no_profile", ("structure profile",), "no structure profile"),
    ("completeness", ("completeness",), "content missing"),
    ("toc", ("toc",), "TOC unusable"),
    ("sectioning", ("sectioning",), "sections without content"),
)

_SCORED = re.compile(r"^(completeness|toc|sectioning)\s+([0-9.]+)\s*<\s*([0-9.]+)$")


def _trigger_clause(text: str) -> dict[str, Any] | None:
    """One clause of an entry reason -> a structured cause.

    A scored clause ("toc 65.0 < 70") keeps its numbers, because "TOC unusable" alone does not
    say whether it missed the bar by a point or by sixty."""
    text = (text or "").strip()
    if not text:
        return None
    low = text.lower()
    m = _SCORED.match(low)
    if m:
        # No `detail`: the numbers are already captured structurally, and repeating the raw
        # clause under a line that says the same thing is noise in a dense panel.
        return {"cause": m.group(1), "label": m.group(1), "score": float(m.group(2)),
                "threshold": float(m.group(3)), "detail": None}
    for cause, needles, label in TRIGGER_CAUSES:
        if any(n in low for n in needles):
            return {"cause": cause, "label": label, "score": None, "threshold": None,
                    "detail": text[:200]}
    return {"cause": "other", "label": text[:60], "score": None, "threshold": None,
            "detail": text[:200]}


def trigger_causes(reason: str | None) -> list[dict[str, Any]]:
    """An entry reason -> its structured causes. Semicolon-joined by needs_help."""
    return [c for c in (_trigger_clause(part) for part in str(reason or "").split(";")) if c]


def primary_cause(causes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one cause a table cell has room for: the furthest below its bar, else the first.

    Lowest score rather than first-listed, because needs_help emits them in a fixed dimension
    order — so the first clause is an artefact of that order, not the worst problem."""
    return (min((c for c in causes if c.get("score") is not None),
                key=lambda c: c["score"], default=None) or (causes[0] if causes else None))


def fallback_trigger(fb: dict[str, Any] | None) -> dict[str, Any] | None:
    """Why the chain was entered, and how it ended. None when it never was.

    `causes` is what the row shows; `first` is the state Stage 1 was in when it escalated,
    preserved by run_chain BEFORE any tier rewrites the scorecard — which matters most for a
    TOC-triggered rescue, since the rescue replaces the outline and the re-scored `toc`
    dimension reads healthy afterwards."""
    fb = fb or {}
    if not fb.get("triggered"):
        return None
    first = fb.get("first_attempt") or {}
    raw = first.get("entry_reason") or fb.get("reason") or ""
    causes = trigger_causes(raw)
    return {
        "reason": str(raw) or None,
        "causes": causes,
        "primary": primary_cause(causes),
        "first": {k: first.get(k) for k in
                  ("gate", "worst_score", "weakest_dimension", "completeness_score",
                   "toc_score", "toc_status", "toc_reason", "toc_rescuable")} if first else {},
        "adopted_tier": fb.get("adopted_tier"),
        "chain": fb.get("chain") or [],
        # Every tier ran and none cleared the bar: the pipeline has nothing better to offer and
        # is saying so, rather than handing back the least-bad number as a result.
        "accepted": fb.get("accepted"),
        "hard_fail": bool(fb.get("hard_fail")),
        "hard_fail_reason": fb.get("hard_fail_reason"),
    }


def _step_seconds(row: dict[str, Any], step: str) -> float:
    """Seconds recorded for one pipeline step, or 0.0 if the row does not say.

    Every value here came out of a worker's JSONL and is therefore untrusted shape, not just
    untrusted content: `steps` has been seen as a list, and a duration can arrive as a string.
    This used to be read as `(row["steps"] or {}).get(step) or 0`, where a list raised
    AttributeError and a string duration raised TypeError on the comparison -- and because the
    whole job list is built in one pass, ONE malformed row 502'd the entire Extraction tab
    rather than degrading that row. A step that cannot be read is a step that did not run.
    """
    steps = row.get("steps")
    if not isinstance(steps, dict):
        return 0.0
    try:
        return float(steps.get(step) or 0)
    except (TypeError, ValueError):
        return 0.0


def _finished_job(row: dict[str, Any]) -> dict[str, Any]:
    """One ledger row as the screen wants it: gate, pass, cost and where it got to."""
    gate = row.get("gate") or "unknown"
    crashed = gate == "error"
    # A worker killed mid-document writes its row too, with gate "interrupted". It is not a
    # failure — nothing is wrong with the document, the node went away — but it is not a scored
    # document either, so it takes the crash branch below for everything that assumes a gate was
    # reached, and only the STATUS tells the two apart.
    interrupted = gate == "interrupted"
    unscored = crashed or interrupted
    # A crash stops WHERE IT CRASHED — that is the whole point of recording the stage — while a
    # scored document reached the gate by definition.
    # A scored document has reached the GATE, which is where extraction ends — not the
    # last entry in STAGES. Those are now the two opt-in AI stages, which most documents
    # never enter, and "len(STAGES) - 1" silently claimed every scored document had run
    # them the moment they were added to the list.
    idx = stage_index(row.get("stage")) if unscored else GATE_INDEX
    # Stages 4-5 are opt-in and run out of band, so the only trace in the ledger is the
    # time they took. A recorded ZERO means the flag refused the run -- asked for, nothing
    # spent -- so it must not advance the document, which is why presence is read
    # separately from value.
    if not unscored:
        if _step_seconds(row, "stage4_ai") > 0:
            idx = stage_index("stage4_ai")
        if _step_seconds(row, "stage5_subchunk") > 0:
            idx = stage_index("stage5_subchunk")
    # Ledger rows written before the worker learned to record `route` have neither it nor a
    # correct `tier` -- corpus_worker.finished() used to fall through to "first_pass" for
    # every document, summary-route ones included. But `steps` already named the step
    # "summary_ai" regardless (run_corpus._step ticks it under that name), so the route is
    # still derivable from data already in the ledger row -- no extra scorecard read, which
    # is the rule this module exists to keep. Self-correcting, so it fixes every run already
    # on disk, not just ones extracted after the worker is redeployed.
    route = row.get("route") or ("summary_ai" if "summary_ai" in (row.get("steps") or {})
                                  else None)
    tier = "summary_ai" if route == "summary_ai" else (row.get("tier") or "first_pass")
    # The worker publishes the raw entry reason; classifying it HERE keeps one vocabulary for
    # the row and the drill-down, exactly as classify_failure does for crashes.
    causes = trigger_causes(row.get("trigger_reason"))
    return {
        "trigger": {"reason": row.get("trigger_reason"), "causes": causes,
                    "primary": primary_cause(causes)} if causes else None,
        # Every tier ran and none cleared the bar. Distinct from the gate: a hard fail says the
        # pipeline is out of options, not merely that this document scored badly.
        "hard_fail": bool(row.get("hard_fail")),
        "accepted": row.get("accepted"),
        "product": row.get("product") or "",
        "label": row.get("label") or "",
        "shard": row.get("shard"),
        "gate": None if unscored else gate,
        "status": ("error" if crashed else
                   "interrupted" if interrupted else GATE_STATUS.get(gate, "review")),
        "worst": row.get("worst"),
        # Aliased, not renamed: the shared summary-route graph (service/summary_trace_graph.py)
        # was written against the local pipeline monitor's job shape, which calls this
        # worst_score. Same value, both names, so the graph reads correctly from either host.
        "worst_score": row.get("worst"),
        # Which tree `gate`/`worst` above actually describe. 3 (the extraction gate) unless
        # the row says otherwise -- every ledger row written before this field existed was
        # extraction-gate-only by construction, so absence means 3, not "unknown".
        "scored_stage": row.get("scored_stage") or 3,
        # The extraction (stage 1-3) verdict, so the screen can show SC1 and SC2 in the same
        # row. Absent on every row written before the worker recorded it -- the screen shows
        # a dash there rather than guessing, EXCEPT where scored_stage is 3, in which case
        # gate/worst above ARE the extraction verdict and there is no second one.
        "gate_extraction": row.get("gate_extraction"),
        "worst_extraction": row.get("worst_extraction"),
        "stage": row.get("stage"),
        "stage_idx": idx,
        "stage_label": STAGES[idx]["label"],
        "tier": tier,
        "tier_label": TIER_LABEL.get(tier, tier),
        # Which route this document was extracted by -- None for the spine everyone else
        # walks, "summary_ai" for the product-rule route straight to one AI call. The only
        # thing that tells the screen which trace graph to draw.
        "route": route,
        # What the summary AI route spent -- the only route that costs money, so it has no
        # equivalent on any other row.
        "cost_usd": row.get("cost_usd"),
        # Stage 4's own per-section health -- None (not "ok") on every ledger row written
        # before the worker recorded it, and on every document Stage 4 does not apply to
        # (never ran, or the summary-AI route): the screen shows blank for those rather than
        # a false green. "failed"/"incomplete" name the section file(s) here; the reason each
        # one failed for is one GET away, in job_detail, not a cost every row on this screen
        # pays for a document nobody has opened.
        "stage4_status": row.get("stage4_status"),
        "stage4_failed_sections": row.get("stage4_failed_sections") or [],
        "fallback": bool(row.get("fallback")),
        "cloned": bool(row.get("cloned")),
        # Not a tier: the pre-flight rebuilt the outline from the printed contents page
        # BEFORE Stage 2, so the document is still on its first pass -- but a first pass
        # over a rescued outline, which is a different document from an untouched one.
        "toc_rescued": bool(row.get("toc_rescued")),
        "seconds": row.get("seconds"),
        "pages": row.get("pages"),
        "spp": row.get("spp"),
        "steps": row.get("steps") or {},
        "slowest": row.get("slowest"),
        "fb_seconds": row.get("fb_seconds") or 0,
        "tables": row.get("tables"),
        "mineru_pages": row.get("mineru_pages"),
        "review": row.get("review") or [],
        "error": row.get("error"),
        "cause": classify_failure(row["error"]) if row.get("error") else None,
        "started": row.get("started_at"),
        "updated": row.get("ts"),
        "live": False,
    }


def _running_job(shard: dict[str, Any], now: float) -> dict[str, Any] | None:
    """The document a worker has in flight, as a job row.

    Without this the job list is a list of the PAST — and the row an operator most wants to see is
    the one that has been in stage2_mineru for 40 minutes."""
    cur = shard.get("current")
    if not cur or not cur.get("label"):
        return None
    stage = cur.get("stage")
    idx = stage_index(stage)
    started = cur.get("started_at")
    # The worker announces the trigger the moment the chain decides, which is what lets a live
    # row say "re-running because toc 65 < 70" instead of showing a document that has silently
    # gone backwards to a stage it already finished.
    fbinfo = cur.get("fallback") or {}
    causes = trigger_causes(fbinfo.get("reason"))
    in_fallback = stage in FALLBACK_STAGES or bool(fbinfo)
    done_steps = {k: v for k, v in (cur.get("steps") or {}).items() if k != "total"}
    elapsed = (now - started) if started else None
    stage_secs = round(now - cur["stage_at"], 1) if cur.get("stage_at") else None
    # The stage in flight shown at its CURRENT elapsed, so the pass it belongs to is placed and
    # sized even though it has not finished. Marked `running` below so nothing reads it as final.
    live_step = {stage: stage_secs} if stage and stage_secs is not None else {}
    # The summary AI route is a product rule, decided before Stage 1 would even run -- not a
    # fallback tier reached by comparing scores. So unlike the tiers below, it is known the
    # moment the stage is entered, not deferred until something is adopted.
    route = "summary_ai" if stage == "summary_ai" else None
    return {
        "product": cur.get("product") or "",
        "label": cur.get("label") or "",
        "shard": shard.get("shard"),
        "gate": None, "status": "running", "worst": None, "worst_score": None,
        "scored_stage": None,
        "stage": cur.get("stage"),
        "stage_idx": idx,
        "stage_label": STAGES[idx]["label"],
        "route": route,
        "cost_usd": None,
        # Nothing has been scored yet, so no tier has been ADOPTED — that is decided by
        # comparing scores after the tier finishes. But WHICH tier is running, and why it was
        # entered, are both known now, and they are the whole answer to "where is it". The
        # summary AI route is the exception: its "tier" is a product rule, not a comparison,
        # so it is set immediately rather than left null like every fallback tier below.
        "tier": ("summary_ai" if route else None),
        "tier_label": (TIER_LABEL.get("summary_ai") if route else None),
        "cloned": False,
        "fallback": in_fallback,
        "running_tier": TIER_OF_STAGE.get(stage),
        "running_tier_label": TIER_LABEL.get(TIER_OF_STAGE.get(stage) or ""),
        "trigger": ({"reason": fbinfo.get("reason"), "causes": causes,
                     "primary": primary_cause(causes)} if causes else None),
        "trigger_at": fbinfo.get("at"),
        # What the FIRST pass scored, available the moment the chain escalates — long before
        # this document writes a scorecard of its own. Until now a running document showed no
        # numbers at all, even when the pipeline had already computed a full set for it.
        "first_attempt": fbinfo.get("first") or None,
        # Which stage is still going, so the screen can size it without claiming it is done.
        "running_step": stage,
        "hard_fail": False, "accepted": None,
        # Wall clock so far — the only honest answer while it is still running.
        "seconds": round(now - started, 1) if started else None,
        "pages": cur.get("pages"),
        # Everything the document has ALREADY spent, stage by stage. The worker closes each
        # stage off as the next begins, so a document 40 minutes into a run is no longer a blank
        # row that says only "stage2_mineru" — it carries the same breakdown a finished one does,
        # missing only the stage still running.
        "steps": done_steps,
        "slowest": (max(done_steps.items(), key=lambda kv: kv[1])[0] if done_steps else None),
        "spp": (round(elapsed / cur["pages"], 2)
                if cur.get("pages") and elapsed else None),
        "passes": timing_passes({**done_steps, **live_step}),
        # The stage in flight, kept separate from the finished ones: it has no final duration
        # yet, and folding a still-growing number into the breakdown would misreport it.
        "stage_seconds": stage_secs,
        "fb_seconds": round(sum(v for k, v in done_steps.items() if k in COMPOSITE_STEPS), 1),
        "tables": None, "mineru_pages": None, "review": [], "error": None, "cause": None,
        "started": started,
        "updated": shard.get("updated_at"),
        "live": True,
    }


def jobs_view(ledger: list[dict[str, Any]], shards: list[dict[str, Any]],
              now: float | None = None) -> dict[str, Any]:
    """The job list and the stage funnel — pure, so this is what the tests exercise.

    In-flight documents come FIRST and are never duplicated by a ledger row: a document is written
    to the ledger only once it is finished, at which point `current` has already been cleared."""
    now = time.time() if now is None else now
    live = [j for j in (_running_job(s, now) for s in shards) if j]
    running = {(j["product"], j["label"]) for j in live}
    done = [_finished_job(r) for r in ledger
            if (r.get("product") or "", r.get("label") or "") not in running]
    jobs = live + done
    # `optional` travels WITH the funnel row, not just on STAGES: the screen renders from
    # the funnel alone, and without the flag an opt-in stage sitting at 0 is indistinguishable
    # from a stage the whole run failed to reach.
    funnel = [{"key": st["key"], "label": st["label"],
               "optional": bool(st.get("optional")),
               "count": sum(1 for j in jobs if j["stage_idx"] >= i)}
              for i, st in enumerate(STAGES)]
    # None, not 0, when nobody has spent anything -- both the summary-AI route and stage 4
    # are opt-in, so most runs have no priced job at all and "$0.00 so far" would claim a
    # spend that never happened. Summed here rather than in the browser because it is
    # already in hand: this whole function runs once per poll to build the job list.
    costs = [j["cost_usd"] for j in jobs if j.get("cost_usd") is not None]
    # Wall-clock spent inside stage 4 specifically -- `seconds` on the row is the WHOLE
    # document (stage 1-3 included), so that total would answer a different question. Key
    # PRESENCE, same as _finished_job does for the funnel: stage4_ai is absent entirely on
    # a document that never ran it, not recorded as a zero.
    ai_secs = [j["steps"]["stage4_ai"] for j in jobs if "stage4_ai" in (j.get("steps") or {})]
    return {"stages": STAGES, "jobs": jobs, "funnel": funnel,
            "total": len(jobs), "running": len(live),
            "cost_usd_total": round(sum(costs), 4) if costs else None,
            "stage4_seconds_total": round(sum(ai_secs), 1) if ai_secs else None,
            # Whether the ledger is there at all. A run extracted before the workers wrote one has
            # no job list, and the screen must say that rather than showing an empty table as
            # though the run had done nothing.
            "ledger": bool(ledger)}


def jobs(bucket: str, prefix: str, region: str, now: float | None = None) -> dict[str, Any]:
    """Job list for one run: one GET per shard ledger plus the shard summaries already read."""
    now = time.time() if now is None else now
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    shards: list[dict[str, Any]] = []
    for key in _shard_keys(s3, prefix):
        try:
            shards.append(json.loads(s3.get_bytes(key)))
        except Exception:                                    # noqa: BLE001
            continue
    live, _superseded = current_topology(shards)
    return jobs_view(read_ledger(bucket, prefix, region), live, now=now)


# ---------------------------------------------------------------------------
# Splitting a document's timings into the PASSES they belong to.
#
# run_corpus records every tier as ONE step alongside the stages, so a rescued document's
# timings read:
#
#     toc_rescue     796.7s        <- the whole second pass, including its own MinerU run
#     stage2_mineru  782.6s        <- only the FIRST pass's MinerU
#     stage1           7.2s
#
# Listed flat, `toc_rescue` looks like a peer of `stage1`: a cheap re-shuffle of the hierarchy
# beside the real work. It is the opposite. rescue_outline._rerun_and_maybe_promote re-runs
# stage1 -> stage2 (MinerU) -> stage3 -> validate -> score on a repaired PDF, so on this
# document the rescue duplicated the entire extraction — 796.7s against a 797.3s first pass.
#
# Grouping them by pass is what makes that readable, and it is the difference between "why did
# a TOC re-shuffle take 13 minutes" and "it ran MinerU again".
# ---------------------------------------------------------------------------

# step name -> (tier key, what it actually is)
COMPOSITE_STEPS = {
    "toc_rescue": ("toc_rescue", "a complete re-extraction on the repaired PDF: stages 1\u20133 "
                                 "including MinerU, then validate and score"),
    "mineru_full": ("mineru_full", "the whole document handed to MinerU, then validate and score"),
    "mineru_fallback": ("mineru_full", "the whole document handed to MinerU, then validate and score"),
    "hybrid": ("mineru_full", "the whole document handed to MinerU, then validate and score"),
}

PASS_LABEL = {"first": "First pass", "toc_rescue": "TOC rescue", "mineru_full": "MinerU full re-parse"}


def timing_passes(steps: dict[str, Any] | None,
                  chain: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """A document's step timings, grouped into the passes that produced them.

    `chain` supplies each tier's INNER breakdown when the run recorded one (the rescue times its
    own stages), so a 13-minute tier can say how much of it was MinerU rather than staying an
    opaque lump. Runs that predate that instrumentation simply have no inner steps, and the pass
    still carries its total and an explanation of what it contains."""
    steps = steps or {}
    inner_by_tier: dict[str, dict[str, Any]] = {}
    for rec in chain or []:
        if rec.get("steps"):
            inner_by_tier[rec.get("tier") or ""] = rec["steps"]

    first: dict[str, Any] = {}
    passes: list[dict[str, Any]] = []
    for name, secs in steps.items():
        if name == "total":
            continue
        if name in COMPOSITE_STEPS:
            tier, note = COMPOSITE_STEPS[name]
            passes.append({"key": tier, "label": PASS_LABEL.get(tier, tier), "step": name,
                           "seconds": secs, "composite": True, "note": note,
                           "steps": inner_by_tier.get(tier) or {}})
        else:
            first[name] = secs

    out = [{"key": "first", "label": PASS_LABEL["first"], "step": None,
            "seconds": round(sum(first.values()), 1), "composite": False,
            "note": "stages 1\u20133, then validate and score", "steps": first}]
    out.extend(passes)
    # Numbered in the order they ran, which is the order run_chain escalates.
    for i, p in enumerate(out):
        p["ordinal"] = i + 1
    return out


def job_detail(bucket: str, prefix: str, region: str, product: str,
               label: str) -> dict[str, Any] | None:
    """The full scorecard for ONE document, read on demand.

    This is the exception that proves the rule: the module never reads scorecards in BULK, because
    891 x ~20KB per page load is what took the service down. Reading exactly one, only when a
    reviewer clicks the row, is the opposite trade — it costs a single GET and it is the only
    place the whole fallback chain and the per-dimension scores exist.

    It also works on runs that predate the ledger, since a scored document has always had one."""
    for seg in (product, label):
        if "/" in seg or ".." in seg:
            raise ValueError("invalid path segment")
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    base = f"{prefix.rstrip('/')}/{product}/{label}"
    try:
        sc = json.loads(s3.get_bytes(f"{base}/scorecard.json"))
    except Exception:                                        # noqa: BLE001
        return None
    # THE FINAL VERDICT, not the extraction gate: scorecard.json is frozen at stage 3 (the
    # --resume marker), but when stage 4/5 ran, that tree is what actually ships, and a
    # reviewer opening this document should see how it reads NOW. One extra small GET, on
    # the same on-demand path as the scorecard itself -- see the module docstring's rule
    # about never reading scorecards in bulk; this is still a single document opened by hand.
    try:
        sc_post = json.loads(s3.get_bytes(f"{base}/scorecard_post_ai.json"))
    except Exception:                                        # noqa: BLE001
        sc_post = None
    final = sc_post if (sc_post and sc_post.get("gate") is not None) else sc
    # DID THE PRE-FLIGHT FIRE? Nothing in the scorecard answers this, and it is the reason a
    # document's Stage 1 most often ran twice. The pre-flight repairs the bookmark outline,
    # re-extracts on the repaired PDF and then gets out of the way, so afterwards a document it
    # rewrote and one it left alone are indistinguishable in the scores. run_corpus writes the
    # answer beside the scorecard, and _preflight_outline writes the file ONLY when it applied a
    # repair or errored trying -- so absence means "left the outline alone", not "unknown",
    # provided `timing.steps` shows the pre-flight step ran at all.
    #
    # One extra small GET, on the same on-demand path as the scorecard itself: the never-read-
    # scorecards-in-bulk rule this module exists to keep is about the 891-document page load,
    # and this is still a single document opened by hand.
    try:
        pf = json.loads(s3.get_bytes(f"{base}/toc_preflight.json"))
    except Exception:                                        # noqa: BLE001
        pf = None                                            # the outline was trusted as it stood
    # Stage 4's own per-section reasons — the ledger row already named which files, this adds
    # WHY. One more small GET on the same on-demand path; skipped (not an error) on a document
    # Stage 4 never touched, which is most of them.
    stage4 = None
    try:
        s4_report = json.loads(s3.get_bytes(f"{base}/04_stage4_ai/stage4_report.json"))
    except Exception:                                        # noqa: BLE001
        s4_report = None
    if s4_report is not None:
        from aosphere_core_index.extract.ai_postprocess import stage4_failed_sections_from_report
        failed = stage4_failed_sections_from_report(s4_report)
        stage4 = {"status": "failed" if failed else "ok", "failed_sections": failed}
    tm = sc.get("timing") or {}
    fb = sc.get("fallback") or {}
    return {
        "product": product, "label": label,
        # None = no repair was applied. The trace distinguishes that from "this run predates the
        # pre-flight" by looking for the step in `timing.steps`, which it already has.
        "preflight": pf,
        # None where Stage 4 never wrote a report for this document (never ran here, or the
        # report is simply absent) -- the row's own `stage4_status` (from the ledger, computed
        # at extraction time) already says "incomplete" for a Stage 4 that started and never
        # finished; this only adds the REASON text for each failed/missing section, which the
        # ledger deliberately does not carry.
        "stage4": stage4,
        "gate": final.get("gate"), "worst_score": final.get("worst_score"),
        # The per-dimension verdicts, which is what turns "review" into something actionable.
        "dimensions": final.get("dimensions") or final.get("checks") or {},
        # Which tree the fields above describe -- 3 for the (majority) documents that never
        # ran stage 4/5, 5 when the post-AI tree is what is shown. And the extraction gate
        # alongside it even when stage 5 wins, so "the AI pass moved this from review to
        # pass" (or the reverse) is a fact the screen can state, not one a reviewer has to
        # reconstruct by remembering the old number.
        "scored_stage": final.get("scored_stage") or (5 if final is sc_post else 3),
        "extraction_gate": ({"gate": sc.get("gate"), "worst_score": sc.get("worst_score")}
                            if final is sc_post else None),
        # Grouped by pass as well as listed flat: a tier is a whole re-extraction, and shown
        # beside the stages it looks like one of them.
        "passes": timing_passes(tm.get("steps"), fb.get("chain")),
        "timing": {"seconds": tm.get("seconds"), "steps": tm.get("steps") or {},
                   "slowest_step": tm.get("slowest_step"),
                   "fallback_seconds": tm.get("fallback_seconds"),
                   "pages": tm.get("pages"),
                   "seconds_per_page": tm.get("seconds_per_page"),
                   "seconds_per_crop_page": tm.get("seconds_per_crop_page"),
                   "mineru_crop_pages": tm.get("mineru_crop_pages"),
                   "backfilled": bool(tm.get("backfilled"))},
        # Every tier tried and why — including the ones that ran and were thrown away, which the
        # job row can only summarise as "adopted" or "tried". `trigger` adds the half that was
        # missing entirely: what Stage 1 looked like at the moment it escalated.
        "fallback": {"triggered": bool(fb.get("triggered")), "reason": fb.get("reason"),
                     "adopted_tier": fb.get("adopted_tier"), "chain": fb.get("chain") or [],
                     "accepted": fb.get("accepted"), "hard_fail": bool(fb.get("hard_fail")),
                     "hard_fail_reason": fb.get("hard_fail_reason")},
        "trigger": fallback_trigger(fb),
    }


def review_html(bucket: str, prefix: str, region: str, product: str, label: str,
                kind: str = "viewer") -> bytes | None:
    """The self-contained viewer (or inspection page) a RUN wrote beside its extraction.

    A run's output lives under corpus/<run>/, not the Doc Library's index/<version>/doc-gallery/,
    so the gallery cannot serve it — the document is not published there and publishing is a
    separate step against a finished corpus. The worker therefore builds the same artefact into
    the job dir, and this streams it back so the bucket stays private, exactly as the gallery
    does for published documents.

    None when the run predates that (no viewer object), so the caller can say so rather than 404
    into a blank tab."""
    if kind not in ("viewer", "inspect"):
        raise ValueError("kind must be viewer or inspect")
    for seg in (product, label):
        if "/" in seg or ".." in seg:
            raise ValueError("invalid path segment")
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    key = f"{prefix.rstrip('/')}/{product}/{label}/{kind}.html"
    try:
        return s3.get_bytes(key)
    except Exception:                                        # noqa: BLE001
        return None


def review_available(bucket: str, prefix: str, region: str, product: str,
                     label: str) -> dict[str, bool]:
    """Which review artefacts exist for one document — a HEAD each, not a download.

    The screen must not offer a link that leads to a 404: runs extracted before the worker started
    building these have no viewer, and a document still in flight has none yet."""
    for seg in (product, label):
        if "/" in seg or ".." in seg:
            raise ValueError("invalid path segment")
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    base = f"{prefix.rstrip('/')}/{product}/{label}"
    return {kind: s3.exists(f"{base}/{kind}.html") for kind in ("viewer", "inspect")}


# ---------------------------------------------------------------------------
# Marking a document for RE-EXTRACTION.
#
# Resume is "a document with a scorecard is done", which is what makes an interrupted run cheap —
# and what makes a BAD result sticky: re-running the same prefix skips exactly the documents a
# reviewer wants redone. Deleting the scorecard would work but destroys the evidence of what went
# wrong, and only the worker can write to the run otherwise.
#
# So a reviewer leaves a marker. The worker treats a marked document as not-done regardless of its
# scorecard, and removes the marker once it has re-extracted it. The marker records WHO asked,
# because it costs GPU time.
# ---------------------------------------------------------------------------

def _retry_key(prefix: str, product: str, label: str) -> str:
    for seg in (product, label):
        if "/" in seg or ".." in seg:
            raise ValueError("invalid path segment")
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{product}__{label}")
    return f"{prefix.rstrip('/')}/_retry/{safe}.json"


def _retried_key(prefix: str, product: str, label: str) -> str:
    return _retry_key(prefix, product, label).replace("/_retry/", "/_retried/", 1)


def mark_retry(bucket: str, prefix: str, region: str, product: str, label: str,
               by: str | None = None, force: bool = False) -> dict[str, Any]:
    """Queue one document for re-extraction on the next run of this prefix.

    ONE retry per document, unless someone explicitly forces another. A document that fails for a
    reason the retry does not change — a GPU too small for it, a page the parser cannot read —
    fails again, and an unbounded retry is then a loop that burns GPU on the same document for
    every run of the prefix. The worker records each retry it performs, and a second request is
    refused unless it is deliberate."""
    import boto3

    if not force:
        already = ReadOnlyS3(bucket=bucket, region=region).exists(
            _retried_key(prefix, product, label))
        if already:
            return {"queued": False, "product": product, "label": label,
                    "reason": "already retried once — pass force to retry again"}
    key = _retry_key(prefix, product, label)
    body = json.dumps({"product": product, "label": label, "requested_at": time.time(),
                       "by": by or "unknown", "forced": bool(force)}, indent=2).encode()
    boto3.client("s3", region_name=region).put_object(Bucket=bucket, Key=key, Body=body,
                                                     ContentType="application/json")
    return {"queued": True, "product": product, "label": label}


def list_retries(bucket: str, prefix: str, region: str) -> list[str]:
    """"<product>/<label>" for every document currently marked, so the screen can show it."""
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    out = []
    for obj in s3.list_objects(f"{prefix.rstrip('/')}/_retry/", ".json"):
        try:
            d = json.loads(s3.get_bytes(obj["key"]))
            out.append(f"{d['product']}/{d['label']}")
        except Exception:                                    # noqa: BLE001
            continue
    return sorted(out)


# ---------------------------------------------------------------------------
# WHY a document failed, from the per-failure logs the worker writes.
#
# A crash and a bad score are different problems with different remedies, and the run does not
# distinguish them anywhere a reviewer can see: a crashed document simply has no scorecard. Since
# resume is "a scorecard means done", crashed documents are ALREADY re-extracted by the next run
# of the same prefix — no marker needed. What is missing is knowing which failed, and why, so an
# infrastructure fault (a GPU that ran out of memory) is not mistaken for a document the pipeline
# cannot handle.
# ---------------------------------------------------------------------------

# Ordered: the first match wins, so the specific causes are checked before the generic ones.
#
# The split that matters is RETRYABLE vs PERMANENT. A GPU that ran out of memory may well succeed
# on the next node; a document the pipeline cannot find headings in will produce the identical
# error forever, because the pipeline has already diagnosed it:
#
#   "No structure found: no bookmark outline, no heading-sized text, and no BOLD CAPITALISED
#    lines to fall back on … there IS a text layer, so this is a heading-DETECTION gap"
#
# Re-running that is not a retry, it is a loop — and since a crash writes no scorecard, resume
# would re-attempt it on every future run of the prefix. Those get a marker instead, so the run
# skips them and someone fixes the code.
FAILURE_CAUSES = (
    # --- permanent: the pipeline cannot process this document as it stands ---
    ("no_structure", ("no structure found",)),
    ("pipeline_bug", ("indexerror", "keyerror:", "typeerror:", "attributeerror:",
                      "zerodivisionerror", "unboundlocalerror")),
    ("scanned_pdf", ("no text layer",)),
    ("bad_source", ("cannot open broken document", "not a pdf", "damaged", "password")),
    # --- transient: worth another attempt ---
    ("gpu_oom", ("cuda out of memory", "torch.outofmemoryerror", "cublas_status_alloc_failed",
                 "cuda error: out of memory")),
    ("host_oom", ("memoryerror", "cannot allocate memory", "exit status 137", "signal 9",
                  "oomkilled")),
    ("no_compiler", ("failed to find c compiler",)),
    ("shared_memory", ("bus error", "dataloader worker")),
    ("model_missing", ("no such file or directory", "offline mode", "can't load")),
    ("timeout", ("timed out", "timeouterror", "connection reset")),
)


# Causes a retry cannot change. Everything else is assumed worth one more attempt — the
# conservative direction, because wrongly calling something permanent silently drops a document.
PERMANENT_CAUSES = frozenset({"no_structure", "pipeline_bug", "bad_source", "scanned_pdf"})


def is_permanent(cause: str) -> bool:
    """Whether re-running this document would produce the same failure."""
    return cause in PERMANENT_CAUSES


def classify_failure(text: str) -> str:
    """One cause for a failure log. Unknown rather than a guess when nothing matches."""
    low = (text or "").lower()
    for cause, needles in FAILURE_CAUSES:
        if any(n in low for n in needles):
            return cause
    return "unknown"


def list_failures(bucket: str, prefix: str, region: str, limit: int = 200) -> list[dict[str, Any]]:
    """Failed documents with their cause, newest first.

    Reads <run>/_failures/, which holds one small object per failed document — not the run, so
    this stays cheap however large the corpus gets."""
    s3 = ReadOnlyS3(bucket=bucket, region=region)
    objs = s3.list_objects(f"{prefix.rstrip('/')}/_failures/", ".log")
    objs.sort(key=lambda o: (o.get("last_modified") or 0), reverse=True)
    out: list[dict[str, Any]] = []
    for obj in objs[:limit]:
        try:
            text = s3.get_bytes(obj["key"]).decode("utf-8", errors="replace")
        except Exception:                                    # noqa: BLE001
            continue
        fields = {}
        for line in text.splitlines()[:6]:
            if "=" in line and not line.startswith(" "):
                k, _, v = line.partition("=")
                fields[k.strip()] = v.strip()
        lm = obj.get("last_modified")
        cause = classify_failure(text)
        out.append({
            "permanent": is_permanent(cause),
            "product": fields.get("product") or "?",
            "label": fields.get("label") or obj["key"].rsplit("/", 1)[-1][:-4],
            "shard": fields.get("shard"),
            "cause": cause,
            # The line that names the fault, so a reviewer does not open the log to triage.
            "excerpt": next((l.strip() for l in text.splitlines()
                             if any(n in l.lower() for _c, ns in FAILURE_CAUSES for n in ns)),
                            "")[:240],
            "at": lm.timestamp() if hasattr(lm, "timestamp") else None,
        })
    return out


def failure_summary(failures: list[dict[str, Any]]) -> dict[str, int]:
    """Counts by cause, for the screen."""
    out: dict[str, int] = {}
    for f in failures:
        out[f["cause"]] = out.get(f["cause"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
