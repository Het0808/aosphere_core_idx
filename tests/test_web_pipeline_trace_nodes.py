"""The pipeline trace must show every stage the pipeline has.

The trace is drawn twice — a pip rail in each job row, and the full graph in the drill-down —
and BOTH geometries hard-coded the node count as the literal 13:

    const EXTR_X=[8,26,44,62,80,98,116,134,152,170,188,206,226];
    const EXG_PITCH=122, EXGW=EXG_PITCH*13, EXGH=214;
    const EXG_X=[]; for(let i=0;i<13;i++) EXG_X.push(...);

So adding a node to EXG_STEPS placed no pip for it and drew no rail segment into it: the stage
was absent from the picture with nothing on screen to say so. Stages 4 and 5 were added to the
funnel and to the stage model and were still missing here, which is exactly the failure this
pins — the count now comes off EXG_STEPS, and these tests fail if anyone puts a literal back.

Text-level assertions on purpose: there is no JS engine in this test environment, so the rule
is enforced where it can be, rather than trusted to a comment.
"""

import re
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent / "src/aosphere_core_index/service"
WEB = SERVICE / "web.py"
GRAPH = SERVICE / "trace_graph.py"
# The graph moved into trace_graph.py so the local pipeline monitor could draw the same
# picture instead of carrying a second copy of it. These assertions are about the UI as
# shipped, so they read both: the shared graph and the page that hosts it.
SRC = GRAPH.read_text(encoding="utf-8") + WEB.read_text(encoding="utf-8")

RAIL = re.search(r"const EXG_STEPS=\[(.*?)\];", SRC, re.S)
NODES = re.findall(r"\{k:'(\w+)'", RAIL.group(1)) if RAIL else []


def test_the_rail_carries_both_opt_in_ai_stages():
    assert "ai" in NODES and "subchunk" in NODES, NODES


def test_the_ai_stages_come_after_the_scorecard():
    """They run after the gate, and the rail's reading order IS the execution order. The
    gate lives on the Scorecard now: `Verdict` and `acceptable?` each had a box and said
    nothing it does not -- both read the same scorecard -- so the pair read as process
    steps that do not exist."""
    assert "verdict" not in NODES and "accept" not in NODES
    assert NODES.index("ai") > NODES.index("score")
    assert NODES.index("subchunk") == NODES.index("ai") + 1


def test_the_worker_step_names_map_onto_the_ai_nodes():
    """A step no node owns is a stage that vanishes off the screen."""
    assert "stage4_ai:'ai'" in SRC.replace(" ", "")
    assert "stage5_subchunk:'subchunk'" in SRC.replace(" ", "")


def test_every_node_has_inspector_help():
    """Clicking a node must explain what it is; a node with no entry shows an empty panel."""
    help_block = re.search(r"const EXG_HELP=\{(.*?)\n\n", SRC, re.S).group(1)
    documented = set(re.findall(r"^ (\w+):", help_block, re.M))
    # the pure decision points are labelled on the graph itself, not in the help panel
    gates = set(re.findall(r"\{k:'(\w+)'[^}]*gate:1", RAIL.group(1)))
    assert set(NODES) - gates - documented == set(), set(NODES) - gates - documented


def test_no_geometry_hard_codes_how_many_nodes_there_are():
    """The bug this file exists for, in both renderers.

    The row rail derives its positions from EXG_STEPS. The graph is laid out on a column
    grid, so its width must come from the rightmost column IN USE — EXG_COL(11) was the
    same mistake as the old literal 13, just one level up: a node placed one column
    further right would have been drawn off the canvas, invisible with nothing to say so.
    """
    assert "const EXTR_X=EXG_STEPS.map(" in SRC
    assert "EXG_COL(Math.max.apply(null,Object.keys(EXG_LAY).map(k=>EXG_LAY[k].c)))" in SRC
    # no stray literal geometry may come back
    assert not re.search(r"i<13;i\+\+", SRC)
    assert not re.search(r"EXG_PITCH\*13", SRC)
    assert "EXG_COL(11)" not in SRC


def test_the_row_svg_viewbox_follows_the_rail_width():
    """A fixed 236px viewBox would clip the two new pips off the right-hand edge."""
    assert 'viewBox="0 0 \'+EXTR_W+\' 26"' in SRC
    assert "const EXTR_W=EXTR_X[EXTR_X.length-1]+EXTR_PAD;" in SRC
    assert 'viewBox="0 0 236 26"' not in SRC


