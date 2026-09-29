"""extract/mineru_extract.py: PDF-only. Hierarchy is rebuilt with the ported
MinerU2.5 "Document Chunker" logic (extract/mineru_chunking.py), then the
aosphere adapter layers on three policies these tests pin:

  1. Real clause keys matching the legacy convention: A, A1, A1.1, A1.6.1 —
     Part letter + deepest decimal marker + paren markers.
  2. Per-product depth cap (shared with legacy's max_heading_level): decimal
     clauses kept up to the cap; paren items "(a)"/"(i)" and over-cap decimals
     fold into their nearest kept clause's BODY.
  3. Table-of-contents exclusion (entries with trailing page numbers / sharing
     the "Contents" page are dropped from content)."""

from pathlib import Path

import pytest

from aosphere_core_index.extract.mineru_extract import blocks_to_doc

# product "Shareholding Disclosure" -> depth cap 3 (styles.max_heading_level)
_META = {"DOCID": "x", "DOCNAME": "Doc", "RENDERSTYLENAME": "Shareholding Disclosure",
         "JURISDICTIONNAME": "Test"}


def _doc(blocks, **meta):
    return blocks_to_doc(blocks, doc_meta={**_META, **meta}, source_key="k")


def _text(text, **extra):
    return {"type": "text", "text": text, **extra}


def _by_key(doc):
    return {s.key: s for s in doc.sections}


def _body(section):
    return "\n".join(e.text for e in section.elements)


# --- root & clause keys -----------------------------------------------------

def test_synthetic_root_carries_doc_title_and_preamble():
    doc = _doc([_text("preamble, before any heading"), _text("1. Intro", text_level=1)],
               DOCNAME="My Doc")
    root = doc.sections[0]
    assert root.level == 0 and root.title == "My Doc" and root.key == "0"
    assert "preamble" in root.elements[0].text


def test_decimal_clause_keys_without_a_part():
    doc = _doc([_text("1. First", text_level=1), _text("2. Second", text_level=1)])
    keys = _by_key(doc)
    assert "1" in keys and "2" in keys
    assert keys["2"].parent_id == doc.sections[0].id  # both under the root


def test_part_qualified_keys_never_collide_across_parts():
    # "1." under Part A is A1; "1." under Part B is B1 — the Part prefix keeps
    # same-numbered clauses in different Parts globally unique.
    doc = _doc([
        _text("A. Substantial Shareholding", text_level=1),
        _text("1. Overview", text_level=1),
        _text("1.1 General Disclosure", text_level=1),
        _text("B. Sensitive Industries", text_level=1),
        _text("1. Restrictions", text_level=1),
    ])
    keys = _by_key(doc)
    assert set(keys) >= {"0", "A", "A1", "A1.1", "B", "B1"}
    assert keys["A1"].part_letter == "A" and keys["B1"].part_letter == "B"
    assert keys["A1"].parent_id == keys["A"].id
    assert keys["B1"].parent_id == keys["B"].id


def test_depth_recovered_when_mineru_flattens_all_headings_to_level_1():
    doc = _doc([
        _text("A. Substantial Shareholding", text_level=1),
        _text("1. Overview", text_level=1),
        _text("1.1 General Disclosure", text_level=1),
        _text("1.6.1 Current sources", text_level=1),
    ])
    keys = _by_key(doc)
    assert keys["A1"].parent_id == keys["A"].id
    assert keys["A1.1"].parent_id == keys["A1"].id
    # "1.6.1" is decimal depth 3 == cap, still a kept clause
    assert "A1.6.1" in keys


# --- depth cap + fold -------------------------------------------------------

def test_paren_items_fold_into_the_enclosing_clause_body():
    doc = _doc([
        _text("A. Substantial Shareholding", text_level=1),
        _text("1. Overview", text_level=1),
        _text("1.1 General Disclosure", text_level=1),
        _text("(a) applicable to any person", text_level=1),
        _text("connector sentence"),  # no text_level -> body, stays under (a)
        _text("(i) acquires securities;", text_level=1),
        _text("(b) applicable to controller", text_level=1),
        _text("1.2 Exemptions", text_level=1),
    ])
    keys = _by_key(doc)
    # no paren clause becomes its own section
    assert not any("(" in k for k in keys)
    a11 = keys["A1.1"]
    body = _body(a11)
    assert "(a) applicable to any person" in body
    assert "(i) acquires securities;" in body
    assert "(b) applicable to controller" in body
    # 1.2 is a real sibling clause, not folded
    assert "A1.2" in keys and keys["A1.2"].parent_id == keys["A1"].id


