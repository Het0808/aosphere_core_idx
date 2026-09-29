"""Configuration for the GraphIndex pipeline.

Reads from environment variables (and a local .env if present). Holds only
non-secret settings plus the names of source locations; AWS credentials come
from the standard boto3 credential chain (env vars / profile).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The source bucket is read-only. The pipeline must never write to it.
SOURCE_BUCKET = "aosphere-aosphere-tenant-prod-advanced-search-store"
PRODUCT_PREFIX = "Advanced_Search/155_Data_Privacy"
WORKING_PREFIX = f"{PRODUCT_PREFIX}/working"

# Expected source AWS account, asserted at startup as a guard.
EXPECTED_ACCOUNT = "053277883003"


class Settings(BaseSettings):
    """Runtime settings, overridable via env vars (prefix ACI_)."""

    model_config = SettingsConfigDict(env_prefix="ACI_", env_file=".env", extra="ignore")

    aws_region: str = "eu-west-2"
    # WHERE EVERY AI (BEDROCK) CALL GOES. One setting, read by stage 4, the AI-Mode
    # agent, the LLM reranker, query expansion and the Titan embedder, so the region
    # is answerable in one place instead of from a literal in each caller -- the
    # earlier per-caller defaults had drifted to eu-west-1 while aws_region said
    # eu-west-2, and a stage 4 run went to a region nothing in config named.
    # Override with ACI_BEDROCK_REGION. Kept separate from aws_region (S3/STS) on
    # purpose: Bedrock model availability is per-region and does not have to follow
    # where the buckets live.
    bedrock_region: str = "eu-west-2"
    source_bucket: str = SOURCE_BUCKET
    working_prefix: str = WORKING_PREFIX
    expected_account: str = EXPECTED_ACCOUNT

    # Local working directory for downloaded source + built artifacts (git-ignored).
    data_dir: Path = Path("data")

    # Embedding backend for the index + query encoder.
    #   "bge"   -> local FastEmbed bge-small (offline, no Bedrock; default for local/dev)
    #   "titan" -> Bedrock Titan Text Embeddings v2 (better cross-region recall; needs
    #              Bedrock access at build AND query time). The index artifact and this
    #              setting must match — deployment ships the Titan index with this=titan.
    embed_backend: str = "bge"
    titan_model: str = "amazon.titan-embed-text-v2:0"
    titan_dim: int = 1024

    # Document parsing backend: "legacy" (Word-style docx extractor) or "mineru"
    # (layout/OCR parser via the local mineru CLI — see extract/mineru_extract.py).
    # Legacy stays the default; mineru is opt-in per build via ACI_EXTRACT_BACKEND.
    extract_backend: str = "legacy"
    # MinerU parse backend for the mineru extractor:
    #   hybrid-engine = VLM on the hard parts + pipeline models elsewhere (production
    #                   default; needs both local model sets in mineru_model_cache).
    #   vlm-engine    = full MinerU2.5-Pro VLM (highest fidelity, slowest).
    #   pipeline      = layout+OCR models only, CPU, weaker.
    # mineru_effort applies ONLY to hybrid-* backends:
    #   medium = faster, image/chart analysis OFF (production default — text-heavy docs).
    #   high   = image/chart analysis ON (slower).
    mineru_backend: str = "hybrid-engine"     # pipeline | vlm-engine | hybrid-engine
    mineru_effort: str = "medium"             # medium | high  (hybrid-engine only)
    mineru_model_cache: Path = Path("data/.mineru_models")  # locally-downloaded models (offline)
    mineru_timeout_s: int = 2400

    # ---- STAGE 4: AI TABLE POST-PROCESSING ------------------------------------
    # Off by default, and deliberately a hard gate rather than a preference: stage 4 is
    # the only stage that calls a paid API and the only one that can alter a document's
    # text, so a deployment has to opt in rather than inherit it. Set
    # ACI_STAGE4_AI_ENABLED=1 to allow it on a server.
    #
    # This gates the pass ITSELF, not just the CLI flag, so a programmatic caller cannot
    # spend money on a host where it was never enabled. Local and experimental runs pass
    # force=True to run_stage4 explicitly.
    stage4_ai_enabled: bool = False
    # WHICH model, when it is enabled: "sonnet5" | "sonnet" (4.6) | "haiku".
    #
    # A corpus run MULTIPLIES this. At the Sonnet rate a 74-page MRAM document is about
    # $1.07 against $0.34 on haiku, so 150 documents is roughly $160 against $50. That is
    # a deliberate choice, not an oversight -- haiku broke the column structure this pass
    # exists to repair -- but it is the number to check before enabling the flag on a
    # corpus rather than a document.
    #
    # Named explicitly rather than left to a default argument. run_stage4's `models` tuple
    # starts with haiku, and every ad-hoc re-run silently inherited that while the results
    # were being compared against sonnet.
    # A short alias -- "haiku" | "sonnet" (4.6) | "sonnet5" -- or ANY Bedrock model id,
    # passed through untouched. That is what makes a newly released model selectable by
    # setting this variable rather than by editing ai_postprocess.MODEL_ALIASES.
    #
    # A typo is therefore indistinguishable from a new model id and fails at Bedrock rather
    # than here. That is the intended failure: loud, and before any work. Silently
    # correcting it to a default would bill a run on a model nobody chose.
    stage4_ai_model: str = "sonnet4.6"
    # WHICH model for the TEXT-section pass specifically -- a plain-prose section, not a
    # table, so it does not need stage4_ai_model's structural judgement and can run
    # cheaper. Haiku by default. Independent of stage4_ai_model on purpose: a host can run
    # Sonnet on tables and Haiku on text in the same stage-4 pass without one setting
    # overriding the other.
    stage4_text_ai_model: str = "haiku"
    # USD per 1M tokens for the model above. Set BOTH or neither. Needed for a model this
    # build has no price for, and useful for correcting one it has wrong -- SONNET5's rate
    # is carried over from Sonnet 4.6 and unconfirmed, because the AWS pricing API is denied
    # to the invoke-only role. When set, this WINS over the built-in table.
    stage4_ai_price_in: float | None = None
    stage4_ai_price_out: float | None = None
    # How many sections to send to Bedrock concurrently in section mode. Sections are
    # independent files with independent model calls, so this is wall-clock only -- it
    # does not change what gets billed. Kept small: each call already carries several
    # page images, and Bedrock's own per-account concurrency limit is the real ceiling.
    stage4_ai_workers: int = 6

    @property
    def products_dir(self) -> Path:
        """Root of the product-nested index: data/products/<product>/<jurisdiction>/,
        plus the shared data/products/_multi/ flat index."""
        return self.data_dir / "products"

    def region_root(self, name: str) -> Path:
        """Per-region folder holding both source files and built artifacts.

        Layout is product-nested: data/regions/<product>/<jurisdiction>/. The region
        identity is product-qualified ("France" for Data Privacy; "Shareholding
        Disclosure — France" otherwise); we split it to the nested path so callers
        and the flat multi-index keep using a single name."""
        from aosphere_core_index.regions.region_map import split_region

        product, jurisdiction = split_region(name)
        return self.products_dir / product / jurisdiction

    def region_source(self, name: str) -> Path:
        """Where a region's downloaded source files are cached."""
        return self.region_root(name) / "source"

    def region_artifacts(self, name: str) -> Path:
        """Where a region's built outputs (graph, markdown, html) are written."""
        return self.region_root(name) / "artifacts"


settings = Settings()
