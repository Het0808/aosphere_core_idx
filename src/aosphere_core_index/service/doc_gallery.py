"""Doc Gallery — browse the Stage-1 extracted opinion memos and open the per-doc
md ⇄ PDF viewer, all behind the Core Index's existing auth.

Design (see memory doc-gallery-architecture): the browser NEVER touches S3. A
``DocStore`` reads the extraction ``manifest.json`` and the self-contained viewer
HTML from either the local ``out/extractions/`` dir (dev) or a PRIVATE S3 bucket
(prod), and the API PROXY-STREAMS those bytes through the app. Swap dev↔prod purely
by config — no route/UI change:

    (default)                      -> LocalDocStore(out/extractions)
    ACI_DOC_GALLERY_BUCKET set     -> S3DocStore(bucket, prefix)   [private, EU]

Endpoints (all require the same access as /api/search):
    GET /api/doc-gallery            -> {count, docs:[{slug, product, region, ...}]}
    GET /api/doc-gallery/tree       -> the extraction scorecard: product / jurisdiction
                                       / document roll-up (same shape the extraction
                                       dashboard's /api/corpus serves)
    GET /api/doc-gallery/{slug}/view -> the doc's self-contained viewer HTML
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aosphere_core_index.config import settings
from aosphere_core_index.service import local_extraction as _lx

log = logging.getLogger(__name__)


# ---------------- storage backends ----------------
class DocStore:
    """Reads the extraction manifest + viewer HTML. Bytes never leave the backend
    except as the proxied HTTP response."""

    def manifest(self) -> list[dict]:
        raise NotImplementedError

    def viewer_bytes(self, viewer_rel: str) -> bytes | None:
        raise NotImplementedError

    def stamp(self, rel: str) -> str | None:
        """Cheap version marker for a stored object, used as a cache key so a
        re-published file is picked up without a restart. None = "assume immutable"."""
        return None

    @property
    def cache_scope(self) -> str:
        """Namespace for cached derivations of this store's objects.

        Every store addresses its objects by the SAME relative paths, so the scorecard
        summary cache needs to know which store a path was read from — otherwise asking
        for run A's `155_Data_Privacy/Spain__172575/scorecard.json` would be served run
        B's cached summary of the identical path."""
        return "default"