def test_unmarked_heading_stays_inside_its_clause_and_keeps_the_part_prefix():
    # Regression: an unmarked callout ("Aviation") that MinerU tags text_level=1
    # used to pop the whole stack (MinerU2.5 flattens every heading to level 1),
    # ejecting itself out to a top-level sibling of B AND stripping the B prefix
    # from every clause after it (B1.2 -> "1.2", B2 -> "2"). It must instead nest
    # under the open clause and leave the rest of B intact.
    doc = _doc([
        _text("B. Sensitive Industries", text_level=1),
        _text("1. Restrictions", text_level=1),
        _text("1.1 Summary Matrix", text_level=1),
        _text("Aviation", text_level=1),                 # unmarked callout inside B1.1
        _text("Air transport rules apply here."),        # its body
        _text("1.2 Screening", text_level=1),
        _text("2. Sanctions", text_level=1),
    ])
    keys = _by_key(doc)
    aviation = next(s for s in doc.sections if s.title.strip() == "Aviation")
    assert aviation.parent_id == keys["B1.1"].id       # nested inside B1.1
    assert aviation.parent_id != doc.sections[0].id    # NOT a top-level sibling of B
    assert "Air transport rules apply here." in _body(aviation)
    # the cascade is intact — clauses after the callout keep their B prefix
    assert keys["B1.2"].parent_id == keys["B1"].id
    assert keys["B2"].parent_id == keys["B"].id


def test_over_cap_decimal_folds_into_nearest_kept_clause():
    # cap is 3; "1.1.1.1" is depth 4 -> folds into its nearest kept ancestor.
    doc = _doc([_text("A. Part", text_level=1), _text("1.1.1 Kept", text_level=1),
                _text("1.1.1.1 Too Deep", text_level=1)])
    keys = _by_key(doc)
    assert "A1.1.1" in keys
    assert "A1.1.1.1" not in keys
    assert "1.1.1.1 Too Deep" in _body(keys["A1.1.1"])


def test_higher_cap_product_keeps_deeper_decimals():
    doc = _doc([_text("A. Part", text_level=1), _text("1.1.1.1 Deep", text_level=1)],
               RENDERSTYLENAME="Data Privacy")  # default cap 8
    assert "A1.1.1.1" in _by_key(doc)


def test_grouped_list_block_expanded_then_paren_items_folded():
    doc = _doc([
        _text("1.1 General Disclosure", text_level=1),
        {"type": "list", "page_idx": 0, "bbox": [100, 200, 600, 400], "list_items": [
            "(a) applicable to any person",
            "When a person, acting together with others,",
            "(i) acquires securities;",
            "(b) applicable to controller",
        ]},
        _text("1.2 Reporting Deadline", text_level=1),
    ])
    keys = _by_key(doc)
    assert "1.1" in keys and "1.2" in keys
    assert not any("(" in k for k in keys)  # paren items folded, not sections
    body = _body(keys["1.1"])
    assert "(a) applicable to any person" in body
    assert "When a person, acting together with others," in body
    assert "(i) acquires securities;" in body


# --- tables, footnotes, furniture -------------------------------------------

def test_table_rendered_as_pipe_text_with_caption_and_footnote():
    doc = _doc([
        _text("1. Top", text_level=1),
        {"type": "table", "table_body": "<table><tr><td>H1</td><td>H2</td></tr></table>",
         "table_caption": ["Table 1"], "table_footnote": ["source: x"], "page_idx": 0},
    ])
    table_el = next(e for e in _by_key(doc)["1"].elements if e.kind == "table")
    assert "H1 | H2" in table_el.text and "Table 1" in table_el.text and "source: x" in table_el.text


def test_page_footnotes_broadcast_to_every_section_on_that_page():
    doc = _doc([
        _text("1. Results", text_level=1, page_idx=0),
        _text("body", page_idx=0),
        _text("1.1 Details", text_level=1, page_idx=0),
        {"type": "page_footnote", "text": "1 Baseline per Appendix A.", "page_idx": 0},
        {"type": "page_footnote", "text": "2 See Smith et al.", "page_idx": 0},
        _text("2. Next Page", text_level=1, page_idx=1),
    ])
    keys = _by_key(doc)
    assert doc.footnotes == {"1": "1 Baseline per Appendix A.", "2": "2 See Smith et al."}
    assert keys["1"].footnote_ids == ["1", "2"]
    assert keys["1.1"].footnote_ids == ["1", "2"]
    assert keys["2"].footnote_ids == []


