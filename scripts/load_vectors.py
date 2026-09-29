"""Load the flat multi.npz (vectors + metadata) into an external vector store, so we can
A/B external ANN backends against the in-memory exact index behind the SAME search_all.

Vectors are unit-normalized, so cosine == the in-memory dot product (fair comparison).

  ACI_VECTOR_BACKEND=opensearch python scripts/load_vectors.py     # -> local OpenSearch
  ACI_VECTOR_BACKEND=atlas      python scripts/load_vectors.py     # -> MongoDB Atlas
"""
import json
import os
import sys

import numpy as np

from aosphere_core_index.config import settings

# Resolve the index from the SAME place the service reads it (settings.products_dir,
# which honours ACI_DATA_DIR) so this loader run INSIDE the service container/pod
# picks up the already-mounted /data/products/_multi/multi.npz with no path fuss.
# Override with ACI_MULTI_NPZ for a local build elsewhere. Running in-container means
# volume -> OpenSearch in-region: no S3 hop, no laptop upload.
NPZ = os.getenv("ACI_MULTI_NPZ") or str(settings.products_dir / "_multi" / "multi.npz")
INDEX = os.getenv("ACI_VECTOR_INDEX", "aci-vectors")

# Optional region allow-list (JSON list of region names). Lets us stage only the eval
# regions into a small/free vector store — scoped retrieval is exact per region, so a
# region subset yields an identical eval to the full corpus while fitting a tiny tier.
_REGIONS_FILE = os.getenv("ACI_LOAD_REGIONS_FILE")
_REGIONS = set(json.load(open(_REGIONS_FILE))) if _REGIONS_FILE else None


def _rows():
    d = np.load(NPZ, allow_pickle=False)
    m, region, keys = d["matrix"], d["row_region"], d["keys"]
    titles, levels, kinds = d["titles"], d["levels"], d["row_kind"]
    n = m.shape[0]
    kept = 0
    print(f"{n} rows, dim {m.shape[1]}" + (f" | region allow-list: {len(_REGIONS)} regions" if _REGIONS else ""), flush=True)
    for i in range(n):
        reg = str(region[i])
        if _REGIONS is not None and reg not in _REGIONS:
            continue
        kept += 1
        yield i, m[i], reg, str(keys[i]), str(titles[i]), int(levels[i]), str(kinds[i])
    if _REGIONS is not None:
        print(f"filtered -> {kept} rows in allow-listed regions", flush=True)


def _print_progress(t0):
    import time as _t

    def prog(done, phase="inserting"):
        el = _t.perf_counter() - t0
        rate = f" ({done / el:.0f} docs/s)" if el else ""
        print(f"  {phase}: {done} in {el:.1f}s{rate}", flush=True)
    return prog


def load_opensearch():
    """Thin wrapper: build rows from multi.npz (honouring the region allow-list) and
    delegate to embeddings.vector_load (the one source of truth for index mappings)."""
    import time as _t

    from aosphere_core_index.embeddings import vector_load
    d = np.load(NPZ, allow_pickle=False)
    dim, total = int(d["matrix"].shape[1]), int(d["matrix"].shape[0])
    r = vector_load.load_opensearch(_rows(), total, dim, _print_progress(_t.perf_counter()))
    print(f"OpenSearch indexed ok={r['indexed']} errs={r['errors']} count={r['count']}/{total}", flush=True)


def load_atlas():
    import time as _t

    from aosphere_core_index.embeddings import vector_load
    d = np.load(NPZ, allow_pickle=False)
    dim, total = int(d["matrix"].shape[1]), int(d["matrix"].shape[0])
    r = vector_load.load_atlas(_rows(), total, dim, _print_progress(_t.perf_counter()),
                               batch=int(os.getenv("ACI_ATLAS_BATCH", "2000")))
    print(f"Atlas: inserted {r['inserted']} count={r['count']} index={r['index']}", flush=True)


if __name__ == "__main__":
    b = os.getenv("ACI_VECTOR_BACKEND", "opensearch").strip().lower()
    print(f"loading -> {b}", flush=True)
    (load_atlas if b == "atlas" else load_opensearch)()
    print("LOAD_DONE", flush=True)
