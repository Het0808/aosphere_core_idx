"""Extraction-invariant tests — shapes taken from real corpus failures."""

from aosphere_core_index.extract.validate import validate_sections


def _doc(parts):
    """Build a minimal section list: parts is [(letter, title, n_children)]."""
    out = []
    for letter, title, n in parts:
        out.append({"key": letter, "title": title, "level": 0})
        for i in range(n):
            out.append({"key": f"{letter}{i+1}", "title": f"{title} child {i+1}", "level": 1})
    # pad to clear the min-section floor
    for i in range(30):
        out.append({"key": f"Z9.{i}", "title": "pad", "level": 2})
    return out


GOOD_SD = _doc([("A", "Substantial Shareholding", 3), ("B", "Sensitive Industries", 2),
                ("C", "Takeovers", 2), ("D", "Issuer Initiated Disclosure", 1),
                ("E", "Short-Selling", 1)])


def test_healthy_doc_passes():
    errors, _ = validate_sections(GOOD_SD, "Shareholding Disclosure", "Australia")
    assert errors == []


def test_date_part_is_error():
    # the real Spain failure: cover date became Part A, shifting all letters
    bad = _doc([("A", "14 November 2025", 0), ("B", "Substantial Shareholding", 3)])
    errors, warnings = validate_sections(bad, "Shareholding Disclosure", "Spain")
    assert any("date" in e for e in errors)
    assert any("first part" in w for w in warnings)  # first-part check fires as warning


def test_slash_variant_first_part_passes():
    # "Substantial / Controlling Shareholding" (Guernsey) must not warn
    doc = _doc([("A", "Substantial / Controlling Shareholding", 2),
                ("B", "Sensitive Industries", 2)])
    errors, warnings = validate_sections(doc, "Shareholding Disclosure", "Guernsey")
    assert errors == [] and not any("first part" in w for w in warnings)


def test_address_part_is_error():
    bad = _doc([("A", "1 Queen Street", 0), ("B", "Substantial Shareholding", 3)])
    errors, _ = validate_sections(bad, "Shareholding Disclosure", "New Zealand")
    assert any("number-led" in e for e in errors)


def test_bare_empty_part_is_error():
    # a part whose internal headings were all missed (Spain's bare 'A' pre-fix)
    bad = _doc([("A", "Substantial Shareholding", 0), ("B", "Sensitive Industries", 2)])
    errors, _ = validate_sections(bad, "Shareholding Disclosure", "Spain")
    assert any("no child sections" in e for e in errors)


def test_childless_part_with_content_passes():
    # legitimate single-block part (e.g. DP "G Employment" holds its own elements)
    doc = _doc([("A", "Substantial Shareholding", 2), ("B", "Sensitive Industries", 2)])
    doc.append({"key": "G", "title": "Employment", "level": 0,
                "elements": [{"kind": "body", "text": "..."}]})
    errors, _ = validate_sections(doc, "Shareholding Disclosure", "X")
    assert errors == []


def test_too_few_sections_is_error():
    errors, _ = validate_sections([{"key": "A", "title": "Laws", "level": 0}], "Data Privacy", "X")
    assert any("sections extracted" in e for e in errors)


def test_duplicate_keys_warn():
    doc = GOOD_SD + [{"key": "A1", "title": "dupe", "level": 1}]
    _, warnings = validate_sections(doc, "Shareholding Disclosure", "Australia")
    assert any("duplicate" in w for w in warnings)


def test_dp_has_no_first_part_expectation():
    dp = _doc([("A", "What laws apply", 2), ("B", "Regulator", 2)])
    errors, _ = validate_sections(dp, "Data Privacy", "France")
    assert errors == []