def test_footnotes_cover_every_page_a_clause_spans():
    # A clause whose content runs across pages 0-2 (including a folded sub-item
    # on page 2) must collect the footnotes on ALL of those pages; a later clause
    # gets only its own page's.
    doc = _doc([
        _text("1. Long Clause", text_level=1, page_idx=0),
        _text("body on page 0", page_idx=0),
        {"type": "page_footnote", "text": "fn A (p0)", "page_idx": 0},
        _text("more body on page 1", page_idx=1),
        {"type": "page_footnote", "text": "fn B (p1)", "page_idx": 1},
        _text("(a) sub-item", text_level=1, page_idx=2),   # folds into clause 1, on page 2
        {"type": "page_footnote", "text": "fn C (p2)", "page_idx": 2},
        _text("2. Next Clause", text_level=1, page_idx=3),
        {"type": "page_footnote", "text": "fn D (p3)", "page_idx": 3},
    ])
    keys = _by_key(doc)
    assert {doc.footnotes[f] for f in keys["1"].footnote_ids} == {"fn A (p0)", "fn B (p1)", "fn C (p2)"}
    assert {doc.footnotes[f] for f in keys["2"].footnote_ids} == {"fn D (p3)"}


def test_folded_subitems_pull_their_footnotes_up_to_the_kept_clause():
    # A footnote on a later page that only the folded sub-item spans must still
    # reach the kept clause it folds into.
    doc = _doc([
        _text("1.1 Clause", text_level=1, page_idx=0),
        _text("(a) sub-item", text_level=1, page_idx=1),
        {"type": "page_footnote", "text": "fn on page 1", "page_idx": 1},
    ])
    assert _by_key(doc)["1.1"].footnote_ids == ["1"]
    assert doc.footnotes == {"1": "fn on page 1"}


def test_repeating_header_mistagged_as_heading_is_dropped_as_furniture():
    # A backend (pipeline/auto) can mis-tag a running header ("aosphere") as
    # text + heading-level, once per page. Repeated across many pages it must be
    # treated as page furniture, not become dozens of spurious clause sections
    # (the vlm backend avoids this by tagging it type=header in the first place).
    blocks = [_text("1. Real Clause", text_level=1, page_idx=0)]
    for p in range(6):
        blocks.append(_text("aosphere", text_level=2, page_idx=p))
        blocks.append(_text(f"body on page {p}", page_idx=p))
    doc = _doc(blocks)
    assert not any(s.title.strip().lower() == "aosphere" for s in doc.sections)
    assert not any(e.text.strip().lower() == "aosphere" for s in doc.sections for e in s.elements)
    assert "1" in _by_key(doc)  # the genuine clause is untouched


def test_running_page_furniture_dropped_from_body():
    doc = _doc([
        _text("1. Top", text_level=1, page_idx=0),
        {"type": "header", "text": "Acme Corp — Confidential", "page_idx": 0},
        {"type": "page_number", "text": "3", "page_idx": 0},
        {"type": "footer", "text": "running footer", "page_idx": 0},
    ])
    assert _by_key(doc)["1"].elements == []


def test_toc_run_excluded_from_content():
    doc = _doc([
        _text("Contents", text_level=1, page_idx=0),
        _text("A. Substantial Shareholding 3", text_level=1, page_idx=0),
        _text("B. Sensitive Industries 34", text_level=1, page_idx=0),
        _text("A. Substantial Shareholding", text_level=1, page_idx=1),  # real body Part
        _text("1. Overview", text_level=1, page_idx=1),
    ])
    keys = _by_key(doc)
    titles = {s.title for s in doc.sections}
    assert "Contents" not in titles
    assert "A. Substantial Shareholding 3" not in titles  # TOC line dropped
    assert "A" in keys and "A1" in keys                    # real body Part kept
    assert not any(s.key != "0" and s.title.rstrip()[-1:].isdigit() for s in doc.sections)


# --- real cached PDF sanity -------------------------------------------------

@pytest.mark.parametrize("filename", ["Argentina_content_list.json"])
def test_real_cached_pdf_sanity(filename):
    import json
    import re
    files = [f for f in Path("data/.mineru_cache").rglob(filename) if "v2" not in f.name]
    if not files:
        pytest.skip("no cached content_list.json available")
    blocks = json.loads(files[0].read_text())
    doc = blocks_to_doc(blocks, doc_meta={**_META, "DOCNAME": "Argentina"}, source_key="k")
    assert len(doc.sections) > 0
    ids = {s.id for s in doc.sections}
    assert all(s.parent_id is None or s.parent_id in ids for s in doc.sections)
    assert len({s.key for s in doc.sections}) == len(doc.sections)  # keys unique
    # real clause keys: Part-qualified ones exist, none exceed the depth cap
    assert any(re.fullmatch(r"[A-E]\d(?:\.\d+)*", s.key) for s in doc.sections)
    assert all(s.key == "0" or s.key.count(".") + 1 <= 3 or not s.key[:1].isalpha()
               or "(" in s.key for s in doc.sections if s.part_letter)
    # every footnote id referenced exists in the footnote table
    assert all(fid in doc.footnotes for s in doc.sections for fid in s.footnote_ids)
