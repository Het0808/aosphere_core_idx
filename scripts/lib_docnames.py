#!/usr/bin/env python3
"""lib_docnames — the human name of a document, from the source corpus's Doc_metadata.

The pipeline identifies a document by its numeric id (172490), which is what every
tree, manifest entry and gallery row has shown so far. The source corpus ships a
`Doc_metadata.json` beside each jurisdiction's files listing every document in it:

    {"OPINIONNAME": "Brazil (Data Privacy)", "DOCNAME": "Data Privacy Survey dated …",
     "FILENAME": "172490", "DOCID": 172490, "VERSION": 15, "SOURCEDATE": "March, 30 2025 …"}

DOCNAME is the label to display. OPINIONNAME is not used: it repeats the jurisdiction
the row already sits under ("Brazil (Data Privacy)"). DOCNAME is what distinguishes the
documents WITHIN that jurisdiction — "Data Privacy Survey dated 30th September, 2025",
or plainly the edition date where that is all the publisher recorded.

One index is built per process from every Doc_metadata.json under the known corpus
roots and cached — a document is looked up by id, so where its folder happens to sit
does not matter.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# every place a source corpus has been unpacked in this repo
ROOTS = ("RAG-json_docx_v1_2026-06-30", "data-all-produc-samples", "out/samples",
         "All_Alerts_json_v1_2026-07-01")

_index: dict[str, dict] | None = None


def index() -> dict[str, dict]:
    global _index
    if _index is not None:
        return _index
    _index = {}
    for root in ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for f in base.rglob("Doc_metadata.json"):
            try:
                rows = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, ValueError):
                continue
            for r in (rows if isinstance(rows, list) else [rows]):
                if not isinstance(r, dict):
                    continue
                # keyed by BOTH: FILENAME is what the extracted job dir is named after,
                # DOCID is the same number as an int in most (not all) records
                for key in (r.get("FILENAME"), r.get("DOCID")):
                    if key:
                        _index.setdefault(str(key), r)
    return _index


def _rows_of(payload) -> list[dict]:
    """The records in one parsed Doc_metadata.json — a list, or a single object."""
    rows = payload if isinstance(payload, list) else [payload]
    return [r for r in rows if isinstance(r, dict)]


def index_from_payloads(payloads) -> dict[str, dict]:
    """Build the id -> record index from ALREADY-PARSED Doc_metadata.json bodies.

    The on-disk `index()` walks repo-local corpus roots, which do not exist in a
    promotion pod: there the same files are objects under `corpus-src/`, fetched and
    parsed by the caller. Same keying rule as `index()` — by FILENAME (what the
    extracted job dir is named after) AND by DOCID — so a lookup by either works.
    """
    out: dict[str, dict] = {}
    for payload in payloads:
        for r in _rows_of(payload):
            for key in (r.get("FILENAME"), r.get("DOCID")):
                if key:
                    out.setdefault(str(key), r)
    return out


def fields_from_record(r: dict) -> dict:
    """{name, version, date, opinion} from one Doc_metadata row — empty dict for no row.

    Split out of `lookup` so a caller holding its own index (see `index_from_payloads`)
    projects the record exactly the way the on-disk path does, rather than reaching for
    the raw uppercase keys and drifting.
    """
    if not r:
        return {}
    return {
        "name": r.get("DOCNAME") or r.get("OPINIONNAME"),
        "version": r.get("VERSION"),
        "date": r.get("SOURCEDATE") or r.get("MODIFIEDDATE"),
        "opinion": r.get("OPINIONNAME"),
    }


def lookup(doc_id: str | int) -> dict:
    """{name, version, date, opinion} for a document id — empty dict if unknown."""
    return fields_from_record(index().get(str(doc_id)) or {})


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for a in sys.argv[1:]:
            print(a, "->", json.dumps(lookup(a), ensure_ascii=False))
    else:
        print(f"{len(index())} document ids indexed from {ROOTS}")
