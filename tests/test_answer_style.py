"""Answers are brief by default; `explain` asks for the reasoning.

A closed question ("can the investor net the 3% short against the 5.3% long?") was answered
correctly and then explained for another twenty lines: a heading, the rule, a block quote,
"Applied to Your Scenario", "Consequence", and an "In short" that repeated the first line.
The prompt's only style guidance was "Be concise and precise", which lost to the model's
default urge to structure an essay.

Worse, the answer opened by narrating its own retrieval — "Based on the information already
retrieved in this session… No further search or read is needed" — which is scaffolding the
reader should never see, in either mode.

So answer shape is now a per-request choice with brevity as the default, and the
process-narration ban belongs to BOTH modes.
"""

import pytest

pytest.importorskip("agents")

from aosphere_core_index.llm import agent as A  # noqa: E402


def test_brief_is_the_default():
    assert A.instructions_for() == A.instructions_for(explain=False)
    assert "ANSWER FORMAT — BRIEF" in A.instructions_for()
    assert "ANSWER FORMAT — EXPLAIN" not in A.instructions_for()


def test_explain_swaps_the_format_and_nothing_else():
    """Only the answer-shape block changes: sourcing, method and tone are not negotiable."""
    brief, explain = A.instructions_for(False), A.instructions_for(True)
    assert "ANSWER FORMAT — EXPLAIN" in explain
    assert "ANSWER FORMAT — BRIEF" not in explain
    assert brief.split("ANSWER FORMAT")[0] == explain.split("ANSWER FORMAT")[0]
    for rule in ("Answer ONLY", "Cite every statement as [Jurisdiction · ClauseKey]",
                 "NEVER narrate your own process", "TONE: keep it professional"):
        assert rule in brief and rule in explain


def test_the_brief_format_bans_the_padding_the_argentina_answer_had():
    """Named, because a general "be concise" did not work: the model reliably produced
    these exact sections."""
    brief = A.instructions_for()
    for banned in ("In short", "Consequence", "Applied to your scenario"):
        assert banned in brief, f"the brief format must name {banned!r} as padding to omit"
    assert "no closing summary" in brief


def test_brevity_never_licenses_dropping_substance():
    """The failure mode of a word budget is a wrong answer that is short. Completeness
    outranks it — a list question still gets the whole list."""
    brief = A.instructions_for()
    assert "COMPLETE list" in brief
    assert "completeness outranks the word budget" in brief.lower()
    assert "keep every figure, deadline and condition" in brief


def test_process_narration_is_banned_in_both_modes():
    """The reported artifact, verbatim from the answer that prompted this."""
    for explain in (False, True):
        text = A.instructions_for(explain)
        assert "based on the information already retrieved" in text.lower()
        assert "no further search is needed" in text.lower()
        assert "as read above" in text.lower()


def test_both_runners_accept_explain():
    """Threading, not behaviour: the flag must reach the agent construction on both the
    streaming and non-streaming paths."""
    import inspect
    for fn in (A.run_agent_async, A.run_agent_stream):
        params = inspect.signature(fn).parameters
        assert "explain" in params, f"{fn.__name__} must take explain"
        assert params["explain"].default is False, f"{fn.__name__} must default to brief"
    src = inspect.getsource(A)
    assert "instructions=INSTRUCTIONS" not in src, \
        "a runner still pins the base prompt, so its answer style cannot be switched"
    # Both runners must resolve the per-product prompt and pass it through (AOSNG-3442);
    # neither may fall back to assembling the prompt itself.
    assert src.count("instructions=instructions_for(explain, resolved.prompt,\n"
                     "                                                   resolved.owns_format)"
                     ) == 2, "a runner drops owns_format, so a pre-mode prompt would lose "\
                             "its answer-format block"
    assert src.count("resolved = _resolve_prompt(products, explain)") == 2, \
        "a runner is not resolving the product prompt, so a product override would be ignored"
    # The MODE has to reach resolution, or one stored prompt would serve both answers.
    assert "_resolve_prompt(products)" not in src, \
        "a runner resolves without the mode, so summary and explain would share one prompt"
    # And the user-turn note must know an override is in force, or it restates the built-in
    # contract on top of a custom format and the custom format loses.
    assert src.count("_style_note(\n        explain, custom=not resolved.is_default "
                     "and resolved.owns_format)") == 2


