"""Eval-case test suite: run the eval cases in test/*.xlsx and report coverage.

The workbooks hold two kinds of case:
  * retrieval/citation cases — a question + the expected "correct doc+section
    citation" (a jurisdiction + one or more clause keys). We check whether the
    system RETRIEVES that clause for that jurisdiction (citation hit@k). This is
    objective and cheap (no LLM), so it runs at scale by default.
  * behavioural cases — expected ACTION is refuse / out-of-scope / disambiguate
    rather than a citation. These need the agent + judgement; they are counted
    and listed but not auto-scored here (that's a separate agent eval).

Sources parsed:
  - ph1 v1  'retrieval_eval'  (Data Privacy; question + jur_soft_link "GB_B2")
  - SD v1   topic sheets      (Shareholding Disclosure; "AU Survey A3.1")
  - v2      special-case tabs (behavioural; expected action + optional citation)

Usage:
  .venv/bin/python scripts/eval_cases.py [--k 20] [--product P] [--limit N]
Writes test/eval_report.md + test/eval_report.json and prints a summary.
Requires ACI_OFFLINE=1 ACI_EMBED_BACKEND=titan and Bedrock creds (Titan query embed).
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import defaultdict

import openpyxl

from aosphere_core_index.regions.region_map import PRODUCTS, qualified, split_region

# ISO-2 (and a few names/aliases) -> our jurisdiction identity.
ISO = {
    "GB": "United Kingdom", "UK": "United Kingdom", "DE": "Germany", "KR": "South Korea",
    "AU": "Australia", "BR": "Brazil", "ES": "Spain", "EU": "European Union", "IN": "India",
    "JM": "Jamaica", "KZ": "Kazakhstan", "MX": "Mexico", "NO": "Norway", "NZ": "New Zealand",
    "SV": "El Salvador", "VN": "Vietnam", "FR": "France", "BE": "Belgium", "IT": "Italy",
    "US": "United States", "JP": "Japan", "CN": "China", "HK": "Hong Kong", "SG": "Singapore",
    "CA": "Canada", "IE": "Ireland", "NL": "Netherlands", "PL": "Poland", "SE": "Sweden",
    "CH": "Switzerland", "AT": "Austria", "PT": "Portugal", "ZA": "South Africa",
}
_SECTION = re.compile(r"^[A-K]\d[\w.()]*$")
_ROOT = "test"
DP, SD = "Data Privacy", "Shareholding Disclosure"


def _jur(token: str) -> str | None:
    t = (token or "").strip()
    if not t:
        return None
    if t.upper() in ISO:
        return ISO[t.upper()]
    return t  # already a full name (best effort)


def _sections(text: str) -> list[str]:
    """Clause keys from a citation tail, e.g. 'A6.7, A6.8, A7.1' -> [...]."""
    out = []
    for tok in re.split(r"[,;]| and ", text or ""):
        tok = tok.strip().rstrip(".")
        # keep leading clause-key token only (drop trailing prose)
        m = re.match(r"([A-K]\d[\w.()]*)", tok)
        if m:
            out.append(m.group(1))
    return out


def parse_citation(cite: str, soft: bool) -> list[tuple[str, str]]:
    """-> [(jurisdiction, section_key)] from a citation string."""
    cite = str(cite or "").strip()
    if not cite:
        return []
    if soft:  # "GB_B2"
        code, _, sec = cite.partition("_")
        j = _jur(code)
        return [(j, sec.strip())] if j and _SECTION.match(sec.strip()) else []
    # "AU Survey A3.1, A3.2"  /  "Belgium Survey A2.3"
    m = re.match(r"(.+?)\s+Survey\s+(.*)$", cite, re.I)
    if not m:
        return []
    j = _jur(m.group(1))
    return [(j, s) for s in _sections(m.group(2))] if j else []


def load_cases() -> list[dict]:
    cases = []
    # --- DP retrieval_eval (machine-readable) ---
    f = f"{_ROOT}/aosphere AI project ph 1 - eval cases v1.xlsx"
    if os.path.exists(f):
        wb = openpyxl.load_workbook(f, data_only=True)
        for r in list(wb["retrieval_eval"].iter_rows(values_only=True))[1:]:
            if not r[2]:
                continue
            cases.append({"product": DP, "sheet": f"DP:{r[0]}", "group": r[1] or "",
                          "question": str(r[2]).strip(), "action": (r[3] or "Answer"),
                          "expected": parse_citation(r[4], soft=True), "source": "ph1 retrieval_eval"})
        wb.close()
    # --- SD topic sheets ---
    f = f"{_ROOT}/aosphere adv search - SD - eval cases v1.xlsx"
    if os.path.exists(f):
        wb = openpyxl.load_workbook(f, data_only=True)
        for s in wb.sheetnames:
            if s.lower() in ("tools",):
                continue
            for r in wb[s].iter_rows(values_only=True):
                q = r[0] if r else None
                cite = r[2] if len(r) > 2 else None
                if not q or not cite or "Survey" not in str(cite):
                    continue
                cases.append({"product": SD, "sheet": f"SD:{s}", "group": "",
                              "question": str(q).strip(), "action": (r[1] or "Answer"),
                              "expected": parse_citation(cite, soft=False), "source": "SD v1"})
        wb.close()
    # --- v2 special / behavioural ---
    f = f"{_ROOT}/aosphere AI project - eval cases v2 (special cases tabs).xlsx"
    if os.path.exists(f):
        wb = openpyxl.load_workbook(f, data_only=True)
        for s in wb.sheetnames:
            for r in wb[s].iter_rows(values_only=True):
                q = r[0] if r else None
                if not q or str(q).strip().lower().startswith(("extra cases", "cases where", "out of product")):
                    continue
                action = r[1] if len(r) > 1 else ""
                cite = r[11] if len(r) > 11 else None
                exp = parse_citation(cite, soft=False) if cite else []
                cases.append({"product": DP, "sheet": f"v2:{s}", "group": "",
                              "question": str(q).strip(), "action": str(action or "").strip(),
                              "expected": exp, "source": "v2 special"})
        wb.close()
    return cases


def _nk(key: str) -> str:
    """Normalise a clause key so sub-markers compare uniformly: 'H1.6(a)' -> 'H1.6.a'
    (parens become dot-levels), so a retrieved sub-clause matches its expected parent."""
    return key.replace("(", ".").replace(")", "").strip(".")


def _section_root(key: str) -> str:
    """Topic/section root of a key: the Part+number head, e.g. 'H1.6(a)' -> 'H1',
    'A3.5.7' -> 'A3', 'B2' -> 'B2'. The first dotted token already carries the part
    letter + top number, so the root is just that token."""
    return _nk(key).split(".")[0]


def hit_ok(expected_sections: list[str], hit_keys: list[str]) -> tuple[bool, int]:
    """Pass if any expected clause (or its family — parent/child) appears in the
    retrieved keys. Returns (passed, rank_of_first_match or -1). Paren sub-markers
    are normalised so 'H1.6(a)' matches expected 'H1.6'."""
    exps = [_nk(e) for e in expected_sections]
    for rank, k in enumerate(hit_keys):
        nk = _nk(k)
        for e in exps:
            if nk == e or nk.startswith(e + ".") or e.startswith(nk + "."):
                return True, rank
    return False, -1


def section_rank(expected_sections: list[str], hit_keys: list[str]) -> int:
    """Rank of the first hit in the same SECTION (topic area) as any expected clause,
    e.g. expected H1.7 is 'found' by any H1.* hit. -1 if none."""
    roots = {_section_root(e) for e in expected_sections}
    for rank, k in enumerate(hit_keys):
        if _section_root(k) in roots:
            return rank
    return -1


def main() -> None:
    k = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--k=")), 20)
    prod_filter = next((a.split("=")[1] for a in sys.argv if a.startswith("--product=")), None)
    limit = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--limit=")), 0)

    from aosphere_core_index.embeddings.multi_index import load_multi
    from aosphere_core_index.service.registry import search_all
    indexed = set(load_multi().regions)

    cases = load_cases()
    if prod_filter:
        cases = [c for c in cases if c["product"] == prod_filter]
    # classify
    for c in cases:
        c["region_id"] = qualified(c["product"], c["expected"][0][0]) if c["expected"] else None
        c["scorable"] = bool(c["expected"]) and c["region_id"] in indexed
    scorable = [c for c in cases if c["scorable"]]
    if limit:
        scorable = scorable[:limit]
    print(f"loaded {len(cases)} cases | retrieval-scorable {len(scorable)} "
          f"(k={k}); running…", flush=True)

    for i, c in enumerate(scorable, 1):
        exp_secs = [s for (_j, s) in c["expected"]]
        try:
            hits = search_all(c["question"], k=k, jurisdictions=[c["region_id"]])
            keys = [h["key"] for h in hits[:k]]
            ok, rank = hit_ok(exp_secs, keys)
            srank = section_rank(exp_secs, keys)
        except Exception as e:  # noqa: BLE001
            ok, rank, keys, srank = False, -1, [], -1
            c["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        c["pass"] = ok
        c["rank"] = rank            # rank of exact clause/family match
        c["section_rank"] = srank   # rank of first hit in the right topic section
        c["top_keys"] = keys[:5]
        if i % 25 == 0:
            print(f"  {i}/{len(scorable)}", flush=True)

    _report(cases, scorable, k)


def _report(cases, scorable, k) -> None:
    from collections import Counter
    by_sheet = defaultdict(lambda: {"total": 0, "scorable": 0, "pass": 0, "not_indexed": 0, "behavioural": 0})
    for c in cases:
        b = by_sheet[c["sheet"]]
        b["total"] += 1
        if c.get("scorable"):
            b["scorable"] += 1
            if c.get("pass"):
                b["pass"] += 1
        elif c["expected"] and c["region_id"] not in _INDEXED:
            b["not_indexed"] += 1
        else:
            b["behavioural"] += 1

    n_pass = sum(1 for c in scorable if c.get("pass"))
    lines = ["# Eval-case suite report", ""]
    lines.append(f"- Total cases parsed: **{len(cases)}**")
    lines.append(f"- Retrieval-scorable (has expected citation + jurisdiction indexed): **{len(scorable)}**")
    if scorable:
        lines.append(f"- **Citation hit@{k}: {n_pass}/{len(scorable)} ({100*n_pass/len(scorable):.0f}%)**")
    beh = sum(1 for c in cases if not c.get("scorable") and not (c["expected"] and c["region_id"] not in _INDEXED))
    ni = sum(1 for c in cases if c["expected"] and c["region_id"] not in _INDEXED)
    lines.append(f"- Behavioural / no-citation cases (need agent eval): **{beh}**")
    lines.append(f"- Cases whose jurisdiction isn't in the index: **{ni}**")
    # Record the retrieval pipeline this report reflects (reranker backend + query
    # expansion), so the committed numbers are unambiguous. Prefer a meta stamped at
    # RUN time (correct even when the .md is later regenerated from cached JSON in a
    # different env); fall back to the live env only if none was stamped.
    meta = _META
    if meta is None:
        try:
            from aosphere_core_index.embeddings import query_expand as _qe
            from aosphere_core_index.embeddings.reranker import backend as _rb
            meta = {"reranker": _rb(), "query_expand": _qe.enabled()}
        except Exception:  # noqa: BLE001 — never let a config-probe break the report
            meta = {}
    if meta:
        lines.append(f"- Pipeline: reranker=`{meta.get('reranker','?')}`, "
                     f"query_expansion=`{meta.get('query_expand','?')}`")
    # ranking quality — what matters is the right clause in the FIRST few results
    def at(cases, field, kk):
        n = len(cases) or 1
        return 100 * sum(1 for c in cases if 0 <= c.get(field, -1) < kk) / n

    def mrr(cases):
        n = len(cases) or 1
        return sum(1 / (c["rank"] + 1) for c in cases if c.get("rank", -1) >= 0) / n

    lines += ["", "## Ranking quality (exact clause) — hit@N", "",
              "_**hit@N** = share of queries whose exact expected clause is in the top N reranked results "
              "(higher is better; it's a yes/no per query). **MRR** = mean reciprocal rank: the average of "
              "1 ⁄ (rank of the first correct clause) — 1.0 if it's always #1, 0.5 if #2, 0.33 if #3, 0 if "
              "not found. It rewards ranking the answer high; MRR ≈ 0.62 means the right clause typically "
              "sits around rank 1–2._", "",
              "| Scope | n | hit@1 | hit@3 | hit@5 | hit@10 | hit@20 | hit@40 | MRR |",
              "|---|---|---|---|---|---|---|---|---|"]
    for label, sc in [("Overall", scorable)] + [(p, [c for c in scorable if c["product"] == p]) for p in PRODUCTS]:
        if sc:
            lines.append(f"| {label} | {len(sc)} | {at(sc,'rank',1):.0f}% | {at(sc,'rank',3):.0f}% | "
                         f"{at(sc,'rank',5):.0f}% | {at(sc,'rank',10):.0f}% | {at(sc,'rank',20):.0f}% | "
                         f"{at(sc,'rank',40):.0f}% | {mrr(sc):.2f} |")
    lines += ["", "## Topic-area hit (expected clause's section in top-N)", "",
              "_Credits returning the right section family (e.g. any H1.* for expected H1.7) — "
              "closer to 'is the relevant area surfaced'._", "",
              "| Scope | n | sec@3 | sec@5 | sec@10 |", "|---|---|---|---|---|"]
    for label, sc in [("Overall", scorable)] + [(p, [c for c in scorable if c["product"] == p]) for p in PRODUCTS]:
        if sc:
            lines.append(f"| {label} | {len(sc)} | {at(sc,'section_rank',3):.0f}% | "
                         f"{at(sc,'section_rank',5):.0f}% | {at(sc,'section_rank',10):.0f}% |")
    # per product
    lines += ["", "## By product", "", "| Product | scorable | hit@%s |" % k, "|---|---|---|"]
    for p in PRODUCTS:
        sc = [c for c in scorable if c["product"] == p]
        if sc:
            lines.append(f"| {p} | {len(sc)} | {100*sum(c.get('pass',0) for c in sc)/len(sc):.0f}% |")
    # per sheet
    lines += ["", "## By sheet", "", "| Sheet | total | scorable | hit@%d | behavioural | not-indexed |" % k, "|---|---|---|---|---|---|"]
    for sh in sorted(by_sheet):
        b = by_sheet[sh]
        rate = f"{100*b['pass']/b['scorable']:.0f}%" if b["scorable"] else "—"
        lines.append(f"| {sh} | {b['total']} | {b['scorable']} | {rate} | {b['behavioural']} | {b['not_indexed']} |")
    # Behavioural cases — no target clause, so retrieval hit@k does NOT apply: success is
    # the agent's ACTION, given in the source `action` column. These are reported here for
    # visibility but must be scored by a separate agent-behaviour pass, not this retriever.
    beh_cases = [c for c in cases if not c.get("scorable") and not c.get("expected")]
    if beh_cases:
        _BEH_DESC = {
            "v2:Redirect to tools": "multi-jurisdiction / cross-tool asks → redirect to the right aosphere tool (e.g. Compare), with advisory text",
            "v2:TL retrieval": "top-level / terse queries (e.g. 'dpo uk') → surface the topic",
            "v2:Negatives": "correct reply is a negative / advisory ('no, that isn't sufficient'), not a clause citation",
            "v2:Topics to not answer": "should REFUSE — historical / political / off-domain questions",
            "v2:Out-of-scope": "outside the survey's scope → decline or advise, don't fabricate a cite",
            "v2:Self-referencing": "about the assistant/tool itself ('what are you?') → handled specially",
        }
        lines += [
            "", f"## Behavioural cases — not retrieval-scored ({len(beh_cases)})", "",
            "_These have **no expected clause**, so citation hit@k can't grade them — success is "
            "the agent taking the right **action** (redirect / refuse / advisory / negative answer), "
            "recorded in the source `action` column. They are excluded from the hit@k tables above "
            "and need a separate **agent-behaviour eval** (does the agent act correctly?)._", "",
            "| Category | n | expected behaviour |", "|---|---|---|"]
        for sh, n in Counter(c["sheet"] for c in beh_cases).most_common():
            lines.append(f"| {sh} | {n} | {_BEH_DESC.get(sh, 'answer expected but no clause tagged in the source')} |")
    # misses — show expected vs the top keys actually returned (rank of the first
    # right-family hit in parens, so a near-miss is distinguishable from a total miss).
    misses = [c for c in scorable if not c.get("pass")]
    # Miss analysis — bucket each miss by the fix it needs (mirrors the architecture doc).
    # Heuristic: known typo probes + a hand-flagged gold-review set; otherwise a miss whose
    # right SECTION is in top-k is agent-solvable (the agent traverses to the leaf), a two-hop
    # question needs agent reasoning, and the rest are retrieval (jargon/vocab) gaps.
    import re as _re
    _TYPO = _re.compile(r"cooker|PCPI", _re.I)
    _GOLD = {("United Kingdom", "D1"), ("Spain", "B6"), ("Australia", "A5.21")}
    def _bucket(c):
        reg, key = c["expected"][0]
        if _TYPO.search(c["question"]):           return "retrieval"   # typo -> expansion/typo layer
        if c["sheet"].startswith("v2:Two-hop"):   return "agent"       # multi-hop reasoning
        if (reg, key) in _GOLD:                   return "gold"        # benchmark-label error
        if 0 <= c.get("section_rank", -1) < k:    return "agent"       # sibling: right section, agent traverses
        return "retrieval"                                             # jargon / vocab gap
    bc = Counter(_bucket(c) for c in misses)
    lines += ["", "## Miss analysis", "",
              "_Each miss bucketed by the fix it needs. Only the **retrieval** bucket is a system change; "
              "**agent-solvable** misses already surface the right section (the agent reads + traverses) or "
              "need multi-hop reasoning; **gold** misses are benchmark-label errors for review._", "",
              "| Bucket | n | how to close |", "|---|---|---|",
              f"| Retrieval-fixable | {bc['retrieval']} | broaden LLM query-expansion (jargon) + typo→retrieval |",
              f"| Agent-solvable | {bc['agent']} | right section already found (agent traverses); decomposition for two-hop |",
              f"| Gold / benchmark error | {bc['gold']} | eval-owner review of the expected citation |"]
    lines += ["", f"## Retrieval misses ({len(misses)})", ""]
    for c in misses[:80]:
        exp = ", ".join(f"{j}:{s}" for j, s in c["expected"])
        got = ", ".join(str(x) for x in (c.get("top_keys") or [])) or "(nothing)"
        sr = c.get("section_rank", -1)
        fam = f" · right-section@{sr + 1}" if sr is not None and sr >= 0 else " · section not found"
        lines.append(f"- [{c['sheet']}] **{c['question'][:70]}** — expected `{exp}`{fam}; got `{got}`")

    md = "\n".join(lines)
    open(f"{_ROOT}/eval_report.md", "w").write(md)
    json.dump({"cases": cases, "k": k, "meta": meta}, open(f"{_ROOT}/eval_report.json", "w"),
              default=str, indent=1)
    print("\n" + "\n".join(lines[:18]))
    print(f"\nsaved -> {_ROOT}/eval_report.md  +  {_ROOT}/eval_report.json")


_INDEXED: set = set()
_META: dict | None = None  # pipeline config stamped at run time (see __main__); regen reads it back

if __name__ == "__main__":
    from aosphere_core_index.embeddings import query_expand as _qe
    from aosphere_core_index.embeddings.multi_index import load_multi
    from aosphere_core_index.embeddings.reranker import backend as _rb
    _INDEXED = set(load_multi().regions)
    _META = {"reranker": _rb(), "query_expand": _qe.enabled()}  # correct env at run time
    main()
