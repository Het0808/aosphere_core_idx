#!/usr/bin/env python3
"""fault_injection.py — who tests the tests?

Every other script here checks an extraction. Nothing checks the CHECKERS, so a
regression in one of them would pass unnoticed — and that nearly happened
already: a fix that recovered a merged table silently stopped emitting footnote
definitions, and it was caught by hand rather than by anything automated.

This takes a known-good extraction, deliberately breaks it in one specific way,
and asserts the right check notices. It measures the checks, not the pipeline —
and needs no human labelling, which is what makes it the cheapest way to stop
the scores being purely a matter of opinion.

Each fault is a HISTORICAL REGRESSION: every one corresponds to a real bug found
in this pipeline, so once fixed it stays fixed.

    drop_paragraph   a whole paragraph deleted from a section
    transpose_number a number's digits swapped — same words, wrong value
    drop_decimal     a decimal point removed — a tenfold error that reads fine
    flip_negation    "is not X" -> "is X": wording intact, meaning inverted
    move_table       a table's content moved into the wrong section
    move_heading     a heading filed under the wrong parent (the 2.1-under-1 bug)
    scan_page        a page replaced by an image, i.e. text fitz cannot read
    delete_table     a table's rendered rows removed entirely

Usage:
    python scripts/fault_injection.py out/hybrid_extractions/_ui/<job_id>
    python scripts/fault_injection.py <job_dir> --only flip_negation --keep
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import BOLD, DIM, GREEN, RED, YELLOW, banner, tokenize

STAGE_DIRS = ("01_stage1_extract", "02_stage2_mineru_tables", "03_stage3_final")


# ---------------- the faults ----------------
def _content_files(tree: Path):
    skip = {"README.md", "CONVERSION_REPORT.md", "STAGE3_REPORT.md", "PIPELINE_SUMMARY.md"}
    return [p for p in sorted(tree.rglob("*.md")) if p.name not in skip]


def drop_paragraph(root: Path) -> str | None:
    for f in _content_files(root / "03_stage3_final"):
        t = f.read_text()
        paras = [x for x in t.split("\n\n")
                 if len(x) > 300 and not x.lstrip().startswith(("#", "*Source", ">", "<", "!["))]
        if paras:
            f.write_text(t.replace(paras[0] + "\n\n", "", 1))
            return f"deleted a {len(paras[0])}-char paragraph from {f.name}"
    return None


_NUM_IN_TREE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?%?)(?![\w])")


def _swap_last_two_digits(tok: str) -> str | None:
    """'35%' -> '53%', '1,234' -> '1,243'. Same digits, different value — the
    corruption that survives every presence check because nothing is missing."""
    idx = [i for i, c in enumerate(tok) if c.isdigit()]
    for a, b in zip(reversed(idx[:-1]), reversed(idx[1:])):
        if tok[a] != tok[b]:
            out = list(tok)
            out[a], out[b] = out[b], out[a]
            return "".join(out)
    return None


def transpose_number(root: Path) -> str | None:
    """Transpose two digits of a number in the tree.

    The value is now wrong but every word is still present, so word coverage,
    content-localisation and table-presence all stay green — only
    check_numeric_integrity can see it. This is the fault that would have caught
    the percentage bug: is_transposition used rstrip('0123456789,.') to find the
    suffix, which cannot reach past a trailing '%', so '35%' vs '53%' compared as
    DIFFERENT suffixes and percentage transpositions were never detected at all."""
    files = _content_files(root / "03_stage3_final")
    whole = "\n".join(f.read_text() for f in files)
    for f in files:
        t = f.read_text()
        for m in _NUM_IN_TREE.finditer(t):
            tok = m.group(1)
            if len(re.sub(r"\D", "", tok)) < 3 and not tok.endswith("%"):
                continue                       # is_transposition ignores these
            swapped = _swap_last_two_digits(tok)
            if not swapped:
                continue
            # the original must be unique in the tree (so removing it makes the
            # value genuinely missing) and the swapped form must be new (so it
            # shows up as extra rather than colliding with a real value)
            if whole.count(tok) != 1 or swapped in whole:
                continue
            f.write_text(t[:m.start(1)] + swapped + t[m.end(1):])
            return f"transposed {tok} -> {swapped} in {f.name}"
    return None


def drop_decimal(root: Path) -> str | None:
    """Remove a decimal point: '1.5' -> '15', a tenfold error that reads fine."""
    files = _content_files(root / "03_stage3_final")
    whole = "\n".join(f.read_text() for f in files)
    for f in files:
        t = f.read_text()
        for m in re.finditer(r"(?<![\w.])(\d+)\.(\d+)(?![\w])", t):
            merged = m.group(1) + m.group(2)
            if whole.count(m.group(0)) != 1 or merged in whole:
                continue
            f.write_text(t[:m.start()] + merged + t[m.end():])
            return f"dropped the decimal point in {m.group(0)} -> {merged} in {f.name}"
    return None


def flip_negation(root: Path) -> str | None:
    for f in _content_files(root / "03_stage3_final"):
        t = f.read_text()
        m = re.search(r"\bis not\b", t)
        if m:
            f.write_text(t[:m.start()] + "is" + t[m.end():])
            return f"inverted a clause in {f.name}: 'is not' -> 'is'"
    return None


def delete_table(root: Path) -> str | None:
    for f in _content_files(root / "03_stage3_final"):
        t = f.read_text()
        m = re.search(r"<table>.*?</table>", t, re.S)
        if m and len(m.group(0)) > 400:
            f.write_text(t[:m.start()] + t[m.end():])
            return f"removed a {len(m.group(0))}-char rendered table from {f.name}"
    return None


def move_table(root: Path) -> str | None:
    """Cut a table out of its own section and paste it into an unrelated one —
    content still present in the tree, but under the wrong heading."""
    files = _content_files(root / "03_stage3_final")
    src = dst = None
    for f in files:
        if re.search(r"<table>.*?</table>", f.read_text(), re.S):
            src = f
            break
    if src is None:
        return None
    for f in files:
        if f != src and f.parent != src.parent:
            dst = f
            break
    if dst is None:
        return None
    t = src.read_text()
    m = re.search(r"<table>.*?</table>", t, re.S)
    src.write_text(t[:m.start()] + t[m.end():])
    dst.write_text(dst.read_text() + "\n\n" + m.group(0) + "\n")
    return f"moved a table from {src.name} into {dst.name} (different section)"


def move_heading(root: Path) -> str | None:
    """File a section under the wrong parent — the 2.1-under-section-1 bug.

    The moved file must correspond to a REAL outline heading. pdf2mdtree also
    creates synthetic nodes ("00-overview") that appear in no outline, so moving
    one of those tests nothing: the hierarchy check has no heading to reason
    about and correctly stays silent."""
    tree = root / "03_stage3_final"
    mp = root / "01_stage1_extract" / "headings_manifest.json"
    if not mp.exists():
        return None
    # Match on the "N.M" number, not the title. Titles collide badly here —
    # "00-overview.md" (an appendix's synthetic overview node) slugs identically
    # to the real heading "1. Overview", so a title match moves the wrong file
    # and the check rightly stays silent.
    nums = {h["title"].split()[0].rstrip(".")
            for h in json.loads(mp.read_text()).get("headings", [])
            if h["level"] >= 2 and re.match(r"^\d+\.\d+", h["title"])}
    dirs = [d for d in tree.iterdir()
            if d.is_dir() and not d.name.startswith("_") and (d / "README.md").exists()]
    if len(dirs) < 2:
        return None
    for d in dirs:
        for leaf in sorted(d.rglob("*.md")):
            if leaf.name == "README.md":
                continue
            m = re.match(r"^(\d+\.\d+)-", leaf.name)
            if not m or m.group(1) not in nums:
                continue
            other = next((o for o in dirs if o != d), None)
            if other is None:
                return None
            shutil.move(str(leaf), str(other / leaf.name))
            return f"moved {leaf.name} from {d.name}/ into {other.name}/"
    return None


def scan_page(root: Path) -> str | None:
    """Replace a text page with a rendered image — text fitz cannot read at all,
    the case that used to score a perfect 100%."""
    import fitz
    pdf = root / "source.pdf"
    doc = fitz.open(str(pdf))
    try:
        if doc.page_count < 4:
            return None
        target = doc.page_count // 2
        out = fitz.open()
        for i in range(doc.page_count):
            if i == target:
                pix = doc[i].get_pixmap(matrix=fitz.Matrix(2, 2))
                pg = out.new_page(width=doc[i].rect.width, height=doc[i].rect.height)
                pg.insert_image(pg.rect, pixmap=pix)
            else:
                out.insert_pdf(doc, from_page=i, to_page=i)
        out.save(str(pdf.with_suffix(".tmp.pdf")))
        out.close()
    finally:
        doc.close()
    pdf.with_suffix(".tmp.pdf").replace(pdf)
    return f"replaced page {target + 1} with an image (no text layer)"


# fault -> (injector, the check that must notice, how to read its report,
#           known_gap reason or None)
#
# A fault marked known_gap is a hole we have MEASURED and not yet closed. It is
# kept in the suite so the gap stays visible, but it does not fail the run —
# otherwise the suite would be permanently red and stop being read. Closing one
# means deleting its reason, and the suite then enforces it forever.
FAULTS = {
    "drop_paragraph": (drop_paragraph, "content_localized",
                       lambda r: r.get("silent_count", 0), None),
    "flip_negation": (flip_negation, "semantic_integrity",
                      lambda r: r.get("negation_flips", 0), None),
    "move_heading": (move_heading, "heading_hierarchy",
                     lambda r: len([f for f in r.get("flags", [])
                                    if f.get("kind") in ("wrong_parent", "wrong_depth")]), None),
    "scan_page": (scan_page, "engine_agreement",
                  lambda r: r.get("unreadable_count", 0), None),
    # Closed: check_table_presence compares extracted cells against the tree as an
    # unordered set, so neither near-identical siblings nor row-major reordering
    # can hide a deleted table. Enforced from here on.
    "delete_table": (delete_table, "table_presence",
                     lambda r: r.get("missing_count", 0), None),
    "transpose_number": (transpose_number, "numeric_integrity",
                         lambda r: len(r.get("transpositions", [])), None),
    # A dropped decimal changes the DIGIT MULTISET, so is_transposition rejects it
    # (it requires the same digits in a different order). It lands in `missing`
    # instead, and missing numbers are deliberately unscored — measurement showed
    # they are overwhelmingly footnote/list/TOC markers, and penalising them pinned
    # Integrity at 0. So a tenfold error currently passes every check. Recorded so
    # the hole stays visible rather than being discovered by a reader.
    "drop_decimal": (drop_decimal, "numeric_integrity",
                     lambda r: len(r.get("transpositions", [])),
                     "a dropped decimal point is not a digit permutation, so "
                     "is_transposition cannot see it, and it falls through to the "
                     "unscored `missing numbers` bucket. Needs either a "
                     "same-digits-different-magnitude rule in check_numeric_integrity "
                     "or a scored severity for missing numbers that carry context."),
    "move_table": (move_table, "content_localized",
                   lambda r: r.get("silent_count", 0),
                   "the content is still in the tree, just under the wrong heading, so a "
                   "presence check cannot see it. check_table_placement only compares PAGE "
                   "RANGES from the manifest, not where the rendered table actually landed. "
                   "This is the content-level placement purity work."),
}


def _run_check(name: str, root: Path) -> dict:
    from check_content_localized import compute_content_localized
    from check_engine_agreement import compute_engine_agreement
    from check_heading_hierarchy import compute_heading_hierarchy
    from check_numeric_integrity import compute_numeric_integrity
    from check_semantic_integrity import compute_semantic_integrity
    from check_table_presence import compute_table_presence
    fn = {"content_localized": compute_content_localized,
          "table_presence": compute_table_presence,
          "engine_agreement": compute_engine_agreement,
          "heading_hierarchy": compute_heading_hierarchy,
          "numeric_integrity": compute_numeric_integrity,
          "semantic_integrity": compute_semantic_integrity}[name]
    return fn(root)


def _tree_text(work: Path) -> dict[str, str]:
    """Every content file's raw text, keyed by name — the before/after snapshot."""
    tree = work / "03_stage3_final"
    return {str(p.relative_to(tree)): p.read_text(encoding="utf-8")
            for p in _content_files(tree)}


