#!/usr/bin/env python3
"""stage4_run.py — run stage 4 (and stage 5 after it) on a job that already has stage 3.

Prompt work is re-running the same document and comparing, and there was no entry point
for it: hybrid_extract.py starts from a PDF and would redo Stage 1 and MinerU -- ten
minutes of GPU to reach code that was already sitting on disk -- so every re-run was an
ad-hoc python invocation. Which is how the model came to be whatever `run_stage4`'s
default tuple happened to start with, on runs that were being compared against Sonnet.

    python scripts/stage4_run.py <job_dir> --model sonnet --snapshot newprompt

--snapshot copies the job to a sibling directory FIRST and runs there, so the previous
result survives and both appear side by side on the pipeline monitor. Without it, stage 4
is rewritten in place.

Stage 4 costs money. Nothing here is opt-in-by-default: run_stage4 is called with
force=True because asking for it on the command line IS the opt-in, exactly as
hybrid_extract does. Stage 5 follows automatically, because it always must.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    MODEL_ALIASES, PRICES, resolve_model, run_stage4,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir", help="a job directory holding 03_stage3_final/")
    # NOT `choices`: a Bedrock model id has to be accepted verbatim, or a model released
    # after this script was written could not be run without editing it.
    ap.add_argument("--model", default=None, metavar="NAME",
                    help="a short alias (" + " | ".join(sorted(MODEL_ALIASES)) + ") or any "
                         "Bedrock model id. Default: ACI_STAGE4_AI_MODEL.")
    ap.add_argument("--mode", default="section", choices=("section", "batched"))
    ap.add_argument("--only", default=None,
                    help="restrict to these section files by filename prefix, "
                         "comma-separated (e.g. '04-,05-').")
    ap.add_argument("--snapshot", metavar="NOTE", default=None,
                    help="copy the job to a sibling first and run THERE, so the previous "
                         "stage 4 survives for comparison.")
    args = ap.parse_args()

    job = Path(args.job_dir).resolve()
    if not (job / "03_stage3_final").is_dir():
        print(f"no 03_stage3_final/ in {job} — stage 4 has nothing to read", file=sys.stderr)
        return 2

    # Fail BEFORE the snapshot and before any section is touched. Without this, a missing
    # AWS session produced a snapshot, eight instant per-section failures, a stage 5 split
    # of the untouched stage-3 text, and "0/8 accepted, $0.0000" on screen -- a result that
    # looks exactly like a model that read everything and changed nothing.
    try:
        import boto3
        from aosphere_core_index.config import settings
        boto3.client("bedrock-runtime", region_name=settings.bedrock_region).converse(
            modelId=resolve_model(args.model),
            messages=[{"role": "user", "content": [{"text": "ok"}]}],
            inferenceConfig={"maxTokens": 4})
    except Exception as e:  # noqa: BLE001 — any failure here means the run cannot work
        print(f"cannot reach Bedrock: {type(e).__name__}: {e}", file=sys.stderr)
        print("  (an SSO session is needed: AWS_PROFILE=dev1, or `aws sso login "
              "--profile dev1`)", file=sys.stderr)
        return 3

    if args.snapshot:
        # snapshot_run.py owns the naming and the APFS clone; shelling out keeps one copy
        # of that logic rather than a second, subtly different one here.
        out = subprocess.run([sys.executable, str(HERE / "snapshot_run.py"), str(job),
                              args.model, "--note", args.snapshot],
                             capture_output=True, text=True)
        sys.stdout.write(out.stdout)
        sys.stderr.write(out.stderr)
        if out.returncode != 0:
            return out.returncode
        # snapshot_run prints two lines -- "snapshot -> <dir>" then "job id  cp-...". Read
        # the one that names the DIRECTORY: taking the last line got the job id, and stage 4
        # would then have run in the original, overwriting the result being compared against.
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("snapshot ->")), "")
        job = Path(line.split("->", 1)[1].strip()) if "->" in line else job
        if not job.is_dir():
            print(f"could not find the snapshot directory (got {job!r})", file=sys.stderr)
            return 1
        print(f"running stage 4 in the snapshot: {job}")

    model = resolve_model(args.model)
    rate = PRICES.get(model)
    price = (f"${rate[0]}/${rate[1]} per 1M tokens in/out" if rate
             else "NO KNOWN PRICE — set ACI_STAGE4_AI_PRICE_IN/_OUT or costs read $0")
    print(f"=== Stage 4: {model}  ({price}), mode={args.mode}")
    only = [s for s in (args.only or "").split(",") if s] or None
    t0 = time.time()
    rep = run_stage4(job / "03_stage3_final", job / "04_stage4_ai",
                     job / "source.pdf", only=only, mode=args.mode,
                     models=(model,), force=True)
    u = rep.get("usage") or {}
    sub = rep.get("subchunk") or {}
    print(f"\n=== done in {time.time() - t0:.0f}s")
    print(f"  sections   : {rep.get('sections_accepted')}/{rep.get('sections_total')} accepted")
    print(f"  tokens     : {u.get('total_tokens', 0):,} "
          f"({u.get('input_tokens', 0):,} in / {u.get('output_tokens', 0):,} out)")
    print(f"  cost       : ${u.get('cost_usd', 0):.4f}")
    if sub:
        # subchunks_total, not subchunks -- the wrong key printed "None sub-chunks"
        # under a log line that had just said 25.
        print(f"  stage 5    : {sub.get('sections_split')} sections -> "
              f"{sub.get('subchunks_total')} sub-chunks in {sub.get('seconds')}s")
    print(f"  output     : {job / '04_stage4_ai'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
