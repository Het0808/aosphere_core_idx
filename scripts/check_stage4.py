#!/usr/bin/env python3
"""check_stage4.py — did AI post-processing change the document?

Stages 1-3 can only LOSE or MISPLACE content. Stage 4 is the only stage that can
silently REWRITE it, and that is a different question from the one every other check
asks. So this check is not primarily a quality score: its job is to prove, mechanically,
that the text stage 4 emitted is the text stage 3 gave it.

It is judged against STAGE 3, not against the PDF. Stage 3 is stage 4's input and the
only thing it is permitted to differ from, in exactly three known ways:

  * whitespace and <br> moved            (a dropped space or line break restored)
  * cell and row boundaries moved        (a merged label split back into its column)
  * a duplicate label row removed        (promoted to a heading, so still present once)

Anything else is a defect. In particular NOTHING may ever be added: not a word, not a
punctuation mark, not a <strong>, not a bullet glyph. The observed failure modes are all
real, all from Haiku 4.5 on the pilot document — it rewrote (“IFA”) to ("IFA"), invented
<strong> and colspan="2", added bullet glyphs it had seen in a page image, and changed a
cross-reference from "see answer to 5(c)" to "5(a)" four times in one table. Every one of
those is invisible to a coverage check: the words are all still there.

Two design notes worth keeping:

  * The primitives come from the stage-4 module itself (content_stream, split_rows,
    markup_signature, table_width) rather than being reimplemented here. A validator that
    reimplements the gate's own normalisation drifts from it, and then agrees with the
    thing it is supposed to be auditing for the wrong reason.
  * The check trusts stage4_report.json for what stage 4 CLAIMS it removed, then verifies
    that claim against the trees. A claim that does not reconcile is itself a failure, so
    a lying or stale report cannot buy a pass.

Usage:
    python scripts/check_stage4.py out/corpus/<product>/<job>
"""
from __future__ import annotations

import argparse
import collections
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from lib_content_compare import BOLD, DIM, GREEN, RED, YELLOW, banner, run_cli  # noqa: E402

from aosphere_core_index.extract.ai_postprocess import (  # noqa: E402
    _STRUCTURAL_TAGS, _row_width, content_stream, linkify_urls, markup_signature,
    split_rows, strict_stream, table_width,
)

_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_BADGE_LINE = re.compile(r"^.*MinerU-extracted table.*$", re.M)

# The one tag this pipeline adds on purpose, after the model returns: linkify_urls()
# wraps a bare URL in <a href="..." target="_blank" rel="noopener noreferrer">, in code,
# never by the model (see its own docstring). It was added after THIS check was written,
# which is why "the model invented an <a>" and "the pipeline linkified a URL" used to
# score identically: 0.
#
# Verified, not assumed: re-run the SAME deterministic function against stage 3 and
# compare the anchors it produces to the anchors stage 4/5 actually has. A hallucinated
# link, a wrong href, or an <a> the model wrote itself still won't match and the flag
# still stands — this excuses exactly the one addition the pipeline is known to make,
# nothing broader.
_A_TAG = re.compile(r"<a\b[^>]*>.*?</a>", re.S | re.I)


def _links_are_all_linkify_wraps(a_text: str, b_text: str) -> bool:
    expected, _n = linkify_urls(b_text)
    return set(_A_TAG.findall(a_text)) <= set(_A_TAG.findall(expected))

# Rejection is the gate working, not the document breaking — a rejected batch keeps its
# stage-3 rows. But a HIGH rate means the model or prompt has drifted and the run is
# buying less than it costs, so it is scored rather than ignored.
REJECTION_WARN = 0.10
REJECTION_PENALTY = 60.0     # x rejection_rate
# Reprojection is healthy by design (the model re-punctuates constantly and we overwrite
# it with the original), so it is reported but only lightly penalised.
REPROJECTION_PENALTY = 10.0  # x reprojection_rate


def _content_files(d: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.md"))
            if not p.name.isupper() and not p.name.startswith(("README", "STAGE", "CONVERSION"))}


def _tables(text: str) -> list[str]:
    return _TABLE.findall(text or "")


def _row_stream(text: str) -> str:
    """STRICT stream of every table ROW in a file, tables in document order.

    Prose is deliberately excluded: a label row promoted to a heading moves OUT of the
    tables and into prose, so comparing whole files would report that legitimate move as
    drift. Comparing rows isolates the question "did any table content change" — and
    table rows are the only thing the model is ever given to edit.

    Strict, not alphanumeric. The file-level accounting below has to tolerate markdown
    punctuation moving (sub-chunking really does add "###" characters), so it compares on
    alphanumerics. Rows have no such excuse, and the looser stream is blind to exactly
    the edits that matter most: normalising (“IFA”) to ("IFA") and inserting a bullet
    glyph both leave the alphanumerics untouched. Both were observed; both must fail.
    """
    return "".join(strict_stream(r) for t in _tables(text) for r in split_rows(t))


