"""Convert the (finer) Claude-from-PDF markdown tree into a keyed content.json.

Captures EVERY content file (skips only README/glossary TOCs):
  <N.M>-clause.md                      -> <Part><N.M>            (clause)
  <N.M>-clause/<letter|roman>-*.md     -> <Part><N.M>(a) / (iii) (sub-clause; roman kept as-is)
  <N.M>-clause/00-overview.md          -> <Part><N.M>            (clause parent node)
  <NN>-section.md  or  <NN>-section/00-overview.md -> <Part><NN> (section node)
  <Part>/00-introduction.md            -> <Part>                 (part node)
Family-match in eval credits the parent clause, so roman/(a-h) scheme differences are fine.
Oversized bodies are windowed (#c chunks); collisions on a key continue the #c counter so
two files sharing a derived key are BOTH embedded (nothing silently dropped).
"""
import glob, json, os, re, sys


SUBS = set("abcdefgh") | {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}

# A part folder is "A-substantial-shareholding" on most documents and
# "01-a-substantial-shareholding" on others — the extractor keeps whatever ordinal the
# source outline carried. Only the first form was recognised, so five regions came out of
# the converter EMPTY while their extractions were perfectly good: Data Privacy Algeria and
# Shareholding Disclosure Curaçao, Denmark, Serbia and Slovenia. An empty region is
# invisible rather than obviously broken — it drops out of the index with no error — which
# is why it took a jurisdiction going missing to notice.
# A-L, not A-K: the surveys run to K, but India Data Privacy carries a country-specific part
# L ("L-aadhaar-act", 8,546 words) that the K ceiling dropped entirely. L is the only letter
# beyond K anywhere in the corpus, so the ceiling is raised by exactly one rather than opened
# to Z — a bare single letter plus hyphen is a common enough folder name ("e-signatures") that
# a wide range would start reading product names as part letters.
_PART_DIR = re.compile(r"^(?:\d+-)?([A-La-l])-")


def part_letter(top: str) -> str | None:
    """The part letter of a top-level folder, ordinal prefix or not."""
    m = _PART_DIR.match(top)
    return m.group(1).upper() if m else None


def derive_key(rel):
    parts = rel.split(os.sep)
    part = part_letter(parts[0])                    # excludes annex-*, top glossary/README
    if part is None:
        return None
    fn = parts[-1]
    if re.match(r"(README|glossary)", fn, re.I):   # pure TOC/index files
        return None
    nm = sec = None
    for comp in parts[1:-1]:                        # ancestor dirs -> N.M and section NN
        m = re.match(r"(\d+(?:\.\d+)+)-", comp)
        if m:
            nm = m.group(1); continue
        ms = re.match(r"0*(\d+)-", comp)
        if ms:
            sec = ms.group(1)
    # N.M and DEEPER. Austria and Kazakhstan number their clauses three levels
    # ("4.3-calculation/4.3.2-what-is-included-in-the-numerator.md"), and a two-component
    # pattern matched none of them: 72 files, 19,114 words, silently dropped — 23% of Austria
    # Shareholding Disclosure. key_meta already parses a three-level key (A4.3.2 -> parent
    # A4.3), so nothing downstream needed changing.
    m_nm = re.match(r"(\d+(?:\.\d+)+)-", fn)
    m_sub = re.match(r"\(?([a-z]{1,4})\)?[-.]", fn)
    m_sec = re.match(r"0*(\d+)-", fn)
    if m_nm:                                        # clause file N.M
        return f"{part}{m_nm.group(1)}"
    if m_sub and m_sub.group(1) in SUBS and (nm or sec):   # letter/roman sub-clause
        return f"{part}{nm or sec}({m_sub.group(1)})"
    if fn.startswith("00-"):                        # overview/intro -> nearest parent node
        return f"{part}{nm}" if nm else (f"{part}{sec}" if sec else part)
    if m_sec:                                       # section-level content file NN-name
        return f"{part}{m_sec.group(1)}"
    if len(parts) == 1 and part_letter(fn):
        # The FILE is the whole part: "I-changes-in-regulation.md" with no folder under it.
        # The letter is explicit in the name — nothing is inferred — yet this returned None,
        # so part I went missing from most Data Privacy documents (6,434 words in the
        # Netherlands, 4,200 in Poland, 3,921 in Germany …).
        return part
    return None

