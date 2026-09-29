"""Both artefacts the gallery links to must show every stage the document has.

A document that goes through AI post-processing produces three trees — 03_stage3_final,
04_stage4_ai, 05_subchunks — and both published artefacts showed only the first. A reader
opening an AI-post-processed extraction got the pass's INPUT, with no way to reach what the
pass actually did.

They were broken differently, which is why fixing one did not fix the other:

  viewer.html   build_viewer takes ONE tree_dir. No switcher existed at all.
  inspect.html  already SHIPS a working switcher (renderStageSwitch, STAGE_LABEL) and was
                starved twice over: payloads() embedded only stage 3 and never set
                stages_available, AND the shim's regex stripped `?stage=N`, so every stage
                resolved to the same payload. Either alone makes a working switcher look
                broken.

Size was the objection to embedding rather than serving, and it was measured wrong. On the
real Bahamas job: inspect 14.94 -> 15.06 MB (+0.7%, it is dominated by the embedded PDF and
page images), viewer 1.53 -> 2.27 MB. The trees are markdown only — _assets is skipped, as
the dashboard skips it.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import push_hybrid_s3 as PH  # noqa: E402


def make_job(tmp_path: Path, stages=(3, 4, 5)) -> Path:
    names = {3: "03_stage3_final", 4: "04_stage4_ai", 5: "05_subchunks"}
    job = tmp_path / "Bahamas__183503"
    for n in stages:
        d = job / names[n]
        (d / "_assets").mkdir(parents=True)
        (d / "_assets" / "page-001.png").write_bytes(b"\x89PNG")
        (d / "README.md").write_text(f"# stage {n}")
        for i in range(n):                      # a different file count per stage
            (d / f"{i:02d}-sec.md").write_text(f"stage {n} section {i}")
    job.mkdir(exist_ok=True)
    (job / "source.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return job


def payload(html: str) -> dict:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def stages_of(job: Path, which=(3, 4, 5)) -> dict:
    names = {3: "03_stage3_final", 4: "04_stage4_ai", 5: "05_subchunks"}
    return {n: job / names[n] for n in which if (job / names[n]).is_dir()}


# ---------------- the viewer ----------------
def test_one_stage_behaves_exactly_as_before(tmp_path):
    """A document that never went through stage 4 must be unchanged — no switcher, no
    `stages` key, nothing for a reader to notice."""
    job = make_job(tmp_path, stages=(3,))
    d = payload(PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s"))
    assert "stages" not in d and "available" not in d
    assert d["tree"] and d["files"]


def test_every_stage_is_carried(tmp_path):
    job = make_job(tmp_path)
    d = payload(PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s",
                                stages=stages_of(job)))
    assert d["available"] == [3, 4, 5]
    assert sorted(d["stages"]) == ["3", "4", "5"]


def test_each_stage_gets_its_OWN_files(tmp_path):
    """build_node writes into an enclosing dict. Rebinding it per stage without capturing
    the first handed the current stage whichever tree was built last."""
    job = make_job(tmp_path)
    d = payload(PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s",
                                stages=stages_of(job)))
    counts = {k: len(v["files"]) for k, v in d["stages"].items()}
    assert counts == {"3": 4, "4": 5, "5": 6}, counts     # README + n sections


def test_the_default_is_the_HIGHEST_stage(tmp_path):
    """The document as the pipeline finished it — what someone opening an AI-post-processed
    extraction is asking to see. Stage 3 stays one click away."""
    job = make_job(tmp_path)
    d = payload(PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s",
                                stages=stages_of(job)))
    assert d["stage"] == 5
    assert d["files"].keys() == d["stages"]["5"]["files"].keys()


def test_the_tree_dir_does_not_mislabel_the_current_stage(tmp_path):
    """The caller passes the stage-3 directory. Labelling THAT as the default stage is how a
    stage-3 tree gets served under a Stage 5 pill — silently, and only once the default
    stopped being 3."""
    job = make_job(tmp_path)
    d = payload(PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s",
                                stages=stages_of(job)))
    assert len(d["files"]) == 6, "stage 5's file count, not stage 3's"


def test_an_explicit_stage_is_honoured(tmp_path):
    job = make_job(tmp_path)
    d = payload(PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s",
                                stages=stages_of(job), current_stage=3))
    assert d["stage"] == 3 and len(d["files"]) == 4


def test_page_snapshots_stay_out_of_the_payload(tmp_path):
    """_assets is 59-62 files and ~10 MB per stage on a real document."""
    job = make_job(tmp_path)
    d = payload(PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s",
                                stages=stages_of(job)))
    for st in d["stages"].values():
        assert not any("_assets" in k for k in st["files"])


def test_the_template_renders_a_switcher(tmp_path):
    job = make_job(tmp_path)
    html = PH.build_viewer(job / "03_stage3_final", job / "source.pdf", "t", "s",
                           stages=stages_of(job))
    assert 'id="stage-switch"' in html and "renderStageSwitch" in html


# ---------------- the inspect shim ----------------
def test_the_shim_keys_on_the_stage_query():
    """Stripping the query served the same stage-3 tree whichever pill was clicked — a
    switcher that looked broken while working perfectly."""
    js = (Path(__file__).resolve().parent.parent / "assets" / "inspect_shim.js").read_text()
    assert "key + q" in js, "the shim must include the query in the payload key"
    assert "if (key in EMB.api)" in js, "and still fall back for pages built before this"
