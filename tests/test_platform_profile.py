"""extract/platform_profile.py: the per-host MinerU tuning these tests pin.

The profile exists because the same pipeline is run on Apple Silicon and on a
Windows box with a discrete NVIDIA GPU, and each needs a different correction:

  1. Apple Silicon gets a hybrid batch ratio, because MinerU's own get_vram()
     has no `mps` branch and therefore reports 1GB for every Mac -- pinning
     even a 64GB machine to batch_ratio 1.
  2. Windows gets CUDA_PATH (so lmdeploy's turbomind loads, instead of MinerU
     degrading ~5x to its `transformers` engine) and PYTHONIOENCODING (so the
     runner's non-ASCII progress output cannot kill a run on a cp1252 console).
  3. A caller's own value always wins, because the wrapper scripts and CI set
     these deliberately and a profile must not fight them.
  4. Nothing it does may ever raise: a missing optimisation is acceptable, a
     failed extraction is not.
"""

import pytest

from aosphere_core_index.extract import platform_profile as pp


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "darwin")
    monkeypatch.setattr(pp.platform, "machine", lambda: "arm64")


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "win32")


# ---- 1. Apple Silicon: stand in for the missing `mps` branch ----------------

@pytest.mark.parametrize("total_gb,expected", [
    (8, None),      # budget 4  -> under every threshold; MinerU's own 1 is correct
    (18, None),     # budget 9  -> still under 12; a box this tight should not batch
    (24, "4"),      # budget 12 -> first threshold
    (36, "8"),      # budget 18
    (48, "8"),      # budget 24
    (64, "16"),     # budget 32 -> top threshold
])
def test_apple_batch_ratio_tracks_unified_memory(mac, monkeypatch, total_gb, expected):
    monkeypatch.setattr(pp, "_total_memory_gb", lambda: total_gb)
    got = pp.mineru_env_defaults().get("MINERU_HYBRID_BATCH_RATIO")
    assert got == expected


def test_apple_uses_batch_ratio_not_virtual_vram(mac, monkeypatch):
    """MINERU_VIRTUAL_VRAM_SIZE would rewrite get_vram() for every caller, inflating
    unrelated batch/cleanup decisions on memory shared with the OS. Only the
    mis-sized knob may be touched."""
    monkeypatch.setattr(pp, "_total_memory_gb", lambda: 48)
    env = pp.mineru_env_defaults()
    assert "MINERU_HYBRID_BATCH_RATIO" in env
    assert "MINERU_VIRTUAL_VRAM_SIZE" not in env


def test_apple_gets_no_windows_keys(mac, monkeypatch):
    monkeypatch.setattr(pp, "_total_memory_gb", lambda: 48)
    env = pp.mineru_env_defaults()
    assert "CUDA_PATH" not in env
    assert "PYTHONIOENCODING" not in env


def test_unknown_memory_yields_no_opinion(mac, monkeypatch):
    monkeypatch.setattr(pp, "_total_memory_gb", lambda: None)
    assert "MINERU_HYBRID_BATCH_RATIO" not in pp.mineru_env_defaults()


# ---- 2. Windows: the two fixes that make hybrid-engine usable at all --------

def test_windows_sets_encoding(win):
    assert pp.mineru_env_defaults()["PYTHONIOENCODING"] == "utf-8"


def test_windows_cuda_path_only_when_shim_resolves(win, monkeypatch, tmp_path):
    """A CUDA_PATH pointing at nothing trades turbomind's clear assert for an obscure
    DLL-load error, so the shim is only advertised when its `bin` really exists."""
    monkeypatch.setattr(pp, "_windows_cuda_path", lambda: None)
    assert "CUDA_PATH" not in pp.mineru_env_defaults()

    shim = tmp_path / "shim"
    (shim / "bin").mkdir(parents=True)
    monkeypatch.setattr(pp, "_windows_cuda_path", lambda: str(shim))
    assert pp.mineru_env_defaults()["CUDA_PATH"] == str(shim)


def test_windows_shim_probe_requires_bin_subdir(win, monkeypatch, tmp_path):
    """turbomind reads $CUDA_PATH/bin, so a shim without `bin` is useless. With no
    torch CUDA DLLs to link to either, the honest answer is None."""
    monkeypatch.setattr(pp, "__file__", str(tmp_path / "a" / "b" / "c" / "x.py"))
    monkeypatch.setattr(pp, "_torch_cuda_dll_dir", lambda: None)
    assert pp._windows_cuda_path() is None


# ---- 2b. The shim builds itself: `data/` is gitignored, so a clone has none ----

