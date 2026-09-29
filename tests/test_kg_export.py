"""Generic MinerU chunker + KG exporter — marker detection, page-scoped
footnote/page-note attachment, per-block rendering, KG schema, file I/O."""

import json

from aosphere_core_index.extract.kg_export import detect_marker, export_chunks


def _text(text, page=0, **extra):
    return {"type": "text", "text": text, "page_idx": page, **extra}


def _heading(text, level, page=0):
    return _text(text, page=page, text_level=level)


def _list(items, page=0):
    return {"type": "list", "items": items, "page_idx": page}


def _manifest(out_dir):
    return json.loads((out_dir / "knowledge_graph.json").read_text())


# ---- marker detection ----------------------------------------------------

def test_decimal_depth_is_dot_count():
    assert detect_marker("1. Overview", roman_open=False, parent_is_alpha=False).depth == 1
    assert detect_marker("1.1 Sub-point", roman_open=False, parent_is_alpha=False).depth == 2
    assert detect_marker("1.1.1 Deep", roman_open=False, parent_is_alpha=False).depth == 3


def test_upper_alpha_and_lowercase_paren_alpha():
    assert detect_marker("A. Substantial Shareholding", roman_open=False, parent_is_alpha=False).family == "upper_alpha"
    assert detect_marker("(b) some item", roman_open=False, parent_is_alpha=False).family == "paren_alpha"


def test_unambiguous_multi_char_roman():
    assert detect_marker("(iv) fourth item", roman_open=False, parent_is_alpha=False).family == "paren_roman"


def test_ambiguous_single_letter_prefers_alpha_by_default():
    assert detect_marker("(i) plain item", roman_open=False, parent_is_alpha=False).family == "paren_alpha"


def test_ambiguous_single_letter_prefers_roman_under_alpha_parent():
    assert detect_marker("(i) nested item", roman_open=False, parent_is_alpha=True).family == "paren_roman"


def test_ambiguous_single_letter_prefers_roman_when_roman_context_open():
    assert detect_marker("(v) fifth item", roman_open=True, parent_is_alpha=False).family == "paren_roman"


def test_plain_sentence_is_not_a_marker():
    assert detect_marker("This is just a sentence.", roman_open=False, parent_is_alpha=False) is None


# ---- chunk tree + nesting --------------------------------------------------

def test_heading_hierarchy_and_skipped_levels(tmp_path):
    blocks = [
        _heading("Root Title", 1),
        _text("intro line"),
        _heading("Deeply nested", 3),  # no level-2 in between -> attaches to level 1
        _text("deep content"),
    ]
    export_chunks(blocks, doc_id="doc1", source_file="doc1.pdf", out_dir=tmp_path)
    items = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    assert items["Deeply nested"]["parent"] == items["Root Title"]["@id"]


