"""A group label printed above a heading belongs IN that heading's section.

Every memo in Marketing Restrictions - Asset Management heads an appendix with the label
alone on one line and the descriptive title on the next:

    APPENDIX 2
    DISCLAIMERS: CLOSED-ENDED FUND

Only the second line is ever recognised as a heading, because the bookmark outline and
the contents page both name the appendix "2 Disclaimers: Closed-Ended Fund". The label
therefore fell one paragraph short of the section it introduces and was emitted as the
LAST LINE of the PREVIOUS appendix's content:

    14-disclaimers-open-ended-fund.md      <- appendix 1's content
        ... its last line: "APPENDIX 2"    <- appendix 2's label, stranded
    15-disclaimers-closed-ended-fund.md    <- appendix 2's content

Measured over 49 documents: 156 chunks ended on a label belonging to the next one.

The label is MOVED, and nothing else. An earlier attempt folded it into the title
instead, and that title is the chunk's name and its entry in the section map -- so
rewriting it moved the node the scorecard checks. Same 49 documents: labels were
relocated exactly as intended, and `sectioning` collapsed anyway (Bahrain 100 -> 73.3,
Australia 100 -> 9.3), taking pass from 75 to 14 with 6 outright failures. The orphan was
a content problem, so the repair belongs in the content. With the label merely moved, the
same corpus is gate-for-gate identical (41 pass / 8 review / 0 fail both ways) with the
worst score up +0.59 on average.

These tests pin the fix AND that invariant: the chunk names must come out byte-for-byte
the same with the flag on and off.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fitz  # noqa: E402
from pdf2mdtree import _GROUP_LABEL_RE as LABEL  # noqa: E402


# ---- the label pattern: alone on its line, or nothing --------------------------

@pytest.mark.parametrize("line,expected", [
    ("APPENDIX 2", ("APPENDIX", "2")),
    ("Appendix 1", ("Appendix", "1")),
    ("SCHEDULE 1", ("SCHEDULE", "1")),
    ("Schedule 12", ("Schedule", "12")),
    ("ANNEX 3", ("ANNEX", "3")),
    ("Annexure 3", ("Annexure", "3")),
    ("EXHIBIT 4", ("EXHIBIT", "4")),
    ("APPENDIX 2:", ("APPENDIX", "2")),          # a trailing colon is still just a label
])
def test_a_label_alone_on_its_line_is_recognised(line, expected):
    m = LABEL.match(line)
    assert m is not None, f"{line!r} should be recognised as a group label"
    assert (m.group("word"), m.group("num")) == expected


@pytest.mark.parametrize("line", [
    "Appendix 1(A) applies.",       # Chile 169565: a cross-reference at the top of a page
    "See Appendix 2 for details",
    "APPENDIX",                     # no number: cannot say which appendix
    "APPENDIX 2 DISCLAIMERS",       # already one line — nothing to move
    "PART B",                       # heads whole memoranda here; not a section label
    "1. Disclaimers",
    "",
])
def test_anything_else_is_not_a_label(line):
    """Alone-on-line is the whole test. Chile 169565 opens a page with "Appendix 1(A)
    applies." mid-sentence; moving that into a section would relocate prose."""
    assert LABEL.match(line) is None, f"{line!r} must not be taken as a group label"


# ---- end to end, through the real extractor -----------------------------------

def _memo(tmp_path: Path, label: str = "APPENDIX") -> Path:
    """A memo shaped like this product's, WITH a bookmark outline.

    The outline is what makes this the real case: it names the descriptive titles only
    ("Disclaimers: Open-Ended Fund"), never the labels, so `APPENDIX 1` matches no entry
    and stays body text -- landing at the end of whichever section precedes it. Without
    an outline both lines are merely bold, the label becomes a heading of its own, and
    there is no orphan to move at all (Italy 181491) -- which is a different shape and
    deliberately left alone.
    """
    doc = fitz.open()
    body = doc.new_page(width=595, height=842)
    body.insert_text((72, 90), "1. BACKGROUND", fontsize=14)
    body.insert_text((72, 130), "Body text for the background section.", fontsize=11)

    titles = ["DISCLAIMERS: OPEN-ENDED FUND", "DISCLAIMERS: CLOSED-ENDED FUND"]
    for n, title in enumerate(titles, start=1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), f"{label} {n}", fontsize=11)       # the label, body-sized
        page.insert_text((72, 115), title, fontsize=14)               # the heading
        page.insert_text((72, 160), f"Content belonging to {label.lower()} {n}.", fontsize=11)

    doc.set_toc([[1, "1. BACKGROUND", 1]]
                + [[1, t.title(), i + 2] for i, t in enumerate(titles)])
    out = tmp_path / "memo.pdf"
    doc.save(str(out))
    doc.close()
    return out


