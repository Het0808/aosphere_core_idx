"""A clause too long for one vector: chunked for the INDEX, whole for everything else.

The contract, and why each half matters:

  * content.json holds ONE section per clause. Windowing used to create a section per
    window, which pushed an index detail into the product — a comparison table spanning
    two windows arrived as `E2.2` and `E2.2#c1`, and the reader drew two half-tables.
  * the embedder splits that clause into overlapping rows keyed `<key>#c<i>`, because the
    whole clause does not fit one vector and the tail must stay searchable (it used to be
    truncated away).
  * a hit on `<key>#c3` collapses back to `<key>` at query time, and AI Mode's read then
    returns the WHOLE clause — not the window that happened to match.

The last point is the one that fails silently: a partial read makes the model answer
thinly rather than error, so it is asserted here on text taken from the START, MIDDLE and
END of a long clause, plus its table.
"""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from aosphere_core_index.embeddings.section_index import (  # noqa: E402
    _SNIPPET_CHARS, _windows, answer_text_dict,
)
from aosphere_core_index.llm import agent as A  # noqa: E402

# markers placed far apart in the body, so a windowed read cannot contain them all
HEAD = "OPENING-MARKER the first obligation is registration with the regulator."
MID = "MIDDLE-MARKER cross-border transfers require an adequacy assessment."
TAIL = "CLOSING-MARKER enforcement is by administrative fine only."
FILLER = ("Each controller shall maintain a record of processing activities describing "
          "the categories of data subjects and the purposes of the processing. ")
TABLE = ("| Mechanism | Available | Conditions |\n"
         "| --- | --- | --- |\n"
         "| Adequacy decision | Yes | Recipient country listed by the regulator |\n"
         "| Standard clauses | Yes | Plus a transfer impact assessment |")


def _long_body() -> str:
    """A clause several windows long, with its markers at known positions."""
    bulk = FILLER * 40                      # ~4x _SNIPPET_CHARS of prose
    half = len(bulk) // 2
    return f"{HEAD}\n\n{bulk[:half]}\n\n{MID}\n\n{bulk[half:]}\n\n{TAIL}\n\n{TABLE}\n"


@pytest.fixture
def converted(tmp_path):
    """Run the real converter over a one-clause md tree and return its content.json."""
    tree = tmp_path / "03_stage3_final" / "E-data-sharing" / "02-international-transfer"
    tree.mkdir(parents=True)
    (tree / "2.2-lawful-mechanisms.md").write_text(
        f"# 2.2 Lawful international transfer mechanisms\n\n"
        f"*Source: `source.pdf`, page 41*\n\n{_long_body()}", encoding="utf-8")
    out = tmp_path / "Testland.content.json"
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "tree_to_content.py"),
                        str(out), str(tmp_path / "03_stage3_final"), "Testland", "Data Privacy"],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stderr or r.stdout
    return json.loads(out.read_text())


def test_the_clause_is_long_enough_to_be_chunked(converted):
    """Guard the premise: if this stops exceeding one window the test proves nothing."""
    body = answer_text_dict(converted["sections"][0]["elements"])
    assert len(body) > _SNIPPET_CHARS * 2
    assert len(_windows(body)) > 2


def test_content_has_one_whole_section_not_one_per_window(converted):
    secs = converted["sections"]
    assert [s["key"] for s in secs] == ["E2.2"], "a window must not become its own section"
    body = answer_text_dict(secs[0]["elements"])
    for marker in (HEAD, MID, TAIL):
        assert marker.split()[0] in body


def test_the_table_survives_whole_in_one_element(converted):
    tables = [e for e in converted["sections"][0]["elements"] if e["kind"] == "table"]
    assert len(tables) == 1, "the table must not be split across elements or sections"
    assert tables[0]["text"].count("\n") == 3          # header + separator + 2 data rows
    assert "Adequacy decision" in tables[0]["text"] and "Standard clauses" in tables[0]["text"]


