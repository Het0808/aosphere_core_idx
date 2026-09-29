"""Per-region in-memory bundle for the search service.

Lazily builds and caches, per region: the extracted doc, the content model
(sections + links + semantically-mapped alerts), and the section vector index.
Loaded once on first request; reused thereafter.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

log = logging.getLogger(__name__)

from aosphere_core_index.embeddings.embedder import Embedder, make_embedder
from aosphere_core_index.embeddings.vector_backend import retrieve
from aosphere_core_index.embeddings.multi_index import (
    MultiIndex,
    build_multi,
    index_model,
    load_multi,
    regions_with_index,
    save_multi,
)
from aosphere_core_index.embeddings.section_index import (
    SectionIndex,
    build_section_index,
    extend_index,
    load_index,
    save_index,
)
from aosphere_core_index.extract.model import ExtractedDoc
from aosphere_core_index.extract import extract_document
from aosphere_core_index.ingest.alerts import gather_region_alerts
from aosphere_core_index.ingest.source import fetch_jurisdiction
from aosphere_core_index.mapping.semantic_mapper import map_alerts_semantic, map_guidance_semantic
from aosphere_core_index.navigator.content_export import (
    alert_rows, build_content, guidance_rows, unlinked_guidance_rows,
)
from aosphere_core_index.config import settings

from collections import OrderedDict

_EMBEDDER: Embedder | None = None
# LRU-bounded so a long-lived pod doesn't accumulate every queried region's content.json.
# MEASURED, not assumed: one k=40 search touching 19 regions moved the container from 133MB
# to 554MB — about 22MB per bundle as Python objects, not the 1-2MB this comment used to
# claim. The corpus grew (14.5M words) and the cap did not, so 64 bundles is ~1.4GB of
# resident content; with OpenSearch at 2.7GB and a reindex holding the 320MB vector matrix,
# the local stack OOM-killed this process. 24 keeps the cache under ~550MB, still far more
# than any single query needs (a broad search touches ~20 regions) and every eviction just
# reloads from disk. Unbounded when ACI_WARM_BUNDLES=1 (that mode opts into holding them all).
_BUNDLE_MAX = 0 if os.getenv("ACI_WARM_BUNDLES", "0") == "1" else int(os.getenv("ACI_BUNDLE_CACHE_MAX", "24"))
_BUNDLES: "OrderedDict[str, RegionBundle]" = OrderedDict()
_MULTI: MultiIndex | None = None
# Runs LLM query expansion concurrently with the raw embed+retrieve (dual-query
# retrieval) so expansion adds ~no wall-clock. Threads suit the I/O-bound Bedrock call;
# concurrent search requests each get a worker.
_EXPAND_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.getenv("ACI_EXPAND_WORKERS", "8")), thread_name_prefix="qexpand")


def embedder() -> Embedder:
    """Query encoder. Offline (the deployed pod), we MATCH the published index's model
    so the query vectors can't mismatch the index — regardless of ACI_EMBED_BACKEND."""
    global _EMBEDDER
    if _EMBEDDER is None:
        hint = index_model() if bool(os.getenv("ACI_OFFLINE")) else None
        _EMBEDDER = make_embedder(hint)
    return _EMBEDDER


def get_multi() -> MultiIndex:
    """Flat cross-region index. Loads the cached build. Offline (read-only /data mount)
    the published index is authoritative — never rebuild/write. Only a writable,
    non-offline environment rebuilds when the model changed.

    Under an EXTERNAL vector backend (opensearch/atlas) the vectors are served from the
    store and retrieve() ignores the in-memory matrix, so we load METADATA-ONLY and leave
    the ~0.5GB matrix on disk — a big RAM win on the serving pod. The admin paths that do
    need the vectors (reindex, eval-divergence) go through multi_with_matrix()."""
    global _MULTI
    if _MULTI is not None:
        return _MULTI
    from aosphere_core_index.embeddings.vector_backend import backend

    lean = backend() != "memory"
    mi = load_multi(with_matrix=not lean)
    if mi is None or (not bool(os.getenv("ACI_OFFLINE")) and mi.model != embedder().name):
        mi = build_multi(regions_with_index(), embedder().name)
        try:
            save_multi(mi)
        except OSError as e:  # read-only mount: keep the in-memory index, don't crash
            log.warning("multi-index not persisted (%s); using in-memory build", e)
    elif lean:
        log.info("multi-index: metadata-lean load (%d regions, %d rows); matrix + per-row "
                 "metadata left on disk for backend=%s", len(mi.regions), mi.n_rows, backend())
    _MULTI = mi
    return mi


