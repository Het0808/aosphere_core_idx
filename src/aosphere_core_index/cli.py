"""Command-line interface for the Step-1 GraphIndex (no vectorization yet).

All outputs are stored per region under data/regions/<Region>/:

  data/regions/France/source/      downloaded docx/pdf + JSON sidecars
  data/regions/France/artifacts/   France.graph.pkl, France.md, France.graphml,
                                   France.reader.html, France.navigator.html

Commands:
  aci regions                      list jurisdictions available in the bucket
  aci build France                 extract -> chunk -> graph -> markdown
  aci reader France                content reader HTML (read text + follow links)
  aci navigate France [--clause K] interactive graph HTML + GraphML export
  aci all France                   build + reader + navigate in one go
  aci summary France               node/edge counts
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import typer
from rich import print as rprint


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from ./.env into the process env.

    Existing environment variables win. Static AWS credential keys are NOT loaded
    here: they would shadow an SSO profile (AWS_PROFILE) used for Bedrock and
    cause expired-credential failures. The build script sources .env directly when
    it needs the read-only source keys.
    """
    skip = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # A trailing " # comment" is common style in .env.example (see most lines in it) and
        # was silently kept as part of the value -- ACI_DATA_DIR=data-local # isolated build
        # resolved to a literal "data-local # isolated build" path, not "data-local". Split on
        # the first " #" only, so a value that legitimately contains "#" with no space before
        # it (none currently do) is left alone.
        val = val.split(" #", 1)[0].strip()
        if key not in skip:
            os.environ.setdefault(key, val)

from aosphere_core_index.chunking.chunker import chunk_document
from aosphere_core_index.config import settings
from aosphere_core_index.embeddings.embedder import make_embedder
from aosphere_core_index.embeddings.section_index import (
    build_section_index,
    load_index,
    save_index,
)
from aosphere_core_index.mapping.semantic_mapper import map_alerts_semantic
from aosphere_core_index.extract import extract_document
from aosphere_core_index.extract.markdown import doc_to_markdown
from aosphere_core_index.graph.builder import build_graph
from aosphere_core_index.graph.store import graph_summary, load_graph, save_graph
from aosphere_core_index.ingest.alerts import gather_region_alerts
from aosphere_core_index.ingest.source import fetch_jurisdiction
from aosphere_core_index.mapping.alert_mapper import map_alerts
from aosphere_core_index.embeddings.multi_index import (
    build_multi, regions_with_content, regions_with_index, save_multi)
from aosphere_core_index.navigator.content_export import build_content
from aosphere_core_index.navigator.graph_export import build_compact
from aosphere_core_index.regions.region_map import region_for
from aosphere_core_index.navigator.render import (
    export_graphml,
    find_node_by_clause,
    render_ego,
    render_structure,
)

app = typer.Typer(add_completion=False, help="aosphere-core-index Step-1 GraphIndex")
_TEMPLATES = Path(__file__).parent / "navigator"


@app.callback()
def _main() -> None:
    """Load .env before any command runs."""
    _load_dotenv()


def _graph_path(region: str) -> Path:
    return settings.region_artifacts(region) / f"{region}.graph.pkl"


def _build(region: str):
    """Fetch + extract + chunk + build graph for a region's primary DOCX."""
    src = fetch_jurisdiction(region)
    docx_docs = [
        d for d in src.docs
        if str(d.get("EXTENSION", "")).lower() == "docx" and d.get("local_path")
    ]
    if not docx_docs:
        raise typer.BadParameter(f"No DOCX source found for region {region!r}")
    doc = extract_document(
        docx_docs[0]["local_path"], doc_meta=docx_docs[0], source_key=docx_docs[0]["source_key"]
    )
    chunks = chunk_document(doc)
    g = build_graph(doc, chunks, src)
    return src, doc, chunks, g


def _load_or_build(region: str):
    path = _graph_path(region)
    if path.exists():
        return load_graph(path)
    _, _, _, g = _build(region)
    save_graph(g, path)
    return g


