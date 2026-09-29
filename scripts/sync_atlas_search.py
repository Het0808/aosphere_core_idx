#!/usr/bin/env python3
"""Sync MSSQL entities -> Spotlight search index (phase 1).

Backend is selected by --backend / ACI_ENTITY_SEARCH_BACKEND:
  * opensearch (default): bulk-load into a fresh index + atomic alias swap
                          (zero-downtime reindex) with an FST completion suggester.
  * atlas:                upsert into MongoDB + (re)build the Atlas Search index.
The MSSQL extraction/transform (extract()) is shared by both — only the load target
and the index definition differ.

Hierarchy (metadata flows top-to-bottom):
    Product (RenderingStyle) -> Opinion -> Template -> Question

Each document:
    _id:         "<entityType>:<sourceId>"
    entityType:  product | opinion | template | question
    title, description
    metadata:    ancestry (products/opinions/templates as {id,name}),
                 jurisdictions, status, dates
    source:      {table, pk}
    syncRun, syncedAt

Rerunnable: upserts by _id, then removes docs not seen in this run.
Also creates/updates the Atlas Search index (default name: entity_search).

Usage:
    pip install pymssql pymongo
    python scripts/sync_atlas_search.py [--dry-run] [--verify] [--status all]

Env (.env): MSSQL_HOST/PORT/USER/PASSWORD/DATABASE, MONGODB_URI,
            ATLAS_DB (default aosphere_search), ATLAS_COLLECTION (default entities)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_NAME = "entity_search"
MAX_TEXT = 8000  # cap stored text length

_AUTOCOMPLETE = {"type": "autocomplete", "tokenization": "edgeGram",
                 "minGrams": 2, "maxGrams": 15, "foldDiacritics": True}


# lucene.english removes stopwords (of/the/an...) and stems at index AND query
# time, so "types of transaction" can't rank docs that merely contain "of".
_STRING_EN = {"type": "string", "analyzer": "lucene.english"}


def _ref_mapping(autocomplete: bool) -> dict:
    """{id, name} subdocument mapping. `autocomplete=True` adds edgeGram prefix
    search on the name (used for small vocabularies: categories, products,
    templates, subjects — NOT the huge opinion/question arrays)."""
    name: list = [{"type": "token"}, dict(_STRING_EN)]
    if autocomplete:
        name.append(_AUTOCOMPLETE)
    return {"type": "document", "fields": {"id": {"type": "number"}, "name": name}}


SEARCH_INDEX_DEFINITION = {
    "mappings": {
        "dynamic": False,
        "fields": {
            "entityType": {"type": "token"},
            "title": [
                dict(_STRING_EN),
                _AUTOCOMPLETE,
            ],
            "description": dict(_STRING_EN),
            "metadata": {
                "type": "document",
                "fields": {
                    "products": _ref_mapping(autocomplete=True),
                    "categories": _ref_mapping(autocomplete=True),
                    "templates": _ref_mapping(autocomplete=True),
                    "subjects": _ref_mapping(autocomplete=True),
                    "opinions": _ref_mapping(autocomplete=False),
                    "mainQuestions": _ref_mapping(autocomplete=False),
                    "breadcrumbs": dict(_STRING_EN),
                    "jurisdictions": [{"type": "token"}, dict(_STRING_EN),
                                      _AUTOCOMPLETE],
                    "jurisdictionCodes": {"type": "token"},
                    "status": {"type": "number"},
                    "modifiedDate": {"type": "date"},
                },
            },
        },
    }
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean(text) -> str | None:
    """Strip HTML tags/entities, collapse whitespace, cap length."""
    if text is None:
        return None
    s = WS_RE.sub(" ", TAG_RE.sub(" ", html.unescape(str(text)))).strip()
    return s[:MAX_TEXT] or None


def load_env() -> None:
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                v = v.strip()
                # strip matching quotes (single-quote values in .env so docker
                # compose doesn't interpolate $ inside passwords)
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)


def mongo_uri() -> str:
    """MONGODB_URI, with optional MONGODB_USERNAME/MONGODB_PASSWORD injected
    percent-encoded — so passwords with @ : / # etc. never break URI parsing.
    Any raw userinfo already present in MONGODB_URI is replaced."""
    from urllib.parse import quote_plus

    uri = os.environ["MONGODB_URI"]
    user = os.environ.get("MONGODB_USERNAME")
    pwd = os.environ.get("MONGODB_PASSWORD")
    if user and pwd:
        scheme, sep, rest = uri.partition("://")
        rest = rest.rsplit("@", 1)[-1]
        uri = f"{scheme}{sep}{quote_plus(user)}:{quote_plus(pwd)}@{rest}"
    return uri


def mongo_client():
    """MongoClient with certifi's CA bundle when available — python.org macOS
    installs don't see the system trust store, so Atlas TLS verification fails
    without it (CERTIFICATE_VERIFY_FAILED)."""
    from pymongo import MongoClient

    kwargs = {}
    try:
        import certifi
        kwargs["tlsCAFile"] = certifi.where()
    except ImportError:
        pass
    return MongoClient(mongo_uri(), **kwargs)


# ---------------------------------------------------------------- extract

def fetch_all(cur, sql: str) -> list[dict]:
    cur.execute(sql)
    return cur.fetchall()


def dedupe_opinion_versions(opinions: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """Collapse opinion versions to the latest one per lineage.

    Versions may CHAIN (v3.parentID -> v2, v2.parentID -> v1), so the lineage
    root is resolved transitively: follow masterOpinionID/parentID links until
    a row that points nowhere (or outside the set). One-hop grouping would
    split a chained lineage into several groups and leak old versions.

    Returns (latest_rows, latest_of) where latest_of maps EVERY opinion id to
    its lineage's latest id — used to remap template/question ancestry.
    Latest: highest version (tie-breaks: latest modifiedDate, highest id).
    """
    parent: dict[int, int] = {}
    for o in opinions:
        link = o["masterOpinionID"] or o["parentID"]
        if link and link != o["id"]:
            parent[o["id"]] = link

    def root(i: int) -> int:
        seen = set()
        while i in parent and i not in seen:   # cycle-safe chain walk
            seen.add(i)
            i = parent[i]
        return i

    groups: dict[int, list[dict]] = defaultdict(list)
    for o in opinions:
        groups[root(o["id"])].append(o)
    latest_rows, latest_of = [], {}
    for rows in groups.values():
        best = max(rows, key=lambda r: (r["version"] or 0,
                                        r["lastUpdatedOn"] or r["modifiedDate"]
                                        or datetime.min, r["id"]))
        latest_rows.append(best)
        for r in rows:
            latest_of[r["id"]] = best["id"]
    dropped = len(opinions) - len(latest_rows)
    print(f"  opinion versions: {len(opinions)} rows -> {len(latest_rows)} latest "
          f"({dropped} older versions collapsed, {len(groups)} lineages)")
    return latest_rows, latest_of


def _mssql_conn():
    import pymssql

    return pymssql.connect(
        server=os.environ["MSSQL_HOST"], port=int(os.environ.get("MSSQL_PORT", "1433")),
        user=os.environ["MSSQL_USER"], password=os.environ["MSSQL_PASSWORD"],
        database=os.environ["MSSQL_DATABASE"], timeout=120, login_timeout=30, as_dict=True)


# Tables whose rows feed the documents; count + max-modified fingerprints them.
_FP_TABLES = {
    "Opinion": "lastUpdatedOn", "Template": "modifiedDate", "Question": "modifiedDate",
    "Subject": "subjectCreatedDate", "RenderingStyle": None, "Jurisdiction": None,
    "TemplateQuestion": None, "TemplateOpinionOrg": None, "TemplateRenderingStyle": None,
    "OpinionRenderingStyle": None, "OpinionJurisdiction": None, "SubjectQuestion": None,
    "QuestionResponseType": None, "QuestionSubAnswerResponseType": None,
    "RenderingStyleProductCategory": None,
}


def source_fingerprint() -> dict:
    """Cheap change-detection: per-table row count + max modified timestamp.
    If nothing moved since the last sync, the whole run can be skipped."""
    conn = _mssql_conn()
    cur = conn.cursor()
    fp = {}
    for table, modcol in _FP_TABLES.items():
        cur.execute(f"SELECT COUNT(*) AS n"
                    + (f", MAX([{modcol}]) AS m" if modcol else "")
                    + f" FROM dbo.[{table}]")
        r = cur.fetchone()
        fp[table] = f"{r['n']}|{r.get('m')}"
    conn.close()
    return fp


def extract(status_filter: str, org: str = "101") -> dict[str, list[dict]]:
    """Pull all phase-1 tables and build hierarchy-aware documents.

    `org` scopes the sync to one organisation's content: opinions owned by the
    org, products it offers or that its opinions use, and templates/questions/
    subjects/jurisdictions reachable from those opinions. "all" disables.
    """
    org_id = None if org == "all" else int(org)
    org_where = f"WHERE orgID = {org_id}" if org_id is not None else ""

    conn = _mssql_conn()
    cur = conn.cursor()

    products = fetch_all(cur, """
        SELECT renderStyleID AS id, renderStyleName AS name,
               TooltipDescription AS description, SF_ProductName, SF_ProductID,
               SF_ProductFamily, orgID, status
        FROM dbo.RenderingStyle""")
    opinions = fetch_all(cur, f"""
        SELECT opinionID AS id, opinionName AS name, AlternativeText AS description,
               status, version, parentID, masterOpinionID, orgID,
               effectiveDate, modifiedDate, lastUpdatedOn
        FROM dbo.Opinion {org_where}""")
    templates = fetch_all(cur, """
        SELECT templateID AS id, templateName AS name, templateType, status,
               modifiedDate
        FROM dbo.Template""")
    questions = fetch_all(cur, """
        SELECT questionID AS id, questionTitle AS name,
               questionDescription, questionComment, status, modifiedDate
        FROM dbo.Question""")
    subjects = fetch_all(cur, """
        SELECT subjectID AS id, subjectName AS name, subjectDescription,
               status, subjectCreatedDate
        FROM dbo.Subject""")

    prod_cat = fetch_all(cur, """
        SELECT DISTINCT rpc.renderStyleID, pc.categoryID, pc.categoryName
        FROM dbo.RenderingStyleProductCategory rpc
        JOIN dbo.ProductCategory pc ON pc.categoryID = rpc.categoryID""")
    # NOTE: mapping tables are fetched UNFILTERED — their orgID is the
    # SUBSCRIBER org, not ownership. Org scoping happens via the opinions
    # themselves (Opinion.orgID), which gate these rows by membership below.
    op_prod = fetch_all(cur, "SELECT DISTINCT opinionID, renderStyleID FROM dbo.OpinionRenderingStyle")
    op_jur = fetch_all(cur, """
        SELECT DISTINCT oj.opinionID, j.jurisdictionID, j.jurisdictionName, j.jurisdictionCode
        FROM dbo.OpinionJurisdiction oj
        JOIN dbo.Jurisdiction j ON j.jurisdictionID = oj.JurisdictionID""")
    jurisdictions = fetch_all(cur, """
        SELECT jurisdictionID AS id, jurisdictionName AS name, jurisdictionCode,
               parentID, continentID, masterstatus AS status
        FROM dbo.Jurisdiction""")
    tpl_prod = fetch_all(cur, "SELECT DISTINCT templateID, renderStyleID FROM dbo.TemplateRenderingStyle")
    tpl_op = fetch_all(cur, "SELECT DISTINCT templateID, opinionID FROM dbo.TemplateOpinionOrg")
    tpl_q = fetch_all(cur, """
        SELECT templateID, questionID, MIN(displayOrder) AS displayOrder
        FROM dbo.TemplateQuestion GROUP BY templateID, questionID""")
    # Question hierarchy signals (validated against the report renderer):
    # - responseTypeID 15/17 in QuestionResponseType marks a DIVIDER question
    # - 'Line Above' styling (SubAnswerResponseType 6001/6005) marks a divider
    #   that STARTS a new top-level section (the visual separator)
    # - info-only questions (no answer responseTypes 3/6, e.g. 'Warning',
    #   'Scope of report') act as PART BOUNDARIES that reset the hierarchy
    rtypes_of_q: dict[int, set] = defaultdict(set)
    for r in fetch_all(cur, "SELECT questionID, responseTypeID FROM dbo.QuestionResponseType"):
        rtypes_of_q[r["questionID"]].add(r["responseTypeID"])
    divider_qids = {qid for qid, s in rtypes_of_q.items() if s & {15, 17}}
    line_above_qids = {r["questionID"] for r in fetch_all(
        cur, "SELECT DISTINCT questionID FROM dbo.QuestionSubAnswerResponseType "
             "WHERE subAnswerResponseTypeID IN (6001, 6005)")}
    subj_q = fetch_all(cur, "SELECT DISTINCT subjectID, questionID FROM dbo.SubjectQuestion")
    conn.close()

    # Report status distributions so filtering choices are visible.
    for label, rows in [("product", products), ("opinion", opinions),
                        ("template", templates), ("question", questions),
                        ("subject", subjects)]:
        print(f"  {label} status distribution: {dict(Counter(r['status'] for r in rows))}")

    if status_filter != "all":
        allowed = {int(s) for s in status_filter.split(",")}
        products = [r for r in products if r["status"] in allowed]
        opinions = [r for r in opinions if r["status"] in allowed]
        templates = [r for r in templates if r["status"] in allowed]
        questions = [r for r in questions if r["status"] in allowed]
        subjects = [r for r in subjects if r["status"] in allowed]

    # Collapse opinion versions: index only the latest per lineage, and remap
    # every ancestry reference (template/question metadata) onto it.
    opinions, latest_of = dedupe_opinion_versions(opinions)
    op_by_id = {o["id"]: o for o in opinions}

    # --- org scoping cascade: opinions are already org-filtered in SQL; keep
    # only products the org owns or its opinions use, templates linked to its
    # opinions, questions in those templates, jurisdictions of its opinions.
    if org_id is not None:
        used_prods = {r["renderStyleID"] for r in op_prod if r["opinionID"] in op_by_id}
        products = [p for p in products
                    if p["id"] in used_prods or p["orgID"] == org_id]
        # templates: in scope if tied to an in-scope PRODUCT (TemplateRenderingStyle)
        # or to an in-scope opinion. (TemplateOpinionOrg's orgID is the
        # SUBSCRIBER org, so it alone under-selects aosphere-owned templates.)
        kept_prod_ids = {p["id"] for p in products}
        kept_tpls = {r["templateID"] for r in tpl_prod
                     if r["renderStyleID"] in kept_prod_ids}
        kept_tpls |= {r["templateID"] for r in tpl_op
                      if latest_of.get(r["opinionID"]) in op_by_id}
        templates = [t for t in templates if t["id"] in kept_tpls]
        kept_qs = {r["questionID"] for r in tpl_q if r["templateID"] in kept_tpls}
        questions = [q for q in questions if q["id"] in kept_qs]
        used_jurs = {r["jurisdictionID"] for r in op_jur if r["opinionID"] in op_by_id}
        jurisdictions = [j for j in jurisdictions if j["id"] in used_jurs]
        print(f"  org={org_id} scope: products={len(products)} opinions={len(opinions)} "
              f"templates={len(templates)} questions={len(questions)} "
              f"jurisdictions={len(jurisdictions)}")

    prod_by_id = {p["id"]: p for p in products}
    tpl_by_id = {t["id"]: t for t in templates}

    # --- lookups (survivors of status filter + version dedupe only) ---
    # Direct opinion attributes come from the latest version's OWN rows.
    prods_of_op = defaultdict(set)
    for r in op_prod:
        if r["opinionID"] in op_by_id and r["renderStyleID"] in prod_by_id:
            prods_of_op[r["opinionID"]].add(r["renderStyleID"])
    jur_of_op = defaultdict(set)
    for r in op_jur:
        if r["opinionID"] in op_by_id:
            jur_of_op[r["opinionID"]].add((r["jurisdictionName"], r["jurisdictionCode"]))
    prods_of_tpl = defaultdict(set)
    for r in tpl_prod:
        if r["templateID"] in tpl_by_id and r["renderStyleID"] in prod_by_id:
            prods_of_tpl[r["templateID"]].add(r["renderStyleID"])
    # Template->opinion links may point at ANY version; remap to the latest.
    ops_of_tpl = defaultdict(set)
    for r in tpl_op:
        latest_id = latest_of.get(r["opinionID"])
        if r["templateID"] in tpl_by_id and latest_id in op_by_id:
            ops_of_tpl[r["templateID"]].add(latest_id)
    tpls_of_q = defaultdict(set)
    for r in tpl_q:
        if r["templateID"] in tpl_by_id:
            tpls_of_q[r["questionID"]].add(r["templateID"])
    subj_by_id = {s["id"]: s for s in subjects}
    q_by_id = {q["id"]: q for q in questions}

    # Section-path walk. The data is SEQUENTIAL — there is no true hierarchy,
    # only dividers with styling — so breadcrumbs are capped at TWO levels:
    #   anchor  = last Line-Above divider (visual separator), or the first
    #             divider after a part boundary ('Warning'/'Scope of report')
    #   parent  = FIRST non-anchor divider of the latest consecutive divider
    #             run (the renderer shows that one as the section heading;
    #             later dividers in the run are not rendered as headings)
    #   answer question -> path (anchor, parent), deduped
    # The same question under different paths is how legitimate "duplicates"
    # arise (e.g. 'Lapse' under Call vs Put option sections).
    paths_of_q: dict[int, set] = defaultdict(set)  # qid -> set of ancestor-path tuples
    tq_by_tpl: dict[int, list] = defaultdict(list)
    for r in tpl_q:
        if r["templateID"] in tpl_by_id and r["questionID"] in q_by_id:
            tq_by_tpl[r["templateID"]].append((r["displayOrder"] or 0, r["questionID"]))
    for tid, entries in tq_by_tpl.items():
        anchor = None
        run: list[int] = []                          # latest consecutive divider run
        prev_divider = False
        for _, qid in sorted(entries):
            if qid in divider_qids:
                if qid in line_above_qids or anchor is None:
                    anchor, run = qid, [qid]         # separator / first after boundary
                elif prev_divider:
                    run.append(qid)
                else:
                    run = [qid]                      # new run after content
                prev_divider = True
                parent = next((x for x in run if x != anchor and x != qid), None)
                own = tuple(x for x in (anchor, parent) if x)
                if own and own != (qid,):
                    paths_of_q[qid].add(own)
            elif not (rtypes_of_q.get(qid, set()) & {3, 6}):
                anchor, run, prev_divider = None, [], False   # part boundary
            else:
                prev_divider = False
                parent = next((x for x in run if x != anchor), None)
                path = tuple(dict.fromkeys(x for x in (anchor, parent) if x))
                if path:
                    paths_of_q[qid].add(path)
    subjs_of_q, qs_of_subj = defaultdict(set), defaultdict(set)
    for r in subj_q:
        if r["subjectID"] in subj_by_id and r["questionID"] in q_by_id:
            subjs_of_q[r["questionID"]].add(r["subjectID"])
            qs_of_subj[r["subjectID"]].add(r["questionID"])

    def prod_ref(pid): return {"id": pid, "name": prod_by_id[pid]["name"]}
    def op_ref(oid): return {"id": oid, "name": op_by_id[oid]["name"]}
    def tpl_ref(tid): return {"id": tid, "name": tpl_by_id[tid]["name"]}
    def subj_ref(sid): return {"id": sid, "name": subj_by_id[sid]["name"]}

    docs: dict[str, list[dict]] = {"product": [], "opinion": [], "template": [],
                                   "question": [], "subject": [], "jurisdiction": []}

    cats_of_prod = defaultdict(list)
    for r in prod_cat:
        cats_of_prod[r["renderStyleID"]].append(
            {"id": r["categoryID"], "name": r["categoryName"]})

    for p in products:
        desc = " — ".join(x for x in (clean(p["description"]), clean(p["SF_ProductName"])) if x)
        docs["product"].append({
            "_id": f"product:{p['id']}", "entityType": "product",
            "sourceId": p["id"], "title": clean(p["name"]), "description": desc or None,
            "metadata": {
                # self-reference: lets entitlement/product filters use one
                # uniform `in` on metadata.products across ALL entity types
                "products": [prod_ref(p["id"])],
                "categories": sorted(cats_of_prod.get(p["id"], []),
                                     key=lambda c: c["id"]),
                "sfProductId": clean(p["SF_ProductID"]),
                "sfProductFamily": clean(p["SF_ProductFamily"]),
                "status": p["status"]},
            "source": {"table": "RenderingStyle", "pk": "renderStyleID"}})

    for o in opinions:
        jurs = sorted(jur_of_op.get(o["id"], set()))
        docs["opinion"].append({
            "_id": f"opinion:{o['id']}", "entityType": "opinion",
            "sourceId": o["id"], "title": clean(o["name"]),
            "description": clean(o["description"]),
            "metadata": {
                "products": [prod_ref(p) for p in sorted(prods_of_op.get(o["id"], set()))],
                "jurisdictions": [j[0] for j in jurs],
                "jurisdictionCodes": [j[1] for j in jurs if j[1]],
                "orgId": o["orgID"], "version": o["version"],
                "status": o["status"], "effectiveDate": o["effectiveDate"],
                "modifiedDate": o["lastUpdatedOn"] or o["modifiedDate"]},
            "source": {"table": "Opinion", "pk": "opinionID"}})

    for t in templates:
        ops = sorted(ops_of_tpl.get(t["id"], set()))
        prods = set(prods_of_tpl.get(t["id"], set()))
        jurs, codes = set(), set()
        for oid in ops:
            prods |= prods_of_op.get(oid, set())
            for jn, jc in jur_of_op.get(oid, set()):
                jurs.add(jn)
                if jc:
                    codes.add(jc)
        docs["template"].append({
            "_id": f"template:{t['id']}", "entityType": "template",
            "sourceId": t["id"], "title": clean(t["name"]), "description": None,
            "metadata": {
                "products": [prod_ref(p) for p in sorted(prods)],
                "opinions": [op_ref(o) for o in ops],
                "jurisdictions": sorted(jurs), "jurisdictionCodes": sorted(codes),
                "templateType": t["templateType"], "status": t["status"],
                "modifiedDate": t["modifiedDate"]},
            "source": {"table": "Template", "pk": "templateID"}})

    # One doc per questionID (ids are unique entities; template membership is
    # many-to-many via TemplateQuestion). Report identical-text ids so
    # apparent duplicates in search results can be traced back to the DB.
    seen_titles: dict[str, list[int]] = defaultdict(list)
    for q in questions:
        seen_titles[(clean(q["name"]) or "").lower()].append(q["id"])
    dups = {t: ids for t, ids in seen_titles.items() if t and len(ids) > 1}
    if dups:
        print(f"  note: {len(dups)} question titles are shared by multiple ids, e.g.:")
        for t, ids in sorted(dups.items(), key=lambda kv: -len(kv[1]))[:5]:
            print(f"    {len(ids)}x '{t[:60]}' ids={ids[:10]}")

    for q in questions:
        tpls = sorted(tpls_of_q.get(q["id"], set()))
        ops, prods, jurs, codes = set(), set(), set(), set()
        for tid in tpls:
            ops |= ops_of_tpl.get(tid, set())
            prods |= prods_of_tpl.get(tid, set())
        for oid in ops:
            prods |= prods_of_op.get(oid, set())
            for jn, jc in jur_of_op.get(oid, set()):
                jurs.add(jn)
                if jc:
                    codes.add(jc)
        desc = " ".join(x for x in (clean(q["questionDescription"]),
                                    clean(q["questionComment"])) if x)
        docs["question"].append({
            "_id": f"question:{q['id']}", "entityType": "question",
            "sourceId": q["id"], "title": clean(q["name"]), "description": desc or None,
            "metadata": {
                "isTitleQuestion": q["id"] in divider_qids,
                # distinct section ancestors (filter/search) + display paths
                "mainQuestions": [{"id": a, "name": clean(q_by_id[a]["name"])}
                                  for a in sorted({a for p in paths_of_q.get(q["id"], set())
                                                   for a in p if a in q_by_id})],
                "breadcrumbs": sorted({" › ".join(
                    clean(q_by_id[a]["name"]) for a in p if a in q_by_id)
                    for p in paths_of_q.get(q["id"], set())})[:10],
                "subjects": [subj_ref(s) for s in sorted(subjs_of_q.get(q["id"], set()))],
                "templates": [tpl_ref(t) for t in tpls],
                "opinions": [op_ref(o) for o in sorted(ops)],
                "products": [prod_ref(p) for p in sorted(prods)],
                "jurisdictions": sorted(jurs), "jurisdictionCodes": sorted(codes),
                "status": q["status"], "modifiedDate": q["modifiedDate"]},
            "source": {"table": "Question", "pk": "questionID"}})

    # Jurisdictions as first-class entities. Products derived from the opinions
    # in that jurisdiction; self-reference in metadata.jurisdictions so the
    # jurisdiction entitlement filter applies uniformly.
    jurname_prods, jurname_ops = defaultdict(set), defaultdict(int)
    for oid, prods_set in prods_of_op.items():
        for jn, _ in jur_of_op.get(oid, set()):
            jurname_prods[jn] |= prods_set
    for oid in op_by_id:
        for jn, _ in jur_of_op.get(oid, set()):
            jurname_ops[jn] += 1
    for j in jurisdictions:
        docs["jurisdiction"].append({
            "_id": f"jurisdiction:{j['id']}", "entityType": "jurisdiction",
            "sourceId": j["id"], "title": clean(j["name"]),
            "description": None,
            "metadata": {
                "jurisdictions": [j["name"]],
                "jurisdictionCodes": [j["jurisdictionCode"]] if j["jurisdictionCode"] else [],
                "products": [prod_ref(p) for p in sorted(jurname_prods.get(j["name"], set()))],
                "opinionCount": jurname_ops.get(j["name"], 0),
                "parentId": j["parentID"], "continentId": j["continentID"],
                "status": j["status"]},
            "source": {"table": "Jurisdiction", "pk": "jurisdictionID"}})

    for s in subjects:
        qids = sorted(qs_of_subj.get(s["id"], set()))
        if not qids:      # org scoping: drop subjects with no in-scope questions
            continue
        tpls, prods, jurs, codes = set(), set(), set(), set()
        for qid in qids:
            tpls |= tpls_of_q.get(qid, set())
        for tid in tpls:
            prods |= prods_of_tpl.get(tid, set())
            for oid in ops_of_tpl.get(tid, set()):
                prods |= prods_of_op.get(oid, set())
                for jn, jc in jur_of_op.get(oid, set()):
                    jurs.add(jn)
                    if jc:
                        codes.add(jc)
        docs["subject"].append({
            "_id": f"subject:{s['id']}", "entityType": "subject",
            "sourceId": s["id"], "title": clean(s["name"]),
            "description": clean(s["subjectDescription"]),
            "metadata": {
                # question->subject binding is one-way: subjects do NOT carry
                # question titles (they'd hijack question-text searches);
                # only the derived ancestry is kept
                "questionCount": len(qids),
                "templates": [tpl_ref(t) for t in sorted(tpls)],
                "products": [prod_ref(p) for p in sorted(prods)],
                "jurisdictions": sorted(jurs), "jurisdictionCodes": sorted(codes),
                "status": s["status"], "modifiedDate": s["subjectCreatedDate"]},
            "source": {"table": "Subject", "pk": "subjectID"}})

    return docs


# ------------------------------------------------------------------ load

def load(docs: dict[str, list[dict]]) -> None:
    from pymongo import ReplaceOne

    client = mongo_client()
    coll = client[os.environ.get("ATLAS_DB", "aosphere_search")][
        os.environ.get("ATLAS_COLLECTION", "entities")]

    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    chunk_size = 500
    for etype, batch in docs.items():
        if not batch:
            continue
        for d in batch:
            d["syncRun"] = run_id
            d["syncedAt"] = now
        t0 = time.time()
        upserted = modified = 0
        for i in range(0, len(batch), chunk_size):
            chunk = batch[i:i + chunk_size]
            res = coll.bulk_write(
                [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in chunk],
                ordered=False)
            upserted += res.upserted_count
            modified += res.modified_count
            done = min(i + chunk_size, len(batch))
            print(f"  {etype}: {done}/{len(batch)} ({time.time() - t0:.0f}s)",
                  end="\r", flush=True)
        stale = coll.delete_many({"entityType": etype, "syncRun": {"$ne": run_id}})
        print(f"  {etype}: upserted={upserted} modified={modified} "
              f"stale_removed={stale.deleted_count} total={len(batch)} "
              f"in {time.time() - t0:.0f}s")

    ensure_search_index(coll)
    client.close()


def ensure_search_index(coll) -> None:
    from pymongo.operations import SearchIndexModel

    existing = {ix["name"] for ix in coll.list_search_indexes()}
    if INDEX_NAME in existing:
        coll.update_search_index(INDEX_NAME, SEARCH_INDEX_DEFINITION)
        print(f"    index '{coll.name}.{INDEX_NAME}' updated")
    else:
        coll.create_search_index(SearchIndexModel(SEARCH_INDEX_DEFINITION, name=INDEX_NAME))
        print(f"    index '{coll.name}.{INDEX_NAME}' created (builds async, ~1 min)")


# -------------------------------------------------------- OpenSearch (default)

def _entity_backend(cli_val: str | None = None) -> str:
    """opensearch (default) | atlas — mirrors the app's ACI_ENTITY_SEARCH_BACKEND."""
    return (cli_val or os.getenv("ACI_ENTITY_SEARCH_BACKEND", "opensearch")).strip().lower()


