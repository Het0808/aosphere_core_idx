#!/usr/bin/env python3
"""Walk the guided interview against a running core-index, printing what each step does.

    scripts/try_interview.py
    scripts/try_interview.py --base https://<dev-url> --q "selling a UCITS in Malta"
    scripts/try_interview.py --answer            # also run the agent on the confirmed scenario

Exercises the four endpoints in the order a client calls them, and answers each question by
picking the first option that is not ruled out — so a completed run shows the composed
query, the confirmed scenario, and the memo sections it resolved to.

A browser User-Agent is sent deliberately: the ALB WAF in front of dev answers a bare curl
with a 403, which looks exactly like an auth failure.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PRODUCT = "Marketing Restrictions - Asset Management"
UA = "Mozilla/5.0 (interview-smoke)"


def _ssl_context():
    """A context with a CA bundle that actually exists.

    A python.org macOS build ships no system CA store, so an https call fails with
    CERTIFICATE_VERIFY_FAILED until someone runs Install Certificates.command — which
    looks like the service refusing the connection rather than a local trust-store gap.
    certifi is present in this project's venv (boto3 pulls it in), so prefer it and fall
    back to the default context where it is not.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                           # noqa: BLE001
        return ssl.create_default_context()


CTX = _ssl_context()


# A bearer token, for an environment with auth enabled (dev, stage). Keycloak tokens are
# ~5 minutes lived, so fetch one immediately before a run or it 401s.
TOKEN = ""


def call(base: str, path: str, body: dict | None = None) -> dict:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("User-Agent", UA)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            print("  !! 401 — pass --token (Keycloak tokens last ~5 minutes)",
                  file=sys.stderr)
            raise SystemExit(1)
        if e.code == 403 and "<html" in detail.lower():
            # The ALB WAF's own 403 is HTML; the app's is JSON. Worth distinguishing,
            # because the WAF one usually means the query string got too long.
            print("  !! 403 from the WAF, not the app (check the query-string length)",
                  file=sys.stderr)
            raise SystemExit(1)
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            print("  !! TLS trust store is empty — this Python has no CA bundle.\n"
                  "     Run it with the project venv:  .venv/bin/python "
                  "scripts/try_interview.py ...\n"
                  "     or install certifi for this interpreter, or run "
                  "/Applications/Python*/Install Certificates.command",
                  file=sys.stderr)
            raise SystemExit(1)
        print(f"  !! could not reach {url}: {reason}", file=sys.stderr)
        raise SystemExit(1)
        # 503 is the router, not the service: it means Bedrock could not be reached
        # (locally, usually an expired SSO session — see docs/INTERVIEW_API.md).
        print(f"  !! HTTP {e.code} on {path}: {detail}", file=sys.stderr)
        raise SystemExit(1)


def show_question(q: dict) -> None:
    print(f"  asks    : {q['field']} -> {q['question']}")
    for o in q["options"][:8]:
        mark = "[X]" if o.get("excluded_reason") else "[ ]"
        why = f"   <- {o['excluded_reason']}" if o.get("excluded_reason") else ""
        print(f"     {mark} {o['value']}{why}")
    if len(q["options"]) > 8:
        print(f"     … {len(q['options']) - 8} more options")


def show_handoff(h: dict) -> None:
    print("\n  COMPLETE\n")
    print(f"  query      : {h['query']}\n")
    print("  " + h["scenario"].replace("\n", "\n  ") + "\n")
    secs = ", ".join(f"{s['key']} {s['title']}" for s in h["sections"])
    print(f"  sections   : {secs or '(none resolved for this jurisdiction)'}")
    print(f"  region     : {h['region']}")
    print(f"  definitions: {len(h['definitions'])} chars of selected-term grounding")


