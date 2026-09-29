#!/usr/bin/env python3
"""build_inspect — freeze the extraction dashboard's per-document page into ONE
self-contained HTML file the Doc Gallery can proxy-stream, exactly like the md/PDF
viewer next to it.

Nothing about the page is re-implemented here: the markup and every payload builder
are imported from ``hybrid_extract_ui``, which stays the single source of truth for
what the Scorecard / Document / MinerU Inspector / Validation / Stage 4 · AI / Page
Review tabs show. This module only:

  * runs those builders once, against a finished job directory, and
  * embeds the results (gzipped) plus the source PDF and the Stage-1 page snapshots,
    so ``assets/inspect_shim.js`` can answer the page's fetches and render its page
    images client-side.

Why frozen rather than served: the gallery hands the browser a blob URL (the bytes
are proxied through the app so S3 is never exposed), and a blob document cannot call
the app's authenticated API. The viewer solved this the same way — one file with
everything in it.

  python3 scripts/build_inspect.py out/corpus/106_repoAnalytics/Anguilla__179897 -o /tmp/x.html
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import hybrid_extract_ui as UI  # noqa: E402  — the dashboard IS the template

SHIM = REPO / "assets" / "inspect_shim.js"

# The frozen page has one document, so the dashboard's job id is a constant; the shim
# matches on the path shape (/api/jobs/<anything>/…) rather than this value.
JOB_ID = "published"


def _job(job_dir: Path, title: str) -> dict:
    """The same job dict the dashboard builds when it adopts a corpus document, so
    every payload builder below sees exactly what it sees there."""
    job = {
        "id": JOB_ID, "filename": title, "status": "done", "error": None,
        "created_at": job_dir.stat().st_mtime, "root": str(job_dir),
        "mineru_backend": None, "mineru_effort": None,
        "manifest": None, "stage2_report": None, "stage3_report": None,
    }
    UI._load_corpus_artifacts(job_dir, job)
    return job


def _total_pages(job_dir: Path) -> int | None:
    p = job_dir / "01_stage1_extract" / "stage1_report.json"
    if not p.exists():
        p = job_dir / "corpus_meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("pages")
    except (OSError, json.JSONDecodeError):
        return None


def _tables_detail(job: dict, job_dir: Path) -> dict:
    """Mirror of the dashboard's /tables_detail.json — per-table MinerU input vs
    output. Kept in step with UI.tables_detail(); it reads the same two files."""
    out = []
    for t in (job.get("stage2_report") or {}).get("tables", []):
        d = dict(t)
        if t.get("ok"):
            tdir = job_dir / "02_stage2_mineru_tables" / "tables" / t["table_id"]
            md_p, html_p = tdir / "table.md", tdir / "table.html"
            d["table_md"] = md_p.read_text(encoding="utf-8") if md_p.exists() else ""
            d["table_html"] = html_p.read_text(encoding="utf-8") if html_p.exists() else ""
        d["asset_urls"] = [f"/api/jobs/{JOB_ID}/assets/page-{p:03d}.png" for p in t["pages"]]
        out.append(d)
    return {"tables": out}


def _assets(job_dir: Path) -> dict[str, str]:
    """Stage-1 page snapshots as data URIs — the markdown links to them, so the
    Document and Page Review tabs need them inline."""
    d = job_dir / "01_stage1_extract" / "_assets"
    if not d.is_dir():
        # Imported exports have their snapshots alongside the final Markdown.
        d = next((job_dir / name / "_assets" for name in
                  ("05_subchunks", "04_stage4_ai", "03_stage3_final")
                  if (job_dir / name / "_assets").is_dir()), d)
    if not d.is_dir():
        return {}
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml"}
    out = {}
    for f in sorted(d.iterdir()):
        m = mime.get(f.suffix.lower())
        if f.is_file() and m:
            out[f.name] = f"data:{m};base64," + base64.b64encode(f.read_bytes()).decode()
    return out


SC_FILES = {"extraction": "scorecard.json", "post_ai": "scorecard_post_ai.json"}


def _scorecards(job_dir: Path, job: dict) -> tuple[dict[str, dict], list[str], str]:
    """Both scorecards the page can switch between, keyed by the view name the page asks for.

    TWO FILES, TWO VERDICTS, AND THE PAGE HAS TO BE ABLE TO REACH BOTH. scorecard.json is
    frozen at the end of stage 3 -- it is the --resume completion marker, and a paid AI pass
    that fails must not un-complete a document that extracted cleanly -- so when stage 4/5
    ran, the tree that actually ships is scored in scorecard_post_ai.json instead. The live
    dashboard serves whichever the reviewer asks for (job_scorecard's `view`) and shows a
    switcher whenever both exist; this page embedded ONLY scorecard.json, with no `views` to
    put the switcher on screen. So a published document showed its stage-3 verdict, silently,
    with no way to reach the one e197d72 made the gate -- Japan read completeness 99.6 /
    uniqueness 79.2 here while the monitor beside it read 95.7 / 92.1 for the same document.

    `view` and `views` are what job_scorecard attaches, and the page reads both: `views`
    decides whether the switcher renders at all, and `view` is what the JS syncs SC_VIEW back
    to, so a document with no post-AI scorecard lands on "extraction" by itself.

    `cohort` is deliberately NOT attached. It is a statistic over the PRODUCT's other
    jurisdictions (see _cohort_for), and this builder holds exactly one job dir -- a cohort
    computed from a single member would be a number with nothing behind it.
    """
    views = ["extraction"]
    if (job_dir / SC_FILES["post_ai"]).exists():
        views.append("post_ai")
    # Same default the page takes (SC_VIEW = 'post_ai'): where stage 4/5 ran, that tree is
    # what ships, so it is the verdict a reviewer opening the document should meet first.
    default_view = views[-1]
    out: dict[str, dict] = {}
    for v in views:
        if v == "extraction":
            sc = job.get("scorecard")
        else:
            try:
                sc = json.loads((job_dir / SC_FILES[v]).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
        out[v] = {**(sc or {"gate": "unknown", "error": "scorecard not computed"}),
                  "view": v, "views": views}
    if default_view not in out:                    # unreadable post-AI file: fall back
        default_view = "extraction"
    return out, views, default_view


def _validations(job_dir: Path, job: dict) -> dict[str, dict]:
    """The per-check detail behind each scorecard, keyed by the same view names.

    Mirrors _scorecards: validation.json is the stage-3 gate's working, validation_post_ai
    .json the shipped tree's. A check whose answer depends on which tree it read — does the
    SHIPPED tree still carry every outline heading? — otherwise reports the pre-AI answer
    under the post-AI verdict.
    """
    out = {"extraction": dict(job.get("validation") or {})}
    out["extraction"]["view"] = "extraction"
    p = job_dir / "validation_post_ai.json"
    if p.exists():
        try:
            post = json.loads(p.read_text(encoding="utf-8"))
            post["view"] = "post_ai"
            out["post_ai"] = post
        except (OSError, ValueError):
            pass          # no post-AI detail: _scorecards decides the views, not this
    return out


def payloads(job_dir: Path, title: str) -> dict:
    """Every response the page's tabs fetch, keyed by the path after the job id
    ("job" for the bare /api/jobs/<id>)."""
    job = _job(job_dir, title)
    summary = UI._job_summary(job)
    summary["tables"] = (job.get("stage2_report") or {}).get("tables", []) or []

    # A tree PER STAGE the job has, not just stage 3. The page already ships the switcher —
    # it renders from data.json's `stages_available` and re-fetches with ?stage=N — but this
    # only ever embedded 03_stage3_final, so a reader of an AI-post-processed document got
    # the pass's INPUT under a Stage 4 tab with nothing to switch to.
    #
    # The cost is small where it was feared to be large: the trees are markdown only
    # (_assets is skipped, as the dashboard skips it), ~670KB across three stages, gzipped
    # into a file whose 14.9MB is almost entirely the embedded PDF and its page images.
    available = [n for n, d in UI.STAGE_DIRS.items() if (job_dir / d).is_dir()]
    pages = _total_pages(job_dir)
    trees = {}
    for n in available:
        tree, files = UI._build_tree(job_dir / UI.STAGE_DIRS[n], JOB_ID)
        trees[n] = {"tree": tree, "files": files, "total_pages": pages,
                    "stage": n, "stages_available": available}
    # The default is the HIGHEST stage the job has: that is the document as the pipeline
    # finished it, and what someone opening an AI-post-processed extraction is asking to
    # see. Stage 3 stays one click away as the reference the others are compared against.
    default = trees[available[-1]] if available else {
        "tree": None, "files": {}, "total_pages": pages, "stage": None,
        "stages_available": []}
    from check_stage4 import stage4_dashboard
    scorecards, sc_views, default_view = _scorecards(job_dir, job)
    api = {
        "job": summary,
        "data.json": default,
        "scorecard.json": scorecards[default_view],
        # Per view, and DEFAULTED TO THE SAME VIEW the scorecard is. Both of these used
        # to be baked once, from the extraction view, while the page opens on post_ai --
        # so a published document showed Scorecard 2's verdict with stage 3's page issues
        # and stage 3's per-check numbers underneath it. Measured on Ecuador 32033: the
        # post-AI view has findings on pages 1-3 and the baked payload had page 1 alone,
        # so two of the three pages a reader was sent to looked clean.
        "validation.json": _validations(job_dir, job)[default_view],
        "page_issues.json": UI._build_page_issues(job, view=default_view),
        "tables_detail.json": _tables_detail(job, job_dir),
        # Mirrors the live dashboard's /api/jobs/{id}/stage4.json (job_stage4 in
        # hybrid_extract_ui.py) — a pure function of the job dir on disk, computed here
        # once at publish time instead of never, which is what left the published page's
        # Stage 4 · AI tab permanently 404ing ("not published: stage4.json") after the
        # tab was renamed from Cross-check and wired to this payload everywhere else.
        "stage4.json": stage4_dashboard(job_dir),
    }
    # Keyed WITH the query, because that is what the page asks for when the switcher is
    # used. The shim falls back to the bare key, so a page built before this still works.
    for n, payload in trees.items():
        api[f"data.json?stage={n}"] = payload
    for v, payload in scorecards.items():
        api[f"scorecard.json?view={v}"] = payload
    validations = _validations(job_dir, job)
    for v in sc_views:
        api[f"page_issues.json?view={v}"] = UI._build_page_issues(job, view=v)
        api[f"validation.json?view={v}"] = validations[v]
    return api


def build_inspect_page(job_dir: Path, title: str) -> str:
    job_dir = Path(job_dir)
    blob = {"api": payloads(job_dir, title), "assets": _assets(job_dir)}
    gz = base64.b64encode(gzip.compress(json.dumps(blob).encode("utf-8"), 6)).decode()
    pdf = job_dir / "source.pdf"
    pdf_b64 = base64.b64encode(pdf.read_bytes()).decode() if pdf.exists() else ""
    # base64 in <script> bodies: no escaping needed (no '<' in the alphabet), unlike
    # inlining the JSON, where a stray "</script>" inside extracted text would end the
    # block early and take the rest of the page with it.
    inject = (f'<script id="emb-gz" type="application/base64-gzip">{gz}</script>\n'
              f'<script id="emb-pdf" type="application/base64">{pdf_b64}</script>\n'
              f'<script>\n{SHIM.read_text(encoding="utf-8")}\n</script>\n')
    html = UI.JOB_HTML.format(filename=title, job_id=JOB_ID)
    if "<body>" not in html:
        raise RuntimeError("dashboard template changed: no <body> to inject the shim after")
    return html.replace("<body>", "<body>\n" + inject, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("job_dir", help="a finished job dir (out/corpus/<product>/<jur>__<id>)")
    ap.add_argument("-o", "--out", default=None, help="output HTML (default: <job_dir>/inspect.html)")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()
    job_dir = Path(args.job_dir).resolve()
    title = args.title or f"{job_dir.parent.name} / {job_dir.name}"
    out = Path(args.out) if args.out else job_dir / "inspect.html"
    html = build_inspect_page(job_dir, title)
    out.write_text(html, encoding="utf-8")
    print(f"{out}  {len(html) // 1024}KB")


if __name__ == "__main__":
    main()
