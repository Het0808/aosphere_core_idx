"""Per-product master prompts stored in S3 (AOSNG-3442).

NO CONFIGURATION IS NEEDED. The default is S3, at `prompts/` in the environment's OWN
private bucket — the one the Doc Gallery already reads (`ACI_DOC_GALLERY_BUCKET`, or
`ACI_EXTRACTION_BUCKET`). So dev writes to the dev bucket and prod to the prod bucket, with
nothing to set and nothing to get wrong.

`settings.source_bucket` is deliberately NOT used, tempting as it looks. It is the read-only
INGEST bucket, it defaults to a **production** bucket, and nothing in the deployment sets it —
so defaulting to it would have had dev1 writing its prompts into prod. The gallery bucket is
the one that actually tracks the environment.

`ACI_PROMPT_STORE_S3` overrides the location entirely, as a full S3 URI:

    ACI_PROMPT_STORE_S3=s3://some-other-bucket/some-prefix/

If no bucket can be determined at all — a laptop with no gallery configured — there is no
guess to make, and the service falls back to a local directory or to refusing writes.

**The prefix is deliberately OUTSIDE `index/<version>/`.** A prompt is authored against a
product, not against a build of the corpus, and AOSNG-3442 decided prompts survive a reindex.
Storing them under an index version would either strand them at cutover or force a copy step
that could silently drop one. `prompts/` at the bucket root has neither problem.

One object per product, keyed `<prefix><slug>-<hash8>.json`. The slug is there so the bucket
is readable in the console; the hash is what makes the key unique, because product names carry
ampersands, parentheses, commas and accents, and a slug alone would let two products collide
onto one key and silently share a prompt.

IAM the service role needs (the pattern `deploy/extraction-job.yaml` uses — scoped to the
prefix, never the bucket):

    s3:GetObject, s3:PutObject, s3:DeleteObject   on arn:aws:s3:::BUCKET/prompts/*
    s3:ListBucket                                 on arn:aws:s3:::BUCKET
                                                  Condition: s3:prefix = prompts/*

ListBucket is bucket-scoped because that is the only level S3 grants it at; the condition is
what keeps it to this prefix. Without ListBucket the Prompts screen cannot say which products
carry an override — it would show every product as being on the default.

CACHING. `resolve()` runs on EVERY AI Mode question, so an uncached backend would put an S3
GET on the request path of every answer — latency, cost, and a hard dependency on S3 for a
feature that is supposed to degrade to the default. Reads are cached for `_TTL` seconds, and a
write through this process invalidates its own entry immediately. The consequence, which is
worth knowing: a prompt saved on one pod can take up to `_TTL` to take effect on another.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

from aosphere_core_index.llm.prompt_store import (
    MODES,
    PromptRecord,
    PromptStoreUnavailable,
    valid_mode,
    valid_product,
)

log = logging.getLogger(__name__)

_TTL = float(os.getenv("ACI_PROMPT_STORE_TTL", "60"))
_SUFFIX = ".json"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _aws_code(e: Exception) -> str:
    """The AWS error code, which is the one fact worth having.

    `AccessDenied` vs `NoSuchBucket` vs `ExpiredToken` are three different operator actions,
    and the exception's class name — `ClientError` for all three — distinguishes none of them.
    """
    return (getattr(e, "response", {}) or {}).get("Error", {}).get("Code") or type(e).__name__


def _slug(product: str) -> str:
    return _SLUG_RE.sub("-", product.lower()).strip("-")[:48] or "product"


def legacy_key_for(prefix: str, product: str) -> str:
    """The key used before prompts were stored per mode.

    A colleague's prompt is live on dev under this key. Changing the layout without reading it
    would orphan it and the service would silently answer with the default — the exact failure
    this store is built to avoid. Read-only compatibility: never written, only found.
    """
    h = hashlib.sha256(product.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{_slug(product)}-{h}{_SUFFIX}"


def key_for(prefix: str, product: str, mode: str = "summary") -> str:
    """One object per (product, mode). The mode is in the key, not inside the object, so the
    two modes are written, read and deleted independently — customising the explaining answer
    cannot touch the summary one."""
    h = hashlib.sha256(product.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{_slug(product)}-{h}-{mode}{_SUFFIX}"


def parse_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/prompts/` -> ('bucket', 'prompts/'). The prefix always ends in '/'."""
    u = urlparse(uri.strip())
    if u.scheme != "s3" or not u.netloc:
        raise ValueError(f"expected an s3:// URI, got {uri!r}")
    prefix = u.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return u.netloc, prefix


