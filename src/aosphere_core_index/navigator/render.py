"""Navigator: render the GraphIndex interactively (pyvis) and export GraphML.

Two views are produced:
  - a structure skeleton (jurisdiction -> opinion -> document -> section tree)
  - an ego view around any node (e.g. a clause or a question), to SEE how the
    graph would be traversed (question -> answer -> cited clause -> parent).
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
from pyvis.network import Network

_COLORS = {
    "jurisdiction": "#1f77b4",
    "opinion": "#9467bd",
    "document": "#2ca02c",
    "section": "#17becf",
    "chunk": "#9aa0a6",
    "footnote": "#bcbd22",
    "template": "#ff7f0e",
    "subject": "#ffbb78",
    "question": "#e377c2",
    "rated_answer": "#8e24aa",
    "alert": "#d62728",
}
_RATING_COLORS = {"Green": "#2e7d32", "Amber": "#f9a825", "Red": "#c62828"}


def _node_color(data: dict) -> str:
    if data.get("ntype") == "rated_answer":
        return _RATING_COLORS.get(data.get("color", ""), _COLORS["rated_answer"])
    return _COLORS.get(data.get("ntype"), "#cccccc")


def _hover(node: str, data: dict) -> str:
    bits = [f"type: {data.get('ntype')}"]
    if data.get("breadcrumb"):
        bits.append(f"path: {data['breadcrumb']}")
    if data.get("text"):
        bits.append(str(data["text"])[:300])
    return " | ".join(bits)


def _render(g: nx.DiGraph, nodes: list[str], out_html: Path, title: str) -> Path:
    net = Network(
        height="800px", width="100%", directed=True, cdn_resources="in_line",
        bgcolor="#ffffff", font_color="#222222",
    )
    net.barnes_hut(spring_length=140)
    sub = g.subgraph(nodes)
    for n, data in sub.nodes(data=True):
        net.add_node(
            n, label=str(data.get("label", n))[:48], title=_hover(n, data),
            color=_node_color(data), shape="dot",
            size=18 if data.get("ntype") in ("jurisdiction", "document") else 11,
        )
    for u, v, data in sub.edges(data=True):
        net.add_edge(u, v, label=data.get("rel", ""), arrows="to")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    net.set_options('{"edges":{"font":{"size":9},"color":{"opacity":0.5}},'
                    '"nodes":{"font":{"size":12}}}')
    net.save_graph(str(out_html))
    # Prepend a small title banner.
    html = out_html.read_text()
    out_html.write_text(html.replace("<body>", f"<body><h3 style='font-family:sans-serif'>{title}</h3>"))
    return out_html


def render_structure(g: nx.DiGraph, out_html: Path) -> Path:
    """The document skeleton: jurisdiction/opinion/document + full section tree."""
    nodes = [n for n, d in g.nodes(data=True)
             if d.get("ntype") in ("jurisdiction", "opinion", "document", "section")]
    return _render(g, nodes, out_html, "GraphIndex — document structure (France)")


def render_ego(g: nx.DiGraph, center: str, out_html: Path, radius: int = 2) -> Path:
    """Neighborhood around a node on the undirected closure — 'how AI navigates'."""
    ego = nx.ego_graph(g.to_undirected(as_view=True), center, radius=radius)
    return _render(g, list(ego.nodes), out_html,
                   f"GraphIndex — navigation around {g.nodes[center].get('label', center)}")


def _sanitize_for_graphml(g: nx.DiGraph) -> nx.DiGraph:
    """GraphML rejects None / non-primitive attrs; coerce to str/primitive."""
    h = g.copy()
    for _, data in h.nodes(data=True):
        for k, v in list(data.items()):
            if v is None:
                data[k] = ""
            elif not isinstance(v, (str, int, float, bool)):
                data[k] = str(v)
    for *_, data in h.edges(data=True):
        for k, v in list(data.items()):
            if v is None:
                data[k] = ""
            elif not isinstance(v, (str, int, float, bool)):
                data[k] = str(v)
    return h


def export_graphml(g: nx.DiGraph, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(_sanitize_for_graphml(g), str(path))
    return path


def find_node_by_clause(g: nx.DiGraph, key: str) -> str | None:
    for n, d in g.nodes(data=True):
        if d.get("ntype") == "section" and d.get("key") == key:
            return n
    return None
