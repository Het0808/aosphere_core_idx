#!/usr/bin/env python3
"""subchunk.py — split AI-post-processed sections into a nested per-sub-section tree.

Stage 4 lifts numbered sub-section labels out of the tables into `### 6.2 Funds` headings,
which makes each sub-section separable for the first time: in stage 3 those labels are
rows INSIDE one 33-row table and there is nothing to split on. This turns that into a
directory per section holding a file per sub-section:

    05_subchunks/
      07-marketing-selling-to-the-public.md          <- the section heading and its intro
      07-marketing-selling-to-the-public/
        01-6.1-mutual-recognition-of-funds.md
        02-6.2-funds.md
        03-6.3-investment-management-advisory-services.md

Written to its own stage directory rather than restructuring 04_stage4_ai, because both
conservation checks glob `*.md` at the top of a stage dir — moving files into folders
would drop every sub-chunked section out of the checks and read as total content loss.

Two gates, both required. The product must be listed in product_rules.SUBCHUNK_PRODUCTS,
and the document must actually have been through stage 4. Neither is a default: a product
whose sections are ordinary prose has no numbered split points, and stage 3 alone has none
either.

Usage:
    python scripts/subchunk.py out/corpus/<product>/<job>
    python scripts/subchunk.py out/corpus/<product>/<job> --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from product_rules import SUBCHUNK_PRODUCTS  # noqa: E402

# `### 6.2 Funds` — a numbered sub-section heading, which is what stage 4 emits. Level is
# not fixed at three: stage 1 writes `##` for a sub-section it recognised itself, and a
# section may hold both.
_SUB_HEADING = re.compile(r"(?m)^(#{2,4})\s+(\d+(?:\.\d+)+)\s*(.*)$")
_SKIP = {"README.md", "CONVERSION_REPORT.md", "STAGE3_REPORT.md", "STAGE4_REPORT.md"}


def slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", text.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:limit].rstrip("-") or "section"



# Stage 4's text pass writes an appendix heading as "# Appendix 2 Disclaimers: Closed-
# Ended Fund" -- the page's own label leading the title, no bare digit first -- so the
# number has to be read out of "Appendix N" too, not only a leading "N ". Both shapes are
# optional here on purpose: an ordinary section heading ("# 6 Marketing/Selling...") has
# no "Appendix" to match and falls through to the plain digit group unchanged.
_H1 = re.compile(r"(?m)^#\s+(?:Appendix\s+)?(\d+)\.?\s", re.I)
# The appendices restart their numbering at 1, so a plain section number is not unique
# across the document — "1" is both BACKGROUND and Disclaimers: Open-Ended Fund, and
# separately "5"/"6" are both Passive Marketing/Marketing-Selling AND the FinSA appendices
# (FinSA Client Segmentation, FinSA requirements and CISA restrictions). All of them take
# an A prefix instead, which also keeps them together at the end: letters sort after
# digits, so 00, 01..11, A1..A6 is document order.
#
# Kept only as the LAST of the three tests below. On its own it was the whole rule, keyed
# off the FILENAME, and it is wrong whenever an appendix is not a disclaimer: measured on
# the 16-09-26 corpus, 12 of 54 documents filed one under a body-section number, where it
# sorts into the middle of the tree and reads as missing — United Kingdom's "Appendix 5
# Exempt Person under the Exemption Order" landed as 05-, immediately before 05-passive-
# marketing, so the tail of the tree ended at A4 and appendix 5 was nowhere a reader would
# look. Australia, DIFC, Kuwait, Malaysia and Russia the same.
_APPENDIX_MARKS = ("disclaimer", "finsa")

# The label as the PAGE prints it, which is the signal that does not depend on the title:
# "# Appendix 2 Disclaimers: Closed-Ended Fund", or the bare "APPENDIX 2" / "SCHEDULE 1"
# line the memo sets above the title and stage 4 folds into the H1. Families are kept
# apart — Guernsey 174507 prints both a SCHEDULE 1 and an APPENDIX 1, and they are
# different divisions — and each gets its own letter, all of which sort after digits so
# back matter stays at the end: 00, 01..11, A1..A5, S1.
_FAMILY = {"appendix": "A", "appendices": "A", "annex": "N", "annexe": "N",
           "annexes": "N", "annexure": "N", "annexures": "N",
           "schedule": "S", "schedules": "S", "exhibit": "E", "exhibits": "E"}
_W = (r"appendices|appendix|annexures?|annexes?|annexe|annex"
      r"|schedules?|exhibits?")
_H1_LABEL = re.compile(rf"^#\s+(?:\*\*)?\s*({_W})\s+(\d{{1,3}})\b", re.I)
_BODY_LABEL = re.compile(rf"(?mi)^\s*(?:\*\*|__)?\s*({_W})\s+(\d{{1,3}})"
                         r"\s*[:.\-–—]?\s*(?:\*\*|__)?\s*$")


def _printed_label(text: str) -> str | None:
    """The division label this section opens with — "A5", "S1" — or None.

    Looked for in the H1 first, then in the section's opening lines, because the two
    stages spell it differently: stage 3 carries "# 1 Disclaimers: Open-Ended Fund" with a
    bare "APPENDIX 1" line under it, and stage 4 folds that line INTO the heading.
    """
    m = _H1_LABEL.match(text.split("\n", 1)[0])
    if m:
        return f"{_FAMILY[m.group(1).lower()]}{int(m.group(2))}"
    m = _BODY_LABEL.search("\n".join(text.split("\n")[:12]))
    return f"{_FAMILY[m.group(1).lower()]}{int(m.group(2))}" if m else None


def section_prefix(text: str, fallback_index: int, name: str,
                   stage3_text: str | None = None, body_seen: set | None = None) -> str:
    """The prefix a section's stage-5 file or folder takes.

    Stage 1 numbers files by their position in the tree, which runs one ahead of the
    document's own numbering — chunk 09 is section 8 — so a folder's prefix contradicted
    the 8.1/8.2 files inside it. Here the prefix IS the section number, read from the H1,
    so the two always agree.

    Back matter is decided by the label the page PRINTS, in three descending tests. Each
    exists because a real document in this corpus needs it and the one above it cannot
    reach:

      1. the printed label, from the H1 or the section's opening ("Appendix 5 Exempt
         Person…", "SCHEDULE 2"). `stage3_text` is consulted when stage 4 has removed it:
         its prompt folds an orphaned "APPENDIX N" into the heading and deletes the line,
         and on Russian Federation 89570 it deleted without folding, leaving the evidence
         only in the deterministic tree.
      2. the disclaimer/finsa filename marker, for the appendices whose H1 carries no
         number at all to fold — Belgium 163341 heads them "# DISCLAIMERS: OPEN-ENDED
         FUND".
      3. a section number already used by an earlier body section. The appendices restart
         at 1, so a repeat is the numbering resetting: Dubai 181097's "# 5 Recognised
         Jurisdictions" says appendix nowhere, and section 5 is already Passive Marketing.
    """
    lab = _printed_label(text) or (_printed_label(stage3_text) if stage3_text else None)
    if lab:
        return lab
    m = _H1.search(text)
    if any(mark in name.lower() for mark in _APPENDIX_MARKS):
        return f"A{m.group(1)}" if m else f"A{fallback_index}"
    if m:
        n = int(m.group(1))
        if body_seen is not None:
            if n in body_seen:
                return f"A{n}"           # the numbering restarted — this is back matter
            body_seen.add(n)
        return f"{n:02d}"
    return "00"          # front matter and anything else with no numbered heading


def ai_processed(job_dir: Path) -> dict | None:
    """The stage-4 report if the document went through the AI pass, else None.

    The report is the flag: it is written only on a completed run and it carries the
    model and cost, so anything downstream can say WHICH pass produced a tree rather
    than just that one did.
    """
    for d in sorted(Path(job_dir).glob("04_*")):
        rp = d / "stage4_report.json"
        if rp.exists():
            try:
                return json.loads(rp.read_text())
            except (OSError, json.JSONDecodeError):
                return None
    return None


def product_of(job_dir: Path) -> str:
    meta = Path(job_dir) / "corpus_meta.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text()).get("product") or job_dir.parent.name
        except (OSError, json.JSONDecodeError):
            pass
    return job_dir.parent.name


def subchunk_eligible(job_dir: Path) -> tuple[bool, str]:
    """(eligible, why). Both gates, so the caller can report which one refused."""
    job_dir = Path(job_dir)
    product = product_of(job_dir)
    if product not in SUBCHUNK_PRODUCTS:
        return False, f"product {product!r} is not in SUBCHUNK_PRODUCTS"
    if ai_processed(job_dir) is None:
        return False, "no completed stage 4 (04_*/stage4_report.json) for this document"
    return True, f"{product}, AI-processed"


def split_file(text: str) -> tuple[str, list[dict]]:
    """(what stays in the parent file, [{id, title, body}, ...]).

    The parent keeps everything above the first numbered sub-heading — the `# 6` title,
    the source breadcrumb, any intro prose. Each sub-section carries its own heading so a
    chunk read on its own still says what it is.

    Only sub-headings that EXTEND this section's own number are split points: `### 8.12`
    under `# 10 Licence` is a label belonging to section 8, not a sub-section of 10. The
    template's section 8 questionnaire ends on an "8.12 Relocation of Investor" row whose
    table runs over the page break into the NEXT section's table, so MinerU hands stage 3
    a table_0NN that opens with that label row and stage 4 — correctly, by its own rule —
    lifts it into a heading at the top of a section it does not belong to. Splitting on it
    put the whole of section 10 inside `01-8.12-relocation-of-investor.md` and left
    `00-section.md` holding four lines of breadcrumb. 9 documents on the 22-09-26 corpus:
    Bermuda, Chile, Hong Kong SAR and Malaysia under Licence, Finland, Norway, Poland and
    Sweden under Prospectus Regulation, and a stray 7.5 leading Bahamas' section 8.

    A rejected heading is not dropped — nothing is written that could lose it. It falls
    into whichever kept chunk encloses it (the parent, when it leads the section, as all
    nine of these do), so the bytes are conserved either way.

    A section whose H1 carries no number at all (front matter) keeps the old behaviour and
    splits on every numbered heading: there is no own-number to check against.
    """
    own = _H1.search(text)
    own = own.group(1) if own else None
    heads = [m for m in _SUB_HEADING.finditer(text)
             if own is None or m.group(2).split(".")[0] == own]
    if not heads:
        return text, []
    parent = text[:heads[0].start()].rstrip() + "\n"
    subs = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        subs.append({"id": m.group(2), "title": m.group(3).strip(),
                     "body": text[m.start():end].rstrip() + "\n"})
    return parent, subs


# The breadcrumb the review tool navigates by. hybrid_extract_ui reads it with
# /^\*Source:.*?page\s+(\d+)(?:\s*[\u2013-]\s*(\d+))?\*/m — line-anchored, en dash or
# hyphen — so anything written here has to match that shape exactly.
_SOURCE = re.compile(r"^\*Source:\s*`([^`]+)`,\s*page\s+(\d+)(?:\s*[\u2013-]\s*(\d+))?\*",
                     re.M)


def _parent_source(text: str) -> tuple[str, int, int] | None:
    """(pdf name, first page, last page) from a section's own breadcrumb, or None."""
    m = _SOURCE.search(text)
    if not m:
        return None
    lo = int(m.group(2))
    return m.group(1), lo, int(m.group(3) or lo)


