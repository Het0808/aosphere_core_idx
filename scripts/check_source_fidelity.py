#!/usr/bin/env python3
"""Compare exported chunks with PDF cells and headings, independently of MinerU.

Findings are review candidates with source evidence, not claims of legal meaning.
Only ruled tables are structurally comparable; coverage and unassessed pages are
reported explicitly. No source manifests, network calls or LLMs are needed.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path

from lib_content_compare import (clean_markdown, parse_page_range, resolve_pdf,
                                 resolve_stage_dir, tokenize)


def tokens(text):
    # Join PDF line-end hyphenation; normalize typography on BOTH sides.
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"\[\^\d+\]", "", text)
    return tokenize(clean_markdown(text))


def shingles(ts, size=4):
    return {tuple(ts[i:i + size]) for i in range(len(ts) - size + 1)}


def coverage(needle, hay):
    if not needle:
        return 0.0
    if len(needle) < 4:
        return float(any(hay[i:i + len(needle)] == needle
                         for i in range(len(hay) - len(needle) + 1)))
    blocks = SequenceMatcher(None, needle, hay, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks if b.size >= 3) / len(needle)


class GridParser(HTMLParser):
    """Keep logical coordinates, including occupied rowspan/colspan slots."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.table = -1
        self.row = -1
        self.col = 0
        self.busy = {}
        self.current = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table += 1
                self.row, self.col, self.busy = -1, 0, {}
        if self.depth != 1:
            return
        if tag == "tr":
            self.row += 1
            self.col = 0
        if tag in {"td", "th"}:
            attrs = dict(attrs)
            def span(name):
                try:
                    return max(1, min(1000, int(attrs.get(name, 1))))
                except (ValueError, TypeError):
                    return 1
            rs, cs = span("rowspan"), span("colspan")
            while self.busy.get((self.row, self.col)):
                self.col += 1
            self.current = {"table": self.table, "row": self.row, "col": self.col,
                            "rowspan": rs, "colspan": cs, "parts": []}
            for r in range(self.row, self.row + rs):
                for c in range(self.col, self.col + cs):
                    self.busy[r, c] = True
            self.col += cs
        elif self.current and tag in {"br", "p", "li", "div"}:
            self.current["parts"].append(" ")

    def handle_data(self, data):
        if self.current:
            self.current["parts"].append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.current and self.depth == 1:
            self.current["text"] = "".join(self.current.pop("parts"))
            self.current["tokens"] = tokens(self.current["text"])
            self.cells.append(self.current)
            self.current = None
        elif tag == "table":
            self.depth = max(0, self.depth - 1)


def read_chunks(root):
    chunks, cells = [], []
    for path in sorted(Path(root).rglob("*.md")):
        if path.name in {"CONVERSION_REPORT.md", "STAGE3_REPORT.md", "PIPELINE_SUMMARY.md"}:
            continue
        raw = path.read_text(encoding="utf-8")
        title = re.search(r"^#{1,6}\s+(.+)", raw, re.M)
        chunk = {"file": path.relative_to(root).as_posix(), "raw": raw,
                 "title": title.group(1) if title else "", "pages": parse_page_range(raw),
                 "tokens": tokens(raw)}
        chunks.append(chunk)
        parser = GridParser()
        parser.feed(raw)
        parsed = parser.cells
        # Pipe tables have explicit row and cell boundaries too.
        table, row, in_table = parser.table + 1, 0, False
        for line in raw.splitlines():
            if line.strip().startswith("|") and line.strip().endswith("|"):
                if re.fullmatch(r"[\s|:\-]+", line):
                    continue
                for col, value in enumerate(re.split(r"(?<!\\)\|", line.strip())[1:-1]):
                    parsed.append({"table": table, "row": row, "col": col,
                                   "rowspan": 1, "colspan": 1, "text": value,
                                   "tokens": tokens(value)})
                row += 1
                in_table = True
            elif in_table:
                table, row, in_table = table + 1, 0, False
        for cell in parsed:
            cells.append({**cell, "file": chunk["file"], "pages": chunk["pages"]})
    return chunks, cells


