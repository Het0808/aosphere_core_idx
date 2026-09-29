"""Normalise an alerts drop into data/alerts/alerts.json for offline indexing.

The drop lays out a consolidated list + per-alert attachment PDFs:
  <drop>/155_Data_Privacy/All_Alerts/<date>/All_Alerts_List.json
  <drop>/155_Data_Privacy/All_Alerts/<date>/<ALERTID>/<file>.pdf

For each alert we: split the comma-separated JURISDICTIONNAME into canonical
region names (so multi-jurisdiction alerts attach to every region they cover),
and extract the attachment PDF's text (build-time; PyMuPDF). Output keeps the
raw alert fields (TITLE/SUMMARY/IMPACT/PUBLICATIONDATE/ALERTID) so the existing
semantic mapper consumes it unchanged, plus `regions` and `attachment_text`.

  .venv/bin/python scripts/import_alerts.py [--dry-run] [<drop_dir>]
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

from aosphere_core_index.config import settings
from aosphere_core_index.extract.pdf_extract import extract_pdf_text_via
from aosphere_core_index.regions.region_map import qualified

DEFAULT_DROP = "All_Alerts_json_v1_2026-07-01"

# alert JURISDICTIONNAME -> our canonical region name(s)
ALIAS = {
    "EU Member States": "European Union",
    "Hong Kong SAR": "Hong Kong",
    "Türkiye": "Turkey",
    "Slovak Republic": "Slovakia",
}


def resolve_regions(jur: str, canon: set[str]) -> list[str]:
    """Map one alert jurisdiction name to the canonical region(s) it covers. Handles
    aliases, `US - X` -> `United States - X` (California -> both Long/Short forms),
    and bare US territories. Returns [] for jurisdictions we don't index."""
    j = re.sub(r"\s*\(Data Privacy\)$", "", jur).strip()
    j = ALIAS.get(j, j)
    if not j:
        return []
    if j in canon:
        return [j]
    m = re.match(r"^US - (.+)$", j)
    name = m.group(1) if m else None
    if name:  # e.g. "US - California" -> "United States - California (Long Form)"/(Short Form)
        pref = f"United States - {name}"
        return sorted(c for c in canon if c == pref or c.startswith(pref + " ("))
    if f"United States - {j}" in canon:  # bare US territory ("Guam", "Puerto Rico")
        return [f"United States - {j}"]
    return []


def _product_from_folder(name: str) -> str:
    """'155_Data_Privacy' -> 'Data Privacy'; '104_Shareholding_Disclosure' -> 'Shareholding Disclosure'."""
    return re.sub(r"^\d+_", "", name).replace("_", " ").strip()


def find_product_lists(drop: str) -> list[tuple[str, str]]:
    """[(product, latest All_Alerts_List.json)] for every '<id>_<Product>' folder in the
    drop — so BOTH Data Privacy and Shareholding Disclosure alerts are imported."""
    out = []
    for folder in sorted(glob.glob(os.path.join(drop, "*"))):
        if not os.path.isdir(folder):
            continue
        hits = glob.glob(os.path.join(folder, "All_Alerts", "*", "All_Alerts_List.json"))
        if hits:
            out.append((_product_from_folder(os.path.basename(folder)), sorted(hits)[-1]))
    return out


def _canon_for_product(product: str) -> set[str]:
    """Jurisdiction dir names indexed under data/products/<product>/ (nested layout)."""
    base = settings.products_dir / product
    return {os.path.basename(p) for p in glob.glob(str(base / "*"))
            if os.path.isdir(p) and not os.path.basename(p).endswith(".retired")}


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    drop = args[0] if args else DEFAULT_DROP
    lists = find_product_lists(drop)
    if not lists:
        sys.exit(f"No <product>/All_Alerts/*/All_Alerts_List.json under {drop}")

    out, n_att, unmatched = [], 0, set()
    for product, list_json in lists:
        date_dir = os.path.dirname(list_json)
        alerts = json.loads(open(list_json).read())
        canon = _canon_for_product(product)  # jurisdictions indexed for THIS product
        n0 = len(out)
        for a in alerts:
            regions = []
            for j in str(a.get("JURISDICTIONNAME", "")).split(","):
                if not j.strip():
                    continue
                matched = resolve_regions(j, canon)
                if matched:
                    # Qualify by product so it matches the region identity search uses:
                    # Data Privacy stays bare ('France'); others prefix ('Shareholding
                    # Disclosure — France'). Bare/unqualified would never match SD regions.
                    regions.extend(qualified(product, m) for m in matched)
                else:
                    unmatched.add(f"{product}: {re.sub(r'\s*\(Data Privacy\)$', '', j).strip()}")
            regions = sorted(set(regions))
            att_text = ""
            att = str(a.get("ATTACHMENT", "")).strip()
            if att:
                path = os.path.join(date_dir, str(a.get("ALERTID")), att)
                if os.path.exists(path):
                    att_text = extract_pdf_text_via(path, settings.extract_backend)
                    if att_text:
                        n_att += 1
            rec = dict(a)
            rec["regions"] = regions
            rec["attachment_text"] = att_text
            rec["product"] = product
            out.append(rec)
        print(f"  {product}: {len(alerts)} alerts, {len(out) - n0} imported")

    multi = [a for a in out if len(a["regions"]) > 1]
    print(f"alerts: {len(out)} | with attachment text: {n_att} | multi-jurisdiction: {len(multi)}")
    if unmatched:
        print(f"jurisdiction names with NO matching region ({len(unmatched)}): {sorted(unmatched)}")
    if dry:
        print("DRY RUN — not writing.")
        return
    dest = settings.data_dir / "alerts" / "alerts.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
