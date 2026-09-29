"""Vendored from the "MinerU2.5" document-chunking project (local repo, not
published — commit dc47d6148d351b6627d1dca8f6927c885a9f4446, 2026-07-10).

Modules here (`extraction.py`, `office_footnotes.py`, `hierarchy.py`) are kept
byte-for-byte identical to their source except for this attribution header —
do not hand-edit; fix upstream and re-vendor instead. The aosphere-side
integration (orchestration, level-convention translation into this repo's own
clause-key `Section` model) lives in `extract/mineru_adapter.py` and
`extract/profiles/mineru_profile.py`, not in this package.

Deliberately NOT vendored: `kg_entity.py`, `pipeline.py`, `jobs.py`, `main.py`,
`pages.py`, `static/` — the source project's own knowledge_graph.json/URN
schema and web-app layer, which this integration does not adopt (see
docs/MINERU_MIGRATION.md for why).
"""

from __future__ import annotations
