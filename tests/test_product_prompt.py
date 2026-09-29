"""Which master prompt answers a question, and the rules that decide it (AOSNG-3442).

This is the structural half of the feature: prompt assembly is product-aware and the product
scope reaches the agent. Storage and the admin UI come later, so with no store configured every
request still resolves to the default — and the first test here is the one that proves this
restructure changed nothing observable.

Two properties are worth more than the rest:

  * With no override, the assembled prompt is BYTE-IDENTICAL to the previous single-string
    version. The prompt decides what the assistant asserts about regulated content, so a
    refactor of it has to be provably inert.

  * A multi-product question takes the DEFAULT. The product filter is multi-select and a
    question can legitimately span products; using one product's prompt for a cross-product
    question would change the answer with no way for the user to tell which prompt applied.
"""

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aosphere_core_index.llm import agent as A  # noqa: E402
from aosphere_core_index.llm.product_prompt import Resolved, resolve  # noqa: E402


class _Store:
    """A prompt store standing in for S3, so the RULE is tested without any I/O."""

    def __init__(self, mapping=None, raises=None):
        """`mapping` is keyed by product, or by (product, mode) to differ between modes."""
        self.mapping, self.raises, self.asked = mapping or {}, raises, []

    def get(self, product, mode="summary"):
        self.asked.append((product, mode))
        if self.raises:
            raise self.raises
        if (product, mode) in self.mapping:
            return self.mapping[(product, mode)]
        return self.mapping.get(product)


# --------------------------------------------------------------------- inertness

@pytest.mark.parametrize("explain", [False, True])
def test_assembly_with_no_override_is_byte_identical_to_before(explain):
    """The whole restructure must be observably a no-op until an override exists."""
    previously = A.INSTRUCTIONS + (A._ANSWER_EXPLAIN if explain else A._ANSWER_BRIEF)
    assert A.instructions_for(explain) == previously
    assert A.instructions_for(explain, None) == previously


def test_the_shared_rules_block_is_empty_and_that_is_deliberate():
    """STANDARD_RULES is the seam decided in AOSNG-3442, deliberately unpopulated.

    The grounding and citation invariants stay at the TOP of INSTRUCTIONS: agent.py records a
    measurement showing an instruction at the END of a ~1,500-word prompt did not hold, which
    is why the answer contract is also restated in the user turn. Populating this block with
    the invariants would move the one thing that must not fail into the weakest position.
    """
    assert A.STANDARD_RULES == ""
    head = A.INSTRUCTIONS[:400]
    assert "never outside knowledge" in head, \
        "the grounding rule must stay near the START of the prompt"


def test_an_override_owns_the_answer_format_for_its_mode():
    """Prompts are stored PER MODE, and that is the whole reason: the built-in blocks say
    "about 60 words", "no headings", "at most THREE sentences". Appending one to a summary
    prompt that asks for a labelled structure would contradict it — and the built-in block,
    being later in the prompt, is the one the model would follow. So an override replaces the
    format block as well as the product half."""
    out = A.instructions_for(False, "CUSTOM SUMMARY PROMPT")
    assert out.startswith("CUSTOM SUMMARY PROMPT")
    assert "ANSWER FORMAT — BRIEF" not in out, "the built-in format must not fight the override"
    assert A.INSTRUCTIONS not in out, "the default must not be concatenated with the override"


def test_the_user_turn_note_stops_restating_the_built_in_contract_too():
    """The same trap one layer out. _STYLE_NOTE_BRIEF is injected into the USER turn — the
    strongest position — and says "no headings". Left in place it would defeat every custom
    format. Replaced by a pointer with no shape of its own, so the position is kept without
    the contradiction."""
    custom = A._style_note(False, custom=True)
    assert "no headings" not in custom and "60 words" not in custom
    assert "your instructions" in custom
    # and with no override, the built-in contract is unchanged
    assert A._style_note(False) == A._STYLE_NOTE_BRIEF
    assert A._style_note(True) == A._STYLE_NOTE_EXPLAIN


# --------------------------------------------------------------------- the rule

def test_exactly_one_product_uses_its_override():
    store = _Store({"Data Privacy": "DP PROMPT"})
    r = resolve(["Data Privacy"], store)
    assert r.prompt == "DP PROMPT" and r.product == "Data Privacy"
    assert not r.is_default and "summary override for Data Privacy" == r.source


def test_the_two_modes_resolve_independently():
    """An SME may customise the explaining answer and leave the summary alone. The mode that
    was NOT customised has to keep behaving exactly as it did before."""
    store = _Store({("Data Privacy", "explain"): "DP EXPLAIN"})
    assert resolve(["Data Privacy"], store, "explain").prompt == "DP EXPLAIN"
    assert resolve(["Data Privacy"], store, "summary").is_default


def test_each_mode_asks_the_store_for_its_own_prompt():
    """A store consulted without the mode would serve one prompt for both answers."""
    store = _Store({"Data Privacy": "P"})
    resolve(["Data Privacy"], store, "explain")
    assert store.asked == [("Data Privacy", "explain")]


def test_the_mode_travels_on_the_resolution_so_a_trace_can_name_it():
    r = resolve(["Data Privacy"], _Store(), "explain")
    assert r.mode == "explain" and "explain" in r.source


@pytest.mark.parametrize("products", [
    None, [], ["Data Privacy", "G20"], ["Data Privacy", "G20", "Shareholding Disclosure"],
])
def test_anything_other_than_exactly_one_product_uses_the_default(products):
    """The decision that matters most: a cross-product question must not silently inherit one
    product's prompt."""
    store = _Store({"Data Privacy": "DP PROMPT", "G20": "G20 PROMPT"})
    r = resolve(products, store)
    assert r.is_default, f"{products} should fall back to the default"
    assert "default" in r.source
    assert store.asked == [], "the store must not even be consulted for a multi-product question"


