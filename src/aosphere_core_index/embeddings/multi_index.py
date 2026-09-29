"""A single flat vector index across many regions.

Each row carries its region + clause_key as metadata, so one query searches all
regions at once and results are naturally tagged by region (and filterable by a
subscriber's entitled regions). Built by concatenating the cached per-region
section indices — no giant merged graph.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from aosphere_core_index.embeddings.section_index import SectionIndex, load_index
from aosphere_core_index.config import settings

log = logging.getLogger(__name__)

# Cap the per-row display title stored in the flat index. numpy fixed-width unicode
# pads EVERY title to the longest one, so a single mis-extracted paragraph-length
# "title" (~1200 chars) inflated the whole titles array to ~500MB (index 979MB).
# Titles this long are extraction artifacts, the UI shows only the first line, and
# the reranker uses the full content.json title — so capping is display-only.
_TITLE_MAX = int(os.getenv("ACI_MULTI_TITLE_MAX", "200"))


@dataclass
class MultiIndex:
    regions: list[str]
    row_region: np.ndarray  # per-row region name
    keys: list[str]
    titles: list[str]
    levels: list[int]
    matrix: np.ndarray
    model: str
    row_kind: np.ndarray | None = None  # per-row kind: "clause"|"guidance"|"alert"
    n_rows: int = -1                    # true row count; survives a metadata-lean load
    _rows_cache: dict | None = None     # region -> np.ndarray of row indices (lazy)

    def __post_init__(self):
        if self.n_rows < 0:
            self.n_rows = len(self.keys)

    def _rows(self, region: str) -> np.ndarray:
        """Row indices for one region, cached (regions' rows are contiguous slices
        of the concatenated matrix, but we don't rely on that)."""
        if self._rows_cache is None:
            self._rows_cache = {}
        rows = self._rows_cache.get(region)
        if rows is None:
            rows = np.where(self.row_region == region)[0]
            self._rows_cache[region] = rows
        return rows

    def search(
        self, query_vec: np.ndarray, k: int = 10,
        jurisdictions: list[str] | None = None, per_region: int = 0,
    ) -> list[dict]:
        """Top-k across all rows, optionally restricted to entitled jurisdictions.

        `per_region` (scoped queries only): additionally guarantee each scoped
        region its own top-m rows, unioned with the global top-k. Without it, one
        strong region can crowd the others out of the candidate pool before the
        reranker ever sees them — fatal for "compare across N jurisdictions"
        queries. Costs nothing extra: similarities for the whole scope are already
        computed; this only changes which rows are kept.
        """
        if self.matrix.size == 0 and self.n_rows:
            raise RuntimeError(
                "MultiIndex.search on a metadata-only index (matrix not loaded). This index "
                "was loaded for an external vector backend; route through "
                "vector_backend.retrieve() instead, or load with with_matrix=True.")
        groups: list[tuple[str, np.ndarray]] | None = None
        if jurisdictions:
            groups = [(r, self._rows(r)) for r in dict.fromkeys(jurisdictions)]
            groups = [(r, g) for r, g in groups if g.size]
            if not groups:
                return []
            idxs = np.concatenate([g for _, g in groups])
            sims = self.matrix[idxs] @ query_vec
        else:
            # No filter: score the whole matrix in place — avoid copying all
            # ~150MB of vectors (matrix[idxs]) on every unscoped query.
            idxs = None
            sims = self.matrix @ query_vec
        sel = [int(o) for o in np.argsort(-sims)[:k]]
        if per_region > 0 and groups is not None:
            off = 0
            for _r, g in groups:  # each region's rows are a contiguous slice of sims
                local = sims[off:off + g.size]
                top = np.argsort(-local)[:per_region] + off
                sel.extend(int(t) for t in top)
                off += g.size
            seen: set[int] = set()
            sel = [s for s in sorted(sel, key=lambda s: -sims[s])
                   if not (s in seen or seen.add(s))]
        out = []
        for o in sel:
            i = o if idxs is None else int(idxs[o])
            out.append({
                "jurisdiction": str(self.row_region[i]),
                "key": self.keys[i],
                "title": self.titles[i],
                "level": self.levels[i],
                "score": float(sims[o]),
                "kind": str(self.row_kind[i]) if self.row_kind is not None else "clause",
            })
        return out


def _multi_path() -> Path:
    return settings.products_dir / "_multi" / "multi.npz"


def _regions_with_artifact(suffix: str) -> list[str]:
    """Product-qualified region names owning <region>.<suffix>. Scans the product-nested
    layout data/products/<product>/<jurisdiction>/artifacts/ (top-level _ dirs skipped)."""
    from aosphere_core_index.regions.region_map import qualified

    found = []
    if not settings.products_dir.exists():
        return found
    for product_dir in sorted(settings.products_dir.iterdir()):
        if not product_dir.is_dir() or product_dir.name.startswith("_"):
            continue
        for jur_dir in sorted(product_dir.iterdir()):
            if not jur_dir.is_dir():
                continue
            name = qualified(product_dir.name, jur_dir.name)
            if (jur_dir / "artifacts" / f"{name}.{suffix}").exists():
                found.append(name)
    return found


def regions_with_index() -> list[str]:
    """Regions with a cached section index (embedded) on disk."""
    return _regions_with_artifact("sections.npz")


def regions_with_content() -> list[str]:
    """Regions with extracted CONTENT on disk, embedded or not.

    A freshly built data dir has content.json and no .npz — scripts/build_hybrid_index.py
    converts extracted trees into content and stops there — so a first embed cannot be
    driven off regions_with_index(), which by definition only lists what was embedded
    already. That is what made `aci reembed` report "0 jurisdictions" against a new dir."""
    return _regions_with_artifact("content.json")


def build_multi(regions: list[str], model_name: str) -> MultiIndex:
    """Concatenate per-region section indices into one flat MultiIndex.

    A region is included only if its cached index matches `model_name` — you can't
    mix embedding models in one flat index (dims/spaces differ). Skips are LOGGED
    (WARNING): a silent skip previously let bge-built regions vanish from a titan
    index unnoticed. `mismatched`/`missing`/`empty` counts are summarised at the end.
    """
    row_region, keys, titles, levels, kinds, mats = [], [], [], [], [], []
    used = []
    missing, mismatched, empty = [], [], []
    for r in regions:
        p = settings.region_artifacts(r) / f"{r}.sections.npz"
        if not p.exists():
            missing.append(r)
            continue
        idx: SectionIndex = load_index(p)
        if idx.model != model_name:
            mismatched.append((r, idx.model))
            continue
        if not idx.keys:
            empty.append(r)
            continue
        n = len(idx.keys)
        row_region += [r] * n
        keys += list(idx.keys)
        titles += [str(t)[:_TITLE_MAX] for t in idx.titles]  # cap: avoid numpy pad-to-longest bloat
        levels += list(idx.levels)
        kinds += idx.row_kinds()
        mats.append(idx.matrix)
        used.append(r)
    if mismatched:
        log.warning(
            "multi-index: SKIPPED %d region(s) with a model != %r (rebuild them or "
            "they are absent from search): %s", len(mismatched), model_name,
            ", ".join(f"{r} [{m}]" for r, m in mismatched[:20])
            + (" …" if len(mismatched) > 20 else ""),
        )
    if missing:
        log.warning("multi-index: %d region(s) had no .sections.npz (not built): %s",
                    len(missing), ", ".join(missing[:20]) + (" …" if len(missing) > 20 else ""))
    if empty:
        log.warning("multi-index: %d region(s) had an empty index: %s",
                    len(empty), ", ".join(empty[:20]))
    log.info("multi-index: %d region(s) included, %d rows (model=%s)",
             len(used), len(keys), model_name)
    matrix = np.vstack(mats) if mats else np.zeros((0, 384), dtype="float32")
    return MultiIndex(
        regions=used, row_region=np.array(row_region), keys=keys,
        titles=titles, levels=levels, matrix=matrix, model=model_name,
        row_kind=np.array(kinds),
    )


def save_multi(mi: MultiIndex) -> Path:
    p = _multi_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        p, regions=np.array(mi.regions), row_region=mi.row_region,
        keys=np.array(mi.keys), titles=np.array(mi.titles),
        levels=np.array(mi.levels), matrix=mi.matrix, model=np.array(mi.model),
        row_kind=(mi.row_kind if mi.row_kind is not None else np.array(["clause"] * len(mi.keys))),
    )
    return p


