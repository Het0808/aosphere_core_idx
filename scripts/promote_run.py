#!/usr/bin/env python3
"""promote_run — publish a finished extraction run into the Doc Gallery.

    promote_run.py --run 2026-08-21-02 --plan          # decide, print, write NOTHING
    promote_run.py --run 2026-08-21-02 --probe-write   # prove the index/* IAM in 2 seconds
    promote_run.py --run 2026-08-21-02                 # do it
    promote_run.py --from-request --run … --version …  # what the K8s Job runs

A run writes to `corpus/<run>/`; the published gallery reads `index/<version>/doc-gallery/`.
`extraction_monitor.py` and `corpus_worker.py` both say it outright — "publishing is a
separate step against a finished corpus" — and for a CLUSTER run that step did not exist:
`push_hybrid_s3.py` is a local-tree publisher from end to end (local paths, a local PDF
base64'd into the viewer, fitz for the page count, an SSO profile that no pod has).

This is cheap because the work is already done. `corpus_worker.build_review_artefacts`
wrote `viewer.html` and `inspect.html` INTO each S3 job directory, so a promotion is one
scorecard GET (paid once, at plan time) and two or three server-side `copy_object` calls
per document. No PDF is downloaded, no viewer is rebuilt, no tree is walked.

WHAT IS SAFE ABOUT IT, in one property: **objects are invisible until the manifest names
them**, and the manifest is written ONCE, at the end. A promotion that dies — a spot
reclaim, a 403, an operator's stop — leaves the live gallery byte-for-byte untouched. The
resume then re-lists the target, skips what is already there, and commits at the end of a
completed pass. `--manifest-every N` exists for a promotion too large to be all-or-nothing;
it is off by default, and it is the only way to give that property up.

Progress is durable in S3 under `corpus/<run>/_promotion/<version>/` and read by
`service/promotion_monitor.py`; see that module for the layout and why it lives there.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lib_gallery import LEGACY_PREFIX, rescue_from_toc  # noqa: E402

from aosphere_core_index.regions.region_map import PRODUCTS, product_from_dir  # noqa: E402
from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402
from aosphere_core_index.service.doc_gallery import valid_run  # noqa: E402
from aosphere_core_index.config import settings as _settings

CT = {"viewer.html": "text/html; charset=utf-8",
      "inspect.html": "text/html; charset=utf-8",
      "scorecard.json": "application/json",
      "scorecard_post_ai.json": "application/json"}

# A single copy_object caps at 5GB. A viewer is 3-7MB so this is never hit, but a 400-page
# monster would fail the promotion rather than take the managed-copy path, and the check is
# free: the size came back with the listing.
_COPY_MAX = 5_000_000_000

LEDGER_EVERY_ROWS = 25          # S3 cannot append, so the ledger is re-uploaded whole
LEDGER_EVERY_S = 30.0
STOP_CHECK_EVERY_S = 15.0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _client(region: str, profile: str | None = None):
    """A WRITABLE S3 client, made fresh at the point of use.

    Bounded timeouts and adaptive retries, and deliberately not held across a long phase:
    `push_hybrid_s3._s3_client`'s note records what a reused client cost — its SSO
    credentials came due for refresh mid-phase and the very next upload hung past every
    configured timeout.
    """
    import boto3
    from botocore.config import Config

    cfg = Config(connect_timeout=10, read_timeout=90,
                 retries={"max_attempts": 4, "mode": "adaptive"},
                 max_pool_connections=max(10, int(os.getenv("ACI_S3_MAX_POOL", "80"))))
    profile = profile or os.getenv("AWS_PROFILE") or None
    session = boto3.Session(profile_name=profile, region_name=region) if profile else \
        boto3.Session(region_name=region)
    return session.client("s3", config=cfg)


def _err_code(e) -> str:
    return getattr(e, "response", {}).get("Error", {}).get("Code", "") or ""


# ---------------------------------------------------------------- the lease
class Lease:
    """One promotion per target version, enforced by an object.

    `promotion_id == version`, so "a second concurrent promotion of this version" and "the
    same promotion" are the same thing — which is what makes this enforceable at all. Two
    writers merging into one `manifest.json` is the 455-documents-became-5 incident in its
    concurrent form, and that one was a single process getting the merge wrong.

    Acquired with a conditional PUT (`IfNoneMatch: *`), which is a real compare-and-swap.
    Released by WRITING `released_at`, not by deleting: the promotion role has no
    DeleteObject anywhere, on purpose.
    """

    def __init__(self, s3, bucket: str, key: str, ttl_s: float = PM.LEASE_TTL_S):
        self.s3, self.bucket, self.key, self.ttl_s = s3, bucket, key, ttl_s
        self.holder = f"{socket.gethostname()}:{os.getpid()}"
        self._beat_at = 0.0
        self.held = False

    def _body(self, **extra) -> bytes:
        now = time.time()
        return json.dumps({"holder": self.holder, "host": socket.gethostname(),
                           "pid": os.getpid(), "acquired_at": now, "heartbeat_at": now,
                           "ttl_s": self.ttl_s, **extra}, indent=2).encode()

    def acquire(self, force: bool = False) -> tuple[bool, str]:
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=self._body(),
                               ContentType="application/json", IfNoneMatch="*")
            self.held, self._beat_at = True, time.time()
            return True, ""
        except Exception as e:                               # noqa: BLE001
            if _err_code(e) not in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise
        try:
            cur = json.loads(self.s3.get_object(Bucket=self.bucket, Key=self.key)["Body"].read())
        except Exception:                                    # noqa: BLE001
            cur = {}
        who = cur.get("host") or cur.get("holder") or "another promoter"
        # THREE states, not two. A RELEASED lease was handed back cleanly, so the next
        # promoter takes it with no ceremony. An EXPIRED one only stopped writing, which is
        # evidence and not permission: the holder may be paused rather than dead, and
        # stealing it would put two writers on one manifest.
        if cur.get("released_at"):
            self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=self._body(),
                               ContentType="application/json")
            self.held, self._beat_at = True, time.time()
            return True, ""
        beat = cur.get("heartbeat_at") or cur.get("acquired_at") or 0
        age = time.time() - beat if beat else None
        if age is None or age <= (cur.get("ttl_s") or self.ttl_s):
            return False, (f"held by {who}"
                           + (f", last heartbeat {int(age)}s ago" if age else ""))
        if not force:
            return False, (f"a lease from {who} looks abandoned"
                           f" ({int(age)}s since its last heartbeat)"
                           " — pass --force-lease to take it over")
        self.s3.put_object(Bucket=self.bucket, Key=self.key,
                           Body=self._body(broken_from=who, broken_at=time.time()),
                           ContentType="application/json")
        self.held, self._beat_at = True, time.time()
        return True, f"took over an abandoned lease from {who}"

    def beat(self, every_s: float = 30.0) -> None:
        if not self.held or time.time() - self._beat_at < every_s:
            return
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=self._body(),
                               ContentType="application/json")
            self._beat_at = time.time()
        except Exception as e:                               # noqa: BLE001
            log(f"⚠ lease heartbeat failed ({type(e).__name__}) — continuing")

    def release(self) -> None:
        if not self.held:
            return
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self.key,
                               Body=self._body(released_at=time.time()),
                               ContentType="application/json")
        except Exception as e:                               # noqa: BLE001
            log(f"⚠ lease release failed ({type(e).__name__}) — it will expire in "
                f"{int(self.ttl_s)}s")
        self.held = False


# ---------------------------------------------------------------- durable progress
class PromotionProgress:
    """The promotion's state, rewritten to S3 as it goes.

    Modelled on `corpus_worker.ShardProgress`, including the part that is easy to leave
    out: `_carry_forward`. Counters live in this process, and S3 has no append — so a
    restarted pod that starts from an empty local ledger REPLACES the published history
    with one row, and the screen goes from "431 done" to "1 done". That is not a display
    bug, it is the record being destroyed. So the published state and ledger are read back
    before the first document and the counters resume from them.

    Every write is best-effort. A progress write that fails must never fail the promotion:
    the promotion is the thing with value, the progress object is how we watch it.
    """

    def __init__(self, s3, bucket: str, run: str, run_prefix: str, version: str,
                 by: str | None, selection: dict):
        self.s3, self.bucket = s3, bucket
        self.run_prefix, self.pid = run_prefix, version
        self.state_key = PM.state_key(run_prefix, version)
        self.ledger_key = PM.ledger_key(run_prefix, version)
        self.failures_key = PM.failures_key(run_prefix, version)
        self.stop_key = PM.stop_key(run_prefix, version)
        now = time.time()
        self.d: dict = {
            "schema": PM.SCHEMA, "promotion_id": version, "run": run,
            "run_prefix": run_prefix, "version": version,
            "target_prefix": PM.promotion_target(version),
            "started_at": now, "updated_at": now, "by": by or "unknown",
            "host": socket.gethostname(), "restarts": 0, "stopping": False,
            "selection": selection, "stages": {}, "current": None,
            "counts": {"copied": 0, "skipped": 0, "failed": 0, "objects": 0, "bytes": 0},
            "gates": {}, "recent": [], "manifest": {}, "error": None,
        }
        self.ledger: list[str] = []
        self.failures: list[dict] = []
        # Failures IN THIS PASS, deliberately not carried forward. `counts.failed` is the
        # cumulative record and belongs in the state object; the EXIT CODE has to be about
        # this attempt, or a promotion that fully succeeds on its retry still exits non-zero
        # and the Job keeps retrying a finished promotion until it marks itself Failed.
        self.pass_failed = 0
        self._ledger_at = 0.0
        self._ledger_rows = 0
        self._stop_at = 0.0
        self._stop = False

    # ---- resume ----
    def carry_forward(self) -> None:
        prev = self._get_json(self.state_key)
        if isinstance(prev, dict) and prev.get("promotion_id") == self.pid:
            for k in ("counts", "gates", "stages", "recent", "manifest",
                     "rewritten", "content", "index", "verify"):
                if prev.get(k):
                    self.d[k] = prev[k]
            self.d["started_at"] = prev.get("started_at") or self.d["started_at"]
            self.d["restarts"] = int(prev.get("restarts") or 0) + 1
            log(f"resuming promotion {self.pid} (restart #{self.d['restarts']}, "
                f"{self.d['counts'].get('copied', 0)} already copied)")
        try:
            raw = self.s3.get_object(Bucket=self.bucket, Key=self.ledger_key)["Body"].read()
            self.ledger = [ln for ln in raw.decode("utf-8", "replace").split("\n") if ln]
        except Exception:                                    # noqa: BLE001
            self.ledger = []
        prevf = self._get_json(self.failures_key)
        self.failures = prevf if isinstance(prevf, list) else []

    def _get_json(self, key: str):
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except Exception:                                    # noqa: BLE001
            return None

    # ---- stages ----
    def stage(self, key: str, total: int | None = None, note: str | None = None) -> None:
        rec = self.d["stages"].setdefault(key, {})
        if rec.get("finished_at"):
            return                                           # already done on a prior pass
        rec.setdefault("started_at", time.time())
        rec.setdefault("done", 0)
        rec.setdefault("failed", 0)
        rec.setdefault("skipped", 0)
        if total is not None:
            rec["total"] = int(total)
        if note:
            rec["note"] = note
        rec["state"] = "running"
        log(f"stage {key}" + (f" — {rec.get('total')} units" if rec.get("total") else ""))
        self.publish()

    def finish(self, key: str, **fields) -> None:
        rec = self.d["stages"].setdefault(key, {})
        rec.update(fields)
        rec["finished_at"] = time.time()
        rec["state"] = "complete"
        if rec.get("total") is None:
            rec["total"] = rec.get("done") or 1
            rec["done"] = rec["total"]
        self.d["current"] = None
        self.publish(force=True)

    def fail(self, key: str, err: str) -> None:
        rec = self.d["stages"].setdefault(key, {})
        rec["state"] = "failed"
        rec["finished_at"] = time.time()
        rec["error"] = err[:PM.ERROR_CHARS]
        self.d["error"] = f"{key}: {err[:PM.ERROR_CHARS]}"
        self.publish(force=True)

    def bump(self, key: str, field: str = "done", n: int = 1) -> None:
        rec = self.d["stages"].setdefault(key, {})
        rec[field] = int(rec.get(field) or 0) + n

    # ---- per-document ----
    def start_doc(self, row: dict) -> None:
        self.d["current"] = {"job": row["job"], "slug": row["slug"],
                             "product": row["product"], "started_at": time.time()}

    def copied(self, row: dict, objects: int, nbytes: int, seconds: float) -> None:
        c = self.d["counts"]
        c["copied"] += 1
        c["objects"] += objects
        c["bytes"] += nbytes
        g = row.get("gate") or "unknown"
        self.d["gates"][g] = int(self.d["gates"].get(g) or 0) + 1
        self.bump("copy")
        self._recent({"job": row["job"], "slug": row["slug"], "gate": g,
                      "objects": objects, "seconds": round(seconds, 2), "status": "copied"})
        self._ledger_row({"ts": time.time(), "job": row["job"], "slug": row["slug"],
                          "gate": g, "objects": objects, "bytes": nbytes,
                          "seconds": round(seconds, 2), "status": "copied"})

    def skipped(self, row: dict, reason: str = "present") -> None:
        self.d["counts"]["skipped"] += 1
        self.bump("copy")
        self.bump("copy", "skipped")
        self._ledger_row({"ts": time.time(), "job": row["job"], "slug": row["slug"],
                          "status": "skipped", "reason": reason})

    def doc_failed(self, row: dict, exc: BaseException) -> None:
        self.d["counts"]["failed"] += 1
        self.pass_failed += 1
        self.bump("copy")
        self.bump("copy", "failed")
        err = f"{type(exc).__name__}: {exc}"[:PM.ERROR_CHARS]
        self._recent({"job": row["job"], "slug": row["slug"], "status": "failed",
                      "error": err})
        self._ledger_row({"ts": time.time(), "job": row["job"], "slug": row["slug"],
                          "status": "failed", "error": err})
        # Bounded. A file the screen reads on every poll must not become a log — the
        # extraction ledger's rule, and the reason the error text is truncated too.
        self.failures.insert(0, {"ts": time.time(), "job": row["job"],
                                 "slug": row["slug"], "error": err})
        del self.failures[PM.FAILURES_MAX:]
        self._put(self.failures_key, json.dumps(self.failures, indent=2).encode())
        log(f"  ✗ {row['job']}: {err}")

    def _recent(self, row: dict) -> None:
        self.d["recent"].insert(0, row)
        del self.d["recent"][PM.RECENT_MAX:]

    def _ledger_row(self, row: dict) -> None:
        self.ledger.append(json.dumps(row, ensure_ascii=False))
        self._ledger_rows += 1

    # ---- publishing ----
    def _put(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                               ContentType=content_type)
        except Exception as e:                               # noqa: BLE001
            log(f"⚠ could not write {key.rsplit('/', 1)[-1]} ({type(e).__name__}) "
                f"— continuing")

    def publish(self, force: bool = False) -> None:
        self.d["updated_at"] = time.time()
        self._put(self.state_key, json.dumps(self.d, ensure_ascii=False, indent=1).encode())
        # The ledger is re-uploaded WHOLE (S3 has no append), so it is throttled where the
        # 3KB state object is not: at 600 documents, per-document re-uploads of a growing
        # file are tens of megabytes of PUTs for a file nothing reads per document.
        if (force or self._ledger_rows >= LEDGER_EVERY_ROWS
                or time.time() - self._ledger_at > LEDGER_EVERY_S):
            if self.ledger:
                self._put(self.ledger_key,
                          ("\n".join(self.ledger) + "\n").encode("utf-8"), "application/x-ndjson")
            self._ledger_at, self._ledger_rows = time.time(), 0

    def write_plan(self, plan: dict) -> None:
        self._put(PM.plan_key(self.run_prefix, self.pid),
                  json.dumps(plan, ensure_ascii=False).encode())

    def stopping(self) -> bool:
        """Has somebody asked this promotion to drain? Re-read, throttled, from S3.

        A marker object rather than a signal: the stop may be requested before this process
        exists, while it is restarting, or from a laptop that cannot reach the node.
        """
        if self._stop:
            return True
        if time.time() - self._stop_at < STOP_CHECK_EVERY_S:
            return False
        self._stop_at = time.time()
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.stop_key)
            self._stop = True
            self.d["stopping"] = True
            log("stop requested — draining after the current document")
        except Exception:                                    # noqa: BLE001
            pass
        return self._stop


# ---------------------------------------------------------------- resume: what is there
def present_slugs(s3, bucket: str, target: str) -> set[str]:
    """Slugs already fully published under the target prefix — ONE paginated listing.

    A document counts as present only when BOTH its viewer and its scorecard are there: a
    half-copied document must be finished, not skipped.

    The authority is the TARGET, not a marker file this job wrote. A pod that copied and
    then died would under-report from its own marker (safe, but not true), and the target
    is the thing the gallery actually reads. Rejected: a HEAD per document —
    `push_hybrid_s3.present_docs` measured 455 sequential round trips, 8-15 minutes before
    the first upload.

    And unlike `push_hybrid_s3.unchanged_docs` this does NOT compare verdicts. A promotion
    is pinned to one immutable run prefix, so the source objects cannot change underneath
    it: presence is sufficient here, and correct.
    """
    seen: dict[str, set[str]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                             Prefix=f"{target}/"):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            for suffix, kind in (("-viewer.html", "v"), ("-scorecard.json", "s")):
                if name.endswith(suffix):
                    seen.setdefault(name[:-len(suffix)], set()).add(kind)
    return {slug for slug, kinds in seen.items() if kinds >= {"v", "s"}}


# ---------------------------------------------------------------- per document
def copy_doc(s3, bucket: str, run_prefix: str, row: dict, target: str, run: str,
             pid: str) -> tuple[dict, int, int]:
    """Copy one document's artefacts into the gallery. -> (manifest entry, objects, bytes).

    Server-side copies: nothing is downloaded. `MetadataDirective="REPLACE"` because the
    worker uploaded these with `aws s3 sync`, whose content-type guess for `scorecard.json`
    varies by CLI version — the gallery proxy sets its own header today so it does not
    matter, but it would the moment anything is presigned.
    """
    product, jur, doc_id = row["product"], row["jurisdiction"], row["doc_id"]
    slug = row["slug"]
    region = f"{jur} (MinerU)"          # LOAD-BEARING: see lib_gallery.gslug
    src_dir = f"{run_prefix}/{row['job']}"
    rels: dict[str, str] = {}
    objects = nbytes = 0
    for art in PM.PROMOTE_ARTEFACTS:
        if art not in row.get("artefacts", []):
            continue
        ext = ".json" if art.endswith(".json") else ".html"
        stem = art[:-len(ext)]
        rel = f"{product}/{region}/{slug}-{stem}{ext}"
        s3.copy_object(Bucket=bucket, Key=f"{target}/{rel}",
                       CopySource={"Bucket": bucket, "Key": f"{src_dir}/{art}"},
                       MetadataDirective="REPLACE", ContentType=CT[art],
                       Metadata={"aci-run": run, "aci-promotion": pid,
                                 "aci-doc-id": str(doc_id)})
        rels[art] = rel
        objects += 1
    nbytes = int(row.get("bytes") or 0)

    # scorecard_post_ai.json — OPTIONAL, and not one of PM.PROMOTE_ARTEFACTS: it exists only
    # on documents that ran stage 4/5, and a promotion must not require it to decide anything
    # (that stays scorecard.json's job, the --resume completion marker). `row["gate"]` and
    # `["worst_score"]` are already the FINAL verdict computed at plan time — see plan_run's
    # `final` rule — so copying this file alongside is what makes the published gallery's own
    # scorecard fetch (doc_gallery._scorecard_summary) read the SAME tree those numbers
    # describe, instead of silently falling back to the frozen stage-3 one.
    if row.get("has_post_ai"):
        art = "scorecard_post_ai.json"
        rel = f"{product}/{region}/{slug}-scorecard_post_ai.json"
        s3.copy_object(Bucket=bucket, Key=f"{target}/{rel}",
                       CopySource={"Bucket": bucket, "Key": f"{src_dir}/{art}"},
                       MetadataDirective="REPLACE", ContentType=CT[art],
                       Metadata={"aci-run": run, "aci-promotion": pid,
                                 "aci-doc-id": str(doc_id)})
        rels[art] = rel
        objects += 1

    entry = {
        "product": product, "region": region, "slug": slug, "doc_id": doc_id,
        # The S3 key of the source PDF. Today's publisher writes an absolute path on the
        # publishing MACHINE here, which means nothing to anyone else; nothing in the
        # gallery reads this field except push_hybrid_s3.backfill_inspect.
        "pdf": f"{src_dir}/source.pdf",
        "status": "ok",
        "viewer": rels.get("viewer.html"),
        # The FINAL scorecard -- post-AI when stage 4/5 ran, the frozen stage-3 one
        # otherwise. `gate`/`worst_score` below already describe whichever this is.
        "scorecard": rels.get("scorecard_post_ai.json") or rels.get("scorecard.json"),
        "gate": row.get("gate"), "worst_score": row.get("worst_score"),
        "stats": row.get("stats") or {},
        # Which extraction this row came from — unanswerable from a published gallery today.
        "source": {"run": run, "job": row["job"], "promotion": pid,
                   "promoted_at": time.time()},
    }
    if rels.get("inspect.html"):
        entry["inspect"] = rels["inspect.html"]
    for k in ("doc_name", "doc_version", "doc_date"):
        if row.get(k):
            entry[k] = row[k]

    # Rescue provenance. The mineru-full tier came off the scorecard at plan time; the
    # toc-outline tier lives in its own <1KB object, and the plan already recorded whether
    # this document has one — so this GET is paid only by the minority that do. toc-outline
    # wins when both apply: it is the more specific claim about what produced the structure.
    rescued = row.get("rescued")
    if row.get("has_toc_rescue"):
        try:
            payload = json.loads(s3.get_object(
                Bucket=bucket, Key=f"{src_dir}/rescued_by_toc.json")["Body"].read())
            rescued = rescue_from_toc(payload) or rescued
        except Exception:                                    # noqa: BLE001
            rescued = rescued or {"method": "toc-outline"}
    if rescued:
        entry["rescued"] = rescued
    return entry, objects, nbytes


# ---------------------------------------------------------------- the commit point
def merge_manifest(s3, bucket: str, target: str, entries: list[dict],
                   attempts: int = 3) -> list[dict]:
    """Read, UPSERT BY SLUG, write back — conditionally. This is the commit point.

    Four properties, each of which has gone wrong or would:

    1. THERE IS NO FULL-REPLACE BRANCH. `push_hybrid_s3.py`'s
       `[d for d in man if not d["slug"].endswith("-mineru")]` is what turned 455 published
       documents into 5 manifest entries. A promotion is partial by construction — a
       product allow-list and a gate filter — so that branch must be unreachable here under
       any flag, `--force` included.
    2. ONLY DOCUMENTS THAT WERE ACTUALLY COPIED GET ROWS. A row pointing at a key that is
       not there is a guaranteed 404, and `_doc_row`'s `has_viewer` exists because a
       reviewer cannot tell that apart from a broken viewer.
    3. THE WRITE IS CONDITIONAL. Minutes pass between the read and the write, so another
       writer can commit in the window; IfMatch turns silent data loss into a refusal.
    4. IT HAPPENS ONCE, AT THE END. Objects are invisible until the manifest names them, so
       a promotion that dies leaves the live gallery untouched.
    """
    key = f"{target}/manifest.json"
    mine = {e["slug"] for e in entries}
    for attempt in range(attempts):
        try:
            o = s3.get_object(Bucket=bucket, Key=key)
            man, etag = json.loads(o["Body"].read()), o["ETag"]
        except Exception as e:                               # noqa: BLE001
            if _err_code(e) not in ("NoSuchKey", "404", ""):
                raise
            man, etag = [], None
        if not isinstance(man, list):
            raise RuntimeError(f"{key} is not a JSON list — refusing to overwrite it")
        merged = [d for d in man if d.get("slug") not in mine] + entries
        cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            s3.put_object(Bucket=bucket, Key=key,
                          Body=json.dumps(merged, ensure_ascii=False).encode(),
                          ContentType="application/json", **cond)
            return merged
        except Exception as e:                               # noqa: BLE001
            if _err_code(e) not in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise
            log(f"⚠ manifest moved under us (attempt {attempt + 1}/{attempts}) — re-merging")
    raise RuntimeError("the manifest changed under us on every attempt — refusing to "
                       "write blind. The documents ARE copied; re-run to merge them in.")


# ---------------------------------------------------------------- gallery seed
def seed_gallery_from_latest(s3, bucket: str, target: str, latest: str | None,
                             exclude_slugs) -> list[dict]:
    """Read-only: which live gallery entries this promotion should carry forward.

    Phase 2's `seed` stage exists because an unseeded index build produces a `multi.npz`
    containing only the promoted regions, and flipping `index/latest` to it deletes every
    other jurisdiction from search. The gallery manifest has no equivalent by default: a
    promotion mints a fresh `doc-gallery/` prefix and writes rows only for what IT promotes,
    so a product-scoped run silently drops every other product from the Doc Gallery on
    cutover -- while search keeps answering for them, because that half IS seeded. Same
    failure shape, same fix, opt-in via --seed-gallery-from-latest because a normal
    every-product run doesn't need it and copying forward is not free.

    `exclude_slugs` is what THIS promotion's own `todo` is about to write fresh rows for --
    deliberately not a product list. A product can be in scope and still have some regions
    missing from `todo` (gated out this pass, or just absent from this run), and
    `promote_index.py`'s content stage only deletes the `.npz` of a region it actually
    rewrites -- so a region `todo` skips keeps its OLD content live in search either way. The
    gallery must match that, or dropping its entry here recreates the same inconsistency this
    feature exists to close, just pointed the other way.
    """
    if not latest:
        return []
    src = PM.promotion_target(latest)
    if src == target:
        return []
    try:
        raw = s3.get_object(Bucket=bucket, Key=f"{src}/manifest.json")["Body"].read()
    except Exception as e:                                       # noqa: BLE001
        if _err_code(e) in ("NoSuchKey", "404", ""):
            return []
        raise
    man = json.loads(raw)
    if not isinstance(man, list):
        raise RuntimeError(f"{src}/manifest.json is not a JSON list — refusing to seed from it")
    return [e for e in man if e.get("slug") not in exclude_slugs]


def copy_gallery_entry(s3, bucket: str, target: str, src: str, entry: dict) -> int:
    """Server-side copy one carried-forward entry's artefacts. -> objects copied.

    MetadataDirective=COPY, not REPLACE: this document did not come from the run being
    promoted, and its existing `aci-run`/`aci-promotion` metadata IS its true provenance --
    overwriting it would claim this promotion produced a document it only carried forward.
    """
    objects = 0
    for field in ("viewer", "scorecard", "inspect"):
        rel = entry.get(field)
        if not rel:
            continue
        s3.copy_object(Bucket=bucket, Key=f"{target}/{rel}",
                       CopySource={"Bucket": bucket, "Key": f"{src}/{rel}"},
                       MetadataDirective="COPY")
        objects += 1
    return objects


def verify_target(s3, bucket: str, target: str, entries: list[dict]) -> dict:
    """Re-list the target and confirm every row the manifest now names is really there.

    One listing. A promotion that reports `complete` while a row points at a missing object
    has published a 404 the reviewer cannot diagnose, so this runs before it says so.
    """
    present = present_slugs(s3, bucket, target)
    missing = sorted(e["slug"] for e in entries if e["slug"] not in present)
    return {"checked": len(entries), "present": len(entries) - len(missing),
            "missing": missing[:50], "missing_count": len(missing),
            "ok": not missing}


# ---------------------------------------------------------------- document names
def load_names(s3ro, src_prefix: str, product_dirs: list[str],
               fanout: int = 32) -> dict[str, dict]:
    """id -> Doc_metadata record, from the SOURCE corpus in S3.

    The gallery shows "Data Privacy Survey dated 30th September, 2025" where a run-mode row
    shows "172575", and the difference is this file. `lib_docnames` builds the same index
    from repo-local corpus roots, which do not exist in a pod — so here the same objects are
    fetched from `corpus-src/`.

    Delimiter-walked two levels rather than listed recursively: a recursive listing of the
    source area walks every source PDF to find a few hundred small JSON files. Best-effort
    throughout — a promotion must never fail over a display name.
    """
    from concurrent.futures import ThreadPoolExecutor
    from lib_docnames import index_from_payloads

    src = src_prefix.strip("/")
    jur_prefixes: list[str] = []
    for d in product_dirs:
        try:
            jur_prefixes += s3ro.list_common_prefixes(f"{src}/{d}/")
        except Exception as e:                               # noqa: BLE001
            log(f"⚠ names: cannot list {src}/{d}/ ({type(e).__name__})")

    def find(prefix: str) -> str | None:
        try:
            for o in s3ro.list_level_objects(prefix):
                if o["key"].endswith("/Doc_metadata.json"):
                    return o["key"]
        except Exception:                                    # noqa: BLE001
            pass
        return None

    with ThreadPoolExecutor(max_workers=max(1, fanout)) as ex:
        keys = [k for k in ex.map(find, jur_prefixes) if k]

    def fetch(key: str):
        try:
            return json.loads(s3ro.get_bytes(key))
        except Exception:                                    # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=max(1, fanout)) as ex:
        payloads = [p for p in ex.map(fetch, keys) if p is not None]
    names = index_from_payloads(payloads)
    log(f"names: {len(names)} document ids from {len(payloads)} Doc_metadata.json objects")
    return names


# ---------------------------------------------------------------- plan output
def print_plan(plan: dict, target: str, latest: str | None) -> None:
    e = PM.eligibility(plan)
    print()
    print(f"  run              {plan['run']}")
    print(f"  version          {plan['version']}")
    print(f"  target           s3://.../{target}/")
    print(f"  index/latest     {latest or '(unreadable)'}"
          + ("   <-- THIS IS LIVE" if latest and latest == plan["version"] else ""))
    print(f"  products         {', '.join(plan['products'])}")
    print(f"                   ({e['products_walked']} product prefixes walked)")
    print(f"  gates            {', '.join(plan['gates'])}")
    print()
    print(f"  PROMOTE          {e['eligible']} documents, "
          f"{e['objects']} objects, {e['bytes'] / 1e6:.1f} MB")
    for product, gates in sorted(e["by_product"].items()):
        bits = ", ".join(f"{n} {g}" for g, n in sorted(gates.items()))
        print(f"                   {product:45s} {bits}")
    if e["excluded"]:
        print()
        print("  held back")
        for reason, n in sorted(e["excluded"].items(), key=lambda kv: -kv[1]):
            print(f"                   {reason:28s} {n}")
            for job in (e["examples"].get(reason) or [])[:3]:
                print(f"                       e.g. {job}")
        if any(r.endswith("/no_viewer") for r in e["excluded"]):
            print("                   (no_viewer: run scripts/backfill_review_artefacts.py "
                  f"--run {plan['run']})")
    print()


def probe_write(s3, bucket: str, target: str) -> None:
    """PUT and GET one tiny object in the target before doing any work.

    `--plan` never calls PutObject, so a green dry run says NOTHING about whether this
    principal can write `index/*` — the runbook lists that as a known gap, and the
    extraction path learned it expensively when silently-swallowed 403s on
    `corpus/*/_retry/*` cost a day. This turns a 403 into two seconds instead of twenty
    minutes of copying followed by a failed manifest write.
    """
    key = f"{target}/_promotion_probe.json"
    body = json.dumps({"probe": True, "at": time.time(),
                       "host": socket.gethostname()}).encode()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert json.loads(got)["probe"] is True
    log(f"✓ write probe OK — this principal can write {target}/")


def read_latest(s3, bucket: str, pointer: str = "index/latest") -> str | None:
    """See promote_index._read_pointer_strict for why this refuses rather than returning
    None on a read failure. Imported from there so the two cannot drift apart."""
    import promote_index

    return promote_index._read_pointer_strict(s3, bucket, pointer, "")


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    """Env first, args second — the K8s Job then needs no arguments at all, which is
    `corpus_worker.py`'s convention and the reason its Job template never changes."""
    E = os.environ.get
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=E("PROMOTE_RUN"), help="the extraction run id")
    ap.add_argument("--version", default=E("PROMOTE_VERSION"),
                    help="index version to publish into (default: <run>-p1)")
    ap.add_argument("--bucket", default=E("ACI_EXTRACTION_BUCKET") or E("ACI_DOC_GALLERY_BUCKET"))
    ap.add_argument("--prefix", default=E("ACI_EXTRACTION_PREFIX") or "corpus")
    ap.add_argument("--src-prefix", default=E("ACI_CORPUS_SRC_PREFIX") or "corpus-src")
    ap.add_argument("--region", default=E("ACI_EXTRACTION_REGION") or E("AWS_REGION")
                    or "eu-west-1")
    ap.add_argument("--profile", default=None, help="AWS profile (local use; $AWS_PROFILE)")
    ap.add_argument("--gate", default=E("PROMOTE_GATES") or ",".join(PM.GATES_DEFAULT),
                    help="comma-separated scorecard gates to promote")
    ap.add_argument("--products", default=E("PROMOTE_PRODUCTS") or None,
                    help="comma-separated product labels (default: every indexable product)")
    ap.add_argument("--only", action="append", default=None, metavar="PRODUCT_DIR/LABEL",
                    help="promote exactly these job dirs (repeatable)")
    ap.add_argument("--exclude", action="append", default=None, metavar="PRODUCT_DIR/LABEL",
                    help="hold these job dirs back (repeatable). Applied in the PLAN, so "
                         "the document leaves the gallery and the index together, and the "
                         "index keeps whatever the seed carried over for that region.")
    ap.add_argument("--limit", type=int, default=None, help="promote at most N documents")
    ap.add_argument("--plan", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything except copy/put — proves the whole read path")
    ap.add_argument("--probe-write", action="store_true",
                    help="PUT+GET one object in the target first, to fail fast on IAM")
    ap.add_argument("--force", action="store_true", help="re-copy documents already present")
    ap.add_argument("--force-lease", action="store_true",
                    help="take over a lease whose heartbeat has gone stale")
    ap.add_argument("--merge-into-live", action="store_true",
                    help="allow the target to be the version index/latest points at")
    ap.add_argument("--seed-gallery-from-latest", action="store_true",
                    help="carry every index/latest doc-gallery entry this run is not about "
                         "to overwrite into the new version first, the way Phase 2's seed "
                         "stage already does for the search index. Without this, a "
                         "product-scoped promotion's Doc Gallery drops every other product "
                         "on cutover even though search still answers for them.")
    ap.add_argument("--progress-every", type=int, default=10, metavar="N",
                    help="log a copy progress line every N documents (0 = silent); a line "
                         "is also forced every 30s so a slow stretch still reports")
    ap.add_argument("--manifest-every", type=int, default=0, metavar="N",
                    help="commit the manifest every N documents instead of once at the end "
                         "(GIVES UP the all-or-nothing property; default 0 = once)")
    ap.add_argument("--no-names", action="store_true",
                    help="skip the Doc_metadata lookup (rows show numeric doc ids)")
    ap.add_argument("--from-request", action="store_true",
                    help="take the selection from _promotion/<version>/request.json")
    ap.add_argument("--by", default=E("PROMOTE_BY"), help="recorded in state.json")

    g = ap.add_argument_group(
        "index chain (--with-index)",
        "Takes the promoted documents on to a published index version and cuts over to it. "
        "Runs locally: it needs Bedrock for the embeddings and a reachable vector store.")
    g.add_argument("--with-index", action="store_true",
                   help="after the gallery, build+publish the index version, flip "
                        "index/latest, and reload the vector store")
    g.add_argument("--data-dir", default=E("PROMOTE_DATA_DIR"),
                   help="index artifacts dir to build into (default: data-<version>)")
    g.add_argument("--trees-dir", default=E("PROMOTE_TREES_DIR"),
                   help="scratch for the downloaded stage-3 markdown "
                        "(default: out/_promote_trees/<version>)")
    g.add_argument("--seed-from", default=None, metavar="DIR",
                   help="seed the data dir from a LOCAL dir instead of downloading "
                        "index/<latest>/products/ from S3 (~1GB). The S3 default is what "
                        "makes the new version a provable superset of what is live.")
    g.add_argument("--src-root-local", default="corpus-src", metavar="DIR",
                   help="local source tree holding each jurisdiction's Doc_metadata.json, "
                        "used to pick the keeper when two documents map to one region")
    g.add_argument("--set-latest", action="store_true", default=True,
                   help="flip index/latest to the new version (default)")
    g.add_argument("--no-set-latest", dest="set_latest", action="store_false",
                   help="publish and verify, but leave index/latest alone")
    g.add_argument("--reload-vectors", action="store_true", default=True,
                   help="clear the vector index and reload it (default)")
    g.add_argument("--no-reload-vectors", dest="reload_vectors", action="store_false")
    g.add_argument("--allow-content-failures", action="store_true",
                   help="publish even if some promoted documents failed to convert. Off by "
                        "default: the gallery would then offer documents the index cannot "
                        "answer about.")
    g.add_argument("--embed-backend", default=E("ACI_EMBED_BACKEND") or "titan")
    g.add_argument("--bedrock-region", default=E("ACI_BEDROCK_REGION") or _settings.bedrock_region)
    g.add_argument("--vector-backend", default=E("ACI_VECTOR_BACKEND") or "opensearch")
    g.add_argument("--vector-index", default=E("ACI_VECTOR_INDEX") or "aci-vectors")
    g.add_argument("--opensearch-url", default=E("ACI_OPENSEARCH_URL"))
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.bucket:
        log("✗ no bucket: pass --bucket or set ACI_EXTRACTION_BUCKET")
        return 2
    if not args.run or not valid_run(args.run):
        log(f"✗ not a valid run id: {args.run!r}")
        return 2

    version = args.version or PM.default_version(args.run)
    s3 = _client(args.region, args.profile)

    if args.from_request:
        req = None
        for cand in (version, PM.default_version(args.run)):
            key = PM.request_key(PM.run_prefix_for(args.prefix, args.run), cand)
            try:
                req = json.loads(s3.get_object(Bucket=args.bucket, Key=key)["Body"].read())
                version = req.get("version") or cand
                break
            except Exception:                                # noqa: BLE001
                continue
        if req is None:
            log(f"✗ no request.json for run {args.run}")
            return 2
        log(f"consuming request from {req.get('by')} at "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(req.get('requested_at') or 0))}")
        args.products = ",".join(req["products"]) if req.get("products") else args.products
        args.gate = ",".join(req["gates"]) if req.get("gates") else args.gate
        args.limit = req.get("limit") if req.get("limit") else args.limit
        args.merge_into_live = args.merge_into_live or bool(req.get("merge_into_live"))
        args.seed_gallery_from_latest = (args.seed_gallery_from_latest
                                         or bool(req.get("seed_gallery_from_latest")))
        args.by = args.by or req.get("by")

    if not PM.valid_version(version):
        log(f"✗ not a valid index version: {version!r} (lowercase, [a-z0-9._-], "
            f"<=49 chars — it has to be a legal OpenSearch index suffix in Phase 2)")
        return 2

    gates = tuple(g.strip() for g in args.gate.split(",") if g.strip())
    bad = [g for g in gates if g not in PM.GATES_ALL]
    if bad:
        log(f"✗ unknown gate(s): {', '.join(bad)} (known: {', '.join(PM.GATES_ALL)})")
        return 2
    products = tuple(p.strip() for p in args.products.split(",")
                     if p.strip()) if args.products else tuple(PRODUCTS)
    unknown = [p for p in products if p not in PRODUCTS]
    if unknown:
        log(f"✗ product(s) the index cannot represent: {', '.join(unknown)}. "
            f"A product outside region_map.PRODUCTS has no region identity, so its "
            f"documents cannot be searched — add it there first.")
        return 2

    run_prefix = PM.run_prefix_for(args.prefix, args.run)
    target = PM.promotion_target(version)
    if target == LEGACY_PREFIX:
        log("✗ refusing to promote into the legacy flat gallery prefix")
        return 2
    explicit = os.getenv("ACI_DOC_GALLERY_PREFIX", "").strip()
    if explicit and explicit != target:
        log(f"⚠ ACI_DOC_GALLERY_PREFIX={explicit!r} is set: a READER with that env will "
            f"not look at {target!r}. Unset it, or pin it to the target.")
    latest = read_latest(s3, args.bucket)
    if latest and latest == version and not args.merge_into_live:
        log(f"✗ {version} is what index/latest points at — promoting into it edits the LIVE "
            f"gallery in place. Mint a new version, or pass --merge-into-live.")
        return 2

    # ---- plan (reads only) ----
    from aosphere_core_index.aws.s3_readonly import ReadOnlyS3
    s3ro = ReadOnlyS3(bucket=args.bucket, region=args.region)
    t0 = time.time()
    names = {}
    if not args.no_names and not args.plan:
        names = load_names(s3ro, args.src_prefix,
                           _product_dirs(s3ro, run_prefix, products))
    plan = PM.plan_run(s3ro, args.run, run_prefix, version,
                       products=products, gates=gates, names=names,
                       exclude=args.exclude)
    log(f"planned {len(plan['docs'])} job dirs in {time.time() - t0:.1f}s")

    if args.only:
        want = set(args.only)
        for d in plan["docs"]:
            if d["job"] not in want and d["decision"] == "promote":
                d["decision"], d["reason"] = "skip", "not_in_only"
    todo = [d for d in plan["docs"] if d["decision"] == "promote"]
    if args.limit:
        todo = todo[:args.limit]

    # This is deliberately NOT `products` (the --products scan scope): a product can be in
    # scope and still have zero documents in `todo` -- no job dirs this run, or everything
    # gated out -- and its incumbent gallery entries must be carried forward exactly the same
    # as a product outside scope entirely. What matters is what THIS run is about to write
    # fresh rows for, i.e. `todo`, not what it was allowed to look at.
    carry: list[dict] = []
    if args.seed_gallery_from_latest:
        if not latest:
            log("gallery seed: index/latest is unset — nothing to carry forward")
        else:
            touched = {d["slug"] for d in todo}
            carry = seed_gallery_from_latest(s3, args.bucket, target, latest, touched)
            by_product: dict[str, int] = {}
            for e in carry:
                by_product[e.get("product", "?")] = by_product.get(e.get("product", "?"), 0) + 1
            log(f"gallery seed: {len(carry)} entries carried from {latest} "
                f"({', '.join(f'{v} {k}' for k, v in by_product.items()) or 'none'})")

    if args.plan:
        print_plan(plan, target, latest)
        return 0
    if not todo:
        print_plan(plan, target, latest)
        log("nothing to promote")
        return 0

    # ---- lease ----
    lease = Lease(s3, args.bucket, PM.lease_key(run_prefix, version))
    if not args.dry_run:
        ok, why = lease.acquire(force=args.force_lease)
        if not ok:
            log(f"✗ refusing to run: {why}")
            return 3
        if why:
            log(f"⚠ {why}")
    if args.probe_write and not args.dry_run:
        probe_write(s3, args.bucket, target)

    selection = {"products": list(products), "gates": list(gates),
                 **{k: v for k, v in PM.eligibility(plan).items()
                    if k in ("eligible", "excluded", "by_product", "objects", "bytes")}}
    prog = PromotionProgress(s3, args.bucket, args.run, run_prefix, version,
                             args.by, selection)
    try:
        return _run(args, s3, s3ro, prog, plan, todo, target, run_prefix, version, lease,
                    carry, latest)
    finally:
        lease.release()


