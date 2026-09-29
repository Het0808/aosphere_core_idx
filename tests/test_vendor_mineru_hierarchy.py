"""Drift guard for extract/vendor/mineru25/hierarchy.py.

Adapted from the source project's own tests/test_hierarchy.py (commit
dc47d6148d351b6627d1dca8f6927c885a9f4446) — trimmed to the subset that
exercises hierarchy.py's own pure functions (classify_heading_family,
build_chunks, expand_list_blocks, _estimate_item_bbox, slugify) without the
source project's kg_entity.py (not vendored here — see
extract/vendor/mineru25/__init__.py for why). A few fixtures had their
kg_entity-dependent assertions (chunk_body_markdown, build_entities, etc.)
dropped, keeping only the hierarchy-level checks.

Fails loudly if the vendored copy is ever hand-edited or re-vendored from a
different commit — fix upstream and re-vendor rather than editing this file's
expectations to match a drifted copy.
"""

from aosphere_core_index.extract.vendor.mineru25.hierarchy import (
    _estimate_item_bbox,
    build_chunks,
    classify_heading_family,
    expand_list_blocks,
    slugify,
)

# Real MinerU2.5 output on a report-style PDF reported every heading as
# text_level=1, regardless of its actual visual/numbered depth.
FLAT_LEVEL_BLOCKS = [
    {"type": "text", "text": "Annual Research Report", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "1. Introduction", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "Intro body.", "page_idx": 0},
    {"type": "text", "text": "1.1 Background", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "Background body.", "page_idx": 0},
    {"type": "text", "text": "2. Methodology", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "2.1 Data Collection", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "2.1.1 Preprocessing", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "Preprocessing body.", "page_idx": 0},
]


def test_classify_heading_family_decimal_depth():
    assert classify_heading_family("1. Introduction", []) == "decimal-1"
    assert classify_heading_family("1.1 Background", []) == "decimal-2"
    assert classify_heading_family("2.1.1 Preprocessing", []) == "decimal-3"
    assert classify_heading_family("Annual Research Report", []) is None
    assert classify_heading_family("Conclusion", []) is None


def test_classify_heading_family_upper_alpha_and_paren_alpha():
    assert classify_heading_family("A. Substantial Shareholding", []) == "upper-alpha"
    assert classify_heading_family("(a) Some clause", []) == "paren-alpha"
    assert classify_heading_family("(b) Another clause", ["upper-alpha", "decimal-2"]) == "paren-alpha"


def test_classify_heading_family_roman_vs_alpha_ambiguity():
    assert classify_heading_family("(ii) second point", []) == "paren-roman"
    assert classify_heading_family("(iv) fourth point", []) == "paren-roman"
    assert classify_heading_family("(i) first point", ["upper-alpha", "paren-alpha"]) == "paren-roman"
    assert classify_heading_family("(v) fifth point", ["paren-alpha", "paren-roman"]) == "paren-roman"
    assert classify_heading_family("(i) first point", []) == "paren-alpha"


# The canonical fixture the new adapter's own key-parity test
# (tests/test_mineru_adapter.py) is verified against.
LEGAL_STYLE_BLOCKS = [
    {"type": "text", "text": "A. Substantial Shareholding", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "1. Overview", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "1.1 General Disclosure Obligation", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "Intro sentence before the lettered points.", "page_idx": 0},
    {"type": "text", "text": "(a) Disclosure obligation applicable to any person generally", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "When a person, directly or indirectly, acting together with others,", "page_idx": 0},
    {"type": "text", "text": "(i) acquires or sells securities of a public issuer;", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "(ii) changes its direct or indirect holding in a public issuer;", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "(b) Disclosure obligation applicable to controlling shareholder", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "1.2 Another Clause", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "B. Another Top-Level Section", "text_level": 1, "page_idx": 0},
]


