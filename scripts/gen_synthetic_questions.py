"""Generate per-clause SYNTHETIC QUESTIONS for retrieval augmentation (embed-only).

For each clause an LLM produces the natural-language questions a real user would ask
that THIS clause answers; those questions are PREPENDED to the clause's embedding text
only (see embeddings/section_index._load_synth_questions, gated by ACI_SYNTH_Q=1). This
bridges the vocabulary gap between how users phrase a question and how a legal opinion
phrases the answer — validated to lift hit@10 by ~+3..+13 across UK/Germany/South Korea
(DP) and Australia (SD). Embed-only: display/snippet/rerank/agent keep using answer_text().

Cache (keyed by clause key, one file per JURISDICTION to match the embed hook):
    <ACI_SYNTH_Q_DIR>/<jurisdiction>.json = {clause_key: {"questions": [...]}}

Cost control (same question quality): QUESTIONS ONLY — we do not generate keywords (the
BM25/lexical hybrid was dropped, and keywords were never embedded anyway — the hook only
prepends "questions"), so ~25% of output tokens aren't spent on unused data. Batching
clauses per call and cheaper models (Nova) were BOTH tested and rejected: each lost the
hit@10 recall gain. One clause per call on Claude Haiku is the validated config.

Usage:
    ACI_SYNTH_Q_DIR=data/products/_synthq \\
    ACI_BEDROCK_PROFILE=dev1 AWS_PROFILE=dev1 AWS_REGION=eu-west-2 \\
      python scripts/gen_synthetic_questions.py "United Kingdom" "Germany"   # or --all

Then re-bundle with ACI_SYNTH_Q=1 pointing at the same dir to make it take effect.
Resumable: a region whose cache exists (non-empty) is skipped; delete the dir to redo.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3

from aosphere_core_index.config import settings
from aosphere_core_index.embeddings.section_index import answer_text_dict
from aosphere_core_index.config import settings as _settings

MODEL = os.getenv("ACI_SYNTH_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
QDIR = os.getenv("ACI_SYNTH_Q_DIR") or str(settings.products_dir / "_synthq")
WORKERS = int(os.getenv("ACI_SYNTH_WORKERS", "12"))
TRUNC = int(os.getenv("ACI_SYNTH_TRUNC", "3000"))    # per-clause chars sent to ground the questions

# NOTE ON COST vs QUALITY (measured on a UK A/B, hit@10 by clause key):
#   * questions-only (drop keywords)  -> SAFE: keywords are never embedded (the hook only
#     prepends "questions"), so this ~25% output saving costs nothing. KEPT.
#   * batching N clauses per call     -> REJECTED: gave the model less focus per clause and
#     lost the @10 recall gain (+7 -> -3). Kept one clause per call.
#   * cheaper model (Nova Micro)      -> REJECTED: flat @10 lift (+0 vs Haiku's +3..+7); the
#     savings come straight out of top-10 recall. Kept Claude Haiku.
# One clause per call, Claude Haiku, full clause text = the validated +3..+13 @10 config.
_COMMON = """Given ONE clause (its heading path + text), output the questions a real user \
would ask that THIS clause answers.

QUESTIONS (6-8) — write them the way actual people search, a deliberate MIX of registers:
  * short keyword-style
  * natural spoken question
  * practical / scenario (a concrete situation the clause resolves)
  * formal / statutory
Rules:
  - Vary vocabulary and length. Sometimes expand acronyms, sometimes use them.
  - Do NOT prefix every question with the country name; a user rarely repeats it.
  - Every question MUST be answerable from THIS clause's own text - never invent facts, thresholds, or dates.
  - Ask what the clause ANSWERS; do not restate the answer inside the question.

