"""Recovering documents whose outline carried no part letters.

`derive_key` reads a clause's part letter off the top-level folder (`A-substantial-
shareholding`). Plenty of outlines have no letters — the tree is
`02-iceland/01-background-to-tda-disclosure-rules/1.1-….md` — so no key could be derived and
the ENTIRE document was skipped without a word: 590,706 in Shareholding Disclosure and
434,570 in Data Privacy, including 98.5% of Italy Data Privacy and 100% of Iceland.

The letter cannot be guessed from position: in some documents SD part E (short selling) is
the only part present, and counting from the left calls it A. It CAN be learned, because ~100
SD and ~75 DP trees do carry letters and use the same folder names. These tests pin the
learning, the resolution, and the three ways it went wrong while being built:

  * a loose FILE never matched the lexicon (".md" was left on the name) and sorted after every
    folder (a file has no "first page" under it), so every one of them inherited the LAST
    part of the document — part I of Italy was filed under K;
  * folding the letter into the section folder (`B-02-organisation/…`) discarded the section
    ordinal, and then every section's `00-overview.md` in a part derived the bare part key
    and collided — ten of them in Italy;
  * a `78-glossary.md` was swallowed as clause `E78`, taking a key that `extra_key` labels
    properly as GLOSSARY.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import part_lexicon as L  # noqa: E402
import tree_to_content as T  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent / "out" / "corpus"


# ---- the lexicon -------------------------------------------------------------

def test_norm_compares_folders_files_and_continuations_alike():
    assert L.norm("A-substantial-shareholding") == "substantial-shareholding"
    assert L.norm("01-a-substantial-shareholding") == "substantial-shareholding", \
        "the ordinal+letter form names the same part"
    assert L.norm("03-security") == "security"
    assert L.norm("03-security.md") == "security", "a loose file names the same section"
    assert L.norm("05-data-sharing-2.md") == "data-sharing", "a split continuation"


def _tree(root: Path, files: list[str]) -> Path:
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {p.stem}\n\n*Source: `source.pdf`, page 1*\n\n"
                     f"Body text for {p.stem}, long enough to survive the minimum length.\n",
                     encoding="utf-8")
    return root


def _product(tmp_path: Path, n_lettered: int = 5) -> Path:
    """A product folder with `n_lettered` trees that DO carry letters, to learn from."""
    product = tmp_path / "104_Shareholding_Disclosure"
    for i in range(n_lettered):
        _tree(product / f"Country{i}__{i}" / "03_stage3_final", [
            "A-substantial-shareholding/01-background/1.1-a.md",
            "A-substantial-shareholding/08-how-to-make-a-disclosure/8.1-a.md",
            "B-sensitive-industries/01-restrictions-on-investment/1.1-b.md",
            "B-sensitive-industries/03-how-to-make-a-disclosure/3.1-b.md",
            "E-short-selling/01-introduction/1.1-e.md",
        ])
    return product


def test_the_lexicon_learns_part_names_from_the_trees_that_carry_them(tmp_path):
    lex = L.build(_product(tmp_path))
    assert lex["lettered_trees"] == 5
    assert lex["parts"]["substantial-shareholding"] == "A"
    assert lex["parts"]["sensitive-industries"] == "B"
    assert lex["parts"]["short-selling"] == "E"


def test_a_section_name_used_by_two_parts_is_left_unresolved(tmp_path):
    """"how-to-make-a-disclosure" ends both A and B here (five parts in the real corpus), so
    the corpus cannot say which it is — and a coin flip would file content under the wrong
    part. Unambiguous section names are kept."""
    lex = L.build(_product(tmp_path))
    assert lex["sections"]["background"] == "A"
    assert lex["sections"]["restrictions-on-investment"] == "B"
    assert "how-to-make-a-disclosure" not in lex["sections"]


def test_a_part_name_beats_a_section_name(tmp_path):
    lex = L.build(_product(tmp_path))
    lex["sections"]["short-selling"] = "A"          # contrived conflict
    assert L.letter_for(lex, "01-short-selling") == "E"


def test_the_lexicon_is_cached_beside_the_trees(tmp_path):
    """The converter runs as one subprocess per document; rescanning the product for each
    would dominate the build."""
    product = _product(tmp_path)
    L.load(product)
    assert (product / L.CACHE_NAME).exists()
    cached = json.loads((product / L.CACHE_NAME).read_text())
    assert cached["parts"]["short-selling"] == "E"


# ---- resolving one tree ------------------------------------------------------

def test_a_tree_that_already_has_letters_is_left_alone(tmp_path):
    product = _product(tmp_path)
    root = product / "Country0__0" / "03_stage3_final"
    assert T.infer_structure(str(root)) == (None, {})


def test_a_wrapper_that_is_itself_a_part_keeps_its_own_letter(tmp_path):
    """Croatia 181018: the whole document is part E, and counting from the left says A."""
    product = _product(tmp_path)
    root = _tree(product / "Croatia__1" / "03_stage3_final", [
        "01-short-selling/01-introduction/1.1-scope.md",
        "01-short-selling/02-shares/2.1-what-shares.md",
    ])
    wrapper, letters = T.infer_structure(str(root))
    assert wrapper is None
    assert letters["01-short-selling"] == ("E", True)
    assert T.derive_key(T.canonical_rel("01-short-selling/02-shares/2.1-what-shares.md",
                                        wrapper, letters)) == "E2.1"


def test_a_wrapper_named_after_the_jurisdiction_is_descended_through(tmp_path):
    """Iceland 180630: `02-iceland/` hides the real level; its children are sections of A,
    and restrictions-on-investment starts B."""
    product = _product(tmp_path)
    root = _tree(product / "Iceland__2" / "03_stage3_final", [
        "02-iceland/01-background/1.1-eu-rules.md",
        "02-iceland/01-restrictions-on-investment/1.1-sectors.md",
    ])
    wrapper, letters = T.infer_structure(str(root))
    assert wrapper == "02-iceland"
    assert letters["01-background"] == ("A", False)
    assert letters["01-restrictions-on-investment"] == ("B", False)
    assert T.derive_key(T.canonical_rel("02-iceland/01-background/1.1-eu-rules.md",
                                        wrapper, letters)) == "A1.1"


def test_a_section_keeps_its_ordinal_so_two_overviews_do_not_collide(tmp_path):
    """Folding the letter into the section folder lost the ordinal, and every `00-overview.md`
    in a part then derived the bare part key."""
    product = _product(tmp_path)
    root = _tree(product / "Iceland__3" / "03_stage3_final", [
        "02-iceland/01-background/00-overview.md",
        "02-iceland/02-issuers-and-markets/00-overview.md",
    ])
    wrapper, letters = T.infer_structure(str(root))
    keys = {T.derive_key(T.canonical_rel(f"02-iceland/{d}/00-overview.md", wrapper, letters))
            for d in ("01-background", "02-issuers-and-markets")}
    assert len(keys) == 2, f"both overviews derived the same key: {keys}"


def test_a_file_that_states_its_own_part_is_not_overridden(tmp_path):
    """`I-changes-in-regulation.md` is part I in one file. Inheritance filed it under K, and
    derive_key had no rule for a file that IS a part, so it was dropped from most DP docs."""
    assert T.derive_key("I-changes-in-regulation.md") == "I"
    product = _product(tmp_path)
    root = _tree(product / "Loose__4" / "03_stage3_final", [
        "02-jur/01-background/1.1-a.md",
        "02-jur/I-changes-in-regulation.md",
    ])
    wrapper, letters = T.infer_structure(str(root))
    assert "I-changes-in-regulation.md" not in letters
    assert T.derive_key(T.canonical_rel("02-jur/I-changes-in-regulation.md",
                                        wrapper, letters)) == "I"


def test_a_glossary_is_left_to_extra_key_not_taken_as_a_clause(tmp_path):
    """`78-glossary.md` became clause "E78" — a real key, wrong meaning, and it lost the
    GLOSSARY label the reader groups extras under."""
    product = _product(tmp_path)
    root = _tree(product / "Gloss__5" / "03_stage3_final", [
        "02-jur/01-background/1.1-a.md",
        "02-jur/78-glossary.md",
    ])
    wrapper, letters = T.infer_structure(str(root))
    assert "78-glossary.md" not in letters
    canon = T.canonical_rel("02-jur/78-glossary.md", wrapper, letters)
    assert T.derive_key(canon) is None
    assert T.extra_key("02-jur/78-glossary.md", {}) == "GLOSSARY"


def test_a_loose_file_inherits_the_part_open_at_its_page(tmp_path):
    """A section whose part folder the outline never created. Ordering is by SOURCE PAGE:
    with files treated as pageless they all sorted last and inherited the final part."""
    product = _product(tmp_path)
    root = product / "Loose__6" / "03_stage3_final"
    _tree(root, ["02-jur/01-background/1.1-a.md",
                 "02-jur/01-restrictions-on-investment/1.1-b.md"])
    (root / "02-jur" / "01-background" / "1.1-a.md").write_text(
        "# 1.1\n\n*Source: `source.pdf`, page 5*\n\nBody long enough to be kept here.\n")
    (root / "02-jur" / "01-restrictions-on-investment" / "1.1-b.md").write_text(
        "# 1.1\n\n*Source: `source.pdf`, page 90*\n\nBody long enough to be kept here.\n")
    (root / "02-jur" / "04-aggregation-of-holdings.md").write_text(
        "# 4\n\n*Source: `source.pdf`, page 40*\n\nBody long enough to be kept here.\n")
    wrapper, letters = T.infer_structure(str(root))
    # page 40 falls after background (5) and before restrictions (90) -> part A
    assert letters["04-aggregation-of-holdings.md"][0] == "A"


def test_nothing_is_inferred_outside_a_corpus_layout(tmp_path):
    """No product folder to learn from -> the old behaviour, not a guess."""
    root = _tree(tmp_path / "loose-tree", ["02-jur/01-background/1.1-a.md"])
    assert T.infer_structure(str(root)) == (None, {})


# ---- build ------------------------------------------------------------------

def test_fragments_of_one_clause_are_merged_not_dropped(tmp_path):
    """The extractor splits one clause across files when the source repeats its heading:
    Italy's 1.1 arrives from pages 195, 210-211, 243-244 and 253. All four were embedded but
    sections_by_key keeps ONE row per key, so the reader saw a fragment."""
    product = _product(tmp_path)
    root = product / "Frag__7" / "03_stage3_final"
    d = root / "02-jur" / "01-background"
    d.mkdir(parents=True)
    for i, extra in enumerate(("", "-2", "-a3")):
        (d / f"1.1-what-laws-apply{extra}.md").write_text(
            f"# 1.1 What laws apply\n\n*Source: `source.pdf`, page {195 + i * 20}*\n\n"
            f"Fragment number {i} of this clause, with distinct wording {'x' * 40}.\n",
            encoding="utf-8")
    out = tmp_path / "out.content.json"
    T.build(str(out), str(root), "Frag", "Shareholding Disclosure")
    secs = json.loads(out.read_text())["sections"]
    a11 = [s for s in secs if s["key"] == "A1.1"]
    assert len(a11) == 1, "one clause, one section"
    text = " ".join(e.get("text", "") for e in a11[0]["elements"])
    for i in range(3):
        assert f"Fragment number {i}" in text, "every fragment must survive the merge"


def test_two_different_sections_on_one_key_stay_separate(tmp_path):
    """The opposite case: distinct sections must not absorb each other's text."""
    product = _product(tmp_path)
    root = product / "Clash__8" / "03_stage3_final"
    _tree(root, ["02-jur/01-background/1.1-first.md",
                 "02-jur/01-restrictions-on-investment/1.1-second.md"])
    # force both folders onto part A so their 1.1 files collide
    out = tmp_path / "clash.content.json"
    T.build(str(out), str(root), "Clash", "Shareholding Disclosure")
    secs = json.loads(out.read_text())["sections"]
    bodies = [" ".join(e.get("text", "") for e in s["elements"]) for s in secs]
    assert len({s["key"] for s in secs}) == len(secs), "keys must stay unique"
    assert sum("1.1-first" in b or "first" in b for b in bodies) >= 1
    assert not any(("first" in b and "second" in b) for b in bodies), \
        "unrelated sections must not be merged into one body"


# ---- the real corpus (skipped when it is not present) ------------------------

@pytest.mark.skipif(not (CORPUS / "104_Shareholding_Disclosure").exists(),
                    reason="corpus not present")
@pytest.mark.parametrize("job,expect_key", [
    ("104_Shareholding_Disclosure/Croatia__181018", "E1.1"),
    ("104_Shareholding_Disclosure/Iceland__180630", "A1.1"),
    ("155_Data_Privacy/Italy__96416", "H1.1"),
])
def test_the_documents_that_were_empty_now_key_to_real_letters(job, expect_key):
    """Croatia is part E (short selling), Iceland's part A, Italy's breach response is H —
    the letters a lettered tree of the same product uses, not positional ones."""
    root = CORPUS / job / "03_stage3_final"
    if not root.exists():
        pytest.skip(f"{job} not extracted")
    wrapper, letters = T.infer_structure(str(root))
    keys = set()
    for p in root.rglob("*.md"):
        rel = os.path.relpath(p, root)
        canon = T.canonical_rel(rel, wrapper, letters) if (wrapper or letters) else rel
        k = T.derive_key(canon)
        if k:
            keys.add(k)
    assert expect_key in keys, f"{job}: {expect_key} missing from {sorted(keys)[:12]}"