_TABLE_RE = re.compile(r"<table\b.*?</table>", re.I | re.S)


def _table_to_md(html):
    """One MinerU <table> HTML block -> a markdown pipe table (string), or None if it
    doesn't parse as a >=2-row table. The core-index viewer escapes element text, so
    tables must be markdown, not HTML; a pipe table in a kind:'table' element renders
    monospace-aligned (<pre>). rowspan/colspan are flattened (the grid comparison tables
    don't use them); the doc-gallery keeps the rich HTML from the stage-3 tree."""
    from lxml import etree
    root = etree.HTML(html)
    if root is None:
        return None

    def cells(tr):
        out = []
        for td in tr:
            if not isinstance(td.tag, str) or td.tag.lower() not in ("td", "th"):
                continue
            t = re.sub(r"\s+", " ", " ".join(td.itertext())).strip().replace("|", "\\|")
            out.extend([t] + [""] * (int(td.get("colspan") or 1) - 1))
        return out

    rows = [r for r in (cells(tr) for tr in root.iter("tr")) if r]
    if len(rows) < 2:
        return None
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    head, *body = rows
    md = ["| " + " | ".join(head) + " |", "| " + " | ".join(["---"] * ncol) + " |"]
    md += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(md)


# ---- extraction scaffolding that must not become product content ----------------
#
# The stage-3 markdown is written for the DOC VIEWER, where provenance is the point: each
# file opens with a source breadcrumb, tables MinerU recovered carry a verify-this note,
# and footnote bodies are collected under a rule at the end. None of that is the surveyed
# CONTENT, and leaving it in was doing real damage in both directions:
#
#   * the reader showed "*Source: `source.pdf`, page 5*" as the first line of 2,705
#     clause bodies, and MinerU's "verify this landed in the right section" QA note in 85;
#   * every clause VECTOR started with that same breadcrumb, so ~all 233 regions embedded
#     a near-identical prefix — tokens spent making every clause look slightly more like
#     every other clause.
#
# Footnote DEFINITIONS are not noise — they are the citations, and the reader has a place
# for them: every section carries a `footnotes: [{id, text}]` field that the clause and
# full-document panes render as a "Footnotes (N)" block, exactly as the legacy docx content
# did. So they are LIFTED OUT of the prose (inline they read as a wall of URLs) and
# attached to the section, with the [^n] markers left in the text pointing at them.
_SOURCE_LINE = re.compile(r"^\s*\*Source:.*?\*\s*$", re.M)
# Blockquote NOTES the extractor writes for a reviewer, matched by their own vocabulary.
# NOT "every blockquote": these documents quote statute in blockquotes as a matter of
# course ("> A company qualifies as tax resident in the UAE if it is: ..."), 2,060 lines
# of them across the corpus, so a blanket rule here would delete the law itself.
_VIEWER_NOTE = re.compile(
    r"^\s*>\s*(?:\*\*\[TABLE (?:CONTINUATION|EXTRACTION FAILED|PENDING)\b.*"
    r"|Page \d+ of the source PDF contains a complex table.*)$", re.M)
# Page snapshots are viewer-only: a relative path into the tree's _assets/ that means
# nothing once the body is served as product content.
_SNAPSHOT_IMG = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$", re.M)
# Inline HTML the extractor leaves in prose. The reader ESCAPES html — deliberately, since
# a document must never inject markup — so "<sup>158</sup>Regulation 9" was displayed to
# the user as exactly that, tags and all, in 3,842 elements across 83 regions.
#
# <sup>158</sup> IS a footnote reference, so it becomes the markdown marker the reader
# already knows how to draw ("[^158]" -> <sup class="fnref">[158]</sup>) rather than being
# deleted. Other inline tags lose the tag and keep the words.
_SUP_EL = re.compile(r"<sup\b[^>]*>\s*([^<]*?)\s*</sup>", re.I | re.S)
_INLINE_TAG = re.compile(r"</?(?:b|i|u|em|strong|span|font|small|sub|br|p|div)\b[^>]*>", re.I)
_MINERU_NOTE = re.compile(r"^\s*\*\*[^*]*MinerU-extracted table\*\*.*$", re.M)
_FOOTNOTE_DEF = re.compile(r"^\s*\[\^[^\]]+\]:.*(?:\n(?!\s*(?:\[\^|$)).*)*", re.M)
_FOOTNOTE_REF = re.compile(r"\[\^[^\]]+\]")
_RULE_ONLY = re.compile(r"^\s*-{3,}\s*$", re.M)


