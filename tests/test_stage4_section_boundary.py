"""A stage-4 reply may contain its own section and nothing else.

Two short sections routinely share a page, so a section's ground-truth images can show
the whole of its neighbour — heading and all. The prompt forbids transcribing it;
these tests are what happens when the prompt loses. Measured on the 77-document MRAM
run, it loses on 7 of 1036 sections, five of them the same short "Disclaimers:
Closed-Ended Fund" appendix whose successor begins on its own last page.
"""

from aosphere_core_index.extract.ai_postprocess import (
    _trim_foreign_sections,
    accept_text_section,
)

# The real shape, from Chile 169565: the reply opens with its OWN heading (correctly
# folding the orphaned "APPENDIX 2" label in, as the prompt asks) and then carries
# straight on into Appendix 3.
BEFORE = """# 2 Disclaimers: Closed-Ended Fund

*Source: `source_repaired.pdf`, page 135-136*

**A: WRITTEN DISCLAIMERS**

Appendix 1(A) applies.
"""

AFTER_CONTAMINATED = """# Appendix 2 Disclaimers: Closed-Ended Fund

*Source: `source_repaired.pdf`, page 135-136*

**A: WRITTEN DISCLAIMERS**

Appendix 1(A) applies.

---

# Appendix 3 Disclaimers: Investment Management & Advisory Services

*Source: `source_repaired.pdf`, page 136*

**A: WRITTEN DISCLAIMERS**

If marketing material is distributed under the Offshore Basis, the following applies.
"""


def test_the_identity_check_alone_does_not_catch_this():
    """Why this guard has to exist: the reply's FIRST heading is correct, so the
    accept check's "did you answer a different section?" test passes."""
    ok, why = accept_text_section(BEFORE, AFTER_CONTAMINATED)
    assert ok, why
    assert "added" in why


def test_foreign_section_is_trimmed():
    out, foreign = _trim_foreign_sections(BEFORE, AFTER_CONTAMINATED)
    assert foreign == "# Appendix 3 Disclaimers: Investment Management & Advisory Services"
    assert "Appendix 3" not in out
    assert "Offshore Basis" not in out


def test_the_section_keeps_its_own_repair():
    """Trimming, not rejecting: the work above the cut is what the call was paid for."""
    out, _ = _trim_foreign_sections(BEFORE, AFTER_CONTAMINATED)
    assert out.splitlines()[0] == "# Appendix 2 Disclaimers: Closed-Ended Fund"
    assert "**A: WRITTEN DISCLAIMERS**" in out
    assert "Appendix 1(A) applies." in out


def test_trimmed_reply_then_passes_as_clean():
    """The whole point of trimming BEFORE the accept check: drift is measured against
    the section the model was actually asked for."""
    ok_before, why_before = accept_text_section(BEFORE, AFTER_CONTAMINATED)
    out, _ = _trim_foreign_sections(BEFORE, AFTER_CONTAMINATED)
    ok, why = accept_text_section(BEFORE, out)
    assert ok
    # accept_text_section measures a CHARACTER stream, so the only drift left is the
    # eight letters of "Appendix" — the heading rewrite the prompt explicitly asks for.
    # The contaminated reply reported 185 added against the same section.
    assert why == "text 0 lost / 8 added — reported, not enforced", (why_before, why)
    assert "185 added" in why_before


def test_a_clean_reply_is_untouched():
    clean = BEFORE.replace("# 2 ", "# Appendix 2 ")
    out, foreign = _trim_foreign_sections(BEFORE, clean)
    assert foreign is None
    assert out == clean


def test_subheadings_are_not_boundaries():
    """Only TOP-level headings bound a section — a section's own ## / ### sub-headings
    are its content, and stage 4 exists partly to create them."""
    reply = BEFORE + "\n## 2.1 A sub-heading\n\ntext\n\n### 2.1.1 Deeper\n\nmore\n"
    out, foreign = _trim_foreign_sections(BEFORE, reply)
    assert foreign is None
    assert out == reply


def test_unnumbered_section_swallowing_a_numbered_one():
    """Front matter is unnumbered; the section after it is not. The prompt warns about
    exactly this pair because they share a page in nearly every document."""
    before = "# Front Matter\n\nsome front matter text\n"
    after = before + "\n# 1 BACKGROUND\n\nthe background section\n"
    out, foreign = _trim_foreign_sections(before, after)
    assert foreign == "# 1 BACKGROUND"
    assert "the background section" not in out


def test_numbered_neighbour_that_is_not_an_appendix():
    """Germany 179874: section 10's file carried the whole of section 11."""
    before = "# 10 LICENCE\n\nthe licence table\n"
    after = before + "\n# 11. PENALTIES/SANCTIONS\n\nthe penalties table\n"
    out, foreign = _trim_foreign_sections(before, after)
    assert foreign == "# 11. PENALTIES/SANCTIONS"
    assert "the penalties table" not in out
    assert "the licence table" in out


# ---------------------------------------------------------------- outline coverage gate

def test_outline_coverage_follows_the_ai_stages(tmp_path, monkeypatch):
    """A job that ran stage 4 gets its headings checked whatever its product.

    The product allowlist stays the rule for a deterministic job -- the five flat-tree
    products flag 100% of theirs and have never been audited -- but stage 4 is the stage
    that rewrites the tree the headings live in, so a paid pass is always checked.
    """
    import json as _json
    import sys
    sys.path.insert(0, "scripts")
    from check_outline_coverage import ai_ran

    job = tmp_path / "SomeOtherProduct" / "Nowhere__1"
    job.mkdir(parents=True)

    # No marker at all -- a deterministic job.
    (job / "corpus_meta.json").write_text(_json.dumps({"doc_id": "1"}))
    assert ai_ran(job) is False

    # Stage 4 completed and said so.
    (job / "corpus_meta.json").write_text(_json.dumps({"doc_id": "1", "ai_processed": True}))
    assert ai_ran(job) is True


def test_a_half_written_stage4_does_not_count_as_having_run(tmp_path):
    """The marker is written when the pass COMPLETES, so a crashed stage 4 that left a
    04_ directory behind must not switch the check on."""
    import json as _json
    import sys
    sys.path.insert(0, "scripts")
    from check_outline_coverage import ai_ran

    job = tmp_path / "P" / "J__1"
    (job / "04_stage4_ai").mkdir(parents=True)
    (job / "corpus_meta.json").write_text(_json.dumps({"doc_id": "1"}))
    assert ai_ran(job) is False


def test_unreadable_meta_is_not_an_error(tmp_path):
    import sys
    sys.path.insert(0, "scripts")
    from check_outline_coverage import ai_ran
    job = tmp_path / "P" / "J__1"
    job.mkdir(parents=True)
    assert ai_ran(job) is False          # no corpus_meta.json at all
    (job / "corpus_meta.json").write_text("{not json")
    assert ai_ran(job) is False
