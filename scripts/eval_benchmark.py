"""Evaluate the current AI engine against the graph-qa approved benchmark.

Runs each approved question through the agent and grades it two ways:

(a) EQUIVALENCE + RUBRIC judge — instead of strict match-the-reference, it asks
    whether the system conveys the same core legal conclusion, scoring it
    EQUIVALENT / SYSTEM_BETTER (more complete or more current) / SYSTEM_WORSE /
    DIVERGENT, plus a rubric (conclusion_correct, specifics, contradiction). PASS =
    EQUIVALENT or SYSTEM_BETTER — so a more-current/more-complete answer is not
    penalised for diverging from terse/stale gold.

(b) CITATION-GROUNDING (faithfulness) — gold-independent. Parses the answer's
    [Jurisdiction · ClauseKey] citations, fetches those clauses from the index, and
    checks the answer's claims are actually supported by the cited text (and that the
    cited clauses exist). Measures hallucination / faithfulness to the corpus directly.

  eval "$(aws configure export-credentials --profile dev1 --format env)"
  ACI_OFFLINE=1 ACI_EMBED_BACKEND=titan ACI_BEDROCK_REGION=eu-west-2 \
    .venv/bin/python scripts/eval_benchmark.py [--limit N] [--model ID] [--concurrency K]

Output: /tmp/eval_results.json
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import os
import re
from aosphere_core_index.config import settings as _settings

csv.field_size_limit(10 ** 7)

CSV = "ai-answers-export-2026-06-22T11_56_13.733Z.csv"
JUDGE_MODEL = os.getenv("ACI_JUDGE_MODEL", "eu.anthropic.claude-sonnet-4-6")
REGION = os.getenv("ACI_BEDROCK_REGION") or _settings.bedrock_region

_CC = {"AU": "Australia", "BE": "Belgium", "DE": "Germany", "FR": "France", "GB": "United Kingdom",
       "UK": "United Kingdom", "HK": "Hong Kong", "Z-HK": "Hong Kong", "IN": "India", "IT": "Italy",
       "JP": "Japan", "KR": "South Korea", "MX": "Mexico", "PE": "Peru", "PH": "Philippines",
       "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore", "TH": "Thailand", "TR": "Turkey",
       "UY": "Uruguay"}
_US = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
       "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
       "FL": "Florida", "GA": "Georgia", "GU": "Guam", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
       "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
       "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
       "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
       "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
       "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island",
       "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
       "VT": "Vermont", "VA": "Virginia", "VI": "Virgin Islands", "WA": "Washington", "WV": "West Virginia",
       "WI": "Wisconsin", "WY": "Wyoming"}


def _index_names() -> set[str]:
    return {p.split("/")[2] for p in glob.glob("data/regions/*/artifacts/*.content.json")}


def _resolve(code: str, idx: set[str]) -> str | None:
    code = code.strip()
    if code.startswith("US-"):
        st = _US.get(code[3:])
        if not st:
            return None
        cands = sorted((j for j in idx if j.startswith("United States -") and st in j),
                       key=lambda j: ("Long Form" not in j, j))  # prefer CA Long Form
        return cands[0] if cands else None
    name = _CC.get(code)
    return name if name in idx else None


def _clean(ans: str) -> str:
    ans = re.sub(r"#+\s*", "", ans)
    return re.split(r"_*This is a provisional", ans)[0].strip()


def load_gold() -> list[dict]:
    idx = _index_names()
    rows = [r for r in csv.DictReader(open(CSV, newline=""))
            if r["Approved"].strip().lower() == "true"
            and r["Query / Question"].strip() and r["Direct Answer"].strip()]
    out = []
    for r in rows:
        jur = _resolve(r["Relevant Jurisdictions"].strip(), idx)
        out.append({"q": r["Query / Question"].strip(), "gold": _clean(r["Direct Answer"]),
                    "code": r["Relevant Jurisdictions"].strip(), "jurisdiction": jur})
    return out


def _ask(client, prompt: str, max_tokens: int = 400) -> dict:
    """One temperature-0 judge call returning the JSON object it emits. Retries a few
    times on transient Bedrock/network errors so a blip doesn't crash a 105-q run."""
    import time
    last = None
    for attempt in range(4):
        try:
            r = client.converse(modelId=JUDGE_MODEL,
                                messages=[{"role": "user", "content": [{"text": prompt}]}],
                                inferenceConfig={"maxTokens": max_tokens, "temperature": 0})
            txt = r["output"]["message"]["content"][0]["text"]
            m = re.search(r"\{.*\}", txt, re.S)
            return json.loads(m.group(0)) if m else {"_parse_error": txt[:200]}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    return {"_parse_error": f"judge failed: {type(last).__name__}: {str(last)[:120]}"}


