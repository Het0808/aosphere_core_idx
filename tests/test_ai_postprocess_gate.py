"""The stage-4 safety gate. These tests ARE the guarantee that AI post-processing
cannot alter a word of the document — if they pass, no model output that mutates
content can reach the tree, whatever the prompt said."""

import re
from pathlib import Path

import pytest

from aosphere_core_index.extract.ai_postprocess import (
    VERDICT_CLEAN,
    VERDICT_REJECT,
    VERDICT_SPACING,
    VERDICT_STRUCTURAL,
    Batch,
    attribute_rows,
    content_stream,
    plan_batches,
    split_rows,
    strict_stream,
    verdict,
)

# ---------------------------------------------------------------- the invariant


def test_stream_ignores_tags_entities_and_whitespace():
    assert content_stream("<td>a &amp; b</td>") == content_stream("<td>\n a&b \n</td>")


def test_stream_is_blind_to_the_repairs_we_want():
    # Every legitimate fix leaves the stream untouched.
    assert content_stream("registrationThe") == content_stream("registration The")
    assert content_stream("t.Marketing") == content_stream("t. Marketing")
    assert content_stream("<td>ab</td>") == content_stream("<td>a<br>b</td>")


def test_stream_is_not_blind_to_content_change():
    assert content_stream("<td>hello</td>") != content_stream("<td>hallo</td>")
    assert content_stream("<td>a b</td>") != content_stream("<td>a b c</td>")


# ---------------------------------------------------------------- verdicts

ROW = "<tr><td>registrationThe fund</td><td>yes</td></tr>"


def test_spacing_fix_accepted():
    v, _ = verdict(ROW, "<tr><td>registration The fund</td><td>yes</td></tr>")
    assert v == VERDICT_SPACING


def test_inserted_line_break_accepted():
    v, _ = verdict("<tr><td>one two</td></tr>", "<tr><td>one<br>two</td></tr>")
    assert v == VERDICT_SPACING


def test_row_resplit_is_structural_but_accepted():
    v, _ = verdict("<tr><td>a b</td></tr>", "<tr><td>a</td></tr><tr><td>b</td></tr>")
    assert v == VERDICT_STRUCTURAL


def test_cell_boundary_shift_is_structural_but_accepted():
    v, _ = verdict("<tr><td>ab</td><td>c</td></tr>", "<tr><td>a</td><td>bc</td></tr>")
    assert v == VERDICT_STRUCTURAL


def test_identical_input_is_clean():
    assert verdict(ROW, ROW)[0] == VERDICT_CLEAN


def test_paraphrase_rejected():
    v, why = verdict("<tr><td>shall not market</td></tr>",
                     "<tr><td>must not market</td></tr>")
    assert v == VERDICT_REJECT
    assert "diverges" in why


def test_dropped_row_rejected():
    v, _ = verdict("<tr><td>a</td></tr><tr><td>b</td></tr>", "<tr><td>a</td></tr>")
    assert v == VERDICT_REJECT


def test_invented_content_rejected():
    v, _ = verdict("<tr><td>a</td></tr>", "<tr><td>a</td><td>N/A</td></tr>")
    assert v == VERDICT_REJECT


def test_single_letter_change_rejected():
    # The gate is character-exact, not fuzzy.
    v, _ = verdict("<tr><td>2019</td></tr>", "<tr><td>2018</td></tr>")
    assert v == VERDICT_REJECT


def test_dropped_footnote_marker_rejected():
    v, _ = verdict("<tr><td>the Act3</td></tr>", "<tr><td>the Act</td></tr>")
    assert v == VERDICT_REJECT


# ---------------------------------------------------------------- batching

def test_split_rows_finds_each_row():
    assert len(split_rows("<table><tr><td>a</td></tr><tr><td>b</td></tr></table>")) == 2


def test_batches_may_span_pages_now_that_images_are_gone():
    # Page boundaries only mattered while each request carried that page's image.
    rows = ["<tr><td>a</td></tr>"] * 6
    pages = [1, 1, 2, 2, 3, 3]
    assert len(plan_batches(rows, pages, "t1", max_rows=10)) == 1


def test_page_grouping_still_available_when_asked_for():
    rows = ["<tr><td>a</td></tr>"] * 6
    pages = [1, 1, 2, 2, 3, 3]
    bs = plan_batches(rows, pages, "t1", max_rows=10, group_by_page=True)
    assert [b.page for b in bs] == [1, 2, 3]


def test_batches_respect_row_cap():
    rows = ["<tr><td>a</td></tr>"] * 7
    assert len(plan_batches(rows, [1] * 7, "t1", max_rows=3)) == 3


def test_batches_respect_char_cap():
    rows = ["<tr><td>" + "x" * 3000 + "</td></tr>"] * 4
    assert len(plan_batches(rows, [1] * 4, "t1", max_rows=99, max_chars=6000)) >= 2


def test_batches_cover_every_row_exactly_once():
    rows = [f"<tr><td>{i}</td></tr>" for i in range(20)]
    pages = [1] * 7 + [2] * 6 + [3] * 7
    out = [r for b in plan_batches(rows, pages, "t1") for r in b.rows]
    assert out == rows