class LocalDocStore(DocStore):
    """Dev: reads out/extractions/ (manifest.json + <product>/<region>/<slug>-viewer.html)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def cache_scope(self) -> str:
        return f"local:{self.root}"

    def manifest(self) -> list[dict]:
        f = self.root / "manifest.json"
        if not f.is_file():
            return []
        return json.loads(f.read_text(encoding="utf-8"))

    def viewer_bytes(self, viewer_rel: str) -> bytes | None:
        # viewer_rel comes from the trusted manifest, but resolve + confine to root anyway
        p = (self.root / viewer_rel).resolve()
        if os.path.commonpath([p, self.root]) != str(self.root) or not p.is_file():
            return None
        return p.read_bytes()

    def stamp(self, rel: str) -> str | None:
        # mtime: re-running the publisher over a doc must invalidate its cached
        # scorecard summary, which is the normal dev loop against out/extractions.
        try:
            return str((self.root / rel).stat().st_mtime_ns)
        except OSError:
            return None


class S3DocStore(DocStore):
    """Prod: reads a PRIVATE bucket via the read-only boto3 client (IAM role)."""

    def __init__(self, bucket: str, prefix: str, region: str) -> None:
        from aosphere_core_index.aws.s3_readonly import ReadOnlyS3

        self.s3 = ReadOnlyS3(bucket=bucket, region=region)
        self.prefix = prefix.strip("/")
        self._man: list[dict] | None = None
        self._read_at = 0.0

    @property
    def cache_scope(self) -> str:
        return f"s3:{self.s3.bucket}/{self.prefix}"

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def manifest(self) -> list[dict]:
        # CACHED, on the same TTL as a run's synthesised manifest. This object grew with the
        # corpus: at ~700 bytes an entry a 986-document version is ~700KB, and it was re-GET
        # on every call — so one gallery page load, which asks for the manifest for the list,
        # the tree roll-up and again per row, paid for it several times over. A manifest a few
        # minutes stale can only under-report a publish that just landed; it cannot describe
        # a document that is not there, because the publisher writes the manifest LAST.
        now = time.monotonic()
        if self._man is not None and now - self._read_at < MANIFEST_TTL_S:
            return self._man
        try:
            self._man = json.loads(self.s3.get_bytes(self._key("manifest.json")))
        except Exception as e:  # missing manifest / access error -> empty gallery
            log.warning("doc-gallery: S3 manifest read failed: %s", e)
            # Keep serving the previous manifest rather than emptying the screen on one
            # transient read error; only a never-read store degrades to empty.
            self._man = self._man or []
        self._read_at = now
        return self._man

    def viewer_bytes(self, viewer_rel: str) -> bytes | None:
        try:
            return self.s3.get_bytes(self._key(viewer_rel))
        except Exception as e:
            log.warning("doc-gallery: S3 viewer read failed (%s): %s", viewer_rel, e)
            return None


# ---------------- an extraction RUN, read in place ----------------
# The published gallery reads a manifest.json the publisher writes. A RUN has no manifest —
# publishing is a separate step against a finished corpus — so this store SYNTHESISES one from
# what the run actually contains. That is the whole point of it: the run is what you want to
# look at BEFORE deciding whether to publish it, and until now the only way to see a run's
# output was one document at a time through the monitor's per-row links.
#
# Layout written by scripts/corpus_worker.py:
#     <root>/<run>/<product>/<jurisdiction>__<doc_id>/{scorecard.json,viewer.html,inspect.html}
# plus the extraction tree, the source PDF and the intermediate stages in the same job dir, and
# `_`-prefixed run-level directories (_progress, _failures, _permanent) that are not documents.
#
# COST is the whole design constraint here, and it is worth writing down what was measured on
# run 2026-08-21-02 (986 finished documents) because the obvious implementation is unusable:
#
#   recursive listing of the run, fanned out per product : 380,609 keys, 64.4s   REJECTED
#   delimiter walk to the job directories                :     986 prefixes, 0.9s
#   one-level listing of each job dir + one scorecard GET : 19.5s at fan-out 64
#                                                           29.1s at fan-out 32
#   the scorecard table, re-fetching each scorecard       : 303s                  REJECTED
#   the scorecard table, from summaries kept by the build : 0.02s
#
# A job dir holds three artefacts the gallery wants and several hundred it does not, so NEVER
# list a run recursively — walk to the job dirs by delimiter and read one level of each.
#
# The build parses every scorecard once and keeps only summarise_scorecard's dozen numbers. It
# does NOT keep the objects: those averaged 97KB, 96MB for this run, in the same process as the
# vector matrix — and "the gallery loaded every scorecard" is what took the service down once.
#
# ~20s on a cold read of a finished run is the price, and the assembled manifest is then cached.
# A run only ever GROWS, so a manifest a few minutes stale under-reports what finished; it never
# describes something that is not there.
RUN_TTL_S = float(os.getenv("ACI_GALLERY_RUN_TTL", "300"))
# The published manifest is one object, but a big one (see S3DocStore.manifest). Shorter
# than the run TTL because a publish is the thing an operator is waiting to see land.
MANIFEST_TTL_S = float(os.getenv("ACI_GALLERY_MANIFEST_TTL", "60"))
_FANOUT = int(os.getenv("ACI_GALLERY_RUN_FANOUT", "64"))

# The per-document artefacts a run carries. Everything else in a job dir (the stage
# directories, source.pdf, the 500MB of intermediates) is never addressed by the gallery.
# scorecard_post_ai.json is OPTIONAL -- it exists only on documents that ran stage 4/5 --
# and is read in preference to scorecard.json when present; see _build's `final` below.
# summary_ai_rule.json marks the summary-AI product-rule route, which writes its OWN
# 04_stage4_ai/ as the primary extraction rather than a repair pass -- it never writes a
# stage4_report.json, by design, and _build must not read that absence as "stuck" (see
# ai_postprocess.stage4_section_health, which this mirrors for the same reason).
_JOB_ARTEFACTS = ("scorecard.json", "scorecard_post_ai.json", "viewer.html", "inspect.html",
                  "summary_ai_rule.json")

_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_run(run: str) -> bool:
    """A run id is a single S3 path segment. Rejecting `_`-prefixed names keeps the
    run-level bookkeeping directories (_progress, _failures) from being addressed as runs."""
    return bool(run) and not run.startswith("_") and bool(_RUN_RE.match(run))


def extraction_target() -> tuple[str, str, str]:
    """(bucket, root prefix, region) for extraction runs — the single reading of this config.

    Defaults to the Doc Gallery's own bucket because extraction output and the published
    corpus live together by design: these documents are all destined to be served."""
    bucket = (os.getenv("ACI_EXTRACTION_BUCKET", "").strip()
              or os.getenv("ACI_DOC_GALLERY_BUCKET", "").strip())
    root = os.getenv("ACI_EXTRACTION_PREFIX", "").strip() or "corpus"
    region = (os.getenv("ACI_EXTRACTION_REGION", "").strip()
              or os.getenv("ACI_DOC_GALLERY_REGION", "").strip()
              or settings.aws_region)
    return bucket, root, region


def _run_slug(product: str, label: str) -> str:
    """A URL-safe slug for one job dir.

    Jurisdiction names carry spaces, commas, parentheses and accents ("Canada (Alberta,
    British Columbia, Ontario, Quebec and Canadian federal law)", "Aruba, Curaçao and St.
    Maarten"), and the slug travels in a URL PATH segment. Collapse to a safe charset and let
    the caller de-duplicate: two different jurisdictions can collapse to the same slug."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", f"{product}__{label}")).strip("-")


