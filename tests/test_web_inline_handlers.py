"""An inline onclick cannot carry a value that contains a double quote.

The Extraction tab's version picker was written as

    '<button ... onclick="pickExVersion('+JSON.stringify(r.run)+')">'

and JSON.stringify emits DOUBLE quotes, inside a DOUBLE-quoted HTML attribute:

    onclick="pickExVersion("2026-08-21-02")"

The browser ends the attribute at the second quote, so the handler is `pickExVersion(` — a
syntax error — and the rest becomes stray attributes. Nothing throws, nothing logs; the button
is simply dead. That picker silently did nothing for as long as it existed, and it was invisible
because loadExRuns already defaults to the newest run, which is what people wanted most of the
time. The same defect then got copied into the "Browse run" button.

web.py already carries the rule in a comment ("Keys ride in data-* attributes, never in an
inline onclick"), and exRetry already escapes its value. This is that rule as a test, because a
comment two hundred lines away does not stop the next person concatenating a handler.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src/aosphere_core_index/service/web.py"
RAW = WEB.read_text(encoding="utf-8")


def _without_comments(src: str) -> str:
    """Blank out whole-line JS/Python comments, keeping line numbers intact.

    Needed because the comment explaining this very bug QUOTES the broken markup, and a guard
    that flags its own documentation is a guard people delete."""
    out = []
    for line in src.split("\n"):
        out.append("" if line.lstrip().startswith(("//", "#")) else line)
    return "\n".join(out)


SRC = _without_comments(RAW)

# A double-quoted inline handler being built by string concatenation, up to the closing quote.
_HANDLER = re.compile(r'on(?:click|change|input|submit)="[^"\n]*?\'\s*\+[^\n]*?\+\s*\'[^"\n]*?"')


def _interpolations(handler: str) -> list[str]:
    """The '+ ... +' expressions spliced into one attribute value."""
    return re.findall(r"\+([^+]*?)\+", handler)


def test_no_inline_handler_splices_a_raw_json_stringify():
    """JSON.stringify is the specific trap: it is the obvious way to quote a value and it is
    the one that breaks. Escaping it (exRetry does) or moving the value to a data-* attribute
    (the run bar now does) are both fine."""
    offenders = []
    for m in _HANDLER.finditer(SRC):
        h = m.group(0)
        for expr in _interpolations(h):
            if "JSON.stringify" in expr and "&quot;" not in expr:
                line = SRC[:m.start()].count("\n") + 1
                offenders.append(f"web.py:{line}  {h[:96]}")
    assert not offenders, (
        "an inline handler is being given a JSON.stringify value, which emits double quotes "
        "inside a double-quoted attribute and kills the handler:\n  " + "\n  ".join(offenders))


def test_the_run_bar_carries_its_run_id_in_a_data_attribute():
    """The positive half: the fix is present, not merely the bug absent."""
    assert 'data-exrun="' in RAW
    assert 'data-exact="' in RAW
    # and the click is delegated, so a bar rebuilt on every poll keeps working
    assert 'closest("button[data-exrun]")' in RAW


def test_the_run_bar_escapes_the_run_id_it_puts_in_the_attribute():
    bar = re.search(r"function exVerBtn\(.*?\n\}", RAW, re.S)
    assert bar, "exVerBtn not found"
    assert 'escA(run)' in bar.group(0), "the run id must be attribute-escaped"


def test_every_delegated_key_attribute_is_escaped():
    """Product names and jurisdictions carry quotes, ampersands and parentheses — the reason
    the scorecard table moved to data-* in the first place. Any data-key/data-exrun value
    spliced from a variable has to go through escA."""
    bad = []
    for m in re.finditer(r'data-(?:key|jkey|exrun|slug)="\'\s*\+\s*([A-Za-z0-9_.$()\[\]]+)', SRC):
        if not m.group(1).startswith("escA"):
            bad.append(f"web.py:{SRC[:m.start()].count(chr(10)) + 1}  {m.group(0)}")
    assert not bad, "unescaped value in a data-* attribute:\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------------------
# Cancellation policy: leaving a screen must cancel that screen's requests.
#
# The Doc Gallery would flip back to extraction results by itself. Both screens fan out —
# the monitor HEADs the review artefacts of every recent document one at a time, and a run
# gallery indexes ~1000 S3 objects (~15s) — so a continuation lands well after the user has
# clicked away and repaints #detail. An entry-time mode check cannot fix it: the check passes,
# then the work lands late.
#
# What must NOT be cancelled is as important as what must, so both halves are asserted.
# ---------------------------------------------------------------------------

def test_a_screen_change_opens_a_new_abort_scope():
    assert "function newViewScope()" in SRC
    assert "_viewScope.abort()" in SRC, "the previous screen's requests must actually be aborted"
    # every entry point into a different screen's worth of work
    for fn in ("function setMode(", "function pickExVersion(", "function openGalleryRun(",
               "function openGalleryVersion("):
        i = SRC.index(fn)
        assert "newViewScope()" in SRC[i:i + 400], f"{fn} must open a new scope"


def test_authfetch_attaches_the_scope_signal_by_default():
    """Attached centrally so it cannot be forgotten at a call site — there are 20-odd of them."""
    i = SRC.index("async function authFetch(")
    body = SRC[i:i + 400]
    assert "opts.signal===undefined" in body and "_viewScope.signal" in body
    assert "opts.signal=_viewScope.signal" in body


WORK_THAT_MUST_SURVIVE = {
    # app-level state fetched once at startup: cancelling leaves the app without its filters
    "loadRegions": "/api/regions",
    "loadAi": "/api/ai-status",
    # a streaming answer rendered into the CHAT pane; people switch to Search while it thinks
    # (sendChat now only decides whether the guided interview handles the turn — the stream
    # itself moved into streamAnswer, which both paths share)
    "streamAnswer": "/api/agent/stream",
    # the guided interview's own calls: abandoning one mid-flow strands the user on a
    # question card with no way forward, and its state lives in the page
    "ivPost": "/api/interview/",
    # a WRITE — an aborted write may have been applied, with the UI reporting nothing
    "exRetry": "/api/extraction/retry",
    # fills a tab the user opened; cancelling strands that tab on "Loading…"
    "exOpenReview": "/api/extraction/review/",
}


def test_work_that_must_survive_a_screen_change_opts_out_explicitly():
    for fn, endpoint in WORK_THAT_MUST_SURVIVE.items():
        i = SRC.index(f"function {fn}(")
        body = SRC[i:SRC.find("\n}", i)]
        assert endpoint in body, f"{fn} no longer calls {endpoint} — update this test"
        assert ("NO_CANCEL" in body or "signal:null" in body or "signal: null" in body), (
            f"{fn} calls {endpoint} and MUST opt out of cancellation — see the comment on "
            f"NO_CANCEL for why")


def test_a_mutation_is_never_auto_cancelled():
    """Singled out because it is the one with a consequence beyond a wasted request: the server
    may have written the retry marker while the UI reports nothing back."""
    i = SRC.index("function exRetry(")
    body = SRC[i:SRC.find("\n}", i)]
    posts = body.count("method:'POST'") + body.count('method:"POST"')
    assert posts >= 1
    assert body.count("signal:null") >= posts, (
        "every POST in exRetry needs signal:null — it has a normal path and a force path")


def test_an_abort_is_never_rendered_as_an_error():
    """A cancelled request is the expected outcome of clicking away, not a failure to report."""
    assert "function isAbort(" in SRC
    # the two screens the bug was reported on
    for fn in ("loadGalleryTree", "refreshExtraction"):
        i = SRC.index(f"function {fn}(")
        body = SRC[i:SRC.find("\n}", i)]
        assert "isAbort(e)" in body, f"{fn} must not paint an error when the user just left"


def test_an_abort_is_never_cached_as_a_miss():
    """The subtle one. These caches live for the session, so caching an abort as "there is no
    scorecard / no section / no viewer" hides something that exists, permanently, because
    somebody changed screens at the wrong moment."""
    for fn in ("fetchScorecard", "exProbeReview"):
        i = SRC.index(f"function {fn}(")
        body = SRC[i:SRC.find("\n}", i)]
        assert "isAbort(e)" in body, f"{fn} caches its result and must not cache an abort"


def test_renderers_refuse_to_paint_a_screen_the_user_has_left():
    """Belt and braces: a response parsed just before the abort is already in hand."""
    for fn, mode in (("renderExtraction", "extractmode"),
                     ("renderScTable", "gallerymode"),
                     ("renderGallery", "gallerymode")):
        i = SRC.index(f"function {fn}(")
        head = SRC[i:i + 320]
        assert f'classList.contains("{mode}")' in head, f"{fn} must check it is still visible"


def test_the_scorecard_table_does_not_link_a_viewer_it_knows_is_absent():
    """has_viewer===false must suppress the link, not just fail on click. Compared with ===
    false on purpose: an older published manifest has no has_viewer field at all, and undefined
    there has to keep meaning "assume it is there"."""
    i = SRC.index('data-view="')
    cell = SRC[max(0, i - 700):i + 200]
    assert "d.has_viewer===false" in cell, "the viewer link must be conditional"
    assert "no viewer" in cell, "and it should say why there is no link"
