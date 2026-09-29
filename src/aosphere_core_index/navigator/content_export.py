"""Export a content-rich JSON for the reader navigator.

Unlike the compact graph export (labels only), this carries the actual section
text, footnote text, and the curated Q&A / alerts linked to each clause — so the
navigator is a reading experience, not just dots.
"""

from __future__ import annotations

import html as _html
import re
from difflib import SequenceMatcher

from aosphere_core_index.extract.model import ExtractedDoc
from aosphere_core_index.ingest.source import JurisdictionSource

_CLAUSE_RE = re.compile(r"\b[A-K]\d+(?:\.\d+)*(?:\([a-z]+\))*")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str | None) -> str:
    """Strip HTML tags and unescape entities (&nbsp; &ldquo; &ndash; …) so the text
    is clean for embedding, snippets, and display."""
    return _html.unescape(_TAG_RE.sub(" ", s or "")).strip()


_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm_title(s: str) -> str:
    return " ".join(_WORD_RE.findall((s or "").lower()))


def _toks(s: str) -> set[str]:
    return {t.rstrip("s") for t in _WORD_RE.findall((s or "").lower()) if len(t) > 2}


class GuidanceLinker:
    """Structural guidance→clause linking for products whose REFERENCETEXT carries
    NO clause keys (Shareholding Disclosure cites memorandum pages/legislation, so
    the DP-style clause-ref regex links exactly zero of its ~200 curated answers
    per jurisdiction). Instead we use the answer's own survey coordinates:
    TEMPLATENAME → the Part (regime), SUBJECTNAME → a section title within that
    part, QUESTIONTITLE → a clause title within that section. Matching is exact on
    normalized titles and scope-aware ("Overview" exists under every part, so it
    only links within the resolved part/subject)."""

    # Fuzzy tier acceptance: best candidate needs a decent absolute score AND a
    # clear lead over the runner-up (ties = ambiguity = stay unlinked). Jaccard is
    # used (not containment) so a one-word subject like "Restrictions" can't
    # "fully match" every long title containing that word.
    FUZZY_MIN = 0.66
    FUZZY_MARGIN = 0.10

    def __init__(self, sections) -> None:
        def _get(s, k):
            return s[k] if isinstance(s, dict) else getattr(s, k)
        self.rows = [(str(_get(s, "key")), str(_get(s, "title")), int(_get(s, "level")))
                     for s in sections]
        self.parts = [(k, t) for k, t, lvl in self.rows if lvl == 0 and len(k) == 1]
        self.by_title: dict[str, list[str]] = {}
        self._prepped = []  # (key, level, norm_title, tokens)
        for k, t, lvl in self.rows:
            self.by_title.setdefault(_norm_title(t), []).append(k)
            self._prepped.append((k, lvl, _norm_title(t), _toks(t)))

    def _part_for(self, template: str) -> str | None:
        tt = _toks(template)
        best, best_n = None, 0
        for k, t in self.parts:
            n = len(tt & _toks(t))
            if n > best_n:
                best, best_n = k, n
        return best

    def _match(self, title: str, scope: str | None) -> list[str]:
        keys = self.by_title.get(_norm_title(title), [])
        if not title or not keys:
            return []
        if scope:
            prefix = scope if len(scope) == 1 else scope + "."
            return [k for k in keys if k == scope or k.startswith(prefix)]
        return keys

    def _fuzzy(self, title: str, scope: str | None) -> str | None:
        """Best fuzzily-matching section for a paraphrased title ("How to Make a
        Notification or Disclosure" vs "How to make a disclosure"). Score = max of
        token-set Jaccard and character similarity; accepted only above FUZZY_MIN,
        with FUZZY_MARGIN over the runner-up, never a Part root."""
        nt, tt = _norm_title(title), _toks(title)
        if not nt:
            return None
        prefix = None if not scope else (scope if len(scope) == 1 else scope + ".")
        scored: list[tuple[float, str]] = []
        for k, lvl, n, toks in self._prepped:
            if lvl < 1:  # never fuzzy-link to a Part root
                continue
            if prefix and not (k == scope or k.startswith(prefix)):
                continue
            union = tt | toks
            j = len(tt & toks) / len(union) if union else 0.0
            # char-level ratio only when tokens overlap a bit (perf + noise guard)
            s = max(j, SequenceMatcher(None, nt, n).ratio()) if j >= 0.2 else j
            scored.append((s, k))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        best, key = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0.0
        if best >= self.FUZZY_MIN and best - runner >= self.FUZZY_MARGIN:
            return key
        return None

    def key_for(self, r: dict) -> str | None:
        """The most specific clause this answer belongs to, or None. Exact title
        matches first, fuzzy (guarded) second; never links across parts when the
        template resolves; ambiguous titles stay unlinked rather than guessed."""
        part = self._part_for(r.get("TEMPLATENAME", ""))
        subject = r.get("SUBJECTNAME", "")
        question = r.get("QUESTIONTITLE", "")
        subj = self._match(subject, part)
        anchor = subj[0] if len(subj) == 1 else None
        q = self._match(question, anchor or part)
        if len(q) == 1:
            return q[0]
        if question:
            fq = self._fuzzy(question, anchor or part)
            if fq:
                return fq
        if anchor:
            return anchor
        return self._fuzzy(subject, part) if subject else None