def _os_client():
    # Reuse the app's client factory (SigV4 for an AWS domain/serverless, plain HTTP for
    # local Docker) so the loader authenticates exactly like the query path.
    from aosphere_core_index.embeddings.vector_backend import _os_client as f
    return f()


def _os_alias() -> str:
    from aosphere_core_index.service.entity_index import INDEX_ALIAS
    return INDEX_ALIAS


def ensure_os_entity_index(client, index_name: str, *, bulk_mode: bool = False) -> None:
    """Create a concrete entity index from the shared entity_index definition. When
    bulk_mode, disable refresh for a faster load (caller refreshes + restores after)."""
    from aosphere_core_index.service.entity_index import index_body

    body = index_body()
    if bulk_mode:
        body["settings"]["index"]["refresh_interval"] = "-1"
    if client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
    client.indices.create(index=index_name, body=body)


def _swap_alias(client, alias: str, new_index: str) -> None:
    """Atomically point `alias` at `new_index`, removing it from any other index. If a
    CONCRETE index literally named `alias` exists (from a pre-alias load), drop it first —
    an alias can't share a name with an index."""
    if client.indices.exists(index=alias) and not client.indices.exists_alias(name=alias):
        client.indices.delete(index=alias)
    actions: list[dict] = []
    try:
        for idx in client.indices.get_alias(name=alias):
            if idx != new_index:
                actions.append({"remove": {"index": idx, "alias": alias}})
    except Exception:
        pass  # alias doesn't exist yet
    actions.append({"add": {"index": new_index, "alias": alias}})
    client.indices.update_aliases(body={"actions": actions})


