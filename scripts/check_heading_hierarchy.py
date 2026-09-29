#!/usr/bin/env python3
"""check_heading_hierarchy.py — is the SHAPE of the section tree right?

check_table_placement.py asks whether a table landed under the right heading.
This asks a different question nothing else covers: is the heading tree itself
correct? A table can sit under exactly the right heading while that heading is
nested in the wrong parent — 2.1 filed under section 1 instead of section 2 —
and every content and placement check still passes, because all the text is
present and each table is under its own heading.

The PDF's own bookmark outline states the intended depth of every heading
(level 1, 2, 3 …). pdf2mdtree turns that into real directories and files. So the
outline is the answer key and the folder tree is the answer, and the two can be
compared directly:

  * DEPTH      — does each heading sit at the nesting depth its level implies?
  * PARENTAGE  — is each heading's parent the heading that immediately precedes
                 it at one level up? (the 2.1-under-1 failure)
  * ORDER      — does the tree preserve document order?
  * COMPLETENESS — did every outline heading reach the tree at all?

Depth is capped by --depth at extraction time (default 3), so headings deeper
than that legitimately collapse into their parent file; those are excluded
rather than reported.

Usage:
    python scripts/check_heading_hierarchy.py out/hybrid_extractions/_ui/<job_id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (  # noqa: E402
    BOLD, DIM, GREEN, RED, YELLOW, banner, parse_page_range, resolve_stage_dir,
    resolve_tree_pdf, run_cli,
)
from product_rules import census_levels  # noqa: E402

# pdf2mdtree slugs a title into a file/dir name; recover enough of it to match a
# heading back to its node without depending on the exact slug rules.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug_key(s: str, n: int = 24) -> str:
    """Comparable key for a title or a tree node name. pdf2mdtree zero-pads the
    ordering prefix it adds to directories ("4. Aggregation" -> "04-aggregation"),
    so leading zeros must be normalised away or every numbered section reads as
    mis-parented."""
    # Strip the ordering prefix while its separator is still there to bound it.
    # subchunk.py renumbers the appendices with a LETTER prefix ("13-disclaimers-
    # open-ended-fund" -> "A1-disclaimers-open-ended-fund") so that letters sort after
    # digits and document order survives. The digit strip below cannot see that — it
    # runs after punctuation is gone, by which point "A1-disclaimers" is
    # "a1disclaimers" and the prefix is no longer separable. Measured on
    # Bahamas-sonnet-nocounts: all four disclaimer appendices reported as sections the
    # tree never built, purely because of the rename.
    t = re.sub(r"^[a-z]?\d+[-.\s]+", "", (s or "").lower())
    t = _SLUG_STRIP.sub("", t)
    # pdf2mdtree prefixes tree nodes with an ordering token that may not exist in
    # the title at all ("Appendix 1 …" -> "06-appendix-1-…"), and zero-pads the
    # ones that do ("4." -> "04-"). Dropping leading digits from BOTH sides is the
    # only reliable way to line them up. Trade-off: two sections whose titles
    # differ only by their number would collide — that can mask a real error but
    # cannot invent one, which is the right direction to fail in.
    t = re.sub(r"^\d+", "", t)
    return t[:n]


# A file that stands for the DIRECTORY holding it rather than for a section of its
# own. README.md is pdf2mdtree's name for it; 00-section.md is subchunk.py's, holding
# the section heading and intro above the sub-section files beside it. Both had to be
# listed or the census reads a whole sub-chunked section as never built: stage 5 turns
# `06-marketing-selling-to-the-public.md` into a directory of the same name, and with
# only README.md recognised nothing in it represented that section at all. Measured on
# Bahamas-sonnet-nocounts at stage 5: 8 of 15 promised sections reported missing, every
# one of them present.
_DIR_INDEX_NAMES = {"README.md", "00-section.md"}
_SKIP_NAMES = {"CONVERSION_REPORT.md", "STAGE3_REPORT.md", "STAGE4_REPORT.md",
               "PIPELINE_SUMMARY.md"}


def _tree_nodes(tree_root: Path) -> list[dict]:
    """Every markdown node with its depth (directory nesting) and parent chain."""
    skip = _DIR_INDEX_NAMES | _SKIP_NAMES
    out = []
    for p in sorted(tree_root.rglob("*.md")):
        rel = p.relative_to(tree_root)
        parts = list(rel.parts)
        if p.name in skip:
            # An index file represents its own directory as a section node.
            if len(parts) == 1 or p.name in _SKIP_NAMES:
                continue
            out.append({"kind": "dir", "path": str(rel), "depth": len(parts) - 1,
                        "name": parts[-2], "parents": parts[:-2]})
            continue
        out.append({"kind": "file", "path": str(rel), "depth": len(parts),
                    "name": parts[-1][:-3], "parents": parts[:-1]})
    return out


def _outline_headings(out_root: Path, tree_root: Path) -> list[dict]:
    """What the PDF itself says its sections are.

    This is the only answer key that is independent of the extraction. The
    manifest below records what Stage 1 BUILT, so a section Stage 1 never noticed
    is missing from the manifest too and cannot be reported against it — which is
    exactly the failure mode worth catching, because the commonest cause (a
    heading printed inside a table region Stage 1 defers to MinerU) means Stage 1
    never saw the heading at all."""
    try:
        with fitz.open(resolve_tree_pdf(out_root)) as doc:
            toc = doc.get_toc() or []
    except Exception:  # noqa: BLE001 — no PDF, no outline, or an unreadable one
        return []
    # 1-INDEXED, like every other page number this pipeline reports: the section
    # breadcrumbs, tables_manifest, the page heat-map and every other finding. This
    # returned pg-1 and nothing converted it back, so the census named the page
    # BEFORE the one the outline points at — "5 Recognised Overseas Regulatory
    # Regimes" is on page 150 and was reported as 149. It also made the
    # front-matter comparison below compare a 0-indexed bound against a 1-indexed
    # node range, which let the printed contents page count as an unpromised extra.
    return [{"level": lvl, "title": (title or "").strip(), "page": pg}
            for lvl, title, pg in toc if (title or "").strip()]


_LEAD_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.)\s]")
# Share of an outline's candidate entries that must carry a leading section number
# before an UNNUMBERED entry is treated as anomalous for that document. Deliberately
# self-calibrating: plenty of documents number nothing and their unnumbered headings
# are perfectly real, so the test is "does this document number its sections, and is
# this one the exception" rather than "is it numbered".
NUMBERED_CONVENTION = 0.8


def _implied_level(title: str) -> int | None:
    """The depth a title's OWN numbering implies — '7.4 Foo' is 2 whatever level the
    outline claims. None when it carries no number.

    The outline's levels cannot be trusted on a rescued document: rescue_outline's
    _legal_levels shifts and clamps whatever the printed contents page yielded, so a
    subsection can arrive at level 1. Australia__181814's section 7.4 does exactly
    that, and then gets charged against a rule that says subsections do not exist."""
    m = _LEAD_NUM_RE.match(title or "")
    return None if not m else m.group(1).count(".") + 1


def _collapsed_by_rule(promised: list[dict], max_depth: int) -> tuple[list[dict], list[dict]]:
    """Split the outline's candidates into (real sections, entries the rule collapses).

    Two reasons an entry is not a section this tree was ever going to build:

    ITS OWN NUMBER SAYS SUBSECTION. '7.4' implies depth 2; under a flat rule
    (max_depth 1) it is collapsed into clause 7 by design, so counting it as a
    missing section charges the extraction for obeying its own rule.

    IT IS NOT A HEADING AT ALL. A document that numbers its sections and then
    yields one unnumbered entry has an outline defect, not a section — on
    Australia__181814 those are 'Services)' and 'Vehicles, Segregated Managed
    Accounts and Family Offices', the second and third lines of section 7.4's title
    after the TOC parser split it at the line breaks. 16 of that document's 18
    level-1 entries are numbered, so the two that are not stand out; on a document
    that numbers nothing this test never fires."""
    numbered = [h for h in promised if _implied_level(h["title"]) is not None]
    convention = len(numbered) / max(len(promised), 1) >= NUMBERED_CONVENTION

    real, collapsed = [], []
    for h in promised:
        lvl = _implied_level(h["title"])
        if lvl is not None and lvl > max_depth:
            collapsed.append({**h, "why": f"its own number implies depth {lvl}, "
                                          f"which this tree collapses"})
        elif lvl is None and convention:
            collapsed.append({**h, "why": "no section number, in a document that numbers "
                                          "its sections — an outline fragment, not a heading"})
        else:
            real.append(h)
    return real, collapsed


def _section_census(out_root: Path, tree_root: Path, nodes: list[dict],
                    max_depth: int) -> dict:
    """Did the tree build the sections the document says it has — no more, no fewer?

    Deliberately separate from the nesting checks below, and computed against the
    PDF outline rather than the manifest they use. Both directions are reported:
    FEWER means sections were dropped (their content merged into a neighbour);
    MORE means the tree invented boundaries the document does not have, which
    splits one clause across two nodes and is just as bad for retrieval.

    Returns available=False when the PDF has no outline. There is then no
    independent statement of what the document contains, so any count this could
    produce would be the extraction grading its own work."""
    outline = _outline_headings(out_root, tree_root)
    if not outline:
        return {"available": False,
                "reason": "the PDF has no bookmark outline to compare the tree against"}

    by_key: dict[str, list[dict]] = {}
    for n in nodes:
        by_key.setdefault(_slug_key(n["name"]), []).append(n)

    promised = [h for h in outline if h.get("level", 1) <= max_depth]
    # An entry the rule was never going to build is not a missing section. Split
    # those out BEFORE anything is counted, so they leave the numerator and the
    # denominator together (see _collapsed_by_rule).
    promised, collapsed = _collapsed_by_rule(promised, max_depth)
    if not promised:
        # Every outline entry is deeper than the depth Stage 1 ran with, so all of
        # them legitimately collapse into a parent file and none was ever going to
        # be a node. Nothing to compare — and the `min()` below has no sequence.
        return {"available": False,
                "reason": f"every outline entry is deeper than the --depth {max_depth} "
                          f"cap Stage 1 ran with, so none was expected to become a node"}
    used: set[str] = set()
    missing = []
    # Every promised section in document order, with the node it landed in (or
    # None). This is what lets the scorecard draw the document's own outline with
    # a mark against each entry — a list of missing titles says WHAT is gone, the
    # map says WHERE the holes are, which is what tells you whether a whole branch
    # collapsed or one clause slipped.
    sections = []
    for h in promised:
        cands = [n for n in by_key.get(_slug_key(h["title"]), []) if n["path"] not in used]
        node = cands[0]["path"] if cands else None
        if cands:
            used.add(node)
        else:
            missing.append({"title": h["title"], "level": h["level"], "page": h.get("page")})
        sections.append({"title": h["title"], "level": h["level"],
                         "page": h.get("page"), "node": node})

    # Pages before the outline's first entry are recovered on purpose — a
    # publisher who bookmarks from page 3 leaves the cover and contents page
    # un-promised, and Stage 1 rebuilds them with the font-size heuristic rather
    # than dropping their content. Those nodes are not "extra" in any meaningful
    # sense, so charging for them would report a deliberate recovery as a defect.
    first_page = min(h["page"] for h in promised)
    extra = []
    for n in nodes:
        if n["path"] in used:
            continue
        # An overview node is scaffolding the tree builder emits for a section
        # that has both intro prose and children. It is not a claim that the
        # document contains a section by that name, so it cannot be "extra".
        if Path(n["path"]).name == "00-overview.md":
            continue
        rng = _node_pages(tree_root / n["path"])
        if rng and rng[0] < first_page:
            continue
        extra.append({"path": n["path"], "pages": list(rng) if rng else None})

    return {
        "available": True,
        "promised": len(promised),
        "delivered": len(promised) - len(missing),
        "nodes_in_tree": len(nodes),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "outline_starts_page": first_page,
        "missing": missing[:25],
        "extra": extra[:25],
        "sections": sections,
        # Reported, never scored: entries the outline lists that this tree's rule
        # collapses or that are not headings at all. Shown so nothing disappears
        # silently — an outline defect should be visible as an outline defect.
        "collapsed_count": len(collapsed),
        "collapsed": [{"title": c["title"], "page": c.get("page"), "why": c["why"]}
                      for c in collapsed[:25]],
    }


def _node_pages(path: Path) -> tuple[int, int] | None:
    """A node's declared page range, read from its own breadcrumb. Both node
    kinds point at a real markdown file — a directory node at the README that
    represents it (see _tree_nodes)."""
    try:
        return parse_page_range(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def compute_heading_hierarchy(out_root: Path, stage: int = 1, max_depth: int = 3,
                              tree_stage: int = 3) -> dict:
    """`stage` is where the headings MANIFEST lives — Stage 1's record of what it
    built, which never moves. `tree_stage` is which output tree to compare against.

    They are separate because the manifest describes the input and the tree is the
    output, and only the second one changes when a later stage rewrites the document.
    The census below was pinned to stage 3 regardless, so the post-AI scorecard graded
    a tree that stage 4 and stage 5 had already replaced — the one dimension that can
    say "this section was never built" was answering for the wrong document."""
    stage1 = resolve_stage_dir(out_root, stage)
    hpath = stage1 / "headings_manifest.json"
    if not hpath.exists():
        return {"passed": True, "skipped": True,
                "reason": f"{hpath.name} not found — re-run Stage 1 to generate it",
                "flags": []}
    manifest = json.loads(hpath.read_text())
    headings = manifest.get("headings", [])
    if not headings:
        return {"passed": True, "skipped": True, "reason": "no headings in manifest",
                "flags": []}

    tree_root = resolve_stage_dir(out_root, tree_stage)
    nodes = _tree_nodes(tree_root)
    by_key: dict[str, list[dict]] = {}
    for n in nodes:
        by_key.setdefault(_slug_key(n["name"]), []).append(n)

    flags = []
    matched = 0
    used: set[str] = set()
    # Expected parent = the nearest preceding heading exactly one level shallower.
    # That IS the definition of correct nesting, and the 2.1-under-1 bug is
    # precisely this relation being violated.
    for i, h in enumerate(headings):
        lvl = h["level"]
        if lvl > max_depth:
            continue                       # legitimately collapsed into its parent
        key = _slug_key(h["title"])
        all_cands = by_key.get(key) or []
        if not all_cands:
            flags.append({"kind": "missing", "title": h["title"], "level": lvl,
                          "page": h.get("page"),
                          "detail": "this outline heading has no node in the tree — its "
                                    "content was merged into another section"})
            continue
        matched += 1

        expected_parent = None
        for j in range(i - 1, -1, -1):
            if headings[j]["level"] == lvl - 1:
                expected_parent = headings[j]
                break
            if headings[j]["level"] < lvl - 1:
                break                      # a shallower heading intervened

        # Sub-section titles repeat across parts in these documents — every part
        # has its own "Sources of Law" — so several tree nodes share one key. The
        # question is whether a node for this heading exists under the RIGHT
        # parent, not whether the first node with that name happens to. So prefer
        # a candidate whose parent matches, and consume nodes so repeated titles
        # map one-to-one in document order.
        cands = [c for c in all_cands if c["path"] not in used] or all_cands
        node = None
        want = _slug_key(expected_parent["title"]) if expected_parent is not None else ""
        if want:
            node = next((c for c in cands
                         if c["parents"] and _slug_key(c["parents"][-1]) == want), None)
        if node is None:
            node = cands[0]
        used.add(node["path"])

        if expected_parent is not None and node["parents"]:
            got = _slug_key(node["parents"][-1])
            if got and want and got != want:
                flags.append({
                    "kind": "wrong_parent", "title": h["title"], "level": lvl,
                    "page": h.get("page"), "path": node["path"],
                    "expected_parent": expected_parent["title"],
                    "detail": f"outline says this belongs under \"{expected_parent['title']}\" "
                              f"but the tree files it under \"{node['parents'][-1]}\"",
                })

        # Depth: a level-N heading should sit N-1 directories deep (its own file
        # being the Nth component). Tolerate one level of drift, which the
        # depth cap and single-child folder collapsing can legitimately cause.
        if node["kind"] == "file" and abs(node["depth"] - lvl) > 1:
            flags.append({
                "kind": "wrong_depth", "title": h["title"], "level": lvl,
                "page": h.get("page"), "path": node["path"], "tree_depth": node["depth"],
                "detail": f"outline level {lvl} but nested {node['depth']} deep in the tree",
            })

    counts = {}
    for f in flags:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {
        "headings_total": len(headings),
        "headings_matched": matched,
        "structure_source": manifest.get("structure_source"),
        "flags": flags,
        "counts": counts,
        # Promised vs delivered, against the PDF's own outline. Kept apart from
        # `flags` above on purpose: those are measured against the manifest, which
        # is Stage 1's record of what it built, so they cannot see a section Stage
        # 1 never knew about. Nothing here feeds `passed` — the census is scored
        # by check_scorecard (_score_sectioning), not by this check.
        # Measured against the depth the tree was TRYING to build, not this
        # function's default. A product whose subsections cannot be split is
        # built flat on purpose (see product_rules.SECTION_DEPTH); graded at the
        # default it reports every collapsed subsection as a missing section,
        # which is the opposite of the truth.
        "census": _section_census(out_root, tree_root, nodes,
                                  census_levels(manifest.get("build_depth")) or max_depth),
        # The rules this tree was BUILT under, carried through so the scorecard can
        # declare them: a document built flat has smaller denominators in every
        # ratio-based dimension and its score is not comparable with a sibling's.
        "build_depth": manifest.get("build_depth"),
        "clause_tables": manifest.get("clause_tables"),
        # Which output tree the census above actually graded. Stamped rather than
        # assumed: both scorecards have the same shape, and a reader who cannot tell
        # a stage-3 census from a stage-5 one will read "3 sections never built"
        # against the wrong document.
        "tree_stage": tree_stage,
        # A wrong parent is a real structural defect. A heading merged into its
        # parent is usually the known text-match limitation and is reported but
        # does not fail on its own.
        "passed": counts.get("wrong_parent", 0) == 0 and counts.get("wrong_depth", 0) == 0,
    }


def print_report(report: dict, top: int = 25):
    if report.get("skipped"):
        print(YELLOW(f"skipped — {report['reason']}"))
        return
    print(f"{DIM('headings:')} {report['headings_matched']} matched of {report['headings_total']}"
          f"    {DIM('source:')} {report.get('structure_source')}")

    c = report.get("census") or {}
    if not c.get("available"):
        print(DIM(f"section census: skipped — {c.get('reason', 'not computed')}"))
    else:
        bad = c["missing_count"] or c["extra_count"]
        paint = RED if bad else GREEN
        print(f"{BOLD('section census')}   : the PDF outline promises {c['promised']} section(s); "
              f"the tree delivers " + paint(str(c['delivered'])))
        if c["missing_count"]:
            print(RED(f"  {c['missing_count']} PROMISED BUT NOT BUILT") + DIM(
                "  — content merged into a neighbouring section"))
            for m in c["missing"][:top]:
                print(f"      {BOLD(m['title'][:64])}  {DIM('(level ' + str(m['level'])
                                                           + ', page ' + str(m['page']) + ')')}")
        if c["extra_count"]:
            print(YELLOW(f"  {c['extra_count']} BUILT BUT NOT PROMISED") + DIM(
                "  — a boundary the outline does not have, or a recovered heading"))
            for e in c["extra"][:top]:
                print(f"      {e['path']}  {DIM('pages ' + str(e['pages']))}")
    print()

    if not report["flags"]:
        print(f"\n{GREEN(BOLD('✓ PASS — every heading sits at the right depth under the right parent'))}")
        return
    label = {"wrong_parent": RED("WRONG PARENT"), "wrong_depth": RED("WRONG DEPTH"),
             "missing": YELLOW("NOT IN TREE")}
    for f in report["flags"][:top]:
        print(f"\n    {label.get(f['kind'], f['kind'])}  {BOLD(f['title'][:60])}"
              f"  {DIM('(level ' + str(f['level']) + ', page ' + str(f.get('page')) + ')')}")
        print(f"      {DIM(f['detail'])}")
    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — nesting is correct")) + DIM(
            f"  ({report['counts'].get('missing', 0)} heading(s) merged into a parent)"))
    else:
        n = report["counts"].get("wrong_parent", 0) + report["counts"].get("wrong_depth", 0)
        print(RED(BOLD(f"✗ FAIL — {n} heading(s) nested incorrectly")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--depth", type=int, default=3, help="the --depth Stage 1 ran with")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Heading hierarchy · tree shape vs the PDF outline")
    report = compute_heading_hierarchy(Path(args.out_root).resolve(), max_depth=args.depth)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
