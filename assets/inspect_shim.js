/* inspect_shim — lets the extraction dashboard's per-document page (Scorecard /
   Document / MinerU Inspector / Validation / Stage 4 · AI / Page Review) run as ONE
   frozen file with no server behind it.

   The page itself is the dashboard's, unmodified (scripts/hybrid_extract_ui.py owns
   it). Everything it asks a server for is answered here instead:

     fetch('/api/jobs/<id>/…')  -> the payload, embedded gzipped in #emb-gz
     <img src="…/page_image/N">  -> rendered client-side with pdf.js from the PDF
     <img src="…/page_bbox_image/N">   embedded in #emb-pdf, with Rule B's geometry
     <img src="…/assets/x.png">        drawn on top — the deployed app has no
                                       PyMuPDF and never re-parses a PDF, so the
                                       dashboard's server-side renders can't run.

   Loaded BEFORE the dashboard's own script, so the shimmed fetch is already in
   place by the time load() runs. */
(function () {
  const el = id => document.getElementById(id);
  const b64bytes = s => {
    const raw = atob(s.trim());
    const a = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) a[i] = raw.charCodeAt(i);
    return a;
  };

  let EMB = null;
  const READY = (async () => {
    if (typeof DecompressionStream !== 'function')
      throw new Error('this browser cannot ungzip the embedded data (DecompressionStream)');
    const stream = new Blob([b64bytes(el('emb-gz').textContent)]).stream()
      .pipeThrough(new DecompressionStream('gzip'));
    EMB = JSON.parse(await new Response(stream).text());
    return EMB;
  })();
  READY.catch(e => {
    document.body.insertAdjacentHTML('afterbegin',
      '<div style="padding:14px;background:#2a1414;color:#ff8080">Could not read the embedded '
      + 'extraction data: ' + (e && e.message || e) + '</div>');
  });

  // ---------- the tabs' fetches, served from the embedded payloads ----------
  const API_RE = /\/api\/jobs\/[^/]+\/?([^?#]*)/;
  const json = (o, status) => new Response(JSON.stringify(o),
    { status: status || 200, headers: { 'Content-Type': 'application/json' } });
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = String(input && input.url ? input.url : input);
    const m = url.match(API_RE);
    if (!m) return realFetch(input, init);
    await READY;
    // dismiss/restore write to a live job dir + dismissal store, which a published
    // document has neither of. The POST is a no-op; the caller's re-render then
    // shows the same scorecard back, and the buttons are hidden below anyway.
    if (init && init.method && init.method.toUpperCase() !== 'GET') return json({ status: 'unavailable' });
    const key = m[1] || 'job';
    // The stage switcher asks for `data.json?stage=4`, so the query is part of the key —
    // stripping it served the same stage-3 tree whichever stage was clicked, and the
    // switcher looked broken while working perfectly. Falls back to the bare key so a page
    // built before the per-stage payloads existed still answers.
    const q = url.indexOf('?') >= 0 ? url.slice(url.indexOf('?')) : '';
    if (q && (key + q) in EMB.api) return json(EMB.api[key + q]);
    if (key in EMB.api) return json(EMB.api[key]);
    return json({ error: 'not published: ' + key }, 404);
  };

  // ---------- page images, rendered from the embedded PDF ----------
  const PDFJS = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/';
  const SCALE = 1.6;                      // the zoom the dashboard's fitz render used
  // same colours check_scorecard / the dashboard draw with (fitz 0-1 floats -> rgb)
  const GEO = { confident: '#3ddb75', uncertain: '#f0a83d', missing: '#ff6b6b' };
  const PLACEHOLDER = '#4f8ff7';

  let docP = null;
  function pdfDoc() {
    if (!docP) docP = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = PDFJS + 'pdf.min.js';
      s.onload = () => {
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS + 'pdf.worker.min.js';
        window.pdfjsLib.getDocument({ data: b64bytes(el('emb-pdf').textContent) })
          .promise.then(resolve, reject);
      };
      s.onerror = () => reject(new Error('pdf.js failed to load'));
      document.head.appendChild(s);
    });
    return docP;
  }

  /* Rule B geometry for one page: the pdf2mdtree placeholder bbox (blue) and the
     MinerU block(s) matched to it (green confident / amber uncertain / red none) —
     the same overlay the dashboard's page_bbox_image draws server-side. */
  function boxesFor(pno) {
    const out = [];
    for (const t of ((EMB.api['tables_detail.json'] || {}).tables) || []) {
      if (!t.bbox || (t.pages || []).indexOf(pno) < 0) continue;
      out.push({ r: t.bbox, color: PLACEHOLDER, width: 1.6 });
      for (const mb of t.mineru_bboxes || [])
        if (mb) out.push({ r: mb, color: GEO[t.match_status] || GEO.missing, width: 1.6, dash: true });
      if (t.match_status === 'missing') out.push({ r: t.bbox, color: GEO.missing, width: 2.2 });
    }
    return out;
  }

  const rendered = new Map();
  function pageImage(pno, withBoxes) {
    const key = pno + (withBoxes ? ':bbox' : '');
    if (!rendered.has(key)) rendered.set(key, (async () => {
      const doc = await pdfDoc();
      const page = await doc.getPage(Math.max(1, Math.min(doc.numPages, pno)));
      const vp = page.getViewport({ scale: SCALE });
      const canvas = document.createElement('canvas');
      canvas.width = vp.width; canvas.height = vp.height;
      const ctx = canvas.getContext('2d');
      await page.render({ canvasContext: ctx, viewport: vp }).promise;
      if (withBoxes) {
        await READY;
        // fitz rects and pdf.js's unrotated viewport share a top-left origin, so the
        // page-point coords scale straight into canvas pixels
        for (const b of boxesFor(pno)) {
          ctx.strokeStyle = b.color; ctx.lineWidth = b.width;
          ctx.setLineDash(b.dash ? [3, 2] : []);
          ctx.strokeRect(b.r[0] * SCALE, b.r[1] * SCALE,
                         (b.r[2] - b.r[0]) * SCALE, (b.r[3] - b.r[1]) * SCALE);
        }
      }
      return canvas.toDataURL('image/png');
    })());
    return rendered.get(key);
  }

  const IMG_RE = /\/api\/jobs\/[^/]+\/(page_image|page_bbox_image|assets)\/([^/?#]+)/;
  function swapImage(img) {
    const src = img.getAttribute('src');
    if (!src || img.dataset.embSwap) return;
    const m = src.match(IMG_RE);
    if (!m) return;
    img.dataset.embSwap = '1';
    img.removeAttribute('src');           // nothing would answer it — don't try
    if (m[1] === 'assets') {
      // page snapshots the markdown links to, embedded as data URIs
      READY.then(() => { const d = EMB.assets[decodeURIComponent(m[2])]; if (d) img.src = d; });
      return;
    }
    pageImage(parseInt(m[2], 10), m[1] === 'page_bbox_image')
      .then(url => { img.src = url; })
      .catch(e => { img.alt = 'page render failed: ' + (e && e.message || e); });
  }
  function scan(node) {
    if (node.nodeType !== 1) return;
    if (node.tagName === 'IMG') swapImage(node);
    else if (node.querySelectorAll) node.querySelectorAll('img').forEach(swapImage);
  }
  new MutationObserver(muts => {
    for (const mu of muts) for (const n of mu.addedNodes) scan(n);
  }).observe(document.documentElement, { childList: true, subtree: true });

  // ---------- "Open viewer" ----------
  // The dashboard's own button is window.open('/api/jobs/<id>/viewer.html') -- a NAVIGATION,
  // which the fetch shim above never sees, to a path that does not exist in a published
  // document. A blob: document resolves it against its own opaque base, so the new tab lands
  // on a blank page: no request, no error, nothing on screen. It worked locally only because
  // there the dashboard really is serving that route.
  //
  // The viewer DOES exist -- built beside this page by the same worker -- but only the app
  // that opened this document knows its URL, so ask the app rather than guessing at one.
  // Two containers, two channels: the Doc Library opens this page in an IFRAME (parent), the
  // extraction monitor opens it in a TAB (opener, and `window.name` carries which document,
  // stamped by exOpenReview before the blob is loaded). Neither present -- someone opened the
  // file on their own -- leaves the button inert rather than opening a blank tab; the
  // viewer.html beside it is the thing to open by hand.
  const VIEWER_URL = /\/api\/jobs\/[^/]+\/viewer\.html/;
  const realOpen = window.open.bind(window);
  window.open = function (url, ...rest) {
    if (!VIEWER_URL.test(String(url || ''))) return realOpen(url, ...rest);
    if (parent !== window) parent.postMessage({ aci: 'gal-viewer' }, '*');
    else if (window.opener) window.opener.postMessage({ aci: 'ex-viewer', ref: window.name }, '*');
    return null;
  };

  // Escape in the dashboard navigates the frame to "/", which is not where this page
  // lives. Capture it first and hand it to the app shell instead.
  window.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const t = document.activeElement;
    if (t && ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) return;
    e.stopImmediatePropagation();
    parent.postMessage({ aci: 'gal-back' }, '*');
  }, true);

  // Must run AFTER the rest of the document has parsed. A bare setTimeout(0) is not
  // enough: this shim is injected at the top of <body>, the parser yields while
  // reading the (large) rest of the document, and the timer fires mid-parse — before
  // elements like #back even exist. DOMContentLoaded is the first point at which
  // every inline script in the document has run and the full DOM is in place.
  document.addEventListener('DOMContentLoaded', () => {
    const style = document.createElement('style');
    style.textContent = '#score-body .find-btn{display:none}';   // read-only: no dismissals
    document.head.appendChild(style);

    const back = document.getElementById('back');
    if (back) back.onclick = e => { e.preventDefault(); parent.postMessage({ aci: 'gal-back' }, '*'); };
  }, 0);
})();
