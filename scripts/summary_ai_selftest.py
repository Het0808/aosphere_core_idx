#!/usr/bin/env python3
"""summary_ai_selftest.py — does the scorecard actually CATCH anything?

The scorecard reports four numbers about a transcript. Nothing in the pipeline tests
whether those numbers respond to a defect, and a check that cannot fail is not a check --
it is decoration that reads as assurance. Every dimension in here scored 98-100 on the
first document ever run through it, which is exactly the situation where a broken detector
and a good extraction are indistinguishable.

So this takes a transcript that is already on disk, BREAKS IT ON PURPOSE in one specific
way at a time, re-scores it, and asserts the right dimension moved. It is mutation testing
for the measurement rather than for the code: the question is not "does coverage() run"
but "if a paragraph went missing, would coverage() say so".

Free, and deterministic. No model is called -- the mutations are applied to text already
extracted, so this can run on every commit and after any change to a dimension.

    python scripts/summary_ai_selftest.py out/summary_ai/<product>/<job>

What it CANNOT tell you: whether the transcript is faithful to the PDF in ways no
dimension measures. A mutation this file does not model is a blind spot this file cannot
find, so the EXPECTED-MISS cases below are as important as the caught ones -- they are the
holes, written down and asserted to still be holes, so a future change that closes one is
visible as a test that started passing.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import summary_ai_extract as S  # noqa: E402


def score(pdf: Path, pages: list[int], md: str) -> dict:
    """Every SIGNAL the scorecard carries, not only its four percentages.

    The counts are in here because the percentages cannot resolve a small defect and it
    would be self-deceiving to test only the instrument that cannot see it: one wrong word
    in 4,764 is 0.021%, which rounds away at one decimal place. `added_words` sees it,
    scores do not, and the scorecard reports both -- so both are tested.
    """
    ptexts = {p: S._page_text(pdf, p) for p in pages}
    source = S.pdf_text(pdf, pages)
    cov = S.coverage(source, md, ptexts)
    checks = S.attribute_pages(S.parse_tree(md), pdf, pages)
    punct = S.punctuation(source, md, ptexts)
    sc = S.scorecard(cov, S.headings(md), checks, 0,
                     {"seconds": 0, "pages": len(pages), "steps": {}},
                     "selftest", punct, S.page_health(ptexts, md))
    out = {k: v["score"] for k, v in sc["dimensions"].items()}
    # Negated, so "the signal moved in the bad direction" is one comparison for every
    # signal: a score falling and a count rising both read as a drop.
    out["added_words"] = -(cov["out_words"] - cov["matched"])
    out["missing_words"] = -(cov["pdf_words"] - cov["matched"])
    return out


# ---- the mutations. Each returns a broken transcript, or None if it does not apply ------

def drop_paragraph(md: str):
    """A whole paragraph of prose deleted — the defect that matters most."""
    paras = [p for p in md.split("\n\n") if len(p.split()) > 40 and not p.startswith("#")]
    if not paras:
        return None
    return md.replace(paras[len(paras) // 2] + "\n\n", "", 1)


def drop_heading(md: str):
    """A section heading deleted, its prose left in place. The transcript still reads
    fine, which is why nothing but a geometry check finds this."""
    for ln in md.splitlines():
        if ln.startswith("## ") and not ln.startswith("###"):
            return md.replace(ln + "\n", "", 1)
    return None


def duplicate_paragraph(md: str):
    """A paragraph emitted twice — the failure mode of a model that loses its place."""
    paras = [p for p in md.split("\n\n") if len(p.split()) > 40 and not p.startswith("#")]
    if not paras:
        return None
    p = paras[len(paras) // 2]
    return md.replace(p, p + "\n\n" + p, 1)


def invent_sentence(md: str):
    """Text with no source in the PDF, in vocabulary the document does not use."""
    return md.rstrip() + ("\n\nThe zebra octopus tribunal hereby rescinds every "
                          "quixotic marzipan covenant.\n")


def paraphrase(md: str):
    """A real sentence reworded — same subject, different words. The defect the whole
    'transcription not summary' instruction exists to prevent."""
    m = re.search(r"(?m)^([A-Z][^\n#]{120,240}\.)$", md)
    if not m:
        return None
    return md.replace(m.group(1), "This provision applies subject to the usual "
                                  "conditions and should be reviewed with counsel.", 1)


def change_number(md: str):
    """One figure altered — EUR 20 million becomes EUR 90 million."""
    m = re.search(r"\b(\d{1,3}) (million|billion)\b", md)
    if not m:
        return None
    return md.replace(m.group(0), f"9{m.group(1)} {m.group(2)}", 1)


def flatten_quotes(md: str):
    """Curly quotes written straight — the defect actually observed in the wild."""
    out = md.replace("“", '"').replace("”", '"').replace("’", "'")
    return out if out != md else None


def swap_sections(md: str):
    """Two whole sections exchanged. Every word is still present, exactly once."""
    parts = re.split(r"(?m)^(## .*)$", md)
    heads = [i for i in range(1, len(parts), 2)]
    if len(heads) < 3:
        return None
    a, b = heads[1], heads[2]
    parts[a], parts[b] = parts[b], parts[a]
    parts[a + 1], parts[b + 1] = parts[b + 1], parts[a + 1]
    return "".join(parts)


def spurious_heading(md: str):
    """An existing line of prose PROMOTED to a heading.

    Chosen so not a single word is added or removed -- only the markup changes -- which is
    the only way to probe the structure check's asymmetry on its own. Inventing a new
    heading phrase would be caught by fidelity's vocabulary check, and duplicating an
    existing one by its duplication check, so either would pass this test for a reason
    that has nothing to do with the hole being tested.
    """
    for ln in md.splitlines():
        t = ln.strip()
        if 30 < len(t) < 90 and not t.startswith(("#", "-", "[", "*")) and t.endswith("."):
            return md.replace(ln, "## " + t, 1)
    return None


def move_prose_under_wrong_heading(md: str):
    """A paragraph relocated from its own section into the previous one. Every word
    present, every heading present, only the ASSIGNMENT wrong."""
    parts = re.split(r"(?m)^(## .*)$", md)
    heads = [i for i in range(1, len(parts), 2)]
    if len(heads) < 3:
        return None
    body = parts[heads[2] + 1]
    paras = [p for p in body.split("\n\n") if len(p.split()) > 40]
    if not paras:
        return None
    p = paras[0]
    parts[heads[2] + 1] = body.replace(p + "\n\n", "", 1)
    parts[heads[1] + 1] = parts[heads[1] + 1].rstrip() + "\n\n" + p + "\n\n"
    return "".join(parts)


# (name, mutation, dimension that MUST fall, why it matters)
CAUGHT = [
    ("a paragraph deleted",        drop_paragraph,      "completeness",
     "prose silently missing is the defect with the worst consequences"),
    ("a section heading deleted",  drop_heading,        "structure",
     "the prose survives unlabelled, so only the PDF geometry can find this"),
    ("a paragraph duplicated",     duplicate_paragraph, "fidelity",
     "text the PDF prints once appearing twice inflates the document"),
    ("a sentence invented",        invent_sentence,     "fidelity",
     "text with no source is the most serious thing a transcription can do"),
    ("a sentence paraphrased",     paraphrase,          "fidelity",
     "summarising instead of transcribing"),
    # Asserted on the COUNT, not the score, and that is the finding this file was written
    # to produce: at one decimal place a single altered token is invisible to every
    # percentage, so the scorecard reports absolute counts alongside them.
    ("a number changed",           change_number,       "added_words",
     "a wrong figure in a legal summary — invisible to the percentages, which is why the "
     "scorecard reports absolute word counts as findings"),
    ("curly quotes flattened",     flatten_quotes,      "punctuation",
     "the defect observed in the wild"),
]

# Mutations no dimension models. ASSERTED to be missed, so closing one shows up as a test
# that started passing rather than as a silent improvement nobody noticed.
EXPECTED_MISS = [
    ("two sections swapped",       swap_sections,
     "completeness and fidelity are word MULTISETS, deliberately order-blind so the "
     "cover's own broken text-layer order does not condemn a correct transcript. The "
     "price is that order is not checked at all."),
    ("prose promoted to a heading", spurious_heading,
     "structure checks bars -> headings, never headings -> bars, so a heading the PDF "
     "draws no bar for costs nothing. Closing it means asserting every heading in the "
     "transcript sits on a bar — worth doing, and not done."),
    ("prose under the wrong heading", move_prose_under_wrong_heading,
     "nothing scores which SECTION a paragraph landed in — every word and every "
     "heading is still present."),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir", help="a job dir holding source.pdf + transcript.md")
    ap.add_argument("--tolerance", type=float, default=0.05,
                    help="how far a dimension must move to count as 'noticed' (default 0.05)")
    args = ap.parse_args()

    job = Path(args.job_dir).resolve()
    pdf, tr = job / "source.pdf", job / "transcript.md"
    if not (pdf.exists() and tr.exists()):
        print(f"need source.pdf and transcript.md in {job}", file=sys.stderr)
        return 2

    import fitz
    with fitz.open(str(pdf)) as d:
        pages = list(range(1, d.page_count + 1))
    md = tr.read_text()

    base = score(pdf, pages, md)
    print(f"{job.name} — baseline")
    print("   " + "  ".join(f"{k} {v}" for k, v in base.items()))
    print()

    failures, skipped = [], []
    print("MUTATIONS THAT MUST BE CAUGHT")
    for name, fn, dim, why in CAUGHT:
        broken = fn(md)
        if broken is None or broken == md:
            skipped.append(name)
            print(f"  ?  {name:32} n/a on this document")
            continue
        got = score(pdf, pages, broken)
        drop = base[dim] - got[dim]
        moved = {k: round(base[k] - got[k], 2) for k in got if base[k] - got[k] > args.tolerance}
        ok = drop > args.tolerance
        print(f"  {'OK ' if ok else 'MISS'} {name:32} {dim} {base[dim]} -> {got[dim]}"
              f"  (-{drop:.2f}){'' if ok else '   <-- NOT NOTICED'}")
        if moved and list(moved) != [dim]:
            print(f"       also moved: {moved}")
        if not ok:
            failures.append((name, dim, why))

    print()
    print("KNOWN BLIND SPOTS — asserted to be missed")
    closed = []
    for name, fn, why in EXPECTED_MISS:
        broken = fn(md)
        if broken is None or broken == md:
            print(f"  ?  {name:32} n/a on this document")
            continue
        got = score(pdf, pages, broken)
        moved = {k: round(base[k] - got[k], 2) for k in got if base[k] - got[k] > args.tolerance}
        if moved:
            closed.append((name, moved))
            print(f"  !  {name:32} NOW CAUGHT: {moved}  <-- update EXPECTED_MISS")
        else:
            print(f"  -  {name:32} not detected, as documented")
            print(f"       {why}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} mutation(s) a dimension should have caught and did not")
        for n, d, w in failures:
            print(f"  - {n}: {d} did not move. {w}")
        return 1
    print(f"every modelled defect was caught ({len(CAUGHT) - len(skipped)} of {len(CAUGHT)} "
          f"applicable)" + (f"; {len(skipped)} n/a" if skipped else ""))
    if closed:
        print(f"{len(closed)} documented blind spot(s) are now covered — tighten EXPECTED_MISS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
