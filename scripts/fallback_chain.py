#!/usr/bin/env python3
"""fallback_chain — what to try, and in what order, when Stage 1 lost the structure.

There are three ways to get a tree out of one of these PDFs, and they are not
equally trustworthy or equally cheap. This module fixes the order:

    1. the normal pipeline   deterministic Stage 1 (fitz outline / font heuristic)
                             + Stage 2 tables + Stage 3 combine. Always runs, for
                             every document, unchanged — the chain never touches it.
    2. TOC rescue            the document's own PRINTED table of contents, parsed and
                             verified page-by-page, written back as bookmarks, then
                             re-extracted through the SAME pipeline. Cheap, and the
                             structure comes from the publisher rather than a guess.
    3. MinerU full re-parse  hand the whole document to a visual model and take ITS
                             hierarchy. The most expensive thing we can do (minutes of
                             GPU per document) and the least like the rest of the
                             corpus, so it is the last resort, not the first.

Tier 3 was previously the only fallback and fired the moment completeness dipped
below its threshold — so a document whose own contents page would have fixed it paid
for a full visual re-parse anyway. The trigger is unchanged (mineru_fallback's
`should_fallback`, completeness < 60); what changes is that tier 2 gets to try first,
and tier 3 only runs if the document is STILL below the threshold afterwards.

Every tier is adopted only if it scores better than the best result so far — a printed
TOC can be sparser than the heuristic's guess, and a MinerU hierarchy can be worse
than either (see rescue_outline._is_better for the case that taught us). Nothing is
deleted: a rejected attempt stays on disk (fallback/, mineru_full_attempt/) and the
whole decision is recorded in scorecard["fallback"]["chain"].

Env kill-switches, for iterating on one stage without paying for the others:
    DISABLE_TOC_RESCUE=1        skip tier 2
    DISABLE_MINERU_FALLBACK=1   skip the chain entirely (mineru_fallback's own switch)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from check_scorecard import SHORT_DOC_MAX_PAGES  # noqa: E402
from mineru_fallback import (CHAIN_ENTRY_COMPLETENESS, STAGE_DIR_NAMES, needs_help,
                             structure_recovered, result_is_acceptable, run_fallback,
                             should_fallback)  # noqa: E402

# how much better a tier has to score to be worth adopting — anything smaller is
# noise, and swapping a document's whole tree for noise is not an improvement
MARGIN = 0.05


def _worst(sc: dict | None) -> float | None:
    w = (sc or {}).get("worst_score")
    return w if isinstance(w, (int, float)) else None


def _completeness(sc: dict | None) -> float | None:
    return ((sc or {}).get("dimensions") or {}).get("completeness", {}).get("score")


def _floor_detail(sc: dict | None) -> tuple[float, int]:
    """Resolution BELOW the score floor: (word coverage %, -pages missing).

    completeness clamps at 0.0, so two attempts can tie at 0.0 while being nothing
    alike — G20 Brazil 182791 scores 0.0 from Stage 1 (no content file at all, 4 of 6
    pages missing) and 0.0 from MinerU (15% coverage, 0 pages missing). The raw
    completeness inputs still separate them when the score cannot."""
    d = ((sc or {}).get("dimensions") or {}).get("completeness", {}).get("detail") or {}
    cov, missing = d.get("coverage_pct"), d.get("pages_missing")
    return (cov if isinstance(cov, (int, float)) else -1.0,
            -(missing if isinstance(missing, int) else 10 ** 6))


def _better(new: dict | None, cur: dict | None) -> bool:
    """Should this tier's result replace what we have?

    ABSOLUTE first: a result that passes the accept test is adopted outright, because
    "good enough to stop" is the whole question the chain exists to answer. Only when
    neither passes does the relative comparison matter, and then it is choosing the
    least-bad thing to hand back with a hard-fail flag \u2014 not declaring success.

    The old rule was relative only, which let a tier be adopted for going 0.0 -> 15.4
    while still being broken, and blocked a genuinely fine result from replacing a
    higher-scoring but structurally useless one."""
    from mineru_fallback import result_is_acceptable
    if new is None:
        return False
    new_ok, _ = result_is_acceptable(new)
    cur_ok, _ = result_is_acceptable(cur) if cur is not None else (False, "")
    if new_ok and not cur_ok:
        return True
    if cur_ok and not new_ok:
        return False
    a, b = _worst(new), _worst(cur)
    if a is None:
        return False
    if b is None or a > b + MARGIN:
        return True
    if a < b - MARGIN:
        return False
    # tied — and only worth looking closer when both are pinned to the floor, where
    # the tie is an artefact of the clamp rather than two genuinely equal results
    if a <= MARGIN and b <= MARGIN:
        return _floor_detail(new) > _floor_detail(cur)
    return False


def _restore_previous(dest: Path, rejected_val: dict, rejected_sc: dict) -> None:
    """Undo run_fallback's swap. The MinerU tree moves aside to mineru_full_attempt/ —
    kept, never deleted — WITH its own validation and scorecard, so "we tried MinerU and
    it was worse" can be read back per dimension instead of taken on trust. The stage
    dirs run_fallback parked in hybrid_attempt/ come back as the result."""
    backup, aside = dest / "hybrid_attempt", dest / "mineru_full_attempt"
    if aside.exists():
        shutil.rmtree(aside)
    aside.mkdir(parents=True, exist_ok=True)
    for name in STAGE_DIR_NAMES:
        cur, src = dest / name, backup / name
        if cur.exists():
            shutil.move(str(cur), str(aside / name))
        if src.exists():
            shutil.move(str(src), str(cur))
    (aside / "validation.json").write_text(json.dumps(rejected_val, indent=2))
    (aside / "scorecard.json").write_text(json.dumps(rejected_sc, indent=2))
    # hybrid_attempt/ now only holds a copy of the result restored at dest root
    shutil.rmtree(backup, ignore_errors=True)


def _mineru_full(dest: Path, pdf: Path, val: dict, sc: dict, *, backend=None, effort=None):
    """Tier 3. Alpha's whole-document MinerU re-parse, kept only if it scores better."""
    rec: dict = {"tier": "mineru_full", "worst_before": _worst(sc)}
    new_val, new_sc = run_fallback(dest, pdf, val, sc, backend=backend, effort=effort)
    rec["worst_after"] = _worst(new_sc)
    if _better(new_sc, sc):
        rec.update(status="re-parsed through MinerU", adopted=True)
        return new_val, new_sc, rec
    _restore_previous(dest, new_val, new_sc)
    rec.update(adopted=False,
               status=(f"re-parsed through MinerU — NOT adopted: {_worst(new_sc)} is not "
                       f"better than {_worst(sc)}"))
    return val, sc, rec


