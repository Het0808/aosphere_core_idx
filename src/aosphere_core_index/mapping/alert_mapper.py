"""Map topical alerts to the document sections they most likely relate to.

Alerts are news items (e.g. "Hamburg DPA publishes cookie guidance"), so they
carry topics, not clause codes. This is a STEP-1 lexical stopgap: IDF-weighted
keyword overlap between an alert's text and each section's title path. Step 2
replaces this with Titan v2 semantic similarity, which will be materially better.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass

from aosphere_core_index.extract.model import ExtractedDoc

_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-z][a-z\-]{2,}")
_STOP = frozenset(
    """the and for that with this from are was has have not but they will would which
    data privacy personal eu member states law laws new publishes published rules ruling
    regulation regulations regulator authority commission court act bill its his her their
    your you our who whom whose been being also more most other such may can must should
    into out over under about between within during per via using used use case cases""".split()
)
TOPK = 5
MIN_SCORE = 0.06


def _clean(s: str | None) -> str:
    return html.unescape(_TAG.sub(" ", s or "")).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


@dataclass
class SectionTopic:
    key: str
    title: str
    level: int
    tokens: set[str]


def _section_topics(doc: ExtractedDoc) -> list[SectionTopic]:
    """Topic tokens per section = its title + ancestor titles (the breadcrumb)."""
    by_id = {s.id: s for s in doc.sections}
    topics = []
    for s in doc.sections:
        words, cur = [], s
        while cur is not None:
            words.append(cur.title)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        topics.append(SectionTopic(s.key, s.title, s.level, _tokens(" ".join(words))))
    return topics


def _idf(topics: list[SectionTopic]) -> dict[str, float]:
    n = len(topics)
    df: dict[str, int] = {}
    for t in topics:
        for w in t.tokens:
            df[w] = df.get(w, 0) + 1
    return {w: math.log(1 + n / c) for w, c in df.items()}


@dataclass
class AlertMapping:
    alert_id: str
    title: str
    impact: str
    publication_date: str
    summary: str
    matches: list[tuple[str, str, float]]  # (section_key, section_title, score)


def map_alerts(doc: ExtractedDoc, alerts: list[dict], topk: int = TOPK) -> list[AlertMapping]:
    """Score every alert against every section; keep the top matches."""
    topics = _section_topics(doc)
    idf = _idf(topics)
    results = []
    for a in alerts:
        atext = f"{_clean(a.get('TITLE'))} {_clean(a.get('SUMMARY'))}"
        atoks = _tokens(atext)
        scored = []
        for t in topics:
            shared = atoks & t.tokens
            if not shared:
                continue
            score = sum(idf.get(w, 0) for w in shared) / (1 + math.log(1 + len(t.tokens)))
            scored.append((t.key, t.title, round(score, 3)))
        scored.sort(key=lambda x: x[2], reverse=True)
        matches = [m for m in scored[:topk] if m[2] >= MIN_SCORE]
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