def walk_run_jobs(s3, prefix: str, want: tuple[str, ...] = _JOB_ARTEFACTS,
                  product_filter=None, fanout: int | None = None
                  ) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Walk one run to its job directories. -> ({"<product_dir>/<label>": {artefact: size}},
    product prefixes).

    Extracted from RunDocStore._build so the gallery and a PROMOTION share ONE
    implementation of the never-list-recursively rule. Measured on run 2026-08-21-02
    (986 finished documents):

        recursive listing of the run, fanned out per product : 380,609 keys, 64.4s  REJECTED
        delimiter walk to the job directories                :     986 prefixes, 0.9s
        one-level listing of each job dir                    : 19.5s at fan-out 64

    `product_filter(dir_name) -> bool` is applied to the PRODUCT PREFIX, before any job
    directory under it is listed. That ordering is the point: a run carries ~36 product
    directories and a promotion wants 3, so filtering first is a 12x saving on the walk
    rather than a filter over results already paid for.

    `_`-prefixed product directories are always skipped: those are the run-level
    bookkeeping dirs (_progress, _failures, _retry, _promotion), not products.
    """
    pool = max(1, fanout if fanout is not None else _FANOUT)
    cut = len(prefix) + 1

    products = []
    for q in s3.list_common_prefixes(f"{prefix}/"):
        name = q.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith("_"):
            continue
        if product_filter is not None and not product_filter(name):
            continue
        products.append(q)

    with ThreadPoolExecutor(max_workers=pool) as ex:
        job_prefixes = [j for part in ex.map(s3.list_common_prefixes, products)
                        for j in part]

    def artefacts(job_prefix: str) -> tuple[str, dict[str, int]]:
        # One level only. A nested retry attempt is a common prefix here, not a key, so
        # its scorecard cannot be counted as a second document.
        keys, prefixes = s3.list_level_with_prefixes(job_prefix)
        found = {}
        for o in keys:
            name = o["key"][len(job_prefix):]
            if name in want:
                found[name] = o["size"]
        # Stage 4's own subdirectory — one level below the job dir, so the file listing
        # above cannot see it, but the SAME paginated call already returns it as a common
        # prefix, so noting its presence costs nothing extra. -1 is a sentinel (never a
        # real size): only membership is meaningful here, and PROMOTE_ARTEFACTS-filtered
        # byte sums elsewhere ignore any key that is not one of their own.
        if any(p.endswith("04_stage4_ai/") for p in prefixes):
            found["04_stage4_ai"] = -1
        return job_prefix[cut:].rstrip("/"), found

    with ThreadPoolExecutor(max_workers=pool) as ex:
        jobs = dict(ex.map(artefacts, job_prefixes))
    return jobs, products


class RunDocStore(DocStore):
    """Reads one extraction run in place, synthesising the manifest by listing it."""

    def __init__(self, bucket: str, root: str, run: str, region: str) -> None:
        from aosphere_core_index.aws.s3_readonly import ReadOnlyS3

        self.s3 = ReadOnlyS3(bucket=bucket, region=region)
        self.run = run
        self.prefix = f"{root.strip('/')}/{run}" if root.strip("/") else run
        self._man: list[dict] | None = None
        self._built_at = 0.0
        # "could not read the run" and "the run has finished nothing yet" both produce an
        # empty manifest and must not read the same on screen: the first is a problem to fix,
        # the second is a run that has just started.
        self.last_error: str | None = None
        # Nothing fetched is retained as bytes. The build parses each scorecard once, keeps
        # only the dozen numbers the table needs (see summarise_scorecard) and drops the rest:
        # retaining the raw objects measured 96MB for one 986-document run, in the same process
        # that holds the vector matrix, and loading every scorecard is what took the service
        # down once already. The full scorecard is re-fetched on demand — one GET, only when
        # somebody opens that one document.

    @property
    def cache_scope(self) -> str:
        return f"run:{self.s3.bucket}/{self.prefix}"

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}"

    def manifest(self) -> list[dict]:
        now = time.monotonic()
        if self._man is not None and now - self._built_at < RUN_TTL_S:
            return self._man
        try:
            self._man = self._build()
            self.last_error = None
        except Exception as e:                               # noqa: BLE001
            log.warning("doc-gallery: run %s manifest build failed: %s", self.run, e)
            # Keep serving the previous manifest rather than emptying the screen on one
            # transient listing error; only a never-built store degrades to empty.
            self._man = self._man or []
            self.last_error = f"{type(e).__name__}: {e}"
        self._built_at = now
        return self._man

    def _build(self) -> list[dict]:
        t0 = time.monotonic()
        pool = max(1, _FANOUT)
        jobs, products = walk_run_jobs(self.s3, self.prefix)
        t_list = time.monotonic() - t0

        # A document without a scorecard has not finished (or it failed); not a gallery row.
        scored = sorted(j for j, arts in jobs.items() if "scorecard.json" in arts)

        def read(job: str) -> tuple[str, bytes | None, bytes | None, bytes | None]:
            try:
                raw = self.s3.get_bytes(self._key(f"{job}/scorecard.json"))
            except Exception:                                # noqa: BLE001
                raw = None
            # Only asked for when the walk already saw it -- most documents never ran
            # stage 4/5, and this must not turn into a second GET per document.
            raw_post = None
            if "scorecard_post_ai.json" in jobs[job]:
                try:
                    raw_post = self.s3.get_bytes(self._key(f"{job}/scorecard_post_ai.json"))
                except Exception:                            # noqa: BLE001
                    raw_post = None
            # Same rule: only asked for when the walk's free prefix check (see
            # walk_run_jobs) saw the 04_stage4_ai/ directory at all. A run where stage 4
            # was run out-of-band per-document (scripts/stage4_run.py, not the corpus
            # worker) never wrote a ledger row for it either, so this is the only place
            # that gives Doc Gallery a TRUE answer instead of the ledger's partial one.
            #
            # EXCEPT the summary-AI product-rule route (summary_ai_rule.json present):
            # that route writes its OWN 04_stage4_ai/ as the primary extraction, not a
            # repair pass, and never writes a stage4_report.json by design -- asking for
            # one here would call every summary-AI document "stuck" for a report it was
            # never going to write. Same exemption ai_postprocess.stage4_section_health
            # applies for a local caller; this is that rule for one read from S3.
            raw_stage4 = None
            if "04_stage4_ai" in jobs[job] and "summary_ai_rule.json" not in jobs[job]:
                try:
                    raw_stage4 = self.s3.get_bytes(self._key(f"{job}/04_stage4_ai/stage4_report.json"))
                except Exception:                            # noqa: BLE001
                    raw_stage4 = None
            return job, raw, raw_post, raw_stage4

        from aosphere_core_index.extract.ai_postprocess import stage4_failed_sections_from_report

        cards: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=pool) as ex:
            for job, raw, raw_post, raw_stage4 in ex.map(read, scored):
                try:
                    sc = json.loads(raw) if raw is not None else {}
                except (ValueError, UnicodeDecodeError):
                    sc = {}
                sc_post = None
                if raw_post is not None:
                    try:
                        sc_post = json.loads(raw_post)
                    except (ValueError, UnicodeDecodeError):
                        sc_post = None
                # None: stage 4 never touched this document (most of them), OR it is the
                # summary-AI route, whose 04_stage4_ai/ IS the extraction and never carries
                # a report — not applicable, not "stuck" (see the exemption in `read` above).
                # "incomplete": the directory exists but no report was ever written —
                # started, crashed or killed mid-run, the same signal
                # ai_postprocess.stage4_section_health gives a local caller, computed here
                # from a LIST result instead of a filesystem listdir. Front matter is
                # excluded from failed_sections by stage4_failed_sections_from_report itself.
                stage4 = None
                if "04_stage4_ai" in jobs[job] and "summary_ai_rule.json" not in jobs[job]:
                    if raw_stage4 is None:
                        stage4 = {"status": "incomplete", "failed_sections": []}
                    else:
                        try:
                            failed = stage4_failed_sections_from_report(json.loads(raw_stage4))
                            stage4 = {"status": "failed" if failed else "ok",
                                     "failed_sections": failed}
                        except (ValueError, UnicodeDecodeError):
                            stage4 = {"status": "incomplete", "failed_sections": []}
                # THE FINAL VERDICT, not the extraction gate: scorecard.json is frozen at
                # stage 3 (the --resume marker), but when stage 4/5 ran, that tree is what
                # actually ships and a reviewer scanning this table should see how it reads
                # NOW -- the same rule extraction_monitor.job_detail and run_corpus's
                # _final_verdict already apply for the same question.
                final = sc_post if (sc_post and sc_post.get("gate") is not None) else sc
                rel = f"{job}/scorecard_post_ai.json" if final is sc_post else f"{job}/scorecard.json"
                # Prime the table's cache from the object we are already holding, then let it
                # go. Without this the table re-fetched all 986 scorecards one at a time —
                # 303 seconds, longer than the whole build.
                _sc_summary_cache[(self.cache_scope, rel, self.stamp(rel))] = \
                    summarise_scorecard(final)
                # gate and worst_score are all the MANIFEST needs; the rest is now summarised.
                # `rescued` stays tied to the base scorecard -- it names the Stage 1-3 tier
                # that produced the document's structure, a question stage 4 does not touch.
                cards[job] = {"gate": final.get("gate"), "worst_score": final.get("worst_score"),
                              "doc_name": final.get("doc_name"),
                              "doc_version": final.get("doc_version"),
                              "rescued": sc.get("rescued"), "scorecard_rel": rel,
                              "stage4": stage4}

        man, used = [], set()
        for job in scored:
            product, label = job.split("/", 1)
            jur, _, doc_id = label.rpartition("__")
            sc = cards.get(job) or {}
            slug = _run_slug(product, label) or job
            if slug in used:                                 # distinct docs, colliding slugs
                slug = f"{slug}-{len(used)}"
            used.add(slug)
            entry = {
                "product": product,
                "region": jur or label,
                "slug": slug,
                "doc_id": doc_id or label,
                "status": "ok",
                "scorecard": sc.get("scorecard_rel", f"{job}/scorecard.json"),
                "gate": sc.get("gate"),
                "worst_score": sc.get("worst_score"),
                "doc_name": sc.get("doc_name"),
                "doc_version": sc.get("doc_version"),
                "rescued": sc.get("rescued"),
                # None where Stage 4 never touched this document — most of them, it is
                # opt-in. {"status": "ok"|"failed"|"incomplete", "failed_sections": [...]}
                # otherwise, read fresh from this run's own stage4_report.json rather than
                # the ledger, which only knows about documents the corpus worker itself
                # ran stage 4 for — a document stage4_run.py touched directly has none.
                "stage4": sc.get("stage4"),
                # No `stats`: those come from the publisher's parse_report, which has not run
                # for a run. Every number the scorecard table shows is read from the scorecard
                # itself (see _scorecard_summary), so the rows are complete without them —
                # only the publish-time extras (snapshots, files written, word delta) are blank.
            }
            for art, field in (("viewer.html", "viewer"), ("inspect.html", "inspect")):
                if art in jobs[job]:
                    entry[field] = f"{job}/{art}"
            man.append(entry)
        log.info("doc-gallery: run %s manifest built — %d products, %d jobs, %d documents "
                 "(list %.1fs, scorecards %.1fs)", self.run, len(products), len(jobs), len(man),
                 t_list, time.monotonic() - t0 - t_list)
        return man

    def viewer_bytes(self, viewer_rel: str) -> bytes | None:
        try:
            return self.s3.get_bytes(self._key(viewer_rel))
        except Exception as e:                               # noqa: BLE001
            log.warning("doc-gallery: run read failed (%s): %s", viewer_rel, e)
            return None


class LocalRunDocStore(LocalDocStore):
    """RunDocStore's local twin: browses `scripts/run_corpus.py`'s own output tree
    (ACI_LOCAL_CORPUS_DIR) in place, the same way RunDocStore browses one S3 run in
    place — no manifest.json to publish first, no bucket, no AWS credentials.

    `viewer_bytes`/`stamp` are inherited unchanged from LocalDocStore: both stores read
    the same shape of relative path (`<product>/<label>/scorecard.json`, confined to
    root), so the confinement check and the mtime-based cache key need writing once.
    Only `manifest()` differs, because a local run has no manifest.json to read — the
    tree IS the manifest, discovered the same way the Extraction tab's job list is
    (local_extraction.discover_jobs), not a second filesystem walk of its own."""

    @property
    def cache_scope(self) -> str:
        return f"local-run:{self.root}"

    def manifest(self) -> list[dict]:
        man: list[dict] = []
        used: set[str] = set()
        for j in _lx.discover_jobs(self.root):
            if j["gate"] is None:
                continue                                     # not finished -- not a gallery row
            job = f"{j['product']}/{j['label']}"
            slug = _run_slug(j["product"], j["label"])
            if slug in used:
                slug = f"{slug}-{len(used)}"
            used.add(slug)
            entry = {
                "product": j["product"], "region": j["jurisdiction"] or j["label"],
                "slug": slug, "doc_id": j["doc_id"], "status": "ok",
                "scorecard": f"{job}/scorecard.json",
                "gate": j["gate"], "worst_score": j["worst_score"],
                "doc_name": None, "doc_version": None, "rescued": None, "stage4": None,
                # Both built lazily, on the FIRST open -- see viewer_bytes below -- so
                # they are offered whenever the inputs for them exist, not only once the
                # file has actually been written. inspect.html is the real payoff: the
                # same Scorecard / Document / MinerU Inspector / Validation /
                # Cross-check / Page Review dashboard the S3 flow freezes, reused
                # unchanged (scripts/build_inspect.py) rather than a second scorecard UI
                # built here -- openScorecardPage() already tries this endpoint first
                # and only falls back to the bare gate/worst_score summary if it 404s.
            }
            if _lx.viewer_inputs_available(self.root, j["product"], j["label"]):
                entry["viewer"] = f"{job}/viewer.html"
            if _lx.inspect_inputs_available(self.root, j["product"], j["label"]):
                entry["inspect"] = f"{job}/inspect.html"
            man.append(entry)
        return man

    def viewer_bytes(self, viewer_rel: str) -> bytes | None:
        # Built lazily and cached to disk on first open -- exactly where
        # corpus_worker.build_review_artefacts would have written it during extraction,
        # so a second open (or a restart) reads the same file straight off disk with no
        # rebuild. manifest() already only offered this rel when the inputs exist, but a
        # direct fetch of an out-of-date manifest entry must still degrade to None,
        # never a stack trace -- ensure_viewer/ensure_inspect are best-effort by design.
        parts = Path(viewer_rel).parts
        if len(parts) == 3 and parts[2] == "viewer.html":
            _lx.ensure_viewer(self.root, parts[0], parts[1])
        elif len(parts) == 3 and parts[2] == "inspect.html":
            _lx.ensure_inspect(self.root, parts[0], parts[1])
        return super().viewer_bytes(viewer_rel)


_store: DocStore | None = None
_run_stores: dict[str, DocStore] = {}
_version_stores: dict[str, DocStore] = {}


# The gallery belongs TO an index version, not beside it. A flat "doc-gallery/" prefix is a
# single mutable copy: flip the index pointer and the scorecards still describe the previous
# extraction, and rolling the index back leaves the gallery rolled forward. Nesting it under
# index/<version>/doc-gallery/ makes the pair move together — the viewers, scorecards and
# verdicts a reader sees are the ones for the content being searched.
#
# Resolution order: an explicit ACI_DOC_GALLERY_PREFIX always wins (a pinned version, or a
# one-off). Otherwise follow index/latest, the same pointer the data mount resolves. If that
# cannot be read, fall back to the legacy flat prefix rather than serving nothing.
_LEGACY_GALLERY_PREFIX = "doc-gallery"


def _gallery_prefix(bucket: str, region: str) -> str:
    explicit = os.getenv("ACI_DOC_GALLERY_PREFIX", "").strip()
    if explicit:
        return explicit
    pointer = os.getenv("ACI_INDEX_POINTER", "index/latest").strip()
    try:
        import boto3

        body = boto3.client("s3", region_name=region).get_object(
            Bucket=bucket, Key=pointer)["Body"].read().decode().strip()
        if body:
            log.info("doc-gallery: following %s -> %s", pointer, body)
            return f"index/{body}/doc-gallery"
    except Exception as e:  # noqa: BLE001 — an unreadable pointer must not take the tab down
        log.warning("doc-gallery: could not read %s (%s); using %r",
                    pointer, type(e).__name__, _LEGACY_GALLERY_PREFIX)
    return _LEGACY_GALLERY_PREFIX


def version_prefix(version: str) -> str:
    """The gallery prefix for ONE index version — the promotion's target.

    Kept beside `_gallery_prefix` deliberately: a promotion that writes where the gallery
    does not read fails silently, and a blank tab is the only symptom. This is the third
    resolver of the same string; `tests/test_gallery_prefix.py` pins all three together.
    """
    return f"index/{version}/doc-gallery"


def store(run: str | None = None, version: str | None = None) -> DocStore:
    """Pick the backend once from config: S3 if ACI_DOC_GALLERY_BUCKET is set, else local.

    ``run`` selects an extraction run instead of the published gallery. The default — no run —
    is the current published folder and is unchanged by this, which matters because that is
    what every existing caller and every deployed environment asks for.

    ``version`` selects a STAGED gallery version: `index/<version>/doc-gallery`, whether or
    not `index/latest` points at it. That is what makes a promotion reviewable — the whole
    point of publishing under the version and not flipping the pointer is that somebody can
    look at it first, and without this the only readable gallery is the live one.
    """
    if run and version:
        raise ValueError("ask for a run or a version, not both")
    if version:
        if not valid_run(version):
            raise ValueError(f"not a valid index version: {version!r}")
        if version not in _version_stores:
            bucket = os.getenv("ACI_DOC_GALLERY_BUCKET", "").strip()
            if not bucket:
                raise LookupError("staged gallery versions need S3 "
                                  "(set ACI_DOC_GALLERY_BUCKET)")
            region = os.getenv("ACI_DOC_GALLERY_REGION", "").strip() or settings.aws_region
            try:
                _version_stores[version] = S3DocStore(bucket, version_prefix(version), region)
            except Exception as e:                           # noqa: BLE001
                # Same reasoning as the run path below: building the client reads the AWS
                # environment and raises on a broken one, which is a CONFIGURATION answer,
                # not a bug in the gallery.
                raise LookupError(f"cannot open gallery version {version}: "
                                  f"{type(e).__name__}: {e}") from e
            log.info("doc-gallery: version store bucket=%s version=%s", bucket, version)
        return _version_stores[version]
    if run:
        if not valid_run(run):
            raise ValueError(f"not a valid run id: {run!r}")
        if run not in _run_stores:
            # Local wins over S3 whenever it is configured, the same priority the
            # Extraction tab's own routes use (see app.py's /api/extraction/* handlers) --
            # never inferred from `run` merely equalling "local", only from the env var
            # actually being set, so a real S3 run id can never collide with it.
            local_root = _lx.configured_root()
            if local_root is not None and run == _lx.LOCAL_RUN_ID:
                _run_stores[run] = LocalRunDocStore(local_root)
                log.info("doc-gallery: local run store root=%s", local_root)
                return _run_stores[run]
            bucket, root, region = extraction_target()
            if not bucket:
                raise LookupError("extraction runs are not configured on this environment "
                                  "(set ACI_EXTRACTION_BUCKET)")
            try:
                _run_stores[run] = RunDocStore(bucket, root, run, region)
            except Exception as e:                           # noqa: BLE001
                # Building the client reads the AWS environment (region, profile, credential
                # chain) and raises on a broken one. That is a CONFIGURATION answer — "this
                # screen cannot reach the run" — not a bug in the gallery, and the difference
                # matters because the manifest build below is already guarded: without this,
                # the one unguarded line in the run path is the one that fails on a laptop
                # with no credentials, and the tab shows a stack trace.
                raise LookupError(f"cannot open extraction run {run}: "
                                  f"{type(e).__name__}: {e}") from e
            log.info("doc-gallery: run store bucket=%s run=%s", bucket, run)
        return _run_stores[run]
    global _store
    if _store is None:
        bucket = os.getenv("ACI_DOC_GALLERY_BUCKET", "").strip()
        if bucket:
            region = os.getenv("ACI_DOC_GALLERY_REGION", "").strip() or settings.aws_region
            _store = S3DocStore(bucket, _gallery_prefix(bucket, region), region)
            log.info("doc-gallery: S3 store bucket=%s", bucket)
        else:
            root = Path(os.getenv("ACI_DOC_GALLERY_DIR", "out/extractions"))
            _store = LocalDocStore(root)
            log.info("doc-gallery: local store dir=%s", root)
    return _store


def _ok_docs(run: str | None = None, version: str | None = None) -> list[dict]:
    return [d for d in store(run, version).manifest()
            if d.get("status") in ("ok", "cached")]


def _snap_count(v) -> int:
    """pages_snapshotted may be a list, an int, or a stringified list "[3, 4, 5]"
    (parse_report keeps it as a string) — normalise to a count."""
    if isinstance(v, list):
        return len(v)
    if isinstance(v, int):
        return v
    s = str(v or "").strip("[] ")
    return s.count(",") + 1 if s else 0


# ---------------- payload builders (the HTTP layer lives in app.py) ----------------
def read_error(run: str | None = None, version: str | None = None) -> str | None:
    """Why a run's listing came up empty, when it did. None = nothing went wrong."""
    return getattr(store(run, version), "last_error", None)