class S3Backend:
    """Overrides stored as one JSON object per product."""

    def __init__(self, bucket: str, prefix: str, region: str | None = None,
                 client=None) -> None:
        self.bucket, self.prefix = bucket, prefix
        self._client = client
        self._region = region
        self._cache: dict[tuple[str, str], tuple[float, PromptRecord | None]] = {}
        self._listing: tuple[float, dict[str, set[str]]] | None = None
        # When the store is broken, resolve() is still called once per AI Mode question, so an
        # unthrottled warning would produce one line per question for as long as the outage
        # lasts. First failure logs immediately; repeats are throttled per operation.
        self._logged: dict[str, float] = {}

    def _warn(self, op: str, msg: str, *args) -> None:
        now = time.monotonic()
        if self._logged.get(op, 0.0) > now:
            return
        self._logged[op] = now + _TTL
        log.warning(msg, *args)

    @property
    def s3(self):
        if self._client is None:
            import boto3

            from aosphere_core_index.config import settings
            self._client = boto3.client("s3", region_name=self._region or settings.aws_region)
        return self._client

    # -- reads -----------------------------------------------------------------------------
    def get(self, product: str, mode: str = "summary") -> PromptRecord | None:
        if not valid_product(product) or not valid_mode(mode):
            return None
        ck = (product, mode)
        hit = self._cache.get(ck)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        rec = self._fetch(product, mode)
        self._cache[ck] = (time.monotonic() + _TTL, rec)
        return rec

    def _fetch(self, product: str, mode: str,
               key: str | None = None, legacy: bool = False) -> PromptRecord | None:
        key = key or key_for(self.prefix, product, mode)
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:  # noqa: BLE001 — botocore raises a generated class
            if getattr(e, "response", {}).get("Error", {}).get("Code") in (
                    "NoSuchKey", "404", "NotFound"):
                if legacy:
                    return None
                # No per-mode prompt. Fall back to a prompt saved before modes existed, which
                # served BOTH answers; it is returned marked legacy so assembly keeps
                # appending the built-in format block exactly as it did then. Saving either
                # mode from the screen writes a per-mode key and takes over from here.
                return self._fetch(product, mode,
                                   legacy_key_for(self.prefix, product), legacy=True)
            # AccessDenied, a network failure, a wrong bucket: these must NOT be reported as
            # "this product has no override". resolve() catches this and falls back to the
            # default with the reason in `source`, so an answer is still produced and the
            # trace says why it was not the SME's prompt.
            #
            # That fallback is otherwise SILENT server-side: the question is answered, nothing
            # 500s, and an operator watching logs would see a healthy service quietly ignoring
            # every prompt an SME had written. This is the line that says so.
            code = _aws_code(e)
            self._warn("get", "prompt store: could not read s3://%s/%s (%s) — answering with "
                       "the DEFAULT %s prompt for %r. Check s3:GetObject on %s*",
                       self.bucket, key, code, mode, product, self.prefix)
            raise PromptStoreUnavailable(f"could not read {key}: {code}") from e
        try:
            d = json.loads(body)
        except ValueError as e:
            self._warn("json", "prompt store: s3://%s/%s is not valid JSON — answering with "
                       "the DEFAULT %s prompt for %r", self.bucket, key, mode, product)
            raise PromptStoreUnavailable(f"the stored prompt at {key} is not valid JSON") from e
        prompt = d.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        return PromptRecord(product, prompt, d.get("updated_by"), d.get("updated_at"), mode,
                            legacy=legacy)

    def overrides(self) -> dict[str, set[str]]:
        """Which products carry an override — one LIST, cached.

        The names come from inside the objects, not from the keys: a key carries a slug and a
        hash, and reversing a slug back to "Marketing Restrictions - Asset Management" is not
        possible. This is the only place that reads every object, and it is called once per
        load of the Prompts screen, never on the answer path.
        """
        if self._listing and self._listing[0] > time.monotonic():
            return {k: set(v) for k, v in self._listing[1].items()}
        out: dict[str, set[str]] = {}
        try:
            p = self.s3.get_paginator("list_objects_v2")
            for page in p.paginate(Bucket=self.bucket, Prefix=self.prefix):
                for obj in page.get("Contents", []):
                    if not obj["Key"].endswith(_SUFFIX):
                        continue
                    try:
                        d = json.loads(
                            self.s3.get_object(Bucket=self.bucket,
                                               Key=obj["Key"])["Body"].read())
                    except Exception as e:  # noqa: BLE001
                        # One unreadable object must not hide the others, but it does mean a
                        # product silently shows as "default" on the Prompts screen.
                        self._warn(f"obj:{obj['Key']}",
                                   "prompt store: skipping unreadable object s3://%s/%s (%s); "
                                   "its product will show as being on the default",
                                   self.bucket, obj["Key"], _aws_code(e))
                        continue
                    name, prompt = d.get("product"), d.get("prompt")
                    if not (isinstance(name, str) and isinstance(prompt, str)
                            and prompt.strip()):
                        continue
                    m = d.get("mode")
                    if m in MODES:
                        out.setdefault(name, set()).add(m)
                    elif m is None:
                        # A pre-mode prompt answers BOTH modes, so the screen must show it
                        # against both — otherwise it reads as "on the default" while it is
                        # very much in force.
                        out.setdefault(name, set()).update(MODES)
        except Exception as e:  # noqa: BLE001
            code = _aws_code(e)
            self._warn("list", "prompt store: could not list s3://%s/%s (%s) — the Prompts "
                       "screen will show every product as being on the default. Check "
                       "s3:ListBucket with a prefix condition on %s",
                       self.bucket, self.prefix, code, self.prefix + "*")
            raise PromptStoreUnavailable(f"could not list the prompt store: {code}") from e
        self._listing = (time.monotonic() + _TTL, {k: set(v) for k, v in out.items()})
        return out

    # -- writes ----------------------------------------------------------------------------
    def put(self, product: str, prompt: str, by: str | None,
            mode: str = "summary") -> PromptRecord:
        if not valid_product(product):
            raise PromptStoreUnavailable(f"{product!r} is not a storable product name")
        if not valid_mode(mode):
            raise PromptStoreUnavailable(f"{mode!r} is not an answer mode")
        if not prompt.strip():
            # The API turns an empty box into a DELETE before reaching here. Refuse anyway: an
            # empty system prompt would drop the grounding and citation rules entirely.
            raise PromptStoreUnavailable("an empty prompt cannot be stored")
        rec = PromptRecord(product, prompt, by, time.time(), mode)
        body = json.dumps({"product": product, "mode": mode, "prompt": prompt,
                           "updated_by": by, "updated_at": rec.updated_at},
                          ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key_for(self.prefix, product, mode),
                               Body=body, ContentType="application/json")
        except Exception as e:  # noqa: BLE001
            # NOT throttled: a save is a deliberate human action that just failed in front of
            # someone, and there is one log line per attempt, not per question.
            code = _aws_code(e)
            log.error("prompt store: SAVE FAILED for %r (%s) by %s — s3://%s/%s (%s). Check "
                      "s3:PutObject on %s*", product, mode, by or "unknown", self.bucket,
                      key_for(self.prefix, product, mode), code, self.prefix)
            raise PromptStoreUnavailable(f"could not save the prompt: {code}") from e
        self._invalidate(product, mode, rec)
        return rec

    def delete(self, product: str, mode: str = "summary") -> None:
        if not valid_product(product):
            raise PromptStoreUnavailable(f"{product!r} is not a storable product name")
        if not valid_mode(mode):
            raise PromptStoreUnavailable(f"{mode!r} is not an answer mode")
        try:
            self.s3.delete_object(Bucket=self.bucket,
                                  Key=key_for(self.prefix, product, mode))
        except Exception as e:  # noqa: BLE001
            code = _aws_code(e)
            log.error("prompt store: DELETE FAILED for %r (%s) — s3://%s/%s (%s). The "
                      "override is still in force. Check s3:DeleteObject on %s*", product,
                      mode, self.bucket, key_for(self.prefix, product, mode), code,
                      self.prefix)
            raise PromptStoreUnavailable(f"could not remove the prompt: {code}") from e
        self._invalidate(product, mode, None)

    def _invalidate(self, product: str, mode: str, rec: PromptRecord | None) -> None:
        """A save must be visible to THIS process at once.

        Without this an SME would save a prompt, ask a question on the same pod, and get the
        old one for up to a minute — which reads exactly like the feature not working.
        """
        self._cache[(product, mode)] = (time.monotonic() + _TTL, rec)
        self._listing = None


