"""Agent tool-layer scope enforcement — the UI's product/jurisdiction selection
must bind what the tools search/read, regardless of what the model types."""

import pytest

pytest.importorskip("agents")
pytest.importorskip("litellm")

from aosphere_core_index.llm.agent import AgentCtx, _resolve_scope, _scope_note

SD = ["Shareholding Disclosure — Australia", "Shareholding Disclosure — France",
      "Shareholding Disclosure — Spain"]


def test_no_request_defaults_to_full_scope():
    assert _resolve_scope(AgentCtx(allowed=SD), None) == SD


def test_bare_name_resolves_within_selected_product():
    # the reported bug: SD selected, agent types 'Australia'
    out = _resolve_scope(AgentCtx(allowed=SD), ["Australia"])
    assert out == ["Shareholding Disclosure — Australia"]


def test_hyphen_variant_resolves():
    out = _resolve_scope(AgentCtx(allowed=SD), ["Shareholding Disclosure - France"])
    assert out == ["Shareholding Disclosure — France"]


def test_cannot_escape_scope():
    # a name outside the selection must NOT widen the search beyond `allowed`
    out = _resolve_scope(AgentCtx(allowed=SD), ["Germany"])
    assert out == SD  # falls back to the enforced scope, never outside it


def test_multiple_names():
    out = _resolve_scope(AgentCtx(allowed=SD), ["Australia", "Spain"])
    assert out == ["Shareholding Disclosure — Australia", "Shareholding Disclosure — Spain"]


def test_scope_note_compact():
    note = _scope_note(SD)
    assert "Shareholding Disclosure" in note
    assert "Australia" in note          # <=15 names are listed bare
    assert "—" not in note.split(":")[1].split("(")[0]  # product listed once, not per-identity
    big = [f"Shareholding Disclosure — J{i}" for i in range(40)]
    assert "40 jurisdictions" in _scope_note(big)
    assert _scope_note(None) == "" and _scope_note([]) == ""