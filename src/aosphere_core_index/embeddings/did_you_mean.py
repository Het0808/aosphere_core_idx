"""Typo detection for search queries — a "did you mean?" suggester.

Complements (does not replace) the existing query paths: jurisdiction aliases
(regions/aliases.py) normalize naming variants, and LLM query expansion
(query_expand.py) bridges jargon. What neither catches is misspellings —
"cooker walls" (cookie walls), "PCPI" (PIPC) — where the embedding silently
degrades. This module checks query tokens against a domain lexicon built from
the index's own content (section titles + body text) plus jurisdiction/alias
vocabulary, and returns a corrected query for the UI to offer as a suggestion.
It never rewrites the query it's given — the caller decides what to do with
the suggestion (suggest-only per product decision; retrieval is untouched).

Dependency-free (difflib + a vendored English wordlist) and offline-safe: the
lexicon is built from the same published content.json artifacts the service
already serves, so it works in ACI_OFFLINE pods and tracks index rebuilds.

Three conservative rules (measured: 14 suggestions on the 722-question eval
suite, all genuine typos or harmless; see tests):
1. UNKNOWN WORD — token is neither common English nor domain vocabulary →
   fuzzy-match against domain words ("demoninator" -> "denominator").
2. ACRONYM TRANSPOSITION — short unknown all-caps token whose letters anagram
   to a domain acronym ("PCPI" -> "PIPC"); corpus frequency breaks ties.
3. BIGRAM REPAIR — token IS a valid English word, but the corrected token
   forms a corpus-attested bigram with a content-word neighbour ("cooker
   walls" -> "cookie walls"). Catches typos that happen to spell real words.
"""

from __future__ import annotations

import gzip
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")
# Tokens with digits (clause keys "A3.2", thresholds "5%") are never touched.
_ACRO_RE = re.compile(r"[A-Z]{2,6}s?")  # acronyms incl. plural ("CFDs")
_CUTOFF = 0.80
_MIN_LEN = 4           # shorter unknown tokens: anagram rule only
_ACRO_LEN = (4, 6)     # anagram window; 3-char acronyms are too collision-prone
_BODY_MIN_FREQ = 3     # body word must recur to enter the vocabulary
_BIGRAM_MIN_FREQ = 2   # bigram must recur to count as attested phrasing

# Function words: never corrected themselves, and never trusted as the
# neighbour in a bigram repair ("deadline FOR" must not suggest "dealing").
_STOP = frozenset(
    "the a an and or of in on for to with is are was were be been do does did "
    "can could shall should will would may might must have has had you your we "
    "our it its they their there this that these those what which who when how "
    "where why any all some no not if as at by from about between under over "
    "make get was other more most".split()
)

_ENGLISH: set[str] | None = None


def _english() -> set[str]:
    """Vendored common-English wordlist (top ~59k by frequency, incl. British
    spellings). Lazy: loaded once, only when a suggestion is computed."""
    global _ENGLISH
    if _ENGLISH is None:
        p = Path(__file__).parent / "english_words.txt.gz"
        with gzip.open(p, "rt", encoding="utf-8") as f:
            _ENGLISH = {w.strip() for w in f}
    return _ENGLISH


@dataclass
class Lexicon:
    words: set[str]                       # domain vocabulary
    freq: Counter = field(default_factory=Counter)              # corpus word counts
    after: dict[str, set[str]] = field(default_factory=dict)    # w -> words following w
    before: dict[str, set[str]] = field(default_factory=dict)   # w -> words preceding w


def _norm(token: str) -> str:
    t = token.lower().rstrip("'")           # trailing/plural apostrophe: issuers'
    return t[:-2] if t.endswith("'s") else t


