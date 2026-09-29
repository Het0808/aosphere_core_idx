"""No Converse call sends `temperature`, and stage 4 fails silently when one does.

Sonnet 5 rejects the parameter: Converse returns "ValidationException: The model returned
the following errors: `temperature` is deprecated for this model" — for EVERY section, so
the pass keeps stage 3 throughout and reports success. A cluster run on 2026-09-08 finished
`gate=pass worst=93.4` in 178s having processed 0 of 8 sections for $0.00, because the
extraction gate measures stages 1-3 and nothing downstream reads stage 4's rejection count.
Nothing about that run looked wrong except one line in the middle of the log.

It became reachable when stage4_ai_model started defaulting to sonnet5: the default model
and the three hardcoded inferenceConfig literals could not both be right.

Dropped for every model rather than suppressed for Sonnet 5 alone — one config that every
model accepts cannot drift out of step with the model list. The trade is deliberate:
omitting temperature is NOT sending 0, so each model applies its own non-deterministic
default and a repair may come back differently on a retry. The character-exact reprojection
gate is what makes that safe — a re-roll that changes content is rejected, not kept — so the
exposure is extra rejections and tokens, never altered text.
"""
import inspect

import pytest

pytest.importorskip("numpy")

from aosphere_core_index.extract import ai_postprocess as ap  # noqa: E402
from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    HAIKU, SONNET, SONNET5, inference_config)


@pytest.mark.parametrize("model", [HAIKU, SONNET, SONNET5])
def test_no_model_is_sent_temperature(model):
    assert "temperature" not in inference_config(model, 8192)


@pytest.mark.parametrize("model", [HAIKU, SONNET, SONNET5])
def test_max_tokens_is_carried(model):
    assert inference_config(model, 1234) == {"maxTokens": 1234}


def test_an_unknown_model_is_treated_the_same():
    assert inference_config("some.new.model-v9", 512) == {"maxTokens": 512}


def test_no_call_site_builds_inferenceConfig_inline():
    """The bug was three hardcoded literals. If a fourth call site appears with its own
    dict, this catches it here rather than at a Sonnet 5 ValidationException in a GPU run."""
    src = inspect.getsource(ap)
    assert "inferenceConfig={" not in src, \
        "a converse() call is building inferenceConfig inline instead of via inference_config()"
    assert src.count("inferenceConfig=inference_config(") == 3


def test_temperature_appears_nowhere_in_a_request():
    """Belt and braces: the word may survive in commentary explaining the removal, but not
    in any dict this module hands to boto3."""
    src = inspect.getsource(ap)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert '"temperature"' not in code and "'temperature'" not in code
