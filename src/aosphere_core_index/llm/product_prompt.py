"""Which master prompt answers this question (AOSNG-3442).

The prompt shipped in agent.py is the DEFAULT. A product may carry an override, authored by a
subject-matter expert through the admin UI. This module answers one question — given the
products a request is scoped to, whose prompt applies — and nothing else: no storage, no I/O,
no HTTP. That keeps the rule a pure function, which matters because it decides what the
assistant asserts about regulated content and therefore has to be testable exhaustively.

The rule (decided in AOSNG-3442):

    exactly ONE product in scope  ->  that product's override FOR THIS MODE, if it has one
    zero, or two or more          ->  the default

A product carries one prompt per answer mode ("summary" and "explain" — the UI's Explain
toggle). The two resolve independently, so customising one leaves the other exactly as it
was.

Falling back on multi-product scope is not timidity, it is the only honest option. The product
filter is multi-select and a question can legitimately span products; picking one product's
prompt for a cross-product question would silently change the answer, and the user could not
tell which prompt had been applied. The consequence is that the DEFAULT prompt is the one that
must be generic enough for any question — see the note in agent.py.

Storage is deliberately not here. A `PromptStore` is anything with `get(product) -> str | None`,
so resolution can be tested against a dict and wired to S3 later without touching this rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PromptStore(Protocol):
    """Somewhere per-product overrides live. Returns None when there is no override."""

    def get(self, product: str, mode: str) -> str | None: ...


@dataclass(frozen=True)
class Resolved:
    """The prompt for a request, and WHY it was chosen.

    `source` exists so an answer can be attributed. Prompts are stored independently of the
    index version (AOSNG-3442 decision 4), so a prompt tuned against last month's content stays
    in force after a reindex — meaning a degraded answer has two candidate causes and the trace
    has to say which prompt was in play. `prompt` is None for "use the built-in default", so
    this module never has to import or copy the default's text.
    """

    prompt: str | None
    source: str
    product: str | None = None
    mode: str | None = None
    # Whether this prompt IS the whole system prompt for its mode (True, the normal case) or
    # is the product half with the built-in answer-format block appended (False — a prompt
    # saved before prompts were per mode, which must keep behaving as it did).
    owns_format: bool = True

    @property
    def is_default(self) -> bool:
        return self.prompt is None


def resolve(products: list[str] | set[str] | None, store: PromptStore | None,
            mode: str = "summary") -> Resolved:
    """Pick the master prompt for a request scoped to `products`, in this answer `mode`.

    `products` is the request's product scope: None or empty means unscoped (the whole
    entitlement), which is a multi-product question by definition and so takes the default.

    `mode` is "summary" or "explain" — the two answers the UI's Explain toggle chooses
    between. A product carries one prompt per mode, resolved independently: an SME may
    customise the explaining answer and leave the summary on the default, and the mode that
    was NOT customised must keep behaving exactly as it did before.
    """
    names = sorted({p for p in (products or ()) if p})
    if len(names) != 1:
        why = ("no product in scope" if not names
               else f"{len(names)} products in scope: {', '.join(names[:3])}"
                    + (" …" if len(names) > 3 else ""))
        return Resolved(None, f"default ({why})", None, mode)
    product = names[0]
    if store is None:
        return Resolved(None, "default (no prompt store configured)", product, mode)
    try:
        override = store.get(product, mode)
    except Exception as e:  # noqa: BLE001
        # A store that cannot be read must not take AI Mode down, and must not silently
        # masquerade as "this product has no override" either — the source string is what
        # reaches the trace, so the reason travels with the answer.
        #
        # The MESSAGE, not just the class. The store raises one exception type for every
        # failure, so a bare class name reads "PromptStoreUnavailable" and tells a reader
        # nothing; the message is where AccessDenied, NoSuchBucket or ExpiredToken lives, and
        # those are three different fixes. Truncated, because this string is shown to a user.
        why = str(e).strip().replace("\n", " ")[:120] or type(e).__name__
        return Resolved(None, f"default (prompt store unavailable: {type(e).__name__}: "
                              f"{why})", product, mode)
    owns_format = True
    if isinstance(override, tuple):            # (text, owns_format) from the store adapter
        override, owns_format = override
    if not override or not override.strip():
        return Resolved(None, f"default (no {mode} override for this product)", product, mode)
    why = f"{mode} override for {product}" + ("" if owns_format else " (pre-mode prompt)")
    return Resolved(override, why, product, mode, owns_format)
