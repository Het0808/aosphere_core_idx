"""MLflow monitoring for AI Mode.

Traces every LLM/agent call (prompt, model, tool calls, latency, tokens) to the
configured MLflow server under a dedicated experiment. Best-effort: if MLflow is
unset or unreachable, the service runs normally without tracing.

Env:
  MLFLOW_TRACKING_URI   e.g. https://mlflow.dev1.aoslogin.net
  MLFLOW_EXPERIMENT     default "aosphere-core-index"

The image ships `mlflow-skinny`, the CLIENT, because the server is deployed separately
(see requirements.txt for why: the full package carried 19 of the image's Trivy findings).
An http/https URI therefore resolves to MLflow's RestStore, which is all this module
needs. A LOCAL tracking URI ('sqlite:///...', './mlruns') is not supported by the skinny
client — setup_mlflow() will simply return False and the service runs untraced, rather
than failing.
"""

from __future__ import annotations

import os

_READY = False


def setup_mlflow() -> bool:
    """Configure MLflow tracking + LiteLLM autolog. Returns True if enabled."""
    global _READY
    if _READY:
        return True
    uri = os.getenv("MLFLOW_TRACKING_URI")
    if not uri:
        return False
    try:
        import mlflow

        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment(os.getenv("MLFLOW_EXPERIMENT", "aosphere-core-index"))
        # Trace each LiteLLM call (the Agents SDK routes through LiteLLM).
        mlflow.litellm.autolog()
        _READY = True
        return True
    except Exception:  # unreachable / version mismatch — never block the service
        return False