def _fake_repo(monkeypatch, tmp_path):
    """Point the module at a throwaway repo root (parents[3] of its own __file__)."""
    fake = tmp_path / "repo" / "src" / "pkg" / "extract"
    fake.mkdir(parents=True)
    monkeypatch.setattr(pp, "__file__", str(fake / "platform_profile.py"))
    return tmp_path / "repo"


def _fake_torch_lib(tmp_path, with_dlls=True):
    lib = tmp_path / "site-packages" / "torch" / "lib"
    lib.mkdir(parents=True)
    if with_dlls:
        (lib / "cudart64_12.dll").write_text("")
        (lib / "cublas64_12.dll").write_text("")
    return lib


def test_shim_is_created_when_missing(win, monkeypatch, tmp_path):
    """The whole point: a fresh clone has no data/.cuda_shim, and a setup step nobody
    knows to run is the same silent 5x slowdown as having no shim at all."""
    repo = _fake_repo(monkeypatch, tmp_path)
    lib = _fake_torch_lib(tmp_path)
    monkeypatch.setattr(pp, "_torch_cuda_dll_dir", lambda: lib)

    assert not (repo / "data" / ".cuda_shim").exists()
    assert pp._windows_cuda_path() == str(repo / "data" / ".cuda_shim")
    assert (repo / "data" / ".cuda_shim" / "bin").is_dir()


def test_the_shim_is_inert_and_cannot_reach_torch(win, monkeypatch, tmp_path):
    """The safety property, and the reason this is a plain directory.

    An earlier version made bin a junction onto torch's lib: correct, free, and a live
    handle on the torch install. Any recursive delete of the shim followed the link and
    took torch's DLLs with it -- "ImportError: DLL load failed while importing _C" with
    no visible cause. It happened, and cost a reinstall.

    So: nothing inside, and deleting the whole shim must leave torch untouched.
    """
    repo = _fake_repo(monkeypatch, tmp_path)
    lib = _fake_torch_lib(tmp_path)
    monkeypatch.setattr(pp, "_torch_cuda_dll_dir", lambda: lib)
    pp._windows_cuda_path()

    bindir = repo / "data" / ".cuda_shim" / "bin"
    assert list(bindir.iterdir()) == [], "the shim must stay empty"
    assert not bindir.is_symlink(), "no link of any kind"

    import shutil                                   # the exact hazard, reproduced
    shutil.rmtree(repo / "data" / ".cuda_shim")
    assert (lib / "cudart64_12.dll").exists(), "deleting the shim must not touch torch"


def test_existing_shim_is_reused(win, monkeypatch, tmp_path):
    repo = _fake_repo(monkeypatch, tmp_path)
    lib = _fake_torch_lib(tmp_path)
    monkeypatch.setattr(pp, "_torch_cuda_dll_dir", lambda: lib)
    (repo / "data" / ".cuda_shim" / "bin").mkdir(parents=True)
    assert pp._windows_cuda_path() == str(repo / "data" / ".cuda_shim")


def test_no_shim_when_torch_is_cpu_only(win, monkeypatch, tmp_path):
    """A CPU-only torch wheel HAS a lib directory but none of the CUDA DLLs. No
    add_dll_directory can conjure them, so turbomind's clear assert beats an obscure
    DLL-load failure later — and the shim is not even created."""
    repo = _fake_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(pp, "_torch_cuda_dll_dir", lambda: None)
    assert pp._windows_cuda_path() is None
    assert not (repo / "data" / ".cuda_shim").exists()


def test_no_shim_when_the_directory_cannot_be_made(win, monkeypatch, tmp_path):
    lib = _fake_torch_lib(tmp_path)
    _fake_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(pp, "_torch_cuda_dll_dir", lambda: lib)

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(pp.Path, "mkdir", boom)
    assert pp._windows_cuda_path() is None


def test_torch_lib_without_dlls_is_not_offered(monkeypatch, tmp_path):
    """_torch_cuda_dll_dir globs for the DLLs rather than trusting the directory."""
    lib = _fake_torch_lib(tmp_path, with_dlls=False)
    import importlib.util

    spec = importlib.util.spec_from_file_location("torch", str(lib.parent / "__init__.py"))
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: spec)
    assert pp._torch_cuda_dll_dir() is None


# ---- 3. The caller always wins ---------------------------------------------

def test_caller_values_are_never_displaced(win):
    env = {"CUDA_PATH": r"D:\operator-choice", "PYTHONIOENCODING": "latin-1"}
    out = pp.apply_mineru_env(dict(env))
    assert out["CUDA_PATH"] == env["CUDA_PATH"]
    assert out["PYTHONIOENCODING"] == env["PYTHONIOENCODING"]


