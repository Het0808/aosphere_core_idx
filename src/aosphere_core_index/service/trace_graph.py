"""The per-document pipeline trace — one copy, two screens.

The graph is drawn by the Core Index UI (service/web.py) and by the local pipeline monitor
(scripts/pipeline_monitor.py). It used to exist only in the first, and copying it into the
second would have guaranteed the drift this file exists to prevent: in one sitting the node
count, the CSS min-width and the SVG arrowhead colours each fell out of step with the code
they were supposed to match, and every one of those failures was invisible -- the JS still
parsed and every test still passed.

`CSS` and `JS` are spliced verbatim into each host page. The CSS deliberately names its own
tokens (--g-*) rather than either host's palette, because the two are unrelated: the Core
Index is a light neutral scale, the monitor has a warm scale AND a dark mode. Each host maps
the six tokens once; nothing else here needs to know which page it is on.

    --g-ink          node outlines and names of steps NOT walked
    --g-muted        captions
    --g-surface      the page behind a hollow node
    --g-line         edges and outlines of steps never reached
    --g-line-strong  edges of steps not walked
    --g-faint        the faintest text
    --prog           the path this document actually took
    --prog-ink       a walked step's name

The graph itself takes two objects: a job row and (optionally) its scorecard detail. See
exNodeStates for exactly which fields it reads -- it is written to degrade, so a host that
cannot supply the detail still gets a correct if less specific picture.
"""

CSS = r"""
 .exgscroll{grid-column:1/-1;overflow-x:auto;padding-bottom:3px}
 /* min-width == the viewBox width, so the graph is never scaled BELOW 1:1 and its 11px
    node labels never shrink out of legibility; .exgscroll takes over below that. It was
    1586px, tuned to the old 1830-wide single rail — left alone it would have stretched
    the narrower lane layout 15% wider than natural and scrolled when it need not. */
 .exg{display:block;width:100%;min-width:1040px;height:auto}
 /* Node state, GREYSCALE FIRST. Almost every document runs almost every stage, so painting
    "it ran" in a colour spends the loudest thing on the screen on the least surprising fact --
    and this table sits inside a UI that is otherwise neutrals and a near-black accent. So
    traversed is dark, un-traversed is faint, and the three colours left are only ever the
    exceptions worth spotting across 891 rows: it is running, a tier was thrown away, it failed.
    Escalation needs no colour of its own -- the filled second lane already shows it. */
 /* The graph reads as PROGRESS: it starts blank and each step the document completed
    fills in blue, node and arrow together, so the blue line IS how far this document got.
    Two values only -- blue for travelled, blank for not -- because the previous palette
    graded 'done' and 'skip' as two shades of the same ink and the path could not be found
    without reading every label.

    Blue therefore means TRAVELLED, not "running". Running is the same blue left hollow and
    pulsing, so a step in flight reads as the frontier of the line rather than part of it. */
 .s-done  .exgbox,.s-warn .exgbox{fill:var(--prog);stroke:var(--prog)}
 .s-run   .exgbox{fill:var(--g-surface);stroke:var(--prog);stroke-width:2;
                  animation:exgpulse 1.4s ease-in-out infinite}
 @keyframes exgpulse{0%,100%{opacity:1}50%{opacity:.35}}
 /* Travelled, but the result was thrown away / the run stopped here. Both are ON the line,
    so they stay saturated -- they are not the blank of a path never taken. */
 .s-disc  .exgbox{fill:#b45309;stroke:#b45309}
 .s-crash .exgbox{fill:#c1342d;stroke:#c1342d}
 /* NOT WALKED: hollow and BLACK. The graph is the pipeline and stays fully legible in
    ink; blue is an overlay that says where this one document went. Faded to near-white
    these read as damage rather than as roads not taken. */
 .s-skip  .exgbox{fill:var(--g-surface);stroke:var(--g-ink)}
 .s-none  .exgbox{fill:var(--g-surface);stroke:var(--g-ink)}
 .s-clone .exgbox{fill:var(--g-surface);stroke:var(--g-ink);stroke-dasharray:2.5 2}
 .s-unk   .exgbox{fill:var(--g-surface);stroke:var(--g-ink);stroke-dasharray:2.5 2}
 .exgbox{stroke-width:1.2}
 /* The full graph is a RAIL, not a row of filled blocks: a dot per node with its name above and
    its cost below. Three lines of reversed-out text per node, twelve times over, was the bulk of
    what made this shout. */
 .exgnl{font-size:11px;font-weight:600;fill:var(--g-ink);text-anchor:middle}
 .exgnt{font-size:8.8px;fill:var(--g-muted);text-anchor:middle;letter-spacing:.01em}
 .exgnc{font-size:9.5px;fill:var(--g-muted);text-anchor:middle;font-variant-numeric:tabular-nums}
 /* Every name is readable ink. Only a WALKED step's name turns blue. */
 .s-done .exgnl,.s-warn .exgnl,.s-run .exgnl{fill:var(--prog-ink)}
 .s-done .exgnt,.s-warn .exgnt,.s-run .exgnt{fill:var(--prog)}
 .s-none .exgnl,.s-skip .exgnl,.s-unk .exgnl,.s-clone .exgnl{fill:var(--g-ink)}
 .s-none .exgnc,.s-skip .exgnc,.s-unk .exgnc,.s-clone .exgnc{fill:var(--g-muted)}
 .exgring{fill:none;stroke:var(--g-ink);stroke-width:1.4}
 .exghit{fill:transparent}
 .exgn{cursor:pointer}
 .exgn:hover .exgbox{stroke:var(--g-ink)}
 .exgn:hover .exgnl{fill:var(--g-ink)}
 .exgn.sel .exgnl{fill:var(--g-ink);font-weight:700}
 /* An edge the document TOOK is drawn dark; one it could have taken and did not stays faint,
    so the path through the graph is legible without reading a single label. */
 /* The structure, in ink. Dashed so a route not taken is distinguishable at a glance
    even before the colour registers, and so it never competes with the blue. */
 .exgedge{stroke:var(--g-line-strong);stroke-width:1.1;fill:none;stroke-dasharray:4 3}
 .exgedge.on{stroke:var(--prog);stroke-width:1.8;stroke-dasharray:none}
 /* the segment being walked right now: the same blue, marching */
 .exgedge.live{stroke:var(--prog);stroke-width:1.8;stroke-dasharray:5 3;
               animation:exgmarch 1s linear infinite}
 @keyframes exgmarch{to{stroke-dashoffset:-8}}
 .exgel{font-size:9.5px;fill:var(--g-muted)}
 .exgel.on{fill:var(--prog-ink);font-weight:600}
 .exglane{font-size:9.5px;fill:var(--g-faint);letter-spacing:.06em;text-transform:uppercase}
"""