def _tree(tmp_path: Path, pdf: Path, *, group_labels: bool = True) -> dict[str, str]:
    """Run Stage 1 and return {filename: text} for the emitted sections.

    PYTHONUTF8 is set because pdf2mdtree writes its markdown with the interpreter's
    default encoding, so on a cp1252 Windows console the en-dash in "page 1-2" lands as
    an undecodable byte. The pipeline's own runner exports it; without it the same tree
    is written in two different encodings depending on the host.
    """
    import os
    import subprocess

    outdir = tmp_path / ("tree_on" if group_labels else "tree_off")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable,
           str(Path(__file__).resolve().parent.parent / "scripts" / "pdf2mdtree.py"),
           str(pdf), "-o", str(outdir)]
    if group_labels:
        cmd.append("--group-labels")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    assert r.returncode == 0, f"pdf2mdtree failed:\n{r.stderr[-2000:]}"
    # Sections only. CONVERSION_REPORT.md and README.md are the run's own metadata, and
    # the report legitimately gains a group_labels_moved count when the flag is on.
    reports = {"CONVERSION_REPORT.md", "README.md", "STAGE3_REPORT.md"}
    return {p.name: p.read_text(encoding="utf-8", errors="replace")
            for p in outdir.glob("*.md") if p.name not in reports}


def _body_lines(text: str) -> list[str]:
    """A section's content lines: heading, source note and blanks dropped."""
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.startswith(("#", "*Source:"))]


def _stranded(files: dict[str, str]) -> list[tuple[str, str]]:
    """Labels sitting at the END of a section — i.e. in the wrong chunk."""
    out = []
    for name, text in files.items():
        body = _body_lines(text)
        if body and LABEL.match(body[-1]):
            out.append((name, body[-1]))
    return out


def _opens_with_label(files: dict[str, str]) -> list[tuple[str, str]]:
    """Labels OPENING a section — i.e. in the chunk they name."""
    out = []
    for name, text in files.items():
        body = _body_lines(text)
        if body and LABEL.match(body[0]):
            out.append((name, body[0]))
    return out


@pytest.mark.parametrize("label", ["APPENDIX", "SCHEDULE", "ANNEX"])
def test_the_label_moves_out_of_the_previous_section(tmp_path, label):
    """The defect: the label stranded at the end of the section BEFORE the one it names."""
    pdf = _memo(tmp_path, label)
    off = _tree(tmp_path, pdf, group_labels=False)
    on = _tree(tmp_path, pdf, group_labels=True)

    assert _stranded(off), (
        f"the fixture does not reproduce the defect for {label} — nothing stranded. "
        f"Got: { {n: _body_lines(t) for n, t in off.items()} }")
    assert not _stranded(on), \
        f"a label is still stranded at the end of a section: {_stranded(on)}"


@pytest.mark.parametrize("label", ["APPENDIX", "SCHEDULE", "ANNEX"])
def test_the_label_opens_the_section_it_names(tmp_path, label):
    """And it lands in the right place: the first content line of its own section, so the
    chunk finally says which appendix it is."""
    on = _tree(tmp_path, _memo(tmp_path, label), group_labels=True)
    opened = _opens_with_label(on)
    assert len(opened) == 2, f"expected both labels to open their own section, got {opened}"
    assert {lbl for _n, lbl in opened} == {f"{label} 1", f"{label} 2"}