def build_lexicon(titles: list[str], extra: list[str] | None = None,
                  bodies: list[str] | None = None) -> Lexicon:
    """Domain vocabulary and attested bigrams.

    - titles: section titles — every word (len>=3) enters the vocabulary and
      every adjacent pair becomes a bigram (titles are trusted).
    - extra: jurisdiction names, alias keys, regulator acronyms (len>=2).
    - bodies: clause body text — words recurring >= _BODY_MIN_FREQ enter the
      vocabulary; bigrams of two vocabulary words recurring >= _BIGRAM_MIN_FREQ
      are attested. Frequencies also break anagram/close-match ties.
    """
    lex = Lexicon(set())

    def add_bigram(a: str, b: str) -> None:
        lex.after.setdefault(a, set()).add(b)
        lex.before.setdefault(b, set()).add(a)

    for t in titles:
        toks = [_norm(w) for w in _WORD_RE.findall(t)]
        for w in toks:
            if len(w) >= 3:
                lex.words.add(w)
                lex.freq[w] += 1
        for a, b in zip(toks, toks[1:]):
            add_bigram(a, b)
    for t in extra or []:
        for w in (_norm(x) for x in _WORD_RE.findall(t)):
            if len(w) >= 2:
                lex.words.add(w)
                lex.freq[w] += 1
    if bodies:
        wc: Counter = Counter()
        pairs: Counter = Counter()
        for t in bodies:
            toks = [_norm(w) for w in _WORD_RE.findall(t)]
            wc.update(w for w in toks if len(w) >= 3)
            pairs.update((a, b) for a, b in zip(toks, toks[1:])
                         if len(a) >= 4 and len(b) >= 4
                         and a not in _STOP and b not in _STOP)
        for w, n in wc.items():
            if n >= _BODY_MIN_FREQ:
                lex.words.add(w)
            lex.freq[w] += n
        for (a, b), n in pairs.items():
            if n >= _BIGRAM_MIN_FREQ and a in lex.words and b in lex.words:
                add_bigram(a, b)
    return lex


def _inflection_of(a: str, b: str) -> bool:
    """True when a/b differ only by a short suffix (cookie/cookies,
    publish/published, borrow/borrowing) — never worth suggesting."""
    a, b = (a, b) if len(a) <= len(b) else (b, a)
    return b.startswith(a) and len(b) - len(a) <= 3


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _close_match(token: str, lex: Lexicon,
                 prev: str | None = None, nxt: str | None = None) -> str | None:
    """Best domain word for an unknown token. Candidates pre-filtered by first
    letter and length so the difflib pass stays cheap. A candidate forming an
    attested bigram with a neighbour outranks a marginally closer one
    ("substsntial hilding": 'holding' beats 'hiding' via "substantial holding");
    corpus frequency breaks remaining ties. No inflection guard here — the
    token is not a real word, so its inflected stem is a fine suggestion
    ("disclos" -> "disclose").

    Ranking: banded ratio first (a clearly closer word always wins:
    "disclosble" -> "disclosable" 0.95, not an attested-but-distant
    "disclose" 0.89), bigram attestation breaks near-ties within a band,
    then corpus frequency. Only content-word neighbours count — a stopword
    neighbour ("in", "for") attests nothing."""
    def content(w: str | None) -> str | None:
        return w if w and w not in _STOP and len(w) >= 4 else None

    prev, nxt = content(prev), content(nxt)
    first, n = token[0], len(token)
    best, best_key = None, (0.0, False, 0.0, 0)
    for w in lex.words:
        if w[0] != first or abs(len(w) - n) > 2 or w == token:
            continue
        r = _ratio(token, w)
        if r < _CUTOFF:
            continue
        attested = ((prev is not None and w in lex.after.get(prev, ()))
                    or (nxt is not None and w in lex.before.get(nxt, ())))
        key = (round(r, 1), attested, r, lex.freq[w])
        if key > best_key:
            best, best_key = w, key
    return best


