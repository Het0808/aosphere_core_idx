#!/usr/bin/env python3
"""lib_gallery — the Doc Gallery's naming rules, importable without a PDF stack.

Split out of `push_hybrid_s3.py` so a PROMOTION can resolve the gallery prefix and
build a slug without dragging in the extraction toolchain. `push_hybrid_s3` imports
`hybrid_extract` at module scope, which imports `pdf2mdtree` (which `sys.exit`s when
pymupdf is missing), `fitz`, `lxml` and MinerU — several gigabytes of model tooling a
server-side S3 copy has no use for and a slim image does not carry.

Everything here is stdlib plus, at most, a lazy boto3 import inside one function. The
reason it lives in ONE module rather than being reimplemented on the promotion side is
that these two rules are the ones whose drift is invisible until the gallery tab is
blank: a writer that publishes to a prefix the reader does not read, or a slug that
does not match the one the manifest carries, both fail silently and identically.

Three resolvers must agree on the prefix and are pinned to each other by
tests/test_gallery_prefix.py:

    scripts/lib_gallery.gallery_prefix()                     the publisher (and promotion)
    service/doc_gallery._gallery_prefix()                    the reader
    service/promotion_monitor.promotion_target()             the promotion's target
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

BUCKET = "aosphere-tenant-dev1-core-index"
LEGACY_PREFIX = "doc-gallery"
REGION = "eu-west-1"
PROFILE = "dev1"


def gallery_prefix(s3=None) -> str:
    """Where this gallery is published: index/<version>/doc-gallery.

    The gallery belongs TO an index version. A single flat "doc-gallery/" prefix is one
    mutable copy shared by every version, so flipping the index pointer left the scorecards
    describing the PREVIOUS extraction, and rolling the index back rolled the gallery
    forward. Nesting under the version keeps the pair together.

    Resolved lazily, never at import: run_corpus imports this module for the LOCAL publish
    path and must not need S3 credentials to do it.
    """
    explicit = os.getenv("ACI_DOC_GALLERY_PREFIX", "").strip()
    if explicit:
        return explicit
    version = os.getenv("ACI_INDEX_VERSION", "").strip()
    if not version:
        try:
            import boto3
            cli = s3 or boto3.Session(profile_name=PROFILE, region_name=REGION).client("s3")
            version = cli.get_object(Bucket=BUCKET, Key="index/latest")["Body"].read().decode().strip()
        except Exception as e:  # noqa: BLE001
            print(f"⚠ could not read index/latest ({type(e).__name__}); "
                  f"publishing to the legacy {LEGACY_PREFIX!r} prefix")
            return LEGACY_PREFIX
    return f"index/{version}/doc-gallery"


def gslug(product: str, region: str, stem: str) -> str:
    """The manifest's URL key for one document.

    The "-mineru" suffix is LOAD-BEARING, not decoration: `web.py::galIsMineru` and
    `doc_gallery._MINERU_SUFFIX` both key off it to tell a 2-pass re-extraction apart from
    the original publish of the same document, and `_doc_row` uses it to group the variant
    under its bare jurisdiction so the two sit side by side. `region` here is the BARE
    jurisdiction — the " (MinerU)" display suffix is added to the manifest's `region`
    field, never to the slug's input, or the slug changes and the pair separates.
    """
    return re.sub(r"[^a-z0-9]+", "-", f"{product}-{region}-{stem}".lower()).strip("-") + "-mineru"


def doc_name_fields(doc_id: str, names: dict | None = None) -> dict:
    """The document's human name from the source corpus's Doc_metadata.json — the
    gallery shows a numeric id otherwise. Best-effort: an unknown id just keeps the id.

    `names` lets a caller that has already built an id -> record index (a promotion
    reading Doc_metadata.json out of S3, where the repo-local corpus roots do not exist)
    supply it instead of the on-disk lookup.
    """
    try:
        if names is not None:
            from lib_docnames import fields_from_record
            m = fields_from_record(names.get(str(doc_id)) or {})
        else:
            from lib_docnames import lookup
            m = lookup(doc_id)
    except Exception:  # noqa: BLE001 — a metadata miss must never sink a publish
        return {}
    return {k: v for k, v in (("doc_name", m.get("name")), ("doc_version", m.get("version")),
                              ("doc_date", m.get("date"))) if v}


# ---------------- rescue provenance ----------------
# Two tiers can rescue a document (scripts/fallback_chain.py) and a reader has to be able
# to tell them apart, because they are not equally trustworthy:
#
#     toc-outline   the outline was rebuilt from the document's own PRINTED table of
#                   contents and re-extracted (rescued_by_toc.json)
#     mineru-full   the whole document was re-parsed by a visual model, which took over
#                   the hierarchy as well (the scorecard's fallback chain)
#
# The document keeps its real id either way — a reader must not have to decode a suffix —
# so the provenance travels as a flag instead.
#
# Split in two because a PROMOTION has the scorecard in hand (one GET it already pays for)
# but would need a second GET for rescued_by_toc.json. The job-dir listing says whether
# that file exists, so the promotion fetches it only for the minority of documents that
# have one, and calls the two halves separately.


def rescue_from_toc(payload: dict | None) -> dict | None:
    """The toc-outline tier, from a parsed rescued_by_toc.json (None if there wasn't one)."""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return {"method": "toc-outline"}
    c = payload.get("check") or {}
    return {"method": "toc-outline", "engine": payload.get("engine"),
            "entries": c.get("entries"), "verified": c.get("verified"),
            "page_offset": c.get("offset"), "was": (payload.get("before") or {}).get("gate")}


def rescue_from_scorecard(sc: dict | None) -> dict | None:
    """The mineru-full tier, from a parsed scorecard.

    None for an ordinary extraction, and also when a fallback ran but nothing beat Stage 1:
    in that case the result IS the normal pipeline's and claiming a rescue would be a lie.
    """
    fb = (sc or {}).get("fallback") or {}
    if fb.get("adopted_tier") != "mineru_full":
        return None
    first = fb.get("first_attempt") or {}
    toc = next((c for c in (fb.get("chain") or []) if c.get("tier") == "toc_rescue"), {})
    return {"method": "mineru-full", "was": first.get("gate"),
            "was_score": first.get("worst_score"),
            "was_completeness": first.get("completeness_score"),
            "toc_rescue": toc.get("status")}


def rescue_flag(job_dir) -> dict | None:
    """How this document was recovered — the on-disk path, for a local job directory.

    toc-outline wins when both are present: it is the more specific claim about what
    produced the structure that shipped.
    """
    job_dir = Path(job_dir)
    f = job_dir / "rescued_by_toc.json"
    if f.exists():
        try:
            return rescue_from_toc(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            return {"method": "toc-outline"}
    try:
        sc = json.loads((job_dir / "scorecard.json").read_text()) or {}
    except (OSError, json.JSONDecodeError):
        return None
    return rescue_from_scorecard(sc)