def strip_scaffolding(body: str) -> str:
    """Remove viewer-only provenance from a clause body, leaving the surveyed text."""
    body = _SOURCE_LINE.sub("", body)
    body = _MINERU_NOTE.sub("", body)
    body = _VIEWER_NOTE.sub("", body)
    body = _SNAPSHOT_IMG.sub("", body)
    body = _SUP_EL.sub(lambda m: f"[^{m.group(1).strip()}]" if m.group(1).strip() else "", body)
    body = _INLINE_TAG.sub("", body)
    body = _RULE_ONLY.sub("", body)             # the rule that separated the footnote block
    return re.sub(r"\n{3,}", "\n\n", body).strip()


_PIPE_ROW = re.compile(r"\s*\|.*\|\s*$")


_BULLET = re.compile(r"^\s*[-•▪◦–]\s+")


def _elements(text):
    """Split one window into viewer elements, restoring the structure a flat kind:'answer'
    blob would lose (see web.py renderEls):
      - a block of markdown pipe-table lines -> kind:'table'  (monospace <pre>)
      - lines starting with '- '/'•'          -> kind:'bullet' (<li>, grouped into <ul>)
      - any other blank-line-separated block   -> kind:'answer' (<p>)
    All elements stay in the clause's own section (co-located; a separate #c section would
    fall outside _subtree)."""
    out, para, tbl = [], [], []

    def flush_para():
        t = re.sub(r"\s+", " ", " ".join(para)).strip()
        if t:
            out.append({"kind": "answer", "text": t})
        para.clear()

    def flush_tbl():
        if tbl:
            out.append({"kind": "table", "text": "\n".join(tbl)})
            tbl.clear()

    for ln in text.split("\n"):
        if _PIPE_ROW.match(ln):                # table row (contiguous; no blank line needed)
            flush_para(); tbl.append(ln); continue
        flush_tbl()
        if not ln.strip():                     # blank line ends the current paragraph
            flush_para()
        elif _BULLET.match(ln):                # bullet -> its own <li>
            flush_para(); out.append({"kind": "bullet", "text": _BULLET.sub("", ln).strip()})
        else:
            para.append(ln)                    # accumulate a prose paragraph
    flush_tbl(); flush_para()
    return out or [{"kind": "answer", "text": re.sub(r"\s+", " ", text).strip()}]


# The trees write a footnote body two ways — "[^7]: text" and "[^7] text" (1,890 of the
# latter in the DP/SD build) — so both count as a definition. A bare marker alone on a
# line does not: that is a reference, and swallowing it would eat the line after it.
_FN_DEF_LINE = re.compile(r"^\s*\[\^([^\]]+)\](?::[ \t]*|[ \t]+)(\S.*)$")


def extract_footnotes(body: str):
    """-> (body without the definition block, {id: text}).

    A definition runs from "[^7]: ..." until the next definition or a blank line, so a
    wrapped citation keeps its tail. Markers in the prose are left alone: they are what
    the reader turns into superscripts, and they are what ties a superscript to an entry."""
    out, notes, cur = [], {}, None
    for ln in body.split("\n"):
        m = _FN_DEF_LINE.match(ln)
        if m:
            cur = m.group(1).strip()
            notes[cur] = m.group(2).strip()
            continue
        if cur is not None and ln.strip() and not ln.lstrip().startswith("|"):
            notes[cur] = (notes[cur] + " " + ln.strip()).strip()   # continuation line
            continue
        cur = None
        out.append(ln)
    return "\n".join(out), notes


def footnotes_for(text: str, notes: dict) -> list:
    """The notes actually referenced by this window, in first-appearance order — a windowed
    section must not claim the whole document's citations."""
    seen, out = set(), []
    for mid in _FOOTNOTE_REF_ID.findall(text):
        mid = mid.strip()
        if mid in notes and mid not in seen:
            seen.add(mid)
            out.append({"id": mid, "text": notes[mid]})
    return out


_FOOTNOTE_REF_ID = re.compile(r"\[\^([^\]]+)\]")


