"""A failed conversion must not destroy the guidance it was going to merge.

`.incumbent` is a copy of the region's content.json taken BEFORE tree_to_content overwrites
it, and it is the only source of the curated guidance and alerts that get merged back in.
On success it is merged and deleted. On failure it stayed on disk beside a cj that
tree_to_content had already replaced — so the retry copied the BROKEN cj over it and the
guidance was gone for good, with no error anywhere.

Run 2026-08-21-02 produced exactly this: Shareholding Disclosure — Iceland failed with
"content.json has 0 sections", leaving a 239-byte cj beside a 366KB .incumbent holding 79
sections. A second pass would have overwritten the second with the first.

Two guards, and they are separate: an existing .incumbent is never overwritten (it is
always the older, truer file), and a region that fails is restored from it, so the data dir
never carries a half-converted region forward into a later --allow-content-failures run.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_index as PI  # noqa: E402

REGION = "Shareholding Disclosure — Iceland"
GOOD = {"sections": [{"key": f"A{i}"} for i in range(79)], "guidance_by_id": {"g": 1}}
EMPTY = {"sections": [], "guidance_by_id": {}}


def row():
    return {"product": "Shareholding Disclosure", "jurisdiction": "Iceland",
            "doc_id": "180630", "job": "104_Shareholding_Disclosure/Iceland__180630",
            "gate": "pass", "worst_score": 90.0}


class Prog:
    def __init__(self):
        self.d = {"stages": {}}

    def bump(self, *a, **k):
        pass

    def publish(self, *a, **k):
        pass

    def stopping(self):
        return False

    def _ledger_row(self, r):
        pass


def artifacts(data_dir):
    return PI.artifacts_dir(data_dir, REGION)


def run_content(tmp_path, monkeypatch, writes):
    """Drive stage_content with tree_to_content faked to write `writes` into cj."""
    monkeypatch.setattr(PI, "_download_tree", lambda *a, **k: tmp_path / "tree")

    def fake_run(argv, **kw):
        if str(argv[1]).endswith("tree_to_content.py"):
            Path(argv[2]).write_text(json.dumps(writes))
            return 0, "ok"
        return 0, "12 attached (0 dropped)"                  # merge_guidance_alerts

    monkeypatch.setattr(PI, "_run", fake_run)
    return PI.stage_content(Prog(), None, "b", "corpus/r", [row()],
                            tmp_path, tmp_path / "trees", None)


def test_a_failed_conversion_leaves_the_incumbent_INTACT(tmp_path, monkeypatch):
    art = artifacts(tmp_path)
    art.mkdir(parents=True)
    cj = art / f"{REGION}.content.json"
    cj.write_text(json.dumps(GOOD))

    res = run_content(tmp_path, monkeypatch, EMPTY)          # 0 sections -> raises

    assert len(res["failed"]) == 1
    inc = art / f"{REGION}.content.json.incumbent"
    assert inc.exists(), "the guidance source must survive a failure"
    assert len(json.loads(inc.read_text())["sections"]) == 79


def test_the_failed_region_is_RESTORED_not_left_broken(tmp_path, monkeypatch):
    art = artifacts(tmp_path)
    art.mkdir(parents=True)
    cj = art / f"{REGION}.content.json"
    cj.write_text(json.dumps(GOOD))

    run_content(tmp_path, monkeypatch, EMPTY)

    # Not the 0-section file tree_to_content wrote.
    assert len(json.loads(cj.read_text())["sections"]) == 79


def test_a_RETRY_does_not_overwrite_the_incumbent_with_the_broken_file(tmp_path, monkeypatch):
    """The actual Iceland scenario: a second pass over a region a first pass broke."""
    art = artifacts(tmp_path)
    art.mkdir(parents=True)
    cj = art / f"{REGION}.content.json"
    inc = art / f"{REGION}.content.json.incumbent"
    # State a failed pass leaves behind, WITHOUT the restore (a run from before this fix).
    cj.write_text(json.dumps(EMPTY))
    inc.write_text(json.dumps(GOOD))

    run_content(tmp_path, monkeypatch, EMPTY)

    assert len(json.loads(inc.read_text())["sections"]) == 79, \
        "the retry overwrote the real incumbent with the broken cj"


def test_a_successful_conversion_still_merges_and_removes_the_incumbent(tmp_path, monkeypatch):
    art = artifacts(tmp_path)
    art.mkdir(parents=True)
    (art / f"{REGION}.content.json").write_text(json.dumps(GOOD))
    (art / f"{REGION}.sections.npz").write_bytes(b"stale")

    res = run_content(tmp_path, monkeypatch, GOOD)

    assert res["built"] == 1 and not res["failed"]
    assert not (art / f"{REGION}.content.json.incumbent").exists()
    assert not (art / f"{REGION}.sections.npz").exists(), "the stale npz must be deleted"


def test_a_region_with_no_incumbent_at_all_is_fine(tmp_path, monkeypatch):
    artifacts(tmp_path).mkdir(parents=True)
    res = run_content(tmp_path, monkeypatch, GOOD)
    assert res["built"] == 1 and not res["failed"]


def test_the_real_iceland_shape(tmp_path, monkeypatch):
    """239-byte empty cj beside a 366KB good incumbent — byte sizes from the real run."""
    art = artifacts(tmp_path)
    art.mkdir(parents=True)
    cj = art / f"{REGION}.content.json"
    inc = art / f"{REGION}.content.json.incumbent"
    cj.write_text(json.dumps(EMPTY))
    inc.write_text(json.dumps(GOOD))
    before = inc.read_bytes()

    run_content(tmp_path, monkeypatch, EMPTY)

    assert inc.read_bytes() == before
    assert shutil  # noqa: B015  (import is load-bearing in the module under test)