def test_the_reason_for_the_default_is_stated_not_just_the_fact():
    """`source` reaches the answer trace. Prompts are stored independently of the index
    version, so a degraded answer has two candidate causes and the trace must say which."""
    assert "no product in scope" in resolve(None, _Store()).source
    assert "2 products in scope" in resolve(["A", "B"], _Store()).source
    assert "no summary override" in resolve(["A"], _Store()).source
    assert "no explain override" in resolve(["A"], _Store(), "explain").source
    assert "no prompt store" in resolve(["A"], None).source


def test_duplicate_and_blank_product_names_collapse():
    """The scope arrives from a CSV query parameter, so it can carry repeats and empties."""
    store = _Store({"Data Privacy": "DP"})
    assert resolve(["Data Privacy", "Data Privacy", ""], store).prompt == "DP"
    assert resolve(["", None], store).is_default


def test_a_whitespace_only_override_is_treated_as_absent():
    """An SME clearing the box in the UI must restore the default, not send an empty prompt."""
    assert resolve(["A"], _Store({"A": "   \n  "})).is_default


def test_a_broken_store_falls_back_and_says_so_rather_than_taking_ai_mode_down():
    """A prompt store that cannot be read must not fail the request, and must not masquerade
    as 'this product has no override' either — the reason travels in `source`."""
    r = resolve(["Data Privacy"], _Store(raises=RuntimeError("AccessDenied")))
    assert r.is_default
    assert "unavailable" in r.source and "RuntimeError" in r.source


# --------------------------------------------------------------------- plumbing

def test_both_runners_accept_the_product_scope_as_keyword_only():
    """Keyword-only on purpose: scripts/eval_benchmark.py:254 passes the jurisdiction list
    positionally, and a positional `products` there would be silently misread as jurisdictions.
    """
    for fn in (A.run_agent_async, A.run_agent_stream):
        p = inspect.signature(fn).parameters
        assert "products" in p, f"{fn.__name__} must take the product scope"
        assert p["products"].kind is inspect.Parameter.KEYWORD_ONLY, \
            f"{fn.__name__}: products must be keyword-only"
        assert p["products"].default is None


def test_the_api_forwards_the_product_scope_separately_from_the_region_scope(monkeypatch):
    """_scope_ids flattens (products x jurisdictions) into qualified region ids, so the product
    names — and how MANY there were — cannot be recovered downstream. The scope has to travel
    on its own, and this is what proves it does end to end."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aosphere_core_index.service import app as APP

    seen = {}

    async def capture(q, allowed, model=None, session_id=None, explain=False, products=None,
                      context=None):
        seen["products"] = products
        yield {"kind": "answer", "answer": "ok", "sources": []}

    monkeypatch.setattr(A, "run_agent_stream", capture, raising=False)
    monkeypatch.setattr(APP, "_scope_ids", lambda p, j, u: None)
    APP.app.dependency_overrides[APP.require_access] = lambda: {"anonymous": True}
    try:
        c = TestClient(APP.app)
        c.post("/api/agent/stream", json={"q": "q", "products": "Data Privacy"})
        assert seen["products"] == ["Data Privacy"]
        c.post("/api/agent/stream", json={"q": "q", "products": "Data Privacy,G20"})
        assert seen["products"] == ["Data Privacy", "G20"]
        c.post("/api/agent/stream", json={"q": "q"})
        assert seen["products"] is None, "no filter must arrive as None, not an empty list"
    finally:
        APP.app.dependency_overrides.clear()


def test_resolution_never_raises_whatever_it_is_given():
    """It sits in the request path for every AI Mode question."""
    for products in (None, [], ["A"], ["A", "B"], [""], [None], ["A", None, ""]):
        for store in (None, _Store(), _Store(raises=ValueError("boom"))):
            r = resolve(products, store)
            assert isinstance(r, Resolved)


def test_agent_resolves_through_the_store_and_still_defaults_with_no_backend():
    """A store IS wired now, and with the NullBackend installed — the default until a backend
    is configured — every request still takes the built-in prompt. That is the property that
    matters: the feature is inert, not half-working, in an environment with nowhere to store
    overrides."""
    from aosphere_core_index.llm import prompt_store as PS

    assert A._prompt_store() is not None, "the agent must resolve through the store"
    assert isinstance(PS.backend(), PS.NullBackend), "no backend is configured by default"
    assert A._resolve_prompt(["Data Privacy"]).is_default
    assert A._resolve_prompt(None).is_default


def test_an_installed_backend_actually_changes_the_prompt(monkeypatch):
    """End to end through the agent's own helper: a stored override must reach assembly, or
    the whole feature is decorative."""
    from aosphere_core_index.llm import prompt_store as PS

    class _B(PS.NullBackend):
        def get(self, product, mode="summary"):
            if product != "Data Privacy":
                return None
            return PS.PromptRecord(product, f"DP {mode.upper()} OVERRIDE", mode=mode)

    monkeypatch.setattr(PS, "_backend", _B())
    r = A._resolve_prompt(["Data Privacy"])
    assert r.prompt == "DP SUMMARY OVERRIDE" and not r.is_default
    assert A.instructions_for(False, r.prompt).startswith("DP SUMMARY OVERRIDE")
    # the Explain toggle must reach the store, or both answers use one prompt
    assert A._resolve_prompt(["Data Privacy"], True).prompt == "DP EXPLAIN OVERRIDE"
    assert A.mode_of(True) == "explain" and A.mode_of(False) == "summary"
    # a second product in scope still falls back, even with an override present
    assert A._resolve_prompt(["Data Privacy", "G20"]).is_default