def test_header_carried_as_context_but_not_for_the_first_batch():
    rows = [f"<tr><td>{i}</td></tr>" for i in range(9)]
    bs = plan_batches(rows, [1] * 9, "t1", max_rows=3)
    assert bs[0].header is None          # the header IS the first batch
    assert bs[1].header == rows[0]


# ---------------------------------------------------------------- attribution

# Fragments shorter than 12 characters are never used to place a row: a probe that
# short matches almost any page by chance, so a bad attribution (wrong page image
# shown to the model) is likelier than no attribution. Fixtures are sized accordingly.
PAGE_A = "the principal sources of law applicable to the marketing of funds"
PAGE_B = "no relevant guidance has been issued by the commission at this time"


def test_rows_attributed_to_the_page_containing_their_text():
    streams = {5: content_stream(PAGE_A), 6: content_stream(PAGE_B)}
    rows = [f"<tr><td>{PAGE_A[4:40]}</td></tr>", f"<tr><td>{PAGE_B[3:40]}</td></tr>"]
    assert attribute_rows(rows, [5, 6], streams) == [5, 6]


def test_row_matched_on_its_tail_when_its_opening_words_are_mangled():
    streams = {5: content_stream(PAGE_A), 6: content_stream(PAGE_B)}
    row = "<tr><td>QQQQ zzz garbled opening " + PAGE_B[20:60] + "</td></tr>"
    assert attribute_rows([row], [5, 6], streams) == [6]


def test_unlocatable_row_inherits_the_previous_page():
    streams = {5: content_stream(PAGE_A), 6: content_stream(PAGE_B)}
    rows = [f"<tr><td>{PAGE_A[4:40]}</td></tr>", "<tr><td>%%%</td></tr>"]
    assert attribute_rows(rows, [5, 6], streams) == [5, 5]


# ---------------------------------------------------------------- assembly

from aosphere_core_index.extract.ai_postprocess import (
    _rebuild_table,
    _tables_in,
    repair_batch,
)


def test_rebuild_survives_a_row_being_split_in_two():
    # The repaired row list is NOT 1:1 with the input once a row splits, which is why
    # the table is rebuilt from its wrapper rather than patched row by row.
    orig = "<table><tr><td>a b</td></tr></table>"
    out = _rebuild_table(orig, split_rows(orig),
                         ["<tr><td>a</td></tr><tr><td>b</td></tr>"])
    assert content_stream(out) == content_stream(orig)
    assert len(split_rows(out)) == 2


def test_table_block_paired_with_the_badge_above_it():
    text = ("**⚙ MinerU-extracted table** — table_003, pages 13–14, 6 rows × 2 cols\n\n"
            "<table><tr><td>x</td></tr></table>\n")
    assert _tables_in(text)[0][0] == "table_003"