def _locate_subsections(pdf: Path, labels: list[str], lo: int, hi: int) -> dict[str, int]:
    """{label: 1-based page where that numbered sub-section starts}.

    Read off the PDF's own text layer, so this is deterministic, free, and calls no model.
    Two things keep it honest rather than clever:

      * only pages lo..hi are searched -- the parent's own range -- so a "6.2" printed in
        a cross-reference in some other section cannot claim the heading; and
      * labels are matched IN ORDER, so 6.2 can never be located before 6.1. A stray
        match earlier in the range is skipped rather than accepted.

    A label that cannot be found is simply absent from the result, and the caller falls
    back to the parent's range: landing on the right section beats landing nowhere.
    """
    try:
        import fitz
    except ImportError:
        return {}
    found: dict[str, int] = {}
    try:
        with fitz.open(str(pdf)) as doc:
            pending = list(labels)
            for page_no in range(max(lo, 1), min(hi, doc.page_count) + 1):
                if not pending:
                    break
                text = doc[page_no - 1].get_text("text")
                # As many as start on this page, still in order.
                while pending:
                    label = pending[0]
                    # The label at the start of a line, followed by punctuation, space or
                    # end -- not "6.2" inside "16.25" and not a bare mention mid-sentence.
                    if re.search(rf"^\s*{re.escape(label)}(?![\d.])", text, re.M):
                        found[label] = page_no
                        pending.pop(0)
                    else:
                        break
    except Exception:                                    # noqa: BLE001
        return found          # a damaged PDF must not sink the split
    return found