def gallery_list(run: str | None = None, version: str | None = None) -> list[dict]:
    """Metadata list for the gallery grid (not the content)."""
    docs = []
    for d in _ok_docs(run, version):
        s = d.get("stats") or {}
        docs.append({
            "slug": d["slug"],
            "product": d.get("product"),
            "region": d.get("region"),
            "doc_id": d.get("doc_id"),
            "pages": s.get("pages"),
            "tables": s.get("tables_converted"),
            "snapshots": _snap_count(s.get("pages_snapshotted")),
            "headings_matched": s.get("headings_matched"),
            "headings_total": s.get("headings_total"),
            "files": s.get("files_written"),
            "word_delta": s.get("word_delta_signed", s.get("word_delta_pct")),
            # validation scorecard — computed once at publish time (push_hybrid_s3.py),
            # not re-run per gallery view. Absent on docs published before that wiring.
            "gate": d.get("gate"),
            "worst_score": d.get("worst_score"),
            "has_viewer": bool(d.get("viewer")),
            "has_scorecard": bool(d.get("scorecard")),
            "has_inspect": bool(d.get("inspect")),
            "rescued": d.get("rescued"),
            "doc_name": d.get("doc_name"),
            "doc_version": d.get("doc_version"),
            "stage4": d.get("stage4"),
        })
    docs.sort(key=lambda d: (d["product"] or "", d["region"] or ""))
    return docs


