"""The trace graph, EXECUTED — does the path a document took actually light up?

Every other test of this graph reads its source text. That catches a renamed token and
nothing else: the picture is produced by ~350 lines of JS whose whole job is to decide
which of fifteen edges are on, and text assertions cannot see a wrong decision. Three
defects in this file's history were invisible to both the parser and the text tests:

  * `st[k]!=='none'` counted a SKIPPED node as reached, so a blue arrow pointed at a
    hollow node on 42 of 150 real documents — the graph asserting a path it drew as
    not taken.
  * the rise back to the Scorecard was anchored on `acceptable?`, which a page-routed
    document never passes (`fb["accepted"]` is written at the END of run_chain and the
    short-document branch returns first), so it came out of a hollow node on all 14.
  * a tier still RUNNING reported "adopted", off the row's `tier` field alone, with no
    scorecard on disk to support it.

So this runs the real thing in a JS engine and asserts one invariant above all: a blue
edge may never touch a hollow node. Skipped where no engine is installed — quickjs
compiles from source and is not worth making the suite depend on.
"""

import json
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs", reason="no JS engine: pip install quickjs")

GRAPH = (Path(__file__).resolve().parent.parent
         / "src/aosphere_core_index/service/trace_graph.py")

# What each host page supplies. Kept minimal on purpose: anything the graph needs beyond
# this is a hidden coupling, and there is exactly one such helper (exDur).
SHIM = r"""
var esc = function(s){ return String(s==null?"":s).replace(/[&<>]/g, function(c){
  return ({"&":"&amp;","<":"&lt;",">":"&gt;"})[c]; }); };
var escA = esc;
function exDur(s){ return s==null ? '-' : (s<1 ? (Math.round(s*10)/10)+'s' : Math.round(s)+'s'); }
"""

REPORT = r"""
function traceReport(jJson, dJson){
  var j = JSON.parse(jJson), d = JSON.parse(dJson), S = exNodeStates(j, d);
  var svg = exGraph(j, S, null), states = {}, edges = {};
  for (var i=0;i<EXG_STEPS.length;i++){ states[EXG_STEPS[i].k] = S.st[EXG_STEPS[i].k]; }
  var re = /class="exgedge([^"]*)" data-edge="([^"]+)"/g, m;
  while ((m = re.exec(svg)) !== null) { edges[m[2]] = m[1].indexOf('on') >= 0; }
  return JSON.stringify({states: states, edges: edges});
}
"""

WALKED = ("done", "run", "warn", "disc", "crash", "clone")


@pytest.fixture(scope="module")
def trace():
    """The shared graph, loaded once and callable with a job row plus its detail."""
    src = GRAPH.read_text(encoding="utf-8")
    js = src.split('JS = r"""', 1)[1].rsplit('"""', 1)[0]
    ctx = quickjs.Context()
    ctx.eval(SHIM)
    ctx.eval(js)
    ctx.eval(REPORT)
    fn = ctx.get("traceReport")

    def run(job, detail=None):
        return json.loads(fn(json.dumps(job), json.dumps(detail)))
    return run


def _job(**kw):
    j = {"label": "X__1", "gate": "pass", "worst": 91, "tier": "first_pass",
         "steps": {"stage1": 6.0, "stage2_mineru": 574.0, "stage3": 0.1,
                   "validation": 3.5, "scorecard": 0.0}}
    j.update(kw)
    return j


def _det(**kw):
    d = {"gate": "pass", "worst_score": 91, "dimensions": {},
         "timing": {"steps": {}, "seconds": 590.0},
         "fallback": {"triggered": False, "chain": []}}
    d.update(kw)
    return d


def _no_blue_into_hollow(r):
    """THE invariant. Everything else in this file is a special case of it."""
    return [(name, end, r["states"][end])
            for name, on in r["edges"].items() if on
            for end in name.split("->")
            if end in r["states"] and r["states"][end] not in WALKED]


# ---- the healthy document: one straight line, nothing else lit ---------------------

def test_a_clean_document_lights_the_spine_and_nothing_below_it(trace):
    r = trace(_job(), _det())
    assert _no_blue_into_hollow(r) == []
    for e in ("stage1->stage2", "stage2->stage3", "stage3->score"):
        assert r["edges"][e] is True, e
    # the machinery it never entered stays dark
    for e in ("score->needs", "needs->mineru", "short->mineru", "stage1->tocrescue"):
        assert r["edges"][e] is False, e
    # ...and so does the run on to Stage 4, because the AI was never asked for. The blue
    # path of an ordinary document ENDS at the Scorecard, which is where extraction ends.
    assert r["states"]["ai"] == "none"
    assert r["edges"]["score->ai"] is False


def test_the_run_on_to_stage_4_lights_only_when_the_ai_actually_ran(trace):
    r = trace(_job(steps={"stage1": 6.0, "scorecard": 0.1, "stage4_ai": 484.0,
                          "stage5_subchunk": 0.3}), _det())
    assert r["states"]["ai"] == "done" and r["states"]["subchunk"] == "done"
    assert r["edges"]["score->ai"] is True
    assert r["edges"]["ai->subchunk"] is True
    assert _no_blue_into_hollow(r) == []


# ---- the page-count route ----------------------------------------------------------

