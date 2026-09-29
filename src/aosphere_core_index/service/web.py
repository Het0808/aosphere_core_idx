"""Cross-region search UI served at / (inline, self-contained).

Themed to match aosphere-ai-playground: light, monochrome neutral scale, Inter.
Results are grouped Region (continent) -> Jurisdiction -> Clause; region-group
chips filter (entitlement). Selecting a clause shows its content AND its
parent-child subtree so heading-only clauses are meaningful.
"""

from aosphere_core_index.service import summary_trace_graph, trace_graph

_PAGE_TMPL = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>aosphere · Core Index</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="alternate icon" href="https://dev1.aoslogin.net/aosphere.ico">
<script src="https://cdn.jsdelivr.net/npm/oidc-client-ts@2.4.0/dist/browser/oidc-client-ts.min.js"></script>
<style>
 :root{
   --neutral-50:#F7F8F9;--neutral-100:#EAEDED;--neutral-200:#D4D9D9;--neutral-300:#B4C0C3;
   --neutral-400:#92A4A6;--neutral-500:#7B888A;--neutral-600:#4D5454;--neutral-700:#383B3B;
   --neutral-800:#262727;--neutral-900:#181919;--neutral-950:#0C0D0D;
   --ink:var(--neutral-900);--muted:var(--neutral-600);--line:var(--neutral-100);
   --surface:#fff;--bg:var(--neutral-50);--accent:var(--neutral-900);--accent-d:var(--neutral-950);
   /* Progress blue: what a document has actually been through. One token, so the nodes,
      the edges and the SVG arrowheads cannot drift apart. */
   --prog:#2a78d6;--prog-ink:#1b4f8f;
   /* The trace graph names its own tokens so it can be dropped into a page with a
      different palette (the local monitor's is warm, and has a dark mode). Mapped here. */
   --g-ink:var(--ink);--g-muted:var(--muted);--g-surface:var(--surface);
   --g-line:var(--neutral-100);--g-line-strong:var(--neutral-700);--g-faint:var(--neutral-300);
   --danger:#c1342d;--warn:#b45309;--radius:12px;
   --shadow:0 1px 2px rgba(24,25,25,.06),0 4px 16px rgba(24,25,25,.06);
 }
 *{box-sizing:border-box}
 html,body{height:100%;margin:0}
 body{font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
   color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
 header{padding:14px 24px;border-bottom:1px solid var(--neutral-200);background:var(--surface)}
 .brand{display:flex;align-items:baseline;gap:12px}
 .brand h1{font-size:18px;margin:0;font-weight:700;letter-spacing:-.01em}
 .brand .sub{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
 .authbox{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:13px}
 .authbox .who{color:var(--muted)}
 .authbtn{font:inherit;font-size:12.5px;font-weight:600;padding:5px 12px;border-radius:8px;border:1px solid var(--neutral-200);background:#fff;color:var(--ink);cursor:pointer}
 .authbtn:hover{border-color:var(--accent)}
 .gate{margin:auto;text-align:center;padding:60px 24px;color:var(--muted)}
 .gate button{margin-top:16px}
 .searchbar{margin-top:12px;display:flex;gap:10px}
 #q{flex:1;font:inherit;font-size:15px;padding:12px 14px;border-radius:8px;border:1px solid var(--neutral-200);background:#fff;color:var(--ink)}
 #q:focus{outline:none;border-color:var(--accent)}
 #minscore{font:inherit;font-size:14px;color:var(--ink);padding:0 34px 0 14px;border-radius:8px;border:1px solid var(--neutral-200);background:#fff;cursor:pointer;
   -webkit-appearance:none;appearance:none;
   background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%237B888A' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>");
   background-repeat:no-repeat;background-position:right 12px center}
 #minscore:focus{outline:none;border-color:var(--accent)}
 #go{font:inherit;font-weight:600;padding:12px 22px;border-radius:8px;border:1px solid transparent;background:var(--accent);color:#fff;cursor:pointer;transition:.15s}
 #go:hover{background:var(--accent-d)}
 .tabs{display:inline-flex;gap:4px;margin-left:18px}
 .tab{font:inherit;font-size:13px;font-weight:600;padding:5px 13px;border-radius:8px;border:1px solid var(--neutral-200);background:#fff;color:var(--muted);cursor:pointer}
 .tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
 .aionly{display:none;align-items:center;gap:8px;margin-left:auto}
 .aionly select{font:inherit;font-size:13px;padding:6px 9px;border-radius:8px;border:1px solid var(--neutral-200);background:#fff}
 .aionly .cost{font-size:11.5px;color:var(--muted)}
 /* Answers are brief by default; this asks for the reasoning. It sits next to Send, where
    the question is typed — in the header it was next to the model dropdown and unfindable. */
 #explainwrap{display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;padding:0 12px;
   font-size:13px;color:var(--muted);white-space:nowrap;cursor:pointer;user-select:none;
   border:1px solid var(--neutral-200);border-radius:9px;background:#fff}
 #explainwrap input{margin:0;cursor:pointer;accent-color:var(--accent)}
 #explainwrap:hover{border-color:var(--accent)}
 #explainwrap:has(input:checked){color:var(--ink);font-weight:600;border-color:var(--accent);
   box-shadow:inset 0 0 0 1px var(--accent)}
 .answer-h{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px}
 .answer-h b{color:var(--accent);text-transform:none;letter-spacing:0;font-size:13px}
 .answer-body{font-size:14.5px;line-height:1.7;color:var(--neutral-900)}
 .answer-body b{font-weight:650}
 .answer-body h3{font-size:15px;margin:14px 0 4px} .answer-body h4{font-size:13.5px;margin:12px 0 3px}
 .answer-body p{margin:.45em 0} .answer-body ul,.answer-body ol{margin:.4em 0 .6em 1.3em}
 .answer-body li{margin:.18em 0;line-height:1.55}
 .answer-body code{background:var(--neutral-100);padding:1px 5px;border-radius:5px;font-size:12.5px}
 .answer-body table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px;display:block;overflow-x:auto}
 .answer-body th,.answer-body td{border:1px solid var(--neutral-200);padding:6px 9px;text-align:left;vertical-align:top}
 .answer-body th{background:var(--neutral-50);font-weight:650}
 .acts{margin:6px 0 14px;border-left:2px solid var(--neutral-200);padding-left:12px}
 /* one line at a time while streaming — click to expand the full step-by-step list below it */
 .acts-line{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--neutral-700);padding:2px 0;cursor:pointer}
 .acts-line:hover{color:var(--ink)}
 .acts-line .ai{display:inline-block;width:18px;flex:none}
 .acts-text{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;animation:actfade .35s ease}
 .acts-line .chev{flex:none;font-size:9px;color:var(--muted);transition:transform .15s}
 .acts.open .chev{transform:rotate(180deg)}
 .acts-list{display:none;margin-top:2px}
 .acts.open .acts-list{display:block}
 .act{font-size:12.5px;color:var(--neutral-700);padding:2px 0;animation:fade .3s ease}
 .act .ai{display:inline-block;width:18px}
 @keyframes fade{from{opacity:0;transform:translateY(-2px)}to{opacity:1}}
 @keyframes actfade{from{opacity:0}to{opacity:1}}
 .regionbar{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
 .regionbar .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-right:4px}
 .rdrop{position:relative}
 .rdrop-btn{font:inherit;font-size:12.5px;padding:6px 12px;border-radius:8px;border:1px solid var(--neutral-200);background:#fff;color:var(--ink);cursor:pointer}
 .rdrop-panel{position:absolute;top:120%;left:0;z-index:30;width:340px;max-height:62vh;overflow:auto;background:#fff;border:1px solid var(--neutral-200);border-radius:11px;box-shadow:var(--shadow);padding:10px;display:none}
 .rdrop-panel.open{display:block}
 #rsearch{width:100%;font:inherit;font-size:13px;padding:8px 10px;border:1px solid var(--neutral-200);border-radius:8px;margin-bottom:8px}
 #rsearch:focus{outline:none;border-color:var(--accent)}
 .rdrop-bar{display:flex;gap:14px;margin:0 2px 8px}
 .lnk{background:none;border:0;color:var(--accent);font:inherit;font-size:12px;cursor:pointer;padding:0;font-weight:600}
 .rrow{display:flex;align-items:center;gap:7px;padding:4px 6px;border-radius:6px;font-size:13px}
 .rrow.reg{font-weight:650}
 .rrow.prod{font-size:13.5px;border-bottom:1px solid var(--line);margin-top:2px}
 .ptag{display:inline-block;white-space:nowrap;font-size:10px;font-weight:700;background:#0f766e;color:#fff;padding:1px 6px;border-radius:5px;vertical-align:middle}
 .rrow:hover,.jrow:hover{background:var(--neutral-50)}
 .rrow .badge{width:9px;height:9px;border-radius:50%}
 .caret{width:14px;text-align:center;cursor:pointer;color:var(--muted);user-select:none}
 .jlist{margin-left:24px}
 .jrow{display:flex;align-items:center;gap:7px;padding:3px 6px;border-radius:6px;font-size:12.5px;cursor:pointer}
 .rdrop input[type=checkbox]{accent-color:var(--accent);width:15px;height:15px}
 main{flex:1;display:grid;grid-template-columns:minmax(520px,600px) 6px 1fr;min-height:0}
 main.chat{grid-template-columns:1fr 6px minmax(340px,460px)}
 main.chat #left{display:none}
 /* Drag handle between the left pane (results/chat) and #detail — JS overrides the pixel
    widths inline while dragging; hidden wherever there is only one column to split. */
 #colsplit{width:6px;cursor:col-resize;background:var(--line);transition:background .15s}
 #colsplit:hover,#colsplit.dragging{background:var(--accent)}
 main.docmode #colsplit,main.gallerymode #colsplit,main.extractmode #colsplit,main.promptmode #colsplit{display:none}
 #chat{display:none;flex-direction:column;min-height:0;border-right:1px solid var(--line);background:var(--surface)}
 main.chat #chat{display:flex}
 #msgs{flex:1;overflow:auto;padding:18px 20px;display:flex;flex-direction:column;gap:12px}
 .msg{font-size:14px;line-height:1.6}
 .msg.user{align-self:flex-end;max-width:85%;background:var(--accent);color:#fff;padding:8px 13px;border-radius:13px 13px 4px 13px}
 .msg.ai{align-self:flex-start;width:100%;background:#fff;border:1px solid var(--line);padding:11px 14px;border-radius:13px 13px 13px 4px;box-shadow:var(--shadow)}
 .srclabel{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:12px 0 5px}
 .answer-body .cite{color:var(--accent);font-weight:600;cursor:pointer;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:2px}
 .answer-body .cite:hover{background:var(--neutral-100);border-radius:4px}
 .answer-body blockquote{border-left:3px solid var(--neutral-300);margin:.6em 0;padding:2px 12px;background:var(--neutral-50);color:var(--neutral-700);border-radius:0 8px 8px 0}
 .answer-body blockquote p{margin:.35em 0}
 .answer-body hr{border:0;border-top:1px solid var(--neutral-200);margin:14px 0}
 .answer-body em{font-style:italic}
 .bar{height:3px;border-radius:3px;margin:8px 0;background:linear-gradient(90deg,var(--neutral-100),var(--accent),var(--neutral-100));background-size:200% 100%;animation:barflow 1.1s linear infinite}
 @keyframes barflow{0%{background-position:200% 0}100%{background-position:-200% 0}}
 /* ---- guided interview (Marketing Restrictions), inside the AI Mode chat ---- */
 /* Deliberately NOT styled as an assistant answer: it is a form the user has to act on,
    and one that reads as prose gets scrolled past. */
 .ivcard{align-self:flex-start;width:100%;background:var(--neutral-50);border:1px solid var(--neutral-200);
   border-left:3px solid var(--accent);padding:12px 14px;border-radius:4px 13px 13px 13px}
 .ivstep{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
 .ivq{font-size:14.5px;font-weight:600;line-height:1.5;margin-bottom:10px}
 .ivopts{display:flex;flex-wrap:wrap;gap:7px}
 .ivopt{font:inherit;font-size:13px;padding:7px 13px;border-radius:999px;border:1px solid var(--neutral-200);
   background:#fff;color:var(--ink);cursor:pointer;transition:.12s;text-align:left}
 .ivopt:hover{border-color:var(--accent);background:#fff}
 .ivopt .ivdef{display:none}
 /* A ruled-out option is SHOWN, not hidden: one that silently vanishes reads as a bug,
    while one greyed out with its reason teaches the vocabulary. */
 .ivopt .ivrec{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);font-weight:700;margin-left:5px}
 .ivopt.out{opacity:.5;cursor:not-allowed;text-decoration:line-through;text-decoration-thickness:1px}
 .ivopt.out:hover{border-color:var(--neutral-200)}
 .ivwhy{font-size:12.5px;color:var(--muted);margin-top:9px;line-height:1.5}
 .ivnote{font-size:12.5px;color:var(--muted);margin-top:8px;font-style:italic}
 /* The scenario, once confirmed: the last chance to catch a wrong answer before an
    authoritative-looking reply is generated from it. */
 .ivscen{align-self:flex-start;width:100%;font-size:13px;background:#fff;border:1px solid var(--line);
   border-radius:10px;padding:10px 13px;box-shadow:var(--shadow)}
 .ivscen .ivstep{margin-bottom:7px}
 .ivscen table{border-collapse:collapse;width:100%}
 .ivscen td{padding:2px 0;vertical-align:top}
 .ivscen td.k{color:var(--muted);width:150px;padding-right:10px;white-space:nowrap}
 .ivscen .ivsecs{margin-top:8px;padding-top:8px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
 .ivscen .ivsecs .kk{color:var(--accent);cursor:pointer;font-weight:600}
 .ivedit{font:inherit;font-size:12px;padding:3px 10px;border-radius:7px;border:1px solid var(--neutral-200);
   background:#fff;color:var(--ink);cursor:pointer;margin-top:9px}
 .ivresume{font:inherit;font-size:12.5px;font-weight:600;padding:7px 13px;border-radius:999px;border:1px solid var(--accent);
   background:#fff;color:var(--accent);cursor:pointer;margin-top:4px;align-self:flex-start}
 #composer{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line);background:var(--surface)}
 #composer input{flex:1;font:inherit;padding:10px 12px;border-radius:9px;border:1px solid var(--neutral-200)}
 #composer input:focus{outline:none;border-color:var(--accent)}
 #composer button{font:inherit;font-weight:600;padding:10px 16px;border-radius:9px;border:0;background:var(--accent);color:#fff;cursor:pointer}
 #left{overflow:auto;border-right:1px solid var(--line);background:var(--surface)}
 #status{padding:12px 18px;color:var(--muted);font-size:12.5px}
 .dym{padding:10px 18px 0;font-size:12.5px;color:var(--muted)}
 .dym a{color:var(--accent);font-weight:600;font-style:italic;text-decoration:underline}
 .jchips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:8px 18px 0}
 .jclabel{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;font-weight:700}
 .jchip{display:inline-flex;align-items:center;gap:6px;font-size:12px;background:#fff;border:1px solid var(--neutral-200);border-radius:999px;padding:3px 10px;cursor:pointer;color:var(--ink);transition:.12s;white-space:nowrap}
 .jchip:hover{border-color:var(--accent)}
 .jchip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
 .jcdot{width:8px;height:8px;border-radius:50%;flex:none}
 .jchip.active .jcdot{background:#fff!important}
 .rgrp{padding:4px 14px}
 .rgrp .rhead{font-size:12px;font-weight:700;letter-spacing:.04em;color:var(--ink);margin:16px 0 4px;display:flex;align-items:center;gap:8px}
 .rgrp .rhead .badge{width:10px;height:10px;border-radius:50%}
 .jgrp h3{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:10px 0 4px}
 #rows{padding:6px 14px 16px}
 .r{padding:12px 14px;margin:0 0 10px;border:1px solid var(--line);border-radius:var(--radius);background:#fff;cursor:pointer;transition:.15s;box-shadow:var(--shadow)}
 .r.ralert{border-left:3px solid #ca8a04;background:#fffdf5}
 .atag{font-size:11px}
 .r:hover{border-color:var(--neutral-400)}
 .r.sel{border-color:var(--accent)}
 .r .top{display:flex;justify-content:space-between;gap:8px}
 .r .k{font-weight:650;font-size:13.5px;color:var(--ink)}
 .r .sc{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
 .jmatch{color:#ca8a04}
 .r .p{color:var(--muted);font-size:11px;margin:3px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .r .sn{font-size:12.5px;color:var(--neutral-700);margin-top:4px;max-height:34px;overflow:hidden}
 /* ---- Full-document links (guidance/alert -> section): on-demand slide-in drawer ---- */
 .docwrap{display:block}
 .docview{width:100%;min-width:0}
 .doclinks{position:fixed;top:0;right:0;z-index:90;width:340px;max-width:88vw;height:100vh;overflow:auto;
   background:var(--neutral-50);border-left:1px solid var(--line);box-shadow:-8px 0 32px rgba(0,0,0,.18);
   padding:12px 12px 28px;transform:translateX(100%);transition:transform .18s ease}
 .doclinks.open{transform:translateX(0)}
 .lkhead{display:flex;align-items:center;justify-content:space-between;padding:2px 2px 8px;
   border-bottom:1px solid var(--line);margin-bottom:6px;position:sticky;top:0;background:var(--neutral-50);z-index:1}
 .drawx{border:0;background:none;font-size:22px;line-height:1;color:var(--muted);cursor:pointer}
 .linkstoggle{margin-left:auto;flex:none;border:1px solid var(--line);background:#fff;border-radius:8px;
   padding:3px 10px;font-size:12px;font-weight:600;color:var(--ink);cursor:pointer}
 .linkstoggle:hover{border-color:var(--accent);color:var(--accent-d)}
 .lksec{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:12px 4px 5px}
 .lkrow{display:flex;gap:7px;align-items:flex-start;padding:7px 8px;margin-bottom:5px;border:1px solid var(--line);border-radius:8px;background:#fff;cursor:pointer;transition:.12s}
 .lkrow:hover{border-color:var(--accent);box-shadow:var(--shadow)}
 .lkkey{flex:none;font-size:10.5px;font-weight:700;color:#fff;background:var(--accent);border-radius:5px;padding:2px 6px;font-variant-numeric:tabular-nums;cursor:pointer}
 .lkkey:hover{background:var(--accent-d)}
 .lkrow .b{min-width:0;flex:1}
 .lkrow .t{font-size:12px;line-height:1.35;color:var(--ink);overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
 .lkscore{flex:none;font-size:10px;font-weight:700;padding:2px 5px;border-radius:5px;color:#fff;font-variant-numeric:tabular-nums}
 /* ---- modal popup (full guidance/alert text) ---- */
 .modal{position:fixed;inset:0;z-index:100;background:rgba(12,13,13,.45);display:flex;align-items:center;justify-content:center;padding:24px}
 .modal-card{position:relative;background:#fff;border-radius:14px;max-width:680px;width:100%;max-height:82vh;overflow:auto;padding:24px 28px;box-shadow:0 12px 48px rgba(0,0,0,.3)}
 .modal-x{position:absolute;top:10px;right:14px;border:0;background:none;font-size:24px;line-height:1;color:var(--muted);cursor:pointer}
 .modal .mhead{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
 .modal .mtitle{font-size:15px;font-weight:650;color:var(--ink);margin-bottom:10px;line-height:1.5}
 .modal .mbody{font-size:14px;line-height:1.65;color:var(--neutral-800);white-space:pre-wrap}
 .modal #mgoto{margin-top:16px}
 #detail{overflow:auto;padding:30px 40px;background:var(--bg)}
 .crumb{color:var(--muted);font-size:12.5px;margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
 #detail h2{font-size:22px;margin:.1em 0 .6em;letter-spacing:-.01em;color:var(--ink)}
 .rtag{display:inline-block;white-space:nowrap;font-size:11px;color:#fff;padding:3px 10px;border-radius:999px;vertical-align:middle;margin-left:10px;font-weight:600}
 .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px 26px}
 #detail p{line-height:1.65;font-size:14.5px;color:var(--neutral-800)}
 #detail ul{margin:.3em 0 .6em 1.1em} #detail li{font-size:14.5px;line-height:1.55;color:var(--neutral-800)}
 .qd{background:var(--neutral-50);border-left:3px solid var(--neutral-900);padding:9px 12px;margin:.5em 0;border-radius:0 8px 8px 0}
 .rn{background:#fffaf0;border-left:3px solid var(--warn);padding:9px 12px;margin:.5em 0;font-style:italic;color:#7a5b1e}
 pre{background:var(--neutral-50);border:1px solid var(--neutral-100);border-radius:8px;padding:10px;overflow:auto;font-size:12px;white-space:pre-wrap;color:var(--neutral-800)}
 .sub{margin-top:14px;border-left:2px solid var(--neutral-100);padding-left:16px}
 .sub h4{margin:14px 0 4px;font-size:14px;color:var(--ink);font-weight:650}
 .sub .lvl3{font-size:13px;color:var(--neutral-700)}
 .fnref{color:var(--neutral-600);font-weight:700;font-size:.72em;vertical-align:super}
 .fn{margin-top:16px;border-top:1px solid var(--neutral-100);padding-top:10px;font-size:12px;color:var(--muted)}
 .links{margin-top:22px;display:grid;grid-template-columns:1fr 1fr;gap:18px}
 .links h3{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 7px}
 .chip{display:inline-block;font-size:12px;background:#fff;border:1px solid var(--neutral-200);border-radius:8px;padding:4px 9px;margin:2px 4px 0 0;cursor:pointer;color:var(--ink);transition:.12s}
 .chip:hover{border-color:var(--accent)}
 .ans{border:1px solid var(--line);border-radius:10px;padding:9px 11px;margin:7px 0;font-size:12.5px;background:#fff;cursor:pointer;transition:.12s}
 .ans[data-g]:hover,.alert[data-a]:hover{border-color:var(--accent);box-shadow:var(--shadow)}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
 .alert{border-left:3px solid var(--danger);padding:8px 11px;margin:7px 0;font-size:12.5px;background:#fff;border-radius:0 8px 8px 0;cursor:pointer;transition:.12s}
 .muted{color:var(--muted)}
 /* ---- right-pane detail tabs + full-document viewer ---- */
 .dtabs{position:sticky;top:-30px;z-index:5;display:flex;align-items:center;gap:6px;margin:-30px -40px 18px;padding:14px 40px;background:var(--bg);border-bottom:1px solid var(--neutral-200)}
 .dtabs .dtab-j{margin-left:auto;font-size:12px;color:var(--muted);font-weight:600}
 .relstat{display:none;align-items:center;gap:6px;font-size:11.5px;font-weight:600;margin-left:12px;padding:3px 9px;border-radius:999px}
 .relstat.busy{display:inline-flex;color:var(--warn);background:#fff7e6}
 .relstat.done{display:inline-flex;color:var(--neutral-600);background:var(--neutral-100)}
 .spin{width:11px;height:11px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .6s linear infinite}
 @keyframes spin{to{transform:rotate(360deg)}}
 /* relevance evidence: elements the ranking cross-encoder scored highest for the query */
 .relhit{background:#fff8db;border-radius:6px;box-shadow:inset 3px 0 0 #f0c000;padding-left:11px!important}
 .relhit.reltop{background:#fff3b0;box-shadow:inset 3px 0 0 var(--accent)}
 .relwhy{display:block;font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--warn);margin-bottom:4px}
 .docview .docsec{scroll-margin-top:64px;padding:2px 0}
 .docview .dsec{margin:16px 0 4px;font-weight:650;color:var(--ink);letter-spacing:-.005em}
 .docview .dsec.lvl0,.docview .dsec.lvl1{font-size:17px;margin-top:24px}
 .docview .dsec.lvl2{font-size:15px}
 .docview .dsec.lvl3,.docview .dsec.lvl4{font-size:13.5px;color:var(--neutral-700)}
 .docview .docsec.hl{background:#fffbe6;border-left:3px solid var(--accent);border-radius:0 8px 8px 0;padding:4px 0 4px 14px;margin-left:-17px}
 /* ---- Full-document master-detail: hierarchy tree (left) + chunk detail (right) ---- */
 main.docmode{grid-template-columns:minmax(180px,230px) 1fr}            /* push the results panel narrow */
 main.docmode #detail{padding:0;overflow:hidden;display:flex;flex-direction:column}
 main.docmode .dtabs{position:static;margin:0;padding:10px 18px;flex:0 0 auto}
 main.docmode #detbody{flex:1;min-height:0}
 .docmaster{display:grid;grid-template-columns:minmax(240px,340px) 1fr;height:100%;min-height:0}
 .doctree{overflow:auto;border-right:1px solid var(--neutral-200);padding:0 4px 24px;background:var(--surface)}
 .doctreehd{position:sticky;top:0;z-index:1;background:var(--surface);font-size:11px;font-weight:700;color:var(--muted);
   text-transform:uppercase;letter-spacing:.06em;padding:12px 10px 10px;border-bottom:1px solid var(--neutral-100)}
 .tnode{font-size:12.5px;line-height:1.35;padding:5px 8px;margin:1px 2px;cursor:pointer;border-radius:7px;
   color:var(--neutral-800);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .tnode:hover{background:var(--neutral-100)}
 .doctree .tnode.sel{background:var(--neutral-900);color:#fff}
 .tnode.sel .tk{color:var(--neutral-300)}
 .tnode .tk{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:11px;margin-right:6px}
 .tnode.l1{font-weight:700;color:var(--ink);margin-top:6px}
 .tnode.l2{font-weight:600}
 .docdet{overflow:auto;padding:22px 40px 56px;background:var(--bg)}
 .ddhead{border-bottom:1px solid var(--neutral-200);padding-bottom:14px;margin-bottom:18px}
 .ddhead h2{font-size:21px;margin:.15em 0 .1em;letter-spacing:-.01em;color:var(--ink)}
 .ddhead .dk{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:15px;font-weight:600;margin-right:4px}
 .crumb .cb{white-space:nowrap;cursor:pointer} .crumb .cb:hover{color:var(--ink);text-decoration:underline}
 .crumb .csep{color:var(--neutral-300);margin:0 3px}
 .badges{display:flex;gap:8px;margin-top:10px}
 .lvlbadge,.typebadge{font-size:11px;padding:3px 10px;border-radius:999px;font-weight:600}
 .lvlbadge{background:var(--neutral-100);color:var(--neutral-700)}
 .typebadge{background:var(--neutral-900);color:#fff}
 .ddbody p{line-height:1.65;font-size:14.5px;color:var(--neutral-800)}
 .ddbody ul{margin:.3em 0 .6em 1.1em} .ddbody li{font-size:14.5px;line-height:1.55;color:var(--neutral-800)}
 .docdet .fn{margin-top:22px;border-top:1px solid var(--neutral-200);padding-top:12px;font-size:12px;color:var(--muted)}
 .docdet .fn>b{display:block;margin-bottom:6px;color:var(--neutral-700)} .docdet .fn div{margin:3px 0;line-height:1.5}
 .dref{color:var(--accent);font-weight:600;cursor:pointer;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:2px}
 .dref:hover{background:var(--neutral-100);border-radius:4px}
 .hero{padding:56px 40px;color:var(--muted);text-align:center;font-size:14.5px;line-height:1.7}
 .hero b{color:var(--ink)}
 main.gallerymode,main.extractmode,main.promptmode{grid-template-columns:1fr}
 main.gallerymode #left,main.gallerymode #chat,
 main.extractmode #left,main.extractmode #chat,
 main.promptmode #left,main.promptmode #chat{display:none}
 main.gallerymode #detail,main.extractmode #detail,main.promptmode #detail{padding:0;background:var(--bg)}
 /* ---- Extraction monitor ---- */
 .exwrap{padding:18px 22px;max-width:1180px}
 .exhead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
 .exhead h2{font-size:17px;margin:0}
 .exsel{font:inherit;font-size:13px;padding:4px 8px;border-radius:8px;border:1px solid var(--neutral-200)}
 .exstate{font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px;text-transform:uppercase;letter-spacing:.04em}
 .exs-running{background:#e7f2ff;color:#1257a5}
 .exs-complete{background:#e8f6ec;color:#1b6b34}
 .exs-idle,.exs-stale,.exs-stopping{background:#fdf0e3;color:#8a4b12}
 .exs-interrupted{background:#f0edf8;color:#4b3b85}
 .exs-stalled{background:#fbeceb;color:#a3302a}
 .exquiet{display:block;font-size:11px;font-weight:600;color:var(--muted);text-transform:none;letter-spacing:0;margin-top:3px}
 .exquiet.bad{color:#a3302a}
 .exquietchip{font-size:12px;font-weight:700;color:#a3302a;background:#fbeceb;padding:3px 9px;border-radius:99px}
 .exbar{height:10px;border-radius:99px;background:var(--neutral-200);overflow:hidden;margin:10px 0 4px}
 .exbar>i{display:block;height:100%;background:var(--accent);transition:width .4s}
 .extiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
 .extile{background:#fff;border:1px solid var(--neutral-200);border-radius:10px;padding:10px 12px}
 .extile b{display:block;font-size:20px;line-height:1.25}
 .extile span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
 .extab{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--neutral-200);border-radius:10px;overflow:hidden;font-size:13px}
 .extab th{text-align:left;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;padding:8px 10px;border-bottom:1px solid var(--neutral-200)}
 .extab td{padding:8px 10px;border-bottom:1px solid var(--neutral-100,#f1f1f1);vertical-align:top}
 .extab tr:last-child td{border-bottom:0}
 .exdim{color:var(--muted);font-size:12px}
 /* Version picker: every extraction run is a version; the list is cheap metadata only. */
 .exvers{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}
 .exver{font:inherit;text-align:left;padding:7px 11px;border-radius:9px;border:1px solid var(--neutral-200);background:#fff;cursor:pointer;line-height:1.3}
 .exver.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
 .exver b{display:block;font-size:13px}
 .exver span{font-size:11px;color:var(--muted)}
 .exsub{font-size:12px;color:var(--muted);margin:16px 0 6px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
 /* ---- Jobs: the per-document view of a run. Same columns the local pipeline monitor
    (scripts/pipeline_monitor.py) shows off disk, built here from the workers' ledgers so a
    deployed run can be debugged without SSH-ing to a pod. ---- */
 .exjobs{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--neutral-200);
   border-radius:10px;overflow:hidden;font-size:13px;font-variant-numeric:tabular-nums}
 .exjobs th{text-align:left;font-size:10.5px;color:var(--muted);text-transform:uppercase;
   letter-spacing:.05em;padding:9px 10px;border-bottom:1px solid var(--neutral-200);font-weight:600}
 .exjobs td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
 .exjobs tr:last-child td{border-bottom:0}
 .exjobs tbody tr{cursor:pointer}
 .exjobs tbody tr:hover{background:var(--neutral-50)}
 .exjobs tr.exopen{background:var(--neutral-50)}
 .exdocid{font-weight:600;font-size:13.5px}
 .exjur{color:var(--muted);font-size:11.5px;margin-top:1px}
 /* The six-dot stepper: the whole pipeline at a glance, so a stalled document is visible in the
    shape of the row rather than by reading the stage column. */
 /* ---- the per-document trace: the pipeline drawn as a graph -------------------------
    Replaces a strip of six equal dots. Six positions on a line cannot say the three things
    a row is actually asked: did the pre-flight repair the outline (so Stage 1 ran twice),
    did the document enter the fallback chain, and which tier's tree is the one on disk.
    Those are BRANCHES, so the cell draws branches. Same node states and the same state
    colours at both scales, so the row and the full graph below it read as one picture. */
 .exstep{display:flex;align-items:center}
 /* No width here on purpose: the rail's width comes off EXG_STEPS and is set inline on
    the element. A fixed width in the stylesheet WINS over the SVG's width attribute, so
    it silently scaled a longer rail down to fit instead of showing it at full size. */
 .extrace{display:block;height:26px;flex:none}
 /*TRACE_CSS*/
 /* The node inspector: one node's own facts, opened by clicking it. */
 .exgins{margin-top:14px;border-top:1px solid var(--neutral-200);padding:13px 2px 0}
 .exginsh{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:8px}
 .exginsh b{font-size:13.5px}
 .exginsh .exginsw{font-size:11.5px;color:var(--muted)}
 .exgpill{font-size:9.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
   padding:2px 8px;border-radius:99px;color:var(--muted);background:var(--surface);
   border:1px solid var(--neutral-300)}
 .exgpill.p-done,.exgpill.p-warn{color:var(--neutral-700);border-color:var(--neutral-400)}
 .exgpill.p-run{color:#2a78d6;border-color:#2a78d6}
 .exgpill.p-disc{color:#b45309;border-color:#b45309}
 .exgpill.p-crash{color:#c1342d;border-color:#c1342d}
 .exgkv{display:grid;grid-template-columns:158px 1fr;gap:0 14px;font-size:12.5px}
 .exgkv>dt{color:var(--muted);padding:4px 0;border-top:1px solid var(--neutral-100)}
 .exgkv>dd{margin:0;padding:4px 0;border-top:1px solid var(--neutral-100);
   font-variant-numeric:tabular-nums;word-break:break-word}
 .exgkv>dt:first-of-type,.exgkv>dt:first-of-type + dd{border-top:0}
 .exginsnote{font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.45}
 .exghint{font-size:11.5px;color:var(--muted);margin-top:9px}
 .expass{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:99px;
   font-size:11.5px;font-weight:600;white-space:nowrap;border:1px solid transparent}
 .expass i{width:6px;height:6px;border-radius:50%;background:currentColor;font-style:normal}
 .expass.p-first{background:var(--neutral-50);color:var(--muted);border-color:var(--neutral-200)}
 .expass.p-rescue{background:#fdf3e2;color:#8a5a12}
 .expass.p-mineru{background:#fbeceb;color:#a3302a}
 .expass.p-clone{background:#eef2f7;color:#4a5a6a}
 .expass.p-summary{background:#e8f1fc;color:#1b5fa3}
 .expass.p-live i{animation:expulse 1.6s ease-in-out infinite}
 @keyframes expulse{0%,100%{opacity:1}50%{opacity:.3}}
 .expassnote{font-size:10.5px;color:var(--muted);margin-top:2px}
 /* The rescue is a property of the DOCUMENT, not a tier that ran, so it reads as a
    note under the pass badge rather than as a second badge competing with it. */
 .exresc{color:var(--ink);font-weight:600}
 .extook{font-weight:600}
 .extooksub{font-size:10.5px;color:var(--muted);margin-top:2px}
 .extooklive{color:#2a78d6;font-weight:600}
 .extookfb{color:#8a5a12}
 /* The funnel: how many documents reached each stage. The one view that says WHERE a run is
    losing documents, rather than only how many it has lost. */
 .exfun{display:flex;flex-direction:column;gap:7px;background:#fff;border:1px solid var(--neutral-200);
   border-radius:10px;padding:13px 15px}
 .exfunrow{display:grid;grid-template-columns:165px 1fr 44px;align-items:center;gap:10px}
 .exfunname{font-size:12px;color:var(--muted)}
 .exfunopt{opacity:.6}
 .exopttag{margin-left:6px;font-size:9.5px;text-transform:uppercase;letter-spacing:.05em;border:1px solid var(--line);border-radius:3px;padding:0 3px;vertical-align:1px}
 .exfuntrack{height:16px;background:var(--neutral-50);border:1px solid var(--neutral-100);
   border-radius:5px;overflow:hidden}
 .exfunfill{height:100%;width:0;background:#2a78d6;transition:width .4s ease}
 .exfunfill.done{background:#1b8a44}
 .exfuncount{font-size:12px;text-align:right;font-weight:600}
 .exlegend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin-top:9px}
 .exlegend span{display:inline-flex;align-items:center;gap:5px}
 .exlegend i{width:8px;height:8px;border-radius:50%;font-style:normal}
 .exjobsbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 8px}
 .exfbtn{font:inherit;font-size:12px;padding:3px 11px;border-radius:999px;border:1px solid var(--neutral-200);
   background:#fff;color:var(--muted);cursor:pointer}
 .exfbtn:hover{border-color:var(--accent)}
 .exfbtn.active{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
 .exsearch{font:inherit;font-size:12px;padding:4px 10px;border-radius:999px;
   border:1px solid var(--neutral-200);min-width:190px}
 .exsearch:focus{outline:none;border-color:var(--accent)}
 /* The drill-down. One scorecard, read only when a row is opened — the per-step timings, the
    fallback chain including the tiers that were tried and thrown away, and the dimension that
    decided the gate. */
 .exdet td{background:var(--neutral-50);padding:0}
 .exdetin{padding:13px 15px;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
 .exdetsec h4{margin:0 0 7px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
 .exbars{display:flex;flex-direction:column;gap:4px}
 .exbarrow{display:grid;grid-template-columns:112px 1fr 52px;align-items:center;gap:8px;font-size:12px}
 .exbarrow>span:first-child{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .exbartrack{height:11px;background:var(--neutral-100);border-radius:4px;overflow:hidden}
 .exbarfill{height:100%;background:#2a78d6;border-radius:4px}
 .exbarrow.slow .exbarfill{background:#c1342d}
 .exbarrow b{text-align:right;font-weight:600}
 .exchain{font-size:12px;line-height:1.65;margin:0;padding-left:16px}
 .exchain li.adopted{font-weight:600;color:#1b6b34}
 .exkv{font-size:12px;line-height:1.75}
 .exkv b{font-weight:600}
 /* The trigger, on the row: which bar the first pass missed, and by how much. */
 .extrig{display:inline-block;margin-top:3px;font-size:10.5px;padding:1px 6px;border-radius:99px;
   background:var(--neutral-50);border:1px solid var(--neutral-200);color:var(--muted);
   white-space:nowrap;max-width:150px;overflow:hidden;text-overflow:ellipsis}
 .exhard{font-size:10.5px;color:#c1342d;font-weight:600;margin-top:2px}
 .exhardbox{margin:8px 0 0;padding:8px 10px;border-radius:7px;font-size:12px;line-height:1.55;
   background:#fbeceb;border:1px solid #f0cfcd;color:#7f1d1d}
 .exhardbox.exfbnow{background:#fdf3e2;border-color:#e8d3a8;color:#7a4d0a}
 .excauses li{margin-bottom:3px}
 .exchain li.skipped{color:var(--muted)}
 /* Passes: one block per run of the pipeline. The visual separation is the point — a tier
    listed flat beside the stages reads as one of them. */
 .expassblk{margin:0 0 9px;padding:7px 9px;border-radius:7px;background:var(--neutral-50);
   border:1px solid var(--neutral-100)}
 .expassblk.composite{border-color:#e8d3a8;background:#fdfaf3}
 .expasshd{display:flex;align-items:baseline;gap:7px;font-size:12px;margin-bottom:2px}
 .expasshd b{font-size:12.5px}
 .expasssec{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums}
 .expasstag{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#8a5a12;
   background:#fdf3e2;border:1px solid #e8d3a8;border-radius:99px;padding:0 6px}
 .expassnote2{font-size:11px;line-height:1.45;margin-bottom:5px}
 .expassblk.running{border-color:#a9c7ea;background:#f5f9fe}
 .expasstag.live{color:#1257a5;background:#e7f2ff;border-color:#a9c7ea}
 /* A stage still counting must never read as a measured duration beside the finished ones. */
 .exbarrow.live .exbarfill{background:repeating-linear-gradient(45deg,#2a78d6,#2a78d6 5px,
   #6ba3e4 5px,#6ba3e4 10px)}
 .exbarrow i.exrun{font-style:normal;font-size:9.5px;text-transform:uppercase;letter-spacing:.04em;
   color:#1257a5;background:#e7f2ff;border-radius:99px;padding:0 5px;margin-left:4px}
 .exworse{color:#c1342d;font-weight:600}
 .exbetter{color:#1b6b34;font-weight:600}
 .exerr{font-family:ui-monospace,Menlo,monospace;font-size:11px;line-height:1.5;white-space:pre-wrap;
   background:#fbeceb;border:1px solid #f0cfcd;color:#7f1d1d;border-radius:7px;padding:9px 11px;
   max-height:200px;overflow:auto;margin:0}
 .galwrap{padding:22px 28px;max-width:1180px;font-variant-numeric:tabular-nums}
 .galhead{font-size:18px;font-weight:700;color:var(--ink)}
 .galsub{color:var(--muted);font-size:13px;margin:3px 0 4px}
 .galfilter{display:flex;gap:6px;margin:10px 0 6px}
 .galfbtn{font-size:12px;padding:4px 12px;border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--muted);cursor:pointer}
 .galfbtn:hover{border-color:var(--accent)}
 .galfbtn.active{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
 .galstats{margin:10px 0 4px;padding:9px 14px;background:#fff7e6;border:1px solid #e0b050;border-radius:8px;font-size:13px;color:#7a4d0a}
 .galstats .galstat-total{font-weight:700;font-size:15px;color:#b45309;margin-right:2px}
 .galstats b{color:#8a5a12}
 .galprod{font-size:15px;font-weight:600;color:var(--ink);margin:26px 0 8px;border-bottom:1px solid var(--line);padding-bottom:6px}
 .galgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;align-items:start}
 .galcard{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:13px 15px;cursor:pointer;transition:border-color .15s,box-shadow .15s}
 .galcard:hover{border-color:var(--accent);box-shadow:var(--shadow)}
 .galtitle{font-weight:600;font-size:14px;color:var(--ink)}
 .galtitle .did{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted);margin-left:6px;font-weight:400}
 .galbadges{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
 .gbadge{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
 .gbadge.g{color:#15803d;border-color:#8fd6a8} .gbadge.a{color:#b45309;border-color:#e6c34a} .gbadge.r{color:#c1342d;border-color:#e39aa0}
 .gbadge.snap{color:#b45309;border-color:#e0b050;background:#fff7e6;font-weight:600}
 .gallink{margin-top:10px;font-size:12px}.gallink a{color:var(--accent);text-decoration:none;cursor:pointer}
 .galview{display:flex;flex-direction:column;height:100%}
 .galviewbar{display:flex;gap:8px;align-self:flex-start;margin:10px 14px 8px}
 .galback{font:inherit;font-size:13px;font-weight:600;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--surface);color:var(--muted);cursor:pointer}
 .galback:hover{border-color:var(--accent);color:var(--ink)}
 .galframe{flex:1;width:100%;border:none;min-height:0;background:#fff}
 /* A run gallery is NOT the published one: say so unmissably, because every number on
    the screen means something different (nothing here is live to search yet). */
 .galrun{margin:0 0 14px;padding:10px 12px;border-radius:8px;font-size:13px;line-height:1.5;
   background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12}
 .galrun b{font-weight:600}
 .galrun.galrunbad{background:#fef2f2;border-color:#fecaca;color:#7f1d1d}
 .galrunerr{font-family:ui-monospace,Menlo,monospace;font-size:12px}
 .galrun .galfbtn{margin-left:8px;vertical-align:baseline}
 .exver.exverlink{border-style:dashed}
 /* Promotion panel (Extraction tab). Reuses the ex* vocabulary on purpose: it is the same
    kind of screen — a long job, watched by polling a durable summary — and a second visual
    language for it would only make the two harder to read together. */
 .pmwrap{margin-top:22px;border-top:1px solid var(--neutral-200);padding-top:14px}
 .pmstages{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
 .pmstage{flex:1 1 120px;min-width:120px;background:#fff;border:1px solid var(--neutral-200);
   border-radius:8px;padding:7px 9px}
 .pmstage b{display:block;font-size:12px;font-weight:600}
 .pmstage span{display:block;font-size:11px;color:var(--muted)}
 .pmstage.s-complete{border-color:#a7d8b6;background:#f2fbf5}
 .pmstage.s-running{border-color:#9cc6f0;background:#f2f8ff}
 .pmstage.s-failed{border-color:#f0aaa5;background:#fdf3f2}
 .pmstage.s-stale{border-color:#f0c79a;background:#fdf7f0}
 /* Phase 2 is DECLARED, not hidden. "In the gallery" is not "searchable", and a screen that
    dropped the remaining stages would let a staged promotion read as a finished one. */
 .pmstage.s-not_implemented{opacity:.5;border-style:dashed;background:transparent}
 .pmstage.s-not_implemented b:after{content:' · phase 2';font-weight:400;color:var(--muted)}
 .pmnote{font-size:12px;color:var(--muted);margin:6px 0}
 .pmwarn{margin:10px 0;padding:9px 11px;border-radius:8px;font-size:12.5px;line-height:1.5;
   background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12}
 .pmbtn{font:inherit;font-size:12.5px;font-weight:600;padding:5px 11px;border-radius:8px;
   border:1px solid var(--neutral-200);background:#fff;cursor:pointer}
 .pmbtn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
 .pmbtn:disabled{opacity:.5;cursor:default}
 .pmmodal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:flex;align-items:center;
   justify-content:center;z-index:60}
 .pmcard{background:#fff;border-radius:12px;padding:18px 20px;max-width:620px;width:92%;
   max-height:82vh;overflow:auto;font-size:13px;line-height:1.55}
 .pmcard h3{margin:0 0 10px;font-size:15px}
 .pmcard table{width:100%;border-collapse:collapse;margin:8px 0}
 .pmcard td{padding:3px 6px;border-bottom:1px solid var(--neutral-100)}
 .pmcard td.n{text-align:right;font-variant-numeric:tabular-nums}
 .pmacts{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}

 /* ---- master prompts per product (AOSNG-3442) ---- */
 .prwrap{max-width:1200px;margin:0 auto;padding:22px}
 .prcols{display:grid;grid-template-columns:300px 1fr;gap:18px;margin-top:16px;align-items:start}
 .prlist{display:flex;flex-direction:column;gap:4px;max-height:70vh;overflow:auto}
 .pritem{display:flex;justify-content:space-between;align-items:center;gap:8px;text-align:left;
   padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);
   cursor:pointer;font-size:13px;color:inherit}
 .pritem:hover{border-color:#94a3b8}
 .pritem.active{border-color:#2563eb;background:#eff6ff}
 .pritem b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .prbadge{flex:none;font-size:11px;padding:1px 7px;border-radius:999px;background:#f1f5f9;
   color:#475569;border:1px solid #e2e8f0}
 /* an override is the exception and must be visible at a glance — it changes what the
    assistant asserts, so "which products are not on the default" is the first question */
 .prbadge.prcustom{background:#fff7ed;color:#9a3412;border-color:#fed7aa}
 /* The default is not one of the products, it is what they all fall back to, so it sits above
    them and set apart rather than reading as a fourth product */
 .prlist .prdefrow{margin-bottom:8px;background:var(--neutral-50)}
 .prlist .prdefrow.active{background:#eff6ff}
 .prlist .prdefrow b{font-weight:600}
 .prdefbox{margin-top:12px;min-height:340px;max-height:60vh}
 .prpane{min-width:0}
 .prtitle{font-size:15px;font-weight:600;display:flex;align-items:center;gap:10px}
 .prtext{width:100%;min-height:340px;margin-top:12px;padding:12px;border:1px solid var(--line);
   border-radius:8px;background:var(--card);color:inherit;
   font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.55;
   resize:vertical}
 .prtext:focus{outline:2px solid #93c5fd;outline-offset:1px}
 /* Edit / Preview / Diff. The textarea stays mounted and hidden behind the derived views, so a
    caret in a 4k-character prompt survives a look at the diff */
 /* Which of the two answers is being edited. Given more weight than the Edit/Preview/Diff
    switch below it, because editing the wrong mode's prompt is the mistake with a cost */
 .prmodes{display:flex;align-items:center;gap:8px;margin:12px 0 4px}
 .prmbtn{padding:7px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);
   color:#475569;font-size:13px;cursor:pointer}
 .prmbtn.active{background:var(--ink);color:#fff;border-color:var(--ink);font-weight:600}
 .prbadge.prother{background:var(--neutral-50);color:#64748b;font-style:italic}
 .prviews{display:flex;gap:6px;margin-top:12px}
 /* EasyMDE ships its own light theme with Bootstrap-ish borders and a proportional font.
    These overrides are only what is needed to make it read as part of this page — and to keep
    the buffer MONOSPACED, because column position matters in a prompt that contains indented
    rules and table examples. Loaded after the CDN stylesheet, so these win. */
 #preditwrap .EasyMDEContainer{margin-top:12px}
 #preditwrap .editor-toolbar{border-color:var(--line);border-top-left-radius:8px;
   border-top-right-radius:8px;background:var(--neutral-50);padding:6px 8px}
 #preditwrap .editor-toolbar button{width:auto;min-width:30px;padding:0 9px;color:#475569;
   font-size:12.5px;font-weight:600;border-radius:6px}
 #preditwrap .editor-toolbar button:hover{background:var(--neutral-100);border-color:transparent}
 #preditwrap .editor-toolbar button.active{background:var(--neutral-100);color:inherit}
 #preditwrap .editor-toolbar i.separator{border-left-color:var(--line);
   border-right-color:transparent;margin:0 3px}
 #preditwrap .CodeMirror{border-color:var(--line);border-bottom-left-radius:8px;
   border-bottom-right-radius:8px;background:var(--card);color:inherit;padding:6px 4px;
   font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.55}
 #preditwrap .CodeMirror-cursor{border-left-color:var(--ink)}
 #preditwrap .cm-s-easymde .cm-header{font-size:100%;color:#0f766e}
 #preditwrap .cm-s-easymde .cm-comment{background:transparent;color:#9a3412}
 .prvbtn{padding:5px 11px;border:1px solid var(--line);border-radius:7px;background:var(--card);
   color:#475569;font-size:12.5px;cursor:pointer}
 .prvbtn.active{background:var(--neutral-100);color:inherit;font-weight:600;border-color:#cbd5e1}
 .prrender{margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:8px;
   background:var(--card);min-height:340px;max-height:60vh;overflow:auto}
 .prmd{font-size:13.5px;line-height:1.6}
 .prmd h3,.prmd h4{margin:14px 0 6px;font-size:14px}
 .prmd code{background:var(--neutral-100);padding:1px 4px;border-radius:4px;font-size:12px}
 .prmd ul,.prmd ol{margin:6px 0 6px 20px}
 /* A diff of a prompt is read to answer "what did the SME change", so additions and removals
    have to be distinguishable without relying on colour alone — hence the +/- kept in the text */
 .prdiff{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.55;
   margin-top:8px;white-space:pre-wrap;word-break:break-word}
 .prdiff>div{padding:1px 6px;border-left:3px solid transparent}
 .prdadd{background:#f0fdf4;border-left-color:#22c55e!important}
 .prddel{background:#fef2f2;border-left-color:#ef4444!important;color:#7f1d1d}
 .prdsame{color:#64748b}
 .prdskip{color:#94a3b8;font-style:italic;background:var(--neutral-100);margin:4px 0}
 .prbar{display:flex;align-items:center;gap:10px;margin-top:10px}
 .prbar .galfbtn[disabled]{opacity:.45;cursor:default}
 .prdefault{margin-top:16px;font-size:13px}
 .prdefault summary{cursor:pointer;color:#475569}
 .prpre{margin-top:8px;padding:12px;border:1px solid var(--line);border-radius:8px;
   background:var(--card);white-space:pre-wrap;word-break:break-word;
   font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.5;
   max-height:420px;overflow:auto}
 @media (max-width:900px){ .prcols{grid-template-columns:1fr} .prlist{max-height:none} }

 /* ---- extraction scorecard table (the gallery's landing view) ---- */
 .sctwrap{padding:22px 28px;font-variant-numeric:tabular-nums}
 .scbar{display:flex;gap:20px;flex-wrap:wrap;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:11px 16px;margin:12px 0 10px}
 .scbar .kv{display:flex;flex-direction:column}
 .scbar .kv span{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.4px}
 .scbar .kv b{font-size:17px}
 .sctable{width:100%;border-collapse:collapse;font-size:13px}
 .sctable th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);
   font-weight:600;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
 .sctable td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
 .sctable tr.scrow{cursor:pointer}
 .sctable tr.scrow:hover>td{background:var(--bg)}
 .sctable tr.scj>td:first-child{padding-left:26px}
 .sctable tr.scd>td{background:var(--bg);font-size:12.5px}
 .sctable tr.scd>td:first-child{padding-left:52px}
 .scdid{color:var(--ink);font-weight:600}
 .scdocid{font-weight:400;font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 6px;margin-left:5px}
 .galdocname{font-weight:400;font-size:12px;color:var(--muted);margin-top:2px}
 .scresc{font-size:10px;font-weight:600;letter-spacing:.3px;text-transform:uppercase;margin-left:6px;
   padding:1px 6px;border-radius:999px;background:#eef2ff;color:#3b53a8;border:1px solid #c7d2fe;cursor:help}
 .scdbadges{display:flex;flex-wrap:wrap;align-items:baseline;margin-top:3px;font-size:11.5px;color:var(--muted)}
 .scdims{display:inline-flex;margin-right:16px}
 /* fixed slot width: the same five dimensions on every row, aligned as a column */
 .scdim{display:inline-block;min-width:42px;white-space:nowrap}
 .scdim i{font-style:normal;opacity:.65;margin-right:3px}
 .scdim b{font-weight:650}
 .scfacts{white-space:normal}
 .scfacts b{font-weight:600}
 .sccaret{display:inline-block;width:12px;color:var(--muted)}
 sup.fnref{cursor:pointer;color:#1a4f8a;font-weight:600}
 sup.fnref:hover{text-decoration:underline}
 .fnitem{margin:3px 0;line-height:1.5}
 a.fnback{color:#1a4f8a;text-decoration:none;font-weight:600}
 a.fnback:hover{text-decoration:underline}
 a.fnlink{color:#1a4f8a;word-break:break-all}
 .fnhit{background:#fff3bf;border-radius:3px;transition:background .4s}
 .mdtwrap{overflow-x:auto;margin:8px 0}
 table.mdt{border-collapse:collapse;font-size:13px;line-height:1.45;width:100%}
 table.mdt th,table.mdt td{border:1px solid #e3e6ea;padding:6px 9px;vertical-align:top;text-align:left}
 table.mdt th{background:#f6f7f9;font-weight:600}
 table.mdt tr:nth-child(even) td{background:#fbfcfd}
 .scfp{font-size:11px;font-weight:600;color:#b45309;margin-left:2px}
 .scfp.scfpbad{color:#c1342d}
 .scpill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:.3px}
 .scp-pass{background:#e7f5ec;color:#15803d} .scp-review{background:#fdf3e0;color:#b45309}
 .scp-fail{background:#fbeaec;color:#c1342d} .scp-ungated{background:var(--bg);color:var(--muted);border:1px solid var(--line)}
 .sccnt{color:var(--muted)} .scscore{font-weight:650}
 .scname{font-weight:650;color:var(--ink)} .scmuted{color:var(--muted)}
 .scacts{white-space:nowrap;text-align:right}
 .scacts a{color:var(--accent);text-decoration:none;cursor:pointer;font-size:12px;margin-left:10px}
 .scacts a:hover{text-decoration:underline}
 /* ---- Scorecard detail (inline in the table, and the full scorecard page) ---- */
 .sc-gate{border:1px solid var(--line);border-left:4px solid;border-radius:10px;padding:12px 14px;margin-bottom:14px}
 .sc-gate-verdict{font-size:17px;font-weight:700}
 .sc-gate-sub{font-size:13px;color:var(--muted);margin-top:2px}
 .sc-gate-note{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.5}
 .sc-dim-grid{display:flex;flex-direction:column;gap:10px;margin-bottom:14px}
 .sc-dim{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
 .sc-dim.sc-advisory{opacity:.85}
 .sc-dim-head{display:flex;align-items:center;gap:8px;font-size:13px}
 .sc-dim-name{font-weight:650;color:var(--ink)}
 .sc-dim-score{margin-left:auto;font-weight:700;font-size:14px}
 .sc-tag{font-size:10px;padding:1px 6px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
 .sc-tag-gate{color:var(--ink);border-color:var(--accent)}
 .sc-bar{height:6px;border-radius:999px;background:var(--line);margin-top:6px;overflow:hidden}
 .sc-bar div{height:100%}
 .sc-what{font-size:12px;color:var(--muted);margin-top:6px}
 .sc-stats{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
 .sc-stat{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted);display:flex;gap:4px}
 .sc-stat.bad{color:#c1342d;border-color:#e39aa0} .sc-stat.warn{color:#b45309;border-color:#e6c34a}
 .sc-advice,.sc-from,.sc-formula,.sc-caveat{font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.5}
 .sc-caveat{color:#b45309}
 .sc-panel{border-top:1px solid var(--line);padding-top:12px;margin-top:4px}
 .sc-panel h4{font-size:13px;margin:0 0 8px}
 .sc-find{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:6px}
 .sc-find-silent .sc-find-kind.sc-find-silent{background:#3a1414;color:#ff8a8a}
 .sc-find-top{display:flex;gap:6px;align-items:center;font-size:11px;color:var(--muted)}
 .sc-find-kind{border:1px solid var(--line);border-radius:999px;padding:1px 6px}
 .sc-find-title{font-size:12.5px;font-weight:600;margin-top:3px;color:var(--ink)}
 .sc-find-detail{font-size:12px;color:var(--muted);margin-top:2px}
 .sc-find-none{font-size:12.5px;color:#15803d}
 .sc-heat{display:flex;flex-wrap:wrap;gap:2px}
 .sc-heat-cell{width:9px;height:9px;border-radius:2px}
 .sc-h-ok{background:#8fd6a8} .sc-h-flagged{background:#e6c34a} .sc-h-silent{background:#e39aa0} .sc-h-unvalidatable{background:var(--line)}
 .sc-strip{display:flex;flex-wrap:wrap;gap:6px}
 .sc-chip{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
</style></head>
<body>
<header>
 <div class="brand"><h1>aosphere</h1><span class="sub">Core Index</span>
   <span class="tabs"><button class="tab active" data-mode="search">Search</button><button class="tab aitab" data-mode="ai" style="display:none">AI Mode</button><button class="tab" data-mode="gallery">Doc Gallery</button><button class="tab" data-mode="extract">Extraction</button><button class="tab prompttab" data-mode="prompts" style="display:none">Prompts</button></span>
   <span class="authbox" id="authbox"></span>
 </div>
 <div class="searchbar" id="searchbar">
   <input id="q" placeholder="Ask across all regions, e.g. how long do we have to report a data breach?" autofocus>
   <select id="minscore" title="Relevance cutoff">
     <option value="5">High relevance</option>
     <option value="2" selected>Balanced</option>
     <option value="0">Broad</option>
     <option value="-100">Show all</option>
   </select>
   <button id="go">Search</button>
 </div>
 <div class="regionbar"><span class="lbl">Filter</span>
   <div class="rdrop">
     <button id="pbtn" class="rdrop-btn">Product ▾</button>
     <div id="ppanel" class="rdrop-panel" style="width:260px">
       <div id="ptree"></div>
     </div>
   </div>
   <div class="rdrop">
     <button id="rbtn" class="rdrop-btn">All jurisdictions ▾</button>
     <div id="rpanel" class="rdrop-panel">
       <input id="rsearch" placeholder="Find a jurisdiction…" autocomplete="off">
       <div class="rdrop-bar"><button class="lnk" id="rall">Select all</button><button class="lnk" id="rnone">Clear</button></div>
       <div id="rtree"></div>
     </div>
   </div>
   <span class="aionly"><select id="model" title="Model"></select><span class="cost" id="cost"></span></span>
 </div>
</header>
<main>
 <div id="left"><div class="hero">One query searches <b>every jurisdiction</b>.<br>Results group by region → jurisdiction → clause — ready for the AI to compare.</div></div>
 <section id="chat">
   <div id="msgs"><div class="hero">Ask a question — the agent searches the index, explains the answer, and cites the clauses it read.<br>Follow-up questions keep context.</div></div>
   <div id="composer"><input id="chatq" placeholder="Ask a question…">
     <label id="explainwrap" title="Off: the answer and the clause it rests on, nothing more. On: the reasoning — operative wording, conditions, thresholds and exceptions."><input type="checkbox" id="explain"> Explain</label>
     <button id="chatsend">Send</button></div>
 </section>
 <div id="colsplit" title="Drag to resize"></div>
 <div id="detail"><div class="hero">Select a result to read the exact clause,<br>its sub-clauses, footnotes, cross-references, guidance and alerts.</div></div>
</main>
<div id="modal" class="modal" style="display:none"><div class="modal-card"><button class="modal-x" id="modalx">×</button><div id="modal-body"></div></div></div>
<script>
const RC={Europe:"#1f4e79",Americas:"#2e7d32",["Asia-Pacific"]:"#b45309","Middle East":"#7b3fa0",Africa:"#b02a37",Other:"#7B888A"};
// Per-product badge colour (shown on EVERY result so the product is always explicit).
const PCOL={"Data Privacy":"#334155","Shareholding Disclosure":"#0f766e"};
const prodCol=p=>PCOL[p]||"#4D5454";
const PDELIM=" — ";  // split a product-qualified id -> [product, jurisdiction name]
const splitProd=id=>{const i=(id||"").indexOf(PDELIM);return i>=0?[id.slice(0,i),id.slice(i+PDELIM.length)]:["Data Privacy",id||""];};
const RAT={Green:"#15803d",Amber:"#ca8a04",Red:"#c1342d",Yellow:"#ca8a04",White:"#7B888A"};
const esc=s=>String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
// Attribute-safe: quotes MUST be escaped when a value lands in an HTML attribute —
// one raw quote ends the attribute early and takes the element's behaviour with it.
const escA=s=>esc(s).replace(/"/g,"&quot;").replace(/'/g,"&#39;");
const fmt=t=>esc(t).replace(/\[\^(\d+)\]/g,(_,n)=>`<sup class="fnref" data-fn="${n}" title="Jump to footnote ${n}">[${n}]</sup>`)
  // Markdown the extracted bodies carry that this renderer would otherwise show as
  // literal punctuation: **emphasis** (74 elements in the DP/SD build) and a heading
  // that belongs to the clause body rather than to the tree (86). Applied AFTER esc(),
  // so the input is already inert and this can only ever add markup of our own.
  .replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')
  // Stage 4's text pass marks an underlined defined term with <u>...</u> in the source
  // document. esc() above has already turned that into the inert &lt;u&gt;...&lt;/u&gt;,
  // so this only ever restores exactly that one shape back into real markup -- it cannot
  // be used to smuggle in an arbitrary tag the way trusting raw <u> straight from esc()
  // would.
  .replace(/&lt;u&gt;([^\n]+?)&lt;\/u&gt;/g,'<u>$1</u>')
  .replace(/^#{1,6}\s+(.+)$/gm,'<strong>$1</strong>');
// A markdown pipe table -> a real <table>. The extractor emits tables as pipe rows and
// the reader showed them in a <pre>: correct but unreadable, since one row of a
// state-by-state comparison is a 300-character line and the separator ('| --- | --- |')
// is rendered as content. Cells are esc()aped through fmt(), so nothing here trusts the
// document. Anything that does not look like a table falls back to the old <pre>.
const _SEP_ROW=/^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$/;
const splitRow=r=>{ let c=r.trim(); if(c.startsWith('|'))c=c.slice(1);
  if(c.endsWith('|'))c=c.slice(0,-1); return c.split('|').map(x=>x.trim()); };
function pipeTable(text){
  const rows=text.split('\n').map(r=>r.trim()).filter(r=>r.includes('|'));
  if(rows.length<2) return null;
  const body=rows.filter(r=>!_SEP_ROW.test(r));
  if(!body.length) return null;
  const cells=body.map(splitRow);
  // Markdown says "separator on line 2 => line 1 is the header", but these tables are
  // often emitted headerless with the separator after the first DATA row — and shading a
  // 300-character clause of legal text as a header is worse than having no header. So
  // require the first row to also LOOK like labels: short cells.
  const headed=rows.length>1&&_SEP_ROW.test(rows[1])
    &&cells[0].every(c=>c.length<=60);
  const width=Math.max(...cells.map(c=>c.length));
  if(width<2) return null;
  const cell=(v,tag)=>`<${tag}>${richText(v)}</${tag}>`;
  let h='<table class="mdt">';
  cells.forEach((row,i)=>{
    const tag=(headed&&i===0)?'th':'td';
    h+='<tr>'+row.map(v=>cell(v,tag)).join('')
      +'<td></td>'.repeat(Math.max(0,width-row.length))+'</tr>';
  });
  return h+'</table>';
}
const enc=encodeURIComponent;
// A citation is usually a URL ("[^1] http://kenyalaw.org/..."), and a URL a reader cannot
// click is a URL they have to retype. Runs over ESCAPED html and only inside text
// segments (onText), so it can neither trust the document nor break existing markup.
// Trailing punctuation stays with the sentence: "(https://x/a.pdf)" must not swallow ")".
const URLRE=/\bhttps?:\/\/[^\s<>"']+/g;
const linkUrls=html=>onText(html,t=>t.replace(URLRE,u=>{
  const tail=(u.match(/[)\].,;:'"]+$/)||[""])[0];
  const href=u.slice(0,u.length-tail.length);
  if(!/^https?:\/\/\S{4,}/.test(href)) return u;
  return `<a href="${href}" target="_blank" rel="noopener noreferrer" class="fnlink">${href}</a>${tail}`;
}));
// One footnote block, ids scoped to the clause so two clauses on screen cannot collide.
// The superscript in the prose is what a reader clicks; fnBlock is where it lands.
const fnBlock=(footnotes,key,has)=>!footnotes||!footnotes.length?"":
  `<div class="fn"><b>Footnotes (${footnotes.length})</b>`
  +footnotes.map(f=>`<div class="fnitem" id="fn-${enc(key||'')}-${enc(f.id)}">`
    +`<a class="fnback" href="#" data-fnback="${esc(f.id)}">[${esc(f.id)}]</a> `
    +`${linkUrls(richText(f.text,has))}</div>`).join("")
  +`</div>`;
// ---- shared text rendering (clause view + full-document view) ----
const KEYRE=/[A-K]\d+(?:\.\d+)*(?:\([a-z0-9]+\))*/g;
// apply fn only to text segments (outside HTML tags) so we never break markup
const onText=(html,fn)=>html.replace(/(<[^>]+>)|([^<]+)/g,(m,tag,txt)=>tag!==undefined?tag:fn(txt));
const linkRefs=(html,has)=>{ if(!has||!has.size) return html;
 return onText(html,t=>t.replace(KEYRE,k=>has.has(k)?`<a class="dref" data-k="${k}">${k}</a>`:k)); };
// fmt (escape + footnote sups), then optionally linkify in-doc clause refs
const richText=(text,has)=>linkRefs(fmt(text),has);
// rel = {byIndex:Map(elementIndex->score), top:index, maxLabel:"+2.3"} for the
// clause the model ranked — highlights the elements that justify the ranking.
const REL_MIN=0;  // cross-encoder scores separate around 0
function renderEls(els,opts){
 opts=opts||{}; const rel=opts.rel, has=opts.has;
 let h="",ul=false;
 (els||[]).forEach((e,idx)=>{
   const sc=rel&&rel.byIndex.has(idx)?rel.byIndex.get(idx):null;
   const top=rel&&idx===rel.top;
   const hot=sc!=null&&(sc>REL_MIN||top);
   const cls=hot?(" relhit"+(top?" reltop":"")):"";
   const why=top?`<span class="relwhy">most relevant to your query · ${rel.maxLabel}</span>`:"";
   if(e.kind==="bullet"){if(!ul){h+="<ul>";ul=true;}h+=`<li class="${cls.trim()}">${richText(e.text,has)}${why}</li>`;return;}
   if(ul){h+="</ul>";ul=false;}
   if(e.kind==="question")h+=`<div class="qd${cls}">${why}<b>Q.</b> ${richText(e.text,has)}</div>`;
   else if(e.kind==="readernote")h+=`<div class="rn${cls}">${richText(e.text,has)}</div>`;
   else if(e.kind==="table"){const tb=pipeTable(e.text);
     h+=tb?`<div class="mdtwrap${cls}">${why}${tb}</div>`
          // richText, not esc: it escapes first and THEN draws footnote markers as
          // superscripts, so a table that falls back to <pre> still shows "[158]" rather
          // than the raw "[^158]" a reader has no use for.
          :`<pre class="${cls.trim()}">${why}${richText(e.text,has)}</pre>`;}
   else h+=`<p class="${cls.trim()}">${why}${richText(e.text,has)}</p>`;
 });
 if(ul)h+="</ul>";return h;
}

// ---- auth: Keycloak OIDC, same realm/flow as aosphere-ai-playground ----
let USER=null, _um=null;
function applyScoreScale(scale){
 // The reranker's score scale drives the relevance-cutoff dropdown. Cross-encoder
 // ('logit') scores separate around 0 — keep the 5/2/0 presets. LLM/cohere ('unit')
 // scores are 0..1, so recalibrate; otherwise the default (2) filters out EVERY result.
 if(scale!=="unit") return;
 const sel=document.getElementById("minscore");
 if(!sel) return;
 // Unit scores: the LLM reranker bands its own "relevant" set above 0.5, so Balanced=0.5
 // shows exactly the clauses the model judged relevant (adaptive per query) — not a fixed
 // rank cut. High tightens within that set; Broad reaches into the tangential band.
 sel.innerHTML =
   '<option value="0.75">High relevance</option>'+
   '<option value="0.5" selected>Balanced</option>'+
   '<option value="0.25">Broad</option>'+
   '<option value="-100">Show all</option>';
}
// Server feature flags, fetched once with the auth config. The promote controls are
// gated on CFG.promotion so the UI hides rather than offering a button that 404s.
let CFG={};
async function initAuth(){
 let cfg; try{ cfg=await (await fetch("/api/config")).json(); }catch(e){ return true; }
 CFG=cfg||{};                                  // feature flags the UI gates on (promotion, …)
 applyScoreScale(cfg.score_scale);             // calibrate relevance cutoffs to the reranker scale
 if(!cfg.auth_enabled) return true;            // local / offline: no login required
 const {UserManager, WebStorageStateStore} = window.oidc;
 const redirect = window.location.origin + window.location.pathname;
 _um = new UserManager({
   authority: `${cfg.keycloak_url}/realms/${cfg.realm}`,
   client_id: cfg.client_id,
   redirect_uri: redirect, post_logout_redirect_uri: redirect,
   response_type: "code", scope: "openid profile email",
   userStore: new WebStorageStateStore({store: window.localStorage}),
   automaticSilentRenew: true,
 });
 const params = new URLSearchParams(window.location.search);
 if(params.get("code") || params.get("error")){            // returning from Keycloak
   try{ await _um.signinRedirectCallback(); }catch(e){}
   history.replaceState({}, "", window.location.pathname);  // strip code/state; q is in sessionStorage
 }
 let u = await _um.getUser();
 if(u && u.expired){ try{ u = await _um.signinSilent(); }catch(e){ u=null; } }
 if(!u){                                                    // not logged in -> redirect to Keycloak
   const q=(document.getElementById("q")||{}).value;
   if(q && q.trim()) sessionStorage.setItem("aci_q", q.trim());
   try{ await _um.signinRedirect(); }catch(e){ showGate(); return false; }
   return new Promise(()=>{});                              // halt: page is navigating away
 }
 USER=u; renderAuthbox();
 return true;
}
function renderAuthbox(){
 const b=document.getElementById("authbox"); if(!b||!USER) return;
 const name=(USER.profile&&(USER.profile.preferred_username||USER.profile.email))||"";
 b.innerHTML=`<span class="who">${esc(name)}</span><button class="authbtn" id="logout">Logout</button>`;
 document.getElementById("logout").onclick=()=>{ try{_um.signoutRedirect();}catch(e){} };
}
function showGate(){
 document.querySelector("main").innerHTML=`<div class="gate">Sign in to search the Core Index.<br><button class="authbtn" onclick="location.reload()">Sign in</button></div>`;
}
function showAdminGate(){
 document.getElementById("searchbar").style.display="none";
 document.querySelector(".regionbar").style.display="none";
 document.querySelector(".tabs").style.display="none";
 document.querySelector("main").innerHTML=`<div class="gate"><b>Restricted.</b><br>Access to the Core Index hasn't been granted for this account.<br>Contact an aosphere administrator to be added to the access list.<br><button class="authbtn" id="ggout" style="margin-top:16px">Sign out</button></div>`;
 const o=document.getElementById("ggout"); if(o&&_um) o.onclick=()=>{try{_um.signoutRedirect();}catch(e){}};
}
async function authToken(){
 if(!_um) return null;
 let u=await _um.getUser();
 if(u && u.expired){ try{ u=await _um.signinSilent(); }catch(e){} }
 return u ? u.access_token : null;
}
// ---- one abort scope per screen ----
// Leaving a screen must CANCEL what that screen started, not merely ignore it. Two of these
// screens fan out badly: the extraction monitor HEADs the review artefacts of every recent
// document one at a time, and a run gallery indexes ~1000 S3 objects on its first look. Either
// can still be running seconds after the user has clicked away, and its continuation then
// repaints #detail — which is why the Doc Gallery would flip back to extraction results by
// itself. An entry-time mode check cannot fix that: the check passes, then the work lands late.
//
// So every screen change opens a new scope and aborts the previous one. authFetch attaches the
// current scope's signal unless the caller passed its own, so this cannot be forgotten at a
// call site. Renderers additionally check viewStale() before writing, because a response that
// arrived just before the abort is already parsed and would otherwise still paint.
let _viewScope=null, _viewSeq=0;
// Three things must NOT be cancelled by a screen change, and they say so at the call site by
// passing an explicit signal:
//   * loadRegions / loadAi — app-level state (the filter tree, the model list), fetched once at
//     startup. Clicking a tab while they are in flight would leave the app permanently without
//     its filters, which is worse than a wasted request.
//   * sendChat — a streaming answer that renders into the CHAT pane, not #detail. People switch
//     to Search to look something up while the agent is still thinking, and the answer has to be
//     there when they come back. Cancelling it would throw away work already paid for.
const NO_CANCEL = {signal: null};

function newViewScope(){
 if(_viewScope) _viewScope.abort();       // cancel the screen we are leaving
 _viewScope=new AbortController(); _viewSeq++;
 return _viewSeq;
}
function viewStale(token){ return token!==_viewSeq; }
function isAbort(e){ return e && (e.name==="AbortError" || e.code===20); }

async function authFetch(url, opts){
 opts=opts||{};
 if(opts.signal===undefined && _viewScope) opts.signal=_viewScope.signal;
 const t=await authToken();
 if(t) opts.headers=Object.assign({}, opts.headers||{}, {Authorization:"Bearer "+t});
 return fetch(url, opts);
}
// Product is single-select (exactly one, always) and drives which jurisdictions the
// second dropdown offers — a jurisdiction only appears there if JP lists it under the
// selected product. RTREE stays the full continent -> [jurisdiction name] tree shared
// across products; prodTree()/prodJurisdictions() narrow it to selectedProduct on demand.
let PRODUCTS_ALL=[],RTREE={},JP={},selectedProduct="",enabledP=new Set(),enabledJ=new Set(),expanded=new Set(),RSEARCH="",results=[];
let _autoJur=null;  // jurisdiction names auto-applied from the current query's chips (multi-select)
const sameSet=(a,b)=>a.size===b.size&&[...a].every(x=>b.has(x));
let pendingAi=null;  // a search query to carry into AI Mode ONCE (armed by a search, consumed on first AI open)

function prodTree(product){
 if(!product) return RTREE;   // no product picked yet -> every jurisdiction, unfiltered
 const out={};
 for(const [rg,items] of Object.entries(RTREE)){
   const f=items.filter(n=>(JP[n]||[]).includes(product));
   if(f.length) out[rg]=f;
 }
 return out;
}
const prodJurisdictions=product=>Object.values(prodTree(product)).flat();

async function loadRegions(){
 const d=await (await authFetch("/api/regions", NO_CANCEL)).json();
 PRODUCTS_ALL=d.products||[]; RTREE=d.regions||{}; JP=d.jurisdiction_products||{};
 selectedProduct="";                     // default: no product picked -> unrestricted
 enabledP=new Set();
 enabledJ=new Set();                     // default: no jurisdiction picked -> unrestricted
 expanded=new Set(Object.keys(RTREE));   // continents expanded by default
 renderProducts(); updatePbtn();
 renderTree(); updateRbtn();
 const ppanel=document.getElementById("ppanel"), panel=document.getElementById("rpanel");
 document.getElementById("pbtn").onclick=()=>ppanel.classList.toggle("open");
 document.getElementById("rbtn").onclick=()=>panel.classList.toggle("open");
 // Keep panel clicks from reaching the outside-close handler (re-render detaches
 // the clicked checkbox mid-event, which otherwise reads as an outside click).
 ppanel.addEventListener("click",e=>e.stopPropagation());
 panel.addEventListener("click",e=>e.stopPropagation());
 document.addEventListener("click",e=>{
   document.querySelectorAll(".rdrop").forEach(d=>{ if(!d.contains(e.target)) d.querySelector(".rdrop-panel")?.classList.remove("open"); });
 });
 document.getElementById("rsearch").addEventListener("input",e=>{RSEARCH=e.target.value.toLowerCase();renderTree();});
 document.getElementById("rall").onclick=()=>{enabledJ=new Set(prodJurisdictions(selectedProduct));renderTree();updateRbtn();};
 document.getElementById("rnone").onclick=()=>{enabledJ.clear();renderTree();updateRbtn();};
}
function renderProducts(){
 document.getElementById("ptree").innerHTML=PRODUCTS_ALL.map(p=>
   `<label class="rrow prod"><input type="radio" name="prodsel" class="prodradio" data-p="${esc(p)}" ${selectedProduct===p?"checked":""}>${esc(p)}</label>`
 ).join("");
 document.querySelectorAll(".prodradio").forEach(r=>r.onchange=()=>{
   selectedProduct=r.dataset.p; enabledP=new Set([selectedProduct]);
   enabledJ=new Set();   // jurisdictions are scoped to the product -> a fresh pick starts empty
   document.getElementById("ppanel").classList.remove("open");
   updatePbtn(); renderTree(); updateRbtn();
 });
}
function updatePbtn(){
 document.getElementById("pbtn").textContent=`${selectedProduct||"Product"} ▾`;
}
function updateRbtn(){
 const nj=enabledJ.size, mj=prodJurisdictions(selectedProduct).length;
 const jL=nj===0?"All jurisdictions":nj===mj?"All jurisdictions":nj+"/"+mj+" jurisdictions";
 document.getElementById("rbtn").textContent=`${jL} ▾`;
}
function renderTree(){
 const f=RSEARCH, tree=document.getElementById("rtree");
 const scoped=prodTree(selectedProduct);  // continent -> jurisdictions offering selectedProduct
 const contHtml=Object.keys(scoped).map(rg=>{
   const items=scoped[rg];
   const mitems=f?items.filter(n=>rg.toLowerCase().includes(f)||n.toLowerCase().includes(f)):items;
   if(f&&mitems.length===0) return "";
   const sel=items.filter(n=>enabledJ.has(n)).length, rOpen=f?true:expanded.has(rg);
   const jr=mitems.map(n=>`<label class="jrow"><input type="checkbox" class="jcheck" data-j="${esc(n)}" ${enabledJ.has(n)?"checked":""}>${esc(n)}</label>`).join("");
   return `<div class="rnode" style="margin-left:0">
     <div class="rrow reg"><input type="checkbox" class="rcheck" data-k="${esc(rg)}" ${sel===items.length?"checked":""}>
       <span class="caret" data-k="${esc(rg)}">${rOpen?"▾":"▸"}</span>
       <span class="badge" style="background:${RC[rg]||'#888'}"></span>${esc(rg)} <span class="muted">${sel}/${items.length}</span></div>
     <div class="jlist" style="display:${rOpen?'block':'none'}">${jr}</div></div>`;
 }).join("");
 tree.innerHTML=contHtml.trim()||'<div class="muted" style="padding:6px">No match.</div>';
 tree.querySelectorAll(".rcheck").forEach(cb=>{const items=scoped[cb.dataset.k]||[];const sel=items.filter(n=>enabledJ.has(n)).length;cb.indeterminate=sel>0&&sel<items.length;
   cb.onchange=()=>{const on=cb.checked;items.forEach(n=>on?enabledJ.add(n):enabledJ.delete(n));renderTree();updateRbtn();};});
 tree.querySelectorAll(".jcheck").forEach(cb=>cb.onchange=()=>{cb.checked?enabledJ.add(cb.dataset.j):enabledJ.delete(cb.dataset.j);renderTree();updateRbtn();});
 tree.querySelectorAll(".caret[data-k]").forEach(c=>c.onclick=()=>{const k=c.dataset.k;expanded.has(k)?expanded.delete(k):expanded.add(k);renderTree();});
}

const SEARCH_K=80, PAGE=40;
let shown=0, io=null;
async function run(fresh=true){
 const q=document.getElementById("q").value.trim();if(!q)return;
 pendingAi=q;  // arm: switching to AI Mode after this search carries this query (once)
 // Fresh query: if the active jurisdiction filter is our own auto-scope from the previous
 // query's chips, drop it so THIS query detects + scopes cleanly (a manual filter is left).
 if(fresh){ if(_autoJur&&sameSet(enabledJ,_autoJur)) enabledJ=new Set(); _autoJur=null; }
 const left=document.getElementById("left");
 const ms=document.getElementById("minscore").value;
 // POST, for the same reason AI Mode does: a partial jurisdiction filter is a CSV of up to
 // 12KB, and in a URL the WAF rejects the whole request with its own 403 past ~2KB.
 const body={q:q, k:SEARCH_K, min_score:parseFloat(ms)||0};   // ||0: an empty/NaN input is 0
 if(enabledP.size&&enabledP.size<PRODUCTS_ALL.length) body.products=[...enabledP].join(",");
 if(enabledJ.size) body.jurisdictions=[...enabledJ].join(",");
 left.innerHTML=`<div id="status">searching…</div>`;
 // Leaving the Search screen cancels the search. This function had no catch at all, because
 // before there was an abort signal the fetch could not be interrupted — now it can.
 const tok=_viewSeq;
 let r, data;
 try{
   r=await authFetch("/api/search",{method:"POST",
     headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
   if(viewStale(tok)) return;
   data=await r.json();
 }catch(e){
   if(isAbort(e)) return;
   left.innerHTML=`<div id="status">Search failed. Try again.</div>`; return;
 }
 if(viewStale(tok)) return;
 results=data.results;
 // Multi-select: on a fresh query, add every jurisdiction it names to the filter by default
 // (only when no manual filter is active) and re-run once, scoped to that set.
 if(fresh){
   const det=(data.jurisdictions||[]).map(j=>j.name).filter(n=>prodJurisdictions(selectedProduct).includes(n));
   if(det.length && enabledJ.size===0){
     enabledJ=new Set(det); _autoJur=new Set(det); renderTree(); updateRbtn();
     return run(false);
   }
 }
 // Suggest-only "did you mean": retrieval already ran on the raw query; this just
 // surfaces a likely typo. Clicking re-runs the search with the corrected query.
 const dym=data.did_you_mean?`<div class="dym">Did you mean <a href="#" id="dymlink">${esc(data.did_you_mean)}</a>?</div>`:"";
 const chips=jchipsHtml(data.jurisdictions);
 if(!results.length){left.innerHTML=`${dym}${chips}<div id="status">No matches above the relevance cutoff. Try “Broad” or “Show all”.</div>`;wireDym(data.did_you_mean);wireJchips();return;}
 left.innerHTML=`${dym}${chips}<div id="status">${results.length} relevant matches${results.length>=SEARCH_K?'+':''}</div><div id="rows"></div><div id="more"></div>`;
 wireDym(data.did_you_mean);wireJchips();
 shown=0; appendRows();
 const first=document.querySelector("#rows .r"); if(first) select(0, first);
 if(io) io.disconnect();
 io=new IntersectionObserver(es=>{ if(es[0].isIntersecting) appendRows(); }, {root:left});
 io.observe(document.getElementById("more"));
}
function wireDym(sugg){
 const a=document.getElementById("dymlink"); if(!a||!sugg) return;
 a.onclick=e=>{e.preventDefault();document.getElementById("q").value=sugg;run();};
}
// Jurisdictions the query names (from /api/search .jurisdictions) -> clickable chips
// that scope the search to that jurisdiction (click the active chip again to clear).
function jchipsHtml(js){
 if(!js||!js.length) return "";
 const on=j=>enabledJ.has(j.name);  // in the active multi-select
 const chip=j=>`<button class="jchip${on(j)?' active':''}" data-j="${esc(j.name)}" title="${on(j)?'Remove '+esc(j.name)+' from filter':'Add '+esc(j.name)+' to filter'}"><span class="jcdot" style="background:${RC[j.region]||'#888'}"></span>${esc(j.name)}</button>`;
 return `<div class="jchips"><span class="jclabel">In your query</span>${js.map(chip).join("")}</div>`;
}
function wireJchips(){
 document.querySelectorAll(".jchip").forEach(b=>b.onclick=()=>{
   const j=b.dataset.j;
   if(enabledJ.has(j)) enabledJ.delete(j); else enabledJ.add(j);   // multi-select toggle; empty -> unrestricted
   _autoJur=new Set(enabledJ);   // keep chip ownership so the next fresh query resets cleanly
   renderTree(); updateRbtn(); run(false);
 });
}
function appendRows(){
 const rows=document.getElementById("rows"); if(!rows) return;
 const slice=results.slice(shown, shown+PAGE);
 const pTag=x=>x.product?`<span class="ptag" title="product" style="background:${prodCol(x.product)}">${esc(x.product)}</span> `:'';
 const jname=x=>esc(x.jurisdiction_name||x.jurisdiction);
 rows.insertAdjacentHTML("beforeend", slice.map((x,i)=>{const idx=shown+i;
   if(x.kind==='alert'){const a=x.alert||{}; const col=RAT[a.impact]||'#7B888A';
     const att=a.has_attachment?' <span class="atag" title="has attachment">📎</span>':'';
     return `<div class="r ralert" data-i="${idx}">
       <div class="top"><span class="k"><span class="badge" style="background:${col}"></span> 🔔 ${pTag(x)}${jname(x)} · ${esc(x.title)}${att}</span><span class="sc">${x.boosted?'<span class="jmatch">★</span> ':''}${x.score}</span></div>
       <div class="p">${esc(x.path)}</div><div class="sn">${esc(x.snippet)}</div></div>`;}
   return `<div class="r" data-i="${idx}">
     <div class="top"><span class="k"><span class="badge" style="background:${RC[x.region]||'#888'}"></span> ${pTag(x)}${jname(x)} · [${esc(x.key)}] ${esc(x.title)}</span><span class="sc">${x.boosted?'<span class="jmatch" title="matches a jurisdiction named in your query">★</span> ':''}${x.score}</span></div>
     <div class="p">${esc(x.path)}</div><div class="sn">${esc(x.snippet)}</div></div>`;}).join(""));
 shown+=slice.length;
 rows.querySelectorAll(".r:not([data-b])").forEach(d=>{d.setAttribute("data-b","1");d.onclick=()=>{
   const x=results[+d.dataset.i];
   if(x&&x.kind==='alert') alertResultModal(x); else select(+d.dataset.i,d);
 };});
 const more=document.getElementById("more"); if(more) more.textContent = shown<results.length ? `… ${shown} of ${results.length}` : "";
}

async function loadAi(){
 let d; try{ d=await (await authFetch("/api/ai-status", NO_CANCEL)).json(); }catch(e){ return; }
 if(!d.enabled||!(d.models||[]).length) return;
 const sel=document.getElementById("model");
 sel.innerHTML=d.models.map(m=>`<option value="${esc(m.id)}" data-in="${m.in}" data-out="${m.out}">${esc(m.label)} · $${m.in}/$${m.out} per 1M</option>`).join("");
 sel.value=d.default_model;
 const showCost=()=>{const o=sel.selectedOptions[0]; document.getElementById("cost").textContent=`in $${o.dataset.in} · out $${o.dataset.out} per 1M tokens`;};
 sel.onchange=()=>{ showCost(); resetChat(); };  // switching model starts a fresh chat
 showCost();
 document.querySelector(".aitab").style.display="inline-block";  // AI tab available
}

function mdToHtml(t){
 t=esc(t||"");
 t=t.replace(/^#{4,6}\s+(.+)$/gm,"<h4>$1</h4>").replace(/^###\s+(.+)$/gm,"<h4>$1</h4>")
    .replace(/^##\s+(.+)$/gm,"<h3>$1</h3>").replace(/^#\s+(.+)$/gm,"<h3>$1</h3>");
 t=t.replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/`([^`]+)`/g,"<code>$1</code>");
 t=t.replace(/\*(\S[^*\n]*?)\*/g,"<em>$1</em>");  // *italic* (opening * must hug text, so '* ' bullets are safe)
 // make [Jurisdiction · ClauseKey] citations clickable -> open that clause on the right.
 // Supports a LIST of keys ("… · A3.1.1; A3.1.2; A3.1.3") — each key becomes its own
 // link (a single-key regex would fail to match the whole bracket and drop the link).
 // keys: clause (A1.2(a)), or standalone ALERT:<id> / GUID:<id> rows (unmapped
 // alerts / unlinked guidance the agent cites) — each rendered as its own link.
 t=t.replace(/\[([^\]·]+?)\s*·\s*((?:[A-K]\d[\w.()\-]*|ALERT:\d+|GUID:[\w-]+)(?:\s*[;,]\s*(?:[A-K]\d[\w.()\-]*|ALERT:\d+|GUID:[\w-]+))*)\]/g,
   (m,j,keys)=>{
     const jj=j.trim();
     const links=keys.split(/\s*[;,]\s*/).filter(Boolean)
       .map(k=>`<a class="cite" data-j="${jj}" data-k="${k.trim()}">${k.trim()}</a>`).join("; ");
     return `[${jj} · ${links}]`;
   });
 const isRow=s=>/^\s*\|.*\|\s*$/.test(s);
 const isSep=s=>/\|/.test(s)&&/-/.test(s)&&/^[\s|:-]+$/.test(s.trim());
 const cells=s=>s.trim().replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
 const lines=t.split("\n"), out=[]; let ul=false, ol=false, bq=false;
 const close=()=>{ if(ul){out.push("</ul>");ul=false;} if(ol){out.push("</ol>");ol=false;} if(bq){out.push("</blockquote>");bq=false;} };
 for(let i=0;i<lines.length;i++){
   const ln=lines[i]; let m;
   if(/^\s*([-*_])\1{2,}\s*$/.test(ln)){ close(); out.push("<hr>"); }   // --- *** ___ rule
   else if(isRow(ln) && i+1<lines.length && isSep(lines[i+1])){  // GitHub-style table
     close();
     let h="<table><thead><tr>"+cells(ln).map(c=>"<th>"+c+"</th>").join("")+"</tr></thead><tbody>";
     i+=2;
     for(; i<lines.length && isRow(lines[i]) && !isSep(lines[i]); i++)
       h+="<tr>"+cells(lines[i]).map(c=>"<td>"+c+"</td>").join("")+"</tr>";
     i--;
     out.push(h+"</tbody></table>");
   }
   else if(m=ln.match(/^\s*&gt;\s?(.*)$/)){ if(!bq){close();out.push("<blockquote>");bq=true;} if(m[1].trim()) out.push("<p>"+m[1]+"</p>"); }
   else if(m=ln.match(/^\s*[-*•]\s+(.+)$/)){ if(!ul){close();out.push("<ul>");ul=true;} out.push("<li>"+m[1]+"</li>"); }
   else if(m=ln.match(/^\s*\d+[.)]\s+(.+)$/)){ if(!ol){close();out.push("<ol>");ol=true;} out.push("<li>"+m[1]+"</li>"); }
   else if(ln.trim()===""){ close(); }
   else if(/^<h[34]>/.test(ln)){ close(); out.push(ln); }
   else { close(); out.push("<p>"+ln+"</p>"); }
 }
 close(); return out.join("");
}

let SID="s"+Math.random().toString(36).slice(2);
function resetChat(){
 SID="s"+Math.random().toString(36).slice(2);  // new server-side session
 const m=document.getElementById("msgs");
 if(m) m.innerHTML='<div class="hero">Ask a question — the agent searches the index, explains the answer, and cites the clauses it read.<br>Follow-up questions keep context.</div>';
}

let secCache={};
async function renderSectionInto(det, jurisdiction, region, key){
 const sk=jurisdiction+"|"+key;
 let s=secCache[sk];
 if(!s){
   // An abort (the user changed screens mid-load) must not be cached as "no such section":
   // secCache is keyed for the whole session, so a cached abort hides a clause that exists.
   try{ s=await (await authFetch(`/api/section?jurisdiction=${enc(jurisdiction)}&key=${enc(key)}`)).json(); }
   catch(e){ if(isAbort(e)) return; s=null; }
   if(s && Array.isArray(s.elements)) secCache[sk]=s;  // don't cache error bodies
 }
 // A 404/error body lacks elements/guidance/alerts — show a message, don't crash.
 if(!s || !Array.isArray(s.elements)){
   det.innerHTML=`<div class="card"><div class="crumb">${esc(jurisdiction)}</div>`
     +`<h2>[${esc(key)}]</h2><p class="muted">Couldn't load this clause${s&&s.detail?` — ${esc(s.detail)}`:''}.</p></div>`;
   return;
 }
 const rel=(curRel&&curRel.key===key)?curRel:null;
 let body=renderEls(s.elements,{rel})||'<p class="muted">(no text of its own — see sub-clauses below)</p>';
 if(s.subtree&&s.subtree.length)
   body+=`<div class="sub">`+s.subtree.map(c=>`<h4 class="${c.level>=3?'lvl3':''}">[${esc(c.key)}] ${esc(c.title)}</h4>${renderEls(c.elements)}`).join("")+`</div>`;
 const chips=a=>a.length?a.map(c=>`<span class="chip" data-k="${esc(c.key)}">${esc(c.key)} ${esc((c.title||'').slice(0,22))}</span>`).join(""):'<span class="muted">none</span>';
 const guid=s.guidance.length?s.guidance.map((g,i)=>`<div class="ans" data-g="${i}" title="Click for full guidance"><span class="dot" style="background:${RAT[g.color]||'#888'}"></span><b>${esc(g.color)}</b> · ${esc(g.subject||'')}<br>${esc(g.question||'')}<br><span class="muted">${esc((g.answer||'').slice(0,300))}${(g.answer||'').length>300?'…':''}</span></div>`).join(""):'<span class="muted">none</span>';
 const alerts=s.alerts.length?s.alerts.map((a,i)=>`<div class="alert" data-a="${i}" title="Click for full alert"><b>${esc(a.title)}</b> <span class="muted">${esc(a.impact||'')} ${esc(a.date||'')}</span><br><span class="muted">${esc((a.summary||'').slice(0,260))}${(a.summary||'').length>260?'…':''}</span></div>`).join(""):'<span class="muted">none</span>';
 // The pane shows this clause AND its subtree inline, so the footnote block has to
 // cover both: a heading-only parent (B4.1 has one element and no footnotes) renders
 // children carrying 29 of them, and reading only s.footnotes left every one of those
 // superscripts pointing at nothing. Deduped by id — ids are document-wide, and a
 // parent and child can cite the same note.
 const fnAll=[], fnSeen=new Set();
 for(const src of [s].concat(s.subtree||[])) for(const f of (src.footnotes||[]))
   if(!fnSeen.has(String(f.id))){ fnSeen.add(String(f.id)); fnAll.push(f); }
 fnAll.sort((a,b)=>(parseInt(a.id,10)||0)-(parseInt(b.id,10)||0));
 const fn=fnBlock(fnAll,key);
 const col=RC[region]||"#262727";
 const [pp,jj]=splitProd(jurisdiction);
 det.innerHTML=`<div class="card"><div class="crumb">${esc(s.breadcrumb)}</div>
    <h2>[${esc(s.key)}] ${esc(s.title)}<span class="rtag" style="background:${prodCol(pp)}" title="product">${esc(pp)}</span><span class="rtag" style="background:${col}">${esc(jj)}</span></h2>`+
   body+fn+`<div class="links">
      <div><h3>References →</h3>${chips(s.cites_out)}</div>
      <div><h3>Referenced by ←</h3>${chips(s.cited_by)}</div>
      <div><h3>Guidance</h3>${guid}</div>
      <div><h3>Alerts</h3>${alerts}</div></div></div>`;
 det.querySelectorAll(".chip").forEach(c=>c.onclick=()=>openClause(jurisdiction, region, c.dataset.k));
 det.querySelectorAll(".ans[data-g]").forEach(el=>el.onclick=()=>guidanceModal(s.guidance[+el.dataset.g]));
 det.querySelectorAll(".alert[data-a]").forEach(el=>el.onclick=()=>alertModal(s.alerts[+el.dataset.a]));
 document.getElementById("detail").scrollTop=0;
}

// ---- right-pane detail: Clause | Full document tabs ----
let detTab="clause", curJ=null, curRegion="", curKey=null, curQuery="", docCache={};
let curRel=null, relCache={};
function openClause(jurisdiction, region, key, query){
 // Standalone rows aren't clauses — route to their own popup (covers agent
 // citations, source chips, and search-result clicks through one point).
 if(/^ALERT:/.test(key)) return openAlertById(jurisdiction, key);
 if(/^GUID:/.test(key))  return openGuidanceById(jurisdiction, key);
 curJ=jurisdiction; curRegion=region||""; curKey=key;
 if(query!==undefined) curQuery=query||"";
 renderDetail();
}
async function openAlertById(jur, key){
 const id=String(key).split(":").pop();
 let a; try{ a=await (await authFetch(`/api/alert?jurisdiction=${enc(jur)}&id=${enc(id)}`)).json(); }catch(e){ return; }
 if(!a || a.detail) return;  // 404 -> {detail:...}
 alertResultModal({jurisdiction:jur, title:a.title,
   alert:{impact:a.impact, date:a.date, summary:a.summary, attachment:a.attachment, mapped:a.mapped||[]}});
}
async function openGuidanceById(jur, key){
 const id=String(key).split(":").pop();
 let g; try{ g=await (await authFetch(`/api/guidance?jurisdiction=${enc(jur)}&id=${enc(id)}`)).json(); }catch(e){ return; }
 if(!g || g.detail) return;
 guidanceModal(g);
}
// Score the clause's elements against the query with the ranking cross-encoder,
// so we highlight the model's evidence for the ranking (not literal keywords).
async function loadRelevance(){
 curRel=null;
 if(!curQuery||!curJ||!curKey) return;
 const ck=curJ+"|"+curKey+"|"+curQuery;
 if(ck in relCache){ curRel=relCache[ck]; return; }
 let d; try{ d=await (await authFetch(`/api/relevance?jurisdiction=${enc(curJ)}&key=${enc(curKey)}&q=${enc(curQuery)}`)).json(); }
 catch(e){ relCache[ck]=null; return; }
 if(!d||!d.enabled||!(d.passages||[]).length){ relCache[ck]=null; return; }
 const byIndex=new Map(d.passages.map(p=>[p.index,p.score]));
 let top=d.passages[0].index, max=d.passages[0].score;
 for(const p of d.passages) if(p.score>max){ max=p.score; top=p.index; }
 curRel={key:d.key||curKey, byIndex, top, maxLabel:(max>=0?"+":"")+max};
 relCache[ck]=curRel;
}
let renderSeq=0;
function renderBody(body){
 if(detTab==="doc") return renderDocumentInto(body, curJ, curRegion, curKey, curQuery);
 return renderSectionInto(body, curJ, curRegion, curKey);
}
async function renderDetail(){
 const det=document.getElementById("detail");
 if(!curJ||!curKey) return;
 document.querySelector("main").classList.toggle("docmode", detTab==="doc");  // full-doc = wide master-detail
 applySplit();
 const seq=++renderSeq;  // guard: ignore stale async work if the user navigates again
 det.innerHTML=`<div class="dtabs">
   <button class="tab ${detTab==='clause'?'active':''}" data-dt="clause">Clause</button>
   <button class="tab ${detTab==='doc'?'active':''}" data-dt="doc">Full document</button>
   <span class="relstat" id="relstat"></span>
   <span class="dtab-j">${esc(curJ)}</span></div><div id="detbody"></div>`;
 det.querySelectorAll(".dtabs .tab").forEach(t=>t.onclick=()=>{ if(detTab!==t.dataset.dt){ detTab=t.dataset.dt; renderDetail(); }});
 const body=det.querySelector("#detbody");
 // 1) paint immediately, no highlights — the pane never waits on the reranker
 curRel=null;
 await renderBody(body);
 if(seq!==renderSeq) return;  // superseded by a newer selection
 // 2) score the clause's passages, then re-paint with the evidence highlighted
 if(curQuery){
   const st=document.getElementById("relstat");
   if(st){ st.className="relstat busy"; st.innerHTML='<span class="spin"></span>finding key passages…'; }
   await loadRelevance();
   if(seq!==renderSeq) return;
   if(curRel){ await renderBody(body);
     const s2=document.getElementById("relstat"); if(s2){ s2.className="relstat done"; s2.textContent="✓ relevant passage marked"; } }
   else { const s2=document.getElementById("relstat"); if(s2) s2.className="relstat"; }
 }
}
function jumpTo(det,key){
 const el=det.querySelector(`section[data-key="${(key||'').replace(/"/g,'\\"')}"]`);
 if(!el) return;
 det.querySelectorAll("section.docsec.hl").forEach(s=>s.classList.remove("hl"));
 el.classList.add("hl");
 el.scrollIntoView({block:"start"});
}
// Full-document viewer as MASTER-DETAIL: left = the whole document's nested
// hierarchy (indented by level, click to navigate); right = the selected chunk's
// detail (breadcrumb + level/type badges, its text first, footnotes at the bottom).
// No page image — the parsed content is what matters. Backed by /api/document
// (all sections in order, each with elements + footnotes), so navigation is
// instant and needs no per-click fetch.
async function renderDocumentInto(det, jurisdiction, region, key, query){
 det.innerHTML='<div class="muted" style="padding:8px">Loading document…</div>';
 let doc=docCache[jurisdiction];
 if(!doc){
   try{ doc=await (await authFetch(`/api/document?jurisdiction=${enc(jurisdiction)}`)).json(); }
   catch(e){ if(isAbort(e)) return; doc=null; }
   if(doc && Array.isArray(doc.sections)) docCache[jurisdiction]=doc;  // don't cache error bodies
 }
 if(!doc || !Array.isArray(doc.sections)){
   det.innerHTML=`<div class="card"><div class="crumb">${esc(jurisdiction)}</div>`
     +`<p class="muted">Couldn't load this document${doc&&doc.detail?` — ${esc(doc.detail)}`:''}.</p></div>`;
   return;
 }
 const secs=doc.sections.filter(s=>(s.level||0)>0);   // drop the synthetic level-0 root
 const has=new Set(doc.sections.map(s=>s.key));
 const byKey={}; secs.forEach(s=>byKey[s.key]=s);
 // Breadcrumb per node: walk the running ancestor stack by decreasing level.
 const crumbs={}, stk=[];
 for(const s of secs){
   while(stk.length && stk[stk.length-1].level>=s.level) stk.pop();
   crumbs[s.key]=stk.concat([s]);
   stk.push(s);
 }
 const kindOf=s=>{ const k=new Set((s.elements||[]).map(e=>e.kind));
   return k.has("table")?"table":k.has("bullet")?"list":k.has("question")?"guidance":"text"; };
 // LEFT: nested hierarchy, each row indented by its level.
 const treeRows=secs.map(s=>{
   const lvl=Math.max(1,Math.min(6,s.level||1));
   return `<div class="tnode l${lvl}" data-k="${esc(s.key)}" style="padding-left:${8+(lvl-1)*15}px" title="${esc(s.key)} ${esc(s.title)}"><span class="tk">${esc(s.key)}</span><span class="tt">${esc(s.title)}</span></div>`;
 }).join("");
 det.innerHTML=`<div class="docmaster"><div class="doctree" id="doctree"><div class="doctreehd">${esc(doc.title)}</div>${treeRows}</div><div class="docdet" id="docdet"><div class="hero" style="padding:20px">Select a section on the left to read it here.</div></div></div>`;
 const tree=det.querySelector("#doctree"), pane=det.querySelector("#docdet");
 // RIGHT: the clicked node — text first, footnotes at the bottom.
 function selectNode(k){
   const s=byKey[k]; if(!s) return;
   curKey=k;
   tree.querySelectorAll(".tnode.sel").forEach(n=>n.classList.remove("sel"));
   const node=tree.querySelector(`.tnode[data-k="${(k||'').replace(/"/g,'\\"')}"]`);
   if(node){ node.classList.add("sel"); node.scrollIntoView({block:"nearest"}); }
   const cr=(crumbs[k]||[]).map(a=>`<span class="cb" data-k="${esc(a.key)}">${esc(a.title)}</span>`).join('<span class="csep">›</span>');
   const rel=(curRel&&curRel.key===k)?curRel:null;
   const bodyHtml=renderEls(s.elements,{rel,has})||'<p class="muted">(no text of its own — see its sub-clauses)</p>';
   const fn=fnBlock(s.footnotes,k,has);
   pane.innerHTML=`<div class="ddhead"><div class="crumb">${cr}</div>`
     +`<h2><span class="dk">[${esc(k)}]</span> ${esc(s.title)}</h2>`
     +`<div class="badges"><span class="lvlbadge">level ${s.level}</span><span class="typebadge">${kindOf(s)}</span></div></div>`
     +`<div class="ddbody">${bodyHtml}</div>${fn}`;
   // clickable cross-refs + breadcrumb hops navigate within the tree
   pane.querySelectorAll(".dref,.cb[data-k]").forEach(a=>a.onclick=()=>{ if(byKey[a.dataset.k]) selectNode(a.dataset.k); else openClause(jurisdiction,region,a.dataset.k); });
   pane.scrollTop=0;
 }
 tree.querySelectorAll(".tnode").forEach(n=>n.onclick=()=>selectNode(n.dataset.k));
 const start=byKey[key]?key:(secs[0]&&secs[0].key);   // open on the searched clause, else the top
 if(start) selectNode(start);
}

async function fillDocLinks(det, jurisdiction){
 let links; try{ links=await loadLinks(jurisdiction); }catch(e){ return; }
 const aside=det.querySelector(".doclinks");
 if(!aside || aside.dataset.jur!==jurisdiction) return;  // user navigated away
 const lkbody=aside.querySelector(".lkbody");
 const chip=(it,kind)=> kind==='alert'
   ? (it.score!=null?`<span class="lkscore" title="semantic mapping confidence (cosine)" style="background:${cosCol(it.score)}">${it.score}</span>`:'')
   : (it.color?`<span class="lkscore" title="rating" style="background:${RAT[it.color]||'#7B888A'}">${esc(it.color[0])}</span>`:'');
 const lrow=(it,kind,i)=>`<div class="lkrow" data-kind="${kind}" data-i="${i}">
     <span class="lkkey" data-jump="${esc(it.key)}" title="jump to ${esc(it.key)}">${esc(it.key)}</span>
     <span class="b"><div class="t">${esc(kind==='alert'?it.alert:(it.subject||it.question||''))}</div></span>
     ${chip(it,kind)}</div>`;
 const al=links.alerts.map((a,i)=>lrow(a,'alert',i)).join("")||'<div class="muted" style="padding:2px 8px">none</div>';
 const un=links.unmapped_alerts.length?`<div class="lksec">⚠ Unmapped (${links.unmapped_alerts.length})</div>`
   +links.unmapped_alerts.map((a,i)=>`<div class="lkrow" data-kind="unmapped" data-i="${i}"><span class="lkkey" style="background:var(--danger)">none</span><span class="b"><div class="t">${esc(a.alert)}</div></span></div>`).join(""):'';
 const gu=links.guidance.map((g,i)=>lrow(g,'guidance',i)).join("")||'<div class="muted" style="padding:2px 8px">none</div>';
 lkbody.innerHTML=`<div class="lksec">Alerts → section (${links.alerts.length})</div>${al}${un}<div class="lksec">Guidance → section (${links.guidance.length})</div>${gu}`;
 const btn=det.querySelector("#lkbtn"); if(btn) btn.textContent=`🔗 Links (${links.alerts.length+links.guidance.length+links.unmapped_alerts.length})`;
 lkbody.querySelectorAll(".lkrow").forEach(r=>r.onclick=(e)=>{
   if(e.target.dataset.jump){ jumpTo(det, e.target.dataset.jump); aside.classList.remove("open"); return; }
   const k=r.dataset.kind, i=+r.dataset.i;
   if(k==='alert') linkPopup(jurisdiction, links.alerts[i], 'alert');
   else if(k==='guidance') linkPopup(jurisdiction, links.guidance[i], 'guidance');
   else { const a=links.unmapped_alerts[i]; openModal(`<div class="mhead">🔔 Unmapped alert (no clause)</div><div class="mtitle"><b>${esc(a.alert)}</b> <span class="muted">${esc(a.impact||'')}</span></div><div class="mbody">${fmt(a.summary||'')}</div>`); }
 });
}
function select(i,node){
 document.querySelectorAll(".r").forEach(n=>n.classList.remove("sel"));
 if(node)node.classList.add("sel");
 const hit=results[i];
 openClause(hit.jurisdiction, hit.region, hit.key, document.getElementById("q").value.trim());
}

// ---- draggable column split (#left/#chat <-> #detail) ----
// One remembered fraction (left pane's share of main's width) applied uniformly across
// every 2-column mode; single-column modes (gallery/extract/prompt) and docmode (its own
// internal master-detail split) hide the handle and leave their own grid rule in charge.
const SPLIT_KEY="aci_split", SPLIT_MIN=260, SPLIT_W=6;
const splittable=()=>{ const c=document.querySelector("main").classList;
 return !c.contains("docmode")&&!c.contains("gallerymode")&&!c.contains("extractmode")&&!c.contains("promptmode"); };
// Both sides keep SPLIT_MIN whenever the window has room for it; on a window too narrow
// for both minimums (rare, but a garbage stored fraction or a tiny viewport can get here),
// fall back to an even split instead of forcing an overflow that pushes the handle — and
// everything right of it — off screen with no way to drag it back.
function clampLeft(px, totalW){
 const avail=totalW-SPLIT_W-SPLIT_MIN;
 if(avail<SPLIT_MIN) return Math.max(0, Math.round((totalW-SPLIT_W)/2));
 return Math.max(SPLIT_MIN, Math.min(px, avail));
}
function readSplitFrac(){
 const f=parseFloat(localStorage.getItem(SPLIT_KEY));
 return (isFinite(f)&&f>0&&f<1) ? f : 0.6;   // ignore anything corrupt (NaN/Infinity/out of range)
}
function applySplit(){
 const m=document.querySelector("main");
 if(!splittable()){ m.style.gridTemplateColumns=""; return; }
 const w=m.clientWidth; if(!w) return;
 const leftPx=clampLeft(Math.round(w*readSplitFrac()), w);
 m.style.gridTemplateColumns=`${leftPx}px ${SPLIT_W}px 1fr`;
}
(function(){
 const sp=document.getElementById("colsplit");
 let dragging=false;
 // Pointer Capture, not plain mouse events: once the button goes down on the handle, EVERY
 // subsequent pointer event for that pointer is delivered to `sp` even if the cursor leaves
 // the viewport mid-drag (a fast drag to the window edge). Without capture, a browser can
 // stop delivering move/up once the cursor exits the document, leaving `dragging` stuck
 // true forever — which is exactly what made the handle unrecoverable.
 sp.addEventListener("pointerdown",e=>{
   if(!splittable())return;
   dragging=true; sp.classList.add("dragging"); sp.setPointerCapture(e.pointerId);
   document.body.style.cursor="col-resize"; document.body.style.userSelect="none";
   e.preventDefault();
 });
 sp.addEventListener("pointermove",e=>{
   if(!dragging)return;
   const m=document.querySelector("main"), r=m.getBoundingClientRect();
   const leftPx=clampLeft(e.clientX-r.left, r.width);
   m.style.gridTemplateColumns=`${leftPx}px ${SPLIT_W}px 1fr`;
   localStorage.setItem(SPLIT_KEY, String(leftPx/r.width));
 });
 const endDrag=e=>{
   if(!dragging)return;
   dragging=false; sp.classList.remove("dragging");
   document.body.style.cursor=""; document.body.style.userSelect="";
   try{ sp.releasePointerCapture(e.pointerId); }catch(_){}
 };
 sp.addEventListener("pointerup",endDrag);
 sp.addEventListener("pointercancel",endDrag);
 // A manual way back regardless of how it got stuck: double-click the handle to drop the
 // saved split and return to the default ratio.
 sp.addEventListener("dblclick",()=>{ localStorage.removeItem(SPLIT_KEY); applySplit(); });
 window.addEventListener("resize",()=>applySplit());
})();

// ---- tabs: search vs conversational AI ----
function setMode(mode){
 newViewScope();                // cancel whatever the screen we are leaving still has running
 const ai=mode==="ai", gal=mode==="gallery", ex=mode==="extract", pr=mode==="prompts";
 const main=document.querySelector("main");
 main.classList.remove("docmode");  // leave full-doc layout when switching mode
 main.classList.toggle("chat", ai);
 main.classList.toggle("gallerymode", gal);
 main.classList.toggle("extractmode", ex);
 main.classList.toggle("promptmode", pr);
 document.getElementById("searchbar").style.display = (ai||gal||ex||pr) ? "none" : "flex";
 const _rb=document.querySelector(".regionbar"); if(_rb) _rb.style.display = (gal||ex||pr) ? "none" : "";  // no search-filter outside search/AI
 document.querySelectorAll(".aionly").forEach(e=>e.style.display = ai ? "inline-flex" : "none");
 document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active", t.dataset.mode===mode));
 applySplit();
 if(!ex) stopExPoll();          // never keep polling a screen the user has left
 if(ai){
   document.getElementById("detail").innerHTML='<div class="hero">Click a source the agent cites to read that clause here.</div>';
   if(pendingAi){ const x=pendingAi; pendingAi=null; sendChat(x); }  // one-shot; tab toggling won't re-fire
   else { const c=document.getElementById("chatq"); if(c) c.focus(); }
 } else if(gal){
   loadGallery();
 } else if(ex){
   loadExtraction();
 } else if(pr){
   loadPrompts();
 }
}

// ---- Prompts: the master prompt per product (AOSNG-3442) ----
// The prompt decides what the assistant asserts about regulated content, so this screen is
// built around one question: WHICH prompt answers a question about this product, and is it the
// default or something a subject-matter expert wrote. Editing is admin-only server-side; this
// UI does not enforce that, it reflects it — a 403 on save is shown as a refusal, not an error.
let _prProducts=[], _prSel=null, _prData=null, _prDraft=null, _prSaving=false;
let _prView='edit';   // edit | preview | diff
// The list's first row is the DEFAULT, which is not a product: it is the prompt every product
// without an override falls back to, and it is shown read-only. A sentinel rather than a null
// selection so that "nothing selected" stays distinguishable from "the default is selected".
const PRDEF='__default__';
function prIsDefault(){ return _prSel===PRDEF; }
// Which of the two answers is being edited. The AI Mode screen has one "Explain" checkbox:
// off is a summary, on is an explanation, and each has its own master prompt.
let _prMode='summary';
let _prMde=null, _prMdePromise=null;

function prDirty(){ return _prDraft !== null && _prData && _prDraft !== (_prData.override || ""); }

// A line diff of the draft against the DEFAULT prompt.
//
// Worth more on this screen than a rendered preview: the SME's real question is not "how does
// my markdown look" but "what have I changed from the prompt that already works". It also
// gives a reviewer something to read before a prompt goes live — the text decides what the
// assistant asserts about regulated content, and 4.4k characters of prose hides a one-word
// change completely.
//
// Longest-common-subsequence over LINES. Lines, not words: this text is read by a model, and
// its paragraph and bullet structure is the part that carries meaning (see the note in
// agent.py about where an instruction sits). A word diff would scatter the change across a
// wall of highlights and lose exactly that.
function prLineDiff(a, b){
  const A=(a||"").split("\n"), B=(b||"").split("\n");
  // Guard the O(n*m) table: these are ~120-line texts, but a pasted document must not hang
  // the tab. Above the cap, fall back to "everything replaced", which is honest.
  if(A.length*B.length > 400000) return [["-",a||""],["+",b||""]];
  const L=Array.from({length:A.length+1},()=>new Uint32Array(B.length+1));
  for(let i=A.length-1;i>=0;i--) for(let j=B.length-1;j>=0;j--)
    L[i][j] = A[i]===B[j] ? L[i+1][j+1]+1 : Math.max(L[i+1][j], L[i][j+1]);
  const out=[]; let i=0,j=0;
  while(i<A.length && j<B.length){
    if(A[i]===B[j]){ out.push([" ",A[i]]); i++; j++; }
    else if(L[i+1][j] >= L[i][j+1]){ out.push(["-",A[i]]); i++; }
    else { out.push(["+",B[j]]); j++; }
  }
  while(i<A.length) out.push(["-",A[i++]]);
  while(j<B.length) out.push(["+",B[j++]]);
  return out;
}

function prRenderDiff(){
  const base=_prData ? (_prData.default_prompt || "") : "";
  const draft=_prDraft || "";
  if(!base) return '<div class="galsub">The default prompt was not returned, so there is '
    +'nothing to compare against.</div>';
  if(draft==="") return '<div class="galsub">No override — this product uses the default '
    +'prompt unchanged.</div>';
  const rows=prLineDiff(base, draft);
  const added=rows.filter(r=>r[0]==="+").length, removed=rows.filter(r=>r[0]==="-").length;
  if(!added && !removed) return '<div class="galsub">Identical to the default prompt.</div>';
  let h='<div class="galsub">'+added+' line(s) added, '+removed+' removed, against the '
   +'default.</div><div class="prdiff">';
  let hidden=0;
  for(let k=0;k<rows.length;k++){
    const [mark,text]=rows[k];
    if(mark===" "){
      // collapse long unchanged stretches: keep a line of context either side of a change
      const near=(rows[k-1]&&rows[k-1][0]!==" ")||(rows[k+1]&&rows[k+1][0]!==" ");
      if(!near){ hidden++; continue; }
    }
    if(hidden){ h+='<div class="prdskip">… '+hidden+' unchanged line(s)</div>'; hidden=0; }
    const cls = mark==="+" ? "prdadd" : mark==="-" ? "prddel" : "prdsame";
    h+='<div class="'+cls+'">'+esc(mark+" "+text)+'</div>';
  }
  if(hidden) h+='<div class="prdskip">… '+hidden+' unchanged line(s)</div>';
  return h+'</div>';
}

// Repaint only the pane body, never the whole screen: the textarea stays mounted (hidden), so
// switching to the preview and back does not lose the caret, the selection or the scroll
// position in a 4,000-character prompt.
function setPrView(v){
  _prView=v;
  const root=document.getElementById("detail"); if(!root) return;
  root.querySelectorAll("[data-prview]").forEach(b=>
    b.classList.toggle("active", b.getAttribute("data-prview")===v));
  // Hide the WRAPPER, not the textarea: once EasyMDE has mounted, the visible element is its
  // own container beside the (now hidden) textarea, and the editor must survive a look at the
  // diff with its caret and scroll position intact.
  const ew=root.querySelector("#preditwrap"), rd=root.querySelector("#prview");
  if(!ew||!rd) return;
  ew.hidden = v!=="edit";
  rd.hidden = v==="edit";
  if(v==="preview") rd.innerHTML=prRenderPreview();
  else if(v==="diff") rd.innerHTML=prRenderDiff();
  if(v==="edit"){
    // CodeMirror measures itself on mount; while it was inside a hidden container those
    // measurements were all zero, so it needs a refresh or it paints an empty gutter.
    if(_prMde){ _prMde.codemirror.refresh(); _prMde.codemirror.focus(); }
    else { const ta=root.querySelector("#prtext"); if(ta) ta.focus(); }
  }
}

// ---- Markdown editing (EasyMDE, MIT) ----------------------------------------------------
// EasyMDE is a SOURCE editor, not a WYSIWYG: CodeMirror over the markdown text, with a toolbar
// that inserts markdown syntax. That distinction is the reason it is acceptable here. The stored
// text goes VERBATIM into the system prompt and agent.py records a measurement showing that
// where an instruction sits changes whether it holds — a WYSIWYG round-trip through HTML would
// normalise whitespace and could alter the prompt without the author seeing it. Here the buffer
// is the same characters the author typed.
//
// Loaded LAZILY, on first entry to this screen: it is 319KB of JavaScript for an admin-only
// tab, and no other user of the page should pay for it. Pinned to an exact version with an SRI
// hash, because a script that can rewrite the prompt is a script that decides what the
// assistant asserts about regulated content.
//
// If it fails to load — blocked CDN, SRI mismatch, offline — the plain textarea underneath
// stays exactly as it is and remains fully usable. The editor is an affordance, never the
// mechanism.
const MDE_JS="https://cdn.jsdelivr.net/npm/easymde@2.20.0/dist/easymde.min.js";
const MDE_CSS="https://cdn.jsdelivr.net/npm/easymde@2.20.0/dist/easymde.min.css";
const MDE_JS_SRI="sha384-YDXeUfPZ4SP6vJpnF+ZMmf4B1bax6yd4Q/aNbkvLidRD843hPG5RE67M0IYT4LOq";
const MDE_CSS_SRI="sha384-3AvV7152TgYAMYdGZPqG9BpmSH2ZW6ewTDL0QV5PyNkl19KMI+yLMdJz183N8A2d";

function prLoadEditor(){
  if(window.EasyMDE) return Promise.resolve(true);
  if(_prMdePromise) return _prMdePromise;
  _prMdePromise=new Promise(res=>{
    const l=document.createElement("link");
    l.rel="stylesheet"; l.href=MDE_CSS; l.integrity=MDE_CSS_SRI; l.crossOrigin="anonymous";
    document.head.appendChild(l);
    const sc=document.createElement("script");
    sc.src=MDE_JS; sc.integrity=MDE_JS_SRI; sc.crossOrigin="anonymous";
    sc.onload=()=>res(!!window.EasyMDE);
    sc.onerror=()=>res(false);          // fall back to the textarea, silently and safely
    document.head.appendChild(sc);
  });
  return _prMdePromise;
}

// Four tools, as asked: heading, bold, lists, table. Deliberately no link, image, code, quote,
// fullscreen or guide — this is a prompt, not a document, and every extra button is a way to
// paste syntax the renderer downstream does not read.
//
// Text labels rather than icons: EasyMDE emits Font Awesome class names and ships no icon font,
// so the stock toolbar renders as blank buttons unless a whole icon pack is added. Four labels
// cost nothing and stay legible.
//
// The inserted syntax is pinned to what mdToHtml can actually render — `**bold**`, `-` bullets,
// and a table with a separator row. A toolbar that inserted `__bold__` would produce text the
// preview shows as literal underscores.
function prToolbar(){
  const E=window.EasyMDE;
  return [
    {name:"heading", action:E.toggleHeadingSmaller, text:"H",       title:"Heading"},
    {name:"bold",    action:E.toggleBold,           text:"B",       title:"Bold"},
    "|",
    {name:"unordered-list", action:E.toggleUnorderedList, text:"• List",    title:"Bulleted list"},
    {name:"ordered-list",   action:E.toggleOrderedList,   text:"1. List",   title:"Numbered list"},
    "|",
    {name:"table",   action:E.drawTable,            text:"Table",   title:"Insert a table"},
  ];
}

function prDestroyEditor(){
  // Called before #detail is replaced. Without this, every repaint would leak a CodeMirror
  // instance and its document-level key handlers.
  if(!_prMde) return;
  try{ _prMde.toTextArea(); }catch(e){}
  _prMde=null;
}

async function prMountEditor(){
  const ta=document.querySelector("#prtext");
  if(!ta || _prMde) return;
  if(!await prLoadEditor()) return;                 // textarea stays; nothing else to do
  // The screen may have been left, or repainted, during the load.
  const still=document.querySelector("#prtext");
  if(!still || still!==ta || _prMde) return;
  _prMde=new window.EasyMDE({
    element: ta,
    toolbar: prToolbar(),
    toolbarTips: true,
    status: false,
    // OFF, deliberately. Autosave is keyed by a single uniqueId in localStorage, so with
    // several products on one screen it would restore the WRONG product's draft — and a stale
    // draft of a regulated-content prompt silently reappearing is precisely the failure this
    // screen must not have. Unsaved work is already tracked by prDirty() and confirmed on
    // switch.
    autosave: {enabled:false},
    spellChecker: false,        // its dictionary is US-English and underlines every legal term
    nativeSpellcheck: false,
    lineWrapping: true,         // the prompt is prose; horizontal scrolling would be unreadable
    indentWithTabs: false,
    minHeight: "340px",
    blockStyles: {bold:"**", italic:"*"},
    unorderedListStyle: "-",
    insertTexts: {table:["", "\n\n| Column 1 | Column 2 |\n| --- | --- |\n| Text | Text |\n\n"]},
    forceSync: true,            // keep the underlying textarea in step, so #prtext stays truthful
    previewRender: (t)=>mdToHtml(t),   // one renderer for the whole page, one escaping path
    placeholder: ta.getAttribute("placeholder")||"",
    initialValue: _prDraft||"",
  });
  // forceSync writes to the textarea programmatically, which fires no input event, so the
  // draft and the dirty state are tracked from CodeMirror directly.
  _prMde.codemirror.on("change", ()=>{ _prDraft=_prMde.value(); prSyncDirty(); });
}

function prSyncDirty(){
  const det=document.getElementById("detail"); if(!det) return;
  const st=det.querySelector("#prstate"); if(st) st.textContent=prDirty()?"unsaved changes":"";
  const sv=det.querySelector("#prsave"); if(sv) sv.disabled=!prDirty();
  const sd=det.querySelector("#prseed"); if(sd) sd.disabled=!!(_prDraft||"").trim();
}

// The stored text goes VERBATIM into the system prompt, so this is a preview and not an editor.
// agent.py records a measurement showing that where an instruction sits inside the prompt
// changes whether it holds; a WYSIWYG round-trip through HTML would normalise whitespace and
// could alter the prompt without the author seeing it. The textarea remains the only source of
// truth, byte for byte.
function prRenderPreview(){
  // Whatever the pane is showing: the draft for a product, the default itself on the read-only
  // row — where the draft is null precisely so nothing can be saved.
  const ro=!!(_prData && _prData.read_only);
  const t=ro ? ((_prData.default_prompt)||"") : (_prDraft||"");
  if(!t.trim()) return '<div class="galsub">Nothing to preview — this product uses the '
    +'default prompt.</div>';
  // mdToHtml is the renderer used for AI answers, so no new dependency. It also turns
  // [Jurisdiction · Clause] into .cite anchors; the prompt contains those as FORMAT EXAMPLES,
  // and here they are deliberately inert — the click handler is bound only inside an answer
  // body, and .cite is styled only there, so an example reads as text rather than as a link
  // into a clause that does not exist.
  return '<div class="galsub">Rendered as markdown, for reading only. The prompt is sent to '
    +'the model exactly as typed — whitespace and line breaks included.</div>'
    +'<div class="prmd">'+mdToHtml(t)+'</div>';
}

async function revealPromptsTab(){
  // The tab is hidden until the server says this user may see it. Editing is admin-only and
  // enforced server-side; probing rather than trusting a client-side role means the UI cannot
  // offer a screen the API will refuse, and cannot be talked into showing one either.
  try{
    const r=await authFetch("/api/prompts", NO_CANCEL);
    if(!r.ok) return;                       // 403/404 -> stay hidden, no error to the user
    const t=document.querySelector(".prompttab");
    if(t) t.style.display="inline-flex";
  }catch(e){ /* absent endpoint or offline: the tab simply does not appear */ }
}

async function loadPrompts(){
  const det=document.getElementById("detail");
  det.innerHTML='<div class="prwrap"><div class="galsub">Loading products…</div></div>';
  const tok=_viewSeq;
  try{
    const r=await authFetch("/api/prompts");
    if(viewStale(tok)) return;
    if(!r.ok){
      det.innerHTML='<div class="prwrap"><div class="galsub">Could not load prompts ('+r.status+').'
        +(r.status===403?' This screen is restricted to admin users.':'')+'</div></div>';
      return;
    }
    const d=await r.json();
    if(viewStale(tok)) return;
    _prProducts=d.products||[];
    // The default row survives a reload: it is always available, even with no products.
    if(!prIsDefault() && (!_prSel || !_prProducts.some(p=>p.product===_prSel)))
      _prSel=(_prProducts[0]||{}).product||PRDEF;
    renderPrompts();
    if(_prSel) loadOnePrompt(_prSel);
  }catch(e){
    if(isAbort(e)) return;
    det.innerHTML='<div class="prwrap"><div class="galsub">Error loading prompts.</div></div>';
  }
}

async function loadOnePrompt(product, force){
  // Switching product with unsaved edits would lose them silently. `force` is set by a mode
  // switch, which has already asked.
  if(!force && prDirty() && !confirm("Discard your unsaved changes to "+_prSel+"?")) return;
  _prSel=product; _prData=null; _prDraft=null;
  // There is nothing to diff the default against — itself.
  if(product===PRDEF && _prView==="diff") _prView="edit";
  renderPrompts();
  const tok=_viewSeq;
  try{
    const r=await authFetch(product===PRDEF
      ? "/api/prompts/_default?mode="+enc(_prMode)
      : "/api/prompts/"+enc(product)+"?mode="+enc(_prMode));
    if(viewStale(tok)) return;
    if(!r.ok){ _prData={error:"HTTP "+r.status}; renderPrompts(); return; }
    _prData=await r.json();
    if(viewStale(tok)) return;
    // Left NULL for the default, deliberately: prDirty() and savePrompt() both key on the
    // draft, so a read-only pane cannot become dirty and cannot be saved even if a button for
    // it were ever rendered.
    _prDraft=_prData.read_only ? null : (_prData.override || "");
    renderPrompts();
  }catch(e){ if(!isAbort(e)){ _prData={error:"request failed"}; renderPrompts(); } }
}

async function setPrMode(mode){
  if(mode===_prMode) return;
  // Same rule as switching product: the two modes are separate prompts, so moving between
  // them discards whatever is in the box.
  if(prDirty() && !confirm("Discard your unsaved changes to the "+_prMode+" prompt?")) return;
  _prMode=mode; _prData=null; _prDraft=null; _prView='edit';
  renderPrompts();
  if(_prSel) loadOnePrompt(_prSel, true);
}

async function savePrompt(){
  if(!_prSel || _prDraft===null || _prSaving) return;
  const body=_prDraft.trim();
  // An empty box means "use the default", which is a DELETE not an empty prompt — an empty
  // system prompt would strip the grounding and citation rules entirely.
  const clearing = body === "";
  if(clearing && !confirm("Clear the override for "+_prSel+" and go back to the default prompt?")) return;
  _prSaving=true; renderPrompts();
  try{
    const r=await authFetch("/api/prompts/"+enc(_prSel)+"?mode="+enc(_prMode),
      clearing ? {method:"DELETE", signal:null}
               : {method:"PUT", signal:null, headers:{"Content-Type":"application/json"},
                  body:JSON.stringify({prompt:_prDraft})});
    if(!r.ok){
      alert(r.status===403 ? "Not permitted: editing prompts is restricted to admin users."
                           : "Save failed ("+r.status+").");
      return;
    }
    _prData=await r.json(); _prDraft=_prData.override || "";
    await loadPrompts();
  }catch(e){ if(!isAbort(e)) alert("Save failed."); }
  finally{ _prSaving=false; renderPrompts(); }
}

function renderPrompts(){
  if(!document.querySelector("main").classList.contains("promptmode")) return;
  prDestroyEditor();          // #detail is about to be replaced; detach before it is
  const det=document.getElementById("detail");
  let h='<div class="prwrap"><div class="galhead">Master prompts</div>'
   +'<div class="galsub">A question scoped to exactly <b>one</b> product uses that product&#39;s '
   +'prompt. Anything broader uses the default.</div>'
   +'<div class="prmodes">'
   +'<button class="prmbtn'+(_prMode==="summary"?" active":"")+'" data-prmode="summary">'
   +'Summary answers</button>'
   +'<button class="prmbtn'+(_prMode==="explain"?" active":"")+'" data-prmode="explain">'
   +'Explain answers</button>'
   +'<span class="galsub">'+(_prMode==="explain"
      ? "The prompt used when Explain is on."
      : "The prompt used when Explain is off — the default answer.")+'</span></div>'
   +'<div class="prcols"><div class="prlist">'
   +'<button class="pritem prdefrow'+(prIsDefault()?' active':'')+'" data-prod="'+escA(PRDEF)+'">'
   +'<b>Default</b><span class="prbadge">read-only</span></button>';
  for(const p of _prProducts){
    // The badge is for the mode being edited. A single "override" badge would say a product
    // was customised when only its OTHER answer was — the likeliest way to edit the wrong one.
    const on=(p.modes||[]).indexOf(_prMode)>=0;
    const other=(p.modes||[]).filter(m=>m!==_prMode);
    h+='<button class="pritem'+(p.product===_prSel?' active':'')+'" data-prod="'+escA(p.product)+'">'
      +'<b>'+esc(p.product)+'</b>'
      +'<span class="'+(on?'prbadge prcustom':'prbadge')+'">'+(on?'override':'default')+'</span>'
      +(other.length?'<span class="prbadge prother" title="customised in the other mode">'
        +esc(other.join(", "))+'</span>':'')
      +'</button>';
  }
  h+='</div><div class="prpane">'+renderPromptPane()+'</div></div></div>';
  det.innerHTML=h;
  det.querySelectorAll(".pritem").forEach(b=>
    b.addEventListener("click",()=>loadOnePrompt(b.getAttribute("data-prod"))));
  det.querySelectorAll("[data-prmode]").forEach(b=>
    b.addEventListener("click",()=>setPrMode(b.getAttribute("data-prmode"))));
  det.querySelectorAll("[data-prview]").forEach(b=>
    b.addEventListener("click",()=>setPrView(b.getAttribute("data-prview"))));
  const sv=det.querySelector("#prsave"); if(sv) sv.addEventListener("click",savePrompt);
  const rv=det.querySelector("#prrevert");
  if(rv) rv.addEventListener("click",()=>{ _prDraft=""; savePrompt(); });
  // Copies the default into the box WITHOUT saving: an override authored from an empty box
  // starts by throwing away a prompt that is known to work, and this is the difference between
  // "edit the prompt" and "write a new one from nothing".
  const sd=det.querySelector("#prseed");
  if(sd) sd.addEventListener("click",()=>{
    if((_prDraft||"").trim()) return;
    _prDraft=(_prData&&_prData.default_prompt)||"";
    _prView="edit"; renderPrompts();
  });
  const ta=det.querySelector("#prtext");
  if(ta) ta.addEventListener("input",()=>{ _prDraft=ta.value; prSyncDirty(); });
  prMountEditor();            // upgrades the textarea in place, or leaves it alone
}

// The default prompt, shown and never editable. Two views only — the text as the model
// receives it, and a markdown rendering of it — because the other two controls have no meaning
// here: there is nothing to save it to (the store holds overrides only) and nothing to diff it
// against. Editing it would change every product's answer at once, which is what the
// per-product override below it is for.
function renderDefaultPane(){
  const t=(_prData.default_prompt)||"";
  const prev=_prView==="preview";
  return '<div class="galsub">The built-in prompt for '
   +(_prMode==="explain"?"explaining":"summary")+' answers, used by every product that has no '
   +'override. Read-only — to change what one product answers, pick it in the list and write '
   +'an override.</div>'
   +'<div class="prviews">'
   +'<button class="prvbtn'+(prev?"":" active")+'" data-prview="edit">Text</button>'
   +'<button class="prvbtn'+(prev?" active":"")+'" data-prview="preview">Preview</button>'
   +'</div>'
   +'<div id="preditwrap"'+(prev?" hidden":"")+'>'
   +'<pre class="prpre prdefbox">'+esc(t)+'</pre></div>'
   +'<div id="prview" class="prrender"'+(prev?"":" hidden")+'>'
   +(prev?prRenderPreview():"")+'</div>'
   +'<div class="prbar"><span class="galsub">'+t.length.toLocaleString()
   +' characters, sent to the model exactly as shown.</span></div>';
}

function renderPromptPane(){
  if(!_prSel) return '<div class="galsub">No products available.</div>';
  if(!_prData) return '<div class="galsub">Loading '
    +(prIsDefault()?'the default prompt':esc(_prSel))+'…</div>';
  if(_prData.error) return '<div class="galsub">Could not load this '
    +(prIsDefault()?'prompt':'product')+' ('+esc(_prData.error)+').</div>';
  if(_prData.read_only) return renderDefaultPane();
  const isOverride=!!(_prData.override && _prData.override.trim());
  const eff=_prData.effective||"";
  let h='';
  if(_prData.updated_by||_prData.updated_at)
    h+='<div class="galsub">Last changed by '+esc(_prData.updated_by||"unknown")
      +(_prData.updated_at?' · '+fmtAgo(_prData.updated_at):'')+'</div>';
  h+='<div class="prviews">'
   +'<button class="prvbtn'+(_prView==="edit"?" active":"")+'" data-prview="edit">Edit</button>'
   +'<button class="prvbtn'+(_prView==="preview"?" active":"")+'" data-prview="preview">'
   +'Preview</button>'
   +'<button class="prvbtn'+(_prView==="diff"?" active":"")+'" data-prview="diff">'
   +'Diff vs default</button></div>'
   +'<div id="preditwrap"'+(_prView==="edit"?"":" hidden")+'>'
   +'<textarea id="prtext" class="prtext" spellcheck="false"'
   +' placeholder="Empty — this product '
   +'uses the default prompt. Type here to write an override.">'+esc(_prDraft||"")+'</textarea>'
   +'</div>'
   +'<div id="prview" class="prrender"'+(_prView==="edit"?" hidden":"")+'>'
   +(_prView==="preview"?prRenderPreview():_prView==="diff"?prRenderDiff():"")+'</div>'
   +'<div class="prbar"><button class="galfbtn" id="prsave"'+(prDirty()?'':' disabled')+'>'
   +(_prSaving?'Saving…':'Save')+'</button>'
   +'<button class="galfbtn" id="prrevert"'+(isOverride?'':' disabled')+'>Revert to default</button>'
   +'<button class="galfbtn" id="prseed"'+((_prDraft||"").trim()?' disabled':'')
   +'>Start from the default</button>'
   +'<span class="galsub" id="prstate">'+(prDirty()?'unsaved changes':'')+'</span></div>'
   +'<details class="prdefault"><summary>Show the prompt actually in effect ('
   +eff.length.toLocaleString()+' characters) — the box above, plus answer rules that are '
   +'not editable here</summary><pre class="prpre">'+esc(eff)+'</pre></details>';
  return h;
}

// ---- Extraction monitor: how far along a GPU extraction run is ----
// Reads the per-shard summaries the workers publish to S3 (one small object each), never the
// scorecards — 891 documents x ~20KB per page load is exactly what took the service down when
// the gallery loaded every scorecard, and this corpus only grows.
let _exRun=null, _exTimer=null, _exRuns=[], _exData=null;

// Links into the RUN's own output, not the Doc Library: a run writes to corpus/<run>/, which the
// gallery does not read, so a document that just finished is not published anywhere yet. The
// worker builds the same viewer and inspection page beside each extraction and the backend
// proxy-streams them, so the bucket stays private.
//
// A link is only offered when the artefact EXISTS. Newer runs say so in the progress object
// (`review`); older ones are probed once per document with a HEAD and cached, never on the poll.
const EXREV={};                                    // "run|product|label" -> {viewer, inspect}
function exRevKey(p,l){ return _exRun+'|'+p+'|'+l; }
async function exProbeReview(rows){
 const todo=rows.filter(r=>r.product&&r.label&&r.review===undefined
                           && EXREV[exRevKey(r.product,r.label)]===undefined);
 if(!todo.length) return false;
 const tok=_viewSeq;
 for(const r of todo){
   // One request per document, sequentially: on a busy run this loop outlives the screen by a
   // wide margin, so it checks between every request instead of only at the end.
   if(viewStale(tok)) return false;
   const k=exRevKey(r.product,r.label);
   EXREV[k]=null;                                  // in flight: do not ask twice
   try{
     const q='run='+enc(_exRun)+'&product='+enc(r.product)+'&label='+enc(r.label);
     const resp=await authFetch('/api/extraction/review-status?'+q);
     EXREV[k]= resp.ok ? await resp.json() : {viewer:false, inspect:false};
   }catch(e){
     // An aborted probe must not be cached as "no viewer": the answer is unknown, and
     // caching the abort would permanently hide a link that does exist.
     if(isAbort(e)){ delete EXREV[k]; return false; }
     EXREV[k]={viewer:false, inspect:false};
   }
 }
 return true;
}
function exViewLink(product, label, review){
 if(!product||!label) return '';
 let have;
 if(Array.isArray(review)) have={viewer:review.includes('viewer.html'),
                                 inspect:review.includes('inspect.html')};
 else have=EXREV[exRevKey(product,label)] || null;
 if(!have) return '<span class="exdim">–</span>';
 const q='run='+enc(_exRun)+'&product='+enc(product)+'&label='+enc(label);
 const rk=product+'/'+label;
 // A crash writes no scorecard and no viewer, so "failed, no viewer" is exactly the shape a
 // reviewer should be able to act on — and the cause says whether a retry can help at all.
 const retryCell = EXRETRY.has(rk)
   ? '<span class="exdim">retry queued</span>'
   : '<a href="#" onclick="exRetry('+JSON.stringify(product).replace(/"/g,'&quot;')+','
     +JSON.stringify(label).replace(/"/g,'&quot;')+');return false">retry</a>';
 const a=(kind,text)=>'<a href="#" onclick="exOpenReview(\''+kind+'\','
   +JSON.stringify(product).replace(/"/g,'&quot;')+','
   +JSON.stringify(label).replace(/"/g,'&quot;')+');return false">'+text+'</a>';
 const bits=[];
 if(have.viewer)  bits.push(a('viewer','view'));
 if(have.inspect) bits.push(a('inspect','scorecard'));
 bits.push(retryCell);
 return bits.join(' · ');
}

// Marking a document for re-extraction. Resume skips anything with a scorecard, so this is how a
// reviewer gets ONE bad result redone without a new prefix that redoes everything. The worker
// clears the marker once it has re-extracted the document.
const EXRETRY=new Set();
async function exRetry(product, label){
 if(!confirm('Re-extract '+label+' on the next run of '+_exRun+'?')) return;
 try{
   // signal:null — a screen change must NEVER cancel this. It is a write, and an aborted write
   // is the worst of both: the server may have applied the marker already while the UI reports
   // nothing, so the reviewer cannot tell whether the document was queued.
   const r=await authFetch('/api/extraction/retry',{method:'POST', signal:null,
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({run:_exRun, product:product, label:label})});
   if(r.status===409){
     const d=await r.json().catch(()=>({}));
     // One retry per document by default: a fault the retry cannot change would otherwise be
     // paid for on every run of this prefix.
     if(confirm((d.reason||'Already retried once.')+'\n\nRetry anyway?')){
       const f=await authFetch('/api/extraction/retry',{method:'POST',
         headers:{'Content-Type':'application/json'},
         signal:null,
         body:JSON.stringify({run:_exRun, product:product, label:label, force:true})});
       if(!f.ok){ alert('Could not queue it ('+f.status+')'); return; }
     } else return;
   } else if(!r.ok){ alert('Could not queue it ('+r.status+')'); return; }
   EXRETRY.add(product+'/'+label);
   if(_exData) renderExtraction(_exData, null);
 }catch(e){ alert('Could not queue it.'); }
}
// Crashes, with a cause. Separate from the gate: a document that crashed has no scorecard at
// all, so it never appears as a bad verdict — and because resume keys on the scorecard, the next
// run of this prefix already redoes it. This is triage, so an infrastructure fault (a GPU out of
// memory) is not read as a document the pipeline cannot handle.
let EXFAILS=null;
async function exLoadFailures(){
 try{
   const tok=_viewSeq;
   const r=await authFetch('/api/extraction/failures?run='+enc(_exRun));
   if(!r.ok || viewStale(tok)) return;
   const d=await r.json();
   if(viewStale(tok)) return;
   const before=EXFAILS && EXFAILS.count;
   EXFAILS=d;
   if(d.count!==before && _exData) renderExtraction(_exData, null);
 }catch(e){ /* triage is a convenience; its absence must not break the screen */ }
}
const EXCAUSE={gpu_oom:'GPU out of memory', host_oom:'host out of memory',
               no_compiler:'no C compiler', shared_memory:'/dev/shm too small',
               model_missing:'model not resolved', timeout:'timed out', unknown:'unclassified',
               no_structure:'no headings detected', pipeline_bug:'pipeline error',
               bad_source:'unreadable source', scanned_pdf:'scanned — needs OCR'};
function exFailuresBlock(){
 if(!EXFAILS || !EXFAILS.count) return '';
 const chips=Object.entries(EXFAILS.by_cause||{}).map(([c,n])=>
   '<div class="extile"><b>'+n+'</b><span>'+esc(EXCAUSE[c]||c)+'</span></div>').join('');
 const rows=(EXFAILS.failures||[]).slice(0,12).map(f=>
   '<tr><td>'+esc(f.product||'')+'</td><td>'+esc(f.label||'')+'</td>'
   +'<td>'+esc(EXCAUSE[f.cause]||f.cause)+'</td>'
   +'<td class="exdim">'+esc(f.excerpt||'')+'</td></tr>').join('');
 return '<div class="exsub">Crashed — no scorecard written ('+EXFAILS.count+')</div>'
   +'<div class="extiles">'+chips+'</div>'
   +'<div class="exdim" style="margin:0 0 6px">A transient failure (GPU memory, a timeout) is '
   +'re-extracted automatically by the next run of this prefix, since resume keys on the scorecard '
   +'and a crash never wrote one — marking those is unnecessary. A <b>permanent</b> one is recorded '
   +'and skipped instead: the pipeline has already diagnosed it and would fail identically, so it '
   +'needs a code change, not another run.</div>'
   +'<table class="extab"><tr><th>product</th><th>document</th><th>cause</th><th>what it said</th>'
   +'</tr>'+rows+'</table>';
}
async function exLoadRetries(){
 try{
   const r=await authFetch('/api/extraction/retries?run='+enc(_exRun));
   if(!r.ok) return;
   EXRETRY.clear();
   for(const k of ((await r.json()).queued||[])) EXRETRY.add(k);
 }catch(e){ /* the marker state is a convenience */ }
}

// A plain <a href> is a browser NAVIGATION, which carries no Authorization header — so on a
// deployment with auth on it arrives as 401 "access token not supplied". Locally auth is off, so
// the links appeared to work. Fetch it with the bearer like everything else, then hand the tab a
// blob: the same approach the Doc Library viewer uses, opened in a tab rather than an iframe.
//
// The tab is opened BEFORE the await, inside the click, or a popup blocker eats it.
async function exOpenReview(kind, product, label){
 const w=window.open('', '_blank');
 // WHICH DOCUMENT THIS TAB IS. window.name belongs to the browsing context, not the document,
 // so it survives the blob: navigation below -- which is the only thing that does. The
 // inspection page's "Open viewer" button sends it back here (assets/inspect_shim.js), and
 // without it this app would know a tab wants a viewer but not for what.
 if(w) w.name='aci-review:'+product+'|'+label;
 if(w) w.document.write('<title>Loading…</title><p style="font:14px system-ui;padding:16px">'
   +'Loading '+(kind==='inspect'?'scorecard':'viewer')+'…</p>');
 try{
   const q='run='+enc(_exRun)+'&product='+enc(product)+'&label='+enc(label);
   // signal:null — this fills a TAB THE USER OPENED, not the current screen. Cancelling it on a
   // screen change would leave that tab stuck on "Loading…" forever.
   const r=await authFetch('/api/extraction/review/'+enc(kind)+'?'+q, NO_CANCEL);
   if(!r.ok){
     const msg='Could not load ('+r.status+(r.status===404?' — no '+kind+' for this document':'')+')';
     if(w){ w.document.body.innerHTML='<p style="font:14px system-ui;padding:16px">'+msg+'</p>'; }
     else alert(msg);
     return;
   }
   const url=URL.createObjectURL(new Blob([await r.text()],{type:'text/html'}));
   if(w) w.location.href=url; else window.open(url,'_blank');
 }catch(e){
   if(w) w.document.body.innerHTML='<p style="font:14px system-ui;padding:16px">Error loading.</p>';
 }
}
// The inspection page opened above asking for its viewer. Same run, same document, the
// ordinary review path -- so this is exOpenReview again with the kind flipped.
function exOpenViewerFor(ref){
 const m=/^aci-review:([^|]*)\|(.*)$/.exec(String(ref||''));
 if(m) exOpenReview('viewer', m[1], m[2]);
}
// ---- Jobs: the run, document by document ----
// The shard summaries say how far along the run is; this says WHICH document is slow and WHERE it
// stopped. It reads the append-only ledger each worker keeps (one GET per shard), so it is the
// whole run rather than the ten-document tail `recent` carries — a slow document four hours ago
// has long since fallen off that list, and it is exactly the one worth looking at.
//
// Deliberately the same columns as the local pipeline monitor (scripts/pipeline_monitor.py), which
// reads the same pipeline off disk. Two screens describing one pipeline differently is how you
// end up debugging the screen instead of the run.
let EXJOBS=null, EXJOBQ='', EXJOBFILTER='all', EXJOBOPEN=null, EXJOBMORE=false;
const EXJOBDETAIL={};                          // "product/label" -> scorecard detail | null (in flight)
// Which node of the trace is open, per document. Module level for the same reason EXJOBOPEN is:
// renderExtraction rebuilds the whole table every poll, so a selection held in the DOM would be
// thrown away every 15 seconds while someone was reading it.
const EXNODESEL={};                            // "product/label" -> node key | undefined
const EXJOB_PAGE=60;                           // 891 rows of DOM per poll is a visibly janky screen

async function exLoadJobs(){
 try{
   const tok=_viewSeq;
   const r=await authFetch('/api/extraction/jobs?run='+enc(_exRun));
   if(!r.ok || viewStale(tok)) return;
   const d=await r.json();
   if(viewStale(tok)) return;
   EXJOBS=d;
   if(_exData) renderExtraction(_exData, null);
 }catch(e){ /* the job list is detail; losing it must not blank the screen */ }
}

// label is "<jurisdiction>__<doc id>" (run_corpus.discover). Splitting it puts the document id
// where the eye lands and the jurisdiction underneath, which is how the run is actually scanned.
// fmtDur rounds to the nearest minute, which is right for a run's elapsed time and wrong here:
// 4m 56s and 5m 24s both read as "5m", and telling slow documents apart is the entire purpose of
// this table. Same formatter the local pipeline monitor uses, so the two screens agree.
function exDur(s){
 if(s==null) return '–';
 if(s<1) return (Math.round(s*10)/10)+'s';
 s=Math.round(s);
 if(s<60) return s+'s';
 const m=Math.floor(s/60), r=s%60;
 if(m<60) return r? m+'m '+r+'s' : m+'m';
 const h=Math.floor(m/60);
 return h+'h '+(m%60)+'m';
}
function exSplitLabel(label){
 const i=(label||'').lastIndexOf('__');
 return i<0 ? {id:label||'', jur:''} : {id:label.slice(i+2), jur:label.slice(0,i)};
}
function exJobKey(j){ return j.product+'/'+j.label; }

// ---- the per-document trace: the pipeline drawn as a graph ------------------------------------
// This replaces a strip of six equal dots, one per STAGES entry. Six positions on a line cannot
// express what a row is actually asked: did the PRE-FLIGHT repair the outline (so Stage 1 ran
// twice), did the document enter the FALLBACK CHAIN, and which tier's tree is the one on disk.
// Those are branches, not positions, so both scales draw the branches.
//
// ONE derivation feeds both scales. The row cell holds only the ledger row; the drill-down also
// holds the scorecard, which is the only place the chain's per-tier verdicts and the pre-flight
// record live. exNodeStates(j, d) therefore takes `d` as optional and marks what it cannot know
// 'unk' rather than guessing — a node that says "open the row" is honest, a green node nobody
// checked is not.
//
// The evidence for "this node ran" is the presence of its run_corpus._step name in the timings,
// never a guess from elapsed time: _step records a stage in a finally block, so the step exists
// even when the stage raised, which is also what lets a crash be placed exactly where it happened.

/*TRACE_JS*/
// ---- the row cell ----------------------------------------------------------------------------
// Fixed geometry on every row so the column aligns down the table: one position per node on one
// rail, in execution order, with the bypasses arced underneath. A document that skipped the
// chain reads as a run of hollow pips under one long arc, which is the same picture the
// full-size graph draws — one shape at two scales, rather than two shapes to learn.
// DERIVED from EXG_STEPS, never a literal count. Both geometries hard-coded 13, so adding a
// node to the rail left its pip unplaced and its rail segment undrawn — the node simply was
// not there, with nothing on screen to say so.
const EXTR_PITCH=18, EXTR_PAD=8;
const EXTR_X=EXG_STEPS.map((n,i)=>EXTR_PAD+EXTR_PITCH*i);
const EXTR_W=EXTR_X[EXTR_X.length-1]+EXTR_PAD;
const EXTR_Y=10, EXTR_ARC=[16,19];          // bypass depths: pass · short

function exTrPip(n,x,y,state,facts,size){
 const f=facts||{}, w=size||9;
 const tip=n.s+' · '+n.t+' — '+(f.word||EXG_STATEWORD[state]||state)
   +(f.seconds!=null?' ('+exDur(f.seconds)+')':'');
 const shape=n.gate
   ? '<polygon class="exgbox" points="'+[x+','+(y-w/2-1),(x+w/2+1)+','+y,
       x+','+(y+w/2+1),(x-w/2-1)+','+y].join(' ')+'"/>'
   : '<rect class="exgbox" x="'+(x-w/2)+'" y="'+(y-w/2)+'" width="'+w+'" height="'+w
     +'" rx="'+(n.k==='verdict'?3:1.5)+'"/>';
 return '<g class="s-'+state+'"><title>'+esc(tip)+'</title>'+shape+'</g>';
}

// A bypass, drawn as an arc under the rail from one node to another. `depth` picks the band, so
// two bypasses that overlap horizontally never overprint each other.
function exTrHop(a,b,depth,on){
 const x1=EXTR_X[a], x2=EXTR_X[b];
 return '<path class="exgedge'+(on?' on':'')+'" d="M '+x1+' '+(EXTR_Y+4)
   +' L '+x1+' '+depth+' L '+x2+' '+depth+' L '+x2+' '+(EXTR_Y+4)+'"/>';
}

// The summary AI route's row-cell trace: its own three nodes (source, AI call, scorecard),
// never the eleven-node spine it doesn't walk. EXG_STEPS answers "how far through the spine
// did this document get", which is not a question this route was ever asked — drawing all
// ten/eleven spine pips here read as a document that skipped almost everything, when the
// truth is it took a different, three-step path entirely. Mirrors SUMG_STEPS (the drill-down
// graph's own node list) so the row cell and the full graph agree on what this route's steps
// are.
const EXTR_SUM_STEPS=[
 {k:'source', s:'Source PDF',    t:'fetched / queued'},
 {k:'ai',     s:'AI extraction', t:'one call, every page'},
 {k:'score',  s:'Scorecard',     t:''},
];
function exTraceSummary(j){
 const live=!!j.live, scored=j.gate!=null;
 const st={
   source:'done',
   ai: live?'run':(scored||j.seconds!=null)?'done':'none',
   // 'crash' (not 'fail') is deliberate: the shared exgbox CSS has no s-fail rule, only
   // s-crash for the red state, and s-warn doubles as "done" ink for review.
   score: live?'none':j.gate==='pass'?'done':j.gate==='review'?'warn':j.gate==='fail'?'crash':'none',
 };
 const fa={
   source:{},
   ai:{word: live?'reading every page now':(j.seconds!=null?exDur(j.seconds):undefined)},
   score:{word: scored?('worst '+j.worst_score):(live?'not scored yet':undefined)},
 };
 const X=EXTR_SUM_STEPS.map((n,i)=>EXTR_PAD+EXTR_PITCH*i);
 const W=X[X.length-1]+EXTR_PAD;
 let g='';
 for(let i=0;i<EXTR_SUM_STEPS.length-1;i++){
   const bK=EXTR_SUM_STEPS[i+1].k, reached=st[bK]!=='none';
   g+='<polyline class="exgedge'+((live&&st[bK]==='run')?' live':reached?' on':'')
      +'" points="'+X[i]+','+EXTR_Y+' '+X[i+1]+','+EXTR_Y+'"/>';
 }
 EXTR_SUM_STEPS.forEach((n,i)=>{ g+=exTrPip(n,X[i],EXTR_Y,st[n.k],fa[n.k],9); });
 const path=EXTR_SUM_STEPS.filter(n=>['done','run','warn','crash'].indexOf(st[n.k])>=0)
   .map(n=>n.s).join(' → ');
 return '<svg class="extrace" viewBox="0 0 '+W+' 26" width="'+W+'"'
   +' style="width:'+W+'px" role="img" aria-label="'
   +escA('path: '+(path||'nothing recorded'))+'"><title>'+esc(path||'nothing recorded')
   +'</title>'+g+'</svg>';
}

function exTrace(j){
 if(j.route==='summary_ai') return exTraceSummary(j);
 const S=exNodeStates(j,EXJOBDETAIL[exJobKey(j)]);
 const st=S.st, fa=S.fa, ed=S.ed;
 const E=(pts,on,live)=>'<polyline class="exgedge'+(live?' live':on?' on':'')
   +'" points="'+pts+'"/>';
 // REACHED, not "did work". A document whose outline needed no repair still walked from Stage 1
 // to Stage 2 through the pre-flight decision, so that segment is part of its path even though
 // the node is hollow. Using "did work" put a faint break in the middle of every healthy spine.
 const reached=k=>st[k]!=='none';
 let g='';
 // The rail is drawn SEGMENT BY SEGMENT, never as one line. A single line is dark for its whole
 // length, which drew a dark rail straight through the stages a crashed document never reached
 // and through the tiers a short document was routed past — the picture then showed a path that
 // was never walked. Each segment takes the state of the node it leads INTO.
 for(let i=0;i<EXTR_X.length-1;i++)
   g+=E(EXTR_X[i]+','+EXTR_Y+' '+EXTR_X[i+1]+','+EXTR_Y, reached(EXG_STEPS[i+1].k),
        S.live&&EXSTAGE_NODE[j.running_step||j.stage||'']===EXG_STEPS[i+1].k);
 // the one branch that goes backwards: the pre-flight repaired the outline, so Stage 1 ran again
 if(ed.repair) g+='<path class="exgedge on" d="M '+EXTR_X[1]+' '+(EXTR_Y-4)+' Q '
   +((EXTR_X[0]+EXTR_X[1])/2)+' 1 '+EXTR_X[0]+' '+(EXTR_Y-4)+'"/>';
 // the three forward bypasses, deepest last so the longest arc sits outermost
 // needs_help said no, so the arc jumps the tier. It used to land on `verdict`, which no
 // longer has a node -- EXG_IX.verdict became undefined and EXTR_X[undefined] is NaN, which
 // silently drops the whole polyline.
 g+=exTrHop(EXG_IX.needs,EXG_IX.ai,EXTR_ARC[0],ed.passBypass);
 g+=exTrHop(EXG_IX.short,EXG_IX.mineru,EXTR_ARC[1],ed.shortBypass);
 EXG_STEPS.forEach((n,i)=>{
   g+=exTrPip(n,EXTR_X[i],EXTR_Y,st[n.k],fa[n.k],n.k==='verdict'?12:(n.gate?10:9)); });
 const path=EXG_ORDER.filter(k=>['done','run','disc','warn','crash'].indexOf(st[k])>=0)
   .map(k=>EXG_NODE[k].s).join(' → ');
 return '<svg class="extrace" viewBox="0 0 '+EXTR_W+' 26" width="'+EXTR_W+'"'
   +' style="width:'+EXTR_W+'px" role="img" aria-label="'
   +escA('path: '+(path||'nothing recorded'))+'"><title>'+esc(path||'nothing recorded')
   +'</title>'+g+'</svg>';
}

// ---- the node inspector ----------------------------------------------------------------------
// Clicking a node answers "what happened HERE", from what the pipeline already recorded. Nothing
// is invented: a field the run never wrote is left out rather than shown as a zero.
// Bold = this dimension set THIS document's verdict. Which ones do is decided per document and
// recorded by check_scorecard in `dimensions[k].critical` and `gating_dimensions`; this used to
// bold a hardcoded {completeness, placement, fidelity, sectioning} instead, and so reported
// gating failures that never happened. A document under 10 pages gates on completeness ALONE:
// Angola (Data Privacy)__113163 passes at 99.8 on that rule and carries sectioning 0.0, which
// the old line bolded as a critical dimension on the floor of a passing document. `sectioning`
// is likewise advisory outside the products whose section count is known.
function exGDims(dims,gating){
 const ks=Object.keys(dims||{});
 if(!ks.length) return null;
 const set=(gating&&gating.length)?gating:null;
 return ks.map(k=>{
   const v=dims[k], o=(v&&typeof v==='object'), sc=o?(v.score!=null?v.score:v.value):v;
   const s=(sc==null?'–':sc);
   const crit=(o&&v.critical!=null)?!!v.critical:(set?set.indexOf(k)>=0:!!EXG_CRIT[k]);
   const txt=esc(k)+' '+esc(String(s));
   return crit?'<b>'+txt+'</b>':txt;
 }).join(' · ');
}
function exGSteps(o){
 const ks=Object.keys(o||{}).filter(k=>k!=='total');
 if(!ks.length) return null;
 return ks.sort((a,b)=>o[b]-o[a]).map(k=>esc(k)+' '+exDur(o[k])).join(' · ');
}
function exGChainRows(rec,add){
 if(!rec) return;
 add('recorded as', rec.status);
 add('engine', rec.engine);
 add('contents page', Array.isArray(rec.toc_pages)?rec.toc_pages.join(', '):rec.toc_pages);
 if(rec.worst_before!=null&&rec.worst_after!=null)
   add('worst score', rec.worst_before+' → '+rec.worst_after
       +(rec.worst_after<rec.worst_before?'  (worse)':'  (better)'));
 if(rec.adopted!=null) add('adopted', rec.adopted?'yes — this is the tree on disk':'no');
 add('inside this tier', exGSteps(rec.steps));
 add('note', rec.note);
}

function exGInspect(j,S,key){
 const n=EXG_NODE[key]; if(!n) return '';
 const f=S.fa[key]||{}, state=S.st[key]||'none', det=S.det;
 const rows=[];
 const add=(k,v)=>{ if(v!==null&&v!==undefined&&v!=='') rows.push([k,v]); };
 if(f.seconds!=null) add('took', exDur(f.seconds));
 if(key==='stage1') add('pages', j.pages||((det&&det.timing)||{}).pages);
 if(key==='preflight'){
   const pf=det?(det.preflight||null):undefined;
   if(pf===undefined) add('record','the pre-flight record is read with the scorecard — reopen the row');
   else if(!pf) add('outcome','no toc_preflight.json was written, which is how this step records '
                    +'"the outline was trusted as it stood"');
   else {
     add('applied', pf.applied===true?'yes — the bookmarks were rewritten':'no');
     add('trigger', pf.trigger==='outline_disagrees_with_printed_toc'
        ? 'the embedded outline disagrees with the printed contents page'
        : pf.trigger==='suspect_outline' ? 'the outline is suspect (cheap triage)' : pf.trigger);
     add('why', pf.trigger_detail);
     add('repair', pf.status); add('engine', pf.engine);
     add('contents page', Array.isArray(pf.toc_pages)?pf.toc_pages.join(', '):pf.toc_pages);
     add('entries parsed', pf.entries); add('entries verified', pf.verified);
     add('error', pf.error);
   }
   if(f.repaired) add('cost note','this step contains a whole extra Stage 1 — that is the point '
                      +'of taking the decision here rather than after scoring');
 }
 if(key==='stage2'){
   const t=(det&&det.timing)||{};
   add('crop pages', t.mineru_crop_pages!=null?t.mineru_crop_pages:j.mineru_pages);
   add('tables found', j.tables!=null?j.tables:null);
   add('per crop page', t.seconds_per_crop_page!=null?t.seconds_per_crop_page+'s':null);
 }
 // The Scorecard absorbed the Verdict and the accept test, so it answers for all three.
 if(key==='score'){
   add('gate', ((det&&det.gate)||f.gate)||null);
   add('worst score', f.worst!=null?f.worst:(det?det.worst_score:null));
   // Both numbers, named, when the run ledger and the scorecard disagree. That happens whenever
   // the document was re-scored after its run, and showing one silently made the gate
   // unreconcilable with the dimensions underneath it.
   if(f.ledger!=null) add('run ledger recorded', f.ledger
     +' — the scorecard on disk has since been rewritten; the figure above is the scorecard\'s');
   add('dimensions', det?exGDims(det.dimensions,det.gating_dimensions)
                        :'read with the scorecard — reopen the row');
   const gset=(f.gating&&f.gating.length)?f.gating:null;
   if(gset) add('gated on', gset.join(' · ')+' (bold above)'
     +(gset.length===1?' — everything else is shown but did not set this verdict':''));
   else if(det) add('gated on','not recorded on this scorecard — bold falls back to the usual '
     +'critical set, so read it as an assumption rather than this document\'s own rule');
   if(f.mode&&f.mode.mode) add('scoring rule', (f.mode.mode==='short_document'
     ? 'short document ('+f.mode.pages+'pp, bar is '+f.mode.threshold+'pp)'
     : f.mode.mode==='product_rule_flat_sections'
     ? 'a product rule built this tree flat (depth '+f.mode.build_depth+')'
     : f.mode.mode)+' — '+f.mode.why);
 }
 if(key==='short'){
   add('pages', f.pages);
   add('route', f.short
     ? 'routed on page count alone, before Stage 1 ran — the extraction stages were never '
       +'started, needs_help was never asked, and the tree came from the whole-document '
       +'MinerU re-parse'
     : 'long enough for the normal route: Stage 1 runs and the entry gate later decides');
 }
 if(key==='needs'){
   const t=(det&&det.trigger)||j.trigger;
   add('escalated', S.triggered?'yes':'no');
   add('entry reason', t&&t.reason);
   if(t&&(t.causes||[]).length) add('causes', t.causes.map(c=>esc(c.label)
     +((c.score!=null&&c.threshold!=null)?' <b>'+(+c.score.toFixed(1))+'</b> below '+(+c.threshold):''))
     .join(' · '));
   const fp=(t&&t.first)||j.first_attempt;
   if(fp) add('first pass', [fp.gate?'gate '+fp.gate:'',
     fp.worst_score!=null?'worst '+fp.worst_score:'',
     fp.weakest_dimension?'weakest '+fp.weakest_dimension:'',
     fp.completeness_score!=null?'completeness '+fp.completeness_score:'',
     fp.toc_score!=null?'toc '+fp.toc_score:''].filter(Boolean).join(' · '));
 }
 if(key==='mineru') exGChainRows(f.rec,add);
 if(key==='score'){
   // Carried over from the two nodes this one replaced: the chain's accept test, and the
   // verdict's tier/crash/link rows. Same facts, one box.
   add('chain accepted', f.accepted==null?null:(f.accepted?'yes':'no'));
   add('accept test', f.accept_word);
   add('accept reason', f.accept_why);
   const fb=(det&&det.fallback)||{};
   add('adopted tier', fb.adopted_tier||j.tier_label||j.tier);
   add('hard fail', fb.hard_fail?'yes — every tier ran and none cleared the bar':null);
   add('hard fail reason', fb.hard_fail_reason);
   add('crash', f.error);
   add('open', exViewLink(j.product,j.label,j.review));
 }
 const kv=rows.length
   ? '<dl class="exgkv">'+rows.map(r=>'<dt>'+esc(r[0])+'</dt><dd>'+r[1]+'</dd>').join('')+'</dl>'
   : '<div class="exdim">Nothing was recorded for this node on this run.</div>';
 return '<div class="exgins"><div class="exginsh"><b>'+esc(n.s)+'</b>'
   +'<span class="exgpill p-'+state+'">'+esc(f.word||EXG_STATEWORD[state]||state)+'</span>'
   +'<span class="exginsw">'+esc(n.t)+'</span></div>'
   +kv+'<div class="exginsnote">'+esc(EXG_HELP[key]||'')+'</div></div>';
}

// The graph section as it appears in the drill-down, with whatever node is selected below it.
function exGraphBlock(j){
 const key=exJobKey(j);
 // A route of its own gets a graph of its own, asked before the shared spine rather than
 // inside it — exactly as scripts/pipeline_monitor.py does. exNodeStates reasons entirely
 // about a first pass and its fallback tiers, and none of those questions was ever asked of
 // a document the product rule sent straight to the summary AI pipeline: no Stage 1, no
 // MinerU, no splice, so eleven mostly-dark nodes would read as a document that failed
 // almost everything rather than one that never walked that path.
 if(j.route==='summary_ai'){
   const d=EXJOBDETAIL[key];
   return '<div class="exdetsec" style="grid-column:1/-1"><h4>Path through the pipeline</h4>'
     +sumGraph(Object.assign({},j,{detail:(d&&!d.missing)?d:null}))
     +'</div>';
 }
 const S=exNodeStates(j,EXJOBDETAIL[key]);
 S.cause=exGCause(j,S.det);
 const sel=EXNODESEL[key]||null;
 return '<div class="exdetsec" style="grid-column:1/-1"><h4>Path through the pipeline</h4>'
   +exGraph(j,S,sel)
   +(sel?exGInspect(j,S,sel)
        :'<div class="exghint">Click any node for what happened there — the pre-flight record, '
         +'each tier verdict, the causes that escalated it, the dimensions behind the gate. '
         +'A dark edge is a path this document took; a faint one is a path it did not.</div>')
   +'</div>';
}
function exGCause(j,d){
 const t=(d&&d.trigger)||j.trigger;
 if(!t||!t.primary) return '';
 const p=t.primary;
 return (p.score!=null&&p.threshold!=null)
   ? p.label+' '+(+p.score.toFixed(1))+' < '+(+p.threshold) : p.label;
}

// Which pass produced the tree that is on disk. Saying "MinerU full" without saying whether it
// was ADOPTED is actively misleading: a tier can run, score worse than the first pass and be
// thrown away, and the row would then claim a tree that was discarded.
// The trigger in one line. A fallback with no cause attached is half the story: a document
// rescued because its printed TOC was lacking is a different problem from one that lost two
// thirds of its words, and the remedy differs.
function exTrigChip(t){
 if(!t||!t.primary) return '';
 const p=t.primary;
 const txt=(p.score!=null&&p.threshold!=null)
   ? p.label+' '+(+p.score.toFixed(1))+'<'+(+p.threshold)   // the numbers say how far below
   : p.label;
 const more=(t.causes||[]).length>1 ? ' +'+((t.causes.length)-1) : '';
 return '<div class="extrig" title="'+escA(t.reason||'')+'">'+esc(txt)+more+'</div>';
}
function exPassCell(j){
 // Running: WHICH tier, and why it was entered. Without this a rescued document looks like one
 // that has gone backwards through stages it already finished.
 if(j.live){
   // Known the moment the route is decided, before any work runs — a product rule, not
   // a comparison of tiers — so this document never shows as bare "running" with no context.
   if(j.route==='summary_ai'){
     return '<span class="expass p-summary p-live"><i></i>Summary PDF</span>'
       +'<div class="expassnote">AI transcription in progress</div>';
   }
   if(j.fallback){
     const lab=j.running_tier_label||'fallback';
     const cls=j.running_tier==='mineru_full'?'p-mineru':'p-rescue';
     return '<span class="expass '+cls+' p-live"><i></i>'+esc(lab)+'</span>'
       +'<div class="expassnote">re-running \u2014 2nd pass</div>'+exTrigChip(j.trigger);
   }
   return '<span class="expass p-first p-live"><i></i>running</span>';
 }
 if(j.cloned) return '<span class="expass p-clone"><i></i>clone</span>'
   +'<div class="expassnote">identical PDF already extracted</div>';
 if(!j.tier) return '<span class="exdim">–</span>';
 const cls=j.tier==='first_pass'?'p-first':j.tier==='summary_ai'?'p-summary'
   :j.tier==='mineru_full'?'p-mineru':'p-rescue';
 const note=(j.tier==='first_pass'||j.tier==='summary_ai') ? ''
   : (j.tier==='stage1' ? 'tried, first pass held' : 'adopted');
 // A TOC-RESCUED document is still on its first pass -- the rescue happens in the
 // pre-flight, before Stage 2, so no tier ran and no extraction was repeated. But its
 // structure came from the printed contents page instead of its own bookmarks, which is
 // worth knowing at a glance and used to be invisible here: the rescue stopped being a
 // tier and stopped being labelled at the same time.
 const resc=j.toc_rescued
   ? '<div class="expassnote exresc" title="the bookmark outline was not usable, so the'
     +' outline was rebuilt from the contents page the document prints — before Stage 2 ran,'
     +' so nothing was extracted twice">\u27f2 TOC rescued</div>'
   : '';
 return '<span class="expass '+cls+'"><i></i>'+esc(j.tier_label||j.tier)+'</span>'
   +(note?'<div class="expassnote">'+note+'</div>':'')+resc
   +exTrigChip(j.trigger);
}

// Finished -> the measured cost, with the per-stage breakdown in the tooltip. Still running ->
// wall clock so far, which is the only honest answer: nothing has been scored, so there is no
// total to report yet.
function exTookCell(j){
 if(j.seconds==null) return '<td><span class="exdim">–</span></td>';
 if(j.live){
   const sub=j.stage?esc(j.stage)+(j.stage_seconds!=null?' '+exDur(j.stage_seconds):''):'running';
   return '<td title="elapsed since this document started — not yet scored">'
     +'<span class="extooklive">'+exDur(j.seconds)+'</span><div class="extooksub">'+sub+'</div></td>';
 }
 const steps=Object.entries(j.steps||{}).map(([k,v])=>k+': '+exDur(v)).join('\n');
 const rate=[j.pages?j.pages+' pages':null, j.spp?j.spp+'s / page':null,
             j.mineru_pages?j.mineru_pages+' crop-pages':null,
             j.tables!=null?j.tables+' tables':null].filter(Boolean).join(' · ');
 const tip=[steps, j.fb_seconds?('fallback tiers: '+exDur(j.fb_seconds)):'', rate]
   .filter(Boolean).join('\n');
 const sub=j.fb_seconds ? '<div class="extooksub extookfb">'+exDur(j.fb_seconds)+' fallback</div>'
   : (j.spp ? '<div class="extooksub">'+j.spp+'s/pp</div>' : '');
 return '<td title="'+escA(tip)+'"><span class="extook">'+exDur(j.seconds)+'</span>'+sub+'</td>';
}

// How long stage 4 itself ran — distinct from Took, which is the WHOLE document (stages
// 1-3 included). Absent entirely on a document that never ran it, not a 0s.
function exAiTimeCell(j){
 const s=j.steps&&j.steps.stage4_ai;
 if(s==null) return '<td><span class="exdim">–</span></td>';
 const sub=j.steps.stage5_subchunk? '<div class="extooksub">+'+exDur(j.steps.stage5_subchunk)+' stage 5</div>':'';
 return '<td title="stage 4 · AI post-processing time for this document">'+exDur(s)+sub+'</td>';
}
// Only the two paid passes (the summary-AI route, stage 4) ever set this — every other row
// has no figure to show, and "$0.00" would claim a spend that never happened.
function exCostCell(j){
 if(j.cost_usd==null) return '<td><span class="exdim">–</span></td>';
 return '<td title="AI post-processing spend for this document">$'
   +j.cost_usd.toFixed(j.cost_usd<0.01?4:2)+'</td>';
}
// Stage 4's own per-section health, not the extraction gate: a document can score PASS on
// stages 1-3 while Stage 4 quietly lost a section repairing it — a timeout, a throttle that
// exhausted its retries, a validation error, or a section the report never mentions at all
// because the process was killed mid-run. None of that shows up in gate/worst above, which is
// why this is its own column rather than folded into Status.
//
// Blank (not green) where Stage 4 does not apply: it never ran here, or the row predates the
// worker recording this (older ledger rows). Green means every section it DID run came back —
// front matter is excluded either way (see ai_postprocess._IGNORED_SECTION_RE), losing a cover
// page is not what this column exists to catch.
function exAiStageCell(j){
 const st=j.stage4_status;
 if(!st) return '<td><span class="exdim">–</span></td>';
 if(st==='ok') return '<td><span class="scpill scp-pass" title="Stage 4: every section came back">ok</span></td>';
 if(st==='incomplete') return '<td><span class="scpill scp-fail" title="Stage 4 started but never wrote its report — crashed, was killed, or is still stuck mid-run">stuck</span></td>';
 // The COUNT in the pill, the filenames in the tooltip and the drill-down. Spelling every
 // filename out here is what this column first did, and one document with five failed
 // disclaimers sections wrapped its row to six lines and pushed every other column off the
 // screen — the one thing a dense table cannot afford. The count is what a reviewer scans
 // for; which sections is what they open the row for.
 const files=(j.stage4_failed_sections||[]);
 const title=files.length ? 'Stage 4 did not finish: '+files.join(', ')+'\n\nOpen the row for why each one failed.'
                          : 'Stage 4 did not finish';
 return '<td><span class="scpill scp-fail" title="'+escA(title)+'">'
   +(files.length||'')+' section'+(files.length===1?'':'s')+'</span></td>';
}
// SC1 AND SC2 SIDE BY SIDE. The status pill shows ONE verdict -- whichever is final -- so a
// reviewer could see that a document passed, but not that the AI pass is what moved it there,
// or that it scored 94.6 having come in at 87.1. Both numbers now ride the ledger row
// (corpus_worker.finished), so this costs no extra read: the rule that this screen never opens
// scorecards in bulk is untouched.
//
// Three states per cell, and the dash means different things in each:
//   SC1 dash -> the row predates the worker recording it (scored_stage 5, no gate_extraction)
//   SC2 dash -> stage 4/5 never ran for this document, which is the majority
function exScoreCells(j){
 const cell=(gate,worst,extra,title)=> '<td'+(title?' title="'+escA(title)+'"':'')+'>'
   +(gate?scPill(gate):'<span class="exdim">\u2013</span>')
   +(worst!=null?' <span class="exdim">'+worst+'</span>':'')+(extra||'')+'</td>';
 // Where stage 4/5 never ran, the row's own gate/worst ARE the extraction verdict -- it is the
 // same scorecard under both names, not a missing one.
 const sc1Gate=j.gate_extraction || (j.scored_stage===3 ? j.gate : null);
 const sc1Worst=j.worst_extraction!=null ? j.worst_extraction
   : (j.scored_stage===3 ? j.worst : null);
 const ranAi=j.scored_stage>3;
 // The whole point of showing both: what the AI pass did to the number.
 let delta='';
 if(ranAi && sc1Worst!=null && j.worst!=null){
   const d=Math.round((j.worst-sc1Worst)*10)/10;
   delta=' <span class="exdelta" style="color:'+(d>0?'#15803d':(d<0?'#c1342d':'var(--neutral-500)'))
     +'" title="change in the weakest dimension\u2019s score, Scorecard 1 \u2192 Scorecard 2">'
     +(d>0?'+':(d<0?'':'\u00b1'))+d+'</span>';
 }
 return cell(sc1Gate, sc1Worst, '',
             sc1Gate ? 'Scorecard 1 \u2014 the extraction gate (stages 1\u20133)'
                     : 'not recorded by the worker that extracted this document')
   + (ranAi ? cell(j.gate, j.worst, delta,
                   'Scorecard 2 \u2014 the tree stage 4/5 left behind')
            : cell(null, null, '', 'stage 4/5 did not run for this document'));
}
function exStatusCell(j){
 if(j.live) return '<span class="exstate exs-running">running</span>';
 if(j.status==='error') return '<span class="exstate exs-idle" title="'+escA(j.error||'')+'">crashed</span>';
 // Not a crash and not a verdict: the worker was signalled while this document was in flight,
 // so it has no scorecard and the next run extracts it again from the top.
 if(j.status==='interrupted') return '<span class="exstate exs-interrupted" title="the worker was '
   +'stopped mid-document; it will be extracted again on the next run">interrupted</span>';
 // A hard fail says every tier ran and none cleared the bar — the pipeline is out of options,
 // which is a different instruction to a reviewer than "this scored badly".
 const hf=j.hard_fail
   ? '<div class="exhard" title="every fallback tier ran and none cleared the bar">no tier cleared</div>' : '';
 // The verdict shown is Scorecard 2's (stage 5), not Scorecard 1's (the extraction gate),
 // whenever stage 4/5 ran (see run_corpus._final_verdict) — tagged so a reviewer knows
 // this number describes the AI-processed document, not the one Stage 3 handed off.
 const postAi=j.scored_stage===5
   ? '<span class="exopttag" title="Scorecard 2 (stage 5, post-AI) verdict — not Scorecard 1, the extraction gate">SC2</span>' : '';
 return (j.gate ? scPill(j.gate) : '<span class="exdim">–</span>')+postAi+hf;
}

function exFunnelBlock(){
 if(!EXJOBS || !EXJOBS.funnel || !EXJOBS.total) return '';
 const tot=EXJOBS.total;
 // Extraction ENDS at the gate, so the finish line is the last REQUIRED row -- not the last
 // row in the list. Stages 4-5 are opt-in and sit after it, and anchoring "done" on the end
 // of the array moved the finish marker onto a stage most documents never enter.
 const lastReq=EXJOBS.funnel.reduce((a,f,i)=>f.optional?a:i,0);
 const rows=EXJOBS.funnel.map((f,i)=>{
   const pct=tot?Math.round(f.count/tot*100):0;
   // An opt-in stage reading 0 means "nobody asked for it", not "the run died here". Dimmed
   // and tagged, because an undifferentiated empty bar reads as a stalled pipeline.
   const tag=f.optional?'<span class="exopttag">opt-in</span>':'';
   return '<div class="exfunrow'+(f.optional?' exfunopt':'')+'">'
     +'<div class="exfunname">'+esc(f.label)+tag+'</div>'
     +'<div class="exfuntrack"><div class="exfunfill'+(i===lastReq?' done':'')
     +'" style="width:'+pct+'%"></div></div>'
     +'<div class="exfuncount">'+f.count+'</div></div>';
 }).join('');
 return '<div class="exsub">Pipeline funnel — documents by stage reached</div>'
   +'<div class="exfun">'+rows+'</div>';
}

// Filters are client-side on purpose: the ledger is already in hand, so narrowing to "crashed" or
// to one product costs nothing and never re-reads S3.
const EXJOBFILTERS=[['all','All'],['running','Running'],['review','Review'],['fail','Fail'],
                    ['error','Crashed'],['fallback','Fell back'],['slow','Slowest']];
function exFilterJobs(jobs, key){
 const f=key||EXJOBFILTER;
 let out=jobs;
 if(f==='running') out=out.filter(j=>j.live);
 else if(f==='fallback') out=out.filter(j=>j.fallback);
 else if(f==='slow') out=out.filter(j=>j.seconds!=null&&!j.live)
   .slice().sort((a,b)=>b.seconds-a.seconds);
 else if(f!=='all') out=out.filter(j=>j.status===f);
 if(EXJOBQ){
   const q=EXJOBQ.toLowerCase();
   out=out.filter(j=>(j.label||'').toLowerCase().includes(q)
                   ||(j.product||'').toLowerCase().includes(q));
 }
 return out;
}

function exJobsBlock(){
 if(!EXJOBS) return '<div class="exsub">Jobs</div><div class="exdim">Loading the job list…</div>';
 if(!EXJOBS.ledger && !EXJOBS.running){
   // A run extracted before the workers wrote a ledger has no job list, and an empty table would
   // read as though it had extracted nothing.
   return '<div class="exsub">Jobs</div><div class="exdim">This run has no per-document ledger — '
     +'it was extracted before the workers started writing one. The worker summaries above still '
     +'describe it, and any single document can still be opened from <b>Recently finished</b>.</div>';
 }
 const all=EXJOBS.jobs||[];
 const shown=exFilterJobs(all);
 const cap=EXJOBMORE?shown.length:EXJOB_PAGE;
 const rows=shown.slice(0,cap).map(j=>{
   const nm=exSplitLabel(j.label), k=exJobKey(j), open=EXJOBOPEN===k;
   const tr='<tr class="exjobrow'+(open?' exopen':'')+'" data-exjob="'+escA(k)+'">'
     +'<td><div class="exdocid">'+esc(nm.id)+'</div>'
       +'<div class="exjur">'+esc(j.product||'')+(nm.jur?' · '+esc(nm.jur):'')+'</div></td>'
     +'<td>'+esc(j.stage_label||'')+(j.shard!=null?'<div class="exjur">worker '+j.shard+'</div>':'')+'</td>'
     +'<td>'+exPassCell(j)+'</td>'
     +exTookCell(j)
     +exAiTimeCell(j)
     +exCostCell(j)
     +exAiStageCell(j)
     +'<td><div class="exstep">'+exTrace(j)+'</div></td>'
     +exScoreCells(j)
     +'<td>'+exStatusCell(j)
       +(j.cause?'<div class="exjur">'+esc(EXCAUSE[j.cause]||j.cause)+'</div>':'')+'</td>'
     +'<td class="exdim">'+(j.started?new Date(j.started*1000).toTimeString().slice(0,8):'–')+'</td>'
     +'<td class="exdim" title="'+escA(j.updated?new Date(j.updated*1000).toLocaleString():'')+'">'
       +(j.updated?fmtAgo(j.updated):'–')+'</td></tr>';
   return tr + (open ? exJobDetailRow(j) : '');
 }).join('');
 // Each button carries ITS OWN count, not the current view's — a filter that would show nothing
 // should say so before it is clicked, rather than emptying the table to explain itself.
 const filters=EXJOBFILTERS.map(([k,lab])=>{
   const n=exFilterJobs(all,k).length;
   return '<button class="exfbtn'+(EXJOBFILTER===k?' active':'')+'" data-exjobf="'+k+'"'
     +(n?'':' disabled')+'>'+lab+' <span class="exdim">'+n+'</span></button>';
 }).join('');
 const more=shown.length>cap
   ? '<div class="exdim" style="margin-top:8px"><button class="exfbtn" data-exjobmore="1">'
     +'Show all '+shown.length+' documents</button> — showing the '+cap+' most recent</div>' : '';
 return '<div class="exsub">Jobs — every document in this run ('+shown.length
   +(shown.length!==all.length?' of '+all.length:'')+')</div>'
   +'<div class="exjobsbar">'+filters
   +'<input class="exsearch" id="exjobq" placeholder="filter by document or product"'
   +' value="'+escA(EXJOBQ)+'"></div>'
   +'<table class="exjobs"><thead><tr><th>Document</th><th>Stage</th><th>Pass</th><th>Took</th>'
   +'<th>AI time</th><th>Cost</th>'
   +'<th title="Stage 4’s own per-section health — green if every section it ran came back, red if one timed out, errored, or was never attempted at all (a process killed mid-run). Front matter is excluded. Blank where Stage 4 does not apply to this document.">AI stage</th>'
   +'<th>Trace</th>'
   +'<th title="Scorecard 1 \u2014 the extraction gate: stages 1\u20133 only">SC1 \u00b7 extraction</th>'
   +'<th title="Scorecard 2 \u2014 scoring the tree stage 4/5 left behind">SC2 \u00b7 post-AI</th>'
   +'<th>Status</th><th>Started</th><th>Updated</th></tr></thead>'
   +'<tbody>'+rows+'</tbody></table>'+more
   +'<div class="exlegend">'
   +'<span><i style="background:var(--neutral-700)"></i>ran</span>'
   +'<span><i style="background:#fff;border:1px solid var(--neutral-300)"></i>not run</span>'
   +'<span><i style="background:var(--neutral-100)"></i>not reached</span>'
   +'<span><i style="background:#2a78d6"></i>running</span>'
   +'<span><i style="background:#b45309"></i>tier discarded</span>'
   +'<span><i style="background:#c1342d"></i>stopped / failed</span>'
   +'</div>'
   +'<div class="exdim" style="margin-top:6px">The trace is the pipeline itself: the top rail is '
   +'the spine every document walks, the second rail is the fallback chain, and a hook back over '
   +'Stage 1 means the pre-flight repaired the outline and Stage 1 re-ran. Diamonds are decisions. '
   +'Click any row for the full graph \u2014 every node clickable, with the tiers it tried, the '
   +'causes that escalated it and the dimension that decided its gate. One scorecard, read only '
   +'when you open it.</div>';
}

// ---- the drill-down: ONE scorecard, read on demand ----
// The module never reads scorecards in bulk — 891 x ~20KB per page load is what took the service
// down. Reading exactly one, only when a row is opened, is the opposite trade: a single GET for
// the only place the whole fallback chain and the per-dimension scores exist. It works on runs
// that predate the ledger too, since a scored document has always had a scorecard.
async function exOpenJob(key){
 if(EXJOBOPEN===key){ EXJOBOPEN=null; if(_exData) renderExtraction(_exData,null); return; }
 EXJOBOPEN=key;
 if(!(key in EXJOBDETAIL)){
   const tok=_viewSeq;
   EXJOBDETAIL[key]=null;                       // in flight: render the placeholder, ask once
   if(_exData) renderExtraction(_exData,null);
   const i=key.indexOf('/');
   try{
     const q='run='+enc(_exRun)+'&product='+enc(key.slice(0,i))+'&label='+enc(key.slice(i+1));
     const r=await authFetch('/api/extraction/job?'+q);
     // The run may have been switched while this was in flight, and pickExVersion has already
     // cleared the cache — writing into it now would file one run's scorecard under another's.
     if(viewStale(tok)){ delete EXJOBDETAIL[key]; return; }
     EXJOBDETAIL[key]= r.ok ? await r.json() : {missing:true, status:r.status};
   }catch(e){
     // An aborted fetch is not an answer: caching it would leave the row permanently empty.
     if(isAbort(e)){ delete EXJOBDETAIL[key]; return; }
     EXJOBDETAIL[key]={missing:true};
   }
   if(viewStale(tok)) return;
 }
 if(_exData) renderExtraction(_exData,null);
}
function exBars(steps, slowest, running){
 const ents=Object.entries(steps||{}).filter(([k])=>k!=='total').sort((a,b)=>b[1]-a[1]);
 if(!ents.length) return '<div class="exdim">no per-stage timings on this run</div>';
 const max=ents[0][1]||1;
 return '<div class="exbars">'+ents.map(([k,v])=>{
   // A stage still in flight is striped and labelled: its number is still growing, and shown
   // plain it reads as a measured duration alongside the finished ones.
   const live=(k===running);
   return '<div class="exbarrow'+(k===slowest?' slow':'')+(live?' live':'')+'">'
     +'<span title="'+escA(k)+'">'+esc(k)+(live?' <i class="exrun">running</i>':'')+'</span>'
     +'<div class="exbartrack"><div class="exbarfill" style="width:'+Math.max(2,Math.round(v/max*100))+'%"></div></div>'
     +'<b>'+exDur(v)+'</b></div>';
 }).join('')+'</div>';
}
// Passes, shared by the finished and the running views. A tier is recorded upstream as ONE
// step beside the stages, so rendered flat it reads as a peer of stage1 — a cheap re-shuffle
// next to the real work — when it is actually a second run of the whole pipeline.
function exPasses(passes, slowest, running){
 if(!passes||!passes.length) return '';
 return passes.map(ps=>{
   // A tier is its OWN pass, so `running` names the pass itself rather than a step inside it —
   // matching only on the inner steps left a tier that was running right now unmarked.
   const isRun=!!running&&(ps.step===running
     ||Object.prototype.hasOwnProperty.call(ps.steps||{},running));
   const inner=Object.keys(ps.steps||{}).length
     ? exBars(ps.steps, ps.key==='first'?slowest:null, running)
     : (ps.composite
        ? '<div class="exdim expassnote2">'
          +(isRun ? 'Still running — its own stage-by-stage breakdown is written when it finishes.'
                  : 'This run predates inner-stage timing for the tier, so only its total is known.')
          +'</div>'
        : '');
   return '<div class="expassblk'+(ps.composite?' composite':'')+(isRun?' running':'')+'">'
     +'<div class="expasshd"><b>'+ps.ordinal+'. '+esc(ps.label)+'</b>'
     +(ps.composite?'<span class="expasstag">re-runs the pipeline</span>':'')
     +(isRun?'<span class="expasstag live">in progress</span>':'')
     +'<span class="expasssec">'+exDur(ps.seconds)+'</span></div>'
     +'<div class="exdim expassnote2">'+esc(ps.note||'')+'</div>'
     +inner+'</div>';
 }).join('');
}
// What the FIRST pass scored. Available the moment the chain escalates — long before the
// document writes a scorecard of its own — so a running document can show real numbers instead
// of nothing at all for however long the tier takes.
function exFirstPass(f){
 if(!f) return '';
 const bits=[
   f.gate?'gate '+scPill(f.gate):'',
   f.worst_score!=null?'worst <b>'+f.worst_score+'</b>':'',
   f.weakest_dimension?'weakest <b>'+esc(f.weakest_dimension)+'</b>':'',
   f.completeness_score!=null?'completeness <b>'+f.completeness_score+'</b>':'',
   f.toc_score!=null?'toc <b>'+f.toc_score+'</b>'
     +(f.toc_status?' <span class="exdim">('+esc(f.toc_status)+')</span>':''):'',
   f.toc_rescuable===false?'<span class="exdim">printed TOC not rescuable</span>':''
 ].filter(Boolean);
 if(!bits.length) return '';
 return '<div class="exdetsec"><h4>First pass scored</h4><div class="exkv">'+bits.join(' · ')
   +'</div><div class="exdim expassnote2">These are the numbers that sent it to the fallback '
   +'chain. The pass now running will be scored separately and adopted only if it beats them.'
   +'</div></div>';
}

function exJobDetailRow(j){
 const key=exJobKey(j), d=EXJOBDETAIL[key];
 const cols=11;
 // The graph leads every drill-down, crashed and running included: it is built from the ledger
 // row alone, so it is on screen before the scorecard GET returns and then fills in the nodes
 // only the scorecard can answer (the pre-flight record, each tier verdict).
 const wrap=inner=>'<tr class="exdet"><td colspan="'+cols+'"><div class="exdetin">'
   +exGraphBlock(j)+inner+'</div></td></tr>';
 // A crashed document has no scorecard at all — that is what "crashed" means here — so the row
 // shows the fault instead of pretending a read failed.
 if(j.status==='error'){
   return wrap('<div class="exdetsec" style="grid-column:1/-1"><h4>Crashed in '
     +esc(j.stage||'an unrecorded stage')+'</h4>'
     +(j.cause?'<div class="exkv"><b>'+esc(EXCAUSE[j.cause]||j.cause)+'</b></div>':'')
     +'<pre class="exerr">'+esc(j.error||'no detail recorded')+'</pre>'
     +'<div class="exdim" style="margin-top:6px">No scorecard was written, so resume already '
     +'redoes this document on the next run of this prefix.</div></div>');
 }
 // A RUNNING document used to show one line: which stage, and for how long. Everything else
 // waited on the scorecard, which is written last — so for the 40 minutes the largest documents
 // take, the screen knew almost nothing while the answer was most wanted. All of this is
 // available before the document finishes: the stages already closed off, the pass structure,
 // and (once the chain escalates) the first pass's full set of scores.
 if(j.live){
   const t2=j.trigger;
   const causes=t2?(t2.causes||[]).map(c=>'<li>'+esc(c.label)
     +((c.score!=null&&c.threshold!=null)
        ? ' <b>'+(+c.score.toFixed(1))+'</b> <span class="exdim">below '+(+c.threshold)+'</span>':'')
     +'</li>').join(''):'';
   // A second pass IN PROGRESS is the case this screen handled worst: the stage ticks back to
   // work the document already finished, and nothing said it was a rescue or what caused it.
   const fbnow=j.fallback
     ? '<div class="exhardbox exfbnow"><b>Second pass — '+esc(j.running_tier_label||'fallback')
       +'.</b> The pipeline is being re-run for this document, MinerU included.'
       +(causes?'<ul class="exchain excauses">'+causes+'</ul>'
               :'<div class="exdim">This worker did not report a reason (it predates trigger '
                +'reporting) — the scorecard will carry it once the document finishes.</div>')
       +'</div>' : '';
   const head='<div class="exdetsec"><h4>Running now</h4>'
     +'<div class="exkv">In <b>'+esc(j.stage||'?')+'</b>'
     +(j.stage_seconds!=null?' for <b>'+exDur(j.stage_seconds)+'</b>':'')+'</div>'
     +'<div class="exkv">'+exDur(j.seconds)+' elapsed · '+(j.pages||'?')+' pages'
     +(j.spp?' · <b>'+j.spp+'s</b>/page so far':'')
     +' · worker '+(j.shard!=null?j.shard:'?')+'</div>'
     +fbnow+'</div>';
   const sofar=(j.passes&&j.passes.length&&Object.keys((j.passes[0]||{}).steps||{}).length)
     ? '<div class="exdetsec"><h4>Where the time has gone</h4>'
       +exPasses(j.passes, j.slowest, j.running_step)
       +'<div class="exdim expassnote2">Stages already finished are measured; the one marked '
       +'<i>running</i> is still counting.</div></div>'
     : '<div class="exdetsec"><h4>Where the time has gone</h4><div class="exdim">No stage has '
       +'completed yet for this document'+(j.stage?' — it is still in '+esc(j.stage):'')+'.'
       +'</div></div>';
   return wrap(head+sofar+exFirstPass(j.first_attempt));
 }
 if(d===undefined||d===null) return wrap('<div class="exdim">Reading its scorecard…</div>');
 if(d.missing) return wrap('<div class="exdim">No scorecard could be read for this document'
   +(d.status===404?' — it may have been cleared, or the run moved.':'.')+'</div>');
 const t=d.timing||{}, fb=d.fallback||{};
 // Grouped by PASS, not listed flat. A tier is recorded upstream as one step sitting beside
 // the stages, so flat it reads as a peer of stage1 — a cheap re-shuffle next to the real
 // work. It is the reverse: a TOC rescue re-runs stages 1–3 INCLUDING MinerU, and on
 // Bermuda__166524 that second pass cost 796.7s against a 797.3s first pass. Listing them
 // side by side is what made "why did a TOC rescue take 13 minutes" unanswerable.
 const passHtml=exPasses(d.passes, t.slowest_step, null);
 const timings='<div class="exdetsec"><h4>Where the time went</h4>'
   +(passHtml||exBars(t.steps, t.slowest_step))
   +'<div class="exkv" style="margin-top:7px">'
   +'total <b>'+exDur(t.seconds)+'</b>'
   +(t.pages?' · '+t.pages+' pages':'')
   +(t.seconds_per_page?' · <b>'+t.seconds_per_page+'s</b>/page':'')
   +(t.seconds_per_crop_page?' · <b>'+t.seconds_per_crop_page+'s</b>/crop-page':'')
   +(t.mineru_crop_pages?' · '+t.mineru_crop_pages+' crop-pages':'')
   +(t.backfilled?' <span class="exdim">(backfilled — window approximate)</span>':'')
   +'</div></div>';
 // Every tier tried, including the ones thrown away. The job row can only say "adopted" or
 // "tried"; this says which ran, in what order, and what happened to each.
 // Which entry WON is carried by adopted_tier, not by a flag on the entry — a real chain reads
 // [{tier:'toc_rescue', status:'TOC rejected: only 0 entries parsed'}, {tier:'mineru_full',
 // worst_before:99.2, worst_after:92.6}]. Scoring both numbers matters: a tier can run, come out
 // WORSE than the first pass and still be the one adopted, and that is worth seeing.
 const chain=(fb.chain||[]).map((c,i)=>{
   const won=c.adopted||(fb.adopted_tier&&c.tier===fb.adopted_tier);
   const skipped=/^skipped|^not needed/.test(String(c.status||''));
   const delta=(c.worst_before!=null&&c.worst_after!=null)
     ? ' <span class="'+(c.worst_after<c.worst_before?'exworse':'exbetter')+'">'
       +c.worst_before+' → '+c.worst_after+'</span>' : '';
   return '<li class="'+(won?'adopted':skipped?'skipped':'')+'"><b>'+(i+1)+'. '
     +esc(c.tier||'?')+'</b> — '+esc(c.status||(won?'adopted':'rejected'))+delta+'</li>';
 }).join('');
 // WHY it escalated — the fact this screen was missing entirely. A fallback with no cause
 // attached is half the story: a document rescued because its printed TOC was lacking is a
 // different problem from one that lost two thirds of its words, and the remedy differs.
 // Preserved from BEFORE any tier rewrote the scorecard: the TOC rescue REPLACES the outline,
 // so the re-scored `toc` dimension reads healthy afterwards and the original failure is gone
 // from the final numbers.
 const trg=d.trigger;
 let why='';
 if(trg){
   const causes=(trg.causes||[]).map(c=>
     '<li>'+esc(c.label)
     +((c.score!=null&&c.threshold!=null)
        ? ' <b>'+(+c.score.toFixed(1))+'</b> <span class="exdim">below '+(+c.threshold)+'</span>':'')
     +(c.detail&&c.detail!==c.label?'<div class="exdim">'+esc(c.detail)+'</div>':'')
     +'</li>').join('');
   const f=trg.first||{};
   const at=[
     f.weakest_dimension?'weakest <b>'+esc(f.weakest_dimension)+'</b>':'',
     f.worst_score!=null?'worst <b>'+f.worst_score+'</b>':'',
     f.completeness_score!=null?'completeness <b>'+f.completeness_score+'</b>':'',
     f.toc_score!=null?'toc <b>'+f.toc_score+'</b>'
       +(f.toc_status?' <span class="exdim">('+esc(f.toc_status)+')</span>':''):'',
     f.toc_rescuable===false?'<span class="exdim">printed TOC not rescuable</span>':''
   ].filter(Boolean).join(' · ');
   why='<div class="exdetsec"><h4>Why it escalated</h4>'
     +(causes?'<ul class="exchain excauses">'+causes+'</ul>'
             :'<div class="exdim">reason not recorded on this run</div>')
     +(at?'<div class="exkv" style="margin-top:6px"><span class="exdim">first pass:</span> '+at+'</div>':'')
     +'</div>';
 }
 // A hard fail is not a bad gate. It says every tier ran and none cleared the bar, so the
 // instruction to a reviewer is "look at this", not "run it again".
 const outcome=fb.hard_fail
   ? '<div class="exhardbox"><b>No tier cleared the bar.</b> '+esc(fb.hard_fail_reason||'')
     +'<div class="exdim">Every tier ran and none produced an acceptable result — this needs a '
     +'human, not another run.</div></div>'
   : (fb.accepted===true?'<div class="exkv exbetter">accepted</div>':'');
 const fbsec='<div class="exdetsec"><h4>Path</h4>'
   +(fb.triggered
     ? (chain?'<ol class="exchain">'+chain+'</ol>':'')
       +'<div class="exkv">adopted <b>'+esc(fb.adopted_tier||'first pass')+'</b>'
       +(t.fallback_seconds?' · cost <b>'+exDur(t.fallback_seconds)+'</b>':'')+'</div>'
       +outcome
     : '<div class="exdim">Not triggered — the first pass scored well enough to keep, so '
       +'stages 1–3 ran exactly once.</div>')
   +'</div>';
 const dims=Object.entries(d.dimensions||{}).map(([k,v])=>{
   const score=(v&&typeof v==='object')?(v.score!=null?v.score:v.value):v;
   const verdict=(v&&typeof v==='object')?(v.gate||v.verdict||''):'';
   return '<div class="exkv">'+esc(k)+' <b>'+esc(String(score==null?'–':score))+'</b>'
     +(verdict?' <span class="exdim">'+esc(verdict)+'</span>':'')+'</div>';
 }).join('');
 // scorecard.json (Scorecard 1) is frozen at stage 3 — the --resume marker, never touched
 // after a stage-4 pass runs. When it DID run, job_detail() already swapped gate/worst_score/
 // dimensions above for Scorecard 2's (scorecard_post_ai.json), and carries Scorecard 1's
 // gate alongside so this can say plainly what changed, rather than a reviewer having to
 // remember the number that used to be here.
 const postAiNote=(d.scored_stage===5)
   ? '<div class="exkv" style="margin-top:5px"><span class="exdim">Scorecard 2 · stage 5 '
     +'(post-AI)</span>'+(d.extraction_gate?' — Scorecard 1 (extraction gate) was '
     +scPill(d.extraction_gate.gate)+(d.extraction_gate.worst_score!=null
       ?' <b>'+d.extraction_gate.worst_score+'</b>':''):'')+'</div>'
   : '';
 // Stage 4's own per-section reasons. The row's tooltip already names WHICH files failed
 // (the ledger carries that with no extra read); this is the one GET job_detail paid to say
 // WHY each one did. d.stage4 is only ever the "ok"/"failed" verdict this document's report
 // itself supports -- a Stage 4 that never wrote a report at all (j.stage4_status
 // 'incomplete') has no report to read a reason out of, so that case is shown from the row's
 // own ledger fields instead of d.stage4, which would just be absent.
 // Deliberately NOT gated on the ledger's stage4_status: d.stage4 is read from the document's
 // OWN stage4_report.json when the row is opened, so this works on every run already in the
 // bucket — including the ones extracted before the worker learned to record the field, whose
 // rows show a dash in the column. Same self-correcting rule `route` follows in
 // extraction_monitor._finished_job: fix what is on disk, not only what is extracted next.
 let aiStage='';
 const s4=d.stage4;
 if(j.stage4_status==='incomplete'){
   aiStage='<div class="exdetsec"><h4>AI stage</h4><div class="exhardbox">'
     +'<b>Stage 4 never finished.</b> 04_stage4_ai exists but no report was ever written — '
     +'crashed, was killed, or is still stuck mid-run.</div></div>';
 } else if((s4&&s4.status==='failed') || j.stage4_status==='failed'){
   const secs=(s4&&s4.failed_sections)||[];
   const items=secs.length
     ? secs.map(s=>'<li><b>'+esc(s.file)+'</b> — '+esc(s.reason)+'</li>').join('')
     : (j.stage4_failed_sections||[]).map(f=>'<li><b>'+esc(f)+'</b></li>').join('');
   const n=secs.length||(j.stage4_failed_sections||[]).length;
   aiStage='<div class="exdetsec"><h4>AI stage</h4><div class="exhardbox">'
     +'<b>'+n+' section(s) did not complete.</b>'
     +'<ul class="exchain">'+items+'</ul></div></div>';
 } else if(s4&&s4.status==='ok'){
   aiStage='<div class="exdetsec"><h4>AI stage</h4>'
     +'<div class="exkv exbetter">every section came back</div></div>';
 }
 const gate='<div class="exdetsec"><h4>Gate</h4>'
   +'<div class="exkv">'+(d.gate?scPill(d.gate):'–')
   +(d.worst_score!=null?' worst <b>'+d.worst_score+'</b>':'')+'</div>'
   +postAiNote
   +(dims||'<div class="exdim">no per-dimension detail on this scorecard</div>')
   +'<div class="exkv" style="margin-top:7px">'+exViewLink(j.product,j.label,j.review)+'</div>'
   +'</div>';
 return wrap(why+timings+fbsec+aiStage+gate);
}
const EX_POLL_MS=15000;
function stopExPoll(){ if(_exTimer){ clearInterval(_exTimer); _exTimer=null; } }
function fmtDur(s){
 if(s==null) return '–';
 if(s<90) return Math.round(s)+'s';
 if(s<5400) return Math.round(s/60)+'m';
 return (s/3600).toFixed(1)+'h';
}
function fmtAgo(epoch){ return epoch? fmtDur(Date.now()/1000-epoch)+' ago' : '–'; }
// THE EVIDENCE BEHIND "running". A working worker beats every 60s (corpus_worker's _beat), so a
// gap past a couple of beats means the screen is repeating a claim it can no longer support.
// Showing the gap even while it is healthy is the point: an operator who knows the normal cadence
// spots a dead worker long before the staleness threshold agrees, which is exactly what did NOT
// happen when a worker sat dead for 14 minutes under a 45-minute threshold, reading `running`.
function exQuiet(q){
 if(q==null) return '';
 return '<span class="exquiet'+(q>120?' bad':'')+'">last beat '+fmtDur(q)+' ago</span>';
}

// Versions are listed from ONE S3 listing (name, workers that reported, last activity). The
// expensive part — percentages, gates, per-worker detail — is a GET per shard and is only paid
// for the version actually selected, so a project with 30 historical runs still opens instantly.
async function loadExtraction(){
 const det=document.getElementById("detail");
 det.innerHTML='<div class="exwrap"><div class="exdim">Loading extraction versions…</div></div>';
 try{
   const r=await authFetch("/api/extraction/runs");
   const d=r.ok?await r.json():{runs:[],configured:false};
   _exRuns=d.runs||[];
   if(!d.configured){
     det.innerHTML='<div class="exwrap"><h2>Extraction</h2><div class="exdim">'
       +'Monitoring is not configured on this environment. Set <code>ACI_EXTRACTION_BUCKET</code> '
       +'(and optionally <code>ACI_EXTRACTION_PREFIX</code>, default <code>corpus</code>).</div></div>';
     return;
   }
   if(!_exRuns.length){
     det.innerHTML='<div class="exwrap"><h2>Extraction</h2><div class="exdim">'
       +'No extraction versions have reported progress yet. A version appears here as soon as its '
       +'first worker starts.</div></div>';
     return;
   }
 }catch(e){
   if(isAbort(e)) return;
   det.innerHTML='<div class="exwrap"><div class="exdim">Could not list versions.</div></div>'; return; }
 if(!_exRun || !_exRuns.some(r=>r.run===_exRun)) _exRun=_exRuns[0].run;   // newest activity first
 await exLoadRetries();
 await refreshExtraction();
 stopExPoll();
 _exTimer=setInterval(refreshExtraction, EX_POLL_MS);
}
function pickExVersion(run){
 // A different run is a different screen's worth of reads — abort the previous run's probes
 // rather than letting them finish and paint over the run just selected.
 newViewScope();
 _exRun=run; _exData=null; EXRETRY.clear();
 // Jobs, filters and any open drill-down belong to the run that was showing. Carrying them over
 // would paint one run's documents under another run's name.
 EXJOBS=null; EXJOBOPEN=null; EXJOBMORE=false; EXJOBQ=''; EXJOBFILTER='all';
 for(const k in EXJOBDETAIL) delete EXJOBDETAIL[k];
 _promos=[]; _promoSel=null; _promoData=null;    // one run's promotions under another's name
 renderExtraction(null,null); exLoadRetries().then(refreshExtraction); }
async function refreshExtraction(){
 if(!document.querySelector("main").classList.contains("extractmode")){ stopExPoll(); return; }
 try{
   const tok=_viewSeq;
   const r=await authFetch("/api/extraction/progress?run="+enc(_exRun));
   if(viewStale(tok)) return;
   if(!r.ok){ renderExtraction(null, 'Could not read this version ('+r.status+').'); return; }
   _exData=await r.json();
   if(viewStale(tok)) return;
   renderExtraction(_exData, null);
   // Fill in which documents actually have a viewer, then re-render. Once per document, cached.
   // This is the slow one — one HEAD per recent document, sequentially — so it is checked both
   // inside the loop (via the abort signal) and again here before it paints.
   // The job list is its own read (the ledgers) and is fetched every poll: it is what makes the
   // in-flight rows move. Ordered after the first paint so the headline never waits on it.
   await exLoadJobs();
   if(viewStale(tok)) return;
   const probed=await exProbeReview(_exData.recent||[]);
   if(viewStale(tok)) return;
   if(probed) renderExtraction(_exData, null);
   await exLoadFailures();
   // Rides the extraction timer (EX_POLL_MS) rather than starting one of its own: a
   // promotion is minutes, the same order as a poll, and one clock is easier to reason
   // about than two. Leaving the tab stops it, because stopExPoll stops that timer.
   await pmLoadForRun();
   if(viewStale(tok)) return;
   await pmRefresh();
   if(viewStale(tok)) return;
   renderExtraction(_exData, null);
 }catch(e){
   if(isAbort(e)) return;                      // the user left; not an error
   renderExtraction(null, 'Error reading this version.');
 }
}
// The run id rides in a data-* attribute and the click is delegated — NEVER an inline onclick.
// This bar was written as onclick="pickExVersion('+JSON.stringify(run)+')", and JSON.stringify
// emits DOUBLE quotes inside a double-quoted attribute:
//
//     onclick="pickExVersion("2026-08-21-02")"
//
// so the browser reads the handler as `pickExVersion(` and everything after the second quote as
// stray attributes. The version picker silently did nothing on click for as long as it existed —
// it only ever showed the newest run, because that is what loadExRuns defaults to. Same failure
// the scorecard table already carries a comment about (see onScTableClick).
function exVerBtn(run, cls, body, title){
 return '<button class="'+cls+'" data-exrun="'+escA(run)+'" data-exact="'+escA(title?'gallery':'pick')+'"'
   +(title?' title="'+escA(title)+'"':'')+'>'+body+'</button>';
}
function exVersionList(){
 return '<div class="exvers">'+_exRuns.map(r=>
   exVerBtn(r.run, 'exver'+(r.run===_exRun?' active':''),
     '<b>'+esc(r.run)+'</b><span>'+(r.workers_reported||0)+' worker'+((r.workers_reported||0)===1?'':'s')
     +' · '+fmtAgo(r.last_activity)+'</span>')).join('')
   // Straight from the run list into the run's own scorecard. Reviewing a run used to mean
   // opening its documents one row at a time from the progress table; this is the same output
   // seen whole, product by product, without publishing it first.
   +(_exRun? exVerBtn(_exRun, 'exver exverlink',
       '<b>📚 Browse run</b><span>'+esc(_exRun)+' in Doc Gallery</span>',
       'Browse this run\'s extracted documents in the Doc Gallery') : '')
   +'</div>';
}
// ---- Promotion (Extraction tab) ------------------------------------------------------
// What happened to this run AFTER it finished — a question the Extraction screen could not
// answer at all. Deliberately not a fourth tab: this is renderExtraction with different
// nouns, and a tab of its own would duplicate exVersionList, exQuiet, fmtDur, the
// _viewSeq/viewStale abort discipline and the stopExPoll lifecycle.
let _promos=[], _promoSel=null, _promoData=null, _promoBusy=false;
// One place the two ids can be forgotten — galQ's pattern, for the same reason.
function pmQ(run, pid){ return '?run='+enc(run)+'&promotion='+enc(pid); }
async function pmLoadForRun(){
 if(!_exRun){ _promos=[]; return; }
 try{
   const r=await authFetch("/api/promotion/runs?run="+enc(_exRun));
   const d=r.ok? await r.json() : {promotions:[]};
   _promos=d.promotions||[];
   if(!_promoSel || !_promos.some(x=>x.promotion===_promoSel))
     _promoSel=_promos.length? _promos[0].promotion : null;
 }catch(e){ if(!isAbort(e)) _promos=[]; }
}
async function pmRefresh(){
 if(!_promoSel){ _promoData=null; return; }
 try{
   const tok=_viewSeq;
   const r=await authFetch("/api/promotion/progress"+pmQ(_exRun,_promoSel));
   if(viewStale(tok)) return;
   _promoData = r.ok? await r.json() : null;
 }catch(e){ if(!isAbort(e)) _promoData=null; }
}
function pmStateChip(st){
 const cls={running:'running',complete:'complete',failed:'stalled',stalled:'stalled',
   stopping:'stopping',requested:'idle',idle:'idle'}[st]||'idle';
 return '<span class="exstate exs-'+cls+'">'+esc(st||'idle')+'</span>';
}
function pmStages(rows){
 return '<div class="pmstages">'+(rows||[]).map(r=>{
   const n = r.total? (r.done+' / '+r.total) : (r.state==='complete'?'done':'–');
   return '<div class="pmstage s-'+esc(r.state)+'"><b>'+esc(r.label)+'</b>'
     +'<span>'+esc(r.detail)+'</span>'
     +'<span>'+esc(n)+(r.failed?' · '+r.failed+' failed':'')
     +(r.skipped?' · '+r.skipped+' skipped':'')+'</span></div>';
 }).join('')+'</div>';
}
function pmRecent(rows){
 if(!(rows||[]).length) return '';
 return '<div class="exsub">Recently promoted</div><table class="extab">'
  +'<tr><th>document</th><th>gate</th><th>objects</th><th>took</th><th></th></tr>'
  +rows.map(r=>'<tr><td>'+esc(r.job||'')+'</td>'
    +'<td>'+(r.gate?scPill(r.gate):'')+'</td>'
    +'<td>'+(r.objects!=null?r.objects:'–')+'</td>'
    +'<td class="exdim">'+(r.seconds!=null?fmtDur(r.seconds):'–')+'</td>'
    +'<td class="exdim">'+esc(r.error||r.status||'')+'</td></tr>').join('')
  +'</table>';
}
function pmBlock(){
 if(!CFG.promotion && !_promos.length) return '';
 let h='<div class="pmwrap"><div class="exhead"><h2 style="font-size:15px">Promotion</h2>'
  +'<span class="exdim">'+(_promos.length? _promos.length+' promotion'
     +(_promos.length===1?'':'s')+' of this run' : 'this run has not been promoted')+'</span>'
  +(CFG.promotion? '<button class="pmbtn" data-pmact="plan" data-pmrun="'+escA(_exRun)
      +'">Plan a promotion…</button>' : '')+'</div>';
 if(_promos.length>1){
   h+='<div class="exvers">'+_promos.map(x=>'<button class="exver'
     +(x.promotion===_promoSel?' active':'')+'" data-pmact="pick" data-pmid="'+escA(x.promotion)
     +'"><b>'+esc(x.promotion)+'</b><span>'+esc(x.state||'')+' · '
     +fmtAgo(x.updated_at)+'</span></button>').join('')+'</div>';
 }
 const d=_promoData;
 if(!d){ return h+'<div class="pmnote">'+(_promoSel? 'Loading '+esc(_promoSel)+'…'
   : 'Nothing promoted yet.')+'</div></div>'; }
 h+='<div class="exhead" style="margin-top:10px">'+pmStateChip(d.state)
   +'<b>'+esc(d.version||d.promotion||'')+'</b>'
   +(d.percent!=null? '<span class="exdim">'+d.percent+'%</span>':'')
   // The claim and its evidence arrive together — the exQuiet lesson, applied here.
   +(d.state==='running'&&d.quiet_seconds>60
      ? '<span class="exquietchip">nothing written for '+fmtDur(d.quiet_seconds)+'</span>':'')
   +(d.state==='stalled'&&d.quiet_seconds!=null
      ? '<span class="exdim">nothing written for '+fmtDur(d.quiet_seconds)
        +' — the promotion stopped before finishing</span>':'')
   +'</div>';
 if(d.state==='requested'){
   h+='<div class="pmwarn">Requested'+(d.requested&&d.requested.by? ' by '
     +esc(d.requested.by):'')+' — <b>waiting for a promoter to pick it up</b>. The service '
     +'records the request; it does not start the job.</div>';
 }
 h+='<div class="exbar"><i style="width:'+Math.min(100,d.percent||0)+'%"></i></div>'
   +pmStages(d.stages);
 const c=d.counts||{};
 h+='<div class="extiles">'+[
    ['copied', c.copied||0], ['skipped', c.skipped||0], ['failed', c.failed||0],
    ['objects', c.objects||0],
    ['moved', c.bytes? (c.bytes/1e6).toFixed(1)+' <span class="exdim">MB</span>':'0'],
    ['elapsed', fmtDur(d.elapsed_seconds)], ['eta', fmtDur(d.eta_seconds)],
    ['docs/min', d.docs_per_minute!=null? d.docs_per_minute : '–'],
   ].map(([k,x])=>'<div class="extile"><b>'+x+'</b><span>'+k+'</span></div>').join('')+'</div>';
 if(d.error) h+='<div class="pmwarn">'+esc(d.error)+'</div>';
 // A promotion screen that says "done" is lying about the one thing the operator actually
 // asked: whether it is searchable. Three different endings, and they are three different
 // statements — so the screen makes them, rather than leaving "complete" to be interpreted.
 const browse='<button class="pmbtn" data-pmact="browse" data-pmver="'
   +escA(d.version||'')+'">Browse this version</button>';
 if(d.state==='complete'&&!d.pointer_flipped)
   h+='<div class="pmwarn"><b>Staged, not live.</b> These documents are in <code>'
     +esc(d.target_prefix||'')+'</code> and readable here, but <code>index/latest</code> is '
     +'untouched'+(d.with_index?'':' and no vectors have been built')+' — nothing is '
     +'searchable in Search Mode or AI Mode yet. '+browse+'</div>';
 else if(d.state==='complete')
   // The pointer moved, so this IS the live version for any pod created from now on. But
   // /data is a read-only mount whose prefix is fixed at pod creation, so a RUNNING pod
   // still has the previous version's content.json and drops the new regions from results
   // (registry logs BundleUnavailable per region). Saying "live" without that is the 13 Aug
   // configuration described as a success.
   h+='<div class="pmwarn"><b>Cut over — pods still need replacing.</b> '
     +'<code>index/latest</code> now points at <code>'+esc(d.version||'')+'</code>'
     +(d.vectors_loaded? ' and the vector store has been reloaded':
        ' but the vector store was NOT reloaded, so search still answers from the previous '
        +'index\'s vectors')+'. A running pod keeps the mount it started with, so newly '
     +'promoted regions stay absent from results until it is recreated '
     +'(<code>rollout restart</code>, or <code>docker compose up -d --force-recreate '
     +'core-index</code>). '+browse+'</div>';
 if(CFG.promotion && (d.state==='running'||d.state==='stopping'))
   h+='<div class="pmnote"><button class="pmbtn" data-pmact="stop" data-pmid="'
     +escA(_promoSel)+'"'+(d.state==='stopping'?' disabled':'')+'>Stop after the current '
     +'document</button></div>';
 return h+pmRecent(d.recent)+'</div>';
}
async function pmOpenPlan(run){
 if(_promoBusy) return; _promoBusy=true;
 pmModal('<h3>Planning…</h3><div class="pmnote">Walking the run — around 20 seconds the first '
   +'time.</div>');
 try{
   const r=await authFetch("/api/promotion/plan?run="+enc(run));
   if(!r.ok){ pmModal('<h3>Could not plan</h3><div class="pmnote">'+esc('HTTP '+r.status)
     +'</div>'+pmActs()); return; }
   pmPlanModal(await r.json(), run);
 }catch(e){ if(!isAbort(e)) pmModal('<h3>Could not plan</h3>'+pmActs()); }
 finally{ _promoBusy=false; }
}
function pmPlanModal(e, run){
 const prods=Object.entries(e.by_product||{}).map(([p,g])=>
   '<tr><td>'+esc(p)+'</td><td class="n">'+Object.entries(g).map(([k,v])=>v+' '+esc(k))
     .join(', ')+'</td></tr>').join('');
 const held=Object.entries(e.excluded||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>
   '<tr><td>'+esc(k)+'</td><td class="n">'+v+'</td></tr>').join('');
 pmModal('<h3>Promote run '+esc(run)+'</h3>'
  +'<div class="pmnote">Into <code>'+esc(e.target_prefix||'')+'</code>'
  +(e.index_latest? ' · index/latest is <code>'+esc(e.index_latest)+'</code>':'')+'</div>'
  +'<table><tr><td><b>'+(e.eligible||0)+' documents</b> would be promoted</td>'
  +'<td class="n">'+(e.objects||0)+' objects · '+((e.bytes||0)/1e6).toFixed(1)+' MB</td></tr>'
  +prods+'</table>'
  +'<div class="pmnote">Products: '+esc((e.products||[]).join(', '))+' · gates: '
  +esc((e.gates||[]).join(', '))+'</div>'
  +(held? '<div class="pmnote">Held back</div><table>'+held+'</table>' : '')
  +'<div class="pmnote">This publishes to a NEW index version and does not touch '
  +'<code>index/latest</code> — nothing becomes searchable until Phase 2 runs.</div>'
  +pmActs(e.eligible? '<button class="pmbtn primary" data-pmact="go" data-pmrun="'+escA(run)
    +'">Promote '+(e.eligible||0)+' documents</button>' : ''));
}
function pmActs(extra){ return '<div class="pmacts">'+(extra||'')
  +'<button class="pmbtn" data-pmact="close">Close</button></div>'; }
function pmModal(inner){
 let m=document.getElementById("pmmodal");
 if(!m){ m=document.createElement("div"); m.id="pmmodal"; m.className="pmmodal";
   document.body.appendChild(m); }
 m.innerHTML='<div class="pmcard">'+inner+'</div>';
}
function pmCloseModal(){ const m=document.getElementById("pmmodal"); if(m) m.remove(); }
async function pmRequest(run){
 if(_promoBusy) return; _promoBusy=true;
 try{
   const r=await authFetch("/api/promotion/request",
     {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({run:run})});
   const d=await r.json().catch(()=>({}));
   // 409 is a REFUSAL with a reason the operator can act on, not an error to shout about.
   if(r.status===409){ pmModal('<h3>Not queued</h3><div class="pmnote">'
     +esc(d.reason||'a promotion is already under way')+'</div>'+pmActs()); return; }
   if(!r.ok){ pmModal('<h3>Could not request</h3><div class="pmnote">'
     +esc(d.detail||('HTTP '+r.status))+'</div>'+pmActs()); return; }
   pmCloseModal(); _promoSel=d.promotion||null;
   await pmLoadForRun(); await pmRefresh(); renderExtraction(_exData,null);
 }catch(e){ if(!isAbort(e)) pmModal('<h3>Could not request</h3>'+pmActs()); }
 finally{ _promoBusy=false; }
}
async function pmStop(pid){
 try{
   await authFetch("/api/promotion/stop",{method:"POST",
     headers:{"Content-Type":"application/json"},
     body:JSON.stringify({run:_exRun,promotion:pid})});
   await pmRefresh(); renderExtraction(_exData,null);
 }catch(e){ if(!isAbort(e)) return; }
}
// Delegated, with the ids in data-* attributes — NEVER an inline onclick carrying a run id.
// See exVerBtn: JSON.stringify emits double quotes inside a double-quoted attribute, which
// left the version picker silently dead for as long as it existed.
document.addEventListener("click",e=>{
 const b=e.target.closest&&e.target.closest("[data-pmact]");
 if(!b) return;
 const act=b.getAttribute("data-pmact");
 if(act==="close") pmCloseModal();
 else if(act==="plan") pmOpenPlan(b.getAttribute("data-pmrun"));
 else if(act==="go") pmRequest(b.getAttribute("data-pmrun"));
 else if(act==="stop") pmStop(b.getAttribute("data-pmid"));
 else if(act==="browse") openGalleryVersion(b.getAttribute("data-pmver"));
 else if(act==="pick"){ _promoSel=b.getAttribute("data-pmid"); _promoData=null;
   renderExtraction(_exData,null); pmRefresh().then(()=>renderExtraction(_exData,null)); }
});

function renderExtraction(p, err){
 // The last line of defence: a response parsed just before the abort is still in hand, and
 // painting it is what put extraction results on top of the Doc Gallery.
 if(!document.querySelector("main").classList.contains("extractmode")) return;
 const det=document.getElementById("detail");
 // The whole screen is re-innerHTML'd every poll, which would yank the caret out of the job
 // filter mid-word every 15 seconds. Remember where it was and put it back after the paint.
 const _fb=document.getElementById("exjobq");
 const _fsel=(_fb&&document.activeElement===_fb)?[_fb.selectionStart,_fb.selectionEnd]:null;
 if(_fsel) setTimeout(()=>{ const b=document.getElementById("exjobq");
   if(b){ b.focus(); try{ b.setSelectionRange(_fsel[0],_fsel[1]); }catch(e){} } },0);
 const head='<div class="exwrap"><div class="exhead"><h2>Extraction</h2>'
   +'<span class="exdim">'+_exRuns.length+' version'+(_exRuns.length===1?'':'s')+'</span></div>'
   +exVersionList();
 if(err){ det.innerHTML=head+'<div class="exdim">'+esc(err)+'</div></div>'; return; }
 if(!p){ det.innerHTML=head+'<div class="exdim">Loading '+esc(_exRun)+'…</div></div>'; return; }
 const v=p.version||{};
 // The VERSION bar is the headline: how much of the corpus this version has extracted, counting
 // what earlier attempts finished. The attempt figure sits underneath, because after a resume the
 // two diverge and only the version answers "is this version ready to publish".
 const vpct=(v.percent==null)?null:v.percent;
 const tiles=[
   ['documents (version)', (v.docs_done!=null? v.docs_done : p.done)
      +' <span class="exdim">/ '+(v.docs_total!=null? v.docs_total : p.jobs_total)+'</span>'],
   ['this attempt', (p.done||0)+' <span class="exdim">/ '+(p.jobs_total||0)+' docs · '+(p.percent||0)+'%</span>'],
   ['workers live', (p.shards_live||0)+' <span class="exdim">/ '+(p.shards||[]).length+'</span>'
      +exQuiet(p.quiet_seconds)],
   ['elapsed', fmtDur(p.elapsed_seconds)],
   ['eta (attempt)', fmtDur(p.eta_seconds)],
   ['pages/hour', p.pages_per_hour? p.pages_per_hour.toLocaleString() : '–'],
   ['cloned duplicates', (p.cloned||0)],
   ['failures', (p.failed||0)],
   // From the jobs list, not the shard summaries progress() reads — the only place the
   // per-document AI spend and stage-4 duration exist. None (shown as –) until EXJOBS has
   // loaded, or on a run where nothing has run stage 4 yet: distinct from "0s"/"$0.00 spent".
   ['ai time so far', (EXJOBS&&EXJOBS.stage4_seconds_total!=null) ? fmtDur(EXJOBS.stage4_seconds_total) : '–'],
   ['ai cost so far', (EXJOBS&&EXJOBS.cost_usd_total!=null) ? '$'+EXJOBS.cost_usd_total.toFixed(2) : '–'],
 ].map(([k,x])=>'<div class="extile"><b>'+x+'</b><span>'+k+'</span></div>').join('');
 const gates=Object.entries(p.gates||{}).sort((a,b)=>b[1]-a[1]);
 const shardRows=(p.shards||[]).map(s=>{
   // The STAGE matters as much as the document: one MinerU subprocess can hold a worker for
   // 20+ minutes reporting nothing, so "stage2_mineru for 18m" is a healthy worker while the
   // same elapsed time in stage1 is not.
   const cur=s.current
     ? esc(s.current.product||'')+' <span class="exdim">'+esc(s.current.label||'')
       +' · '+(s.current.pages||0)+'pp · '+fmtDur(s.current_seconds)
       +(s.current.stage?' · '+esc(s.current.stage):'')+'</span>'
     : (s.state==='interrupted' && s.interrupted_document
        ? '<span class="exdim">cut off in '+esc(s.interrupted_document)
          +(s.interrupted_stage?' · '+esc(s.interrupted_stage):'')+'</span>'
        : '<span class="exdim">'+(s.state==='complete'?'finished':'—')+'</span>');
   const cls=s.state==='running'?'running':s.state==='complete'?'complete'
     :s.state==='stale'?'stale':s.state==='interrupted'?'interrupted':'idle';
   return '<tr><td><b>'+s.shard+'</b></td>'
     +'<td><span class="exstate exs-'+cls+'">'+esc(s.state)+'</span></td>'
     +'<td>'+s.done+' <span class="exdim">/ '+s.jobs_total+'</span></td>'
     +'<td>'+(s.pages_done||0).toLocaleString()+' <span class="exdim">/ '+(s.pages_total||0).toLocaleString()+'</span></td>'
     +'<td>'+cur+'</td><td class="exdim">'+fmtDur(s.idle_seconds)+' ago</td>'
     +'<td>'+(s.failed?('<b>'+s.failed+'</b>'):'0')+'</td></tr>';
 }).join('');
 // A finished document links to the viewer the Doc Library already builds — the md⇄PDF pair and
 // the inspection page with its scorecard. No second content browser: the same artefact a
 // reviewer opens from the gallery, reached from the row that says it just finished. The link
 // appears only once the document is published there; EXGAL maps doc id -> slug.
 const recent=(p.recent||[]).map(r=>
   '<tr><td>'+esc(r.product||'')+'</td><td>'+esc(r.label||'')+'</td>'
   // An interrupted row has no verdict to show — the document never reached the gate — so it
   // gets the neutral pill rather than being coloured as though it had been scored and lost.
   +'<td>'+(r.gate?scPill(r.gate==='error'?'fail':r.gate==='interrupted'?'ungated':r.gate):'')
   +(r.gate==='interrupted'?' <span class="exdim">interrupted</span>':'')
   +(r.cloned?' <span class="exdim">clone</span>':'')
   +(r.cause?' <span class="exdim">'+esc(EXCAUSE[r.cause]||r.cause)+'</span>':'')+'</td>'
   +'<td>'+(r.worst==null?'<span class="exdim">–</span>':r.worst)+'</td>'
   +'<td class="exdim">'+(r.seconds!=null?fmtDur(r.seconds):'')+'</td>'
   +'<td>'+exViewLink(r.product, r.label, r.review)+'</td>'
   +'<td class="exdim">'+esc(r.error||'')+'</td></tr>').join('');
 det.innerHTML=head
   +'<div class="exhead" style="margin-top:14px"><span class="exstate exs-'+esc(p.state||'idle')+'">'
   +esc(p.state||'idle')+'</span><b>'+esc(_exRun)+'</b>'
   // A run still claiming to be live while nothing has reported for minutes is the case this
   // screen got wrong: it asserted `running` with total confidence and buried the silence in a
   // table column. The headline now carries it, so the claim and its evidence arrive together.
   +(p.state==='running'&&p.quiet_seconds>120
       ? '<span class="exquietchip">no beat for '+fmtDur(p.quiet_seconds)+'</span>' : '')
   // A stalled run is the one case where "when did this stop" is the first question, so the
   // header answers it rather than leaving the reader to work it back from the worker table.
   +(p.state==='stalled'&&p.quiet_seconds!=null
       ? '<span class="exdim">no worker has reported for '+fmtDur(p.quiet_seconds)
         +' — the run stopped before finishing</span>' : '')
   +'<span class="exdim">'+(vpct==null
       ? 'version progress unavailable for this run (extracted before version totals were recorded)'
       : vpct+'% of the corpus extracted'
         +(v.resumed_from? ' · '+v.resumed_from+' carried over from earlier attempts':''))
   +'</span></div>'
   +'<div class="exbar"><i style="width:'+Math.min(100,vpct==null?(p.percent||0):vpct)+'%"></i></div>'
   +(p.superseded_workers? '<div class="exdim">'+p.superseded_workers+' progress record(s) from an '
       +'earlier worker layout of this version are being ignored.</div>':'')
   +'<div class="extiles">'+tiles+'</div>'
   +(gates.length?('<div class="exsub">Gates so far</div><div class="extiles">'
      +gates.map(([g,n])=>'<div class="extile"><b>'+n+'</b><span>'+esc(g)+'</span></div>').join('')+'</div>'):'')
   +'<div class="exsub">Workers</div>'
   +'<table class="extab"><tr><th>shard</th><th>state</th><th>docs</th><th>pages</th>'
   +'<th>current document</th><th>last update</th><th>fail</th></tr>'+shardRows+'</table>'
   +exFunnelBlock()
   +exJobsBlock()
   +exFailuresBlock()
   +(recent?('<div class="exsub">Recently finished</div><table class="extab">'
      +'<tr><th>product</th><th>document</th><th>gate</th><th>worst</th><th>took</th>'
   +'<th>viewer / retry</th><th></th></tr>'
      +recent+'</table>'):'')
   +pmBlock()
   +'</div>';
}

// ---- Doc Gallery: browse extracted memos; open the md⇄PDF viewer (proxy-streamed by the backend) ----
const GATE_COL={pass:'g',review:'a',fail:'r'};
function galBadges(d){
 const b=[];
 if(d.gate) b.push('<span class="gbadge '+(GATE_COL[d.gate]||'')+'" title="Validation scorecard verdict">'+esc((d.gate||'').toUpperCase())+(d.worst_score!=null?' '+Math.round(d.worst_score):'')+'</span>');
 const w=d.word_delta;
 if(w!=null){
   if(w<=-5) b.push('<span class="gbadge r">−'+Math.abs(w)+'% missing</span>');
   else if(Math.abs(w)<1) b.push('<span class="gbadge g">Δ '+w+'%</span>');
   else if(w<0) b.push('<span class="gbadge a">'+w+'% short</span>');
   else b.push('<span class="gbadge a">Δ +'+w+'% struct</span>');
 }
 b.push('<span class="gbadge">'+(d.pages!=null?d.pages:'?')+'pp</span>');
 if(d.headings_total) b.push('<span class="gbadge '+(d.headings_matched===d.headings_total?'g':'a')+'">'+d.headings_matched+'/'+d.headings_total+' headings</span>');
 if(d.tables) b.push('<span class="gbadge">'+d.tables+' tables</span>');
 if(d.snapshots) b.push('<span class="gbadge snap">'+d.snapshots+' snapshots</span>');
 if(d.files) b.push('<span class="gbadge">'+d.files+' files</span>');
 const ai=galAiStageBadge(d);
 if(ai) b.push(ai);
 return b.join('');
}
let _galDocs=[], _galMode='all', _galView='table', _galTree=null;
// null = the published gallery (the current folder), which is the default and what every
// deployed environment shows. A run id points the same screen at an extraction run read in
// place, so a run can be reviewed BEFORE anyone decides to publish it.
let _galRun=null, _galListError=null;
// A promotion publishes to index/<version>/doc-gallery and deliberately does NOT flip
// index/latest, so a staged version is readable but not live. This is how it gets reviewed
// before cutover — without it the only readable gallery is the one already serving.
let _galVersion=null;
// Every gallery fetch goes through this, so there is one place the run can be forgotten.
function galQ(extra){ const q=[]; if(extra) q.push(extra); if(_galRun) q.push('run='+enc(_galRun));
 if(_galVersion) q.push('version='+enc(_galVersion));
 return q.length? '?'+q.join('&') : ''; }
function _galReset(){ _galTree=null; _galDocs=[];
 for(const k in _scCache) delete _scCache[k];      // scorecards are per-run: never carry them over
 _galOpen.clear(); }
function openGalleryRun(run){ newViewScope(); _galReset(); _galRun=run||null; _galVersion=null;
 setMode('gallery'); galLoadRetries().then(loadGallery); }
// Stage 4's own per-section health, read straight from this run's own stage4_report.json
// (doc_gallery.RunDocStore._build) rather than the Extraction tab's ledger: the ledger only
// knows about documents the corpus worker itself ran stage 4 for, so a document stage 4 was
// re-run on directly (scripts/stage4_run.py, out-of-band) has a real report on S3 with no
// ledger row to match — the ledger would silently under-report an otherwise fully-processed
// run. `d.stage4` rides the SAME tree/list response every other column reads, at no extra
// network cost to this screen.
// failed_sections is [{file, reason}, ...] (ai_postprocess.stage4_failed_sections_from_report),
// not the ledger's flat filename list exAiStageCell reads — joining it as strings here would
// print "[object Object]" for every entry.
function galAiChip(status, failed){
 if(!status) return '<span class="exdim">–</span>';
 if(status==='ok') return '<span class="scpill scp-pass" title="Stage 4: every section came back">AI ok</span>';
 if(status==='incomplete') return '<span class="scpill scp-fail" title="Stage 4 started but never wrote '
   +'its report — crashed, was killed, or is still stuck mid-run">AI stuck</span>';
 const items=failed||[];
 const tip=items.length
   ? 'Stage 4 did not finish:\n'+items.map(it=>'• '+(it.file||'?')+(it.reason?' — '+it.reason:'')).join('\n')
   : 'Stage 4 did not finish';
 return '<span class="scpill scp-fail" title="'+escA(tip)+'">'+(items.length||'')
   +' section'+(items.length===1?'':'s')+' failed</span>';
}
// The pill is a COUNT, scanned down the whole table; this is what a reviewer actually opens
// the row for — which section, and why — so it is printed directly rather than left behind a
// hover tooltip. Table only: a card's badges are a flex-wrap of inline pills (galBadges), and
// a multi-line list per card would break that row instead of just widening it.
function galAiFailList(failed){
 const items=failed||[];
 if(!items.length) return '';
 const trunc=(s,n)=> (s && s.length>n) ? s.slice(0,n-1)+'…' : (s||'');
 return '<div class="scmuted" style="font-size:11px;line-height:1.5;margin-top:3px">'
   +items.map(it=>'<div title="'+escA(it.reason||'')+'"><b>'+esc(it.file||'?')+'</b>'
     +(it.reason? ' — '+esc(trunc(it.reason,70)) : '')+'</div>').join('')+'</div>';
}
function galAiStageCell(stage4){
 const failed=(stage4&&stage4.status==='failed')? stage4.failed_sections : null;
 return '<td>'+galAiChip(stage4&&stage4.status, stage4&&stage4.failed_sections)
   +(failed? galAiFailList(failed) : '')+'</td>';
}
// Blank (not a "–" chip) when Stage 4 does not apply: unlike the table, a card wall only
// shows a badge that HAS something to say, the same rule galBadges already follows for
// every other badge on the card.
function galAiStageBadge(d){
 if(!d.stage4 || !d.stage4.status) return '';
 return galAiChip(d.stage4.status, d.stage4.failed_sections);
}
// Retry marking, ported from the Extraction tab onto the same run/product/label shape: Doc
// Gallery can browse a live run's own documents, so the same "redo just this one" marker
// applies here. Only offered while a run is open — a published gallery has no run to mark.
const GALRETRY=new Set();
async function galLoadRetries(){
 if(!_galRun){ GALRETRY.clear(); return; }
 try{
   const r=await authFetch('/api/extraction/retries?run='+enc(_galRun));
   if(!r.ok) return;
   GALRETRY.clear();
   for(const k of ((await r.json()).queued||[])) GALRETRY.add(k);
 }catch(e){ /* the marker state is a convenience */ }
}
async function galRetry(product, label){
 if(!_galRun) return;
 if(!confirm('Re-extract '+label+' on the next run of '+_galRun+'?')) return;
 try{
   // signal:null — a screen change must NEVER cancel this, same reasoning as exRetry: the
   // server may already have applied the marker while the UI reports nothing.
   const r=await authFetch('/api/extraction/retry',{method:'POST', signal:null,
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({run:_galRun, product:product, label:label})});
   if(r.status===409){
     const d=await r.json().catch(()=>({}));
     if(confirm((d.reason||'Already retried once.')+'\n\nRetry anyway?')){
       const f=await authFetch('/api/extraction/retry',{method:'POST',
         headers:{'Content-Type':'application/json'},
         signal:null,
         body:JSON.stringify({run:_galRun, product:product, label:label, force:true})});
       if(!f.ok){ alert('Could not queue it ('+f.status+')'); return; }
     } else return;
   } else if(!r.ok){ alert('Could not queue it ('+r.status+')'); return; }
   GALRETRY.add(product+'/'+label);
   _galView==='table' ? renderScTable() : renderGallery();
 }catch(e){ alert('Could not queue it.'); }
}
function galRetryCell(product, label){
 if(!_galRun) return '';
 const rk=product+'/'+label;
 return GALRETRY.has(rk)
   ? ' · <span class="scmuted">retry queued</span>'
   : ' · <a data-retry-product="'+escA(product)+'" data-retry-label="'+escA(label)+'">retry</a>';
}
function galCardRetryLink(d){
 if(!_galRun) return '';
 const label=(d.region||'')+'__'+(d.doc_id||'');
 const rk=d.product+'/'+label;
 if(GALRETRY.has(rk)) return '<span style="margin-left:12px" class="galsub">retry queued</span>';
 const pj=JSON.stringify(d.product).replace(/"/g,'&quot;'), lj=JSON.stringify(label).replace(/"/g,'&quot;');
 return '<a style="margin-left:12px" onclick="event.stopPropagation();galRetry('+pj+','+lj+');return false">retry</a>';
}
function openGalleryVersion(version){ newViewScope(); _galReset();
 _galVersion=version||null; _galRun=null; setMode('gallery'); loadGallery(); }
function galRunBanner(err){
 if(_galVersion) return galVersionBanner(err);
 if(!_galRun) return '';
 // An unreadable run and a run that has finished nothing both come back empty. Saying
 // "no documents yet" for the first would send someone looking at the wrong thing.
 const body = err
   ? 'Could not read extraction run <b>'+esc(_galRun)+'</b> — <span class="galrunerr">'+esc(err)+'</span>. '
     +'This is not "no documents": the listing itself failed.'
   : 'Showing extraction run <b>'+esc(_galRun)+'</b> — read straight from the run, not the '
     +'published Doc Gallery. Documents appear as they finish; a document with no scorecard yet '
     +'is not listed.';
 return '<div class="galrun'+(err?' galrunbad':'')+'">'+body
  +' <button class="galfbtn" onclick="openGalleryRun(null)">← back to the published gallery</button>'
  // The decision to promote is made HERE, looking at the run's own scorecard — that is what
  // this screen is for. Gated on the server flag so it is absent rather than broken.
  +(CFG.promotion? ' <button class="pmbtn primary" data-pmact="plan" data-pmrun="'+escA(_galRun)
      +'">Promote this run →</button>' : '')
  +'</div>';
}
// A staged version and a run look similar and mean very different things: a run is not
// published anywhere, a staged version IS published but nothing points at it. Both are
// "not live", and the banner has to say which kind of not-live this is.
function galVersionBanner(err){
 const body = err
   ? 'Could not read gallery version <b>'+esc(_galVersion)+'</b> — <span class="galrunerr">'
     +esc(err)+'</span>.'
   : 'Showing <b>'+esc(_galVersion)+'</b> — a STAGED gallery version. It is published to S3 '
     +'but <b>index/latest</b> does not point at it, so nothing here is live to search and '
     +'the deployed gallery is unchanged.';
 return '<div class="galrun'+(err?' galrunbad':'')+'">'+body
  +' <button class="galfbtn" onclick="openGalleryVersion(null)">← back to the published gallery</button>'
  +'</div>';
}
function galIsMineru(d){ return (d.slug||'').endsWith('-mineru') || (d.region||'').includes('(MinerU)'); }
function setGalMode(m){ _galMode=m; loadGallery(); }
function setGalView(v){ _galView=v; loadGallery(); }
// The gallery LANDS on the scorecard table; the card wall is the alternate view.
function loadGallery(){ return _galView==='table' ? loadGalleryTree() : loadDocGallery(); }

// ---- Extraction scorecard table: product / jurisdiction / document, every level
// verdicted by its WEAKEST document (an average hides the one doc you must open).
// The backend does the filtering AND the roll-up (/api/doc-gallery/tree?variant=),
// so a filtered view's counts are the counts of what it shows.
const _galOpen=new Set();      // expanded product / jurisdiction rows
const _scCache={};             // slug -> scorecard JSON (or null when there is none)
function scPill(v){ return '<span class="scpill scp-'+v+'">'+v+'</span>'; }
function scNum(v){ return (v==null)?'<span class="scmuted">–</span>':v; }
// Fail COUNT plus its share of the folder's documents. A raw count reads the same for
// 3-of-6 as for 3-of-58; the share is what says which products are in real trouble.
// Rate, not count, decides the colour for the same reason.
function scFail(n, total){
 if(!total) return scNum(n);
 if(!n) return '<span class="scmuted">0</span>';
 const pct = Math.round(100*n/total);
 return n+' <span class="scfp'+(pct>=50?' scfpbad':'')+'">'+pct+'%</span>';
}
function scScore(v){
 if(v==null) return '<span class="scmuted">–</span>';
 const c=v>=90?'#15803d':v>=70?'#b45309':'#c1342d';
 return '<span class="scscore" style="color:'+c+'">'+v.toFixed(1)+'</span>';
}
// What the extraction produced, on the row itself: the columns carry the VERDICT,
// this carries the shape of the document behind it, so a row can be judged without
// opening anything. Deliberately NOT badges — five pills per dimension plus a pill
// per statistic wrapped to three ragged lines and read as noise. Instead: one fixed
// -order score strip (same five slots on every row, so the columns line up down the
// table) and one quiet facts line. Colour is spent only on numbers worth looking at.
// Structure NOT built the way every other tree here was — provenance a reader of the
// score needs. Two tiers can rescue a document and they are not equally trustworthy, so
// the badge names which one did it rather than just saying "rescued":
//   TOC rescued     — the outline came from the document's own printed contents page
//   MinerU rescued  — a visual model re-parsed the whole document and supplied the
//                     hierarchy too, so the tree is structured unlike its neighbours
function scRescued(d){
 const r=d.rescued; if(!r) return '';
 if(r.method==='mineru-full'){
   const b=[r.was?'was '+r.was:null, r.was_score!=null?'worst '+r.was_score:null,
     r.was_completeness!=null?r.was_completeness+' completeness':null,
     r.toc_rescue?'TOC rescue: '+r.toc_rescue:null].filter(Boolean).join(', ');
   return ' <span class="scresc" title="Deterministic extraction lost the structure and the'
     +' printed table of contents could not fix it, so the whole document was re-parsed by'
     +' MinerU, which supplied the hierarchy'+(b?' — '+escA(b):'')+'">MinerU rescued</span>';
 }
 const bits=[r.entries?r.entries+' TOC entries':null, r.verified!=null?r.verified+' verified':null,
   r.page_offset?'page offset '+(r.page_offset>0?'+':'')+r.page_offset:null,
   r.was?'was '+r.was:null].filter(Boolean).join(', ');
 return ' <span class="scresc" title="Outline rebuilt from the printed table of contents'
   +(bits?' — '+escA(bits):'')+'">TOC rescued</span>';
}
const DIM_ABBR={Completeness:'C',Placement:'P',Fidelity:'F',Uniqueness:'U',Integrity:'I'};
function scDimColor(s){ return s>=90?'#15803d':s>=70?'#b45309':'#c1342d'; }
function scDocBadges(d){
 // Whole numbers looked cleaner across a wide table, but a small real deduction
 // (a couple of cited cells out of hundreds checked) rounded away to the SAME
 // whole number as a clean 100 and read as if nothing had moved — a hover-only
 // tooltip fixed that for a reader who thought to hover, not for a scan of the
 // table. One decimal place is still compact and now never hides a real change.
 const dims=(d.dims||[]).filter(x=>x.score!=null).map(x=>
   '<span class="scdim" title="'+escA(x.label)+(x.critical?' — sets the verdict':' — advisory, never moves the verdict')+'">'
   +'<i>'+esc(DIM_ABBR[x.label]||String(x.label).slice(0,1))+'</i>'
   +'<b style="color:'+scDimColor(x.score)+'">'+(+x.score.toFixed(1))+'</b></span>').join('');
 const f=[], bad=(t,c)=>'<b style="color:'+c+'">'+t+'</b>';
 if(d.pages!=null) f.push(d.pages+'pp');
 if(d.coverage!=null) f.push(d.coverage>=98?d.coverage+'% words'
   :bad(d.coverage+'% words', d.coverage>=90?'#b45309':'#c1342d'));
 const pl=(n,w)=>n+' '+w+(n===1?'':'s');
 if(d.pages_silent) f.push(bad(pl(d.pages_silent,'silent page'),'#c1342d'));
 if(d.pages_flagged) f.push(bad(pl(d.pages_flagged,'flagged page'),'#b45309'));
 if(d.headings_total) f.push(d.headings_matched===d.headings_total
   ? d.headings_total+' headings' : bad(d.headings_matched+'/'+d.headings_total+' headings','#b45309'));
 const w=d.word_delta;   // originals carry no dimensions — this is their coverage signal
 if(w!=null) f.push(w<=-5?bad('−'+Math.abs(w)+'% missing','#c1342d')
   : Math.abs(w)<1 ? 'Δ '+w+'%' : bad((w<0?w+'% short':'+'+w+'% struct'),'#b45309'));
 if(d.snapshots) f.push(pl(d.snapshots,'snapshot'));
 return (dims?'<span class="scdims">'+dims+'</span>':'')
      + (f.length?'<span class="scfacts">'+f.join(' · ')+'</span>':'');
}
async function loadGalleryTree(){
 const det=document.getElementById("detail");
 // A run is read live from S3 and indexed on the first look — ~20s for a finished run, then
 // cached. Saying so beats a spinner that looks stuck.
 det.innerHTML='<div class="sctwrap"><div class="galsub">'
   +(_galRun? 'Indexing extraction run '+esc(_galRun)+' — this takes around 20 seconds the first '
             +'time, then it is cached.' : 'Loading extraction scorecard…')+'</div></div>';
 try{
   // Indexing a run is ~15s of S3 reads. Whatever the user does in that window wins.
   const tok=_viewSeq;
   const r=await authFetch("/api/doc-gallery/tree"+galQ("variant="+enc(_galMode)));
   if(viewStale(tok)) return;
   if(!r.ok){ det.innerHTML='<div class="sctwrap"><div class="galsub">Could not load the scorecard ('+r.status+').</div></div>'; return; }
   _galTree=await r.json();
   if(viewStale(tok)) return;
   renderScTable();
 }catch(e){
   if(isAbort(e)) return;                      // the user left; not an error
   det.innerHTML='<div class="sctwrap"><div class="galsub">Error loading the scorecard.</div></div>';
 }
}
function renderScTable(){
 if(!document.querySelector("main").classList.contains("gallerymode")) return;
 const det=document.getElementById("detail"), t=_galTree.totals, vc=t.variants||{};
 const fb=(m,lbl,n)=>(!n&&m!=='all')?'':'<button class="galfbtn'+(_galMode===m?' active':'')+'" onclick="setGalMode(\''+m+'\')">'+lbl+' · '+(n||0)+'</button>';
 const vb=(v,lbl)=>'<button class="galfbtn'+(_galView===v?' active':'')+'" onclick="setGalView(\''+v+'\')">'+lbl+'</button>';
 let h='<div class="sctwrap">'+galRunBanner(_galTree.error)+'<div class="galhead">'
  +(_galRun? 'Extraction run scorecard' : 'Corpus extraction scorecard')+'</div>'
  +'<div class="galsub">One row per product folder. A folder&#39;s verdict is its <b>weakest</b> document — '
  +'an average would hide the one document you need to look at. Scored documents only — '
  +'anything published without a scorecard lives in Cards. Click a row to drill in, then open a document&#39;s viewer or its scorecard.</div>'
  +'<div class="galfilter">'+fb('all','All',vc.all)+fb('original','Original',vc.original)+fb('mineru','MinerU',vc.mineru)
  +'<span style="flex:1"></span>'+vb('table','Scorecard')+vb('cards','Cards')+'</div>'
  +'<div class="scbar">'
  +'<div class="kv"><span>folders</span><b>'+t.folders+'</b></div>'
  +'<div class="kv"><span>documents</span><b>'+t.documents+'</b></div>'
  +'<div class="kv"><span>pass</span><b style="color:#15803d">'+t.pass+'</b></div>'
  +'<div class="kv"><span>review</span><b style="color:#b45309">'+t.review+'</b></div>'
  +'<div class="kv"><span>fail</span><b style="color:#c1342d">'+t.fail
  +(t.documents?' <span class="scfp scfpbad">'+Math.round(100*t.fail/t.documents)+'%</span>':'')+'</b></div>'
  +'<div class="kv"><span>page snapshots</span><b>'+(t.snapshots||0).toLocaleString()+'</b></div></div>'
  +'<table class="sctable"><thead><tr>'
  +'<th style="width:44%">product / jurisdiction / document</th><th>verdict</th><th>pass</th><th>review</th><th>fail</th>'
  +'<th>worst</th><th>mean</th><th>tables</th><th>failed</th><th>findings</th>'
  +'<th title="Stage 4: whether the AI table-repair pass finished every section of this document">AI stage</th><th></th>'
  +'</tr></thead><tbody id="screws">'+scTableRows()+'</tbody></table></div>';
 det.innerHTML=h;
 // One delegated listener per render — rows are rebuilt wholesale on every toggle.
 document.getElementById("screws").addEventListener("click", onScTableClick);
}
function scTableRows(){
 let h='';
 for(const f of _galTree.folders){
   const fOpen=_galOpen.has(f.product);
   // Keys ride in data-* attributes, never in an inline onclick: product names carry
   // quotes, & and () that terminate an attribute and silently kill the handler.
   h+='<tr class="scrow scf" data-key="'+escA(f.product)+'">'
    +'<td><span class="sccaret">'+(fOpen?'▾':'▸')+'</span><b class="scname">'+esc(f.product)+'</b>'
    +' <span class="sccnt">'+f.jurisdictions.length+' folder'+(f.jurisdictions.length===1?'':'s')
    +' · '+f.total+' doc'+(f.total===1?'':'s')+'</span></td>'
    +'<td>'+scPill(f.verdict)+'</td><td class="sccnt">'+f.pass+'</td><td class="sccnt">'+f.review+'</td>'
    +'<td class="sccnt">'+scFail(f.fail, f.total)+'</td><td>'+scScore(f.worst_score)+'</td><td>'+scScore(f.mean_score)+'</td>'
    +'<td></td><td></td><td></td><td></td><td></td></tr>';
   if(!fOpen) continue;
   for(const jr of f.jurisdictions){
     // Printable separator: a control character does not survive the round-trip
     // through an HTML attribute, so the key read back would never match the Set.
     const jkey=f.product+'|~|'+jr.jurisdiction, jOpen=_galOpen.has(jkey);
     h+='<tr class="scrow scj" data-key="'+escA(jkey)+'">'
      +'<td><span class="sccaret">'+(jOpen?'▾':'▸')+'</span>'+esc(jr.jurisdiction)
      +' <span class="sccnt">'+jr.total+' doc'+(jr.total===1?'':'s')+'</span></td>'
      +'<td>'+scPill(jr.verdict)+'</td><td class="sccnt">'+jr.pass+'</td><td class="sccnt">'+jr.review+'</td>'
      +'<td class="sccnt">'+scFail(jr.fail, jr.total)+'</td><td>'+scScore(jr.worst_score)+'</td><td>'+scScore(jr.mean_score)+'</td>'
      +'<td></td><td></td><td></td><td></td><td></td></tr>';
     if(!jOpen) continue;
     for(const d of jr.documents){
       h+='<tr class="scd">'
        +'<td><div class="scdid">'+esc(d.doc_name||d.doc_id||d.slug)
        +(d.doc_name?' <span class="scdocid">'+esc(d.doc_id||'')+'</span>':'')
        +(d.doc_version?' <span class="scmuted">v'+esc(d.doc_version)+'</span>':'')
        +(d.mineru?' <span class="scmuted">MinerU</span>':'')+scRescued(d)+'</div>'
        +'<div class="scdbadges">'+scDocBadges(d)+'</div></td>'
        +'<td>'+scPill(d.gate)+'</td><td></td><td></td><td></td>'
        +'<td>'+scScore(d.worst_score)+'</td>'
        +'<td class="sccnt scmuted">'+esc(d.weakest||'')+'</td>'
        +'<td class="sccnt">'+scNum(d.tables)+'</td>'
        +'<td class="sccnt"'+(d.tables_failed?' style="color:#c1342d"':'')+'>'+scNum(d.tables_failed)+'</td>'
        +'<td class="sccnt">'+scNum(d.findings)+'</td>'
        +galAiStageCell(d.stage4)
        // Only offer what exists. has_viewer is false for a document whose viewer was never
        // built — a link there is a guaranteed 404, indistinguishable from a broken viewer.
        // Absent on older published manifests, so undefined means "assume it is there".
        +'<td class="scacts">'
        +((d.has_viewer===false)?'<span class="scmuted" title="No viewer was built for this '
            +'document — it was extracted before the run built review artefacts">no viewer</span>'
          :'<a data-view="'+escA(d.slug)+'">viewer ↗</a>')
        +((d.has_scorecard===false)?''
          :'<a data-sc="'+escA(d.slug)+'">scorecard ↗</a>')
        +galRetryCell(f.product, jr.jurisdiction+'__'+(d.doc_id||''))+'</td></tr>';
     }
   }
 }
 return h||'<tr><td colspan="12" class="scmuted">Nothing extracted yet.</td></tr>';
}
function onScTableClick(e){
 const a=e.target.closest("a[data-view],a[data-sc],a[data-retry-product]");
 if(a){
   if(a.dataset.view) openDocGallery(a.dataset.view);
   else if(a.dataset.sc) openScorecardPage(a.dataset.sc);
   else galRetry(a.dataset.retryProduct, a.dataset.retryLabel);
   return;
 }
 const tr=e.target.closest("tr[data-key]");   // folder / jurisdiction rows only
 if(!tr) return;
 const k=tr.dataset.key;
 _galOpen.has(k)?_galOpen.delete(k):_galOpen.add(k);
 document.getElementById("screws").innerHTML=scTableRows();
}
// Breadcrumb for a slug — the tree knows product/jurisdiction, the card list knows
// the published region label; the slug itself is only the last resort.
function galDocLabel(slug){
 if(_galTree) for(const f of _galTree.folders) for(const jr of f.jurisdictions) for(const d of jr.documents)
   if(d.slug===slug) return f.product+' / '+jr.jurisdiction+' · '+(d.doc_id||'')+(d.mineru?' (MinerU)':'');
 const c=_galDocs.find(d=>d.slug===slug);
 return c?((c.product||'')+' / '+(c.region||'')+' · '+(c.doc_id||'')):slug;
}
async function fetchScorecard(slug){
 if(slug in _scCache) return _scCache[slug];
 try{
   const r=await authFetch("/api/doc-gallery/"+enc(slug)+"/scorecard"+galQ());
   _scCache[slug]=r.ok?await r.json():null;   // cache the miss too: no refetch loop
 }catch(e){
   // An ABORT is not a miss. Caching it as null would make this document permanently
   // scorecard-less for the rest of the session, just because someone changed screens.
   if(isAbort(e)) return null;
   _scCache[slug]=null;
 }
 return _scCache[slug];
}
async function loadDocGallery(){
 const det=document.getElementById("detail");
 det.innerHTML='<div class="galwrap"><div class="galsub">Loading documents…</div></div>';
 try{
   const tok=_viewSeq;
   const r=await authFetch("/api/doc-gallery"+galQ());
   if(viewStale(tok)) return;
   if(!r.ok){ det.innerHTML='<div class="galwrap"><div class="galsub">Could not load documents ('+r.status+').</div></div>'; return; }
   const data=await r.json();
   if(viewStale(tok)) return;
   if(!data.count){ det.innerHTML='<div class="galwrap">'+galRunBanner(data.error)+'<div class="galsub">'
     +(_galRun? 'No finished documents in this run yet — a document appears here once it has a scorecard.'
              : 'No extracted documents available.')+'</div></div>'; return; }
   _galDocs=data.docs; _galListError=data.error||null;
   renderGallery();
 }catch(e){
   if(isAbort(e)) return;
   det.innerHTML='<div class="galwrap"><div class="galsub">Error loading documents.</div></div>';
 }
}
function renderGallery(){
 if(!document.querySelector("main").classList.contains("gallerymode")) return;
 const det=document.getElementById("detail");
 const nMin=_galDocs.filter(galIsMineru).length, nOrig=_galDocs.length-nMin;
 const docs=_galDocs.filter(d=> _galMode==='mineru'?galIsMineru(d) : _galMode==='original'?!galIsMineru(d) : true);
 const byProd={};
 docs.forEach(d=>{ (byProd[d.product]=byProd[d.product]||[]).push(d); });
 const fmt=n=>(n||0).toLocaleString();
 const totSnap=docs.reduce((a,d)=>a+(d.snapshots||0),0);
 const snapByProd={}; docs.forEach(d=>{ if(d.snapshots) snapByProd[d.product]=(snapByProd[d.product]||0)+d.snapshots; });
 const snapBreak=Object.keys(snapByProd).sort().map(p=>esc(p)+' <b>'+fmt(snapByProd[p])+'</b>').join(' &nbsp;·&nbsp; ');
 // verification roll-up (from each doc's scorecard gate on the manifest)
 const gc={pass:0,review:0,fail:0}; let ungated=0;
 docs.forEach(d=>{ if(d.gate&&gc[d.gate]!=null) gc[d.gate]++; else if(galIsMineru(d)) ungated++; });
 const gatePills=[['pass','g'],['review','a'],['fail','r']].filter(([k])=>gc[k])
   .map(([k,c])=>'<span class="gbadge '+c+'">'+gc[k]+' '+k.toUpperCase()+'</span>').join(' ');
 const fb=(m,lbl)=>'<button class="galfbtn'+(_galMode===m?' active':'')+'" onclick="setGalMode(\''+m+'\')">'+lbl+'</button>';
 const vb=(v,lbl)=>'<button class="galfbtn'+(_galView===v?' active':'')+'" onclick="setGalView(\''+v+'\')">'+lbl+'</button>';
 let h='<div class="galwrap">'+galRunBanner(_galListError)+'<div class="galhead">'
      +(_galRun? 'Extraction run — documents' : 'Document Gallery')+'</div>'
      +'<div class="galfilter">'+fb('all','All · '+_galDocs.length)+fb('original','Original · '+nOrig)+fb('mineru','MinerU · '+nMin)
      +'<span style="flex:1"></span>'+vb('table','Scorecard')+vb('cards','Cards')+'</div>'
      +'<div class="galsub">'+docs.length+' shown · '+Object.keys(byProd).length+' products &nbsp;·&nbsp; '
      +'<span style="color:#15803d">green</span> tight · <span style="color:#b45309">amber</span> structure/short · <span style="color:#c1342d">red</span> content missing</div>'
      +'<div class="galstats"><span class="galstat-total">'+fmt(totSnap)+' page snapshots</span> across '+docs.length+' shown'
      +(snapBreak?' &nbsp;·&nbsp; '+snapBreak:'')+'</div>'
      +(gatePills?'<div class="galstats"><span class="galstat-total">Verification</span> '+gatePills
        +(ungated?' &nbsp;·&nbsp; <span class="galsub">'+ungated+' MinerU doc(s) not yet gated</span>':'')+'</div>':'');
 Object.keys(byProd).sort().forEach(p=>{
   h+='<div class="galprod">'+esc(p)+' · '+byProd[p].length+' memos</div><div class="galgrid">';
   byProd[p].forEach(d=>{
     h+='<div class="galcard" onclick="openDocGallery(\''+esc(d.slug)+'\')">'
       +'<div class="galtitle">'+esc(d.region||d.slug)
       +(d.doc_name?'<div class="galdocname">'+esc(d.doc_name)+(d.doc_version?' · v'+esc(d.doc_version):'')+'</div>':'')
       +'<span class="did">'+esc(d.doc_id||'')+'</span>'
       +(d.rescued?'<span class="scresc" title="Outline rebuilt from the printed table of contents">rescued</span>':'')+'</div>'
       +'<div class="galbadges">'+galBadges(d)+'</div>'
       +'<div class="gallink">'
       +((d.has_viewer===false)?'<span class="galsub">no viewer</span>'
         :'<a onclick="event.stopPropagation();openDocGallery(\''+esc(d.slug)+'\')">open viewer ↗</a>')
       +(d.has_scorecard?'<a style="margin-left:12px" onclick="event.stopPropagation();openScorecardPage(\''+esc(d.slug)+'\')">scorecard ↗</a>':'')
       +galCardRetryLink(d)+'</div>'
       +'</div>';
   });
   h+='</div>';
 });
 if(!docs.length) h+='<div class="galsub">No documents in this filter.</div>';
 det.innerHTML=h;
}

async function openDocGallery(slug, target){
 const det=document.getElementById("detail");
 det.innerHTML='<div class="galwrap"><div class="hero">Loading viewer…</div></div>';
 try{
   // A viewer is ~1.7MB and an inspection page ~15MB. Leaving mid-download cancels it.
   const tok=_viewSeq;
   const r=await authFetch("/api/doc-gallery/"+enc(slug)+"/view"+galQ());
   if(viewStale(tok)) return;
   if(!r.ok){ det.innerHTML='<div class="galwrap"><div class="hero">Could not load viewer ('+r.status+').</div></div>'; return; }
   const body=await r.text();
   if(viewStale(tok)) return;
   const url=URL.createObjectURL(new Blob([body],{type:"text/html"}));
   const src=url+(target?'#f='+encodeURIComponent(target):'');   // viewer opens at this section
   // No scorecard button here: the scorecard is reached from the table row, and the
   // viewer stays a reader.
   det.innerHTML='<div class="galview"><div class="galviewbar">'
     +'<button class="galback" onclick="loadGallery()">← All documents</button>'
     +'<span class="galsub" style="align-self:center;margin:0 0 0 6px">'+esc(galDocLabel(slug))+'</span>'
     +'</div><iframe class="galframe" src="'+src+'"></iframe></div>';
 }catch(e){
   if(isAbort(e)) return;
   det.innerHTML='<div class="galwrap"><div class="hero">Error loading viewer.</div></div>'; }
}

// ---- Scorecard: same validation detail the extraction dev dashboard shows,
// computed once at publish time (push_hybrid_s3.py) and read back here. Rendered in
// two places from ONE builder — inline under a table row, and as the full scorecard
// page the viewer's Scorecard button goes to. Read-only —
// dismiss/restore need a live job dir + dismissal store, which a published S3 doc
// doesn't have, so those controls are left out here rather than shown non-functional.
const SC_GATE_TEXT={
 pass:{icon:'✓',title:'PASS',sub:'Every dimension scored at or above the pass bar.'},
 review:{icon:'⚠',title:'REVIEW',sub:'Usable, but at least one dimension needs a human look before you trust this extraction.'},
 fail:{icon:'✕',title:'FAIL',sub:'At least one dimension is bad enough that this extraction should not be used as-is.'},
 unknown:{icon:'?',title:'UNKNOWN',sub:'Not enough data to score this job.'},
};
function scGateColor(gate){ return gate==='pass'?'#15803d':gate==='review'?'#b45309':gate==='fail'?'#c1342d':'#7B888A'; }
function scScoreCls(s,pass,review){ if(s==null) return 'sc-na'; return s>=pass?'sc-good':(s>=review?'sc-warn':'sc-bad'); }
function scDimension(key,d,th){
 const s=d.score, cls=scScoreCls(s,th.pass,th.review);
 const barCol=cls==='sc-good'?'#15803d':cls==='sc-warn'?'#b45309':'#c1342d';
 const stats=(d.detail&&d.detail.stats||[]).map(st=>
   '<div class="sc-stat'+(st.bad?' bad':st.warn?' warn':'')+'"><span>'+esc(st.label)+'</span><b>'+esc(String(st.value))+'</b></div>').join('');
 const naReason=d.detail&&d.detail.reason?'<div class="sc-caveat">Not scored: '+esc(d.detail.reason)+'</div>':'';
 const tag=d.critical?'<span class="sc-tag sc-tag-gate" title="This dimension can set the verdict">sets verdict</span>'
   :'<span class="sc-tag sc-tag-adv" title="Reported, but never changes the verdict">advisory</span>';
 return '<div class="sc-dim'+(d.critical?'':' sc-advisory')+'">'
   +'<div class="sc-dim-head"><span class="sc-dim-name">'+esc(d.label)+'</span>'+tag
   +'<span class="sc-dim-score" style="color:'+(s==null?'#7B888A':barCol)+'">'+(s==null?'n/a':s.toFixed(1))+'</span></div>'
   +'<div class="sc-bar"><div style="width:'+(s==null?0:s)+'%;background:'+barCol+'"></div></div>'
   +'<div class="sc-what">'+esc(d.what||'')+'</div><div class="sc-stats">'+stats+'</div>'+naReason
   +'<div class="sc-advice"><b>What to do:</b> '+esc(d.advice||'')+'</div>'
   +(d.caveat?'<div class="sc-caveat"><b>Limit of this check:</b> '+esc(d.caveat)+'</div>':'')
   +'<div class="sc-from"><b>Measured from:</b> '+esc(d.from||'')+'</div>'
   +(d.formula?'<div class="sc-formula"><b>score =</b> '+esc(d.formula)+'</div>':'')+'</div>';
}
const SC_PAGE_STATE_LABEL={ok:'clean',flagged:'flagged loss',silent:'SILENT loss',unvalidatable:'visual-only'};
function scFinding(f, slug){
 const sevCls='sc-find-'+(f.dismissed?'advisory':(f.severity||'advisory'));
 const base=f.file?f.file.split('/').pop():null;
 const inner=[base,(f.pages&&f.pages.length)?'p'+f.pages.join('–'):null].filter(Boolean).join(' · ');
 const where=(slug&&f.file)
   ? '<a class="sc-find-link" style="cursor:pointer;color:var(--accent);text-decoration:underline" '
     +'onclick="gotoSection(\''+esc(slug)+'\',\''+esc(f.file)+'\')" title="Open this section in the viewer">'+esc(inner)+' ↗</a>'
   : esc(inner);
 return '<div class="sc-find '+sevCls+'">'
   +'<div class="sc-find-top"><span class="sc-find-kind">'+esc(f.kind||'')+'</span>'
   +(f.severity==='silent'?'<span class="sc-find-kind sc-find-silent">silent</span>':'')
   +'<span class="sc-find-where">'+where+'</span></div>'
   +'<div class="sc-find-title">'+esc(f.title||'')+'</div>'
   +'<div class="sc-find-detail">'+esc(f.detail||'')+'</div></div>';
}
function renderScorecardBody(sc, slug){
 if(sc.error) return '<div class="sc-caveat">Scorecard unavailable: '+esc(sc.error)+'</div>';
 const th=sc.gate_thresholds||{pass:90,review:70};
 const g=SC_GATE_TEXT[sc.gate]||SC_GATE_TEXT.unknown;
 const weak=sc.weakest_dimension?((sc.dimensions[sc.weakest_dimension]||{}).label):null;
 const critNames=(sc.critical_dimensions||[]).map(k=>(sc.dimensions[k]||{}).label||k).join(', ');
 let html='<div class="sc-gate" style="border-color:'+scGateColor(sc.gate)+'">'
   +'<div class="sc-gate-verdict" style="color:'+scGateColor(sc.gate)+'">'+g.icon+' '+g.title+'</div>'
   +'<div class="sc-gate-sub">'+esc(g.sub)+'</div>'
   +'<div class="sc-gate-note">'+(sc.gate_reasons||[]).map(esc).join(' ')+' '
   +(weak?'Lowest verdict-setting dimension: <b>'+esc(weak)+'</b> at '+sc.worst_score+'. ':'')
   +'The score-based verdict is the worst of '+esc(critNames)+' — never an average. Source fidelity findings can additionally require review.'
   +(sc.dismissed_count?' <b>'+sc.dismissed_count+' finding(s) dismissed</b> as false positives at publish time.':'')+'</div></div>';
 const ordered=Object.entries(sc.dimensions||{}).sort((a,b)=>(b[1].critical?1:0)-(a[1].critical?1:0));
 html+='<div class="sc-dim-grid">'+ordered.map(([k,d])=>scDimension(k,d,th)).join('')+'</div>';
 const findings=(sc.findings||[]).filter(f=>!f.dismissed);
 html+='<div class="sc-panel"><h4>Findings — '+findings.length+' open</h4>';
 html+=findings.length?findings.map(f=>scFinding(f, slug)).join(''):'<div class="sc-find-none">✓ No open findings.</div>';
 html+='</div>';
 const pages=sc.pages||{states:{},counts:{},total:0};
 if(pages.total){
   html+='<div class="sc-panel"><h4>Page health — '+pages.total+' pages</h4><div class="sc-heat">';
   for(let p=1;p<=pages.total;p++){
     const st=pages.states[String(p)]||'ok';
     html+='<div class="sc-heat-cell sc-h-'+st+'" title="Page '+p+' — '+SC_PAGE_STATE_LABEL[st]+'"></div>';
   }
   html+='</div></div>';
 }
 if((sc.tables||[]).length){
   html+='<div class="sc-panel"><h4>Tables — '+sc.tables.length+' detected</h4><div class="sc-strip">';
   for(const t of sc.tables){
     const iou=t.match_iou!=null?' '+Math.round(t.match_iou*100)+'%':'';
     html+='<span class="sc-chip sc-c-'+esc(t.bucket)+'" title="'+esc(t.reason||t.bucket)+'">'
       +esc(t.table_id.replace('table_','T'))+' p'+t.pages[0]+iou+'</span>';
   }
   html+='</div></div>';
 }
 return html;
}
// The scorecard PAGE. Where a document has a published INSPECTION page — the
// extraction dashboard's own six tabs (Scorecard / Document / MinerU Inspector /
// Validation / Stage 4 · AI / Page Review), frozen at publish time — that is what
// opens, proxy-streamed exactly like the viewer. Docs published before that wiring
// fall back to rendering their scorecard JSON here.
// Which document the open inspection page is showing. The page is a blob: iframe with no
// URL of its own to carry it, and its "Open viewer" message arrives long after the call that
// opened it -- so the answer has to be held here rather than passed through the frame.
let _galInspectSlug=null;
async function openScorecardPage(slug){
 _galInspectSlug=slug;
 const det=document.getElementById("detail");
 const bar='<div class="galviewbar" style="margin:0 0 14px">'
   +'<button class="galback" onclick="loadGallery()">← All documents</button>'
   +'<button class="galback" onclick="openDocGallery(\''+esc(slug)+'\')">📄 Open viewer</button></div>';
 const shell=b=>'<div class="sctwrap" style="max-width:1100px">'+bar
   +'<div class="galhead">📊 Scorecard</div><div class="galsub">'+esc(galDocLabel(slug))+'</div>'
   +'<div style="margin-top:14px">'+b+'</div></div>';
 det.innerHTML=shell('Loading…');
 // An inspection page is ~15MB. If the user leaves while it downloads, the download is aborted
 // and nothing here paints — including the scorecard fallback below, which would otherwise
 // arrive on whatever screen replaced this one.
 const tok=_viewSeq;
 try{
   const r=await authFetch("/api/doc-gallery/"+enc(slug)+"/inspect"+galQ());
   if(viewStale(tok)) return;
   if(r.ok){
     const body=await r.text();
     if(viewStale(tok)) return;
     const url=URL.createObjectURL(new Blob([body],{type:"text/html"}));
     det.innerHTML='<div class="galview"><div class="galviewbar">'
       +'<button class="galback" onclick="loadGallery()">← All documents</button>'
       +'<button class="galback" onclick="openDocGallery(\''+esc(slug)+'\')">📄 Open viewer</button>'
       +'<span class="galsub" style="align-self:center;margin:0 0 0 6px">'+esc(galDocLabel(slug))+'</span>'
       +'</div><iframe class="galframe" src="'+url+'"></iframe></div>';
     return;
   }
 }catch(e){
   if(isAbort(e)) return;                  /* the user left; do not paint the fallback either */
   /* otherwise fall through to the scorecard-only render below */
 }
 const sc=await fetchScorecard(slug);
 if(viewStale(tok)) return;
 det.innerHTML=shell(sc?renderScorecardBody(sc, slug)
   :'<div class="sc-caveat">No scorecard for this document — it may have been published before scorecards were wired in.</div>');
}
// The inspection page's own "← All documents" / Escape hand control back here, and its
// "Open viewer" button does too: that button is a window.open of a dashboard route which does
// not exist in a published document (assets/inspect_shim.js explains the blank tab it used to
// produce). The page cannot know the viewer's URL -- only this app does -- so it asks, and the
// answer is the viewer for whichever document is on screen.
window.addEventListener("message", e=>{
 if(!e.data) return;
 if(e.data.aci==="gal-back") loadGallery();
 else if(e.data.aci==="gal-viewer" && _galInspectSlug) openDocGallery(_galInspectSlug);
 else if(e.data.aci==="ex-viewer") exOpenViewerFor(e.data.ref);
});
// finding -> open that section in the doc viewer (viewer reads #f=<path> on load)
function gotoSection(slug, file){ closeModal(); openDocGallery(slug, file); }

// ---- alert mapping-confidence color (cosine 0-1: >=.8 strong, >=.6 ok, else suspect) ----
const cosCol=s=> s==null?'#7B888A': s>=0.8?'#15803d': s>=0.6?'#ca8a04':'#c1342d';
// ---- full-text popup for a guidance/alert link ----
function openModal(html){
 document.getElementById("modal-body").innerHTML=html;
 document.getElementById("modal").style.display="flex";
}
function closeModal(){ document.getElementById("modal").style.display="none"; }
// full-text popups for the clause-view alert / guidance cards (which are truncated)
function alertModal(a){
 openModal(`<div class="mhead">🔔 Alert <span class="lkscore" style="background:${RAT[a.impact]||'#7B888A'}">${esc(a.impact||'')}</span> <span class="muted">${esc(a.date||'')}</span></div>
   <div class="mtitle"><b>${esc(a.title)}</b></div><div class="mbody">${fmt(a.summary||'')}</div>`);
}
// full popup for an ALERT search result: summary + attachment text + related clauses
function alertResultModal(x){
 const a=x.alert||{}; const col=RAT[a.impact]||'#7B888A';
 const mapped=(a.mapped||[]).map(m=>`<span class="chip" data-r="${esc(x.jurisdiction)}" data-k="${esc(m[0])}">${esc(x.jurisdiction)} · ${esc(m[0])}</span>`).join(" ");
 const att=a.attachment?`<div class="mhead" style="margin-top:16px">📎 Attachment</div><div class="mbody">${fmt(a.attachment)}</div>`:'';
 openModal(`<div class="mhead">🔔 Alert <span class="lkscore" style="background:${col}">${esc(a.impact||'')}</span> <span class="muted">${esc(a.date||'')} · ${esc(x.jurisdiction)}</span></div>
   <div class="mtitle"><b>${esc(x.title)}</b></div><div class="mbody">${fmt(a.summary||'')}</div>
   ${mapped?`<div class="mhead" style="margin-top:14px">Related clauses</div><div>${mapped}</div>`:''}${att}`);
 document.querySelectorAll("#modal .chip").forEach(c=>c.onclick=()=>{ closeModal(); openClause(c.dataset.r,"",c.dataset.k,""); });
}
function guidanceModal(g){
 openModal(`<div class="mhead">📑 Guidance <span class="lkscore" style="background:${RAT[g.color]||'#7B888A'}">${esc(g.color||'')}</span> <span class="muted">${esc(g.subject||'')}</span></div>
   <div class="mtitle">${esc(g.question||'')}</div><div class="mbody">${fmt(g.answer||'')}</div>`);
}
let _linkCache={};
async function loadLinks(jur){
 if(_linkCache[jur]) return _linkCache[jur];
 const d=await (await authFetch(`/api/links?jurisdiction=${enc(jur)}`)).json();
 _linkCache[jur]=d; return d;
}
function linkPopup(jur, it, kind){
 const score=kind==="alert"&&it.score!=null
   ? `<span class="lkscore" style="background:${cosCol(it.score)}">mapping ${it.score}</span>`:'';
 const head=kind==="alert"
   ? `<b>${esc(it.alert)}</b> <span class="muted">${esc(it.impact||'')}</span>`
   : `<b>${esc(it.subject||'')}</b><br>${esc(it.question||'')}`;
 const ratecol=kind==="guidance"&&it.color?`<span class="lkscore" style="background:${RAT[it.color]||'#7B888A'}">${esc(it.color)}</span> `:'';
 openModal(
   `<div class="mhead">${kind==="alert"?'🔔 Alert':'📑 Guidance'} → <span class="lkkey">${esc(it.key)}</span> ${esc(it.clause_title||'')} ${ratecol}${score}</div>
    <div class="mtitle">${head}</div>
    <div class="mbody">${fmt(it.answer||it.summary||'')}</div>
    <button class="authbtn" id="mgoto">↪ Open clause ${esc(it.key)} in the document</button>`);
 document.getElementById("mgoto").onclick=()=>{ closeModal(); jumpTo(document.getElementById("detail"), it.key); };
}
function scrollMsgs(){const m=document.getElementById("msgs");m.scrollTop=m.scrollHeight;}
function chatSrc(w,s){
 const chips=(s&&s.length)?s.map(x=>`<span class="chip" data-r="${esc(x.jurisdiction)}" data-k="${esc(x.key)}">${esc(x.jurisdiction)} · ${esc(x.key)}</span>`).join(""):"";
 w.innerHTML=chips?`<div class="srclabel">Sources the agent read</div>${chips}`:"";
 w.querySelectorAll(".chip").forEach(c=>c.onclick=()=>openClause(c.dataset.r, "", c.dataset.k, ""));
}
// ---- Guided interview (Marketing Restrictions) --------------------------------------
// A Marketing Restrictions question is usually asked in a form the memo cannot answer
// definitively — "can I market my fund in Jersey?" — because the memo carries a different
// route per scenario. Before answering, the five facts that select the route are collected
// here; the confirmed scenario then travels with the question (see /api/interview/* and
// _interview_answer_context in app.py).
//
// The interview runs only when the scope is EXACTLY ONE product and that product is
// Marketing Restrictions. Same rule as the master prompt (AOSNG-3442): a question that
// spans products is broader than this vocabulary, and answering it through an interview
// about funds would narrow it without the user asking.
const IV_PRODUCT="Marketing Restrictions - Asset Management";
// null slots, not {}: /api/interview/* distinguishes "no opinion" from "cleared", and an
// absent key would read as the latter.
const IV_EMPTY={jurisdiction:null,activity:null,category:null,marketer:null,investor_type:null};
let _ivSlots={...IV_EMPTY}, _ivSignals={product_kind:null,category_hint:null,activity_hint:null};
let _ivPending=null;      // the field currently being asked, or null
let _ivQuestion="";       // the original wording — the retrieval query keeps it
let _ivOther=null;        // the "other" lane's carve-out kind, when there was one
let _ivSuspended=null;    // interview state parked while an aside is answered
let _ivBusy=false;
// Interview order, and how each field is labelled in the confirmed-scenario card.
const IV_FIELD_ORDER=["jurisdiction","activity","category","marketer","investor_type"];
const IV_LABELS={jurisdiction:"Jurisdiction",activity:"Marketing activity",
  category:"Product / service",marketer:"Marketing carried out by",
  investor_type:"Target investors"};

// Three product states, three behaviours. The filter starts with EVERY product selected,
// so gating on "exactly Marketing Restrictions" alone left the interview invisible by
// default — a user asking "can I market my fund in Jersey" got the hedging answer and
// never saw that a guided scoping existed.
//
//   exactly this product   -> the interview, straight away
//   this product AND others -> classify first; if it really is a marketing question, ASK
//                             which product before collecting five answers about funds
//   not in scope at all     -> the plain path, and no router call
function ivApplies(){
  return enabledP.size===1 && enabledP.has(IV_PRODUCT);
}
function ivAmbiguous(){
  return enabledP.size>1 && enabledP.has(IV_PRODUCT);
}
// Narrow the sidebar to one product, so the rest of the session — the answer's scope, the
// master prompt, a follow-up question — agrees with what was just chosen here.
function ivPickProduct(name){
  selectedProduct=name; enabledP=new Set([name]);
  enabledJ=new Set();   // jurisdictions are scoped to the product -> a switch starts empty
  renderProducts(); updatePbtn(); renderTree(); updateRbtn();
}

// "Which product is this about?" — asked ONLY once the router has said the question is a
// permitted-activity one, so a Data Privacy question is never interrupted by it. The
// classification is kept and reused, so choosing here costs no second model call.
function ivAskProduct(routed){
  const opts=[...enabledP].sort((a,b)=>(a===IV_PRODUCT?-1:b===IV_PRODUCT?1:0))
    .map(p=>`<button class="ivopt" data-ivprod="${escA(p)}">${esc(p)}`
      +(p===IV_PRODUCT?' <span class="ivrec">guided</span>':'')+`</button>`).join("");
  const card=ivMsg(
    `<div class="ivstep">Step 1 · Product</div>`
    +`<div class="ivq">Which product is this question about?</div>`
    +`<div class="ivopts">${opts}</div>`
    +`<div class="ivnote">Marketing Restrictions has a guided scoping — it asks a few `
    +`questions so the answer can be definitive rather than "it depends".</div>`, "ivcard");
  card.querySelectorAll("[data-ivprod]").forEach(b=>
    b.addEventListener("click",async()=>{
      const name=b.getAttribute("data-ivprod");
      ivMsg(esc(name), "msg user");
      ivPickProduct(name);
      if(name!==IV_PRODUCT){ ivReset(); return streamAnswer(_ivQuestion, null, null); }
      await ivApply(routed);
    }));
}
// The jurisdiction filter is an ANSWER, not a hint. Selecting a region in the sidebar is
// the same statement as answering "which jurisdiction are you asking about?", so asking
// again is asking the user to repeat themselves. ONE selected jurisdiction fills the slot;
// several narrow the options to those, because "which of these?" is still a real question.
function ivFilterJ(){
  // Selecting every jurisdiction the product covers is the same statement as selecting
  // none, and updateRbtn already labels both "All jurisdictions" -- so neither sends a
  // filter. Without the second test, clicking "All" sent the whole list as a filter that
  // narrows nothing.
  const mj=prodJurisdictions(selectedProduct).length;
  return enabledJ.size && enabledJ.size<mj ? [...enabledJ].join(",") : null;
}
function ivPinnedJ(){
  return enabledJ.size===1 ? [...enabledJ][0] : null;
}
// A filter moved to a different region invalidates what has been collected: it describes a
// scenario in a jurisdiction the user is no longer asking about.
function ivSyncFilter(){
  const pinned=ivPinnedJ();
  if(_ivSlots.jurisdiction && pinned && pinned!==_ivSlots.jurisdiction) ivReset();
  return pinned;
}
function ivReset(){
  _ivSlots={...IV_EMPTY};
  _ivSignals={product_kind:null,category_hint:null,activity_hint:null};
  _ivPending=null; _ivQuestion=""; _ivOther=null;
}
function ivPost(path, body){
  // The filter travels on EVERY call: it narrows both the options offered and the
  // jurisdictions the extractor is told are allowed, so the two can never disagree.
  return authFetch("/api/interview/"+path, {method:"POST", signal:null,
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({product:IV_PRODUCT, jurisdictions:ivFilterJ(), ...body})});
}
function ivMsg(html, cls){
  const m=document.getElementById("msgs");
  if(m.querySelector(".hero")) m.innerHTML="";
  m.insertAdjacentHTML("beforeend", `<div class="${cls||"msg ai"}">${html}</div>`);
  scrollMsgs();
  return m.lastElementChild;
}

// The question card. Options carry their definition as a tooltip and their exclusion
// reason inline, so the vocabulary is learnable without leaving the chat.
function ivRenderQuestion(q){
  _ivPending=q.field;
  const answered=IV_FIELD_ORDER.filter(f=>_ivSlots[f]).length;
  const opts=q.options.map(o=>{
    const out=o.excluded_reason?" out":"";
    const tip=esc(o.excluded_reason||o.definition||"");
    return `<button class="ivopt${out}" data-ivval="${escA(o.value)}" `
      +`${out?"disabled":""} title="${tip}">${esc(o.value)}</button>`;
  }).join("");
  const why=q.options.filter(o=>o.excluded_reason)
    .map(o=>`<div class="ivwhy">${esc(o.value)} — ${esc(o.excluded_reason)}</div>`).join("");
  const card=ivMsg(
    `<div class="ivstep">Step ${answered+1} · ${esc(q.label)}</div>`
    +`<div class="ivq">${esc(q.question)}</div>`
    +`<div class="ivopts">${opts}</div>${why}`
    +`<div class="ivnote">Or just type the answer — and ask what a term means if you need to.</div>`,
    "ivcard");
  card.querySelectorAll("[data-ivval]").forEach(b=>
    b.addEventListener("click",()=>ivPick(q.field, b.getAttribute("data-ivval"))));
}
// A clicked option. Advances through /next, which is DELIBERATELY not /route: choosing
// from a list needs no wording mapped, so routing a click through the extractor would
// spend a Bedrock call and ~1.5s to be told what we already know.
async function ivPick(field, value){
  if(_ivBusy) return;
  _ivBusy=true;
  ivMsg(esc(value), "msg user");
  _ivSlots={..._ivSlots, [field]:value};
  try{
    const r=await ivPost("next", {slots:_ivSlots, signals:_ivSignals,
                                  original_question:_ivQuestion, other_kind:_ivOther});
    if(!r.ok) throw new Error("HTTP "+r.status);
    await ivApply(await r.json());
  }catch(e){
    ivMsg('<span class="muted">The interview is temporarily unavailable. Please try again.</span>');
  }finally{ _ivBusy=false; }
}

// Act on a /route or /next response: ask the next question, or answer.
async function ivApply(d){
  _ivSlots=d.slots||_ivSlots;
  if(d.signals) _ivSignals=d.signals;
  if(d.acknowledgement) ivMsg(mdToHtml(d.acknowledgement));
  if(d.capability){ ivMsg(mdToHtml(d.capability)); _ivPending=null; return; }
  if(d.complete && d.handoff){ _ivPending=null; await ivAnswer(d.handoff); return; }
  // The "other" lane: a definition, a penalty, a disclaimer — informational rather than a
  // permission check. It skips the interview but still needs a jurisdiction, and the
  // carve-out has already routed it to its own part of the memo.
  if(d.general && !d.general.needs_jurisdiction){
    _ivPending=null; _ivOther=d.other_kind||null;
    await ivAnswer({jurisdiction:d.general.jurisdiction, query:d.general.query,
                    sections:d.general.sections||[], slots:_ivSlots, general:true});
    return;
  }
  if(d.other_kind) _ivOther=d.other_kind;
  if(d.next_question) ivRenderQuestion(d.next_question);
  else _ivPending=null;
}

// The confirmed scenario, shown before the answer. This is the last chance to catch a
// wrong answer before an authoritative-looking reply is generated from it — and it is why
// the slots are echoed rather than just used.
function ivScenarioCard(h){
  const rows=IV_FIELD_ORDER.filter(f=>h.slots&&h.slots[f]).map(f=>
    `<tr><td class="k">${esc(IV_LABELS[f])}</td><td>${esc(h.slots[f])}</td></tr>`).join("");
  const secs=(h.sections||[]).map(x=>
    `<span class="kk" data-ivsec="${escA(x.key)}">${esc(x.key)}</span> ${esc(x.title)}`)
    .join(" · ");
  const card=ivMsg(
    `<div class="ivstep">${h.general?"Answering from":"Scenario confirmed"}</div>`
    +(rows?`<table>${rows}</table>`:"")
    +(secs?`<div class="ivsecs">Memo sections: ${secs}</div>`:"")
    +`<button class="ivedit" id="ivedit">Change an answer</button>`, "ivscen");
  // The QUALIFIED region id, not the bare name: "Jersey" exists under two products and
  // the clause endpoint would have to guess which memo was meant.
  const region=h.region||h.jurisdiction;
  card.querySelectorAll("[data-ivsec]").forEach(el=>el.addEventListener("click",
    ()=>openClause(region, "", el.getAttribute("data-ivsec"), "")));
  const ed=card.querySelector("#ivedit");
  if(ed) ed.addEventListener("click",()=>ivRestart());
}
function ivRestart(){
  const q=_ivQuestion;
  ivReset(); _ivQuestion=q;
  ivMsg('<span class="muted">Starting the scenario again.</span>');
  ivStart(q, true);
}

// Hand off to AI Mode. The scenario travels as SLOTS, not as prose: the server
// re-validates them and composes the "these facts are confirmed" block itself, so what
// the model is told cannot be a scenario the interview could not have produced.
async function ivAnswer(h){
  ivScenarioCard(h);
  await streamAnswer(h.query || _ivQuestion,
                     h.general ? null : {...IV_EMPTY, ...(h.slots||{})},
                     h.jurisdiction || null);
}

// First turn: classify + extract, then ask or answer.
async function ivStart(q, keepQuestion){
  if(!keepQuestion) _ivQuestion=q;
  const pinned=ivSyncFilter();
  if(pinned && !_ivSlots.jurisdiction){
    _ivSlots={..._ivSlots, jurisdiction:pinned};
    // Said explicitly, because the server's acknowledgement only covers what the
    // EXTRACTOR set this turn — a slot filled from the filter would otherwise appear out
    // of nowhere, and the user could not tell why they were never asked.
    ivMsg(`<span class="muted">Using <b>${esc(pinned)}</b> from your jurisdiction filter.</span>`);
  }
  const think=ivMsg('<div class="bar"></div><span class="muted">Working out what you\'re asking…</span>');
  try{
    const r=await ivPost("route", {messages:[{role:"user",content:q}],
                                   slots:_ivSlots, signals:_ivSignals});
    think.remove();
    if(r.status===503){
      // The router is down, not the answer path — so fall through to a plain answer
      // rather than stranding the user in a dead interview.
      ivMsg('<span class="muted">Couldn\'t run the guided scoping just now — answering as asked.</span>');
      await streamAnswer(q, null, null);
      return;
    }
    if(!r.ok) throw new Error("HTTP "+r.status);
    const d=await r.json();
    // Only a permitted-activity question is worth narrowing the scope for. Anything else
    // — a definition, a capability question, a Data Privacy question that happened to be
    // asked with everything selected — takes the plain path untouched.
    if(ivAmbiguous()){
      if(d.intent!=="permitted_activity"){ ivReset(); return streamAnswer(q, null, null); }
      return ivAskProduct(d);
    }
    await ivApply(d);
  }catch(e){
    think.remove();
    await streamAnswer(q, null, null);
  }
}

// A typed turn while a question is pending. Triaged first, because the three lanes need
// three different handlings and getting it wrong loses the user's answers.
async function ivTyped(q){
  const think=ivMsg('<div class="bar"></div><span class="muted">One moment…</span>');
  let lane="answer", answer=null, hasAnswer=false;
  try{
    const r=await ivPost("triage", {messages:[{role:"user",content:q}],
                                    pending_field:_ivPending,
                                    jurisdiction:_ivSlots.jurisdiction});
    if(r.ok){ const d=await r.json(); lane=d.lane; answer=d.answer; hasAnswer=!!d.has_answer; }
  }catch(e){ /* treat as an answer: the extractor validates whatever it finds anyway */ }
  think.remove();

  if(lane==="scoping_question"){
    // Explained from the controlled definitions, and the interview is untouched: the same
    // question is asked again below. A MIXED turn also states a value, so it still goes
    // to the extractor — otherwise the value is dropped on the floor.
    if(answer) ivMsg(mdToHtml(answer));
    if(hasAnswer){ await ivExtract(q); return; }
    const back=_ivPending;
    _ivPending=null;
    if(back) await ivReask(back);
    return;
  }
  if(lane==="route_onward"){
    // A genuine regulatory question. The interview is PARKED, not abandoned — losing the
    // answers to a useful aside is the thing that makes a guided flow untrustworthy.
    _ivSuspended={slots:{..._ivSlots}, signals:{..._ivSignals},
                  pending:_ivPending, question:_ivQuestion};
    _ivPending=null;
    await streamAnswer(q, null, _ivSlots.jurisdiction||null);
    const r=ivMsg('<button class="ivresume" id="ivresume">↩ Pick up where you left off</button>', "msg ai");
    const btn=r.querySelector("#ivresume");
    if(btn) btn.addEventListener("click",()=>ivResume());
    return;
  }
  await ivExtract(q);
}

// Re-ask the pending field without a model call — the answers have not changed.
async function ivReask(field){
  try{
    const r=await ivPost("next", {slots:_ivSlots, signals:_ivSignals,
                                  original_question:_ivQuestion, other_kind:_ivOther});
    if(r.ok) await ivApply(await r.json());
  }catch(e){ /* the card stays on screen; the user can type instead */ }
}

// Free text that is an answer: the whole conversation goes back to the extractor, which
// re-reads it and maps wording onto the controlled values.
async function ivExtract(q){
  const think=ivMsg('<div class="bar"></div><span class="muted">Noting that…</span>');
  try{
    const r=await ivPost("route", {messages:[{role:"user",content:_ivQuestion},
                                             {role:"user",content:q}],
                                   slots:_ivSlots, signals:_ivSignals});
    think.remove();
    if(!r.ok) throw new Error("HTTP "+r.status);
    const d=await r.json();
    // Nothing new landed and the same field is still pending: say so, rather than
    // repainting an identical card and looking stuck.
    const before=_ivPending;
    await ivApply(d);
    if(before && _ivPending===before && !d.acknowledgement)
      ivMsg('<span class="muted">I couldn\'t map that to one of the options — pick one above, or rephrase.</span>');
  }catch(e){
    think.remove();
    ivMsg('<span class="muted">The interview is temporarily unavailable. Please try again.</span>');
  }
}

function ivResume(){
  if(!_ivSuspended) return;
  _ivSlots=_ivSuspended.slots; _ivSignals=_ivSuspended.signals;
  _ivQuestion=_ivSuspended.question;
  const back=_ivSuspended.pending;
  _ivSuspended=null;
  ivMsg('<span class="muted">Back to the scenario.</span>');
  if(back) ivReask(back); else ivStart(_ivQuestion, true);
}

// The chat's entry point: decide whether this turn is interview business at all, then
// either collect the missing scenario or answer as asked. Splitting the send from the
// STREAM (streamAnswer, below) is what lets the interview hand a composed query to the
// same answer path the plain case uses — one streaming implementation, not two.
async function sendChat(q){
 q=(q||"").trim(); if(!q) return;
 const m=document.getElementById("msgs");
 if(m.querySelector(".hero")) m.innerHTML="";
 m.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div>`);
 scrollMsgs();
 if(ivApplies() || ivAmbiguous()){
   ivSyncFilter();
   if(_ivPending) return ivTyped(q);
   return ivStart(q);
 }
 // Scope moved off this product entirely: any half-collected scenario is about a question
 // that is no longer being asked.
 if(_ivPending || _ivSlots.jurisdiction) ivReset();
 return streamAnswer(q, null, null);
}

// The streaming answer. `interview` is a confirmed slot set (or null) and travels to the
// server, which re-validates it and composes the confirmed-facts block; `jurisdiction`
// narrows the scope to the one the user actually named.
async function streamAnswer(q, interview, jurisdiction){
 const m=document.getElementById("msgs");
 if(m.querySelector(".hero")) m.innerHTML="";
 m.insertAdjacentHTML("beforeend", `<div class="msg ai"><div class="acts"><div class="acts-line"><span class="ai"></span><span class="acts-text"></span><span class="chev">▾</span></div><div class="acts-list"></div></div><div class="bar"></div><div class="answer-body muted">Working on it…</div><div class="srcwrap"></div></div>`);
 const b=m.lastElementChild, acts=b.querySelector(".acts"), bar=b.querySelector(".bar"), ans=b.querySelector(".answer-body"), sw=b.querySelector(".srcwrap");
 const actsLine=acts.querySelector(".acts-line"), actsIcon=actsLine.querySelector(".ai"), actsText=actsLine.querySelector(".acts-text"), actsList=acts.querySelector(".acts-list");
 // Collapsed by default: the line shows only the latest step, fading in as it changes.
 // Click toggles the full step-by-step history open — same rows this used to show inline.
 actsLine.onclick=()=>acts.classList.toggle("open");
 scrollMsgs();
 const model=document.getElementById("model").value;
 const body={q:q, model:model, session:SID, explain:document.getElementById("explain").checked};
 if(enabledP.size&&enabledP.size<PRODUCTS_ALL.length) body.products=[...enabledP].join(",");
 if(enabledJ.size) body.jurisdictions=[...enabledJ].join(",");
 if(interview) body.interview=interview;
 // The answered jurisdiction OVERRIDES the sidebar filter, matching what the server does
 // with it: the user was asked which jurisdiction and said so, which is more specific
 // than whatever happened to be selected.
 if(jurisdiction) body.jurisdictions=jurisdiction;
 const onEvent=d=>{
   if(d.kind==="activity"){
     actsIcon.textContent=d.icon||"•"; actsText.textContent=d.text;
     actsText.style.animation="none"; void actsText.offsetWidth; actsText.style.animation="";  // re-trigger the fade
     actsList.insertAdjacentHTML("beforeend",`<div class="act"><span class="ai">${d.icon||"•"}</span>${esc(d.text)}</div>`);
     scrollMsgs();
   }
   else if(d.kind==="answer"){ if(bar)bar.remove(); ans.classList.remove("muted"); ans.innerHTML=mdToHtml(d.answer||"");
     ans.querySelectorAll(".cite").forEach(a=>a.onclick=()=>openClause(a.dataset.j, "", a.dataset.k, ""));
     chatSrc(sw,d.sources); scrollMsgs(); }
   else if(d.kind==="error"){ if(bar)bar.remove(); ans.innerHTML=`<span class="muted">${esc(d.text)}</span>`; chatSrc(sw,d.sources); }
 };
 // POST + read the stream, rather than EventSource. EventSource can only GET, which forced
 // the question, the scope CSVs and a claims-bearing stream token into the URL — and the WAF
 // in front of dev/prod rejects a query string over ~2KB with its own 403, so a long
 // question failed before it reached the app. In a body there is no such limit, and the
 // bearer token travels in a header (authFetch refreshes it if it has expired).
 try{
   const r=await authFetch("/api/agent/stream",{method:"POST", signal:null,
     headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
   if(!r.ok||!r.body) throw new Error("HTTP "+r.status);
   const rd=r.body.getReader(), dec=new TextDecoder(); let buf="", finished=false;
   while(!finished){
     const {value,done}=await rd.read();
     if(done) break;
     buf+=dec.decode(value,{stream:true});
     let i;                                  // SSE frames are separated by a blank line
     while((i=buf.indexOf("\n\n"))>=0){
       const frame=buf.slice(0,i); buf=buf.slice(i+2);
       for(const line of frame.split("\n")){
         if(!line.startsWith("data:")) continue;
         let d; try{ d=JSON.parse(line.slice(5)); }catch(e){ continue; }
         if(d.kind==="done"){ finished=true; break; }
         onEvent(d);
       }
     }
   }
   rd.cancel().catch(()=>{});
 }catch(e){
   if(bar)bar.remove();
   ans.innerHTML=`<span class="muted">AI Mode is temporarily unavailable. Please try again.</span>`;
 }
}
document.getElementById("go").onclick=run;
document.getElementById("q").addEventListener("keydown",e=>{if(e.key==="Enter")run();});
document.getElementById("minscore").addEventListener("change",()=>{if(document.getElementById("q").value.trim())run();});
// Footnote navigation. Delegated at the document, because every pane that shows a clause
// re-renders its own innerHTML — a listener bound to the block would die with it. The
// superscript finds its citation INSIDE THE SAME clause container, so two clauses on
// screen (a result and its sub-clauses) cannot steal each other's footnote 3.
// Run bar (Extraction tab). Delegated on document rather than attached per render, because
// renderExtraction rebuilds that bar on every poll — a per-render listener would be re-added
// each time on a node that is about to be replaced.
document.addEventListener("click",e=>{
  const b=e.target.closest&&e.target.closest("button[data-exrun]");
  if(!b) return;
  const run=b.getAttribute("data-exrun");
  if(b.getAttribute("data-exact")==="gallery") openGalleryRun(run); else pickExVersion(run);
});

// Jobs table (Extraction tab). Delegated for the same reason as the run bar above — the table is
// rebuilt on every poll, so a listener bound per render would be attached to a doomed node.
//
// The key rides in data-exjob rather than an inline onclick: a document label is arbitrary text
// and JSON.stringify emits double quotes, which is precisely the bug that left the version picker
// silently dead for as long as it existed (see exVerBtn).
document.addEventListener("click",e=>{
  if(!e.target.closest) return;
  const f=e.target.closest("button[data-exjobf]");
  if(f){ EXJOBFILTER=f.getAttribute("data-exjobf"); EXJOBMORE=false;
         if(_exData) renderExtraction(_exData,null); return; }
  if(e.target.closest("button[data-exjobmore]")){
    EXJOBMORE=true; if(_exData) renderExtraction(_exData,null); return; }
  // A node in the trace graph selects that node. Checked BEFORE the row, because the graph lives
  // inside the expanded row and a bare row handler would collapse the thing being inspected.
  const nd=e.target.closest("[data-exnode]");
  if(nd){
    const nrow=nd.closest("tr[data-exjob]");
    if(nrow){
      const jk=nrow.getAttribute("data-exjob"), nk=nd.getAttribute("data-exnode");
      EXNODESEL[jk]=(EXNODESEL[jk]===nk)?null:nk;
      if(_exData) renderExtraction(_exData,null);
      return;
    }
  }
  const row=e.target.closest("tr[data-exjob]");
  // A row is a toggle, but the cells carry real links (view / scorecard / retry). Clicking one of
  // those must open the link, not collapse the row underneath it.
  if(row && !e.target.closest("a")) exOpenJob(row.getAttribute("data-exjob"));
});
// The graph nodes are focusable, so they must also be operable from the keyboard.
document.addEventListener("keydown",e=>{
  if(e.key!=="Enter"&&e.key!==" ") return;
  const nd=e.target.closest&&e.target.closest("[data-exnode]");
  if(!nd) return;
  const nrow=nd.closest("tr[data-exjob]");
  if(!nrow) return;
  const jk=nrow.getAttribute("data-exjob"), nk=nd.getAttribute("data-exnode");
  EXNODESEL[jk]=(EXNODESEL[jk]===nk)?null:nk;
  e.preventDefault();
  if(_exData) renderExtraction(_exData,null);
});
document.addEventListener("input",e=>{
  if(e.target && e.target.id==="exjobq"){
    EXJOBQ=e.target.value; EXJOBMORE=false;
    if(_exData) renderExtraction(_exData,null);
  }
});

document.addEventListener("click",e=>{
  const ref=e.target.closest("sup.fnref[data-fn]");
  if(ref){
    const scope=ref.closest(".ddbody,.detail,#detail,#pane,body");
    const item=scope&&scope.querySelector(`.fnitem[id$="-${CSS.escape(ref.dataset.fn)}"]`);
    if(item){
      item.scrollIntoView({block:"center",behavior:"smooth"});
      item.classList.add("fnhit"); setTimeout(()=>item.classList.remove("fnhit"),1600);
    }
    e.preventDefault(); return;
  }
  const back=e.target.closest("a.fnback[data-fnback]");
  if(back){
    const scope=back.closest(".ddbody,.detail,#detail,#pane,body");
    const src=scope&&scope.querySelector(`sup.fnref[data-fn="${CSS.escape(back.dataset.fnback)}"]`);
    if(src){
      src.scrollIntoView({block:"center",behavior:"smooth"});
      src.classList.add("fnhit"); setTimeout(()=>src.classList.remove("fnhit"),1600);
    }
    e.preventDefault();
  }
});
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>setMode(t.dataset.mode)));
document.getElementById("modalx").onclick=closeModal;
document.getElementById("modal").addEventListener("click",e=>{if(e.target.id==="modal")closeModal();});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});
document.getElementById("chatsend").onclick=()=>{const v=document.getElementById("chatq").value;document.getElementById("chatq").value="";sendChat(v);};
document.getElementById("chatq").addEventListener("keydown",e=>{if(e.key==="Enter"){const v=e.target.value;e.target.value="";sendChat(v);}});

// A query carried in from another app (e.g. the playground's search box) or held
// across the login redirect: prefill and run it once the index is ready.
function applyQueryParam(){
 const q=new URLSearchParams(window.location.search).get("q") || sessionStorage.getItem("aci_q");
 if(!q) return;
 sessionStorage.removeItem("aci_q");
 const el=document.getElementById("q"); if(!el) return;
 el.value=q; run();
}

(async()=>{
 if(await initAuth()===false) return;   // halts here when redirecting to Keycloak
 try{
   const me=await (await authFetch("/api/me")).json();   // platform is admin-only
   if(!(me.has_access ?? me.is_admin)){ showAdminGate(); return; }
 }catch(e){ /* /api/me unreachable: fall through (e.g. auth disabled) */ }
 await loadRegions();
 await loadAi();
 applySplit();
 revealPromptsTab();          // fire and forget: a hidden tab must not delay first paint
 applyQueryParam();
})();
</script>
</body></html>
"""

# The trace graphs are shared with scripts/pipeline_monitor.py, so they are spliced in
# rather than written here twice -- see service/trace_graph.py and
# service/summary_trace_graph.py for why. TWO graphs: the spine almost every document
# walks, and the summary AI route's own three-node path, which never touches the spine.
PAGE = (_PAGE_TMPL.replace("/*TRACE_CSS*/", trace_graph.CSS + summary_trace_graph.CSS)
                  .replace("/*TRACE_JS*/", trace_graph.JS + summary_trace_graph.JS))
