"""Stage 1's report must be ONE log line, not a pretty-printed payload.

pdf2mdtree emits a machine report: page counts, plus `pages_snapshotted` (57 numbers on a
354-page document) and warnings that enumerate every unlocated heading. Echoed verbatim with
indent=2 that is ~81 lines per document — roughly 88,000 lines across a 1,088-document run — and
in a log stream each line is its own record, so the fields worth searching are buried inside a
payload nobody can read.

The full report is written to report.json beside the tree, so nothing is lost by summarising.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import hybrid_extract as H  # noqa: E402

REPORT = {
    "warnings": ["outline starts at page 10, but pages 1-9 contain heading-shaped text",
                 "116 outline headings could not be located: " + ", ".join(f"'{i}. Heading'" for i in range(60))],
    "pages": 354, "outline_entries": 243, "structure_source": "PDF bookmark outline",
    "paragraphs": 3814, "footnotes_found": 1014, "files_written": 131,
    "pages_snapshotted": list(range(10, 67)), "headings_total": 243, "headings_matched": 127,
    "word_delta_pct": 30.69,
}


def _lines(capsys, report=None):
    H._log_stage1(json.dumps(report or REPORT, separators=(",", ":")))
    return [l for l in capsys.readouterr().out.splitlines() if l.strip()]


def test_one_line_for_the_numbers_plus_one_per_warning(capsys):
    lines = _lines(capsys)
    assert len(lines) == 3, lines                      # summary + 2 warnings
    assert json.dumps(REPORT, indent=2).count("\n") > 70, "the payload really is that big"


def test_arrays_are_reduced_to_counts(capsys):
    """57 snapshot page numbers become snapshots=57 — the count is the useful part."""
    summary = _lines(capsys)[0]
    assert "snapshots=57" in summary
    assert "10, 11, 12" not in summary and "[10," not in summary


def test_values_with_spaces_are_quoted_so_logfmt_parses(capsys):
    """Unquoted, `src=PDF bookmark outline` parses as three fields and indexes rubbish."""
    summary = _lines(capsys)[0]
    assert 'src="PDF bookmark outline"' in summary


def test_a_long_warning_is_truncated_with_an_ellipsis(capsys):
    warnings = _lines(capsys)[1:]
    assert any(w.endswith("…") for w in warnings), warnings
    assert all(len(w) < 280 for w in warnings), [len(w) for w in warnings]


def test_the_fields_worth_searching_survive(capsys):
    summary = _lines(capsys)[0]
    for expect in ("pages=354", "outline=243", "files=131", "headings=127/243", "warnings=2"):
        assert expect in summary, (expect, summary)


def test_unparseable_output_is_collapsed_rather_than_dumped(capsys):
    """A crash or a non-JSON print must not spill dozens of lines either."""
    H._log_stage1("Traceback (most recent call last):\n" + "\n".join(f"  line {i}" for i in range(40)))
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 1, lines
    assert "Traceback" in lines[0]