def test_a_short_document_lights_the_route_to_mineru_full(trace):
    """Under the page bar, routed straight to MinerU: the branch out of the entry and the
    long run along the bottom into MinerU full are its path, and must both be lit."""
    sm = {"mode": "short_document", "pages": 6, "threshold": 10}
    fb = {"triggered": True, "adopted_tier": "mineru_full",
          "chain": [{"tier": "mineru_full", "status": "re-parsed through MinerU",
                     "adopted": True}]}
    r = trace(_job(tier="mineru_full", fallback=True, pages=6),
              _det(special_mode=sm, fallback=fb))
    assert _no_blue_into_hollow(r) == []
    assert r["states"]["short"] in WALKED and r["states"]["mineru"] in WALKED
    assert r["edges"]["entry->short"] is True
    assert r["edges"]["short->mineru"] is True
    # it never descends through the entry gate: that is the whole point of the route
    assert r["edges"]["score->needs"] is False


def test_the_rise_always_starts_at_mineru_full(trace):
    """It used to anchor on `acceptable?` when the accept test had been recorded -- which a
    page-routed document never reaches, because fb["accepted"] is written at the END of
    run_chain and the short-document branch returns first (14 of 30 chain entrants). That
    node is gone now, so there is one anchor and it cannot be a hollow one."""
    sm = {"mode": "short_document", "pages": 6, "threshold": 10}
    fb = {"triggered": True, "adopted_tier": "mineru_full",
          "chain": [{"tier": "mineru_full", "status": "re-parsed", "adopted": True}]}
    r = trace(_job(tier="mineru_full", fallback=True, pages=6),
              _det(special_mode=sm, fallback=fb))
    assert "accept" not in r["states"]
    assert r["edges"].get("accept->score") is None
    assert r["edges"]["mineru->score"] is True


# ---- the TOC rescue ----------------------------------------------------------------

def test_the_rescue_lights_only_when_the_outline_was_rebuilt(trace):
    applied = _det(preflight={"applied": True, "entries": 40, "verified": 40})
    r = trace(_job(steps={"stage1": 6.0, "toc_preflight": 6.4, "stage2_mineru": 5.0,
                          "stage3": 0.1, "scorecard": 0.0}), applied)
    assert r["edges"]["stage1->tocrescue"] is True
    assert r["states"]["preflight"] in WALKED
    assert _no_blue_into_hollow(r) == []


def test_a_check_that_changed_nothing_is_not_a_rescue(trace):
    """It RAN -- it has a recorded duration -- but it rebuilt nothing, and the node says
    so. A blue edge into that hollow node was this graph's most common contradiction: 42
    of 150 documents in out/corpus."""
    r = trace(_job(steps={"stage1": 6.0, "toc_preflight": 6.4, "stage2_mineru": 5.0,
                          "stage3": 0.1, "scorecard": 0.0}),
              _det(preflight=None))
    assert r["states"]["preflight"] not in WALKED
    assert r["edges"]["stage1->tocrescue"] is False
    assert _no_blue_into_hollow(r) == []


# ---- escalation --------------------------------------------------------------------

def test_an_escalated_document_descends_and_rises_again(trace):
    fb = {"triggered": True, "adopted_tier": "mineru_full", "accepted": True,
          "chain": [{"tier": "mineru_full", "status": "re-parsed through MinerU",
                     "adopted": True}]}
    r = trace(_job(tier="mineru_full", fallback=True, worst=88),
              _det(worst_score=88, fallback=fb))
    assert _no_blue_into_hollow(r) == []
    assert r["edges"]["score->needs"] is True
    assert r["edges"]["needs->mineru"] is True
    assert r["edges"]["mineru->score"] is True          # re-scored


# ---- the states that must never be claimed -----------------------------------------

def test_a_running_tier_is_not_reported_as_adopted(trace):
    """With no scorecard on disk the only evidence is the row's `tier`, and that says
    which tier is IN FLIGHT as readily as which one won."""
    r = trace(_job(gate=None, tier="mineru_full", fallback=True, live=True,
                   running_step="mineru_full", steps={}), None)
    assert r["states"]["mineru"] == "run"
    assert _no_blue_into_hollow(r) == []


def test_a_crash_lights_nothing_past_the_stage_it_died_in(trace):
    # `status`, not `gate`: a crashed document has no gate at all, and the graph reads
    # j.status === 'error'. (The local monitor cannot currently supply this — it infers
    # stages from files on disk and has no error record, so a crash there is
    # indistinguishable from a run still in progress.)
    r = trace(_job(gate=None, status="error", stage="stage2_mineru", worst=None,
                   error="CalledProcessError", steps={"stage1": 6.0}), None)
    assert r["states"]["stage2"] == "crash"
    # the Scorecard is the judgement node now, so it carries the crash
    assert r["states"]["stage3"] == "none" and r["states"]["score"] == "crash"
    assert r["edges"]["stage2->stage3"] is False
    assert _no_blue_into_hollow(r) == []


def test_every_edge_the_graph_draws_is_named(trace):
    """An unnamed edge cannot be asserted about, which is how the three defects above
    survived. Fifteen edges, every one addressable."""
    r = trace(_job(), _det())
    assert len(r["edges"]) == 13, sorted(r["edges"])
    assert all("->" in name for name in r["edges"])
