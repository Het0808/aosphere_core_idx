"""Fetch a jurisdiction's source files from the read-only working/ prefix.

For a country folder we pull the DOCX/PDF source docs plus the structured JSON
sidecars (Doc_metadata.json, RAG_rated_answers.json, Survey_Alerts*.json) and
join documents to their metadata by DOCID.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from aosphere_core_index.aws.s3_readonly import ReadOnlyS3
from aosphere_core_index.config import settings


@dataclass
class JurisdictionSource:
    """Local paths + parsed sidecars for one jurisdiction."""

    name: str
    folder_prefix: str
    docs: list[dict] = field(default_factory=list)  # Doc_metadata records + local_path
    rated_answers: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)


def _read_json_key(s3: ReadOnlyS3, key: str) -> list | dict | None:
    try:
        return json.loads(s3.get_bytes(key))
    except Exception:
        return None


def _load_offline(name: str) -> JurisdictionSource:
    """Build a JurisdictionSource from the local source cache — no S3.

    Phase 2 of the two-account build: source was mirrored locally with the
    read-only .env keys (scripts/fetch_sources.py); embedding runs in a
    different account, so the build must not touch the source bucket. Mirrors
    fetch_jurisdiction's field layout from data/regions/<name>/source/.
    """
    folder = f"{settings.working_prefix}/{name}/"
    cache = settings.region_source(name)
    src = JurisdictionSource(name=name, folder_prefix=folder)
    if not cache.exists():
        raise FileNotFoundError(f"no local source cache for {name!r} at {cache} "
                                "(run scripts/fetch_sources.py first)")

    for f in cache.iterdir():
        base = f.name
        if base == "Doc_metadata.json":
            src.docs = list(json.loads(f.read_text()) or [])
        elif base == "RAG_rated_answers.json":
            src.rated_answers = json.loads(f.read_text()) or []
        elif base.startswith("Survey_Alerts"):
            src.alerts = json.loads(f.read_text()) or []

    for doc in src.docs:
        ext = str(doc.get("EXTENSION", "")).lower()
        filename = f"{doc.get('FILENAME')}.{ext}"
        local = cache / filename
        if local.exists():
            doc["local_path"] = str(local)
            doc["source_key"] = f"{folder}{filename}"
    return src


def fetch_jurisdiction(name: str, s3: ReadOnlyS3 | None = None) -> JurisdictionSource:
    """Download a jurisdiction's docs + sidecars to the local cache.

    With ACI_SOURCE_OFFLINE set, read the already-mirrored cache instead (lets
    the build run in a Bedrock-only account with no source-bucket access)."""
    if os.getenv("ACI_SOURCE_OFFLINE"):
        return _load_offline(name)
    s3 = s3 or ReadOnlyS3()
    folder = f"{settings.working_prefix}/{name}/"
    cache = settings.region_source(name)

    keys = s3.list_keys(folder)
    src = JurisdictionSource(name=name, folder_prefix=folder)

    # Sidecars
    for key in keys:
        base = key.rsplit("/", 1)[-1]
        if base == "Doc_metadata.json":
            meta = _read_json_key(s3, key) or []
            src.docs = list(meta)
        elif base == "RAG_rated_answers.json":
            src.rated_answers = _read_json_key(s3, key) or []
        elif base.startswith("Survey_Alerts"):
            src.alerts = _read_json_key(s3, key) or []

    # Attach a local path to each doc by matching <DOCID>.<ext>.
    by_filename: dict[str, str] = {}
    for key in keys:
        base = key.rsplit("/", 1)[-1]
        if base.lower().endswith((".docx", ".pdf")):
            by_filename[base] = key

    for doc in src.docs:
        ext = str(doc.get("EXTENSION", "")).lower()
        filename = f"{doc.get('FILENAME')}.{ext}"
        key = by_filename.get(filename)
        if key:
            local = s3.download(key, cache / filename)
            doc["local_path"] = str(local)
            doc["source_key"] = key

    return src