# Material that sits outside the A-K parts but is still CONTENT. derive_key() dropped every
# top-level file as a "pure TOC/index file" — true of README, wrong about the rest. A
# glossary is a table of legal DEFINITIONS ("Personal Data", "Controller", "Sensitive Data",
# each with the meaning the rest of the document relies on), and appendices and annexes
# carry substantive rules. Measured across the extracted corpus: 788 files and 415,181 words
# in 160 documents, of which the glossary alone is 78 files and 115,636 words — none of it
# reaching search or AI Mode, while the legacy docx index DID carry glossary sections. So
# this was a regression against what dev served, not merely a gap.
#
# Keys of their own (GLOSSARY, APPENDIX-2, ANNEX-3) rather than being forced under a part:
# a clause key is always a letter followed by digits, so these cannot collide, and level 1
# with no parent puts them beside the parts in the reader.
_EXTRA_LABELS = ("GLOSSARY", "APPENDIX", "ANNEX", "SCHEDULE")
_EXTRA_KINDS = (("glossary", "GLOSSARY"), ("definitions", "GLOSSARY"),
                ("appendix", "APPENDIX"), ("annex", "ANNEX"), ("schedule", "SCHEDULE"))
# Pure navigation — no content of its own, and the tree already carries the structure.
_NAV_FILE = re.compile(r"^(readme|index|contents|table-of-contents)$", re.I)


def extra_key(rel: str, seen: dict) -> str | None:
    """A key for a non-part content file, or None if the file is pure navigation.

    Numbered per label in tree order so two appendices stay distinct and stable across
    rebuilds (APPENDIX, APPENDIX-2, ...)."""
    parts = rel.split(os.sep)
    name = os.path.splitext(parts[-1])[0].lower()
    if _NAV_FILE.match(name):
        return None
    haystack = f"{parts[0].lower()}/{name}"
    for needle, label in _EXTRA_KINDS:
        if needle in haystack:
            n = seen.get(label, 0) + 1
            seen[label] = n
            return label if n == 1 else f"{label}-{n}"
    return None


# ---- trees whose outline carried no part letters -------------------------------------
#
# derive_key needs a part letter, and it reads one off the top-level folder name, which comes
# from the document's own outline. Plenty of outlines carry no letters, and every file in
# those documents was dropped without a word: 590,706 in Shareholding Disclosure and 434,570
# in Data Privacy, e.g. Italy 96416 kept ONE file of its 129,636 words.
#
# The letter is not guessable from position — for some documents SD part E (short selling) is
# the only part present, and counting from the left would call it A — but it IS learnable.
# ~100 SD and ~75 DP trees do carry letters and use the same folder names, so part_lexicon
# reads the answer off them (see that module for the evidence). What remains is choosing the
# LEVEL to resolve at, because a letterless tree usually wraps everything in one folder named
# after the jurisdiction:
#
#   Croatia   01-short-selling/{01-introduction, 02-shares, ...}
#             the wrapper is itself a part name -> E, and its children are E's sections
#   Iceland   02-iceland/{01-background-to-tda…, …, 01-restrictions-on-investment}
#             the wrapper is a jurisdiction -> descend; the children are A's sections, and
#             restrictions-on-investment starts B
#   Italy     02-italy/{01-breach-response, 04-processing-requirements, …} — same, in DP
#
# Clause files carry their own "N.M-" numbering, so once parts[0] has the right letter the
# existing key logic produces exactly the keys a lettered tree would.

_SRC_PAGE = re.compile(r"\*Source:[^*\n]*page (\d+)", re.I)


def _first_page(d: str) -> int:
    """Lowest source page at a path — the tree's own record of document order, which the
    folder NAMES cannot give when their ordinals restart. Accepts a file as well as a
    folder: loose files interleave with the folders, and treating them as pageless sorted
    every one of them to the end of the document, where they inherited the last part."""
    if os.path.isfile(d):
        m = _SRC_PAGE.search(open(d, encoding="utf-8", errors="ignore").read(4000))
        return int(m.group(1)) if m else 10 ** 6
    pages = []
    for f in sorted(glob.glob(os.path.join(d, "**", "*.md"), recursive=True))[:4]:
        m = _SRC_PAGE.search(open(f, encoding="utf-8", errors="ignore").read(4000))
        if m:
            pages.append(int(m.group(1)))
    return min(pages) if pages else 10 ** 6