def _render_template(name: str, token: str, data_json: str, out: Path) -> Path:
    html = (_TEMPLATES / name).read_text().replace(token, data_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out


_EMBEDDER = None


def _embedder():
    """Shared embedder, selected by ACI_EMBED_BACKEND (bge local | titan Bedrock)."""
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = make_embedder()
    return _EMBEDDER


def _section_index(region: str, doc):
    """Load the cached section vector index, building (and caching) it if absent."""
    emb = _embedder()
    path = settings.region_artifacts(region) / f"{region}.sections.npz"
    if path.exists():
        idx = load_index(path)
        if idx.model == emb.name and len(idx.keys):
            return idx
    rprint(f"[dim]embedding {len(doc.sections)} sections (one-time, cached)...[/dim]")
    idx = build_section_index(doc, emb)
    save_index(idx, path)
    return idx


@app.command()
def regions():
    """List jurisdictions (regions) available under working/."""
    from aosphere_core_index.aws.s3_readonly import ReadOnlyS3

    s3 = ReadOnlyS3()
    s3.assert_identity()
    prefixes = s3.list_common_prefixes(settings.working_prefix + "/")
    names = sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes)
    rprint(f"[green]{len(names)} regions[/green]")
    for n in names:
        rprint(f"  {n}")


@app.command()
def build(region: str):
    """Extract, chunk, build the linked graph, and persist artifacts."""
    src, doc, chunks, g = _build(region)
    art = settings.region_artifacts(region)
    save_graph(g, _graph_path(region))
    (art / f"{region}.md").write_text(doc_to_markdown(doc))
    rprint(f"[green]Built {region}[/green]: {len(doc.sections)} sections, {len(chunks)} chunks")
    rprint(graph_summary(g))
    rprint(f"artifacts -> {art}/")


@app.command()
def reader(region: str, lexical: bool = typer.Option(False, help="Use lexical alert mapping")):
    """Build the content reader HTML (read text + follow links + mapped alerts)."""
    src, doc, chunks, g = _build(region)
    save_graph(g, _graph_path(region))
    alerts_raw = gather_region_alerts(region)
    if lexical:
        mappings = map_alerts(doc, alerts_raw)
    else:
        mappings = map_alerts_semantic(alerts_raw, _section_index(region, doc), _embedder())
    content = build_content(doc, src, alert_mappings=mappings)
    out = settings.region_artifacts(region) / f"{region}.reader.html"
    _render_template("reader_template.html", "__CONTENT_DATA__",
                     json.dumps(content, separators=(",", ":")), out)
    rprint(f"[green]Reader[/green] -> {out}  ({len(mappings)} alerts mapped)")


@app.command()
def alerts(region: str, method: str = typer.Option("semantic", help="semantic | lexical | both")):
    """Extract last-6-months alerts from input/ and map them to sections."""
    _, doc, _, _ = _build(region)
    raw = gather_region_alerts(region)
    if method == "both":
        lex = map_alerts(doc, raw)
        sem = map_alerts_semantic(raw, _section_index(region, doc), _embedder())
        rprint(f"[green]{region}[/green]: {len(raw)} alerts (last 6 months) — semantic vs lexical")
        for s, l in zip(sem, lex):
            st = f"{s.matches[0][0]} {s.matches[0][1][:30]}" if s.matches else "(none)"
            lt = f"{l.matches[0][0]} {l.matches[0][1][:30]}" if l.matches else "(none)"
            typer.echo(f"\n  {s.publication_date}  {s.title[:64]}")
            typer.echo(f"     semantic: {st}")
            typer.echo(f"     lexical : {lt}")
        return
    mappings = (
        map_alerts(doc, raw) if method == "lexical"
        else map_alerts_semantic(raw, _section_index(region, doc), _embedder())
    )
    report = [
        {
            "alert_id": m.alert_id, "date": m.publication_date, "impact": m.impact,
            "title": m.title,
            "sections": [{"key": k, "title": t, "score": s} for k, t, s in m.matches],
        }
        for m in mappings
    ]
    out = settings.region_artifacts(region) / f"{region}.alerts.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    mapped = sum(1 for m in mappings if m.matches)
    rprint(f"[green]{region}[/green]: {len(mappings)} alerts (last 6 months), "
           f"{mapped} mapped to >=1 section")
    for m in mappings:
        top = f"{m.matches[0][0]} {m.matches[0][1][:36]}" if m.matches else "(no match)"
        typer.echo(f"  {m.impact:6}  {m.publication_date}  {m.title[:58]:58}  -> {top}")
    rprint(f"report -> {out}")


@app.command()
def navigate(region: str, clause: str = typer.Option(None, help="Ego view around a clause key")):
    """Render the interactive graph HTML + GraphML export."""
    g = _load_or_build(region)
    art = settings.region_artifacts(region)
    rprint(f"GraphML -> {export_graphml(g, art / f'{region}.graphml')}")
    if clause:
        center = find_node_by_clause(g, clause)
        if not center:
            raise typer.BadParameter(f"clause {clause} not found in {region}")
        html = render_ego(g, center, art / f"{region}.{clause}.html")
    else:
        html = render_structure(g, art / f"{region}.structure.html")
    rprint(f"[green]Navigator[/green] -> {html}")