class _Stub:
    """Stands in for bedrock-runtime; returns whatever the script dictates."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        text = self.replies.pop(0)
        if isinstance(text, Exception):
            raise text
        return {"output": {"message": {"content": [{"text": text}]}},
                "usage": {"inputTokens": 100, "outputTokens": 50}}


def _batch():
    return Batch("table_001", 13, 0, ["<tr><td>registrationThe fund</td></tr>"])


def test_accepted_repair_is_applied_and_billed():
    c = _Stub("<tr><td>registration The fund</td></tr>")
    r = repair_batch(c, _batch(), None, models=("m1",))
    assert r.verdict == VERDICT_SPACING and r.changed
    assert r.usage == [{"model": "m1", "in": 100, "out": 50}]


def test_mutated_output_is_discarded_and_stage3_kept():
    c = _Stub("<tr><td>registration The trust</td></tr>")   # 'fund' -> 'trust'
    b = _batch()
    r = repair_batch(c, b, None, models=("m1",))
    assert r.verdict == VERDICT_REJECT
    assert r.html == b.html          # the original survives untouched


def test_retry_escalates_and_the_reason_is_fed_back():
    c = _Stub("<tr><td>totally different</td></tr>",        # rejected
              "<tr><td>registration The fund</td></tr>")    # accepted
    r = repair_batch(c, _batch(), None, models=("m1", "m2"))
    assert r.verdict == VERDICT_SPACING and r.model == "m2" and r.attempts == 2
    # the second call must carry the rejection reason, or the retry is just a re-roll
    sent = " ".join(p.get("text", "") for p in c.calls[1]["messages"][0]["content"])
    assert "DISCARDED" in sent
    assert len(r.usage) == 2         # the rejected attempt is still billed


def test_bedrock_failure_does_not_lose_the_table():
    b = _batch()
    r = repair_batch(_Stub(RuntimeError("throttled")), b, None, models=("m1",))
    assert r.verdict == VERDICT_REJECT and r.html == b.html


def test_chatter_and_fences_are_stripped():
    c = _Stub("Here you go:\n```html\n<tr><td>registration The fund</td></tr>\n```\nHope that helps!")
    assert repair_batch(c, _batch(), None, models=("m1",)).verdict == VERDICT_SPACING


# ---------------------------------------------------------------- tightened guards
# Each of these is a real behaviour observed from Haiku 4.5 on the Bahamas document
# during the first pilot run. All four passed an alphanumeric-only gate; none may pass
# this one.

def test_smart_quote_normalisation_rejected():
    # (“IFA”) -> ("IFA") leaves the words alone but rewrites a legal definition marker.
    v, why = verdict('<tr><td>(as amended) (“IFA”)</td></tr>',
                     '<tr><td>(as amended) ("IFA")</td></tr>')
    assert v == VERDICT_REJECT
    assert "punctuation/case" in why


def test_invented_emphasis_rejected():
    v, why = verdict("<tr><td>Commission</td></tr>",
                     "<tr><td><strong>Commission</strong></td></tr>")
    assert v == VERDICT_REJECT
    assert "introduced markup" in why


def test_invented_colspan_rejected():
    v, why = verdict("<tr><td>a</td></tr>", '<tr><td colspan="2">a</td></tr>')
    assert v == VERDICT_REJECT
    assert "colspan" in why


def test_invented_bullet_glyph_rejected():
    # The model can SEE bullets on the page image; they are still not in the text.
    v, _ = verdict("<tr><td>one<br>two</td></tr>",
                   "<tr><td>one<br>•\ttwo</td></tr>")
    assert v == VERDICT_REJECT


def test_case_change_rejected():
    v, why = verdict("<tr><td>the commission</td></tr>", "<tr><td>The Commission</td></tr>")
    assert v == VERDICT_REJECT
    assert "punctuation/case" in why


def test_widening_past_the_table_width_is_rejected():
    # Observed: '<td>(d) Are there…' split into '(d)' + 'Are there…' inside a 2-col table.
    # The table width is what bounds this, so it has to be supplied — without it the
    # check cannot know that three cells is one too many.
    v, why = verdict("<tr><td>(d) Are there any changes</td><td>None</td></tr>",
                     "<tr><td>(d)</td><td>Are there any changes</td><td>None</td></tr>",
                     width=2)
    assert v == VERDICT_REJECT
    assert "wider than the table" in why


def test_splitting_a_multi_column_row_is_rejected_as_unverifiable():
    """A known and deliberate limit of the gate.

    Splitting a wrongly-merged row in a MULTI-column table is a legitimate repair, but it
    necessarily reorders the linear reading stream — the cells regroup from row-major
    ("q one q two" then "a one a two") to interleaved ("q one","a one","q two","a two").
    No order-preserving check can tell that apart from the model shuffling content, so
    the change is refused and the stage-3 rows are kept. We would rather leave a merged
    row merged than apply a reordering we cannot verify.
    """
    v, _ = verdict("<tr><td>q one q two</td><td>a one a two</td></tr>",
                   "<tr><td>q one</td><td>a one</td></tr>"
                   "<tr><td>q two</td><td>a two</td></tr>")
    assert v == VERDICT_REJECT


def test_splitting_a_single_column_row_is_allowed():
    # With one column there is no regrouping, so reading order is preserved and the
    # split is verifiable.
    v, _ = verdict("<tr><td>line one line two</td></tr>",
                   "<tr><td>line one</td></tr><tr><td>line two</td></tr>")
    assert v == VERDICT_STRUCTURAL


def test_cell_boundary_shift_within_a_row_is_allowed():
    # The structural repair that actually matters for these tables: content that bled
    # into the wrong cell, moved back across the boundary.
    v, _ = verdict("<tr><td>question textANSWER</td><td>rest</td></tr>",
                   "<tr><td>question text</td><td>ANSWERrest</td></tr>")
    assert v == VERDICT_STRUCTURAL


def test_br_and_cell_tags_may_still_be_introduced():
    v, _ = verdict("<tr><td>one two</td></tr>", "<tr><td>one<br>two</td></tr>")
    assert v == VERDICT_SPACING


# ---------------------------------------------------------------- reprojection

from aosphere_core_index.extract.ai_postprocess import reproject


def test_reprojection_restores_curly_quotes_but_keeps_the_line_break():
    before = '<tr><td>(as amended) (“IFA”), effective 1 September</td></tr>'
    model = '<tr><td>(as amended) ("IFA"),<br>effective 1 September</td></tr>'
    out = reproject(before, model)
    assert "“IFA”" in out and "<br>" in out
    assert verdict(before, out)[0] == VERDICT_SPACING


def test_reprojection_preserves_entity_source_form():
    before = "<tr><td>a &amp; b</td></tr>"
    out = reproject(before, "<tr><td>a & b</td></tr>")
    assert out == before


def test_reprojection_refuses_when_a_word_changed():
    assert reproject("<tr><td>shall not</td></tr>", "<tr><td>must not</td></tr>") is None


def test_reprojection_refuses_when_content_was_added():
    assert reproject("<tr><td>one<br>two</td></tr>",
                     "<tr><td>one<br>• two</td></tr>") is None


def test_reprojection_output_is_content_identical_by_construction():
    before = "<tr><td>Commission&#x27;s siteThe fund</td><td>“yes”</td></tr>"
    model = '<tr><td>Commission\'s site<br>The fund</td><td>"yes"</td></tr>'
    out = reproject(before, model)
    assert strict_stream(out) == strict_stream(before)


# ---------------------------------------------------------------- column repair
# The defect from table_006: MinerU merged the "(g)" label into the question cell, so
# two rows of a 3-column table came back with 2 cells. The first stage-4 run did not fix
# it because the prompt forbade any cell-count change at all.

from aosphere_core_index.extract.ai_postprocess import table_width  # noqa: E402

RAGGED = ("<tr><td>(e)</td><td>Does a Fund have to be domiciled?</td><td>N/A</td></tr>"
          "<tr><td>(f)</td><td>Is any local presence required?</td><td>N/A</td></tr>"
          "<tr><td>(g) Do the Arrangements apply to both?</td><td>N/A</td></tr>")


def test_width_comes_from_the_badge_when_stage2_declared_it():
    assert table_width(RAGGED, "table_006, pages 19–33, 33 rows × 3 cols") == 3


def test_width_falls_back_to_the_widest_row_not_the_commonest():
    # table_006 in the real document: 21 rows have 2 cells, 12 have 3. The mode would say
    # 2 and enshrine the defect, so the maximum is used instead.
    ragged = ("<tr><td>a</td><td>b</td></tr>" * 21) + ("<tr><td>a</td><td>b</td><td>c</td></tr>" * 12)
    assert table_width(ragged) == 3


def test_width_counts_a_colspan_cell_as_the_columns_it_covers():
    assert table_width('<tr><td colspan="2">Questions</td><td>Answers</td></tr>') == 3


def test_short_row_may_be_widened_to_the_modal_width():
    before = "<tr><td>(g) Do the Arrangements apply?</td><td>N/A</td></tr>"
    after = "<tr><td>(g)</td><td>Do the Arrangements apply?</td><td>N/A</td></tr>"
    assert verdict(before, after, width=3)[0] == VERDICT_STRUCTURAL


def test_row_may_not_be_widened_past_the_modal_width():
    before = "<tr><td>(g) Do the Arrangements apply?</td><td>N/A</td></tr>"
    after = "<tr><td>(g)</td><td>Do</td><td>the Arrangements apply?</td><td>N/A</td></tr>"
    v, why = verdict(before, after, width=3)
    assert v == VERDICT_REJECT
    # Either width rule may fire first; both name the width as the problem.
    assert "width" in why or "wider" in why


def test_widening_still_cannot_move_content():
    before = "<tr><td>(g) Do the Arrangements apply?</td><td>N/A</td></tr>"
    after = "<tr><td>(g)</td><td>N/A</td><td>Do the Arrangements apply?</td></tr>"
    assert verdict(before, after, width=3)[0] == VERDICT_REJECT


def test_padding_a_short_row_with_empty_cells_is_rejected():
    """Padding was briefly allowed on the grounds that an empty <td> adds no content.
    It adds no CONTENT and changes the RENDERING, which is how a full-width banner row
    got flattened into a narrow column — see the colspan tests below. A short row that
    stays short is the correct answer."""
    before = "<tr><td>6.2 Funds</td></tr>"
    after = "<tr><td>6.2</td><td>Funds</td><td></td></tr>"
    v, why = verdict(before, after, width=3)
    assert v == VERDICT_REJECT
    assert "empty cells added" in why


def test_splitting_a_label_without_padding_is_still_allowed():
    before = "<tr><td>6.2 Funds</td></tr>"
    after = "<tr><td>6.2</td><td>Funds</td></tr>"
    assert verdict(before, after, width=3)[0] == VERDICT_STRUCTURAL


# ---- colspan preservation ----------------------------------------------------
# The real regression: told "this table has 7 columns, every row must have 7 cells",
# Haiku rewrote the full-width banner row as one text cell plus six empty ones. 29 of
# the document's 36 span attributes were destroyed, and every existing check passed —
# the content stream is identical and the row still covers 7 columns.

BANNER = ('<tr><td colspan="7">What we are seeking to draw out in this section is '
          'whether there are any particular requirements.</td></tr>')


def test_flattening_a_colspan_banner_row_is_rejected():
    flat = BANNER.replace('<td colspan="7">', "<td>").replace(
        "</td></tr>", "</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>")
    v, why = verdict(BANNER, flat, width=7)
    assert v == VERDICT_REJECT
    assert "span attribute removed" in why or "empty cells added" in why


def test_removing_a_colspan_without_padding_is_still_rejected():
    v, why = verdict(BANNER, BANNER.replace('<td colspan="7">', "<td>"), width=7)
    assert v == VERDICT_REJECT
    assert "span attribute removed" in why


def test_a_colspan_row_left_alone_passes():
    assert verdict(BANNER, BANNER, width=7)[0] == VERDICT_CLEAN


def test_respacing_inside_a_colspan_row_still_passes():
    fixed = BANNER.replace("this section is", "this section<br>is")
    assert verdict(BANNER, fixed, width=7)[0] == VERDICT_SPACING


# ---------------------------------------------------------------- sub-chunking

from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    find_subsections, split_table, verify_split,
)

SECTION = ("<table>"
           "<tr><td>Questions</td><td>Answers</td><td></td></tr>"
           "<tr><td>6.1</td><td>Mutual Recognition</td><td></td></tr>"
           "<tr><td>(a)</td><td>Are there arrangements?</td><td>No.</td></tr>"
           "<tr><td>6.2 Funds</td></tr>"
           "<tr><td>(b)</td><td>Any regime?</td><td>Yes.</td></tr>"
           "</table>")
BOUNDS = [{"row": 1, "id": "6.1", "title": "Mutual Recognition"},
          {"row": 3, "id": "6.2", "title": "Funds"}]


def test_split_produces_one_heading_and_table_per_subsection():
    out = split_table(SECTION, BOUNDS)
    assert out.count("<table") == 2
    assert "### 6.1 Mutual Recognition" in out
    assert "### 6.2 Funds" in out


def test_split_drops_the_label_row_that_became_a_heading():
    # This is the duplication being removed: the label existed as heading AND row.
    out = split_table(SECTION, BOUNDS)
    assert "<td>6.1</td>" not in out


def test_split_keeps_every_other_row_verbatim_and_in_order():
    ok, why = verify_split(SECTION, split_table(SECTION, BOUNDS), BOUNDS)
    assert ok, why


def test_split_verifier_catches_a_dropped_row():
    out = split_table(SECTION, BOUNDS).replace(
        "<tr><td>(b)</td><td>Any regime?</td><td>Yes.</td></tr>", "")
    ok, why = verify_split(SECTION, out, BOUNDS)
    assert not ok and "row count changed" in why


def test_split_verifier_catches_a_reworded_heading():
    out = split_table(SECTION, BOUNDS).replace("### 6.2 Funds", "### 6.2 Investment Funds")
    ok, why = verify_split(SECTION, out, BOUNDS)
    assert not ok and "does not carry" in why


def test_header_row_rides_with_the_first_subsection():
    out = split_table(SECTION, BOUNDS)
    assert out.index("Questions") > out.index("### 6.1")
    assert out.index("Questions") < out.index("### 6.2")


def test_no_boundaries_leaves_the_table_untouched():
    assert split_table(SECTION, []) == SECTION


class _JsonStub:
    def __init__(self, payload):
        self.payload = payload

    def converse(self, **kw):
        return {"output": {"message": {"content": [{"text": self.payload}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5}}


def test_boundaries_accepted_when_the_row_really_says_so():
    bs, _ = find_subsections(_JsonStub(
        '[{"row":1,"id":"6.1","title":"Mutual Recognition"}]'), SECTION, models=("m",))
    assert bs == [{"row": 1, "id": "6.1", "title": "Mutual Recognition"}]


def test_a_title_the_model_invents_is_ignored_in_favour_of_the_row():
    # The model is not trusted with the title at all: whatever it sends is discarded and
    # the heading is read off the row, so it cannot be truncated or invented.
    bs, _ = find_subsections(_JsonStub(
        '[{"row":1,"id":"6.1","title":"Private Placement Regime"}]'), SECTION, models=("m",))
    assert bs == [{"row": 1, "id": "6.1", "title": "Mutual Recognition"}]


def test_long_titles_survive_the_truncated_preview():
    long = "Investment Management & Advisory Services in respect of Single Investor " \
           "Vehicles, Managed Accounts and Funds of One"
    table = ("<table><tr><td>Questions</td><td>Answers</td></tr>"
             f"<tr><td>7.5</td><td>{long}</td><td></td></tr>"
             "<tr><td>(a)</td><td>Any regime?</td><td>Yes.</td></tr></table>")
    bs, _ = find_subsections(_JsonStub('[{"row":1,"id":"7.5"}]'), table, models=("m",))
    assert bs[0]["title"] == long
    ok, why = verify_split(table, split_table(table, bs), bs)
    assert ok, why


def test_boundary_pointing_at_the_wrong_row_is_dropped():
    bs, _ = find_subsections(_JsonStub(
        '[{"row":2,"id":"6.1","title":"Mutual Recognition"}]'), SECTION, models=("m",))
    assert bs == []


def test_out_of_range_and_malformed_boundaries_are_dropped():
    bs, _ = find_subsections(_JsonStub(
        '[{"row":99,"id":"6.1"},{"nope":1},{"row":1,"id":"6.1","title":"Mutual Recognition"}]'),
        SECTION, models=("m",))
    assert bs == [{"row": 1, "id": "6.1", "title": "Mutual Recognition"}]


def test_unparseable_reply_yields_no_boundaries():
    bs, _ = find_subsections(_JsonStub("sorry, I cannot help"), SECTION, models=("m",))
    assert bs == []


# ---------------------------------------------------------------- heading reconciliation

from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    drop_duplicate_lead_heading, preceding_heading, remove_empty_shells,
)


def test_lead_heading_dropped_when_stage1_already_wrote_it():
    out, reused = drop_duplicate_lead_heading(
        "### 6.1 Mutual Recognition\n\n<table><tr><td>x</td></tr></table>",
        ("6.1", "6.1 Mutual Recognition"))
    assert reused and out.startswith("<table>")


def test_lead_heading_kept_when_the_existing_one_says_something_else():
    # Same number, different title — not a duplicate, so both must survive.
    out, reused = drop_duplicate_lead_heading(
        "### 6.2 Funds of One\n\n<table><tr><td>x</td></tr></table>",
        ("6.2", "6.2 Funds"))
    assert not reused and "6.2 Funds of One" in out


def test_empty_shell_removed_only_when_the_section_lives_elsewhere():
    text = ("## 6.2 Funds\n\n<table><tr><td>x</td></tr></table>\n\n"
            "## 6.2 Funds\n\n## 6.3 Other\n\nprose\n")
    out, gone = remove_empty_shells(text, {"6.2"}, {"6.2": "6.2 Funds"})
    assert gone == ["6.2 Funds"]
    assert out.count("## 6.2 Funds") == 1


def test_shell_holding_prose_is_never_removed():
    text = ("## 6.3 Other\n\n<table><tr><td>x</td></tr></table>\n\n"
            "## 6.3 Other\n\norphan prose that has nowhere else to go\n")
    out, gone = remove_empty_shells(text, {"6.3"}, {"6.3": "6.3 Other"})
    assert gone == [] and out == text


def test_sole_occurrence_is_never_removed():
    text = "## 6.2 Funds\n\n## 6.3 Other\n\nprose\n"
    out, gone = remove_empty_shells(text, {"6.2"}, {"6.2": "6.2 Funds"})
    assert gone == [] and out == text


def test_preceding_heading_finds_the_nearest_one():
    assert preceding_heading("## 6.1 A\n\ntext\n\n## 6.2 B\n\n")[0] == "6.2"


def test_json_fenced_boundaries_are_parsed():
    # Haiku answers the sub-section prompt in a ```json fence; an html-only fence
    # pattern silently swallowed every split.
    bs, _ = find_subsections(
        _JsonStub('```json\n[{"row":1,"id":"6.1"}]\n```'), SECTION, models=("m",))
    assert bs == [{"row": 1, "id": "6.1", "title": "Mutual Recognition"}]


def test_plain_fenced_rows_are_still_parsed():
    bs, _ = find_subsections(
        _JsonStub('```\n[{"row":1,"id":"6.1"}]\n```'), SECTION, models=("m",))
    assert bs and bs[0]["row"] == 1


# ---------------------------------------------------------------- trailing duplicates

from aosphere_core_index.extract.ai_postprocess import prune_trailing_duplicates  # noqa: E402

DUP_DOC = (
    "# 6 X\n\n## 6.1 Alpha\n\n"
    "<table><tr><td>(a)</td><td>Services to the public in your jurisdiction "
    "permitted without restriction, subject to the Act.</td></tr></table>\n\n"
    "Services to the public in your jurisdiction permitted without\n\n"
    "## 6.3 Gamma\n\n"
    "A sentence that appears nowhere inside any table at all.\n"
)


def test_leftover_fragment_already_inside_a_table_is_removed():
    out, info = prune_trailing_duplicates(DUP_DOC)
    assert info["removed"] == ["Services to the public in your jurisdiction permitted without"]
    assert "Services to the public in your jurisdiction permitted without\n" not in out


def test_prose_that_is_not_in_any_table_is_kept():
    out, _ = prune_trailing_duplicates(DUP_DOC)
    assert "A sentence that appears nowhere inside any table at all." in out


def test_headings_are_never_removed():
    """An empty stage-1 shell may well be a duplicate, but deciding that is heading
    reconciliation's job — this step only ever touches prose."""
    out, _ = prune_trailing_duplicates(DUP_DOC)
    assert "## 6.3 Gamma" in out


