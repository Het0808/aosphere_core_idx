"""Session-store bounds: TTL expiry, history cap, max-session eviction."""

import pytest

pytest.importorskip("agents")  # agent.py imports the OpenAI Agents SDK at module load
pytest.importorskip("litellm")

from aosphere_core_index.llm import agent


@pytest.fixture(autouse=True)
def _clean_store():
    agent._SESSIONS.clear()
    yield
    agent._SESSIONS.clear()


def test_roundtrip():
    agent._session_store("s1", [{"role": "user", "content": "hi"}])
    assert agent._session_history("s1") == [{"role": "user", "content": "hi"}]
    assert agent._session_history(None) == []
    assert agent._session_history("unknown") == []


def test_history_capped():
    items = [{"i": n} for n in range(agent._HISTORY_CAP + 25)]
    agent._session_store("s1", items)
    assert len(agent._session_history("s1")) == agent._HISTORY_CAP
    assert agent._session_history("s1")[-1] == items[-1]  # newest kept


def test_ttl_expiry(monkeypatch):
    agent._session_store("s1", [{"i": 1}])
    real = agent.time.monotonic
    monkeypatch.setattr(agent.time, "monotonic", lambda: real() + agent._SESSION_TTL + 1)
    assert agent._session_history("s1") == []
    assert "s1" not in agent._SESSIONS  # expired entry removed


def test_max_sessions_evicts_oldest(monkeypatch):
    monkeypatch.setattr(agent, "_SESSION_MAX", 3)
    for n in range(4):
        agent._session_store(f"s{n}", [{"i": n}])
    assert len(agent._SESSIONS) <= 3
    assert "s0" not in agent._SESSIONS  # oldest evicted
    assert agent._session_history("s3") == [{"i": 3}]
