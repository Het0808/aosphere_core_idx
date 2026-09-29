"""LLM query expansion for dual-query retrieval.

A Bedrock Claude (Haiku) call rewrites the query to expand abbreviations/jargon into the
formal statutory terminology the clauses actually use (e.g. "TRS" -> "total return swap",
"UBO" -> "ultimate beneficial owner"). The caller retrieves with BOTH the raw and the
expanded query and UNIONs the candidate pools, so the expanded pass surfaces clauses the
raw query misses on vocabulary (mostly Shareholding Disclosure) — while never dropping a
raw hit (no dilution). Measured: SD recall@20 86% -> 92% (union), +6-9 at every rank.

Best-effort: expand() never raises and returns None on any failure/timeout, so search
degrades gracefully to raw-only. Off by default; enable with ACI_QUERY_EXPAND=1.
"""

from __future__ import annotations

import logging
import os

from ..config import settings

log = logging.getLogger(__name__)

_ENABLED = os.getenv("ACI_QUERY_EXPAND", "0") == "1"
_REGION = os.getenv("ACI_QUERY_EXPAND_REGION") or settings.bedrock_region
_MODEL = os.getenv("ACI_QUERY_EXPAND_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
_MAXLEN = int(os.getenv("ACI_QUERY_EXPAND_MAXLEN", "400"))  # skip pathologically long queries
_BR = None
_SYS = (
    "You rewrite legal/regulatory search queries to maximize document retrieval against "
    "formal statutory text. Expand every abbreviation/acronym to its full formal term "
    "(e.g. 'TRS' -> 'total return swap', 'UBO' -> 'ultimate beneficial owner', 'CFD' -> "
    "'contract for difference') AND add the formal regulatory terminology a statute would "
    "use for the concept. Keep the original wording too. Keep it to one line. Output ONLY "
    "the rewritten query, nothing else."
)


def enabled() -> bool:
    return _ENABLED


def _bedrock():
    global _BR
    if _BR is None:
        import boto3
        from botocore.config import Config

        # Best-effort + on the search hot path: short timeouts and few retries so a slow
        # Bedrock never stalls search (the caller also caps the wait and falls back to raw).
        _BR = boto3.client(
            "bedrock-runtime",
            region_name=_REGION,
            config=Config(retries={"max_attempts": 2, "mode": "adaptive"},
                          connect_timeout=3, read_timeout=8),
        )
    return _BR


def expand(query: str) -> str | None:
    """Return an expanded query string, or None if expansion is off / failed / degenerate.
    Never raises: dual-query retrieval falls back to raw-only whenever this returns None."""
    if not _ENABLED or not query or len(query) > _MAXLEN:
        return None
    try:
        r = _bedrock().converse(
            modelId=_MODEL,
            system=[{"text": _SYS}],
            messages=[{"role": "user", "content": [{"text": query}]}],
            inferenceConfig={"maxTokens": 150, "temperature": 0},
        )
        out = r["output"]["message"]["content"][0]["text"].strip()
        return out or None
    except Exception as e:  # noqa: BLE001 — expansion is best-effort, must not break search
        log.warning("query expansion failed (%s: %s); using raw query only",
                    type(e).__name__, str(e)[:120])
        return None