def test_short_fragments_are_reported_not_deleted():
    doc = DUP_DOC.replace(
        "Services to the public in your jurisdiction permitted without\n", "services;\n")
    out, info = prune_trailing_duplicates(doc)
    assert info["removed"] == [] and info["candidates"] == ["services;"]
    assert "services;" in out


def test_text_before_the_last_table_is_untouched():
    doc = ("# 6 X\n\nSome intro prose.\n\n<table><tr><td>Some intro prose.</td></tr></table>\n\n"
           "<table><tr><td>b</td></tr></table>\n")
    out, info = prune_trailing_duplicates(doc)
    assert info["removed"] == []          # the duplicate sits BEFORE the last table
    assert "Some intro prose." in out


def test_a_file_with_no_tables_is_returned_unchanged():
    doc = "# 4 [SECTION INTENTIONALLY LEFT BLANK]\n"
    out, info = prune_trailing_duplicates(doc)
    assert out == doc and info["removed"] == []


def test_removal_only_ever_deletes_text_the_tables_still_hold():
    out, info = prune_trailing_duplicates(DUP_DOC)
    tables = "".join(re.findall(r"<table\b.*?</table>", out, re.S))
    for line in info["removed"]:
        assert content_stream(line) in content_stream(tables)


def test_a_no_op_prune_leaves_the_text_byte_identical():
    doc = "# 6 X\n\n<table><tr><td>a</td></tr></table>\n\n\n\nUnrelated prose.\n"
    out, info = prune_trailing_duplicates(doc)
    assert out == doc and info["removed"] == []