def test_the_api_defaults_to_brief_and_forwards_the_flag(monkeypatch):
    """End to end through the endpoint: absent -> brief, explain:true -> explain."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aosphere_core_index.service import app as APP

    seen = []

    async def capture(q, allowed, model=None, session_id=None, explain=False, products=None,
                      context=None):
        seen.append(explain)
        yield {"kind": "answer", "answer": "ok", "sources": []}

    monkeypatch.setattr(A, "run_agent_stream", capture, raising=False)
    monkeypatch.setattr(APP, "_scope_ids", lambda p, j, u: None)
    APP.app.dependency_overrides[APP.require_access] = lambda: {"anonymous": True}
    try:
        c = TestClient(APP.app)
        c.post("/api/agent/stream", json={"q": "can the investor net the positions?"})
        c.post("/api/agent/stream", json={"q": "same question", "explain": True})
        c.get("/api/agent/stream", params={"q": "same question", "explain": "true"})
    finally:
        APP.app.dependency_overrides.clear()

    assert seen == [False, True, True]


def test_the_ui_offers_the_switch_and_sends_it():
    from aosphere_core_index.service.web import PAGE

    assert 'id="explain"' in PAGE, "there must be a control for it"
    assert 'explain:document.getElementById("explain").checked' in PAGE, \
        "the toggle must reach the request body"


def test_the_switch_sits_in_the_composer_next_to_send():
    """It first went in the header beside the model dropdown, where it could not be found.
    A control for how the ANSWER comes back belongs where the question is typed."""
    from aosphere_core_index.service.web import PAGE

    composer = PAGE.split('id="composer"')[1].split("</div>")[0]
    assert 'id="explain"' in composer, "the switch must be in the composer row"
    assert composer.index('id="explainwrap"') < composer.index('id="chatsend"'), \
        "it should read as Ask … | Explain | Send"


# ---- sources on a follow-up ---------------------------------------------------
# ctx.sources only records what THIS turn read. A follow-up answers from clauses read in an
# earlier turn — that is what session history is for — so answers came back full of
# [Jurisdiction · Key] citations with an empty "Sources the agent read" strip: a citation the
# reader could see but not click. The citations are the authoritative list.

def IDENT(jurisdiction, key):
    """Resolver for the parsing tests: pretend every cited key is in the index."""
    return key


def test_a_cited_clause_becomes_a_source_even_if_this_turn_read_nothing():
    answer = ("**No** — netting is not permitted [Argentina · A3.3(c)].\n"
              "- Writing the call may itself trigger disclosure [Argentina · A6.2 §6.2.2].")
    out = A._with_cited_sources(answer, [], resolve=IDENT)
    assert [(s["jurisdiction"], s["key"]) for s in out] == [
        ("Argentina", "A3.3(c)"), ("Argentina", "A6.2")]


def test_a_clause_actually_read_keeps_its_metadata_and_is_not_duplicated():
    """The read log is richer (title, region) — harvesting must not overwrite or repeat it."""
    read = [{"jurisdiction": "Argentina", "key": "A3.3(c)", "title": "Netting",
             "region": "Americas"}]
    out = A._with_cited_sources("cited [Argentina · A3.3(c)] once", read, resolve=IDENT)
    assert out == read


def test_a_flag_emoji_prefix_does_not_create_a_second_jurisdiction():
    """The tone rule lets a flag prefix a jurisdiction name, and it leaks into citations."""
    out = A._with_cited_sources("[🇦🇷 Argentina · A6.2] and [Argentina · A6.2]",
                                [], resolve=IDENT)
    assert len(out) == 1 and out[0]["jurisdiction"] == "Argentina"


def test_prose_containing_a_middot_is_not_mistaken_for_a_citation():
    """Only bracketed [X · Y] is a citation; a stray interpunct in prose is not."""
    assert A._with_cited_sources("the ratio is 5.3% · 3% in practice", [], resolve=IDENT) == []
    assert A._with_cited_sources("see [a very long piece of prose that is not a citation "
                                 "at all because it exceeds the bound · k]",
                                 [], resolve=IDENT) == []


def test_both_runners_harvest_citations_into_sources():
    import inspect
    src = inspect.getsource(A)
    assert src.count("_with_cited_sources(result.final_output") == 2, \
        "both the streaming and non-streaming answers must carry cited sources"


def test_one_bracket_holding_several_citations_yields_several_sources():
    """Observed live: "[Argentina · A3.3(c)(ii); Argentina · A6.2 §6.2.2(a)]" became ONE
    source whose key was "A3.3(c)(ii); Argentina · A6.2 §6.2.2(a)" — unclickable nonsense."""
    out = A._with_cited_sources(
        "cannot be netted [Argentina · A3.3(c)(ii); Argentina · A6.2 §6.2.2(a)].",
        [], resolve=IDENT)
    assert [(s["jurisdiction"], s["key"]) for s in out] == [
        ("Argentina", "A3.3(c)(ii)"), ("Argentina", "A6.2")]


def test_a_repeated_jurisdiction_may_be_omitted_in_the_same_bracket():
    out = A._with_cited_sources("see [Argentina · A3.3; A6.2]", [], resolve=IDENT)
    assert [(s["jurisdiction"], s["key"]) for s in out] == [
        ("Argentina", "A3.3"), ("Argentina", "A6.2")]


def test_a_sub_reference_is_trimmed_to_the_real_clause_key():
    """'A6.2 §6.2.2(a)' is not a key: the chip's lookup fails and it duplicates 'A6.2'."""
    read = [{"jurisdiction": "Shareholding Disclosure — Argentina", "key": "A6.2",
             "title": "Options"}]
    assert A._with_cited_sources("[Argentina · A6.2 §6.2.2(a)]", read, resolve=IDENT) == read