def _delete_old_entity_indices(client, alias: str, keep: str) -> None:
    try:
        for idx in client.indices.get(index=f"{alias}-*"):
            if idx != keep and not idx.endswith("-meta"):
                client.indices.delete(index=idx)
    except Exception:
        pass


def load_opensearch_entities(docs: dict[str, list[dict]], run_stamp: str) -> dict:
    """Bulk-load all entities into a fresh concrete index, then atomically repoint the
    `aci-entities` alias — zero-downtime reindex (readers keep hitting the old index until
    the swap). The completion field (title_suggest) is derived per doc from entity_index."""
    from opensearchpy import helpers

    from aosphere_core_index.service.entity_index import suggest_field

    client = _os_client()
    alias = _os_alias()
    new_index = f"{alias}-{run_stamp}"
    ensure_os_entity_index(client, new_index, bulk_mode=True)

    def actions():
        for etype, batch in docs.items():
            for d in batch:
                src = {k: v for k, v in d.items() if k != "_id"}
                sf = suggest_field(d)
                if sf is not None:
                    src["title_suggest"] = sf
                yield {"_index": new_index, "_id": d["_id"], "_source": src}

    t0 = time.time()
    ok, errs = helpers.bulk(client, actions(), chunk_size=1000, request_timeout=180,
                            raise_on_error=False)
    client.indices.put_settings(index=new_index, body={"index": {"refresh_interval": "1s"}})
    client.indices.refresh(index=new_index)
    count = client.count(index=new_index)["count"]
    n_err = len(errs) if isinstance(errs, list) else errs
    print(f"  opensearch: indexed={ok} errors={n_err} count={count} "
          f"into {new_index} in {time.time() - t0:.0f}s")
    if n_err:
        print(f"    first error: {errs[0]}" if isinstance(errs, list) and errs else "")
    _swap_alias(client, alias, new_index)
    print(f"    alias '{alias}' -> {new_index}")
    _delete_old_entity_indices(client, alias, keep=new_index)
    return {"backend": "opensearch", "index": new_index, "indexed": ok,
            "errors": n_err, "count": count}


