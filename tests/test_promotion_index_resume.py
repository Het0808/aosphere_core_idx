"""A failed Phase-2 stage must be retried on resume, not skipped as if it had finished.

Found live, on an actual promotion: one region's embed call hit a transient Bedrock
timeout, `stage_embed` raised, and `PromotionProgress.fail()` -- correctly, for the STATE
SCREEN's benefit -- stamped `finished_at` on the "embed" stage record so `stage_rows()`'s
ladder can tell "failed a while ago" from "still stuck mid-stage". But `run_index_stages`'s
own `done()` resume check read the SAME field to mean "this stage completed", so re-running
with the same --version skipped `embed` entirely: `flat` built a multi-index missing the one
region whose `.npz` never got regenerated (321 jurisdictions instead of 322), and the
promotion published an incomplete index. `index_verify`'s superset/promoted-regions checks
caught it that time -- but by making a verify gate the only thing standing between "a stage
failed" and "silently published incomplete", not because the resume logic was correct.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_index as PI  # noqa: E402


class Cfg:
    data_dir = trees_dir = src_root_local = seed_from = None
    embed_backend = bedrock_region = vector_backend = vector_index = opensearch_url = None
    bucket = "bkt"
    profile = None
    allow_content_failures = False
    set_latest = False                # stop before cutover/vectors either way


class Prog:
    def __init__(self, stages):
        self.d = {"stages": stages, "rewritten": ["kenya"]}

    def stage(self, key, **kw):
        pass

    def finish(self, key, **kw):
        self.d["stages"].setdefault(key, {})["finished_at"] = time.time()
        self.d["stages"][key]["state"] = "complete"

    def fail(self, key, err):
        self.d["stages"].setdefault(key, {})["finished_at"] = time.time()
        self.d["stages"][key]["state"] = "failed"

    def publish(self, *a, **k):
        pass


def _done_stage():
    return {"finished_at": time.time(), "state": "complete"}


def _failed_stage():
    return {"finished_at": time.time(), "state": "failed"}


@pytest.fixture
def stub_stages(monkeypatch):
    """Every stage becomes a call counter. seed/content/flat/publish/index_verify report
    success unconditionally, so the only thing under test is whether `embed` gets called."""
    calls = {"seed": 0, "content": 0, "embed": 0, "flat": 0, "publish": 0, "index_verify": 0}

    def mk(name, ret):
        def fn(*a, **k):
            calls[name] += 1
            return ret
        return fn

    monkeypatch.setattr(PI, "stage_seed", mk("seed", {"seeded": 0, "from": None}))
    monkeypatch.setattr(PI, "stage_content", mk("content", {
        "built": 1, "collisions": [], "guidance_dropped": 0, "failed": [], "rewritten": ["kenya"],
    }))
    monkeypatch.setattr(PI, "stage_embed", mk("embed", {"regions": 1}))
    monkeypatch.setattr(PI, "stage_flat", mk("flat", {"rows": 1, "regions": 1, "model": "titan"}))
    monkeypatch.setattr(PI, "stage_publish", mk("publish", {"prefix": "index/v1"}))
    monkeypatch.setattr(PI, "stage_index_verify", mk("index_verify",
        {"ok": True, "checks": [], "rows": 1, "regions": 1}))
    monkeypatch.setattr(PI, "_read_pointer", lambda *a, **k: None)
    return calls


def test_a_failed_embed_is_retried_on_resume(stub_stages, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stages = {"seed": _done_stage(), "content": _done_stage(), "embed": _failed_stage()}
    prog = Prog(stages)
    rc = PI.run_index_stages(prog, s3=object(), s3ro=object(), cfg=Cfg(), rows=[],
                             run="r", run_prefix="corpus/r", version="v1")
    assert rc == 0
    assert stub_stages["embed"] == 1, "a stage that FAILED must be retried, not skipped"
    assert stub_stages["seed"] == 0, "a stage that already COMPLETED must still be skipped"
    assert stub_stages["content"] == 0


def test_a_completed_embed_is_not_repeated(stub_stages, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stages = {"seed": _done_stage(), "content": _done_stage(), "embed": _done_stage()}
    prog = Prog(stages)
    rc = PI.run_index_stages(prog, s3=object(), s3ro=object(), cfg=Cfg(), rows=[],
                             run="r", run_prefix="corpus/r", version="v1")
    assert rc == 0
    assert stub_stages["embed"] == 0, "a genuinely finished stage must not re-run"


def test_every_stage_resumes_after_its_own_failure(stub_stages, tmp_path, monkeypatch):
    """Not just embed -- fail() is shared by every stage, so the bug (and the fix) is generic."""
    monkeypatch.chdir(tmp_path)
    for key in ("seed", "content", "embed", "flat", "publish"):
        stub_stages_local = dict.fromkeys(stub_stages, 0)
        stub_stages.update(stub_stages_local)
        stages = {"seed": _done_stage(), "content": _done_stage(), "embed": _done_stage(),
                  "flat": _done_stage(), "publish": _done_stage()}
        stages[key] = _failed_stage()
        prog = Prog(stages)
        rc = PI.run_index_stages(prog, s3=object(), s3ro=object(), cfg=Cfg(), rows=[],
                                 run="r", run_prefix="corpus/r", version="v1")
        assert rc == 0
        assert stub_stages[key] == 1, f"{key} failed previously and must be retried"