def test_a_disabled_stage_4_is_read_by_key_presence_not_by_value():
    """The disabled path records exactly 0.0 seconds. Tested by truthiness that zero vanishes
    and the stage reads as 'never asked' — losing the one fact the run recorded."""
    assert "has('stage4_ai')&&aiSec>0" in SRC
    assert "else if(has('stage4_ai')) aiState='skip'" in SRC


def test_never_asked_and_asked_but_refused_are_different_states():
    """One is the absence of a decision, the other is a decision. The file draws that
    distinction everywhere else; it must hold for the stage whose normal state is 'nobody
    asked' — otherwise every document in the corpus looks like it refused the AI."""
    assert "aiState='none', aiF.word='not requested'" in SRC
    assert "disabled for this run" in SRC


# ---- the lane layout ---------------------------------------------------------------
# The graph was one 15-node rail 1830px wide, which read as one undifferentiated queue:
# the four stages a healthy document walks were lost among the nine nodes of machinery
# most documents never touch. It is now lanes — a short spine, the scorecard and fallback
# chain hanging under Validation, and the page-count route on its own band.

def _consts():
    lay = {m[0]: (float(m[1]), m[2]) for m in re.findall(
        r"(\w+):\{c:(-?[\d.]+),y:'(\w+)'\}",
        re.search(r"const EXG_LAY=\{(.*?)\};", SRC, re.S).group(1))}
    lanes = {k: int(v) for k, v in re.findall(
        r"(\w+):(\d+)", re.search(r"const EXG_LANE=\{(.*?)\};", SRC, re.S).group(1))}
    pitch = int(re.search(r"const EXG_PITCH=(\d+)", SRC).group(1))
    return lay, lanes, pitch, (lambda c: round(64 + pitch * c))


def test_every_node_has_a_layout_entry():
    """exGraph reads EXG_LAY[n.k].c for each node. A node with no entry raises a
    TypeError mid-render, and the ENTIRE graph blanks — not just that node. This is the
    cost of splitting layout out of EXG_STEPS, and the reason it is pinned here."""
    lay, _, _, _ = _consts()
    assert [k for k in NODES if k not in lay] == []


def test_the_spine_is_only_the_stages_a_healthy_document_walks():
    """The whole point of the redesign. Anything else on the spine puts conditional
    machinery back on the happy path, which is what made the old rail unreadable."""
    lay, lanes, _, _ = _consts()
    spine = {k for k in NODES if lay[k][1] == "spine"}
    assert spine == {"stage1", "stage2", "stage3", "score", "ai", "subchunk"}


def test_validation_has_no_node_but_its_time_is_not_lost():
    """The 12 checks have no outcome separate from the scorecard they roll into, so the
    node was always the same colour as the one beside it and earned no space. Its STEP is
    folded onto the Scorecard: a step no node owns is a cost that vanishes off the screen,
    which is the trap this file's pre-flight comment already warns about."""
    assert "validate" not in NODES
    assert "step:['validation','scorecard']" in SRC
    # and a crash during validation must still land somewhere
    assert "validation:'score'" in SRC


def test_the_scorecard_is_on_the_spine_not_below_it():
    """EVERY document is scored, so the Scorecard is not conditional machinery. Only the
    descent below it is conditional, which is what makes a healthy document one straight
    line — the old picture put four nodes on the page all reading "chain not entered"."""
    lay, _, _, col = _consts()
    assert lay["score"][1] == "spine"
    assert col(lay["score"][0]) > col(lay["stage3"][0])
    assert lay["needs"][1] == "chain" and col(lay["needs"][0]) > col(lay["score"][0])


def test_the_structure_gate_node_is_gone():
    """It only ever restated the question its neighbour answered: the outcome is recorded
    on the MinerU full node as "not needed: structure is sound". Over the 30 documents
    that entered the chain it skipped the tier on none of them."""
    assert "recov" not in NODES
    assert "s:'recovered?'" not in SRC


def test_the_loop_closes_on_the_scorecard_not_on_stage_1():
    """MinerU full does NOT re-run Stages 1-3 — it replaces the tree and the document is
    scored again (run_fallback returns a new scorecard, _better compares the two). The
    label said "a tier re-runs Stages 1-3", which was true of the printed-TOC tier and
    never true of this one; removing that tier made the label wrong."""
    assert "re-scored" in SRC
    assert "a tier re-runs Stages 1-3" not in SRC
    assert "EXGX('score')+','+(sy+G)" in SRC        # the rise lands on the Scorecard


