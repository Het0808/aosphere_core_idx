"""A reasoning model's answer is not the first content block.

Sonnet 5 returns its thinking alongside the answer, so Converse replies with
`[reasoningContent, text]` and `content[0]["text"]` raises KeyError. It only thinks when the
task warrants it, so this fails by PROMPT rather than by model: a one-line probe returns a
single text block and looks fine, while the real stage-4 prompt does not. That is how it
survived the temperature fix — every section failed with `KeyError: 'text'`, each was caught
per-section and reported "kept as stage 3", and the run finished gate=pass having done
nothing at all.

Measured on the real code path, one section of a Marketing Restrictions document:

    model       before            after
    haiku       1/1  $0.0174      1/1  $0.0145
    sonnet4.6   1/1  $0.0413      1/1  $0.0415
    sonnet5     0/1  KeyError     1/1  $0.0868   (15,738 tok — reasoning tokens are billed)
"""
import pytest

pytest.importorskip("numpy")

from aosphere_core_index.extract import ai_postprocess as ap  # noqa: E402
from aosphere_core_index.extract.ai_postprocess import response_text  # noqa: E402


def resp(*blocks, stop="end_turn"):
    return {"output": {"message": {"content": list(blocks)}}, "stopReason": stop}


def test_a_plain_text_response():
    assert response_text(resp({"text": "hello"})) == "hello"


def test_the_text_is_found_AFTER_a_reasoning_block():
    """The actual Sonnet 5 shape — this is the whole bug."""
    r = resp({"reasoningContent": {"reasoningText": {"text": "thinking…", "signature": "x"}}},
             {"text": "<table>…</table>"})
    assert response_text(r) == "<table>…</table>"


def test_reasoning_text_is_never_mistaken_for_the_answer():
    """reasoningContent nests its own 'text' key. Returning the model's thinking as the
    repaired table would put the reasoning INTO the document."""
    r = resp({"reasoningContent": {"reasoningText": {"text": "I should rewrite the table"}}},
             {"text": "THE ANSWER"})
    assert response_text(r) == "THE ANSWER"


def test_several_leading_blocks_are_skipped():
    r = resp({"reasoningContent": {}}, {"toolUse": {}}, {"text": "answer"})
    assert response_text(r) == "answer"


def test_an_empty_text_block_is_still_the_answer():
    assert response_text(resp({"reasoningContent": {}}, {"text": ""})) == ""


def test_no_text_block_raises_with_something_to_debug_from():
    with pytest.raises(RuntimeError) as e:
        response_text(resp({"reasoningContent": {}}, stop="max_tokens"))
    msg = str(e.value)
    assert "reasoningContent" in msg, "the block shape must be in the message"
    assert "max_tokens" in msg, "stopReason explains a truncated response"


@pytest.mark.parametrize("bad", [{}, {"output": {}}, {"output": {"message": {}}},
                                 {"output": {"message": {"content": []}}}])
def test_a_malformed_response_raises_rather_than_KeyError(bad):
    with pytest.raises(RuntimeError):
        response_text(bad)


def test_no_call_site_indexes_content_zero_any_more():
    """The bug was three copies of the same expression; this catches a fourth."""
    import inspect
    src = inspect.getsource(ap)
    assert '["content"][0]' not in src, \
        "a Converse response is being read positionally instead of via response_text()"
    assert src.count("response_text(resp)") == 3