# --- fingerprint storage (OpenSearch): tiny KV index so --if-changed works for cron ---

def _os_meta_index(client) -> str:
    name = f"{_os_alias()}-meta"
    if not client.indices.exists(index=name):
        client.indices.create(index=name, body={"mappings": {"enabled": False}})
    return name


def os_get_fingerprint(client) -> dict | None:
    try:
        return client.get(index=f"{_os_alias()}-meta",
                          id="source_fingerprint")["_source"].get("fp")
    except Exception:
        return None


def os_set_fingerprint(client, fp: dict) -> None:
    name = _os_meta_index(client)
    client.index(index=name, id="source_fingerprint",
                 body={"fp": fp, "syncedAt": datetime.now(timezone.utc).isoformat()})
    client.indices.refresh(index=name)


def verify_opensearch() -> None:
    from aosphere_core_index.service import entity_backend

    client = _os_client()
    alias = _os_alias()
    count = client.count(index=alias)["count"]
    by_type = client.search(index=alias, body={"size": 0, "aggs": {
        "t": {"terms": {"field": "entityType", "size": 20}}}})
    dist = {b["key"]: b["doc_count"] for b in by_type["aggregations"]["t"]["buckets"]}
    print(f"\nOpenSearch '{alias}': {count} docs {dist}")
    samples = [
        ("suggest 'sing'", lambda: [s["title"] for s in
                                    entity_backend.suggest(q="sing")][:5]),
        ("suggest fuzzy 'singaore'", lambda: [s["title"] for s in
                                              entity_backend.suggest(q="singaore")][:5]),
        ("search 'netting'", lambda: [f"[{h['entityType']}] {h['title']}" for h in
                                      entity_backend.search(q="netting", size=5)][:5]),
        ("search 'collateral' jur=Singapore",
         lambda: [f"[{h['entityType']}] {h['title']}" for h in entity_backend.search(
             q="collateral", size=5, types=["question"], jurisdictions=["Singapore"])][:5]),
    ]
    for label, run in samples:
        try:
            print(f"\n{label}:")
            rows = run()
            for r in rows:
                print(f"  {str(r)[:80]}")
            if not rows:
                print("  (no hits)")
        except Exception as e:  # noqa: BLE001
            print(f"\n{label}: FAILED — {e}")