def _index_chain(args, s3, s3ro, prog, promoted: list[dict], run_prefix: str,
                 version: str) -> int:
    """Hand off to the index half, on the same progress object.

    Imported here rather than at module scope so a gallery-only promotion — which is what
    runs in the cluster — never needs numpy, the CLI, or a Bedrock-capable environment.
    """
    import promote_index

    return promote_index.run_index_stages(prog, s3, s3ro, args, promoted, args.run,
                                          run_prefix, version)


def _product_dirs(s3ro, run_prefix: str, products) -> list[str]:
    """The run's product DIRECTORY names that map to an allowed product label.

    The directory carries the publisher's numeric id ("155_Data_Privacy") and the source
    corpus uses the same names, so this is also what tells `load_names` which `corpus-src/`
    prefixes to look under — without hardcoding a single directory name anywhere.
    """
    allowed = set(products)
    out = []
    for cp in s3ro.list_common_prefixes(f"{run_prefix.rstrip('/')}/"):
        name = cp.rstrip("/").rsplit("/", 1)[-1]
        if name and not name.startswith("_") and product_from_dir(name) in allowed:
            out.append(name)
    return out


def _run(args, s3, s3ro, prog: PromotionProgress, plan: dict, todo: list[dict],
         target: str, run_prefix: str, version: str, lease: Lease,
         carry: list[dict] | None = None, latest: str | None = None) -> int:
    prog.carry_forward()

    present = set() if args.dry_run else present_slugs(s3, args.bucket, target)

    # Carried-forward entries from OTHER products, so the gallery stays complete even though
    # this promotion only touches its own slice. See seed_gallery_from_latest's docstring —
    # this must land in the SAME manifest commit as the promoted entries below, both because
    # the manifest is meant to be written once and because a reviewer stopping the promotion
    # here should see a gallery that is missing new documents, not one missing old ones.
    #
    # Runs (as a no-op when `carry` is empty) whether or not --seed-gallery-from-latest was
    # passed: it is a phase-1 stage in STAGES, so summarize()'s "every phase-1 stage complete"
    # check needs a finished row here on EVERY promotion, the same way `select` always runs.
    entries: list[dict] = []
    carry = carry or []
    prog.stage("gallery_seed", total=len(carry),
              note=f"{len(carry)} entries carried from {latest}" if carry else None)
    done = 0
    if carry:
        src = PM.promotion_target(latest)
        pending = [e for e in carry if e["slug"] not in present]
        entries.extend(e for e in carry if e["slug"] in present)
        done = len(entries)
        if pending and args.dry_run:
            entries.extend(pending)
            done = len(carry)
        elif pending:
            # copy_object round trips, not bytes — a laptop watching stdout saw nothing for
            # the whole stage and read it as a hang (this loop originally had no heartbeat at
            # all), and sequentially that is genuinely slow: ~450 entries x up to 3 objects
            # each. Fanned out the same way load_names() already fans out Doc_metadata GETs.
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _copy(e):
                copy_gallery_entry(s3, args.bucket, target, src, e)
                return e

            beat_n = max(0, int(getattr(args, "progress_every", 10) or 0))
            beat_s = 30.0
            t_started = last_beat = time.time()
            with ThreadPoolExecutor(max_workers=32) as ex:
                futures = [ex.submit(_copy, e) for e in pending]
                for fut in as_completed(futures):
                    try:
                        entries.append(fut.result())
                    except Exception as e:                       # noqa: BLE001
                        # One bad carried document must not sink the promotion, same as the
                        # main copy loop below -- it just does not get a manifest row.
                        log(f"⚠ gallery seed: one carried document failed to copy: {e}")
                    done += 1
                    now = time.time()
                    if beat_n and (done % beat_n == 0 or now - last_beat >= beat_s
                                  or done == len(carry)):
                        last_beat = now
                        prog.d["stages"]["gallery_seed"]["done"] = done
                        prog.publish()
                        lease.beat()
                        rate = done / (now - t_started) if now > t_started else 0.0
                        log(f"  gallery_seed {done}/{len(carry)} "
                           f"({done / len(carry) * 100:.0f}%)  {rate:.1f}/s")
        present |= {e["slug"] for e in carry}
    prog.finish("gallery_seed", done=done)

    prog.stage("select", total=1, note=f"{len(todo)} eligible of {len(plan['docs'])} jobs")
    if not args.dry_run:
        prog.write_plan(plan)
    prog.finish("select", done=1)

    if present:
        log(f"{len(present)} document(s) already published under this version")

    prog.stage("copy", total=len(todo))
    # Documents that ARE in the gallery for this version — copied on this pass, or already
    # there from an earlier one. The index must describe exactly this set, not `todo`: a
    # document the gallery offers and the index cannot answer about is worse than neither.
    in_gallery: list[dict] = []
    committed = 0
    # Terminal heartbeat. `prog.publish()` puts full progress in S3 after every document,
    # but a laptop watching stdout saw nothing at all for the whole copy and read it as a
    # hang. Cadence is whichever of N documents / T seconds comes first, so a slow stretch
    # still reports. --progress-every 0 silences it.
    beat_n = max(0, int(getattr(args, "progress_every", 10) or 0))
    beat_s = 30.0
    t_started = time.time()
    last_beat = t_started
    for i, row in enumerate(todo, 1):
        if prog.stopping():
            break
        if row["slug"] in present and not args.force:
            prog.skipped(row)
            in_gallery.append(row)
            prog.publish()
            continue
        t = time.time()
        try:
            if (row.get("bytes") or 0) > _COPY_MAX:
                # Never hit by a 3-7MB viewer, but the size came free with the listing and
                # a single copy_object caps at 5GB — so a future monster is a named
                # failure rather than an opaque one.
                raise RuntimeError(f"{row['bytes']} bytes exceeds the single-copy limit")
            if args.dry_run:
                entry, objects, nbytes = {"slug": row["slug"]}, 0, 0
            else:
                entry, objects, nbytes = copy_doc(s3, args.bucket, run_prefix, row,
                                                  target, args.run, version)
            entries.append(entry)
            in_gallery.append(row)
            prog.copied(row, objects, nbytes, time.time() - t)
        except Exception as e:                               # noqa: BLE001
            # One bad document must not sink the promotion. It is counted, written to the
            # bounded failures file, and the loop continues; the exit code reports it.
            prog.doc_failed(row, e)
        prog.publish()
        lease.beat()
        now = time.time()
        if beat_n and (i % beat_n == 0 or now - last_beat >= beat_s or i == len(todo)):
            last_beat = now
            el = now - t_started
            c = prog.d["counts"]
            rate = i / el if el > 0 else 0.0
            eta = (len(todo) - i) / rate / 60 if rate > 0 else 0.0
            log(f"  copy {i}/{len(todo)} ({i / len(todo) * 100:.0f}%)  "
                f"copied={c.get('copied', 0)} skipped={c.get('skipped', 0)} "
                f"failed={c.get('failed', 0)}  {c.get('bytes', 0) / 1e9:.1f}GB  "
                f"{rate:.2f}/s  eta {eta:.1f}m")
        if args.manifest_every and len(entries) - committed >= args.manifest_every:
            if not args.dry_run:
                merge_manifest(s3, args.bucket, target, entries[committed:])
            committed = len(entries)
            log(f"  … manifest checkpoint at {committed} documents")
    drained = prog.stopping()
    if drained:
        # A DRAINED stage is not a finished one. Marking it complete would let a promotion
        # that was stopped half-way read `complete` on the screen with the copy stage green
        # and 200 of 612 documents — the same class of lie as a dead worker reading
        # `running`. It keeps its counters, gains a note, and the derived state says
        # `stopping`, so the operator can see it stopped early and re-run to finish.
        prog.d["stages"]["copy"]["note"] = (
            f"stopped early: {prog.d['stages']['copy'].get('done', 0)} of "
            f"{prog.d['stages']['copy'].get('total', 0)} documents")
        prog.publish(force=True)
        log("drained on request — the documents copied so far will still be committed")
    else:
        prog.finish("copy", done=prog.d["stages"]["copy"].get("done", 0))

    c = prog.d["counts"]
    log(f"copied {c['copied']}, skipped {c['skipped']}, failed {c['failed']} "
        f"({c['objects']} objects, {c['bytes'] / 1e6:.1f} MB)"
        + (f"  [this pass: {prog.pass_failed} failed]" if prog.pass_failed else ""))

    # ---- the commit point ----
    pending = entries[committed:]
    prog.stage("manifest", total=1)
    if args.dry_run:
        log("[dry-run] manifest NOT written")
        prog.finish("manifest", done=1, note="dry run")
    elif not pending and not entries:
        prog.finish("manifest", done=1, note="nothing copied — manifest untouched")
    else:
        try:
            merged = merge_manifest(s3, args.bucket, target, pending)
            prog.d["manifest"] = {"entries_after": len(merged),
                                  "entries_written": len(pending),
                                  "written_at": time.time()}
            prog.finish("manifest", done=1,
                        note=f"{len(pending)} upserted, {len(merged)} total")
            log(f"manifest committed: {len(merged)} documents under {target}/")
        except Exception as e:                               # noqa: BLE001
            prog.fail("manifest", f"{type(e).__name__}: {e}")
            log(f"✗ manifest NOT committed: {e}")
            return 1

    # ---- verify ----
    prog.stage("verify", total=1)
    if args.dry_run:
        prog.finish("verify", done=1, note="dry run")
    else:
        v = verify_target(s3, args.bucket, target, entries)
        if not v["ok"]:
            prog.fail("verify", f"{v['missing_count']} promoted slug(s) are not in the "
                                f"target: {', '.join(v['missing'][:5])}")
            log(f"✗ verify: {v['missing_count']} missing")
            return 1
        prog.finish("verify", done=1, note=f"{v['present']}/{v['checked']} present")
        # Provenance on the target side: which extraction this gallery version came from.
        prog._put(PM.provenance_key(version), json.dumps({
            "run": args.run, "run_prefix": run_prefix, "version": version,
            "promotion": version, "promoted_at": time.time(),
            "by": args.by or "unknown", "products": list(plan["products"]),
            "gates": list(plan["gates"]), "documents": c["copied"] + c["skipped"],
        }, indent=2).encode())

    prog.publish(force=True)
    if drained:
        log(f"◐ promotion {version} STOPPED EARLY — what was copied is committed and "
            f"browsable; re-run the same --version to finish the rest.")
        return 0
    log(f"✓ gallery for {version} complete — browse it at "
        f"/api/doc-gallery/tree?version={version}")
    if prog.pass_failed:
        log(f"  {prog.pass_failed} document(s) failed in this pass — re-run the same "
            f"--version to retry only those.")

    if args.with_index:
        if prog.pass_failed and not args.allow_content_failures:
            # Building an index from a gallery that is missing documents means the two
            # describe different corpora. Refuse before an hour of embedding, not after.
            log(f"✗ not starting the index chain: {prog.pass_failed} document(s) failed to "
                f"reach the gallery. Re-run to retry them, or pass "
                f"--allow-content-failures to proceed without them.")
            return 1
        if args.dry_run:
            log("[dry-run] index chain NOT run")
            return 0
        log("")
        return _index_chain(args, s3, s3ro, prog, in_gallery, run_prefix, version)

    log("  NOT live: index/latest is untouched and no vectors have been built — nothing "
        "here is searchable yet. Add --with-index to take it all the way.")
    return 1 if prog.pass_failed else 0


if __name__ == "__main__":
    sys.exit(main())
