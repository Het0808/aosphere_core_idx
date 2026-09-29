"""A product whose sections are files, not folders, must still reach the index.

derive_key reads a clause key out of the lettered path a Data Privacy or Shareholding Disclosure
tree has — `<L>-part/NN-section/N.M-clause.md`. Sixteen of the thirty-seven extracted products
have no such nesting at all: product_rules gives them SECTION_DEPTH 0, so every section is a file
at the tree root (`15-marketing-activities.md`). derive_key returns None for each one, and because
build() skips a file it cannot key, those products converted to ZERO rows and silently never
reached search — measured on Marketing Restrictions - Asset Management, 88 documents and 3.4M
words of content.

The fix keys a flat section by the document's OWN ordinal (S15 for `15-…`), which is stable across
a re-extraction that renames the slug. Nothing downstream changes: a long section is still split
into `<key>#c<i>` windows by the embedder and collapsed back to the whole clause at query time,
exactly as a nested product's clause is.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tree_to_content as T  # noqa: E402

BODY = ("This clause states the applicable requirement in enough words to survive the "
        "minimum-body filter, which drops anything under twenty characters. " * 4)


def _write(tree: Path, rel: str, heading: str, body: str | None = None) -> None:
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    # The body must be DISTINCT per section: build() drops an exact-ish duplicate body on purpose
    # (the extractor repeats a heading's text across pages), so a fixture reusing one string would
    # be testing the dedupe rather than the keying.
    body = f"{heading}. {BODY}" if body is None else body
    p.write_text(f"# {heading}\n\n*Source: `source.pdf`, page 3*\n\n{body}\n")


def _convert(tree: Path, out: Path, product="Marketing Restrictions - Asset Management",
             jur="Austria") -> dict:
    r = subprocess.run([sys.executable, str(SCRIPTS / "tree_to_content.py"), str(out), str(tree),
                        f"{product}::{jur}", product], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text())


def test_a_tree_of_files_is_flat_and_a_tree_of_folders_is_not(tmp_path):
    flat = tmp_path / "flat"
    _write(flat, "01-background.md", "Background")
    _write(flat, "15-marketing-activities.md", "Marketing activities")
    assert T.is_flat_tree(str(flat)) is True

    nested = tmp_path / "nested"
    _write(nested, "a-part/01-section/1.1-clause.md", "A clause")
    assert T.is_flat_tree(str(nested)) is False


def test_page_assets_do_not_make_a_tree_look_nested(tmp_path):
    """Every tree has an _assets/ directory of page images; only .md files decide the shape."""
    tree = tmp_path / "t"
    _write(tree, "01-background.md", "Background")
    (tree / "_assets").mkdir()
    (tree / "_assets" / "page-3.png").write_bytes(b"\x89PNG")
    assert T.is_flat_tree(str(tree)) is True


@pytest.mark.parametrize("name, expect", [
    ("15-marketing-activities.md", "S15"),
    ("01-background.md", "S01"),
    ("22-disclaimers-other.md", "S22"),
    ("07_part_b_contents.md", "S07"),
])
def test_the_key_is_the_documents_own_section_ordinal(name, expect):
    """Taken from the file's ordinal, not a running counter, so a re-extraction that renames the
    slug leaves the key — and any citation of it — intact."""
    assert T.flat_key(name, 99) == expect


def test_a_file_without_an_ordinal_still_gets_a_key():
    assert T.flat_key("marketing-activities.md", 4) == "S04"


def test_only_root_level_files_are_keyed_this_way():
    """A nested path must fall through to derive_key, which understands part letters."""
    assert T.flat_key("a-part/01-section/1.1-clause.md", 3) is None


def test_a_flat_product_converts_to_one_section_per_file(tmp_path):
    tree = tmp_path / "tree"
    _write(tree, "08-background.md", "Background")
    _write(tree, "13-marketing-selling-to-the-public.md", "Marketing/Selling to the Public")
    _write(tree, "14-private-placement-regime.md", "Private Placement Regime")
    doc = _convert(tree, tmp_path / "out.json")

    keys = [s["key"] for s in doc["sections"]]
    assert keys == ["S08", "S13", "S14"], keys
    titles = [s["title"] for s in doc["sections"]]
    assert "Private Placement Regime" in titles
    # Flat sections are top-level: no part letter to parent them to.
    assert {s["level"] for s in doc["sections"]} == {1}
    assert {s.get("parent") for s in doc["sections"]} == {None}


def test_an_appendix_keeps_its_own_label_in_a_flat_tree(tmp_path):
    """extra_key still wins where it applies, so an appendix does not become S23."""
    tree = tmp_path / "tree"
    _write(tree, "08-background.md", "Background")
    _write(tree, "23-appendix-1-forms.md", "Appendix 1 — Forms")
    doc = _convert(tree, tmp_path / "out.json")
    keys = [s["key"] for s in doc["sections"]]
    assert "S08" in keys
    assert any(k.startswith("APPENDIX") for k in keys), keys


def test_a_nested_product_is_unaffected(tmp_path):
    """The DP/SD path must key exactly as before — this change adds a fallback, not a rewrite."""
    tree = tmp_path / "tree"
    _write(tree, "a-substantial-shareholdings/01-thresholds/1.1-notification.md", "Notification")
    _write(tree, "a-substantial-shareholdings/01-thresholds/1.2-timing.md", "Timing")
    doc = _convert(tree, tmp_path / "out.json", product="Shareholding Disclosure", jur="France")
    keys = [s["key"] for s in doc["sections"]]
    assert keys and all(not k.startswith("S") for k in keys), keys
    assert any(k.startswith("A1") for k in keys), keys


def test_a_cover_page_split_into_stub_headings_does_not_produce_sections(tmp_path):
    """Some documents' outlines start with the title broken across lines ("RESTRICTIONS ON
    CROSS-BORDER" / "MARKETING AND SELLING OF" / "INTO" / "ARGENTINA"). Those files hold a few
    words each and must not arrive as clauses — the body-length floor is what drops them."""
    tree = tmp_path / "tree"
    _write(tree, "01-restrictions-on-cross-border.md", "RESTRICTIONS ON CROSS-BORDER", body="of")
    _write(tree, "04-into.md", "INTO", body="x")
    _write(tree, "08-background.md", "Background")
    doc = _convert(tree, tmp_path / "out.json")
    assert [s["key"] for s in doc["sections"]] == ["S08"]
