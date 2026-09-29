"""Per-machine MinerU tuning, chosen from what the host actually is.

The extraction pipeline was developed on Apple Silicon and later run on a Windows
box with a discrete NVIDIA GPU. Neither machine is wrong, but the *optimal* MinerU
invocation differs between them in ways that are invisible until something is slow
or broken:

  macOS (Apple Silicon)
      MinerU picks its `mlx-engine` (Metal + unified memory) automatically, which is
      already the right engine. What it gets WRONG is the batch size: MinerU sizes
      hybrid batching from `get_vram(device)`, and that function branches on cuda /
      npu / gcu / musa / mlu / sdaa -- there is no `mps` branch, so `total_memory`
      keeps its initialised value of 1 and every Apple machine lands in the
      `else: batch_ratio = 1` arm no matter how much unified memory it has. A 48GB
      M-series box therefore batches exactly as narrowly as a 2GB card. This module
      supplies the batch ratio that the missing branch would have produced.

  Windows (discrete NVIDIA GPU)
      `get_vram` works here (real VRAM, correctly detected), so batching is already
      right and is deliberately left alone -- on an 8GB card `batch_ratio = 1` is the
      honest answer and forcing it higher just OOMs. What Windows needs instead is
      two environment fixes:
        * CUDA_PATH -- lmdeploy's turbomind bootstrap asserts it is set and then does
          add_dll_directory("$CUDA_PATH/bin"), nothing more. Without it `hybrid-engine`
          dies with "Can not find $env:CUDA_PATH" and MinerU silently degrades to its
          unoptimised `transformers` engine (measured 5x slower on a table-dense
          document: 830s vs 166s). A CUDA *Toolkit* install is not required -- the
          torch wheel already ships the CUDA DLLs turbomind links against.
        * PYTHONIOENCODING -- run_corpus.py prints a "delta" glyph, and the default
          cp1252 console raises UnicodeEncodeError mid-run.

  Linux
      MinerU picks vllm, `get_vram` works, nothing to correct. Present as a profile so
      the dispatch is exhaustive rather than "everything that isn't mac or windows".

Two rules this module keeps to, because it runs inside every extraction:
  * It NEVER overwrites a variable the caller already set. Every value is a default,
    so an operator (or a CI job, or `run_corpus.py`'s wrapper) still wins.
  * It NEVER raises. A profile is an optimisation; failing to compute one must not
    fail an extraction. Every probe is wrapped and simply yields no opinion.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

# Apple Silicon only: the fraction of unified memory to treat as a GPU budget when
# standing in for MinerU's missing `mps` branch. Unified memory is SHARED with the OS,
# the page cache and this very Python process -- handing MinerU the full figure invites
# swapping, and on a Mac swapping a VLM is far more expensive than a small batch. Half
# is deliberately conservative: it still unlocks 4x-16x batching on 24GB+ machines while
# leaving an 18GB machine at ratio 1, which is the correct answer for a box that tight.
_APPLE_GPU_MEMORY_FRACTION = 0.5

# MinerU's own auto thresholds, mirrored so an Apple machine batches the way an
# equivalently-sized CUDA card would (see mineru/backend/hybrid/hybrid_analyze.py's
# get_batch_ratio). Kept in this order: first threshold that fits wins.
_BATCH_RATIO_BY_GB = ((32, 16), (16, 8), (12, 4))


def _total_memory_gb() -> int | None:
    """Physical RAM in GB, or None if it cannot be determined.

    psutil is present in practice but is a transitive dependency rather than a declared
    one, so it is imported defensively and `sysctl` is kept as an Apple-native fallback.
    """
    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024 ** 3))
    except Exception:
        pass
    try:
        import subprocess

        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        return int(int(out) / (1024 ** 3))
    except Exception:
        return None


def _apple_batch_ratio() -> int | None:
    """The hybrid batch ratio MinerU would have chosen if it understood `mps`."""
    total = _total_memory_gb()
    if not total:
        return None
    budget = int(total * _APPLE_GPU_MEMORY_FRACTION)
    for floor, ratio in _BATCH_RATIO_BY_GB:
        if budget >= floor:
            return ratio
    return None      # under the smallest threshold: MinerU's own default of 1 is right


def _torch_cuda_dll_dir() -> Path | None:
    """torch's own library directory, but only if it really holds the CUDA runtime.

    Located with find_spec rather than `import torch`, which would cost seconds of
    import time on a path that runs before every MinerU subprocess (this is also how
    lmdeploy's own bootstrap finds torch). A CPU-only torch wheel has this directory
    but none of the DLLs, so the glob -- not the directory's existence -- is the test.
    """
    try:
        import importlib.util

        spec = importlib.util.find_spec("torch")
        if spec is None or not spec.origin:
            return None
        lib = Path(spec.origin).parent / "lib"
        return lib if any(lib.glob("cudart64_*.dll")) else None
    except Exception:
        return None


def _windows_cuda_path() -> str | None:
    """A directory usable as CUDA_PATH, without a CUDA Toolkit installation.

    lmdeploy's turbomind bootstrap does exactly two things with it: assert it is set, and
    `os.add_dll_directory("$CUDA_PATH/bin")`. It never reads the directory. And the DLLs
    it needs are already on the search path by then, because `import torch` registers its
    own `lib` there and lmdeploy imports torch first. So the directory only has to EXIST:
    an empty `data/.cuda_shim/bin` is enough, and was verified to load turbomind and
    VLAsyncEngine with nothing in it.

    It is deliberately empty rather than a junction onto torch's `lib`. The junction
    version worked and cost nothing, but it made `data/.cuda_shim` a live handle on the
    torch installation: any recursive delete of the shim -- `rm -rf`, a stale-cache
    cleanup, a tidy-up script -- follows the link and takes torch's 37 DLLs with it,
    leaving "ImportError: DLL load failed while importing _C" and no obvious cause. That
    is not a hypothetical; it happened here, and cost a torch reinstall to undo. An empty
    directory cannot do it, needs no refresh when torch is upgraded, and is less code.

    Still built rather than committed: `data/` is generated state. It cannot be a manual
    setup step either -- a clone without it gets no CUDA_PATH, turbomind's assert fires,
    and MinerU quietly drops to its unoptimised `transformers` engine (measured 5x
    slower). A setup step nobody knows to run is that same silent slowdown.

    Requires a CUDA torch: with a CPU-only wheel the DLLs are absent, no `add_dll_directory`
    can conjure them, and turbomind's clear assert is better than an obscure DLL failure
    later -- so None, and behaviour is exactly as before.
    """
    if _torch_cuda_dll_dir() is None:
        return None
    shim = Path(__file__).resolve().parents[3] / "data" / ".cuda_shim"
    bindir = shim / "bin"
    try:
        bindir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return str(shim) if bindir.is_dir() else None


def profile_name() -> str:
    """Short label for the active profile, for logs and reports."""
    if sys.platform == "darwin":
        return "apple-silicon" if platform.machine() == "arm64" else "apple-intel"
    if sys.platform == "win32":
        return "windows-cuda"
    return sys.platform


def mineru_env_defaults() -> dict[str, str]:
    """Environment defaults for this host. Callers merge these UNDER their own values."""
    env: dict[str, str] = {}
    try:
        if sys.platform == "darwin" and platform.machine() == "arm64":
            ratio = _apple_batch_ratio()
            if ratio:
                # Deliberately MINERU_HYBRID_BATCH_RATIO and not MINERU_VIRTUAL_VRAM_SIZE:
                # the latter rewrites get_vram() for every caller, inflating unrelated
                # batch/cleanup decisions on a machine whose memory is shared with the OS.
                # This knob moves only the thing that is actually mis-sized.
                env["MINERU_HYBRID_BATCH_RATIO"] = str(ratio)
        elif sys.platform == "win32":
            cuda_path = _windows_cuda_path()
            if cuda_path:
                env["CUDA_PATH"] = cuda_path
            env["PYTHONIOENCODING"] = "utf-8"
    except Exception:
        return {}      # an optimisation must never break an extraction
    return env


def apply_mineru_env(env: dict[str, str]) -> dict[str, str]:
    """Fill this host's defaults into `env` without displacing anything already set."""
    for key, value in mineru_env_defaults().items():
        env.setdefault(key, value)
    return env


def mineru_cli() -> str | None:
    """Path to the `mineru` CLI shipped in this interpreter's environment.

    Windows puts console scripts in `Scripts/` as `.exe`, POSIX puts them in `bin/` with
    no suffix. Probing only the POSIX shape (as the original did) meant Windows always
    fell through to a bare "mineru", which resolves only when .venv/Scripts happens to be
    on PATH -- so extraction worked from an activated shell and failed from anywhere else.
    Returns None when nothing is found, leaving the bare-name PATH lookup as the fallback.
    """
    bindir = Path(sys.executable).parent
    for name in ("mineru.exe", "mineru.cmd", "mineru"):
        candidate = bindir / name
        if candidate.exists():
            return str(candidate)
    return None
