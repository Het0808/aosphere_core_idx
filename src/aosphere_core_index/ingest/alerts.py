"""Gather all alerts for a region from the input/ prefix.

Alerts live in two places under input/:
  - input/<date>/<CountryFolder>/Survey_Alerts (XX).json   (per-date country batches)
  - input/All_Alerts/<date>/All_Alerts_List.json           (consolidated, all regions)

We union both, keep only the region's alerts (by JURISDICTIONNAME), and dedupe by
ALERTID (keeping the most recently exported copy).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from aosphere_core_index.aws.s3_readonly import ReadOnlyS3
from aosphere_core_index.config import settings

_INPUT_PREFIX = "Advanced_Search/155_Data_Privacy/input"
LOOKBACK_DAYS = 183  # ~6 months


def _offline_alerts_path():
    return settings.data_dir / "alerts" / "alerts.json"


def _gather_offline(region: str, cutoff: date) -> list[dict]:
    """Alerts for a region from the locally-normalised data/alerts/alerts.json
    (produced by scripts/import_alerts.py). Each record carries `regions` (the
    canonical regions the alert covers — multi-jurisdiction aware) and
    `attachment_text`. Filtered by region membership + lookback."""
    data = json.loads(_offline_alerts_path().read_text())
    out = []
    for a in data:
        if region not in a.get("regions", []):
            continue
        pub = _pub_date(a)
        if pub is None or pub < cutoff:
            continue
        out.append(a)
    return sorted(out, key=lambda a: str(a.get("PUBLICATIONDATE", "")), reverse=True)


def _pub_date(alert: dict) -> date | None:
    """Parse PUBLICATIONDATE like '2025-05-22 00:00:00.0' to a date."""
    raw = str(alert.get("PUBLICATIONDATE", ""))[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _load(s3: ReadOnlyS3, key: str) -> list:
    try:
        data = json.loads(s3.get_bytes(key))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def gather_region_alerts(
    region: str, s3: ReadOnlyS3 | None = None, lookback_days: int = LOOKBACK_DAYS,
    today: date | None = None,
) -> list[dict]:
    """Return unique alerts for a region published within the lookback window.

    Deduped by ALERTID (newest export wins). Alerts older than lookback_days, or
    with an unparseable publication date, are excluded.
    """
    cutoff = (today or date.today()) - timedelta(days=lookback_days)
    # Offline: prefer the locally-normalised alerts drop (no S3, multi-jurisdiction
    # aware, carries attachment text). Used by the offline build.
    if os.getenv("ACI_ALERTS_OFFLINE") or _offline_alerts_path().exists():
        return _gather_offline(region, cutoff)

    s3 = s3 or ReadOnlyS3()
    keys = s3.list_keys(_INPUT_PREFIX + "/")

    region_lc = region.lower()
    survey_keys = [
        k for k in keys
        if "/Survey_Alerts" in k and f"/{region}/".lower() in k.lower()
    ]
    all_alerts_keys = [k for k in keys if "/All_Alerts/" in k and k.endswith("All_Alerts_List.json")]

    raw: list[dict] = []
    for k in survey_keys:
        raw.extend(_load(s3, k))
    # From the consolidated feed, keep only this region's alerts.
    for k in all_alerts_keys:
        for a in _load(s3, k):
            if str(a.get("JURISDICTIONNAME", "")).lower() == region_lc:
                raw.append(a)

    by_id: dict[str, dict] = {}
    for a in raw:
        aid = str(a.get("ALERTID"))
        pub = _pub_date(a)
        if not aid or pub is None or pub < cutoff:
            continue
        prev = by_id.get(aid)
        if prev is None or str(a.get("EXPORT_DATE", "")) > str(prev.get("EXPORT_DATE", "")):
            by_id[aid] = a
    return sorted(by_id.values(), key=lambda a: str(a.get("PUBLICATIONDATE", "")), reverse=True)
