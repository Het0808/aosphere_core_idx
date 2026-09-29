"""The chain announces WHY it escalated, before it spends the time.

`step` announces only a tier NAME, so a monitoring screen could see "now in mineru_full" and
never learn what sent it there — a document on its second pass looked identical to one that had
never left Stage 1. The tier takes minutes, so an announcement that waited for the scorecard
would arrive after the wait it explains.

The tier itself is stubbed here: what is under test is the ORDER and the CONTENT of the
announcement, not the extraction.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fc():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("fallback_chain",
                                                  ROOT / "scripts" / "fallback_chain.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fallback_chain"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def events():
    return []


def _wire(fc, monkeypatch, events, *, pages=90, enter=True,
          why="completeness 35.1 < 60; toc 65.0 < 70"):
    """Stub every tier so the chain runs in milliseconds, recording the order of events."""
    monkeypatch.setattr(fc, "pdf_page_count", lambda pdf: pages)
    monkeypatch.setattr(fc, "needs_help", lambda sc: (enter, why))
    monkeypatch.setattr(fc, "structure_recovered", lambda sc: (True, "structure is sound"))
    monkeypatch.setattr(fc, "result_is_acceptable", lambda sc: (True, ""))

    def _min(dest, pdf, val, sc, **kw):
        events.append(("tier", "mineru_full"))
        return val, sc, {"tier": "mineru_full", "adopted": True, "status": "re-parsed"}

    monkeypatch.setattr(fc, "_mineru_full", _min)
    return lambda info: events.append(("trigger", info))


def test_the_trigger_is_announced_before_any_tier_runs(fc, monkeypatch, events, tmp_path):
    """The whole point: the screen must know why while it is waiting, not afterwards."""
    cb = _wire(fc, monkeypatch, events)
    # Content actually missing, so the one remaining tier is reached. The chain used to
    # open with an UNGATED printed-TOC tier, so any escalating scorecard ran something;
    # now MinerU full is gated on structure and completeness, and a scorecard that only
    # says "toc 65" is one the chain looks at and declines.
    sc = {"dimensions": {"toc": {"score": 65.0, "detail": {"status": "lacking"}},
                         "completeness": {"score": 35.1}}}
    fc.run_chain(tmp_path, tmp_path / "x.pdf", {}, sc, on_trigger=cb)
    kinds = [e[0] for e in events]
    assert kinds[0] == "trigger", f"the trigger must come first, got {kinds}"
    assert "tier" in kinds, "a tier still has to run"


def test_the_announcement_carries_the_reason_and_the_first_pass_state(fc, monkeypatch,
                                                                     events, tmp_path):
    cb = _wire(fc, monkeypatch, events)
    sc = {"gate": "fail", "worst_score": 35.1, "weakest_dimension": "completeness",
          "dimensions": {"completeness": {"score": 35.1},
                         "toc": {"score": 65.0,
                                 "detail": {"status": "lacking", "rescuable": True}}}}
    fc.run_chain(tmp_path, tmp_path / "x.pdf", {}, sc, on_trigger=cb)
    info = next(e[1] for e in events if e[0] == "trigger")
    assert info["reason"] == "completeness 35.1 < 60; toc 65.0 < 70"
    first = info["first_attempt"]
    assert first["toc_score"] == 65.0 and first["toc_status"] == "lacking"
    assert first["toc_rescuable"] is True
    assert first["weakest_dimension"] == "completeness"


def test_a_short_document_announces_its_own_reason(fc, monkeypatch, events, tmp_path):
    """It skips tier 2 entirely, so needs_help never runs and entry_reason stays None — this
    path is 14 of the current corpus's 18 fallbacks."""
    cb = _wire(fc, monkeypatch, events, pages=6)
    fc.run_chain(tmp_path, tmp_path / "x.pdf", {}, {"dimensions": {}}, on_trigger=cb)
    info = next(e[1] for e in events if e[0] == "trigger")
    assert "short document (6 pages)" in info["reason"]
    assert [e[0] for e in events][0] == "trigger"


def test_a_document_that_does_not_escalate_announces_nothing(fc, monkeypatch, events, tmp_path):
    cb = _wire(fc, monkeypatch, events, enter=False, why="completeness and structure both fine")
    fc.run_chain(tmp_path, tmp_path / "x.pdf", {}, {"dimensions": {}}, on_trigger=cb)
    assert events == []


def test_a_raising_callback_never_breaks_the_run(fc, monkeypatch, events, tmp_path):
    """Progress reporting is bookkeeping. It must not cost a GPU hour."""
    _wire(fc, monkeypatch, events)

    def boom(info):
        raise RuntimeError("monitoring is down")

    sc = {"dimensions": {"toc": {"score": 65.0}, "completeness": {"score": 35.1}}}
    fc.run_chain(tmp_path, tmp_path / "x.pdf", {}, sc, on_trigger=boom)   # must not raise
    assert ("tier", "mineru_full") in events


def test_the_chain_still_works_with_no_callback_at_all(fc, monkeypatch, events, tmp_path):
    """Every existing caller passes none."""
    _wire(fc, monkeypatch, events)
    val, sc = fc.run_chain(tmp_path, tmp_path / "x.pdf", {}, {"dimensions": {}})
    assert (sc.get("fallback") or {}).get("triggered") is True
