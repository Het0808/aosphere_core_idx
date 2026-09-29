# Changes report — scripts/

Comparison: `scripts-prev.zip` (baseline snapshot) vs. the current `scripts/` folder.
Only 4 of the ~150 files in `scripts/` differ. Everything else is unchanged.

## Files changed

- `scripts/check_scorecard.py`
- `scripts/hybrid_extract.py`
- `scripts/build_inspect.py`
- `scripts/hybrid_extract_ui.py`

Attribution: most of this work was done in an earlier/different chat session, not this
one. Two specific edits were made in *this* session and are called out explicitly below;
everything else predates this session and is being documented here, not authored here.

---

## scripts/check_scorecard.py

**1. New finding: blank/dropped answer segments in multi-part table cells**
(`find_empty_answer_segments`, `EMPTY_ANSWER_SEGMENT_PENALTY = 5.0`) — prior session.

Some answer cells hold several `<br><br>`-separated segments, one per sub-question the
paired question cell enumerates. 4+ consecutive `<br>` tags means a segment that should
hold real content is blank. This was invisible to word-coverage checks because the
corpus pattern is a repeated short boilerplate line ("N/a. Please see 7.1(a) above.")
that still appears elsewhere in the *same* cell — so "is this text anywhere in the
tree" reads satisfied even though one copy is genuinely missing. A helper
`_locate_page` anchors the text immediately before the gap against a per-page token
index of the source PDF to find which page the gap is on. Adds a new
`empty_answer_segment` finding (dimension `completeness`, severity `silent`) and a fixed
5.0-point penalty per occurrence to the completeness score.

*This session's edit* (called out in the diff's own comments): a `"file_pages"`
fallback — when the page-anchor match fails (routine for table cells, since a cell's
PDF reading order routinely scrambles a contiguous token match), the finding now falls
back to the *file's own page range* instead of shipping an empty `pages: []`. Before
this fix the finding was invisible in the Page Review tab (any finding with no pages is
silently dropped there) despite still counting against the score — this is the "N/a.
Please see 7.1(a) above" / page 46 bug reported during this session.

before :-
```python
# find_empty_answer_segments — no fallback: an unmatched anchor shipped nothing
out.append({
    "file": str(p.relative_to(tree_dir)).replace("\\", "/"),
    "empty_segments": empty_segments,
    "before": " ".join(before[-14:]),
    "after": " ".join(after[:14]),
    "page": page,
})
...
# _collect_findings — pages stayed [] whenever the anchor missed, so Page Review
# (everything there is keyed by page number) silently dropped the finding
pages = [e["page"]] if e.get("page") else []
```

after :-
```python
# find_empty_answer_segments (check_scorecard.py:474-487)
out.append({
    "file": str(p.relative_to(tree_dir)).replace("\\", "/"),
    "empty_segments": empty_segments,
    "before": " ".join(before[-14:]),
    "after": " ".join(after[:14]),
    "page": page,
    "file_pages": list(file_pages) if file_pages else None,
})
...
# _collect_findings (check_scorecard.py:1913) — falls back to the file's own page range
pages = [e["page"]] if e.get("page") else (e.get("file_pages") or [])
```

**2. New finding/penalty: table rows split across a page break**
(`ROW_SPLIT_PENALTY = 0.15`, `ROW_SPLIT_PENALTY_UNCONFIRMED = 0.08`) — prior session.

Reads `stage2.stitch_anomalies` (from `hybrid_extract.stitch_table_html`, see below) and
classifies each anomaly by its `action`:
- `kept_as_separate_row` → confirmed split (stitcher couldn't safely rejoin a row after
  a page break). New finding kind `row_split`, penalty 0.15/occurrence.
- `unflagged_continuation` → weaker evidence (the new page's leading cell already has
  real text, so the usual blank-cell continuation check never caught it). New finding
  kind `row_split_unflagged`, half penalty (0.08), flagged as carrying real
  false-positive risk.
- anything else (`realigned_and_merged` / `widened_and_merged`) → already repaired by
  Stage 2; reported as advisory-only `row_continuation_merged`, no score penalty.

before :- no classification of `stitch_anomalies` existed in check_scorecard.py; a row
split across a page break was silently absorbed into hybrid_extract.py's stitching and
never turned into a finding or a score penalty.

