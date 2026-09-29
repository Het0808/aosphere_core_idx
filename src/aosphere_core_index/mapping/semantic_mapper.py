"""Semantic alert -> section mapping using embeddings.

Drop-in alternative to the lexical mapper: embeds each alert's text and ranks
sections by cosine similarity against the cached section index. Returns the same
AlertMapping shape so the reader/report consume it unchanged.
"""

from __future__ import annotations

import html
import re

from aosphere_core_index.embeddings.embedder import Embedder
from aosphere_core_index.embeddings.section_index import SectionIndex
from aosphere_core_index.mapping.alert_mapper import AlertMapping

_TAG = re.compile(r"<[^>]+>")
TOPK = 3          # keep only an alert's strongest few sections
MIN_SIM = 0.55    # absolute floor on cosine similarity (bge)
MARGIN = 0.05     # drop matches more than this below the alert's best score


def _clean(s: str | None) -> str:
    return html.unescape(_TAG.sub(" ", s or "")).strip()


# Curated-guidance clause references (same pattern build_content uses).
_CLAUSE_RE = re.compile(r"\b[A-K]\d+(?:\.\d+)*(?:\([a-z]+\))*")
GUID_MIN_SIM = 0.45  # floor for mapping a guidance Q&A to a clause


def map_guidance_semantic(
    rated_answers: list[dict], doc_keys: set[str], index: SectionIndex, embedder: Embedder,
    min_sim: float = GUID_MIN_SIM, topk: int = 1, sections=None,
) -> tuple[list[tuple[str, str, int, str, str]], set[str]]:
    """Guidance rows for entries NO other linker could place. Each such Q&A is
    embedded and mapped to its nearest clause section(s); returns self-described
    (key, title, level, text, "guidance") rows plus the guidance ids that mapped.

    Precedence: entries resolved by explicit clause refs OR by the structural
    template/subject/question linker (when `sections` is given) are skipped — those
    paths already index them; this is the semantic FALLBACK, and the returned
    mapped-id set lets the caller index only the true remainder as standalone rows."""
    from aosphere_core_index.navigator.content_export import (
        GuidanceLinker, _guidance_id, guidance_refs,
    )

    linker = GuidanceLinker(sections) if sections is not None else None
    blobs: list[tuple[str, str]] = []  # (guidance_id, text)
    for i, r in enumerate(rated_answers or []):
        if linker is not None:
            if guidance_refs(r, doc_keys, linker):
                continue  # explicitly or structurally linked already
        elif set(_CLAUSE_RE.findall(_clean(r.get("REFERENCETEXT", "")))) & doc_keys:
            continue  # ref-based guidance already covers this entry
        answer = " ".join(x for x in [_clean(r.get("SETANSWEROTHER")), _clean(r.get("SETCOMMENTS"))] if x)
        blob = " ".join(x for x in [r.get("SUBJECTNAME", ""), r.get("QUESTIONTITLE", ""), answer] if x).strip()
        if blob:
            blobs.append((_guidance_id(r, i), blob))
    if not blobs:
        return [], set()
    rows, mapped = [], set()
    for (gid, blob), vec in zip(blobs, embedder.embed([b for _, b in blobs])):
        for key, title, level, score in index.search(vec, max(topk * 3, 8))[:topk]:
            if score >= min_sim:
                rows.append((key, title, level, blob, "guidance"))
                mapped.add(gid)
    return rows, mapped


def map_alerts_semantic(
    alerts: list[dict], index: SectionIndex, embedder: Embedder,
    topk: int = TOPK, min_sim: float = MIN_SIM, margin: float = MARGIN,
) -> list[AlertMapping]:
    texts = [f"{_clean(a.get('TITLE'))}. {_clean(a.get('SUMMARY'))}" for a in alerts]
    vecs = embedder.embed(texts)
    results = []
    for a, v in zip(alerts, vecs):
        ranked = index.search(v, max(topk * 3, 8))
        best = ranked[0][3] if ranked else 0.0
        matches = [
            (key, title, round(score, 3))
            for key, title, _lvl, score in ranked
            if score >= min_sim and score >= best - margin
        ][:topk]
        results.append(
            AlertMapping(
                alert_id=str(a.get("ALERTID")),
                title=_clean(a.get("TITLE")),
                impact=a.get("IMPACT", ""),
                publication_date=str(a.get("PUBLICATIONDATE", ""))[:10],
                summary=_clean(a.get("SUMMARY")),
                matches=matches,
            )
        )
    return results