# ---------------- extraction scorecard tree ----------------
# The gallery's landing view is a scorecard table, not a card wall: one row per
# product folder, drilling down product -> jurisdiction -> document. Its numbers
# come from each doc's PUBLISHED scorecard.json, which the manifest only carries
# two fields of (gate, worst_score) — the rest (weakest dimension, table buckets,
# open findings) is read from the scorecard file itself and cached, because doing
# ~70 S3 GETs on every gallery load would make the landing page unusable.
_sc_summary_cache: dict[tuple[str, str, str | None], dict] = {}

_MINERU_SUFFIX = re.compile(r"\s*\(MinerU\)\s*$")


def _scorecard_summary(d: dict, st: DocStore) -> dict:
    """The handful of scorecard numbers the table shows, for one manifest entry."""
    rel = d.get("scorecard")
    if not rel:
        return {}
    key = (st.cache_scope, rel, st.stamp(rel))
    hit = _sc_summary_cache.get(key)
    if hit is not None:
        return hit
    raw = st.viewer_bytes(rel)
    try:
        sc = json.loads(raw) if raw is not None else {}
    except (ValueError, UnicodeDecodeError):
        sc = {}
    out = summarise_scorecard(sc)
    _sc_summary_cache[key] = out
    return out


def summarise_scorecard(sc: dict) -> dict:
    """Reduce one parsed scorecard to the numbers the table ranks on.

    Split out from the cache lookup so a store that has ALREADY parsed a run's scorecards can
    pre-compute these and hand them over, instead of the table fetching every object again.
    That matters for size as much as for time: the summary is a dozen scalars, where the
    scorecards it comes from averaged 97KB each — 96MB for one 986-document run, held in the
    same process as the vector matrix."""
    tables = sc.get("tables") or []
    dims = sc.get("dimensions") or {}
    pages, heads = sc.get("pages") or {}, sc.get("headings") or {}
    counts = pages.get("counts") or {}
    comp = ((dims.get("completeness") or {}).get("detail") or {})
    out = {
        "weakest": (dims.get(sc.get("weakest_dimension")) or {}).get("label")
                   or sc.get("weakest_dimension"),
        "tables": len(tables) or None,
        "tables_failed": sum(1 for t in tables if t.get("bucket") == "failed"),
        "findings": sc.get("active_finding_count"),
        "dismissed": sc.get("dismissed_count") or 0,
        # what the extraction actually produced — the manifest stats for a MinerU
        # publish are thin (pages + table counts), and these are the numbers a
        # reviewer scans a row for
        "pages": pages.get("total"),
        "pages_silent": counts.get("silent"),
        "pages_flagged": counts.get("flagged"),
        "headings_matched": heads.get("matched"),
        "headings_total": heads.get("total"),
        "coverage": round(comp["coverage_pct"], 1) if comp.get("coverage_pct") is not None else None,
        "sections": comp.get("files_scanned"),
        "sections_silent": comp.get("files_with_silent_gap"),
        # every scored dimension, so the row carries the report card itself
        "dims": [{"label": (v or {}).get("label") or k, "score": (v or {}).get("score"),
                  "critical": bool((v or {}).get("critical"))}
                 for k, v in dims.items()],
    }
    return out


