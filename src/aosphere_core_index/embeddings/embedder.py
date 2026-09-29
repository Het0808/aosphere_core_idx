"""Embedder interface + a local FastEmbed implementation.

The interface is deliberately tiny so a Bedrock Titan v2 implementation can be
dropped in later with no caller changes. FastEmbed runs a small ONNX model
in-process (no torch, no external API), so it is free and stays in-account.
"""

from __future__ import annotations

import os
import threading
from typing import Protocol

from ..config import settings

import numpy as np


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return L2-normalized embeddings as a (len(texts), dim) float32 array."""
        ...


def _normalize(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype("float32")


class FastEmbedEmbedder:
    """Local ONNX embeddings via fastembed (default BAAI/bge-small-en-v1.5)."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", dim: int = 384) -> None:
        self.name = model_name
        self.dim = dim
        self._model = None

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding

            cache_dir = os.getenv("ACI_FASTEMBED_CACHE")  # baked into the image
            self._model = TextEmbedding(self.name, cache_dir=cache_dir)
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        vecs = np.asarray(list(self._ensure().embed(list(texts))), dtype="float32")
        return _normalize(vecs)


class TitanEmbedder:
    """Bedrock Titan Text Embeddings v2 (amazon.titan-embed-text-v2:0), 1024-dim.

    Stays in-account (eu-west-2). Titan embeds one text per request, so we fan out
    over a thread pool with a per-thread boto3 client (clients aren't meant to be
    shared mid-call). Used for the cross-region index: far better cross-region
    recall than bge-small (0.59 vs 0.33 @ top-80). Needs Bedrock access at build
    AND query time — keep bge for offline/local via ACI_EMBED_BACKEND=bge.
    """

    def __init__(self, model_name: str = "amazon.titan-embed-text-v2:0", dim: int = 1024,
                 region: str | None = None, max_workers: int | None = None) -> None:
        self.name = model_name
        self.dim = dim
        self._region = (region or os.getenv("ACI_BEDROCK_REGION")
                        or os.getenv("AWS_REGION_NAME") or settings.bedrock_region)
        self._max_workers = max_workers or int(os.getenv("ACI_TITAN_WORKERS", "12"))
        self._tl = threading.local()

    def _client(self):
        c = getattr(self._tl, "client", None)
        if c is None:
            import boto3
            from botocore.config import Config

            # Adaptive retries absorb throttling; we add an explicit retry for the
            # transient ModelErrorException Bedrock occasionally returns. Bounded
            # timeouts are essential: without them a stalled socket hangs the caller
            # forever (0% CPU, no progress) instead of timing out and retrying — this
            # stalled a batch eval indefinitely and would hang a live search request too.
            cfg = Config(region_name=self._region, retries={"max_attempts": 8, "mode": "adaptive"},
                         connect_timeout=5, read_timeout=20)
            # The source bucket and Bedrock can live in different accounts; let the
            # build use the default (source) creds for S3 and a dedicated profile
            # for Bedrock only. Unset -> default chain (single-account / runtime).
            prof = os.getenv("ACI_BEDROCK_PROFILE")
            session = boto3.Session(profile_name=prof) if prof else boto3.Session()
            c = session.client("bedrock-runtime", config=cfg)
            self._tl.client = c
        return c

    def _embed_one(self, text: str) -> list[float]:
        import json
        import time

        body = json.dumps({"inputText": (text or " ")[:8000], "dimensions": self.dim,
                           "normalize": True})
        last = None
        for attempt in range(6):
            try:
                resp = self._client().invoke_model(modelId=self.name, body=body)
                return json.loads(resp["body"].read())["embedding"]
            except Exception as e:  # transient ModelErrorException / throttling
                last = e
                if "ModelError" not in type(e).__name__ and "Throttl" not in type(e).__name__ \
                        and "ServiceUnavailable" not in str(e):
                    raise
                time.sleep(min(2 ** attempt * 0.5, 8))
        raise last

    def embed(self, texts: list[str]) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        # Serve-time queries embed 1-2 texts: run on the CALLING thread so the persistent
        # (uvicorn worker) thread's cached boto3 client is reused across requests, instead of
        # spinning a throwaway pool + fresh thread that re-inits its client on every query.
        # Build-time batches (many clauses) still parallelize over the pool.
        if len(texts) <= 2:
            vecs = [self._embed_one(t) for t in texts]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
                vecs = list(ex.map(self._embed_one, texts))
        return _normalize(np.asarray(vecs, dtype="float32"))


def make_embedder(model: str | None = None) -> Embedder:
    """The embedder for queries. If `model` is given (e.g. a published index's model
    name) we match it EXACTLY so the query encoder can't mismatch the index — Titan
    for a titan model id, else FastEmbed for that model. With no hint, config decides
    (ACI_EMBED_BACKEND=titan -> Titan; default 'bge' stays local/offline)."""
    if model:
        return TitanEmbedder(model) if "titan" in model.lower() else FastEmbedEmbedder(model)
    from aosphere_core_index.config import settings

    if settings.embed_backend.lower() == "titan":
        return TitanEmbedder(settings.titan_model, settings.titan_dim)
    return FastEmbedEmbedder()


def cosine_topk(query: np.ndarray, matrix: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Top-k (index, score) of a single normalized query against a normalized matrix."""
    if matrix.shape[0] == 0:
        return []
    sims = matrix @ query
    idx = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in idx]
