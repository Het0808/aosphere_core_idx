"""Where an unclaimed MinerU table block is allowed to land, and what it may displace.

Every bug this pass has had came from deciding ownership by a PROXY instead of by the
content: by the block's anchor page, by "how many regions claim that page", or by a bare
normalised row signature. Each proxy was right often enough to look like a fix and wrong
on exactly the pages where ownership is contested — the ones carrying a section boundary.

The shapes below are the real ones, from the runs on disk:

  Saudi Arabia__174731 p16   MinerU's content_list MERGES a page-spanning table into one
                             entry, so the reverse-enquiry table covering pages 16-26
                             arrived as a single 11,911-char block anchored on p16. The
                             only region claiming p16 is section THREE's three-row tail,
                             so "sole candidate" filed 13 rows of section 5 under section
                             3 — and then deleted 7 of them from table_007, the region
                             that had extracted them correctly.
  Malta__181816 p48          a 58-char orphan holding one "Questions | Answers" row. That
                             signature opens every section table in these memos, so
                             matching on it deleted the header from EIGHT other tables.
  Switzerland__176654 p35    a genuine misfiling: section 7.5's tail sits inside section
                             8's region, which spans the page. This one must still move.

Measured over the MRAM corpus before this change: 16 placements, 11 of which removed rows
from a region that already held them. `removed_from` being non-empty is itself evidence
that nothing was lost — a genuinely lost orphan appears nowhere else.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hybrid_extract as he  # noqa: E402

HEADER = "<tr><td>Questions</td><td>Answers</td></tr>"
# Long enough to clear ORPHAN_ROW_MIN_CHARS — a row shorter than that is not evidence.
Q_A = ("<tr><td>(a) Is marketing/selling of a Fund permitted following an unsolicited "
       "approach by an investor to an OFI?</td><td>There are two main restrictions that "
       "an OFI needs to consider when seeking to rely on this.</td></tr>")
Q_B = ("<tr><td>(i) What constitutes a reverse-enquiry within the meaning of the "
       "regulations?</td><td>No CMA guidance has been issued over and above what is "
       "contained in the FAQ published by the regulator.</td></tr>")
Q_C = ("<tr><td>(e) Please provide the name of the competent regulator and set out its "
       "statutory basis.</td><td>The Capital Market Authority, established under the "
       "Capital Market Law of 2003.</td></tr>")


def _mktables(tmp_path, spec):
    """spec: {table_id: [row_html, ...]} -> a tables/ dir shaped like stage 2 writes it."""
    root = tmp_path / "tables"
    for tid, rows in spec.items():
        d = root / tid
        d.mkdir(parents=True)
        (d / "table.md").write_text(
            f"### {tid}\n\n<table>{''.join(rows)}</table>\n")
        (d / "status.json").write_text(json.dumps({"rows": len(rows), "cols": 2}))
    return root


def _rows_of(root, tid):
    return (root / tid / "table.md").read_text()


def _orphan(page, rows, candidates):
    return {"page": page, "candidates": candidates,
            "block": {"table_body": "<table>" + "".join(rows) + "</table>"}}


def _sections(page_map, text_map=None):
    """A stand-in for _section_resolver: text wins where it is recognised (the
    reading-order question), otherwise the page's open section."""
    text_map = text_map or {}

    def at(pg, text=""):
        for cand in ([text] if isinstance(text, str) else list(text or ())):
            for frag, sec in text_map.items():
                if frag in (cand or ""):
                    return sec
        return page_map.get(pg)
    return at