def _surviving_copy(before: dict[str, str], after: dict[str, str],
                    min_run: int = 12) -> str | None:
    """Did the injected fault actually remove content from the TREE, or does a copy
    survive elsewhere in it?

    Without this the suite lies in the safest-looking direction. Measured on
    124_Marketing_Restrictions/Bahamas-sonnet-nocounts, dropping a paragraph from each
    of the 8 eligible files in turn: 4 of the 6 "MISSED" verdicts were cases where the
    same text is also rendered inside a questionnaire table, so the content never left
    the document and the check was RIGHT to stay silent. Reporting those as misses
    makes a green suite impossible and a red one meaningless.

    -> None when content genuinely left the tree (the check is then obliged to fire),
    otherwise the surviving run, so the report can say why the fault tested nothing.
    """
    haystack = " ".join(" ".join(tokenize(t)) for t in after.values())
    for name, old in before.items():
        new = after.get(name, "")
        if old == new:
            continue
        old_toks, new_toks = tokenize(old), tokenize(new)
        sm = SequenceMatcher(a=old_toks, b=new_toks, autojunk=False)
        for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
            if tag not in ("delete", "replace") or (i2 - i1) < min_run:
                continue
            run = " ".join(old_toks[i1:i2])
            # Judge on the TAIL. A removed paragraph's opening line is often a heading
            # repeated elsewhere in the tree, so matching on the start of the run
            # reports "a copy survives" for text whose body is genuinely gone.
            tail = " ".join(old_toks[max(i1, i2 - min_run):i2])
            if tail not in haystack:
                return None
            return run[:160]
    return None                      # nothing removed at all -> nothing to survive


