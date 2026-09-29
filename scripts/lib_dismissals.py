#!/usr/bin/env python3
"""lib_dismissals.py — persistent "this finding is a false positive" store.

A validation dashboard is only useful if a reviewer can clear noise; it is only
TRUSTWORTHY if clearing noise leaves a trace. So dismissals here are:

  * PERSISTENT ACROSS RE-RUNS — keyed by a hash of the source PDF, not by job
    id. Re-uploading the same document keeps its dismissals, which is the whole
    point: the same false positive would otherwise have to be dismissed again on
    every run.
  * NEVER DESTRUCTIVE — a dismissal hides a finding from the active score but
    the finding is still returned (flagged `dismissed`), still counted, and
    restorable. Nothing is deleted.
  * FAIL-SAFE — a finding key is derived from the finding's own content. If the
    extraction changes enough that a key no longer matches, the finding
    REAPPEARS rather than staying silently hidden. Wrong in the safe direction.

Store layout (one JSON file shared by every job under the same output root):

    {"<doc_key>": {"<finding_key>": {"reason": str, "at": float, "label": str}}}
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

STORE_NAME = "_dismissals.json"


def _sha(*parts: str, n: int = 10) -> str:
    h = hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace"))
    return h.hexdigest()[:n]


def doc_key(pdf_path: Path) -> str:
    """Identity of the SOURCE DOCUMENT, so dismissals follow the document rather
    than the job. Content hash (not filename) — the same PDF re-uploaded under
    any name keeps its dismissals, and an edited PDF correctly starts clean."""
    try:
        return hashlib.sha1(Path(pdf_path).read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


# ---- stable finding keys -------------------------------------------------
# Each key is derived only from things that survive a re-extraction. Where a key
# includes a text snippet it is normalised (lowercased tokens joined by spaces)
# so trivial whitespace differences don't invalidate a dismissal.
def gap_key(file: str, tokens: list[str]) -> str:
    return "gap|" + _sha(file, " ".join(tokens[:8]).lower())


def placement_key(table_id: str, pages: list[int]) -> str:
    return "place|" + _sha(table_id, ",".join(str(p) for p in pages))


def table_key(table_id: str, pages: list[int]) -> str:
    return "table|" + _sha(table_id, ",".join(str(p) for p in pages))


def number_key(surface: str) -> str:
    return "num|" + _sha(str(surface).lower())


def transposition_key(missing: str, extra: str) -> str:
    return "trans|" + _sha(str(missing).lower(), str(extra).lower())


def meaning_key(phrase: str) -> str:
    return "mean|" + _sha(str(phrase).lower())


def hierarchy_key(title: str) -> str:
    return "hier|" + _sha(str(title).lower())


def unreadable_key(page: int) -> str:
    return "unread|" + _sha(str(page))


def engine_key(page: int) -> str:
    return "engine|" + _sha(str(page))


def page_absent_key(page: int) -> str:
    """A source page the tree accounts for nowhere. Its own namespace rather than
    unreadable_key's: a page can be BOTH unreadable and absent, and dismissing one
    must not silently dismiss the other."""
    return "pageabsent|" + _sha(str(page))


# ---- store ---------------------------------------------------------------
def store_path(out_root: Path) -> Path:
    """Shared across sibling jobs — dismissals belong to the document, and the
    same document is typically re-run many times into different job dirs."""
    return Path(out_root).parent / STORE_NAME


def load_all(out_root: Path) -> dict:
    p = store_path(out_root)
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_for_doc(out_root: Path, dkey: str) -> dict:
    return load_all(out_root).get(dkey, {}) or {}


def dismiss(out_root: Path, dkey: str, finding_key: str, label: str = "", reason: str = "") -> dict:
    data = load_all(out_root)
    data.setdefault(dkey, {})[finding_key] = {
        "reason": reason, "label": label, "at": time.time(),
    }
    store_path(out_root).write_text(json.dumps(data, indent=2))
    return data[dkey]


def restore(out_root: Path, dkey: str, finding_key: str) -> dict:
    data = load_all(out_root)
    data.get(dkey, {}).pop(finding_key, None)
    store_path(out_root).write_text(json.dumps(data, indent=2))
    return data.get(dkey, {})
