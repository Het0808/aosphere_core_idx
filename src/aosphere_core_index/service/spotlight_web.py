"""Spotlight-style search page (served at /spotlight).

Clean command-palette look: white panel, plain rows (icon / title with bold
matched terms / one gray meta line with dot separators / chevron), uppercase
group headers with a hairline rule, keyboard-hint footer, and a "Deep search"
hand-off row into the main semantic search UI. Ghost-text autocomplete (Tab
completes). Meta segments (template / subject / product) click to filter;
active filters render as small gray pills under the field.
Auth mirrors the main page (Keycloak OIDC; open when auth is disabled).
"""

SPOTLIGHT_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aosphere Spotlight</title>
<script src="https://cdn.jsdelivr.net/npm/oidc-client-ts@2.4.0/dist/browser/oidc-client-ts.min.js"></script>
<style>
  :root{
    --accent:#4f46e5; --txt:#1f2430; --dim:#8b93a3; --dim2:#b3b9c6;
    --ghost:#c3c8d4; --line:#eceef2; --hover:#f4f5f7;
  }
  *{box-sizing:border-box} html,body{margin:0;height:100%}
  body{color:var(--txt);
       font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:#e9ebef}
  .center{display:flex;justify-content:center;padding:10vh 16px 40px}
  .panel{width:760px;max-width:96vw;background:#fff;border-radius:16px;
         overflow:hidden;box-shadow:0 18px 60px rgba(30,35,50,.22)}
  /* ---- search field ---- */
  .field{position:relative;height:64px;border-bottom:1px solid var(--line)}
  #ghost,#q{position:absolute;inset:0;width:100%;height:100%;padding:0 88px 0 58px;
            font:inherit;font-size:21px;line-height:64px;white-space:pre;overflow:hidden}
  #ghost{color:var(--ghost);pointer-events:none}
  #ghost b{color:transparent}
  #q{background:transparent;border:0;outline:0;color:var(--txt);caret-color:var(--accent)}
  #q::placeholder{color:var(--dim2)}
  .mag{position:absolute;left:20px;top:21px;width:22px;height:22px;color:var(--dim)}
  #clear{position:absolute;right:18px;top:20px;width:24px;height:24px;border:0;
         background:transparent;color:var(--dim);font-size:20px;cursor:pointer;
         display:none;padding:0;line-height:1}
  .tabhint{position:absolute;right:50px;top:21px;font-size:10px;color:var(--dim);
           border:1px solid var(--line);border-radius:5px;padding:3px 6px;display:none}
  /* ---- active filters ---- */
  #fbar{display:none;flex-wrap:wrap;gap:6px;padding:10px 22px;border-bottom:1px solid var(--line)}
  #fbar.open{display:flex}
  .fpill{font-size:12px;color:var(--txt);background:var(--hover);border:1px solid var(--line);
         border-radius:6px;padding:2px 8px;display:inline-flex;gap:6px;align-items:center;cursor:pointer}
  .fpill .x{color:var(--dim);font-size:14px} .fpill:hover .x{color:var(--txt)}
  /* ---- results ---- */
  #out{max-height:62vh;overflow-y:auto;overscroll-behavior:contain;display:none;padding:6px 0}
  #out.open{display:block}
  .ghead{display:flex;align-items:center;gap:12px;margin:14px 22px 4px;
         font-size:11px;font-weight:600;letter-spacing:.12em;color:var(--dim)}
  .ghead::after{content:"";flex:1;height:1px;background:var(--line)}
  .hit{display:flex;gap:16px;align-items:center;padding:10px 22px;cursor:default}
  .hit.sel{background:var(--hover)}
  .icon{flex:0 0 40px;height:40px;border-radius:10px;background:#f1f2f5;color:#6b7280;
        display:flex;align-items:center;justify-content:center}
  .icon svg{width:18px;height:18px}
  .body{min-width:0;flex:1}
  .t{font-size:15.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .t em{font-style:normal;font-weight:700}
  .m{font-size:13px;color:var(--dim);margin-top:1px;white-space:nowrap;
     overflow:hidden;text-overflow:ellipsis}
  .m em{font-style:normal;font-weight:600;color:#6d7382}
  .m .sep{margin:0 6px;color:var(--dim2)}
  .m .fseg{cursor:pointer} .m .fseg:hover{text-decoration:underline;color:var(--txt)}
  .chev{flex:0 0 auto;color:var(--dim2);visibility:hidden}
  .hit.sel .chev{visibility:visible}
  /* deep search hand-off */
  .deep{display:flex;gap:16px;align-items:center;margin:12px 16px 10px;padding:12px 16px;
        background:#eef0fe;border-radius:12px;cursor:pointer}
  .deep.sel,.deep:hover{background:#e3e6fd}
  .dicon{flex:0 0 40px;height:40px;border-radius:10px;background:var(--accent);color:#fff;
         display:flex;align-items:center;justify-content:center}
  .dt{color:var(--accent);font-weight:600;font-size:15px}
  .dd{color:#7a80e8;font-size:13px}
  /* footer */
  .foot{display:none;justify-content:center;gap:18px;align-items:center;
        padding:10px 22px;border-top:1px solid var(--line);font-size:12.5px;color:var(--dim)}
  .foot.open{display:flex}
  .key{border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-size:11px;
       background:#fafbfc;margin-right:4px}
  .empty,.gate{color:var(--dim);text-align:center;padding:26px 0;font-size:14px}
  .authbtn{font:inherit;font-size:13px;color:var(--txt);cursor:pointer;background:var(--hover);
           border:1px solid var(--line);border-radius:7px;padding:5px 12px;margin-top:10px}
  #authbox{position:fixed;top:14px;right:18px;font-size:12px;color:#5c6370;
           display:flex;gap:8px;align-items:center}
  #authbox .authbtn{margin:0;padding:3px 9px;font-size:12px}
</style>
</head>
<body>
<div id="authbox"></div>
<div class="center">
  <div class="panel">
    <div class="field">
      <svg class="mag" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="11" cy="11" r="7"/><path d="m20 20-3.8-3.8"/></svg>
      <div id="ghost"></div>
      <input id="q" autocomplete="off" spellcheck="false" placeholder="Search">
      <span class="tabhint" id="tabhint">TAB</span>
      <button id="clear" title="Clear">&times;</button>
    </div>
    <div id="fbar"></div>
    <div id="out"></div>
    <div class="foot" id="foot">
      <span><span class="key">&uarr;</span><span class="key">&darr;</span> to navigate</span>
      <span><span class="key">&crarr;</span> to open</span>
      <span><span class="key">TAB</span> to complete</span>
    </div>
  </div>
</div>
<script>
"use strict";
/* ---------- auth (identical lifecycle to the main core-index page) ---------- */
let USER=null, _um=null;
async function initAuth(){
  let cfg; try{ cfg=await (await fetch("/api/config")).json(); }catch(e){ return true; }
  if(!cfg.auth_enabled) return true;
  const {UserManager, WebStorageStateStore} = window.oidc;
  const redirect = window.location.origin + window.location.pathname;
  _um = new UserManager({
    authority:`${cfg.keycloak_url}/realms/${cfg.realm}`, client_id:cfg.client_id,
    redirect_uri:redirect, post_logout_redirect_uri:redirect,
    response_type:"code", scope:"openid profile email",
    userStore:new WebStorageStateStore({store:window.localStorage}),
    automaticSilentRenew:true,
  });
  const p=new URLSearchParams(window.location.search);
  if(p.get("code")||p.get("error")){
    try{ await _um.signinRedirectCallback(); }catch(e){}
    history.replaceState({},"",window.location.pathname);
  }
  let u=await _um.getUser();
  if(u&&u.expired){ try{ u=await _um.signinSilent(); }catch(e){ u=null; } }
  if(!u){
    const v=(document.getElementById("q")||{}).value;
    if(v&&v.trim()) sessionStorage.setItem("aci_spot_q", v.trim());
    try{ await _um.signinRedirect(); }catch(e){ showGate(); return false; }
    return new Promise(()=>{});
  }
  USER=u; renderAuthbox();
  return true;
}
function renderAuthbox(){
  const b=document.getElementById("authbox"); if(!b||!USER) return;
  const name=(USER.profile&&(USER.profile.preferred_username||USER.profile.email))||"";
  b.innerHTML=`<span>${esc(name)}</span><button class="authbtn" id="logout">Logout</button>`;
  document.getElementById("logout").onclick=()=>{ try{_um.signoutRedirect();}catch(e){} };
}
function showGate(){
  show(`<div class="gate">Sign in to use Spotlight search.<br><button class="authbtn" onclick="location.reload()">Sign in</button></div>`);
}
function showAdminGate(){
  q.disabled=true;
  show(`<div class="gate"><b>Restricted.</b><br>Access to the Core Index hasn't been granted for this account.<br>Contact an aosphere administrator to be added to the access list.<br><button class="authbtn" id="ggout">Sign out</button></div>`);
  const o=document.getElementById("ggout"); if(o&&_um) o.onclick=()=>{try{_um.signoutRedirect();}catch(e){}};
}
async function authToken(){
  if(!_um) return null;
  let u=await _um.getUser();
  if(u&&u.expired){ try{ u=await _um.signinSilent(); }catch(e){} }
  return u ? u.access_token : null;
}
async function authFetch(url,opts){
  opts=opts||{};
  const t=await authToken();
  if(t) opts.headers=Object.assign({},opts.headers||{},{Authorization:"Bearer "+t});
  const r=await fetch(url,opts);
  if(r.status===401 && _um){
    try{ await _um.signinSilent(); }catch(e){}
    const t2=await authToken();
    if(t2 && t2!==t){
      opts.headers=Object.assign({},opts.headers||{},{Authorization:"Bearer "+t2});
      return fetch(url,opts);
    }
  }
  return r;
}
/* ---------- entity types ---------- */
const ICONS={
  product :'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 8 12 3 3 8v8l9 5 9-5V8Z"/><path d="M3 8l9 5 9-5M12 13v8"/></svg>',
  opinion :'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M9 13h6M9 17h6"/></svg>',
  template:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>',
  question:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.4 2.3c-.8.34-1.4 1-1.4 1.9v.3M12 17h.01"/></svg>',
  subject :'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1Z"/></svg>',
  jurisdiction:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>'};
const LABEL={jurisdiction:"JURISDICTIONS",product:"PRODUCTS",opinion:"OPINIONS",
             template:"TEMPLATES",question:"QUESTIONS",subject:"SUBJECTS"};
const ORDER=["jurisdiction","product","opinion","template","question","subject"];
/* ---------- state ---------- */
const q=document.getElementById("q"), ghost=document.getElementById("ghost"),
      out=document.getElementById("out"), hint=document.getElementById("tabhint"),
      fbar=document.getElementById("fbar"), foot=document.getElementById("foot"),
      clearBtn=document.getElementById("clear");
let completion="", tSug=null, tRes=null, acSug=null, acRes=null, selIdx=-1, flat=[];
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const enc=encodeURIComponent;
const FILTERS={products:new Set(),templates:new Set(),subjects:new Set(),
               categories:new Set(),jurisdictions:new Set()};
function filterQS(){
  return Object.entries(FILTERS).filter(([,s])=>s.size)
    .map(([k,s])=>`&${k}=${enc([...s].join(","))}`).join("");
}
function renderFilters(){
  const pills=Object.entries(FILTERS).flatMap(([k,s])=>[...s].map(v=>
    `<span class="fpill" data-k="${k}" data-v="${esc(v)}">${esc(v)}<span class="x">&times;</span></span>`));
  fbar.innerHTML=pills.join("");
  fbar.classList.toggle("open",pills.length>0);
  fbar.querySelectorAll(".fpill").forEach(el=>el.onclick=()=>{
    FILTERS[el.dataset.k].delete(el.dataset.v); renderFilters(); rerun();
  });
}
function addFilter(k,v){ if(!FILTERS[k].has(v)){ FILTERS[k].add(v); renderFilters(); rerun(); } }
function rerun(){ const v=q.value.trim(); if(v) fetchResults(v); else show(""); }
function show(html){
  out.innerHTML=html; out.classList.toggle("open",!!html);
  foot.classList.toggle("open",!!html && !html.includes('class="gate"'));
}
/* ---------- search ---------- */
function setGhost(){
  if(completion && completion.toLowerCase().startsWith(q.value.toLowerCase()) && q.value){
    ghost.innerHTML="<b>"+esc(q.value)+"</b>"+esc(completion.slice(q.value.length));
    hint.style.display="inline";
  } else { ghost.innerHTML=""; hint.style.display="none"; }
}
async function fetchSuggest(v){
  if(acSug) acSug.abort(); acSug=new AbortController();
  try{
    const r=await authFetch(`/api/entity-search?q=${enc(v)}&suggest=1`,{signal:acSug.signal});
    if(!r.ok) return;
    const d=await r.json();
    if(q.value.trim()!==v) return;
    const m=(d.suggestions||[]).find(s=>s.title.toLowerCase().startsWith(v.toLowerCase()));
    completion=m?m.title:""; setGhost();
  }catch(e){}
}
async function fetchResults(v){
  if(acRes) acRes.abort(); acRes=new AbortController();
  try{
    const r=await authFetch(`/api/entity-search?q=${enc(v)}&limit=20${filterQS()}`,{signal:acRes.signal});
    if(!r.ok){ show('<div class="empty">Search unavailable.</div>'); return; }
    const d=await r.json();
    if(q.value.trim()!==v) return;
    render(d);
  }catch(e){ if(e.name!=="AbortError") show('<div class="empty">Search failed.</div>'); }
}
/* ---------- render ---------- */
function render(d){
  selIdx=-1; flat=[];
  const groups=d.groups||{};
  let html="";
  for(const t of ORDER){
    const hits=groups[t]||[]; if(!hits.length) continue;
    html+=`<div class="ghead">${LABEL[t]}</div>`;
    for(const h of hits){
      const idx=flat.length; flat.push(h);
      const HL=h.highlights||[];
      const hlHtml=(path,fallback)=>{
        const e=HL.filter(x=>x.path===path);
        if(!e.length) return fallback==null?"":esc(fallback);
        return e.map(en=>en.texts.map(x=>x.type==="hit"
          ?`<em>${esc(x.value)}</em>`:esc(x.value)).join("")).join(" &hellip; ");
      };
      html+=`<div class="hit" data-i="${idx}">
        <div class="icon">${ICONS[t]}</div>
        <div class="body"><div class="t">${hlHtml("title",h.title)}</div>
        ${metaLine(t,h,HL)}</div>
        <svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 6 6 6-6 6"/></svg>
      </div>`;
    }
  }
  if(html){
    html+=`<div class="deep" data-i="${flat.length}">
      <div class="dicon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/><circle cx="12" cy="12" r="4"/></svg></div>
      <div><div class="dt">Deep search</div>
      <div class="dd">Search across all memoranda and legal content for &ldquo;${esc(q.value.trim())}&rdquo;</div></div>
    </div>`;
    flat.push({deep:true});
  }
  show(html||'<div class="empty">No results</div>');
  out.querySelectorAll(".hit,.deep").forEach(el=>{
    el.onclick=()=>pick(+el.dataset.i);
    el.onmousemove=()=>setSel(+el.dataset.i);
  });
  if(flat.length) setSel(0);
}
/* one gray meta line per entity type, dot-separated, filterable segments */
function metaLine(t,h,HL){
  const m=h.metadata||{};
  const hitVals=path=>HL.filter(x=>x.path===path)
    .flatMap(en=>en.texts.filter(x=>x.type==="hit").map(x=>x.value.toLowerCase()));
  const pickName=(arr,path)=>{
    if(!(arr||[]).length) return null;
    const hits=hitVals(path);
    const isHit=x=>hits.some(v=>String(x.name??x).toLowerCase().includes(v));
    const s=[...arr].sort((a,b)=>isHit(b)-isHit(a))[0];
    const name=s.name??s;
    return isHit(s)?`<em>${esc(name)}</em>`:esc(name);
  };
  const fseg=(html,param,plain)=>html?`<span class="fseg" data-f="${param}" data-v="${esc(plain)}" title="Click to filter">${html}</span>`:null;
  const plain=(arr)=>((arr||[])[0]||{}).name??(arr||[])[0];
  const njur=(m.jurisdictions||[]).length;
  const parts=[];
  if(t==="question"){
    parts.push(fseg(pickName(m.templates,"metadata.templates.name"),"templates",plain(m.templates)));
    parts.push(fseg(pickName(m.subjects,"metadata.subjects.name"),"subjects",plain(m.subjects)));
    const bcs=m.breadcrumbs||[];
    const bhits=hitVals("metadata.breadcrumbs");
    const bc=bcs.find(b=>bhits.some(v=>b.toLowerCase().includes(v)))||bcs[0];
    if(bc) parts.push(esc(bc));
    parts.push(njur?`${njur} jurisdiction${njur>1?"s":""}`:null);
  } else if(t==="opinion"){
    parts.push(fseg(pickName(m.products,"metadata.products.name"),"products",plain(m.products)));
    parts.push((m.jurisdictions||[])[0]?esc(m.jurisdictions[0]):null);
    if(m.effectiveDate) parts.push(esc(String(m.effectiveDate).slice(0,10)));
  } else if(t==="template"){
    parts.push(fseg(pickName(m.products,"metadata.products.name"),"products",plain(m.products)));
    parts.push(njur?`${njur} jurisdictions`:null);
  } else if(t==="product"){
    parts.push(fseg(pickName(m.categories,"metadata.categories.name"),"categories",plain(m.categories)));
    if(m.sfProductFamily) parts.push(esc(m.sfProductFamily));
  } else if(t==="subject"){
    parts.push(fseg(pickName(m.templates,"metadata.templates.name"),"templates",plain(m.templates)));
    parts.push(njur?`${njur} jurisdictions`:null);
  } else if(t==="jurisdiction"){
    const np=(m.products||[]).length;
    parts.push(np?`${np} product${np>1?"s":""}`:null);
    if(m.opinionCount) parts.push(`${m.opinionCount} opinions`);
  }
  const line=parts.filter(Boolean).join('<span class="sep">&middot;</span>');
  return line?`<div class="m">${line}</div>`:"";
}
function setSel(i){
  selIdx=i;
  out.querySelectorAll(".hit,.deep").forEach(el=>el.classList.toggle("sel",+el.dataset.i===i));
  const el=out.querySelector(`[data-i="${i}"]`);
  if(el) el.scrollIntoView({block:"nearest"});
}
function pick(i){
  const h=flat[i]; if(!h) return;
  if(h.deep){ window.location.href="/?q="+enc(q.value.trim()); return; }
  // hook: navigate to the entity once detail pages exist
  console.log("selected", h.entityType, h.sourceId, h.title);
}
/* ---------- events ---------- */
q.addEventListener("input",()=>{
  const v=q.value.trim();
  clearBtn.style.display=q.value?"block":"none";
  completion=completion && q.value && completion.toLowerCase().startsWith(q.value.toLowerCase())
             ? completion : "";
  setGhost();
  clearTimeout(tSug); clearTimeout(tRes);
  if(!v){ show(""); completion=""; setGhost(); return; }
  tSug=setTimeout(()=>fetchSuggest(v),120);
  tRes=setTimeout(()=>fetchResults(v),220);
});
out.addEventListener("click",e=>{
  const s=e.target.closest(".fseg");
  if(s){ e.stopPropagation(); addFilter(s.dataset.f, s.dataset.v); }
},true);
clearBtn.onclick=()=>{ q.value=""; completion=""; setGhost(); show("");
  clearBtn.style.display="none"; q.focus(); };
q.addEventListener("keydown",e=>{
  if(e.key==="Tab" && completion && completion.length>q.value.length){
    e.preventDefault(); q.value=completion; completion=""; setGhost();
    clearTimeout(tRes); fetchResults(q.value.trim());
  } else if(e.key==="ArrowDown"){ e.preventDefault(); if(flat.length) setSel(Math.min(selIdx+1,flat.length-1)); }
  else if(e.key==="ArrowUp"){ e.preventDefault(); if(flat.length) setSel(Math.max(selIdx-1,0)); }
  else if(e.key==="Enter" && selIdx>=0){ pick(selIdx); }
  else if(e.key==="Escape"){ q.value=""; completion=""; setGhost(); show(""); clearBtn.style.display="none"; }
});
function applyQueryParam(){
  const v=new URLSearchParams(window.location.search).get("q")
        || sessionStorage.getItem("aci_spot_q");
  if(!v) return;
  sessionStorage.removeItem("aci_spot_q");
  q.value=v; clearBtn.style.display="block"; fetchSuggest(v.trim()); fetchResults(v.trim());
}
(async()=>{
  if(await initAuth()===false) return;
  try{
    const me=await (await authFetch("/api/me")).json();
    if(me.authenticated && !(me.has_access ?? me.is_admin)){ showAdminGate(); return; }
  }catch(e){}
  q.focus();
  applyQueryParam();
})();
</script>
</body>
</html>"""
