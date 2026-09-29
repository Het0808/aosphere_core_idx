#!/usr/bin/env python3
"""mineru_fallback.py — when the hybrid pipeline's completeness score is too
low, re-parse the same document entirely through MinerU (mineru_full_extract.py)
instead, re-score it the same way, and mark the result so it's obvious a
fallback happened.

Trigger is deliberately just the `completeness` dimension, not the overall
gate: `placement`/`fidelity` failing doesn't mean the deterministic Stage 1
structure was wrong (a misplaced table is a Stage 2 matching problem, not a
reason to distrust headings/prose), but a badly low `completeness` score does
mean Stage 1 lost or misread real content — exactly what re-parsing through a
different engine entirely can fix.

The threshold (60) sits well below the scorecard's own `fail` floor (70) —
this is a second, more severe cutoff for "bad enough to redo from scratch",
not a replacement for the existing pass/review/fail gate.

Used by both scripts/run_corpus.py and scripts/hybrid_extract_ui.py, right
after each computes its first scorecard and before it's written to disk —
see either call site for the exact hook point.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from check_scorecard import compute_scorecard  # noqa: E402
from lib_validate import validate  # noqa: E402 — the one canonical 9-check set
from mineru_full_extract import run_mineru_full  # noqa: E402

FALLBACK_COMPLETENESS_THRESHOLD = 60.0
# A TOC score below this escalates too — see should_fallback. Set at the gate's
# own review line so 'this document failed the TOC dimension' and 'this
# document escalates' can never drift apart.
TOC_QUALITY_THRESHOLD = 70

STAGE_DIR_NAMES = ("01_stage1_extract", "02_stage2_mineru_tables", "03_stage3_final")


# ---- HARD GATE: a bookmark outline of 1-2 entries -----------------------------
# NOTE — THIS IS A HARD GATE, CHOSEN NOT MEASURED.
#
# An outline with one or two entries is not a description of a document. Every
# instance in this corpus is a publisher bookmarking a single anchor: curcao
# ("Appendix" + "Discretionary Managed Holdings", both on p37 of 40), and G20
# Brazil / China / Canada, whose two bookmarks are internal anchor NAMES
# ("bmkFrontPage", "bmkPrimaryFrontPage") that appear nowhere in the page text.
# Stage 1 trusts the outline, reads from its first entry, and emits a near-empty
# tree — Brazil produced 0 content files from a 1552-word PDF.
#
# ZERO entries is deliberately NOT included. No outline at all means Stage 1 never
# used one: it fell back to the font-size heuristic, which works. Measured over 101
# documents, 40 have no outline and their median completeness is 90.2 against 94.3
# for documents with a proper one. Escalating those was exactly the mistake the
# earlier `toc` gate made — it failed 39 documents that had extracted correctly and
# sent each through a MinerU re-parse that could not help.
#
# So the gate is narrow on purpose: 1 <= entries <= 2 catches ~5 documents where an
# outline EXISTS and is provably useless, and leaves the 40 no-outline documents to
# be judged on what they actually produced.
#
# Unlike OUTLINE_THIN_SHARE (set inside a measured empty band) this bound is a
# judgement: "one or two bookmarks cannot describe a document" is an assertion about
# publishing, not a threshold read off a distribution. If a real document ever ships
# a legitimate 2-entry outline, this will escalate it needlessly — that is the known
# cost, accepted because the failure it prevents is total content loss.
USELESS_OUTLINE_MAX_ENTRIES = 2

# Completeness bar for ENTERING the chain. Lower than ACCEPT_COMPLETENESS (90),
# which is the bar for STOPPING: a document at 85 is not good enough to call finished,
# but it is not broken enough that re-parsing it is likely to help — and every tier
# costs a full MinerU pass. Entry is now also triggered by the structural dimensions,
# so a document whose words are all present and whose sections are ruined no longer
# sails past the front door (Australia__183509: completeness 91.9, toc 45,
# sectioning 53.8 — never entered the chain, never got the rescue that fixed it).
CHAIN_ENTRY_COMPLETENESS = 60.0
# 60, not 80. At 80 the chain fires on a document whose words are substantially present and
# whose structure is already correct, and the tier it then runs can make the document WORSE
# while scoring better. Measured on Australia__183509: the pre-flight rebuilt its outline from
# the printed contents page (41 entries, 22 files, 16 deferred tables, "6.1" folded under "6."),
# scored completeness 74.1, entered on "completeness 74.1 < 80", and toc_rescue's result was
# adopted for taking the worst score 74.1 -> 87.9 — while uniqueness fell 90.5 -> 71.8, deferred
# tables went 16 -> 37, and every "6.1"/"8.9" came back as a top-level chunk. That cost 1167s of
# the document's 1204s: 97% of the runtime, a second full MinerU pass, to replace a correct tree.
#
# Corpus-wide, 27 of 217 scored documents sit in [60, 80) and stop entering on completeness;
# 3 sit below 60 and still enter. The structural triggers below (toc/sectioning < 70) and the
# hard shape gates in result_is_acceptable are untouched, so a document whose SECTIONS are
# ruined still enters regardless of how complete it reads — which is the case the 80 bar was
# raised for (see the note above about toc 45 / sectioning 53.8).


def _structural_keys(scorecard: dict | None) -> tuple[str, ...]:
    """Which structural dimensions may trigger or veto a fallback for this document.

    `sectioning` only counts where the scorecard says it sets the verdict — see
    product_rules.SECTIONING_GATES. Outside 124 it is advisory, and an advisory
    dimension must not drag a document through the most expensive thing the pipeline
    can do: Germany (Data Privacy)__180656 was extracted cleanly (completeness 94.3,
    fidelity 98.0, toc 100.0) and re-parsed for 10.9 minutes on `sectioning 60.8`
    alone. The scorecard publishes `sectioning_advisory`, so the chain and the
    scorecard cannot disagree about it.

    An OLD scorecard has no such key; absent means falsy means sectioning still
    counts, which is exactly the behaviour those scorecards were written under.
    """
    if (scorecard or {}).get("sectioning_advisory"):
        return ("toc",)
    return ("toc", "sectioning")


def needs_help(scorecard: dict | None) -> tuple[bool, str]:
    """Should the fallback chain be entered at all? -> (enter, why).

    completeness < CHAIN_ENTRY_COMPLETENESS, or either structural dimension below
    TOC_QUALITY_THRESHOLD. The hard shape gates in result_is_acceptable (a blob, a
    1-2 entry outline, no profile at all) still force entry on their own — those say
    Stage 1 read from the wrong place, whatever the scores look like."""
    if os.environ.get("DISABLE_MINERU_FALLBACK"):
        return False, "fallback disabled"
    dims = (scorecard or {}).get("dimensions") or {}
    why = []
    comp = (dims.get("completeness") or {}).get("score")
    if comp is not None and comp < CHAIN_ENTRY_COMPLETENESS:
        why.append(f"completeness {comp} < {CHAIN_ENTRY_COMPLETENESS:.0f}")
    for key in _structural_keys(scorecard):
        score = (dims.get(key) or {}).get("score")
        if score is not None and score < TOC_QUALITY_THRESHOLD:
            why.append(f"{key} {score} < {TOC_QUALITY_THRESHOLD}")
    # The shape gates are not score comparisons and must not be softened by the
    # looser completeness bar above.
    ok, hard = result_is_acceptable(scorecard)
    if not ok and ("outline has only" in hard or "structure profile" in hard
                   or "divided" in hard or "chunk" in hard):
        why.append(hard)
    return (bool(why), "; ".join(why) if why else "completeness and structure both fine")


def structure_recovered(scorecard: dict | None) -> tuple[bool, str]:
    """Is the document's STRUCTURE good enough that a full MinerU re-parse is not
    worth trying? -> (recovered, why), where recovered is None when neither
    structural dimension was scored and the caller should use its own gate.

    Tier 3 used to be gated on `should_fallback`, i.e. on completeness alone. That
    escalated ADGM__170680 after its TOC rescue had already fixed it: the rescue took
    the worst dimension 50.0 -> 80.2 with `sectioning` at 100, but completeness read
    80.2 — under the 90 bar — so MinerU ran anyway and its result (completeness 97.6,
    `sectioning` 54.1, gate FAIL) was adopted because the accept test never looks at
    sectioning. A 26-point structural regression, recorded as a success.

    Completeness is the wrong question for tier 3. Tier 3 replaces the document's
    HIERARCHY, so what decides whether to try it is whether the hierarchy is already
    sound: is the outline it was built from trustworthy (`toc`), and did the sections
    it promised arrive with their content (`sectioning`)? Both at or above
    TOC_QUALITY_THRESHOLD means yes — leave it alone.

    A dimension that is absent or unscored does not block: a missing signal is not
    evidence of a problem, and tier 3 is the most expensive thing the pipeline can do.
    """
    dims = (scorecard or {}).get("dimensions") or {}
    # Same dimension set the entry gate uses, so tier 3 cannot be vetoed (or forced)
    # by a dimension the scorecard treats as advisory. See _structural_keys.
    scored = {k: (dims.get(k) or {}).get("score") for k in _structural_keys(scorecard)}
    scored = {k: v for k, v in scored.items() if v is not None}
    if not scored:
        # NEITHER dimension scored — a short document, or a check that did not run.
        # Returning "recovered" here would silently switch tier 3 off for those, so
        # defer to the caller's previous gate rather than inventing an answer from
        # an absent signal.
        return None, "no structural dimension scored"
    weak = [f"{k} {v}" for k, v in scored.items() if v < TOC_QUALITY_THRESHOLD]
    if weak:
        return False, ("below the structural bar: "
                       + ", ".join(weak) + f" (need >= {TOC_QUALITY_THRESHOLD})")
    return True, "structure is sound: " + ", ".join(f"{k} {v}" for k, v in scored.items())


def should_fallback(scorecard: dict | None) -> bool:
    """Is this result good enough to STOP, or should the next tier be tried?

    An ABSOLUTE bar, not a comparison. The old rule escalated on completeness < 60
    and adopted a tier merely for scoring better than the one before, which let
    0.0 -> 15.4 count as a success and left a document sitting at 55 with 96%
    completeness because nothing scored higher. The same two questions are now asked
    after every tier:

        are the words there?          completeness >= ACCEPT_COMPLETENESS
        did it divide into sections?  structure_is_acceptable()

    False whenever DISABLE_MINERU_FALLBACK is set — useful while iterating on Stage 1,
    where a low score would otherwise trigger a re-parse that replaces the very output
    you are trying to inspect."""
    if os.environ.get("DISABLE_MINERU_FALLBACK"):
        return False
    return not result_is_acceptable(scorecard)[0]


def result_is_acceptable(scorecard: dict | None) -> tuple[bool, str]:
    """-> (good enough to stop, why). The single accept test, shared by every tier."""
    from check_structure_profile import ACCEPT_COMPLETENESS, structure_is_acceptable
    dims = (scorecard or {}).get("dimensions") or {}
    comp = (dims.get("completeness") or {}).get("score")
    if comp is None:
        return False, "no completeness score"
    if comp < ACCEPT_COMPLETENESS:
        return False, f"completeness {comp} is below {ACCEPT_COMPLETENESS:.0f}"
    # Hard gate — see USELESS_OUTLINE_MAX_ENTRIES. Checked BEFORE the shape tests
    # because a document built on a 2-entry outline can look perfectly shaped: it is
    # the CONTENT that went missing, and the tree that remains is small and tidy.
    tocd = ((scorecard or {}).get("dimensions") or {}).get("toc", {}).get("detail") or {}
    n_entries = tocd.get("outline_entries")
    if isinstance(n_entries, int) and 1 <= n_entries <= USELESS_OUTLINE_MAX_ENTRIES:
        return False, (f"the PDF's bookmark outline has only {n_entries} entr"
                       f"{'y' if n_entries == 1 else 'ies'} \u2014 too few to describe a "
                       f"document, so Stage 1 read from the wrong place")

    prof = (scorecard or {}).get("structure") or {}
    if not prof.get("chunks"):
        return False, "no structure profile"
    ok, why = structure_is_acceptable(prof)
    return (True, f"completeness {comp} and {why}") if ok else (False, why)


def run_fallback(dest: Path, pdf_path: Path, first_val: dict, first_sc: dict,
                 *, backend: str | None = None, effort: str | None = None) -> tuple[dict, dict]:
    """Move the hybrid attempt's artifacts aside (never delete — see
    hybrid_attempt/ under dest), re-parse pdf_path entirely via MinerU, and
    re-run the exact same validation/scoring over the new output. Returns
    (validation, scorecard) for the fallback attempt, with scorecard["fallback"]
    stamped so the caller/UI can tell this result came from a fallback."""
    dest = Path(dest)
    backup = dest / "hybrid_attempt"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    for name in STAGE_DIR_NAMES:
        src = dest / name
        if src.exists():
            shutil.move(str(src), str(backup / name))
    (backup / "validation.json").write_text(json.dumps(first_val, indent=2))
    (backup / "scorecard.json").write_text(json.dumps(first_sc, indent=2))

    run_mineru_full(Path(pdf_path), dest, backend=backend, effort=effort)
    val = validate(dest)
    sc = compute_scorecard(dest, val)
    first_completeness = ((first_sc.get("dimensions") or {}).get("completeness") or {}).get("score")
    sc["fallback"] = {
        "triggered": True,
        "first_attempt": {
            "gate": first_sc.get("gate"),
            "worst_score": first_sc.get("worst_score"),
            "weakest_dimension": first_sc.get("weakest_dimension"),
            "completeness_score": first_completeness,
        },
    }
    # If the fallback ITSELF still scores below threshold, there's nothing
    # further to fall back to — it's simply left as-is, still badged, so a
    # reviewer sees "this is already the MinerU attempt" rather than the
    # pipeline looping on a document neither engine can parse well.
    return val, sc