def _anagram_match(token: str, lex: Lexicon) -> str | None:
    """Same-letters domain word for short acronyms (pcpi -> pipc). Multiple
    hits (pipc vs picp): the corpus-dominant one wins; a true tie stays None."""
    sig = sorted(token)
    hits = [w for w in lex.words if len(w) == len(token) and w != token and sorted(w) == sig]
    if not hits:
        return None
    # A single adjacent-letter swap is the classic typo: if exactly one hit is
    # one swap away, it wins outright ("dasr" -> "dsar", not the more frequent
    # "adrs"). Otherwise the corpus-dominant hit wins; a near-tie stays None.
    swaps = [w for w in hits
             if any(token[:j] + token[j + 1] + token[j] + token[j + 2:] == w
                    for j in range(len(token) - 1))]
    if len(swaps) == 1:
        return swaps[0]
    hits.sort(key=lambda w: -lex.freq[w])
    if len(hits) > 1 and lex.freq[hits[0]] < 2 * max(1, lex.freq[hits[1]]):
        return None  # no clear winner — a wrong suggestion is worse than none
    return hits[0]


def _bigram_repair(token: str, prev: str | None, nxt: str | None, lex: Lexicon) -> str | None:
    """Correction for a VALID English word that isn't domain vocabulary, when
    the correction forms an attested bigram with a CONTENT-WORD neighbour
    ("cooker walls" -> "cookie walls"). Guards: the neighbour must be domain
    vocabulary (stopword bigrams like "deadline for" prove nothing), the raw
    bigram must be unattested, and the candidate must be a substantial word."""
    for other, partners in ((nxt, lex.before), (prev, lex.after)):
        if not other or other in _STOP or len(other) < 4 or other not in lex.words:
            continue
        cands = partners.get(other, ())
        if token in cands:
            return None  # bigram already attested — nothing to repair
        best, best_key = None, (0.0, 0)
        for c in cands:
            if len(c) < 4 or _inflection_of(token, c):
                continue
            key = (_ratio(token, c), lex.freq[c])
            if key > best_key:
                best, best_key = c, key
        if best and best_key[0] >= _CUTOFF:
            return best
    return None


def suggest(query: str, lexicon: Lexicon) -> str | None:
    """Corrected query if any token looks like a typo, else None. Corrections
    are lowercase, except acronym fixes (upper) and capitalized originals."""
    if not query or not lexicon.words:
        return None
    matches = list(_WORD_RE.finditer(query))
    norms = [_norm(m.group(0)) for m in matches]
    out, changed, pos = [], False, 0
    eng = _english()
    for i, m in enumerate(matches):
        token, tl = m.group(0), norms[i]
        out.append(query[pos:m.start()])
        pos = m.end()
        fix = None
        if tl in _STOP or tl in lexicon.words:
            pass
        elif "-" in tl and all(p in eng for p in tl.split("-") if p):
            pass  # hyphenated compound of real words ("opt-in") — known
        elif _ACRO_RE.fullmatch(token):
            # Acronyms: fuzzy matching is unsafe (CFDs ~ CDS); anagram only,
            # on the singular stem, keeping the original case.
            stem = tl[:-1] if token.endswith("s") else tl
            if _ACRO_LEN[0] <= len(stem) <= _ACRO_LEN[1] and stem not in eng:
                fix = _anagram_match(stem, lexicon)
                if fix:
                    fix = fix.upper() + ("s" if token.endswith("s") else "")
        elif tl not in eng:
            if len(tl) >= _MIN_LEN:
                fix = _close_match(tl, lexicon, norms[i - 1] if i else None,
                                   norms[i + 1] if i + 1 < len(norms) else None)
            if fix is None and _ACRO_LEN[0] <= len(tl) <= _ACRO_LEN[1]:
                fix = _anagram_match(tl, lexicon)
        elif len(tl) >= _MIN_LEN:
            fix = _bigram_repair(tl, norms[i - 1] if i else None,
                                 norms[i + 1] if i + 1 < len(norms) else None, lexicon)
        if fix and token[0].isupper() and not fix[0].isupper():
            fix = fix.capitalize()  # proper-noun typo keeps its capital (Britan -> Britain)
        if fix:
            out.append(fix)
            changed = True
            norms[i] = _norm(fix)  # corrected token becomes bigram context for the next one
        else:
            out.append(token)
    out.append(query[pos:])
    return "".join(out) if changed else None
