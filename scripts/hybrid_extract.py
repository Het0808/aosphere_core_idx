#!/usr/bin/env python3
"""
hybrid_extract — 3-stage local PDF extraction pipeline (dev/testing only, no
S3, no other external source — everything reads/writes local disk).

  Stage 1  extract    pdf2mdtree.py --defer-tables — the same deterministic
                       markdown-tree conversion as the existing single-stage
                       pipeline (headings, prose, footnotes, formulas), EXCEPT
                       detected tables are left as visible placeholders. That
                       script's own cell-splitting does a poor job on
                       ruled-but-columnless tables, which is why this exists.
  Stage 2  tables      Crop every page that contains a deferred table into one
                       combined mini-PDF (original page order preserved), run
                       it once through MinerU (hybrid-engine / effort=medium —
                       MinerU's table-structure model does a much better job
                       than local ruling heuristics), and re-serialize each
                       table's raw HTML (rowspan/colspan preserved natively —
                       GFM markdown has no cell-merge syntax, so flattening to
                       a pipe table would mean faking a merge by duplicating
                       or blanking text). Multi-page tables get their blocks
                       stitched together (dedup a repeated header row, merge a
                       later block's leading row into the previous block's
                       trailing row if a value wrapped across the page-crop
                       boundary).
  Stage 3  combine     Splice Stage 2's markdown tables into Stage 1's tree at
                       their placeholders. Any table MinerU could not produce
                       is left as a clearly-flagged failure marker — never
                       silently dropped or papered over — so failures stay
                       visible while validating quality.

Every stage's output is kept on disk under -o <out_dir>/<pdf-stem>/ so you can
inspect exactly what happened at each step:

  01_stage1_extract/        tree with table placeholders + tables_manifest.json
  02_stage2_mineru_tables/  combined_tables.pdf, raw MinerU output, per-table
                             table.html / table.md / status.json
  03_stage3_final/          the final merged tree + stage3_report.json

Usage:
  python3 scripts/hybrid_extract.py input.pdf -o out_dir [--depth 3]
                                    [--mineru-backend hybrid-engine] [--mineru-effort medium]

When two separate tables share one source page, MinerU's blocks for that page
are paired to pdf2mdtree's table regions by vertical (top-to-bottom) order
rather than by page alone — coordinate systems differ (pdf2mdtree's bbox is in
PDF points, MinerU's in its own rendered-image space), but top-to-bottom
ordering is preserved in both, which is all that's needed to tell two stacked
tables apart.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pdf2mdtree  # noqa: E402  (reused for normspace)

sys.path.insert(0, str(HERE.parent / "src"))
from aosphere_core_index.config import settings  # noqa: E402
from aosphere_core_index.extract.mineru_extract import run_mineru_to_dir  # noqa: E402

import fitz  # noqa: E402
from lxml import etree  # noqa: E402

TABLE_RE = re.compile(r"<!-- TABLE:(\w+) -->.*?<!-- /TABLE:\1 -->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# Rule B (geometric table matching): pdf2mdtree's placeholder bbox vs MinerU's
# table-block bbox, both converted to the same PDF-point space (see
# _mineru_bbox_to_pt). CONFIDENT_IOU is the accept-without-review bar;
# CONSIDER_IOU is the floor below which a block isn't even considered related
# (filters out unrelated tables that happen to share a page).
# CONT_EDGE_FRAC / MERGE_CHAIN_HOPS / REGION_COVERED / MIN_REGION_TOKENS were
# defined here, for the page-edge continuation guess and the bag-of-words
# absorption test. Both are gone — see the deletion notes in run_stage2 and the
# threshold below that replaced them.

# ---- verbatim-sequence verification (the referee for every block assignment) ----
# A placeholder with no table block of its own is the EXPECTED outcome whenever
# MinerU merges a page-spanning table: it attaches the whole table to one page
# and leaves empty stubs for the rest. The pipeline used to treat that as a
# failure to be solved by guessing (adopt any unclaimed block nearby, or infer a
# "continuation" from stub/edge geometry), which is how a 2-column grey box from
# a different section got spliced into the middle of a 4-column table.
#
# So instead of asking "which block belongs to this placeholder?", ask the
# question the SOURCE PDF can actually answer: "does this region's own text
# appear, VERBATIM, inside some block?"
#
# Word-overlap cannot answer it — measured on the corpus, the wrong block scored
# 23% and the right one 33% (legal documents share far too much vocabulary), and
# bag-of-words recall scored ~100% for everything including genuinely lost
# tables. Contiguous word sequences separate them cleanly: on the page-38..40
# run that motivated this, the true merge target scored 96.1%, the neighbouring
# block 6.1%, and the wrongly-adopted grey box 0.0%. Corpus-wide the medians are
# 98.8% for tables that own their block and 0-9.5% for every other outcome, so
# the threshold sits in a very wide empty gap and is not finely tuned.
SHINGLE_N = 6             # words per sequence; long enough that shared legal
                          # phrasing cannot match by coincidence
REGION_VERBATIM = 0.50    # share of the region's sequences found in a block
MIN_REGION_SHINGLES = 10  # below this the region is too short to judge

CONFIDENT_IOU = 0.5
CONSIDER_IOU = 0.15


def slug_stem(pdf_path):
    stem = Path(pdf_path).stem
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-") or "document"


def _iou(a, b):
    """Intersection-over-union of two [x0,y0,x1,y1] boxes in the SAME space. 0
    if either box is missing or they don't overlap."""
    if not a or not b:
        return 0.0
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _mineru_bbox_to_pt(bbox_norm, page_w_pt, page_h_pt):
    """MinerU's content_list.json normalizes bbox to a 0-1000 scale PER AXIS —
    x relative to page width, y relative to page height (see
    make_blocks_to_content_list in mineru's vlm_middle_json_mkcontent.py:
    `x * 1000 / page_width`). Both pdf2mdtree (PyMuPDF) and MinerU already use
    a top-left origin with y increasing downward, so this is a plain per-axis
    rescale — no axis flip needed. Verified empirically against a real
    combined_tables.pdf run: PyMuPDF bbox [56,80,538,615] on a 595x842pt page
    <-> MinerU bbox [92,91,905,722] on the same table, and 92/1000*595=54.7,
    905/1000*595=538.5 — matches within a few points."""
    if not bbox_norm or not page_w_pt or not page_h_pt:
        return None
    x0, y0, x1, y1 = bbox_norm
    return [x0 * page_w_pt / 1000, y0 * page_h_pt / 1000,
            x1 * page_w_pt / 1000, y1 * page_h_pt / 1000]


# ---------------- stage 1: extract (tables deferred) ----------------
# ---- extraction progress -----------------------------------------------------
# A long extraction used to be entirely opaque while it ran. Stage 2 shells out to
# MinerU as ONE subprocess that writes its content list only at the end, so there
# is no per-table signal to poll and `mineru_raw/` sits at zero bytes for twenty
# minutes — which reads as a hung job. The dashboard could only say "running".
#
# So each stage stamps a small progress.json in the job dir as it starts and
# finishes. It is written best-effort and never raises: a heartbeat that can sink
# an extraction is worse than no heartbeat.
# The stages a run reports progress through. stage4_ai and stage5_subchunk are opt-in
# (ACI_STAGE4_AI_ENABLED) so a normal run finishes at stage3 and never advances past it —
# they are named here so the progress file always shows what a run COULD do, and a
# consumer can tell "not enabled" from "still running".
PIPELINE_STAGES = ("stage1", "stage2_mineru", "stage3", "stage4_ai", "stage5_subchunk")


def write_progress(job_dir, stage: str, status: str, **detail) -> None:
    try:
        path = Path(job_dir) / "progress.json"
        try:
            cur = json.loads(path.read_text())
        except (OSError, ValueError):
            cur = {"done": []}
        if status == "done" and stage not in cur["done"]:
            cur["done"].append(stage)
        cur.update({"stage": stage, "status": status, "at": time.time(),
                    "stages": list(PIPELINE_STAGES), "detail": detail or cur.get("detail") or {}})
        path.write_text(json.dumps(cur, indent=1))
    except Exception:  # noqa: BLE001 — never let the heartbeat break the run
        pass


def _log_stage1(stdout: str) -> None:
    """Stage 1's report as ONE log line, with arrays reduced to counts.

    The report is machine output, not prose: it carries `pages_snapshotted` (57 page numbers on a
    354-page document) and warnings listing every unlocated heading. Echoed verbatim that is a
    ~60-line record per document — ~65,000 lines across a full run — and in Kibana each line is a
    separate record, so the useful fields are buried in their own payload.

    So: the numbers on one line, then each warning on its own line, truncated. The full report is
    already on disk as report.json for anyone who needs the arrays."""
    try:
        r = json.loads(stdout.strip().splitlines()[-1])
    except Exception:                                        # noqa: BLE001
        head = " ".join(stdout.split())[:400]
        print(f"  [stage1] {head}")
        return
    warn = r.get("warnings") or []

    def kv(k, v):
        # logfmt: a value containing spaces must be quoted, or "src=PDF bookmark outline" parses
        # as three fields and the log tool indexes rubbish.
        return f'{k}="{v}"' if isinstance(v, str) and " " in v else f"{k}={v}"

    bits = [kv("pages", r.get("pages")),
            kv("outline", r.get("outline_entries")),
            kv("src", r.get("structure_source")),
            kv("paras", r.get("paragraphs")),
            kv("footnotes", r.get("footnotes_found")),
            kv("files", r.get("files_written")),
            kv("snapshots", len(r.get("pages_snapshotted") or [])),
            kv("headings", f"{r.get('headings_matched')}/{r.get('headings_total')}"),
            kv("delta_pct", r.get("word_delta_pct")),
            kv("warnings", len(warn))]
    print("  [stage1] " + " ".join(b for b in bits if not b.endswith("=None")))
    for w in warn:
        one = " ".join(str(w).split())
        print(f"  [stage1] warn: {one[:220]}{'…' if len(one) > 220 else ''}")


def derotate_pdf(path, log=print) -> int:
    """Bake /Rotate into the page content so text reads upright. -> pages changed.

    A page with /Rotate 90 hands back text whose coordinates are in UNROTATED space:
    reading order runs along DECREASING y and successive lines advance along INCREASING
    x. Every geometric judgement Stage 1 makes assumes the opposite — y is the line
    axis, x is the position within a line — so on a rotated page it reads the document
    sideways. Line order, table-region containment, heading position, bullet indent:
    all of it is measured on the wrong axis.

    Japan 172122 is what this cost. 62 of its 83 pages are /Rotate 90, and its page 16
    prints "4. [SECTION INTENTIONALLY LEFT BLANK]" above "5. PASSIVE MARKETING
    (REVERSE-ENQUIRY)". Rotated, those two sit at y=514.1 and y=504.8 — side by side in
    rotated space, not stacked — so sorting by y put clause 5 BEFORE clause 4 in the
    paragraph stream. The outline matcher only ever moves forward (`pi = found`), so
    once clause 4 matched at the later index, clause 5 could never be found: it went
    into the "could not be located" list with 7 others, its content merged into the
    empty clause-4 stub, and that stub then owned pages 16-22. Clause 10 was lost the
    same way on page 66, behind clause 9's stub.

    Bahamas 183503 — the document Japan was compared against — has all 74 pages upright,
    which is the whole reason its tree came out clean. Nothing else about it differed.

    RARE, so the blast radius is small by construction: 3 of 153 documents in the corpus
    have any rotated page at all (Japan 62/83, Greece 138436 6/13, Austria 137944 6/14),
    74 rotated pages in 13,603. A document with none is not rewritten at all.

    Appearance is preserved — remove_rotation folds the rotation into the content matrix
    rather than discarding it — so page snapshots, the MinerU crops and Stage 4's page
    images all still render exactly as the document prints. Never raises: a PDF that
    cannot be rewritten is left alone and extracted as it would have been before.
    """
    path = Path(path)
    try:
        doc = fitz.open(str(path))
    except Exception as e:                                   # noqa: BLE001
        log(f"  [derotate] cannot open {path.name}: {type(e).__name__}: {e}")
        return 0
    try:
        pages = [pg for pg in doc if pg.rotation]
        if not pages:
            return 0
        if not hasattr(pages[0], "remove_rotation"):
            log("  [derotate] PyMuPDF has no remove_rotation(); pages left rotated")
            return 0
        n = 0
        for pg in pages:
            pg.remove_rotation()
            n += 1
        # Written beside the original and moved into place, because saving over an open
        # document needs either incremental mode or a second file, and incremental
        # cannot rewrite page content.
        tmp = path.with_suffix(path.suffix + ".derot")
        doc.save(str(tmp), garbage=3, deflate=True)
        doc.close()
        os.replace(tmp, path)
        log(f"  [derotate] {n} rotated page(s) baked upright in {path.name}")
        return n
    except Exception as e:                                   # noqa: BLE001
        log(f"  [derotate] {path.name} left as-is: {type(e).__name__}: {e}")
        return 0
    finally:
        try:
            doc.close()
        except Exception:                                    # noqa: BLE001
            pass


def run_stage1(pdf_path, stage1_dir, depth=None, clause_tables=None, product=None):
    """Stage 1, with this document's PRODUCT RULE resolved here rather than by the
    caller.

    depth / clause_tables default to None meaning "use the rule". Passing either
    explicitly overrides it, which is what a CLI --depth is for.

    The lookup lives here because it used to live in the callers, and only ONE of
    them did it: run_corpus. A 124 document rebuilt through the dashboard upload,
    run_hybrid_one or run_baseline came out at the pipeline default with its
    subsections split and its clause tables fragmented, and nothing said so. The
    product is read from the job's own corpus_meta.json (stage1_dir's parent), so
    any entry point that writes into a corpus job dir now gets the rule for free."""
    from product_rules import (
        clause_table_runs,
        group_label_headings,
        product_for_job,
        section_depth,
    )
    prod = product or product_for_job(Path(stage1_dir).parent)
    if depth is None or clause_tables is None:
        if depth is None:
            depth = section_depth(prod)
        if clause_tables is None:
            clause_tables = clause_table_runs(prod)
    # Resolved unconditionally rather than behind the None-defaults above: an explicit
    # --depth from a caller must not also silently decide whether appendix labels are
    # joined to their headings.
    group_labels = group_label_headings(prod)

    # Clean slate: a stale tree from an earlier run would otherwise survive
    # alongside the new one (pdf2mdtree only ever overwrites the files it writes
    # this time), leaving orphaned sections and placeholders whose ids no longer
    # appear in the fresh manifest.
    job = Path(stage1_dir).parent
    write_progress(job, "stage1", "running", depth=depth, clause_tables=bool(clause_tables))
    if Path(stage1_dir).exists():
        shutil.rmtree(stage1_dir)
    cmd = [sys.executable, str(HERE / "pdf2mdtree.py"), str(pdf_path),
           "-o", str(stage1_dir), "--depth", str(depth)]
    if clause_tables:
        cmd.append("--clause-tables")
    if group_labels:
        cmd.append("--group-labels")
    # encoding/errors pinned rather than locale-derived. Stage 1's diagnostics quote the
    # document's own text — heading names, dropped paragraphs — and these are legal memos
    # for every jurisdiction on earth (section marks, curly quotes, "Côte d'Ivoire", CJK).
    # Under a cp1252 default those bytes raise UnicodeDecodeError in subprocess's reader
    # thread, which does NOT raise here: returncode stays 0 and the warnings below simply
    # vanish, so a document silently loses the only report of what Stage 1 dropped.
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.stdout:
        _log_stage1(proc.stdout)
    # Stage 1's warnings (unmatched outline headings, word-count delta) go to
    # stderr. Surfacing them ONLY on failure meant they were never seen in a
    # normal run — which is exactly when they matter.
    if proc.stderr:
        for line in proc.stderr.rstrip().splitlines():
            print(f"  [stage1] {line}", file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"stage 1 (pdf2mdtree.py) failed:\n{proc.stderr}")
    manifest_path = stage1_dir / "tables_manifest.json"
    if manifest_path.exists():
        write_progress(job, "stage1", "done",
                       regions=len((json.loads(manifest_path.read_text()) or {}).get("tables", [])))
        return json.loads(manifest_path.read_text())
    return {"tables": []}


# ---------------- stage 2: MinerU table extraction ----------------
def build_combined_pdf(pdf_path, pages, dest_path):
    """pages: sorted, deduped 1-indexed page numbers. Returns
    {position_in_mini_pdf (0-indexed): original_page_number (1-indexed)}.

    A BLANK SEPARATOR PAGE is inserted wherever two consecutive selected pages
    are NOT adjacent in the source document.

    MinerU runs its own page-spanning table merge over whatever it is handed,
    and it cannot know this mini-PDF is a crop: two tables thirteen pages apart
    in the real document sit side by side here, and when the first runs to the
    bottom edge and the second starts at the top — the exact signature MinerU
    treats as one table continuing — it merges them. Measured on
    155_Data_Privacy/Angola: the consent table on page 24 absorbed the whole
    international-transfers table from page 37, and page 37's own placeholder
    was then matched to the empty stub MinerU leaves behind for a region it
    merged elsewhere. Six documents in the corpus carry that signature.

    The separator breaks the adjacency that triggers the merge. It gets NO
    offset_map entry, and every consumer resolves blocks via
    offset_map.get(page_idx) and skips an unmapped page, so a stray block
    landing on a separator is dropped rather than misattributed."""
    src = fitz.open(str(pdf_path))
    mini = fitz.open()
    offset_map = {}
    prev = None
    for pg in pages:
        if prev is not None and pg != prev + 1:
            r = src[pg - 1].rect
            mini.new_page(width=r.width, height=r.height)
        mini.insert_pdf(src, from_page=pg - 1, to_page=pg - 1)
        offset_map[mini.page_count - 1] = pg
        prev = pg
    mini.save(str(dest_path))
    mini.close()
    src.close()
    return offset_map