def test_the_qualified_region_id_and_the_bare_name_are_the_same_source():
    """The read log records 'Shareholding Disclosure — Argentina'; the model cites
    'Argentina'. Live, that listed A3.3 twice."""
    read = [{"jurisdiction": "Shareholding Disclosure — Argentina", "key": "A3.3",
             "title": "Netting", "region": "Americas"}]
    assert A._with_cited_sources("netting is not permitted [Argentina · A3.3]", read, resolve=IDENT) == read


# ---- a chip must open something ----------------------------------------------
# The model cites the numbering it reads INSIDE a clause — "A3.3(c)(ii)" — but the index
# holds "A3.3". Verified against the running service: A3.3 -> 200, A3.3(c) -> 404,
# A3.3(c)(ii) -> 404. Harvested verbatim, those chips were dead links.

def test_a_sub_letter_citation_resolves_to_the_clause_the_index_has():
    index = {"A3.3": {}, "A6.2": {}}
    out = A._with_cited_sources("[Argentina · A3.3(c)(ii)] and [Argentina · A6.2]", [],
                                resolve=lambda j, k: A._existing_key_in(index, k))
    assert [s["key"] for s in out] == ["A3.3", "A6.2"]


def test_a_citation_the_index_cannot_place_is_dropped_not_shown_dead():
    """A hallucinated key must not become a chip that 404s."""
    out = A._with_cited_sources("[Argentina · Z9.9(b)]", [],
                                resolve=lambda j, k: A._existing_key_in({"A1": {}}, k))
    assert out == []


def test_resolution_walks_up_to_a_real_parent_only_via_bracket_groups():
    """Only "(x)" groups are stripped. "A3.4" must NOT be answered with "A3" — that would
    turn a wrong key into a confident link to the wrong clause."""
    index = {"A3": {}, "A3.3": {}}
    assert A._existing_key_in(index, "A3.3(c)(ii)") == "A3.3"
    assert A._existing_key_in(index, "A3.4") is None
    assert A._existing_key_in(index, "ALERT:xyz") == "ALERT:xyz"


def test_an_unreachable_index_keeps_the_citation_rather_than_hiding_it(monkeypatch):
    """If the bundle can't be loaded, a visible (if imprecise) chip beats none."""
    def boom(_region):
        raise RuntimeError("no index mounted")
    monkeypatch.setattr("aosphere_core_index.service.registry.get_bundle", boom)
    assert A._existing_key("Argentina", "A3.3(c)") == "A3.3(c)"