# --------------------------------------------------------------------------
# Saudi Arabia: the merged block is a duplicate VIEW, not a recovery
# --------------------------------------------------------------------------
def test_a_merged_block_is_routed_to_the_region_that_owns_its_content(tmp_path):
    """The p16 shape. The block is MinerU's merged view of the pages-16-26 table, so
    its content belongs to table_007 -- which already extracted it page by page from
    middle.json. Section 3's tail must not gain a single row of it, and the region
    that holds it correctly must not lose one."""
    root = _mktables(tmp_path, {
        "table_006": [Q_C],                 # section 3's tail on p16
        "table_007": [Q_A, Q_B],            # section 5's table, pages 17-26 — correct
    })
    before6 = _rows_of(root, "table_006")
    res = he.place_orphan_rows(
        # the router offers all three adjacencies; only one is in section 5
        [_orphan(16, [HEADER, Q_A, Q_B], ["table_006", "table_005", "table_007"])], root,
        section_at=_sections({16: 3, 18: 5}, {"unsolicited approach": 5}),
        pages_by_id={"table_006": (16,), "table_007": tuple(range(17, 27))})
    p = res["placements"][0]
    assert p["placed"] is True
    assert p["table_id"] == "table_007", "section 5's content goes to section 5's region"
    assert p["section_matched"] == ["table_007"]
    assert p["removed_from"] == [], "nothing is displaced by a block already in place"
    assert res["rows_removed"] == 0
    assert Q_A in _rows_of(root, "table_007") and Q_B in _rows_of(root, "table_007")
    assert _rows_of(root, "table_006") == before6, "section 3 is untouched"
    assert p["rows_added"] == 1, "only the header row was actually missing"


def test_content_already_emitted_in_the_same_section_is_not_placed_twice(tmp_path):
    """The holder is not among the candidates, so nothing stops the block being added
    a second time except knowing it is already there — in its own section, correctly."""
    root = _mktables(tmp_path, {
        "table_011": [Q_C],                 # section 7, the candidate
        "table_012": [Q_A, Q_B],            # section 7 too, already holds the content
    })
    res = he.place_orphan_rows(
        [_orphan(50, [Q_A, Q_B], ["table_011"])], root,
        section_at=_sections({50: 7, 52: 7}),
        pages_by_id={"table_011": (50,), "table_012": (51, 52)})
    p = res["placements"][0]
    assert p["placed"] is False
    assert p["duplicate_of"] == ["table_012"]
    assert "already emitted in the same section" in p["reason"]
    assert res["rows_before"] == res["rows_after"]
    assert Q_A not in _rows_of(root, "table_011")
    assert Q_A in _rows_of(root, "table_012"), "the holder keeps it"


def test_the_destination_must_be_in_the_orphans_own_section(tmp_path):
    """Same page, but table_007 does not yet hold the rows. The sole region CLAIMING
    p16 is still section 3's tail, and that is not a reason to file section 5 there."""
    root = _mktables(tmp_path, {"table_006": [Q_C], "table_007": [Q_B]})
    res = he.place_orphan_rows(
        [_orphan(16, [Q_A], ["table_006"])], root,   # only the wrong-section candidate
        section_at=_sections({16: 3, 18: 5}, {"unsolicited approach": 5}),
        pages_by_id={"table_006": (16,), "table_007": tuple(range(17, 27))})
    p = res["placements"][0]
    assert p["placed"] is False
    assert p["section_matched"] == []
    assert "no candidate region is in section 5" in p["reason"]
    assert Q_A not in _rows_of(root, "table_006")


def test_the_next_sections_region_is_reachable_as_a_candidate(tmp_path):
    """A section opening on this page has its region on the NEXT one. Ownership by
    "regions claiming this page" cannot see it; that is why p16 had only a wrong answer."""
    root = _mktables(tmp_path, {"table_006": [Q_C], "table_007": [Q_B]})
    res = he.place_orphan_rows(
        [_orphan(16, [Q_A], ["table_006", "table_007"])], root,
        section_at=_sections({16: 3, 18: 5}, {"unsolicited approach": 5}),
        pages_by_id={"table_006": (16,), "table_007": tuple(range(17, 27))})
    p = res["placements"][0]
    assert p["placed"] is True and p["table_id"] == "table_007"
    assert Q_A in _rows_of(root, "table_007")
    assert Q_A not in _rows_of(root, "table_006")


