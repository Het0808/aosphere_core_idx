"""Build the linked GraphIndex (no embeddings yet).

Nodes and edges are deterministic from the extracted structure + the JSON
sidecars. This is the artifact we navigate to verify extraction/linking before
spending anything on vectorization.

Node types : jurisdiction, opinion, document, section, chunk, footnote,
              template, subject, question, rated_answer, alert
Edge rels   : has_opinion, has_document, has_section, has_subsection,
              has_chunk, refers_to (footnote), cites (clause ref),
              has_subject, has_question, has_rated_answer, affects
"""

from __future__ import annotations

import re

import networkx as nx

from aosphere_core_index.chunking.chunker import Chunk
from aosphere_core_index.extract.model import ExtractedDoc
from aosphere_core_index.ingest.source import JurisdictionSource

_CLAUSE_RE = re.compile(r"\b[A-K]\d+(?:\.\d+)*(?:\([a-z]+\))*")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(html: str | None) -> str:
    return _TAG_RE.sub(" ", html or "").strip()


def _clause_refs(text: str) -> list[str]:
    return sorted(set(_CLAUSE_RE.findall(text or "")))


def build_graph(
    doc: ExtractedDoc, chunks: list[Chunk], src: JurisdictionSource
) -> nx.DiGraph:
    """Assemble the typed, linked graph for one jurisdiction's document."""
    g = nx.DiGraph()
    key_to_section = {s.key: s.id for s in doc.sections}

    jur_id = f"jur:{doc.jurisdiction}"
    opi_id = f"opi:{doc.opinion_id}"
    doc_id = f"doc:{doc.doc_id}"
    g.add_node(jur_id, ntype="jurisdiction", label=doc.jurisdiction,
               jurisdiction_id=doc.jurisdiction_id)
    g.add_node(opi_id, ntype="opinion", label=f"Opinion {doc.opinion_id}",
               opinion_id=doc.opinion_id)
    g.add_node(doc_id, ntype="document", label=doc.title, product=doc.product)
    g.add_edge(jur_id, opi_id, rel="has_opinion")
    g.add_edge(opi_id, doc_id, rel="has_document")

    # Sections + hierarchy
    for s in doc.sections:
        g.add_node(
            s.id, ntype="section", label=f"[{s.key}] {s.title}", key=s.key,
            level=s.level, title=s.title,
        )
        if s.parent_id:
            g.add_edge(s.parent_id, s.id, rel="has_subsection")
        else:
            g.add_edge(doc_id, s.id, rel="has_section")
        for fid in s.footnote_ids:
            fn_node = f"fn:{doc.doc_id}:{fid}"
            if fn_node not in g:
                g.add_node(fn_node, ntype="footnote", label=f"Footnote {fid}",
                           text=doc.footnotes.get(fid, ""))
            g.add_edge(s.id, fn_node, rel="refers_to")

    # Chunks + clause-ref cites
    for c in chunks:
        cid = f"chunk:{c.id}"
        g.add_node(cid, ntype="chunk", label=f"chunk {c.section_key}#{c.id.split(':')[-1]}",
                   breadcrumb=c.breadcrumb, tokens=c.token_estimate, text=c.text[:4000])
        g.add_edge(c.section_id, cid, rel="has_chunk")
        for ref in c.clause_refs:
            target = key_to_section.get(ref)
            if target and target != c.section_id:
                g.add_edge(cid, target, rel="cites")

    # Curated Q&A overlay (RAG_rated_answers.json)
    for r in src.rated_answers:
        tmpl = f"tmpl:{r.get('TEMPLATEID')}"
        subj = f"subj:{r.get('SUBJECTID')}"
        q = f"q:{r.get('QUESTIONID')}"
        ans = f"ans:{r.get('RESPONSEROWID')}"
        if tmpl not in g:
            g.add_node(tmpl, ntype="template", label=r.get("TEMPLATENAME", "Template"))
        if subj not in g:
            g.add_node(subj, ntype="subject", label=r.get("SUBJECTNAME", "Subject"))
            g.add_edge(tmpl, subj, rel="has_subject")
        if q not in g:
            g.add_node(q, ntype="question", label=r.get("QUESTIONTITLE", "Question"))
            g.add_edge(subj, q, rel="has_question")
        g.add_node(ans, ntype="rated_answer",
                   label=f"{r.get('COLOROPTION', '')}: {(r.get('SETANSWEROTHER') or '')[:40]}",
                   color=r.get("COLOROPTION", ""), text=_clean(r.get("SETCOMMENTS")))
        g.add_edge(q, ans, rel="has_rated_answer")
        # Link curated answer to the document clauses it references.
        for ref in _clause_refs(_clean(r.get("REFERENCETEXT"))):
            target = key_to_section.get(ref)
            if target:
                g.add_edge(ans, target, rel="cites")

    # Alerts overlay (frequently-appended, additive)
    for a in src.alerts:
        aid = f"alert:{a.get('ALERTID')}"
        g.add_node(aid, ntype="alert", label=(a.get("TITLE") or "Alert")[:60],
                   impact=a.get("IMPACT", ""), summary=(a.get("SUMMARY") or "")[:500])
        g.add_edge(aid, jur_id, rel="affects")
        for ref in _clause_refs(f"{a.get('SUMMARY','')} {a.get('TAGS','')}"):
            target = key_to_section.get(ref)
            if target:
                g.add_edge(aid, target, rel="cites")

    return g