DEFAULT_PREFIX = "prompts/"


def from_env() -> S3Backend | None:
    """The backend `ACI_PROMPT_STORE_S3` asks for, or None when it is unset."""
    uri = (os.environ.get("ACI_PROMPT_STORE_S3") or "").strip()
    if not uri:
        return None
    bucket, prefix = parse_uri(uri)   # a malformed URI raises here, at startup, not mid-request
    log.info("prompt store: S3 s3://%s/%s (configured, read cache %.0fs)", bucket, prefix, _TTL)
    return S3Backend(bucket, prefix)


def default() -> S3Backend | None:
    """The zero-config store: `prompts/` in this environment's own private bucket.

    The env vars are read here rather than through `doc_gallery.extraction_target()` because
    `llm` importing `service` would invert the dependency (service imports llm). The coupling
    is deliberate and one-way: prompts live beside the gallery, in the same bucket, so an
    environment configured for one is configured for both.
    """
    bucket = (os.getenv("ACI_DOC_GALLERY_BUCKET", "").strip()
              or os.getenv("ACI_EXTRACTION_BUCKET", "").strip())
    if not bucket:
        return None
    region = (os.getenv("ACI_DOC_GALLERY_REGION", "").strip()
              or os.getenv("ACI_EXTRACTION_REGION", "").strip() or None)
    log.info("prompt store: S3 s3://%s/%s (default, read cache %.0fs)",
             bucket, DEFAULT_PREFIX, _TTL)
    return S3Backend(bucket, DEFAULT_PREFIX, region=region)