def finding(kind, file, pages, detail, **evidence):
    identity = repr((kind, file, pages, evidence))
    if evidence.get("source_cells"):
        cells = evidence["source_cells"]
        detail += " Source: " + "; ".join(
            f"row {c['row']+1}, column {c['col']+1}: {c['text'][:160]}" for c in cells[:3])
    if evidence.get("source_answer"):
        detail += (f" Answer: {evidence['source_answer']!r}; expected "
                   f"{evidence['expected_occurrences']}, found {evidence['actual_occurrences']}.")
    return {"key": "source-" + hashlib.sha256(identity.encode()).hexdigest()[:16],
            "kind": kind, "dimension": "source_fidelity",
            "severity": "advisory" if evidence.get("confidence") == "advisory" else "review",
            "file": file, "pages": pages, "title": kind.replace("_", " "),
            "detail": detail, "evidence": evidence}


def heading_anchors(doc, chunks):
    """Match full chunk titles to consecutive PDF lines, retaining vertical position.

    Page ranges constrain anchors; front matter has no literal heading in most
    sources. A title matching prose mid-line is deliberately not an anchor.
    """
    by_page = defaultdict(list)
    for chunk in chunks:
        if chunk["pages"] and chunk["title"]:
            a, b = chunk["pages"]
            for p in range(max(1, a), min(len(doc), b) + 1):
                by_page[p].append(chunk)
    anchors = []
    seen = set()
    def compact(s):
        return "".join(tokens(s)).replace("-", "").replace("'", "")
    for p, candidates in sorted(by_page.items()):
        lines = [(line["bbox"], "".join(s["text"] for s in line["spans"]))
                 for block in doc[p - 1].get_text("dict")["blocks"] if "lines" in block
                 for line in block["lines"]]
        lines = [(box, text) for box, text in lines if compact(text)]
        for chunk in candidates:
            if chunk["file"] in seen:
                continue
            target = compact(chunk["title"])
            if len(target) < 6:
                continue
            for i, (box, _) in enumerate(lines):
                joined = ""
                for next_box, text in lines[i:i + 7]:
                    if next_box[1] < box[1] - 2 or next_box[1] > box[1] + 100:
                        break
                    joined += compact(text)
                    if joined == target:
                        anchors.append((p, box[1], chunk["file"]))
                        seen.add(chunk["file"])
                        break
                    if not target.startswith(joined):
                        break
                if chunk["file"] in seen:
                    break
    return sorted(anchors)


def compare_grid(source, output):
    """Match within a source table page, then compare row/column relationships.

    A split requires two substantial disjoint fragments whose union preserves
    nearly all the source cell. A merge requires distinct source cells. Ambiguous
    repeated text never proves a structural error.
    """
    flags, mapped = [], {}
    output_index = defaultdict(set)
    for j, cell in enumerate(output):
        for sh in shingles(cell["tokens"]):
            output_index[sh].add(j)
    for i, src in enumerate(source):
        ts = src["tokens"]
        if len(ts) < 8:
            continue
        votes = Counter(j for sh in shingles(ts) for j in output_index.get(sh, ()))
        candidates = [j for j, _ in votes.most_common(12)]
        full = [j for j in candidates if coverage(ts, output[j]["tokens"]) >= .97]
        if len(full) == 1 and coverage(ts, output[full[0]]["tokens"]) >= .99:
            mapped[i] = full[0]
            continue
        if len(full) > 1:  # same text repeated: cannot infer which cell owns it
            continue
        parts = []
        for j in candidates:
            dest = output[j]["tokens"]
            if len(dest) >= 4 and coverage(dest, ts) >= .98:
                blocks = SequenceMatcher(None, ts, dest, autojunk=False).get_matching_blocks()
                positions = {k for b in blocks if b.size >= 3 for k in range(b.a, b.a+b.size)}
                if len(positions) >= 4:
                    parts.append((j, positions))
        # Only fragments in one output table establish a split.
        groups = defaultdict(list)
        for j, positions in parts:
            groups[output[j]["file"], output[j]["table"]].append((j, positions))
        split = False
        for group in groups.values():
            covered, selected = set(), []
            for j, positions in sorted(group, key=lambda x: -len(x[1])):
                if len(positions - covered) >= 4:
                    covered |= positions
                    selected.append(j)
            if len(selected) >= 2 and len(covered)/len(ts) >= .90:
                flags.append(("cell_split", [i], selected))
                split = True
                break
        if not split and len(full) == 1:
            mapped[i] = full[0]
    by_dest = defaultdict(list)
    for i, j in mapped.items():
        by_dest[j].append(i)
    for j, ids in by_dest.items():
        # Reject duplicate/nested source text and overlapping matches.
        unique = []
        for i in sorted(ids, key=lambda k: -len(source[k]["tokens"])):
            if not any(coverage(source[i]["tokens"], source[k]["tokens"]) >= .8 for k in unique):
                unique.append(i)
        if len(unique) >= 2:
            rows = {source[i]["row"] for i in unique}
            cols = {source[i]["col"] for i in unique}
            kind = "row_merge" if len(cols) == 1 else "column_merge" if len(rows) == 1 else "block_merge"
            flags.append((kind, unique, [j]))
    pairs = list(mapped.items())
    for pos, (i, j) in enumerate(pairs):
        for k, l in pairs[pos+1:]:
            a, b, x, y = source[i], source[k], output[j], output[l]
            if j == l or (x["file"], x["table"]) != (y["file"], y["table"]):
                continue
            # Adjacent source cells in one row should still share a rendered row.
            if a["row"] == b["row"] and abs(a["col"]-b["col"]) == 1:
                overlap = max(x["row"], y["row"]) < min(x["row"]+x["rowspan"], y["row"]+y["rowspan"])
                if not overlap:
                    flags.append(("row_alignment", [i, k], [j, l]))
    return flags, mapped