def multi_with_matrix() -> MultiIndex:
    """Full index WITH vectors, for the admin paths that push to / compare against the
    external store (reindex, eval-divergence). Reuses the cached index when it already
    holds the matrix (memory backend); otherwise loads a fresh full copy from disk so the
    lean served singleton stays matrix-free."""
    mi = get_multi()
    if getattr(mi, "matrix", None) is not None and mi.matrix.size:
        return mi
    full = load_multi(with_matrix=True)
    return full if full is not None else mi


_LEXICON: set[str] | None = None


def did_you_mean(query: str) -> str | None:
    """Typo suggestion for the UI ("did you mean …?"). Suggest-only: retrieval
    always runs on the raw query (plus expansion); this just tells the user.
    Lexicon = section titles + clause body text from the published content.json
    artifacts (same files the bundles serve, so it works offline and tracks
    rebuilds) + jurisdiction names + alias vocabulary. Built once, lazily."""
    from aosphere_core_index.embeddings.did_you_mean import build_lexicon, suggest
    from aosphere_core_index.regions.aliases import ALIASES
    from aosphere_core_index.regions.region_map import split_region

    global _LEXICON
    if _LEXICON is None:
        titles, bodies = [], []
        for region in regions_with_index():
            try:
                with open(settings.region_artifacts(region) / f"{region}.content.json",
                          encoding="utf-8") as f:
                    content = json.load(f)
            except OSError:
                continue
            titles.append(split_region(region)[1])
            for s in content.get("sections", []):
                if s.get("title"):
                    titles.append(s["title"])
                bodies.extend(e.get("text", "") for e in s.get("elements", []))
        extra = list(ALIASES) + list(ALIASES.values())
        _LEXICON = build_lexicon(titles, extra, bodies)
        log.info("did_you_mean lexicon: %d words", len(_LEXICON.words))
    try:
        return suggest(query, _LEXICON)
    except Exception:  # noqa: BLE001 — a suggestion must never break search
        log.exception("did_you_mean failed")
        return None


def _query_jurisdictions(query: str, jurisdictions) -> set[str]:
    """Jurisdictions explicitly named in the query text — jurisdiction names,
    US-state names, and aliases (cities, regulators, colloquial names). Matches
    product-qualified identities on their bare jurisdiction part, so
    'Shareholding Disclosure — United Kingdom' is found by "UK" too."""
    from aosphere_core_index.regions.aliases import named_jurisdictions

    return named_jurisdictions(query, jurisdictions)


