"""A /Rotate 90 page must be baked upright before Stage 1 reads it.

fitz hands back text on a rotated page in UNROTATED coordinates: reading order runs
along DECREASING y, and successive lines advance along INCREASING x. Every geometric
judgement Stage 1 makes assumes the opposite — y is the line axis, x is the position
within a line — so on a rotated page it reads the document sideways: line order, table
region containment, heading position, bullet indent, all measured on the wrong axis.

Japan 172122 is what this cost. 62 of its 83 pages are /Rotate 90. Page 16 prints

    4.   [SECTION INTENTIONALLY LEFT BLANK]
    5.   PASSIVE MARKETING (REVERSE-ENQUIRY)

and rotated, those two lines report y=514.1 and y=504.8 — side by side in rotated space
rather than stacked — so ordering by y put clause 5 BEFORE clause 4 in the paragraph
stream. The outline matcher only moves forward (`pi = found`), so once clause 4 matched
at the later index clause 5 was unreachable: it joined the "could not be located" list
with seven others, its content merged into the empty clause-4 stub, and that stub then
owned pages 16-22. Clause 10 went the same way on page 66, behind clause 9's stub. Stage
4, which is handed a section's own pages as ground truth, then rebuilt 6.1/6.2/6.3 inside
the stub from pages that print section 6 — so the content existed twice and Stage 5 split
both copies.

Bahamas 183503, the document Japan was compared against, has all 74 pages upright. That
is the entire reason its tree came out clean.

Measured after this fix: Japan locates 39 of 40 outline headings (was 32), clauses 5 and
10 exist as their own sections, and both "[SECTION INTENTIONALLY LEFT BLANK]" stubs carry
no pages at all — the same shape Bahamas has.

Rare enough that the blast radius is small by construction: 3 of 153 corpus documents have
any rotated page (Japan 62/83, Greece 138436 6/13, Austria 137944 6/14), 74 pages in
13,603. A document with none is not rewritten.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

fitz = pytest.importorskip("fitz")

from hybrid_extract import derotate_pdf  # noqa: E402

# Japan 172122 page 16's two clause headings, in the order the page prints them.
BLANK = "4.   [SECTION INTENTIONALLY LEFT BLANK]"
PASSIVE = "5.   PASSIVE MARKETING (REVERSE-ENQUIRY)"


def _pdf(tmp_path, rotation):
    """A page shaped like Japan 172122's page 16.

    The rotation has to be built the way a real landscape document has it: the text is
    drawn ROTATED in the content stream and /Rotate cancels it for display, so the page
    prints upright. Drawing upright text and then setting /Rotate models the opposite
    document — one that really is printed sideways — and derotating that correctly
    produces sideways text, which made this fixture pass while proving nothing.

    So consecutive lines advance along x, as Japan's two clause headings do at x=316 and
    x=340, and ordering the blocks by y therefore reads them backwards.
    """
    doc = fitz.open()
    page = doc.new_page(width=792, height=612)
    if rotation:
        page.insert_text((316, 560), BLANK, fontsize=9, rotate=rotation)
        page.insert_text((340, 550), PASSIVE, fontsize=9, rotate=rotation)
    else:
        page.insert_text((60, 300), BLANK, fontsize=9)
        page.insert_text((60, 324), PASSIVE, fontsize=9)
    page.set_rotation(rotation)
    p = tmp_path / f"rot{rotation}.pdf"
    doc.save(str(p))
    doc.close()
    return p


def _reading_order(pdf):
    """The page's text blocks as Stage 1 orders them: by y, then x."""
    doc = fitz.open(str(pdf))
    blocks = []
    for b in doc[0].get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        text = " ".join(s["text"] for l in b["lines"] for s in l["spans"]).strip()
        if text:
            blocks.append((b["bbox"][1], b["bbox"][0], text))
    doc.close()
    return [t for _y, _x, t in sorted(blocks)]


def test_a_rotated_page_reads_in_the_wrong_order_before_the_fix(tmp_path):
    """The defect itself, pinned on the input, so the fix below measures something real.

    Both halves matter: the text direction is the one Japan reports, and ordering by y
    genuinely reverses the two clauses.
    """
    pdf = _pdf(tmp_path, 90)
    doc = fitz.open(str(pdf))
    dirs = {tuple(round(v) for v in l["dir"])
            for b in doc[0].get_text("dict")["blocks"] if b.get("type") == 0
            for l in b["lines"]}
    doc.close()
    assert dirs == {(0, -1)}, f"expected Japan's rotated text direction, got {dirs}"
    order = _reading_order(pdf)
    assert order.index(PASSIVE) < order.index(BLANK), \
        f"fixture does not reproduce the inversion: {order}"


def test_derotation_restores_reading_order(tmp_path):
    pdf = _pdf(tmp_path, 90)
    assert derotate_pdf(pdf, log=lambda *a: None) == 1
    order = _reading_order(pdf)
    assert order.index(BLANK) < order.index(PASSIVE), order