def _widths(text: str) -> tuple[int, int]:
    """(rows matching their table's declared width, rows total) across a file.

    Width is colspan-aware on both sides. A header row written as
    `<td colspan="2">Questions</td><td>Answers</td>` holds two cells but occupies three
    columns, so counting <td> tags would score the one correctly-formed row in the table
    as the broken one.
    """
    ok = total = 0
    for m in _TABLE.finditer(text):
        tbl = m.group(0)
        # The NEAREST preceding badge, not the file's first — a split table has several,
        # and after sub-chunking the later ones have none and inherit the table's own.
        badges = _BADGE_LINE.findall(text[:m.start()])
        w = table_width(tbl, badges[-1] if badges else "")
        for r in split_rows(tbl):
            total += 1
            ok += _row_width(r) == w
    return ok, total


def _row_removals(report: dict, fname: str) -> list[str]:
    """Labels that left the TABLES of this file, as heading strings.

    Every successful split promotes its label rows into headings, so every one of them
    leaves the tables — whether or not a stage-1 heading was reused for the first. Row
    accounting and file accounting are different questions and were briefly conflated
    here, which reported two clean documents as corrupted.
    """
    return [s for sp in report.get("splits", [])
            if sp.get("file") == fname and sp.get("ok")
            for s in sp.get("sections", [])]


def _file_removals(report: dict, fname: str) -> list[str]:
    """Text actually DELETED from this file — a strictly smaller set than the above.

    A promoted label normally just moves: out of a table row, into the heading written
    directly above it, so the file keeps every character. Text only truly leaves in two
    cases, and both are removals of a proven DUPLICATE:

      * `reused_existing_heading` — stage 1 had already written that heading above the
        table, so the split's own heading was dropped and the label row with it. Only
        the FIRST section of a split can be reused, which is why this takes sections[0]
        and not the whole list. Taking the whole list over-declared by ~160 characters
        on this document and would have licensed a real deletion of that size.
      * `shells_removed` — an empty stage-1 heading deleted once its section was
        re-emitted with content elsewhere in the file.
    """
    out: list[str] = []
    for sp in report.get("splits", []):
        if sp.get("file") == fname and sp.get("ok") and sp.get("reused_existing_heading"):
            secs = sp.get("sections") or []
            if secs:
                out.append(secs[0])
    for f in report.get("files", []):
        if f.get("file") == fname:
            out.extend(f.get("shells_removed") or [])
    return out