def guidance_refs(r: dict, keys: set[str], linker: GuidanceLinker | None = None) -> list[str]:
    """Clause keys a curated answer links to: explicit clause refs in
    REFERENCETEXT (Data Privacy style) first; structural template/subject/question
    matching as the fallback (Shareholding Disclosure style)."""
    refs = [ref for ref in sorted(set(_CLAUSE_RE.findall(_clean(r.get("REFERENCETEXT")))))
            if ref in keys]
    if refs:
        return refs
    if linker is not None:
        k = linker.key_for(r)
        if k and k in keys:
            return [k]
    return []


def guidance_rows(rated_answers: list[dict], keys: set[str],
                  sections=None) -> list[tuple[str, str]]:
    """(clause_key, guidance_text) — ONE pair per curated guidance entry (subject +
    question + answer) whose reference resolves to a real clause. Each becomes its
    own index row so a query that IS a guidance question (e.g. "Is there a test for
    the law to apply…?") retrieves the clause; the bi-encoder otherwise embeds only
    the clause answer + breadcrumb and never surfaces it. Same ref resolution as
    build_content's answers_by_clause, so the two stay in lockstep."""
    linker = GuidanceLinker(sections) if sections is not None else None
    out: list[tuple[str, str]] = []
    for r in rated_answers or []:
        answer = " ".join(
            x for x in [_clean(r.get("SETANSWEROTHER")), _clean(r.get("SETCOMMENTS"))] if x
        )
        blob = " ".join(
            x for x in [r.get("SUBJECTNAME", ""), r.get("QUESTIONTITLE", ""), answer] if x
        ).strip()
        if blob:
            for ref in guidance_refs(r, keys, linker):
                out.append((ref, blob))
    return out


def _guidance_id(r: dict, i: int) -> str:
    return str(r.get("RESPONSEROWID") or f"i{i}")