def index_model() -> str | None:
    """The model name the cached multi-index was built with, read WITHOUT loading the
    matrix (so an offline service can pick a matching query encoder cheaply)."""
    p = _multi_path()
    if not p.exists():
        return None
    try:
        z = np.load(p, allow_pickle=False)
        return str(z["model"])
    except Exception:
        return None


def load_multi(with_matrix: bool = True) -> MultiIndex | None:
    """Load the cached flat index.

    `with_matrix=False` is a METADATA-LEAN load for an EXTERNAL vector backend: it skips BOTH
    the vector matrix (~0.5GB) AND the per-row arrays (keys/titles/row_region/levels/row_kind,
    ~0.2GB) — keeping only the region list, model, and a row count. Under opensearch/atlas the
    store returns title/key/level/kind per hit and retrieve() ignores the in-memory index, so
    the serving path needs nothing else from here (see service.registry.get_multi; healthz uses
    n_rows). np.load reads each .npz member lazily, so the skipped arrays are never read into
    RAM. Admin reindex / eval-divergence force a full load via registry.multi_with_matrix()."""
    p = _multi_path()
    if not p.exists():
        return None
    z = np.load(p, allow_pickle=False)
    if not with_matrix:
        return MultiIndex(
            regions=list(z["regions"]), row_region=np.empty(0, dtype="<U1"),
            keys=[], titles=[], levels=[], matrix=np.empty((0, 0), dtype="float32"),
            model=str(z["model"]), row_kind=None,
            n_rows=int(z["levels"].shape[0]),  # count only; levels is the smallest per-row array
        )
    return MultiIndex(
        regions=list(z["regions"]), row_region=z["row_region"], keys=list(z["keys"]),
        titles=list(z["titles"]), levels=[int(x) for x in z["levels"]],
        matrix=z["matrix"], model=str(z["model"]),
        row_kind=(z["row_kind"] if "row_kind" in z.files else None),
    )