@app.command()
def all(region: str):
    """Build everything for a region: graph + markdown + reader + graph HTML."""
    src, doc, chunks, g = _build(region)
    art = settings.region_artifacts(region)
    save_graph(g, _graph_path(region))
    (art / f"{region}.md").write_text(doc_to_markdown(doc))
    mappings = map_alerts_semantic(gather_region_alerts(region), _section_index(region, doc),
                                   _embedder())
    _render_template("reader_template.html", "__CONTENT_DATA__",
                     json.dumps(build_content(doc, src, alert_mappings=mappings),
                                separators=(",", ":")),
                     art / f"{region}.reader.html")
    export_graphml(g, art / f"{region}.graphml")
    render_structure(g, art / f"{region}.structure.html")
    rprint(f"[green]Done {region}[/green]: {len(doc.sections)} sections, {len(chunks)} chunks")
    rprint(f"artifacts -> {art}/")


@app.command()
def graph(region: str):
    """Build the interactive force navigator (expand a node to reveal its links,
    including footnotes connected via refers_to)."""
    g = _load_or_build(region)
    out = settings.region_artifacts(region) / f"{region}.navigator.html"
    _render_template("artifact_template.html", "__GRAPH_DATA__",
                     json.dumps(build_compact(g), separators=(",", ":")), out)
    rprint(f"[green]Graph navigator[/green] -> {out}")


@app.command()
def search(region: str, query: str, k: int = 5):
    """Semantic clause search: embed the query, rank sections, show backtrace path."""
    _, doc, _, _ = _build(region)
    idx = _section_index(region, doc)
    qvec = _embedder().embed([query])[0]
    by_key = {s.key: s for s in doc.sections}
    by_id = {s.id: s for s in doc.sections}
    rprint(f"[green]{region}[/green]  query: [italic]{query}[/italic]")
    for key, title, level, score in idx.search(qvec, k):
        s = by_key.get(key)
        crumb, cur = [], s
        while cur is not None:
            crumb.append(cur.key)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        path = " > ".join(reversed(crumb))
        typer.echo(f"  {score:.3f}  [{key}] {title[:50]}")
        typer.echo(f"          path: {path}")


def _is_built(n: str) -> bool:
    art = settings.region_artifacts(n)
    return (art / f"{n}.sections.npz").exists() and (art / f"{n}.content.json").exists()


@app.command(name="build-batch")
def build_batch(regions: str):
    """Worker: build a comma-separated set of jurisdictions in THIS process, then exit.

    build-all runs this in a fresh subprocess per batch so native memory
    (onnxruntime arenas) is reclaimed by the OS between batches.
    """
    from aosphere_core_index.service.registry import export_content

    for n in [r.strip() for r in regions.split(",") if r.strip()]:
        if _is_built(n):
            continue
        try:
            export_content(n)
            rprint(f"  built {n}")
        except Exception as e:
            rprint(f"[red]  failed {n}: {e}[/red]")


@app.command(name="build-all")
def build_all(limit: int = 0, force: bool = False, batch: int = 8):
    """Build artifacts for ALL jurisdictions, then the combined flat index + manifest.

    Memory-safe: each batch of jurisdictions runs in a fresh subprocess so the
    embedding runtime's memory is released between batches. Resumable — already
    built jurisdictions are skipped.
    """
    import subprocess
    import sys

    from aosphere_core_index.aws.s3_readonly import ReadOnlyS3

    s3 = ReadOnlyS3()
    s3.assert_identity()
    prefixes = s3.list_common_prefixes(settings.working_prefix + "/")
    names = [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes]
    names = [n for n in names if not n.startswith("_")]
    if limit:
        names = names[:limit]
    todo = names if force else [n for n in names if not _is_built(n)]
    rprint(f"[bold]{len(todo)}[/bold] to build (of {len(names)}); {len(names) - len(todo)} already done")

    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        rprint(f"[cyan]batch {i // batch + 1}/{(len(todo) + batch - 1) // batch}[/cyan]: {', '.join(chunk)}")
        subprocess.run(
            [sys.executable, "-m", "aosphere_core_index.cli", "build-batch", ",".join(chunk)],
            check=False,
        )
    _reindex()