def _doc_row(d: dict, st: DocStore) -> dict:
    """One document row: identity + the scorecard numbers the table ranks on."""
    s = d.get("stats") or {}
    sc = _scorecard_summary(d, st)
    region = d.get("region") or ""
    gate = d.get("gate")
    return {
        "slug": d["slug"],
        "product": d.get("product") or "(no product)",
        # MinerU variants publish as "<jurisdiction> (MinerU)" — group them under the
        # jurisdiction itself so a doc and its MinerU re-extraction sit side by side.
        "jurisdiction": _MINERU_SUFFIX.sub("", region) or "(root)",
        "region": region,
        "doc_id": d.get("doc_id"),
        "mineru": bool(_MINERU_SUFFIX.search(region)) or str(d["slug"]).endswith("-mineru"),
        # A doc published before scorecards were wired in has no gate at all. It is
        # NOT a failure — it is unscored, so it stays out of the pass/review/fail
        # counts rather than dragging a folder's verdict down on missing data.
        "scored": gate in ("pass", "review", "fail"),
        # A run carries documents with no viewer: build_review_artefacts is best-effort, and
        # 123 documents in run 2026-08-21-02 were extracted before it existed at all. The table
        # must not offer a link to a document it knows has none — that is a guaranteed 404, and
        # the reviewer cannot tell it apart from a broken viewer.
        "has_viewer": bool(d.get("viewer")),
        "has_scorecard": bool(d.get("scorecard")),
        "has_inspect": bool(d.get("inspect")),
        # structure recovered from the printed TOC rather than the PDF's bookmarks
        "rescued": d.get("rescued"),
        # the publisher's own name for the document (Doc_metadata DOCNAME); the numeric
        # id alone tells a reviewer nothing about which edition they are looking at
        "doc_name": d.get("doc_name"),
        "doc_version": d.get("doc_version"),
        "gate": gate,
        "worst_score": d.get("worst_score"),
        "weakest": sc.get("weakest"),
        "tables": (sc.get("tables") if sc.get("tables") is not None
                   else s.get("tables_total", s.get("tables_converted"))),
        "tables_failed": sc.get("tables_failed") if sc else s.get("tables_failed"),
        "findings": sc.get("findings"),
        "dismissed": sc.get("dismissed"),
        "dims": sc.get("dims") or [],
        # Extraction shape, shown on the row so a document can be judged without
        # opening anything. Scorecard first, manifest stats as the fallback: a MinerU
        # publish writes thin stats (pages + table counts) and everything else — page
        # health, heading match, word coverage — only exists in the scorecard.
        "pages": sc.get("pages") if sc.get("pages") is not None else s.get("pages"),
        "pages_silent": sc.get("pages_silent"),
        "pages_flagged": sc.get("pages_flagged"),
        "coverage": sc.get("coverage"),
        "sections": sc.get("sections"),
        "sections_silent": sc.get("sections_silent"),
        "snapshots": _snap_count(s.get("pages_snapshotted")),
        "headings_matched": (sc.get("headings_matched") if sc.get("headings_matched") is not None
                             else s.get("headings_matched")),
        "headings_total": (sc.get("headings_total") if sc.get("headings_total") is not None
                           else s.get("headings_total")),
        "files": s.get("files_written"),
        "word_delta": s.get("word_delta_signed", s.get("word_delta_pct")),
        "stage4": d.get("stage4"),
    }