def test_the_chunk_names_are_byte_for_byte_unchanged(tmp_path):
    """The invariant that the earlier design broke, and the reason this one is safe.

    The title is the chunk's name and its entry in the section map. Folding the label
    into it renamed the node the scorecard checks and cost 61 of 91 documents their gate.
    Moving only the content must leave every name identical.
    """
    pdf = _memo(tmp_path)
    off = _tree(tmp_path, pdf, group_labels=False)
    on = _tree(tmp_path, pdf, group_labels=True)
    assert sorted(off) == sorted(on), (
        "the flag changed the section names — this is what broke `sectioning`.\n"
        f"  off: {sorted(off)}\n  on : {sorted(on)}")


def _headings(files: dict[str, str]) -> dict[str, str]:
    return {n: next((l for l in t.splitlines() if l.startswith("# ")), "")
            for n, t in files.items()}


def _all_lines(files: dict[str, str]) -> list[str]:
    return sorted(l for t in files.values() for l in _body_lines(t))


def test_the_headings_themselves_are_unchanged(tmp_path):
    """Same invariant, checked on the heading line rather than the filename."""
    pdf = _memo(tmp_path)
    off = _tree(tmp_path, pdf, group_labels=False)
    on = _tree(tmp_path, pdf, group_labels=True)
    assert _headings(off) == _headings(on), \
        "the flag rewrote a heading; it must only move content"


def test_no_content_is_lost_or_duplicated(tmp_path):
    """Moving a line must not drop or copy it."""
    pdf = _memo(tmp_path)
    off = _tree(tmp_path, pdf, group_labels=False)
    on = _tree(tmp_path, pdf, group_labels=True)
    assert _all_lines(off) == _all_lines(on), \
        "content changed; the label should only have moved"


def test_the_body_section_is_untouched(tmp_path):
    """Only a label directly above a heading is moved; ordinary sections are unaffected."""
    on = _tree(tmp_path, _memo(tmp_path), group_labels=True)
    background = [t for n, t in on.items() if "background" in n.lower()]
    assert background, f"the body section vanished: {sorted(on)}"
    assert not _opens_with_label({"bg": background[0]})


# ---- product-scoped: off unless the product asks ------------------------------

def test_without_the_flag_the_defect_is_still_there(tmp_path):
    """The scoping, pinned from the outside: no --group-labels, no change. The shape has
    only been counted on one product, so the others must extract exactly as before."""
    off = _tree(tmp_path, _memo(tmp_path), group_labels=False)
    assert _stranded(off), "with the flag OFF the label moved anyway — not product-scoped"
    assert not _opens_with_label(off)


def test_only_this_product_has_the_rule():
    """The rule itself. Adding a product here is a deliberate act, not a default."""
    from product_rules import GROUP_LABEL_HEADINGS, group_label_headings

    assert group_label_headings("124_Marketing_Restrictions_-_Asset_Management") is True
    for other in ("104_Shareholding_Disclosure", "155_Data_Privacy", "999_Unknown", "", None):
        assert group_label_headings(other) is False, f"{other!r} must not opt in by default"
    assert GROUP_LABEL_HEADINGS == {"124_Marketing_Restrictions_-_Asset_Management"}, \
        "a product was added to the rule without its labels being counted"


@pytest.mark.parametrize("product,expected", [
    ("124_Marketing_Restrictions_-_Asset_Management", True),
    ("104_Shareholding_Disclosure", False),
    ("155_Data_Privacy", False),
])
def test_run_stage1_passes_the_flag_for_the_scoped_product_only(tmp_path, monkeypatch,
                                                                product, expected):
    """The wiring, not just the rule. run_stage1 resolves the product from the job's own
    corpus_meta.json and turns it into a CLI flag; a rule nothing passes on does nothing.
    (This is how --depth and --clause-tables already reach Stage 1.)"""
    import json
    import types

    import hybrid_extract as he

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(he.subprocess, "run", fake_run)
    monkeypatch.setattr(he, "write_progress", lambda *a, **k: None)

    job = tmp_path / "Guernsey__174507"
    job.mkdir()
    (job / "corpus_meta.json").write_text(json.dumps({"product": product}))
    with pytest.raises(Exception):                 # bails on the faked returncode
        he.run_stage1(job / "source.pdf", job / "01_stage1_extract")

    assert ("--group-labels" in seen.get("cmd", [])) is expected
