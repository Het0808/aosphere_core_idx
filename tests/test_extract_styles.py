"""Heading-heuristic regressions — each case here corresponds to a real
misextraction found in the corpus (dates/addresses/TOC lines becoming Parts,
which shifted every part letter in 42 of 105 Shareholding Disclosure docs)."""

from aosphere_core_index.extract.styles import (
    generic_heading_level,
    numbering_heading_level,
)

# ---- real headings that must keep working -----------------------------------

def test_letter_part():
    assert numbering_heading_level("A.  Substantial Shareholding") == 0


def test_dotted_decimal():
    assert numbering_heading_level("3.4.17  ETFs") == 3
    assert numbering_heading_level("1.1  Scope") == 2


def test_bare_integer_with_dot():
    assert numbering_heading_level("2.  Overview") == 1


# ---- the misextraction classes (must all be rejected) ------------------------

def test_cover_date_rejected():
    # promoted to Part A in Spain & 30+ others before the fix
    assert numbering_heading_level("14 November 2025") is None
    assert numbering_heading_level("2 August, 2025") is None


def test_address_rejected():
    # "1 Queen Street" (New Zealand), "5 Avenue J.F. Kennedy" (Luxembourg)
    assert numbering_heading_level("1 Queen Street") is None
    assert numbering_heading_level("5 Avenue J.F. Kennedy") is None
    assert numbering_heading_level("20 boulevard Princesse Charlotte") is None


def test_toc_page_number_rejected():
    # custom TOC style lines carry a trailing tab + page number (Kuwait "TOC2_1")
    assert numbering_heading_level("2.\tSUMMARY\t31") is None
    assert numbering_heading_level("A.\tSubstantial Shareholding\t3") is None


def test_sentence_rejected():
    assert numbering_heading_level("3. This is a sentence ending in a period.") is None


# ---- generic style-name levels ------------------------------------------------

def test_implausible_titles_demoted():
    from aosphere_core_index.extract.styles import is_plausible_title
    assert is_plausible_title("General thresholds")
    assert is_plausible_title("What enforcement powers does the regulator have (and use)?")
    # the Bahrain A3.1.1.4 case: ~700-char multi-sentence body paragraph
    assert not is_plausible_title("Further disclosure to the BHB and prior consent " * 15)
    # moderately long AND sentence-shaped -> body
    assert not is_plausible_title(
        "Once the threshold of voting rights or share capital is reached, any further "
        "increase of one percent or more shall also be subject to prior approval.")


def test_generic_style_levels():
    assert generic_heading_level("DPLevel 2") == 1
    assert generic_heading_level("Level 3") == 2
    assert generic_heading_level("Heading 1") == 0
    assert generic_heading_level("DPLevel12") == 11
    assert generic_heading_level("DPLevel 6.5") is None  # fractional -> not parsed
    assert generic_heading_level("BodyText1") is None
