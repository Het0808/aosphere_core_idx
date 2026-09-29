"""Pure command-line construction for the admin entity-reindex job (no subprocess)."""
import sys

from aosphere_core_index.service import entity_reindex as er


def test_build_argv_defaults_target_opensearch():
    argv = er.build_argv("scripts/sync_atlas_search.py")
    assert argv[0] == sys.executable
    assert argv[1] == "scripts/sync_atlas_search.py"
    assert "--backend" in argv and argv[argv.index("--backend") + 1] == "opensearch"
    assert argv[argv.index("--status") + 1] == "1"
    assert "--verify" in argv          # verify defaults on
    assert "--if-changed" not in argv
    assert "--org" not in argv


def test_build_argv_optional_flags():
    argv = er.build_argv("s.py", status_filter="all", org="102",
                         if_changed=True, verify=False)
    assert argv[argv.index("--status") + 1] == "all"
    assert argv[argv.index("--org") + 1] == "102"
    assert "--if-changed" in argv
    assert "--verify" not in argv


def test_status_snapshot_shape():
    j = er.status()
    assert set(j) >= {"phase", "started_at", "elapsed_s", "returncode", "args",
                      "error", "tail"}
    assert j["phase"] in ("idle", "starting", "running", "done", "error")
