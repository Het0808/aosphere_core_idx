"""A section that kept its heading and lost its body to a sibling.

The pipeline's completeness checks all ask the same question — "is this text
anywhere in the tree?" — and for this defect the answer is yes, so nothing was
charged for it. On 124_Marketing_Restrictions/ADGM__170680 four of section 8's
subsections are published as headings with no body (MinerU merged their rows into
the table under 8.8) and the document gated PASS at 95.7 with completeness 96.1
and placement 100.0.

Two things make the test possible, and both are pinned here.

The first is measuring a section against its OWN slice of the source — from its
heading to the next one — rather than against its declared page range. Short
subsections routinely share a page, so a page-range comparison reports each of
them as missing everything its siblings correctly hold, and a section that kept
its 24-word body looks identical to one that kept none of its 219.

The second is refusing to name a section hollow unless the body is findable under
a NAMED sibling. Without that, the check also fires on cover pages, printed
contents listings and "[SECTION INTENTIONALLY LEFT BLANK]" — nodes that kept
nothing because there was nothing to keep. It is also what keeps this dimension
distinct from Completeness: content that is hollow AND genuinely gone is loss,
and loss is already counted somewhere else.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_content_localized import (  # noqa: E402
    HOLLOW_MAX_RETENTION, HOLLOW_MIN_BODY, _document_order, _find_sub,
    _hollow_finding, _own_source_slice, _title_tokens,
)


def toks(s: str) -> list[str]:
    return s.split()


def body(word: str, n: int = HOLLOW_MIN_BODY + 20) -> list[str]:
    """A body long enough to be worth having, distinctive enough to attribute."""
    return [f"{word}{i}" for i in range(n)]


def entry(title: str, pages=(0, 0), abs_pos=0) -> dict:
    return {"title_tokens": toks(title), "pages": pages, "abs_pos": abs_pos}


# ---- locating a section's own body ---------------------------------------

def test_slice_runs_from_this_heading_to_the_next():
    pdf = toks("8 9 arranging is it permitted 8 10 subscription documentation other text")
    got = _own_source_slice(pdf, entry("8 9 arranging"), toks("8 10 subscription documentation"))
    assert got == toks("is it permitted")


def test_slice_runs_to_the_end_when_nothing_follows():
    pdf = toks("8 12 relocation of investor a what restrictions apply")
    got = _own_source_slice(pdf, entry("8 12 relocation of investor"), None)
    assert got == toks("a what restrictions apply")


def test_a_heading_absent_from_the_source_yields_no_slice():
    """A title the tree normalised or invented. No slice, no finding — this
    check would rather miss a hollow section than fabricate one."""
    pdf = toks("some text that never contains the heading")
    assert _own_source_slice(pdf, entry("8 9 arranging"), None) is None


def test_an_unplaced_heading_yields_no_slice():
    e = entry("8 9 arranging")
    e["abs_pos"] = None
    assert _own_source_slice(toks("8 9 arranging body here"), e, None) is None


# ---- boundaries come from the source, not from file order ----------------

def test_document_order_places_headings_by_where_the_source_prints_them():
    """File order is lexicographic, which sorts 8.10 before 8.2. The boundary a
    section is cut at has to come from the document."""
    pages = [toks("cover"), toks("8 2 telephone body 8 10 subscription body")]
    entries = [entry("8 10 subscription", pages=(1, 1)), entry("8 2 telephone", pages=(1, 1))]
    _document_order(entries, pages)
    assert entries[1]["abs_pos"] < entries[0]["abs_pos"]


def test_a_one_token_heading_is_never_placed():
    """This corpus's cover page yields a section titled "Into". Matched as a
    boundary it truncates every section in the document to nothing, because the
    word occurs inside ordinary prose."""
    pages = [toks("marketing of funds into and within the jurisdiction")]
    entries = [entry("into", pages=(0, 0))]
    _document_order(entries, pages)
    assert entries[0]["abs_pos"] is None


# ---- the hollow test itself ----------------------------------------------

def test_a_section_that_kept_its_body_is_not_hollow():
    own = body("clause")
    others = {"sibling.md": body("unrelated")}
    assert _hollow_finding("s.md", (1, 2), own, own, others) is None


def test_a_section_whose_body_is_under_a_sibling_is_hollow():
    own = body("clause")
    scaffolding = toks("8 9 arranging the rows for this section are inside the table above")
    got = _hollow_finding("8.9.md", (60, 60), own, scaffolding, {"8.8.md": own})
    assert got is not None
    assert got["absorbed_by"] == ["8.8.md"]
    assert got["retention_pct"] <= HOLLOW_MAX_RETENTION * 100


def test_a_body_nowhere_in_the_tree_is_loss_not_a_hollow_section():
    """Content that is both missing from its section AND absent from the tree is
    a Completeness finding. Reporting it here too would double-charge it and
    blur what this dimension means."""
    own = body("clause")
    assert _hollow_finding("8.9.md", (60, 60), own, toks("8 9 arranging"),
                           {"8.8.md": body("unrelated")}) is None


def test_a_genuinely_short_section_is_not_hollow():
    """'[SECTION INTENTIONALLY LEFT BLANK]' and cover-page fragments keep almost
    nothing because the source prints almost nothing under them."""
    own = toks("section intentionally left blank")
    assert _hollow_finding("9.md", (17, 17), own, toks("9 section"), {"10.md": own}) is None


def test_page_furniture_alone_does_not_rescue_a_hollow_section():
    """Retention is a ratio, not a token count. A file holding nothing still
    matches the running header that survived boilerplate stripping, plus its own
    title — 125 such tokens on ADGM's section 7.3, against a 1,690-token body it
    does not contain a word of."""
    own = body("clause", 1690)
    leaked = own[:120] + toks("aosphere confidential")
    got = _hollow_finding("7.3.md", (44, 49), own, leaked, {"7.2.md": own})
    assert got is not None
    assert got["kept_tokens"] >= 120


def test_find_sub_respects_the_start_offset():
    hay = toks("a b c a b c")
    assert _find_sub(hay, toks("a b"), 0) == 0
    assert _find_sub(hay, toks("a b"), 1) == 3
    assert _find_sub(hay, toks("x y"), 0) is None


def test_title_tokens_reads_the_h1_only():
    raw = "# 8.9 Arranging\n\n*Source: `x.pdf`, page 60*\n\n## Not the title\n"
    assert _title_tokens(raw) == toks("8 9 arranging")


# ---- section census: promised vs delivered -------------------------------
#
# The hollow test above catches a section built empty. It cannot catch a section
# never built at all — there is no file to measure. That is the commoner and
# worse failure: HEAD's Stage 1 on 124/ADGM__170680 builds 25 of the 39 sections
# the outline promises, dropping 8.1 and 8.6-8.12 outright, because their
# headings are printed inside a table region deferred to MinerU.
#
# The answer key has to be the PDF's own outline. The manifest the nesting checks
# use is Stage 1's record of what it BUILT, so a heading Stage 1 never saw is
# absent from the key as well as from the tree, and the comparison reports 44 of
# 44 on a document missing fourteen sections.

from check_heading_hierarchy import _section_census, _slug_key  # noqa: E402


class _FakeToc:
    """Stands in for a PDF so the census can be driven without one."""

    def __init__(self, entries):
        self.entries = entries

    def install(self, monkeypatch, tree_root):
        import check_heading_hierarchy as H
        monkeypatch.setattr(H, "_outline_headings", lambda *_a, **_k: self.entries)


def promised(title, level=2, page=5):
    return {"level": level, "title": title, "page": page}


def node(name, path=None, kind="file"):
    return {"kind": kind, "path": path or f"{name}.md", "depth": 1,
            "name": name, "parents": []}


def test_a_section_the_outline_names_but_the_tree_lacks_is_missing(monkeypatch, tmp_path):
    _FakeToc([promised("8.9 Arranging"), promised("8.8 Investment Advice")]).install(
        monkeypatch, tmp_path)
    c = _section_census(tmp_path, tmp_path, [node("8.8-investment-advice")], max_depth=3)
    assert c["promised"] == 2 and c["delivered"] == 1
    assert [m["title"] for m in c["missing"]] == ["8.9 Arranging"]


def test_a_fully_built_tree_reports_no_missing(monkeypatch, tmp_path):
    _FakeToc([promised("8.9 Arranging")]).install(monkeypatch, tmp_path)
    c = _section_census(tmp_path, tmp_path, [node("8.9-arranging")], max_depth=3)
    assert c["missing_count"] == 0 and c["extra_count"] == 0


def test_a_node_the_outline_never_names_is_reported_as_extra(monkeypatch, tmp_path):
    """The other direction: a boundary the document does not have splits one
    clause across two nodes."""
    _FakeToc([promised("8.9 Arranging", page=5)]).install(monkeypatch, tmp_path)
    (tmp_path / "invented.md").write_text("# Invented\n\n*Source: `s.pdf`, page 9*\n")
    c = _section_census(tmp_path, tmp_path,
                        [node("8.9-arranging"), node("invented")], max_depth=3)
    assert [e["path"] for e in c["extra"]] == ["invented.md"]


def test_front_matter_before_the_outline_starts_is_not_extra(monkeypatch, tmp_path):
    """A publisher who bookmarks from page 3 leaves the cover un-promised. Stage 1
    rebuilds it on purpose rather than dropping its content, so charging for it
    would report a deliberate recovery as a defect."""
    _FakeToc([promised("1 Background", page=2)]).install(monkeypatch, tmp_path)
    (tmp_path / "cover.md").write_text("# Cover\n\n*Source: `s.pdf`, page 1*\n")
    c = _section_census(tmp_path, tmp_path,
                        [node("1-background"), node("cover")], max_depth=3)
    assert c["extra_count"] == 0


def test_overview_scaffolding_is_not_extra(monkeypatch, tmp_path):
    """00-overview.md is emitted for any section with both intro prose and
    children. It is not a claim that the document has a section by that name."""
    _FakeToc([promised("1 Background", page=2)]).install(monkeypatch, tmp_path)
    c = _section_census(tmp_path, tmp_path,
                        [node("1-background"),
                         node("00-overview", path="1-background/00-overview.md")],
                        max_depth=3)
    assert c["extra_count"] == 0


def test_headings_deeper_than_the_depth_cap_are_not_promised(monkeypatch, tmp_path):
    """Stage 1 runs with --depth 3; deeper outline entries legitimately collapse
    into their parent file and were never going to be nodes."""
    _FakeToc([promised("deep one", level=5, page=2)]).install(monkeypatch, tmp_path)
    c = _section_census(tmp_path, tmp_path, [], max_depth=3)
    assert c["available"] is False or c["promised"] == 0


def test_no_outline_means_no_census(monkeypatch, tmp_path):
    """Without an outline there is no independent statement of what the document
    contains, so any count would be the extraction grading its own work."""
    _FakeToc([]).install(monkeypatch, tmp_path)
    assert _section_census(tmp_path, tmp_path, [node("x")], max_depth=3)["available"] is False


def test_slug_key_lines_up_a_title_with_its_padded_node_name():
    assert _slug_key("8.9 Arranging") == _slug_key("8.9-arranging")
    assert _slug_key("4. Aggregation") == _slug_key("04-aggregation")


# ---- the section map -----------------------------------------------------
#
# A list of missing titles says WHAT is gone. The map says WHERE: four
# consecutive holes under one heading is a table that swallowed a branch, one
# hole on its own is a heading that failed to match. The three states have to be
# distinguishable — "built" and "built with nothing in it" look identical in
# every other view the scorecard offers, and the second is the one that survives
# every existing dimension.

from check_scorecard import SECTION_MAP_FULL_MAX, _section_map_rows  # noqa: E402


def row(title, level=1, status="built"):
    return {"title": title, "level": level, "status": status, "page": 1,
            "node": None if status == "missing" else f"{title}.md"}


def test_a_short_outline_is_shown_whole():
    """A 39-section memorandum is readable entire, holes in context."""
    smap = [row(f"s{i}") for i in range(10)] + [row("gone", status="missing")]
    rows, elided = _section_map_rows(smap)
    assert rows == smap and elided == 0


def test_a_long_outline_shows_only_branches_with_a_hole():
    """The privacy surveys promise 471 sections. A wall of ✓ is where a reader
    stops looking, which defeats the point of drawing the map."""
    smap = [row(f"s{i}") for i in range(SECTION_MAP_FULL_MAX + 20)]
    smap[50] = row("empty one", status="hollow")
    rows, elided = _section_map_rows(smap)
    assert [r["title"] for r in rows] == ["empty one"]
    assert elided == len(smap) - 1


def test_an_elided_map_keeps_the_ancestors_of_each_hole():
    """A subsection has to read under the section it belongs to, or the map
    cannot answer the question it exists for."""
    smap = [row("PART A", level=1), row("A.1", level=2), row("A.1.a", level=3)]
    smap += [row(f"filler{i}", level=2) for i in range(SECTION_MAP_FULL_MAX + 5)]
    smap[2] = row("A.1.a", level=3, status="missing")
    rows, _ = _section_map_rows(smap)
    assert [r["title"] for r in rows] == ["PART A", "A.1", "A.1.a"]


def test_every_status_a_section_can_have_is_marked():
    from check_scorecard import MARKS
    assert set(MARKS) == {"built", "hollow", "missing"}
    assert len({m for m, _c in MARKS.values()}) == 3   # visually distinct
