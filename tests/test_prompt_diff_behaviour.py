"""The prompt diff, actually executed (AOSNG-3442).

The rest of the Prompts screen is pinned by source assertions, which is the repo's pattern for
web.py. That pattern is not enough here: `prLineDiff` is real algorithm code — an LCS over
lines — and "the source contains an LCS-shaped loop" says nothing about whether the diff it
produces is TRUE. A diff that quietly drops or duplicates a line is worse than no diff, because
this screen exists so a reviewer can see what an SME changed in a prompt that decides what the
assistant asserts about regulated content.

So the function is lifted out of the page and run under node against the real default prompt.
The property tested is reconstruction: reading the diff's kept-and-removed lines must rebuild
the LEFT side exactly, and its kept-and-added lines the RIGHT side exactly. That single
property catches every drop, duplication and mis-ordering.

node is a soft dependency — it is already used to syntax-check this page during development —
so the test skips rather than fails where it is absent.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
WEB = (ROOT / "src/aosphere_core_index/service/web.py").read_text(encoding="utf-8")

node = shutil.which("node")
pytestmark = pytest.mark.skipif(not node, reason="node is not installed")


def _fn(name: str) -> str:
    i = WEB.index(f"function {name}(")
    return WEB[i:WEB.index("\n}\n", i) + 3]


def _run(pairs: list[tuple[str, str]]) -> list[list[list[str]]]:
    """Run prLineDiff over each (a, b) pair inside node and return the raw rows."""
    script = _fn("prLineDiff") + (
        "const pairs=JSON.parse(require('fs').readFileSync(0,'utf8'));\n"
        "console.log(JSON.stringify(pairs.map(([a,b])=>prLineDiff(a,b))));\n")
    out = subprocess.run([node, "-e", script], input=json.dumps(pairs),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _default_prompt() -> str:
    from aosphere_core_index.llm.agent import INSTRUCTIONS

    return INSTRUCTIONS


def test_the_diff_reconstructs_both_texts_exactly():
    """The property that matters: no line invented, dropped, duplicated or reordered.

    The cases are the edits an SME actually makes to a master prompt — reword a rule, add one,
    delete one, reorder two — plus the degenerate ends.
    """
    base = _default_prompt()
    lines = base.split("\n")
    long_i = next(k for k, ln in enumerate(lines) if len(ln) > 40)
    cases = [
        (base, base),                                                    # untouched
        (base, "\n".join(lines[:long_i] + ["A REWORDED RULE"] + lines[long_i + 1:])),
        (base, base + "\n\nEXTRA PRODUCT RULE"),                         # append
        (base, "\n".join(lines[:-3])),                                   # truncate
        (base, "\n".join([lines[3], lines[2]] + lines[4:])),             # reorder
        (base, ""),                                                      # cleared
        ("", base),                                                      # written from nothing
        (base, base.replace("\n", "\n\n")),                              # whitespace churn
    ]
    for (a, b), rows in zip(cases, _run(cases), strict=True):
        left = "\n".join(r[1] for r in rows if r[0] != "+")
        right = "\n".join(r[1] for r in rows if r[0] != "-")
        assert left == a, f"the diff does not reconstruct the default ({a[:30]!r}…)"
        assert right == b, f"the diff does not reconstruct the draft ({b[:30]!r}…)"


def test_an_unchanged_prompt_shows_no_changes_at_all():
    """A reviewer must be able to trust 'identical to the default'. A diff that reported
    spurious changes on identical text would make every real change unreadable."""
    base = _default_prompt()
    rows, = _run([(base, base)])
    assert all(r[0] == " " for r in rows)


def test_a_one_line_change_is_reported_as_one_line_changed():
    """Lines, not words, deliberately: the paragraph and bullet structure of this text is the
    part that carries meaning to the model. But a one-line edit must still read as one line —
    if the LCS drifted, a small edit would show as a wholesale rewrite and hide what changed."""
    base = _default_prompt()
    lines = base.split("\n")
    k = next(i for i, ln in enumerate(lines) if len(ln) > 40)
    draft = "\n".join(lines[:k] + [lines[k].replace(" ", " NOT ", 1)] + lines[k + 1:])
    rows, = _run([(base, draft)])
    assert sum(1 for r in rows if r[0] == "+") == 1
    assert sum(1 for r in rows if r[0] == "-") == 1


def test_the_size_guard_only_ever_fires_on_input_the_api_would_refuse():
    """The guard is a safety net, not a working mode. An override is capped at 20,000
    characters server-side, so the diff a user can actually reach must be a REAL diff — if the
    cap were set low enough to catch a legitimate prompt, reviewers would silently start seeing
    'everything replaced' instead of the change."""
    base = _default_prompt()
    worst = "\n".join(["a line of prompt prose, roughly fifty characters."] * 400)  # ~20k chars
    assert len(worst) >= 20000 - 60
    rows, = _run([(base, worst)])
    assert not (len(rows) == 2 and rows[0][0] == "-" and rows[1][0] == "+"), \
        "the largest override the API accepts must still produce a real diff"
    # and beyond it, the fallback is a whole-text replacement rather than a hang or a throw
    huge = "\n".join(f"line {i}" for i in range(3000))
    rows, = _run([(huge, huge + "\nx")])
    assert [r[0] for r in rows] == ["-", "+"]


def test_the_diff_is_only_lifted_from_the_page_never_duplicated():
    """This test executes a COPY of the function. If the page ever gained a second definition,
    the copy under test might not be the one that runs."""
    assert len(re.findall(r"function prLineDiff\(", WEB)) == 1
