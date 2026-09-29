"""Promotion of a finished extraction run — the durable state, and how to read it.

A run writes to `corpus/<run>/`; the Doc Gallery reads `index/<version>/doc-gallery/` and
the search index reads `index/<version>/`. Nothing carried a document from the first to the
second, so a cluster run could be reviewed and never published. A PROMOTION is that step:
one operation, one durable progress object, watchable from the moment it starts.

WHERE THE STATE LIVES, and why it is under the RUN rather than the target:

    corpus/<run>/_promotion/<version>/state.json      the summary, rewritten per document
    corpus/<run>/_promotion/<version>/plan.json       per-document decisions, written once
    corpus/<run>/_promotion/<version>/ledger.jsonl    append-only, re-uploaded whole
    corpus/<run>/_promotion/<version>/failures.json   bounded: the last FAILURES_MAX
    corpus/<run>/_promotion/<version>/lease.json      the concurrency lock
    corpus/<run>/_promotion/<version>/request.json    written by the API, consumed by the job
    corpus/<run>/_promotion/<version>/stop            zero-byte drain marker

The extraction role already writes `corpus/*`. A promotion additionally needs `index/*`,
which is a NEW permission — and the first thing a missing permission does is fail. Keeping
progress under the run means a 403 on the target still leaves a readable, durable record of
what happened, instead of a job that vanished without saying why. The service also reads
this prefix already (`doc_gallery.extraction_target()`), so the screens need no new config
and no new read grant. And `doc_gallery.valid_run` rejects `_`-prefixed names, so
`_promotion` can never be addressed as a run — the same guard that protects `_progress`.

`promotion_id == version`, deliberately. Two writers merging into one
`index/<v>/doc-gallery/manifest.json` is how 455 published documents once became 5 manifest
entries, in its concurrent form. Making the id equal the version turns "a second concurrent
promotion" into "the same promotion object", which the lease then refuses. A deliberate
second attempt mints a new version (`<run>-p2`) — which is correct anyway, since its
contents differ.

NOTHING HERE HOLDS STATE. Every answer is one bounded GET. `vector_load._JOB` and
`entity_reindex._JOB` keep their job state in one pod's memory, and
`docs/VECTOR_INDEX_LIFECYCLE.md` §2 records what that cost: the POST landed on one replica
and every GET on another, so the API reported `phase: idle` while a load was running.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from ..aws.s3_readonly import ReadOnlyS3
from ..regions.region_map import PRODUCTS, product_from_dir
from . import doc_gallery as DG

SCHEMA = 1

# The stages of a promotion, in order. Phase 2 (the search-index chain) is declared here
# from day one and rendered greyed: an operator watching a promotion needs to see that
# "in the gallery" is not "searchable", and a screen that hides the remaining work implies
# the promotion is finished when it is staged. `percent` counts implemented stages only,
# so a completed Phase 1 reads 100% rather than 33%.
# ORDER IS THE SAFETY MODEL, not presentation. `publish` stages the whole index version with
# nothing pointing at it; `index_verify` is a GATE that runs while nothing a reader can see has
# changed; only then does `cutover` flip index/latest, and `vectors` reload the store. A verify
# failure therefore has nothing to roll back, which is the entire reason it sits where it does.
STAGES: tuple[dict[str, Any], ...] = (
    # The gallery's own seed step, opt-in via --seed-gallery-from-latest. Without it, a
    # product-scoped promotion mints a manifest containing only what IT promotes, and cutting
    # over deletes every other product from the Doc Gallery -- the same failure the phase-2
    # `seed` stage below exists to prevent for search, just on the gallery side of the pipeline.
    {"key": "gallery_seed", "label": "Seed",         "detail": "other products",        "phase": 1},
    {"key": "select",       "label": "Select",       "detail": "eligibility",           "phase": 1},
    {"key": "copy",         "label": "Doc Gallery",  "detail": "copy documents",        "phase": 1},
    {"key": "manifest",     "label": "Doc Gallery",  "detail": "commit manifest",       "phase": 1},
    {"key": "verify",       "label": "Doc Gallery",  "detail": "verify",                "phase": 1},
    # Seeding is what makes a promotion ADDITIVE. Without the incumbent version's artifacts
    # in the data dir, `aci reindex` would build a multi.npz containing only the promoted
    # regions — and flipping index/latest to that would delete every other jurisdiction from
    # search. index_verify asserts the superset for the same reason.
    {"key": "seed",         "label": "Seed",         "detail": "incumbent artifacts",   "phase": 2},
    {"key": "content",      "label": "Content",      "detail": "trees to content.json", "phase": 2},
    {"key": "embed",        "label": "Embed",        "detail": "sections",              "phase": 2},
    {"key": "flat",         "label": "Flat index",   "detail": "multi.npz",             "phase": 2},
    {"key": "publish",      "label": "Publish",      "detail": "index/<version>",       "phase": 2},
    {"key": "index_verify", "label": "Verify",       "detail": "the gate",              "phase": 2},
    {"key": "cutover",      "label": "Cut over",     "detail": "index/latest",          "phase": 2},
    {"key": "vectors",      "label": "Vector load",  "detail": "clear and reload",      "phase": 2},
    {"key": "live",         "label": "Live",         "detail": "pods rolled",           "phase": 2},
)
STAGE_INDEX = {s["key"]: i for i, s in enumerate(STAGES)}
STAGE_PHASE = {s["key"]: s["phase"] for s in STAGES}
PHASE1_STAGES = tuple(s["key"] for s in STAGES if s["phase"] == 1)

# Five minutes, not extraction's forty-five. A promotion's unit of work is one scorecard GET
# and three server-side copies; five minutes of silence is already pathological. Extraction's
# window is generous because a 281-page MinerU document legitimately takes forty minutes, and
# there is nothing here that legitimately takes five.
PROMOTION_STALE_AFTER_S = float(os.getenv("ACI_PROMOTION_STALE_AFTER", str(5 * 60)))

# A promotion writes its own bookkeeping under this name inside the run.
PROMOTION_DIR = "_promotion"

RECENT_MAX = 20         # rows kept in state.json — a summary, not a log
FAILURES_MAX = 200      # rows kept in failures.json
ERROR_CHARS = 400       # per-failure error text; a bounded file must not become a log
LEASE_TTL_S = float(os.getenv("ACI_PROMOTION_LEASE_TTL", "300"))

GATES_DEFAULT = ("pass", "review")
GATES_ALL = ("pass", "review", "fail", "error", "unknown")

# The three per-document artefacts a promotion moves, plus the two it reads to decide.
# `corpus_worker.build_review_artefacts` already wrote viewer.html and inspect.html into the
# job directory, which is what makes a promotion a server-side COPY rather than a rebuild.
PROMOTE_ARTEFACTS = ("scorecard.json", "viewer.html", "inspect.html")
# scorecard_post_ai.json is OPTIONAL (only documents that ran stage 4/5 have one) and is not
# part of the eligibility decision, but the walk must still see it so `plan_run` knows
# whether to prefer it -- see the `final` rule there, the same one extraction_monitor and
# run_corpus's _final_verdict already apply.
_WALK_WANT = (*PROMOTE_ARTEFACTS, "scorecard_post_ai.json", "rescued_by_toc.json",
             "source.pdf", "corpus_meta.json")


# ---------------------------------------------------------------- identity
# A version string names THREE things — index/<v>/, index/<v>/doc-gallery/ and (in Phase 2)
# aci-vectors-<v> — so it has to be legal in all three. The S3 constraint is loose; the
# OpenSearch one is not: lowercase only, no spaces, and none of : " * + / \ | ? # < > ,
# A version that publishes to S3 happily and then fails at indices.create forty minutes
# later is the worst possible time to find out, so it is validated at mint time.
_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,48}$")


def valid_version(version: str) -> bool:
    """Whether a string is usable as an index version (and so as a promotion id)."""
    return bool(version) and bool(_VERSION_RE.match(version or ""))


def default_version(run: str, attempt: int = 1) -> str:
    """`<run>-p<n>` — the run id is the only string that already identifies the CONTENT.

    A promotion timestamp would be a second opaque id for the same bytes; the attempt
    suffix is what lets a second promotion of the same run (after fixing a bad selection)
    exist without colliding with the first one's prefix or index.
    """
    return f"{run.lower()}-p{int(attempt)}"


def promotion_target(version: str) -> str:
    """The gallery prefix a promotion of `version` writes to.

    Third of the three resolvers that must agree — see `doc_gallery.version_prefix` (the
    reader) and `scripts/lib_gallery.gallery_prefix` (the publisher). They are pinned to
    each other by tests/test_gallery_prefix.py, because a writer that publishes where the
    reader does not read fails silently and a blank tab is the only symptom.
    """
    return DG.version_prefix(version)


def promotion_root(run_prefix: str, pid: str) -> str:
    return f"{run_prefix.rstrip('/')}/{PROMOTION_DIR}/{pid}"


def state_key(run_prefix: str, pid: str) -> str:
    return f"{promotion_root(run_prefix, pid)}/state.json"


def plan_key(run_prefix: str, pid: str) -> str:
    return f"{promotion_root(run_prefix, pid)}/plan.json"


def ledger_key(run_prefix: str, pid: str) -> str:
    return f"{promotion_root(run_prefix, pid)}/ledger.jsonl"


def failures_key(run_prefix: str, pid: str) -> str:
    return f"{promotion_root(run_prefix, pid)}/failures.json"


def lease_key(run_prefix: str, pid: str) -> str:
    return f"{promotion_root(run_prefix, pid)}/lease.json"


def request_key(run_prefix: str, pid: str) -> str:
    return f"{promotion_root(run_prefix, pid)}/request.json"


def stop_key(run_prefix: str, pid: str) -> str:
    return f"{promotion_root(run_prefix, pid)}/stop"


def provenance_key(version: str) -> str:
    """On the TARGET side: which extraction this gallery version came from.

    Today that question is unanswerable from S3 — a published gallery carries no run id at
    all — so a reader looking at a scorecard cannot tell which extraction produced it.
    """
    return f"{promotion_target(version)}/_promotion.json"


# ---------------------------------------------------------------- pure: the view
# Everything below this line is a pure function of the state object and the clock. That is
# what makes the ladder testable, and the ladder is the part that has been got wrong before.


def _num(v, default=0):
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


def stage_rows(state: dict | None, now: float) -> list[dict[str, Any]]:
    """One row per stage, in order, with its derived state.

    ORDER MATTERS in the ladder below, and one pair of rules in particular. Rule 6
    (silence) is checked BEFORE rule 7 (stopping), because a process that announced a
    drain and then died stayed `stopping` — and therefore counted as live — forever:
    extraction run 2026-08-20-08 was still showing a 1010-hour ETA ninety-nine hours after
    its last write. A flag records an intention at one instant; only `updated_at` is
    evidence that the process is still there to act on it.

    Rule 3 is the other one worth naming: a stage whose counters are complete reads
    `complete` however long ago it wrote. A finished stage is not stale, it is finished.
    """
    st = state or {}
    stages = st.get("stages") or {}
    updated = _num(st.get("updated_at"), 0.0)
    quiet = max(0.0, now - updated) if updated else None
    stopping = bool(st.get("stopping"))
    rows = []
    for meta in STAGES:
        key = meta["key"]
        rec = stages.get(key) or {}
        done, total = _num(rec.get("done")), _num(rec.get("total"))
        implemented = meta["phase"] == 1 or bool(rec)
        if rec.get("state") == "failed":                                    # 1
            state_ = "failed"
        elif rec.get("finished_at"):                                        # 2
            state_ = "complete"
        elif total and done >= total:                                       # 3
            state_ = "complete"
        elif not implemented:                                               # 4
            state_ = "not_implemented"
        elif not rec or rec.get("started_at") is None:                      # 5
            state_ = "pending"
        elif quiet is not None and quiet > PROMOTION_STALE_AFTER_S:         # 6
            state_ = "stale"
        elif stopping:                                                      # 7
            state_ = "stopping"
        else:                                                               # 8
            state_ = "running"
        started, finished = rec.get("started_at"), rec.get("finished_at")
        rows.append({
            "key": key, "label": meta["label"], "detail": meta["detail"],
            "phase": meta["phase"], "implemented": implemented, "state": state_,
            "done": done, "total": total or None, "failed": _num(rec.get("failed")),
            "skipped": _num(rec.get("skipped")),
            "percent": round(100.0 * done / total, 1) if total else (
                100.0 if state_ == "complete" else None),
            "started_at": started, "finished_at": finished,
            "seconds": (round(_num(finished, 0.0) - _num(started, 0.0), 1)
                        if started and finished else
                        round(now - _num(started, 0.0), 1) if started else None),
            "note": rec.get("note"),
        })
    return rows


def percent(rows: list[dict[str, Any]]) -> float | None:
    """Overall progress across the stages this promotion actually runs.

    Implemented stages only. A finished Phase-1 promotion reads 100%, not 33% — the
    remaining stages are declared so the operator can see them, not so they can drag the
    number down and make a completed promotion look broken.
    """
    live = [r for r in rows if r["implemented"]]
    if not live:
        return None
    total = 0.0
    for r in live:
        if r["state"] in ("complete", "failed"):
            total += 1.0
        elif r["total"]:
            total += min(1.0, r["done"] / r["total"])
    return round(100.0 * total / len(live), 1)


def derived_state(state: dict | None, rows: list[dict[str, Any]], now: float) -> str:
    """idle | running | stalled | stopping | complete | failed for the whole promotion."""
    if not state:
        return "idle"
    if state.get("error") or any(r["state"] == "failed" for r in rows):
        return "failed"
    live = [r for r in rows if r["implemented"]]
    if live and all(r["state"] == "complete" for r in live):
        return "complete"
    if any(r["state"] == "stale" for r in live):
        return "stalled"
    if state.get("stopping"):
        return "stopping"
    return "running"


def summarize(state: dict | None, now: float | None = None) -> dict[str, Any]:
    """The whole screen, from one state object. Pure."""
    now = time.time() if now is None else now
    rows = stage_rows(state, now)
    st = state or {}
    counts = st.get("counts") or {}
    started = _num(st.get("started_at"), 0.0)
    updated = _num(st.get("updated_at"), 0.0)
    overall = derived_state(state, rows, now)
    live = overall in ("running", "stopping")

    # The clock STOPS at the last write for a promotion that is not running. Extending
    # `elapsed` to now for a dead process is how a stalled run came to report an ETA in
    # hundreds of hours: every second of nobody watching made the estimate worse.
    elapsed = (now if live else updated or now) - started if started else 0.0
    copied = _num(counts.get("copied"))
    rate = (copied / (elapsed / 60.0)) if live and elapsed > 5 and copied else None
    copy_row = next((r for r in rows if r["key"] == "copy"), {})
    remaining = (copy_row.get("total") or 0) - copy_row.get("done", 0)
    return {
        "promotion": st.get("promotion_id"),
        "run": st.get("run"),
        "version": st.get("version"),
        "target_prefix": st.get("target_prefix"),
        "state": overall,
        "percent": percent(rows),
        "stages": rows,
        "current_stage": next((r["key"] for r in rows
                               if r["state"] in ("running", "stopping", "stale")), None),
        "current": st.get("current"),
        "selection": st.get("selection") or {},
        "counts": counts,
        "gates": st.get("gates") or {},
        "recent": (st.get("recent") or [])[:RECENT_MAX],
        "manifest": st.get("manifest") or {},
        # What has actually happened to LIVE traffic, so a screen never has to infer it from
        # the stage list. `complete` means different things for a gallery-only promotion and
        # one that flipped the pointer, and telling them apart is the difference between
        # "staged, nothing is live" and "live for new pods, running pods need a roll".
        "index_published": _stage_done(rows, "publish"),
        "pointer_flipped": _stage_done(rows, "cutover"),
        "vectors_loaded": _stage_done(rows, "vectors"),
        "with_index": any(r["implemented"] and r["phase"] == 2 for r in rows),
        "by": st.get("by"),
        "host": st.get("host"),
        "restarts": _num(st.get("restarts")),
        "started_at": st.get("started_at"),
        "updated_at": st.get("updated_at"),
        "elapsed_seconds": round(elapsed, 1) if started else None,
        # Published alongside the conclusion on purpose. A screen that reports only its
        # verdict is trusted exactly as far as its threshold is, and the extraction
        # monitor's threshold was 45x too generous for two days without anyone knowing.
        "quiet_seconds": round(now - updated, 1) if updated else None,
        "stale_after_seconds": PROMOTION_STALE_AFTER_S,
        "docs_per_minute": round(rate, 1) if rate else None,
        "eta_seconds": round(remaining / rate * 60.0) if rate and remaining > 0 else None,
        "error": st.get("error"),
    }


def _stage_done(rows: list[dict[str, Any]], key: str) -> bool:
    r = next((r for r in rows if r["key"] == key), None)
    return bool(r and r["implemented"] and r["state"] == "complete")


def eligibility(plan: dict | None) -> dict[str, Any]:
    """The bounded counts a plan screen shows — never the per-document rows."""
    p = plan or {}
    docs = p.get("docs") or []
    by_decision: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_product: dict[str, dict[str, int]] = {}
    examples: dict[str, list[str]] = {}
    promote_bytes = 0
    for d in docs:
        dec = d.get("decision") or "?"
        by_decision[dec] = by_decision.get(dec, 0) + 1
        if dec != "promote":
            reason = f"{dec}/{d.get('reason') or '?'}"
            by_reason[reason] = by_reason.get(reason, 0) + 1
            ex = examples.setdefault(reason, [])
            if len(ex) < 20:
                ex.append(d.get("job") or "?")
            continue
        promote_bytes += _num(d.get("bytes"))
        pr = by_product.setdefault(d.get("product") or "?", {})
        g = d.get("gate") or "unknown"
        pr[g] = pr.get(g, 0) + 1
    return {
        "run": p.get("run"),
        "version": p.get("version"),
        "target_prefix": p.get("target_prefix"),
        "index_latest": p.get("index_latest"),
        "products": p.get("products") or list(PRODUCTS),
        "gates": p.get("gates") or list(GATES_DEFAULT),
        "eligible": by_decision.get("promote", 0),
        "decisions": by_decision,
        "excluded": by_reason,
        "examples": examples,
        "by_product": by_product,
        "objects": sum(_num(d.get("objects")) for d in docs
                       if d.get("decision") == "promote"),
        "bytes": promote_bytes,
        "products_walked": p.get("products_walked"),
        "jobs_seen": len(docs),
    }


# ---------------------------------------------------------------- pure: gallery stats
def promotion_stats(sc: dict | None) -> dict[str, Any]:
    """The manifest's `stats` block, from the scorecard ALONE.

    `push_hybrid_s3.doc_display_stats` opens the source PDF with fitz for the page count and
    rglobs the whole Stage-3 tree for surviving snapshot references. A promotion copies three
    small objects per document server-side; downloading a 7MB PDF and a tree per document to
    recover four numbers the scorecard already carries would cost more than the promotion.

        pages             EXACT   sc.pages.total IS stage-1's fitz page count
        tables_total      EXACT   sc.stage3.tables_total   (check_scorecard embeds the
        tables_converted  EXACT   sc.stage3.tables_filled   stage3 report verbatim)
        tables_failed     EXACT   sc.stage3.tables_failed
        pages_snapshotted APPROX  see below

    `pages_snapshotted` is a property of the FINAL TREE — the pages whose snapshot survived
    `clean_mineru_text` + `strip_filled_snapshots` — so it cannot be read off the scorecard
    exactly. The union below names the same set from the other side: a page marked
    `unvalidatable` is, by `check_scorecard._page_states`, one that was snapshotted in
    stage 1 and not covered by an OK table; and a failed table's pages kept their snapshot
    because the table failed. Only the COUNT is ever rendered (`_snap_count`, `galBadges`),
    and it is tagged with its source so a reader is never left guessing which number this is.

    `files_written` and `word_delta` are absent, as they already are on every published
    MinerU row: they come from `extract_to_s3.parse_report` reading CONVERSION_REPORT.md,
    which this path has never run. Absent, not invented.
    """
    sc = sc or {}
    pages = sc.get("pages") or {}
    stage3 = sc.get("stage3") or {}
    snap = {int(p) for p, st in (pages.get("states") or {}).items()
            if st == "unvalidatable" and str(p).isdigit()}
    for t in sc.get("tables") or []:
        if t.get("bucket") == "failed":
            snap.update(int(p) for p in (t.get("pages") or []) if str(p).isdigit())
    return {
        "pages": pages.get("total") or None,
        "tables_converted": stage3.get("tables_filled"),
        "tables_total": stage3.get("tables_total"),
        "tables_failed": stage3.get("tables_failed"),
        "pages_snapshotted": sorted(snap),
        "pages_snapshotted_source": "scorecard",
    }


# ---------------------------------------------------------------- the plan
def _decide(gate: str | None, artefacts: dict, gates: tuple[str, ...]) -> tuple[str, str]:
    """(decision, reason) for one job directory. Order is the cheapest test first."""
    if "scorecard.json" not in artefacts:
        return "skip", "no_scorecard"
    if (gate or "unknown") not in gates:
        return "skip", "gate"
    if "viewer.html" not in artefacts:
        # A run carries documents with no viewer: build_review_artefacts is best-effort and
        # 123 documents in run 2026-08-21-02 were extracted before it existed at all. A
        # manifest row pointing at a viewer that is not there is a guaranteed 404 the
        # reviewer cannot tell apart from a broken one, so these are held back and named.
        return "blocked", "no_viewer"
    return "promote", ""


def plan_run(s3: ReadOnlyS3, run: str, run_prefix: str, version: str,
             products: tuple[str, ...] | list[str] | None = None,
             gates: tuple[str, ...] | list[str] = GATES_DEFAULT,
             names: dict | None = None, fanout: int | None = None,
             exclude: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
    """Decide, without writing anything, exactly what a promotion of this run would do.

    Shared by `scripts/promote_run.py` (which then acts on it) and
    `GET /api/promotion/plan` (which only shows it), so the plan an operator approves is
    the plan the job executes — computed by the same code, not by two that agree today.

    COST. The product allow-list is applied to the PRODUCT PREFIX before any job directory
    beneath it is listed: a run carries ~36 product directories and the allow-list has 3.
    Then one scorecard GET per candidate document, fanned out, and each scorecard is reduced
    to its decision row and DROPPED. Retaining them measured 96MB for one 986-document run,
    in the same process as the vector matrix, and "the gallery loaded every scorecard" is
    what took the service down once.

    That single GET is also the ONLY read of each scorecard in the whole promotion: the copy
    stage moves the object server-side and takes its `stats` from this row.
    """
    allowed = tuple(products) if products else tuple(PRODUCTS)
    allowed_set = set(allowed)
    gates = tuple(gates)

    def product_filter(dir_name: str) -> bool:
        return product_from_dir(dir_name) in allowed_set

    jobs, product_prefixes = DG.walk_run_jobs(
        s3, run_prefix, want=_WALK_WANT, product_filter=product_filter, fanout=fanout)

    pool = max(1, fanout if fanout is not None else DG._FANOUT)
    scored = sorted(j for j, arts in jobs.items() if "scorecard.json" in arts)

    from concurrent.futures import ThreadPoolExecutor

    def read(job: str) -> tuple[str, dict, dict | None]:
        try:
            sc = json.loads(s3.get_bytes(f"{run_prefix}/{job}/scorecard.json"))
        except Exception:                                    # noqa: BLE001
            sc = {}
        # Only asked for when the walk already saw it -- most documents never ran stage
        # 4/5, and this must stay the ONLY scorecard GET per candidate for the majority.
        sc_post = None
        if "scorecard_post_ai.json" in jobs[job]:
            try:
                sc_post = json.loads(s3.get_bytes(f"{run_prefix}/{job}/scorecard_post_ai.json"))
            except Exception:                                # noqa: BLE001
                sc_post = None
        return job, sc, sc_post

    cards: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=pool) as ex:
        for job, sc, sc_post in ex.map(read, scored):
            # THE FINAL VERDICT, not the extraction gate: scorecard.json is frozen at
            # stage 3 (the --resume marker), but when stage 4/5 ran, that tree is what
            # actually ships -- see extraction_monitor.job_detail / run_corpus's
            # _final_verdict for the same rule applied to the same question, "is this
            # document good". A promotion that only ever read scorecard.json would
            # publish (and gate on) a verdict the AI pass had already overturned.
            final = sc_post if (sc_post and sc_post.get("gate") is not None) else sc
            # Reduced here and the scorecard let go — see the cost note above.
            cards[job] = {"gate": final.get("gate"), "worst_score": final.get("worst_score"),
                          "stats": promotion_stats(final),
                          "rescued": _rescue_from_scorecard(sc),
                          "has_post_ai": final is sc_post}

    excluded_set = {e.strip().strip("/") for e in (exclude or []) if e and e.strip()}
    docs, used = [], set()
    for job in sorted(jobs):
        arts = jobs[job]
        product_dir, _, label = job.partition("/")
        product = product_from_dir(product_dir)
        jur, _, doc_id = label.rpartition("__")
        card = cards.get(job) or {}
        gate = card.get("gate")
        decision, reason = _decide(gate, arts, gates)
        if product not in allowed_set:
            decision, reason = "skip", "product"
        elif job in excluded_set:
            # Excluded here, in the PLAN, so the document leaves the gallery and the index
            # together. Dropping it from only one recreates exactly what the content
            # stage's failure guard exists to prevent: a document the gallery offers and
            # the index cannot answer about.
            decision, reason = "skip", "excluded"
        slug = _gslug(product, jur or label, doc_id or label)
        if decision == "promote":
            if slug in used:
                # Distinct documents whose slugs collide. Refuse rather than let one
                # overwrite the other's manifest row and its viewer key.
                decision, reason = "blocked", "slug_collision"
            else:
                used.add(slug)
        row = {
            "job": job, "product_dir": product_dir, "product": product,
            "jurisdiction": jur or label, "doc_id": doc_id or label, "slug": slug,
            "gate": gate, "worst_score": card.get("worst_score"),
            "decision": decision, "reason": reason,
            "artefacts": sorted(a for a in PROMOTE_ARTEFACTS if a in arts),
            "has_toc_rescue": "rescued_by_toc.json" in arts,
            # Whether stage 4/5 produced a verdict for this document -- if so, `gate` and
            # `worst_score` above already come from it (see the `final` rule at plan time),
            # and copy_doc also publishes scorecard_post_ai.json so the gallery's own
            # scorecard fetch reads the same tree the table's numbers came from.
            "has_post_ai": bool(card.get("has_post_ai")),
            "objects": sum(1 for a in PROMOTE_ARTEFACTS if a in arts),
            "bytes": sum(v for k, v in arts.items() if k in PROMOTE_ARTEFACTS),
        }
        if decision == "promote":
            row["stats"] = card.get("stats")
            row["rescued"] = card.get("rescued")
            row.update(_name_fields(row["doc_id"], names))
        docs.append(row)

    return {"schema": SCHEMA, "run": run, "version": version,
            "target_prefix": promotion_target(version),
            "products": list(allowed), "gates": list(gates),
            "products_walked": len(product_prefixes),
            "planned_at": time.time(), "docs": docs}


def _gslug(product: str, region: str, stem: str) -> str:
    """The manifest's slug. Mirrors scripts/lib_gallery.gslug — see the note there on why
    the "-mineru" suffix is load-bearing. Duplicated in ONE line rather than importing
    scripts/ from the package; tests/test_gallery_prefix.py asserts the two agree."""
    return re.sub(r"[^a-z0-9]+", "-", f"{product}-{region}-{stem}".lower()).strip("-") + "-mineru"


def _rescue_from_scorecard(sc: dict | None) -> dict | None:
    fb = (sc or {}).get("fallback") or {}
    if fb.get("adopted_tier") != "mineru_full":
        return None
    first = fb.get("first_attempt") or {}
    toc = next((c for c in (fb.get("chain") or []) if c.get("tier") == "toc_rescue"), {})
    return {"method": "mineru-full", "was": first.get("gate"),
            "was_score": first.get("worst_score"),
            "was_completeness": first.get("completeness_score"),
            "toc_rescue": toc.get("status")}


def _name_fields(doc_id: str, names: dict | None) -> dict:
    """{doc_name, doc_version, doc_date} from a prebuilt Doc_metadata index, if there is one.

    The gallery shows a bare numeric id otherwise, which is what a run-mode row shows today.
    Never fails a promotion: an unknown id simply keeps the id.
    """
    r = (names or {}).get(str(doc_id)) or {}
    out = {"doc_name": r.get("DOCNAME") or r.get("OPINIONNAME"),
           "doc_version": r.get("VERSION"),
           "doc_date": r.get("SOURCEDATE") or r.get("MODIFIEDDATE")}
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- I/O
# One bounded GET per answer. Nothing cached in module state: see the module docstring on
# why per-pod job state is the anti-pattern here.


def _s3(bucket: str, region: str) -> ReadOnlyS3:
    return ReadOnlyS3(bucket=bucket, region=region)


def _get_json(s3: ReadOnlyS3, key: str) -> Any:
    try:
        return json.loads(s3.get_bytes(key))
    except Exception:                                        # noqa: BLE001
        return None


def run_prefix_for(root: str, run: str) -> str:
    root = (root or "").strip("/")
    return f"{root}/{run}" if root else run


def promotions_for_run(bucket: str, root: str, region: str, run: str,
                       now: float | None = None) -> list[dict[str, Any]]:
    """Every promotion of one run, newest first — cheap metadata only.

    One delimiter listing of `<run>/_promotion/` plus one state GET each. A run has one or
    two promotions, not hundreds, so the per-promotion GET is affordable here in a way the
    per-document reads never are.
    """
    now = time.time() if now is None else now
    s3 = _s3(bucket, region)
    rp = run_prefix_for(root, run)
    out = []
    for cp in s3.list_common_prefixes(f"{rp}/{PROMOTION_DIR}/"):
        pid = cp.rstrip("/").rsplit("/", 1)[-1]
        if not pid:
            continue
        state = _get_json(s3, state_key(rp, pid))
        s = summarize(state, now)
        out.append({"promotion": pid, "run": run, "version": (state or {}).get("version") or pid,
                    "state": s["state"], "percent": s["percent"],
                    "started_at": s["started_at"], "updated_at": s["updated_at"],
                    "counts": s["counts"], "requested": state is None})
    return sorted(out, key=lambda p: p.get("updated_at") or 0, reverse=True)


def list_promotions(bucket: str, root: str, region: str,
                    now: float | None = None) -> list[dict[str, Any]]:
    """Every promotion under `root`, across every run, most recently active first."""
    now = time.time() if now is None else now
    s3 = _s3(bucket, region)
    root = (root or "").strip("/")
    out: list[dict[str, Any]] = []
    for cp in s3.list_common_prefixes(f"{root}/"):
        run = cp.rstrip("/").rsplit("/", 1)[-1]
        if not run or run.startswith("_"):
            continue
        out.extend(promotions_for_run(bucket, root, region, run, now))
    return sorted(out, key=lambda p: p.get("updated_at") or 0, reverse=True)


def progress(bucket: str, root: str, region: str, run: str, pid: str,
             now: float | None = None) -> dict[str, Any] | None:
    """The screen for one promotion — ONE GET. None when there is no such promotion."""
    s3 = _s3(bucket, region)
    rp = run_prefix_for(root, run)
    state = _get_json(s3, state_key(rp, pid))
    if state is None:
        # A promotion that has been REQUESTED but not yet picked up has a request and no
        # state. That is a real, showable status — "waiting for a promoter" — and it must
        # not read as "no such promotion", which is what sends an operator hunting.
        req = _get_json(s3, request_key(rp, pid))
        if req is None:
            return None
        s = summarize(None, now)
        s.update({"promotion": pid, "run": run, "version": req.get("version") or pid,
                  "state": "requested", "requested": req,
                  "target_prefix": promotion_target(req.get("version") or pid)})
        return s
    s = summarize(state, now)
    s["requested"] = None
    s["stopping_requested"] = s3.exists(stop_key(rp, pid))
    return s


def read_plan(bucket: str, root: str, region: str, run: str, pid: str,
              offset: int = 0, limit: int = 500,
              decision: str | None = None) -> dict[str, Any]:
    """A BOUNDED slice of the per-document decisions, plus the whole plan's counts.

    The counts come from `eligibility` over every row; only the slice is returned. A
    1000-document plan is ~400KB and the screen paints 60 rows at a time.
    """
    s3 = _s3(bucket, region)
    plan = _get_json(s3, plan_key(run_prefix_for(root, run), pid)) or {}
    docs = plan.get("docs") or []
    if decision:
        docs = [d for d in docs if d.get("decision") == decision]
    limit = max(1, min(int(limit), 2000))
    offset = max(0, int(offset))
    return {"summary": eligibility(plan), "total": len(docs), "offset": offset,
            "limit": limit, "docs": docs[offset:offset + limit]}


def read_failures(bucket: str, root: str, region: str, run: str,
                  pid: str) -> list[dict[str, Any]]:
    """Bounded already, by the writer — the last FAILURES_MAX, error text truncated."""
    f = _get_json(_s3(bucket, region), failures_key(run_prefix_for(root, run), pid))
    return f if isinstance(f, list) else []


def lease_state(bucket: str, root: str, region: str, run: str, pid: str,
                now: float | None = None) -> dict[str, Any] | None:
    """held | expired for the lease on this promotion; None when nobody holds one."""
    now = time.time() if now is None else now
    lease = _get_json(_s3(bucket, region), lease_key(run_prefix_for(root, run), pid))
    if not isinstance(lease, dict):
        return None
    beat = _num(lease.get("heartbeat_at"), 0.0) or _num(lease.get("acquired_at"), 0.0)
    ttl = _num(lease.get("ttl_s"), LEASE_TTL_S)
    age = now - beat if beat else None
    # A released lease is a WRITE, not a delete: the promotion role has no DeleteObject
    # anywhere, deliberately. So "free" is a state of the object, not its absence.
    if lease.get("released_at"):
        state = "free"
    elif age is not None and age > ttl:
        state = "expired"
    else:
        state = "held"
    return {**lease, "age_seconds": round(age, 1) if age is not None else None,
            "state": state}


def compute_plan(bucket: str, root: str, region: str, run: str, version: str | None = None,
                 products=None, gates=GATES_DEFAULT) -> dict[str, Any]:
    """The live dry run behind GET /api/promotion/plan. Reads only; writes nothing."""
    rp = run_prefix_for(root, run)
    version = version or default_version(run)
    return plan_run(_s3(bucket, region), run, rp, version,
                    products=products, gates=gates)


def request_promotion(bucket: str, root: str, region: str, run: str, version: str,
                      by: str | None = None, products=None, gates=GATES_DEFAULT,
                      limit: int | None = None, merge_into_live: bool = False,
                      seed_gallery_from_latest: bool = False,
                      now: float | None = None) -> dict[str, Any]:
    """Ask for a promotion by writing `request.json`. Does NOT start anything.

    The service has no `batch/v1` RBAC and granting a public-facing pod the right to create
    Kubernetes Jobs is a separate security decision, so the API records the request and a
    promoter (an operator, or an ArgoCD/CronJob watcher) runs `promote_run.py --from-request`.
    Exactly the shape `extraction_monitor.mark_retry` already uses for re-extraction, which
    works and which operators already understand. The UI must therefore say "requested —
    waiting for a promoter", not "promoting".

    Refused, rather than queued twice, when a promotion of this version is already live or
    leased: two writers into one manifest is the incident this whole design is arranged
    around.
    """
    import boto3

    now = time.time() if now is None else now
    if not valid_version(version):
        return {"queued": False, "reason": f"not a valid index version: {version!r}"}
    rp = run_prefix_for(root, run)
    existing = progress(bucket, root, region, run, version, now)
    if existing and existing["state"] in ("running", "stopping", "requested"):
        return {"queued": False, "promotion": version, "reason":
                f"a promotion of {version} is already {existing['state']}"}
    lease = lease_state(bucket, root, region, run, version, now)
    if lease and lease["state"] == "held":
        return {"queued": False, "promotion": version, "reason":
                f"leased by {lease.get('host') or lease.get('holder') or 'another promoter'}"}
    body = json.dumps({"schema": SCHEMA, "run": run, "version": version,
                       "products": list(products) if products else list(PRODUCTS),
                       "gates": list(gates), "limit": limit,
                       "merge_into_live": bool(merge_into_live),
                       "seed_gallery_from_latest": bool(seed_gallery_from_latest),
                       "requested_at": now, "by": by or "unknown"}, indent=2).encode()
    boto3.client("s3", region_name=region).put_object(
        Bucket=bucket, Key=request_key(rp, version), Body=body,
        ContentType="application/json")
    return {"queued": True, "promotion": version, "run": run, "version": version,
            "target_prefix": promotion_target(version)}


def request_stop(bucket: str, root: str, region: str, run: str, pid: str,
                 by: str | None = None) -> dict[str, Any]:
    """Ask a running promotion to drain: finish the document in flight, publish, exit.

    A marker object rather than a signal, for the same reason extraction uses one: the
    process to be stopped may not exist yet, may be restarting, or may be on a node nobody
    can reach — and the marker is durable and re-read on every loop.
    """
    import boto3

    rp = run_prefix_for(root, run)
    if progress(bucket, root, region, run, pid) is None:
        return {"stopping": False, "reason": f"no promotion {pid} of run {run}"}
    boto3.client("s3", region_name=region).put_object(
        Bucket=bucket, Key=stop_key(rp, pid),
        Body=json.dumps({"by": by or "unknown", "at": time.time()}).encode(),
        ContentType="application/json")
    return {"stopping": True, "promotion": pid, "run": run}