def _reindex() -> None:
    """Rebuild the flat multi-index + manifest from the per-jurisdiction artifacts
    already on disk (no embedding — fast, low memory)."""
    regions = regions_with_index()
    mi = build_multi(regions, _embedder().name)
    save_multi(mi)
    # `count` is the JURISDICTION count and always has been, so "did the vector store receive
    # every row this index has?" was unanswerable from here — the question behind both silent
    # partial loads (2,000 of 84,197, then 11,000 of 77,939). `rows` is that number.
    # `version` lets a pod tell which index version its own /data mount carries, and `dim`
    # lets a loader size the k-NN mapping without loading the matrix.
    manifest = {
        "version": os.getenv("ACI_INDEX_VERSION", "").strip(),
        "model": _embedder().name,
        "rows": int(mi.matrix.shape[0]),
        "dim": int(mi.matrix.shape[1]) if mi.matrix.ndim == 2 else None,
        "count": len(regions),
        "regions": len(regions),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_run": os.getenv("ACI_SOURCE_RUN", "").strip(),
        "jurisdictions": [{"name": j, "region": region_for(j)} for j in regions],
    }
    (settings.products_dir / "_multi" / "manifest.json").write_text(json.dumps(manifest, indent=2))
    rprint(f"[green]multi-index[/green]: {len(regions)} jurisdictions, {mi.matrix.shape[0]} vectors")


@app.command()
def reindex():
    """Rebuild the flat multi-index + manifest from on-disk artifacts."""
    _reindex()


@app.command()
def validate(region: str = typer.Option(None, help="One region; default = all built"),
             strict: bool = typer.Option(True, help="Exit non-zero if any error is found")):
    """Validate extraction invariants over built artifacts (content.json).

    Catches structural misextraction that retrieval can't recover from — e.g. a
    cover date promoted to Part A shifting every part letter (the 2026-07-02 bug
    that silently corrupted 42 of 105 Shareholding Disclosure jurisdictions).
    Run after any build; wire into CI before publishing an index version.
    """
    from aosphere_core_index.extract.validate import validate_sections
    from aosphere_core_index.regions.region_map import split_region

    regions = [region] if region else sorted(regions_with_index())
    n_err = n_warn = checked = 0
    for r in regions:
        path = settings.region_artifacts(r) / f"{r}.content.json"
        if not path.exists():
            rprint(f"[yellow]skip {r}: no content.json[/yellow]")
            continue
        product, jurisdiction = split_region(r)
        sections = json.loads(path.read_text()).get("sections", [])
        errors, warnings = validate_sections(sections, product, jurisdiction)
        checked += 1
        for e in errors:
            rprint(f"[red]ERROR[/red] {e}")
        for w in warnings:
            rprint(f"[yellow]warn[/yellow]  {w}")
        n_err += len(errors)
        n_warn += len(warnings)
    rprint(f"\n[bold]validated {checked} regions: "
           f"{n_err} errors, {n_warn} warnings[/bold]")
    if strict and n_err:
        raise typer.Exit(code=1)


@app.command()
def reembed(region: str = typer.Option(None, help="One region; default = all built"),
            force: bool = typer.Option(False, help="Re-embed even if already at the target model "
                                                   "(use when the embed TEXT changed, not the model)")):
    """Re-embed jurisdictions from their local content.json with the configured
    embedder (ACI_EMBED_BACKEND), rewrite each <region>.sections.npz, then rebuild
    the multi-index. Uses content.json only — no S3 source access (so it works with
    dev creds that can't read the prod source bucket). For Titan, export Bedrock
    creds first (eval $(aws configure export-credentials --profile dev1 --format env))."""
    import re as _re

    _FN = _re.compile(r"\[\^\d+\]")
    emb = _embedder()
    if region:
        regions = [region]
    else:
        # Every region with extracted CONTENT, embedded before or not — a first embed of
        # a freshly built data dir is the same operation as a re-embed of an existing one.
        regions = regions_with_content()
    rprint(f"[bold]Re-embedding {len(regions)} jurisdictions[/bold] with [cyan]{emb.name}[/cyan]")
    failed = []
    for r in regions:
        npz = settings.region_artifacts(r) / f"{r}.sections.npz"
        if npz.exists() and not force:  # resumable: skip regions already at the target model
            try:
                if load_index(npz).model == emb.name:
                    rprint(f"  [dim]skip {r} (already {emb.name})[/dim]")
                    continue
            except Exception:
                pass
        try:
            _reembed_region(r, emb, _FN)
        except Exception as e:
            failed.append(r)
            rprint(f"[red]  failed {r}: {type(e).__name__}: {str(e)[:120]}[/red]")
    if failed:
        rprint(f"[yellow]{len(failed)} failed[/yellow]: {', '.join(failed)} — re-run `aci reembed` to resume; "
               f"multi-index NOT rebuilt until all succeed.")
        raise typer.Exit(1)
    _reindex()


