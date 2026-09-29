"""Per-region floor: scoped cross-region search must not starve weak regions."""

import numpy as np
import pytest

pytest.importorskip("numpy")

from aosphere_core_index.embeddings.multi_index import MultiIndex


def _mk_index() -> MultiIndex:
    """3 regions x 10 rows, 4-dim. 'Strong' rows all score higher than every
    'Weak'/'Mid' row for the test query, so a global top-k is 100% Strong."""
    rng = np.random.default_rng(7)
    rows, regions = [], []
    for name, base in [("Strong", 0.9), ("Mid", 0.5), ("Weak", 0.2)]:
        for _ in range(10):
            v = np.array([base, 1 - base, 0, 0]) + rng.normal(0, 0.01, 4)
            rows.append(v / np.linalg.norm(v))
            regions.append(name)
    m = np.array(rows, dtype="float32")
    n = len(regions)
    return MultiIndex(
        regions=["Strong", "Mid", "Weak"], row_region=np.array(regions),
        keys=[f"K{i}" for i in range(n)], titles=[f"t{i}" for i in range(n)],
        levels=[1] * n, matrix=m, model="test",
        row_kind=np.array(["clause"] * n),
    )


QUERY = np.array([1.0, 0, 0, 0], dtype="float32")


def test_global_topk_starves_weak_regions():
    mi = _mk_index()
    hits = mi.search(QUERY, k=8, jurisdictions=["Strong", "Mid", "Weak"])
    assert {h["jurisdiction"] for h in hits} == {"Strong"}  # the failure mode


def test_per_region_floor_guarantees_representation():
    mi = _mk_index()
    hits = mi.search(QUERY, k=8, jurisdictions=["Strong", "Mid", "Weak"], per_region=3)
    by_region = {r: sum(1 for h in hits if h["jurisdiction"] == r)
                 for r in ("Strong", "Mid", "Weak")}
    assert by_region["Strong"] >= 3
    assert by_region["Mid"] >= 3
    assert by_region["Weak"] >= 3
    # still score-ordered
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_floor_ignored_when_unscoped():
    mi = _mk_index()
    hits = mi.search(QUERY, k=5, per_region=3)  # no jurisdictions -> plain top-k
    assert len(hits) == 5
    assert {h["jurisdiction"] for h in hits} == {"Strong"}


def test_scoped_filter_respected():
    mi = _mk_index()
    hits = mi.search(QUERY, k=10, jurisdictions=["Weak"], per_region=2)
    assert {h["jurisdiction"] for h in hits} == {"Weak"}


def test_unknown_region_empty():
    mi = _mk_index()
    assert mi.search(QUERY, k=5, jurisdictions=["Nowhere"]) == []
