"""FastAPI cross-region search service over the GraphIndex.

Endpoints:
  GET /                       cross-region search UI
  GET /api/regions            regions currently in the flat index
  GET /api/search             ONE query, ALL regions -> region-tagged sections
  GET /api/section            full content + links for one (region, section)

Search is cross-region by design: results carry their region as metadata, so the
AI layer downstream can compare/synthesize across jurisdictions.
"""

from __future__ import annotations

import logging
import os

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from aosphere_core_index.service.auth import (
    entitled_jurisdictions,
    get_current_user,
    is_admin,
    issue_stream_token,
    public_config,
    require_access,
    require_admin,
)

log = logging.getLogger(__name__)

# Input bounds: queries are embedded + reranked (expensive), so cap them; the
# jurisdictions CSV is bounded so a crafted request can't inflate the scope loop.
_Q_MAX = 2000
_JURIS_CSV_MAX = 12000
_JURIS_COUNT_MAX = 300
_PRODUCT_CSV_MAX = 400
# Which scorecard gates a promotion ships by default. `fail` and `error` are held back and
# reported rather than published. A literal rather than an import because PromoteRequest
# needs it at class-definition time and this module keeps heavy imports inside handlers;
# tests/test_promotion_api.py asserts it equals promotion_monitor.GATES_DEFAULT.
_PROMOTE_GATES_DEFAULT = ("pass", "review")
_PRODUCT_COUNT_MAX = 20


def _parse_jurisdictions(csv: str | None) -> list[str] | None:
    """Selected jurisdiction NAMES (common across products, e.g. 'France'), or None."""
    if not csv:
        return None
    requested = [r.strip() for r in csv.split(",") if r.strip()]
    if len(requested) > _JURIS_COUNT_MAX:
        raise HTTPException(400, f"too many jurisdictions (max {_JURIS_COUNT_MAX})")
    return requested or None


def _parse_products(csv: str | None) -> list[str] | None:
    """Selected product NAMES (e.g. 'Data Privacy'), or None for all products."""
    if not csv:
        return None
    requested = [r.strip() for r in csv.split(",") if r.strip()]
    if len(requested) > _PRODUCT_COUNT_MAX:
        raise HTTPException(400, f"too many products (max {_PRODUCT_COUNT_MAX})")
    return requested or None
from aosphere_core_index.service.logging_setup import configure_logging

configure_logging()  # honor ACI_LOG_LEVEL however the app is started
from aosphere_core_index.service.registry import (
    _breadcrumb,
    embedder,
    get_bundle,
    get_multi,
    multi_with_matrix,
    search_all,
)
from aosphere_core_index.service.web import PAGE


def _scope(requested: list[str] | None, user: dict) -> list[str] | None:
    """Intersect requested (qualified) region ids with the user's entitlements (if any)."""
    allowed = entitled_jurisdictions(user)
    if allowed is None:
        return requested
    return [j for j in requested if j in allowed] if requested else allowed


def _scope_ids(products_csv: str | None, jurisdictions_csv: str | None,
               user: dict) -> list[str] | None:
    """Expand a common (products x jurisdiction-names) selection into the qualified
    region ids that actually exist in the index, then apply entitlements.

    Region + jurisdiction are common across products: the UI selects products and
    jurisdiction NAMES independently, and we form the cross-product of the two against
    the index. Empty selection on both axes -> no explicit filter (entitlements/all)."""
    from aosphere_core_index.regions.region_map import product_of, split_region

    prods = _parse_products(products_csv)
    names = _parse_jurisdictions(jurisdictions_csv)
    if not prods and not names:
        return _scope(None, user)  # unscoped: falls to entitlements (all, for admins)
    prod_set = set(prods) if prods else None
    name_set = set(names) if names else None
    scope = [
        ident for ident in get_multi().regions
        if (prod_set is None or product_of(ident) in prod_set)
        and (name_set is None or split_region(ident)[1] in name_set)
    ]
    return _scope(scope, user)


def _detected_jurisdictions(q: str, user: dict) -> list[dict]:
    """Jurisdictions explicitly named in the query text — by jurisdiction name,
    US-state name, or alias (city / regulator / colloquial, e.g. "UK", "CNIL").
    Collapsed to one entry per bare jurisdiction name (with its continent, for chip
    colouring), scoped to the caller's entitlements — NOT the active product/juris
    filter, so a named-but-unfiltered jurisdiction still surfaces as a chip to add.
    Lexical only (no embedding), so it's cheap to return with every search."""
    from aosphere_core_index.regions.aliases import named_jurisdictions
    from aosphere_core_index.regions.region_map import region_for, split_region

    ent = _scope(None, user)  # entitled region ids, or None = all
    idents = get_multi().regions if ent is None else ent
    by_name: dict[str, str] = {}
    for ident in named_jurisdictions(q, idents):
        by_name.setdefault(str(split_region(ident)[1]), str(region_for(ident)))
    return [{"name": n, "region": r} for n, r in sorted(by_name.items())]


app = FastAPI(title="aosphere GraphIndex cross-region search")