# ---- (a) equivalence + rubric judge ----
JUDGE_PROMPT = """Compare a SYSTEM answer to an APPROVED reference for the same \
data-privacy question. Ignore wording, length, formatting, and which clause numbers \
are cited — judge only the substantive legal conclusion.

Decide the relationship of SYSTEM to REFERENCE:
- EQUIVALENT   : same core conclusion.
- SYSTEM_BETTER: same conclusion AND adds correct or more-current detail.
- SYSTEM_WORSE : misses or muddles a material point the reference makes.
- DIVERGENT    : contradicts the reference, is off-topic, or fails to answer.

A more complete or more up-to-date answer is BETTER, not a penalty. PASS = EQUIVALENT \
or SYSTEM_BETTER.

QUESTION: {q}
JURISDICTION: {jur}
REFERENCE: {gold}
SYSTEM: {sys}

Respond ONLY as JSON:
{{"verdict":"EQUIVALENT|SYSTEM_BETTER|SYSTEM_WORSE|DIVERGENT",
  "conclusion_correct": true|false,
  "misses_key_point": true|false,
  "contradicts_or_hallucinates": true|false,
  "why":"<=20 words"}}"""

_PASS = {"EQUIVALENT", "SYSTEM_BETTER"}


def judge(client, q, jur, gold, sys_ans) -> dict:
    v = _ask(client, JUDGE_PROMPT.format(q=q, jur=jur, gold=gold, sys=sys_ans))
    v["pass"] = v.get("verdict") in _PASS
    return v


# ---- (b) citation-grounding faithfulness (gold-independent) ----
# Citation key: a clause (A2.2, C1.2(a)) OR a bare Part letter (G) — the latter has
# no digit, so requiring one silently dropped Part-level citations like [Wyoming · G].
# Clause keys ([A-K]…) plus alert keys (ALERT:<id>) — both are citable sources.
_CITE_RE = re.compile(r"\[([^\]·|]+?)\s*[·|]\s*(ALERT:\d+|[A-K](?:\d[\w.()\-]*)?)\]")


def _clause_text(jurisdiction: str, key: str) -> str | None:
    """Full text of a clause (+ its sub-clauses) from the index, or None if the
    citation doesn't resolve (i.e. a hallucinated/invalid clause key). Alert
    citations (ALERT:<id>) resolve against the region's alerts_by_id."""
    from aosphere_core_index.service.registry import get_bundle
    try:
        bundle = get_bundle(jurisdiction)
    except Exception:
        return None
    if key.startswith("ALERT:"):
        rec = bundle.content.get("alerts_by_id", {}).get(key.split(":", 1)[1])
        if not rec:
            return None
        return (f"ALERT: {rec.get('title', '')}\n{rec.get('summary', '')}\n"
                f"{rec.get('attachment_text', '')[:4000]}")
    secs = bundle.content["sections"]
    idx = {s["key"]: i for i, s in enumerate(secs)}
    sec = bundle.sections_by_key.get(key)
    if sec is None or key not in idx:
        return None
    parts, lvl = [], sec["level"]
    for s in secs[idx[key]:]:
        if s is not sec and s["level"] <= lvl:
            break
        body = " ".join(e["text"] for e in s["elements"] if e.get("text"))
        if body:
            parts.append(f"[{s['key']}] {s['title']}: {body}")
    # Match the agent's read cap (ACI_READ_CHARS=12000): a smaller cap here truncates
    # the cited clause and makes the grounding judge falsely report specifics "absent".
    return "\n".join(parts)[:12000] or sec["title"]


FAITH_PROMPT = """Check whether a data-privacy ANSWER is faithful to the SOURCE clauses \
it cites — independent of any reference answer. For each substantive claim the answer \
attributes to a citation, is it actually supported by that clause's text below?

ANSWER:
{ans}

CITED SOURCE CLAUSES (only these were cited):
{sources}

Respond ONLY as JSON:
{{"grounded":"yes|mostly|no",
  "unsupported_claims": ["<claim not supported by the cited clauses>", ...],
  "why":"<=20 words"}}"""