# --------------------------------------------------------------------------
# Malta: boilerplate is not evidence, and must never be deleted
# --------------------------------------------------------------------------
def test_a_repeated_header_row_is_not_evidence_and_deletes_nothing(tmp_path):
    spec = {f"table_{i:03d}": [HEADER, Q_A if i == 7 else Q_C] for i in range(1, 9)}
    root = _mktables(tmp_path, spec)
    before = {t: _rows_of(root, t) for t in spec}
    res = he.place_orphan_rows(
        [_orphan(48, [HEADER], list(spec))], root,
        section_at=_sections({48: 4}),
        pages_by_id={t: (48,) for t in spec})
    p = res["placements"][0]
    assert p["placed"] is False
    assert "boilerplate only" in p["reason"]
    assert res["rows_removed"] == 0
    for t in spec:
        assert _rows_of(root, t) == before[t], f"{t} was modified by a header-only orphan"
        assert "Questions" in _rows_of(root, t), f"{t} lost its header row"


def test_no_placement_can_remove_more_rows_than_it_adds(tmp_path):
    """The conservation counter the earlier passes did not have. 'rows relocated' read
    as a success metric while it counted rows taken OUT of the region that owned them."""
    spec = {f"table_{i:03d}": [HEADER, Q_C] for i in range(1, 9)}
    root = _mktables(tmp_path, spec)
    res = he.place_orphan_rows(
        [_orphan(48, [HEADER, Q_C], list(spec))], root,
        section_at=_sections({48: 4}), pages_by_id={t: (48,) for t in spec})
    assert res["rows_after"] >= res["rows_before"]
    assert res["rows_removed"] == 0


# --------------------------------------------------------------------------
# Switzerland: a real misfiling still gets corrected
# --------------------------------------------------------------------------
def test_a_cross_section_misfiling_is_moved_to_its_own_section(tmp_path):
    root = _mktables(tmp_path, {
        "table_007": [Q_C],            # section 7, ends on p34
        "table_008": [Q_A, Q_B],       # section 8, spans p35-53 — holds 7.5's tail
    })
    res = he.place_orphan_rows(
        [_orphan(35, [Q_A, Q_B], ["table_008", "table_007"])], root,
        section_at=_sections({31: 7, 36: 8}, {"unsolicited approach": 7}),
        pages_by_id={"table_007": (30, 31, 32, 33, 34), "table_008": tuple(range(35, 54))})
    p = res["placements"][0]
    assert p["placed"] is True and p["table_id"] == "table_007"
    assert p["removed_from"] == [{"table_id": "table_008", "rows": 2, "section": 8}]
    assert Q_A in _rows_of(root, "table_007") and Q_A not in _rows_of(root, "table_008")
    assert res["rows_before"] == res["rows_after"], "a move conserves rows"


def test_subtraction_never_reaches_a_region_that_does_not_span_the_page(tmp_path):
    """table_016 sits on page 72 and holds a lookalike row. Nothing on page 35 may
    touch it — the earlier pass reached eight regions across pages 14-73."""
    root = _mktables(tmp_path, {
        "table_007": [Q_C],
        "table_008": [Q_A],
        "table_016": [Q_A],            # far away, same signature
    })
    res = he.place_orphan_rows(
        [_orphan(35, [Q_A], ["table_008", "table_007"])], root,
        section_at=_sections({34: 7, 35: 8, 72: 12}, {"unsolicited approach": 7}),
        pages_by_id={"table_007": (34,), "table_008": (35,), "table_016": (72,)})
    p = res["placements"][0]
    assert p["placed"] is False, "a signature in two regions is not evidence"
    assert Q_A in _rows_of(root, "table_016")
    assert res["rows_removed"] == 0


def test_a_genuinely_absent_orphan_is_still_recovered(tmp_path):
    """The 5 placements out of 16 that were real. Nothing else holds these rows, so
    nothing is removed and the content reaches the tree."""
    root = _mktables(tmp_path, {"table_010": [HEADER], "table_011": [Q_C]})
    res = he.place_orphan_rows(
        [_orphan(50, [Q_A, Q_B], ["table_011"])], root,
        section_at=_sections({49: 6, 50: 7}), pages_by_id={"table_010": (49,), "table_011": (50,)})
    p = res["placements"][0]
    assert p["placed"] is True and p["rows_added"] == 2
    assert p["removed_from"] == []
    assert Q_A in _rows_of(root, "table_011") and Q_B in _rows_of(root, "table_011")
    assert res["rows_after"] - res["rows_before"] == 2