def test_list_item_promoted_to_heading_nests_under_current_section(tmp_path):
    blocks = [
        _heading("A. Substantial Shareholding", 1),
        _list(["1. Overview", "1.1 General rule", "not a marker, stays a bullet"]),
    ]
    export_chunks(blocks, doc_id="doc2", source_file="doc2.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    assert "1. Overview" in by_name and "1.1 General rule" in by_name
    assert by_name["1.1 General rule"]["parent"] == by_name["1. Overview"]["@id"]


def test_alpha_decimal_paren_deepen_one_level_at_a_time(tmp_path):
    # The real bug report: MinerU tags "A."/"1."/"1.1" as SEPARATE standalone
    # text blocks (not list items), all with the same text_level — without
    # marker-based placement they'd all land at the same depth. Each family
    # should nest one level under the last instead.
    blocks = [
        _heading("A. Substantial Shareholding", 1),
        _heading("1. Overview", 1),
        _heading("1.1 General Disclosure Obligation", 1),
        _list(["(i) acquires or sells securities", "(ii) changes its holding"]),
    ]
    export_chunks(blocks, doc_id="doc11", source_file="doc11.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    a = by_name["A. Substantial Shareholding"]["level"]
    one = by_name["1. Overview"]["level"]
    onept1 = by_name["1.1 General Disclosure Obligation"]["level"]
    i = by_name["(i) acquires or sells securities"]["level"]
    ii = by_name["(ii) changes its holding"]["level"]
    assert (a, one, onept1, i) == (a, a + 1, a + 2, a + 3)
    assert i == ii  # siblings, not chained


def test_decimal_with_no_enclosing_alpha_starts_at_the_top(tmp_path):
    # No "A."-style tier at all — "1. Methodology" must anchor to root (level
    # 0), not get pushed down a level just because SOME documents have an
    # alpha tier.
    blocks = [_heading("1. Methodology", 1), _heading("1.1 Data Collection", 1)]
    export_chunks(blocks, doc_id="doc12", source_file="doc12.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    assert by_name["1. Methodology"]["level"] == 1
    assert by_name["1.1 Data Collection"]["level"] == 2


def test_new_top_level_marker_pops_past_deep_unrelated_nesting(tmp_path):
    # "B." must snap back to sibling-of-"A.", not nest under whatever
    # deeply-nested paren item happened to be open last.
    blocks = [
        _heading("A. Substantial Shareholding", 1),
        _heading("1.1 Obligation", 1),
        _list(["(a) first", "(i) sub-point"]),
        _heading("B. Sensitive Industries", 1),
    ]
    export_chunks(blocks, doc_id="doc13", source_file="doc13.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    assert by_name["B. Sensitive Industries"]["level"] == by_name["A. Substantial Shareholding"]["level"]
    assert by_name["B. Sensitive Industries"]["parent"] == by_name["A. Substantial Shareholding"]["parent"]


def test_roman_list_starting_with_ambiguous_i_still_siblings_with_ii(tmp_path):
    # Real bug found against the actual Argentina document: "(i)" alone is
    # ambiguous (nothing precedes it in the list to disambiguate via
    # roman_open), defaulting to paren_alpha — then, since paren_roman ranks
    # deeper than paren_alpha, the unambiguous "(ii)" that follows would
    # wrongly nest UNDER the misclassified "(i)" instead of beside it, unless
    # the list is scanned ahead of time for roman evidence first.
    blocks = [
        _heading("1.1 General Disclosure Obligation", 1),
        _list(["(i) acquires or sells securities", "(ii) changes its holding", "(iii) converts debt"]),
    ]
    export_chunks(blocks, doc_id="doc14", source_file="doc14.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    i, ii, iii = (by_name[n] for n in
                  ("(i) acquires or sells securities", "(ii) changes its holding", "(iii) converts debt"))
    assert i["level"] == ii["level"] == iii["level"]
    assert i["parent"] == ii["parent"] == iii["parent"]


def test_two_unrelated_roman_lists_under_different_alpha_items_do_not_collide(tmp_path):
    # Real bug found against the actual Argentina document: two SEPARATE,
    # unrelated roman-numeral lists (five pages apart in the source, each
    # under its own distinct (a)-style container) both resolved to the same
    # clause key because paren_roman and paren_alpha were ranked as PEERS —
    # a roman list always skipped past its true alpha container back to the
    # same distant decimal ancestor, so both collided on the same parent.
    blocks = [
        _heading("1.1 Obligation", 1),
        _list(["(a) trigger events", "(i) event one", "(ii) event two"]),
        _list(["(f) required documents", "(i) document one", "(ii) document two"]),
    ]
    export_chunks(blocks, doc_id="doc15", source_file="doc15.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    assert by_name["(i) event one"]["parent"] == by_name["(a) trigger events"]["@id"]
    assert by_name["(i) document one"]["parent"] == by_name["(f) required documents"]["@id"]
    assert by_name["(i) event one"]["@id"] != by_name["(i) document one"]["@id"]


def test_roman_numeral_siblings_do_not_chain_into_each_other(tmp_path):
    # Real bug: each subsequent (i)/(ii)/(iii) item was nesting under the
    # PREVIOUS item instead of their shared parent, cascading footnotes
    # needlessly deep through the whole chain.
    blocks = [
        _heading("1.1 General Disclosure Obligation", 1),
        _list(["(i) acquires or sells securities", "(ii) changes its holding", "(iii) converts debt securities"]),
    ]
    export_chunks(blocks, doc_id="doc9", source_file="doc9.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    parent_id = by_name["1.1 General Disclosure Obligation"]["@id"]
    for name in ("(i) acquires or sells securities", "(ii) changes its holding", "(iii) converts debt securities"):
        assert by_name[name]["parent"] == parent_id, f"{name} should be a direct sibling, not chained"


def test_mixed_depth_list_still_nests_correctly(tmp_path):
    blocks = [
        _heading("Part A", 1),
        _list(["1. Intro", "1.1 Sub one", "1.2 Sub two", "2. Next"]),
    ]
    export_chunks(blocks, doc_id="doc10", source_file="doc10.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    part_a = by_name["Part A"]["@id"]
    intro = by_name["1. Intro"]["@id"]
    assert by_name["1.1 Sub one"]["parent"] == intro
    assert by_name["1.2 Sub two"]["parent"] == intro  # sibling of 1.1, not nested under it
    assert by_name["2. Next"]["parent"] == part_a      # back up to Part A, not chained off 1.2


# ---- page-scoped footnotes / page notes ------------------------------------

def test_footnote_attaches_to_every_chunk_sharing_its_page(tmp_path):
    blocks = [
        _heading("Part A", 1, page=0),
        _text("first section body", page=0),
        _heading("Part B", 1, page=0),  # same page as the footnote below
        _text("second section body", page=0),
        {"type": "page_footnote", "text": "Shared footnote.", "page_idx": 0},
    ]
    export_chunks(blocks, doc_id="doc3", source_file="doc3.pdf", out_dir=tmp_path)
    by_name = {e["item"]["name"]: e["item"] for e in _manifest(tmp_path)["itemListElement"]}
    assert by_name["Part A"]["pageFootnotes"] == ["Shared footnote."]
    assert by_name["Part B"]["pageFootnotes"] == ["Shared footnote."]


def test_page_note_excluded_from_body_but_present_in_metadata(tmp_path):
    blocks = [
        _heading("Part A", 1, page=0),
        _text("body text", page=0),
        {"type": "header", "text": "CONFIDENTIAL", "page_idx": 0},
    ]
    filenames, _ = export_chunks(blocks, doc_id="doc4", source_file="doc4.pdf", out_dir=tmp_path)
    part_a_file = next(f for f in filenames if "part-a" in f)
    content = (tmp_path / part_a_file).read_text()
    assert "CONFIDENTIAL" not in content.split("---", 2)[2]  # not in body
    assert "CONFIDENTIAL" in content  # present in the YAML frontmatter (pageNotes)


# ---- content composition / rendering ---------------------------------------

def test_heading_with_no_body_falls_back_to_title_as_description(tmp_path):
    blocks = [_heading("A. Substantial Shareholding", 1), _heading("B. Sensitive Industries", 1)]
    export_chunks(blocks, doc_id="doc5", source_file="doc5.pdf", out_dir=tmp_path)
    a = next(e["item"] for e in _manifest(tmp_path)["itemListElement"]
             if e["item"]["name"] == "A. Substantial Shareholding")
    assert a["description"] == "A. Substantial Shareholding"


def test_table_renders_caption_body_footnote_in_order(tmp_path):
    blocks = [
        _heading("Part A", 1),
        {
            "type": "table", "page_idx": 0,
            "table_caption": ["Table 1"], "table_footnote": ["n=50"],
            "table_body": "<table><tr><td>x</td></tr></table>",
        },
    ]
    filenames, _ = export_chunks(blocks, doc_id="doc6", source_file="doc6.pdf", out_dir=tmp_path)
    body = (tmp_path / next(f for f in filenames if "part-a" in f)).read_text()
    assert body.index("**Table 1**") < body.index("<table>") < body.index("*n=50*")


def test_unrecognized_block_type_falls_back_to_raw_text(tmp_path):
    blocks = [_heading("Part A", 1), {"type": "mystery", "text": "unclassified content", "page_idx": 0}]
    filenames, _ = export_chunks(blocks, doc_id="doc7", source_file="doc7.pdf", out_dir=tmp_path)
    body = (tmp_path / next(f for f in filenames if "part-a" in f)).read_text()
    assert "unclassified content" in body


# ---- file I/O: idempotency + orphan cleanup --------------------------------

def test_idempotent_rewrite_and_orphan_cleanup(tmp_path):
    blocks_v1 = [_heading("Part A", 1), _text("first version")]
    filenames_v1, _ = export_chunks(blocks_v1, doc_id="doc8", source_file="doc8.pdf", out_dir=tmp_path)
    mtimes = {f: (tmp_path / f).stat().st_mtime_ns for f in filenames_v1}

    export_chunks(blocks_v1, doc_id="doc8", source_file="doc8.pdf", out_dir=tmp_path)  # unchanged input
    for f in filenames_v1:
        assert (tmp_path / f).stat().st_mtime_ns == mtimes[f]  # -> nothing rewritten

    # renaming the heading changes the slug -> old file removed, new one written
    blocks_v2 = [_heading("Part A Renamed", 1), _text("first version")]
    filenames_v2, removed = export_chunks(blocks_v2, doc_id="doc8", source_file="doc8.pdf", out_dir=tmp_path)
    assert removed >= 1
    old_file = next(f for f in filenames_v1 if "part-a" in f and "renamed" not in f)
    assert not (tmp_path / old_file).exists()
    assert all((tmp_path / f).exists() for f in filenames_v2)