def test_no_two_nodes_share_the_dead_phrase():
    """Four nodes all reading "chain not entered" is noise, not information."""
    assert SRC.count("word='chain not entered'") == 0


def test_the_remaining_tier_is_named_for_what_it_does():
    """"Tier 3" names a position in a chain nobody can look up; the rest of the UI already
    says "MinerU full" (extraction_monitor.TIER_LABEL)."""
    assert "s:'MinerU full'" in SRC
    assert "s:'Tier 2'" not in SRC and "s:'Tier 3'" not in SRC


def test_the_printed_toc_tier_is_gone_from_the_graph():
    """The pre-flight settles the outline before Stage 2 is paid for, so by the time a
    document escalates this tier either skipped ("the pre-flight already rebuilt this
    outline") or re-ran the same reader that had already failed. Over 148 scored
    documents: 30 entered the chain, it did work on none and was adopted on none."""
    assert "toc" not in NODES
    assert "toc_rescue:'toc'" not in SRC          # no step may map to a missing node
    # scoped to the NODE LIST: comments elsewhere legitimately quote historical chain
    # records, which still carry {tier:'toc_rescue', ...} and always will
    assert "toc_rescue" not in RAIL.group(1)
    # The NAME is not gone -- it moved to the pre-flight node, which performs the same
    # repair earlier. What must not come back is the TIER: a second full extraction,
    # MinerU included, run after the document had already been extracted once.


def test_historical_runs_that_adopted_the_old_tier_still_label():
    """Scorecards already on S3 carry adopted_tier "toc_rescue". Dropping the node must
    not drop the label, or an old document's Pass column goes blank."""
    mon = (Path(__file__).resolve().parent.parent
           / "src/aosphere_core_index/service/extraction_monitor.py").read_text()
    assert '"toc_rescue":' in mon


def test_the_ai_stages_stay_on_the_spine_after_the_scorecard():
    lay, _, _, col = _consts()
    assert lay["ai"][1] == "spine" and lay["subchunk"][1] == "spine"
    assert col(lay["ai"][0]) > col(lay["score"][0])


def test_the_scorecard_carries_the_gate_it_absorbed():
    """Nothing was lost by dropping the two boxes: the gate becomes the Scorecard's COLOUR
    and the numbers its caption, and the accept test's facts ride along for the inspector."""
    assert "st.score = vState;" in SRC
    assert "'worst ' + worst" in SRC
    assert "accept_word" in SRC and "accept_why" in SRC
    assert "n.k==='score'&&f.worst!=null" in SRC


def test_no_two_nodes_in_a_lane_overlap():
    """Node hit-boxes are a full pitch wide and captions are centred; closer than ~60px
    and two nodes' text overprints."""
    lay, lanes, _, col = _consts()
    pos = {k: (col(lay[k][0]), lay[k][1]) for k in NODES}
    assert [(a, b) for a in pos for b in pos
            if a < b and pos[a][1] == pos[b][1] and abs(pos[a][0] - pos[b][0]) < 60] == []


def test_every_node_fits_inside_the_canvas():
    lay, lanes, pitch, col = _consts()
    w = col(max(v[0] for v in lay.values())) + pitch
    h = int(re.search(r"EXGH=(\d+)", SRC).group(1))
    for k in NODES:
        x, y = col(lay[k][0]), lanes[lay[k][1]]
        assert 0 <= x <= w, (k, x, w)
        assert 10 <= y <= h - 30, (k, y, h)


def test_the_css_min_width_matches_the_canvas():
    """min-width below the viewBox width scales the graph under 1:1 and shrinks its 11px
    labels; above it, the graph scrolls when it did not need to."""
    lay, lanes, pitch, col = _consts()
    assert f"min-width:{col(max(v[0] for v in lay.values())) + pitch}px" in SRC


def test_the_re_score_loop_is_drawn_and_labelled():
    """A tier does not continue the pipeline. MinerU full replaces the tree and the
    document is SCORED AGAIN, kept only if it beats the first pass — so the loop closes on
    the Scorecard. Undrawn, the chain reads as a continuation of the spine."""
    assert "kept only if it beats the first pass" in SRC
    assert "ed.mineruRan, true," in SRC and "from+'->score'" in SRC