# ---------------------------------------------------------------- verify

def wait_for_index(coll, timeout_s: int = 600) -> bool:
    """Poll until the search index is queryable (Atlas builds it async)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        ix = next((i for i in coll.list_search_indexes() if i["name"] == INDEX_NAME), None)
        status = (ix or {}).get("status", "MISSING")
        if ix and ix.get("queryable"):
            print(f"  index '{INDEX_NAME}' is queryable (status {status}, "
                  f"waited {time.time() - t0:.0f}s)")
            return True
        print(f"  index '{INDEX_NAME}': {status} — waiting... "
              f"({time.time() - t0:.0f}s)", end="\r", flush=True)
        time.sleep(10)
    print(f"\n  gave up after {timeout_s}s — check Atlas UI (Search tab)")
    return False


def verify() -> None:
    client = mongo_client()
    coll = client[os.environ.get("ATLAS_DB", "aosphere_search")][
        os.environ.get("ATLAS_COLLECTION", "entities")]
    print("\nDoc counts by type:", {r["_id"]: r["n"] for r in coll.aggregate(
        [{"$group": {"_id": "$entityType", "n": {"$sum": 1}}}])})
    if not wait_for_index(coll):
        client.close()
        return
    samples = [
        ("spotlight: autocomplete 'sing' across all entities",
         {"autocomplete": {"query": "sing", "path": "title"}}),
        ("text 'netting' over title+description",
         {"text": {"query": "netting", "path": ["title", "description"]}}),
        ("questions only: 'collateral' filtered to jurisdiction 'Singapore'",
         {"compound": {"must": [{"text": {"query": "collateral", "path": ["title", "description"]}}],
                       "filter": [{"equals": {"path": "entityType", "value": "question"}},
                                  {"text": {"path": "metadata.jurisdictions", "query": "Singapore"}}]}}),
        ("opinions only: autocomplete 'luxem'",
         {"compound": {"must": [{"autocomplete": {"query": "luxem", "path": "title"}}],
                       "filter": [{"equals": {"path": "entityType", "value": "opinion"}}]}}),
    ]
    for label, operator in samples:
        try:
            hits = list(coll.aggregate([
                {"$search": {"index": INDEX_NAME, **operator}},
                {"$limit": 3},
                {"$project": {"title": 1, "entityType": 1,
                              "score": {"$meta": "searchScore"}}}]))
            print(f"\n{label}:")
            for h in hits:
                print(f"  [{h['entityType']}] {str(h.get('title'))[:80]} (score {h['score']:.2f})")
            if not hits:
                print("  (no hits — index may still be building)")
        except Exception as e:
            print(f"\n{label}: FAILED — {e}")
    client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", choices=["opensearch", "atlas"], default=None,
                    help="search backend to sync (default: ACI_ENTITY_SEARCH_BACKEND "
                         "or 'opensearch')")
    ap.add_argument("--dry-run", action="store_true",
                    help="extract + transform only; write sample to data/, no writes")
    ap.add_argument("--verify", action="store_true", help="run sample searches after sync")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip extract/sync; just run the sample searches")
    ap.add_argument("--index-only", action="store_true",
                    help="skip extract/sync; just push/create the search index definition")
    ap.add_argument("--status", default="1",
                    help="comma-separated status values to include, or 'all' (default: 1)")
    ap.add_argument("--org", default=os.environ.get("ACI_SYNC_ORG", "101"),
                    help="organisation id to scope content to, or 'all' (default: 101)")
    ap.add_argument("--if-changed", action="store_true",
                    help="fingerprint source tables first; skip the sync when "
                         "nothing changed since the last run (for cron/CronJob)")
    args = ap.parse_args()

    load_env()
    backend = _entity_backend(args.backend)
    is_os = backend == "opensearch"
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    fp = None
    if args.if_changed:
        fp = source_fingerprint()
        if is_os:
            prev = os_get_fingerprint(_os_client())
        else:
            client = mongo_client()
            prev_doc = client[os.environ.get("ATLAS_DB", "aosphere_search")][
                "sync_meta"].find_one({"_id": "source_fingerprint"})
            client.close()
            prev = prev_doc.get("fp") if prev_doc else None
        if prev == fp:
            print("Source unchanged since last sync — skipping.")
            return
        changed = [t for t in fp if not prev or prev.get(t) != fp[t]]
        print(f"Source changed ({', '.join(changed[:6])}{'…' if len(changed) > 6 else ''}) "
              f"— running sync.")
    if args.index_only:
        if is_os:
            client = _os_client()
            idx = f"{_os_alias()}-{run_stamp}"
            ensure_os_entity_index(client, idx)
            _swap_alias(client, _os_alias(), idx)
            _delete_old_entity_indices(client, _os_alias(), keep=idx)
            print(f"opensearch: created empty {idx} and pointed alias '{_os_alias()}'")
        else:
            client = mongo_client()
            ensure_search_index(client[os.environ.get("ATLAS_DB", "aosphere_search")][
                os.environ.get("ATLAS_COLLECTION", "entities")])
            client.close()
        return
    if args.verify_only:
        verify_opensearch() if is_os else verify()
        return
    print(f"Extracting from {os.environ['MSSQL_HOST']}/{os.environ['MSSQL_DATABASE']} "
          f"(status filter: {args.status}, org: {args.org})")
    docs = extract(args.status, args.org)
    total = sum(len(v) for v in docs.values())
    print(f"Built {total} documents: " + ", ".join(f"{k}={len(v)}" for k, v in docs.items()))

    if args.dry_run:
        out = REPO_ROOT / "data" / "atlas_sync_dryrun_sample.json"
        sample = {k: v[:3] for k, v in docs.items()}
        out.write_text(json.dumps(sample, indent=2, default=str))
        print(f"Dry run — wrote 3 sample docs per entity to {out}")
        return

    if is_os:
        print(f"Loading into OpenSearch (alias '{_os_alias()}')...")
        load_opensearch_entities(docs, run_stamp)
        if fp is not None:  # record fingerprint only after a successful load
            os_set_fingerprint(_os_client(), fp)
        if args.verify:
            verify_opensearch()
    else:
        if "MONGODB_URI" not in os.environ:
            sys.exit("MONGODB_URI not set (add it to .env)")
        print("Loading into Atlas...")
        load(docs)
        if fp is not None:  # record fingerprint only after a successful load
            client = mongo_client()
            client[os.environ.get("ATLAS_DB", "aosphere_search")]["sync_meta"].update_one(
                {"_id": "source_fingerprint"},
                {"$set": {"fp": fp, "syncedAt": datetime.now(timezone.utc)}}, upsert=True)
            client.close()
        if args.verify:
            verify()
    print("Done.")


if __name__ == "__main__":
    main()
