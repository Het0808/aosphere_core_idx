"""Source Connector interface.

A Connector abstracts *where a product's documents come from*. It lists the
product's jurisdictions and fetches one jurisdiction's docs + joined metadata as
a `JurisdictionSource`, regardless of origin.

Decided source model: products' raw docs are exported into an S3 staging layout
  s3://<bucket>/<product>/<jurisdiction>/<docs + Doc_metadata.json>
so a single S3 connector covers all products. The current Data-Privacy
`working/<Jurisdiction>/` reader is one concrete connector.
"""

from __future__ import annotations

from typing import Protocol

from aosphere_core_index.ingest.source import JurisdictionSource


class Connector(Protocol):
    product: str

    def list_jurisdictions(self) -> list[str]:
        """Return the jurisdiction names available for this product."""
        ...

    def fetch(self, jurisdiction: str) -> JurisdictionSource:
        """Download a jurisdiction's docs + join its Doc_metadata.json."""
        ...