def _lexicon(root: str) -> dict:
    """The part lexicon for the product this tree belongs to (out/corpus/<product>/<job>/
    03_stage3_final). Empty when the tree sits outside a corpus layout — then nothing is
    inferred, which is the old behaviour."""
    try:
        from pathlib import Path

        import part_lexicon
        return part_lexicon.load(Path(root).resolve().parent.parent)
    except Exception:
        return {}


def _resolve_level(names: list, base: str, lex: dict) -> dict:
    """Name -> part letter, in document order, carrying the current part forward.

    Inheritance is what makes the ambiguous names work: "how-to-make-a-disclosure" ends five
    different parts, so the corpus cannot say which one it is, but it always FOLLOWS its
    part's other sections — and the part before it is known.

    Loose FILES at this level are resolved the same way. They are sections whose part folder
    the outline never created ("02-iceland/04-how-to-make-a-notification-disclosure.md",
    2,712 words), and they sit in document order among the folders, so the part that is open
    when they appear is theirs."""
    import part_lexicon

    out, current = {}, None
    parts_lex = lex.get("parts", {})
    for n in sorted(names, key=lambda x: _first_page(os.path.join(base, x))):
        low = os.path.splitext(n)[0].lower()
        if _NAV_FILE.match(low) or any(k in low for k, _ in _EXTRA_KINDS):
            continue        # navigation, or a glossary/appendix extra_key labels properly
        own = part_letter(n)
        if own:
            # The name states its own part ("I-changes-in-regulation.md"). Nothing to infer,
            # and inheriting over it is how part I ended up filed under K.
            current = own
            continue
        is_part = part_lexicon.norm(n) in parts_lex
        letter = part_lexicon.letter_for(lex, n)
        if letter:
            current = letter
        if current:
            out[n] = (current, is_part)
    return out


def infer_structure(root: str):
    """-> (wrapper_to_strip, {folder: part letter}) for a tree with no A-K part folders."""
    if not os.path.isdir(root):
        return None, {}
    dirs = sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)) and not d.startswith("_"))
    if not dirs:
        return None, {}
    lettered = [d for d in dirs if part_letter(d)]
    if lettered and len(lettered) == len(dirs):
        return None, {}                      # fully lettered: nothing to infer
    lex = _lexicon(root)
    if not lex.get("parts"):
        return None, {}      # nothing learned -> infer nothing, rather than guess
    import part_lexicon

    # A single wrapper that is not itself a part (it names the jurisdiction) hides the real
    # level; descend through it. One that IS a part (Croatia's "01-short-selling") stays.
    wrapper = None
    if len(dirs) == 1 and not part_lexicon.letter_for(lex, dirs[0]):
        inner = [d for d in os.listdir(os.path.join(root, dirs[0]))
                 if os.path.isdir(os.path.join(root, dirs[0], d)) and not d.startswith("_")]
        if inner:
            wrapper, dirs, root = dirs[0], sorted(inner), os.path.join(root, dirs[0])
    loose = [f for f in os.listdir(root)
             if f.endswith(".md") and not re.match(r"(README|glossary)", f, re.I)]
    if lettered:
        # MIXED: some parts carry their letter, some do not (Brazil's "08-breach-response"
        # beside A-K). Resolve the unlettered ones by NAME only. Inheritance is for a tree
        # with no letters at all, where document order is the only signal; here it would
        # hand a folder the letter of whichever part precedes it — Canada's per-province
        # annexes would become clauses of a federal part, and a reader asking for Canada's
        # H1.4 would get Alberta's answer.
        import part_lexicon
        letters = {}
        for d in [x for x in dirs if not part_letter(x)] + loose:
            letter = part_lexicon.letter_for(lex, d)
            if letter:
                letters[d] = (letter, part_lexicon.norm(d) in lex.get("parts", {}))
        return None, letters
    letters = _resolve_level(dirs + loose, root, lex)
    if not letters:
        # Nothing resolved. Returning the wrapper anyway would strip a path component for no
        # gain, and a folder that merely LOOKS lettered ("e-something") would then be read as
        # part E — inventing keys out of a tree we failed to understand.
        return None, {}
    return wrapper, letters