def _int(v, default=1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_SUP = {**{str(d): c for d, c in zip(range(10), "⁰¹²³⁴⁵⁶⁷⁸⁹")},
        "+": "⁺", "-": "⁻", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ"}
_SUB = {**{str(d): c for d, c in zip(range(10), "₀₁₂₃₄₅₆₇₈₉")}, "+": "₊", "-": "₋"}


_HTML_SUP_RE = re.compile(r"<sup>\s*(\d+)\s*</sup>", re.IGNORECASE)
# A footnote marker MinerU dropped straight into running text with no space
# and no markup at all — "share5", "above11)", "receipts14." — the single
# case this function cannot detect from the string alone (see
# _glue_footnote_re below, which needs the caller's known-id set to be safe).
_GLUE_DIGITS_RE = re.compile(r"(?<=[A-Za-z’”)\"'])(\d{1,4})(?!\d)")


def clean_mineru_text(s: str, footnote_ids: frozenset = frozenset()) -> str:
    """MinerU renders a footnote reference three different ways depending on
    backend/content type, and none of them is `[^n]`:

      `$^{1422}$`        inline LaTeX superscript (math-mode tables)
      `<sup>1422</sup>`  literal HTML tag (OCR/vision "text" blocks)
      `word1422`         glued straight onto the preceding word with NO
                         markup at all — MinerU's table-structure model
                         discards the font-size/position difference for
                         some cells, so nothing distinguishes it from a
                         real number already present in that string

    The prose extractor emits real footnotes as `[^n]` and the viewer turns
    `[^n]` into a linked superscript (renderFootnotes), so all three are
    converted to that same form — the table then references (and links)
    footnotes exactly like the prose, and check_footnote_integrity /
    check_numeric_integrity both recognize them.

    The first two forms are unambiguous (only a footnote reference is ever
    wrapped that way) and convert unconditionally, gated only on the inner
    text being all-digits — a non-numeric super/subscript (rare — actual
    math) falls back to Unicode instead. The third form is NOT
    unambiguous — "share5" could just as easily be a real "5" that happens
    to sit next to a word — so it only converts when the glued digits equal
    a footnote id `footnote_ids` already confirms exists elsewhere in this
    document (pdf2mdtree's own page-bottom footnote-body pass, which MinerU
    never touches). No `footnote_ids` given -> this third form is left
    untouched, same as before this function knew about it.

    A bare '$' (currency, '$50,000') is untouched; only the braced ^{}/_{}
    forms match."""
    def _sup(m):
        inner = m.group(1)
        return f"[^{inner}]" if inner.isdigit() else "".join(_SUP.get(c, c) for c in inner)
    s = re.sub(r"\$\^\{([^}]*)\}\$", _sup, s)
    s = re.sub(r"\$_\{([^}]*)\}\$", lambda m: "".join(_SUB.get(c, c) for c in m.group(1)), s)
    s = _HTML_SUP_RE.sub(lambda m: f"[^{m.group(1)}]", s)
    if footnote_ids:
        s = _GLUE_DIGITS_RE.sub(
            lambda m: f"[^{m.group(1)}]" if int(m.group(1)) in footnote_ids else m.group(0), s)
    return s


_TAG_RE = re.compile(r"(<[^>]+>)")
_ENTITY_RE = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_CELL_RE = re.compile(r"(<(?:td|th)\b[^>]*>)(.*?)(</(?:td|th)>)", re.I | re.S)

# Below this share of a cell's characters aligning to the page, the alignment is not
# this cell's text and nothing is inserted. Deliberately strict: the whole value of
# this pass is that it never invents a space MinerU's own text did not ask for.
_REALIGN_MIN_COVERAGE = 0.9
_REALIGN_MIN_CHARS = 8
# Hyphen, non-breaking hyphen, soft hyphen: a break after one of these is a word the
# PDF split across lines, not two words. See the guard in _realign_cell.
_HYPHENS = "-‐‑­"
# What counts as the PDF saying "these are two words": a real space, not a bare
# newline. See the separator check in _realign_cell.
_SPACES = " \t   "
# How far apart the two characters may be in the page and still count as adjacent.
# 1 is pure whitespace between them; 2 leaves room for a single list glyph, which is
# how a bulleted cell reads once whitespace is dropped ("include:*Abrief").
_REALIGN_MAX_PDF_GAP = 2
# How far either side of a merged block's anchor page its cells are looked for, as a
# LADDER rather than every radius: a cell that is where MinerU said it is stops at the
# first rung, and only a merged monster pays for the rest. The far rungs are needed --
# Australia 181815 table_006 holds ONE cell of 19,676 characters spanning 28 crop pages
# from an anchor on page 7, and it reaches full coverage only at +/-20.
_REALIGN_WINDOW_STEPS = (0, 1, 2, 4, 8, 14, 20)
# Widening that twice in a row without covering any more of the cell means the text is
# not out there; stop rather than pay for the remaining rungs.
_REALIGN_STALL_LIMIT = 2
# Characters of a cell used to find it in the page, and the slack allowed around
# the slice that comes back. See _locate.
# A line break recovered from the PDF, carried through the cell as a character rather
# than as markup. Private-use, so it is not whitespace (normspace leaves it alone) and
# cannot occur in a real document; _serialize_row turns it into <br> at the very end.
_BREAK = "\ue000"
_REALIGN_PROBE = 24
_REALIGN_PAD = 200
# Coverage at which a candidate is taken as complete and the search stops.
_REALIGN_COMPLETE = 0.995


def _squash(s: str) -> tuple[str, list[int]]:
    """s -> (s with every whitespace character removed, source index of each survivor)."""
    keep, idx = [], []
    for i, ch in enumerate(s):
        if not ch.isspace():
            keep.append(ch)
            idx.append(i)
    return "".join(keep), idx


def realign_table_spacing(html: str, page_text: str) -> str:
    """Put back the spaces MinerU drops between lines and style runs inside a cell.

    MinerU's `hybrid` and `vlm` backends do not read a table's text; they hand the
    table IMAGE to a VLM (MinerU2.5-Pro) and take whatever HTML it returns --
    hybrid_magic_model assigns `span["html"] = block_content` with no whitespace
    handling of any kind. The model emits a cell's lines end to end with NO
    separator, so a bulleted cell on page 50 of ADGM 170680:

        Such tear sheets may include:
        *  A brief description of strategy
        *  Summary statistics

    arrives as `...may include:A brief description of strategySummary statistics`.
    The same happens at every bold/italic run: `wording in `+`Section D`+` of each`
    becomes `wording inSection Dof each`. Measured across this corpus: 3,832
    occurrences in 115 of 144 documents, and that is only the ones a capital letter
    makes visible -- a lower-to-lower collision leaves no trace at all.

    This is MinerU behaving as built, not a misconfiguration: its own Gradio app
    produces the identical glue on the identical document. Only the `pipeline`
    backend joins cell fragments with a space (slanet_plus matcher), and that path
    is a different table model, on a corpus where MINERU_EFFORT already records what
    changing the table backend costs.

    So the spacing is recovered from the one source that still has it: the crop
    PDF's own text layer. Every run in this corpus is `ocr_enable: False`, meaning
    a real text layer exists -- and MinerU ALREADY prefers that layer over the model
    for text blocks in non-OCR mode (`not_extract_list`). Tables are simply not on
    that list. This does for a table cell what MinerU does for a paragraph.

    The cell's own text is never replaced, only spaced: both sides are compared with
    all whitespace removed, and a space goes in exactly where the cell has none and
    the PDF has some. So a mis-alignment cannot import the wrong words, and a cell
    aligning below _REALIGN_MIN_COVERAGE is left untouched. Tags are preserved
    rather than flattened -- clean_mineru_text still needs to see `<sup>`.

    NOT restored: line breaks and bullet glyphs. Not an oversight -- _tr_to_cells
    flattens every cell through itertext() and normspace(), so a `<br>` would be
    dropped and a newline collapsed to a space by the very next stage. Preserving
    in-cell line structure is a change to the table rendering model, not to spacing.

    An HTML entity in the cell (`&amp;` against the page's `&`) simply fails to
    align over those few characters, which costs a possible insertion there and
    nothing else.
    """
    texts = [t for t in ((page_text,) if isinstance(page_text, str)
                         else tuple(page_text or ())) if t]
    if not html or not texts:
        return html
    # Squashed once per candidate and kept; a later, wider candidate is usually never
    # asked for at all, because the first one already covers the cell.
    cache: dict[int, tuple | None] = {}

    def prepared(i):
        if i not in cache:
            ps, pidx = _squash(texts[i])
            cache[i] = (ps, pidx, texts[i]) if ps else None
        return cache[i]

    return _CELL_RE.sub(
        lambda m: m.group(1) + _realign_cell(m.group(2), prepared, len(texts)) + m.group(3),
        html)


def _locate(cs: str, ps: str):
    """Where in the squashed page `ps` the squashed cell `cs` most likely sits.

    An exact probe rather than a diff: MinerU's cell text IS the page's text with the
    whitespace dropped, so a run of characters from the cell appears verbatim in the
    page unless something (a bullet, an entity) interrupts that particular run. Four
    probes taken across the cell mean one interruption cannot hide it.

    The slice is padded because the page legitimately holds MORE than the cell -- the
    bullet glyphs and list markers MinerU dropped. Returns None when no probe lands,
    which leaves the cell for the next candidate and, failing that, untouched.
    """
    if len(cs) < _REALIGN_PROBE:
        j = ps.find(cs)
        return (max(0, j - _REALIGN_PAD), min(len(ps), j + len(cs) + _REALIGN_PAD)) \
            if j >= 0 else None
    pad = max(_REALIGN_PAD, len(cs) // 4)
    for frac in (0.0, 0.25, 0.5, 0.75):
        i = min(int(len(cs) * frac), len(cs) - _REALIGN_PROBE)
        j = ps.find(cs[i:i + _REALIGN_PROBE])
        if j >= 0:
            return max(0, j - i - pad), min(len(ps), j - i + len(cs) + pad)
    return None


def _realign_cell(inner: str, prepared, n_candidates: int) -> str:
    """One cell's inner HTML, spaced against the first prepared page it aligns to.

    More than one page is offered because content_list.json MERGES a page-spanning
    table into a single block, so the block's own page holds only part of its cells
    (Argentina 172099 table_005: the anchor page has no "may apply / The Manager" in
    it at all, and the cell stayed glued while the same table's other rows were fixed).
    Candidates are tried nearest-first and the search stops at the page that covers
    this cell, so a cell that is where MinerU said it was still costs one attempt.

    Tags are carried through untouched; only space characters are ever inserted, and
    only into text pieces."""
    pieces = _TAG_RE.split(inner)
    plain, where = [], []          # cell text, and (piece index, offset) of each char
    for pi, piece in enumerate(pieces):
        if piece.startswith("<") and piece.endswith(">"):
            continue
        oi = 0
        while oi < len(piece):
            # An entity is ONE character, standing at its "&". Two reasons, and both
            # matter: it aligns against the character the PDF actually holds (`&quot;`
            # against a real `"`), and an insertion can then only ever land on the `&`
            # -- never inside, which is what produced `&quo t;` in ADGM 170680 p4 and
            # would have reached the reader as literal text.
            m = _ENTITY_RE.match(piece, oi)
            if m:
                plain.append((html_unescape(m.group(0)) or " ")[:1])
                where.append((pi, oi))
                oi = m.end()
                continue
            plain.append(piece[oi])
            where.append((pi, oi))
            oi += 1
    cs, cidx = _squash("".join(plain))
    if len(cs) < _REALIGN_MIN_CHARS:
        return inner

    at = pidx = page_text = None
    best = _REALIGN_MIN_COVERAGE * len(cs)
    high, stalled = -1, 0
    for i in range(n_candidates):
        cand = prepared(i)
        if cand is None:
            continue
        ps, cand_pidx, cand_text = cand
        span = _locate(cs, ps)
        if span is None:
            continue
        lo, hi = span
        # Aligned against the SLICE the cell was located in, not the whole candidate.
        # Diffing a 20k-character cell against 90k characters of joined pages took 134s
        # on Australia 181815 alone; a local window makes the work proportional to the
        # cell instead of to the document, and the coverage test below still decides.
        sm = difflib.SequenceMatcher(autojunk=False)
        sm.set_seq2(ps[lo:hi])
        sm.set_seq1(cs)
        found, covered = {}, 0
        for a, b, size in sm.get_matching_blocks():
            for k in range(size):
                found[a + k] = b + lo + k
            covered += size
        if covered >= best:
            at, pidx, page_text, best = found, cand_pidx, cand_text, covered
        stalled = stalled + 1 if covered <= high else 0
        high = max(high, covered)
        if stalled >= _REALIGN_STALL_LIMIT:
            break
        if covered >= _REALIGN_COMPLETE * len(cs):
            # Wholly accounted for: a wider candidate can only tie, so stop paying for
            # one. Taking the BEST rather than the first is what matters for a cell
            # that straddles a page break -- the anchor page alone can clear 90% while
            # still missing the very join that needs the space (Argentina 172099
            # table_005, "may apply" ending page 6 and "The Manager" opening page 7).
            break
    if at is None:
        return inner

    inserts = []
    for k in range(len(cs) - 1):
        if cidx[k + 1] != cidx[k] + 1:
            continue                        # the cell already separates these two
        if cs[k] in _HYPHENS:
            # "Closed-Ended" printed as "Closed-" / "Ended" over a line break is ONE
            # word the PDF happens to have split, and MinerU rejoined it correctly.
            # The line break is whitespace, so without this the pass would undo that
            # and emit "Closed- Ended" (ADGM 170680 p13, both Open-Ended and Closed-
            # Ended). A hyphen before a break carries no evidence either way, so the
            # conservative reading -- leave MinerU's own join alone -- wins.
            continue
        pa, pb = at.get(k), at.get(k + 1)
        if pa is None or pb is None or pb <= pa:
            continue
        if pb - pa > _REALIGN_MAX_PDF_GAP:
            # The two characters are adjacent in the cell but far apart in the page,
            # so this pair is not one alignment -- the matcher has paired them across
            # a jump, and whatever whitespace lies between belongs to neither. Without
            # this, a page whose wording repeats pairs characters with a distant twin
            # and sprays spaces into words: Australia 181814 p94 read "N o, h owe ver".
            continue
        # Whatever sits between the same two characters in the PDF. A SPACE is the
        # evidence, not whitespace in general: a narrow column wraps a word mid-word
        # with a bare newline and no space -- Bahamas 183503 p31 holds a literal
        # "environment\nal, social", and Belgium 163341 the same word broken one
        # letter later. MinerU rejoins those correctly, and reading the newline as a
        # separator would break the word back apart ("environment al"). A real word
        # boundary in these PDFs always carries a space before the break, so a bullet
        # glyph between the two still qualifies -- it cannot sit there without one.
        gap = page_text[pidx[pa] + 1:pidx[pb]]
        if any(c in _SPACES for c in gap):
            # A newline in that gap means the two were on different LINES of the cell --
            # the bulleted lists this product writes its statutes and its tear-sheet
            # contents as. Recorded as a break rather than a space so the cell can be
            # rendered the way it is printed; a gap with only a space is a space.
            inserts.append((cidx[k + 1], _BREAK if "\n" in gap else " "))
    if not inserts:
        return inner

    by_piece = defaultdict(list)
    for b, sep in inserts:
        pi, oi = where[b]
        by_piece[pi].append((oi, sep))
    out = list(pieces)
    for pi, offsets in by_piece.items():
        s = pieces[pi]
        for oi, sep in sorted(offsets, reverse=True):
            s = s[:oi] + sep + s[oi:]
        out[pi] = s
    return "".join(out)


_SNAP_BLOCK = re.compile(
    r"> Page (\d+) of the source PDF contains[^\n]*snapshot for reference:\s*\n\s*\n"
    r"!\[Page \1\]\([^)]*\)\s*\n?")
_PAGES_IN = re.compile(r"pages? (\d+)(?:[–\-](\d+))?")


def _pages_in(line: str) -> set[int]:
    out = set()
    for m in _PAGES_IN.finditer(line):
        a = int(m.group(1)); b = int(m.group(2)) if m.group(2) else a
        out.update(range(a, b + 1))
    return out


def strip_filled_snapshots(md: str) -> str:
    """--defer-tables emits, per table page, BOTH a table placeholder AND a
    "snapshot for reference" page image. Once Stage 2 fills the table (MinerU),
    that snapshot duplicates the same content. Drop the snapshot for any page
    whose table was FILLED — but keep it where a table on that page FAILED (the
    snapshot is the honest fallback there)."""
    filled, failed = set(), set()
    for line in md.splitlines():
        if "MinerU-extracted table" in line:
            filled |= _pages_in(line)
        elif "TABLE EXTRACTION FAILED" in line:
            failed |= _pages_in(line)
    drop = filled - failed
    if not drop:
        return md
    return _SNAP_BLOCK.sub(lambda m: "" if int(m.group(1)) in drop else m.group(0), md)


def blocks_to_prose(blocks, footnote_ids: frozenset = frozenset()):
    """Render MinerU non-table blocks (text / footnotes / titles) as markdown —
    used to RECLASSIFY a false-positive "table" (a region pdf2mdtree flagged as a
    table but which MinerU sees as text/footnotes) back into prose. Footnote
    blocks become `[^n]: …` definitions so the real table's `[^n]` refs resolve.

    Every block's text also goes through clean_mineru_text: this prose path
    reaches the tree completely separately from `[^n]`-emitting pdf2mdtree
    prose, so a superscript MinerU represents as `<sup>n</sup>` or `$^{n}$`
    (see clean_mineru_text) would otherwise survive into the output verbatim,
    as inert markup or LaTeX nobody renders — same defect class as the
    table-cell case, just via the "reclassified as prose" / "adjacent
    prose recovery" routes instead of a `<td>`."""
    # Sort on (page, y) so a multi-page region reads in document order — y alone
    # interleaved page 2's blocks with page 1's. page_idx is MinerU's index into
    # the combined mini-PDF, which is monotonic in source page, so it orders
    # correctly without needing offset_map here.
    def _order(b):
        g = b.get("bbox_pt") or b.get("bbox")
        return (b.get("page_idx", 0), g[1] if g else 0.0)
    out = []
    for b in sorted(blocks, key=_order):
        t = b.get("type")
        if t in ("footer", "page_number", "discarded"):
            continue
        txt = (b.get("text") or "").strip()
        if not txt:
            continue
        # MinerU sometimes classifies a page-bottom footnote BODY as plain
        # "text" rather than "page_footnote" — its own superscript marker
        # (`<sup>11</sup>` / `$^{11}$`) still opens the block, just without the
        # type tag that would otherwise route it through the branch below.
        # Recognized here the same way: caught only when the leading number is
        # a CONFIRMED footnote id, so an ordinary paragraph that happens to
        # start with a superscripted cross-reference is never misfiled as a
        # definition.
        fn_open = re.match(r"^(?:<sup>\s*(\d+)\s*</sup>|\$\^\{(\d+)\}\$)\s+(.*)", txt, re.DOTALL)
        if fn_open and int(fn_open.group(1) or fn_open.group(2)) in footnote_ids:
            n = fn_open.group(1) or fn_open.group(2)
            out.append(f"[^{n}]: {clean_mineru_text(pdf2mdtree.normspace(fn_open.group(3)), footnote_ids)}")
        elif t == "page_footnote":
            m = re.match(r"^(\d+)\s+(.*)", txt, re.DOTALL)
            out.append(f"[^{m.group(1)}]: {clean_mineru_text(pdf2mdtree.normspace(m.group(2)), footnote_ids)}"
                       if m else clean_mineru_text(txt, footnote_ids))
        else:
            out.append(clean_mineru_text(pdf2mdtree.normspace(txt), footnote_ids))
    return "\n\n".join(out)


# ---- multi-page table continuation ------------------------------------------
# How confident are we that a later block's leading row CONTINUES the previous row
# rather than starting new content? Consulted ONLY when the two rows disagree on
# width, because that is the one case where guessing wrong used to cost real text:
# the old code copied min(len(cells), len(prev)) cells and dropped the rest.
#
# Column agreement is deliberately NOT a signal. The rows most in need of rejoining
# are exactly the ones MinerU split unevenly — Bermuda__166524 p41 is 2 cells against
# 3 — so scoring "same width" positively would refuse the very merges this exists to
# perform. What actually discriminates is whether the previous row stops mid-sentence:
# there it ends "...interests in a" and the continuation opens "Fund to an investor".
CONTINUATION_MERGE_MIN = 60
_SENTENCE_END = frozenset('.;:!?"\u201d)]\u2019\'')


def _ends_mid_sentence(text: str) -> bool:
    """True when the text simply stops, with no terminal punctuation to close it."""
    t = (text or "").rstrip()
    return bool(t) and t[-1] not in _SENTENCE_END


def _continuation_confidence(prev, cells, pages=None):
    """-> (score, [reasons]). Merge at or above CONTINUATION_MERGE_MIN."""
    score, why = 0, []
    if not cells[0]["text"]:
        score += 25
        why.append("continuation row starts with a blank cell")
    tail = next((c["text"] for c in reversed(prev) if c["text"]), "")
    if _ends_mid_sentence(tail):
        score += 40
        why.append(f"previous row ends mid-sentence ({tail[-28:]!r})")
    head = next((c["text"] for c in cells if c["text"]), "")
    if head[:1].islower():
        score += 20
        why.append("continuation opens lower-case")
    if pages and len(pages) > 1:
        score += 15
        why.append("region spans consecutive pages")
    return score, why


# ---- which COLUMN does a continuation cell belong to? ------------------------
# Knowing the rows should be joined (above) does not say how their columns line up.
# Merging index-by-index assumes the surplus column is on the RIGHT. In this corpus
# it is on the LEFT: the Part-B table has three bands — row-ref (x~55), question
# (x~91), answer (x~374) — and where a row STARTS the ref marker shares a line with
# the question, so MinerU emits ONE cell for both (2 cells). Where that row CONTINUES
# there is no marker, the question resumes indented in its own band, and MinerU sees
# the empty ref band too (3 cells). The fragments are therefore OFFSET, not truncated.
#
# Index-aligned merging on Bahrain__169749 p52 put the QUESTION tail into the ANSWER
# cell and left the real answer in a phantom third column; the same shape on
# Bermuda__166524 p41 fused a header onto a question. Losslessness alone does not fix
# that — the offset has to be chosen.
#
# So score every candidate offset by how well each pair actually reads as one
# sentence, and take the best. Offset 0 IS the widen-right behaviour, and ties resolve
# to it, so a genuinely trailing surplus column is untouched.
_TRUNCATION_MIN_WORDS = 5
# A cell that stops on one of these is mid-clause no matter how short it is —
# "...market/sell interests in a" is the Bermuda case.
_FUNCTION_WORDS = frozenset("""
a an the of in on at to for with by as and or nor but if that which who whom whose
from into onto upon over under between among through during before after above below
is are was were be been being has have had do does did shall will would may might can
could must not no than then such other any each per via where when while whether
""".split())


def _looks_truncated(text: str) -> bool:
    """Does this cell stop MID-CLAUSE, rather than simply being short?

    `_ends_mid_sentence` alone is too weak to drive column alignment: a label cell
    ("Carrying on Business Restriction") carries no terminal punctuation either, and
    treating it as truncated invents joins that pull a header onto a question. Real
    truncation shows one of two marks — enough words to be prose, or a stop on a
    function word that cannot end a clause.
    """
    t = (text or "").rstrip()
    if not t or not _ends_mid_sentence(t):
        return False
    words = t.split()
    return len(words) >= _TRUNCATION_MIN_WORDS or words[-1].lower() in _FUNCTION_WORDS


def _alignment_score(prev, cells, off):
    """How well does aligning cells[j+off] onto prev[j] read? Higher is better."""
    score = 0
    for j in range(len(prev)):
        k = j + off
        if k >= len(cells):
            break
        tail, head = prev[j]["text"], cells[k]["text"]
        if not tail or not head:
            continue
        if _looks_truncated(tail):
            # A clause cut by a page break resumes in lower case far more often than
            # not; a capital may still be a defined term ("Fund"), so it only damps.
            score += 4 if head[:1].islower() else 3 - 1
        elif head[:1].islower():
            score -= 3          # a CLOSED sentence followed by lower case is not a join
    # Leading cells no prev column claims have to be prepended as new columns — which
    # is right when they hold text MinerU found, but it widens the table, so prefer an
    # alignment that does not need it. Offset 0 consumes everything by widening right.
    score -= 3 * sum(1 for k in range(off) if cells[k]["text"])
    return score


def _alignment_offset(prev, cells):
    """-> (offset, [(score, offset), ...]) for merging cells onto prev."""
    span = len(cells) - len(prev)
    if span <= 0:
        return 0, []
    # Smallest offset wins a tie, so offset 0 (widen right) stays the default.
    scored = sorted(((_alignment_score(prev, cells, o), o) for o in range(span + 1)),
                    key=lambda t: (-t[0], t[1]))
    return scored[0][1], scored


def _tr_to_cells(tr, footnote_ids: frozenset = frozenset()):
    """One <tr> -> [{'text', 'rowspan', 'colspan'}, ...], preserving MinerU's
    own merge structure instead of flattening it — GFM markdown has no
    cell-merge syntax, but raw HTML embedded in markdown renders rowspan/
    colspan correctly (marked, GitHub, browsers all honor it), so keeping the
    real attributes beats faking a merge with duplicated or blanked text."""
    out = []
    for el in tr:
        if el.tag not in ("td", "th"):
            continue
        _raw = "".join(el.itertext())
        # `text` stays exactly what it has always been -- a break reads as the space it
        # would otherwise have been -- because every stitching heuristic below (_row_key,
        # _looks_truncated, _alignment_score, the page-join merges) reads it and was tuned
        # on that shape. The structure rides alongside in `rich`, which ONLY _serialize_row
        # looks at, so restoring line breaks cannot move a multi-page table's seams.
        out.append({
            "text": clean_mineru_text(
                pdf2mdtree.normspace(_raw.replace(_BREAK, " ")), footnote_ids),
            "rich": clean_mineru_text(pdf2mdtree.normspace(_raw), footnote_ids),
            "rowspan": max(1, _int(el.get("rowspan"), 1)),
            "colspan": max(1, _int(el.get("colspan"), 1)),
        })
    return out


def _row_key(cells):
    return tuple(re.sub(r"\W", "", c["text"].lower()) for c in cells)


def _serialize_row(cells):
    parts = ["<tr>"]
    for c in cells:
        attrs = ""
        if c["rowspan"] > 1:
            attrs += f' rowspan="{c["rowspan"]}"'
        if c["colspan"] > 1:
            attrs += f' colspan="{c["colspan"]}"'
        # <br> only here, at the last moment: escaped first so nothing in the cell can
        # inject markup, then the marker alone becomes a tag.
        _body = html_escape(c.get("rich") or c["text"]).replace(_BREAK, "<br>")
        parts.append(f"<td{attrs}>{_body}</td>")
    parts.append("</tr>")
    return "".join(parts)


def stitch_table_html(html_blocks, footnote_ids: frozenset = frozenset(),
                      table_meta=None, anomalies_out=None):
    """html_blocks: ordered raw `table_body` HTML strings, one per matched
    MinerU block for ONE logical table (almost always just one — multi-page
    tables are the exception). Dedupes a repeated header row on a later block
    and merges a later block's leading row into the previous block's trailing
    row when its first cell is blank (a value wrapped across the page-crop
    boundary) — mirroring pdf2mdtree's own multi-page reconstruction heuristic.

    Crucially, that merge check only fires for the FIRST row of a SUBSEQUENT
    block, never mid-block: a blank first cell in the middle of a single
    block's own rows is MinerU's rowspan structure (e.g. a spanning label with
    no text in its own cell), not a page-break artifact — merging it into the
    row above used to silently corrupt otherwise-correct single-page tables.

    A continuation row WIDER than the row it joins is the dangerous case: this used
    to copy min(len(cells), len(prev)) cells and discard the surplus along with the
    row, which silently deleted a 216-word answer on Bermuda__166524 p41. Nothing is
    discarded now, and below CONTINUATION_MERGE_MIN the row is kept whole as its own
    row rather than merged at all.

    Being lossless is not the same as being CORRECT, though. The surplus column in
    this corpus sits at the LEADING edge, so the fragments are offset and merging them
    index-by-index puts the question tail in the answer cell (see _alignment_offset).
    So the offset is chosen by how well each candidate actually reads, and only an
    offset of 0 — a genuinely trailing surplus — widens the previous row. When no
    candidate reads as a continuation the row is kept separate instead of guessed at.
    Every outcome preserves every character, and every decision is recorded: a split
    row is recoverable by a reader, a deleted answer is not, and a silently
    misaligned one looks correct while being wrong.

    table_meta: the region dict (`pages`, `table_id`), used for the page-adjacency
    signal and to label anomalies. anomalies_out: optional list, appended to for every
    width mismatch — the same out-accumulator shape as
    check_content_localized.find_gaps(stats_out=...).

    Returns (html:str|None, n_rows:int, n_cols:int) — n_cols accounts for
    colspan so it reflects visual width, not raw <td> count."""
    meta = table_meta or {}
    pages = meta.get("pages")
    header_key = None
    rows = []
    for bi, block_html in enumerate(html_blocks):
        if not block_html:
            continue
        root = etree.HTML(block_html)
        if root is None:
            continue
        for ri, tr in enumerate(root.iter("tr")):
            cells = _tr_to_cells(tr, footnote_ids)
            if not cells or not any(c["text"] for c in cells):
                continue
            key = _row_key(cells)
            if header_key is None:
                header_key = key
                rows.append(cells)
                continue
            if key == header_key:
                continue  # repeated header on a later block
            if bi > 0 and ri == 0 and rows and not cells[0]["text"]:
                prev = rows[-1]
                offset, lead = 0, []
                if len(cells) > len(prev):
                    conf, why = _continuation_confidence(prev, cells, pages)
                    anom = {"kind": "TABLE_CONTINUATION_WIDER_ROW",
                            "table_id": meta.get("table_id"), "pages": pages,
                            "block": bi, "previous_cells": len(prev),
                            "new_cells": len(cells), "confidence": conf,
                            "signals": why,
                            "surplus_text": [c["text"] for c in cells[len(prev):]
                                             if c["text"]]}
                    if conf >= CONTINUATION_MERGE_MIN:
                        offset, scored = _alignment_offset(prev, cells)
                        anom["alignment_offset"] = offset
                        anom["alignment_scores"] = [{"offset": o, "score": s}
                                                    for s, o in scored]
                        if scored and max(s for s, _ in scored) <= 0:
                            # The rows belong together (conf passed) but NO candidate
                            # alignment reads as one continued clause. Guessing here is
                            # what fuses a question onto an answer, and that corruption
                            # is invisible downstream; a split row is not. So keep it
                            # whole and let the anomaly say why.
                            anom["action"] = "kept_as_separate_row"
                            anom["reason"] = "no column alignment reads as a continuation"
                            if anomalies_out is not None:
                                anomalies_out.append(anom)
                            rows.append(cells)
                            continue
                        if offset:
                            # Right-aligned: prev already holds a column for every
                            # continuation cell from `offset` on, so it is NOT widened.
                            # Only leading cells that no column claims become new ones,
                            # and a blank ref band — the usual case — adds none at all.
                            anom["action"] = "realigned_and_merged"
                            lead = [c for c in cells[:offset] if c["text"]]
                            if lead:
                                anom["prepended_cells"] = len(lead)
                        else:
                            anom["action"] = "widened_and_merged"
                            prev.extend({"text": "", "rowspan": 1, "colspan": 1}
                                        for _ in range(len(cells) - len(prev)))
                    else:
                        anom["action"] = "kept_as_separate_row"
                        if anomalies_out is not None:
                            anomalies_out.append(anom)
                        rows.append(cells)
                        continue
                    if anomalies_out is not None:
                        anomalies_out.append(anom)
                # Merge the aligned pairs into prev's OWN columns first, so the indices
                # here are the ones _alignment_offset scored. Any unclaimed leading cell
                # is prepended afterwards — doing it first would shift prev underneath
                # this loop and merge that cell's text a second time.
                for j in range(len(prev)):
                    k = j + offset
                    if k >= len(cells):
                        break
                    if cells[k]["text"]:
                        # `rich` follows `text` through every join, or the rendered cell
                        # would lose the half that was merged into it. Read BEFORE the
                        # text merge below: a cell with no rich of its own falls back to
                        # its text, and taking that fallback afterwards would read the
                        # already-merged string and repeat the donor ("B" -> "B B").
                        _rich = prev[j].get("rich") or prev[j]["text"]
                        prev[j]["text"] = pdf2mdtree.normspace(prev[j]["text"] + " " + cells[k]["text"])
                        prev[j]["rich"] = pdf2mdtree.normspace(
                            _rich + " " + (cells[k].get("rich") or cells[k]["text"]))
                if lead:
                    prev[:0] = lead
                continue
            rows.append(cells)
    if not rows:
        return None, 0, 0
    n_cols = max((sum(c["colspan"] for c in r) for r in rows), default=0)
    return "<table>" + "".join(_serialize_row(r) for r in rows) + "</table>", len(rows), n_cols



def middle_table_html_by_page(mineru_raw: Path, stem: str) -> dict:
    """Per-CROP-PAGE table HTML read from MinerU's `_middle.json`.

    We drive everything else off `_content_list.json`, which is the right choice —
    it is the normalised, documented shape. But it MERGES a page-spanning table into
    a single entry (mineru's make_blocks_to_content_list), so the other pages of that
    table arrive as empty `table_body` stubs and their rows are simply not in the file.

    Measured on Saudi Arabia__174731 table_007 (pages 17-26): content_list gave one
    fat block on page 21 and EMPTY bodies for pages 17, 18, 19, 20, 22, 23, 24, 25 —
    and its merged form silently omits question (a) entirely. `_middle.json` keeps the
    same table split per page, with question (a) intact on page 17. Reading it back
    recovers 9 rows and ~9.4k characters on that one region.

    Returns {crop_page_index: [html, ...]} and never raises: a missing or unreadable
    middle.json just means no supplement, which is exactly today's behaviour.
    """
    found = next(iter(mineru_raw.rglob(f"{stem}_middle.json")), None) if mineru_raw.exists() else None
    if found is None:
        return {}
    try:
        mid = json.loads(found.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[int, list[str]] = {}
    for pi, page in enumerate(mid.get("pdf_info") or []):
        htmls = []
        for blk in (page.get("preproc_blocks") or []):
            if blk.get("type") != "table":
                continue
            for sub in (blk.get("blocks") or [blk]):
                for line in (sub.get("lines") or []):
                    for span in (line.get("spans") or []):
                        if span.get("type") == "table" and (span.get("html") or "").strip():
                            htmls.append(span["html"])
        if htmls:
            out[pi] = htmls
    return out


CONT_JOIN_SHINGLES = 0.5


def _load_headings(stage1_dir):
    """Stage 1's heading list: [{level, title, page}, ...]. Used to know that a page
    straddles a section boundary before deciding what belongs to which section."""
    if not stage1_dir:
        return []
    try:
        d = json.loads((Path(stage1_dir) / "headings_manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    items = d if isinstance(d, list) else (d.get("headings") or d.get("items") or [])
    return [x for x in items if isinstance(x, dict)]


def move_continuation_rows(tables, tables_out_dir, doc, footnote_ids=frozenset(),
                           headings=()):
    """Rejoin a table whose region stopped one page before the table did.

    Luxembourg__181768: table_005 covers pages 59-77, but section 6.4(f)'s answer
    plainly continues onto page 78, where TWO regions were detected -- table_006
    (section 7.1's NEW table) and table_007. The continuation became table_007's
    opening row, so in the output 6.4(f) stops mid-sentence at "...the Third
    Country National" and its remainder reappears later as a stray blank-cell row
    heading a different table. Nothing is lost; it is detached, which reads as lost.

    A page-edge continuation guess USED to live in run_stage2 and was deleted (see
    the note on CONT_EDGE_FRAC): it inferred continuation from stub and page-edge
    GEOMETRY without checking the rows were anywhere -- 78 single-page placeholders
    labelled "continuation", median 0% of the region's text present next door. So
    geometry is never consulted here. Three conditions, the third the referee, in
    the spirit of verbatim_match:

      A the donor region's last row ends MID-CLAUSE -- a closed sentence needs no
        continuation;
      B EXACTLY ONE region on the next page opens with a BLANK-leading-cell row.
        A new section's block opens with a populated header ("Questions |
        Answers"), so 7.1's table can never be a candidate -- this is what keeps
        the new table on that page untouched. More than one candidate declines
        rather than guesses;
      C the JOIN reproduces the PDF's own continuous reading order, as 6-word
        shingles over page P's tail plus page P+1's head.

    Measured corpus-wide: A alone fires on 27 boundaries, A+B on 6, A+B+C on 5.
    A+B alone would take a wrong join in 1 of those 6.

    Runs on the STITCHED table.md, deliberately: reading raw content_list blocks
    means re-implementing the middle.json swap and the header dedup, and a first
    attempt that did so found no candidates at all because a long region's pages
    are empty stubs. Stage 3 splices table.md, so rewriting it is sufficient.

    A row is MOVED -- appended to the recipient, deleted from the donor -- so it
    exists exactly once. One hop only; a region that has already donated or
    received is not eligible again (transitive extension is MERGE_CHAIN_HOPS,
    also deleted).
    """
    def _rows(tid):
        p = tables_out_dir / tid / "table.md"
        if not p.exists():
            return None, None
        txt = p.read_text()
        m = re.search(r"<table>.*?</table>", txt, re.DOTALL)
        if not m:
            return None, None
        root = etree.HTML(m.group(0))
        if root is None:
            return None, None
        return [_tr_to_cells(tr, footnote_ids) for tr in root.iter("tr")], (txt, m)

    def _sh(text, n=SHINGLE_N):
        w = re.findall(r"[a-z0-9]+", (text or "").lower())
        return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}

    cache = {t["table_id"]: _rows(t["table_id"]) for t in tables}
    moves, busy = [], set()
    for t in tables:
        pgs = t.get("pages") or []
        rid = t["table_id"]
        if not pgs or rid in busy:
            continue
        rows, _ = cache.get(rid) or (None, None)
        if not rows:
            continue
        last, nxt = pgs[-1], pgs[-1] + 1
        if nxt in pgs:
            continue
        tail = next((c["text"] for c in reversed(rows[-1]) if c["text"]), "")
        if not _looks_truncated(tail):                                   # (A)
            continue
        cands = []
        for o in tables:
            oid = o["table_id"]
            if oid == rid or oid in busy or nxt not in (o.get("pages") or []):
                continue
            orows, _ = cache.get(oid) or (None, None)
            if not orows:
                continue
            first = orows[0]
            if len(first) > 1 and not first[0]["text"] and any(c["text"] for c in first[1:]):
                cands.append(oid)
        if len(cands) != 1:                                              # (B)
            continue
        did = cands[0]
        drows, dpack = cache[did]
        # How MANY leading rows belong to the previous section? Moving exactly one
        # was wrong: Luxembourg p78 carries the 6.4(f) continuation AND question (g)
        # before section 7 begins, so a one-row move left (g) stranded under
        # "# 7 PRIVATE PLACEMENT REGIME" -- a 6.4 question filed in section 7, which
        # no reader would find.
        #
        # The stop marker is where the NEW section's table starts, not a count:
        #   * a row carrying that page's section title, which Stage 1 already
        #     recorded (headings_manifest: page 78 | level 1 | "7 PRIVATE PLACEMENT
        #     REGIME"), or
        #   * a short all-cells-filled header row -- the "Questions | Answers" shape
        #     every section table in these memos opens with.
        # Neither found => decline. Guessing a count is how the deleted page-edge
        # heuristic went wrong; and because the marker itself is never moved, the
        # new section's own table is structurally out of reach.
        _titles = [str(h.get("title") or "") for h in (headings or [])
                   if h.get("page") == nxt]

        def _is_section_start(cells_):
            filled = [c["text"] for c in cells_ if c["text"]]
            nrm = re.sub(r"[^a-z0-9]", "", " ".join(filled).lower())
            for t in _titles:
                tn = re.sub(r"[^a-z0-9]", "", t.lower())
                if tn and (tn in nrm or nrm in tn):
                    return True
            return (len(filled) >= 2 and all(len(x) < 40 for x in filled)
                    and not any(x.rstrip()[-1:] in ".?;" for x in filled))

        stop = next((i for i, r in enumerate(drows[:8]) if _is_section_start(r)), None)
        if not stop:                     # None (no marker) or 0 (nothing precedes)
            continue
        move_rows = drows[:stop]
        head = next((c["text"] for c in move_rows[0] if c["text"]), "")
        try:
            stream = doc[last - 1].get_text() + " " + doc[nxt - 1].get_text()
        except Exception:                                                # noqa: BLE001
            continue
        sh = _sh(" ".join(tail.split()[-12:] + head.split()[:12]))
        hit = (len(sh & _sh(stream)) / len(sh)) if sh else 0.0
        if hit < CONT_JOIN_SHINGLES:                                     # (C)
            continue
        # recipient gains the row, donor loses it -> exactly one copy
        # Fuse into the truncated row rather than appending beside it: the halves
        # are one cell of one row, and the column offset is chosen by the same
        # scorer the in-region continuation merge uses, so a fragment cannot land
        # in the wrong column (the failure that put a question tail in an answer).
        rtxt, rm = cache[rid][1]
        prev = rows[-1]
        merged = [dict(c) for c in prev]
        don = move_rows[0]
        if len(don) > len(merged):
            off, _ = _alignment_offset(merged, don)
            if off == 0:
                merged += [{"text": "", "rowspan": 1, "colspan": 1}
                           for _ in range(len(don) - len(merged))]
            shift = -off          # prev[j] <- don[j+off]
        else:
            # NARROWER donated row: _alignment_offset only scores the wider case
            # and would return 0, which fuses the continuation into the FIRST
            # columns -- on Luxembourg that put the answer's continuation into the
            # QUESTION cell. The row is missing its LEADING columns (no row-ref,
            # no re-stated question on a continuation page), so score every
            # trailing-edge shift and take the best; ties favour the trailing edge,
            # which is the shape a continuation actually has.
            best, shift = None, len(merged) - len(don)
            for sh_ in range(len(merged) - len(don), -1, -1):
                sc = 0
                for _j in range(len(merged)):
                    _k = _j - sh_
                    if not (0 <= _k < len(don)):
                        continue
                    a, b = merged[_j]["text"], don[_k]["text"]
                    if not a or not b:
                        continue
                    if _looks_truncated(a):
                        sc += 4 if b[:1].islower() else 2
                    elif b[:1].islower():
                        sc -= 3
                if best is None or sc > best:
                    best, shift = sc, sh_
        for _j in range(len(merged)):
            _k = _j - shift
            if not (0 <= _k < len(don)):
                continue
            if don[_k]["text"]:
                _rich = merged[_j].get("rich") or merged[_j]["text"]   # before the merge
                merged[_j]["text"] = pdf2mdtree.normspace(
                    merged[_j]["text"] + " " + don[_k]["text"])
                merged[_j]["rich"] = pdf2mdtree.normspace(
                    _rich + " " + (don[_k].get("rich") or don[_k]["text"]))
        # first moved row FUSES into the truncated row; any further moved rows
        # follow it as their own rows, still inside the recipient's table.
        new_r = ("<table>" + "".join(_serialize_row(r)
                 for r in rows[:-1] + [merged] + move_rows[1:]) + "</table>")
        (tables_out_dir / rid / "table.md").write_text(
            rtxt[:rm.start()] + new_r + rtxt[rm.end():])
        dtxt, dm = dpack
        new_d = "<table>" + "".join(_serialize_row(r) for r in drows[stop:]) + "</table>"
        (tables_out_dir / did / "table.md").write_text(
            dtxt[:dm.start()] + new_d + dtxt[dm.end():])
        busy.update({rid, did})
        # status.json's row/col counts were stamped during stitching, before this
        # pass, so refresh them or the badge reads "96 rows" on a 95-row table.
        for _tid, _rws in ((rid, rows[:-1] + [merged] + move_rows[1:]), (did, drows[stop:])):
            _sp = tables_out_dir / _tid / "status.json"
            if _sp.exists():
                try:
                    _st = json.loads(_sp.read_text())
                    _st["rows"] = len(_rws)
                    _st["cols"] = max((sum(c["colspan"] for c in r) for r in _rws), default=0)
                    _sp.write_text(json.dumps(_st, indent=2))
                except (OSError, json.JSONDecodeError):
                    pass
        moves.append({"from": did, "to": rid, "page": nxt, "rows": len(move_rows),
                      "join_shingles": round(hit, 2),
                      "chars": sum(len(c["text"]) for r in move_rows for c in r)})
    return moves


ORPHAN_ROW_MIN_CHARS = 60      # below this a row carries no distinctive content


def _row_text(cells):
    return " ".join(c["text"] for c in cells if c["text"]).strip()


def _section_resolver(headings, doc):
    """-> section_at(page, text) -> int|None: which top-level section owns a piece
    of text, judged by the PDF's own reading order.

    A section boundary is not a page boundary, and the pages where ownership is
    CONTESTED are exactly the pages that carry one. Saudi Arabia__174731 p16 holds,
    in order: the tail of section 3's table, "4 SECTION INTENTIONALLY LEFT BLANK",
    "5 PASSIVE MARKETING (REVERSE-ENQUIRY)", and then section 5's table header. Any
    rule that reads ownership off the page number alone gets that page wrong twice.

    Level 1 is the granularity that matters because Stage 3 emits one file per
    level-1 section -- "does this end up in 04-sources-of-law or in
    06-passive-marketing" is precisely the question a misfiling gets wrong.
    """
    secs = sorted(((int(h["page"]), str(h.get("title") or ""))
                   for h in (headings or [])
                   if h.get("page") and int(h.get("level") or 1) == 1),
                  key=lambda t: t[0])
    if not secs:
        return lambda pg, text="": None
    _flat, _pos = {}, {}

    def flat(pg):
        if pg not in _flat:
            try:
                _flat[pg] = re.sub(r"\s+", " ", doc[pg - 1].get_text()).lower()
            except (IndexError, RuntimeError):
                _flat[pg] = ""
        return _flat[pg]

    def title_pos(pg, title):
        """Where on the page does this heading start? A heading's own number is not
        reliably contiguous with its text in the extracted stream ("8." vs the
        manifest's "8 Marketing Activities"), so try it with and without."""
        if (pg, title) in _pos:
            return _pos[(pg, title)]
        f, found = flat(pg), None
        for cand in (title, re.sub(r"^[\d.]+\s*", "", title)):
            cand = re.sub(r"\s+", " ", cand).strip().lower()
            if len(cand) < 6:
                continue
            i = f.find(cand)
            if i >= 0:
                found = i if found is None else min(found, i)
        _pos[(pg, title)] = found
        return found

    def section_at(pg, text=""):
        """`text` may be one string or several, tried in order until one is LOCATED
        on the page. That matters for MinerU's merged blocks: the block is anchored
        where it STARTS, so only its opening rows are actually on the anchor page,
        and probing with a later row silently finds nothing and falls back to the
        section that was already open. On Saudi Arabia p16 that read the section-5
        reverse-enquiry table as section 3 -- the exact misfiling being guarded."""
        if not pg:
            return None
        # whatever section was already open when the page began
        base = None
        for i, (p, _t) in enumerate(secs):
            if p < pg:
                base = i
        here = [(i, title_pos(pg, t)) for i, (p, t) in enumerate(secs) if p == pg]
        here = [(i, x) for i, x in here if x is not None]
        if not here:
            return base
        cands = [text] if isinstance(text, str) else list(text or ())
        for cand in cands:
            # Entities must be decoded before comparing against the PDF: MinerU writes
            # "&amp;" where the page says "&", and a raw comparison never matches.
            probe = re.sub(r"\s+", " ", html_unescape(pdf2mdtree.normspace(
                cand or ""))).lower()[:60]
            oi = flat(pg).find(probe[:40]) if len(probe) >= 8 else -1
            if oi < 0:
                continue
            prior = [i for i, x in here if x <= oi]
            return max(prior) if prior else base
        return base              # cannot locate it; assume it opened the page

    return section_at


def place_orphan_rows(orphans, tables_out_dir, footnote_ids=frozenset(),
                      section_at=None, pages_by_id=None):
    """Give an orphan's rows to the region that should own them -- and to nothing else.

    Three earlier shapes of this pass each traded one bug for another, because each
    decided ownership from a PROXY instead of from the content:

      * adding the orphan's PAGE to a region's match list triggered run_stage2's
        middle.json swap, which supplies the WHOLE page, so Switzerland__176654 p35
        was emitted in section 7 AND section 8;
      * moving to ROW level fixed that, but identified rows by bare `_row_key` with no
        locality and no substance test. On Saudi Arabia__174731 the orphan carried a
        "Questions | Answers" row, whose signature matches the opening row of EVERY
        section table in these memos, so placing it DELETED the header from seven
        unrelated tables spanning pages 14-73;
      * and ownership was still read off the anchor page. MinerU's content_list MERGES
        a page-spanning table into one entry (see middle_table_html_by_page), so the
        reverse-enquiry table spanning pages 16-26 arrived as a single 11,911-char
        block anchored on p16. The only region claiming p16 is table_006 -- section
        THREE's three-row tail -- so being the sole candidate made it confidently
        wrong, and 13 rows of section 5 were filed under section 3 while the region
        that had correctly extracted them, table_007, had 7 of its rows deleted.

    Measured over the MRAM corpus: 16 placements, of which 11 removed rows from a
    region that already held them. `removed_from` being non-empty is itself the
    evidence that nothing was lost -- a genuinely lost orphan appears nowhere else.

    So ownership is decided from content and from the document's own section
    structure, and the pass is additive unless it can show it is correcting a
    misfiling:

      1. a row is EVIDENCE only if it is substantive (>= ORPHAN_ROW_MIN_CHARS) and its
         signature is not repeated across regions -- boilerplate is excluded by
         measurement, not by a hardcoded list;
      2. the destination must be in the orphan's OWN section, both resolved by reading
         order (`_section_resolver`). Otherwise decline: a visible unrecovered orphan
         beats content silently filed under the wrong heading;
      3. if a region in the SAME section already holds this evidence, the block is a
         duplicate VIEW of content that is already correctly placed -- MinerU's merged
         form. Add nothing, remove nothing;
      4. otherwise the rows are genuinely absent from their section, so add them; and
         remove them only from a holder that is in a DIFFERENT section AND spans the
         orphan's own page -- which is a misfiling being corrected, not a dedup.

    Rule 4's two conditions are what bound the blast radius: on Saudi Arabia the only
    region that could ever have been subtracted from is table_006, never table_009 on
    page 40. Every decision is recorded with its reason, and rows_in/rows_out are
    reported so a pass that loses content cannot look like one that did not.
    """
    section_at = section_at or (lambda pg, text="": None)
    pages_by_id = pages_by_id or {}

    def _read(tid):
        path = tables_out_dir / tid / "table.md"
        if not path.exists():
            return None
        txt = path.read_text()
        m = re.search(r"<table>.*?</table>", txt, re.DOTALL)
        if not m:
            return None
        root = etree.HTML(m.group(0))
        if root is None:
            return None
        return {"path": path, "txt": txt, "m": m,
                "rows": [_tr_to_cells(tr, footnote_ids) for tr in root.iter("tr")]}

    def _write(st):
        body = "<table>" + "".join(_serialize_row(r) for r in st["rows"]) + "</table>"
        st["path"].write_text(st["txt"][:st["m"].start()] + body + st["txt"][st["m"].end():])
        sp = st["path"].parent / "status.json"
        if sp.exists():
            try:
                js = json.loads(sp.read_text())
                js["rows"] = len(st["rows"])
                js["cols"] = max((sum(c["colspan"] for c in r) for r in st["rows"]),
                                 default=0)
                sp.write_text(json.dumps(js, indent=2))
            except (OSError, json.JSONDecodeError):
                pass

    states = {}
    for d in sorted(p for p in tables_out_dir.iterdir() if p.is_dir()):
        st = _read(d.name)
        if st is not None:
            states[d.name] = st
    rows_in = sum(len(st["rows"]) for st in states.values())

    # Which signatures are boilerplate? Repetition across regions is the test.
    holders_of = defaultdict(set)
    for tid, st in states.items():
        for r in st["rows"]:
            holders_of[_row_key(r)].add(tid)

    def _evidence(cells):
        return (len(_row_text(cells)) >= ORPHAN_ROW_MIN_CHARS
                and len(holders_of.get(_row_key(cells), ())) < 2)

    _sec = {}

    def section_of(tid):
        """Which section is this REGION in? Not the same question as where its first
        row reads. Switzerland__176654 table_008 spans pages 35-53 of section 8 but
        OPENS with section 7.5's tail, and judging the region by that tail makes it
        look like a section-7 region -- so the very misfiling it is holding could
        never be corrected. A multi-page region is therefore identified by its BODY,
        and page two onward is unambiguous: whatever section was open when the region
        continued. A single-page region has no body to appeal to, so it is judged by
        its own first row, which is what distinguishes Saudi Arabia's table_006
        (section 3's three-row tail on p16) from the section-5 table beneath it."""
        if tid not in _sec:
            pgs = pages_by_id.get(tid) or []
            st = states.get(tid)
            if len(pgs) > 1:
                _sec[tid] = section_at(pgs[1], "")
            elif pgs:
                head = [_row_text(r) for r in (st["rows"][:6] if st else ())]
                _sec[tid] = section_at(pgs[0], head)
            else:
                _sec[tid] = None
        return _sec[tid]

    out = []
    for i, orph in enumerate(orphans):
        pg, blk = orph.get("page"), orph.get("block") or {}
        body = (blk.get("table_body") or "").strip()
        root = etree.HTML(body) if body else None
        rec = {"index": i, "page": pg, "chars": len(body),
               "candidates": list(orph.get("candidates") or ()),
               "preview": pdf2mdtree.normspace(re.sub(r"<[^>]+>", " ", body))[:160]}
        if root is None:
            rec.update(placed=False, reason="unreadable table body")
            out.append(rec)
            continue
        new_rows = [c for c in (_tr_to_cells(tr, footnote_ids) for tr in root.iter("tr"))
                    if c and any(x["text"] for x in c)]
        rec["rows"] = len(new_rows)
        ev = [r for r in new_rows if _evidence(r)]
        if not ev:
            rec.update(placed=False,
                       reason="no substantive rows to place (boilerplate only)")
            out.append(rec)
            continue
        osec = section_at(pg, [_row_text(r) for r in new_rows[:6]])
        rec["orphan_section"] = osec
        # Exactly one candidate in this content's OWN section is an answer; none is a
        # decline and two is a real choice this pass has no evidence to make. Counting
        # regions on the page instead of sections is what made a sole candidate
        # confidently wrong on Saudi Arabia p16.
        fits = [c for c in (orph.get("candidates") or ())
                if c in states and section_of(c) == osec]
        rec["section_matched"] = fits
        if not fits:
            rec.update(placed=False,
                       reason=f"no candidate region is in section {osec}")
            out.append(rec)
            continue
        # Candidates arrive in ADJACENCY order -- on this page, then ending on the
        # previous one, then starting on the next -- so within the right section the
        # first is the nearest home. Declining a tie instead loses the recoveries that
        # were real: on Saudi Arabia three regions sit in section 7 around page 50, and
        # only one of them is on page 50.
        tid = fits[0]
        st = states[tid]
        rec["table_id"] = tid
        held = defaultdict(int)
        for r in ev:
            for h in holders_of.get(_row_key(r), ()):
                if h != tid:
                    held[h] += 1
        same = sorted(h for h in held if section_of(h) == osec)
        if same:
            rec.update(placed=False, duplicate_of=same,
                       reason="already emitted in the same section by " + ", ".join(same))
            out.append(rec)
            continue
        have = {_row_key(r) for r in st["rows"]}
        add = [r for r in new_rows if _row_key(r) not in have]
        st["rows"] = st["rows"] + add
        _write(st)
        for r in add:
            holders_of[_row_key(r)].add(tid)
        # Subtract only where this is demonstrably a MISFILING being corrected:
        # a different section, holding this evidence, on the orphan's own page.
        removed_from, also_held_by = [], []
        keys = {_row_key(r) for r in ev}
        for h in sorted(held):
            if section_of(h) == osec:
                continue
            if pg not in (pages_by_id.get(h) or ()):
                # A different section holds this evidence but does NOT span the page,
                # so there is no reading-order argument that it swept the rows up as a
                # page neighbour. Which copy is right is not decidable here, and
                # deleting the wrong one is unrecoverable, so BOTH stay and the
                # collision is reported for a human to look at.
                also_held_by.append({"table_id": h, "rows": held[h],
                                     "section": section_of(h)})
                continue
            ost = states.get(h)
            keep = [r for r in ost["rows"] if _row_key(r) not in keys]
            if len(keep) != len(ost["rows"]):
                removed_from.append({"table_id": h,
                                     "rows": len(ost["rows"]) - len(keep),
                                     "section": section_of(h)})
                ost["rows"] = keep
                _write(ost)
        rec.update(placed=True, rows_added=len(add), removed_from=removed_from,
                   reason="absent from its own section")
        if also_held_by:
            rec["also_held_by"] = also_held_by
        out.append(rec)
    rows_out = sum(len(st["rows"]) for st in states.values())
    return {"placements": out, "rows_before": rows_in, "rows_after": rows_out,
            "rows_added": max(0, rows_out - rows_in),
            "rows_removed": max(0, rows_in - rows_out)}


def run_stage2(pdf_path, manifest, stage2_dir, mineru_backend, mineru_effort,
               stage1_dir=None):
    job = Path(stage2_dir).parent
    tables = manifest.get("tables", [])
    # Ground truth for clean_mineru_text's glued-digit recovery (see there) —
    # footnote numbers Stage 1 already confirmed a BODY for, independent of
    # anything MinerU does. Absent on a manifest written before this field
    # existed, in which case that recovery path is simply a no-op.
    footnote_ids = frozenset(int(n) for n in (manifest.get("footnote_ids") or []))
    # Clean slate for everything EXCEPT mineru_raw/. Stale per-table chunks under
    # tables/ would otherwise survive a re-run and be spliced into the new tree by
    # Stage 3 under ids the fresh manifest may no longer use. mineru_raw/ is
    # deliberately spared: it IS the cache, and its validity is decided by the
    # cache key below, not by age.
    if stage2_dir.exists():
        for child in stage2_dir.iterdir():
            if child.name == "mineru_raw":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    stage2_dir.mkdir(parents=True, exist_ok=True)
    tables_out_dir = stage2_dir / "tables"
    tables_out_dir.mkdir(parents=True, exist_ok=True)
    report = {"tables_attempted": len(tables), "tables_ok": 0, "tables_failed": 0,
              "tables_continuation": 0, "tables_reclassified": 0, "tables_absorbed": 0,
              "tables_stage1_duplicate": 0,
              # Every stitching decision that was not a plain same-width join, so a
              # width mismatch is inspectable instead of invisible. The invariant this
              # serves: no extracted text disappears without a record.
              "stitch_anomalies": [],
              "tables": []}

    if not tables:
        (stage2_dir / "stage2_report.json").write_text(json.dumps(report, indent=2))
        write_progress(job, "stage2_mineru", "done", regions=0)
        return report

    pages_union = sorted({pg for t in tables for pg in t["pages"]})
    # Stamped BEFORE the model runs, carrying the size of the job — this is the only
    # number a watcher gets for the next ~20 minutes.
    write_progress(job, "stage2_mineru", "running",
                   regions=len(tables), pages=len(pages_union),
                   backend=str(mineru_backend or settings.mineru_backend),
                   effort=str(mineru_effort or settings.mineru_effort))
    combined_pdf = stage2_dir / "combined_tables.pdf"
    offset_map = build_combined_pdf(pdf_path, pages_union, combined_pdf)
    (stage2_dir / "page_offset_map.json").write_text(
        json.dumps({str(k): v for k, v in offset_map.items()}, indent=2))

    mineru_error = None
    blocks = []
    # Reuse a prior MinerU run only when it was produced from the SAME input under
    # the SAME model settings: re-stitching is cheap, the GPU pass is not. The key
    # covers the combined PDF's bytes (which change whenever the detected table
    # pages change) plus backend and effort — previously the probe was a bare glob,
    # so switching backend in the UI silently replayed the old model's output.
    # Keyed on the INPUTS that determine the combined PDF, not on its bytes:
    # PyMuPDF stamps a fresh document ID on every save, so hashing the output
    # would never match twice and the cache could never hit at all.
    mineru_raw = stage2_dir / "mineru_raw"
    cache_key = hashlib.sha1(b"|".join([
        hashlib.sha1(Path(pdf_path).read_bytes()).digest(),
        ",".join(str(p) for p in pages_union).encode(),
        str(mineru_backend or settings.mineru_backend).encode(),
        str(mineru_effort or settings.mineru_effort).encode(),
        # How build_combined_pdf LAYS OUT those pages is an input too: the
        # blank separators added for non-contiguous runs change what MinerU
        # sees and therefore what it merges. Without this, cached output from
        # the old contiguous layout would replay forever and the fix would
        # never take effect. Bump whenever build_combined_pdf's layout changes.
        b"layout2-blank-separators",
    ])).hexdigest()
    key_file = mineru_raw / ".cache_key"
    cached = None
    if key_file.exists() and key_file.read_text().strip() == cache_key:
        cached = next(iter(mineru_raw.rglob(f"{combined_pdf.stem}_content_list.json")), None)
    if cached is None and mineru_raw.exists():
        shutil.rmtree(mineru_raw)   # stale or unkeyed — never replay it
    try:
        if cached is not None:
            blocks = json.loads(cached.read_text())
        else:
            parse_dir = run_mineru_to_dir(
                str(combined_pdf), out_dir=mineru_raw,
                backend=mineru_backend, effort=mineru_effort,
            )
            content_list_path = parse_dir / f"{combined_pdf.stem}_content_list.json"
            blocks = json.loads(content_list_path.read_text())
            key_file.write_text(cache_key)   # only after a run that produced output
    except subprocess.CalledProcessError as e:
        mineru_error = f"mineru exited {e.returncode}: {(e.stderr or '')[-2000:]}"
    except (FileNotFoundError, RuntimeError) as e:
        mineru_error = str(e)

    def _y0(bbox):
        return bbox[1] if bbox else 0.0

    # Page dimensions (PDF points) for every table page, straight from the
    # ORIGINAL pdf — build_combined_pdf inserts pages unmodified, so these
    # dimensions are identical to the ones pdf2mdtree/PyMuPDF used when it
    # recorded each table's bbox. Needed to convert MinerU's 0-1000-normalized
    # bbox back into the same point space (see _mineru_bbox_to_pt).
    _orig_doc = fitz.open(str(pdf_path))
    page_dims = {pg: (_orig_doc[pg - 1].rect.width, _orig_doc[pg - 1].rect.height)
                 for pg in pages_union}

    def region_tokens(pg, bbox):
        """Words the SOURCE PDF has inside this region, in reading order — the
        thing a placeholder is a stand-in for. Used to answer 'is this content in
        the output?' when geometry can't answer 'where is this table?'."""
        if not bbox or pg not in page_dims:
            return []
        clip = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
        txt = _orig_doc[pg - 1].get_text("text", clip=clip)
        return re.findall(r"[a-z0-9]+", txt.lower())

    def _shingles(words):
        """Overlapping SHINGLE_N-word sequences — the unit of verbatim match."""
        return {tuple(words[i:i + SHINGLE_N])
                for i in range(max(0, len(words) - SHINGLE_N + 1))}

    _block_sh_cache = {}

    def block_shingles(b):
        """Works for both kinds of MinerU block: a table carries its content in
        `table_body` (HTML), a text/footnote block in `text`."""
        key = id(b)
        if key not in _block_sh_cache:
            raw = b.get("table_body") or b.get("text") or ""
            txt = _TAG_RE.sub(" ", raw)
            _block_sh_cache[key] = _shingles(re.findall(r"[a-z0-9]+", txt.lower()))
        return _block_sh_cache[key]

    def region_shingles(t, pages):
        sh = set()
        for pg in pages:
            sh |= _shingles(region_tokens(pg, bbox_for(t, pg)))
        return sh

    def verbatim_match(t, pages, by_page=None, reach_pages=1):
        """-> (block, recall, page) for the block that best accounts for this
        placeholder's own region text, or None when nothing does.

        `by_page` selects the haystack: MinerU's TABLE blocks (default) answers
        "are these rows rendered as a table somewhere?"; passing
        nontable_by_page answers "did this region survive as prose?". One
        referee, two questions — so a region is only ever declared safe on
        evidence of the same kind and strength.

        Searched over the table's own pages plus `reach_pages` either side,
        because a merged page-spanning table parks its rows on a neighbour.
        Deliberately considers blocks another placeholder already claimed: that
        is precisely the "already rendered above" case, and the caller
        distinguishes the two by checking whether the winner is unclaimed."""
        want = region_shingles(t, pages)
        if len(want) < MIN_REGION_SHINGLES:
            return None
        src = blocks_by_page if by_page is None else by_page
        reach = {p + d for p in pages for d in range(-reach_pages, reach_pages + 1)}
        best = None
        for pg in sorted(reach):
            for b in src.get(pg, []):
                got = block_shingles(b)
                if not got:
                    continue
                rec = len(want & got) / len(want)
                if best is None or rec > best[1]:
                    best = (b, rec, pg)
        return best if (best and best[1] >= REGION_VERBATIM) else None

    def _best_verbatim(t, pages):
        """Best verbatim recall found anywhere in reach, IGNORING the threshold —
        reported on a failure so the number behind the verdict is visible."""
        want = region_shingles(t, pages)
        if len(want) < MIN_REGION_SHINGLES:
            return None
        reach = {p + d for p in pages for d in (-1, 0, 1)}
        best = 0.0
        for src in (blocks_by_page, nontable_by_page):
            for pg in reach:
                for b in src.get(pg, []):
                    got = block_shingles(b)
                    if got:
                        best = max(best, len(want & got) / len(want))
        return best

    # Per-page table HTML, for the pages content_list.json left as empty stubs.
    middle_html = middle_table_html_by_page(mineru_raw, combined_pdf.stem)

    # Put the spacing MinerU's VLM dropped back into every cell, from the crop PDF's
    # own text layer (realign_table_spacing). Done HERE, before anything reads a cell:
    # both sources of table HTML converge on this point -- middle.json per crop page,
    # and content_list's `table_body` per block -- and both are keyed by the crop page
    # MinerU actually saw, so no page arithmetic is needed. Measured over the corpus:
    # 3,513 of 3,666 visibly glued words repaired across 122 documents.
    crop_text: dict[int, str] = {}
    try:
        with fitz.open(str(combined_pdf)) as _crop:
            crop_text = {i: _crop[i].get_text() for i in range(len(_crop))}
    except (RuntimeError, OSError, ValueError):
        crop_text = {}          # no text layer to align against -> leave MinerU's text
    if crop_text:
        for _pg, _htmls in middle_html.items():
            if crop_text.get(_pg):
                middle_html[_pg] = [realign_table_spacing(x, crop_text[_pg]) for x in _htmls]
        for b in blocks:
            if b.get("type") != "table" or not b.get("table_body"):
                continue
            # Nearest-first, both ways: content_list MERGES a page-spanning table into
            # ONE block whose page_idx is wherever MinerU anchored it -- often in the
            # MIDDLE of the run (Saudi 174731 table_007 spans pages 17-26 and anchors
            # on 21) -- so the cells of that block live on pages either side of it.
            # Each cell stops at the first page that covers it, so this costs nothing
            # for a block that really is on its own page.
            _anchor = b.get("page_idx")
            if _anchor is None or _anchor not in crop_text:
                continue
            # Candidates widen around the anchor: the page itself, then the page with
            # its neighbours, and so on. CONTIGUOUS and in page order, because that is
            # the order the document reads in -- a cell whose text straddles a page
            # break is contiguous only once the two pages are joined. A cell stops at
            # the first candidate that wholly accounts for it, so the wider ones are
            # built only for the cells that actually need them.
            _window = []
            for _d in _REALIGN_WINDOW_STEPS:
                _joined = "\n".join(crop_text[_p]
                                    for _p in range(_anchor - _d, _anchor + _d + 1)
                                    if _p in crop_text)
                if _joined and (not _window or _joined != _window[-1]):
                    _window.append(_joined)
            if _window:
                b["table_body"] = realign_table_spacing(b["table_body"], _window)

    crop_of_page = {v: k for k, v in offset_map.items()}

    blocks_by_page = defaultdict(list)
    for b in blocks:
        if b.get("type") != "table":
            continue
        orig_pg = offset_map.get(b.get("page_idx"))
        if orig_pg is None:
            continue
        w, h = page_dims.get(orig_pg, (0, 0))
        b["bbox_pt"] = _mineru_bbox_to_pt(b.get("bbox"), w, h)
        blocks_by_page[orig_pg].append(b)
    for pg in blocks_by_page:
        blocks_by_page[pg].sort(key=lambda b: _y0(b.get("bbox_pt") or b.get("bbox")))

    # MinerU's NON-table blocks (text / footnotes / titles) per source page — used
    # to reclassify a false-positive "table" (region is really prose) back to prose,
    # and by recover_adjacent to pull in the lines just above/below a table.
    #
    # These get bbox_pt too. They previously did NOT, which made recover_adjacent
    # compare MinerU's raw 0-1000 normalized y against a manifest bbox in PDF
    # POINTS. The error scales with y, so on a table low on the page the "after"
    # window landed INSIDE the table's own rows — australia-share/table_003
    # (bbox bottom 764pt of an 842pt page) recovered "(Rail: No industry-specific
    # restrictions..." , a row from inside the table, as text below it.
    nontable_by_page = defaultdict(list)
    for b in blocks:
        if b.get("type") == "table":
            continue
        op = offset_map.get(b.get("page_idx"))
        if op is not None:
            w, h = page_dims.get(op, (0, 0))
            b["bbox_pt"] = _mineru_bbox_to_pt(b.get("bbox"), w, h)
            nontable_by_page[op].append(b)

    def bbox_for(t, pg):
        """This table's footprint ON THIS PAGE, in PDF points.

        A single-page table carries one `bbox`. A multi-page comparison table
        carries `bboxes` — one full-width row band per page, from
        reconstruct_multipage_tables' `yr` — because one box cannot describe a
        region spanning 19 pages. Returns None when the table has no geometry at
        all (the unanchored snapshot fallback), which is what selects the
        rank-based fallback below."""
        per_page = t.get("bboxes") or {}
        return per_page.get(str(pg)) or per_page.get(pg) or t.get("bbox")

    _PROSE_ROW_COLS = 12          # wide enough to span any table this corpus produces

    def _inject_page_prose(pg, page_html, t):
        """Fold MinerU's TEXT blocks that sit inside this table's footprint on `pg`
        into that page's rows, each as a full-width row at its own y position.

        Returns page_html unchanged when there is nothing inside the footprint, so a
        table with no stray prose is bit-identical to before."""
        box = bbox_for(t, pg)
        if not box:
            return page_html
        y_top, y_bot = box[1], box[3]
        rows_before, rows_after = [], []
        tb = next((b for _p, b in matched if _p == pg and b is not None), None)
        t_y = (tb.get("bbox_pt") or [0, 0, 0, 0])[1] if tb else float("inf")
        for b in nontable_by_page.get(pg, []):
            if b.get("type") not in ("text", "list"):
                continue
            txt = pdf2mdtree.normspace(_TAG_RE.sub(" ", str(b.get("text", "") or "")))
            if not txt:
                continue
            bb = b.get("bbox_pt") or b.get("bbox") or [0, 0, 0, 0]
            if not (y_top - 2 <= bb[1] <= y_bot + 2):
                continue          # outside the table's own band — not its content
            cell = (f'<tr><td colspan="{_PROSE_ROW_COLS}">'
                    f'{html_escape(txt)}</td></tr>')
            (rows_before if bb[1] < t_y else rows_after).append((bb[1], cell))
        if not rows_before and not rows_after:
            return page_html
        pre = "".join(c for _y, c in sorted(rows_before))
        post = "".join(c for _y, c in sorted(rows_after))
        m = re.search(r"<table[^>]*>", page_html)
        if m:                     # insert inside the existing table element
            i = m.end()
            j = page_html.rfind("</table>")
            return page_html[:i] + pre + page_html[i:j] + post + page_html[j:]
        return f"<table>{pre}{post}</table>" + page_html

    # Tables sharing one source page, top-to-bottom — kept for the rank-based
    # fallback used only when a table has no geometry at all (the unanchored
    # snapshot fallback — see pdf2mdtree's deferred_tables.append call sites).
    tables_by_page = defaultdict(list)
    for t in tables:
        for pg in t["pages"]:
            tables_by_page[pg].append(t)
    for pg in tables_by_page:
        tables_by_page[pg].sort(key=lambda t: _y0(bbox_for(t, pg)))

    def blocks_for_page_rank(table_id, pg):
        """Rank-based pairing (Table Nth-on-page <-> MinerU block Nth-on-page).
        Fragile when the two tools disagree on how many tables are on a page —
        kept only as a fallback for tables with no bbox to match geometrically."""
        siblings = tables_by_page.get(pg, [])
        page_blocks = blocks_by_page.get(pg, [])
        if len(siblings) <= 1:
            return page_blocks
        idx = next((i for i, t in enumerate(siblings) if t["table_id"] == table_id), 0)
        if idx >= len(page_blocks):
            return []
        if idx == len(siblings) - 1:
            return page_blocks[idx:]  # last (bottommost) sibling absorbs any trailing extras
        return [page_blocks[idx]]

    # ---- Rule B: geometric (IoU) matching for every table WITH a real bbox ----
    # Precomputed per-page (not per-table) so a MinerU block claimed as the best
    # match by more than one placeholder on the same page can be detected as a
    # likely MERGE (MinerU fused two visually-distinct tables into one block) —
    # that visibility doesn't exist if each table is matched in isolation.
    # Keyed by (table_id, PAGE), not table_id alone. A multi-page table is matched
    # independently on each of its pages — one entry per page — so its result on
    # page 12 can no longer overwrite its result on page 11.
    geo_match = {}  # (table_id, page) -> {"blocks", "iou", "status", "note"}
    geometric_ids = {t["table_id"] for t in tables
                     if any(bbox_for(t, pg) for pg in t["pages"])}
    for pg, siblings in tables_by_page.items():
        real = [t for t in siblings if bbox_for(t, pg)]
        if not real:
            continue
        page_blocks = blocks_by_page.get(pg, [])
        cands_by_table = {}
        for t in real:
            tb = bbox_for(t, pg)
            cands = sorted(
                ((_iou(tb, b["bbox_pt"]), b) for b in page_blocks if b.get("bbox_pt")),
                key=lambda c: -c[0])
            cands_by_table[t["table_id"]] = cands
        claim_count = defaultdict(int)
        for t in real:
            cands = cands_by_table[t["table_id"]]
            if cands and cands[0][0] >= CONSIDER_IOU:
                claim_count[id(cands[0][1])] += 1
        for t in real:
            cands = cands_by_table[t["table_id"]]
            if not cands or cands[0][0] <= 0:
                geo_match[(t["table_id"], pg)] = {"blocks": [], "iou": 0.0,
                                                  "status": "missing", "note": None}
                continue
            best_iou, best_block = cands[0]
            split_blocks = [b for iou, b in cands if iou >= CONSIDER_IOU]
            merged = claim_count.get(id(best_block), 0) > 1
            if merged:
                status, note = "uncertain", (
                    "this MinerU block also best-matches another table placeholder on "
                    "the same page — MinerU may have merged two tables into one; verify")
            elif best_iou > CONFIDENT_IOU:
                status = "confident"
                note = ("MinerU split this table across multiple blocks — concatenated"
                        if len(split_blocks) > 1 else None)
            else:
                status, note = "uncertain", f"low geometric overlap (IoU {best_iou:.0%}) — verify this is the right table"
            geo_match[(t["table_id"], pg)] = {"blocks": split_blocks or [best_block],
                                              "iou": round(best_iou, 3), "status": status, "note": note}

    def geo_summary(t):
        """Collapse a table's per-page match results into the one status/IoU pair
        the report and the badge show. Worst status wins (missing > uncertain >
        confident) and the IoU is the weakest page's — a 19-page table that
        matched cleanly on 18 pages and missed on one must not read as confident."""
        entries = [geo_match[(t["table_id"], pg)] for pg in t["pages"]
                   if (t["table_id"], pg) in geo_match]
        if not entries:
            return None
        blocks = [b for e in entries for b in e["blocks"]]
        n_missing = sum(1 for e in entries if e["status"] == "missing")
        matched = [e for e in entries if e["status"] != "missing"]
        note = None
        if not matched:
            # nothing overlapped on ANY page — the table really is unmatched
            status, iou = "missing", 0.0
        elif n_missing:
            # partial coverage. NOT "missing": rows were found on the other pages
            # and are in `blocks`. Reporting the whole table as missing here would
            # contradict its own extracted row count.
            status = "uncertain"
            iou = min(e["iou"] for e in matched)
            note = (f"no MinerU block overlapped on {n_missing} of {len(entries)} "
                    f"pages — verify nothing was dropped at those page breaks")
        else:
            rank = {"uncertain": 0, "confident": 1}
            worst = min(matched, key=lambda e: (rank.get(e["status"], 0), e["iou"]))
            status, iou, note = worst["status"], worst["iou"], worst["note"]
        if len(entries) > 1:
            ious = [e["iou"] for e in entries]
            spread = f"matched per page across {len(entries)} pages (IoU {min(ious):.0%}–{max(ious):.0%})"
            note = f"{note}; {spread}" if note else spread
        return {"blocks": blocks, "iou": iou, "status": status, "note": note}

    def blocks_for_page(table_id, pg):
        """Blocks for one table on one page: geometric IoU match when the table
        has a real bbox, else the rank-based fallback."""
        if table_id in geometric_ids:
            gm = geo_match.get((table_id, pg))
            return list(gm["blocks"]) if gm else []
        return blocks_for_page_rank(table_id, pg)

    # ---- cross-page merge recovery ----------------------------------------
    # When a table spans pages, MinerU frequently merges it into ONE logical
    # block: the full HTML is attached to a single page-region (its anchor) and
    # EMPTY stub blocks are emitted for the table's other page-regions. Because
    # our matching is strictly per-page, that produced two wrong outcomes at
    # once: the placeholder sitting over a stub reported "empty table_body" as a
    # FAILURE, while the block actually holding the content was claimed by
    # nobody and its rows were dropped from the output entirely — a real,
    # silent content loss behind a misleading failure marker.
    #
    # So: a placeholder whose matched blocks are ALL empty may adopt an
    # unclaimed content-bearing block on an ADJACENT page. This only ever fires
    # where the pipeline would otherwise have failed outright, so it cannot
    # change the result for a table that already extracts correctly, and it
    # refuses to guess when more than one candidate is in reach.
    def _has_rows(b):
        return bool((b.get("table_body") or "").strip())

    # Both matching paths must be accounted for. Blocks consumed by a
    # rank-fallback table (one with no geometry at all — the unanchored snapshot
    # fallback) are NOT orphans: counting them as such would both over-report
    # loss and risk adopting a block another placeholder is already going to
    # emit, duplicating it.
    _geo_claimed = {id(b) for gm in geo_match.values() for b in gm["blocks"]}
    _rank_claimed = {id(b) for t in tables if t["table_id"] not in geometric_ids
                     for pg in t["pages"] for b in blocks_for_page_rank(t["table_id"], pg)}
    orphan_blocks = [b for pg in sorted(blocks_by_page) for b in blocks_by_page[pg]
                     if _has_rows(b) and id(b) not in _geo_claimed
                     and id(b) not in _rank_claimed]

    # ---- orphan recovery -----------------------------------------------------
    # An orphan is a table MinerU extracted CORRECTLY that no placeholder claimed,
    # so its rows never reach the tree. Recording it (below) made the loss visible;
    # it did not stop it. Measured across the runs on disk: 30 documents carry an
    # orphan of >=200 chars, 61k characters in total, and section 7.5 of the MRAM
    # memo is orphaned on Bahrain (3.5k), South Africa (3.2k), Switzerland (2.8k),
    # Malta (2.4k) and Mexico (1.2k) — the same full-page question table each time.
    #
    # Malta p85 is the shape: Stage 1 detected only the 52pt tail of the PREVIOUS
    # section's matrix (y 79.5-131.7, matched at IoU 0.944), so the 2,395-char table
    # filling the rest of the page had no region to attach to.
    #
    # Recovery is deliberately narrow: a page claimed by EXACTLY ONE region is not a
    # guess, it is the only answer. Where two regions share the page there is a real
    # choice to make, so nothing is adopted and the block stays an orphan for the
    # gate to flag — the failure mode being avoided is adopting a block some other
    # placeholder is also going to emit, which duplicates instead of recovering.
    _regions_on_page = defaultdict(list)
    for _t in tables:
        for _pg in _t["pages"]:
            _regions_on_page[_pg].append(_t["table_id"])
    # A page that STRADDLES a section boundary breaks "one claiming region is the
    # answer". Switzerland__176654 p35 carries the tail of section 7.5 AND the start
    # of "8 Marketing Activities"; the only region claiming p35 is table_008 (35-53),
    # which is section EIGHT's table, so adopting there filed 2,842 characters of
    # section 7 under "# 8 Marketing Activities". Being the sole candidate made it
    # confidently wrong, not right.
    #
    # So on a boundary page, ownership is decided by READING ORDER, which the PDF can
    # answer: text appearing BEFORE the new section's heading belongs to the section
    # that is ending, and its home is the region that ends on the previous page.
    # Everything after the heading keeps the normal rule. When neither the order nor
    # a single previous-page region resolves it, DECLINE -- a visible unrecovered
    # orphan beats content silently filed under the wrong heading.
    _headings = manifest.get("headings") or _load_headings(stage1_dir)
    _section_at = _section_resolver(_headings, _orig_doc)
    _ends_on, _starts_on = defaultdict(list), defaultdict(list)
    for _t in tables:
        if _t.get("pages"):
            _ends_on[_t["pages"][-1]].append(_t["table_id"])
            _starts_on[_t["pages"][0]].append(_t["table_id"])

    # Three ways a section's table can be adjacent to a block that has no region of
    # its own: it is ON this page, it ENDED on the previous page (the block is the
    # tail of a section closing here), or it STARTS on the next page (the block is
    # the head of a section opening here). Switzerland__176654 p35 needs the second
    # and Saudi Arabia__174731 p16 the third; neither is reachable from "the single
    # region claiming this page", which is exactly how both were filed under the
    # wrong heading while looking like confident recoveries.
    #
    # So this only PROPOSES, in adjacency order. place_orphan_rows chooses, because
    # only it can see the stitched rows -- and therefore each region's real section
    # and whether the content is already emitted somewhere correct.
    orphan_candidates, _still_orphan = [], []
    for b in orphan_blocks:
        _pg = offset_map.get(b.get("page_idx"))
        _cands = list(dict.fromkeys((_regions_on_page.get(_pg) or [])
                                    + (_ends_on.get(_pg - 1) or [])
                                    + (_starts_on.get(_pg + 1) or []))) if _pg else []
        if _cands:
            orphan_candidates.append({"page": _pg, "block": b, "candidates": _cands})
        else:
            _still_orphan.append(b)
    orphan_blocks = _still_orphan

    # adopt_orphan() lived here: it took ANY single unclaimed block within +/-1
    # page, with no geometry, no shape and no content check. Superseded by
    # verbatim_match(), which only ever recovers a block that demonstrably holds
    # this region's own text. See the note on REGION_VERBATIM for the measurement
    # that motivated it (the block it used to steal scored 0%).
    _content_pages = {pg for pg in blocks_by_page
                      for b in blocks_by_page[pg] if _has_rows(b)}

    # emitted_tokens()/content_coverage() lived here. content_coverage answered
    # "did this region's content survive?" as a BAG OF WORDS against a haystack
    # pooling +/-1 page of MinerU output PLUS whole Stage-1 sections. Its own
    # docstring argued word-order cannot survive a table's restructuring — true
    # for a table rendered row-major, but it made the test far too permissive:
    # measured across the corpus it returned ~100% for EVERY outcome, including
    # tables whose content was genuinely gone, so "absorbed — not lost" was
    # being printed over real losses. verbatim_match() replaces it: contiguous
    # 6-word sequences, scored separately against MinerU's table blocks and its
    # text blocks, which separates merged-elsewhere (96%) from lost (0%).
    def _page_h(pg):
        return page_dims.get(pg, (0.0, 0.0))[1] or 0.0

    # is_continuation()/stub_chain_anchor()/_runs_to_bottom()/_starts_at_top()
    # lived here. They inferred "this region was merged into a neighbouring
    # table, nothing is missing" from stub and page-edge GEOMETRY, without ever
    # checking that the rows were anywhere. Two ways that went wrong, both
    # measured on this corpus:
    #   * stub_chain_anchor's first hop inspected the placeholder's OWN page, so
    #     any OTHER table extracting successfully on that page counted as proof —
    #     78 single-page placeholders were labelled "continuation", which is
    #     impossible for a table that spans one page.
    #   * across the 27 continuations sampled, the median share of the region's
    #     text actually present in a neighbouring block was 0%.
    # So the reassurance was mostly false. verbatim_match() answers the same
    # question with evidence, and the marker now quotes the number.
    #
    # A heading or short sub-point squeezed right above/below a wide table can
    # get swallowed into pdf2mdtree's table-exclusion zone (Stage 1) even
    # though it isn't part of the table at all — MinerU, reading the whole
    # page rather than cell geometry, still sees it as ordinary text. Recover
    # it here and splice it back in Stage 3 immediately around the table's own
    # placeholder, so it lands exactly where it belongs instead of vanishing.
    # Margin-bounded (not "the rest of the page"), and each MinerU block is
    # claimed at most once so two tables sharing a page never both grab the
    # same recovered paragraph.
    ADJACENT_MARGIN = 100
    claimed_block_ids = set()

    def _page_y(b):
        """(source page, y in PDF points) — the sort key for anything spanning
        more than one page. Sorting on y alone put page 2's blocks before page 1's."""
        return (offset_map.get(b.get("page_idx"), 0), _y0(b.get("bbox_pt") or b.get("bbox")))

    def recover_adjacent(pages, bbox):
        if not bbox:
            return "", ""
        before_blocks, after_blocks = [], []
        for pg in pages:
            for b in nontable_by_page.get(pg, []):
                if id(b) in claimed_block_ids:
                    continue
                # bbox_pt, NOT the raw bbox: `bbox` here is the manifest's, in PDF
                # points, and MinerU's raw bbox is 0-1000 normalized. Comparing the
                # two put the window in the wrong place by a factor of page_height/1000.
                pt = b.get("bbox_pt")
                if pt is None:
                    continue
                by = _y0(pt)
                if bbox[1] - ADJACENT_MARGIN <= by < bbox[1]:
                    before_blocks.append(b)
                    claimed_block_ids.add(id(b))
                elif bbox[3] < by <= bbox[3] + ADJACENT_MARGIN:
                    after_blocks.append(b)
                    claimed_block_ids.add(id(b))
        before_blocks.sort(key=_page_y)
        after_blocks.sort(key=_page_y)
        return (blocks_to_prose(before_blocks, footnote_ids) if before_blocks else "",
                blocks_to_prose(after_blocks, footnote_ids) if after_blocks else "")

    def recover_footnotes(pages):
        """Footnote DEFINITIONS on a table's pages, claimed once each.

        A footnote definition is page-bottom content that can never be part of a
        table, and the table's own `[^n]` references are orphaned without it.
        Two reasons recover_adjacent above doesn't catch them: its ±100pt window
        usually can't reach the bottom of the page, and the false-positive prose
        path — which used to emit them as a side effect — stops running once the
        table extracts successfully. So claim them explicitly."""
        picked = []
        for pg in pages:
            for b in nontable_by_page.get(pg, []):
                if b.get("type") != "page_footnote" or id(b) in claimed_block_ids:
                    continue
                picked.append(b)
                claimed_block_ids.add(id(b))
        picked.sort(key=_page_y)
        return blocks_to_prose(picked, footnote_ids) if picked else ""

    for t in tables:
        table_id = t["table_id"]
        pages = t["pages"]
        tdir = tables_out_dir / table_id
        tdir.mkdir(exist_ok=True)
        status = {"table_id": table_id, "pages": pages,
                  "source": t.get("source", "geometry-detected"),
                  "bbox": t.get("bbox") or bbox_for(t, pages[0] if pages else None)}
        gm = geo_summary(t) if table_id in geometric_ids else None
        if gm is not None:
            # Each overlay box is recorded WITH the page it describes. A bbox is
            # only meaningful on its own page — y=668 on page 40 says nothing
            # about page 39 — so the dashed overlay must never be drawn on a
            # different page than the one it was measured on.
            status.update(match_method="geometric_iou", match_status=gm["status"],
                          match_iou=gm["iou"], match_note=gm["note"],
                          mineru_bboxes=[b.get("bbox_pt") for b in gm["blocks"]],
                          mineru_bbox_pages=[offset_map.get(b.get("page_idx"))
                                             for b in gm["blocks"]])
        else:
            status.update(match_method="rank_fallback", match_status=None, match_iou=None,
                          match_note="no bbox available for this table (unanchored snapshot "
                                     "fallback) — paired by page position, not geometry",
                          mineru_bboxes=[], mineru_bbox_pages=[])
        if mineru_error:
            status.update(ok=False, reason=f"mineru failed: {mineru_error}")
        else:
            matched = [(pg, b) for pg in pages for b in blocks_for_page(table_id, pg)]
            # Orphan adoption used to happen HERE, by appending the orphan's
            # (page, block) to `matched`. That was page-granular in a place where
            # page granularity is wrong: adding page 35 to a region triggers the
            # middle.json swap below, which supplies the WHOLE page -- so
            # Switzerland__176654 emitted page 35 in section 7 AND section 8, one
            # copy from the adoption and one from table_008's own swap. Adoption is
            # now a ROW-level, subtractive post-pass (place_orphan_rows), the same
            # shape as move_continuation_rows, because only add-and-remove conserves
            # content.
            # An EMPTY body is content_list's page-spanning-table stub, not an empty
            # table: content_list MERGES such a table into one entry, so one page
            # carries the whole thing and the rest arrive blank.
            #
            # The two representations must never be MIXED. Filling only the blanks
            # from middle.json leaves content_list's merged block in place as well,
            # so every row it already held is emitted a second time — measured on
            # Saudi Arabia__174731: 78 cells but 47 distinct, 17 duplicated (some x3),
            # uniqueness 86.9 -> 0.0. Coverage rose and the document became unusable
            # for retrieval.
            #
            # So: use middle.json for the WHOLE region, and only when it covers EVERY
            # page the region claims. A partial swap would mix the two again. If it
            # cannot cover the region, change nothing — content_list's behaviour is
            # what shipped before and is never made worse here.
            _mids = {pg: middle_html.get(crop_of_page.get(pg)) or [] for pg, _ in matched}
            _any_blank = any(not (b.get("table_body") or "").strip() for _, b in matched)
            _covers_all = bool(_mids) and all(_mids.get(pg) for pg, _ in matched)
            if _any_blank and _covers_all:
                # ONE entry per PAGE, not per matched block. middle.json is keyed by
                # page and already holds every table on it, so a page carrying two
                # content_list blocks would otherwise emit that page's rows twice —
                # the same duplication this branch exists to avoid, arriving by the
                # other door. Measured on Bahrain__169749 table_007, whose page 40
                # matches twice: 107 long cells but 90 distinct, 17 duplicated. A page
                # carries 2+ table blocks in 38 of the 71 corpus documents, so this is
                # the common shape, not an edge case.
                #
                # `matched` is collapsed too, not just `htmls`: it is zipped with them
                # for the page markers below and must stay in step.
                _seen = set()
                matched = [(pg, b) for pg, b in matched
                           if not (pg in _seen or _seen.add(pg))]
                htmls = ["\n".join(_mids[pg]) for pg, _ in matched]
                status["body_from_middle_json"] = [pg for pg, _ in matched]
            else:
                htmls = [b.get("table_body", "") or "" for _, b in matched]
                if _any_blank and any(_mids.values()):
                    # Recorded, not acted on: middle.json holds rows for SOME of these
                    # pages but not all, so swapping would mix representations. This is
                    # the shape to look at if a region still reads short.
                    status["middle_json_partial"] = [pg for pg, _ in matched if _mids.get(pg)]
            # MinerU's TEXT blocks inside this table's own footprint belong to the
            # table. They are dropped otherwise, and Stage 1 has already suppressed
            # the same prose from the page as "table territory — Stage 2 will extract
            # it", so nothing else emits them and the words leave the document.
            #
            # Austria 170369 lost 810 words this way. Its clause table (table_006,
            # pages 58-78) has numbered notification requirements that MinerU read as
            # prose rather than as rows — "5. verification of payment of the
            # registration fee", "Appointments to be made", the Non-EEA AIFM legal
            # representative paragraphs. MinerU returned all of it, correctly, as 19
            # `text` blocks; only its 21 `table` blocks were kept.
            #
            # Injected AT POSITION, not appended: each block becomes a full-width row
            # placed above or below that page's rows by its own y, so the table keeps
            # its reading order. Emitting the prose after the table would put an
            # answer under the wrong question, which is worse than the loss.
            htmls = [_inject_page_prose(pg, h, t) for (pg, _), h in zip(matched, htmls)]
            html_parts = [f"<!-- page {pg} -->\n{h}" for (pg, _), h in zip(matched, htmls)]
            # Pages of this table that MinerU matched NO table block on. They are not
            # in `matched`, so the loop above never visits them — and on Austria 170369
            # that is exactly where the loss was: its status reads "no MinerU block
            # overlapped on 2 of 17 pages", those two being 63 and 64, whose 810 words
            # MinerU had returned as `text`. Their prose is emitted in page order with
            # the rest, so the table still reads top to bottom.
            _mpages = {pg for pg, _ in matched}
            for _pg in pages:
                if _pg in _mpages:
                    continue
                _extra = _inject_page_prose(_pg, "", t)
                if (_extra or "").strip():
                    html_parts.append(f"<!-- page {_pg} (prose only) -->\n{_extra}")
                    htmls.append(_extra)
                    matched.append((_pg, None))
            if len(matched) > len(_mpages):
                _order = sorted(range(len(matched)), key=lambda i: matched[i][0])
                matched = [matched[i] for i in _order]
                htmls = [htmls[i] for i in _order]
                html_parts = [html_parts[i] for i in _order]
            _verified_merge = None
            if not any((h or "").strip() for h in htmls):
                # Matched only MinerU's empty stubs. That is the EXPECTED shape of
                # a page-spanning table MinerU merged elsewhere — not a failure.
                # Ask the source PDF which block actually holds this region's own
                # text (see verbatim_match), and let the answer decide:
                #
                #   unclaimed winner -> it IS this table's rows, parked on a
                #                       neighbouring page; adopt and render it.
                #   claimed winner   -> already rendered by that placeholder;
                #                       render nothing and say so (below).
                #   no winner        -> nothing anywhere accounts for this region;
                #                       fall through to an honest failure.
                #
                # Replaces the old adopt_orphan-first ordering, which took ANY
                # single unclaimed block within +/-1 page with no check at all.
                vm = verbatim_match(t, pages)
                if vm is not None:
                    vblock, vrec, vpg = vm
                    if id(vblock) not in _geo_claimed and id(vblock) not in _rank_claimed:
                        if vblock in orphan_blocks:
                            orphan_blocks.remove(vblock)
                        matched = [(vpg, vblock)]
                        htmls = [vblock.get("table_body", "")]
                        html_parts = [f"<!-- page {vpg} (MinerU merge anchor) -->\n{htmls[0]}"]
                        status.update(
                            match_status="uncertain",
                            match_note=f"recovered from MinerU's merged block on page {vpg} — "
                                       f"{vrec:.0%} of this region's text appears there VERBATIM, "
                                       "so these are its rows; MinerU merged the page-spanning "
                                       "table and left an empty stub here",
                            # tagged with ITS page (often a neighbour's), so the
                            # overlay is drawn there and not over unrelated
                            # content on this placeholder's own page
                            mineru_bboxes=[vblock.get("bbox_pt")],
                            mineru_bbox_pages=[vpg],
                            # the old IoU described the EMPTY stub this
                            # placeholder first matched, which is gone now —
                            # keeping it made the badge quote a confident-looking
                            # score for a block that was discarded
                            match_iou=None)
                    else:
                        _verified_merge = (vrec, vpg)
            if not any((h or "").strip() for h in htmls):
                # MinerU produced no usable table. If it instead sees text/footnotes
                # on the page (and no real table anywhere on it), pdf2mdtree's table
                # detection was a FALSE POSITIVE — reclassify to prose (recovering any
                # footnote definitions) instead of flagging a failed table + snapshot.
                #
                # ...but ONLY when Stage 1 actually withheld that text. It does so for
                # a region it can locate: pdf2mdtree skips body lines inside a deferred
                # table's footprint (see _in_table_region there) precisely because
                # MinerU is expected to render them. Recovering prose then is the only
                # way those words reach the reader.
                #
                # An UNANCHORED placeholder is the opposite case. pdf2mdtree creates it
                # under `if not page_has_deferred_table:` — the very flag that gates
                # that skip — so no text was withheld and Stage 1 already emitted the
                # whole page. Recovering prose there appends a second copy of the page,
                # header furniture and all. Measured over out/: 321 such recoveries, and
                # of the 319 checkable against their own Stage 1 tree, 317 were already
                # >70% verbatim-present in it — the shortfall being the running header
                # Stage 1 strips — while the 2 remaining were a running header plus a
                # heading Stage 1 emits as `#`. Nothing is recovered here that is not
                # already in the tree, so emit nothing.
                #
                # `_recovered` is computed exactly as `prose` always was, so the
                # false-positive VERDICT is unchanged for every region — the 39
                # unanchored ones MinerU returned nothing usable for still reach the
                # failure path below and are still reported. Only the EMISSION differs.
                _unanchored = t.get("source") == "snapshot_fallback_unanchored"
                fp_blocks = [b for pg in pages for b in nontable_by_page.get(pg, [])]
                real_table = any((b.get("table_body") or "").strip()
                                 for pg in pages for b in blocks_by_page.get(pg, []))
                _recovered = (blocks_to_prose(fp_blocks, footnote_ids)
                              if (fp_blocks and not real_table) else "")
                prose = "" if _unanchored else _recovered
                if _verified_merge is not None:
                    # PROVEN, not inferred: this region's own text is present
                    # verbatim inside a block another placeholder already
                    # renders, so the rows are on the page in front of the
                    # reader. Render nothing here rather than duplicating them.
                    _vrec, _vpg = _verified_merge
                    status.update(ok=False, continuation=True,
                                  continuation_evidence="verbatim",
                                  continuation_verified=True,
                                  merged_into_page=_vpg,
                                  region_verbatim=round(_vrec, 3),
                                  reason=f"already included in the table rendered for page {_vpg} — "
                                         f"{_vrec:.0%} of this region's text appears there VERBATIM "
                                         "(MinerU merged this page-spanning table and left an empty "
                                         "stub here). Nothing is missing; the snapshot is kept so the "
                                         "merge can be confirmed by eye.")
                elif prose.strip():
                    (tdir / "prose.md").write_text(prose + "\n")
                    status.update(ok=False, false_positive=True, prose=prose,
                                  reason="reclassified as prose — MinerU found text/footnotes, not a table")
                elif _unanchored and _recovered.strip():
                    # Same verdict as the branch above — not a table — but the page's
                    # text is already in the tree (see the note at the top of this
                    # block), so nothing is emitted here at all: no prose, no marker,
                    # and Stage 3 drops the page snapshot with it. suppressed_chars is
                    # the size of what was withheld, so a reviewer can see at a glance
                    # that this was a whole page and not a stray fragment.
                    status.update(ok=False, false_positive=True, stage1_duplicate=True,
                                  suppressed_chars=len(_recovered),
                                  reason="not a table — MinerU found only text on this page, and "
                                         "Stage 1 already emitted that text (unanchored placeholder: "
                                         "no footprint was withheld from the prose flow). Emitting "
                                         "the recovered prose here would duplicate the page.")
                else:
                    # Nothing renders these rows as a table. Last legitimate way
                    # the content can still have reached the reader: MinerU
                    # emitted the region as TEXT rather than a table. Same
                    # referee, same threshold — verbatim sequences, not shared
                    # vocabulary.
                    #
                    # The old code asked two much weaker questions here and
                    # answered "not lost" to both. is_continuation() inferred a
                    # merge from stub/edge GEOMETRY without ever checking the
                    # rows were anywhere; measured across the corpus its median
                    # verbatim recall was 0%, i.e. it was reassuring the reader
                    # about content that genuinely was missing. content_coverage()
                    # then matched a bag of words against a haystack pooling +/-1
                    # page of MinerU output PLUS whole Stage-1 sections, which
                    # scored ~100% for every outcome including real failures.
                    # Both are gone: if no block holds this region's text
                    # verbatim, say so honestly and keep the snapshot.
                    vp = verbatim_match(t, pages, by_page=nontable_by_page)
                    if vp is not None:
                        _pblock, _prec, _ppg = vp
                        status.update(ok=False, absorbed=True,
                                      region_verbatim=round(_prec, 3),
                                      merged_into_page=_ppg,
                                      reason=f"no table block of its own, but {_prec:.0%} of this "
                                             f"region's text appears VERBATIM in MinerU's text output "
                                             f"for page {_ppg} — emitted as prose rather than a table, "
                                             "not lost. The snapshot is kept so the layout can be "
                                             "confirmed by eye.")
                    else:
                        # Genuinely missing: no block anywhere within reach holds
                        # this region's text, as a table or as prose. Say so, and
                        # quote the best evidence found so the number can be
                        # sanity-checked rather than taken on trust.
                        _best = _best_verbatim(t, pages)
                        _eviD = ("" if _best is None else
                                 f"; the closest block holds only {_best:.0%} of it verbatim")
                        status.update(ok=False, region_verbatim=(None if _best is None
                                                                 else round(_best, 3)),
                                      reason=("no MinerU table block located on the source page(s)"
                                              if not matched else
                                              f"MinerU located {len(matched)} table block(s) on the "
                                              "source page(s) but returned no structured rows "
                                              "(empty table_body)")
                                             + _eviD
                                             + ". The snapshot is kept so this page can be checked "
                                               "by eye.")
            else:
                _anoms = []
                table_html, n_rows, n_cols = stitch_table_html(
                    htmls, footnote_ids, table_meta=t, anomalies_out=_anoms)
                report["stitch_anomalies"] += _anoms
                if not table_html:
                    status.update(ok=False, reason="stitched table has no rows")
                else:
                    md_lines = []
                    if t.get("caption"):
                        md_lines += [f"**{t['caption']}**", ""]
                    md_lines.append(table_html)
                    (tdir / "table.md").write_text("\n".join(md_lines) + "\n")
                    # table.html is MinerU's own html kept for inspection, so it never
                    # passes through _serialize_row -- the marker has to be rendered here
                    # too, or a private-use character reaches the file.
                    (tdir / "table.html").write_text(
                        "\n\n".join(html_parts).replace(_BREAK, "<br>"))
                    status.update(ok=True, rows=n_rows, cols=n_cols)
        if not status.get("false_positive"):
            # the false-positive path above already recovers everything on the
            # page as prose unconditionally; don't recover it a second time here
            #
            # KNOWN LIMITATION, measured and deliberately not patched here: the
            # window is pdf2mdtree's manifest bbox, but what gets rendered is
            # MinerU's block, and the two disagree at the edges — so a row just
            # outside the box can still be inside the rendered table and get
            # emitted twice. Tried keying the window on the MinerU block and
            # adding a token-overlap dedup guard; both traded Uniqueness against
            # Completeness with no net win on the corpus, so neither is worth the
            # extra machinery until there is a gold set to tune against.
            before, after = recover_adjacent(pages, t.get("bbox"))
            fn = recover_footnotes(pages)
            if fn:
                after = (after.rstrip("\n") + "\n\n" + fn) if after.strip() else fn
            if before.strip():
                status["recovered_before"] = before
            if after.strip():
                status["recovered_after"] = after
        (tdir / "status.json").write_text(json.dumps(status, indent=2))
        report["tables"].append(status)
        # Bucket by OUTCOME, not by the ok flag alone. A continuation and a
        # reclassified false positive both carry ok=False, but neither is a lost
        # table — the continuation's rows are rendered with the adjacent page's
        # table, and the false positive's region is emitted as prose. Counting
        # them as failures is what made usa-dp report 57 failed while its tree
        # contained 37 failure markers, and Stage 3 (which buckets them
        # separately) disagree with Stage 2 on the same run.
        if status.get("ok"):
            report["tables_ok"] += 1
        elif status.get("continuation"):
            report["tables_continuation"] += 1
        elif status.get("stage1_duplicate"):
            report["tables_stage1_duplicate"] += 1
        elif status.get("false_positive"):
            report["tables_reclassified"] += 1
        elif status.get("absorbed"):
            report["tables_absorbed"] += 1
        else:
            report["tables_failed"] += 1

    # Any content-bearing MinerU block STILL unclaimed is a table MinerU
    # extracted successfully that no placeholder took, so its rows never reach
    # the output. That is silent content loss, and it was previously invisible —
    # record it so the dashboard can surface it instead of the document simply
    # missing a table nobody notices.
    _placed = place_orphan_rows(
        orphan_candidates, tables_out_dir, footnote_ids,
        section_at=_section_at,
        pages_by_id={t["table_id"]: tuple(t.get("pages") or ()) for t in tables})
    report["orphans_placed"] = _placed
    # A block that was PROPOSED but declined is still an orphan. Reporting the
    # proposal as a recovery is how "37 rows relocated" read as a success metric
    # while it was in fact the count of rows taken out of the region that owned them.
    report["orphans_recovered"] = [
        {"table_id": p.get("table_id"), "page": p.get("page"), "chars": p.get("chars"),
         "rows": p.get("rows"), "rows_added": p.get("rows_added"),
         "removed_from": p.get("removed_from") or [], "preview": p.get("preview") or ""}
        for p in _placed["placements"] if p.get("placed")]
    _kept = {p["index"] for p in _placed["placements"] if p.get("placed")}
    orphan_blocks = orphan_blocks + [o["block"] for i, o in enumerate(orphan_candidates)
                                     if i not in _kept]

    # Any content-bearing MinerU block STILL unclaimed is a table MinerU
    # extracted successfully that no placeholder took, so its rows never reach
    # the output. That is silent content loss, and it was previously invisible —
    # record it so the dashboard can surface it instead of the document simply
    # missing a table nobody notices.
    _why = {id(orphan_candidates[p["index"]]["block"]): p.get("reason")
            for p in _placed["placements"] if not p.get("placed")}
    report["orphan_blocks"] = [
        {"page": offset_map.get(b.get("page_idx")),
         "rows": (b.get("table_body") or "").count("<tr"),
         "chars": len(b.get("table_body") or ""),
         "bbox": b.get("bbox_pt"),
         "declined_because": _why.get(id(b)),
         "preview": pdf2mdtree.normspace(
             re.sub(r"<[^>]+>", " ", b.get("table_body") or ""))[:200],
         # The FULL text, not just the preview. Whether an orphan is real loss or a
         # duplicate of what another region already emitted can only be judged
         # against all of it: a 200-char preview covers the first sentence or two,
         # and on Luxembourg__181768 p78 that cleared the block as "duplicate"
         # while question (g) further down was misfiled into section 7.
         "text": pdf2mdtree.normspace(
             re.sub(r"<[^>]+>", " ", b.get("table_body") or ""))}
        for b in orphan_blocks]
    report["orphan_block_count"] = len(orphan_blocks)
    report["continuation_rows_moved"] = move_continuation_rows(
        tables, tables_out_dir, _orig_doc, footnote_ids, headings=_headings)
    _orig_doc.close()
    (stage2_dir / "stage2_report.json").write_text(json.dumps(report, indent=2))
    write_progress(job, "stage2_mineru", "done", ok=report.get("tables_ok"),
                   failed=report.get("tables_failed"),
                   reclassified=report.get("tables_reclassified"))
    return report


# ---------------- stage 3: combine ----------------
def _pages_str(pages):
    # An empty list is reachable: a placeholder whose table_id has no stage-2
    # entry reaches failure_marker(id, None), which supplies []. Indexing [0]
    # there raised IndexError and aborted the ENTIRE stage-3 write — the tree was
    # left with unresolved placeholders and no report. Degrade to a readable
    # marker instead; the caller still records it as a failure.
    if not pages:
        return "page unknown"
    return f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}–{pages[-1]}"


# Human-readable flag appended to a table's badge when it didn't come from a
# normal, trusted table-region detection — so a reader can judge whether the
# placement/content deserves a closer look, right where the table itself is,
# not just in a separate report.
_SOURCE_FLAGS = {
    "snapshot_fallback_anchored": "recovered via page-anchored fallback — the "
        "extractor located a region but didn't trust it enough to render; "
        "verify this landed in the right section",
    "snapshot_fallback_unanchored": "recovered via whole-page fallback (no "
        "anchor point) — may be misplaced if a new section starts on this "
        "same page; verify placement",
}


def failure_marker(table_id, st):
    pages = (st or {}).get("pages") or []
    flag = _SOURCE_FLAGS.get((st or {}).get("source"))
    note = f" ({flag})" if flag else ""
    geo = ""
    if st is None:
        # No stage-2 record at all for this placeholder — the manifest and the
        # report disagree, which means one of them is stale. Say so plainly
        # rather than implying MinerU looked and found nothing.
        geo = (" — no Stage 2 record for this table id; the stage-1 tree and "
               "stage2_report.json are out of sync (stale output directory?)")
    elif st.get("match_status") == "missing" and st.get("match_method") == "geometric_iou":
        geo = " — no MinerU table block geometrically overlaps this placeholder's bbox at all"
    return (f"> **[TABLE EXTRACTION FAILED: {table_id} — {_pages_str(pages)}{note}{geo} — "
            f"see 02_stage2_mineru_tables/tables/{table_id}/status.json]**")


def continuation_marker(table_id, st):
    """Deliberately NOT worded as a failure: the table's rows are present in the
    output, just rendered once with the neighbouring page's table because MinerU
    merged the page-spanning table into one.

    "Nothing is missing" is a strong claim, so it now carries its evidence: the
    share of this region's own text found VERBATIM in the table that absorbed
    it, and which page that is. Previously the same sentence was printed on the
    strength of a stub/edge geometry guess that was never checked against the
    rows — corpus-wide, the median such claim had 0% of its text anywhere
    nearby, i.e. it was reassuring readers about content that really was gone."""
    st = st or {}
    ev = ""
    if st.get("continuation_verified") and st.get("region_verbatim") is not None:
        ev = (f" Verified: {st['region_verbatim']:.0%} of this region's text appears verbatim in "
              f"the table rendered for page {st.get('merged_into_page')}.")
    return (f"> **[TABLE CONTINUATION: {table_id} — {_pages_str(st.get('pages') or [])}]** "
            "this region continues a page-spanning table whose rows MinerU merged into the "
            f"adjacent table above; nothing is missing.{ev} Snapshot retained for verification.")


def absorbed_marker(table_id, st):
    """Not a failure: no table block matched this region, but its text is present
    in MinerU's output for the same pages — absorbed into a neighbouring table or
    emitted as prose. Verified by CONTENT, which is the only evidence available
    when geometry has nothing to match against."""
    st = st or {}
    cov = st.get("region_coverage")
    pct = f" ({cov:.0%} of its text confirmed present)" if isinstance(cov, (int, float)) else ""
    return (f"> **[TABLE REGION ABSORBED: {table_id} — {_pages_str(st.get('pages') or [])}]**{pct} "
            "no separate table was extracted for this region, but its content appears in the "
            "adjacent extracted output rather than being lost. Snapshot retained so the layout "
            "can be confirmed by eye.")


def mineru_badge(table_id, st):
    st = st or {}
    flag = _SOURCE_FLAGS.get(st.get("source"))
    note = f" — ⚠ {flag}" if flag else ""
    geo = ""
    if st.get("match_status") == "uncertain":
        # IoU is absent when the match didn't come from geometry at all — a
        # multi-page table has no bbox to compare, and merge-recovery adopts a
        # block from a neighbouring page rather than by overlap.
        iou = st.get("match_iou")
        pct = f" (IoU {iou:.0%})" if isinstance(iou, (int, float)) else ""
        geo = f" — ⚠ match uncertain{pct}: {st.get('match_note', '')}"
    return (f"**⚙ MinerU-extracted table** — {table_id}, {_pages_str(st.get('pages') or [])}, "
            f"{st.get('rows', '?')} rows × {st.get('cols', '?')} cols{note}{geo}")


def run_stage3(stage1_dir, stage2_dir, stage3_dir, stage2_report):
    write_progress(Path(stage3_dir).parent, "stage3", "running")
    if stage3_dir.exists():
        shutil.rmtree(stage3_dir)
    shutil.copytree(stage1_dir, stage3_dir)

    status_by_id = {t["table_id"]: t for t in stage2_report.get("tables", [])}
    counts = {"filled": 0, "failed": 0, "reclassified": 0, "continuation": 0, "absorbed": 0,
              "stage1_duplicate": 0}
    # Both false-positive kinds drop their page snapshot: the prose kind because the
    # recovered text replaces it, the stage1_duplicate kind because Stage 1's own text
    # was already there all along. Neither leaves a table for the image to evidence.
    fp_pages = {pg for t in stage2_report.get("tables", [])
                if t.get("false_positive") for pg in t.get("pages", [])}

    def _sub(m):
        table_id = m.group(1)
        st = status_by_id.get(table_id)
        before = (st.get("recovered_before", "") if st else "").rstrip("\n")
        after = (st.get("recovered_after", "") if st else "").rstrip("\n")
        if st and st.get("ok"):
            table_md_path = stage2_dir / "tables" / table_id / "table.md"
            if table_md_path.exists():
                counts["filled"] += 1
                body = mineru_badge(table_id, st) + "\n\n" + table_md_path.read_text().rstrip("\n")
            else:
                # Stage 2 said ok but its output is gone (moved/partially-copied
                # job dir). Flag it rather than letting an unguarded read_text
                # abort the whole stage-3 write.
                counts["failed"] += 1
                body = failure_marker(table_id, None)
        elif st and st.get("false_positive") and st.get("prose"):
            # not a real table — emit the recovered prose/footnote defs, no marker
            counts["reclassified"] += 1
            body = st["prose"].rstrip("\n")
        elif st and st.get("stage1_duplicate"):
            # not a real table either, but Stage 1 never withheld this page's text, so
            # there is nothing to recover — anything emitted here would be a second
            # copy of the page. No prose, no marker; the snapshot goes via fp_pages.
            counts["stage1_duplicate"] += 1
            body = ""
        elif st and st.get("continuation"):
            # Rows already rendered with the adjacent page's table (MinerU merged
            # this page-spanning table). A FAILURE marker here would claim content
            # is missing when it isn't — emit a quiet pointer instead, and let the
            # page snapshot stand so the merge can be confirmed.
            counts["continuation"] += 1
            body = continuation_marker(table_id, st)
        elif st and st.get("absorbed"):
            counts["absorbed"] += 1
            body = absorbed_marker(table_id, st)
        else:
            counts["failed"] += 1
            body = failure_marker(table_id, st)
        # before/after: text pdf2mdtree swallowed into this table's exclusion
        # zone (a heading or short point squeezed right above/below it) that
        # MinerU still recognized as ordinary text — spliced back immediately
        # around the table so it lands exactly where it belongs.
        return "\n\n".join(p for p in (before, body, after) if p)

    for md_path in stage3_dir.rglob("*.md"):
        text = md_path.read_text()
        if "<!-- TABLE:" not in text:
            continue
        new_text = strip_filled_snapshots(TABLE_RE.sub(_sub, text))
        # false-positive pages carry no marker (prose was emitted, or Stage 1's own text
        # already stands in for it) -> strip their snapshot too
        if fp_pages:
            new_text = _SNAP_BLOCK.sub(
                lambda mm: "" if int(mm.group(1)) in fp_pages else mm.group(0), new_text)
        md_path.write_text(new_text)

    manifest_path = stage1_dir / "tables_manifest.json"
    total_tables = (len(json.loads(manifest_path.read_text()).get("tables", []))
                    if manifest_path.exists() else 0)
    report = {
        "tables_total": total_tables,
        "tables_filled": counts["filled"],
        "tables_reclassified": counts["reclassified"],  # false positives -> prose (no marker/snapshot)
        "tables_stage1_duplicate": counts["stage1_duplicate"],  # false positives whose text Stage 1
                                                        # already emitted -> nothing rendered at all
        "tables_continuation": counts["continuation"],  # merged page-spanning table, rows already emitted
        "tables_absorbed": counts["absorbed"],          # no table of its own, but content verified present
        "tables_failed": counts["failed"],
        "failed_table_ids": [t["table_id"] for t in stage2_report.get("tables", [])
                             if not t.get("ok") and not t.get("false_positive")
                             and not t.get("continuation") and not t.get("absorbed")],
    }
    (stage3_dir / "stage3_report.json").write_text(json.dumps(report, indent=2))
    lines = ["# Stage 3 — combine report", ""]
    for k, v in report.items():
        lines.append(f"- **{k}**: {v}")
    (stage3_dir / "STAGE3_REPORT.md").write_text("\n".join(lines) + "\n")
    return report


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--depth", type=int, default=None,
                    help="override the product rule's nesting depth")
    ap.add_argument("--mineru-backend", default=None,
                    help=f"override MinerU backend (default: settings.mineru_backend"
                         f" = {settings.mineru_backend!r})")
    ap.add_argument("--mineru-effort", default=None,
                    help=f"override MinerU effort (default: settings.mineru_effort"
                         f" = {settings.mineru_effort!r})")
    ap.add_argument("--stage4-ai", action="store_true",
                    help="run stage 4: AI table post-processing (Bedrock; costs money). "
                         "Writes 04_stage4_ai/ alongside stage 3, never in place of it. "
                         "Asking for it on the command line IS the opt-in, so this runs "
                         "regardless of ACI_STAGE4_AI_ENABLED — that flag gates callers "
                         "that did not ask, such as a server.")
    ap.add_argument("--stage4-mode", default="section", choices=("section", "batched"),
                    help="section (default): one call per section with its page images as "
                         "ground truth. batched: row batches with the character-exact "
                         "reprojection gate.")
    # Which model. There was no flag, so every run took models[0] of run_stage4's default
    # tuple -- Haiku -- and a Sonnet comparison had to be driven from a python one-liner
    # that no one could find again. The prices are the ones ai_postprocess.PRICES bills at.
    # NOT `choices`: any Bedrock model id must be accepted verbatim. See
    # ai_postprocess.resolve_model.
    ap.add_argument("--stage4-model", default=None, metavar="NAME",
                    help="a short alias (haiku | sonnet | sonnet5) or any Bedrock model id. "
                         "Default: ACI_STAGE4_AI_MODEL. Section mode makes one call per "
                         "section, so a 74-page MRAM document is roughly $0.34 on haiku and "
                         "$1.07 on either Sonnet.")
    ap.add_argument("--stage4-only", default=None,
                    help="restrict stage 4 to these section files, by filename prefix, "
                         "comma-separated (e.g. '04-,05-'). Default: every section.")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).resolve()
    root = Path(args.out).resolve() / slug_stem(pdf_path)
    stage1_dir = root / "01_stage1_extract"
    stage2_dir = root / "02_stage2_mineru_tables"
    stage3_dir = root / "03_stage3_final"
    root.mkdir(parents=True, exist_ok=True)

    # Rotated pages are read sideways by every geometric test in Stage 1, so they are
    # baked upright first. Only a document that HAS one is rewritten, and only into a
    # working copy here -- the file the caller named is never modified.
    if any(pg.rotation for pg in fitz.open(str(pdf_path))):
        upright = root / "source_upright.pdf"
        upright.write_bytes(pdf_path.read_bytes())
        if derotate_pdf(upright):
            pdf_path = upright

    print(f"=== Stage 1: extract (defer tables) -> {stage1_dir} ===")
    manifest = run_stage1(pdf_path, stage1_dir, args.depth)
    print(f"  {len(manifest.get('tables', []))} table(s) detected and deferred.")

    print(f"=== Stage 2: MinerU table extraction -> {stage2_dir} ===")
    stage2_report = run_stage2(pdf_path, manifest, stage2_dir, args.mineru_backend, args.mineru_effort)
    print(f"  {stage2_report['tables_ok']} ok, {stage2_report['tables_failed']} failed, "
          f"{stage2_report['tables_continuation']} continuation, "
          f"{stage2_report['tables_reclassified']} reclassified as prose, "
          f"{stage2_report['tables_stage1_duplicate']} not a table (already in Stage 1 text).")

    print(f"=== Stage 3: combine -> {stage3_dir} ===")
    stage3_report = run_stage3(stage1_dir, stage2_dir, stage3_dir, stage2_report)
    print(f"  {stage3_report['tables_filled']} filled, {stage3_report['tables_failed']} failed.")

    # Stage 4 is opt-in and additive: it copies stage 3 and repairs table cell text
    # against the page images. Off by default because it calls a paid API, and separate
    # from stage 3 so its output can always be diffed against the input it started from.
    stage4_report = None
    if args.stage4_ai:
        from aosphere_core_index.extract.ai_postprocess import run_stage4

        stage4_dir = root / "04_stage4_ai"
        print(f"=== Stage 4: AI post-processing -> {stage4_dir} ===")
        only = [s for s in (args.stage4_only or "").split(",") if s] or None
        from aosphere_core_index.extract.ai_postprocess import resolve_model
        _model = resolve_model(args.stage4_model)
        print(f"  model: {_model}")
        stage4_report = run_stage4(stage3_dir, stage4_dir, pdf_path, only=only,
                                   mode=args.stage4_mode, force=True,
                                   models=(_model,))
        u = stage4_report["usage"]
        if stage4_report.get("mode") == "grounded":
            print(f"  {stage4_report['tables_accepted']} of {stage4_report['tables_total']} "
                  f"tables repaired, {stage4_report['tables_kept_stage3']} kept as stage 3; "
                  f"{u['total_tokens']:,} tokens, ${u['cost_usd']:.4f}.")
        else:
            print(f"  {stage4_report['batches_changed']} of {stage4_report['batches_total']} "
                  f"batches changed, {stage4_report['batches_rejected']} rejected; "
                  f"{u['total_tokens']:,} tokens, ${u['cost_usd']:.4f}.")

    n_tables = len(manifest.get("tables", []))
    summary = root / "PIPELINE_SUMMARY.md"
    summary.write_text(
        "# Hybrid extraction pipeline — summary\n\n"
        f"Source: `{pdf_path}`\n\n"
        f"- [Stage 1 — extract (tables deferred)](01_stage1_extract/README.md)\n"
        f"- [Stage 2 — MinerU table extraction](02_stage2_mineru_tables/stage2_report.json)\n"
        f"- [Stage 3 — final combined tree](03_stage3_final/README.md)\n"
        + (f"- [Stage 4 — AI post-processing](04_stage4_ai/STAGE4_REPORT.md)\n"
           if stage4_report else "") + "\n"
        f"Tables: {n_tables} detected, {stage3_report['tables_filled']} extracted by MinerU, "
        f"{stage3_report['tables_failed']} failed "
        f"(flagged in Stage 3, not silently dropped), "
        f"{stage3_report['tables_continuation']} continuation of a merged table, "
        f"{stage3_report['tables_reclassified']} reclassified as prose, "
        f"{stage3_report['tables_stage1_duplicate']} not a table and already covered by "
        f"Stage 1's own text.\n"
    )
    print(f"\nDone. See {summary}")


if __name__ == "__main__":
    main()