def missing_answers(source, output):
    """Count short answers in the output row anchored by their source question.

    Document-wide presence of 'No' or 'N/a' cannot excuse losing one answer.
    If multiple questions were merged, compare their required multiplicity.
    """
    groups = defaultdict(list)
    for q in source:
        if q["col"] != 0 or len(q["tokens"]) < 4:
            continue
        answers = [s for s in source if s["row"] == q["row"] and s["col"] == 1
                   and 0 < len(s["tokens"]) <= 12]
        if not answers:
            continue
        hits = [o for o in output if len(o["tokens"]) >= 4
                and shingles(q["tokens"]) & shingles(o["tokens"])
                and coverage(q["tokens"], o["tokens"]) >= .9]
        if len(hits) != 1:
            continue
        dest = hits[0]
        groups[dest["file"], dest["table"], dest["row"], dest["col"]+dest["colspan"]].extend(answers)
    missing = []
    for (file, table, row, answer_col), expected in groups.items():
        answer_cells = [o for o in output if o["file"] == file and o["table"] == table
                        and o["row"] <= row < o["row"]+o["rowspan"] and o["col"] >= answer_col]
        counts = Counter(tuple(s["tokens"]) for s in expected)
        for phrase, count in counts.items():
            actual = sum(sum(tuple(o["tokens"][i:i+len(phrase)]) == phrase
                             for i in range(len(o["tokens"])-len(phrase)+1)) for o in answer_cells)
            if actual < count:
                missing.append((file, {"source_answer": " ".join(phrase), "expected_occurrences": count,
                                       "actual_occurrences": actual, "output_table": table, "output_row": row}))
    return missing


def ragged_tables(output):
    """Logical row widths must agree after applying row/column spans."""
    groups = defaultdict(list)
    for cell in output:
        groups[cell["file"], cell["table"]].append(cell)
    result = []
    for (file, table), cells in groups.items():
        widths = defaultdict(int)
        for cell in cells:
            for row in range(cell["row"], cell["row"] + cell["rowspan"]):
                widths[row] = max(widths[row], cell["col"] + cell["colspan"])
        if len(set(widths.values())) > 1:
            result.append(finding("inconsistent_table_columns", file, list(cells[0]["pages"] or []),
                "Rows have different logical column counts after accounting for spans. Review missing cells or incorrect colspan values.",
                output_table=table, row_widths=dict(widths)))
    return result