def test_no_edge_reads_a_band_that_no_longer_exists():
    """EXG_HOP lost two bands when the arcs they carried were removed. An edge reading a
    deleted band gets `undefined.y`, which throws mid-render and blanks the WHOLE graph —
    a silent failure, because the JS still parses."""
    bands = set(re.findall(r"(\w+):\{y:", re.search(r"const EXG_HOP=\{(.*?)\};",
                                                    SRC, re.S).group(1)))
    used = set(re.findall(r"EXG_HOP\.(\w+)", SRC))
    assert used <= bands, used - bands


def test_the_page_count_route_does_not_leave_stage_1():
    """It is asked BEFORE Stage 1 off the source PDF, and a short document skips Stage 1
    entirely — so drawing the branch out of Stage 1 would claim Stage 1 ran."""
    assert "const ex=EXG_COL(-0.42)" in SRC
    assert "ex+','+sy+' '+ex+','+ry" in SRC


def test_the_preflight_node_is_named_for_the_rescue_it_performs():
    """"Pre-flight" said WHEN it happens and never what it does, so the node that rebuilds
    a document's outline from its printed contents page read as a scheduling detail. It is
    the TOC rescue -- the fallback tier of that name is gone precisely because this runs
    the same repair earlier, before Stage 2 is paid for."""
    assert "s:'TOC rescue'" in SRC
    assert "s:'Pre-flight'" not in SRC
    # and it must still own both step names, or its time vanishes off the screen
    assert "step:['toc_preflight','stage1_toc_rebuild']" in SRC


def test_a_rescued_document_is_labelled_in_the_pass_column():
    """103 of 150 documents in out/corpus were rescued and every one read "first pass"."""
    assert "j.toc_rescued" in SRC
    assert "TOC rescued" in SRC


# ---- contrast: taken is near-black, not taken is plainly grey ----------------------

def test_the_graph_reads_as_progress_filling_in():
    """It starts blank and each completed step fills in blue, node and arrow together, so
    the blue line IS how far the document got. 'done' used to sit at neutral-700 with
    'skip' a hollow box outlined in neutral-300 — two shades of one ink, and the path
    could not be found without reading every label."""
    assert ".s-done  .exgbox,.s-warn .exgbox{fill:var(--prog);stroke:var(--prog)}" in SRC
    assert "--prog:#2a78d6" in SRC


def test_running_is_distinguishable_from_completed():
    """Blue now means TRAVELLED, so a step in flight cannot also be plain blue or it reads
    as already done. It is the same blue left hollow and pulsing — the frontier of the
    line rather than part of it."""
    assert ".s-run   .exgbox{fill:var(--g-surface);stroke:var(--prog)" in SRC
    assert "@keyframes exgpulse" in SRC
    assert "@keyframes exgmarch" in SRC


def test_a_stopped_or_discarded_step_stays_saturated():
    """A crash and a discarded tier are both ON the line: the document went there. They
    must not fade to the blank of a path never taken."""
    assert ".s-crash .exgbox{fill:#c1342d" in SRC
    assert ".s-disc  .exgbox{fill:#b45309" in SRC


def test_an_untaken_edge_and_its_arrowhead_are_both_faint():
    """The edge was lightened and the ARROWHEAD was not, so an untaken branch still ended
    in a visible point — the marker fills are hardcoded hex, out of reach of the tokens."""
    assert ".exgedge.on{stroke:var(--prog)" in SRC
    # A marker's fill cannot inherit or read a CSS token, so the blue is repeated as a
    # literal in exactly one place. This is what keeps that copy honest.
    assert 'fill="#2a78d6"' in SRC        # walked — same hex as --prog
    assert 'fill="#383B3B"' in SRC        # not walked — same hex as --neutral-700
    assert 'fill="#7B888A"' not in SRC and 'fill="#D4D9D9"' not in SRC


def test_a_gate_guarding_an_untaken_branch_is_not_drawn_as_taken():
    """"Asked" and "routed the document" are different facts, and only the second is the
    path. under-10pp? and needs_help? were both 'done' — as dark as Stage 1 — for merely
    having been asked, which drew ink on a road not travelled."""
    assert "sState='skip', sF.word=nPages+'pp — route not taken'" in SRC
    assert "nState='skip', nF.word='not escalated'" in SRC
    assert "sState='done'" not in SRC