def run_suite(job_dir: Path, only: str | None = None, keep: bool = False) -> dict:
    job_dir = Path(job_dir).resolve()
    missing = [d for d in STAGE_DIRS[:1] + ("03_stage3_final",) if not (job_dir / d).exists()]
    if missing or not (job_dir / "source.pdf").exists():
        return {"passed": False, "error": f"job dir incomplete (need source.pdf + {STAGE_DIRS[0]} "
                                          f"+ 03_stage3_final); missing {missing}", "results": []}

    names = [only] if only else list(FAULTS)
    results = []
    for name in names:
        inject, check_name, metric, known_gap = FAULTS[name]
        work = Path(tempfile.mkdtemp(prefix=f"fault_{name}_"))
        try:
            shutil.copy2(job_dir / "source.pdf", work / "source.pdf")
            for d in STAGE_DIRS:
                if (job_dir / d).exists():
                    shutil.copytree(job_dir / d, work / d)
            # No manifest repointing needed: lib_content_compare.resolve_pdf now
            # prefers <job>/source.pdf over the manifest's absolute path, so a
            # copied job dir is evaluated against ITS OWN pdf. Without that, a
            # fault which edits the PDF (scan_page) was silently checked against
            # the pristine original and looked undetectable.
            before = metric(_run_check(check_name, work))
            tree_before = _tree_text(work)
            note = inject(work)
            if note is None:
                results.append({"fault": name, "check": check_name, "status": "skipped",
                                "detail": "no suitable target in this document"})
                continue
            # Did this fault actually take content out of the document? A fault whose
            # text is still readable elsewhere in the tree tests nothing, and scoring
            # the check MISSED on it is simply wrong.
            survivor = _surviving_copy(tree_before, _tree_text(work))
            after = metric(_run_check(check_name, work))
            caught = after > before
            if not caught and survivor is not None:
                results.append({"fault": name, "check": check_name, "status": "no_loss",
                                "injected": note, "survivor": survivor,
                                "detail": "a copy of this text survives elsewhere in the "
                                          "tree, so nothing was lost and silence is correct"})
                continue
            results.append({"fault": name, "check": check_name,
                            "status": "caught" if caught else
                                      ("known_gap" if known_gap else "MISSED"),
                            "known_gap": known_gap,
                            "injected": note, "before": before, "after": after})
        except Exception as e:  # noqa: BLE001 — a broken fault must not hide the others
            results.append({"fault": name, "check": check_name, "status": "error",
                            "detail": str(e)})
        finally:
            if keep:
                print(DIM(f"  kept: {work}"))
            else:
                shutil.rmtree(work, ignore_errors=True)

    tested = [r for r in results if r["status"] in ("caught", "MISSED")]
    caught = [r for r in tested if r["status"] == "caught"]
    gaps = [r for r in results if r["status"] == "known_gap"]
    return {
        "job": str(job_dir),
        "results": results,
        "faults_tested": len(tested),
        "faults_caught": len(caught),
        "detection_rate_pct": round(100.0 * len(caught) / len(tested), 1) if tested else None,
        "known_gaps": [{"fault": g["fault"], "reason": g["known_gap"]} for g in gaps],
        "passed": len(tested) > 0 and len(caught) == len(tested),
    }