# ---------------------------------------------------------------- foreign sections

from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    accept_section,
)

SEC = ("# 3 SOURCES OF LAW\n\n"
       "<table><tr><td>(a)</td><td>Own question here.</td></tr>"
       "<tr><td>(b)</td><td>Another own question.</td></tr></table>\n\n"
       "# 5 PASSIVE MARKETING\n\n"
       "<table><tr><td>(a)</td><td>Foreign question from section five.</td></tr></table>\n")


def test_dropping_a_foreign_section_is_accepted():
    """The cleanup we now ask for: stage 1 dragged section 5 into section 3's chunk."""
    trimmed = SEC.split("# 5 PASSIVE MARKETING")[0]
    ok, why = accept_section(SEC, trimmed)
    assert ok and "lost" in why


def test_keeping_the_foreign_section_is_also_accepted():
    # Not mandatory — the model may leave it, and that is not a failure.
    assert accept_section(SEC, SEC)[0] is True


def test_lost_cell_text_is_reported_not_blocked():
    """Enforcement was dropped: "no cell content may go missing" and "delete other
    sections' content" cannot both be enforced without a way to tell them apart, and on
    the real document the foreign rows sit inside the same table as the section's own.
    The number is recorded in stage4_report.json instead, and stage 3 stays on disk."""
    broken = SEC.replace("<tr><td>(b)</td><td>Another own question.</td></tr>", "")
    ok, why = accept_section(SEC, broken)
    assert ok and "lost" in why


