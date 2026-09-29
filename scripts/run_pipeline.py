#!/usr/bin/env python3
"""run_pipeline.py — run a corpus extraction with the dashboard already up.

Starts scripts/pipeline_monitor.py pointed at the same --out root, waits for
it to answer, prints the URL, THEN runs scripts/run_corpus.py in the
foreground with whatever args you'd normally pass it. The dashboard stays up
after the run finishes so you can review the final state; Ctrl+C stops both.

Usage — identical args to run_corpus.py, just via this script instead:
    python scripts/run_pipeline.py "/path/to/Advanced_Search_All"
    python scripts/run_pipeline.py "/path/to/Advanced_Search_All" --only 104_Shareholding_Disclosure
    python scripts/run_pipeline.py "/path/to/Advanced_Search_All" --monitor-port 8900
    python scripts/run_pipeline.py "/path/to/Advanced_Search_All" --no-monitor
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "out" / "corpus"


def _wait_until_up(host: str, port: int, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="corpus root containing <product>/<jurisdiction>/*.pdf")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output root")
    ap.add_argument("--only", action="append", default=None)
    ap.add_argument("--stage1-only", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--mineru-backend", default=None)
    ap.add_argument("--mineru-effort", default=None)
    ap.add_argument("--monitor-host", default="127.0.0.1")
    ap.add_argument("--monitor-port", type=int, default=8814)
    ap.add_argument("--no-monitor", action="store_true", help="skip the dashboard, just run extraction")
    args = ap.parse_args()

    monitor_proc = None
    if not args.no_monitor:
        monitor_proc = subprocess.Popen(
            [sys.executable, str(HERE / "pipeline_monitor.py"),
             "--root", args.out, "--host", args.monitor_host, "--port", str(args.monitor_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if _wait_until_up(args.monitor_host, args.monitor_port):
            print(f"\nDashboard:  http://{args.monitor_host}:{args.monitor_port}/\n", flush=True)
        else:
            print("\n!! dashboard did not come up in time, continuing without it\n", flush=True)

    run_corpus_args = [sys.executable, str(HERE / "run_corpus.py"), args.root, "--out", args.out]
    for product in (args.only or []):
        run_corpus_args += ["--only", product]
    if args.stage1_only:
        run_corpus_args.append("--stage1-only")
    if args.force:
        run_corpus_args.append("--force")
    if args.mineru_backend:
        run_corpus_args += ["--mineru-backend", args.mineru_backend]
    if args.mineru_effort:
        run_corpus_args += ["--mineru-effort", args.mineru_effort]

    try:
        subprocess.run(run_corpus_args, check=False)
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    finally:
        if monitor_proc is not None:
            print("\nExtraction finished — dashboard still running, Ctrl+C to stop it.", flush=True)
            try:
                monitor_proc.wait()
            except KeyboardInterrupt:
                monitor_proc.terminate()


if __name__ == "__main__":
    main()
