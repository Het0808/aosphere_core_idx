"""Export the full graph (all node types, incl. footnotes) for the interactive
force navigator. Footnotes/chunks are connected via their edges and revealed on
demand when a node is expanded, so the view stays legible at scale.
"""

from __future__ import annotations

import networkx as nx


def build_compact(g: nx.DiGraph) -> dict:
    """Compact, navigable JSON of the whole graph (labels + edges, no heavy text)."""
    nodes = [
        {
            "id": n,
            "t": d.get("ntype"),
            "l": str(d.get("label", n))[:80],
            "k": d.get("key"),
            "lv": d.get("level"),
            "bc": d.get("breadcrumb"),
            "col": d.get("color"),
            "text": (str(d.get("text", ""))[:400] or None),
        }
        for n, d in g.nodes(data=True)
    ]
    edges = [{"s": u, "d": v, "r": dd.get("rel")} for u, v, dd in g.edges(data=True)]
    return {"nodes": nodes, "edges": edges}