def test_apply_fills_only_missing_keys(win, monkeypatch, tmp_path):
    shim = tmp_path / "shim"
    (shim / "bin").mkdir(parents=True)
    monkeypatch.setattr(pp, "_windows_cuda_path", lambda: str(shim))
    out = pp.apply_mineru_env({"CUDA_PATH": "keep-me"})
    assert out["CUDA_PATH"] == "keep-me"          # untouched
    assert out["PYTHONIOENCODING"] == "utf-8"     # filled in


# ---- 4. An optimisation must never break an extraction ---------------------

def test_probe_failure_is_swallowed(mac, monkeypatch):
    def boom():
        raise OSError("sysctl unavailable")

    monkeypatch.setattr(pp, "_total_memory_gb", boom)
    assert pp.mineru_env_defaults() == {}          # no opinion, no exception


def test_apply_survives_a_broken_probe(mac, monkeypatch):
    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(pp, "_total_memory_gb", boom)
    env = {"HF_HOME": "/models"}
    assert pp.apply_mineru_env(dict(env)) == env   # passthrough, unharmed


# ---- 5. CLI resolution: the bug that made Windows depend on an active shell -

def test_cli_prefers_platform_executable(monkeypatch, tmp_path):
    """Windows ships console scripts as Scripts/mineru.exe; probing only the POSIX
    name meant Windows silently fell back to a bare PATH lookup."""
    bindir = tmp_path / "Scripts"
    bindir.mkdir()
    exe = bindir / "mineru.exe"
    exe.write_text("")
    monkeypatch.setattr(pp.sys, "executable", str(bindir / "python.exe"))
    assert pp.mineru_cli() == str(exe)


def test_cli_finds_posix_name(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "mineru"
    cli.write_text("")
    monkeypatch.setattr(pp.sys, "executable", str(bindir / "python"))
    assert pp.mineru_cli() == str(cli)


def test_cli_returns_none_when_absent(monkeypatch, tmp_path):
    """None, not a guess — the caller falls back to a bare PATH lookup."""
    monkeypatch.setattr(pp.sys, "executable", str(tmp_path / "python"))
    assert pp.mineru_cli() is None


# ---- 6. Every MinerU entry point is profiled, not just the main one ---------

def test_runner_applies_the_profile(monkeypatch, tmp_path):
    """mineru_runner is the second way this repo starts MinerU (the PDF alert
    attachment path and the KG UI). It built its environment by hand and never
    consulted the profile, so those runs went unprofiled on both hosts -- no
    Apple batch ratio, and on Windows no CUDA_PATH, which is the whole 5x."""
    from aosphere_core_index.extract import mineru_runner

    monkeypatch.setattr(mineru_runner.platform_profile, "mineru_env_defaults",
                        lambda: {"MINERU_HYBRID_BATCH_RATIO": "8", "CUDA_PATH": "C:/shim"})
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs["env"])
        (tmp_path / "out" / "doc" / "vlm").mkdir(parents=True, exist_ok=True)
        (tmp_path / "out" / "doc" / "vlm" / "doc_content_list.json").write_text("[]")
        return None

    monkeypatch.setattr(mineru_runner.subprocess, "run", fake_run)
    mineru_runner._run("doc.pdf", out_dir=tmp_path / "out", backend="vlm-engine",
                       timeout=60, config_path=None, model_cache=None)
    assert seen["MINERU_HYBRID_BATCH_RATIO"] == "8"
    assert seen["CUDA_PATH"] == "C:/shim"


def test_runner_profile_never_displaces_the_caller(monkeypatch, tmp_path):
    from aosphere_core_index.extract import mineru_runner

    monkeypatch.setenv("MINERU_HYBRID_BATCH_RATIO", "2")
    monkeypatch.setattr(mineru_runner.platform_profile, "mineru_env_defaults",
                        lambda: {"MINERU_HYBRID_BATCH_RATIO": "8"})
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs["env"])
        (tmp_path / "out" / "doc" / "vlm").mkdir(parents=True, exist_ok=True)
        (tmp_path / "out" / "doc" / "vlm" / "doc_content_list.json").write_text("[]")
        return None

    monkeypatch.setattr(mineru_runner.subprocess, "run", fake_run)
    mineru_runner._run("doc.pdf", out_dir=tmp_path / "out", backend="vlm-engine",
                       timeout=60, config_path=None, model_cache=None)
    assert seen["MINERU_HYBRID_BATCH_RATIO"] == "2"