def test_build_chunks_handles_mixed_alpha_numeric_roman_hierarchy():
    chunks = build_chunks(LEGAL_STYLE_BLOCKS, doc_title="Doc")
    by_title = {c.title: c for c in chunks}

    a_section = by_title["A. Substantial Shareholding"]
    overview = by_title["1. Overview"]
    obligation = by_title["1.1 General Disclosure Obligation"]
    point_a = by_title["(a) Disclosure obligation applicable to any person generally"]
    point_i = by_title["(i) acquires or sells securities of a public issuer;"]
    point_ii = by_title["(ii) changes its direct or indirect holding in a public issuer;"]
    point_b = by_title["(b) Disclosure obligation applicable to controlling shareholder"]
    clause_1_2 = by_title["1.2 Another Clause"]
    b_section = by_title["B. Another Top-Level Section"]

    assert a_section.level == 1
    assert overview.level == 2 and overview.parent_order == a_section.order
    assert obligation.level == 3 and obligation.parent_order == overview.order
    assert point_a.level == 4 and point_a.parent_order == obligation.order
    assert point_i.level == 5 and point_i.parent_order == point_a.order
    assert point_ii.level == 5 and point_ii.parent_order == point_a.order
    assert point_b.level == 4 and point_b.parent_order == obligation.order
    assert clause_1_2.level == 3 and clause_1_2.parent_order == overview.order
    assert b_section.level == 1 and b_section.parent_order == a_section.parent_order == 0


def test_build_chunks_recovers_depth_when_mineru_flattens_levels():
    chunks = build_chunks(FLAT_LEVEL_BLOCKS, doc_title="Doc")
    by_title = {c.title: c for c in chunks}

    assert by_title["1. Introduction"].level == 1
    assert by_title["1.1 Background"].level == 2
    assert by_title["1.1 Background"].parent_order == by_title["1. Introduction"].order

    assert by_title["2. Methodology"].level == 1
    assert by_title["2.1 Data Collection"].level == 2
    assert by_title["2.1 Data Collection"].parent_order == by_title["2. Methodology"].order

    assert by_title["2.1.1 Preprocessing"].level == 3
    assert by_title["2.1.1 Preprocessing"].parent_order == by_title["2.1 Data Collection"].order


SAMPLE_BLOCKS = [
    {"type": "text", "text": "Intro paragraph before any heading.", "page_idx": 0},
    {"type": "text", "text": "Chapter 1: Overview", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "Overview body text.", "page_idx": 0},
    {"type": "text", "text": "1.1 Background", "text_level": 2, "page_idx": 1},
    {"type": "text", "text": "Background body text.", "page_idx": 1},
    {"type": "table", "table_body": "<table><tr><td>a</td></tr></table>", "table_caption": ["Table 1"], "page_idx": 1},
    # Skipped level: jump straight from level-2 to level-4 heading, no level-3 in between.
    {"type": "text", "text": "1.1.1.1 Deep detail", "text_level": 4, "page_idx": 1},
    {"type": "text", "text": "Deep detail body.", "page_idx": 1},
    {"type": "text", "text": "Chapter 2: Methods", "text_level": 1, "page_idx": 2},
    {"type": "text", "text": "Methods body text.", "page_idx": 2},
]


def test_root_and_top_level_chunks():
    chunks = build_chunks(SAMPLE_BLOCKS, doc_title="My Doc")
    root = chunks[0]
    assert root.level == 0
    assert root.title == "My Doc"
    assert any("Intro paragraph" in b["text"] for b in root.blocks)

    top_level_titles = [chunks[o].title for o in root.children_orders]
    assert top_level_titles == ["Chapter 1: Overview", "Chapter 2: Methods"]


def test_nested_heading_and_body_attachment():
    chunks = build_chunks(SAMPLE_BLOCKS, doc_title="My Doc")
    by_title = {c.title: c for c in chunks}

    ch1 = by_title["Chapter 1: Overview"]
    assert any("Overview body text" in b["text"] for b in ch1.blocks)

    bg = by_title["1.1 Background"]
    assert bg.parent_order == ch1.order
    assert any("Background body text" in b["text"] for b in bg.blocks)
    assert any(b["type"] == "table" for b in bg.blocks)


def test_skipped_heading_level_attaches_to_nearest_ancestor():
    chunks = build_chunks(SAMPLE_BLOCKS, doc_title="My Doc")
    by_title = {c.title: c for c in chunks}

    bg = by_title["1.1 Background"]
    deep = by_title["1.1.1.1 Deep detail"]
    assert deep.level == 4
    assert deep.parent_order == bg.order
    assert any("Deep detail body" in b["text"] for b in deep.blocks)