def test_an_unusable_reply_is_still_refused():
    assert accept_section(SEC, "")[0] is False
    assert accept_section(SEC, "sorry, I cannot help with that")[0] is False


# ---- which pages the model is shown -----------------------------------------------
# The prompt permits the model to append text that is "visibly printed on one of the pages
# above". That makes the page SET the limit on what it can restore, and the set was wrong:
# it came from the tables manifest alone, which attributes a page shared by two sections to
# the LATER one, so every section lost its last page -- the page a table's tail continues
# onto. On Bahamas that dropped page 34 from section 6, whose first printed line is the
# tail of 6.3's last question. The model could not restore it and was right not to.

def test_the_page_set_covers_the_whole_section_not_just_its_tables():
    from aosphere_core_index.extract.ai_postprocess import _section_pages
    # the real badge line stage 3 writes, so this exercises the manifest half too
    text = ('# 6. MARKETING\n\n*Source: `source_repaired.pdf`, page 19\u201334*\n\n'
            '**\u2699 MinerU-extracted table** \u2014 table_006, pages 19\u201333, 33 rows\n\n'
            '<table><tr><td>x</td></tr></table>\n')
    pages = _section_pages(text, {"table_006": list(range(19, 34))})   # manifest stops at 33
    assert pages == list(range(19, 35)), pages
    assert 34 in pages, "the shared last page is exactly where the lost cell lives"


