"""Central log configuration, driven by ACI_LOG_LEVEL (default INFO).

Set ACI_LOG_LEVEL=DEBUG (in dev1 deploy env, or locally) to trace requests, the
search pipeline, and the agent/LiteLLM→Bedrock calls. Applied wherever the app is
started (uvicorn CMD, `aci serve`, tests) — call configure_logging() once at import.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False
# Loggers we want to follow ACI_LOG_LEVEL (uvicorn + our package + the agent stack).
_TRACKED = (
    "aosphere_core_index", "uvicorn", "uvicorn.error", "uvicorn.access",
    "openai.agents", "LiteLLM", "litellm",
)


def configure_logging() -> str:
    """Configure root + tracked loggers to ACI_LOG_LEVEL. Idempotent; returns the level."""
    global _CONFIGURED
    level = os.getenv("ACI_LOG_LEVEL", "INFO").strip().upper()
    # getLevelName returns an int for a valid level name (portable to Python <3.11,
    # unlike getLevelNamesMapping).
    if not isinstance(logging.getLevelName(level), int):
        level = "INFO"
    # force=True so we install our handler even when uvicorn already configured
    # logging (otherwise basicConfig is a no-op and the level never changes).
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=not _CONFIGURED,
    )
    _CONFIGURED = True
    logging.getLogger().setLevel(level)
    for name in _TRACKED:
        logging.getLogger(name).setLevel(level)
    if level == "DEBUG":
        # LiteLLM reads this env to emit full request/response traces to Bedrock.
        os.environ.setdefault("LITELLM_LOG", "DEBUG")
    logging.getLogger("aosphere_core_index").info("log level = %s", level)
    return level