def _reembed_region(r, emb, _FN):
    """Embed one region's rows from its content.json with `emb`, rewrite its .npz.

    Mirrors build_section_index: clause rows (breadcrumb + answer, kind "clause")
    plus SEPARATE guidance rows (kind "guidance") and alert rows (kind "alert",
    summary + attachment chunks) — so re-embedding from content.json stays in
    lockstep with the source build and never silently drops guidance/alerts."""
    from aosphere_core_index.embeddings.section_index import (
        SectionIndex, _GUID_CHARS, _windows, answer_text_dict, save_index,
    )
    from aosphere_core_index.extract.pdf_extract import chunk_text

    content = json.loads((settings.region_artifacts(r) / f"{r}.content.json").read_text())
    secs = content["sections"]
    by_key = {s["key"]: s for s in secs}
    guidance = content.get("answers_by_clause", {})
    keys, titles, levels, texts, kinds = [], [], [], [], []
    for s in secs:
        crumb, cur = [], s
        while cur is not None:
            crumb.append(cur["title"])
            cur = by_key.get(cur.get("parent_key")) if cur.get("parent_key") else None
        breadcrumb = " > ".join(reversed(crumb))
        # WINDOW, don't truncate. Sections in content.json are whole clauses (the viewer
        # needs a table intact), so a long clause exceeds one vector and its tail was
        # simply cut off — unreachable by search. Same convention as the source build:
        # window 0 keeps the bare key, `#c<i>` rows carry the rest, and registry collapses
        # them back to the clause at query time.
        answer = answer_text_dict(s["elements"], jurisdiction=r)
        for ci, win in enumerate(_windows(answer)):
            keys.append(s["key"] if ci == 0 else f"{s['key']}#c{ci}")
            titles.append(s["title"]); levels.append(s["level"])
            texts.append(f"{breadcrumb}\n{win}".strip()); kinds.append("clause")
    # Separate guidance rows (own vector, no clause-body dilution).
    for key, lst in guidance.items():
        s = by_key.get(key)
        if not s:
            continue
        for a in lst:
            blob = _FN.sub("", " ".join(
                x for x in [a.get("subject", ""), a.get("question", ""), a.get("answer", "")] if x
            )).strip()[:_GUID_CHARS]
            if blob:
                keys.append(key); titles.append(s["title"]); levels.append(s["level"])
                texts.append(blob); kinds.append("guidance")
    # Alert rows (summary + attachment chunks), from alerts_by_id.
    for aid, rec in content.get("alerts_by_id", {}).items():
        title = rec.get("title", "")
        summary = rec.get("summary", "")
        keys.append(f"ALERT:{aid}"); titles.append(title); levels.append(0)
        texts.append(f"{title}. {summary}".strip()); kinds.append("alert")
        for i, chunk in enumerate(chunk_text(rec.get("attachment_text", ""))):
            keys.append(f"ALERT:{aid}#a{i}"); titles.append(title); levels.append(0)
            texts.append(f"{title}. {chunk}".strip()); kinds.append("alert")
    matrix = emb.embed(texts)
    idx = SectionIndex(keys=keys, titles=titles, levels=levels, matrix=matrix,
                       model=emb.name, kinds=kinds)
    save_index(idx, settings.region_artifacts(r) / f"{r}.sections.npz")
    rprint(f"  re-embedded [green]{r}[/green]: {len(keys)} rows -> {matrix.shape}")


@app.command(name="list-jurisdictions")
def list_jurisdictions():
    """Print every jurisdiction name (one per line) for scripting."""
    from aosphere_core_index.aws.s3_readonly import ReadOnlyS3

    s3 = ReadOnlyS3()
    s3.assert_identity()
    for p in s3.list_common_prefixes(settings.working_prefix + "/"):
        n = p.rstrip("/").rsplit("/", 1)[-1]
        if not n.startswith("_"):
            typer.echo(n)


