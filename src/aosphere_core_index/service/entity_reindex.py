"""Admin-triggered background job that runs the MSSQL -> OpenSearch entity (Spotlight)
sync from INSIDE the deployment.

The sync (scripts/sync_atlas_search.py) needs BOTH the private RDS (MSSQL) and the
OpenSearch domain. A deployed pod already sits in the VPC with an IRSA role that carries
`es:ESHttp*`, so it reaches both with no developer VPN and no CronJob. This module runs
that script as a single background subprocess and exposes a pollable status snapshot —
the lexical-search counterpart to embeddings/vector_load, which does the same for the
k-NN vector index behind POST /api/admin/reindex.

Running it as a SUBPROCESS (not an in-process import) reuses the exact, already-validated
CLI verbatim (--if-changed fingerprinting, alias-swap reindex, --verify) and keeps the
heavy MSSQL extraction out of the uvicorn workers' address space.

Env: ACI_ENTITY_SYNC_SCRIPT (default "scripts/sync_atlas_search.py"; the image ships
     scripts/ via `COPY . .` with WORKDIR /app, so the default resolves in-container).
"""
from __future__ import annotations

import collections
import logging
import os
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUNNING = ("starting", "running")
_TAIL_MAX = 80
_JOB: dict = {"phase": "idle", "started_at": None, "elapsed_s": 0.0,
              "returncode": None, "args": None, "error": None, "tail": []}


def _script_path() -> str:
    return os.getenv("ACI_ENTITY_SYNC_SCRIPT", "scripts/sync_atlas_search.py")


def build_argv(script: str, *, status_filter: str = "1", org: str | None = None,
               if_changed: bool = False, verify: bool = True) -> list[str]:
    """The sync command line. Always targets the OpenSearch backend."""
    argv = [sys.executable, script, "--backend", "opensearch",
            "--status", str(status_filter)]
    if org:
        argv += ["--org", str(org)]
    if if_changed:
        argv.append("--if-changed")
    if verify:
        argv.append("--verify")
    return argv


def status() -> dict:
    with _LOCK:
        j = dict(_JOB)
    if j["started_at"] and j["phase"] in _RUNNING:
        j["elapsed_s"] = round(time.time() - j["started_at"], 1)
    return j


def start(*, status_filter: str = "1", org: str | None = None,
          if_changed: bool = False, verify: bool = True) -> dict:
    script = _script_path()
    if not os.path.exists(script):
        raise FileNotFoundError(
            f"entity sync script not found at {script!r} (set ACI_ENTITY_SYNC_SCRIPT)")
    argv = build_argv(script, status_filter=status_filter, org=org,
                      if_changed=if_changed, verify=verify)
    with _LOCK:
        if _JOB["phase"] in _RUNNING:
            raise RuntimeError(
                f"entity reindex already running (phase={_JOB['phase']}, "
                f"elapsed={_JOB['elapsed_s']}s)")
        _JOB.update({"phase": "starting", "started_at": time.time(), "elapsed_s": 0.0,
                     "returncode": None, "args": argv[1:], "error": None, "tail": []})
    threading.Thread(target=_run, args=(argv,), daemon=True, name="entity-reindex").start()
    return status()


def _run(argv: list[str]) -> None:
    t0 = time.time()
    tail: collections.deque = collections.deque(maxlen=_TAIL_MAX)
    try:
        with _LOCK:
            _JOB["phase"] = "running"
        # inherit the pod's env (MSSQL_*, ACI_OPENSEARCH_*, ACI_ENTITY_*) so the child
        # authenticates via IRSA + reaches RDS exactly like the app does.
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=os.environ.copy())
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                tail.append(line)
                with _LOCK:
                    _JOB["tail"] = list(tail)
                    _JOB["elapsed_s"] = round(time.time() - t0, 1)
        rc = proc.wait()
        with _LOCK:
            _JOB.update(phase=("done" if rc == 0 else "error"), returncode=rc,
                        elapsed_s=round(time.time() - t0, 1), tail=list(tail),
                        error=None if rc == 0 else f"sync exited with code {rc}")
    except Exception as e:  # noqa: BLE001 — surface via status(), don't kill the thread
        log.exception("entity reindex failed")
        with _LOCK:
            _JOB.update(phase="error", error=f"{type(e).__name__}: {e}",
                        elapsed_s=round(time.time() - t0, 1), tail=list(tail))