def compute_stage4(out_root: Path) -> dict:
    out_root = Path(out_root)
    s3 = next(iter(sorted(out_root.glob("03_*"))), None)
    s4 = next(iter(sorted(out_root.glob("04_*"))), None)
    if s4 is None or s3 is None:
        # Not a fault: most documents never go through stage 4. Absent, not zero — a
        # dimension scored 0 here would fail every document that was never eligible.
        return {"passed": True, "skipped": True, "score": None,
                "reason": "no 04_* stage directory (AI post-processing not run)",
                "flags": [], "files": []}

    report_path = s4 / "stage4_report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}

    before, after = _content_files(s3), _content_files(s4)
    # Files stage 4 actually processed. The quality metrics aggregate over THESE only:
    # a run scoped to one section leaves every other file byte-identical to stage 3, and
    # averaging those in dilutes the measurement until a real improvement is invisible
    # (chunk 07 went 39% -> 100% on column uniformity, which showed as 39% -> 45%
    # once six untouched files were averaged in).
    processed = {f.get("file") for f in report.get("files", [])} or set(before)
    flags: list[dict] = []
    per_file: list[dict] = []
    w_ok_b = w_tot_b = w_ok_a = w_tot_a = 0

    for name, b_text in before.items():
        a_text = after.get(name)
        if a_text is None:
            flags.append({"severity": "critical", "kind": "file_missing", "file": name,
                          "detail": "stage 3 file has no stage 4 counterpart"})
            continue

        sb, sa = content_stream(b_text), content_stream(a_text)
        cb, ca = collections.Counter(sb), collections.Counter(sa)
        added = ca - cb
        removed = cb - ca

        # ---- Tier 1: nothing may be ADDED, ever.
        if added:
            flags.append({"severity": "critical", "kind": "content_added", "file": name,
                          "detail": f"{sum(added.values())} characters not in stage 3: "
                                    f"{''.join(sorted(added.elements()))[:60]!r}"})

        # ---- Tier 1: anything REMOVED must be a declared, verifiable duplicate.
        declared = _file_removals(report, name)
        claimed = collections.Counter()
        for d in declared:
            claimed += collections.Counter(content_stream(d))
        residual = removed - claimed
        if residual:
            flags.append({"severity": "critical", "kind": "content_removed_unexplained",
                          "file": name,
                          "detail": f"{sum(residual.values())} characters removed with no "
                                    f"declared duplicate: "
                                    f"{''.join(sorted(residual.elements()))[:60]!r}"})
        # The mirror check, and not redundant: Counter subtraction clamps at zero, so a
        # report that over-declares would absorb an unrelated deletion of the same size
        # and pass. Declaring a removal that did not happen is itself a fault.
        overclaim = claimed - removed
        if overclaim:
            flags.append({"severity": "critical", "kind": "removal_overdeclared",
                          "file": name,
                          "detail": f"report claims {sum(overclaim.values())} more removed "
                                    f"characters than actually left the file"})
        # A declared removal must still be present ONCE in the file it was removed from,
        # or it was not a duplicate and the text is simply gone.
        for d in declared:
            if content_stream(d) not in content_stream(a_text):
                flags.append({"severity": "critical", "kind": "removal_not_duplicated",
                              "file": name,
                              "detail": f"removed {d!r} but it appears nowhere in stage 4"})

        # ---- Tier 1: table content must reconcile row-for-row.
        rb, ra = _row_stream(b_text), _row_stream(a_text)
        promoted = collections.Counter()
        for d in _row_removals(report, name):
            promoted += collections.Counter(strict_stream(d))
        # Sub-chunking moves label rows out of the tables; nothing else may differ.
        drift = (collections.Counter(rb) - collections.Counter(ra)) - promoted
        drift_back = collections.Counter(ra) - collections.Counter(rb)
        if drift or drift_back:
            flags.append({"severity": "critical", "kind": "row_content_drift", "file": name,
                          "detail": f"table rows differ from stage 3 by "
                                    f"-{sum(drift.values())}/+{sum(drift_back.values())} chars"})

        # ---- Tier 1: no invented markup.
        illegal = {t for t in markup_signature(a_text) - markup_signature(b_text)
                   if t[0] not in _STRUCTURAL_TAGS or t[1]}
        # Excuse ONLY the <a> entries, and only once verified — a model-invented <strong>
        # or colspan sitting alongside a legitimate linkified URL must still be caught.
        if any(t[0] == "a" for t in illegal) and _links_are_all_linkify_wraps(a_text, b_text):
            illegal = {t for t in illegal if t[0] != "a"}
        if illegal:
            flags.append({"severity": "critical", "kind": "markup_injected", "file": name,
                          "detail": f"tags/attributes not in stage 3: {sorted(illegal)}"})

        # ---- Tier 2: column uniformity, the ragged-row defect.
        ob, tb = _widths(b_text)
        oa, ta = _widths(a_text)
        if name in processed:
            w_ok_b += ob; w_tot_b += tb; w_ok_a += oa; w_tot_a += ta

        nrb = sum(len(split_rows(t)) for t in _tables(b_text))
        nra = sum(len(split_rows(t)) for t in _tables(a_text))
        per_file.append({
            "file": name, "chars_before": len(sb), "chars_after": len(sa),
            "chars_added": sum(added.values()), "chars_removed": sum(removed.values()),
            "declared_removals": declared,
            "row_removals": len(_row_removals(report, name)),
            "rows_before": nrb, "rows_after": nra,
            "width_ok_before": ob, "width_rows_before": tb,
            "width_ok_after": oa, "width_rows_after": ta,
        })

    usage = report.get("usage") or {}
    total = report.get("batches_total") or 0
    rej = report.get("batches_rejected") or 0
    rep_ = report.get("batches_reprojected") or 0
    esc = sum(1 for x in report.get("edits", []) if "sonnet" in (x.get("model") or ""))
    rej_rate = rej / total if total else 0.0
    rep_rate = rep_ / total if total else 0.0

    if rej_rate > REJECTION_WARN:
        flags.append({"severity": "warning", "kind": "high_rejection_rate", "file": "",
                      "detail": f"{rej}/{total} batches rejected by the content gate "
                                f"({rej_rate:.0%}) — model or prompt may have drifted"})

    uni_b = (w_ok_b / w_tot_b) if w_tot_b else None
    uni_a = (w_ok_a / w_tot_a) if w_tot_a else None
    if uni_b is not None and uni_a is not None and uni_a < uni_b:
        flags.append({"severity": "warning", "kind": "column_uniformity_regressed", "file": "",
                      "detail": f"rows matching their table width fell "
                                f"{uni_b:.0%} -> {uni_a:.0%}"})

    critical = [f for f in flags if f["severity"] == "critical"]
    score = 0.0 if critical else max(
        0.0, 100.0 - REJECTION_PENALTY * rej_rate - REPROJECTION_PENALTY * rep_rate)

    return {
        "passed": not critical,
        "skipped": False,
        "score": round(score, 1),
        "integrity_ok": not critical,
        "flags": flags,
        "files": per_file,
        # Tier 2 — repair quality and model health.
        "batches_total": total,
        "batches_changed": report.get("batches_changed") or 0,
        "batches_rejected": rej,
        "batches_reprojected": rep_,
        "rejection_rate": round(rej_rate, 4),
        "reprojection_rate": round(rep_rate, 4),
        "escalated_to_sonnet": esc,
        "spacing_fixes": report.get("spacing_fixes") or 0,
        "structural_fixes": report.get("structural_fixes") or 0,
        "column_uniformity_before": None if uni_b is None else round(uni_b, 4),
        "column_uniformity_after": None if uni_a is None else round(uni_a, 4),
        "files_processed": len(processed),
        "subsections_created": sum(len(s.get("sections", []))
                                   for s in report.get("splits", []) if s.get("ok")),
        "splits_refused": sum(1 for s in report.get("splits", []) if not s.get("ok")),
        "tokens": usage.get("total_tokens") or 0,
        "cost_usd": usage.get("cost_usd") or 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    args = ap.parse_args()
    r = compute_stage4(Path(args.out_root))

    banner("Stage 4 — AI post-processing review")
    if r.get("skipped"):
        print(DIM(r["reason"]))
        return
    colour = GREEN if r["passed"] and not r["flags"] else (RED if not r["passed"] else YELLOW)
    print(colour(BOLD(f"score {r['score']}  integrity "
                      f"{'OK' if r['integrity_ok'] else 'FAILED'}")))
    print(f"  batches      {r['batches_changed']}/{r['batches_total']} changed, "
          f"{r['batches_rejected']} rejected ({r['rejection_rate']:.0%}), "
          f"{r['batches_reprojected']} reprojected")
    print(f"  fixes        {r['spacing_fixes']} spacing, {r['structural_fixes']} structural")
    if r["column_uniformity_before"] is not None:
        print(f"  columns      {r['column_uniformity_before']:.0%} -> "
              f"{r['column_uniformity_after']:.0%} of rows match their table width")
    print(f"  sub-sections {r['subsections_created']} created, {r['splits_refused']} refused")
    print(f"  files        {r['files_processed']} processed")
    print(f"  cost         {r['tokens']:,} tokens, ${r['cost_usd']:.4f}")
    if r["flags"]:
        print()
        for f in r["flags"]:
            c = RED if f["severity"] == "critical" else YELLOW
            print(f"  {c('[' + f['severity'] + '] ' + f['kind'])} "
                  f"{DIM(f['file'])} — {f['detail']}")
    else:
        print("\n  " + GREEN("no integrity flags — stage 4 changed only spacing, "
                             "cell boundaries and duplicate labels"))


if __name__ == "__main__":
    run_cli(main)


# ---------------------------------------------------------------- conservation checks
# Two deterministic checks that together answer "did stage 4 damage the document", where
# neither can answer it alone. The first asks whether anything actually vanished; the
# second whether anything survived but landed in the wrong column, which the first cannot
# see because every character is still present.

MIN_TRACK_CHARS = 25          # below this a cell matches by coincidence, not by identity


def _cells_with_rowshape(text: str) -> list[tuple[str, int, int, str]]:
    """(role, index in row, cells in row, content stream) for every cell."""
    out = []
    for tbl in _TABLE.findall(text or ""):
        for row in split_rows(tbl):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            for i, cell in enumerate(cells):
                role = "answer" if i == len(cells) - 1 and len(cells) > 1 else "question"
                out.append((role, i, len(cells), content_stream(cell)))
    return out


def _cells_with_columns(text: str) -> list[tuple[str, str]]:
    """(role, content stream) for every cell in a file, in document order.

    Role, not column index. An index is not a stable identity: repairing a row from three
    cells to two — the label merged back into the question — moves "Please provide the
    name of the competent regulator" from column 1 to column 0 without changing what it
    is, and an index comparison reports that correct repair as damage. What matters is
    which SIDE of the table a piece of text sits on, and the answer is always the last
    cell of its row.
    """
    out: list[tuple[str, str]] = []
    for tbl in _TABLE.findall(text or ""):
        for row in split_rows(tbl):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            for i, cell in enumerate(cells):
                role = "answer" if i == len(cells) - 1 and len(cells) > 1 else "question"
                out.append((role, content_stream(cell)))
    return out


# A numbered sub-section label, as it sits in a stage-3 table: its own cell holding just
# "8.5", with the title in the NEXT cell. Stage 4 later promotes the pair into a
# "### 8.5 Internet/Social Media" heading, which is why the label only looks like this
# before the AI pass.
_SUBSEC_LABEL = re.compile(r"^\s*(\d+\.\d+)\s*$")
_SUBSEC_INLINE = re.compile(r"^\s*(\d+\.\d+)\s+(\S.{0,70})")


def _cell_subsections(text: str) -> list[str | None]:
    """For every cell of a file, in document order, the sub-section it sits under.

    Iterated EXACTLY as _cells_with_columns does, one entry per cell, so the two lists
    index together. Anything else and a finding gets attributed to the wrong sub-section,
    which is worse than not attributing it at all.

    "09-marketing-activities.md, 5 cells missing" sends a reviewer through 227 cells and
    twenty pages. "8.5 Internet/Social Media" sends them to one row.
    """
    out: list[str | None] = []
    cur: str | None = None
    awaiting_title: str | None = None
    for tbl in _TABLE.findall(text or ""):
        for row in split_rows(tbl):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            for cell in cells:
                # unescaped, or a title reads "Investment Management &amp; Advisory"
                raw = html.unescape(re.sub(r"<[^>]+>", " ", cell))
                raw = " ".join(raw.split())
                m_inline = _SUBSEC_INLINE.match(raw)
                if _SUBSEC_LABEL.match(raw):
                    # A bare label. Its title is the next non-empty cell.
                    awaiting_title = _SUBSEC_LABEL.match(raw).group(1)
                    cur = awaiting_title
                elif awaiting_title and raw:
                    cur = f"{awaiting_title} {raw[:60]}".strip()
                    awaiting_title = None
                elif m_inline:
                    # label and title in one cell, which is how a repaired row reads
                    cur = f"{m_inline.group(1)} {m_inline.group(2)}".strip()
                out.append(cur)
    return out


def _cell_corpus(files: dict[str, str]) -> str:
    """Every file's whole content, concatenated with NO separator.

    The missing separator is the point. A cell split in two ("(g) Do the…" becoming "(g)"
    + "Do the…") still reads as one contiguous run here, and two cells merged into one
    likewise — so legitimate restructuring does not register as absence.

    Whole content rather than cells only: a sub-section label row is deliberately promoted
    OUT of the table into a "### 6.1 Mutual Recognition" heading, and a cell-only corpus
    reported every one of those as lost text when it had simply changed form.
    """
    return "".join(content_stream(text) for text in files.values())


def check_cell_presence(before: dict[str, str], after: dict[str, str],
                        min_chars: int = MIN_TRACK_CHARS) -> list[dict]:
    """Cells present in stage 3 that are nowhere in stage 4 — across the WHOLE tree.

    Document-level, not per file, and that is the whole design. Stage 4 is asked to strip
    a foreign section out of a chunk that should never have held it; those rows then live
    only in their own chunk. Comparing chunk against chunk calls that a loss. Comparing
    the tree against the tree calls it what it is — a move — and still catches a row that
    left every chunk.

    Presence, not count, for the same reason: before the cleanup a row exists in two
    chunks and after it exists in one, and a count would flag that correct de-duplication.
    """
    corpus = _cell_corpus(after)
    missing = []
    for fname, text in before.items():
        subs = _cell_subsections(text)
        for idx, (role, s) in enumerate(_cells_with_columns(text)):
            if len(s) < min_chars:
                continue
            # ABSENCE of the whole cell is the test. Not "fewer occurrences than before"
            # (stage 4 legitimately de-duplicates, so a dropped duplicate flags while its
            # surviving copy sits right there — 57 findings on a document with one real
            # loss), and not "most 40-char windows survive" either, which is what let the
            # real one through: "If so, please provide recommended wording in Section C of
            # Appendix 3" was deleted from the document and never re-homed, and its single
            # window matched a near-identical sentence elsewhere.
            #
            # This direction errs toward FALSE POSITIVES: a cell split across rows loses
            # contiguity and shows up here even though every word survives. That is the
            # error to prefer — a false positive costs a reviewer one look, a false
            # negative costs the document a paragraph. `windows_found` tells the two apart
            # at a glance: a split keeps nearly all its windows, a deletion keeps none.
            if s in corpus:
                continue
            wins = [s[i:i + 40] for i in range(0, max(1, len(s) - 40), 40)]
            found = sum(1 for w in wins if w in corpus)
            missing.append({
                "file": fname, "role": role, "chars": len(s),
                "windows_found": found, "windows": len(wins),
                # Which of the two it is, read off the window pattern:
                #
                #   a SPLIT breaks the seam it was cut at, so it loses a window or three
                #   out of many and keeps the rest — 322/323, 12/13, 16/17.
                #
                #   a DELETION of a short cell keeps every window, because 40 characters
                #   of legal boilerplate match a near-identical sentence somewhere else
                #   in the document while the whole cell exists nowhere. That is exactly
                #   how "If so, please provide recommended wording in Section C of
                #   Appendix 3" hid: 1 window, found, cell gone.
                #
                # A long cell that keeps every window yet is absent as a whole is neither
                # pattern and says so rather than guessing.
                "likely": ("split" if found < len(wins)
                           else "deleted" if len(wins) <= 2 else "inconclusive"),
                # WHICH sub-section, so a finding points at a row rather than at a file.
                # "09-marketing-activities.md, 5 missing" is twenty pages to search.
                "subsection": subs[idx] if idx < len(subs) else None,
                "text": s[:80],
            })
    return missing


def check_column_roles(before: dict[str, str], after: dict[str, str],
                       min_chars: int = MIN_TRACK_CHARS) -> list[dict]:
    """Cell text that survived but changed column — question text in an answer cell.

    This is the damage no content check can see: nothing added, nothing removed, every
    cell accounted for, and the table meaningless. Observed on section 3, where question
    (d)'s continuation — "If so, please: (i) contact us…" — was moved out of the question
    column and appended to (d)'s ANSWER, leaving the row below with an empty question.

    Tracked by containment rather than by pairing cells up: a stage-3 cell that was split
    or merged still sits inside exactly one stage-4 cell, and that cell's column index is
    the one that has to match.
    """
    moved, reshaped = [], []
    after_cells = [(f, role, i, n, s) for f, text in after.items()
                   for role, i, n, s in _cells_with_rowshape(text)]
    for fname, text in before.items():
        for role, _i, n, s in _cells_with_rowshape(text):
            if len(s) < min_chars:
                continue
            hit = next(((f2, r2, i2, n2) for f2, r2, i2, n2, s2 in after_cells if s in s2), None)
            if not hit:
                continue
            f2, r2, i2, n2 = hit
            if n2 != n:
                # The row was RESHAPED, so index-based roles are not comparable and the
                # comparison must not pretend otherwise. Chunk 08's table is the 7-column
                # OPEN-ENDED/CLOSED-ENDED layout with TWO answer columns per row — an
                # answer legitimately sitting at index 2 of 6 was read as a question by
                # the last-cell-only rule and reported six times as damage that was not
                # there. A shape change is worth reporting, but it is not a column swap.
                reshaped.append({"file": fname, "cells_before": n, "cells_after": n2,
                                 "to_file": f2, "chars": len(s), "text": s[:80]})
                continue
            if r2 != role:
                moved.append({"file": fname, "from_role": role, "to_role": r2,
                              "to_file": f2, "chars": len(s), "text": s[:80]})
    return moved, reshaped


def conservation_report(out_root: Path) -> dict:
    """Both checks over a job's stage 3 and stage 4 trees."""
    out_root = Path(out_root)
    s3 = next(iter(sorted(out_root.glob("03_*"))), None)
    s4 = next(iter(sorted(out_root.glob("04_*"))), None)
    if not s3 or not s4:
        return {"skipped": True, "missing_cells": [], "moved_cells": []}
    before, after = _content_files(s3), _content_files(s4)
    missing = check_cell_presence(before, after)
    moved, reshaped = check_column_roles(before, after)
    return {"skipped": False, "missing_cells": missing, "moved_cells": moved,
            "reshaped_cells": reshaped,
            "missing_count": len(missing), "moved_count": len(moved),
            "reshaped_count": len(reshaped)}


# ---------------------------------------------------------------- integrity score

# A cell that left the document entirely is the unrecoverable failure — the text is gone
# and no other chunk holds it. A cell that changed side of the table still has all its
# text, but the table now says something it does not mean: question (d)'s continuation
# appended to (d)'s ANSWER reads as an answer the jurisdiction never gave. Serious, and
# repairable by hand, so weighted lower.
MISSING_PENALTY = 12.0
MOVED_PENALTY = 5.0


def integrity_score(missing: list, moved: list) -> float:
    """100 for a clean run, falling with what the checkers found.

    Deliberately measures ONE thing: is this output safe to keep. It does not credit
    sub-headings created or tables merged, because a score that mixed the two would let
    good structural work mask a lost cell — and structural gain is the part you can see
    by eye, while a missing cell is the part you cannot.
    """
    # Only the cells that look genuinely deleted carry the penalty. A `split` is
    # contiguity lost, not content — six of them on this document, every word present —
    # and scoring those would bury the one real deletion under noise from correct work.
    # They stay in the findings list to be looked at, just not in the arithmetic.
    lost = [m for m in missing if m.get("likely") != "split"]
    return max(0.0, round(100.0 - MISSING_PENALTY * len(lost) - MOVED_PENALTY * len(moved), 1))


def stage4_dashboard(out_root: Path) -> dict:
    """Everything the Stage 4 tab shows: what the run did, and what the checkers make of it."""
    out_root = Path(out_root)
    s4 = next(iter(sorted(out_root.glob("04_*"))), None)
    if s4 is None:
        # Distinguish "never asked for" from "asked for and refused": the flag being off
        # is a deployment decision worth seeing, not an absence of information.
        try:
            from aosphere_core_index.config import settings
            enabled = settings.stage4_ai_enabled
        except Exception:  # noqa: BLE001
            enabled = None
        return {"ran": False, "enabled": enabled,
                "reason": ("AI post-processing is disabled on this host "
                           "(ACI_STAGE4_AI_ENABLED is not set)" if enabled is False else
                           "AI post-processing has not been run for this document")}
    rpt_path = s4 / "stage4_report.json"
    if not rpt_path.exists():
        # A 04_* DIRECTORY IS NOT EVIDENCE STAGE 4 RAN. The summary-AI route
        # (scripts/summary_ai_extract.py) writes its chunk tree into 04_stage4_ai/ in the
        # same stage-3 shape, and never writes a stage4_report.json: it REPLACES stages
        # 1-3 rather than post-processing them, and prices its own model call into
        # report.json instead (pipeline_monitor._ai_block already reasons this way).
        # Without this, every summary document rendered a Stage 4 tab full of zeroes --
        # no model, no tokens, $0.0000 -- under an integrity score of 100 computed by
        # diffing a stage-3 tree that does not exist. Those zeroes read as a real, free
        # run, which is the same class of fault as the "Stage 4 not run" this dashboard
        # was published to stop showing. Stages 4 and 5 write this report only when they
        # finish, so its absence next to a finished job means they did not run.
        summary = (out_root / "summary_ai_rule.json").exists()
        return {"ran": False, "enabled": None,
                "reason": ("this document came from the summary-AI route, which replaces "
                           "stages 1-3 outright — its model, time and cost are in the "
                           "Scorecard's own timing block, not here" if summary else
                           "AI post-processing has not been run for this document "
                           "(no 04_*/stage4_report.json)")}
    report = json.loads(rpt_path.read_text())
    cons = conservation_report(out_root)
    missing, moved = cons.get("missing_cells", []), cons.get("moved_cells", [])
    rp = s4 / "stage4_repairs.json"
    repairs = json.loads(rp.read_text()) if rp.exists() else {"ran": False}

    # Findings are grouped by file so the tab can sit them next to that section's row.
    by_file: dict[str, dict] = {}
    for m in missing:
        by_file.setdefault(m["file"], {"missing": [], "moved": []})["missing"].append(m)
    for m in moved:
        by_file.setdefault(m["file"], {"missing": [], "moved": []})["moved"].append(m)

    sections = []
    for s in report.get("sections", []):
        f = by_file.get(s["file"], {})
        sections.append({**s,
                         "missing": f.get("missing", []),
                         "moved": f.get("moved", [])})
    # Wall clock comes from timings.json, the same file stages 1-3 write to, so the AI
    # stages are read the same way as everything else rather than from a private field.
    tp = out_root / "timings.json"
    steps = {}
    if tp.exists():
        try:
            steps = (json.loads(tp.read_text()).get("steps") or {})
        except (OSError, json.JSONDecodeError):
            steps = {}
    return {
        "ran": True,
        "mode": report.get("mode"),
        "model": report.get("model"),
        "disabled": bool(report.get("disabled")),
        "seconds": report.get("seconds") or steps.get("stage4_ai") or 0.0,
        "stage5_seconds": (report.get("subchunk") or {}).get("seconds")
                          or steps.get("stage5_subchunk") or 0.0,
        "ai_total_seconds": steps.get("ai_total") or 0.0,
        "extraction_seconds": steps.get("total") or 0.0,
        "subchunks": (report.get("subchunk") or {}).get("subchunks_total") or 0,
        "sections_split": (report.get("subchunk") or {}).get("sections_split") or 0,
        "score": integrity_score(missing, moved),
        "missing_total": len(missing),
        "moved_total": len(moved),
        "headings_added": report.get("headings_added", 0),
        "usage": report.get("usage", {}),
        "sections": sections,
        "pipeline": {
            "ai": {"sections": report.get("sections_total", 0),
                   "accepted": report.get("sections_accepted", 0),
                   "headings": report.get("headings_added", 0),
                   "tables_before": sum(s.get("tables_before", 0) for s in report.get("sections", [])),
                   "tables_after": sum(s.get("tables_after", 0) for s in report.get("sections", [])),
                   "rows_before": sum(s.get("rows_before", 0) for s in report.get("sections", [])),
                   "rows_after": sum(s.get("rows_after", 0) for s in report.get("sections", []))},
            "detect": {"moved": repairs.get("detected", len(moved)),
                       "missing": len(missing)},
            "repair": {"ran": repairs.get("ran", False),
                       "repaired": repairs.get("repaired", 0),
                       "declined": len(repairs.get("declined", [])),
                       "applied": repairs.get("applied", []),
                       "declined_detail": repairs.get("declined", [])},
            "recheck": {"remaining_moved": repairs.get("remaining", len(moved)),
                        "remaining_missing": len(missing)},
        },
    }


# ---------------------------------------------------------------- column repair

_CELL_RE = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.S | re.I)