def audit_source(pdf, tree):
    import fitz
    chunks, output = read_chunks(Path(tree))
    if not chunks:
        return {"passed": False, "error": "No Markdown chunks found", "findings": []}
    findings, source_tables, unchecked = ragged_tables(output), 0, []
    matched_cells, source_cells = 0, 0
    with fitz.open(str(pdf)) as doc:
        anchors = heading_anchors(doc, chunks)
        page_tokens = [tokens(p.get_text()) for p in doc]
        for pno, page in enumerate(doc, 1):
            tables = page.find_tables(strategy="lines_strict").tables
            if not tables:
                unchecked.append(pno)
            local_output = [c for c in output if c["pages"] and c["pages"][0] <= pno <= c["pages"][1]]
            local_shingles = [shingles(c["tokens"]) for c in local_output]
            for ti, table in enumerate(tables):
                if table.col_count < 2:
                    continue
                src = []
                for ri, row in enumerate(table.extract()):
                    for ci, text in enumerate(row):
                        box = table.rows[ri].cells[ci]
                        if text and box:
                            src.append({"row": ri, "col": ci, "text": text,
                                        "tokens": tokens(text), "bbox": list(box)})
                substantive = [s for s in src if len(s["tokens"]) >= 8]
                if not substantive:
                    continue
                source_tables += 1
                source_cells += len(substantive)
                flags, mapped = compare_grid(src, local_output)
                matched_cells += len(mapped)
                for file, evidence in missing_answers(src, local_output):
                    findings.append(finding("missing_cell_answer", file, [pno],
                        "A short answer is missing from its question's output row, even if it occurs elsewhere.",
                        source_table=ti, **evidence))
                for kind, ids, dests in flags:
                    findings.append(finding(kind, local_output[dests[0]]["file"], [pno],
                        "PDF cell boundaries disagree with the exported table; inspect the cited cells.",
                        source_table=ti, source_cells=[{k: src[i][k] for k in ("row", "col", "text", "bbox")} for i in ids],
                        output_cells=[{k: local_output[j][k] for k in ("row", "col", "table", "text")} for j in dests]))
                boundary_matches = []
                for i, s in enumerate(src):
                    if len(s["tokens"]) < 12:
                        continue
                    source_shingles = shingles(s["tokens"])
                    boundary_matches.extend((i, j) for j, dest in enumerate(local_output)
                                            if source_shingles & local_shingles[j]
                                            and coverage(s["tokens"], dest["tokens"]) >= .9)
                for i, j in boundary_matches:
                    s, dest = src[i], local_output[j]
                    preceding = [a for a in anchors if (a[0], a[1]) <= (pno, s["bbox"][1])]
                    if not preceding:
                        continue
                    owner = preceding[-1][2]
                    if owner == dest["file"]:
                        continue
                    # Parent landing pages legitimately contain children in unsplit trees.
                    if Path(owner).parent == Path(dest["file"]).parent and Path(owner).name in {"README.md", "00-section.md"}:
                        continue
                    # Strong same-page boundary proof: content above its chunk's own heading.
                    own_anchor = next((a for a in anchors if a[2] == dest["file"]), None)
                    if own_anchor and own_anchor[0] == pno and s["bbox"][3] < own_anchor[1]:
                        findings.append(finding("section_boundary_leak", dest["file"], [pno],
                            "This cell precedes the chunk's heading in the source PDF.",
                            expected_file=owner, text=s["text"], bbox=s["bbox"], heading_y=own_anchor[1]))
                # A table rendered entirely as prose is invisible to cell-only matching.
                if not boundary_matches:
                    for chunk in chunks:
                        if chunk["pages"] and chunk["pages"][0] <= pno <= chunk["pages"][1]:
                            kept = [s for s in substantive if coverage(s["tokens"], chunk["tokens"]) >= .9]
                            if len(kept) >= 2 and len(kept) >= .8 * len(substantive):
                                findings.append(finding("table_as_text", chunk["file"], [pno],
                                    "Source table cells survive as text but do not map to output cells.",
                                    source_table=ti, bbox=list(table.bbox)))
                                break
            # Bordered cover forms may have only an outer box, no internal rules.
            if pno == 1 and not tables:
                for box in page.cluster_drawings():
                    if box.width < page.rect.width*.5 or box.height < page.rect.height*.25:
                        continue
                    text = page.get_text(clip=box)
                    if len(tokens(text)) < 50:
                        continue
                    for chunk in chunks:
                        if chunk["pages"] and chunk["pages"][0] == 1 and coverage(tokens(text), chunk["tokens"]) >= .8:
                            if not any(c["file"] == chunk["file"] for c in output):
                                findings.append(finding("boxed_layout_as_text", chunk["file"], [1],
                                    "The source has a boxed cover form, while the chunk uses prose. Review layout fidelity.",
                                    bbox=list(box), confidence="advisory"))
                                break
                    break
            if not tables:
                groups = defaultdict(list)
                for cell in local_output:
                    if len(cell["tokens"]) >= 8 and coverage(cell["tokens"], page_tokens[pno-1]) >= .9:
                        groups[cell["file"], cell["table"]].extend(cell["tokens"])
                for (file, table), body in groups.items():
                    if len(body) >= 40:
                        findings.append(finding("table_without_source_grid", file, [pno],
                            "The output uses a table where the PDF has no detected ruled grid. Review for prose converted into a table.",
                            output_table=table, confidence="advisory"))
        # Duplicate whole bodies, verified against occurrence counts in the source.
        bodies = defaultdict(list)
        for chunk in chunks:
            body = re.sub(r"^#+.*$", "", chunk["raw"], flags=re.M)
            ts = tokens(body)
            if len(ts) >= 40:
                bodies[tuple(ts)].append(chunk)
            # Pipeline badges are useful provenance but must be visible as such.
            if "MinerU-extracted table" in chunk["raw"]:
                findings.append(finding("extraction_annotation", chunk["file"], list(chunk["pages"] or []),
                    "Extractor metadata is included in the delivered chunk.", confidence="advisory"))
        full_source = " " + " ".join(t for page in page_tokens for t in page) + " "
        for body, copies in bodies.items():
            if len(copies) < 2:
                continue
            anchor = " " + " ".join(body[:30]) + " "
            occurrences = full_source.count(anchor)
            if occurrences == 1:
                findings.append(finding("duplicate_chunk", copies[1]["file"], list(copies[1]["pages"] or []),
                    "Identical chunk bodies repeat a passage found once in the source.",
                    files=[c["file"] for c in copies], source_occurrences=occurrences))
        # A copied section can be embedded in a larger chunk; whole-file equality
        # alone cannot see it. Use substantial cell bodies as independent units.
        repeated_cells = defaultdict(list)
        for cell in output:
            if len(cell["tokens"]) >= 40:
                repeated_cells[tuple(cell["tokens"])].append(cell)
        for body, copies in repeated_cells.items():
            files = sorted({c["file"] for c in copies})
            if len(files) < 2:
                continue
            anchor = " " + " ".join(body[:30]) + " "
            if full_source.count(anchor) == 1:
                findings.append(finding("duplicated_content", files[1], list(copies[1]["pages"] or []),
                    "A substantial table cell repeats in different chunks, but its opening passage occurs once in the PDF.",
                    files=files, text=" ".join(body), source_occurrences=1))
        source_shingles = set().union(*(shingles(ts) for ts in page_tokens))
        for chunk in chunks:
            prose = re.sub(r"<table\b.*?</table>", "", chunk["raw"], flags=re.S | re.I)
            for paragraph in re.split(r"\n\s*\n", prose):
                if paragraph.lstrip().startswith(("#", "|", "![", ">", "*Source:")):
                    continue
                ts = tokens(paragraph)
                if len(ts) < 20:
                    continue
                windows = shingles(ts)
                retained = len(windows & source_shingles) / max(1, len(windows))
                if retained < .2:
                    findings.append(finding("unsupported_prose", chunk["file"], list(chunk["pages"] or []),
                        "Most four-word phrases in this paragraph are absent from the source text layer. Review for added content or an unreadable source.",
                        text=paragraph, source_phrase_coverage=round(retained, 3), confidence="advisory"))
    # Stable de-duplication, with no truncation of review evidence.
    findings = list({f["key"]: f for f in findings}.values())
    active = [f for f in findings if f["evidence"].get("confidence") != "advisory"]
    return {"passed": not active, "status": "review" if active else "no_findings",
            "findings": findings, "review_count": len(active),
            "coverage": {"source_tables": source_tables, "source_cells": source_cells,
                         "matched_cells": matched_cells, "pages_without_ruled_tables": unchecked,
                         "chunks": len(chunks), "headings_anchored": len(anchors),
                         "chunks_without_page_range": [c["file"] for c in chunks if not c["pages"]]},
            "limitations": ["Borderless tables and visual list indentation need visual review.",
                            "Unmatched or ambiguous cells are not proof of a structural defect.",
                            "Cells continuing across page breaks are compared page by page, not fully reconstructed.",
                            "Quote-style changes are normalized; they are not meaning changes."]}


def compute_source_fidelity(out_root: Path, pdf: str | None = None, stage: int = 3):
    return audit_source(resolve_pdf(Path(out_root), pdf), resolve_stage_dir(Path(out_root), stage))