def _with_source(body: str, pdf_name: str, start: int, end: int) -> str:
    """Put a breadcrumb under the sub-section's own heading.

    Sub-chunks were written WITHOUT one: split_file leaves the parent's breadcrumb in
    00-section.md, so 25 of the 41 files in a split had no page reference at all and the
    viewer's PDF pane had nothing to jump to -- clicking 6.2 left it wherever it was.
    """
    if _SOURCE.search(body):
        return body
    lines = body.split("\n")
    page = f"page {start}" if start == end else f"page {start}\u2013{end}"
    crumb = f"*Source: `{pdf_name}`, {page}*"
    # after the heading line, which split_file guarantees is the first line
    return "\n".join([lines[0], "", crumb, *lines[1:]]) if lines else body


def run(job_dir: Path, dry_run: bool = False) -> dict:
    job_dir = Path(job_dir)
    ok, why = subchunk_eligible(job_dir)
    if not ok:
        return {"ran": False, "reason": why, "sections": []}

    src = next(iter(sorted(job_dir.glob("04_*"))))
    dest = job_dir / "05_subchunks"
    if not dry_run:
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        if (src / "_assets").exists():
            # Sub-chunk bodies still carry ../_assets image links from stage 1.
            shutil.copytree(src / "_assets", dest / "_assets")

    out = []
    seen: dict[str, str] = {}          # base name -> the stage-4 file that claimed it
    # Body section numbers already used, in file order. A repeat means the numbering has
    # restarted, which is what the appendices do — see section_prefix's third test.
    body_seen: set[int] = set()
    for idx, md in enumerate(sorted(src.glob("*.md")), 1):
        if md.name in _SKIP:
            continue
        body = md.read_text()
        parent, subs = split_file(body)
        # Rename off stage 1's running ordinal onto the document's own section number.
        stem = re.sub(r"^\d+-", "", md.stem)
        # Stage 3 is consulted for the printed label because stage 4 deletes it once it
        # has folded it into the heading — and sometimes without folding it.
        s3 = job_dir / "03_stage3_final" / md.name
        s3_text = s3.read_text(encoding="utf-8", errors="replace") if s3.exists() else None
        base = f"{section_prefix(body, idx, md.stem, s3_text, body_seen)}-{stem}"
        # Two stage-4 files reducing to the SAME base name would silently overwrite —
        # `dest/<base>.md` is written blind, and a whole section would leave stage 5 with
        # no error and no trace. It has never fired across the 76 runs on disk, but the
        # names it guards are derived (a prefix off the heading, a stem off the filename)
        # and a document that made two agree would lose a section the same way an
        # appendix goes missing: quietly. Loud beats quiet for a conservation failure.
        if base in seen:
            raise ValueError(
                f"stage 5 name collision in {job_dir}: {seen[base]!r} and {md.name!r} both "
                f"reduce to {base!r}; one would overwrite the other")
        seen[base] = md.name
        rec = {"file": md.name, "renamed": base, "subsections": [s["id"] for s in subs]}
        out.append(rec)
        if dry_run:
            continue
        # Resolved BEFORE anything is written, because the parent file needs it as much as
        # the children do: 00-section.md is the section's own landing page, and restoring
        # the breadcrumb only to the sub-chunks left it unnavigable.
        crumb = _parent_source(body)
        if crumb is None:
            # Stage 4 REWRITES each section, and an earlier prompt had it dropping the
            # breadcrumb from 8 of the 20 files it touched. Stage 3 is the input it was
            # handed and always carries one, so recover it from there rather than leave a
            # whole section unnavigable because a model deleted a line -- otherwise
            # invisible until someone notices the PDF pane not moving.
            s3 = job_dir / "03_stage3_final" / md.name
            if s3.exists():
                crumb = _parent_source(s3.read_text(encoding="utf-8", errors="replace"))
                if crumb:
                    rec["source_recovered_from_stage3"] = True
        if crumb and not _SOURCE.search(body):
            body = _with_source(body, *crumb)
            parent = _with_source(parent, *crumb)
        if not subs:
            (dest / f"{base}.md").write_text(body)
            continue
        # The parent goes INSIDE the folder, as its first entry. Kept at the top level
        # it produced two adjacent nodes with the same name — "07-marketing-selling.md"
        # beside "07-marketing-selling/" — which reads as a duplicate rather than as a
        # section and its parts. One top-level entry per section instead.
        folder = dest / base
        folder.mkdir(exist_ok=True)
        (folder / "00-section.md").write_text(parent)
        rec["files"] = [f"{base}/00-section.md"]
        # Each sub-chunk gets its OWN breadcrumb, narrowed to the pages it actually
        # covers, so the viewer's PDF pane lands on 6.2 rather than on the section start.
        pages: dict[str, int] = {}
        if crumb:
            pdf_name, lo, hi = crumb
            pdf = job_dir / pdf_name
            if pdf.exists():
                pages = _locate_subsections(pdf, [x["id"] for x in subs], lo, hi)
            rec["pages_located"] = len(pages)
        for i, s in enumerate(subs, 1):
            # Ordinal prefix because the ids sort wrong as text: section 8 runs to 8.13,
            # and "8.10" lands before "8.2" in any lexical ordering.
            name = f"{i:02d}-{s['id']}-{slug(s['title'])}.md"
            text = s["body"]
            if crumb:
                pdf_name, lo, hi = crumb
                start = pages.get(s["id"], lo)
                # Ends where the NEXT located sub-section starts -- they routinely share a
                # sheet, so this is inclusive rather than next-1, which would invert the
                # range on a page holding two headings.
                later = [pages[x["id"]] for x in subs[i:] if x["id"] in pages]
                end = min(later) if later else hi
                text = _with_source(text, pdf_name, start, max(end, start))
            (folder / name).write_text(text)
            rec.setdefault("files", []).append(f"{base}/{name}")

    report = {"ran": True, "reason": why, "source": src.name,
              "sections": out,
              "sections_split": sum(1 for r in out if r["subsections"]),
              "subchunks_total": sum(len(r["subsections"]) for r in out)}
    if not dry_run:
        (dest / "SUBCHUNK_REPORT.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    r = run(Path(args.job_dir), args.dry_run)
    if not r["ran"]:
        print(f"skipped: {r['reason']}")
        return
    print(f"{'(dry run) ' if args.dry_run else ''}{r['reason']} — "
          f"{r['sections_split']} section(s) split into {r['subchunks_total']} sub-chunks")
    for s in r["sections"]:
        if s["subsections"]:
            print(f"  {s['file']:44s} -> {', '.join(s['subsections'])}")


if __name__ == "__main__":
    main()
