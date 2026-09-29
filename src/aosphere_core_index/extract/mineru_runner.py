"""Shells out to the `mineru` CLI and reads back its output.

MinerU is a heavy, optional, build-time-only dependency (see pyproject.toml's
`mineru` extra) — this is the only module that invokes it, so nothing else
needs `mineru` installed to import the rest of the extraction package.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from aosphere_core_index.extract import platform_profile


class MineruError(RuntimeError):
    """The `mineru` CLI is missing, failed, or produced no parseable output."""


def _run(
    path: str, *, out_dir: Path, backend: str, timeout: int,
    config_path: Path | None, model_cache: Path | None,
) -> Path:
    """Run `mineru -p path -o out_dir -b backend`, return its per-run output dir.

    `config_path` maps to MINERU_TOOLS_CONFIG_JSON (verified against MinerU's own
    config_reader.py — an absolute path here is read directly, overriding its
    default of ~/mineru.json). `model_cache` maps to HF_HOME so the ~1-2GB model
    download lands in a repo-relative, reproducible location instead of a
    developer's home directory.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if config_path is not None:
        env["MINERU_TOOLS_CONFIG_JSON"] = str(config_path.resolve())
    if model_cache is not None:
        model_cache.mkdir(parents=True, exist_ok=True)
        env["HF_HOME"] = str(model_cache.resolve())
    # This host's performance profile, on the same terms as mineru_extract._mineru_env:
    # defaults only, so anything the caller already exported wins. Without it this path
    # ran unprofiled — no Apple batch ratio, and on Windows no CUDA_PATH, which is the
    # difference between turbomind and the 5x-slower `transformers` engine.
    platform_profile.apply_mineru_env(env)
    try:
        subprocess.run(
            # Resolved per-OS (Scripts/mineru.exe vs bin/mineru) instead of relying on a
            # bare PATH lookup, which on Windows only succeeded from an activated shell.
            [platform_profile.mineru_cli() or "mineru",
             "-p", path, "-o", str(out_dir), "-b", backend],
            # encoding/errors pinned, not locale-derived — see the same call in
            # mineru_extract.run_mineru_to_dir: a cp1252 default kills subprocess's
            # reader thread on MinerU's non-ASCII logs and silently discards the
            # stderr this function reports in MineruError.
            check=True, timeout=timeout, capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as e:
        raise MineruError(
            "mineru CLI not found — install the optional extra (uv sync --extra mineru)"
        ) from e
    except subprocess.CalledProcessError as e:
        raise MineruError(f"mineru failed on {path}: {e.stderr[-2000:]}") from e
    except subprocess.TimeoutExpired as e:
        raise MineruError(f"mineru timed out on {path} after {timeout}s") from e

    stem = Path(path).stem
    doc_dir = out_dir / stem
    if not doc_dir.exists():
        raise MineruError(f"mineru produced no output at {doc_dir}")
    # The output subdirectory name depends on backend/parse-method — verified
    # against mineru/cli/common.py: "auto"/"txt"/"ocr" for pipeline, "vlm" for
    # vlm-*, "hybrid_<method>" for hybrid-*, "office" for docx/pptx/xlsx.
    # Discovered dynamically (rather than hardcoded per name) so a future
    # MinerU release renaming these doesn't silently break this integration.
    for candidate in sorted(d for d in doc_dir.iterdir() if d.is_dir()):
        if (candidate / f"{stem}_content_list.json").exists() or (candidate / f"{stem}.md").exists():
            return candidate
    raise MineruError(f"mineru produced no recognizable output under {doc_dir}")


def run_mineru(
    path: str, *, out_dir: Path, backend: str = "vlm-engine", timeout: int = 2400,
    config_path: Path | None = None, model_cache: Path | None = None,
) -> list[dict]:
    """Run MinerU, return its content-list blocks: `{type, text, text_level?,
    bbox, page_idx}` per block (table blocks also carry `table_body` HTML)."""
    parse_dir = _run(path, out_dir=out_dir, backend=backend, timeout=timeout,
                     config_path=config_path, model_cache=model_cache)
    content_list = parse_dir / f"{Path(path).stem}_content_list.json"
    if not content_list.exists():
        raise MineruError(f"mineru produced no content list at {content_list}")
    return json.loads(content_list.read_text())


def run_mineru_markdown(
    path: str, *, out_dir: Path, backend: str = "vlm-engine", timeout: int = 2400,
    config_path: Path | None = None, model_cache: Path | None = None,
) -> str:
    """Run MinerU, return its rendered markdown (headings/tables preserved) —
    used for the PDF alert-attachment path, which just needs readable text."""
    parse_dir = _run(path, out_dir=out_dir, backend=backend, timeout=timeout,
                     config_path=config_path, model_cache=model_cache)
    md = parse_dir / f"{Path(path).stem}.md"
    return md.read_text() if md.exists() else ""
