#!/usr/bin/env python3
"""lib_validate.py — the ONE canonical extraction-validation basis.

Every producer of a validation.json / scorecard.json must import CHECKS and
validate() from here, so the check set can never silently drift between the
corpus runner, the gallery publish path, the baseline regression harness and the
upload UI (it used to be copy-pasted in all four — run_baseline even lost
footnote_integrity that way). The scorecard rollup (check_scorecard) reads the
dimensions off this same check registry.

  CHECKS               the (name, compute_fn) pairs, in the UI's order
  validate(job_root)   -> validation dict (per-check results + `passed` rollup)
  run_and_gate(root)   -> (validation, scorecard)  [validate + compute_scorecard]
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from check_content_localized import compute_content_localized  # noqa: E402
from check_engine_agreement import compute_engine_agreement  # noqa: E402
from check_footnote_integrity import compute_footnote_integrity  # noqa: E402
from check_heading_hierarchy import compute_heading_hierarchy  # noqa: E402
from check_numeric_integrity import compute_numeric_integrity  # noqa: E402
from check_page_coverage import compute_page_coverage  # noqa: E402
from check_semantic_integrity import compute_semantic_integrity  # noqa: E402
from check_appendix_integrity import compute_appendix_integrity  # noqa: E402
from check_outline_coverage import compute_outline_coverage  # noqa: E402
from check_stage4 import compute_stage4  # noqa: E402
from check_structure_profile import compute_structure_profile  # noqa: E402
from check_source_fidelity import compute_source_fidelity  # noqa: E402
# check_table_cells is deliberately NOT in CHECKS. It asks the right question — does
# the rendered grid still match the PDF's? — and its corridor detection works, but its
# cell-LOCATION step does not: see that module's KNOWN DEFECT note. Every flag it
# produced was a lookup failure, so wiring it into the gate would score noise.
from check_table_placement import compute_table_placement  # noqa: E402
from check_text_conservation import compute_text_conservation  # noqa: E402
from check_table_presence import compute_table_presence  # noqa: E402
from check_toc_quality import compute_toc_quality  # noqa: E402
from check_word_coverage import compute_word_coverage  # noqa: E402

# The set the dev dashboard's _run_validation, the corpus runner and the gallery
# publish all score on. check_three_way_crosscheck is deliberately excluded — it
# is the opt-in GPU tier, not part of the default gate.
CHECKS = (
    ("word_coverage", compute_word_coverage),
    ("page_coverage", compute_page_coverage),
    ("content_localized", compute_content_localized),
    ("numeric_integrity", compute_numeric_integrity),
    ("table_placement", compute_table_placement),
    ("engine_agreement", compute_engine_agreement),
    ("semantic_integrity", compute_semantic_integrity),
    ("heading_hierarchy", compute_heading_hierarchy),
    ("table_presence", compute_table_presence),
    ("source_fidelity", compute_source_fidelity),
    # Is any of the source's text absent from the tree ANYWHERE? The only content check
    # with no blind spot by construction: no slices, no per-file attribution, no
    # exclusions, and it reads the index files the others skip. Every other check here
    # compares a file against a slice and misses whatever falls between them — measured,
    # 2 of 8 dropped paragraphs caught; this one missed none of 15.
    ("text_conservation", compute_text_conservation),
    ("footnote_integrity", compute_footnote_integrity),
    # Did every appendix the memo PRINTS become a chunk? The divisions are printed as a
    # bare "APPENDIX 3" label over their title, neither line a heading, so a missed one
    # is invisible to everything else here: the text is still in the tree (completeness
    # clean), the headings that exist still hold their content (sectioning clean), and
    # the census only knows the sections the outline named -- which is the outline that
    # missed it. Five documents in the 16-09-26 corpus lost an appendix this way and all
    # five gated PASS. Scores None outside its product (product_rules.
    # APPENDIX_CHUNK_PRODUCTS), like stage4 does for a job that never ran it.
    ("appendix_integrity", compute_appendix_integrity),
    # Does every heading the OUTLINE names still reach the tree as a heading? The
    # census beside it asks whether each promised section became a NODE and is capped at
    # census_levels() depth; this asks only whether the text survives anywhere, so the
    # sub-sections the census never promises are in scope. Belgium 163341 ships without
    # 4.5, 4.6 and 4.7 while its census promises 17 sections and reports 0 missing.
    ("outline_coverage", compute_outline_coverage),
    # Describes the document's SHAPE rather than checking its content against the
    # source. Included here so every producer stores the profile alongside the
    # other results; the cohort comparison that gives it most of its value
    # (jurisdiction vs its product's median) is computed at VIEW time from
    # sibling jobs, never stored — a stored cohort figure goes stale, silently,
    # the moment any sibling is re-extracted.
    ("structure_profile", compute_structure_profile),
    # Was the thing Stage 1 built the tree FROM trustworthy? Every other
    # check compares output against the PDF and is blind to an outline that
    # is complete, correct, and describes a different document.
    ("toc_quality", compute_toc_quality),
    # Did AI post-processing change the document? Skipped (score None) for the vast
    # majority of jobs, which never run stage 4 — it is the only stage that can rewrite
    # text rather than merely lose or misplace it, so it is audited against stage 3
    # rather than against the PDF.
    ("stage4", compute_stage4),
)

# Checks that compare a TREE against the source PDF, and so can be pointed at a
# different stage's tree. Everything else in CHECKS is deliberately excluded:
#
#   table_placement                      reads stage 1 (regions vs section page
#                                        ranges). Its `stage` default is 1, not 3 —
#                                        it describes the input, not the output tree.
#   engine_agreement / toc_quality       describe the SOURCE (which engine, which
#                                        outline). No output tree to point at.
#   stage4                               is already a stage3-vs-stage4 diff.
#
# Re-scoring a later stage therefore re-runs the content checks and carries the rest
# over unchanged — they would return the same answer either way, and running them
# twice would only cost time.
STAGE_SCOPED = frozenset({
    "word_coverage", "page_coverage", "content_localized", "numeric_integrity",
    "semantic_integrity", "table_presence", "text_conservation", "footnote_integrity",
    "structure_profile", "outline_coverage", "source_fidelity",
})

# heading_hierarchy takes the scored stage as `tree_stage`, not `stage`, because it
# reads TWO things: Stage 1's headings manifest (what the extractor tried to build,
# which never moves) and an output TREE to compare against (which stage 4/5 replace).
# Only the second may be re-pointed — passing `stage` would look for the manifest
# inside 05_subchunks and skip the check entirely.
#
# It belongs here because its section census is the only signal that can say "the
# document promises this section and the tree does not have it", and that question
# is meaningless asked of a tree two stages out of date. Pinned to stage 3 it graded
# the pre-AI document and reported the result as the post-AI verdict.
TREE_STAGE_SCOPED = frozenset({"heading_hierarchy"})

# Stage dirs that can hold a scoreable output tree, worst-to-best. 2 is absent on
# purpose: 02_ is MinerU's per-table HTML/JSON, not a markdown tree.
_OUTPUT_STAGES = (3, 4, 5)


def final_stage(job_root: Path) -> int:
    """The latest output tree this job actually produced — 5, 4, or 3.

    Stage 4 is additive (it writes 04_ beside 03_, never over it) and stage 5 reads
    stage 4, so the highest stage dir present is the document as it now stands."""
    job_root = Path(job_root)
    present = [s for s in _OUTPUT_STAGES if next(iter(job_root.glob(f"{s:02d}_*")), None)]
    return present[-1] if present else 3


def validate(job_root: Path, stage: int | None = None) -> dict:
    """Run every check over one extraction job dir (holds 01_/02_/03_ stage dirs).
    A broken check is captured as `{"error", "passed": False}` rather than sinking
    the job. Adds the top-level `passed` rollup the UI/job-detail endpoint reads —
    without it validation.json has a different shape than the dashboard produces.

    `stage` re-points the content checks (STAGE_SCOPED) at another stage's tree, for
    scoring the post-AI document. None keeps every check on its own default, which is
    the extraction gate and must stay byte-identical to what it was."""
    out: dict = {}
    meta_path = Path(job_root) / "corpus_meta.json"
    try:
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    except (OSError, ValueError):
        meta = {}
    imported = meta.get("import_kind") == "exported_chunks"
    manifests = {"table_placement": "tables_manifest.json",
                 "heading_hierarchy": "headings_manifest.json"}
    for key, fn in CHECKS:
        try:
            if (imported and key in manifests
                    and not list(Path(job_root).glob(f"01_*/{manifests[key]}"))):
                out[key] = {"passed": True, "skipped": True, "available": False,
                            "reason": "Imported chunk export has no original extraction manifest."}
            elif stage and key in STAGE_SCOPED:
                out[key] = fn(job_root, stage=stage)
            elif stage and key in TREE_STAGE_SCOPED:
                out[key] = fn(job_root, tree_stage=stage)
            else:
                out[key] = fn(job_root)
        except Exception as e:  # noqa: BLE001 — one broken check must not sink the job
            out[key] = {"error": str(e), "passed": False}
    out["passed"] = all(v.get("passed") for v in out.values() if isinstance(v, dict))
    if stage:
        out["scored_stage"] = stage
    return out


def run_and_gate(job_root: Path, stage: int | None = None) -> tuple[dict, dict]:
    """validate + compute_scorecard for one job dir -> (validation, scorecard).
    The canonical 'gate this extraction' step; scorecard is layout-agnostic (it
    only needs the stage dirs under job_root)."""
    from check_scorecard import compute_scorecard  # lazy: check_scorecard's own fallback imports us back
    val = validate(job_root, stage=stage)
    return val, compute_scorecard(job_root, val, stage=stage)
