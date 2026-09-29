"""Watch a running promotion from its durable S3 state.

Read-only: it GETs `state.json` and prints. Safe to run against a live promotion — it
touches nothing the promoter writes.

    scripts/watch_promotion.py <run_id> [version]     # default version <run_id>-p1
    scripts/watch_promotion.py <run_id> --once        # one shot, for a script

Exists because the copy loop publishes progress to S3 after every document but does not
log a line, so a terminal running `promote_local.sh` looks frozen for the whole copy.
"""
import json
import os
import subprocess
import sys
import time

BUCKET = os.getenv("ACI_EXTRACTION_BUCKET", "aosphere-tenant-dev1-core-index")
PROFILE = os.getenv("AWS_PROFILE", "dev1")
BAR_W = 26


def fetch(key: str) -> dict | None:
    p = subprocess.run(["aws", "s3", "cp", key, "-", "--profile", PROFILE],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def bar(done: int, total: int) -> str:
    if not total:
        return ""
    f = int(BAR_W * min(done, total) / total)
    return f" [{'#' * f}{'.' * (BAR_W - f)}] {done}/{total} {done / total * 100:5.1f}%"


def render(d: dict) -> str:
    now = time.time()
    stale = now - d.get("updated_at", now)
    out = [f"{d.get('promotion_id')}  ->  {d.get('target_prefix')}",
           f"host {d.get('host')}   restarts {d.get('restarts')}   "
           f"stopping={d.get('stopping')}",
           f"last write {stale:.0f}s ago" + ("   <-- STALLED?" if stale > 120 else ""),
           ""]
    for name, s in (d.get("stages") or {}).items():
        done, total = s.get("done", 0), s.get("total") or 0
        state = s.get("state", "running")
        started = s.get("started_at", d.get("updated_at", now))
        el = (s.get("finished_at") or d.get("updated_at", now)) - started
        rate = f"  {done / el:.2f}/s" if el > 0 and done else ""
        eta = ""
        if total and done and state != "complete" and el > 0:
            eta = f"  eta {(total - done) / (done / el) / 60:.1f}m"
        out.append(f"  {name:<9} {state:<9}{bar(done, total)}{rate}{eta}")
        if s.get("note"):
            out.append(f"            {s['note']}")
    c = d.get("counts") or {}
    out += ["", f"  copied {c.get('copied', 0)}   skipped {c.get('skipped', 0)}   "
                f"failed {c.get('failed', 0)}   objects {c.get('objects', 0)}   "
                f"{c.get('bytes', 0) / 1e9:.2f} GB"]
    if d.get("error"):
        out += ["", f"  ERROR: {d['error']}"]
    return "\n".join(out)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    once = "--once" in sys.argv
    if not args:
        print(__doc__)
        return 2
    run = args[0]
    version = args[1] if len(args) > 1 else f"{run}-p1"
    key = f"s3://{BUCKET}/corpus/{run}/_promotion/{version}/state.json"
    while True:
        d = fetch(key)
        text = render(d) if d else f"(no state at {key} yet)"
        if once:
            print(text)
            return 0
        print(f"\033[2J\033[H{text}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