after :-
```python
# check_scorecard.py:1785-1839
for a in (stage2 or {}).get("stitch_anomalies", []) or []:
    if a.get("action") == "kept_as_separate_row":
        out.append({"kind": "row_split", "dimension": "fidelity", "severity": "silent", ...})
    elif a.get("action") == "unflagged_continuation":
        out.append({"kind": "row_split_unflagged", "dimension": "fidelity", "severity": "silent", ...})
    else:  # realigned_and_merged / widened_and_merged
        out.append({"kind": "row_continuation_merged", "dimension": "fidelity", "severity": "advisory", ...})

# check_scorecard.py:1097-1104 — the score side
row_splits = [a for a in (stitch_anomalies or []) if a.get("action") == "kept_as_separate_row"]
unconfirmed_splits = [a for a in (stitch_anomalies or []) if a.get("action") == "unflagged_continuation"]
earned -= ROW_SPLIT_PENALTY * len(row_splits)
earned -= ROW_SPLIT_PENALTY_UNCONFIRMED * len(unconfirmed_splits)
```

**3. Fidelity dimension no longer blanked out post-AI** — bug fix.

Previously `fidelity` was fully blanked (`score = None`) on the post-AI re-score because
its geometric-bbox table credit is a frozen Stage 2 snapshot, "stale and misleading"
once Stage 4 has rewritten the tree. Now: every table starts at full credit (1.0) on the
post-AI pass instead of re-deriving the stale bucket; a table lost before the final tree
forfeits that same 1.0; and the row-split penalties from #2 (a live Stage 2/3 defect,
unrelated to Stage 4) apply on top and now actually count. `fidelity` is no longer
excluded from the post-AI critical/gating set.

before :-
```python
# _score_fidelity — post-AI (stage truthy) short-circuited to None
if stage:
    return None, {"available": False, "reason": "stale post-Stage-4"}
```

after :-
```python
# check_scorecard.py:1059,1080-1091 — _score_fidelity
earned = float(len(tables)) if stage else 0.0
for t in tables:
    credit, bucket = _table_credit(t)
    if not stage:
        earned += credit
    buckets[bucket] = buckets.get(bucket, 0) + 1
for t in tables:
    if t["table_id"] in lost_ids:
        earned -= 1.0 if stage else _table_credit(t)[0]
```

**4. Sectioning score no longer zeroed for the raw-MinerU fallback tier**
(`_score_sectioning` gains `unchunked` param) — bug fix.

The `RAW_MINERU_OUTPUT` fallback tier writes one synthetic placeholder "heading"
(the PDF filename) into the headings manifest. Treated as a real outline entry, it can
never "reach a heading in the tree," so `outline_lost/outline_total` always came out
1/1, zeroing sectioning for every document on this tier — concretely, Spain__138135
scored `worst_score 0.0` despite a cleanly divided 19-section tree. Fixed by skipping the
outline term when `unchunked` is true.

before :-
```python
# _score_sectioning — no unchunked param; outline term always ran
outline_total = (outl or {}).get("outline_count") or 0
outline_absent = [m for m in ((outl or {}).get("missing") or [])
                  if _norm_section_title(m.get("title") or "") not in census_missing_norm]
outline_lost = len(outline_absent) if ((outl or {}).get("available") and outline_total) else 0
```

after :-
```python
# check_scorecard.py:760-885 — _score_sectioning(..., unchunked: bool = False)
outline_total = 0 if unchunked else ((outl or {}).get("outline_count") or 0)
outline_absent = [] if unchunked else [
    m for m in ((outl or {}).get("missing") or [])
    if _norm_section_title(m.get("title") or "") not in census_missing_norm]
outline_lost = len(outline_absent) if ((outl or {}).get("available") and outline_total) else 0
```

**5. Gate no longer auto-downgraded by source-fidelity findings** — behavioral change.

Previously, any non-advisory `source_fidelity` finding forced the gate down to
`review` even if the worst scored dimension was ≥90. That override is removed: the
gate is now purely the worst dimension score's own verdict. Source-fidelity findings
still populate `gate_reasons`/`source_review` for a reviewer's eye, but are informational
only — "a document scoring in the 90s reads PASS, full stop."

before :-
```python
gate = ("unknown" if worst is None else
        "pass" if worst >= GATE_PASS else
        "review" if worst >= GATE_REVIEW else "fail")
if source_review:
    gate = "review" if gate == "pass" else gate  # downgraded regardless of worst score
```

