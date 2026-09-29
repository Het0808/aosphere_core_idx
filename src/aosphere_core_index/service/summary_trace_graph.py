"""The pipeline trace for the summary AI route — its OWN path, not a variant of the spine.

`trace_graph.py` draws the route almost every document takes: Stage 1, the outline
pre-flight, Stage 2's table parsing, Stage 3's splice, the scorecard, the fallback chain
hanging off it, and the two paid stages after it. Eleven nodes, and every one of them is a
question asked of a document that walks that path.

The MRAM summaries do not walk it. A product rule (product_rules.AI_PIPELINE) sends them
somewhere else entirely, BEFORE Stage 1: one vision-model call reads every page and the
scorecard judges that. There is no Stage 1 to pre-flight, no table pass, no splice and no
chain -- not skipped, not deferred, NOT ON THIS ROUTE.

So this is a separate file rather than a branch inside that one. The first attempt did it
the other way -- a twelfth node on the shared graph, with the other eleven painted
"skipped" -- and the picture it produced was actively wrong: eleven dark nodes and one lit
one reads as a document that failed almost everything, when the truth is that eleven of
them were never part of its journey. Worse, it made the shared graph own a route it has no
stake in, so every future change to either had to be reasoned about against the other.

Three nodes, because the route has three steps:

    Source PDF  ->  AI extraction  ->  Scorecard

Same host contract as trace_graph: `CSS` and `JS` splice verbatim into the host page, and
the host calls one entry point, `sumGraph(job)`. The colour tokens are the SAME SIX the
hosts already map for the other graph (--g-*), each with a literal fallback, so this file
adds nothing for a host to wire up and still renders standalone if one maps none of them.

Reads off the job row, all optional -- it degrades to a correct if vaguer picture rather
than throwing, because a graph must never take the table down with it:

    route          "summary_ai"     which is why this graph was chosen at all
    live           bool             the call is in flight right now
    gate/worst     the verdict, once scored
    seconds        wall clock for the call
    cost_usd       what it cost, which no other route has
    ai_model       the model id, shortened for display
    tokens_in/out  what was sent and returned
    detail         {dimensions, timing, special_mode} — the scorecard, when the host has it
"""

CSS = r"""
 .sumgscroll{grid-column:1/-1;overflow-x:auto;padding-bottom:3px}
 /* min-width is the viewBox width: three nodes need far less room than the spine's eleven,
    so this graph is never the reason a row scrolls. */
 .sumg{display:block;width:100%;max-width:760px;min-width:620px;height:auto}
 .sumgn{cursor:default}
 .sumgnl{font:600 11.5px ui-sans-serif,system-ui,sans-serif;text-anchor:middle;
   fill:var(--g-ink,#222)}
 .sumgnt{font:10px ui-sans-serif,system-ui,sans-serif;text-anchor:middle;
   fill:var(--g-muted,#777)}
 .sumgnc{font:10px ui-sans-serif,system-ui,sans-serif;text-anchor:middle;
   fill:var(--g-faint,#999)}
 .sumgc{fill:var(--g-surface,#fff);stroke:var(--g-line,#ccc);stroke-width:1.5}
 .sumge{fill:none;stroke:var(--g-line-strong,#bbb);stroke-width:1.5}
 /* A step this document actually took. Greyscale first, exactly as the other graph argues:
    "it ran" is the least surprising fact on the screen, so it does not get a colour. */
 .s-done .sumgc{stroke:var(--prog,#111);fill:var(--g-surface,#fff)}
 .s-done .sumgnl{fill:var(--prog-ink,#111)}
 .s-done .sumgm{fill:var(--prog,#111)}
 .s-run .sumgc{stroke:var(--running,#2d7ff9);stroke-width:2.5}
 .s-run .sumgnl{fill:var(--running,#2d7ff9)}
 .s-run .sumgm{fill:var(--running,#2d7ff9)}
 .s-run .sumgc{animation:sumgpulse 1.4s ease-in-out infinite}
 @keyframes sumgpulse{0%,100%{opacity:1}50%{opacity:.45}}
 .s-warn .sumgc{stroke:var(--warning,#c58a00);stroke-width:2}
 .s-warn .sumgnl{fill:var(--warning,#c58a00)}
 .s-warn .sumgm{fill:var(--warning,#c58a00)}
 .s-fail .sumgc{stroke:var(--critical,#c0392b);stroke-width:2}
 .s-fail .sumgnl{fill:var(--critical,#c0392b)}
 .s-fail .sumgm{fill:var(--critical,#c0392b)}
 .sumge.on{stroke:var(--prog,#111)}
 .sumgcap{font:10px ui-sans-serif,system-ui,sans-serif;fill:var(--g-muted,#777)}
"""