def _split_at_stream_suffix(inner: str, suffix: str) -> tuple[str, str] | None:
    """Cut a cell's inner HTML where its content stream's tail equals `suffix`.

    The stream is normalised (tags and whitespace gone, entities resolved) and the HTML
    is not, so the cut point cannot be found by string search — it is located by walking
    the raw HTML and tracking how much stream each character has produced. Returns None
    when no cut reproduces the suffix exactly, which is the signal to decline the repair
    rather than guess at one.
    """
    for i in range(len(inner)):
        if content_stream(inner[i:]) == suffix:
            return inner[:i], inner[i:]
    return None


def plan_column_repairs(before: dict[str, str], after: dict[str, str]) -> tuple[list, list]:
    """(repairs we can prove, moves we decline) for text that changed column role.

    One shape is repaired and one only: text that was its own QUESTION cell in stage 3,
    appended onto the end of an ANSWER cell in stage 4, where the next row's first cell is
    EMPTY and can receive it. That is the (d) defect exactly — "If so, please: (i) contact
    us…" folded into (d)'s answer, leaving the row beneath it with no question.

    Everything else is declined and reported. A move whose target cell already holds text
    has no unambiguous destination, and guessing would turn a visible defect into a silent
    one — the failure this whole layer exists to prevent.
    """
    repairs, declined = [], []
    for m in check_column_roles(before, after)[0]:
        if m["from_role"] != "question" or m["to_role"] != "answer":
            declined.append({**m, "why": "not a question-into-answer move"})
            continue
        fname = m.get("to_file") or m["file"]
        text = after.get(fname, "")
        rows = [(mm.start(), mm.end(), mm.group(0))
                for mm in re.finditer(r"<tr\b.*?</tr>", text, re.S | re.I)]
        hit = next((i for i, (_, _, r) in enumerate(rows)
                    if content_stream(r).endswith(m["text"][:60])), None)
        if hit is None or hit + 1 >= len(rows):
            declined.append({**m, "why": "no following row to move the text into"})
            continue
        nxt = rows[hit + 1][2]
        cells = list(_CELL_RE.finditer(nxt))
        if not cells or content_stream(cells[0].group(2)):
            declined.append({**m, "why": "the next row's first cell is not empty"})
            continue
        repairs.append({**m, "row_index": hit, "target_row": hit + 1})
    return repairs, declined


