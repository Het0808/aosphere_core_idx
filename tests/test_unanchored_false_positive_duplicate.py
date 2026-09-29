"""An unanchored false-positive region must render NOTHING, not a copy of its page.

pdf2mdtree withholds the body text inside a deferred table's footprint, because
MinerU is expected to render it. When MinerU then says "not a table", Stage 2
recovers that withheld text as prose — the only way those words reach the reader.

An UNANCHORED placeholder is the opposite case. pdf2mdtree creates it under
`if not page_has_deferred_table:` — the very flag gating that skip — so nothing was
withheld and Stage 1 already emitted the whole page. Recovering prose there appended
a SECOND copy: Bahamas section 2 ("2 GUIDELINES FOR COMPLETING PART B") shipped its
twelve (a)-(l) guidelines twice, the reprise still carrying the `aosphere` /
`CONFIDENTIAL` page furniture MinerU keeps and Stage 1 strips. It survived Stage 4
and reached the subchunks, so the section was indexed and embedded twice. Measured
over out/: 321 regions across 80 of 105 MRAM runs, including the cover page.

The fix suppresses the EMISSION only. Every verdict — ok / continuation / absorbed /
failed / false-positive — is reached exactly as before, so a region MinerU returned
nothing usable for is still reported as a failure rather than silently swallowed.
"""
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "hybrid_extract", Path(__file__).resolve().parents[1] / "scripts" / "hybrid_extract.py")
hx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hx)

PAGE_TEXT = "\n\n".join([
    "Please check the boxes below to confirm you have followed each of these guidelines:",
    "(a) let us know if there are upcoming legislative changes;",
    "(l) cross-refer to answers to Part A of this Memorandum.",
])
# What blocks_to_prose would hand back for that page — Stage 1's text plus the running
# header it does not strip. This is the string that used to be pasted in a second time.
RECOVERED = "aosphere\n\nCONFIDENTIAL\n\n2. GUIDELINES FOR COMPLETING PART B\n\n" + PAGE_TEXT


def _tree(tmp_path, body):
    """A one-section Stage 1 tree holding a table placeholder and a page snapshot."""
    s1 = tmp_path / "01_stage1_extract"
    (s1 / "_assets").mkdir(parents=True)
    (s1 / "02-guidelines.md").write_text(body)
    (s1 / "tables_manifest.json").write_text(json.dumps({"tables": [{"table_id": "table_002"}]}))
    return s1


BODY = (f"# 2 GUIDELINES FOR COMPLETING PART B\n\n{PAGE_TEXT}\n\n"
        "> Page 12 of the source PDF contains a complex table/diagram; snapshot for reference:\n\n"
        "![Page 12](_assets/page-012.png)\n\n"
        "<!-- TABLE:table_002 -->\n"
        "> **[TABLE PENDING: table_002 — page 12 — awaiting Stage 2 MinerU extraction]**\n"
        "<!-- /TABLE:table_002 -->\n")

DUPLICATE = {"table_id": "table_002", "pages": [12], "ok": False,
             "source": "snapshot_fallback_unanchored",
             "false_positive": True, "stage1_duplicate": True,
             "suppressed_chars": len(RECOVERED)}

PROSE = {"table_id": "table_002", "pages": [12], "ok": False,
         "source": "snapshot_fallback_anchored",
         "false_positive": True, "prose": RECOVERED}


def _run(tmp_path, status):
    s1 = _tree(tmp_path, BODY)
    report = hx.run_stage3(s1, tmp_path / "02_stage2", tmp_path / "03_stage3",
                           {"tables": [status]})
    return (tmp_path / "03_stage3" / "02-guidelines.md").read_text(), report


def test_the_page_is_not_emitted_twice(tmp_path):
    out, _ = _run(tmp_path, DUPLICATE)
    for line in ("Please check the boxes below", "(a) let us know", "(l) cross-refer"):
        assert out.count(line) == 1, f"{line!r} appears {out.count(line)}x — page duplicated"
    assert "CONFIDENTIAL" not in out, "MinerU's page furniture leaked into the tree"


def test_nothing_is_rendered_in_the_placeholder_s_place(tmp_path):
    out, _ = _run(tmp_path, DUPLICATE)
    assert "<!-- TABLE:" not in out, "placeholder left unresolved"
    for marker in ("TABLE PENDING", "EXTRACTION FAILED", "REGION ABSORBED"):
        assert marker not in out, f"{marker} — a false positive must not read as a defect"


def test_the_page_snapshot_goes_with_it(tmp_path):
    out, _ = _run(tmp_path, DUPLICATE)
    assert "page-012.png" not in out
    assert "complex table/diagram" not in out


def test_stage1_s_own_text_is_left_intact(tmp_path):
    out, _ = _run(tmp_path, DUPLICATE)
    assert out.startswith("# 2 GUIDELINES FOR COMPLETING PART B")
    assert "(l) cross-refer to answers to Part A of this Memorandum." in out


def test_it_is_counted_and_not_reported_as_a_failure(tmp_path):
    _out, report = _run(tmp_path, DUPLICATE)
    assert report["tables_stage1_duplicate"] == 1
    assert report["tables_failed"] == 0 and report["failed_table_ids"] == []
    assert report["tables_reclassified"] == 0, "distinct from prose recovery — count it apart"


def test_anchored_prose_recovery_still_emits_its_text(tmp_path):
    """The regression guard: an ANCHORED false positive is the case where Stage 1 DID
    withhold the text, so suppressing it there would delete content outright."""
    out, report = _run(tmp_path, PROSE)
    assert "CONFIDENTIAL" in out, "recovered prose must still be emitted for anchored regions"
    assert report["tables_reclassified"] == 1
    assert report["tables_stage1_duplicate"] == 0