def unlinked_guidance_rows(rated_answers: list[dict], keys: set[str], sections,
                           exclude_ids: set[str] = frozenset(),
                           ) -> list[tuple[str, str, int, str, str]]:
    """Standalone index rows for curated guidance that links to NO clause.

    Curated answers are expert content even when no clause anchors them (~35% of
    Shareholding Disclosure answers have no matching section title) — dropping
    them from the index wastes exactly what makes guidance valuable. Each becomes
    a self-described searchable row (key `GUID:<responserowid>`, kind
    "guidance"), like alerts: retrievable on its own, readable by the agent via
    `Jurisdiction:GUID:<id>`."""
    linker = GuidanceLinker(sections)
    rows: list[tuple[str, str, int, str, str]] = []
    for i, r in enumerate(rated_answers or []):
        if _guidance_id(r, i) in exclude_ids:
            continue  # semantically mapped to a clause — already indexed there
        if guidance_refs(r, keys, linker):
            continue  # linked guidance is already indexed on its clause
        answer = " ".join(
            x for x in [_clean(r.get("SETANSWEROTHER")), _clean(r.get("SETCOMMENTS"))] if x
        )
        title = " · ".join(
            x for x in [r.get("SUBJECTNAME", ""), r.get("QUESTIONTITLE", "")] if x
        ).strip()
        if not (title or answer):
            continue
        rows.append((f"GUID:{_guidance_id(r, i)}", title or "Curated guidance", 0,
                     f"{title}. {answer}".strip(". "), "guidance"))
    return rows


def alert_id(a: dict) -> str:
    return f"ALERT:{a.get('ALERTID')}"


def alert_rows(alerts: list[dict]) -> list[tuple[str, str, int, str, str]]:
    """Index rows for alerts, so alerts are searchable as standalone results.
    Per alert: one SUMMARY row (key `ALERT:<id>`, text = title. summary) plus one row
    per attachment chunk (key `ALERT:<id>#a0/#a1/…`, text = title. <chunk>). Rows are
    self-described (key, title, level, text, kind="alert") — alerts are not clauses,
    so they carry their own title/level and don't map onto doc.sections."""
    from aosphere_core_index.extract.pdf_extract import chunk_text

    rows: list[tuple[str, str, int, str, str]] = []
    for a in alerts or []:
        title = _clean(a.get("TITLE"))
        summary = _clean(a.get("SUMMARY"))
        key = alert_id(a)
        rows.append((key, title, 0, f"{title}. {summary}".strip(), "alert"))
        for i, chunk in enumerate(chunk_text(a.get("attachment_text", ""))):
            rows.append((f"{key}#a{i}", title, 0, f"{title}. {chunk}".strip(), "alert"))
    return rows


def _reshape_elements(elements: list[dict], jurisdiction: str | None) -> list[dict]:
    """US-state surveys embed the SAME ~37k-char multi-state comparison tables in
    every state's doc (A2.2, B2, ...). Reduce each to this state's row(s) so the
    stored content is genuinely per-jurisdiction — fixes retrieval, reads, and size.
    No-op for non-US docs and for tables that are already small/per-state."""
    from aosphere_core_index.embeddings.section_index import _table_for_state, state_of

    state = state_of(jurisdiction)
    if not state:
        return elements
    out = []
    for e in elements:
        if e["kind"] == "table" and len(e["text"]) > 2000:
            e = {**e, "text": _table_for_state(e["text"], state)}
        out.append(e)
    return out


