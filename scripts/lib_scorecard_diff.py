#!/usr/bin/env python3
"""lib_scorecard_diff.py — what Scorecard 1 found, and what became of it.

Scorecard 1 gates the EXTRACTION (stage 3). Scorecard 2 judges the document as it now
stands (stage 4/5). Both are measured against the source PDF, independently — neither is
scored against the other, because stage 3 can itself be wrong and grading stage 5
against it would inherit that.

But a reader still needs the third thing neither scorecard says on its own: of the
problems the extraction had, which ones did the AI pass actually fix, which survived it,
and which did it create? That is this module, and it is a JOIN over the two findings
lists — not a score, and nothing here feeds a verdict.

WHY NOT JOIN ON THE FINDING KEY. Every finding already carries a stable content-derived
key (lib_dismissals), which is what makes a dismissal survive a re-extraction. Those keys
are not usable here, because several of them include the FILE a finding sits in
(gap_key(file, tokens)) and stage 5 renames files as it restructures —
01-front-matter.md becomes 00-front-matter.md, a sub-chunked section becomes a directory,
and the appendices take a letter prefix. Joined on those keys, every gap in the document
would read as "fixed by the AI" and an identical one as "introduced by the AI", which is
worse than saying nothing.

So the join key is derived from what the finding CLAIMS rather than where it sits: its
kind plus its normalised title. Two different findings that state the same thing in the
same words will collide — accepted deliberately, because the alternative (joining on
location) mis-reports every restructured section, and a collision merges two entries
rather than inventing one.

WHAT "FIXED" CAN AND CANNOT MEAN. A finding absent from Scorecard 2 means the check that
raised it no longer raises it. Usually that is a repair. It can also mean the content
moved far enough that the finding now describes something else, or that a check which
legitimately relaxes after stage 4 stopped asking (see check_table_presence's
`restructuring_allowed`). The bucket is evidence, not proof, and says so in `caveats`.
"""
from __future__ import annotations

import re

# Kinds whose disappearance is NOT evidence of a repair, because the check that raises
# them deliberately asks a weaker question once the AI pass is allowed to restructure.
# Listing them here keeps "fixed" honest rather than flattering.
_RELAXED_AFTER_STAGE4 = {"table_lost"}

_WS = re.compile(r"\s+")


def join_key(finding: dict) -> str:
    """Identity of the CLAIM, stable across the renames stage 5 performs."""
    title = _WS.sub(" ", str(finding.get("title") or "")).strip().lower()
    return f"{finding.get('kind')}|{title}"


def _active(sc: dict) -> dict[str, dict]:
    """Findings a reviewer has not already dismissed, keyed for the join."""
    return {join_key(f): f for f in (sc.get("findings") or []) if not f.get("dismissed")}


def _tally(findings) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        d = f.get("dimension") or "other"
        out[d] = out.get(d, 0) + 1
    return out


def finding_deltas(pre: dict, post: dict) -> dict:
    """-> {fixed, persisting, introduced, ...} between two scorecards.

    `pre` is scorecard.json (the extraction gate), `post` is scorecard_post_ai.json.
    Both are the full scorecard dicts, not just their findings.
    """
    a, b = _active(pre), _active(post)
    fixed = [a[k] for k in a if k not in b]
    persisting = [b[k] for k in b if k in a]
    introduced = [b[k] for k in b if k not in a]

    # A finding that only went away because the later check stopped asking is reported
    # separately from one the AI actually repaired.
    relaxed = [f for f in fixed if f.get("kind") in _RELAXED_AFTER_STAGE4]
    repaired = [f for f in fixed if f.get("kind") not in _RELAXED_AFTER_STAGE4]

    caveats = []
    if relaxed:
        caveats.append(
            f"{len(relaxed)} of the cleared findings are kinds whose check relaxes after "
            "stage 4 (a table may legitimately become headings and prose), so their "
            "absence is not by itself evidence of a repair.")
    if introduced:
        caveats.append(
            f"{len(introduced)} finding(s) appear only after the AI pass. Some are real "
            "regressions; some are defects stage 3 could not see because the content was "
            "locked inside one merged table until stage 4 split it.")

    return {
        "fixed": repaired,
        "fixed_by_relaxation": relaxed,
        "persisting": persisting,
        "introduced": introduced,
        "counts": {
            "fixed": len(repaired),
            "fixed_by_relaxation": len(relaxed),
            "persisting": len(persisting),
            "introduced": len(introduced),
            "before": len(a),
            "after": len(b),
        },
        "by_dimension": {
            "fixed": _tally(repaired),
            "persisting": _tally(persisting),
            "introduced": _tally(introduced),
        },
        "dimension_deltas": dimension_deltas(pre, post),
        "scored_stages": {"before": pre.get("scored_stage") or 3,
                          "after": post.get("scored_stage")},
        "gates": {"before": pre.get("gate"), "after": post.get("gate"),
                  "before_score": pre.get("worst_score"),
                  "after_score": post.get("worst_score")},
        "caveats": caveats,
    }


def dimension_deltas(pre: dict, post: dict) -> dict:
    """Per-dimension score movement. A dimension absent from either side is reported
    with a None on that side rather than omitted — "not measured" and "scored zero" are
    different facts and collapsing them is how a regression hides."""
    keys = sorted(set(pre.get("dimensions") or {}) | set(post.get("dimensions") or {}))
    out = {}
    for k in keys:
        before = (pre.get("dimensions") or {}).get(k, {}).get("score")
        after = (post.get("dimensions") or {}).get(k, {}).get("score")
        out[k] = {
            "before": before,
            "after": after,
            "delta": (round(after - before, 1)
                      if isinstance(before, (int, float)) and isinstance(after, (int, float))
                      else None),
            "critical": (post.get("dimensions") or {}).get(k, {}).get("critical"),
        }
    return out