JS = r"""
// ONE list, in the order run_corpus.run_one and fallback_chain.run_chain actually execute.
// It used to be two — a "spine" and a "chain" drawn on a second lane — and that shape made two
// false claims. It put Tier 2's printed-TOC rescue on a rail of its own, as though it were the
// place this pipeline repairs an outline, when the PRE-FLIGHT does that before MinerU is ever
// paid for and Tier 2 stands down whenever it did. And it had no node for the page-count test,
// so a short document appeared to have been escalated by needs_help when in fact that test is
// asked BEFORE the gate and consults no score at all.
//
// Drawn as one rail, every conditional step is still visible as a hollow node with a bypass arc
// over it, and the reading order is the execution order. Nothing is inferred from position that
// the pipeline does not record.
const EXG_STEPS=[
 // FIRST, because run_corpus asks it first: the page count is read off the source PDF, so
 // nothing the extraction produces can change the answer. It used to be asked inside the
 // fallback chain, after Stage 2 had been paid for — 341.6s across this corpus's 14 short
 // documents, every second of it building a tree the re-parse then replaced.
 {k:'short',    s:'under 10pp?', t:'page-count route',    gate:1},
 {k:'stage1',   s:'Stage 1',     t:'PyMuPDF \u00b7 text extraction', step:'stage1'},
 // stage1_toc_rebuild is the SAME repair from a different entrance: Stage 1 found no structure
 // at all, so the printed contents page supplied it. Mapped here rather than dropped, because a
 // step no node owns is a cost that vanishes off the screen.
 // THIS is the TOC rescue. It reads the contents page the document prints, verifies it
 // against the pages it names and rebuilds the outline from it -- and it does so BEFORE
 // Stage 2 is paid for, which is why the fallback tier of the same name is gone. Named
 // for the capability rather than for its position in the run: "Pre-flight" said when it
 // happens and never what it does, so a rescued document read as an untouched one.
 {k:'preflight',s:'TOC rescue',  t:'outline check, rebuild',
  step:['toc_preflight','stage1_toc_rebuild']},
 {k:'stage2',   s:'Stage 2',     t:'MinerU \u00b7 table parsing', step:'stage2_mineru'},
 {k:'stage3',   s:'Stage 3',     t:'combining',           step:'stage3'},
 // Validation had a node of its own and it earned nothing: the 12 checks have no outcome
 // separate from the scorecard they roll into, so the node was always the same colour as the
 // one beside it. Its STEP is folded in here rather than dropped -- a step no node owns is a
 // cost that vanishes off the screen.
 {k:'score',    s:'Scorecard',   t:'12 checks \u2192 7 dimensions',
  step:['validation','scorecard']},
 {k:'needs',    s:'needs_help?', t:'chain entry',         gate:1},
 // Named for WHAT IT DOES, not its position in a chain nobody can look up -- it read
 // "Tier 3". There used to be a printed-TOC tier ahead of it, and it is gone: the pre-flight
 // settles the outline before Stage 2 is paid for, so by the time a document escalates the
 // only repair left is to take MinerU's hierarchy instead. See fallback_chain.run_chain.
 {k:'mineru',   s:'MinerU full', t:'whole document, not just tables',
  step:['mineru_full','mineru_fallback','hybrid'], tier:'mineru_full'},
 // AFTER the gate, because that is when they run: extraction ends at the verdict and these two
 // are a separate, opt-in, PAID pass over the tree it produced. Stage 5 is not a second decision
 // -- it always follows stage 4 -- so it carries no gate diamond.
 {k:'ai',       s:'Stage 4',     t:'AI post-processing',  step:'stage4_ai'},
 {k:'subchunk', s:'Stage 5',     t:'sub-chunks',          step:'stage5_subchunk'}];
const EXG_ORDER=EXG_STEPS.map(n=>n.k);
const EXG_NODE={}; EXG_STEPS.forEach(n=>{EXG_NODE[n.k]=n;});
// Where the chain begins, so the bypasses can be drawn without hard-coding indices.
const EXG_IX={}; EXG_STEPS.forEach((n,i)=>{EXG_IX[n.k]=i;});

// A stage name as the worker reports it -> the node it belongs to. The fallback tiers re-run
// earlier stages, so they map to their own node rather than back onto the spine.
const EXSTAGE_NODE={starting:'stage1',fetch:'stage1',stage1:'stage1',toc_preflight:'preflight',
 stage1_toc_rebuild:'preflight',
 stage2_mineru:'stage2',stage3:'stage3',validation:'score',scorecard:'score',
 mineru_full:'mineru',mineru_fallback:'mineru',hybrid:'mineru',
 publish:'score',
 stage4_ai:'ai', stage5_subchunk:'subchunk'};

const EXG_STATEWORD={done:'ran',run:'running now',disc:'ran, discarded',warn:'branched',
 crash:'stopped here',skip:'not run',none:'not reached',clone:'cloned',unk:'open for detail'};

// What each node IS. Shown in the inspector, so the screen teaches the pipeline as well as
// reporting one document's trip through it.
const EXG_HELP={
 stage1:'Deterministic conversion by pdf2mdtree: headings, prose, footnotes, formulas, with '
  +'tables left as visible placeholders. The structure comes from the PDF bookmark outline, or '
  +'from a font-size heuristic when the PDF has no outline at all.',
 preflight:'Judges the outline BEFORE MinerU is paid for, because that judgement needs nothing '
  +'the extraction produces. When the outline looks suspect, disagrees with the contents page the '
  +'document prints, or holds two entries or fewer — ZERO included — the bookmarks are repaired '
  +'and Stage 1 re-runs in place. Only a healthy outline is left alone: a document with no '
  +'outline scores toc 55, or 65 once its printed contents page verifies, against a chain-entry '
  +'bar of 70, so leaving it alone only moved the same repair into the chain and paid for a '
  +'second MinerU pass to do it. Where a product declares its outline authoritative the '
  +'disagreement test is silenced, but suspect and 0-2 entries still fire.',
 stage2:'Every page holding a deferred table is cropped into one mini-PDF and run through MinerU '
  +'once. Tables only, never the whole document. Normally the dominant cost in the run.',
 stage3:'Splices the Stage 2 tables into the Stage 1 tree at their placeholders. A table MinerU '
  +'could not build stays visibly flagged rather than being quietly dropped.',
 score:'The 12 canonical checks in lib_validate.CHECKS, rolled into 7 dimensions. The gate is the WORST GATING dimension, not the '
  +'average: for legal text one dimension on the floor is a document you cannot ship. WHICH '
  +'dimensions gate is decided per document and recorded in the scorecard — completeness, '
  +'placement and fidelity always; sectioning only where the expected section count is known; '
  +'and for a document under 10 pages the set narrows to completeness alone, because the '
  +'structural measures report the absence of something that was never there.',
 needs:'Chain entry. Any one of: completeness below 60, toc or sectioning below 70, or a hard '
  +'shape gate — all the content in one chunk, no content files at all, a 1-2 entry outline. The '
  +'shape gates force entry whatever the scores look like. sectioning drops out of this test '
  +'wherever the scorecard marks it advisory, so the chain and the gate can never disagree about '
  +'it. Not asked at all for a short document — see the node before this one.',
 short:'The FIRST thing asked about a document, and it reads no score at all — the page count '
  +'comes off the source PDF, so nothing the extraction produces can change the answer. Under 10 '
  +'pages the tree comes from MinerU\'s raw markdown whatever Stage 1 would have built, so Stage '
  +'1, the outline pre-flight, Stage 2 and Stage 3 are all skipped rather than run and thrown '
  +'away. A memo that short has no hierarchy worth reconstructing and usually no printed contents '
  +'page, and it is Stage 1 trusting a two-anchor outline that mangles these in the first place. '
  +'This used to be asked inside the fallback chain, after Stage 2 had already been paid for: '
  +'across this corpus\'s 14 short documents that spent 341.6s on Stages 1-3 plus scoring, and '
  +'all 14 then adopted MinerU\'s tree, so every second of it built a tree that was discarded. '
  +'The same 10 pages narrows the scoring to completeness alone.',
 mineru:'The whole document handed to the visual model, taking ITS hierarchy. The most expensive '
  +'thing the pipeline can do, which is why it is last and not first. Skipped when the hierarchy '
  +'is already sound — toc and sectioning both at 70 or above — because a sound hierarchy is a '
  +'reason not to REPLACE the hierarchy; gating that on completeness instead once cost a '
  +'26-point sectioning regression recorded as a success. It is not a reason to stop on a '
  +'document missing half its words, so completeness below 60 sends it here anyway. '
  +'A rejected attempt is kept '
  +'in mineru_full_attempt/ with its own scorecard and the previous tree is moved back.',
 ai:'Opt-in and PAID, and the only stage that leaves this machine: each long-table section of '
  +'the Stage 3 tree is sent to Bedrock with its own PDF pages as ground truth, and the model '
  +'returns the same table with its structure and cell text repaired. Never runs automatically '
  +'-- ACI_STAGE4_AI_ENABLED gates it -- so on most documents this node is dark because nobody '
  +'asked, which is not the same as a stage that failed to run.',
 subchunk:'Splits each AI-corrected section into one file per numbered sub-section (6.1, 6.2, '
  +'6.3 ...) under a folder for the parent. Deterministic, local and free: no model is called. '
  +'Always follows Stage 4 and runs on nothing else, because only an AI-corrected table has '
  +'reliable sub-section headings to split on.'};

// ---- state derivation ------------------------------------------------------------------------
function exNodeStates(j,d){
 const det=(d&&!d.missing)?d:null;
 const steps=(j.steps&&Object.keys(j.steps).length)?j.steps:(((det&&det.timing)||{}).steps||{});
 const has=k=>Object.prototype.hasOwnProperty.call(steps,k);
 const stepsOf=n=>Array.isArray(n.step)?n.step:(n.step?[n.step]:[]);
 // TRAVELLED and HOW LONG IT TOOK are two different questions, and conflating them left an
 // in-flight document with a blank path behind its pulsing node. A finished document gets
 // its steps from the scorecard's timing block, where every value is a number, so "has a
 // duration" worked as a stand-in for "ran". A LIVE one has no timing block yet -- only the
 // list of stages its progress.json says are done -- so those arrive present-but-null.
 // ranOf answers travelled (presence alone); secOf answers duration and ignores a
 // non-number, so a node can be solid blue with no figure rather than claiming "0s".
 const ranOf=n=>stepsOf(n).some(k=>has(k));
 const secOf=n=>{
   let out=null;
   stepsOf(n).forEach(k=>{ if(has(k)&&typeof steps[k]==='number') out=(out||0)+steps[k]; });
   return out;
 };
 const fb=(det&&det.fallback)||{};
 const chain=fb.chain||[];
 const recOf=t=>chain.filter(c=>(c.tier||'')===t).pop()||null;
 const skipped=r=>/^\s*(skipped|not needed)/i.test(String((r||{}).status||''));
 const live=!!j.live, crashed=j.status==='error';
 const runNode=live?(EXSTAGE_NODE[j.running_step||j.stage||'']||null):null;
 const crashNode=crashed?(EXSTAGE_NODE[j.stage||'']||null):null;
 const triggered=!!(fb.triggered||j.fallback||j.trigger);
 const reason=String(fb.reason||((j.trigger||{}).reason)||'');
 // Routed straight to MinerU: a document too short to have a hierarchy skips the structural tier
 // entirely, so the chain's structural questions were never asked of it.
 //
 // special_mode is the SCORECARD's own record of this decision, carrying the page count and the
 // threshold that produced it. Asked first, so the node can say "6pp, bar is 10" instead of
 // restating a constant this screen would then own a stale copy of. The reason string and the
 // chain status stay as fallbacks for a row whose scorecard has not been read yet.
 const sm=(det&&det.special_mode)||null;
 const shortMode=!!(sm&&sm.mode==='short_document');
 const shortDoc=shortMode||/short document/i.test(reason)
   ||chain.some(c=>/too short/i.test(String(c.status||'')));
 const st={},fa={};
 const put=(k,state,facts)=>{ st[k]=state; fa[k]=facts||{}; };

 // A clone paid for none of this — the twin did. Claiming its stages ran would put a whole
 // corpus of duplicates' worth of GPU time on the screen that nobody ever spent.
 if(j.cloned){
   EXG_ORDER.forEach(k=>put(k,'clone',{word:'inherited from the twin'}));
   put('score', j.gate==='pass'?'done':j.gate==='review'?'warn':j.gate==='fail'?'crash':'none',
       {gate:j.gate,worst:j.worst,word:'inherited — identical PDF already extracted'});
   return {st:st,fa:fa,triggered:false,shortDoc:false,live:live,crashed:false,
           det:det,steps:steps,cloned:true,ed:{}};
 }

 // Routed straight to MinerU with the whole document, so the spine stages were SKIPPED —
 // a decision — rather than NOT REACHED, which is the absence of one, and the two must not
 // look alike. run_corpus takes this route twice: before Stage 1 when the page count says the
 // document is too short to have a hierarchy, and after it when Stage 1 reports it cannot read
 // the document at all. In the first case Stage 1 itself is skipped too.
 const bailed=!!fb.stage1_bail;
 const directMineru=shortDoc||bailed;

 // ---- the spine ----
 ['stage1','stage2','stage3','score'].forEach(k=>{
   const n=EXG_NODE[k], sec=secOf(n), ran=ranOf(n);
   const skippedHere=directMineru&&!ran&&k!=='score';
   put(k, crashNode===k?'crash' : runNode===k?'run' : ran?'done'
        : skippedHere?'skip' : 'none',
       {seconds:sec, word:skippedHere?(shortDoc?'skipped — under 10 pages'
                                               :'skipped — Stage 1 read nothing to work on')
                                     :undefined});
 });
 // Which dimensions set THIS document's verdict is a per-document fact the scorecard records.
 // Carried onto the node so the inspector never has to assume a fixed critical set.
 fa.score.gating=(det&&det.gating_dimensions)||null;
 fa.score.mode=sm;

 // The pre-flight is the one node whose OUTCOME is not in the scorecard. _preflight_outline
 // writes toc_preflight.json only when it applied a repair or errored trying, so the file's
 // ABSENCE is the answer "the outline was trusted", not a gap — as long as the step itself ran.
 const pfSec=secOf(EXG_NODE.preflight);
 const pf=det?(det.preflight||null):undefined;
 let pfState,pfF={seconds:pfSec};
 if(crashNode==='preflight') pfState='crash';
 else if(runNode==='preflight') pfState='run';
 else if(!has('toc_preflight')&&directMineru) pfState='skip',
   pfF.word=shortDoc?'skipped — under 10 pages':'skipped — no tree to repair';
 else if(!has('toc_preflight')) pfState='none', pfF.word=has('stage1')?'not in this run':'not reached';
 // fitted to the node below; the long form is what the inspector and the hover title say
 else if(pf===undefined) pfState='unk';
 else if(pf&&pf.applied===true) pfState='done',
  pfF.word='rescued — outline rebuilt from the printed contents page', pfF.repaired=true;
 else if(pf&&pf.error) pfState='warn', pfF.word='failed', pfF.error=pf.error;
 else pfState='skip', pfF.word='not needed — the outline was trusted', pfF.repaired=false;
 put('preflight',pfState,pfF);

 // ---- the page-count route, asked BEFORE the entry gate and blind to every score ----
 let sState,sF={};
 const nPages=(sm&&sm.pages!=null)?sm.pages
   :(j.pages!=null?j.pages:((det&&det.timing)||{}).pages);
 const sBar=(sm&&sm.threshold!=null)?sm.threshold:null;
 if(nPages!=null) sF.pages=nPages;
 // Asked before anything else runs, so ANY recorded step is evidence it was asked — this
 // node must not wait for the scorecard the way the gates further along do.
 if(shortDoc) sState='warn',
   sF.word=(nPages!=null?nPages+'pp':'short')+(sBar!=null?' < '+sBar:'')+' — straight to MinerU',
   sF.short=true;
 else if(nPages!=null) sState='skip', sF.word=nPages+'pp — route not taken';
 else if(Object.keys(steps).length) sState='skip', sF.word='route not taken';
 else sState='none';
 put('short',sState,sF);

 // ---- the chain entry gate ----
 // Never reached by a short document: run_chain takes the page-count branch and returns before
 // needs_help is called, so painting this node 'escalated' for one of them credited the gate
 // with a decision it was never asked to make.
 let nState,nF={};
 if(shortDoc) nState='skip', nF.word='not asked · page route';
 // A document with a gate has been scored, whatever its timing block says: `scorecard`
 // is a sub-second step and a run that predates its recording has no entry for it, which
 // made a perfectly ordinary scored document report "not reached" at the entry gate.
 else if(crashNode==='score') nState='none';
 else if(!has('scorecard')&&!det&&!j.gate) nState='none';
 else if(triggered) nState='warn', nF.word='escalated';
 else if(det||j.tier==='first_pass'||j.tier==null) nState='skip', nF.word='not escalated';
 else nState='unk';
 put('needs',nState,nF);

 // The structure gate that used to sit here as its own node asked whether replacing the
 // hierarchy was worth it. It is still live code and it can still skip the tier -- but its
 // ANSWER is recorded on the MinerU full node itself, as "not needed: structure is sound",
 // so the node only ever restated the question its neighbour already answered. Measured
 // over the 30 documents that entered the chain: it skipped the tier on none of them.
 const rm=recOf('mineru_full');

 // ---- Tier 3 ----
 let mState,mF={seconds:secOf(EXG_NODE.mineru),rec:rm};
 if(crashNode==='mineru') mState='crash';
 else if(runNode==='mineru') mState='run';
 else if(!triggered) mState='skip', mF.word='not run — the first pass was good enough';
 else if(rm){
   if(skipped(rm)) mState='skip', mF.word='not needed';
   else if(rm.adopted===true) mState='done', mF.word='adopted';
   else if(rm.adopted===false) mState='disc', mF.word='ran, not adopted';
   else mState='warn', mF.word='ran';
 }
 // A LIVE tier has not been ADOPTED -- it has not finished. Reached here from the row's
 // `tier` field alone, a document mid-MinerU reported "adopted" with no scorecard on disk
 // to support it, and the node went solid blue while the run was still going.
 else if(live&&j.tier==='mineru_full') mState='run';
 else if(!det) mState=(j.tier==='mineru_full')?'done':'unk',
                mF.word=(j.tier==='mineru_full')?'adopted':'';
 else mState='none';
 put('mineru',mState,mF);

 // ---- the accept test, folded onto the Scorecard ------------------------------------
 // It had its own node and said nothing the Scorecard does not: "completeness >= 90 AND a
 // usable shape" is read off the same scorecard the node beside it already reports. Kept as
 // FACTS on the score node so the inspector can still show whether the chain accepted the
 // result -- what went is the box, not the information.
 let aState,aF={};
 const hard=!!(j.hard_fail||fb.hard_fail);
 if(!triggered) aState='skip', aF.word='not asked — no tier ran';
 else if(hard) aState='crash', aF.word='no tier cleared', aF.hard=true, aF.why=fb.hard_fail_reason;
 else if(fb.accepted===true||j.accepted===true) aState='done', aF.word='accepted', aF.accepted=true;
 else if(fb.accepted===false||j.accepted===false) aState='warn', aF.word='not accepted', aF.accepted=false;
 else if(live) aState='none';
 else aState=det?'none':'unk';
 // (aState/aF now decorate the Scorecard node below)

 // ---- the verdict, folded onto the Scorecard ---------------------------------------
 // Two boxes for one fact. The Verdict node showed the gate and the worst gating dimension,
 // both of which come from the scorecard the previous node IS -- so the pair read as a
 // process step that does not exist. The Scorecard now carries the gate as its COLOUR and
 // the numbers as its caption, which is the same information in one place and two nodes
 // shorter: the whole trace fits without scrolling.
 // The SCORECARD first, the run ledger only as a stand-in until it loads. It used to be the
 // other way round, so a document re-scored after its run — a re-verify, a --force re-run,
 // anything that rewrites scorecard.json — showed the ledger's old number beside the new
 // scorecard's dimensions, and the two could not be reconciled from the screen. Where they
 // disagree the ledger's figure is kept and shown as its own row rather than dropped.
 const gate=(det&&det.gate)||j.gate||null;
 const scWorst=det?det.worst_score:null;
 const worst=(scWorst!=null)?scWorst:j.worst;
 const ledgerWorst=(scWorst!=null&&j.worst!=null&&Math.abs(scWorst-j.worst)>0.05)?j.worst:null;
 let vState,vF={gate:gate,worst:worst,ledger:ledgerWorst,
   gating:(det&&det.gating_dimensions)||null,mode:sm};
 if(crashed) vState='crash', vF.word='crashed — no scorecard', vF.error=j.error;
 // A running document has not REACHED its verdict, so the terminal is faint like any other node
 // ahead of the live position. Painting it 'running' put a blue terminal on every in-flight row
 // from the first tick, which reads as though the gate were being decided.
 else if(live) vState='none', vF.word='not scored yet';
 else if(hard) vState='crash', vF.word='hard fail';
 else if(gate==='pass') vState='done', vF.word='pass';
 else if(gate==='review') vState='warn', vF.word='review';
 else if(gate==='fail') vState='crash', vF.word='fail';
 else vState='none';
 // The Scorecard becomes the judgement node: coloured by the gate, captioned with the
 // numbers, and still carrying the accept test's facts for the inspector.
 st.score = vState;
 fa.score = Object.assign({}, fa.score || {}, vF, {
   accepted: aF.accepted, accept_word: aF.word, accept_why: aF.why,
   seconds: (fa.score || {}).seconds,
   word: [vF.word, (worst != null ? 'worst ' + worst : null)].filter(Boolean).join(' \u00b7 '),
 });

 // ---- the two opt-in AI stages, after the gate --------------------------------------------
 // The ledger records TIME and nothing else for these, so time is all this can honestly read:
 // no model name, no cost. Three outcomes, and the screen must keep them apart:
 //   ran            -> 'done', with the seconds
 //   asked, refused -> 'skip'  — a DECISION. The disabled path records exactly 0.0, so it is
 //                     read by key PRESENCE; read by value it would vanish into "never asked".
 //   never asked    -> 'none'  — the ABSENCE of a decision, which is what most documents are.
 // That is the same distinction this file already draws everywhere else, applied to a stage
 // whose normal state is "nobody asked for it".
 const aiSec=secOf(EXG_NODE.ai);
 let aiState,aiF={seconds:has('stage4_ai')?aiSec:undefined};
 if(crashNode==='ai') aiState='crash';
 else if(runNode==='ai') aiState='run';
 else if(has('stage4_ai')&&aiSec>0) aiState='done', aiF.word='ran';
 else if(has('stage4_ai')) aiState='skip',
   aiF.word='disabled for this run — nothing sent, nothing spent', aiF.seconds=undefined;
 else aiState='none', aiF.word='not requested';
 put('ai',aiState,aiF);

 // Stage 5 is never a question of its own: it always follows Stage 4 and runs on nothing else,
 // so "Stage 4 did not run" is the whole reason it is dark and is worth saying outright.
 const scSec=secOf(EXG_NODE.subchunk);
 let scState,scF={seconds:has('stage5_subchunk')?scSec:undefined};
 if(crashNode==='subchunk') scState='crash';
 else if(runNode==='subchunk') scState='run';
 else if(has('stage5_subchunk')&&scSec>0) scState='done', scF.word='ran';
 else if(aiState==='done') scState='none', scF.word='Stage 4 ran, split not recorded';
 else if(has('stage5_subchunk')) scState='skip',
   scF.word='Stage 4 did not run', scF.seconds=undefined;
 else scState='none', scF.word='Stage 4 was not requested';
 put('subchunk',scState,scF);

 // Everything after the point a document stopped is not "not run", it is NOT REACHED, and the
 // two must not look alike: one is a decision, the other is the absence of one.
 if(crashNode){
   let past=false;
   EXG_ORDER.forEach(k=>{
     if(k===crashNode){ past=true; return; }
     if(past&&st[k]!=='crash') put(k,'none',{word:'not reached'});
   });
 }

 // Which edges the document actually took. Drawn dark; the rest stay faint, so the path reads
 // without anyone reading a label. On one rail there are only four: the backwards hop when the
 // pre-flight repaired the outline, and three bypasses that jump forward over steps this
 // document was routed past. Everything else is the rail itself, and a rail segment is dark when
 // the document REACHED the node it leads into — the same rule at both scales.
 const ran=k=>['done','run','disc','warn','crash','clone'].indexOf(st[k])>=0;
 const ed={
   repair: pfState==='done'&&pfF.repaired===true,
   // A short document jumps the entry gate: routed to MinerU before the spine runs.
   shortBypass: triggered&&shortDoc,
   // needs_help said no: everything between here and the verdict was never asked. Only TAKEN
   // once the document has a verdict — while it is still in a tier the terminal has not been
   // reached, and drawing the last hop dark claimed a junction the run had not got to yet.
   passBypass: !triggered&&st.needs==='done'&&st.score!=='none',
   mineruRan: ran('mineru'),
   toChain: triggered&&!shortDoc,
   toVerdict: st.score!=='none'
 };
 return {st:st,fa:fa,triggered:triggered,shortDoc:shortDoc,live:live,crashed:crashed,
         det:det,steps:steps,cloned:false,ed:ed};
}


// ---- the full graph, in the drill-down -------------------------------------------------------
// Same nodes, same states, same colours as the row cell — one picture at two scales. What the
// full size adds is the LABELS on the branches: which way each junction went and, where the
// pipeline recorded them, the actual numbers that decided it.
// LANES, not one rail. Fifteen nodes on a single line ran 1830px wide and read as one
// undifferentiated queue: the four stages a healthy document actually walks were lost among the
// nine nodes of machinery that most documents never touch.
//
// The lane split is the fix, and the risk it carries is the reason the old comment here refused
// it: a second lane once made Tier 2 look like THE place this pipeline repairs an outline. So
// the lanes are not two halves of one flow — the SPINE is what a healthy document does, and
// everything below it is conditional machinery hanging off the point that triggers it. The
// re-score loop is drawn explicitly and labelled, because a tier does not continue the
// pipeline: MinerU full replaces the tree and the document is SCORED AGAIN, kept only if it
// beats the first pass. That is why the loop closes on the Scorecard.
//
//        [TOC rescue] (outline check, rebuild, loops back into Stage 1)
//   ->  [Stage 1] -> [Stage 2] -> [Stage 3] -> [Scorecard] -> [Stage 4] -> [Stage 5]
//    \                                            |   ^
//     \                                     needs? |   | re-scored
//      \                                       [MinerU full]
//       -> under 10pp? -------------------------^
//
// The Scorecard is the judgement node: coloured by the gate, captioned "pass . worst 91.9".
// It absorbed two boxes that said nothing it does not -- `acceptable?` reads the same
// scorecard, and `Verdict` showed the gate and worst dimension that ARE the scorecard -- so
// the pair read as process steps that do not exist. Ten nodes at 1040px instead of twelve
// at 1284, which is what makes the whole trace fit without scrolling.
//
const EXG_PITCH=122, EXGR=5.5;
// One shared column grid so a node in the chain lane sits directly under the spine node whose
// decision put it there — needs_help? under the Scorecard, above all.
const EXG_COL=c=>Math.round(64+EXG_PITCH*c);
// Lane spacing allows for a node box of cy-36..cy+26: name, sub-label, mark, caption.
const EXG_LANE={spine:124, chain:216, above:46, route:312};
// Where every node sits. Kept OUT of EXG_STEPS, which has one job: execution order, which the
// row rail, the "path" summary and the crash walk all read positionally.
const EXG_LAY={
 // the spine — what a document that needs no help does, and nothing else
 // The Scorecard is ON the spine: every document is scored, so it is not conditional
 // machinery. The descent below it happens only when the score is bad, which is what
 // makes the picture readable -- a healthy document is one straight line.
 stage1:{c:0,y:'spine'}, stage2:{c:1,y:'spine'}, stage3:{c:2,y:'spine'},
 score:{c:3,y:'spine'},
 ai:{c:6,y:'spine'}, subchunk:{c:7,y:'spine'},
 // above the spine, because its only outcome is to send Stage 1 round again
 preflight:{c:0.5,y:'above'},
 // the chain, hanging off Validation. Starts UNDER the Scorecard because that is what
 // decides whether any of it runs.
 needs:{c:4,y:'chain'}, mineru:{c:5,y:'chain'},
 // the page-count route, which is asked BEFORE Stage 1 and skips the spine entirely
 short:{c:0,y:'route'}};
const EXGX=k=>EXG_COL(EXG_LAY[k].c), EXGY=k=>EXG_LANE[EXG_LAY[k].y];
// Width from the RIGHTMOST column actually in use, never a fixed column index -- that
// was the same mistake as the old literal 13 -- a node one column further right would
// have fallen off the canvas, drawn but invisible.
const EXGW=EXG_COL(Math.max.apply(null,Object.keys(EXG_LAY).map(k=>EXG_LAY[k].c)))+EXG_PITCH, EXGH=352;
// Bands for the arcs that jump nodes, and the y each label sits on. No two that overlap
// horizontally may share a band.
// One band left: the loop from acceptable? back up to the Scorecard. The page-count route
// runs along its own lane (EXG_LANE.route) rather than in a band, so it needs no entry --
// a `route` entry here was dead and read as though a second band existed.
const EXG_HOP={rescore:{y:262,l:274}};
// Last-resort fallback for a scorecard written before `dimensions[k].critical` existed. Never
// consulted when the document's own record is present — see exGDims.
const EXG_CRIT={completeness:1,placement:1,fidelity:1,sectioning:1};

// A gate is a diamond, a stage is a circle. That is the only shape distinction left, and it says
// which kind of thing a node is before a word is read.
function exGMark(cx,cy,gate){
 return gate
  ? '<polygon class="exgbox" points="'+[cx+','+(cy-EXGR-1),(cx+EXGR+1)+','+cy,
      cx+','+(cy+EXGR+1),(cx-EXGR-1)+','+cy].join(' ')+'"/>'
  : '<circle class="exgbox" cx="'+cx+'" cy="'+cy+'" r="'+EXGR+'"/>';
}

// Advance width per character, measured off the rendered face rather than guessed (4.28-4.32 at
// 9.5px). Only ever used to decide whether a caption has to be clipped, never to position
// anything -- a caption wider than its share of the rail collides with its neighbours.
const EXG_CH=4.45;
function exGClip(txt,pitch){
 const max=Math.floor(pitch*0.92/EXG_CH);
 return txt.length<=max?txt:(max>3?txt.slice(0,max-1).trim()+'…':'');
}

// One caption line, not three. The cost, and what became of it only when that is not simply
// "ran". A node's purpose and the long form of its state are in the inspector and the hover
// title, which is where someone asking that question already is.
function exGNode(n,cx,cy,pitch,state,facts,sel){
 const f=facts||{}, word=f.word||EXG_STATEWORD[state]||state;
 // The Scorecard is the judgement node now, so it is the one that falls back to the worst
 // gating dimension when there is no duration to show.
 const fig=f.seconds!=null?exDur(f.seconds):(n.k==='score'&&f.worst!=null?('worst '+f.worst):'');
 const cap=[fig, (word==='ran'?'':word)].filter(Boolean).join(' · ');
 return '<g class="exgn s-'+state+(sel?' sel':'')+'" data-exnode="'+escA(n.k)+'" tabindex="0">'
  +'<title>'+esc(n.s+' · '+n.t+' — '+word+(fig?' ('+fig+')':''))+'</title>'
  +'<rect class="exghit" x="'+(cx-pitch/2)+'" y="'+(cy-36)+'" width="'+pitch+'" height="62"/>'
  +'<text class="exgnl" x="'+cx+'" y="'+(cy-24)+'">'+esc(n.s)+'</text>'
  // WHAT the step is, on its own line under the name. It lived only in the hover title, so
  // the graph said "Stage 1 / Stage 2 / Stage 3" and never which engine does the work --
  // and renaming them in `t` alone changed nothing anyone could see. Clipped to the column
  // like the caption below it; the full text stays in the title.
  +'<text class="exgnt" x="'+cx+'" y="'+(cy-13)+'">'+esc(exGClip(n.t,pitch))+'</text>'
  +exGMark(cx,cy,n.gate)
  +(sel?'<circle class="exgring" cx="'+cx+'" cy="'+cy+'" r="'+(EXGR+3.5)+'"/>':'')
  +(cap?'<text class="exgnc" x="'+cx+'" y="'+(cy+20)+'">'+esc(exGClip(cap,pitch))+'</text>':'')
  +'</g>';
}

function exGraph(j,S,sel){
 const st=S.st, fa=S.fa, ed=S.ed;
 // `id` is not decoration: it is how "did the path this document took actually light up?"
 // stops being a question you answer by looking. Every edge is named, so a test can execute
 // this function against a real job and assert edge by edge -- which is how the chain
 // segment below was found lighting up on a document that never entered the chain.
 const E=(pts,on,arrow,id)=>'<polyline class="exgedge'+(on?' on':'')+'"'
   +(id?' data-edge="'+id+'"':'')+' points="'+pts+'"'
   +(arrow?' marker-end="url(#exgar'+(on?'':'f')+')"':'')+'/>';
 const L=(x,y,txt,on,anchor)=>txt?'<text class="exgel'+(on?' on':'')+'" x="'+x+'" y="'+y
   +'" text-anchor="'+(anchor||'middle')+'">'+esc(txt)+'</text>':'';
 // Blue means WALKED. This used to be `st[k]!=='none'`, which counted 'skip' as reached --
 // fine while 'skip' meant "ran and chose not to act", and wrong the moment the gates that
 // guard an untaken branch became 'skip' too: every edge into under-10pp?, needs_help?,
 // MinerU full and acceptable? drew blue into a hollow node, so the picture claimed a path
 // it also showed as not taken.
 //
 // ONE invariant, and every edge obeys it: a blue arrow can never touch a hollow node.
 // There used to be an exception for a 'skip' that recorded time -- the TOC rescue that
 // looked at the outline and left it alone -- and it produced exactly the contradiction
 // this graph exists to avoid: on 42 of 150 documents a blue arrow pointed at a hollow
 // TOC rescue node. The step did run, but the RESCUE did not, and the node says so; the
 // edge has to agree with the node.
 const WALKED=['done','run','warn','disc','crash','clone'];
 const reached=k=>WALKED.indexOf(st[k])>=0;
 const G=EXGR+3;
 // An edge between two nodes, by KEY rather than by index. Index arithmetic was only ever
 // meaningful while every node sat on one line in execution order; with lanes, "the next node"
 // and "the node to the right" are different questions.
 // BOTH ends, not just the one it leads into. A page-routed document walks under-10pp? and
 // MinerU full but never needs_help?, and "blue if the destination was walked" lit the
 // needs_help? -> MinerU full segment for it -- drawing a path through a node the same
 // picture showed as skipped.
 const seg=(a,b)=>E((EXGX(a)+G)+','+EXGY(a)+' '+(EXGX(b)-G)+','+EXGY(b),
                    reached(a)&&reached(b), true, a+'->'+b);
 let g='<defs>'
  +'<marker id="exgar" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="5" markerHeight="5"'
  // The arrowheads cap the edges and must carry the same two values, but a marker's fill
  // cannot be inherited or reached by a CSS token -- so these are the ONE place the blue is
  // repeated as a literal. Keep them in step with --prog / --neutral-100 above; a test
  // pins the pair. Left as neutral-500/200 they were the reason an untaken branch still
  // ended in a clearly visible point after the edges themselves were lightened.
  +' orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#2a78d6"/></marker>'
  +'<marker id="exgarf" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="5" markerHeight="5"'
  +' orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#383B3B"/></marker></defs>';

 // ---- the spine: Stage 1 -> 2 -> 3 -> Validation ------------------------------------------
 ['stage2','stage3'].forEach((k,i)=>{
   g+=seg(['stage1','stage2'][i], k); });

 // Entry, and the branch taken before Stage 1 exists. The page count is read off the source
 // PDF, so this is asked FIRST — drawing it as a hop out of Stage 1 would claim Stage 1 ran.
 const ex=EXG_COL(-0.42), sy=EXG_LANE.spine, ry=EXG_LANE.route;
 g+=E(ex+','+sy+' '+(EXGX('stage1')-G)+','+sy, reached('stage1'), true, 'entry->stage1');
 g+=E(ex+','+sy+' '+ex+','+ry+' '+(EXGX('short')-G)+','+ry, reached('short'), true,
      'entry->short');

 // ---- the TOC rescue: a side-check ON Stage 1, not a station between 1 and 2 -----------
 // Its outcome is to send Stage 1 round again, so it belongs above Stage 1 as an out-and-back
 // branch. Routing the spine THROUGH it meant the blue Stage1->Stage2 line came out of the
 // rescue node even on a document where the rescue never ran.
 const pfx=EXGX('preflight'), aby=EXG_LANE.above;
 g+=E(EXGX('stage1')+','+(sy-G)+' '+EXGX('stage1')+','+aby+' '+(pfx-G)+','+aby,
      reached('stage1')&&reached('preflight'), true, 'stage1->tocrescue');
 // and back down into Stage 1, dark only when the outline was actually rebuilt
 g+=E((pfx+G)+','+aby+' '+(pfx+22)+','+aby+' '+(pfx+22)+',84 '
      +(EXGX('stage1')+10)+',84 '+(EXGX('stage1')+10)+','+(sy-G), ed.repair, true,
      'tocrescue->stage1');
 g+=L((EXGX('stage1')+EXGX('preflight'))/2+26, 84,
      ed.repair?'outline repaired — Stage 1 re-ran':'', ed.repair, 'start');

 // ---- the Scorecard is ON the spine; the descent below it is CONDITIONAL --------------
 g+=seg('stage3','score');
 // Scorecard -> Verdict along the spine. This IS the "no tier ran" path, so it needs no
 // bypass arc of its own: a healthy document is one straight line and nothing below the
 // spine is on it. Four nodes all reading "chain not entered" was the old picture.
 g+=E((EXGX('score')+G)+','+sy+' '+(EXGX('ai')-G)+','+sy,
      reached('score')&&reached('ai'), true, 'score->ai');
 g+=L((EXGX('score')+EXGX('ai'))/2, sy-15,
      ed.passBypass?'scored well enough \u2014 no tier ran':'', ed.passBypass);
 // and the descent, taken only when the score is bad
 const dsc=EXGY('needs')-50;          // clears the spine caption above and the name below
 g+=E(EXGX('score')+','+(sy+G)+' '+EXGX('score')+','+dsc+' '
      +EXGX('needs')+','+dsc+' '+EXGX('needs')+','+(EXGY('needs')-G),
      ed.toChain&&reached('score')&&reached('needs'), true, 'score->needs');
 g+=L(EXG_COL(3.5), dsc-5,
      ed.toChain?('escalated'+(S.cause?' \u00b7 '+S.cause:'')):'', ed.toChain);
 g+=seg('needs','mineru');

 // ---- acceptable? goes back UP to the Scorecard, because the tier is RE-SCORED --------
 // MinerU full does not re-run Stages 1-3; it replaces the tree and the document is scored
 // AGAIN (fallback_chain._mineru_full -> run_fallback returns a new scorecard, and _better
 // compares the two). So the loop closes on the Scorecard. The label used to read "a tier
 // re-runs Stages 1-3" -- true of the printed-TOC tier, which is gone, and never true of
 // this one.
 // Always from MinerU full now that acceptable? has no node. It used to anchor there when
 // the accept test had been recorded, which a page-routed document never reaches --
 // fb["accepted"] is written at the END of run_chain and the short-document branch returns
 // first, 14 of the 30 chain entrants here.
 const from='mineru';
 const axx=EXGX(from), rsy=EXG_HOP.rescore.y;
 g+=E((axx+G)+','+EXGY(from)+' '+(axx+EXG_PITCH/2)+','+EXGY(from)+' '
      +(axx+EXG_PITCH/2)+','+rsy+' '+EXGX('score')+','+rsy+' '
      +EXGX('score')+','+(sy+G), reached(from)&&reached('score')&&ed.mineruRan, true,
      from+'->score');
 g+=L(EXG_COL(5.3), EXG_HOP.rescore.l,
      ed.mineruRan?'re-scored \u2014 kept only if it beats the first pass':'', ed.mineruRan);
 // and on along the spine to the two opt-in AI stages
 g+=seg('ai','subchunk');

 // ---- the page-count route: it does not rejoin the spine, it lands in MinerU full -----
 g+=E((EXGX('short')+G)+','+ry+' '+EXGX('mineru')+','+ry+' '
      +EXGX('mineru')+','+(EXGY('mineru')+G),
      ed.shortBypass&&reached('short')&&reached('mineru'), true, 'short->mineru');
 g+=L((EXGX('short')+EXGX('mineru'))/2, ry-9,
      ed.shortBypass?'under 10 pages — Stage 1, the pre-flight, Stage 2 and Stage 3 are never run':'',
      ed.shortBypass);

 EXG_STEPS.forEach(n=>{
   g+=exGNode(n,EXGX(n.k),EXGY(n.k),EXG_PITCH,st[n.k],fa[n.k],sel===n.k); });
 return '<div class="exgscroll"><svg class="exg" viewBox="0 0 '+EXGW+' '+EXGH+'" role="img"'
  +' aria-label="'+escA('pipeline trace for '+(j.label||'this document'))+'">'+g+'</svg></div>';
}

"""
