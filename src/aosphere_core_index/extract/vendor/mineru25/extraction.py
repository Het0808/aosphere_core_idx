# Vendored verbatim from "MinerU2.5" (Extraction Research), backend/extraction.py,
# commit dc47d6148d351b6627d1dca8f6927c885a9f4446 (2026-07-10). Do not hand-edit —
# fix upstream and re-vendor. aosphere-side integration: extract/mineru_adapter.py
# and extract/profiles/mineru_profile.py, which wrap this module's `run_mineru()`
# with per-document output-directory scoping, a timeout, and MINERU_API_URL
# plumbing this module does not itself provide — see docs/MINERU_MIGRATION.md.

"""Wraps the `mineru` CLI to parse a document into MinerU's content_list.json format.

MinerU's `vlm-engine` backend runs the MinerU2.5 vision-language checkpoint it
manages via MINERU_MODEL_SOURCE (huggingface/modelscope/local) and dispatches
to whichever local accelerator is installed — on this Apple Silicon Mac that's
the `mlx` extra (mlx / mlx-vlm), no CUDA required.

Without `--api-url`, `mineru` spins up a *throwaway* mineru-api instance per
invocation and reloads the model weights into it from scratch every time. If
`MINERU_API_URL` is set (pointing at a long-lived
`mineru-api --enable-vlm-preload true` process, see README), we pass
`--api-url` so the CLI reuses that already-warm server instead — every job
then only pays for its own page processing, not a fresh model load.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MINERU_API_URL = os.environ.get("MINERU_API_URL")
DEFAULT_BACKEND = os.environ.get("MINERU_BACKEND", "vlm-engine")

# Backends selectable from the UI: both run locally against MINERU_API_URL, no
# external server setup needed. (vlm-http-client/hybrid-http-client require a
# separately hosted OpenAI-compatible model server, out of scope here.)
SELECTABLE_BACKENDS = ("vlm-engine", "hybrid-engine")
SELECTABLE_EFFORTS = ("medium", "high")


class ExtractionError(RuntimeError):
    pass


def _mineru_executable() -> str:
    """Resolve the `mineru` CLI next to the running interpreter (the venv's bin/),
    since the server process may not have the venv "activated" (PATH updated) even
    though it was launched with the venv's own python/uvicorn."""
    candidate = Path(sys.executable).parent / "mineru"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("mineru")
    if found:
        return found
    raise ExtractionError(
        "Could not find the `mineru` executable. Install it into this project's "
        "venv with `./venv/bin/pip install -r requirements.txt`."
    )


def run_mineru(
    input_path: Path,
    output_dir: Path,
    backend: str = DEFAULT_BACKEND,
    effort: str | None = None,
) -> Path:
    """Run the mineru CLI on `input_path`, writing into `output_dir`.

    `effort` (medium|high) only applies to `hybrid-engine`: medium trades away
    image/chart analysis for a large speed win on text-heavy PDFs; high keeps
    full image analysis. Ignored for other backends.

    Returns the path to the resulting `*_content_list.json`.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [_mineru_executable(), "-p", str(input_path), "-o", str(output_dir), "-b", backend]
    if MINERU_API_URL:
        cmd += ["--api-url", MINERU_API_URL]
    if effort and backend.startswith("hybrid"):
        cmd += ["--effort", effort]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ExtractionError(
            f"mineru failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )

    matches = sorted(output_dir.rglob("*_content_list.json"))
    if not matches:
        raise ExtractionError(
            "mineru ran but produced no content_list.json under "
            f"{output_dir}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return matches[0]


def load_content_list(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_origin_pdf(output_dir: Path) -> Path | None:
    """MinerU writes a copy of the exact PDF it parsed (post any format conversion)
    as `*_origin.pdf` next to content_list.json — render pages from this, not the
    raw upload, so bbox coordinates are guaranteed to line up."""
    matches = sorted(output_dir.rglob("*_origin.pdf"))
    return matches[0] if matches else None
