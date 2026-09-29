"""One malformed footnote marker must not produce a 4.9GB scorecard.

Footnote ids are numbered by a human — 1 to a few hundred, 1533 being the largest real one in
this corpus — and the sequence check treats every integer between the smallest and largest
OBSERVED id as a footnote that should exist. Poland Data Privacy 178124 contains
`[^11341434]`, a long number in `I-changes-in-regulation.md` that the reference pattern read
as a marker. The check therefore believed the document's footnotes ran 1–11,341,434 and
emitted one advisory finding per missing integer: **11,339,913 findings, a 4.9GB scorecard**.

Every consequence of that was somewhere else, which is why it took so long to find:

  * the Doc Library read all scorecards to build its tree, so opening it pulled 4.9GB into
    the service and the container was OOM-killed (exit 137);
  * the gallery publisher tried to PUT that object, which looked for four runs like a hung
    S3 upload, a stale client, a dead network — anything but a scorecard;
  * and the gate never moved (pass 95.7 before and after), so no verdict, no total and no
    log line ever said the document was wrong.

The document was fine. The check was unbounded.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_footnote_integrity import (  # noqa: E402
    _MAX_GAPS_REPORTED, _MAX_PLAUSIBLE_ID, _sequence_gaps, compute_footnote_integrity,
)

CORPUS = Path(__file__).resolve().parent.parent / "out" / "corpus"


def test_a_real_gap_is_still_reported():
    """The check must keep doing its job: a hole in a plausible sequence is a finding."""
    assert _sequence_gaps({1, 2, 4, 5}) == [3]
    assert _sequence_gaps({10, 11, 13, 14, 16}) == [12, 15]


def test_too_few_ids_to_assume_a_sequence():
    """Pre-existing behaviour, kept: with a couple of footnotes a gap is as likely to mean
    the document numbers them non-consecutively."""
    assert _sequence_gaps({1, 5}) == []


def test_one_absurd_id_does_not_stretch_the_sequence():
    """The Poland case. 11,341,434 is not a footnote, and treating it as one turned four
    real gaps into eleven million findings."""
    ids = {1, 2, 4, 5, 11341434}
    assert _sequence_gaps(ids) == [3], "the absurd id must be ignored, not enumerated over"


def test_the_gap_list_is_capped_however_pathological_the_document():
    """Defence in depth: even inside a plausible range, no verdict is improved by the
    ten-thousandth identical advisory, and a scorecard must never reach gigabytes."""
    ids = {1, _MAX_PLAUSIBLE_ID}          # a hole of ~5000 between two plausible ids
    ids |= {2, 3, 4}                      # enough ids to trust the sequence
    gaps = _sequence_gaps(ids)
    assert len(gaps) <= _MAX_GAPS_REPORTED
    assert gaps[0] == 5, "the cap truncates the tail, it does not reorder or drop the head"


def test_ids_above_the_bound_are_recorded_not_silently_dropped(tmp_path):
    """An ignored marker is evidence about the EXTRACTION (a number misread as a footnote),
    so it must remain visible rather than vanishing into a filter."""
    tree = tmp_path / "03_stage3_final"
    tree.mkdir(parents=True)
    body = "\n".join(f"Sentence {i} with a marker[^{i}]." for i in range(1, 8))
    defs = "\n".join(f"[^{i}]: Footnote body number {i}, long enough to be real." for i in (1, 2, 3, 4, 5, 6, 7))
    (tree / "01-a.md").write_text(f"# A\n\n{body}\n\nText[^11341434] here.\n\n{defs}\n",
                                  encoding="utf-8")
    r = compute_footnote_integrity(tmp_path)
    assert r["sequence_range"] == [1, 7], "the range must be the one the gap check trusted"
    assert 11341434 in r["sequence_ids_ignored"]


@pytest.mark.skipif(not (CORPUS / "155_Data_Privacy").exists(), reason="corpus not present")
def test_poland_is_bounded_end_to_end():
    """The document that caused it, scored for real."""
    job = CORPUS / "155_Data_Privacy" / "Poland (Data Privacy)__178124"
    if not job.exists():
        pytest.skip("Poland 178124 not extracted here")
    r = compute_footnote_integrity(job)
    assert r["sequence_range"] == [1, 1533]
    assert len(r["sequence_gaps"]) < 100, "was 11,339,913"
    assert r["sequence_ids_ignored"] == [11341434]