def build_content(doc: ExtractedDoc, src: JurisdictionSource, alert_mappings=None,
                  alerts_raw=None) -> dict:
    keys = {s.key for s in doc.sections}

    sections = []
    cited_by: dict[str, list[str]] = {}
    for s in doc.sections:
        cites_out = sorted(
            {r for el in s.elements for r in el.clause_refs if r in keys and r != s.key}
        )
        for tgt in cites_out:
            cited_by.setdefault(tgt, []).append(s.key)
        sections.append(
            {
                "key": s.key,
                "title": s.title,
                "level": s.level,
                "parent_key": next((p.key for p in doc.sections if p.id == s.parent_id), None),
                "elements": _reshape_elements(
                    [{"kind": el.kind, "text": el.text} for el in s.elements], doc.jurisdiction),
                "footnotes": [
                    {"id": fid, "text": doc.footnotes.get(fid, "")} for fid in s.footnote_ids
                ],
                "cites_out": cites_out,
            }
        )

    # Curated answers grouped by the clause they link to — explicit clause refs
    # (DP) or structural template/subject/question matching (SD, whose
    # REFERENCETEXT carries no clause keys).
    linker = GuidanceLinker(doc.sections)
    answers_by_clause: dict[str, list[dict]] = {}
    guidance_by_id: dict[str, dict] = {}
    for i, r in enumerate(src.rated_answers):
        answer = " ".join(
            x for x in [_clean(r.get("SETANSWEROTHER")), _clean(r.get("SETCOMMENTS"))] if x
        )
        rec = {
            "question": r.get("QUESTIONTITLE", ""),
            "subject": r.get("SUBJECTNAME", ""),
            "color": r.get("COLOROPTION", ""),
            "answer": answer[:1200],
        }
        refs = guidance_refs(r, keys, linker)
        for ref in refs:
            answers_by_clause.setdefault(ref, []).append(rec)
        # Every guidance entry is retrievable by id — backs enrichment of GUID:*
        # search hits and agent reads, linked or not.
        gid = _guidance_id(r, i)
        guidance_by_id[gid] = dict(rec, id=gid, linked=refs)

    # Alerts mapped to sections. If lexical/semantic mappings are supplied
    # (from input/ alerts), use them; they place topical alerts on the relevant
    # clauses. Otherwise fall back to clause-ref matching on the working alerts.
    alerts_by_clause: dict[str, list[dict]] = {}
    alerts = []
    if alert_mappings is not None:
        for m in alert_mappings:
            rec = {"title": m.title, "impact": m.impact, "summary": m.summary,
                   "date": m.publication_date}
            alerts.append(rec)
            for sec_key, _title, score in m.matches:
                if sec_key in keys:
                    alerts_by_clause.setdefault(sec_key, []).append(dict(rec, score=score))
    else:
        for a in src.alerts:
            rec = {
                "title": _clean(a.get("TITLE")), "impact": a.get("IMPACT", ""),
                "summary": (_clean(a.get("SUMMARY")))[:600], "date": a.get("PUBLICATIONDATE", ""),
            }
            alerts.append(rec)
            for ref in sorted(set(_CLAUSE_RE.findall(f"{a.get('SUMMARY','')} {a.get('TAGS','')}"))):
                if ref in keys:
                    alerts_by_clause.setdefault(ref, []).append(rec)

    # Alerts keyed by id — with attachment text, the regions the alert covers, and
    # its mapped clauses. Backs offline enrichment of alert search hits, the alert
    # popup (attachment content), and agent reads of ALERT:<id> "clauses".
    mapped_by_id: dict[str, list] = {}
    if alert_mappings is not None:
        for m in alert_mappings:
            mapped_by_id[str(m.alert_id)] = [[k, s] for k, _t, s in m.matches if k in keys]
    alerts_by_id: dict[str, dict] = {}
    for a in alerts_raw or []:
        aid = str(a.get("ALERTID"))
        if not aid:
            continue
        alerts_by_id[aid] = {
            "id": aid,
            "title": _clean(a.get("TITLE")),
            "impact": a.get("IMPACT", ""),
            "summary": _clean(a.get("SUMMARY")),
            "date": str(a.get("PUBLICATIONDATE", ""))[:10],
            "attachment_text": a.get("attachment_text", ""),
            "regions": a.get("regions", []),
            "mapped": mapped_by_id.get(aid, []),
        }

    return {
        "doc": {
            "title": doc.title,
            "jurisdiction": doc.jurisdiction,
            "opinion_id": doc.opinion_id,
            "product": doc.product,
        },
        "sections": sections,
        "cited_by": cited_by,
        "answers_by_clause": answers_by_clause,
        "guidance_by_id": guidance_by_id,
        "alerts_by_clause": alerts_by_clause,
        "alerts": alerts,
        "alerts_by_id": alerts_by_id,
    }