def search_all(query: str, k: int = 80, jurisdictions: list[str] | None = None) -> list[dict]:
    """Cross-jurisdiction semantic search with cross-encoder reranking.

    Retrieve top-k by bi-encoder cosine, then rerank with a cross-encoder so
    `score` is a relevance score that separates around 0 (generalizes across
    queries/regions). Each hit carries region, breadcrumb, snippet, and cosine.
    """
    from aosphere_core_index.embeddings.reranker import enabled as rr_enabled
    from aosphere_core_index.embeddings.reranker import rerank, score_scale
    from aosphere_core_index.regions.region_map import region_for, split_region

    mi = get_multi()
    emb = embedder()
    # Dual-query retrieval: kick off LLM query expansion CONCURRENTLY with the raw
    # embed+retrieve, then UNION the two candidate pools. The expanded query surfaces
    # clauses the raw query misses on vocabulary (jargon -> formal statutory terms); the
    # union never drops a raw hit (no dilution). Async so expansion overlaps raw retrieval
    # and adds ~no wall-clock. Off unless ACI_QUERY_EXPAND=1.
    from aosphere_core_index.embeddings import query_expand
    from time import perf_counter as _pc
    _t0 = _pc()
    exp_future = (_EXPAND_EXECUTOR.submit(query_expand.expand, query)
                  if query_expand.enabled() else None)
    qvec = emb.embed([query])[0]
    _t_embed = _pc()
    # A single item can be represented by several rows now — a clause plus one row
    # per curated-guidance entry, and an alert plus one row per attachment chunk.
    # Fetch extra, dedupe to the best-scoring row per identity, then trim to k
    # (mi.search is score-descending). For alerts the identity is the base alert id
    # (strip the #chunk suffix) so all rows of one alert collapse to one result.
    def _ident(h: dict) -> tuple:
        # Strip the chunk/attachment suffix so all rows of one item collapse to a single
        # result: `#c<i>` = an oversized-section window (see section_index._windows),
        # `#a<i>` = an alert-attachment chunk. Both share the base key's identity.
        return (h["jurisdiction"], h["key"].split("#", 1)[0])

    def _dedupe(rows: list[dict]) -> list[dict]:
        seen_kk, out = set(), []
        for h in rows:
            i = _ident(h)
            if i not in seen_kk:
                seen_kk.add(i)
                out.append(h)
        return out
    # Per-region floor: on a scoped query ("compare X across these jurisdictions")
    # a single global pool lets one strong region crowd the rest out before the
    # reranker sees them. Guarantee each scoped region its best few rows. Bounded:
    # applied only up to a scope size cap so the rerank bill stays predictable
    # (worst case ~= k + floor * scope regions).
    floor = int(os.getenv("ACI_REGION_FLOOR", "3"))
    floor_max_scope = int(os.getenv("ACI_REGION_FLOOR_MAX_SCOPE", "60"))
    use_floor = bool(jurisdictions) and 0 < len(jurisdictions) <= floor_max_scope and floor > 0
    per_region = floor if use_floor else 0
    raw_rows = retrieve(mi, qvec, k * 2, jurisdictions=jurisdictions, per_region=per_region)
    rows = raw_rows
    if exp_future is not None:
        try:
            expanded = exp_future.result(timeout=float(os.getenv("ACI_EXPAND_TIMEOUT", "10")))
        except Exception:  # noqa: BLE001 — timeout/failure -> raw-only, never block search
            expanded = None
        if expanded and expanded.strip().lower() != query.strip().lower():
            evec = emb.embed([expanded])[0]
            exp_rows = retrieve(mi, evec, k * 2, jurisdictions=jurisdictions, per_region=per_region)
            # Union both passes; keep the best (max) score per identity. _dedupe keeps the
            # first occurrence per identity, so sort by score descending first.
            rows = sorted(raw_rows + exp_rows, key=lambda r: r["score"], reverse=True)
    pool = _dedupe(rows)
    if use_floor:
        from collections import Counter
        hits = pool[:k]
        have = Counter(h["jurisdiction"] for h in hits)
        for h in pool[k:]:  # keep sub-top-k rows of under-represented regions
            if have[h["jurisdiction"]] < floor:
                hits.append(h)
                have[h["jurisdiction"]] += 1
    else:
        hits = pool[:k]
    # If the query names a jurisdiction, also pull that jurisdiction's own top-k and
    # merge it in — otherwise its clauses may miss the global candidate pool entirely
    # and never get the chance to rank top.
    named = _query_jurisdictions(query, mi.regions)
    if jurisdictions:
        named &= set(jurisdictions)
    if named:
        seen = {_ident(h) for h in hits}
        for e in retrieve(mi, qvec, k, jurisdictions=sorted(named)):
            i = _ident(e)
            if i not in seen:
                seen.add(i)
                hits.append(e)
    _t_retr = _pc()
    dropped: dict[str, str] = {}
    kept = []
    for h in hits:
        # A VECTOR STORE AND A DATA MOUNT DRIFT. They are loaded by different steps of a
        # deploy (an index publish, a pointer flip, an in-pod reindex), so during any
        # cutover the store can hold a region the mount no longer has — after this index
        # moved to PDF-only extraction, "European Union", "Hong Kong" and "Turkey" were
        # renamed and "United States - States and Territories" dropped, and every query
        # touching one of them returned 500 for EVERY user. One stale region must cost its
        # own hit, not the whole result set.
        try:
            bundle = get_bundle(h["jurisdiction"])
        except BundleUnavailable as e:
            dropped.setdefault(h["jurisdiction"], str(e))
            continue
        kept.append(h)
        h["region"] = region_for(h["jurisdiction"])
        h["cosine"] = round(h["score"], 3)
        h.setdefault("kind", "clause")
        if h["kind"] == "alert":
            # Alert hit: enrich from the alert store, not the clause tree. Collapse to
            # the base alert key and expose the alert payload for the UI / agent.
            base = h["key"].split("#", 1)[0]
            h["key"] = base
            aid = base.split(":", 1)[1] if ":" in base else base
            rec = bundle.content.get("alerts_by_id", {}).get(aid, {})
            att = rec.get("attachment_text", "") or ""
            h["title"] = rec.get("title", h.get("title", ""))
            h["path"] = f"🔔 {rec.get('impact', '')} alert · {rec.get('date', '')}".strip()
            h["snippet"] = (rec.get("summary", "") or "")[:220]
            # Bounded, UI-ready payload (avoid shipping full attachment text per hit).
            h["alert"] = {
                "id": rec.get("id", aid), "impact": rec.get("impact", ""),
                "date": rec.get("date", ""), "summary": rec.get("summary", ""),
                "attachment": att[:4000], "has_attachment": bool(att),
                "mapped": rec.get("mapped", [])[:3],
            }
            h["_doc"] = (f"{h['jurisdiction']}. Alert: {h['title']}. "
                         f"{rec.get('summary', '')} {att[:400]}").strip()
            continue
        if h["kind"] == "guidance" and h["key"].startswith("GUID:"):
            # Standalone (unlinked) curated guidance: enrich from guidance_by_id,
            # not the clause tree.
            gid = h["key"].split(":", 1)[1]
            rec = bundle.content.get("guidance_by_id", {}).get(gid, {})
            h["title"] = rec.get("question") or rec.get("subject") or h.get("title", "")
            h["path"] = f"💡 curated guidance · {rec.get('subject', '')}".strip(" ·")
            h["snippet"] = (rec.get("answer", "") or "")[:220]
            h["guidance"] = {"id": gid, "subject": rec.get("subject", ""),
                             "question": rec.get("question", ""),
                             "color": rec.get("color", ""),
                             "answer": (rec.get("answer", "") or "")[:2000]}
            h["_doc"] = (f"{h['jurisdiction']}. Guidance: {rec.get('subject', '')}. "
                         f"{rec.get('question', '')} {(rec.get('answer', '') or '')[:600]}").strip()
            continue
        # Collapse an oversized-section window (`<key>#c<i>`) back to its clause so the
        # breadcrumb / snippet / guidance lookups (all keyed by the bare clause) resolve.
        h["key"] = h["key"].split("#", 1)[0]
        sec = bundle.sections_by_key.get(h["key"], {})
        h["path"] = _breadcrumb(bundle, h["key"])
        h["snippet"] = _snippet(sec, jurisdiction=h["jurisdiction"])
        # Rerank text: clause title + answer, NOT the full breadcrumb. A deep
        # breadcrumb (e.g. "E › E1 › E1.1 › E1.1(b) › E1.1(b)(ii) Contract")
        # pushes the answer back and adds noise the cross-encoder penalises —
        # the same clause scores -0.1 with the path vs 3.4 with just the title.
        # Include the curated guidance — subject + QUESTION + answer — so guidance-
        # matched clauses rank. The question matters: a user query that IS a guidance
        # question ("Is there a test for the law to apply…?") only matches if the
        # reranker sees that question text; answer-only left such queries scoring
        # negative and filtered out by the min-score threshold.
        guid = " ".join(
            f"{a.get('subject', '')} {a.get('question', '')} {a.get('answer', '')}".strip()
            for a in bundle.content["answers_by_clause"].get(h["key"], [])
        )
        # Prepend the jurisdiction (metadata) so the cross-encoder can match a query
        # that names it ("...for Germany") to the doc — making the reranker itself
        # rank the named jurisdiction high. Generalises to other metadata (product,
        # dates) the same way: include it here and the reranker matches it.
        h["_doc"] = (f"{h['jurisdiction']}. {sec.get('title', '')}. "
                     f"{_snippet(sec, 500, jurisdiction=h['jurisdiction'])} {guid[:600]}").strip()

    if dropped:
        # WARNING, not debug: this means the vector store is describing content the mount
        # does not have, which is a real deployment problem — it just is not a reason to
        # fail the request. Named so the next step is obvious (reindex, or republish).
        log.warning("search: dropped %d hit-region(s) with no readable content — %s",
                    len(dropped), "; ".join(f"{r}: {m}" for r, m in list(dropped.items())[:5]))
    hits = kept

    # Curated-content bonus: a hit whose best-matching row was a GUIDANCE row
    # (a lawyer-curated Q&A matched the query, not just clause prose) is more
    # trustworthy than a cosine-similar clause — nudge it up. Additive on the
    # rerank logit (or cosine when reranking is off), not a hard tier.
    # Additive nudge for curated-guidance hits. Its size must match the reranker's
    # score scale: ~1.0 is "about one relevance tier" on minilm logits, but on cohere's
    # [0,1] scale that would swamp everything — ~0.1 is the equivalent nudge there.
    _t_enrich = _pc()
    guid_bonus = float(os.getenv("ACI_GUIDANCE_BONUS", "0.1" if score_scale() == "unit" else "1.0"))
    if rr_enabled() and hits:
        for h, s in zip(hits, rerank(query, [h["_doc"] for h in hits])):
            h["score"] = round(s + (guid_bonus if h.get("kind") == "guidance" else 0.0), 2)
    else:
        for h in hits:
            h["score"] = h["cosine"] + (guid_bonus * 0.05 if h.get("kind") == "guidance" else 0.0)
    _t_rr = _pc()
    log.info("search timing: embed=%.0f retrieve=%.0f enrich=%.0f rerank=%.0f total=%.0f ms "
             "(n=%d rerank=%s)", (_t_embed - _t0) * 1e3, (_t_retr - _t_embed) * 1e3,
             (_t_enrich - _t_retr) * 1e3, (_t_rr - _t_enrich) * 1e3, (_t_rr - _t0) * 1e3,
             len(hits), rr_enabled())
    # The reranker (with jurisdiction in the doc) already lifts a named jurisdiction;
    # tiering then GUARANTEES the named-jurisdiction (metadata-match) group ranks above
    # all others — a true override of content scores (an additive/multiplicative boost
    # on signed cross-encoder logits is unreliable). `boosted` lets the UI flag them.
    for h in hits:
        h["boosted"] = h["jurisdiction"] in named
    hits.sort(key=lambda h: (not h["boosted"], -h["score"]))
    # Tag each hit with its product + clean jurisdiction name (derived from the
    # qualified identity). `jurisdiction` stays the identity used by /api/section,
    # /api/document, /api/links and agent reads.
    for h in hits:
        h.pop("_doc", None)
        h["product"], h["jurisdiction_name"] = split_region(h["jurisdiction"])
    if log.isEnabledFor(logging.DEBUG):
        scope = f"{len(jurisdictions)} jurisdictions" if jurisdictions else "all regions"
        top = ", ".join(f"{h['jurisdiction']}/{h['key']}={h['score']}" for h in hits[:5])
        log.debug("search %r [%s] rerank=%s -> %d hits; top: %s",
                  query, scope, rr_enabled(), len(hits), top)
    return hits


