"""Build and cache per-section embeddings for a region.

We embed at section granularity (514 sections for the DP survey): each vector
covers the section's breadcrumb (title path) plus a snippet of its own content.
Cached to a .npz next to the region's other artifacts, tagged with the model
name so a model swap (e.g. to Titan v2) invalidates cleanly.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from aosphere_core_index.embeddings.embedder import Embedder, cosine_topk
from aosphere_core_index.extract.model import ExtractedDoc

_FN_MARKER = re.compile(r"\[\^\d+\]")
_SNIPPET_CHARS = 2500
# Targeted chunking: ~13% of sections exceed _SNIPPET_CHARS, so embedding only their
# first _SNIPPET_CHARS chars leaves the rest invisible to retrieval (e.g. a query whose
# answer sits deep in a long "Summary Matrix" never matches). Oversized sections are
# split into overlapping windows, each indexed as its own row keyed `<key>#c<i>` (window
# 0 keeps the bare key). At query time the `#c<i>` suffix collapses back to the section
# (see registry._ident), so this only ADDS recall for long sections — normal (single-
# window) sections are unchanged, unlike wholesale chunking which fragmented them.
_CHUNK_OVERLAP = int(os.getenv("ACI_CHUNK_OVERLAP", "400"))  # ~16% of the window: keeps a clause/
#   table row that straddles a boundary intact in at least one window (context not lost between chunks)
_MAX_CHUNKS = int(os.getenv("ACI_MAX_CHUNKS", "8"))  # cap row growth (some sections are 100k+); bumped
#   from 6 so the wider overlap doesn't cost reach — 8 windows still cover ~17k chars/section

# Editorial "scope note" boilerplate: an instruction to the survey author about
# what a Part deliberately does NOT cover — e.g. "Do not address in this Part D5
# any requirement ... to notify any appointment of a data protection officer ...
# (see Part B ...)." It is NOT substantive legal content. Left in the clause text,
# an agent misreads "not addressed here" as "not required" (the Uruguay DPO-
# notification false-negative). Strip it wherever clause text is assembled. The
# note is a single sentence with no internal periods, so match to the first period.
_SCOPE_NOTE = re.compile(r"(?i)Do not address in this Part\b[^.]*\.")


def strip_scope_notes(text: str) -> str:
    """Remove editorial 'Do not address in this Part …' scope-note sentences."""
    return _SCOPE_NOTE.sub("", text or "").strip()


def state_of(jurisdiction: str | None) -> str | None:
    """'United States - California (Long Form)' -> 'California'; non-US -> None."""
    if not jurisdiction or not jurisdiction.startswith("United States - "):
        return None
    return re.sub(r"\s*\(.*\)$", "", jurisdiction.split(" - ", 1)[1]).strip() or None


def _table_for_state(text: str, state: str) -> str:
    """A US-state survey table is a shared multi-state comparison (~37k chars,
    identical across all 50 state docs). Keep only this state's row(s) + header so
    the embedded vector / snippet reflects THIS state, not a generic blob."""
    lines = text.split("\n")
    header = lines[0] if lines else ""
    rows = [ln for ln in lines[1:] if state.lower() in ln.lower()]
    if not rows:
        return text
    return "\n".join(([header] if header and state.lower() not in header.lower() else []) + rows)


def _answer_from(items, get_kind, get_text, jurisdiction: str | None) -> str:
    state = state_of(jurisdiction)
    parts = []
    for e in items:
        if get_kind(e) == "question":
            continue
        t = strip_scope_notes(get_text(e))
        if get_kind(e) == "table" and state and len(t) > 2000:
            t = _table_for_state(t, state)
        parts.append(t)
    answer = " ".join(p for p in parts if p).strip()
    if not answer:
        answer = " ".join(get_text(e) for e in items).strip()
    return _FN_MARKER.sub("", answer)


def answer_text(elements, jurisdiction: str | None = None) -> str:
    """ANSWER content of a clause (ExtractedDoc Element objects). Drops the template
    QUESTION and leads with the answer (body/bullets/tables) so the vector reflects
    what the clause says. For a US state, big shared comparison tables are reduced to
    that state's row(s). Same shape used to embed and to rerank."""
    return _answer_from(elements, lambda e: getattr(e, "kind", ""), lambda e: e.text, jurisdiction)


def answer_text_dict(elements, jurisdiction: str | None = None) -> str:
    """answer_text for content.json sections (dict elements)."""
    return _answer_from(elements, lambda e: e.get("kind", ""), lambda e: e.get("text", ""), jurisdiction)


_GUID_CHARS = 1000
# A section with less own-text than this is a heading whose content lives in its
# children (e.g. "E1.1 Requirements for sharing data" with the substance in E1.1.x).
# With nothing of its own to embed it can't be retrieved, so a query for the section
# never surfaces it — it borrows a snippet of its descendants' text instead.
_THIN_BODY_CHARS = 40