def _roll_up(node: dict, children: list[dict]) -> None:
    """Give a level its own counts and scores from whatever sits under it.

    Used at BOTH the jurisdiction and the product level so the two agree by
    construction. The folder number is deliberately NOT just an average: a folder
    holding a 95 and a 50 averages to a comfortable-looking 72, hiding exactly the
    document you need to open. So every level reports the pass/review/fail COUNTS
    plus its WORST document alongside the mean."""
    for k in ("pass", "review", "fail", "total"):
        node[k] = sum(c.get(k, 0) for c in children)
    scored = [c["worst_score"] for c in children
              if isinstance(c.get("worst_score"), (int, float))]
    node["worst_score"] = min(scored) if scored else None
    # weighted by document count, so a jurisdiction holding three documents counts
    # three times in its product's mean rather than once
    means = [(c["mean_score"], c.get("scored", 0)) for c in children
             if isinstance(c.get("mean_score"), (int, float))]
    tot = sum(n for _, n in means)
    node["mean_score"] = round(sum(m * n for m, n in means) / tot, 1) if tot else None
    node["scored"] = tot
    node["verdict"] = _verdict(node)


def _verdict(node: dict) -> str:
    # A jurisdiction (or product) row otherwise inherits "review"/"fail" from ANY one
    # of its documents, however far above the pass bar its own worst and mean scores
    # sit — a single low-severity per-document finding (e.g. source_fidelity) can hold
    # a whole jurisdiction at "review" even when both aggregate numbers read well
    # into the 90s. Once the row's own WORST and MEAN both clear 90, show it as pass
    # regardless of that per-document count, since that is what the numbers next to
    # the verdict already say.
    worst, mean = node.get("worst_score"), node.get("mean_score")
    if (isinstance(worst, (int, float)) and worst > 90
            and isinstance(mean, (int, float)) and mean > 90):
        return "pass"
    return "fail" if node["fail"] else "review" if node["review"] else "pass"