def _breadcrumb(bundle: "RegionBundle", key: str) -> str:
    parts, cur = [], bundle.sections_by_key.get(key)
    while cur is not None:
        parts.append(f"{cur['key']} {cur['title']}")
        cur = bundle.sections_by_key.get(cur.get("parent_key")) if cur.get("parent_key") else None
    return " › ".join(reversed(parts))


def _snippet(sec: dict, n: int = 220, jurisdiction: str | None = None) -> str:
    """The clause's answer content (body/bullets/tables; template question dropped),
    footnotes stripped. For a US state, big shared comparison tables are reduced to
    that state's row(s) so the snippet/rerank reflect THIS state — matching the embed
    text in section_index.answer_text."""
    from aosphere_core_index.embeddings.section_index import answer_text_dict

    return answer_text_dict(sec.get("elements", []), jurisdiction)[:n]


@dataclass
class RegionBundle:
    region: str
    doc: ExtractedDoc
    content: dict          # from build_content: sections, cited_by, answers_by_clause, alerts_by_clause
    index: SectionIndex
    sections_by_key: dict  # key -> section dict (content)

    def search(self, query: str, k: int = 8):
        qvec = embedder().embed([query])[0]
        return self.index.search(qvec, k)  # [(key, title, level, score)]


def _build_index(region: str, doc: ExtractedDoc,
                 guidance: list[tuple[str, str]] | None = None,
                 extra_rows: list[tuple[str, str, int, str, str]] | None = None,
                 rated_answers: list[dict] | None = None) -> SectionIndex:
    """Build (and cache) the per-region index.

    Two phases so curated guidance is always retrievable, whatever its reference
    style: (1) build the base clause index folding in REFERENCE-resolved guidance
    (`guidance`); (2) for guidance whose references DON'T resolve to a clause (e.g.
    Shareholding Disclosure, page-link refs), semantically map each Q&A to its
    nearest clause and append it as a guidance row — plus any `extra_rows` (alerts).
    Phase 2 embeds only the new rows (clause vectors are reused, not recomputed)."""
    path = settings.region_artifacts(region) / f"{region}.sections.npz"
    emb = embedder()
    if path.exists():
        idx = load_index(path)
        if idx.model == emb.name and idx.keys:
            return idx
    base = build_section_index(doc, emb, guidance)  # clauses + linked guidance
    add: list[tuple[str, str, int, str, str]] = list(extra_rows or [])
    if rated_answers:
        # Guidance placement precedence — each entry indexed exactly ONCE:
        #   1. explicit clause refs / structural template-subject-question link
        #      (already in `guidance` rows above);
        #   2. semantic fallback: nearest clause by embedding (min-sim gated);
        #   3. standalone GUID:<id> row — unplaceable guidance is still expert
        #      content and stays searchable + readable by the agent.
        keys = {s.key for s in doc.sections}
        mapped: set[str] = set()
        try:
            sem_rows, mapped = map_guidance_semantic(
                rated_answers, keys, base.filter_kinds({"guidance"}), emb,
                sections=doc.sections)
            add += sem_rows
        except Exception as e:  # noqa: BLE001 — mapping is best-effort
            log.warning("semantic guidance mapping failed for %s (%s)", region, e)
        add += unlinked_guidance_rows(rated_answers, keys, doc.sections,
                                      exclude_ids=mapped)
    idx = extend_index(base, emb, add)
    save_index(idx, path)
    return idx