after :-
```python
# check_scorecard.py:2257-2267
gate = ("unknown" if worst is None else
        "pass" if worst >= GATE_PASS else
        "review" if worst >= GATE_REVIEW else "fail")
# source_review/gate_reasons stay informational only — nothing here downgrades a
# genuinely >=GATE_PASS score to "review" underneath it
source_review = [f for f in source_fidelity.get("findings", [])
                 if f["key"] not in dismissed_keys
                 and f.get("evidence", {}).get("confidence") != "advisory"]
```

**6. Readable quotes for `gap`/`found_elsewhere` finding titles**
(`_readable_span`, `_attach_readable_titles`, `_cleaned_pages`, `_page_token_offsets`)
— new feature, cosmetic/display-only.

These findings used to title themselves with the raw normalized tokenizer output
(lowercased, punctuation stripped — e.g. "article 6 4"). A new pass re-tokenizes the
cited PDF page(s), locates the finding's token run, and substitutes the real, punctuated
substring as the title (joining across a page boundary where the span crosses pages).
Falls back silently to the old token-join title if the page can't be opened or the run
can't be relocated.

before :- no readable-title pass existed; `gap`/`found_elsewhere` findings kept the raw
normalized tokenizer join as their title (e.g. `"article 6 4"`).

after :-
```python
# check_scorecard.py:1402-1418
def _attach_readable_titles(findings: list[dict], out_root: Path) -> None:
    for f in findings:
        if f["kind"] not in ("gap", "found_elsewhere") or not f.get("pages"):
            continue
        truncated = f["title"].endswith("…")
        tokens = (f["title"][:-1] if truncated else f["title"]).split()
        readable = _readable_span(pdf_path, f["pages"], tokens)
        if readable:
            f["title"] = readable + ("…" if truncated else "")
```

---

## scripts/hybrid_extract.py