def _windows(text: str) -> list[str]:
    """Overlapping windows of <=_SNIPPET_CHARS over `text`, capped at _MAX_CHUNKS. Text
    that fits yields a single window (== the text unchanged), so only oversized sections
    produce extra rows.

    Each cut lands on WHITESPACE. Cutting at a raw character offset split words down the
    middle, and because these windows become their own `#c1` sections, the mangling was
    visible in the product: 1,169 clause bodies in the Data Privacy build opened mid-word
    ("df : https://..." for a body that continued "...GUIDELINE.pdf"). It also fed Titan
    half-words, which tokenize into nonsense.

    Only the SEARCH for a boundary is bounded (a long unbroken run — a URL, a base64 blob
    — has no space to find), so a pathological input still gets split rather than looping."""
    if len(text) <= _SNIPPET_CHARS:
        return [text]
    step = max(1, _SNIPPET_CHARS - _CHUNK_OVERLAP)
    # how far back from a cut we are willing to look for a space before giving up on it
    slack = max(1, min(80, _SNIPPET_CHARS // 8))

    def snap(i: int) -> int:
        """Move a cut back to just after the nearest preceding whitespace."""
        if i <= 0 or i >= len(text) or text[i].isspace() or text[i - 1].isspace():
            return i
        j = text.rfind(" ", max(0, i - slack), i)
        if j == -1:
            j = text.rfind("\n", max(0, i - slack), i)
        return j + 1 if j != -1 else i

    out, starts = [], range(0, len(text), step)
    for i in list(starts)[:_MAX_CHUNKS]:
        a, b = snap(i), snap(min(i + _SNIPPET_CHARS, len(text)))
        chunk = text[a:b].strip()
        if chunk:
            out.append(chunk)
    return out or [text[:_SNIPPET_CHARS]]


_SYNTH_CACHE: dict[str, dict] = {}


def _load_synth_questions(jurisdiction: str | None) -> dict:
    """EXPERIMENT (ACI_SYNTH_Q=1): per-clause synthetic questions to PREPEND to the
    embed text only (display/rerank/snippet unchanged — they use answer_text). Loaded
    from ACI_SYNTH_Q_DIR/<jurisdiction>.json = {clause_key: {"questions": [...]}}.
    Off by default -> empty dict -> embed text is byte-for-byte the current behaviour."""
    if os.getenv("ACI_SYNTH_Q") != "1" or not jurisdiction:
        return {}
    d = os.getenv("ACI_SYNTH_Q_DIR")
    if not d:
        return {}
    if jurisdiction not in _SYNTH_CACHE:
        p = Path(d) / f"{jurisdiction}.json"
        try:
            _SYNTH_CACHE[jurisdiction] = json.loads(p.read_text()) if p.exists() else {}
        except (OSError, json.JSONDecodeError):
            _SYNTH_CACHE[jurisdiction] = {}
    return _SYNTH_CACHE[jurisdiction]


def _section_embed_text(doc: ExtractedDoc) -> list[tuple[str, str, int, str]]:
    """Return (key, title, level, embed_text) per section — pure clause content
    (breadcrumb + answer). Guidance is indexed as SEPARATE rows (see
    build_section_index), not folded in here: folding it into the clause vector
    dilutes it (a guidance-question match drops from ~0.5 cosine to ~0.19 once
    buried under the clause body). An empty/thin heading borrows its descendants'
    text so it stays retrievable (doc order: a section's subtree is the contiguous
    run of following sections at a deeper level)."""
    by_id = {s.id: s for s in doc.sections}
    secs = doc.sections
    synth = _load_synth_questions(doc.jurisdiction)  # {key: {"questions": [...]}} — embed-only aug
    out = []
    for i, s in enumerate(secs):
        crumb, cur = [], s
        while cur is not None:
            crumb.append(cur.title)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        breadcrumb = " > ".join(reversed(crumb))
        body = answer_text(s.elements, doc.jurisdiction)
        if len(body.strip()) < _THIN_BODY_CHARS:
            borrowed: list[str] = []
            for t in secs[i + 1:]:
                if t.level <= s.level:
                    break  # left this section's subtree
                ct = answer_text(t.elements, doc.jurisdiction)
                if ct:
                    borrowed.append(ct)
                    if sum(len(x) for x in borrowed) >= _SNIPPET_CHARS:
                        break
            if borrowed:
                body = (body + " " + " ".join(borrowed)).strip()
        # Oversized sections -> overlapping windows, each its own row (`key#c<i>`), so
        # content past the first window is still retrievable. Window 0 keeps the bare
        # key and equals the previous (truncated) row, so non-oversized sections and the
        # base row are unchanged; the `#c<i>` rows collapse back to the section at query
        # time (registry._ident).
        qs = (synth.get(s.key) or {}).get("questions") or []
        qprefix = (" ".join(qs) + "\n") if qs else ""  # embed-only (ACI_SYNTH_Q); "" when off
        for ci, win in enumerate(_windows(body)):
            key = s.key if ci == 0 else f"{s.key}#c{ci}"
            out.append((key, s.title, s.level, f"{qprefix}{breadcrumb}\n{win}".strip()))
    return out


@dataclass
class SectionIndex:
    keys: list[str]
    titles: list[str]
    levels: list[int]
    matrix: np.ndarray
    model: str
    kinds: list[str] = field(default_factory=list)  # per-row: "clause"|"guidance"|"alert"

    def row_kinds(self) -> list[str]:
        """Per-row kinds, defaulting to all-"clause" for legacy indices without them."""
        return self.kinds if self.kinds else ["clause"] * len(self.keys)

    def search(self, query_vec: np.ndarray, k: int = 5) -> list[tuple[str, str, int, float]]:
        return [
            (self.keys[i], self.titles[i], self.levels[i], score)
            for i, score in cosine_topk(query_vec, self.matrix, k)
        ]

    def filter_kinds(self, exclude: set[str]) -> "SectionIndex":
        """A view with the given row kinds removed (no re-embedding). Used to map
        alerts against clause/guidance rows only — never against other alert rows."""
        kinds = self.row_kinds()
        idx = [i for i, kd in enumerate(kinds) if kd not in exclude]
        return SectionIndex(
            keys=[self.keys[i] for i in idx], titles=[self.titles[i] for i in idx],
            levels=[self.levels[i] for i in idx], matrix=self.matrix[idx],
            model=self.model, kinds=[kinds[i] for i in idx],
        )


def build_section_index(
    doc: ExtractedDoc, embedder: Embedder,
    guidance_rows: list[tuple[str, str]] | None = None,
    extra_rows: list[tuple[str, str, int, str, str]] | None = None,
) -> SectionIndex:
    """Build the per-region index.

    `guidance_rows` = (clause_key, guidance_text) pairs — each becomes its OWN index
    row tagged with that clause's key/title/level (kind "guidance"), so a query that
    matches curated guidance retrieves the clause without the clause body diluting it.
    `extra_rows` = fully self-described (key, title, level, text, kind) rows for
    content that is NOT a clause (e.g. alerts, kind "alert"), appended as-is.
    Retrieval dedupes rows sharing an identity to one hit."""
    rows = _section_embed_text(doc)  # pure clause rows
    keys = [r[0] for r in rows]
    titles = [r[1] for r in rows]
    levels = [r[2] for r in rows]
    texts = [r[3] for r in rows]
    kinds = ["clause"] * len(rows)
    meta = {s.key: (s.title, s.level) for s in doc.sections}
    for key, gtext in guidance_rows or []:
        if key in meta and gtext:
            t, lvl = meta[key]
            keys.append(key); titles.append(t); levels.append(lvl)
            texts.append(gtext[:_GUID_CHARS]); kinds.append("guidance")
    for key, title, level, text, kind in extra_rows or []:
        if text:
            keys.append(key); titles.append(title); levels.append(level)
            texts.append(text); kinds.append(kind)
    matrix = embedder.embed(texts)
    return SectionIndex(keys=keys, titles=titles, levels=levels, matrix=matrix,
                        model=embedder.name, kinds=kinds)


def extend_index(idx: SectionIndex, embedder: Embedder,
                 rows: list[tuple[str, str, int, str, str]]) -> SectionIndex:
    """Append self-described (key, title, level, text, kind) rows to an already-built
    index, embedding ONLY the new rows (the clause vectors are reused, not recomputed).
    Used to add semantically-mapped guidance / alert rows after the clause index exists
    (they need that index to be mapped in the first place)."""
    rows = [r for r in rows if r[3]]
    if not rows:
        return idx
    add = embedder.embed([r[3][:_GUID_CHARS] for r in rows])
    return SectionIndex(
        keys=idx.keys + [r[0] for r in rows],
        titles=idx.titles + [r[1] for r in rows],
        levels=idx.levels + [r[2] for r in rows],
        matrix=np.vstack([idx.matrix, add]),
        model=idx.model,
        kinds=idx.row_kinds() + [r[4] for r in rows],
    )


def save_index(idx: SectionIndex, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path, keys=np.array(idx.keys), titles=np.array(idx.titles),
        levels=np.array(idx.levels), matrix=idx.matrix, model=np.array(idx.model),
        kinds=np.array(idx.row_kinds()),
    )
    return path


def load_index(path: Path) -> SectionIndex:
    z = np.load(path, allow_pickle=False)
    kinds = list(z["kinds"]) if "kinds" in z.files else []
    return SectionIndex(
        keys=list(z["keys"]), titles=list(z["titles"]),
        levels=[int(x) for x in z["levels"]], matrix=z["matrix"], model=str(z["model"]),
        kinds=kinds,
    )