Output STRICT JSON only, no prose, no markdown fences: {"questions": ["...", "..."]}"""

_DOMAIN = {
    "Data Privacy":
        "You generate search-training data for a legal-opinion search engine covering DATA "
        "PRIVACY law across many jurisdictions (data protection principles, lawful bases, data "
        "protection officers, breach notification, international transfers, data-subject rights, "
        "regulator notification). Each clause is one section of a country's legal opinion.",
    "Shareholding Disclosure":
        "You generate search-training data for a legal-opinion search engine covering SHAREHOLDING "
        "DISCLOSURE law across many jurisdictions (disclosing major/substantial shareholdings and "
        "notifiable interests, notification thresholds and deadlines, short-selling disclosure, "
        "takeover-bid triggers, beneficial ownership, aggregation of holdings, issuer and regulator "
        "notification). Each clause is one section of a country's legal opinion.",
}


def _sys_prompt(product: str) -> str:
    return _DOMAIN.get(product, _DOMAIN["Data Privacy"]) + "\n\n" + _COMMON


def _content_path(region_id: str) -> str | None:
    hits = glob.glob(str(settings.products_dir / "*" / "*" / "artifacts" / f"{region_id}.content.json"))
    return hits[0] if hits else None


def _parse(txt: str) -> list[str] | None:
    m = re.search(r"\{.*\}", txt.strip(), re.S)
    try:
        o = json.loads(m.group(0) if m else txt)
    except json.JSONDecodeError:
        return None
    qs = [q.strip() for q in o.get("questions", []) if isinstance(q, str) and q.strip()][:8]
    return qs or None


def generate_region(region_id: str, client) -> int:
    cj = _content_path(region_id)
    if not cj:
        print(f"  ! {region_id}: no content.json (build it first) — skipped", flush=True)
        return 0
    doc = json.load(open(cj))
    secs = doc["sections"]
    product = doc.get("doc", {}).get("product") or "Data Privacy"
    jur = doc.get("doc", {}).get("jurisdiction") or region_id
    outp = os.path.join(QDIR, f"{jur}.json")
    if os.path.exists(outp) and os.path.getsize(outp) > 2:   # resumable: skip done regions
        print(f"  {region_id}: cached ({jur}.json) — skip", flush=True)
        return 0
    sysprompt = _sys_prompt(product)
    by = {s["key"]: s for s in secs}

    def crumb(s):
        c, cur = [], s
        while cur:
            c.append(cur["title"])
            cur = by.get(cur.get("parent_key"))
        return " > ".join(reversed(c))

    def gen(s):
        body = answer_text_dict(s["elements"], jur)[:TRUNC]
        if len(body.strip()) < 30:   # thin heading: borrows its children's text at embed time
            return s["key"], None
        user = f"Jurisdiction: {jur}\nClause path: {crumb(s)}\n\nClause text:\n{body}"
        for _ in range(2):
            try:
                r = client.converse(modelId=MODEL, system=[{"text": sysprompt}],
                                    messages=[{"role": "user", "content": [{"text": user}]}],
                                    inferenceConfig={"maxTokens": 600, "temperature": 0.3})
                qs = _parse(r["output"]["message"]["content"][0]["text"])
                if qs:
                    return s["key"], {"questions": qs}
            except Exception:  # noqa: BLE001 — retry once, then skip this clause
                pass
        return s["key"], None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        cache = {k: v for k, v in ex.map(gen, secs) if v}
    os.makedirs(QDIR, exist_ok=True)
    with open(outp, "w") as f:
        json.dump(cache, f, ensure_ascii=False)
    print(f"  {region_id} ({product}): {len(cache)}/{len(secs)} clauses -> {outp}", flush=True)
    return len(cache)


def _all_regions() -> list[str]:
    return sorted(
        os.path.basename(p)[:-len(".content.json")]
        for p in glob.glob(str(settings.products_dir / "*" / "*" / "artifacts" / "*.content.json"))
    )


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--all"]
    regions = _all_regions() if "--all" in sys.argv else args
    if not regions:
        print(__doc__)
        sys.exit(1)
    client = boto3.Session(profile_name=os.getenv("ACI_BEDROCK_PROFILE"),
                           region_name=os.getenv("AWS_REGION") or _settings.bedrock_region
                           ).client("bedrock-runtime")
    print(f"generating synthetic questions for {len(regions)} region(s) "
          f"(questions-only, per-clause, {MODEL.split('.')[-1]}) -> {QDIR}", flush=True)
    total = sum(generate_region(r, client) for r in regions)
    print(f"done: {total} clauses augmented across {len(regions)} region(s)", flush=True)


if __name__ == "__main__":
    main()