Table row-continuation/split detection improvements (feeds directly into
check_scorecard.py's `row_split`/`row_split_unflagged` findings above) — prior session.

1. **Fixed a header-row false negative.** A repeated "Questions"/"Answers" header
   sitting at `ri == 0` on a continuation page masked the real continuation row
   (at `ri == 1`) from ever reaching the split-detection checks below it, which tested
   `ri == 0` to mean "the page's first row." Replaced with a `block_started` flag that
   tracks the block's actual first real row, whatever index it lands at. Confirmed
   against Liechtenstein__180334 table_011 (p88→89, p89→90).

   before :-
   ```python
   for ri, tr in enumerate(root.iter("tr")):
       ...
       if key == header_key:
           continue
       is_block_start = (ri == 0)   # header sitting at ri==0 masked the real row at ri==1
   ```

   after :-
   ```python
   # hybrid_extract.py:1052-1079
   block_started = False
   for ri, tr in enumerate(root.iter("tr")):
       ...
       if header_key is None:
           header_key = key
           rows.append(cells); block_started = True
           continue
       if key == header_key:
           continue
       is_block_start = not block_started
       block_started = True
   ```

2. **Added `row_text`** to the anomaly record (the row's own text, independent of which
   column the surplus sits in) — `surplus_text` is empty whenever the real content is at
   the *leading* edge, which previously left a `kept_as_separate_row` outcome with no
   text anywhere in the anomaly to identify the orphaned row by.

   before :-
   ```python
   anom = {"kind": "TABLE_CONTINUATION_WIDER_ROW", ...,
           "surplus_text": [c["text"] for c in cells[len(prev):] if c["text"]]}
   ```

   after :-
   ```python
   # hybrid_extract.py:1085-1098
   anom = {"kind": "TABLE_CONTINUATION_WIDER_ROW", ...,
           "surplus_text": [c["text"] for c in cells[len(prev):] if c["text"]],
           "row_text": [c["text"] for c in cells if c["text"]]}
   ```

3. **New detection path: unflagged continuations.** When the new page's first row
   already has real text in its first cell (e.g. the next sub-question's own label)
   rather than a blank marker column, the existing blank-cell check never ran at all and
   the row was silently appended with no record. Now inferred from the previous row's
   last cell ending mid-sentence, guarded by `MIN_UNFLAGGED_TAIL_WORDS = 10`,
   `MIN_UNFLAGGED_NEW_ROW_WORDS = 4`, and a numbered-heading exclusion
   (`_HEADING_START_RE`) to avoid flagging short canned answers or a genuine new
   subsection. Logged as `TABLE_ROW_SPLIT_UNFLAGGED_CONTINUATION` — detection only,
   never auto-merged, since guessing which words belong to which row risks the exact
   corruption `_alignment_offset` exists to avoid.

   before :- no such path; a non-blank leading cell on the continuation row's first
   cell skipped the blank-cell check entirely and the row was appended silently.

   after :-
   ```python
   # hybrid_extract.py:1184-1199
   if (bi > 0 and is_block_start and rows and cells[0]["text"]
           and len(rows[-1]) and _ends_mid_sentence(
               next((c["text"] for c in reversed(rows[-1]) if c["text"]), ""))):
       tail = next((c["text"] for c in reversed(rows[-1]) if c["text"]), "")
       new_row_words = sum(len(c["text"].split()) for c in cells)
       if (len(tail.split()) >= MIN_UNFLAGGED_TAIL_WORDS
               and new_row_words >= MIN_UNFLAGGED_NEW_ROW_WORDS
               and not _HEADING_START_RE.match(cells[0]["text"])):
           anomalies_out.append({"kind": "TABLE_ROW_SPLIT_UNFLAGGED_CONTINUATION",
                                  "action": "unflagged_continuation", ...})
   ```

---

## scripts/build_inspect.py

Explicit `encoding="utf-8"` added to every `read_text()` call (page-count lookups, table
`.md`/`.html` reads, scorecard/validation JSON reads) — prior session, robustness fix.
Without an explicit encoding, Python falls back to the platform's locale-preferred
encoding (cp1252 on Windows), which could raise `UnicodeDecodeError` or mojibake on
non-ASCII content even though everything is written as UTF-8. No behavioral change on a
system where the locale already happened to be UTF-8.

before :-
```python
return json.loads(p.read_text()).get("pages")
d["table_md"] = md_p.read_text() if md_p.exists() else ""
d["table_html"] = html_p.read_text() if html_p.exists() else ""
sc = json.loads((job_dir / SC_FILES[v]).read_text())
post = json.loads(p.read_text())
```

after :-
```python
# build_inspect.py:64,78-79,142,165
return json.loads(p.read_text(encoding="utf-8")).get("pages")
d["table_md"] = md_p.read_text(encoding="utf-8") if md_p.exists() else ""
d["table_html"] = html_p.read_text(encoding="utf-8") if html_p.exists() else ""
sc = json.loads((job_dir / SC_FILES[v]).read_text(encoding="utf-8"))
post = json.loads(p.read_text(encoding="utf-8"))
```

---

## scripts/hybrid_extract_ui.py

Note: a naive diff against the snapshot showed ~8,500 changed lines, but that was a
line-ending artifact (LF in the snapshot vs. CRLF in the current file), not real content
changes. Re-diffed after normalizing line endings — the genuine change set is ~140 lines
across 14 hunks.

**1. Explicit UTF-8 encoding**, same fix and same rationale as build_inspect.py above,
applied at roughly a dozen call sites (`_read_json`, corpus job/report/manifest reads,
`_progress.json`, table file reads, cached-scorecard reload, etc.) — prior session.

before :- `path.read_text()` / `json.loads(path.read_text())` at each of these call
sites, no `encoding=` argument.

after :- same calls with `encoding="utf-8"` added, e.g. `path.read_text(encoding="utf-8")`.

**2. `PR_KIND_LABEL` / `PR_KIND_CLASS` entries for ten previously-unlabeled finding
kinds** — **this session.** `empty_answer_segment` ("MISSING CONTENT", red), `row_split`
("ROW SPLIT", red), `row_split_unflagged` ("ROW SPLIT (UNCONFIRMED)", amber),
`row_continuation_merged` ("ROW MERGED", amber), `row_merge` ("ROW MERGE", red),
`table_without_source_grid` ("TABLE NO SOURCE GRID", red), `section_boundary_leak`
("SECTION BOUNDARY LEAK", red), `extraction_annotation` ("EXTRACTION ANNOTATION", amber),
`duplicated_content` ("DUPLICATED CONTENT", amber), `inconsistent_table_columns`
("INCONSISTENT COLUMNS", red). Without these, a finding of one of these kinds rendered
in Page Review with a blank/undefined label and no severity color. `empty_answer_segment`
is the display counterpart to the check_scorecard.py fix in this same session, above.

before :-
```js
// hybrid_extract_ui.py:3072-3091 — dict ended at `engine`, no entries for the 10 new kinds
const PR_KIND_LABEL = {{
  ...
  engine: 'PARSER DISAGREEMENT',
}};
const PR_KIND_CLASS = {{
  ...
  engine: 'val-issue-amber',
}};
```

after :-
```js
// hybrid_extract_ui.py:3086-3090, 3106-3110
const PR_KIND_LABEL = {{
  ...
  engine: 'PARSER DISAGREEMENT',
  empty_answer_segment: 'MISSING CONTENT', row_split: 'ROW SPLIT',
  row_split_unflagged: 'ROW SPLIT (UNCONFIRMED)', row_continuation_merged: 'ROW MERGED',
  row_merge: 'ROW MERGE', table_without_source_grid: 'TABLE NO SOURCE GRID',
  section_boundary_leak: 'SECTION BOUNDARY LEAK', extraction_annotation: 'EXTRACTION ANNOTATION',
  duplicated_content: 'DUPLICATED CONTENT', inconsistent_table_columns: 'INCONSISTENT COLUMNS',
}};
const PR_KIND_CLASS = {{
  ...
  engine: 'val-issue-amber',
  empty_answer_segment: 'val-issue-red', row_split: 'val-issue-red',
  row_split_unflagged: 'val-issue-amber', row_continuation_merged: 'val-issue-amber',
  row_merge: 'val-issue-red', table_without_source_grid: 'val-issue-red',
  section_boundary_leak: 'val-issue-red', extraction_annotation: 'val-issue-amber',
  duplicated_content: 'val-issue-amber', inconsistent_table_columns: 'val-issue-red',
}};
```

No other logic in this file changed between the snapshot and the current version.

---

---

# Changes report — src/

Comparison: `src-prev.zip` (baseline snapshot) vs. the current `src/` folder.
Only 1 file differs.

## Files changed

- `src/aosphere_core_index/service/doc_gallery.py`

## src/aosphere_core_index/service/doc_gallery.py

**Jurisdiction/product rollup row no longer stuck on Review by one low-severity
per-document finding** (`_verdict`) — prior session, bug fix.

A jurisdiction or product row in the Doc Gallery tree inherits `review`/`fail` from
*any one* of its documents, however far above the pass bar the row's own worst and mean
scores sit — so a single low-severity per-document finding (e.g. a `source_fidelity`
finding) could hold a whole jurisdiction at "review" even when both aggregate numbers
read well into the 90s. Fixed: once the row's own worst *and* mean scores both clear 90,
the row now shows `pass` regardless of that per-document review/fail count, "since that
is what the numbers next to the verdict already say." This is the rollup-level
counterpart to the per-document gate fix in check_scorecard.py's item 5 above (gate no
longer auto-downgraded by source-fidelity findings) — same underlying Pass/Review bug,
fixed at both the document level and the tree-rollup level.

before :-
```python
def _verdict(node: dict) -> str:
    return "fail" if node["fail"] else "review" if node["review"] else "pass"
```

after :-
```python
# doc_gallery.py:872-884
def _verdict(node: dict) -> str:
    worst, mean = node.get("worst_score"), node.get("mean_score")
    if (isinstance(worst, (int, float)) and worst > 90
            and isinstance(mean, (int, float)) and mean > 90):
        return "pass"
    return "fail" if node["fail"] else "review" if node["review"] else "pass"
```

---

## Data/config changes made this session (outside scripts/, not in the zip)

- Archived (moved, not deleted) `out/corpus/104_Shareholding_Disclosure/` and
  `out/corpus/124_Marketing_Restrictions_-_Asset_Management/{Japan__172122,
  Jersey__181919, Malaysia__176985, Turkey__166449}` to
  `out/corpus/_archived_removed/`, per request to remove those jurisdictions and the
  Share-Holding Disclosure product from the Doc Gallery/Scorecard.
- Regenerated `scorecard.json` / `scorecard_post_ai.json` and rebuilt
  `viewer.html` / `inspect.html` for all 19 remaining documents under
  `124_Marketing_Restrictions_-_Asset_Management`, so the Pass/Review gate on screen
  matches the current scoring rules instead of a stale pre-fix computation. Originals
  kept alongside as `*.bak`. Two documents genuinely still gate Review because their
  worst dimension is under 90 on the extraction pass (Mauritius__183013 at 86.5,
  Spain__176285 at 84.8) — both clear to Pass on the post-AI view.