def answer(base: str, h: dict, model: str | None) -> None:
    """Stream the agent on the confirmed scenario, exactly as AI Mode does."""
    body = {"q": h["query"], "products": PRODUCT, "interview": h["slots"]}
    if model:
        body["model"] = model
    req = urllib.request.Request(f"{base.rstrip('/')}/api/agent/stream",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    print("\n== agent, with the scenario attached ==")
    with urllib.request.urlopen(req, timeout=600, context=CTX) as r:
        for raw in r:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:])
            except Exception:                                   # noqa: BLE001
                continue
            if d.get("kind") == "activity":
                print(f"  · {d.get('text', '')[:140]}")
            elif d.get("kind") == "answer":
                print("\n" + (d.get("answer") or "")[:2000])
                srcs = [f"{s.get('jurisdiction')} {s.get('key')}"
                        for s in d.get("sources", [])]
                print(f"\n  sources: {', '.join(srcs) or '(none)'}")
            elif d.get("kind") == "error":
                print(f"  ERROR: {d.get('text')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--q", default="can I market my hedge fund to pension schemes in Jersey")
    ap.add_argument("--jurisdictions", default=None,
                    help="CSV standing in for the sidebar filter; one name means the "
                         "jurisdiction question is answered already and is skipped")
    ap.add_argument("--answer", action="store_true",
                    help="also run the agent on the confirmed scenario (costs a real run)")
    ap.add_argument("--model", default=None, help="Bedrock model id for --answer")
    ap.add_argument("--eea", action="store_true",
                    help="just print this environment's jurisdiction list, split by "
                         "whether it takes the EEA branch, and stop")
    ap.add_argument("--token", default=None,
                    help="bearer token, for an environment with auth on (or set ACI_TOKEN)")
    a = ap.parse_args()

    global TOKEN
    TOKEN = a.token or os.environ.get("ACI_TOKEN", "")
    if not TOKEN and not a.base.startswith("http://localhost"):
        print("note: no --token/ACI_TOKEN — a deployed environment will answer 401\n",
              file=sys.stderr)

    scope = {"product": PRODUCT}
    if a.jurisdictions:
        scope["jurisdictions"] = a.jurisdictions

    if a.eea:
        # The EEA MEMBERSHIP RULE lives in code and is identical everywhere; WHICH members
        # appear depends on the index this environment has mounted. So the only way to
        # confirm an environment's branch split is to ask that environment.
        d = call(a.base, "/api/interview/schema")
        eea = [j["name"] for j in d["jurisdictions"] if j["eea"]]
        rest = [j["name"] for j in d["jurisdictions"] if not j["eea"]]
        print(f"{a.base} — {d['product']}")
        print(f"\n{len(d['jurisdictions'])} jurisdictions offered\n")
        print(f"EEA branch ({len(eea)}) — passporting/NPPR categories, MiFID II investor "
              f"labels, and the 'who is marketing' question:")
        for j in eea:
            print(f"  {j}")
        print(f"\nnon-EEA branch ({len(rest)}) — open-/closed-ended categories:")
        for j in rest:
            print(f"  {j}")
        return

    print("== schema (one call; a client needs no model to render the flow) ==")
    d = call(a.base, "/api/interview/schema"
             + (f"?jurisdictions={urllib.parse.quote(a.jurisdictions)}"
                if a.jurisdictions else ""))
    eea = sum(1 for j in d["jurisdictions"] if j["eea"])
    print(f"  product      : {d['product']}")
    print(f"  jurisdictions: {len(d['jurisdictions'])} — {eea} on the EEA branch")
    print(f"  fields       : {', '.join(d['fields'])}")

    print(f'\n== route: "{a.q}" ==')
    seed = {}
    # One selected jurisdiction IS the answer to the first question — the UI pre-fills the
    # slot from the filter rather than asking again.
    if a.jurisdictions and len(a.jurisdictions.split(",")) == 1:
        seed = {"jurisdiction": a.jurisdictions.strip()}
        print(f"  (using {seed['jurisdiction']!r} from the filter — not asking for it)")
    d = call(a.base, "/api/interview/route",
             {**scope, "messages": [{"role": "user", "content": a.q}], "slots": seed})
    print(f"  intent  : {d['intent']}   carve-out: {d.get('other_kind')}")
    print(f"  extracted: {({k: v for k, v in d['slots'].items() if v}) or '(nothing)'}")
    s = d.get("signals") or {}
    print(f"  signals : product_kind={s.get('product_kind')} "
          f"category_hint={(s.get('category_hint') or {}).get('value')} "
          f"activity_hint={(s.get('activity_hint') or {}).get('value')}")
    if d.get("acknowledgement"):
        print("  said    : " + d["acknowledgement"].replace("\n", " | "))
    if d.get("capability"):
        print("  capability lane — the interview was not started")
        return
    if d.get("general"):
        g = d["general"]
        print(f"  general lane — jurisdiction={g['jurisdiction']} "
              f"sections={[x['key'] for x in g['sections']]}")
    if d.get("next_question"):
        show_question(d["next_question"])

    print("\n== next: answering by CLICKING options (no model call at all) ==")
    slots, signals = d["slots"], d["signals"]
    handoff = d.get("handoff")
    for _ in range(6):
        if handoff:
            break
        d = call(a.base, "/api/interview/next",
                 {**scope, "slots": slots, "signals": signals,
                  "original_question": a.q})
        slots = d["slots"]
        if d["complete"]:
            handoff = d["handoff"]
            break
        q = d["next_question"]
        pick = next(o["value"] for o in q["options"] if not o.get("excluded_reason"))
        print(f"  {q['field']:<14} {q['question'][:64]:<64} -> {pick!r}")
        slots = {**slots, q["field"]: pick}
    if handoff:
        show_handoff(handoff)

    print("\n== triage: what a typed turn IS while a question is pending ==")
    for t in ["does an OEIC count as open-ended?", "Jersey",
              "can I cold-call investors in Jersey?",
              "we target professional investors — what are they?"]:
        d = call(a.base, "/api/interview/triage",
                 {**scope, "messages": [{"role": "user", "content": t}],
                  "pending_field": "category", "jurisdiction": "Jersey"})
        extra = (d.get("answer") or "").replace("\n", " ")[:80]
        flag = " (+states a value)" if d.get("has_answer") else ""
        print(f"  {t[:46]:<46} -> {d['lane']:<17}{flag} {extra}")

    if a.answer and handoff:
        answer(a.base, handoff, a.model)


if __name__ == "__main__":
    main()