def faithfulness(client, jurisdiction: str, sys_ans: str) -> dict:
    cites = [(j.strip(), k.strip()) for j, k in _CITE_RE.findall(sys_ans)]
    uniq = list(dict.fromkeys(cites))
    if not uniq:
        return {"n_citations": 0, "n_unresolved": 0, "grounded": "n/a",
                "why": "no [Jurisdiction · Clause] citations in answer"}
    blocks, unresolved = [], []
    for j, k in uniq:
        # Resolve against the question's (scoped) jurisdiction first — the answer
        # often cites the alias ("UK"/"GB") rather than the index name; only a key
        # that resolves in NEITHER is a genuinely invalid/hallucinated citation.
        txt = _clause_text(jurisdiction, k) or (_clause_text(j, k) if j else None)
        if txt is None:
            unresolved.append(f"{j or jurisdiction}:{k}")
        else:
            blocks.append(f"=== [{j or jurisdiction} · {k}] ===\n{txt}")
    res = {"n_citations": len(uniq), "n_unresolved": len(unresolved), "unresolved": unresolved}
    if not blocks:  # every cited clause is invalid -> definitively ungrounded
        res.update({"grounded": "no", "why": "all cited clauses are invalid/unresolved"})
        return res
    v = _ask(client, FAITH_PROMPT.format(ans=sys_ans[:3000], sources="\n\n".join(blocks)[:20000]))
    res.update({"grounded": v.get("grounded", "?"),
                "unsupported_claims": v.get("unsupported_claims", []), "why": v.get("why", "")})
    return res


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=None, help="agent model id (default = engine default)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--out", default="/tmp/eval_results.json")
    args = ap.parse_args()

    import boto3

    from aosphere_core_index.llm.agent import run_agent_async

    gold = load_gold()
    if args.limit:
        gold = gold[:args.limit]
    print(f"evaluating {len(gold)} approved Q&A (concurrency={args.concurrency}, "
          f"agent_model={args.model or 'default'}, judge={JUDGE_MODEL})", flush=True)

    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    sem = asyncio.Semaphore(args.concurrency)
    results = [None] * len(gold)
    done = 0

    async def one(i, g):
        nonlocal done
        async with sem:
            try:
                r = await run_agent_async(g["q"], [g["jurisdiction"]] if g["jurisdiction"] else None,
                                          model=args.model)
                sys_ans = r.get("answer", "")
                srcs = r.get("sources", [])
            except Exception as e:  # noqa: BLE001
                sys_ans, srcs = f"[agent error: {type(e).__name__}: {str(e)[:160]}]", []
            # (a) equivalence+rubric vs gold, (b) citation-grounding vs the index
            try:
                v = await asyncio.to_thread(judge, bedrock, g["q"], g["jurisdiction"], g["gold"], sys_ans)
                f = await asyncio.to_thread(faithfulness, bedrock, g["jurisdiction"], sys_ans)
            except Exception as e:  # noqa: BLE001 — never let one item crash the whole run
                v = {"verdict": "error", "pass": None, "why": f"{type(e).__name__}: {str(e)[:120]}"}
                f = {"grounded": "error", "n_citations": 0, "n_unresolved": 0}
            results[i] = {**g, "system": sys_ans, "n_sources": len(srcs),
                          "verdict": v.get("verdict"), "pass": v.get("pass"),
                          "conclusion_correct": v.get("conclusion_correct"),
                          "reason": v.get("why"), "faithfulness": f}
            done += 1
            mark = "PASS" if v.get("pass") else "FAIL"
            print(f"[{done}/{len(gold)}] {mark:4} {str(v.get('verdict'))[:13]:14} "
                  f"grounded={str(f.get('grounded'))[:6]:6} {g['code']:6} {g['q'][:42]}", flush=True)

    await asyncio.gather(*(one(i, g) for i, g in enumerate(gold)))

    from collections import Counter
    eq = Counter(r["verdict"] for r in results)
    gr = Counter(r["faithfulness"]["grounded"] for r in results)
    npass = sum(1 for r in results if r.get("pass"))
    n = len(results)
    unresolved = sum(r["faithfulness"].get("n_unresolved", 0) for r in results)
    cites = sum(r["faithfulness"].get("n_citations", 0) for r in results)
    json.dump({"n": n, "pass": npass, "equivalence": dict(eq), "grounded": dict(gr),
               "results": results}, open(args.out, "w"), indent=1)
    print("\n==== (a) EQUIVALENCE vs gold ====")
    print(f"  PASS (equivalent or better): {npass}/{n} ({npass/n*100:.0f}%)")
    for k in ("EQUIVALENT", "SYSTEM_BETTER", "SYSTEM_WORSE", "DIVERGENT"):
        if eq.get(k):
            print(f"    {k:14}: {eq[k]}")
    print("==== (b) CITATION GROUNDING (faithfulness) ====")
    for k in ("yes", "mostly", "no", "n/a"):
        if gr.get(k):
            print(f"    grounded={k:7}: {gr[k]}")
    print(f"    citations: {cites} total, {unresolved} unresolved (invalid clause keys)")
    print(f"saved -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