def print_report(report: dict):
    if report.get("error"):
        print(RED(report["error"]))
        return
    print(f"{DIM('job:')} {report['job']}\n")
    for r in report["results"]:
        if r["status"] == "caught":
            print(f"  {GREEN('✓ CAUGHT ')} {BOLD(r['fault'].ljust(16))} by {r['check']:<20} "
                  f"{DIM(str(r['before']) + ' -> ' + str(r['after']))}")
            print(f"             {DIM(r['injected'])}")
        elif r["status"] == "MISSED":
            print(f"  {RED('✗ MISSED ')} {BOLD(r['fault'].ljust(16))} {r['check']:<20} "
                  f"{RED('metric unchanged at ' + str(r['before']))}")
            print(f"             {DIM(r['injected'])}")
        elif r["status"] == "no_loss":
            print(f"  {DIM('· NO LOSS ')}{BOLD(r['fault'].ljust(16))} {r['check']:<20} "
                  f"{DIM('tested nothing — a copy survives')}")
            print(f"             {DIM(r['injected'])}")
        elif r["status"] == "known_gap":
            print(f"  {YELLOW('~ KNOWN   ')}{BOLD(r['fault'].ljust(16))} {r['check']:<20} "
                  f"{YELLOW('not covered yet')}")
            print(f"             {DIM(r['known_gap'][:150])}")
        else:
            print(f"  {YELLOW('- ' + r['status'].ljust(8))} {BOLD(r['fault'].ljust(16))} "
                  f"{DIM(r.get('detail', ''))}")
    print()
    if report["detection_rate_pct"] is None:
        print(YELLOW("no faults could be injected into this document"))
    elif report["passed"]:
        print(GREEN(BOLD(f"✓ PASS — {report['faults_caught']}/{report['faults_tested']} "
                         "enforced faults detected")))
        if report.get("known_gaps"):
            print(YELLOW(f"  {len(report['known_gaps'])} known gap(s) still open: "
                         + ", ".join(g["fault"] for g in report["known_gaps"])))
    else:
        print(RED(BOLD(f"✗ FAIL — only {report['faults_caught']}/{report['faults_tested']} "
                       f"detected ({report['detection_rate_pct']}%); a real defect of the "
                       "MISSED kind would ship unnoticed")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir", help="a COMPLETED job dir (source.pdf + stage dirs)")
    ap.add_argument("--only", choices=list(FAULTS), default=None)
    ap.add_argument("--keep", action="store_true", help="keep the broken copies for inspection")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Fault injection · do the checks actually catch things?")
    report = run_suite(Path(args.job_dir), args.only, args.keep)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