def test_page_ranges_computed():
    chunks = build_chunks(SAMPLE_BLOCKS, doc_title="My Doc")
    by_title = {c.title: c for c in chunks}
    ch1 = by_title["Chapter 1: Overview"]
    assert ch1.page_start == 0
    assert ch1.page_end == 0


# Real MinerU2.5 output: a page-bottom footnote (divider rule + small font) is
# tagged "page_footnote" and appears in content_list.json AFTER both sections
# that share its page, in reading order.
FOOTNOTE_PAGE_BLOCKS = [
    {"type": "text", "text": "1. Results", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "Our results show a 42% improvement over baseline methods.", "page_idx": 0},
    {"type": "text", "text": "1.1 Details", "text_level": 2, "page_idx": 0},
    {"type": "text", "text": "See the benchmark numbers below.", "page_idx": 0},
    {"type": "page_footnote", "text": "1 Baseline methods per Appendix A.", "page_idx": 0},
    {"type": "page_footnote", "text": "2 See Smith et al. (2024).", "page_idx": 0},
    {"type": "text", "text": "2. Next Page Section", "text_level": 1, "page_idx": 1},
    {"type": "text", "text": "This section is on the next page, no footnotes here.", "page_idx": 1},
]


def test_footnotes_attach_to_every_chunk_on_that_page():
    chunks = build_chunks(FOOTNOTE_PAGE_BLOCKS, doc_title="Doc")
    by_title = {c.title: c for c in chunks}

    results = by_title["1. Results"]
    details = by_title["1.1 Details"]
    next_page = by_title["2. Next Page Section"]

    assert [fn["text"] for fn in results.footnotes] == [
        "1 Baseline methods per Appendix A.",
        "2 See Smith et al. (2024).",
    ]
    assert [fn["text"] for fn in details.footnotes] == [
        "1 Baseline methods per Appendix A.",
        "2 See Smith et al. (2024).",
    ]
    # a chunk on a different page must not inherit this page's footnotes
    assert next_page.footnotes == []
    # page-broadcast footnotes are the SAME object shared across chunks, not copies
    # (this is the property the new mineru_adapter.py's footnote-id dedup relies on)
    assert results.footnotes[0] is details.footnotes[0]


HEADING_REVIEW_BLOCKS = [
    {"type": "text", "text": "1. Overview", "text_level": 1, "page_idx": 0, "bbox": [10, 10, 100, 20]},
    {"type": "text", "text": "Overview body text.", "page_idx": 0},
    {"type": "unknown_future_type", "text": "Some content MinerU tags with a type we don't recognize yet.", "page_idx": 0},
    {"type": "header", "text": "Running Document Title", "page_idx": 0},
    {"type": "page_number", "text": "3", "page_idx": 0},
]


def test_heading_correction_updates_chunk_title():
    blocks = [{**b, "_index": i} for i, b in enumerate(HEADING_REVIEW_BLOCKS)]
    blocks[0]["_override_markdown"] = "1. Corrected Overview Title"
    chunks = build_chunks(blocks, doc_title="Doc")

    titles = [c.title for c in chunks]
    assert "1. Corrected Overview Title" in titles
    assert "1. Overview" not in titles


# Real MinerU output: an entire lettered/roman enumeration grouped into ONE
# "list" block with a flat list_items array — no per-item type/level/bbox at all.
GROUPED_LIST_BLOCKS = [
    {"_index": 0, "type": "text", "text": "1.1 General Disclosure Obligation", "text_level": 1, "page_idx": 0},
    {"_index": 1, "type": "text", "text": "Intro sentence before the lettered points.", "page_idx": 0},
    {
        "_index": 2,
        "type": "list",
        "page_idx": 0,
        "bbox": [100, 200, 600, 400],
        "list_items": [
            "(a) Disclosure obligation applicable to any person generally",
            "When a person, directly or indirectly, acting together with others,",
            "(i) acquires or sells securities of a public issuer;",
            "(ii) changes its direct or indirect holding in a public issuer;",
            "(b) Disclosure obligation applicable to controlling shareholder",
        ],
    },
    {"_index": 3, "type": "text", "text": "1.2 Reporting Deadline", "text_level": 1, "page_idx": 0},
]