JS = r"""
// ---- the summary AI route: three nodes, and nothing borrowed from the spine ------------
// Its own node list, its own layout, its own renderer. The shared graph is never consulted
// for a document on this route and never has to know the route exists.
const SUMG_STEPS = [
  {k: 'source', s: 'Source PDF',    t: 'fetched / queued'},
  {k: 'ai',     s: 'AI extraction', t: 'one call, every page'},
  {k: 'score',  s: 'Scorecard',     t: ''},
];
const SUMG_W = 700, SUMG_H = 132, SUMG_R = 13;
// Evenly spaced, first and last inset by a node's width so their captions never clip.
const SUMGX = i => Math.round(104 + i * ((SUMG_W - 208) / (SUMG_STEPS.length - 1)));
const SUMGY = 52;
const SUMG_PITCH = Math.round((SUMG_W - 208) / (SUMG_STEPS.length - 1));

function sumgEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
}
function sumgEscA(s) {
  return sumgEsc(s).replace(/"/g, "&quot;").replace(/\u0027/g, "&#39;");
}
function sumgClip(s, w) {
  s = String(s == null ? '' : s);
  const max = Math.max(6, Math.floor(w / 5.4));
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}
function sumgDur(sec) {
  if (sec == null) return '';
  if (sec < 60) return (sec < 10 ? sec.toFixed(1) : Math.round(sec)) + 's';
  return Math.floor(sec / 60) + 'm ' + Math.round(sec % 60) + 's';
}
function sumgModel(m) {
  return String(m || '').replace(/^eu\./, '').replace(/^anthropic\.claude-/, '')
                        .replace(/-v1:0$/, '');
}

// What each node IS, for the hover title. The screen should teach the route as well as
// report one document's trip through it -- the same argument the other graph makes.
const SUMG_HELP = {
  source: 'The PDF as filed, copied into the job directory. Nothing is parsed here: the '
        + 'product rule is decided from the product and folder name alone, before anything '
        + 'reads the document.',
  ai: 'One vision-model call. Every page is rendered at 150 dpi and sent together with the '
    + 'transcription prompt, and the model returns the whole document as Markdown. Sent '
    + 'together rather than page by page because a section continues across a page break '
    + 'and its heading bar is not reprinted -- a page-at-a-time pass cannot know which '
    + 'section it is inside. This is the only route in the pipeline that costs money.',
  score: 'Scored on what a TRANSCRIPTION can get wrong, not on what a stage-1 tree can: '
       + 'every word present (completeness), every heading bar present (structure, checked '
       + 'against the PDF geometry independently of the model), nothing present that was '
       + 'never printed (fidelity), and the typography the page prints kept (punctuation). '
       + 'The gate is the worst of the four.',
};

// job row -> {state, facts} per node. Deliberately tolerant: every field is optional and a
// missing one narrows the picture rather than breaking it.
function sumNodeStates(j) {
  const det = j.detail || null;
  const tm = (det && det.timing) || {};
  const dims = (det && det.dimensions) || {};
  const live = !!j.live;
  const scored = j.gate != null;
  const st = {}, fa = {};

  // The PDF is on disk the moment the job exists -- that is what made the row appear.
  st.source = 'done';
  fa.source = {cap: (j.pages != null ? j.pages : tm.pages) != null
                    ? ((j.pages != null ? j.pages : tm.pages) + 'pp') : ''};

  // Running WINS over the verdict: the gate is decided after the call, so a document with
  // a call in flight is not "at the scorecard", it is upstream of it.
  const ranSec = j.seconds != null ? j.seconds : tm.seconds;
  st.ai = live ? 'run' : (scored || ranSec != null) ? 'done' : 'none';
  const bits = [];
  if (live) bits.push('reading every page now');
  else {
    if (ranSec != null) bits.push(sumgDur(ranSec));
    if (j.cost_usd != null) bits.push('$' + Number(j.cost_usd).toFixed(4));
  }
  fa.ai = {cap: bits.join(' · '),
           model: sumgModel(j.ai_model || tm.model),
           tokens: (j.tokens_in != null && j.tokens_out != null)
                   ? (j.tokens_in + ' in / ' + j.tokens_out + ' out') : ''};

  st.score = live ? 'none'
           : j.gate === 'pass' ? 'done'
           : j.gate === 'review' ? 'warn'
           : j.gate === 'fail' ? 'fail' : 'none';
  const names = Object.keys(dims);
  fa.score = {cap: scored ? ('worst ' + j.worst_score) : (live ? 'not scored yet' : ''),
              dims: names.map(k => k + ' ' + ((dims[k] || {}).score)).join(' · '),
              weakest: j.weakest || (det && det.weakest_dimension) || ''};
  return {st: st, fa: fa, live: live, scored: scored};
}

function sumgNode(n, i, state, facts) {
  const cx = SUMGX(i), f = facts || {};
  const title = n.s + (n.t ? ' — ' + n.t : '') + (f.cap ? ' (' + f.cap + ')' : '')
              + '\n\n' + (SUMG_HELP[n.k] || '');
  return '<g class="sumgn s-' + state + '" data-sumnode="' + sumgEscA(n.k) + '" tabindex="0">'
    + '<title>' + sumgEsc(title) + '</title>'
    + '<text class="sumgnl" x="' + cx + '" y="' + (SUMGY - 26) + '">' + sumgEsc(n.s) + '</text>'
    + (n.t ? '<text class="sumgnt" x="' + cx + '" y="' + (SUMGY - 14) + '">'
           + sumgEsc(sumgClip(n.t, SUMG_PITCH)) + '</text>' : '')
    + '<circle class="sumgc" cx="' + cx + '" cy="' + SUMGY + '" r="' + SUMG_R + '"/>'
    + '<circle class="sumgm" cx="' + cx + '" cy="' + SUMGY + '" r="4.5"/>'
    + (f.cap ? '<text class="sumgnc" x="' + cx + '" y="' + (SUMGY + 26) + '">'
             + sumgEsc(sumgClip(f.cap, SUMG_PITCH + 40)) + '</text>' : '')
    + '</g>';
}

// The entry point the host calls. Returns an HTML string; never throws on a thin job row.
function sumGraph(j) {
  const r = sumNodeStates(j);
  let g = '';
  // Edges first, so the nodes sit on top of them.
  for (let i = 0; i < SUMG_STEPS.length - 1; i++) {
    const a = SUMG_STEPS[i].k, b = SUMG_STEPS[i + 1].k;
    const walked = (r.st[a] === 'done' || r.st[a] === 'run')
                && (r.st[b] === 'done' || r.st[b] === 'run' || r.st[b] === 'warn'
                    || r.st[b] === 'fail');
    g += '<line class="sumge' + (walked ? ' on' : '') + '" x1="' + (SUMGX(i) + SUMG_R + 3)
       + '" y1="' + SUMGY + '" x2="' + (SUMGX(i + 1) - SUMG_R - 3) + '" y2="' + SUMGY + '"/>';
  }
  SUMG_STEPS.forEach((n, i) => { g += sumgNode(n, i, r.st[n.k], r.fa[n.k]); });

  // The caption is not decoration: this graph is three nodes where every neighbouring row
  // has eleven, and without a line saying WHY it reads as a broken or truncated trace.
  g += '<text class="sumgcap" x="8" y="' + (SUMG_H - 26) + '">'
     + sumgEsc('product rule — the MRAM summaries carry their structure in coloured '
             + 'heading bars, so the geometry path is not run at all')
     + '</text>';
  const foot = [r.fa.ai.model ? 'model ' + r.fa.ai.model : '',
                r.fa.ai.tokens, r.fa.score.dims].filter(Boolean).join('   ·   ');
  if (foot) {
    g += '<text class="sumgcap" x="8" y="' + (SUMG_H - 10) + '">' + sumgEsc(foot) + '</text>';
  }
  return '<div class="sumgscroll"><svg class="sumg" viewBox="0 0 ' + SUMG_W + ' ' + SUMG_H
       + '" role="img" aria-label="'
       + sumgEscA('summary AI route for ' + (j.jurisdiction || 'this document'))
       + '">' + g + '</svg></div>';
}
"""