OFFLINE = bool(os.getenv("ACI_OFFLINE"))


def _content_path(region: str):
    return settings.region_artifacts(region) / f"{region}.content.json"


def export_content(region: str) -> None:
    """Pre-serialize a region's content bundle so the service can run offline
    (no S3 / no docx parsing at runtime). Baked into the container image."""
    bundle = _build_bundle(region)
    _content_path(region).write_text(json.dumps(bundle.content, separators=(",", ":")))


class BundleUnavailable(Exception):
    """A region's content bundle could not be read (missing, unreadable, corrupt)."""


def _bundle_from_content(region: str) -> RegionBundle:
    # encoding="utf-8" is explicit on purpose. read_text() with no encoding uses the
    # process locale, and these files are full of non-ASCII — jurisdiction names
    # (Türkiye, Curaçao), em-dashes, curly quotes — so a container started with a
    # non-UTF-8 locale decodes them into UnicodeDecodeError. The data is UTF-8
    # regardless of who runs the process, so say so.
    path = _content_path(region)
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # Name the region AND the path. The traceback for this used to end at io.open,
        # which says nothing about WHICH jurisdiction the vector store asked for.
        raise BundleUnavailable(f"{region}: cannot load {path} ({type(e).__name__}: {e})") from e
    return RegionBundle(region=region, doc=None, content=content, index=None,
                        sections_by_key={s["key"]: s for s in content["sections"]})


