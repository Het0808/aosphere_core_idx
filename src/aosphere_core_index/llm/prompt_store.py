"""Where per-product master prompt overrides live (AOSNG-3442).

Storage is the next step; this is the interface it will satisfy, plus the NULL backend the
service runs with until then. Splitting it this way keeps two things honest:

  * READS work today. With no backend, every product reports "no override" and the API can
    serve the effective prompt, so the admin screen is usable and truthful immediately.
  * WRITES fail LOUDLY rather than appearing to succeed. A save that silently vanished would
    be the worst outcome for this feature: a subject-matter expert would believe a prompt was
    in force when the default was still answering. `PromptStoreUnavailable` is what the API
    turns into a 503 with a reason.

The backend deliberately knows nothing about the default prompt. `agent.INSTRUCTIONS` is the
default and stays in code; a store holds only what someone has written to override it, so
"no override" is representable as absence rather than as a copy that can drift.

Records carry `updated_by` and `updated_at` because the prompt decides what the assistant
asserts about regulated content — "who changed this and when" is not optional metadata here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class PromptStoreUnavailable(RuntimeError):
    """Storage exists in principle but cannot serve this operation right now."""


# A product name reaches this module from a URL path segment. It is used as a storage key, so
# it is validated rather than trusted: the corpus contains names with spaces, ampersands,
# parentheses, commas and accents ("160_Bank_Confidentiality_&_Outsourcing", "Marketing
# Restrictions - Asset Management"), and those are all legitimate. What is not legitimate is a
# path separator or traversal, which is why this is an allowlist of shapes rather than a
# blocklist of characters.
_PRODUCT_RE = re.compile(r"^[^/\\\x00-\x1f]{1,120}$")


def valid_product(name: str) -> bool:
    return bool(name) and ".." not in name and bool(_PRODUCT_RE.match(name))


# The two answers the UI's Explain toggle chooses between. A product carries one prompt per
# mode, because the two answers have different shapes: a summary is a verdict and a citation,
# an explanation develops it. One prompt cannot serve both without the format instruction for
# one contradicting the other.
MODES = ("summary", "explain")


def valid_mode(mode: str) -> bool:
    return mode in MODES


@dataclass(frozen=True)
class PromptRecord:
    """One product's override for ONE mode, with the audit fields the ticket requires."""

    product: str
    prompt: str
    updated_by: str | None = None
    updated_at: float | None = None
    mode: str = "summary"
    # True for a prompt saved BEFORE prompts were stored per mode. Such a prompt was the
    # product half only, with the built-in answer-format block appended, and it served both
    # answers. It must keep behaving exactly that way — see the legacy note in the S3 backend.
    legacy: bool = False


class PromptBackend(Protocol):
    """What a storage implementation must provide."""

    def get(self, product: str, mode: str = "summary") -> PromptRecord | None: ...
    def put(self, product: str, prompt: str, by: str | None,
            mode: str = "summary") -> PromptRecord: ...
    def delete(self, product: str, mode: str = "summary") -> None: ...
    def overrides(self) -> dict[str, set[str]]: ...


class NullBackend:
    """No storage configured. Reads are empty; writes refuse and say why.

    This is what runs until the S3 backend lands, and it is also the correct behaviour for any
    environment that has not been given somewhere to write — better a clear 503 than an editor
    that appears to work.
    """

    reason = ("no prompt store is configured in this environment, so overrides cannot be "
              "saved yet")

    def get(self, product: str, mode: str = "summary") -> PromptRecord | None:
        return None

    def put(self, product: str, prompt: str, by: str | None,
            mode: str = "summary") -> PromptRecord:
        raise PromptStoreUnavailable(self.reason)

    def delete(self, product: str, mode: str = "summary") -> None:
        raise PromptStoreUnavailable(self.reason)

    def overrides(self) -> dict[str, set[str]]:
        return {}


_backend: PromptBackend | None = None


def backend() -> PromptBackend:
    """The configured backend, resolved once.

    The default is S3, and needs no configuration: `prompts/` in the environment's own
    private bucket, the one the Doc Gallery already reads. Explicit settings win over it,
    most specific first:

        1. ACI_PROMPT_STORE_S3   an explicit s3:// URI — another bucket or prefix
        2. ACI_PROMPT_STORE_DIR  an explicit local directory — a laptop, or a test
        3. (default)             S3 at prompts/ in this environment's bucket
        4. NullBackend           no bucket is configured at all, so there is nothing to
                                 guess: reads are empty and writes refuse with a reason

    Explicit beats implicit, which is why a configured directory outranks the S3 default: a
    developer who asked for a local directory should not silently be writing to a bucket.
    """
    global _backend
    if _backend is None:
        from aosphere_core_index.llm import prompt_store_local, prompt_store_s3
        _backend = (prompt_store_s3.from_env()
                    or prompt_store_local.from_env()
                    or prompt_store_s3.default()
                    or NullBackend())
    return _backend


def set_backend(b: PromptBackend) -> None:
    """Install a storage backend. Called from configuration, and from tests."""
    global _backend
    _backend = b


class ResolverAdapter:
    """Adapts a backend to the `PromptStore` shape `product_prompt.resolve` expects.

    resolve() wants `get(product, mode) -> str | None` and nothing else — it must stay a pure
    rule with no knowledge of records, audit fields or storage. This is the one-line bridge.
    """

    def __init__(self, b: PromptBackend | None = None) -> None:
        self._b = b or backend()

    def get(self, product: str, mode: str = "summary"):
        """Returns the prompt text, or a (text, owns_format) pair for a legacy prompt.

        resolve() stays a pure rule and knows nothing about storage, so the one thing it does
        need — whether this prompt replaces the answer-format block or has it appended — rides
        along rather than being looked up.
        """
        rec = self._b.get(product, mode)
        if not rec:
            return None
        return (rec.prompt, not rec.legacy)
