"""Pure request-shaping helpers from the FastAPI app (no index/data needed)."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("numpy")  # app -> registry -> embeddings

from fastapi import HTTPException

from aosphere_core_index.service.app import _parse_jurisdictions, _scope


def test_parse_jurisdictions_none_and_empty():
    assert _parse_jurisdictions(None) is None
    assert _parse_jurisdictions("") is None
    assert _parse_jurisdictions(" , ,") is None


def test_parse_jurisdictions_strips():
    assert _parse_jurisdictions(" France , Germany ") == ["France", "Germany"]


def test_parse_jurisdictions_count_cap():
    too_many = ",".join(f"J{i}" for i in range(500))
    with pytest.raises(HTTPException) as e:
        _parse_jurisdictions(too_many)
    assert e.value.status_code == 400


def test_scope_no_entitlements_passthrough():
    user = {}  # no entitlement claim -> all allowed
    assert _scope(["France"], user) == ["France"]
    assert _scope(None, user) is None


def test_scope_intersects_entitlements():
    user = {"jurisdictions": ["France", "Germany"]}
    assert _scope(["France", "Japan"], user) == ["France"]
    assert _scope(None, user) == ["France", "Germany"]  # default to entitled set