def test_expand_list_blocks_splits_items_with_composite_index_and_sliced_bbox():
    expanded = expand_list_blocks(GROUPED_LIST_BLOCKS)
    list_items = [b for b in expanded if b.get("_from_list")]
    assert len(list_items) == 5
    assert list_items[0]["_index"] == "2:0"
    assert list_items[0]["page_idx"] == 0
    assert list_items[0]["type"] == "text"

    boxes = [tuple(b["bbox"]) for b in list_items]
    assert len(set(boxes)) == len(boxes), "each list item should get a distinct bbox slice"

    parent_bbox = GROUPED_LIST_BLOCKS[2]["bbox"]
    for b in list_items:
        x0, y0, x1, y1 = b["bbox"]
        assert x0 == parent_bbox[0] and x1 == parent_bbox[2]
        assert parent_bbox[1] <= y0 < y1 <= parent_bbox[3]
    assert all(list_items[i]["bbox"][1] <= list_items[i + 1]["bbox"][1] for i in range(len(list_items) - 1))


def test_estimate_item_bbox_slices_evenly():
    assert _estimate_item_bbox([0, 0, 100, 100], 0, 4) == [0, 0, 100, 25]
    assert _estimate_item_bbox([0, 0, 100, 100], 3, 4) == [0, 75, 100, 100]
    assert _estimate_item_bbox(None, 0, 4) is None
    assert _estimate_item_bbox([0, 0, 100, 100], 0, 0) == [0, 0, 100, 100]


def test_expand_list_blocks_leaves_corrected_list_block_intact():
    corrected = [{**b, "_override_markdown": "custom replacement"} if b["_index"] == 2 else b for b in GROUPED_LIST_BLOCKS]
    expanded = expand_list_blocks(corrected)
    assert not any(b.get("_from_list") for b in expanded)
    list_block = next(b for b in expanded if b["_index"] == 2)
    assert list_block["type"] == "list"


def test_build_chunks_recovers_hierarchy_from_grouped_list_block():
    chunks = build_chunks(GROUPED_LIST_BLOCKS, doc_title="Doc")
    by_title = {c.title: c for c in chunks}

    obligation = by_title["1.1 General Disclosure Obligation"]
    point_a = by_title["(a) Disclosure obligation applicable to any person generally"]
    point_i = by_title["(i) acquires or sells securities of a public issuer;"]
    point_ii = by_title["(ii) changes its direct or indirect holding in a public issuer;"]
    point_b = by_title["(b) Disclosure obligation applicable to controlling shareholder"]
    deadline = by_title["1.2 Reporting Deadline"]

    assert point_a.level == obligation.level + 1 and point_a.parent_order == obligation.order
    assert point_i.level == point_a.level + 1 and point_i.parent_order == point_a.order
    assert point_ii.parent_order == point_a.order
    assert point_b.level == point_a.level and point_b.parent_order == obligation.order
    assert deadline.level == obligation.level and deadline.parent_order == obligation.parent_order


def test_slugify_caps_length_so_filenames_never_exceed_filesystem_limits():
    long_heading = (
        "(i) Articles or deed of incorporation and/or corporate bylaws and amendments thereto; "
        "in the event a foreign legal entity manages third party assets, the documents evidencing "
        "the creation of the assets under management, identifying all the parties involved, must be filed."
    )
    slug = slugify(long_heading)
    filename = f"0049_l6_{slug}.md"
    assert len(filename.encode("utf-8")) < 255
    assert slug.startswith("i-articles-or-deed-of-incorporation")
    assert not slug.endswith("-")


def test_slugify_short_text_unaffected():
    assert slugify("1.1 Background") == "1-1-background"


RECLASSIFY_BLOCKS = [
    {"type": "text", "text": "1. Introduction", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "Intro body.", "page_idx": 0},
    {"type": "text", "text": "This looks like a heading but isn't.", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "1.1 Real Subsection", "text_level": 2, "page_idx": 0},
    {"type": "text", "text": "Subsection body.", "page_idx": 0},
    {"type": "text", "text": "A plain sentence that should actually be a heading.", "page_idx": 0},
    {"type": "text", "text": "Body after the missed heading.", "page_idx": 0},
]


