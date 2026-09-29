"""Structural guidance→clause linking (Shareholding Disclosure style: no clause
refs in REFERENCETEXT, link by template/subject/question survey coordinates)."""

from aosphere_core_index.navigator.content_export import (
    GuidanceLinker,
    guidance_refs,
    unlinked_guidance_rows,
)

SECTIONS = [
    {"key": "A", "title": "Substantial Shareholding", "level": 0},
    {"key": "A1", "title": "Overview", "level": 1},
    {"key": "A3", "title": "Disclosure Thresholds", "level": 1},
    {"key": "A3.1", "title": "Are there any exemptions?", "level": 2},
    {"key": "C", "title": "Takeovers", "level": 0},
    {"key": "C1", "title": "Overview", "level": 1},
    {"key": "C2", "title": "Enhanced Disclosure Requirements", "level": 1},
    {"key": "C2.1", "title": "Are there any exemptions?", "level": 2},
]
KEYS = {s["key"] for s in SECTIONS}


def _linker():
    return GuidanceLinker(SECTIONS)


def test_template_scopes_ambiguous_titles():
    # "Overview" exists under A and C — the template (regime) disambiguates
    lk = _linker()
    assert lk.key_for({"TEMPLATENAME": "Takeovers", "SUBJECTNAME": "Overview"}) == "C1"
    assert lk.key_for({"TEMPLATENAME": "Substantial Shareholdings",  # plural variant
                       "SUBJECTNAME": "Overview"}) == "A1"


def test_question_gives_clause_depth():
    lk = _linker()
    r = {"TEMPLATENAME": "Takeovers", "SUBJECTNAME": "Enhanced Disclosure Requirements",
         "QUESTIONTITLE": "Are there any exemptions?"}
    assert lk.key_for(r) == "C2.1"


def test_ambiguous_without_template_stays_unlinked():
    # no template, two "Overview" sections -> refuse to guess
    assert _linker().key_for({"SUBJECTNAME": "Overview"}) is None


def test_explicit_refs_win_over_structural():
    r = {"REFERENCETEXT": "see A3.1 of the survey", "TEMPLATENAME": "Takeovers",
         "SUBJECTNAME": "Overview"}
    assert guidance_refs(r, KEYS, _linker()) == ["A3.1"]


def test_structural_fallback_used_when_no_refs():
    r = {"REFERENCETEXT": "Memorandum page 107", "TEMPLATENAME": "Takeovers",
         "SUBJECTNAME": "Overview"}
    assert guidance_refs(r, KEYS, _linker()) == ["C1"]


def test_no_signal_no_link():
    assert guidance_refs({"REFERENCETEXT": "Memorandum page 3"}, KEYS, _linker()) == []


# ---- fuzzy tier ---------------------------------------------------------------

FUZZY_SECTIONS = [
    {"key": "A", "title": "Substantial Shareholding", "level": 0},
    {"key": "A1", "title": "How to make a disclosure", "level": 1},
    {"key": "A2", "title": "Stock borrower or stock lender", "level": 1},
    {"key": "A3", "title": "Is there a ban or restriction on short selling?", "level": 1},
    {"key": "A4", "title": "Aggregation of holdings", "level": 1},
    {"key": "A5", "title": "Aggregation of positions", "level": 1},
]


def _flk():
    return GuidanceLinker(FUZZY_SECTIONS)


def test_fuzzy_paraphrase_links():
    r = {"TEMPLATENAME": "Substantial Shareholdings",
         "QUESTIONTITLE": "How to Make a Notification or Disclosure"}
    assert _flk().key_for(r) == "A1"


def test_fuzzy_word_order_links():
    r = {"TEMPLATENAME": "Substantial Shareholdings",
         "QUESTIONTITLE": "Stock lender/stock borrower"}
    assert _flk().key_for(r) == "A2"


def test_fuzzy_hyphen_variant_links():
    r = {"TEMPLATENAME": "Substantial Shareholdings",
         "QUESTIONTITLE": "Is there a ban or restriction on short-selling of shares?"}
    assert _flk().key_for(r) == "A3"


def test_fuzzy_ambiguous_near_tie_stays_unlinked():
    # "Aggregation" matches A4 and A5 almost equally -> refuse to guess
    r = {"TEMPLATENAME": "Substantial Shareholdings", "QUESTIONTITLE": "Aggregation"}
    assert _flk().key_for(r) is None


def test_fuzzy_never_links_part_root():
    r = {"TEMPLATENAME": "Substantial Shareholdings",
         "QUESTIONTITLE": "Substantial Shareholding overview of rules"}
    assert _flk().key_for(r) != "A"


# ---- standalone rows for unplaceable guidance ---------------------------------

RATED = [
    # links structurally -> no standalone row
    {"RESPONSEROWID": 1, "TEMPLATENAME": "Takeovers", "SUBJECTNAME": "Overview",
     "SETANSWEROTHER": "Linked answer."},
    # no linkable signal -> standalone GUID row
    {"RESPONSEROWID": 2, "SUBJECTNAME": "Practical points",
     "QUESTIONTITLE": "Timing traps", "SETANSWEROTHER": "Watch the T+2 deadline."},
    # semantically mapped elsewhere -> excluded via exclude_ids
    {"RESPONSEROWID": 3, "SUBJECTNAME": "Netting", "SETANSWEROTHER": "Nettable."},
]


def test_unlinked_guidance_becomes_standalone_rows():
    rows = unlinked_guidance_rows(RATED, KEYS, SECTIONS)
    keys = [r[0] for r in rows]
    assert "GUID:1" not in keys          # structurally linked
    assert "GUID:2" in keys and "GUID:3" in keys
    row = next(r for r in rows if r[0] == "GUID:2")
    assert row[4] == "guidance"
    assert "Timing traps" in row[1] and "T+2" in row[3]


def test_semantically_mapped_ids_excluded():
    rows = unlinked_guidance_rows(RATED, KEYS, SECTIONS, exclude_ids={"3"})
    keys = [r[0] for r in rows]
    assert "GUID:3" not in keys and "GUID:2" in keys