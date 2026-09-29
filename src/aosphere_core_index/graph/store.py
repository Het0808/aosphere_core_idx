"""Persist / load the GraphIndex artifact."""

from __future__ import annotations

import pickle
from collections import Counter
from pathlib import Path

import networkx as nx


def save_graph(g: nx.DiGraph, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(g, fh)
    return path


def load_graph(path: Path) -> nx.DiGraph:
    with path.open("rb") as fh:
        return pickle.load(fh)


def graph_summary(g: nx.DiGraph) -> dict:
    """Counts of nodes by type and edges by relationship."""
    ntypes = Counter(d.get("ntype") for _, d in g.nodes(data=True))
    rels = Counter(d.get("rel") for *_, d in g.edges(data=True))
    return {
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "node_types": dict(ntypes),
        "rels": dict(rels),
    }
