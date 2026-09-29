"""Stage 4 has to find the sections of a NESTED tree, not just a flat one.

Products do not agree on tree shape. 124_Marketing_Restrictions is flat — every section a
top-level `07-marketing-selling….md`. 104_Shareholding_Disclosure nests by Part:
`A-substantial-shareholding/03-disclosure-thresholds/3.1-thresholds.md`. Stage 4 enumerated
its targets with a top-level `glob("*.md")`, which on the nested shape sees ONE content
file — the front matter — and silently skips the rest. Australia 172274 ran that way: 1 of
124 sections, reported as "1/1 sections accepted", $0.01, gate still green. Nothing in the
report, the log or the dashboard said a document had been missed, which is why the walk is
pinned here and not left to the next reader to notice.
"""

from pathlib import Path

from aosphere_core_index.extract.ai_postprocess import (
    section_files,
    section_key,
    stage4_failed_sections_from_report,
)


def _write(root: Path, rel: str, text: str = "# s\n\nbody\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_a_nested_tree_yields_every_section(tmp_path):
    _write(tmp_path, "01-front-matter.md")
    _write(tmp_path, "A-substantial-shareholding/01-overview/00-overview.md")
    _write(tmp_path, "A-substantial-shareholding/01-overview/1.1-general-disclosure.md")
    _write(tmp_path, "B-foreign-investment/02-restrictions/2.1-thresholds.md")

    assert [section_key(tmp_path, p) for p in section_files(tmp_path)] == [
        "01-front-matter.md",
        "A-substantial-shareholding/01-overview/00-overview.md",
        "A-substantial-shareholding/01-overview/1.1-general-disclosure.md",
        "B-foreign-investment/02-restrictions/2.1-thresholds.md",
    ]


def test_content_without_a_leading_digit_is_still_content(tmp_path):
    # The appendix sub-sections are lettered — "A-background.md" — and the old filter
    # ("does the filename start with a digit") excluded them along with the README.
    _write(tmp_path, "07-appendix-1/A-background.md")
    _write(tmp_path, "07-appendix-1/B-manager-has-voting-discretion.md")

    assert [p.name for p in section_files(tmp_path)] == [
        "A-background.md", "B-manager-has-voting-discretion.md"]


def test_reports_and_readmes_are_not_sections(tmp_path):
    _write(tmp_path, "01-front-matter.md")
    for noise in ("README.md", "CONVERSION_REPORT.md", "STAGE3_REPORT.md",
                  "STAGE4_REPORT.md", "A-part/README.md"):
        _write(tmp_path, noise)

    assert [section_key(tmp_path, p) for p in section_files(tmp_path)] == ["01-front-matter.md"]


def test_assets_are_not_sections(tmp_path):
    # Stage 1 writes page snapshots into _assets/; nothing in there is a section, and an
    # AI pass over one would be paid for and meaningless.
    _write(tmp_path, "01-front-matter.md")
    _write(tmp_path, "_assets/notes.md")

    assert [section_key(tmp_path, p) for p in section_files(tmp_path)] == ["01-front-matter.md"]


def test_a_flat_tree_is_keyed_exactly_as_before(tmp_path):
    # The flat products' reports must not change shape: on a flat tree the key IS the
    # basename, so 124_Marketing_Restrictions' stage4_report.json reads as it always did.
    for name in ("01-front-matter.md", "02-background.md", "09-marketing-activities.md"):
        _write(tmp_path, name)

    assert [section_key(tmp_path, p) for p in section_files(tmp_path)] == [
        "01-front-matter.md", "02-background.md", "09-marketing-activities.md"]


def test_two_parts_may_share_a_basename(tmp_path):
    # Each Part has its own 00-overview.md. Keyed by basename, one would overwrite the
    # other in the report and one Part's failure would be invisible.
    _write(tmp_path, "A-substantial-shareholding/01-overview/00-overview.md")
    _write(tmp_path, "B-foreign-investment/01-overview/00-overview.md")

    keys = [section_key(tmp_path, p) for p in section_files(tmp_path)]
    assert len(set(keys)) == 2


def test_a_nested_section_missing_from_the_report_is_caught(tmp_path):
    # The "never attempted" check differences the report's keys against what Stage 1
    # produced. Both sides are relative paths now; if one side were basenames the whole
    # document would read as never attempted.
    report = {"sections": [{"file": "A-part/01-overview/1.1-thresholds.md", "ok": True}]}
    known = {"A-part/01-overview/1.1-thresholds.md", "A-part/01-overview/1.2-exemptions.md"}

    failed = stage4_failed_sections_from_report(report, known)

    assert [f["file"] for f in failed] == ["A-part/01-overview/1.2-exemptions.md"]
    assert "never attempted" in failed[0]["reason"]
