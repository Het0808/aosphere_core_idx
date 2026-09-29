#!/usr/bin/env python3
"""lib_review.py — a reviewer's own verdict and notes on a document.

The scorecard is a MEASUREMENT; this is a JUDGEMENT. They are stored and shown
separately on purpose:

  * The computed gate is never overwritten. An override records what a human
    decided ALONGSIDE the machine's answer, so "the tool said fail, a reviewer
    accepted it anyway, here is why" stays legible months later. Silently
    replacing the score would destroy the evidence that makes an override
    trustworthy, and would make a genuinely green corpus indistinguishable from
    one somebody marked green.
  * An override REQUIRES a reason. A verdict with no argument behind it is worth
    less than no verdict, because the next reader cannot tell whether it was
    considered or clicked past.
  * Notes are independent of overrides. A reviewer often wants to record "the
    table on p12 is a graphic, not an extraction failure" without changing any
    verdict at all.

Persisted exactly like dismissals (see lib_dismissals): keyed by a hash of the
SOURCE PDF, in one JSON file per product folder — so a review survives
re-extraction, which is the only thing that makes it worth writing down. Nothing
is deleted; clearing an override is itself recorded as a note.

Store layout:
    {"<doc_key>": {"override": {...} | null,
                   "notes": [{"id", "text", "author", "at"}, ...]}}
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

STORE_NAME = "_review.json"

# The only verdicts a human may set — deliberately the same vocabulary as the
# computed gate, so the two are directly comparable at a glance.
VERDICTS = ("pass", "review", "fail")


def _store_path(out_root: Path) -> Path:
    """One store per product folder, beside the dismissals store, so every
    jurisdiction under it shares a file instead of scattering one JSON per job."""
    root = Path(out_root)
    return (root.parent if root.parent.name else root) / STORE_NAME


def load(out_root: Path) -> dict:
    try:
        return json.loads(_store_path(out_root).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(out_root: Path, data: dict) -> None:
    p = _store_path(out_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))


def for_doc(out_root: Path, doc_key: str) -> dict:
    """This document's review state, always the same shape so callers need no
    None-checks: {"override": dict|None, "notes": [...]}"""
    entry = load(out_root).get(doc_key) or {}
    return {"override": entry.get("override"), "notes": entry.get("notes") or []}


def set_override(out_root: Path, doc_key: str, verdict: str, reason: str,
                 author: str = "", computed_gate: str = "") -> dict:
    """Record a human verdict. `reason` is required — see the module docstring.

    `computed_gate` is stored alongside so the disagreement itself stays visible:
    an override that merely restates what the tool already said is very different
    from one that reverses it, and only the pair shows which it was."""
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    if not (reason or "").strip():
        raise ValueError("an override requires a reason")
    data = load(out_root)
    entry = data.setdefault(doc_key, {})
    entry["override"] = {"verdict": verdict, "reason": reason.strip(),
                         "author": author.strip(), "at": time.time(),
                         "computed_gate": computed_gate}
    _save(out_root, data)
    return entry["override"]


def clear_override(out_root: Path, doc_key: str, author: str = "") -> None:
    """Remove the override and record that it was removed — a verdict being
    withdrawn is as much a review fact as the verdict itself."""
    data = load(out_root)
    entry = data.setdefault(doc_key, {})
    prev = entry.get("override")
    entry["override"] = None
    if prev:
        entry.setdefault("notes", []).append({
            "id": uuid.uuid4().hex[:8],
            "text": f"Override cleared (was {prev.get('verdict')}: {prev.get('reason', '')})",
            "author": author.strip(), "at": time.time(), "system": True})
    _save(out_root, data)


def add_note(out_root: Path, doc_key: str, text: str, author: str = "") -> dict:
    if not (text or "").strip():
        raise ValueError("a note needs text")
    data = load(out_root)
    entry = data.setdefault(doc_key, {})
    note = {"id": uuid.uuid4().hex[:8], "text": text.strip(),
            "author": author.strip(), "at": time.time()}
    entry.setdefault("notes", []).append(note)
    _save(out_root, data)
    return note


def delete_note(out_root: Path, doc_key: str, note_id: str) -> bool:
    data = load(out_root)
    entry = data.get(doc_key) or {}
    notes = entry.get("notes") or []
    kept = [n for n in notes if n.get("id") != note_id]
    if len(kept) == len(notes):
        return False
    entry["notes"] = kept
    _save(out_root, data)
    return True


def effective_gate(computed_gate: str, review: dict | None) -> tuple[str, bool]:
    """-> (gate to display, whether a human set it).

    The computed gate comes back unchanged when there is no override, so callers
    that ignore reviews keep working. When one exists the caller gets BOTH the
    human verdict and the fact that it is human, and must show the difference."""
    ov = (review or {}).get("override")
    if not ov or not ov.get("verdict"):
        return computed_gate, False
    return ov["verdict"], True