def test_derotation_makes_the_text_upright(tmp_path):
    pdf = _pdf(tmp_path, 90)
    derotate_pdf(pdf, log=lambda *a: None)
    doc = fitz.open(str(pdf))
    page = doc[0]
    dirs = {tuple(round(v) for v in l["dir"])
            for b in page.get_text("dict")["blocks"] if b.get("type") == 0
            for l in b["lines"]}
    rot = page.rotation
    doc.close()
    assert rot == 0
    assert dirs == {(1, 0)}, dirs


def test_the_words_are_unchanged(tmp_path):
    """Rotation is folded into the content matrix, not re-typeset. Nothing may be lost:
    this runs before Stage 1, so anything dropped here is dropped from the document."""
    pdf = _pdf(tmp_path, 90)
    doc = fitz.open(str(pdf))
    before = sorted(doc[0].get_text().split())
    doc.close()
    derotate_pdf(pdf, log=lambda *a: None)
    doc = fitz.open(str(pdf))
    after = sorted(doc[0].get_text().split())
    doc.close()
    assert before == after


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_every_rotation_is_handled(rotation, tmp_path):
    pdf = _pdf(tmp_path, rotation)
    assert derotate_pdf(pdf, log=lambda *a: None) == 1
    doc = fitz.open(str(pdf))
    rot = doc[0].rotation
    doc.close()
    assert rot == 0


def test_an_upright_document_is_not_touched_at_all(tmp_path):
    """150 of the corpus's 153 documents. Not "rewritten identically" — not rewritten,
    so no re-extraction anywhere else can be blamed on this."""
    pdf = _pdf(tmp_path, 0)
    before = pdf.read_bytes()
    assert derotate_pdf(pdf, log=lambda *a: None) == 0
    assert pdf.read_bytes() == before


def test_a_pdf_that_cannot_be_read_is_left_alone(tmp_path):
    """Fail-safe, like the pre-flight: a document that cannot be rewritten is extracted
    as it would have been before, never dropped."""
    bad = tmp_path / "not-a.pdf"
    bad.write_text("this is not a PDF")
    said = []
    assert derotate_pdf(bad, log=said.append) == 0
    assert bad.read_text() == "this is not a PDF"
    assert said, "a refusal must say so — a silent skip is how this went unnoticed"


def test_no_stray_temp_file_is_left_behind(tmp_path):
    pdf = _pdf(tmp_path, 90)
    derotate_pdf(pdf, log=lambda *a: None)
    assert [p.name for p in tmp_path.iterdir()] == [pdf.name]


# ------------------------------------------------- the wiring, and what it must not break
# Derotation rewrites the WORKING COPY (dest/source.pdf), which changes its bytes — so the
# document's identity has to be taken from the SOURCE, not from the copy. corpus_worker
# already groups shards by `pdf_sha1(job["pdf"])`, the source, while run_corpus recorded
# the copy's hash; identical for every upright document, and silently divergent for a
# rotated one, which would have sent a duplicate to a second full MinerU pass.

def _twin(d):
    """A finished job, as run_corpus.run_one's clone path expects to find one."""
    import run_corpus as rc
    for sub in rc.ARTIFACTS:
        if not sub.endswith(".json"):
            (d / sub).mkdir(parents=True, exist_ok=True)
    d.mkdir(parents=True, exist_ok=True)
    (d / "validation.json").write_text("{}")
    (d / "scorecard.json").write_text('{"gate": "review", "worst_score": 84.3}')
    return d


def test_the_working_copy_is_derotated_and_identity_stays_the_sources(tmp_path):
    import run_corpus as rc
    src = _pdf(tmp_path, 90)
    twin = _twin(tmp_path / "twin")
    dest = tmp_path / "dest"
    job = {"pdf": src, "jurisdiction": "Japan", "doc_id": "172122", "label": "Japan__172122"}
    out = rc.run_one(job, "124_Prod", dest, False, False, None, None, None,
                     hash_idx={rc.pdf_sha1(src): twin})
    # Finding the twin at all proves the hash looked up is the SOURCE's: by this point the
    # working copy has been rewritten and hashes differently.
    assert out["status"] == "duplicate", out
    doc = fitz.open(str(dest / "source.pdf"))
    rot = doc[0].rotation
    doc.close()
    assert rot == 0, "the working copy Stage 1 would read is still rotated"


def test_an_unreadable_pdf_does_not_break_the_clone_path(tmp_path):
    """Derotation runs before the twin lookup, so it must never be what stops a job."""
    import run_corpus as rc
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4\nnot a real pdf, never parsed on this path\n")
    twin = _twin(tmp_path / "twin")
    dest = tmp_path / "dest"
    job = {"pdf": src, "jurisdiction": "Peru", "doc_id": "999", "label": "Peru__999"}
    out = rc.run_one(job, "124_Prod", dest, False, False, None, None, None,
                     hash_idx={rc.pdf_sha1(src): twin})
    assert out["status"] == "duplicate", out