@app.command()
def publish(bucket: str, version: str, prefix: str = "index", profile: str = typer.Option(None),
            set_latest: bool = typer.Option(True, "--set-latest/--no-set-latest",
                                            help="Flip the 'latest' pointer to this version.")):
    """Upload the built artifacts to s3://<bucket>/<prefix>/<version>/ (and, unless
    --no-set-latest, update 'latest'). The uploaded tree mirrors data/ exactly, so a
    mount of s3://bucket/<prefix>/<version>/ at /data resolves the product-nested paths.

    NOTE: this layout requires the product-aware service code. When publishing an index
    whose layout differs from what current pods run, stage with --no-set-latest and flip
    'latest' only as part of the coordinated code deploy.

    Use --profile <env-sso> to write with an SSO profile (e.g. dev1). An explicit
    session is used so .env's read-only source keys don't shadow the profile."""
    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")
    base = f"{prefix}/{version}"
    n = 0
    # Mirror the local data/ layout EXACTLY under <base>/ so a mount of
    # s3://bucket/<base>/ at /data resolves the product-nested paths the service
    # reads: /data/products/<product>/<jurisdiction>/artifacts/... + /data/products/_multi/...
    for name in regions_with_index():
        art = settings.region_artifacts(name)  # data/products/<product>/<jurisdiction>/artifacts
        for fn in (f"{name}.sections.npz", f"{name}.content.json"):
            p = art / fn
            if p.exists():
                rel = p.relative_to(settings.data_dir)  # products/<product>/<jur>/artifacts/<fn>
                s3.upload_file(str(p), bucket, f"{base}/{rel.as_posix()}")
                n += 1
    for rel in ("products/_multi/multi.npz", "products/_multi/manifest.json"):
        p = settings.data_dir / rel
        if p.exists():
            s3.upload_file(str(p), bucket, f"{base}/{rel}")
            n += 1
    if set_latest:
        s3.put_object(Bucket=bucket, Key=f"{prefix}/latest", Body=version.encode())
    pointer = f"latest -> {version}" if set_latest else "latest UNCHANGED (staged with --no-set-latest)"
    rprint(f"[green]published[/green] {n} objects -> s3://{bucket}/{base}/  ({pointer})")


@app.command()
def bundle(region: str, force: bool = False):
    """Build one jurisdiction's artifacts (sections.npz + content.json). Skips if
    already built unless --force. One process per call → memory freed on exit."""
    from aosphere_core_index.service.registry import export_content

    if not force and _is_built(region):
        rprint(f"[dim]{region}: already built[/dim]")
        return
    export_content(region)  # extract -> embed (saves npz) -> write content.json
    rprint(f"[green]built[/green] {region} -> {settings.region_artifacts(region)}/")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, warm: str = typer.Option(None, help="Region to preload")):
    """Run the search service (semantic search UI + API) at http://host:port/."""
    import uvicorn

    from aosphere_core_index.service.logging_setup import configure_logging
    from aosphere_core_index.service.registry import get_bundle

    level = configure_logging()
    if warm:
        rprint(f"[dim]warming {warm}...[/dim]")
        get_bundle(warm)
    rprint(f"[green]Search service[/green] -> http://{host}:{port}/  (log {level})")
    uvicorn.run("aosphere_core_index.service.app:app", host=host, port=port,
                log_level=level.lower())


@app.command(name="ab-compare-ui")
def ab_compare_ui(host: str = "127.0.0.1", port: int = 8766):
    """Upload one PDF/DOCX and compare legacy vs MinerU extraction side by side."""
    import uvicorn

    rprint(f"[green]Extractor A/B[/green] -> http://{host}:{port}/  (legacy vs MinerU)")
    uvicorn.run("aosphere_core_index.extract.ab_ui:app", host=host, port=port, log_level="info")


@app.command(name="eval-ui")
def eval_ui(host: str = "127.0.0.1", port: int = 8767):
    """Upload a jurisdiction's PDF + DOCX; score MinerU vs legacy chunking on gold cases."""
    import uvicorn

    rprint(f"[green]Chunker Eval[/green] -> http://{host}:{port}/  (MinerU-PDF vs legacy-DOCX, gold-case hit@k)")
    uvicorn.run("aosphere_core_index.extract.eval_ui:app", host=host, port=port, log_level="info")


@app.command()
def summary(region: str):
    """Print node/edge counts for a built graph."""
    rprint(json.dumps(graph_summary(_load_or_build(region)), indent=2))


if __name__ == "__main__":
    app()