def get_bundle(region: str) -> RegionBundle:
    b = _BUNDLES.get(region)
    if b is not None:
        _BUNDLES.move_to_end(region)   # mark most-recently-used
        return b
    bundle = _bundle_from_content(region) if OFFLINE else _build_bundle(region)
    _BUNDLES[region] = bundle
    if _BUNDLE_MAX and len(_BUNDLES) > _BUNDLE_MAX:
        _BUNDLES.popitem(last=False)   # evict least-recently-used
    return bundle


def _build_bundle(region: str) -> RegionBundle:
    src = fetch_jurisdiction(region)
    docx_docs = [d for d in src.docs
                 if str(d.get("EXTENSION", "")).lower() == "docx" and d.get("local_path")]
    if not docx_docs:
        raise ValueError(f"No DOCX source for region {region!r}")
    doc = extract_document(
        docx_docs[0]["local_path"], doc_meta=docx_docs[0], source_key=docx_docs[0]["source_key"])
    # Fold curated guidance into the clause embeddings so guidance-matched clauses
    # are retrievable (guidance is otherwise attached only after indexing).
    # Passing sections enables the structural linker for products whose
    # REFERENCETEXT carries no clause refs (Shareholding Disclosure).
    guid = guidance_rows(src.rated_answers, {s.key for s in doc.sections}, doc.sections)
    # Alerts are optional at build time (separate bucket, refreshed independently).
    # If they can't be gathered, build without them rather than failing the bundle.
    try:
        alerts = gather_region_alerts(region)
    except Exception as e:  # noqa: BLE001 — S3 access / offline / missing feed
        log.warning("alerts unavailable for %s (%s); building without alerts", region, e)
        alerts = []
    # Alert rows (summary + attachment chunks) are indexed as their own searchable
    # rows (kind "alert"), distinct from clauses.
    arows = alert_rows(alerts)
    # Index clauses + guidance (reference-resolved AND, for page-link-referenced
    # products like Shareholding Disclosure, semantically mapped) + alert rows.
    index = _build_index(region, doc, guid, extra_rows=arows, rated_answers=src.rated_answers)
    # Map alerts -> clauses for the Links drawer, against CLAUSE rows only.
    try:
        mappings = map_alerts_semantic(alerts, index.filter_kinds({"alert", "guidance"}), embedder()) if alerts else []
    except Exception as e:  # noqa: BLE001
        log.warning("alert mapping failed for %s (%s)", region, e)
        mappings = []
    content = build_content(doc, src, alert_mappings=mappings, alerts_raw=alerts)
    # Extraction invariants: a structurally-corrupt extraction (e.g. shifted part
    # letters) must be LOUD at build time, not discovered via bad search results.
    from aosphere_core_index.extract.validate import validate_sections
    errors, warns = validate_sections(content["sections"], doc.product, doc.jurisdiction)
    for e in errors:
        log.error("extraction invariant violated — %s", e)
    for w in warns:
        log.warning("extraction suspicious — %s", w)
    return RegionBundle(
        region=region, doc=doc, content=content, index=index,
        sections_by_key={s["key"]: s for s in content["sections"]},
    )