def _configure_cors(app: FastAPI) -> None:
    """Enable CORS when configured, so browser clients on another origin (e.g. the
    Scalar API-docs UI, or the ai-playground SPA in local dev) can call the API.

    Off by default: with neither env var set no middleware is added and behaviour is
    unchanged. Configure via:
      ACI_CORS_ALLOW_ORIGINS       CSV of exact origins, e.g.
                                   "http://localhost:3000,https://api-docs.dev1.aoslogin.net".
                                   "*" allows any origin (credentials are then disabled,
                                   as browsers forbid wildcard-origin + credentials).
      ACI_CORS_ALLOW_ORIGIN_REGEX  Optional regex alternative, e.g.
                                   "https://.*\\.aoslogin\\.net".
      ACI_CORS_ALLOW_CREDENTIALS   "0" to disable sending cookies/credentials (default "1").
    """
    origins = [o.strip() for o in os.getenv("ACI_CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
    origin_regex = os.getenv("ACI_CORS_ALLOW_ORIGIN_REGEX", "").strip() or None
    if not origins and not origin_regex:
        return  # CORS not configured -> no middleware, behaviour unchanged

    allow_credentials = (
        os.getenv("ACI_CORS_ALLOW_CREDENTIALS", "1").strip().lower() not in ("0", "false", "no")
    )
    # A wildcard origin with credentials is rejected by browsers; drop credentials so
    # the wildcard still works (the bearer token is set explicitly by the client and is
    # permitted via allow_headers, so it keeps working without credentialed CORS).
    if allow_credentials and origins == ["*"]:
        allow_credentials = False
        log.warning("CORS: '*' origin with credentials is not allowed; disabling credentials")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=origin_regex,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info(
        "CORS enabled (origins=%s regex=%s credentials=%s)",
        origins or "-", origin_regex or "-", allow_credentials,
    )


_configure_cors(app)

# Spotlight-style entity search (MongoDB Atlas; see scripts/sync_atlas_search.py).
# Registered unconditionally; the endpoint 503s if MONGODB_URI is unset.
from aosphere_core_index.service.entity_search import router as entity_search_router  # noqa: E402

app.include_router(entity_search_router)

# Doc Gallery: browse the extracted memos + proxy-stream their md⇄PDF viewers (logic in doc_gallery.py).
from aosphere_core_index.service.doc_gallery import (  # noqa: E402
    extraction_target as _dg_extraction_target,
    gallery_list as _dg_list,
    read_error as _dg_error,
    gallery_tree as _dg_tree,
    inspect_html as _dg_inspect,
    scorecard_json as _dg_scorecard,
    valid_run as _dg_valid_run,
    viewer_html as _dg_viewer,
)


def _dg(fn, *args):
    """Call a Doc Gallery function, turning "cannot reach this run" into a 503.

    Only LookupError is mapped: doc_gallery raises it for a run that is unconfigured or whose
    store cannot be constructed. Anything else stays a 500, because it would be a real bug and
    dressing it as unavailability would hide it."""
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


def _gallery_run(run: str | None) -> str | None:
    """Validate the optional ?run= that points the gallery at an extraction run.

    Absent (the default) means the published gallery — the current folder — so the tab
    behaves exactly as it did before for every existing caller. A run id becomes part of an
    S3 prefix, so it is validated here rather than trusted."""
    run = (run or "").strip()
    if not run:
        return None
    if not _dg_valid_run(run):
        raise HTTPException(status_code=400, detail="invalid run id")
    return run


def _gallery_version(version: str | None) -> str | None:
    """Validate the optional ?version= that points the gallery at a STAGED index version.

    A promotion publishes to `index/<version>/doc-gallery/` and deliberately does not flip
    `index/latest`, so without this the only readable gallery is the live one and there is
    nothing to review before cutting over. Same validation as a run id, for the same
    reason: it becomes part of an S3 prefix."""
    version = (version or "").strip()
    if not version:
        return None
    from aosphere_core_index.service.promotion_monitor import valid_version
    if not valid_version(version):
        raise HTTPException(status_code=400, detail="invalid index version")
    return version


def _promotion_selection(products: str | None, gates: str | None, admin: bool = False):
    """Validate a promotion's product and gate selection. -> (products|None, gates).

    SERVER POLICY, not a UI default. A product outside `region_map.PRODUCTS` has no
    product-qualified region identity, so its documents cannot be represented in the search
    index at all — publishing gallery rows for them would promise something Phase 2 cannot
    deliver. And `fail`/`error` are held back unless an environment explicitly opts in,
    because "publish the documents we know are broken" should take a deliberate act."""
    from aosphere_core_index.regions.region_map import PRODUCTS
    from aosphere_core_index.service.promotion_monitor import GATES_ALL

    want_products = None
    if products:
        want_products = tuple(p.strip() for p in products.split(",") if p.strip())
        if len(want_products) > _PRODUCT_COUNT_MAX:
            raise HTTPException(400, f"too many products (max {_PRODUCT_COUNT_MAX})")
        unknown = [p for p in want_products if p not in PRODUCTS]
        if unknown:
            raise HTTPException(400, f"product(s) the index cannot represent: "
                                     f"{', '.join(unknown)}")
    allowed_gates = (GATES_ALL if os.getenv("ACI_ADMIN_PROMOTE_ANY_GATE", "0") == "1"
                     else _PROMOTE_GATES_DEFAULT)
    want_gates = tuple(g.strip() for g in gates.split(",")
                       if g.strip()) if gates else _PROMOTE_GATES_DEFAULT
    bad = [g for g in want_gates if g not in allowed_gates]
    if bad:
        raise HTTPException(400, f"gate(s) not permitted here: {', '.join(bad)} "
                                 f"(allowed: {', '.join(allowed_gates)})")
    return want_products, want_gates


def _gallery_where(run: str | None, version: str | None) -> tuple[str | None, str | None]:
    r, v = _gallery_run(run), _gallery_version(version)
    if r and v:
        raise HTTPException(status_code=400, detail="ask for a run or a version, not both")
    return r, v


@app.get("/api/doc-gallery")
def doc_gallery_list(run: str | None = None, version: str | None = None,
                     user: dict = Depends(require_access)) -> JSONResponse:
    r, v = _gallery_where(run, version)
    docs = _dg(_dg_list, r, v)
    return JSONResponse({"count": len(docs), "docs": docs, "run": r, "version": v,
                         "error": _dg(_dg_error, r, v)})


@app.get("/api/doc-gallery/tree")
def doc_gallery_tree(variant: str = "all", run: str | None = None,
                     version: str | None = None,
                     user: dict = Depends(require_access)) -> JSONResponse:
    """The extraction scorecard the gallery lands on: product / jurisdiction /
    document, each level verdicted by its WEAKEST document.

    ``run`` reads an extraction run in place instead of the published gallery, which is how
    a run gets reviewed before anyone decides to publish it."""
    if variant not in ("all", "original", "mineru"):
        raise HTTPException(status_code=400, detail="variant must be all|original|mineru")
    r, v = _gallery_where(run, version)
    return JSONResponse(_dg(_dg_tree, variant, r, v))


@app.get("/api/extraction/runs")
def extraction_runs(user: dict = Depends(require_access)) -> JSONResponse:
    """Extraction runs that have published progress, newest first.

    Local filesystem monitoring wins when ACI_LOCAL_CORPUS_DIR is configured — it is never
    inferred from S3 also being unset, so an environment can have neither and correctly show
    "not configured" rather than guessing. See local_extraction.py's module docstring for why
    local mode does not try to reproduce the shard/ledger/retry-queue model below."""
    from aosphere_core_index.service import local_extraction as lx
    local_root = lx.configured_root()
    if local_root is not None:
        return JSONResponse({"runs": lx.list_runs(local_root), "configured": True, "local": True})
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        return JSONResponse({"runs": [], "configured": False})
    try:
        return JSONResponse({"runs": em.list_runs(bucket, root, region), "configured": True})
    except Exception as e:                                       # noqa: BLE001
        log.warning("extraction runs listing failed: %s", e)
        return JSONResponse({"runs": [], "configured": True, "error": str(e)[:200]})


@app.get("/api/extraction/progress")
def extraction_progress(run: str = Query(..., min_length=1),
                        user: dict = Depends(require_access)) -> JSONResponse:
    """Aggregated progress of one run, from the per-shard summaries the workers publish.

    Cheap by construction — one GET per shard, never a scorecard — so the screen can poll it
    while a 17-hour run is in flight without adding load to the service."""
    from aosphere_core_index.service import local_extraction as lx
    local_root = lx.configured_root()
    if local_root is not None:
        return JSONResponse(lx.progress(local_root))
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in run or run.startswith("."):
        raise HTTPException(status_code=400, detail="run must be a single prefix segment")
    try:
        return JSONResponse(em.progress(bucket, f"{root.rstrip('/')}/{run}", region))
    except Exception as e:                                       # noqa: BLE001
        log.warning("extraction progress failed for %s: %s", run, e)
        raise HTTPException(status_code=502, detail="could not read run progress") from e


@app.get("/api/extraction/jobs")
def extraction_jobs(run: str = Query(..., min_length=1),
                    user: dict = Depends(require_access)) -> JSONResponse:
    """Every document this run has finished, plus the ones in flight — the job list.

    Separate from /progress because it answers a different question and costs a different amount:
    progress is the run's headline and is polled continuously, this is the per-document detail and
    is read from the append-only ledger each worker keeps. Still one GET per shard, still never a
    scorecard — the rule that keeps this screen cheap is unchanged."""
    from aosphere_core_index.service import local_extraction as lx
    local_root = lx.configured_root()
    if local_root is not None:
        return JSONResponse(lx.jobs_view(local_root))
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in run or run.startswith("."):
        raise HTTPException(status_code=400, detail="run must be a single prefix segment")
    try:
        return JSONResponse(em.jobs(bucket, f"{root.rstrip('/')}/{run}", region))
    except Exception as e:                                       # noqa: BLE001
        log.warning("extraction job list failed for %s: %s", run, e)
        raise HTTPException(status_code=502, detail="could not read the job list") from e


@app.get("/api/extraction/job")
def extraction_job(run: str = Query(..., min_length=1),
                   product: str = Query(..., min_length=1),
                   label: str = Query(..., min_length=1),
                   user: dict = Depends(require_access)) -> JSONResponse:
    """The full scorecard detail for ONE document, read only when a reviewer opens its row.

    The one place this module reads a scorecard at all. In bulk that is what took the service
    down; for a single row it is one GET, and it is the only source for the whole fallback chain
    and the per-dimension scores. It works on runs that predate the ledger too, since a scored
    document has always had a scorecard."""
    from aosphere_core_index.service import local_extraction as lx
    local_root = lx.configured_root()
    if local_root is not None:
        detail = lx.job_detail(local_root, product, label)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such document under the local corpus root")
        return JSONResponse(detail)
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in run or run.startswith("."):
        raise HTTPException(status_code=400, detail="invalid run")
    try:
        detail = em.job_detail(bucket, f"{root.rstrip('/')}/{run}", region, product, label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if detail is None:
        raise HTTPException(status_code=404,
                            detail="no scorecard for this document — it may have crashed, or "
                                   "still be running")
    return JSONResponse(detail)


@app.get("/api/extraction/local-artifact")
def extraction_local_artifact(product: str = Query(..., min_length=1),
                              label: str = Query(..., min_length=1),
                              path: str = Query(..., min_length=1),
                              user: dict = Depends(require_access)) -> Response:
    """One structural extraction artifact, exactly as written on disk — local mode only.

    Local-only, and deliberately separate from /api/extraction/job: the job detail panel
    answers "how did this document score", this answers "show me the actual MinerU table",
    HTML rowspan/colspan and all, for the validator work this local corpus was set up for.
    `path` is checked against an allowlist of stage subdirectories before anything is
    opened — see local_extraction.read_artifact for the traversal guard."""
    from aosphere_core_index.service import local_extraction as lx
    local_root = lx.configured_root()
    if local_root is None:
        raise HTTPException(status_code=404, detail="local extraction mode is not configured")
    result = lx.read_artifact(local_root, product, label, path)
    if result is None:
        raise HTTPException(status_code=404, detail="no such artifact for this document")
    content, content_type = result
    return Response(content=content, media_type=content_type)


@app.get("/api/extraction/failures")
def extraction_failures(run: str = Query(..., min_length=1),
                        user: dict = Depends(require_access)) -> JSONResponse:
    """Failed documents with a classified cause — GPU out of memory, host OOM, missing toolchain,
    shared memory, model resolution, timeout.

    A crash and a bad score need different remedies, and the run distinguishes them nowhere a
    reviewer can see: a crashed document just has no scorecard. Note that those are already
    re-extracted by the next run of this prefix, since resume keys on the scorecard — so this is
    for triage, not for queueing."""
    from aosphere_core_index.service import local_extraction as lx
    if lx.configured_root() is not None:
        # A crashed local job just has no scorecard yet and no ledger to classify it from —
        # discover_jobs already shows it as "running" until it either scores or the run ends.
        # Nothing here to triage a local run does not already show inline in the job table.
        return JSONResponse({"run": lx.LOCAL_RUN_ID, "count": 0, "by_cause": {}, "failures": [],
                             "local": True})
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in run or run.startswith("."):
        raise HTTPException(status_code=400, detail="invalid run")
    try:
        fails = em.list_failures(bucket, f"{root.rstrip('/')}/{run}", region)
        return JSONResponse({"run": run, "count": len(fails),
                             "by_cause": em.failure_summary(fails), "failures": fails})
    except Exception as e:                                   # noqa: BLE001
        log.warning("failure listing failed for %s: %s", run, e)
        return JSONResponse({"run": run, "count": 0, "by_cause": {}, "failures": []})


class RetryRequest(BaseModel):
    run: str = Field(..., min_length=1)
    product: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    # ONE retry per document by default: something that fails for a reason the retry does not
    # change fails again, and an unbounded retry is a loop that pays for the same document on
    # every run of the prefix.
    force: bool = False


@app.post("/api/extraction/retry")
def extraction_retry(body: RetryRequest,
                     user: dict = Depends(require_access)) -> JSONResponse:
    """Mark one document for re-extraction on the next run of its prefix.

    Resume skips any document that has a scorecard, so a bad result is otherwise sticky: only a
    new prefix would redo it, which redoes everything. The marker overrides resume for that one
    document, and the worker clears it once it has re-extracted it.

    Deliberately NOT a delete of the scorecard: the bad result is the evidence of what went wrong.
    Gated on access rather than admin so reviewers can queue their own findings — it costs GPU
    time, so the marker records who asked."""
    from aosphere_core_index.service import local_extraction as lx
    if lx.configured_root() is not None:
        # There is no worker fleet locally to hand a marker to — re-extracting a document
        # means re-running scripts/run_corpus.py (--force, or after deleting its scorecard),
        # by hand, exactly as it was run the first time.
        return JSONResponse({"queued": False, "reason": "local mode has no retry queue — "
                             "re-run scripts/run_corpus.py to re-extract a document"},
                            status_code=409)
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in body.run or body.run.startswith("."):
        raise HTTPException(status_code=400, detail="invalid run")
    try:
        res = em.mark_retry(bucket, f"{root.rstrip('/')}/{body.run}", region,
                            body.product, body.label, by=user.get("email") or user.get("sub"),
                            force=body.force)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:                                   # noqa: BLE001
        log.warning("retry mark failed: %s", e)
        raise HTTPException(status_code=502, detail="could not queue this document") from e
    if not res.get("queued"):
        # Refused, not failed: the caller can repeat it with force if that is really wanted.
        log.info("extraction retry refused (%s): %s/%s in %s", res.get("reason"),
                 body.product, body.label, body.run)
        return JSONResponse(res, status_code=409)
    log.info("extraction retry queued: %s/%s in %s by %s (forced=%s)", body.product, body.label,
             body.run, user.get("email"), body.force)
    return JSONResponse(res)


@app.get("/api/extraction/retries")
def extraction_retries(run: str = Query(..., min_length=1),
                       user: dict = Depends(require_access)) -> JSONResponse:
    """Documents currently marked for re-extraction in this run."""
    from aosphere_core_index.service import local_extraction as lx
    if lx.configured_root() is not None:
        return JSONResponse({"run": lx.LOCAL_RUN_ID, "queued": [], "local": True})
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in run or run.startswith("."):
        raise HTTPException(status_code=400, detail="invalid run")
    try:
        return JSONResponse({"run": run, "queued": em.list_retries(
            bucket, f"{root.rstrip('/')}/{run}", region)})
    except Exception as e:                                   # noqa: BLE001
        log.warning("retry listing failed: %s", e)
        return JSONResponse({"run": run, "queued": []})


@app.get("/api/extraction/review-status")
def extraction_review_status(run: str = Query(..., min_length=1),
                             product: str = Query(..., min_length=1),
                             label: str = Query(..., min_length=1),
                             user: dict = Depends(require_access)) -> JSONResponse:
    """Whether this document has a viewer / inspection page, so the screen only offers real links."""
    from aosphere_core_index.service import local_extraction as lx
    if lx.configured_root() is not None:
        # run_corpus.py does not build a standalone viewer/inspect.html locally (those are
        # published alongside a GPU run for the Doc Gallery to proxy) -- the job detail panel
        # is the local equivalent, reached from the row itself.
        return JSONResponse({"viewer": False, "inspect": False})
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in run or run.startswith("."):
        raise HTTPException(status_code=400, detail="invalid run")
    try:
        return JSONResponse(em.review_available(bucket, f"{root.rstrip('/')}/{run}", region,
                                                product, label))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/extraction/review/{kind}")
def extraction_review(kind: str,
                      run: str = Query(..., min_length=1),
                      product: str = Query(..., min_length=1),
                      label: str = Query(..., min_length=1),
                      user: dict = Depends(require_access)) -> Response:
    """The viewer (or inspection page) a run wrote beside one extraction.

    Proxy-streamed so the bucket stays private — the same arrangement the Doc Library uses for
    published documents. This exists because a run writes to corpus/<run>/, which the Doc Library
    does not read: a document that has just finished is not published anywhere yet."""
    from aosphere_core_index.service import local_extraction as lx
    if lx.configured_root() is not None:
        raise HTTPException(status_code=404,
                            detail="local mode has no published viewer/inspect page — "
                                   "open the document row for its extraction detail instead")
    from aosphere_core_index.service import extraction_monitor as em
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503, detail="extraction monitoring is not configured")
    if "/" in run or run.startswith("."):
        raise HTTPException(status_code=400, detail="invalid run")
    try:
        html = em.review_html(bucket, f"{root.rstrip('/')}/{run}", region, product, label, kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if html is None:
        raise HTTPException(status_code=404,
                            detail=f"no {kind} for this document — the run may predate it")
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{label}-{kind}.html"'})


def _extraction_target() -> tuple[str, str, str]:
    """(bucket, root prefix, region) for extraction runs.

    Delegates to doc_gallery, which needs the same three values to open a run as a gallery.
    Two readings of the same environment is how they drift: the monitor would point at one
    bucket and the run gallery at another, from the same screen."""
    return _dg_extraction_target()


@app.get("/api/doc-gallery/{slug}/view")
def doc_gallery_view(slug: str, run: str | None = None, version: str | None = None,
                    user: dict = Depends(require_access)) -> Response:
    html = _dg(_dg_viewer, slug, *_gallery_where(run, version))
    if html is None:
        raise HTTPException(status_code=404, detail="document or viewer not found")
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{slug}.html"'})


@app.get("/api/doc-gallery/{slug}/inspect")
def doc_gallery_inspect(slug: str, run: str | None = None, version: str | None = None,
                        user: dict = Depends(require_access)) -> Response:
    """The frozen extraction-dashboard page for one doc — all six tabs, self-contained
    (same proxy-stream path as the viewer, so S3 stays private)."""
    html = _dg(_dg_inspect, slug, *_gallery_where(run, version))
    if html is None:
        raise HTTPException(status_code=404, detail="no inspection page published for this document")
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{slug}-inspect.html"'})


@app.get("/api/doc-gallery/{slug}/scorecard")
def doc_gallery_scorecard(slug: str, run: str | None = None, version: str | None = None,
                          user: dict = Depends(require_access)) -> JSONResponse:
    sc = _dg(_dg_scorecard, slug, *_gallery_where(run, version))
    if sc is None:
        raise HTTPException(status_code=404, detail="scorecard not found for this document")
    return JSONResponse(sc)


# ---------------------------------------------------------------- promotion
# Publishing a finished extraction run into the Doc Gallery, and (Phase 2) on into the search
# index. The reads are cheap and poll-friendly, exactly like /api/extraction/*: the promotion
# job publishes small durable objects to S3 and these hand them to the screen. Nothing here
# holds job state in this pod's memory — see promotion_monitor's docstring for what that cost.


def _promotion_target():
    """(bucket, root, region) — the same single reading of the config the run gallery uses."""
    bucket, root, region = _extraction_target()
    if not bucket:
        raise HTTPException(status_code=503,
                            detail="promotion is not configured on this environment "
                                   "(set ACI_EXTRACTION_BUCKET)")
    return bucket, root, region


def _promote_enabled() -> None:
    """Writes are opt-in per environment, and 404 when off — the ACI_ADMIN_REINDEX pattern."""
    if os.getenv("ACI_ADMIN_PROMOTE", "0") != "1":
        raise HTTPException(404, "promotion disabled (set ACI_ADMIN_PROMOTE=1 to enable)")


@app.get("/api/promotion/runs")
def promotion_runs(run: str | None = None,
                   user: dict = Depends(require_access)) -> JSONResponse:
    """Promotions, newest first — every run's, or one run's with ?run=."""
    from aosphere_core_index.service import promotion_monitor as pm
    bucket, root, region = _extraction_target()
    if not bucket:
        return JSONResponse({"promotions": [], "configured": False})
    r = _gallery_run(run)
    try:
        rows = (pm.promotions_for_run(bucket, root, region, r) if r
                else pm.list_promotions(bucket, root, region))
        return JSONResponse({"promotions": rows, "configured": True, "run": r,
                             "enabled": os.getenv("ACI_ADMIN_PROMOTE", "0") == "1"})
    except Exception as e:                                       # noqa: BLE001
        log.warning("promotion listing failed: %s", e)
        return JSONResponse({"promotions": [], "configured": True, "error": str(e)[:200]})


@app.get("/api/promotion/plan")
def promotion_plan(run: str = Query(..., min_length=1, max_length=64),
                   version: str | None = None,
                   products: str | None = Query(None, max_length=_PRODUCT_CSV_MAX),
                   gates: str | None = Query(None, max_length=64),
                   user: dict = Depends(require_access)) -> JSONResponse:
    """What a promotion of this run WOULD do — counts and refusal reasons, never rows.

    Computed live by the same `plan_run` the job executes, so the plan an operator approves
    is the plan that runs. It is a GET: it writes nothing, and it costs the same run walk
    the gallery already pays for a run (~20s cold on a finished one)."""
    from aosphere_core_index.service import promotion_monitor as pm
    bucket, root, region = _promotion_target()
    r = _gallery_run(run)
    v = _gallery_version(version)
    want_products, want_gates = _promotion_selection(products, gates)
    try:
        plan = pm.compute_plan(bucket, root, region, r, v,
                               products=want_products, gates=want_gates)
    except Exception as e:                                       # noqa: BLE001
        log.warning("promotion plan failed for %s: %s", r, e)
        raise HTTPException(status_code=502, detail="could not read this run") from e
    return JSONResponse(pm.eligibility(plan))


@app.get("/api/promotion/progress")
def promotion_progress(run: str = Query(..., min_length=1, max_length=64),
                       promotion: str = Query(..., min_length=1, max_length=64),
                       user: dict = Depends(require_access)) -> JSONResponse:
    """One promotion's stages, counts and derived state — ONE GET, safe to poll."""
    from aosphere_core_index.service import promotion_monitor as pm
    bucket, root, region = _promotion_target()
    r, pid = _gallery_run(run), _gallery_version(promotion)
    try:
        out = pm.progress(bucket, root, region, r, pid)
    except Exception as e:                                       # noqa: BLE001
        log.warning("promotion progress failed for %s/%s: %s", r, pid, e)
        raise HTTPException(status_code=502, detail="could not read this promotion") from e
    if out is None:
        raise HTTPException(status_code=404, detail="no such promotion")
    return JSONResponse(out)


@app.get("/api/promotion/plan-detail")
def promotion_plan_detail(run: str = Query(..., min_length=1, max_length=64),
                          promotion: str = Query(..., min_length=1, max_length=64),
                          offset: int = Query(0, ge=0),
                          limit: int = Query(500, ge=1, le=2000),
                          decision: str | None = Query(None, max_length=16),
                          user: dict = Depends(require_access)) -> JSONResponse:
    """A bounded slice of the plan the job recorded, for the per-document table."""
    from aosphere_core_index.service import promotion_monitor as pm
    bucket, root, region = _promotion_target()
    r, pid = _gallery_run(run), _gallery_version(promotion)
    try:
        return JSONResponse(pm.read_plan(bucket, root, region, r, pid, offset, limit,
                                         decision))
    except Exception as e:                                       # noqa: BLE001
        log.warning("promotion plan read failed for %s/%s: %s", r, pid, e)
        raise HTTPException(status_code=502, detail="could not read this plan") from e


@app.get("/api/promotion/failures")
def promotion_failures(run: str = Query(..., min_length=1, max_length=64),
                       promotion: str = Query(..., min_length=1, max_length=64),
                       user: dict = Depends(require_access)) -> JSONResponse:
    """Documents this promotion could not copy. Bounded by the writer, not by this."""
    from aosphere_core_index.service import promotion_monitor as pm
    bucket, root, region = _promotion_target()
    r, pid = _gallery_run(run), _gallery_version(promotion)
    try:
        rows = pm.read_failures(bucket, root, region, r, pid)
    except Exception as e:                                       # noqa: BLE001
        log.warning("promotion failures read failed for %s/%s: %s", r, pid, e)
        rows = []
    return JSONResponse({"run": r, "promotion": pid, "count": len(rows), "failures": rows})


class PromoteRequest(BaseModel):
    run: str = Field(..., min_length=1, max_length=64)
    # Defaults to <run>-p1. A second promotion of the same run mints -p2 rather than
    # editing the first one's prefix in place.
    version: str | None = None
    products: list[str] | None = None
    gates: list[str] = list(_PROMOTE_GATES_DEFAULT)
    limit: int | None = None
    merge_into_live: bool = False
    seed_gallery_from_latest: bool = False


@app.post("/api/promotion/request")
def promotion_request(body: PromoteRequest,
                      user: dict = Depends(require_admin)) -> JSONResponse:
    """Ask for a promotion. Records the request; does NOT start a job.

    The service has no batch/v1 RBAC and giving a public-facing pod the right to create
    Kubernetes Jobs is a separate security decision, so this writes the request the way
    /api/extraction/retry writes a retry marker, and a promoter picks it up. The UI says
    "requested", not "promoting", because that is what has happened.

    POST with a body rather than a query string: a run id plus a product list plus a gate
    list will exceed the WAF's ~2KB query-string cap."""
    _promote_enabled()
    from aosphere_core_index.service import promotion_monitor as pm
    bucket, root, region = _promotion_target()
    r = _gallery_run(body.run)
    v = _gallery_version(body.version) or pm.default_version(r)
    products, gates = _promotion_selection(
        ",".join(body.products) if body.products else None,
        ",".join(body.gates) if body.gates else None, admin=True)
    try:
        res = pm.request_promotion(bucket, root, region, r, v,
                                   by=user.get("email") or user.get("sub"),
                                   products=products, gates=gates, limit=body.limit,
                                   merge_into_live=body.merge_into_live,
                                   seed_gallery_from_latest=body.seed_gallery_from_latest)
    except Exception as e:                                       # noqa: BLE001
        log.warning("promotion request failed: %s", e)
        raise HTTPException(status_code=502, detail="could not record this request") from e
    if not res.get("queued"):
        # Refused, not failed — the reason is actionable and the caller can change it.
        log.info("promotion refused (%s): %s -> %s", res.get("reason"), r, v)
        return JSONResponse(res, status_code=409)
    log.info("promotion requested: run=%s version=%s by=%s", r, v, user.get("email"))
    return JSONResponse(res)


class PromotionStopRequest(BaseModel):
    run: str = Field(..., min_length=1, max_length=64)
    promotion: str = Field(..., min_length=1, max_length=64)


@app.post("/api/promotion/stop")
def promotion_stop(body: PromotionStopRequest,
                   user: dict = Depends(require_admin)) -> JSONResponse:
    """Ask a running promotion to drain: finish the document in flight, publish, exit."""
    _promote_enabled()
    from aosphere_core_index.service import promotion_monitor as pm
    bucket, root, region = _promotion_target()
    r, pid = _gallery_run(body.run), _gallery_version(body.promotion)
    try:
        res = pm.request_stop(bucket, root, region, r, pid,
                              by=user.get("email") or user.get("sub"))
    except Exception as e:                                       # noqa: BLE001
        log.warning("promotion stop failed: %s", e)
        raise HTTPException(status_code=502, detail="could not request a stop") from e
    return JSONResponse(res, status_code=200 if res.get("stopping") else 404)


@app.on_event("startup")
def _warm() -> None:
    """Load the flat index (needed for every query) and configure MLflow tracing.

    Per-region bundles lazy-load on first access by default: eagerly loading all
    ~233 content.json bundles at startup spikes memory (on top of the index) and can
    OOM a memory-limited pod before it's even ready. Set ACI_WARM_BUNDLES=1 to
    pre-load them (faster first query) where memory headroom allows."""
    try:
        from aosphere_core_index.llm.monitoring import setup_mlflow

        setup_mlflow()
    except Exception:
        log.exception("MLflow tracing setup failed; continuing without tracing")
    try:
        mi = get_multi()  # the flat index every query needs
        if os.getenv("ACI_WARM_BUNDLES", "0") == "1":
            for r in mi.regions:
                get_bundle(r)
    except Exception:  # don't block startup on a single bad region
        log.exception("index/bundle warm-up failed; regions will lazy-load on demand")

    # Warm an EXTERNAL vector backend so the first user query doesn't pay the one-time
    # connection cost (SRV DNS + TLS + replica-set discovery + pool) or the Atlas-side
    # $vectorSearch cold-load — the "slow first query" vs the always-warm in-memory matrix.
    # Runs per pod at startup; skipped for memory (already in RAM). Never blocks readiness.
    try:
        from aosphere_core_index.embeddings.vector_backend import backend, retrieve

        if backend() != "memory" and mi.n_rows:
            # Probe with a freshly embedded query — under an external backend neither the
            # matrix nor the per-row metadata is loaded (metadata-lean), so use mi.regions.
            qv = embedder().embed(["personal data breach notification"])[0]
            reg = str(mi.regions[0])
            retrieve(mi, qv, 5, jurisdictions=[reg])  # scoped: connection + filtered path
            retrieve(mi, qv, 5)                        # unscoped: ANN index paged in
            log.info("warmed vector backend '%s' (connection + query paths)", backend())
    except Exception:
        log.exception("vector-backend warm-up failed; first live query may be slow")


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness + index identity. `rows` (total index vectors, incl. chunk rows) is a
    fingerprint of the exact published index version, so an unauthenticated
    `curl /healthz` verifies which index a pod actually mounted after a deploy.
    `vector_backend`/`entity_backend` expose which store each search path serves from
    (e.g. confirm Spotlight is on `opensearch`, not the legacy `atlas`).

    `rows` is what the MOUNTED DATA expects; `store_rows` is what the search store actually
    holds, and `index_loaded` is whether they agree. They diverge exactly when a load
    truncated or never ran — dev served 11,000 of 77,939 after a rolling deploy killed the
    loader mid-flight, and neither /healthz, the reindex status (per-pod memory) nor the logs
    (the bulk helper swallows errors) said a word. One curl now shows it."""
    from aosphere_core_index.embeddings.vector_backend import backend, store_rows
    from aosphere_core_index.service.entity_backend import backend as entity_backend

    mi = get_multi()
    store = store_rows()
    return JSONResponse({"status": "ok", "regions": len(mi.regions),
                         "rows": mi.n_rows, "model": str(mi.model),
                         "vector_backend": backend(),
                         # None when the store cannot be asked (memory backend, or
                         # unreachable) — absence of an answer is not a failed index.
                         "store_rows": store,
                         "index_loaded": None if store is None else store == mi.n_rows,
                         "entity_backend": entity_backend()})


@app.post("/api/admin/reindex")
def admin_reindex(
    backend: str = Query("atlas", description="target vector store: atlas | opensearch"),
    batch: int = Query(2000, ge=100, le=10000),
    user: dict = Depends(require_admin),
) -> JSONResponse:
    """Load the in-memory index into an external vector store (Atlas / OpenSearch) from
    INSIDE the deployment, where the store is on the LAN — not over a developer VPN. Runs
    in a background thread; poll GET /api/admin/reindex for progress. Admin-only and gated
    by ACI_ADMIN_REINDEX=1 (it writes to the vector store, so it's opt-in per environment)."""
    if os.getenv("ACI_ADMIN_REINDEX", "0") != "1":
        raise HTTPException(404, "reindex disabled (set ACI_ADMIN_REINDEX=1 to enable)")
    from aosphere_core_index.embeddings import vector_load
    try:
        return JSONResponse(vector_load.start(multi_with_matrix(), backend, batch))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:  # a job is already running
        raise HTTPException(409, str(e))


@app.get("/api/admin/reindex")
def admin_reindex_status(user: dict = Depends(require_admin)) -> JSONResponse:
    """Progress of the current/last reindex job: phase, done/total, rate, elapsed, error."""
    from aosphere_core_index.embeddings import vector_load
    return JSONResponse(vector_load.status())


@app.post("/api/admin/entity-reindex")
def admin_entity_reindex(
    status: str = Query("1", description="MSSQL status filter: csv of values or 'all'"),
    org: str | None = Query(None, description="organisation id to scope to, or 'all' "
                                              "(default: ACI_SYNC_ORG or 101)"),
    ifChanged: bool = Query(False, description="skip when source tables are unchanged "
                                               "since the last run (cron-safe)"),
    verify: bool = Query(True, description="run sample searches after the sync"),
    user: dict = Depends(require_admin),
) -> JSONResponse:
    """Run the MSSQL -> OpenSearch entity (Spotlight) sync from INSIDE the deployment,
    where both the private RDS and the OpenSearch domain are reachable via the pod's
    IRSA role + VPC — no developer VPN or CronJob needed. Runs the validated
    scripts/sync_atlas_search.py as a background subprocess (zero-downtime alias-swap
    reindex of `aci-entities`); poll GET /api/admin/entity-reindex for progress.
    Admin-only and gated by ACI_ADMIN_ENTITY_REINDEX=1 (it writes to OpenSearch and reads
    MSSQL, so it's opt-in per environment and needs the MSSQL_* secret on the pod)."""
    if os.getenv("ACI_ADMIN_ENTITY_REINDEX", "0") != "1":
        raise HTTPException(404, "entity reindex disabled "
                                 "(set ACI_ADMIN_ENTITY_REINDEX=1 to enable)")
    from aosphere_core_index.service import entity_reindex
    try:
        return JSONResponse(entity_reindex.start(
            status_filter=status, org=org, if_changed=ifChanged, verify=verify))
    except FileNotFoundError as e:
        raise HTTPException(500, str(e))
    except RuntimeError as e:  # a job is already running
        raise HTTPException(409, str(e))


@app.get("/api/admin/entity-reindex")
def admin_entity_reindex_status(user: dict = Depends(require_admin)) -> JSONResponse:
    """Progress of the current/last entity reindex: phase, elapsed, returncode, error,
    and a tail of the sync's stdout."""
    from aosphere_core_index.service import entity_reindex
    return JSONResponse(entity_reindex.status())


@app.get("/api/admin/eval-divergence")
def admin_eval_divergence(
    backend: str = Query("atlas", description="external backend to check: atlas | opensearch"),
    n: int = Query(40, ge=1, le=500, description="sampled queries"),
    k: int = Query(40, ge=1, le=200),
    user: dict = Depends(require_admin),
) -> JSONResponse:
    """Does `backend` diverge from the in-memory exact index? Samples n index vectors as
    scoped queries and compares top-k row-ids (exact vs backend) + times both — run
    IN-NETWORK to validate an integration and get a representative external latency.
    `diverging` is true only if recall really dropped (not float/tie jitter). Admin-only,
    read-only. The backend must already be loaded (see POST /api/admin/reindex)."""
    from aosphere_core_index.embeddings import vector_eval
    try:
        return JSONResponse(vector_eval.divergence(multi_with_matrix(), backend, n=n, k=k))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # backend not loaded / unreachable -> surface it
        raise HTTPException(503, f"{type(e).__name__}: {e}")


@app.get("/", response_class=HTMLResponse)
def home() -> Response:
    """The whole UI, inline.

    Served with caching switched OFF. It carried no Cache-Control, no ETag and no
    Last-Modified, so browsers fell back to HEURISTIC caching and kept serving the markup
    from before a deploy: the JSON endpoints stayed live, so the page looked like it was
    working while the CSS and JS rendering it were a version old. A UI change would simply
    not appear, with nothing to say why -- which is indistinguishable from the change not
    having been made. scripts/pipeline_monitor.py already carries this rule and the same
    comment; this route needed it more, being the one people actually open.
    """
    return Response(PAGE, media_type="text/html",
                    headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/config")
def config() -> JSONResponse:
    """Public OIDC config for the browser UI to start a Keycloak login (no secrets),
    plus the reranker score scale so the UI can calibrate its relevance-cutoff dropdown
    (cross-encoder 'logit' scores separate around 0; LLM/cohere 'unit' scores are 0..1)."""
    from aosphere_core_index.embeddings.reranker import score_scale

    cfg = dict(public_config())
    cfg["score_scale"] = score_scale()
    # Whether the promote controls should exist at all. The UI asking and getting a 404 is
    # indistinguishable from a broken button, so it is told up front.
    cfg["promotion"] = os.getenv("ACI_ADMIN_PROMOTE", "0") == "1"
    return JSONResponse(cfg)


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)) -> JSONResponse:
    """Identity + access status for the UI to gate on (any authenticated user).
    `has_access` = admin OR email on ACI_ALLOWED_EMAILS (see auth.require_access)."""
    from aosphere_core_index.service.auth import is_allowed

    profile = {} if user.get("anonymous") else user
    return JSONResponse({
        "authenticated": not user.get("anonymous", False),
        "username": profile.get("preferred_username") or profile.get("email") or "",
        "is_admin": is_admin(user),
        "has_access": is_allowed(user),
    })


# ---- Master prompts per product (AOSNG-3442) --------------------------------------------
# Admin-only, on the same gate as the reindex endpoint (require_admin): the prompt decides what
# the assistant asserts about regulated content, so this is not a general-purpose setting.
#
# The UI's tab is hidden until GET /api/prompts answers 200, so a non-admin never sees a screen
# these endpoints would refuse.


def _prompt_products() -> list[str]:
    """Product names present in the index, in the same order the filter shows them.

    Deliberately the SAME source as /api/regions rather than a separate list: a product that
    can be filtered on but not prompted for (or the reverse) would be a silent gap.
    """
    from aosphere_core_index.regions.region_map import PRODUCTS, product_of

    idents = get_multi().regions
    return sorted({product_of(i) for i in idents},
                  key=lambda p: (PRODUCTS.index(p) if p in PRODUCTS else len(PRODUCTS), p))


def _prompt_payload(product: str, mode: str = "summary") -> dict:
    """One product's prompt state FOR ONE MODE: the override if any, and what is in force.

    `effective` is the assembled prompt, not just the product half. The screen exists to answer
    "what is answering questions about this product", and an editor that showed only the box
    would leave an SME guessing about the parts they cannot edit.

    `default_prompt` is the default for this mode — exactly what the editable box replaces, and
    it differs between the two: the summary default carries the brief answer contract, the
    explain default carries the explaining one. It is sent so the UI can diff an override
    against it and so an author can start FROM it, because an override written in an empty box
    begins by discarding a prompt that is known to work.
    """
    from aosphere_core_index.llm.agent import instructions_for
    from aosphere_core_index.llm.prompt_store import backend

    explain = mode == "explain"
    rec = backend().get(product, mode)
    return {
        "product": product,
        "mode": mode,
        "override": rec.prompt if rec else None,
        "is_default": rec is None,
        # The whole default for this mode, because an override replaces the whole thing —
        # sending only the product half would make the diff lie about what changed.
        "default_prompt": instructions_for(explain, None),
        "effective": instructions_for(explain, rec.prompt if rec else None),
        "updated_by": rec.updated_by if rec else None,
        "updated_at": rec.updated_at if rec else None,
    }


def _valid_mode_or_400(mode: str) -> str:
    from aosphere_core_index.llm.prompt_store import MODES, valid_mode

    if not valid_mode(mode):
        raise HTTPException(400, f"mode must be one of {', '.join(MODES)}")
    return mode


def _valid_product_or_404(product: str) -> str:
    """A product must be BOTH a safe storage key and one this index actually has.

    Existence is checked, not just shape: writing an override for a mistyped product would
    store a prompt nothing ever reads, and the SME would have no way to tell.
    """
    from aosphere_core_index.llm.prompt_store import valid_product

    if not valid_product(product):
        raise HTTPException(400, "invalid product name")
    if product not in _prompt_products():
        raise HTTPException(404, f"no such product in this index: {product}")
    return product


class PromptUpdate(BaseModel):
    """A new override. Length-capped because it becomes a system prompt on every request for
    this product — an accidental paste of a whole document would be charged per question."""

    prompt: str = Field(min_length=1, max_length=20000)


@app.get("/api/prompts")
def prompts_list(user: dict = Depends(require_admin)) -> JSONResponse:
    """Every product, and which MODES it has customised. Cheap: one store listing.

    `modes` rather than a single flag, because a product can have a custom explaining answer
    and a default summary one, and the screen has to show which.
    """
    from aosphere_core_index.llm.prompt_store import MODES, backend

    try:
        overridden = backend().overrides()
    except Exception:                                        # noqa: BLE001
        log.exception("prompt store listing failed")
        overridden = {}
    return JSONResponse({"modes": list(MODES), "products": [
        {"product": p,
         "modes": sorted(overridden.get(p, ()), key=MODES.index),
         "has_override": bool(overridden.get(p))}
        for p in _prompt_products()]})


# Registered BEFORE /api/prompts/{product:path}, which is a path route and would otherwise
# swallow "_default". A separate endpoint rather than a read of some product's payload because
# the default belongs to the MODE, not to any product: asking for it through a product would
# imply it could differ per product, and would need an index with at least one product to
# answer at all. Read-only by construction — there is nowhere to save it to, the store holds
# overrides only, and one edit here would change every product's answer at once.
@app.get("/api/prompts/_default")
def prompt_default(mode: str = Query("summary"),
                   user: dict = Depends(require_admin)) -> JSONResponse:
    """The built-in default prompt for ONE mode, with no product involved."""
    from aosphere_core_index.llm.agent import instructions_for

    mode = _valid_mode_or_400(mode)
    return JSONResponse({
        "product": None,
        "mode": mode,
        "read_only": True,
        "default_prompt": instructions_for(mode == "explain", None),
    })


@app.get("/api/prompts/{product:path}")
def prompt_get(product: str, mode: str = Query("summary"),
               user: dict = Depends(require_admin)) -> JSONResponse:
    return JSONResponse(_prompt_payload(_valid_product_or_404(product),
                                        _valid_mode_or_400(mode)))


@app.put("/api/prompts/{product:path}")
def prompt_put(product: str, body: PromptUpdate, mode: str = Query("summary"),
               user: dict = Depends(require_admin)) -> JSONResponse:
    """Install an override for ONE mode. An all-whitespace prompt is a DELETE, never an empty
    prompt: an empty system prompt would drop the grounding and citation rules entirely."""
    from aosphere_core_index.llm.prompt_store import PromptStoreUnavailable, backend

    product = _valid_product_or_404(product)
    mode = _valid_mode_or_400(mode)
    if not body.prompt.strip():
        return prompt_delete(product, mode, user)
    try:
        backend().put(product, body.prompt, _prompt_actor(user), mode)
    except PromptStoreUnavailable as e:
        raise HTTPException(503, str(e))
    log.info("prompt override saved product=%r mode=%s by=%r chars=%d",
             product, mode, _prompt_actor(user), len(body.prompt))
    return JSONResponse(_prompt_payload(product, mode))


@app.delete("/api/prompts/{product:path}")
def prompt_delete(product: str, mode: str = Query("summary"),
                  user: dict = Depends(require_admin)) -> JSONResponse:
    """Remove ONE mode's override so that answer falls back to the default. The other mode is
    untouched — clearing a custom explanation must not silently revert the summary too."""
    from aosphere_core_index.llm.prompt_store import PromptStoreUnavailable, backend

    product = _valid_product_or_404(product)
    mode = _valid_mode_or_400(mode)
    try:
        backend().delete(product, mode)
    except PromptStoreUnavailable as e:
        raise HTTPException(503, str(e))
    log.info("prompt override cleared product=%r mode=%s by=%r",
             product, mode, _prompt_actor(user))
    return JSONResponse(_prompt_payload(product, mode))


def _prompt_actor(user: dict) -> str | None:
    """Who to record as having changed a prompt. Identity comes from the verified token."""
    from aosphere_core_index.service.auth import token_email

    return token_email(user) or user.get("sub") or None


# ---- Guided interview (Marketing Restrictions) ----------------------------------------
# A Marketing Restrictions question is only answerable once the scenario is pinned down:
# jurisdiction, activity, product/service category, (in the EEA, for active marketing) who
# is marketing, and investor type. Asked without them the memo genuinely says "it depends",
# because it carries a different route for each combination. These endpoints collect those
# five and hand the answer path a scenario it can be definitive about.
#
# STATELESS, deliberately. The client holds the transcript and the slots and posts them
# back each turn; the server validates, merges and says what to ask next. AI Mode's
# in-process session store already carries a single-process caveat (see llm/agent.py) and
# an interview is long-lived enough to land on another pod mid-flow.
#
# The three calls, in the order a client makes them:
#   1. GET  /api/interview/schema   once — the vocabulary, options and definitions
#   2. POST /api/interview/triage   for a free-text turn WHILE a field is pending
#   3. POST /api/interview/route    for the first turn, and for any turn that answers
# and then POST /api/interview/handoff (or `handoff` on a complete route response) to get
# the query + scenario to pass to /api/agent.


def _interview_jurisdictions(product: str, user: dict,
                             selected: str | None = None) -> list[str]:
    """Jurisdictions the interview may offer: those this index holds for the product, that
    this caller is entitled to, and — when given — that the caller's own filter selects.

    Taken from the index rather than a list, so a jurisdiction the tool can answer for is
    never one it refuses to ask about.

    `selected` is the caller's jurisdiction filter, and honouring it is what stops the
    interview asking something the user has ALREADY answered by choosing a region: with one
    jurisdiction selected there is nothing to ask, and with a few there are a few options
    rather than 88. It NARROWS only — a filter naming what this index or this caller does
    not have cannot widen the list.
    """
    from aosphere_core_index.regions.region_map import product_of, split_region

    ent = _scope(None, user)
    idents = get_multi().regions if ent is None else ent
    have = {str(split_region(i)[1]) for i in idents if product_of(i) == product}
    only = _parse_jurisdictions(selected)
    if only:
        narrowed = have & set(only)
        # An empty intersection means the filter selects nothing this product covers (a
        # Data Privacy-only jurisdiction, say). Falling back to everything would silently
        # ignore the filter, so the interview offers what it actually has and the mismatch
        # stays visible to the user.
        if narrowed:
            have = narrowed
    return sorted(have)


def _interview_product_or_404(product: str | None) -> str:
    """The product whose interview is being run.

    Only Marketing Restrictions has a vocabulary: these are its own terms of art, and
    offering "Pre-Marketing of Funds" for Data Privacy would be nonsense. A 404 rather
    than a silent fallback, so a caller cannot think it ran an interview that does not
    exist."""
    from aosphere_core_index.interview import vocabulary as IV

    name = product or IV.PRODUCT
    if name != IV.PRODUCT:
        raise HTTPException(404, f"no guided interview for product {name!r}")
    if name not in _prompt_products():
        raise HTTPException(404, f"product {name!r} is not in this index")
    return name


def _interview_options(field: str, slots: dict, signals: dict,
                       jurisdictions: list[str]) -> list[dict]:
    """One field's options, each with its definition and — where it does not apply — the
    reason. Excluded options are RETURNED rather than filtered: an option that silently
    vanishes reads as a bug, while one greyed out with a reason teaches the vocabulary."""
    from aosphere_core_index.interview import engine as IE
    from aosphere_core_index.interview import vocabulary as IV

    excluded = IE.exclusions_for(field, slots, signals)
    out = []
    for value in IE.options_for(field, slots, jurisdictions):
        opt: dict = {"value": value}
        d = IV.option_definition(field, value)
        if d:
            opt["definition"] = d["definition"]
            if d.get("doc_term") and d["doc_term"] != value:
                opt["doc_term"] = d["doc_term"]
            if d.get("drafted"):
                # Surfaced so a reviewer can see which wording is ours rather than the
                # definitions document's.
                opt["drafted"] = True
        if field == "jurisdiction":
            opt["eea"] = IV.is_eea(value)
        if value in excluded:
            opt["excluded_reason"] = excluded[value]
        out.append(opt)
    return out


def _interview_next(slots: dict, signals: dict, jurisdictions: list[str]) -> dict | None:
    from aosphere_core_index.interview import engine as IE
    from aosphere_core_index.interview import vocabulary as IV

    field = IE.next_missing(slots)
    if field is None:
        return None
    return {
        "field": field,
        "label": IV.FIELD_LABELS[field],
        "question": IE.question_for(field, slots, signals),
        "options": _interview_options(field, slots, signals, jurisdictions),
    }


def _interview_sections(product: str, jurisdiction: str | None,
                        titles: list[str]) -> list[dict]:
    """Resolve section-title hints to the real sections of THIS jurisdiction's memo.

    Title-matched case-insensitively because the corpus carries both "PASSIVE MARKETING
    (REVERSE-ENQUIRY)" and the title-case form, and a jurisdiction that simply lacks a
    section (not every memo has a LICENCE part) yields nothing rather than a dead key.
    """
    if not jurisdiction or not titles:
        return []
    from aosphere_core_index.regions.region_map import qualified

    try:
        bundle = get_bundle(qualified(product, jurisdiction))
    except Exception:  # noqa: BLE001 — a missing bundle costs a hint, never the answer
        return []
    by_title: dict[str, dict] = {}
    for s in bundle.content.get("sections", []):
        by_title.setdefault(str(s.get("title", "")).strip().lower(), s)
    out = []
    for t in titles:
        s = by_title.get(t.strip().lower())
        if s is not None:
            out.append({"key": s["key"], "title": s["title"]})
    return out


def _interview_region(product: str, jurisdiction: str | None) -> str | None:
    """The QUALIFIED region id for a jurisdiction, e.g.
    "Marketing Restrictions - Asset Management — Jersey".

    Returned alongside the bare name because the clause endpoints resolve a bare name by
    guessing: `_resolve_region` takes the first identity that matches, and "Jersey" exists
    under both Data Privacy and Marketing Restrictions. A caller opening a section this
    interview named must land in the memo the interview was about.
    """
    if not jurisdiction:
        return None
    from aosphere_core_index.regions.region_map import qualified

    return qualified(product, jurisdiction)


class InterviewMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=_Q_MAX)


# Bounded because the whole transcript goes into a prompt on every turn: unbounded, one
# long conversation costs more per turn than the answer it is working towards.
_INTERVIEW_MSG_MAX = 40


class InterviewSlots(BaseModel):
    """What the client believes it has so far. Every value is re-validated against the
    vocabulary server-side, so a client cannot post its way past a question."""

    jurisdiction: str | None = Field(None, max_length=200)
    activity: str | None = Field(None, max_length=100)
    category: str | None = Field(None, max_length=100)
    marketer: str | None = Field(None, max_length=100)
    investor_type: str | None = Field(None, max_length=100)


class InterviewSignals(BaseModel):
    product_kind: str | None = Field(None, max_length=20)
    category_hint: dict | None = None
    activity_hint: dict | None = None


class InterviewScoped(BaseModel):
    """The scope an interview call is made in — the same two axes AI Mode filters on.

    `jurisdictions` is the caller's own filter, and passing it is what keeps the interview
    from asking a question the user has already answered: see _interview_jurisdictions.
    """

    product: str | None = Field(None, max_length=_PRODUCT_CSV_MAX)
    jurisdictions: str | None = Field(None, max_length=_JURIS_CSV_MAX,
                                      description="CSV of the caller's selected "
                                                  "jurisdiction names")


class InterviewRouteRequest(InterviewScoped):
    messages: list[InterviewMessage] = Field(..., min_length=1,
                                             max_length=_INTERVIEW_MSG_MAX)
    slots: InterviewSlots = InterviewSlots()
    signals: InterviewSignals = InterviewSignals()


class InterviewTriageRequest(BaseModel):
    messages: list[InterviewMessage] = Field(..., min_length=1,
                                             max_length=_INTERVIEW_MSG_MAX)
    pending_field: str | None = Field(None, max_length=40)
    jurisdiction: str | None = Field(None, max_length=200)
    product: str | None = Field(None, max_length=_PRODUCT_CSV_MAX)


class InterviewHandoffRequest(InterviewScoped):
    slots: InterviewSlots
    original_question: str = Field("", max_length=_Q_MAX)
    facets: dict | None = None
    other_kind: str | None = Field(None, max_length=40)


@app.get("/api/interview/schema")
def interview_schema(product: str = Query(None, max_length=_PRODUCT_CSV_MAX),
                     jurisdictions: str = Query(
                         None, max_length=_JURIS_CSV_MAX,
                         description="CSV of selected jurisdiction names, to narrow the "
                                     "offered list to the caller's own filter"),
                     user: dict = Depends(require_access)) -> JSONResponse:
    """The interview's shape and vocabulary, for a client that renders it deterministically.

    Fetched once. With this, picking an option, showing a definition and following the
    branch need no model call at all — the model is only needed to read free text.
    """
    from aosphere_core_index.interview import vocabulary as IV

    name = _interview_product_or_404(product)
    offered = _interview_jurisdictions(name, user, jurisdictions)
    return JSONResponse({
        "product": name,
        "fields": list(IV.FIELDS),
        "labels": IV.FIELD_LABELS,
        "questions": IV.QUESTION_COPY,
        "jurisdictions": [{"name": j, "eea": IV.is_eea(j)} for j in offered],
        "activity_options": list(IV.ACTIVITY_OPTIONS),
        "category_options": {
            "non_eea": list(IV.NON_EEA_CATEGORY_OPTIONS),
            "eea_pre_marketing": list(IV.EEA_PRE_MARKETING_CATEGORY_OPTIONS),
            "eea_passive": list(IV.EEA_PASSIVE_CATEGORY_OPTIONS),
            "eea_active": list(IV.EEA_ACTIVE_CATEGORY_OPTIONS),
        },
        "investor_type_options": {
            "non_eea": list(IV.NON_EEA_INVESTOR_TYPE_OPTIONS),
            "eea": list(IV.EEA_INVESTOR_TYPE_OPTIONS),
        },
        "marketer_options": list(IV.MARKETER_OPTIONS),
        "definitions": IV.OPTION_DEFINITIONS,
        "context_terms": IV.CONTEXT_TERMS,
    })


@app.post("/api/interview/route")
def interview_route(body: InterviewRouteRequest,
                    user: dict = Depends(require_access)) -> JSONResponse:
    """Classify the conversation, extract what the user already said, and say what to ask
    next — or hand off a complete scenario.

    The model MAPS WORDING onto the controlled values; every decision about what happens
    next is taken from those values deterministically (see interview/engine.py). That
    split is why a hallucinated jurisdiction cannot skip the jurisdiction question.
    """
    from aosphere_core_index.interview import engine as IE
    from aosphere_core_index.interview import router as IR
    from aosphere_core_index.interview import vocabulary as IV

    name = _interview_product_or_404(body.product)
    jurisdictions = _interview_jurisdictions(name, user, body.jurisdictions)
    messages = [m.model_dump() for m in body.messages]
    before = body.slots.model_dump()

    try:
        routed = IR.classify(messages, jurisdictions)
    except IR.RouterUnavailable:
        raise HTTPException(503, "The interview router is temporarily unavailable.")

    signals = IE.merge_signals(body.signals.model_dump(), routed["signals"])
    slots = IE.merge_slots(before, routed["slots"], signals, jurisdictions)
    intent = routed["intent"]

    out: dict = {
        "product": name,
        "intent": intent,
        "other_kind": routed["other_kind"],
        "slots": slots,
        "signals": signals,
        "rationales": routed["rationales"],
        "acknowledgement": IE.acknowledgements(before, slots, routed["rationales"],
                                               signals),
        "next_question": None,
        "complete": False,
        "handoff": None,
    }

    if intent == "capability":
        # Answered inline from the vocabulary, and the interview is left exactly as it
        # was: a question about the tool is not an answer to the pending field.
        out["capability"] = IV.capability_text(len(jurisdictions))
        return JSONResponse(out)

    if intent == "other":
        # Not an interview at all — a definition, a penalty, a disclaimer. It still needs
        # a jurisdiction (the corpus is per-jurisdiction), and the carve-out tells us
        # which part of the memo answers it.
        original = next((m["content"] for m in reversed(messages)
                         if m["role"] == "user"), "")
        titles = list(IE.section_hints(slots, routed["other_kind"]))
        out["general"] = {
            "jurisdiction": slots["jurisdiction"],
            "region": _interview_region(name, slots["jurisdiction"]),
            "needs_jurisdiction": not slots["jurisdiction"],
            "query": original,
            "section_hints": titles,
            "sections": _interview_sections(name, slots["jurisdiction"], titles),
        }
        if not slots["jurisdiction"]:
            out["next_question"] = _interview_next(
                {**IE.EMPTY_SLOTS}, signals, jurisdictions)
        return JSONResponse(out)

    nxt = _interview_next(slots, signals, jurisdictions)
    if nxt is not None:
        out["next_question"] = nxt
        return JSONResponse(out)

    original = next((m["content"] for m in messages if m["role"] == "user"), "")
    handoff = IE.handoff(slots, original, other_kind=routed["other_kind"])
    handoff["region"] = _interview_region(name, slots["jurisdiction"])
    handoff["sections"] = _interview_sections(name, slots["jurisdiction"],
                                              handoff["section_hints"])
    out["complete"] = True
    out["handoff"] = handoff
    return JSONResponse(out)


class InterviewNextRequest(InterviewScoped):
    slots: InterviewSlots
    signals: InterviewSignals = InterviewSignals()
    original_question: str = Field("", max_length=_Q_MAX)
    other_kind: str | None = Field(None, max_length=40)


@app.post("/api/interview/next")
def interview_next(body: InterviewNextRequest,
                   user: dict = Depends(require_access)) -> JSONResponse:
    """Advance the interview WITHOUT a model call: given the answers so far, the next
    question — or the hand-off.

    This is the endpoint a clicked option uses, and it is separate from /route for a
    reason: choosing from a list needs no wording mapped, so routing a click through the
    extractor would spend a Bedrock call and a second and a half of latency to be told
    something the client already knows. /route is for free text; this is for choices.

    Same validation as everywhere else — the posted slots are re-checked, so an answer
    that the rest of the scenario no longer permits is dropped and its question re-asked.
    """
    from aosphere_core_index.interview import engine as IE

    name = _interview_product_or_404(body.product)
    jurisdictions = _interview_jurisdictions(name, user, body.jurisdictions)
    signals = body.signals.model_dump()
    slots = IE.merge_slots(body.slots.model_dump(), IE.EMPTY_SLOTS, signals, jurisdictions)
    nxt = _interview_next(slots, signals, jurisdictions)
    out = {"product": name, "slots": slots, "signals": signals,
           "next_question": nxt, "complete": nxt is None, "handoff": None}
    if nxt is None:
        handoff = IE.handoff(slots, body.original_question, other_kind=body.other_kind)
        handoff["region"] = _interview_region(name, slots["jurisdiction"])
        handoff["sections"] = _interview_sections(name, slots["jurisdiction"],
                                                  handoff["section_hints"])
        out["handoff"] = handoff
    return JSONResponse(out)


@app.post("/api/interview/triage")
def interview_triage(body: InterviewTriageRequest,
                     user: dict = Depends(require_access)) -> JSONResponse:
    """What a free-text turn IS while a question is pending.

    Three lanes, three very different handlings, which is why this is its own call rather
    than folded into /route:
      * "answer"           -> post it to /route; the interview advances.
      * "scoping_question" -> `answer` explains the term from the controlled definitions;
                              the interview state is UNTOUCHED and the same field is
                              re-asked. `has_answer` says the turn ALSO stated a value, so
                              it should still go to /route.
      * "route_onward"     -> a genuine regulatory question. Suspend the interview (keep
                              the slots), answer the aside, resume afterwards.
    """
    from aosphere_core_index.interview import router as IR
    from aosphere_core_index.interview import vocabulary as IV

    _interview_product_or_404(body.product)
    if body.pending_field is not None and body.pending_field not in IV.FIELDS:
        raise HTTPException(400, f"pending_field must be one of {', '.join(IV.FIELDS)}")
    messages = [m.model_dump() for m in body.messages]

    try:
        triaged = IR.triage(messages, body.pending_field)
        out = {"lane": triaged["lane"], "has_answer": triaged["has_answer"]}
        if triaged["lane"] == "scoping_question":
            latest = messages[-1]["content"]
            out["answer"] = IR.explain(latest, body.pending_field, body.jurisdiction)
        return JSONResponse(out)
    except IR.RouterUnavailable:
        raise HTTPException(503, "The interview router is temporarily unavailable.")


@app.post("/api/interview/handoff")
def interview_handoff(body: InterviewHandoffRequest,
                      user: dict = Depends(require_access)) -> JSONResponse:
    """The confirmed scenario, composed for the answer path. NO model call — this is the
    same deterministic composition /route returns on completion, exposed for a client that
    collected the answers by clicking options and never needed the extractor.

    `query` is the retrieval wording; `scenario` is the LLM-facing statement of confirmed
    facts; `definitions` grounds the model in the same meanings the user saw; `sections`
    names the memo clauses this scenario is answered from. `original_question` is carried
    through unchanged because it holds the detail the vocabulary has no slot for.
    """
    from aosphere_core_index.interview import engine as IE
    from aosphere_core_index.interview import vocabulary as IV

    name = _interview_product_or_404(body.product)
    jurisdictions = _interview_jurisdictions(name, user, body.jurisdictions)
    slots = IE.merge_slots(body.slots.model_dump(), IE.EMPTY_SLOTS, IE.EMPTY_SIGNALS,
                           jurisdictions)
    missing = IE.next_missing(slots)
    if missing:
        raise HTTPException(400, f"scenario incomplete: {IV.FIELD_LABELS[missing]} is "
                                 f"missing or not valid for this jurisdiction/activity")
    out = IE.handoff(slots, body.original_question, facets=body.facets,
                     other_kind=body.other_kind)
    out["product"] = name
    out["region"] = _interview_region(name, slots["jurisdiction"])
    out["sections"] = _interview_sections(name, slots["jurisdiction"],
                                          out["section_hints"])
    return JSONResponse(out)


@app.get("/api/regions")
def regions(user: dict = Depends(require_access)) -> JSONResponse:
    """Common region model for the UI: region (continent) and jurisdiction are shared
    across products, product is an independent filter.

      products      : product names present in the index (known order first)
      regions       : continent -> [jurisdiction name] (union across products, deduped)
      jurisdiction_products : name -> [products offering it] (so the UI can tag/expand)

    Search then takes selected products x selected jurisdiction names and expands to the
    qualified ids server-side (see _scope_ids)."""
    from aosphere_core_index.regions.region_map import (
        PRODUCTS, REGIONS, product_of, region_for, split_region,
    )

    idents = get_multi().regions
    products_present = sorted(
        {product_of(i) for i in idents},
        key=lambda p: (PRODUCTS.index(p) if p in PRODUCTS else len(PRODUCTS), p),
    )
    by_continent: dict[str, set[str]] = {}
    jur_products: dict[str, set[str]] = {}
    for ident in idents:
        product, name = split_region(ident)
        by_continent.setdefault(region_for(ident), set()).add(name)
        jur_products.setdefault(name, set()).add(product)
    regions_tree = {
        cont: sorted(by_continent[cont])
        for cont in [*REGIONS, *(c for c in by_continent if c not in REGIONS)]
        if cont in by_continent
    }
    return JSONResponse({
        "products": products_present,
        "regions": regions_tree,
        "jurisdiction_products": {n: sorted(p) for n, p in jur_products.items()},
    })


def _search(q: str, k: int, min_score: float, products: str | None,
            jurisdictions: str | None, user: dict) -> JSONResponse:
    """Search body, shared by the GET and POST forms."""
    allowed = _scope_ids(products, jurisdictions, user)
    hits = [h for h in search_all(q, k=k, jurisdictions=allowed) if h["score"] >= min_score]
    from aosphere_core_index.service.registry import did_you_mean

    return JSONResponse({"query": q, "results": hits, "min_score": min_score,
                         "did_you_mean": did_you_mean(q),
                         "jurisdictions": _detected_jurisdictions(q, user)})


@app.get("/api/search")
def search(
    q: str = Query(..., max_length=_Q_MAX),
    k: int = Query(80, ge=1, le=200), min_score: float = 0.0,
    products: str = Query(None, description="CSV of product names",
                          max_length=_PRODUCT_CSV_MAX),
    jurisdictions: str = Query(None, description="CSV of jurisdiction names",
                               max_length=_JURIS_CSV_MAX),
    user: dict = Depends(require_access),
) -> JSONResponse:
    """Cross-region search (cross-encoder reranked). Returns clauses with rerank
    score >= min_score; the score separates ~around 0 across queries/regions.
    Filter by products and/or jurisdiction names (both common across products).

    Prefer the POST form when filtering: a jurisdictions CSV can be several KB, and a
    proxy or WAF may cap the query string well below `_JURIS_CSV_MAX`."""
    return _search(q, k, min_score, products, jurisdictions, user)


class SearchRequest(BaseModel):
    """A search in the request BODY — same fields as the GET.

    The UI uses this for the same reason AI Mode does: narrowing the jurisdiction filter
    put a CSV of up to `_JURIS_CSV_MAX` characters in the URL, and percent-encoded commas
    cost 3 bytes each, so a large-but-partial selection blew past the WAF's 2 KB query
    string limit and search returned the WAF's 403 rather than results.
    """

    q: str = Field(..., max_length=_Q_MAX)
    k: int = Field(80, ge=1, le=200)
    min_score: float = 0.0
    products: str | None = Field(None, max_length=_PRODUCT_CSV_MAX,
                                 description="CSV of product names")
    jurisdictions: str | None = Field(None, max_length=_JURIS_CSV_MAX,
                                      description="CSV of jurisdiction names")


@app.post("/api/search")
def search_post(body: SearchRequest,
                user: dict = Depends(require_access)) -> JSONResponse:
    """Cross-region search, filters in the body. See GET /api/search."""
    return _search(body.q, body.k, body.min_score, body.products, body.jurisdictions, user)


@app.get("/api/ai-status")
def ai_status(user: dict = Depends(require_access)) -> JSONResponse:
    import os

    from aosphere_core_index.llm.agent import DEFAULT_MODEL, MODELS

    return JSONResponse({
        "enabled": os.getenv("ACI_AI_ENABLED", "1") == "1",
        "models": MODELS,
        "default_model": DEFAULT_MODEL,
    })


def _interview_answer_context(interview: "InterviewSlots | None", products: str | None,
                              user: dict) -> tuple[str | None, str | None]:
    """A confirmed interview scenario -> (context block for the seed, jurisdiction CSV).

    Composed HERE, from re-validated slots, rather than accepted as prose from the client.
    The client could put anything in `q` already, so this is not a security boundary — it
    is a correctness one: what reaches the seed as "CONFIRMED" is then always a scenario
    the interview could actually have produced, in the vocabulary of the jurisdiction it
    names.

    The jurisdiction comes back so the caller can NARROW the search scope to it. The whole
    point of asking was that the answer is jurisdiction-specific; leaving the scope wide
    would let the agent answer confidently from a neighbour's memo.
    """
    if interview is None:
        return None, None
    from aosphere_core_index.interview import engine as IE
    from aosphere_core_index.interview import vocabulary as IV

    name = (_parse_products(products) or [IV.PRODUCT])[0]
    if name != IV.PRODUCT:
        return None, None
    jurisdictions = _interview_jurisdictions(name, user)
    slots = IE.merge_slots(interview.model_dump(), IE.EMPTY_SLOTS, IE.EMPTY_SIGNALS,
                           jurisdictions)
    # An incomplete scenario is IGNORED rather than half-stated: "these facts are
    # confirmed" over a partial list invites the model to fill the gaps itself, which is
    # exactly what the interview exists to prevent.
    if IE.next_missing(slots) is not None:
        return None, None
    sections = _interview_sections(name, slots["jurisdiction"], list(
        IE.section_hints(slots)))
    return IE.answer_context(slots, sections), slots["jurisdiction"]


@app.get("/api/agent")
async def agent(
    q: str = Query(..., max_length=_Q_MAX),
    products: str = Query(None, description="CSV of product names",
                          max_length=_PRODUCT_CSV_MAX),
    jurisdictions: str = Query(None, description="CSV of jurisdiction names",
                               max_length=_JURIS_CSV_MAX),
    model: str = Query(None, description="Bedrock model id (from /api/ai-status)",
                       max_length=120),
    explain: bool = Query(False, description="Ask for the reasoning instead of a brief reply"),
    user: dict = Depends(require_access),
) -> JSONResponse:
    """AI Mode (non-streaming): agent searches/reads clauses and answers with citations.

    No `interview` parameter here, unlike the streamed form: a confirmed scenario is five
    fields, and putting them in a query string is how the WAF's ~2 KB limit gets hit (see
    AgentStreamRequest). A caller with a scenario wants POST /api/agent/stream."""
    from aosphere_core_index.llm.agent import run_agent_async

    allowed = _scope_ids(products, jurisdictions, user)
    try:
        return JSONResponse(await run_agent_async(q, allowed, model=model, explain=explain,
                                                  products=_parse_products(products)))
    except Exception:
        log.exception("agent run failed")  # full detail server-side only
        return JSONResponse({"answer": "AI Mode is temporarily unavailable. Please try again.",
                             "sources": [], "error": True}, status_code=200)


@app.post("/api/agent/stream-token")
def agent_stream_token(user: dict = Depends(require_access)) -> JSONResponse:
    """Short-lived opaque token for the SSE endpoint. EventSource can't set headers,
    so the client exchanges its bearer token (sent normally, in a header) for this
    and passes it as `stream_token` — the real JWT never appears in a URL."""
    return JSONResponse({"token": issue_stream_token(user)})


def _agent_sse(q: str, products: str | None, jurisdictions: str | None,
               model: str | None, session: str | None, user: dict,
               explain: bool = False, context: str | None = None):
    """The SSE body, shared by the POST and GET forms of the stream endpoint."""
    import json as _json

    from fastapi.responses import StreamingResponse

    from aosphere_core_index.llm.agent import run_agent_stream

    allowed = _scope_ids(products, jurisdictions, user)
    # The PRODUCT scope travels separately. _scope_ids flattens (products x jurisdictions)
    # into qualified region ids, so by the time the agent sees `allowed` the product names —
    # and how MANY there were, which is the whole rule — are gone (AOSNG-3442).
    in_scope = _parse_products(products)

    async def gen():
        try:
            async for evt in run_agent_stream(q, allowed, model=model, session_id=session,
                                              explain=explain, products=in_scope,
                                              context=context):
                yield f"data: {_json.dumps(evt)}\n\n"
        except Exception:
            log.exception("agent stream failed")  # full detail server-side only
            yield f"data: {_json.dumps({'kind': 'error', 'text': 'AI Mode is temporarily unavailable. Please try again.'})}\n\n"
        yield "data: {\"kind\": \"done\"}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class AgentStreamRequest(BaseModel):
    """A streamed AI Mode question, in the request BODY.

    Everything here used to travel in the query string, because EventSource can only GET.
    In front of an AWS WAF that is a hard ~2 KB ceiling on the WHOLE query string
    (SizeRestrictions_QUERYSTRING), and the claims-bearing stream token alone spent most of
    it — so a long question was rejected with the WAF's own 403 before reaching the app,
    while `_Q_MAX` advertised 2000 characters. In a body there is no such ceiling, and the
    bearer token stays in a header where it belongs.
    """

    q: str = Field(..., max_length=_Q_MAX)
    products: str | None = Field(None, max_length=_PRODUCT_CSV_MAX,
                                 description="CSV of product names")
    jurisdictions: str | None = Field(None, max_length=_JURIS_CSV_MAX,
                                      description="CSV of jurisdiction names")
    model: str | None = Field(None, max_length=120, description="Bedrock model id")
    session: str | None = Field(None, max_length=120,
                                description="Chat session id for multi-turn follow-ups")
    explain: bool = Field(False, description="Ask for the reasoning; default is a brief, "
                                            "lead-with-the-answer reply")
    # The confirmed scenario, when a guided interview produced one (Marketing
    # Restrictions). Sent as SLOTS, not as prose: the server re-validates them and
    # composes the context block itself, so what the model is told is "confirmed" is
    # always a scenario the interview could have produced.
    interview: InterviewSlots | None = Field(
        None, description="Confirmed guided-interview slots (see /api/interview/*)")


@app.post("/api/agent/stream")
async def agent_stream_post(body: AgentStreamRequest,
                            user: dict = Depends(require_access)):
    """AI Mode (streaming SSE), question in the body. This is what the UI uses: it keeps
    long questions off the URL and authenticates with the normal Authorization header."""
    context, jurisdiction = _interview_answer_context(body.interview, body.products, user)
    # The interview's jurisdiction REPLACES the filter when there is one: the user answered
    # "which jurisdiction" explicitly, and that answer is more specific than whatever the
    # sidebar happened to have selected.
    return _agent_sse(body.q, body.products, jurisdiction or body.jurisdictions,
                      body.model, body.session, user, body.explain, context=context)


@app.get("/api/agent/stream")
async def agent_stream(
    q: str = Query(..., max_length=_Q_MAX),
    products: str = Query(None, description="CSV of product names",
                          max_length=_PRODUCT_CSV_MAX),
    jurisdictions: str = Query(None, description="CSV of jurisdiction names",
                               max_length=_JURIS_CSV_MAX),
    model: str = Query(None, description="Bedrock model id", max_length=120),
    session: str = Query(None, description="Chat session id for multi-turn follow-ups",
                         max_length=120),
    explain: bool = Query(False, description="Ask for the reasoning instead of a brief reply"),
    user: dict = Depends(require_access),
):
    """AI Mode (streaming SSE) for EventSource clients, which cannot set headers or POST.
    Pass a stable `session` to keep conversation context across follow-ups.
    Auth: `stream_token` query param from POST /api/agent/stream-token.

    Prefer the POST form: a proxy or WAF may cap the query string well below `_Q_MAX`."""
    return _agent_sse(q, products, jurisdictions, model, session, user, explain)


def _resolve_region(jurisdiction: str, regions) -> str | None:
    """Resolve a jurisdiction name to a real region id, tolerating what an agent
    citation may emit: the exact id, a delimiter/case variant, a bare jurisdiction
    name, or an ALIAS/abbreviation ("EU" -> "European Union"). None if unresolvable."""
    if jurisdiction in set(regions):
        return jurisdiction
    from aosphere_core_index.regions.aliases import named_jurisdictions
    from aosphere_core_index.regions.region_map import match_identities

    ids = match_identities(jurisdiction, regions)  # exact id / hyphen variant / bare name
    if ids:
        return str(ids[0])
    hits = sorted(named_jurisdictions(jurisdiction, regions))  # alias e.g. EU -> European Union
    return str(hits[0]) if hits else None


def _bundle_or_404(jurisdiction: str):
    """get_bundle guarded by the index's region list. Unknown names are a clean 404,
    but agent citations often use an alias/abbreviation ("EU" for "European Union") or
    a bare name — resolve those to the real id before giving up."""
    resolved = _resolve_region(jurisdiction, get_multi().regions)
    if resolved is None:
        raise HTTPException(404, f"unknown jurisdiction {jurisdiction!r}")
    return get_bundle(resolved)


def _subtree(sections: list[dict], idx_by_key: dict, key: str, level: int) -> list[dict]:
    """All descendant sections in document order (children, grandchildren, ...).

    Sections are stored in document order, so a section's subtree is the
    contiguous run of following sections with a deeper level. This lets a
    heading-only parent (e.g. C3.3) render its children's actual content.
    """
    i = idx_by_key[key]
    out = []
    for s in sections[i + 1:]:
        if s["level"] <= level:
            break
        # footnotes travel WITH the elements. A heading-only parent renders its
        # children's content inline, so their superscripts appear on screen — and
        # without their citations the reader saw "[153]" pointing at nothing, because
        # the pane can only show the footnotes it was given.
        out.append({"key": s["key"], "title": s["title"], "level": s["level"],
                    "elements": s["elements"], "footnotes": s.get("footnotes", [])})
    return out


@app.get("/api/alert")
def alert(jurisdiction: str = Query(...), id: str = Query(..., max_length=64),
          user: dict = Depends(require_access)) -> JSONResponse:
    """One alert's full record by id — backs clickable ALERT:<id> citations, including
    UNMAPPED alerts (mapped=[]) that sit on no clause and so appear in no clause's
    Alerts section. Accepts a bare id or the 'ALERT:<id>' citation key."""
    bundle = _bundle_or_404(jurisdiction)
    aid = str(id).split(":")[-1]
    rec = bundle.content.get("alerts_by_id", {}).get(aid)
    if not rec:
        raise HTTPException(404, f"alert {id!r} not found in {jurisdiction}")
    return JSONResponse({
        "id": aid, "title": rec.get("title", ""), "impact": rec.get("impact", ""),
        "date": rec.get("date", ""), "summary": rec.get("summary", "") or "",
        "attachment": (rec.get("attachment_text", "") or "")[:4000],
        "mapped": rec.get("mapped", []),
    })


@app.get("/api/guidance")
def guidance(jurisdiction: str = Query(...), id: str = Query(..., max_length=64),
             user: dict = Depends(require_access)) -> JSONResponse:
    """One curated-guidance entry by id — backs clickable GUID:<id> citations for
    standalone (unlinked) guidance. Accepts a bare id or the 'GUID:<id>' key."""
    bundle = _bundle_or_404(jurisdiction)
    gid = str(id).split(":")[-1]
    rec = bundle.content.get("guidance_by_id", {}).get(gid)
    if not rec:
        raise HTTPException(404, f"guidance {id!r} not found in {jurisdiction}")
    return JSONResponse(rec)


@app.get("/api/section")
def section(jurisdiction: str = Query(...), key: str = Query(...),
            user: dict = Depends(require_access)) -> JSONResponse:
    bundle = _bundle_or_404(jurisdiction)
    sec = bundle.sections_by_key.get(key)
    if sec is None:
        raise HTTPException(404, f"section {key} not found in {jurisdiction}")
    content = bundle.content
    sections = content["sections"]
    idx_by_key = {s["key"]: i for i, s in enumerate(sections)}
    return JSONResponse({
        "jurisdiction": jurisdiction, "key": key, "title": sec["title"], "level": sec["level"],
        "breadcrumb": _breadcrumb(bundle, key),
        "elements": sec["elements"],
        "subtree": _subtree(sections, idx_by_key, key, sec["level"]),
        "footnotes": sec.get("footnotes", []),
        "cites_out": [{"key": kk, "title": bundle.sections_by_key.get(kk, {}).get("title", "")}
                      for kk in sec.get("cites_out", [])],
        "cited_by": [{"key": kk, "title": bundle.sections_by_key.get(kk, {}).get("title", "")}
                     for kk in sorted(set(content["cited_by"].get(key, [])))],
        "guidance": content["answers_by_clause"].get(key, []),
        "alerts": content["alerts_by_clause"].get(key, []),
    })


@app.get("/api/document")
def document(jurisdiction: str = Query(...),
             user: dict = Depends(require_access)) -> JSONResponse:
    """Full document for a jurisdiction: all sections in document order, for the
    right-pane 'Full document' viewer (anchored, scroll-to + highlight by key)."""
    bundle = _bundle_or_404(jurisdiction)
    return JSONResponse({
        "jurisdiction": jurisdiction,
        "title": bundle.content.get("doc", {}).get("title") or jurisdiction,
        "sections": [{"key": s["key"], "title": s["title"], "level": s["level"],
                      "elements": s["elements"], "footnotes": s.get("footnotes", [])}
                     for s in bundle.content["sections"]],
    })


@app.get("/api/relevance")
def relevance(jurisdiction: str = Query(...), key: str = Query(...),
              q: str = Query(..., max_length=_Q_MAX),
              user: dict = Depends(require_access)) -> JSONResponse:
    """Why this clause ranked: score each element of the clause against the query
    with the SAME cross-encoder that produced the search ranking, so the UI can
    highlight the model's actual evidence (not literal keyword matches)."""
    import re

    from aosphere_core_index.embeddings.reranker import crossencoder_scores
    from aosphere_core_index.embeddings.reranker import enabled as rr_enabled

    if not rr_enabled():
        return JSONResponse({"enabled": False, "passages": []})
    bundle = _bundle_or_404(jurisdiction)
    sec = bundle.sections_by_key.get(key) or {}
    cands = [(i, re.sub(r"\[\^\d+\]", "", e.get("text", "")).strip())
             for i, e in enumerate(sec.get("elements", []))]
    cands = [(i, t) for i, t in cands if t]
    if not cands:
        return JSONResponse({"enabled": True, "key": key, "passages": []})
    # Highlighting needs per-passage RELEVANCE (separable logits), so it always uses the
    # cross-encoder — independent of the search reranker backend (whose cohere/LLM rank
    # scores carry no relevance threshold).
    scores = crossencoder_scores(q, [t for _, t in cands])
    passages = [{"index": i, "score": round(float(s), 2)} for (i, _), s in zip(cands, scores)]
    return JSONResponse({"enabled": True, "key": key, "passages": passages})


_LINKS_CACHE: dict = {}


@app.get("/api/links")
def links(jurisdiction: str = Query(...),
          user: dict = Depends(require_access)) -> JSONResponse:
    """Index of guidance + alert -> section linkages for a jurisdiction, so the
    mappings can be eyeballed/tested. Each link carries a cross-encoder `match`
    score = how relevant the guidance/alert text is to the section it's linked to
    (a QA signal: low/negative = a suspect link). Full text returned for the popup;
    alerts mapped to no clause are listed as unmapped. Cached per jurisdiction."""
    if jurisdiction in _LINKS_CACHE:
        return JSONResponse(_LINKS_CACHE[jurisdiction])

    bundle = _bundle_or_404(jurisdiction)
    content = bundle.content
    secmap = bundle.sections_by_key

    # Guidance is linked by EXPLICIT citation (the rated answer cites the clause), not
    # a semantic match — so it carries no match score, just its rating. Alerts ARE
    # semantically mapped, so their stored cosine (0-1) is the real link confidence.
    guidance = sorted(
        ({"key": k, "clause_title": secmap.get(k, {}).get("title", ""),
          "subject": g.get("subject", ""), "question": g.get("question", ""),
          "color": g.get("color", ""), "answer": g.get("answer", "") or ""}
         for k, gs in content.get("answers_by_clause", {}).items() for g in gs),
        key=lambda r: r["key"])
    alerts = [{"key": k, "clause_title": secmap.get(k, {}).get("title", ""),
               "alert": a.get("title", ""), "impact": a.get("impact", ""),
               "summary": a.get("summary", "") or "", "score": a.get("score")}
              for k, as_ in content.get("alerts_by_clause", {}).items() for a in as_]
    alerts.sort(key=lambda r: (r["score"] is not None, r["score"] if r["score"] is not None else 1))
    mapped = {a["alert"] for a in alerts}
    unmapped = [{"alert": a.get("title", ""), "impact": a.get("impact", ""),
                 "summary": a.get("summary", "") or ""}
                for a in content.get("alerts", []) if a.get("title", "") not in mapped]
    out = {
        "jurisdiction": jurisdiction, "guidance": guidance, "alerts": alerts,
        "unmapped_alerts": unmapped,
        "counts": {"guidance": len(guidance), "alerts_mapped": len(alerts),
                   "alerts_total": len(content.get("alerts", [])), "alerts_unmapped": len(unmapped)},
    }
    _LINKS_CACHE[jurisdiction] = out
    return JSONResponse(out)