def test_status_json_row_counts_follow_the_rewrite(tmp_path):
    root = _mktables(tmp_path, {"table_011": [Q_C]})
    he.place_orphan_rows([_orphan(50, [Q_A], ["table_011"])], root,
                         section_at=_sections({50: 7}), pages_by_id={"table_011": (50,)})
    assert json.loads((root / "table_011" / "status.json").read_text())["rows"] == 2


# --------------------------------------------------------------------------
# the section resolver itself
# --------------------------------------------------------------------------
class _Page:
    def __init__(self, text):
        self._t = text

    def get_text(self):
        return self._t


class _Doc(list):
    pass


def _saudi_p16():
    """The real page: section 3's tail, then TWO headings, then section 5's table."""
    return _Doc([_Page("")] * 15 + [_Page(
        "date of entry into force and what the anticipated scope of the changes will be\n"
        "(e) Please provide the name of the competent regulator(s).\n"
        "4 SECTION INTENTIONALLY LEFT BLANK\n"
        "5 PASSIVE MARKETING (REVERSE-ENQUIRY)\n"
        "Questions Answers\n")])


HEADINGS = [{"level": 1, "page": 14, "title": "3 SOURCES OF LAW; GUIDANCE; COMPETENT REGULATOR"},
            {"level": 1, "page": 16, "title": "4 SECTION INTENTIONALLY LEFT BLANK"},
            {"level": 1, "page": 16, "title": "5 PASSIVE MARKETING (REVERSE-ENQUIRY)"}]


def test_text_before_a_mid_page_heading_belongs_to_the_closing_section():
    at = he._section_resolver(HEADINGS, _saudi_p16())
    assert at(16, "date of entry into force and what the anticipated scope") == 0


def test_text_after_a_mid_page_heading_belongs_to_the_opening_section():
    at = he._section_resolver(HEADINGS, _saudi_p16())
    assert at(16, "Questions Answers") == 2, "section 5, not the section 3 tail above it"


def test_a_page_with_no_heading_keeps_whatever_section_was_open():
    at = he._section_resolver(HEADINGS, _Doc([_Page("")] * 20))
    assert at(15, "anything") == 0 and at(20, "anything") == 2


def test_an_html_entity_does_not_defeat_the_lookup():
    """MinerU writes &amp; where the page says &, and a raw comparison silently never
    matched — which is how the section test returned False and stopped guarding."""
    doc = _Doc([_Page("")] * 34 + [_Page("7.5 Investment Management & Advisory Services\n"
                                         "8 Marketing Activities\ntail text here\n")])
    at = he._section_resolver(
        [{"level": 1, "page": 30, "title": "7 PRIVATE PLACEMENT"},
         {"level": 1, "page": 35, "title": "8 Marketing Activities"}], doc)
    assert at(35, "7.5 Investment Management &amp; Advisory Services") == 0
    assert at(35, "tail text here") == 1


def test_no_headings_at_all_is_not_a_crash():
    at = he._section_resolver([], _Doc([_Page("x")]))
    assert at(1, "anything") is None


def test_a_far_away_cross_section_copy_is_reported_not_deleted(tmp_path):
    """A different section holds the same rows but does not span the orphan's page, so
    nothing about reading order says it swept them up as a neighbour. Which copy is
    right is not decidable here and deleting the wrong one is unrecoverable — so both
    stay and the collision is surfaced."""
    root = _mktables(tmp_path, {
        "table_011": [Q_C],            # section 7, the destination
        "table_013": [Q_A],            # section 8, pages 60-69 — nowhere near page 50
    })
    res = he.place_orphan_rows(
        [_orphan(50, [Q_A], ["table_011"])], root,
        section_at=_sections({50: 7, 61: 8}),
        pages_by_id={"table_011": (50,), "table_013": tuple(range(60, 70))})
    p = res["placements"][0]
    assert p["placed"] is True and p["removed_from"] == []
    assert p["also_held_by"] == [{"table_id": "table_013", "rows": 1, "section": 8}]
    assert Q_A in _rows_of(root, "table_013"), "the far copy is never deleted"
    assert res["rows_removed"] == 0