def apply_column_repairs(out_root: Path, dry_run: bool = False) -> dict:
    """Move mis-columned text back, verify it was a pure move, and record what happened."""
    out_root = Path(out_root)
    s3 = next(iter(sorted(out_root.glob("03_*"))), None)
    s4 = next(iter(sorted(out_root.glob("04_*"))), None)
    if not s3 or not s4:
        return {"ran": False, "repaired": [], "declined": []}
    before, after = _content_files(s3), _content_files(s4)
    detected = check_column_roles(before, after)
    repairs, declined = plan_column_repairs(before, after)

    applied, failed = [], []
    for r in repairs:
        fname = r.get("to_file") or r["file"]
        text = after[fname]
        rows = list(re.finditer(r"<tr\b.*?</tr>", text, re.S | re.I))
        src, dst = rows[r["row_index"]], rows[r["target_row"]]
        src_cells = list(_CELL_RE.finditer(src.group(0)))
        if not src_cells:
            failed.append({**r, "why": "source row has no cells"})
            continue
        last = src_cells[-1]
        cut = _split_at_stream_suffix(last.group(2), r["text"] if len(r["text"]) < 80 else
                                      content_stream(last.group(2))[-len(r["text"]):])
        if cut is None:
            failed.append({**r, "why": "could not locate the moved text inside the cell"})
            continue
        head, tail = cut
        new_src = (src.group(0)[:last.start(2)] + head.rstrip()
                   + src.group(0)[last.end(2):])
        dst_cells = list(_CELL_RE.finditer(dst.group(0)))
        first = dst_cells[0]
        new_dst = (dst.group(0)[:first.start(2)] + tail.strip()
                   + dst.group(0)[first.end(2):])
        candidate = text[:src.start()] + new_src + text[src.end():dst.start()] + new_dst + text[dst.end():]
        # A repair is a MOVE. If a character appeared or vanished, it was something else
        # and the file is left exactly as the AI produced it.
        if content_stream(candidate) != content_stream(text):
            failed.append({**r, "why": "repair would have changed content — rolled back"})
            continue
        after[fname] = candidate
        applied.append(r)

    if not dry_run:
        for fname, text in after.items():
            (s4 / fname).write_text(text)
    remaining, _ = check_column_roles(before, after)
    record = {"ran": True,
              "detected": len(detected),
              "repaired": len(applied),
              "declined": [*declined, *failed],
              "remaining": len(remaining),
              "applied": applied,
              "remaining_moves": remaining}
    if not dry_run:
        (s4 / "stage4_repairs.json").write_text(json.dumps(record, indent=2, default=str))
    return record
