"""A file-backed prompt store, for local and dev use (AOSNG-3442).

The S3 backend is the production target. This exists because without SOME writable backend the
feature cannot be exercised at all: reads work against the NullBackend, but every save returns
503, so the override path — the whole point of the ticket — is untestable end to end.

Enabled by setting `ACI_PROMPT_STORE_DIR`. Unset, nothing changes: the service keeps the
NullBackend and keeps refusing writes with a reason. That default matters. A dev machine that
silently started persisting prompts to a container-local directory would be worse than one that
refuses, because the prompts would look saved and would vanish with the container.

One JSON file per product, named by a hash of the product name rather than the name itself.
Product names carry ampersands, parentheses, commas and accents, and macOS is
case-insensitive while Linux is not — a hash sidesteps every one of those without needing a
slugging scheme whose collisions would silently merge two products' prompts.

Writes are atomic (temp file, then replace) because the alternative is a half-written prompt
being read as a system prompt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from aosphere_core_index.llm.prompt_store import (
    MODES,
    PromptRecord,
    PromptStoreUnavailable,
    valid_mode,
    valid_product,
)

log = logging.getLogger(__name__)

_SUFFIX = ".prompt.json"


def _LEGACY_NAME(product: str) -> str:      # noqa: N802 — the pre-mode filename
    return hashlib.sha256(product.encode("utf-8")).hexdigest()[:32] + _SUFFIX


def _name(product: str, mode: str = "summary") -> str:
    """One file per (product, mode), so the two modes are written and deleted separately."""
    return hashlib.sha256(product.encode("utf-8")).hexdigest()[:32] + f"-{mode}" + _SUFFIX


class LocalFileBackend:
    """Overrides stored as JSON files under a directory."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    # -- reads -----------------------------------------------------------------------------
    def get(self, product: str, mode: str = "summary") -> PromptRecord | None:
        if not valid_product(product) or not valid_mode(mode):
            return None
        p = self.dir / _name(product, mode)
        legacy = False
        if not p.exists():
            # A prompt saved before prompts were per mode served BOTH answers; keep reading it.
            p, legacy = self.dir / (_LEGACY_NAME(product)), True
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # A corrupt or unreadable file must not be reported as "this product has no
            # override": that would silently answer with the default while an SME believed
            # their prompt was live. Refusing is the honest failure.
            log.warning("prompt store: could not read %s for %r (%s) — answering with the "
                        "DEFAULT prompt", p, product, mode)
            raise PromptStoreUnavailable(
                f"the stored prompt for {product!r} could not be read") from None
        prompt = d.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        return PromptRecord(product, prompt, d.get("updated_by"), d.get("updated_at"), mode,
                            legacy=legacy)

    def overrides(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        try:
            names = sorted(self.dir.glob("*" + _SUFFIX))
        except OSError:
            return out
        for p in names:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue          # a listing must not fail because one file is broken
            product, prompt = d.get("product"), d.get("prompt")
            if not (isinstance(product, str) and isinstance(prompt, str) and prompt.strip()):
                continue
            m = d.get("mode")
            if m in MODES:
                out.setdefault(product, set()).add(m)
            elif m is None:
                out.setdefault(product, set()).update(MODES)   # pre-mode: answers both
        return out

    # -- writes ----------------------------------------------------------------------------
    def put(self, product: str, prompt: str, by: str | None,
            mode: str = "summary") -> PromptRecord:
        if not valid_product(product):
            raise PromptStoreUnavailable(f"{product!r} is not a storable product name")
        if not valid_mode(mode):
            raise PromptStoreUnavailable(f"{mode!r} is not an answer mode")
        if not prompt.strip():
            # The API turns an empty prompt into a DELETE before reaching here. Refuse anyway:
            # an empty system prompt would drop the grounding and citation rules entirely.
            raise PromptStoreUnavailable("an empty prompt cannot be stored")
        rec = PromptRecord(product, prompt, by, time.time(), mode)
        body = json.dumps({"product": product, "mode": mode, "prompt": prompt,
                           "updated_by": by, "updated_at": rec.updated_at},
                          ensure_ascii=False, indent=2)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # Same directory as the target, so the replace is atomic rather than a cross-device
            # copy that can be observed half-written.
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(body)
                os.replace(tmp, self.dir / _name(product, mode))
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as e:
            raise PromptStoreUnavailable(f"could not write the prompt store: {e}") from e
        return rec

    def delete(self, product: str, mode: str = "summary") -> None:
        if not valid_product(product):
            raise PromptStoreUnavailable(f"{product!r} is not a storable product name")
        if not valid_mode(mode):
            raise PromptStoreUnavailable(f"{mode!r} is not an answer mode")
        try:
            (self.dir / _name(product, mode)).unlink(missing_ok=True)
        except OSError as e:
            raise PromptStoreUnavailable(f"could not remove the prompt: {e}") from e


def from_env() -> LocalFileBackend | None:
    """The backend `ACI_PROMPT_STORE_DIR` asks for, or None when it is unset."""
    d = (os.environ.get("ACI_PROMPT_STORE_DIR") or "").strip()
    if not d:
        return None
    log.info("prompt store: local directory %s", d)
    return LocalFileBackend(d)