def test_force_heading_false_demotes_to_body_and_reparents_its_children():
    blocks = [{**b, "_index": i} for i, b in enumerate(RECLASSIFY_BLOCKS)]
    blocks[2]["_force_heading"] = False
    chunks = build_chunks(blocks, doc_title="Doc")
    titles = {c.title for c in chunks}

    assert "This looks like a heading but isn't." not in titles
    intro = next(c for c in chunks if c.title == "1. Introduction")
    subsection = next(c for c in chunks if c.title == "1.1 Real Subsection")
    assert subsection.parent_order == intro.order


def test_force_heading_true_promotes_body_text_with_explicit_level():
    blocks = [{**b, "_index": i} for i, b in enumerate(RECLASSIFY_BLOCKS)]
    blocks[5]["_force_heading"] = True
    blocks[5]["_force_level"] = 2
    chunks = build_chunks(blocks, doc_title="Doc")

    promoted = next(c for c in chunks if c.title == "A plain sentence that should actually be a heading.")
    assert promoted.level == 2
    subsection = next(c for c in chunks if c.title == "1.1 Real Subsection")
    assert promoted.parent_order == subsection.parent_order


def test_force_heading_true_without_explicit_level_nests_under_current_context():
    blocks = [{**b, "_index": i} for i, b in enumerate(RECLASSIFY_BLOCKS)]
    blocks[5]["_force_heading"] = True
    chunks = build_chunks(blocks, doc_title="Doc")

    promoted = next(c for c in chunks if c.title == "A plain sentence that should actually be a heading.")
    subsection = next(c for c in chunks if c.title == "1.1 Real Subsection")
    assert promoted.level == subsection.level + 1
    assert promoted.parent_order == subsection.order


PAGE_NOTE_MULTI_CHUNK_BLOCKS = [
    {"type": "text", "text": "1. Section One", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "First section body.", "page_idx": 0},
    {"type": "text", "text": "1.1 Subsection", "text_level": 2, "page_idx": 0},
    {"type": "text", "text": "Subsection body.", "page_idx": 0},
    {"type": "header", "text": "Acme Corp — Confidential", "page_idx": 0},
    {"type": "text", "text": "2. Section Two", "text_level": 1, "page_idx": 1},
    {"type": "text", "text": "Second section body, on a different page.", "page_idx": 1},
]


def test_page_notes_attach_to_every_chunk_on_that_page_only():
    blocks = [{**b, "_index": i} for i, b in enumerate(PAGE_NOTE_MULTI_CHUNK_BLOCKS)]
    chunks = build_chunks(blocks, doc_title="Doc")
    by_title = {c.title: c for c in chunks}

    section_one = by_title["1. Section One"]
    subsection = by_title["1.1 Subsection"]
    section_two = by_title["2. Section Two"]

    assert [n["text"] for n in section_one.page_notes] == ["Acme Corp — Confidential"]
    assert [n["text"] for n in subsection.page_notes] == ["Acme Corp — Confidential"]
    assert section_two.page_notes == []


def test_heading_only_leaf_chunk_has_no_own_blocks():
    # Real document shape: a lettered/roman list item promoted to its own heading
    # (via expand_list_blocks) with no separate body block — the heading text IS
    # the entire substantive content (the new adapter relies on this: such a
    # Section legitimately has empty `elements`, same as today's model shape).
    blocks = [
        {"_index": 0, "type": "text", "text": "1.1 Obligation", "text_level": 1, "page_idx": 0},
        {
            "_index": 1,
            "type": "list",
            "page_idx": 0,
            "bbox": [0, 0, 100, 100],
            "list_items": [
                "(i) acquires or sells securities of a public issuer;",
                "(ii) changes its direct or indirect holding in a public issuer;",
            ],
        },
    ]
    chunks = build_chunks(blocks, doc_title="Doc")
    point_i = next(c for c in chunks if c.title == "(i) acquires or sells securities of a public issuer;")
    assert point_i.blocks == []