def test_a_section_with_no_tables_still_gets_its_pages():
    """8 of 16 sections on Bahamas had no manifest entry, so they were repaired with ZERO
    ground truth -- the model was asked to check the markdown against pages it never saw."""
    from aosphere_core_index.extract.ai_postprocess import _section_pages
    text = '# 13. DISCLAIMERS\n\n*Source: `s.pdf`, page 70–72*\n\nprose only\n'
    assert _section_pages(text, {}) == [70, 71, 72]


def test_the_manifest_is_kept_where_it_is_wider_than_the_breadcrumb():
    """A union, not a replacement: a narrow breadcrumb must not silently drop ground truth
    that was being sent before."""
    from aosphere_core_index.extract.ai_postprocess import _section_pages
    text = ('*Source: `s.pdf`, page 5*\n\n'
            '**\u2699 MinerU-extracted table** \u2014 table_001, pages 4\u20136, 3 rows\n\n'
            '<table><tr><td>x</td></tr></table>\n')
    assert _section_pages(text, {"table_001": [4, 5, 6]}) == [4, 5, 6]


def test_a_page_with_no_rendered_image_is_rendered_rather_than_skipped(tmp_path):
    """Stage 2 renders only the pages it crops for tables -- 59 of 74 on Bahamas -- so a
    whole-section page range asks for pages that were never rendered, and the old code
    dropped them without a word while reporting a confident image count."""
    fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")
    from aosphere_core_index.extract.ai_postprocess import _page_png
    pdf = tmp_path / "d.pdf"
    doc = fitz.open()
    for _ in range(3):
        doc.new_page().insert_text((72, 100), "hello")
    doc.save(str(pdf)); doc.close()
    assets = tmp_path / "_assets"
    assets.mkdir()
    data = _page_png(assets, 2, pdf)
    assert data and data[:4] == b"\x89PNG"
    assert (assets / "page-002.png").exists(), "rendered once and cached for the next section"
    # out of range, and no PDF at all, are both survivable
    assert _page_png(assets, 99, pdf) is None
    assert _page_png(assets, 1, None) is None


# ---- MinerU's counts are not shown to the model -----------------------------------
# "31 rows × 3 cols" is MinerU's GUESS, and on table_005 it drove the model to restructure
# a question/answer table into three columns: stage 3 had 29 rows of two cells with the
# label glued into the question and 2 rows of three, and the model normalised all of them
# to the declared three. It was obeying the badge. The pages are the ground truth here; the
# counts are a second opinion from the tool whose output is being corrected.

BADGE = ("**⚙ MinerU-extracted table** — table_005, pages 15–18, "
         "31 rows × 3 cols")
SECTION_MD = ("# 5 PASSIVE MARKETING\n\n*Source: `s.pdf`, page 15–19*\n\n"
              + BADGE + "\n\n<table><tr><td>x</td></tr></table>\n")


def test_the_model_is_not_told_how_many_columns_mineru_thinks_there_are():
    from aosphere_core_index.extract.ai_postprocess import _hide_mineru_counts
    sent, _ = _hide_mineru_counts(SECTION_MD)
    assert "3 cols" not in sent and "31 rows" not in sent