def canonical_rel(rel: str, wrapper, letters: dict) -> str:
    """Rewrite a path so the EXISTING key logic applies: strip the wrapper, and supply the
    part letter the outline never had.

    A folder that IS a part becomes the letter folder. A folder that is a SECTION of a part
    gets a synthetic part folder above it and KEEPS its own ordinal — folding the letter into
    the section folder instead (`B-02-organisation/…`) discarded that ordinal, and then every
    section's `00-overview.md` in the part derived the bare part key and they collided, ten
    of them in Italy alone."""
    parts = rel.split(os.sep)
    if wrapper and parts and parts[0] == wrapper:
        parts = parts[1:]
        if not parts:
            return rel
    entry = letters.get(parts[0]) if parts else None
    if entry:
        letter, is_part = entry
        if is_part:
            parts[0] = f"{letter}-{parts[0]}"
        else:
            parts = [f"{letter}-part"] + parts
    return os.sep.join(parts)


def _dedupe_key(key: str, used: dict) -> str:
    """Keep a second section from silently replacing the first under the same key.

    Inference can land two files on one key — a part-level file numbered like a section its
    part already has. sections_by_key is a dict downstream, so the loser would simply vanish
    from the reader while still occupying an index row."""
    n = used.get(key, 0)
    used[key] = n + 1
    return key if n == 0 else f"{key}~{n + 1}"


