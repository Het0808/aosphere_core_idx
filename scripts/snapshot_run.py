#!/usr/bin/env python3
"""snapshot_run.py — freeze a Stage 4 experiment as its own document in the corpus UI.

Prompt work means running the same document repeatedly and comparing, and each run
overwrites 04_stage4_ai — which lost a Sonnet run mid-comparison and forced a re-run to
get it back. This copies a finished job to a sibling directory under a version label, so
every variant stays on the same dashboard, side by side, with its own scorecard tab.

The UI discovers a job by finding corpus_meta.json under <corpus_root>/<product>/<label>,
so a copy with a new directory name and an edited `jurisdiction` is all it takes — no
server change, and the job id (cp-<product>-<label>) follows the directory name.

Copies with APFS clones (`cp -c`), so an 87 MB job costs no real disk and takes no real
time: stage 2 alone is 52 MB of MinerU output that is identical across every variant.

Usage:
    python scripts/snapshot_run.py <job_dir> sonnet          -> Bahamas-sonnet-0609-2019
    python scripts/snapshot_run.py <job_dir> haiku --note v3 -> Bahamas-haiku-v3-0609-2019
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def snapshot(job_dir: Path, tag: str, note: str | None = None,
             stamp: str | None = None) -> Path:
    job_dir = Path(job_dir).resolve()
    meta_p = job_dir / "corpus_meta.json"
    if not meta_p.exists():
        sys.exit(f"{job_dir} has no corpus_meta.json — not a corpus job")
    meta = json.loads(meta_p.read_text())

    # The base name is the jurisdiction, not the directory: "Bahamas__183503" carries a
    # document id that means nothing in a variant name.
    base = (meta.get("jurisdiction") or job_dir.name.split("__")[0]).strip()
    when = stamp or datetime.now().strftime("%d-%m-%H%M")
    label = "-".join(p for p in (base, tag, note, when) if p)
    dest = job_dir.parent / label
    if dest.exists():
        shutil.rmtree(dest)

    # `cp -c` clones on APFS; fall back to a plain copy elsewhere.
    if subprocess.run(["cp", "-c", "-R", str(job_dir), str(dest)],
                      capture_output=True).returncode != 0:
        shutil.copytree(job_dir, dest)

    # The UI lists documents by `jurisdiction`, so the variant has to say so there or
    # every row on the dashboard reads "Bahamas" and the point is lost.
    meta["jurisdiction"] = label
    meta["snapshot_of"] = job_dir.name
    meta["snapshot_tag"] = tag
    meta["snapshot_at"] = datetime.now().isoformat(timespec="seconds")
    s4 = dest / "04_stage4_ai" / "stage4_report.json"
    if s4.exists():
        try:
            r = json.loads(s4.read_text())
            meta["stage4_model"] = r.get("model")
            meta["stage4_cost_usd"] = (r.get("usage") or {}).get("cost_usd")
        except (OSError, json.JSONDecodeError):
            pass
    (dest / "corpus_meta.json").write_text(json.dumps(meta, indent=2))
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir")
    ap.add_argument("tag", help="what varied in this run, e.g. sonnet / haiku")
    ap.add_argument("--note", default=None, help="extra label, e.g. a prompt version")
    args = ap.parse_args()
    dest = snapshot(Path(args.job_dir), args.tag, args.note)
    print(f"snapshot -> {dest}")
    print(f"job id     cp-{dest.parent.name}-{dest.name}")


if __name__ == "__main__":
    main()
