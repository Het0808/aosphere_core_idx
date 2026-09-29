"""A bad outline on a FLAT product is repaired by rebuilding, not by pruning.

rescue_outline has two repairs for "an outline made of body text". They agree about which entries
are real headings and disagree about the LEVELS of the survivors:

  prune  deletes the prose entries and keeps the embedded outline's own nesting — _legal_levels
         shifts levels and caps jumps, it does not re-derive hierarchy.
  text   parses the contents page the document PRINTS and rebuilds, so levels come from the
         printed structure.

On 124 Marketing Restrictions that difference decides the chunking, because SECTION_DEPTH is 0:
one file per LEVEL-1 heading. This product's embedded outlines put "6.1" and "8.9" at level 1, as
siblings of "6." and "8.", so pruning emits them as top-level chunks and the level-1 boundaries
land INSIDE the page-spanning questionnaire tables that SECTION_DEPTH 0 exists to keep whole.

Measured on Australia__183509, same PDF, same day:
    prune           335 -> 52 entries, levels {1:35, 2:13, 3:3, 4:1}, 34 files, 37 tables, uniq 71.8
    text (rebuild)   41 entries, 41 verified, span 96.7%, levels {1:18, 2:23}, 28 files, 16, uniq 90.5

This is a restoration. The product was extracted on a branch with no prune engine at all, where
every repair went through the rebuild — Bermuda__166524 scored pass 92.1 with levels {1:16, 2:23}.
27 of the product's 105 documents have a prose-heavy embedded outline and would otherwise switch
engines, ADGM__170680 among them, which is the document the printed-TOC rescue was written for.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from product_rules import (  # noqa: E402
    REBUILD_OUTLINE_FROM_PRINTED_TOC,
    rebuild_outline_from_printed_toc,
    section_depth,
)

MRAM = "124_Marketing_Restrictions_-_Asset_Management"


def test_the_flat_product_rebuilds_rather_than_prunes():
    assert rebuild_outline_from_printed_toc(MRAM)


@pytest.mark.parametrize("product", [
    "155_Data_Privacy",
    "104_Shareholding_Disclosure",
    "170_Data_Privacy_(US_States)",
    "unknown_product",
    "",
    None,
])
def test_every_other_product_keeps_the_prune_engine(product):
    """The exemption must not become global — pruning is cheaper and keeps offsets elsewhere."""
    assert not rebuild_outline_from_printed_toc(product)


def test_it_is_the_flat_product_that_needs_this():
    """The rule only makes sense where a wrong LEVEL is a wrong CHUNK, i.e. depth 0."""
    for product in REBUILD_OUTLINE_FROM_PRINTED_TOC:
        assert section_depth(product) == 0, (
            f"{product} rebuilds from its printed TOC but is not flat; the reason this rule "
            "exists is that at depth 0 one file is emitted per level-1 heading")


def test_the_preflight_asks_the_rule_which_engine_to_use():
    """A regression guard on the wiring: run_corpus must pass no_prune, not default it."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_corpus.py").read_text()
    assert "rebuild_outline_from_printed_toc" in src
    assert "no_prune=_no_prune" in src


def test_every_rescue_call_site_asks_the_rule():
    """The pre-flight is not the only caller, and one that forgets undoes the other.

    Measured: the pre-flight rebuilt Australia__183509 correctly (41 entries, 22 files, 16
    tables), then fallback_chain's toc_rescue tier called rescue() without no_prune, pruned,
    and its result was ADOPTED — 52 entries, 34 files, 37 tables, "6.1" back at top level.
    """
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for name in ("run_corpus.py", "fallback_chain.py"):
        src = (scripts / name).read_text()
        for i, line in enumerate(src.splitlines()):
            if "rescue(" not in line or "def " in line or line.lstrip().startswith("#"):
                continue
            if "rescue_outline.rescue(" not in line and "ro.rescue(" not in line:
                continue
            window = "\n".join(src.splitlines()[i:i + 6])
            assert "no_prune" in window, (
                f"{name}:{i + 1} calls rescue() without asking "
                "rebuild_outline_from_printed_toc which engine this product wants")


# ---- the pre-flight now covers documents with NO outline at all -------------------------------