def title_body(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    title, start = "", 0
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = re.sub(r"^\d+(\.\d+)*\s+", "", ln[2:].strip()).strip()
            start = i + 1
            break
    body = "\n".join(lines[start:]).strip()
    return (title or re.sub(r"\.md$", "", os.path.basename(path))), body

_FLAT_ORDINAL = re.compile(r"^(\d+)[-_.]")
_FLAT_KEY = re.compile(r"^S\d+$")


def is_flat_tree(root: str) -> bool:
    """True when a product's sections are FILES at the tree root, not nested folders.

    Measured over out/corpus, 16 of 37 extracted products are shaped this way (Marketing
    Restrictions - Asset Management, repoAnalytics, netalytics, diligence, DRV Repo, …) — they have
    no lettered parts to walk, because product_rules gives them SECTION_DEPTH 0. derive_key needs
    a part letter and returns None for every one of their files, so before this the whole product
    converted to ZERO rows and simply never reached the index."""
    # ANY depth, not just one level down: a Data Privacy tree keeps its clauses three deep
    # (<part>/<section>/<clause>.md), so a one-level probe finds nothing there and would call the
    # most nested product in the corpus "flat".
    return not any(os.sep in os.path.relpath(p, root)
                   for p in glob.glob(f"{root}/**/*.md", recursive=True))


def flat_key(rel: str, seq: int) -> str | None:
    """S<NN> for one section of a flat tree, numbered from the file's own ordinal prefix.

    The ordinal is the document's own section numbering ("15-marketing-activities.md" -> S15), so
    a key stays stable when a re-extraction renames the slug, and a reader can line the key up
    against the tree. Nothing downstream changes: a long section is still split into `<key>#c<i>`
    windows by the embedder and collapsed back to the whole clause at query time, exactly as a
    nested product's clause is."""
    if os.path.dirname(rel):
        return None
    m = _FLAT_ORDINAL.match(os.path.basename(rel))
    return f"S{int(m.group(1)):02d}" if m else f"S{seq:02d}"


def key_meta(key):
    rest = key[1:]
    level = rest.count(".") + rest.count("(") + (1 if rest else 0)
    if "(" in rest:
        parent = key[:key.index("(")]
    elif "." in rest:
        parent = key[:key.rfind(".")]
    elif rest:
        parent = key[0]
    else:
        parent = None
    return level, parent

def build(OUT: str, ROOT: str, JUR: str, PROD: str) -> int:
    """Convert one stage-3 tree into a keyed content.json. Returns the row count.

    A FUNCTION, not module-level work: the key derivation here (derive_key /
    extra_key) is the single source of truth for what reaches the index, and an
    audit that wants to ask "what did we drop?" must be able to import it without
    running a conversion or needing argv."""
    secs, seen_body, extra_seen, used_keys = [], set(), {}, {}
    at_key: dict = {}          # key -> (section, source folder), for fragments of one clause
    wrapper, letters = infer_structure(ROOT)
    flat = is_flat_tree(ROOT)
    seq = 0
    for p in sorted(glob.glob(f"{ROOT}/**/*.md", recursive=True)):
        rel_path = os.path.relpath(p, ROOT)
        canon = canonical_rel(rel_path, wrapper, letters) if (wrapper or letters) else rel_path
        seq += 1
        key = derive_key(canon) or extra_key(rel_path, extra_seen)
        # A flat product has no part letter for derive_key to find, so it keys by the section's
        # own ordinal. extra_key still wins where it applies, so an appendix keeps its label.
        if not key and flat:
            key = flat_key(rel_path, seq)
        if not key:
            continue
        title, body = title_body(p)
        # MinerU <table> HTML -> markdown pipe (viewer escapes HTML); unparseable tables are
        # tag-stripped to text. Done in-body so windowing/sectioning stays exactly as before.
        body = _TABLE_RE.sub(
            lambda m: "\n" + (_table_to_md(m.group(0)) or re.sub(r"<[^>]+>", " ", m.group(0))) + "\n", body)
        body = strip_scaffolding(body)
        body, notes = extract_footnotes(body)
        if len(body) < 20:
            continue
        hb = hash(body[:300])
        if hb in seen_body:            # skip an exact-ish duplicate body
            continue
        seen_body.add(hb)
        # A key already used means one of two different things, and they need opposite
        # treatment. The extractor routinely splits ONE clause across files when the source
        # repeats its heading on later pages — Italy's clause 1.1 arrives as four files from
        # pages 195, 210-211, 243-244 and 253 — and those belong in one section, in page
        # order, or the reader gets a fragment while the rest is invisible to it (they were
        # all embedded, but sections_by_key keeps a single row per key). Two DIFFERENT
        # sections landing on one key is the other case: those must stay separate, so the
        # second takes a suffix rather than absorbing unrelated text.
        folder = os.path.dirname(rel_path)
        prev = at_key.get(key)
        if prev is not None and prev[1] == folder:
            prev_sec = prev[0]
            prev_sec["elements"].extend(_elements(body))
            have = {f.get("id") for f in prev_sec["footnotes"]}
            prev_sec["footnotes"].extend(f for f in footnotes_for(body, notes)
                                         if f.get("id") not in have)
            continue
        if prev is not None:
            key = _dedupe_key(key, used_keys)
        used_keys.setdefault(key, 1)
        # a GLOSSARY / APPENDIX-2 key has no part structure to parse
        # A flat "S15" has no part/clause structure to parse, same as a GLOSSARY/APPENDIX label.
        level, parent = ((1, None)
                         if key.split("-")[0] in _EXTRA_LABELS or _FLAT_KEY.match(key)
                         else key_meta(key))
        # ONE SECTION PER CLAUSE, whole. Windowing is an EMBEDDING concern — a long clause
        # does not fit one vector, so the embedder splits it (section_index._windows, keyed
        # `<key>#c<i>`, collapsed back to the clause by registry at query time). Making each
        # window its own SECTION pushed that split into the product: a comparison table that
        # spanned two windows arrived as `E2.2` and `E2.2#c1`, each holding half the rows, and
        # the reader drew two broken tables. The viewer gets the clause; the index does the
        # chunking.
        sec = {"key": key, "title": title, "level": level,
               "parent_key": parent, "elements": _elements(body),
               "footnotes": footnotes_for(body, notes)}
        secs.append(sec)
        at_key[key] = (sec, folder)

    content = {"doc": {"jurisdiction": JUR, "product": PROD}, "sections": secs,
               "answers_by_clause": {}, "guidance_by_id": {}, "alerts_by_id": {},
               "alerts": [], "alerts_by_clause": {}, "cited_by": {}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(content, open(OUT, "w"), ensure_ascii=False)
    base = {s["key"].split("#")[0] for s in secs}
    print(f"wrote {len(secs)} rows, {len(base)} unique clause keys -> {OUT}")
    return len(secs)


def main() -> None:
    out = sys.argv[1]
    root = sys.argv[2] if len(sys.argv) > 2 else "data-privacy-uk-april-2026"
    jur = sys.argv[3] if len(sys.argv) > 3 else "United Kingdom"
    prod = sys.argv[4] if len(sys.argv) > 4 else "Data Privacy"
    build(out, root, jur, prod)


if __name__ == "__main__":
    main()