def test_the_stages_name_the_engine_that_does_the_work():
    """"extract" / "combine" said nothing about HOW; the engine is the useful fact."""
    assert "PyMuPDF \\u00b7 text extraction" in SRC
    assert "MinerU \\u00b7 table parsing" in SRC
    assert "t:'combining'" in SRC


def test_the_sub_label_is_actually_drawn():
    """`t` lived only in the hover <title>, so the graph read "Stage 1 / Stage 2 / Stage 3"
    and never said which engine does the work -- renaming it changed nothing anyone could
    see. It now has its own line under the node name, clipped to the column."""
    assert 'class="exgnt"' in SRC and "exGClip(n.t,pitch)" in SRC
    assert ".exgnt{font-size:8.8px" in SRC
    # the node box had to grow to hold the extra line
    assert "(cy-36)" in SRC and 'height="62"' in SRC


def test_the_arrowhead_literals_match_the_tokens_they_copy():
    """The SVG marker fills are the only place the palette is duplicated, because a
    marker's fill cannot inherit or read a CSS variable. Drift there is invisible: the
    edge goes blue and its arrowhead stays the old grey."""
    prog = re.search(r"--prog:(#[0-9a-fA-F]{6})", SRC).group(1)
    ink = re.search(r"--neutral-700:(#[0-9a-fA-F]{6})", SRC).group(1)
    assert f'fill="{prog}"' in SRC
    assert f'fill="{ink.upper()}"' in SRC or f'fill="{ink.lower()}"' in SRC


def test_the_ui_route_is_not_cacheable():
    """It carried no Cache-Control, no ETag and no Last-Modified, so browsers cached it
    heuristically and kept serving markup from before a deploy — the JSON stayed live, so
    the page looked like it worked while the CSS and JS rendering it were a version old.
    A UI change simply would not appear, which is indistinguishable from not shipping it."""
    app = (Path(__file__).resolve().parent.parent
           / "src/aosphere_core_index/service/app.py").read_text()
    home = app[app.index('@app.get("/", response_class=HTMLResponse)'):]
    home = home[:home.index("@app.get", 10)]
    assert "no-store" in home


def test_blue_means_walked_and_a_skipped_gate_is_not_walked():
    """The predicate was `st[k]!=='none'`, which counted 'skip' as reached. That was fine
    while 'skip' meant "ran and chose not to act"; it became wrong the moment the gates
    guarding an untaken branch became 'skip' too. The result was blue arrows drawn INTO
    hollow nodes — under-10pp?, needs_help?, MinerU full and acceptable? all had one —
    so the picture asserted a path it simultaneously showed as not taken."""
    assert "const WALKED=['done','run','warn','disc','crash','clone']" in SRC
    assert "const reached=k=>WALKED.indexOf(st[k])>=0;" in SRC
    # The 'skip that recorded time' exception is GONE. It lit the edge into a TOC rescue
    # node that had run and rebuilt nothing — a blue arrow into a hollow node on 42 of 150
    # documents. tests/test_trace_graph_paths.py executes the graph and pins the invariant.
    assert "(st[k]==='skip'&&(fa[k]||{}).seconds!=null)" not in SRC


def test_the_structure_stays_black_and_only_the_path_is_blue():
    """The graph is the pipeline and must stay legible in ink; blue is an overlay saying
    where one document went. Faded to near-white, the untaken parts read as damage."""
    assert ".s-skip  .exgbox{fill:var(--g-surface);stroke:var(--g-ink)}" in SRC
    assert ".s-none .exgnl,.s-skip .exgnl,.s-unk .exgnl,.s-clone .exgnl{fill:var(--g-ink)}" in SRC
    assert ".exgedge{stroke:var(--g-line-strong)" in SRC


def test_the_toc_rescue_is_a_branch_off_stage_1_not_a_station_before_stage_2():
    """Routing the spine THROUGH it made the blue Stage1->Stage2 line emerge from the
    rescue node even on a document where the rescue never ran. Its outcome is to send
    Stage 1 round again, so it is an out-and-back branch above Stage 1."""
    assert "g+=seg('stage1','stage2'" in SRC or "['stage1','stage2'][i]" in SRC
    assert "EXGX('stage2')+','+(sy-G)" not in SRC     # no spine detour through it