def pdf_page_count(pdf: Path) -> int:
    """Page count, cheaply and without ever raising — a routing decision must not be
    able to crash the document it is deciding about.

    Public because run_corpus asks the same question BEFORE Stage 1: the count is a
    property of the source PDF, so nothing the extraction produces can change it, and
    taking the decision here meant a short document paid for Stage 1-3 and a full
    scoring round before being routed to a re-parse that replaced the tree."""
    try:
        import fitz
        d = fitz.open(str(pdf))
        try:
            return d.page_count
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        return 0


def _first_attempt(sc: dict) -> dict:
    """Snapshot of why the document escalated, taken BEFORE any tier rewrites it."""
    dims = (sc or {}).get("dimensions") or {}
    toc = dims.get("toc") or {}
    td = toc.get("detail") or {}
    return {"gate": sc.get("gate"), "worst_score": _worst(sc),
            "weakest_dimension": sc.get("weakest_dimension"),
            "completeness_score": _completeness(sc),
            "toc_score": toc.get("score"), "toc_status": td.get("status"),
            "toc_reason": td.get("explanation"), "toc_rescuable": td.get("rescuable")}


def run_chain(dest: Path, pdf: Path, val: dict, sc: dict, *,
              backend=None, effort=None, step=None, on_trigger=None) -> tuple[dict, dict]:
    """Escalate through the fallback tiers, cheapest and most trustworthy first.

    Takes the normal pipeline's (validation, scorecard) and returns the pair the
    document should be scored on. A no-op unless `should_fallback` says Stage 1 lost
    content, so every call site can call it unconditionally.

    step(name, fn) -> fn() lets the caller time and announce each tier (run_corpus's
    _step does both); without it the tiers just run.

    on_trigger(info) is called ONCE, the moment the chain decides to enter, with the reason and
    the Stage 1 state that caused it. It exists because `step` announces only a tier NAME: a
    monitoring screen could see "now in toc_rescue" but never why, so a document that had gone
    back to Stage 1 for the second time looked identical to one that had never left it. Fired
    BEFORE the first tier runs, since the tier is the part that takes minutes.
    """
    def _announce(reason, first=None):
        if on_trigger:
            try:
                on_trigger({"reason": reason, "first_attempt": first or {}})
            except Exception:                                # noqa: BLE001
                pass                                         # progress is never worth a run
    # SHORT DOCUMENT: skip the TOC tier entirely and go straight to MinerU.
    # A memo under SHORT_DOC_MAX_PAGES pages has no hierarchy worth reconstructing
    # and usually no printed contents page, so tier 2 can only fail; and Stage 1's
    # outline path is the very thing that mangles these (2 anchor-name bookmarks ->
    # an empty tree). MinerU's raw output is the better answer and the only tier
    # that can supply it. Scoring follows the same rule: word coverage only, see
    # check_scorecard.SHORT_DOC_MAX_PAGES.
    #
    # run_corpus now asks this BEFORE Stage 1 (see _straight_to_mineru there), so a
    # corpus run never reaches this branch: by the time the chain is entered a short
    # document has already been routed. It stays because run_chain has a second caller
    # -- hybrid_extract_ui, which runs the stages itself and enters the chain afterwards
    # -- and a document arriving from there must still be routed correctly. Both paths
    # produce the same fallback record, so nothing downstream can tell them apart.
    run = step or (lambda name, fn: fn())
    n_pages = pdf_page_count(pdf)
    short_doc = 0 < n_pages < SHORT_DOC_MAX_PAGES
    if short_doc and not os.environ.get("DISABLE_MINERU_FALLBACK"):
        first = _first_attempt(sc)
        chain = []
        short_reason = (f"short document ({n_pages} pages): routed straight to MinerU")
        _announce(short_reason, first)
        val, sc, mrec = run("mineru_full",
                            lambda: _mineru_full(dest, pdf, val, sc, backend=backend, effort=effort))
        chain.append(mrec)
        fb = sc.setdefault("fallback", {})
        fb["triggered"] = True
        fb["first_attempt"] = first
        fb["chain"] = chain
        fb["adopted_tier"] = "mineru_full" if mrec.get("adopted") else "stage1"
        fb["reason"] = short_reason
        return val, sc

    enter, entry_why = needs_help(sc)
    if not enter:
        return val, sc
    # Why the document escalated, preserved BEFORE any tier rewrites the scorecard.
    # This matters most for a TOC-triggered escalation: the rescue replaces the PDF's
    # outline, so the re-scored `toc` dimension reads "proper" afterwards and the
    # original failure — the whole reason the document was rescued — would otherwise
    # be unrecoverable from the final scorecard.
    _toc = (sc.get("dimensions") or {}).get("toc") or {}
    _toc_d = _toc.get("detail") or {}
    first = {"gate": sc.get("gate"), "worst_score": _worst(sc),
             "weakest_dimension": sc.get("weakest_dimension"),
             "completeness_score": _completeness(sc),
             "toc_score": _toc.get("score"),
             "toc_status": _toc_d.get("status"),
             "toc_reason": _toc_d.get("explanation"),
             "toc_rescuable": _toc_d.get("rescuable"),
             "entry_reason": entry_why}
    _announce(entry_why, first)
    chain = []

    # THE OUTLINE QUESTION IS SETTLED BEFORE THIS POINT. run_corpus._preflight_outline
    # judges the bookmark outline before Stage 2 is paid for and rebuilds it from the
    # document's printed contents page when it cannot be trusted, so a document arriving
    # here has either a healthy outline or a repaired one. There used to be a second
    # printed-TOC tier at the head of this chain and it had nothing left to do: it either
    # skipped ("the pre-flight already rebuilt this outline"), or it re-ran the SAME
    # reader on a document the pre-flight had already failed to verify and failed the
    # same way. Measured over 148 scored documents: 30 entered the chain, it did work on
    # none of them and was adopted on none of them. Its one remaining path -- a healthy
    # outline on a document that escalated anyway -- rebuilds from the printed page the
    # pre-flight has already confirmed AGREES with that outline, which reproduces the
    # outline it started with.
    #
    # So there is one tier: if the score is still bad, take MinerU's hierarchy instead.
    #
    # Gated on STRUCTURE, not completeness. Tier 3 throws away the document's
    # hierarchy and takes MinerU's instead, so the question is whether the hierarchy
    # is already sound — `toc` (was the outline trustworthy) and `sectioning` (did the
    # promised sections arrive with their content), both >= TOC_QUALITY_THRESHOLD.
    # Gating on completeness escalated ADGM__170680 after the rescue had already fixed
    # it, and MinerU's replacement scored `sectioning` 54.1 against the rescue's 100.
    # See mineru_fallback.structure_recovered.
    recovered, why = structure_recovered(sc)
    if recovered is None:
        # No structural signal (short document, or the checks did not run): keep the
        # completeness gate this tier has always used.
        recovered = not should_fallback(sc)
        why = f"{why}; completeness gate says {'stop' if recovered else 'escalate'}"
    # A sound hierarchy is a reason not to REPLACE the hierarchy. It is not a reason to
    # stop on a document that is missing half its content, and the structural test
    # cannot tell the difference: Jersey__181919 scored toc 100 / sectioning 100 with
    # completeness 46.6, so tier 3 recorded "not needed: structure is sound" on the
    # same scorecard that recorded hard_fail "completeness 46.6 is below 90" — the
    # chain declined the only tier it had left and then reported the document as
    # unrecoverable. Structure being sound and content being lost are different claims.
    #
    # This is safe to force now in a way it was not when the structural gate was added.
    # The ADGM__170680 regression it was protecting against (MinerU's re-parse adopted
    # with sectioning 54.1 against the rescue's 100) came from an accept test that
    # never looked at sectioning; _better() now compares `worst_score`, which is the
    # minimum over the CRITICAL dimensions and therefore includes sectioning. A
    # structurally worse re-parse can still run, but it can no longer be adopted.
    comp = _completeness(sc)
    if recovered and comp is not None and comp < CHAIN_ENTRY_COMPLETENESS:
        recovered = False
        why = (f"{why} — but completeness {comp} < {CHAIN_ENTRY_COMPLETENESS:.0f}, "
               f"so there is still content to look for")
    if not recovered:
        val, sc, rec = run("mineru_fallback",
                           lambda: _mineru_full(dest, pdf, val, sc,
                                                backend=backend, effort=effort))
        chain.append(rec)
    else:
        chain.append({"tier": "mineru_full", "adopted": False,
                      "status": f"not needed: {why}"})

    fb = sc.setdefault("fallback", {})
    fb["triggered"] = True
    fb["first_attempt"] = first     # always Stage 1, whatever a later tier recorded
    fb["chain"] = chain
    fb["adopted_tier"] = next((r["tier"] for r in reversed(chain) if r.get("adopted")),
                              "stage1")
    # Every tier has now been tried. If none of them cleared the bar, say so
    # explicitly rather than handing back the least-bad number as if it were a
    # result: "the pipeline has nothing better to offer, a human must look".
    ok, why = result_is_acceptable(sc)
    fb["accepted"] = ok
    if not ok:
        fb["hard_fail"] = True
        fb["hard_fail_reason"] = why
    return val, sc