def test_the_table_id_and_pages_survive_because_they_are_load_bearing():
    """The id is how _tables_in maps a table to its page images, and the pages are what
    the review tool references. Only the COUNTS are hidden."""
    from aosphere_core_index.extract.ai_postprocess import _hide_mineru_counts
    sent, _ = _hide_mineru_counts(SECTION_MD)
    assert "table_005" in sent
    assert "pages 15–18" in sent
    assert "*Source: `s.pdf`, page 15–19*" in sent      # the breadcrumb is untouched


def test_the_counts_are_put_back_so_the_file_on_disk_keeps_them():
    """Hidden for one API call, not removed from the pipeline. table_width still reads the
    declared columns in batched mode, and _tables_in still needs the line."""
    from aosphere_core_index.extract.ai_postprocess import (
        _hide_mineru_counts, _restore_mineru_counts,
    )
    sent, originals = _hide_mineru_counts(SECTION_MD)
    assert _restore_mineru_counts(sent, originals) == SECTION_MD


def test_a_badge_with_no_counts_is_left_alone():
    from aosphere_core_index.extract.ai_postprocess import _hide_mineru_counts
    plain = "**⚙ MinerU-extracted table** — table_009, pages 40–42\n"
    sent, originals = _hide_mineru_counts(plain)
    assert sent == plain and originals == {}


def test_restoring_is_safe_when_the_model_returned_the_badge_unchanged():
    """If a model echoes the full badge anyway, restoring must not double the counts."""
    from aosphere_core_index.extract.ai_postprocess import (
        _hide_mineru_counts, _restore_mineru_counts,
    )
    _, originals = _hide_mineru_counts(SECTION_MD)
    assert _restore_mineru_counts(SECTION_MD, originals) == SECTION_MD


def test_the_call_hides_the_counts_and_the_result_has_them_back(monkeypatch):
    """End to end through repair_section: what leaves is stripped, what returns is whole."""
    import aosphere_core_index.extract.ai_postprocess as ai
    seen = {}

    class FakeClient:
        def converse(self, **kw):
            seen["text"] = kw["messages"][0]["content"][-1]["text"]
            # a model that echoes back exactly what it was shown
            body = seen["text"].split("\n\n", 1)[1]
            return {"usage": {"inputTokens": 1, "outputTokens": 1},
                    "output": {"message": {"content": [{"text": body}]}},
                    "stopReason": "end_turn"}

    after, _, _ = ai.repair_section(FakeClient(), SECTION_MD, [], Path("/nonexistent"))
    assert "3 cols" not in seen["text"], "the counts reached the model"
    assert "31 rows × 3 cols" in after, "the counts did not come back into the file"


# ---------------------------------------------------------------- text-section identity
#
# Front matter and a real numbered section routinely share one page (front matter runs
# to page 3, "1. BACKGROUND" starts partway down that same page), and a text section's
# word-preservation check is deliberately loose — spillover trimming legitimately drops
# text, so a lost/gained count alone cannot tell that apart from the model having
# answered a DIFFERENT section entirely. Observed on Japan 172122: the file still named
# "front-matter" came back headed "# 1. BACKGROUND", with the real background section's
# own content — passed the word-preservation check outright, because prose was merely
# swapped, not shortened or invented.

from aosphere_core_index.extract.ai_postprocess import _heading_number, accept_text_section


def test_heading_number_reads_the_leading_digit():
    assert _heading_number("# 6 Marketing/Selling to the Public\n") == "6"


def test_heading_number_is_none_for_unnumbered_sections():
    assert _heading_number("# Front Matter\n") is None


def test_heading_number_reads_through_an_appendix_label():
    assert _heading_number("# Appendix 2 Disclaimers: Closed-Ended Fund\n") == "2"


def test_front_matter_swapped_for_a_numbered_section_is_rejected():
    before = "# Front Matter\n\n*Source: `source.pdf`, page 1–3*\n\nRESTRICTIONS ON CROSS-BORDER"
    after = "# 1. BACKGROUND\n\n*Source: `source.pdf`, page 1–3*\n\n## 1.1 Introduction\ntext"
    ok, why = accept_text_section(before, after)
    assert not ok
    assert "section number changed" in why


def test_same_section_number_is_still_accepted_even_with_prose_drift():
    before = "# 6 Marketing/Selling to the Public\n\nold wording here"
    after = "# 6 Marketing/Selling to the Public\n\nnew wording, trimmed of spillover"
    ok, _ = accept_text_section(before, after)
    assert ok


def test_orphaned_appendix_label_fold_keeps_the_same_number_and_is_accepted():
    """The one sanctioned heading rewrite: an orphaned "APPENDIX N" line folded into the
    heading. The number never changes, so the identity guard must not catch it."""
    before = "# 2 Disclaimers: Closed-Ended Fund\n\n*Source: `x.pdf`, page 90–90*\n\nAPPENDIX 2\ntext"
    after = "# Appendix 2 Disclaimers: Closed-Ended Fund\n\n*Source: `x.pdf`, page 90–90*\n\ntext"
    ok, _ = accept_text_section(before, after)
    assert ok


def test_empty_output_still_rejected_before_the_identity_check_runs():
    ok, why = accept_text_section("# Front Matter\n\ntext", "   ")
    assert not ok
    assert "no text" in why