def test_the_preflight_no_longer_excludes_a_document_with_no_outline():
    """A 0-entry PDF must reach rescue(), not be returned on before it is looked at.

    It used to bail at `n_entries == 0` on the grounds that Stage 1's font-size heuristic
    handles those. It does — but the `toc` dimension scores such a document 55, +10 when its
    printed contents page verifies = 65, against a chain-entry bar of 70. So it escalated
    anyway, and the chain's TOC tier re-ran Stage 1-3 INCLUDING MinerU to do what the
    pre-flight does for the cost of one Stage 1. Measured on
    154_Marketing_Restrictions/Australia__181815: 56.9 min, of which 26.1 was that duplicate
    tier, for a tree the pre-flight reaches directly.
    """
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_corpus.py").read_text()
    body = src[src.index("def _preflight_outline"):src.index("def run_one")]
    stripped = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    assert "n_entries == 0" not in stripped, (
        "the pre-flight bails on documents with no outline again — the case it exists to "
        "catch cheapest is exactly the one that otherwise pays for MinerU twice")


def test_the_preflight_still_leaves_a_healthy_outline_alone():
    """The saving comes from doing LESS, so most of the corpus must still pay nothing."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_corpus.py").read_text()
    body = src[src.index("def _preflight_outline"):src.index("def run_one")]
    assert "if not suspect and not overseg and n_entries > USELESS_OUTLINE_MAX_ENTRIES" in body


def test_an_unverifiable_printed_toc_is_what_makes_the_unconditional_repair_safe():
    """The pre-flight adopts without comparing scores, so rescue() must be the guard.

    Every failure path -- no printed contents page, nothing parsed, a parse that does not
    check out against the pages it names -- has to return BEFORE a repaired PDF is written,
    so `repaired_pdf` missing is the signal the caller treats as "leave this document alone".
    """
    src = (Path(__file__).resolve().parent.parent / "scripts" / "rescue_outline.py").read_text()
    body = src[src.index("def rescue("):src.index("def _rerun_and_maybe_promote")]
    # Scoped to the PRINTED-TOC path. rescue() has a second, earlier repair -- pruning prose
    # entries out of an outline that already exists -- which writes a repaired PDF without
    # parsing a contents page at all, because it is not reconstructing anything. A document
    # with no outline never takes it: prune_prose_entries needs "body text" in the triage
    # reason, and 0 entries reports "no bookmark outline at all".
    printed = body[body.index("toc_pages = find_toc_pages(doc)"):]
    guard = printed.index('if not check["ok"]:')
    write = printed.index('res["repaired_pdf"]')
    assert guard < write, "rescue() can write a repaired PDF from a TOC that did not verify"

    caller = (Path(__file__).resolve().parent.parent / "scripts" / "run_corpus.py").read_text()
    pf = caller[caller.index("def _preflight_outline"):caller.index("def run_one")]
    assert "if not repaired or not Path(repaired).exists():" in pf


def test_the_chain_holds_no_printed_toc_tier_at_all():
    """The duplicate MinerU pass this file exists to prevent is now impossible by
    construction, not by a marker check.

    The tier used to sit at the head of the chain and re-extract Stages 1-3 -- MinerU
    included -- against the printed contents page. It was made to stand down whenever the
    pre-flight had already rebuilt the outline, because otherwise the second MinerU pass
    came straight back through the chain. Standing down was all it ever did: over 148
    scored documents, 30 entered the chain and it did work on none of them and was adopted
    on none. Its one remaining path -- a healthy outline on a document that escalated for
    another reason -- rebuilds from the printed page the pre-flight has already confirmed
    AGREES with that outline, reproducing what it started with.

    So it is gone, and the worst case is fixed at two MinerU passes: the normal Stage 2,
    then MinerU full.
    """
    src = (Path(__file__).resolve().parent.parent / "scripts" / "fallback_chain.py").read_text()
    assert "_toc_rescue" not in src
    assert "rerun=True" not in src, "nothing in the chain may re-extract for an outline repair"
    # the pre-flight is the only place the printed contents page is now consulted
    run_corpus = (Path(__file__).resolve().parent.parent
                  / "scripts" / "run_corpus.py").read_text()
    assert "_preflight_outline" in run_corpus