def test_the_index_chunks_it_with_collapsible_keys(converted):
    """The embed side: window 0 keeps the bare key, the rest carry `#c<i>` — the suffix
    registry strips when a hit comes back."""
    sec = converted["sections"][0]
    body = answer_text_dict(sec["elements"])
    wins = _windows(body)
    keys = [sec["key"] if i == 0 else f"{sec['key']}#c{i}" for i in range(len(wins))]
    assert keys[0] == "E2.2" and keys[1] == "E2.2#c1" and len(keys) > 2
    assert all(len(w) <= _SNIPPET_CHARS for w in wins), "a row must fit the embedder"
    # every marker is reachable through SOME row, which is the point of windowing
    joined = " ".join(wins)
    for marker in (HEAD.split()[0], MID.split()[0], TAIL.split()[0]):
        assert marker in joined
    # and a hit on any row resolves to the clause
    assert {k.split("#", 1)[0] for k in keys} == {"E2.2"}


def test_ai_mode_reads_the_whole_clause_not_the_matched_window(converted, monkeypatch):
    """The one that fails silently: AI Mode must receive the FULL clause for a key that
    was chunked, including the far end and the table — not the window that matched."""
    bundle = types.SimpleNamespace(
        content=converted,
        sections_by_key={s["key"]: s for s in converted["sections"]},
    )
    monkeypatch.setattr("aosphere_core_index.service.registry.get_bundle",
                        lambda _j: bundle)
    monkeypatch.setattr(A, "_resolve_scope", lambda *_a, **_k: ["Testland"])
    ctx = types.SimpleNamespace(emit=lambda *_a, **_k: None, sources=[],
                               jurisdictions=["Testland"], selected=["Testland"])

    text = A._read_one(ctx, "Testland", "E2.2")

    for marker in (HEAD, MID, TAIL):
        assert marker.split()[0] in text, f"{marker.split()[0]} missing — read was partial"
    assert "Adequacy decision" in text, "the clause's table did not reach the model"
    assert len(text) > _SNIPPET_CHARS * 2, "read looks like a single window"
    assert ctx.sources and ctx.sources[0]["key"] == "E2.2"


def test_ai_mode_read_covers_every_element_kind(converted, monkeypatch):
    """An ALLOW-LIST of element kinds starved this read once already: the docx export
    writes 'body', the extraction pipeline writes 'answer', and filtering to the former
    returned 0 characters for a 1,405-character clause. Kinds are producer vocabulary, so
    the read must not depend on knowing them all."""
    sec = dict(converted["sections"][0])
    sec["elements"] = [{"kind": "answer", "text": "ANSWERKIND obligations apply."},
                       {"kind": "body", "text": "BODYKIND obligations apply."},
                       {"kind": "bullet", "text": "BULLETKIND one exemption exists."},
                       {"kind": "summary", "text": "SUMMARYKIND in short, it applies."},
                       {"kind": "table", "text": TABLE},
                       {"kind": "future_kind_nobody_declared", "text": "NEWKIND still content."}]
    bundle = types.SimpleNamespace(content={"sections": [sec], "doc": {}},
                                   sections_by_key={sec["key"]: sec})
    monkeypatch.setattr("aosphere_core_index.service.registry.get_bundle", lambda _j: bundle)
    monkeypatch.setattr(A, "_resolve_scope", lambda *_a, **_k: ["Testland"])
    ctx = types.SimpleNamespace(emit=lambda *_a, **_k: None, sources=[],
                               jurisdictions=["Testland"], selected=["Testland"])

    text = A._read_one(ctx, "Testland", "E2.2")

    for marker in ("ANSWERKIND", "BODYKIND", "BULLETKIND", "SUMMARYKIND",
                   "Adequacy decision", "NEWKIND"):
        assert marker in text, f"{marker} dropped from the clause read"