_VERDICT_ORDER = {"fail": 0, "review": 1, "pass": 2}


def gallery_tree(variant: str = "all", run: str | None = None,
                 version: str | None = None) -> dict:
    """product -> jurisdiction -> document roll-up, worst-first at every level.

    ``variant`` ("all" | "original" | "mineru") filters BEFORE the roll-up, so the
    counts a filtered view shows are the counts of what it shows — the alternative,
    re-summing in the browser, is the same arithmetic written twice."""
    # Scored documents only. A doc published before scorecards were wired in has no
    # verdict, no dimensions and no findings — in a scorecard table it is a row of
    # dashes that pushes the documents you must actually look at off the screen. They
    # remain browsable in the card view.
    st = store(run, version)
    everything = [r for r in (_doc_row(d, st) for d in _ok_docs(run, version))
                  if r["scored"]]
    n_mineru = sum(1 for d in everything if d["mineru"])
    docs = [d for d in everything
            if variant == "all" or (d["mineru"] if variant == "mineru" else not d["mineru"])]
    folders: dict[str, dict] = {}
    for d in docs:
        f = folders.setdefault(d["product"], {"product": d["product"], "jurisdictions": {}})
        jr = f["jurisdictions"].setdefault(d["jurisdiction"], {
            "jurisdiction": d["jurisdiction"], "documents": [],
            "pass": 0, "review": 0, "fail": 0,
        })
        jr["documents"].append(d)
        jr[d["gate"]] += 1

    for f in folders.values():
        for jr in f["jurisdictions"].values():
            scored = [d["worst_score"] for d in jr["documents"]
                      if isinstance(d["worst_score"], (int, float))]
            jr["worst_score"] = min(scored) if scored else None
            jr["mean_score"] = round(sum(scored) / len(scored), 1) if scored else None
            jr["total"] = len(jr["documents"])
            jr["scored"] = len(scored)
            jr["verdict"] = _verdict(jr)
            jr["documents"].sort(key=lambda d: (d["worst_score"] is None, d["worst_score"],
                                                d["slug"]))
        f["jurisdictions"] = sorted(
            f["jurisdictions"].values(),
            key=lambda x: (x["worst_score"] is None,
                           x["worst_score"] if x["worst_score"] is not None else 999,
                           x["jurisdiction"]))
        _roll_up(f, f["jurisdictions"])
    ordered = sorted(folders.values(),
                     key=lambda f: (_VERDICT_ORDER[f["verdict"]],
                                    f["worst_score"] if f["worst_score"] is not None else 999,
                                    f["product"]))
    return {
        "variant": variant,
        "run": run,
        "error": getattr(st, "last_error", None),
        "folders": ordered,
        "totals": {
            "folders": len(ordered),
            "documents": sum(f["total"] for f in ordered),
            "pass": sum(f["pass"] for f in ordered),
            "review": sum(f["review"] for f in ordered),
            "fail": sum(f["fail"] for f in ordered),
            "snapshots": sum(d["snapshots"] or 0 for d in docs),
            # unfiltered, so the filter buttons can label themselves
            "variants": {"all": len(everything), "mineru": n_mineru,
                         "original": len(everything) - n_mineru},
        },
    }


def viewer_html(slug: str, run: str | None = None, version: str | None = None) -> bytes | None:
    """The self-contained viewer HTML bytes for one doc; None if unknown/missing.

    `.get("viewer")`, not `["viewer"]`: a run carries documents whose viewer failed to build
    (build_review_artefacts is best-effort, because a document with no viewer is still a
    successfully extracted document), and a missing viewer is a 404, not a 500."""
    d = next((x for x in _ok_docs(run, version) if x.get("slug") == slug), None)
    if d is None or not d.get("viewer"):
        return None
    return store(run, version).viewer_bytes(d["viewer"])


def inspect_html(slug: str, run: str | None = None, version: str | None = None) -> bytes | None:
    """The frozen extraction-dashboard page for one doc (Scorecard / Document / MinerU
    Inspector / Validation / Stage 4 · AI / Page Review), built at publish time by
    scripts/build_inspect.py. None for docs published before that wiring — the app
    falls back to rendering the scorecard JSON itself."""
    d = next((x for x in _ok_docs(run, version) if x.get("slug") == slug), None)
    if d is None or not d.get("inspect"):
        return None
    return store(run, version).viewer_bytes(d["inspect"])


def scorecard_json(slug: str, run: str | None = None, version: str | None = None) -> dict | None:
    """The published scorecard for one doc; None if unknown/missing/unparseable.
    Uses the same viewer_bytes() fetch as the HTML viewer — it is a generic
    manifest-relative-path byte fetch, not HTML-specific."""
    d = next((x for x in _ok_docs(run, version) if x.get("slug") == slug), None)
    if d is None or not d.get("scorecard"):
        return None
    raw = store(run, version).viewer_bytes(d["scorecard"])
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
