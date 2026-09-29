#!/usr/bin/env python3
"""run_corpus.py — extract a whole corpus tree, one folder at a time.

The corpus is laid out <product>/<jurisdiction>/<id>.pdf, e.g.

    Advanced_Search_All/104_Shareholding_Disclosure/Argentina/172099.pdf

and the output mirrors that shape so the hierarchy survives on disk and the
dashboard can group by it without a database:

    out/corpus/104_Shareholding_Disclosure/Argentina/
        source.pdf
        01_stage1_extract/  02_stage2_mineru_tables/  03_stage3_final/
        scorecard.json          <- what the UI reads
        validation.json

Deliberately SEQUENTIAL, one product folder at a time. MinerU is GPU- and
memory-bound (the pilot machine swaps heavily on a 267-page document), so
running documents in parallel makes the whole corpus slower, not faster. Working
folder-by-folder also means the first folder's results can be inspected — and the
run killed — long before the corpus is finished.

Every job writes its own scorecard.json. The dashboard therefore never has to
re-run validation to draw the tree, which keeps it responsive across 100+
documents.

Resumable: a job whose scorecard.json already exists is skipped, so an
interrupted run continues where it stopped. --force re-does them.

Usage:
    # everything, folder by folder
    python scripts/run_corpus.py "/path/to/Advanced_Search_All"

    # one folder (the pilot)
    python scripts/run_corpus.py "/path/to/Advanced_Search_All" --only 104_Shareholding_Disclosure

    # Stage 1 only — fast, CPU-only, no MinerU. Tells you how many pages would
    # reach MinerU before committing to the expensive part.
    python scripts/run_corpus.py "/path/to/Advanced_Search_All" --stage1-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hybrid_extract as he  # noqa: E402
from product_rules import (ai_pipeline, clause_table_runs,  # noqa: E402
                           content_tile_name, mineru_effort, section_depth)
from check_scorecard import SHORT_DOC_MAX_PAGES, compute_scorecard  # noqa: E402
from lib_validate import validate as _validate  # noqa: E402  — the one canonical 9-check set
from fallback_chain import pdf_page_count, run_chain
from mineru_fallback import (USELESS_OUTLINE_MAX_ENTRIES,  # noqa: E402
                             result_is_acceptable)
from mineru_full_extract import run_mineru_full  # noqa: E402
from lib_content_compare import BOLD, DIM, GREEN, RED, YELLOW, banner  # noqa: E402

# Structured run events -> stdout -> the node's log agent -> Kibana. Guarded because
# run_corpus is also imported by one-off scripts that may run outside the installed package;
# a missing observability module must degrade to silence, never to an ImportError at the top
# of the extraction pipeline.
try:
    from aosphere_core_index.obs import log as _log
    from aosphere_core_index.obs import probe as _probe
except Exception:                                            # noqa: BLE001
    # A COMPLETE stand-in, every method taking the same call shape as the real one. An
    # incomplete shim is worse than no shim: it turns the situation it exists to survive —
    # the obs package not importing — into an AttributeError or a TypeError at the top of
    # the extraction pipeline, which is a crash rather than the silence that was intended.
    # test_run_events pins the surface against the real module so the two cannot drift.
    class _log:                                              # type: ignore  # noqa: N801
        # Same value as obs.log.MAX_STR. Duplicated only here, on the path taken when that
        # module could not be imported to be asked -- and read by the truncations below, so
        # its absence would be an AttributeError raised while reporting a failure.
        MAX_STR = 1_000
        enabled = staticmethod(lambda: False)
        bind = staticmethod(lambda **kw: None)
        bind_service = staticmethod(lambda name, **kw: None)
        unbind = staticmethod(lambda *keys: None)            # POSITIONAL keys, like the real one
        snapshot = staticmethod(lambda: {})
        bound = staticmethod(lambda: {})
        event = staticmethod(lambda *a, **kw: None)
        exception = staticmethod(lambda *a, **kw: None)
        flatten = staticmethod(lambda prefix, d, keep=60: {})
        install_crash_handlers = staticmethod(lambda: None)

        @staticmethod
        def context(**kw):
            import contextlib as _c
            return _c.nullcontext()
    _probe = None                                            # type: ignore

CORPUS_ROOT = HERE.parent / "out" / "corpus"


def discover(root: Path) -> dict[str, list[dict]]:
    """-> {product_folder: [{label, jurisdiction, doc_id, pdf}, ...]}.

    The label ALWAYS carries the document id, never just the jurisdiction. A
    jurisdiction folder routinely holds more than one PDF in this corpus
    (104_Shareholding_Disclosure/Austria has two, 107_netalytics/Andorra has
    three), and a jurisdiction-only label would send them to the same output
    directory — the second silently overwriting the first.

    Walks to arbitrary depth rather than assuming exactly two levels, so a deeper
    tree still yields a readable, unique name."""
    out: dict[str, list[dict]] = {}
    for product in sorted(p for p in root.iterdir() if p.is_dir()):
        jobs = []
        for pdf in sorted(product.rglob("*.pdf")):
            rel = pdf.relative_to(product).parent
            juris = "__".join(rel.parts) if rel.parts else "(root)"
            jobs.append({"label": f"{juris}__{pdf.stem}", "jurisdiction": juris,
                         "doc_id": pdf.stem, "pdf": pdf})
        if jobs:
            out[product.name] = jobs
    return out


def pdf_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_index(out_root: Path) -> dict[str, Path]:
    """sha1 -> a FINISHED job dir holding that exact PDF's extraction.

    This corpus files one document under many jurisdictions: the 58 US Data Privacy
    documents are 6 distinct PDFs, one 281-page survey appearing 31 times. Extracting
    it 31 times costs ~31x the GPU for byte-identical output."""
    idx: dict[str, Path] = {}
    for meta in out_root.glob("*/*/corpus_meta.json"):
        job = meta.parent
        if not (job / "scorecard.json").exists():
            continue
        try:
            h = (json.loads(meta.read_text()) or {}).get("pdf_sha1")
        except (OSError, json.JSONDecodeError):
            continue
        if h:
            idx.setdefault(h, job)
    return idx


ARTIFACTS = ("01_stage1_extract", "02_stage2_mineru_tables", "03_stage3_final",
             "validation.json", "scorecard.json")


# Which steps are a fallback TIER rather than the normal pipeline. Their cost is the
# answer to "what did escalating actually buy us", so it is summed out separately
# instead of disappearing into the total.
# Both spellings are the same tier: fallback_chain names the step "mineru_full" on the
# short-document path (it skips straight there) and "mineru_fallback" on the normal
# escalation path. Listing only one silently drops that tier's cost from the total --
# India 169716 reported 33s of fallback against a real 86s.
FALLBACK_STEPS = ("toc_rescue", "mineru_full", "mineru_fallback")


def stage1_pages(dest: Path) -> int | None:
    """How many pages the document has, per Stage 1.

    NOT from the tables manifest: that carries only source_pdf/tables/footnote_ids, so
    `manifest.get("pages")` has always been None and every timings.json ever written
    records `"pages": null`. The count is in the Stage 1 report, which the fallback
    tiers rewrite along with the rest of 01_stage1_extract, so it stays correct for a
    rescued document too.
    """
    try:
        r = json.loads((Path(dest) / "01_stage1_extract" / "stage1_report.json").read_text())
    except (OSError, ValueError):
        return None
    p = r.get("pages")
    return p if isinstance(p, int) and p > 0 else None


def timing_block(steps: dict, *, started: float, finished: float, pages=None,
                 tables=None, mineru_crop_pages=None, cloned_from=None) -> dict:
    """What this document cost, in the form the scorecard carries it.

    Wall clock lived only in timings.json, which nothing reads and which a CLONE never
    gets (see ARTIFACTS) -- so cost was invisible to every scorecard consumer: the
    gallery, the pipeline monitor, and any capacity estimate built from a finished
    corpus. It belongs on the scorecard, next to the gate it was spent earning.

    `seconds` is EXTRACTION only, measured to the moment the document is scored. A
    later --publish-local step is gallery wiring, not extraction, and is deliberately
    outside this number; timings.json still records the full wall clock including it.
    """
    total = round(finished - started, 1)
    ranked = sorted(((k, v) for k, v in steps.items() if k != "total"),
                    key=lambda kv: -kv[1])
    fb = round(sum(v for k, v in steps.items() if k in FALLBACK_STEPS), 1)
    return {
        "seconds": total,
        # slowest first: on these documents Stage 2 is so dominant that any other
        # order buries the only step whose cost ever changes
        "steps": {k: v for k, v in ranked},
        "slowest_step": ranked[0][0] if ranked else None,
        "fallback_seconds": fb,
        "pages": pages,
        "seconds_per_page": round(total / pages, 2) if pages else None,
        "mineru_crop_pages": mineru_crop_pages,
        # the number that actually predicts a run: MinerU is priced per cropped page,
        # not per document, and it is the only step that scales with the corpus
        "seconds_per_crop_page": (round(steps["stage2_mineru"] / mineru_crop_pages, 2)
                                  if mineru_crop_pages and steps.get("stage2_mineru")
                                  else None),
        "tables": tables,
        "started_at": round(started, 3),
        "finished_at": round(finished, 3),
        # set only on a clone, which paid none of this -- see run_one
        "cloned_from": cloned_from,
    }


def clone_job(src: Path, dest: Path) -> None:
    """Reproduce a finished extraction in another job dir.

    Hard-linked where the filesystem allows it, so 31 copies of a 281-page tree cost
    one copy of the bytes; the files are ordinary files to every reader (dashboard,
    publisher, validator), which a symlinked tree is not guaranteed to be."""
    for name in ARTIFACTS:
        s, d = src / name, dest / name
        if not s.exists() or d.exists():
            continue
        if s.is_dir():
            shutil.copytree(s, d, copy_function=_link_or_copy)
        else:
            _link_or_copy(s, d)


def _link_or_copy(s, d) -> None:
    try:
        os.link(s, d)
    except OSError:
        shutil.copy2(s, d)


def _publish(publish_local, product: str, job: dict, dest: Path) -> None:
    """Add one finished document to the local Doc Library. Shared by the extraction
    and clone paths so a cloned document cannot be silently left unpublished."""
    if not publish_local:
        return
    try:
        from push_hybrid_s3 import publish_local_one
        from extract_to_s3 import product_from_dir
        publish_local_one(Path(publish_local), product_from_dir(product),
                          job["jurisdiction"], job["doc_id"], dest)
    except Exception as e:  # noqa: BLE001 — a publish failure must not sink the run
        print(RED(f"  ⚠ gallery publish failed: {type(e).__name__}: {e}"))


# ---------------------------------------------------------------------------
# STAGE 1 COULD NOT READ THIS DOCUMENT AT ALL
#
# pdf2mdtree already names the two ways that happens, and exits rather than emitting a tree:
#
#   "No text layer: N page(s) contain no text spans at all"   -> an image-only PDF
#   "No structure found: no bookmark outline, no heading-sized text, and no BOLD
#    CAPITALISED lines to fall back on"                       -> nothing to build a tree FROM
#
# Both used to end the document. run_stage1 turns the exit into a RuntimeError, the corpus
# loop caught it, and the job was recorded as crashed -- with no extraction attempted, even
# though a whole-document MinerU parse is precisely the tool for both cases. The comment at
# that exit ("they cannot be extracted without OCR") predates having that tier at all: MinerU
# is a visual parser, so a scan is exactly what it CAN read.
#
# So a Stage 1 that cannot read the document routes straight to the MinerU tier, and skips
# Stage 2 with it -- cropping tables out of a tree that does not exist is not work worth
# paying for. That makes this the CHEAPEST route through the pipeline at one MinerU pass,
# not the most expensive.
# ---------------------------------------------------------------------------

STAGE1_BAIL = (
    # (cause, needles, what to tell a human)
    # MOST SPECIFIC FIRST. "genuinely scanned" is emitted inside the no-structure message as
    # "effectively no text layer; this looks genuinely scanned", so a bare "no text layer"
    # needle matches it too and would answer with the wrong half of the diagnosis.
    ("scanned", ("genuinely scanned",),
     "no headings and almost no text — this reads as a scan, so only a visual parse can read it"),
    ("scanned", ("no text layer",),
     "no text layer at all — every page is an image, so only a visual parse can read it"),
    ("no_structure", ("no structure found",),
     "no bookmark outline, no heading-sized text and no bold-capitalised lines to build a tree from"),
)


def classify_stage1_failure(err: str) -> tuple[str, str]:
    """A Stage 1 error -> (cause, reason). Order matters: the "genuinely scanned" note is
    emitted INSIDE the no-structure message, and it is the more specific diagnosis.

    Anything unrecognised still routes -- losing a document to an unexpected error helps
    nobody -- but is named as unrecognised so a genuine regression in the extractor stays
    visible instead of being quietly papered over with a MinerU tree."""
    low = (err or "").lower()
    for cause, needles, why in STAGE1_BAIL:
        if any(n in low for n in needles):
            return cause, why
    return "unrecognised", "Stage 1 failed for a reason this route does not recognise"


def _stage1_from_printed_toc(dest: Path, pdf: Path, backend, effort,
                             depth: int = 3, clause_tables: bool = False):
    """Last try before giving the document to MinerU: build the outline Stage 1 was missing.

    "No structure found" means no outline AND no heading-shaped text. But a document that
    PRINTS a contents page still states its own structure — rebuilding bookmarks from it
    gives Stage 1 the thing it could not find, and the document then takes the normal path
    for one MinerU pass instead of a whole-document re-parse. ~1.5s to find out.

    -> a Stage 1 manifest when that worked, else None. Never raises: this is a last chance,
    and a last chance that throws is just a different way to lose the document."""
    try:
        import rescue_outline as ro
        from product_rules import product_for_job, rebuild_outline_from_printed_toc
        res = ro.rescue(dest, rerun=False, promote=False, backend=backend, effort=effort,
                        no_prune=rebuild_outline_from_printed_toc(product_for_job(dest)))
        repaired = res.get("repaired_pdf")
        if not repaired or not Path(repaired).exists():
            return None                       # no printed contents page, or it did not verify
        shutil.copy2(repaired, dest / "source_repaired.pdf")
        manifest = he.run_stage1(Path(repaired), dest / "01_stage1_extract", depth, clause_tables)
        (dest / "toc_preflight.json").write_text(json.dumps(
            {"applied": True, "status": res.get("status"),
             # A distinct trigger from the ordinary pre-flight: this outline was not repaired
             # because it looked wrong, it was BUILT because Stage 1 had nothing to work with.
             "trigger": "stage1_found_no_structure",
             "trigger_detail": "Stage 1 could not find a heading anywhere; the printed "
                               "contents page supplied the structure instead",
             "toc_pages": res.get("toc_pages"), "engine": res.get("engine"),
             "entries": (res.get("check") or {}).get("entries"),
             "verified": (res.get("check") or {}).get("verified")}, indent=2))
        return manifest
    except Exception:                          # noqa: BLE001 — fall through to MinerU
        return None


def _stage4_enabled() -> bool:
    """Is stage 4 allowed on this host? ACI_STAGE4_AI_ENABLED, read fresh each time.

    Read through `settings` so there is ONE definition of the gate -- run_stage4 checks the
    same flag, so a caller cannot spend money by reaching past this.
    """
    try:
        from aosphere_core_index.config import settings
        return bool(settings.stage4_ai_enabled)
    except Exception:                                    # noqa: BLE001
        return False


# A document with dozens of sections must not turn one AI pass into a hundred log records.
# Documents here run 8-20 sections, so this only bites on something pathological — and the
# count is reported either way, so a truncated list still says how much was truncated.
_SECTION_EVENTS_MAX = 40


def _log_stage4_sections(rep: dict, model: str, text_model: str) -> None:
    """One event per section the AI pass got WRONG — by name, with the reason.

    The aggregate ("7/8 sections accepted") says a section failed but never which one or
    why, and those are the only two facts that let anybody act on it: the section file name
    maps straight to a heading in the document, and the reason separates "the prompt did not
    work on this table" from "nothing ran" — the distinction _repair_one_section's own
    comment was added to preserve, and which stops at the report on disk today.

    TWO KINDS OF WRONG, deliberately separate:

      section.rejected  ok=False. The AI output was refused and stage 3's text was kept. The
                        document is unharmed; the pass simply bought nothing here.
      section.lossy     ok=TRUE and cell content went missing anyway. This is the dangerous
                        one: the section was ACCEPTED, so it ships, and the loss is only
                        visible by comparing cell_chars before and after. It is the
                        document-level Bahamas 183503 finding (fidelity 91.9 -> 45.8) located
                        to the individual section that caused it.

    Per-section metrics are named `aci.section_*` rather than reusing `aci.cost_usd` and
    `aci.tokens_*`: those carry the DOCUMENT total on stage.end, and a Kibana sum over a
    field that means both would double-count every run's spend.
    """
    # Guarded as a WHOLE, not per event, and for a specific reason: this is called from
    # inside _run_stage4_in_pipeline's try, whose except reports stage 4 as FAILED. A
    # malformed section record would therefore turn a successful AI pass into a reported
    # outage — a diagnostic inventing the failure it exists to report.
    try:
        _emit_stage4_sections(rep, model, text_model)
    except Exception:                                        # noqa: BLE001
        pass


def _emit_stage4_sections(rep: dict, model: str, text_model: str) -> None:
    sections = [s for s in (rep.get("sections") or []) if isinstance(s, dict)]
    rejected = [s for s in sections if not s.get("ok")]
    lossy = [s for s in sections if s.get("ok") and (s.get("cell_chars_lost") or 0) > 0]
    for kind, rows in (("section.rejected", rejected), ("section.lossy", lossy)):
        for i, sec in enumerate(rows):
            if i >= _SECTION_EVENTS_MAX:
                _log.event(f"{kind}.truncated", level="warn",
                           message=f"{len(rows) - _SECTION_EVENTS_MAX} more not listed",
                           **{"aci.sections_omitted": len(rows) - _SECTION_EVENTS_MAX})
                break
            lost = sec.get("cell_chars_lost") or 0
            before = sec.get("cell_chars_before") or 0
            _log.event(kind, level="warn",
                       message=(f"{sec.get('file')}: "
                                + (f"lost {lost} cell chars" if kind.endswith("lossy")
                                   else f"REJECTED — {sec.get('reason')}")),
                       **{"aci.stage": "stage4_ai",
                          # The section FILE is the addressable unit: "07-marketing-selling-
                          # to-the-public.md" is a heading someone can open, unlike an index.
                          "aci.section": sec.get("file"),
                          "aci.section_kind": sec.get("kind"),
                          "aci.section_reason": sec.get("reason"),
                          # max_tokens is a silent truncation rather than a refusal: the
                          # section comes back plausible and short. Worth its own field.
                          "aci.stop_reason": sec.get("stop_reason"),
                          "aci.section_pages": sec.get("pages"),
                          "aci.section_model": sec.get("model") or (
                              model if sec.get("kind") == "table" else text_model),
                          "aci.section_cost_usd": sec.get("cost_usd"),
                          "aci.section_tokens_in": sec.get("tokens_in"),
                          "aci.section_tokens_out": sec.get("tokens_out"),
                          "aci.tables_before": sec.get("tables_before"),
                          "aci.tables_after": sec.get("tables_after"),
                          "aci.rows_before": sec.get("rows_before"),
                          "aci.rows_after": sec.get("rows_after"),
                          "aci.cell_chars_lost": lost or None,
                          "aci.cell_chars_lost_pct": (round(100 * lost / before, 1)
                                                      if before and lost else None)})


def _run_stage4_in_pipeline(dest: Path, pdf: Path, steps: dict, log=print) -> float | None:
    """Stage 4, then stage 5, on a document that has just finished extraction.

    Never raises. A paid pass that fails must leave a completed extraction completed: the
    scorecard is already written and the tree already published, and 04_stage4_ai is
    additive, so the worst case is a document with no AI output -- not a lost document.

    The measured seconds are folded into `steps`, which is what carries them out of here:
    run_one returns it, corpus_worker writes it onto the ledger row, and the Core Index
    reads that row. Without this the run would do the work and no screen would know.

    The dollar cost is RETURNED instead, not folded into `steps`: `steps` is a map of
    stage durations that the screen formats as times (exDur), so a dollar figure hiding in
    there would render as "$0.4821" seconds of AI post-processing. run_one carries this
    back out as `cost_usd` on its own result -- the same field the summary-AI route
    already uses -- so corpus_worker.finished() picks it up unmodified.
    """
    t_stage4 = time.time()
    try:
        from aosphere_core_index.config import settings
        from aosphere_core_index.extract.ai_postprocess import (resolve_model,
                                                                 resolve_text_model,
                                                                 run_stage4)
        # resolve_model/resolve_text_model own the name -> id mapping, so this reads
        # ACI_STAGE4_AI_MODEL and ACI_STAGE4_TEXT_AI_MODEL and accepts either a short alias
        # or any Bedrock model id. A second copy of either map here is how they would drift.
        model = resolve_model(settings.stage4_ai_model)
        text_model = resolve_text_model(settings.stage4_text_ai_model)
        # Logged every time, and loudly: this is the number that turns into money, and a
        # silently-defaulted model is exactly how a comparison run got billed on one model
        # while being read as though it were the other.
        log(f"    stage 4: enabled, model={model} (tables) / {text_model} (text)")
        # WHICH MODEL, as a field. "a silently-defaulted model is exactly how a comparison run
        # got billed on one model while being read as though it were the other" — that is a
        # log line today and unanswerable in aggregate, which is the form the question is
        # actually asked in ("what did last week's runs cost, by model").
        _log.event("stage4.config", message=f"stage 4 enabled, {model} / {text_model}",
                   **{"aci.stage": "stage4_ai", "aci.model": model,
                      "aci.text_model": text_model})
        rep = run_stage4(dest / "03_stage3_final", dest / "04_stage4_ai", pdf,
                         mode="section", models=(model,), text_model=text_model, log=log)
        if not rep.get("ran", True):
            log(f"    stage 4: {rep.get('reason') or 'did not run'}")
            # "enabled but did not run" is NOT the same as "not enabled", and both look like
            # a missing 04_stage4_ai from outside. Only one of them is a problem.
            _log.event("stage.end", level="warn",
                       message=f"stage 4 did not run: {rep.get('reason') or 'no reason given'}",
                       **{"aci.stage": "stage4_ai", "event.outcome": "skipped",
                          "event.duration_s": round(time.time() - t_stage4, 2),
                          "aci.reason": rep.get("reason") or "unspecified"})
            return None
        u = rep.get("usage") or {}
        sub = rep.get("subchunk") or {}
        steps["stage4_ai"] = round(float(rep.get("seconds") or 0.0), 1)
        steps["stage5_subchunk"] = round(float(sub.get("seconds") or 0.0), 1)
        _log_stage4_sections(rep, model, text_model)
        # The AI pass's own verdict on itself. A run where every section was REJECTED still
        # writes a 04_stage4_ai directory and still reports gate=pass (the gate reads stages
        # 1-3), so "sections_accepted: 0 of 8" is the difference between a pass that worked
        # and one that silently did nothing — and it was only ever a print.
        _log.event("stage.end",
                   level="warn" if not rep.get("sections_accepted") else "info",
                   message=(f"stage 4: {rep.get('sections_accepted')}/"
                            f"{rep.get('sections_total')} sections, "
                            f"${u.get('cost_usd', 0):.4f}"),
                   **{"aci.stage": "stage4_ai", "event.outcome": "success",
                      "event.duration_s": steps["stage4_ai"],
                      "aci.sections_accepted": rep.get("sections_accepted"),
                      "aci.sections_total": rep.get("sections_total"),
                      "aci.sections_rejected": ((rep.get("sections_total") or 0)
                                                - (rep.get("sections_accepted") or 0)) or None,
                      # isinstance-guarded for the same reason _log_stage4_sections is:
                      # this expression is built INSIDE the try whose except reports stage 4
                      # as failed, so a malformed record here would invent an outage.
                      "aci.sections_lossy": sum(
                          1 for sec in (rep.get("sections") or [])
                          if isinstance(sec, dict) and sec.get("ok")
                          and (sec.get("cell_chars_lost") or 0) > 0) or None,
                      "aci.model": model, "aci.text_model": text_model,
                      "aci.tokens_in": u.get("input_tokens"),
                      "aci.tokens_out": u.get("output_tokens"),
                      "aci.tokens_total": u.get("total_tokens"),
                      "aci.cost_usd": u.get("cost_usd")})
        if sub:
            # Stage 5 reported RETROSPECTIVELY, and only an end event.
            #
            # It runs inside run_stage4's call, so by the time its timing is readable here it
            # has already finished — announcing an entry now would put a stage.enter after
            # the stage it describes and make the heartbeat's stage wrong in the other
            # direction. The honest report is the one below: a stage that ran, and how long
            # it took. Heartbeats during sub-chunking will say stage4_ai; separating the two
            # live would mean a callback inside run_stage4, which is a bigger change than the
            # signal is worth.
            _log.event("stage.end",
                       message=f"stage 5: {sub.get('subchunks_total')} sub-chunks",
                       **{"aci.stage": "stage5_subchunk", "event.outcome": "success",
                          "event.duration_s": steps["stage5_subchunk"],
                          "aci.subchunks_total": sub.get("subchunks_total"),
                          "aci.sections_split": sub.get("sections_split")})
        log(f"    stage 4: {rep.get('sections_accepted')}/{rep.get('sections_total')} "
            f"sections, {u.get('total_tokens', 0):,} tokens, ${u.get('cost_usd', 0):.4f}"
            # subchunks_total is subchunk.run()'s real key -- "subchunks" was never a key
            # this dict had, so this line always printed "None sub-chunks" even on a run
            # that split plenty, which read as stage 5 having failed when it had not.
            + (f"; stage 5: {sub.get('subchunks_total')} sub-chunks" if sub else ""))
        _score_post_ai(dest, steps, log=log)
        return u.get("cost_usd")
    except Exception as e:                               # noqa: BLE001
        log(f"    stage 4: FAILED — {type(e).__name__}: {e}")
        # THE SWALLOWED FAILURE. This except is correct — a paid pass that fails must leave a
        # completed extraction completed — but it means a stage 4 outage produces a document
        # that reports `status: done, gate: pass` with no AI output and no error anywhere.
        # Every such document looked exactly like one where stage 4 was simply disabled.
        # `aci.swallowed` is what separates them: the run continued deliberately.
        _log.exception("stage.end", e, message=f"stage 4 FAILED: {type(e).__name__}: {e}",
                       **{"aci.stage": "stage4_ai", "event.outcome": "failure",
                          "event.duration_s": round(time.time() - t_stage4, 2),
                          "aci.swallowed": True})
        return None


def _score_post_ai(dest: Path, steps: dict, log=print) -> None:
    """Score the tree stage 4/5 actually left behind, into scorecard_post_ai.json.

    A SECOND file, never a rewrite of scorecard.json. The reasons scorecard.json is
    written before stage 4 all still hold -- it is the completion marker for --resume,
    its `timing` is extraction cost, and a paid pass that fails must not un-complete a
    document that extracted perfectly well -- so the extraction gate stays exactly where
    it was and this is added beside it.

    It exists because nothing measured stage 4 or 5 at all. That is how a run whose every
    section failed still reported gate=pass: the gate reads stages 1-3, and a stage-4
    rejection count is read by nothing. Measured on Bahamas 183503, stage 3 against stage
    5: completeness 99.2 -> 94.6 and fidelity 91.9 -> 45.8, i.e. the AI pass dropped cell
    content out of half the tables and no screen has ever said so.

    Never raises, for the same reason as its caller: this is a report, not a gate.
    """
    try:
        from lib_validate import final_stage, run_and_gate
        stage = final_stage(dest)
        if stage <= 3:
            return          # stage 4 wrote nothing to score
        t = time.time()
        val, sc = run_and_gate(dest, stage=stage)
        secs = round(time.time() - t, 1)
        steps["scorecard_post_ai"] = secs
        # ...and into timings.json too. run_corpus wrote that file before stage 4 ran, so
        # `steps` alone reaches only the ledger row; the gallery and the pipeline monitor
        # read timings.json. _record_timings MERGES, so the stage 1-3 entries survive.
        try:
            from aosphere_core_index.extract.ai_postprocess import _record_timings
            _record_timings(dest, scorecard_post_ai=secs)
        except Exception:                                # noqa: BLE001 — a timing is not worth failing on
            pass
        (dest / "validation_post_ai.json").write_text(json.dumps(val, indent=2))
        (dest / "scorecard_post_ai.json").write_text(json.dumps(sc, indent=2))
        log(f"    post-AI scorecard (stage {stage}): {sc.get('gate')} "
            f"worst={sc.get('worst_score')} ({sc.get('weakest_dimension')}), {secs}s")
        # DID THE AI PASS MAKE THIS DOCUMENT BETTER OR WORSE. This function's docstring
        # records the measurement that motivated it — Bahamas 183503, completeness 99.2 ->
        # 94.6 and fidelity 91.9 -> 45.8, "no screen has ever said so" — and a per-dimension
        # DROP is the shape that answers it. Emitted as one field per dimension so a
        # regression is a sortable column across the whole run rather than a pair of
        # scorecards someone has to open per document.
        before = {}
        try:
            pre = json.loads((dest / "scorecard.json").read_text())
            before = {k: (v or {}).get("score")
                      for k, v in (pre.get("dimensions") or {}).items()}
        except (OSError, json.JSONDecodeError):
            pass
        after = {k: (v or {}).get("score")
                 for k, v in (sc.get("dimensions") or {}).items()}
        drops = {k: round(before[k] - after[k], 1) for k in after
                 if isinstance(after.get(k), (int, float))
                 and isinstance(before.get(k), (int, float))
                 and before[k] - after[k] > 0.05}
        _log.event("doc.scored_post_ai",
                   # A dimension that LOST more than a point to the AI pass is the signal
                   # this whole file exists for. Warn so one filter finds every one of them.
                   level="warn" if (drops and max(drops.values()) >= 1.0) else "info",
                   message=(f"post-AI (stage {stage}): {sc.get('gate')} "
                            f"worst={sc.get('worst_score')} ({sc.get('weakest_dimension')})"
                            + (f" — dropped {max(drops, key=drops.get)} "
                               f"-{max(drops.values())}" if drops else "")),
                   **{"aci.stage": "scorecard_post_ai", "event.duration_s": secs,
                      "aci.scored_stage": stage,
                      "aci.gate_post_ai": sc.get("gate"),
                      "aci.worst_post_ai": sc.get("worst_score"),
                      "aci.weakest_dimension": sc.get("weakest_dimension"),
                      "aci.dimensions_dropped": len(drops) or None,
                      "aci.worst_drop": max(drops.values()) if drops else None,
                      "aci.worst_drop_dimension": (max(drops, key=drops.get)
                                                   if drops else None),
                      **_log.flatten("aci.dim", after, keep=20),
                      **_log.flatten("aci.drop", drops, keep=20)})
    except Exception as e:                               # noqa: BLE001
        log(f"    post-AI scorecard: FAILED — {type(e).__name__}: {e}")
        _log.exception("stage.end", e,
                       message=f"post-AI scorecard FAILED: {type(e).__name__}: {e}",
                       **{"aci.stage": "scorecard_post_ai", "event.outcome": "failure",
                          "aci.swallowed": True})


def _toc_rescued(dest: Path) -> bool:
    """Did the pre-flight rebuild this document's outline from its printed contents page?

    This IS the TOC rescue. It used to be a fallback tier that ran after a full extraction;
    it now runs before Stage 2 is paid for, which is cheaper but made it invisible -- the
    job list called such a document "first pass", because the pre-flight is not a tier and
    nothing carried its outcome out of the job directory. A document whose structure came
    from its printed contents page rather than its own bookmarks is a materially different
    document, and the screen has to be able to say so.
    """
    try:
        return json.loads((dest / "toc_preflight.json").read_text()).get("applied") is True
    except (OSError, ValueError):
        return False                 # no marker, or unreadable: the outline was trusted


def _preflight_outline(dest: Path, pdf: Path, manifest: dict, backend, effort,
                       depth: int = 3, clause_tables: bool = False):
    """Repair an untrustworthy bookmark outline BEFORE Stage 2 is paid for.

    -> a new Stage 1 manifest when the outline was repaired, else None (caller keeps
    the one it has). Never raises: a pre-flight that fails must leave the document
    exactly as it would have been without it."""
    if os.environ.get("DISABLE_TOC_RESCUE"):
        return None
    try:
        import fitz
        import rescue_outline as ro
        doc = fitz.open(str(pdf))
        try:
            suspect, _why = ro.current_outline_is_suspect(doc)
            toc = doc.get_toc()
            n_entries = len(toc)
            # current_outline_is_suspect is a CHEAP TRIAGE — an outline exists, and it
            # starts near the front. It cannot see the commonest bad outline in this
            # corpus: a Word export that bookmarked every styled PARAGRAPH. Those have
            # hundreds of entries all starting on page 1, so they pass the triage,
            # Stage 1 splits the document mid-clause, the score comes out bad, and the
            # FALLBACK CHAIN then pays for a second full MinerU pass to do what this
            # pre-flight exists to do for the cost of one extra 7-second Stage 1.
            #
            # Measured on ADGM__170680: 92 bookmarks from p3, triage says not suspect,
            # pre-flight skipped, two MinerU passes. So ask the same question
            # check_toc_quality asks — does the outline agree with the contents page
            # the document PRINTS? — and treat disagreement as suspect. Silent unless
            # that printed page parses and verifies, so a document with no independent
            # statement of its structure is left exactly as before.
            overseg, overseg_why = False, ""
            from product_rules import (outline_is_authoritative, product_for_job,
                                       rebuild_outline_from_printed_toc)
            _product = product_for_job(dest)
            _authoritative = outline_is_authoritative(_product)
            # Which REPAIR this product wants: rebuild from the printed contents page, or
            # prune the embedded outline's prose in place. See the rule for the measurement —
            # on a flat product the two engines disagree about LEVELS, and a wrong level is a
            # wrong chunk.
            _no_prune = rebuild_outline_from_printed_toc(_product)
            if not suspect and not _authoritative and n_entries > USELESS_OUTLINE_MAX_ENTRIES:
                from check_toc_quality import _outline_disagrees, _read_printed_toc
                overseg, overseg_why = _outline_disagrees(toc, _read_printed_toc(doc, pdf))
        finally:
            doc.close()
        # Documents that cannot be trusted to use their own outline -- INCLUDING the ones
        # that have none at all. A healthy outline is still left alone entirely, so most of
        # the corpus pays nothing here.
        #
        # 0 entries used to be excluded, on the grounds that Stage 1's font-size heuristic
        # works on those (40 such documents, median completeness 90.2 against 94.3 for
        # properly-outlined ones) and that repairing them was "a different change with its
        # own blast radius". The blast radius turned out to be the other way round. A
        # document with no outline scores `toc` 55, +10 when its printed contents page
        # verifies = 65, against a chain-entry bar of 70 -- so it escalates anyway, and the
        # chain's TOC tier re-runs Stage 1-3 INCLUDING MinerU to do what this step does for
        # the cost of one Stage 1.
        #
        # Measured on 154_Marketing_Restrictions/Australia__181815 (171 pages, 0 entries):
        # first pass 30.4 min of MinerU on a tree that was then thrown away, then 26.1 min
        # more inside toc_rescue -- 56.9 min total for a result the pre-flight reaches in
        # ~26 min, because the adopted tree IS the rescued one. Same output, half the run.
        #
        # This is safe without a score comparison because ro.rescue REFUSES to repair
        # anything it cannot verify: no printed contents page, nothing parsed, or a parse
        # that does not check out against the pages it names all return without a
        # repaired_pdf, and the caller below treats that as "leave this document alone".
        if not suspect and not overseg and n_entries > USELESS_OUTLINE_MAX_ENTRIES:
            return None
        res = ro.rescue(dest, rerun=False, promote=False, backend=backend, effort=effort,
                        no_prune=_no_prune)
        repaired = res.get("repaired_pdf")
        if not repaired or not Path(repaired).exists():
            return None
        # Re-extract Stage 1 from the repaired PDF, in place. Stage 2 then runs ONCE,
        # against a tree that already has the right section boundaries.
        shutil.copy2(repaired, dest / "source_repaired.pdf")
        new_manifest = he.run_stage1(Path(repaired), dest / "01_stage1_extract", depth,
                                     clause_tables)
        (dest / "toc_preflight.json").write_text(json.dumps(
            {"applied": True, "status": res.get("status"),
             # Why the pre-flight fired: the cheap triage, or the printed-TOC
             # disagreement. Recorded so a document that used to need the fallback
             # chain and now does not is traceable to the reason.
             "trigger": ("outline_disagrees_with_printed_toc" if overseg else "suspect_outline"),
             "trigger_detail": overseg_why or _why,
             "toc_pages": res.get("toc_pages"), "engine": res.get("engine"),
             "entries": (res.get("check") or {}).get("entries"),
             "verified": (res.get("check") or {}).get("verified")}, indent=2))
        return new_manifest
    except Exception as e:  # noqa: BLE001 — a pre-flight must never sink a document
        try:
            (dest / "toc_preflight.json").write_text(json.dumps(
                {"applied": False, "error": f"{type(e).__name__}: {e}"}, indent=2))
        except OSError:
            pass
        return None


def _final_verdict(dest: Path, sc: dict) -> tuple[str | None, float | None, int]:
    """(gate, worst_score, scored_stage) -- the verdict a REVIEWER sees, as opposed to the
    extraction gate `sc` itself carries.

    `sc` is frozen at stage 3 on purpose -- it is the --resume completion marker, and a
    stage-4 outage must not un-complete a document that extracted cleanly (see run_one's
    comment above where stage 4 is called). But when stage 4/5 DID run, that tree is what
    actually ships, and "is this document good" should answer for the document as it now
    stands, not as it stood before the AI pass touched it.

    Reads scorecard_post_ai.json rather than recomputing it: _run_stage4_in_pipeline ->
    _score_post_ai already wrote it, if it is going to exist at all. One file read, and only
    on documents that opted into stage 4 in the first place -- every other document returns
    `sc`'s own gate/worst unchanged, at scored_stage 3."""
    post_ai_path = dest / "scorecard_post_ai.json"
    if post_ai_path.exists():
        try:
            sc_post = json.loads(post_ai_path.read_text())
        except (OSError, json.JSONDecodeError):
            sc_post = None
        if sc_post and sc_post.get("gate") is not None:
            return sc_post.get("gate"), sc_post.get("worst_score"), sc_post.get("scored_stage") or 5
    return sc.get("gate"), sc.get("worst_score"), 3


def bind_document(job: dict, product: str, dest: Path, pages: int | None = None,
                  index: int | None = None, total: int | None = None) -> str:
    """Bind the identity of one document ATTEMPT onto every event that follows.

    `aci.doc_run_id` is the field that makes the logs usable: a fresh id per attempt, so a
    Kibana filter on it returns exactly the lines of this pass and nothing from the retry that
    reprocessed the same jurisdiction an hour later. Filtering on jurisdiction alone cannot do
    that, and a stuck document is almost always one that has been attempted more than once.

    `aci.doc_started_at` is bound rather than logged because the heartbeat runs on another
    thread and derives its elapsed time from it -- see probe.Heartbeat._beat."""
    import uuid
    doc_run_id = uuid.uuid4().hex[:16]
    pdf = job.get("pdf")
    _log.bind(**{"aci.product": product,
                 "aci.jurisdiction": job.get("jurisdiction"),
                 "aci.doc_id": job.get("doc_id"),
                 "aci.label": job.get("label"),
                 "aci.doc_run_id": doc_run_id,
                 "aci.doc_index": index, "aci.doc_total": total,
                 "aci.pages": pages,
                 "aci.dest": str(dest),
                 "aci.pdf": str(pdf) if pdf else None,
                 "aci.pdf_bytes": (pdf.stat().st_size if pdf and Path(pdf).exists() else None),
                 "aci.doc_started_at": time.time()})
    return doc_run_id


def unbind_document() -> None:
    _log.unbind("aci.product", "aci.jurisdiction", "aci.doc_id", "aci.label", "aci.doc_run_id",
                "aci.doc_index", "aci.doc_total", "aci.pages", "aci.dest", "aci.pdf",
                "aci.pdf_bytes", "aci.doc_started_at", "aci.stage", "aci.stage_started_at")


# The result keys worth having as their own Kibana fields. run_one's dict is the richest
# summary of a document the pipeline produces and it was going only to a scorecard on disk.
_RESULT_FIELDS = ("status", "gate", "worst", "seconds", "tables", "mineru_pages", "route",
                  "cost_usd", "fallback", "fallback_tier", "fallback_reason",
                  "fallback_hard_fail", "fallback_accepted", "toc_rescued", "twin", "reason",
                  "detail", "stage4_status")


def _result_fields(res: dict) -> dict:
    out = {f"aci.{k}": res.get(k) for k in _RESULT_FIELDS if res.get(k) is not None}
    # The per-stage breakdown, one field each: `aci.step.stage2_mineru` is aggregatable in
    # Kibana, a `steps` object is not.
    for k, v in (res.get("steps") or {}).items():
        out[f"aci.step.{k}"] = v
    # A count, not the filenames: "how many documents had N stage-4 sections fail" is an
    # aggregatable Kibana question, "which ones" is what the ledger row and the drill-down
    # already answer.
    fs = res.get("stage4_failed_sections")
    if fs:
        out["aci.stage4_failed_count"] = len(fs)
    return out


def _run_one(job: dict, product: str, dest: Path, stage1_only: bool, force: bool,
             backend=None, effort=None, publish_local=None, hash_idx=None,
             progress_cb=None, trigger_cb=None) -> dict:
    """Extract one PDF into dest. Returns a small status dict. When publish_local is a
    dir, auto-publishes the finished doc into that local Doc Library store, so an
    extraction shows up in the gallery (with its gate + scorecard) with no separate step.
    progress_cb(stage: str), if given, is called at each stage transition — including
    "toc_rescue"/"mineru_fallback" as each fallback tier is entered — so a caller (the CLI
    loop's _progress.json, or a one-off test script) can show live stage-by-stage
    progress instead of just "running" for however long the whole document takes.

    trigger_cb(info), if given, fires ONCE when the fallback chain is entered, carrying WHY.
    Deliberately a second callback rather than an extra argument to progress_cb: every existing
    caller passes a one-argument function, and widening that signature would break them all for
    a value only the monitoring worker wants."""
    def _tick(stage):
        # Bound BEFORE the callback: the heartbeat thread reads the stage from the bound
        # context, and a beat that fires during a slow callback should already name the stage
        # being entered rather than the one just left.
        _log.bind(**{"aci.stage": stage, "aci.stage_started_at": time.time()})
        _log.event("stage.enter", message=f"-> {stage}", **{"aci.stage": stage})
        if progress_cb:
            progress_cb(stage)

    t_job = time.time()
    pdf = job["pdf"]
    marker = dest / "scorecard.json"
    if marker.exists() and not force:
        try:
            sc = json.loads(marker.read_text())
            return {"status": "skipped", "gate": sc.get("gate"),
                    "worst": sc.get("worst_score")}
        except (OSError, json.JSONDecodeError):
            pass  # unreadable -> fall through and redo it

    dest.mkdir(parents=True, exist_ok=True)
    local = dest / "source.pdf"
    if not local.exists() or force:
        local.write_bytes(pdf.read_bytes())
        # BEFORE anything reads it. A /Rotate 90 page hands back text in unrotated
        # coordinates, so Stage 1 reads it sideways -- see he.derotate_pdf for what that
        # did to Japan 172122. Done on the working copy, so the source is untouched and
        # every stage downstream (pre-flight, Stage 1, the MinerU crops, the page
        # snapshots, Stage 4's page images) sees the same upright pages.
        he.derotate_pdf(local)

    # Same bytes as a document already extracted? Clone it. The identity (product,
    # jurisdiction, doc id) still differs, so it publishes as its own gallery entry —
    # only the extraction is shared, and its scorecard is identical BY CONSTRUCTION,
    # which is more defensible than scoring the same input twice and hoping to agree.
    # The SOURCE's hash, not the working copy's. Identical for every document that has
    # no rotated page (the copy is byte-for-byte), and for the few that do it keeps
    # `pdf_sha1` meaning "which document is this" rather than "what did we rewrite it
    # to" -- which is what the clone/dedupe index is looking up.
    sha = pdf_sha1(pdf)
    (dest / "corpus_meta.json").write_text(json.dumps({
        "product": product, "jurisdiction": job["jurisdiction"],
        "doc_id": job["doc_id"], "source_pdf": str(pdf), "pdf_sha1": sha,
    }, indent=2))
    # A product rule that pins the ROUTE cannot be honoured by a clone: the twin was
    # extracted by whatever route ITS own identity selected, and hard-linking that tree
    # here would file it under a document the rule says must be parsed whole by the VLM.
    # Cheap to forgo — the rule covers one small folder, where a byte-identical duplicate
    # of an already-extracted document is not the corpus-wide 31-copies case dedupe exists for.
    forced_ai = ai_pipeline(product, content_tile_name(pdf, job["doc_id"]))
    twin = None if forced_ai else (hash_idx or {}).get(sha)
    if twin is not None and Path(twin).resolve() != dest.resolve() and not stage1_only:
        clone_job(Path(twin), dest)
        sc = json.loads((dest / "scorecard.json").read_text())
        # A clone hard-links a finished tree; it did NOT pay for the extraction, the
        # twin did. Inheriting the twin's wall clock unchallenged would let a corpus
        # of 53 clones report 53x the GPU hours actually spent -- exactly the number
        # a capacity estimate reads. So the inherited cost is relabelled, not kept.
        inherited = (sc.get("timing") or {}).get("seconds")
        sc["timing"] = dict(sc.get("timing") or {}, **{
            "seconds": round(time.time() - t_job, 1),
            "extraction_seconds": inherited,
            "cloned_from": Path(twin).name,
            "steps": {}, "slowest_step": None,
        })
        # BREAK THE HARD LINK FIRST. clone_job links the artifacts rather than copying
        # them, so dest/scorecard.json and the twin's are the SAME INODE -- writing in
        # place truncates it under both names and rewrites the twin's scorecard with the
        # clone's zero cost. Writing a fresh file and renaming over the link swaps this
        # directory entry alone and leaves the twin's inode untouched.
        _tmp = dest / "scorecard.json.tmp"
        _tmp.write_text(json.dumps(sc, indent=2))
        os.replace(_tmp, dest / "scorecard.json")
        (dest / "duplicate_of.json").write_text(json.dumps(
            {"pdf_sha1": sha, "cloned_from": str(twin)}, indent=2))
        # A clone is a document in its own right — same extraction, its own product,
        # jurisdiction and id — so it must reach the Doc Library like any other.
        # Returning early here published only the 5 extracted US documents and left
        # the 53 cloned ones invisible in the gallery.
        _publish(publish_local, product, job, dest)
        # A CLONE IS SCORED LIKE ANY OTHER DOCUMENT. clone_job links the twin's whole tree,
        # post-AI scorecard included, so reporting `sc` alone would put a stage-3 verdict on
        # the ledger for a document whose published tree went through stage 4/5 -- and the
        # monitor would show it an SC2 dash while its twin, the same bytes, showed a score.
        c_gate, c_worst, c_stage = _final_verdict(dest, sc)
        return {"status": "duplicate", "twin": Path(twin).name,
                "gate": c_gate, "worst": c_worst, "scored_stage": c_stage,
                "gate_extraction": sc.get("gate"), "worst_extraction": sc.get("worst_score")}

    # Per-step wall clock, written to timings.json. Stage 2 dominates so heavily
    # (MinerU is ~11s per cropped table page, everything else is seconds) that a
    # single end-to-end number tells you nothing about where a slow document went.
    t0 = time.time()
    steps: dict[str, float] = {}

    def _step(name, fn, *a, **kw):
        """Time a stage AND announce it — the three consumers want different things: the
        progress file wants to know what is happening now, timings.json wants to know
        how long it took, and Kibana wants both plus which one a crash happened in."""
        _tick(name)
        t = time.time()
        try:
            out = fn(*a, **kw)
        except BaseException as e:
            steps[name] = round(time.time() - t, 1)
            # The stage is the single most useful fact about a failure: a CalledProcessError
            # in stage2_mineru is a MinerU problem and the identical exception in stage1 is
            # not. The exception itself never carries it.
            _log.event("stage.end", level="error",
                       message=f"{name} failed: {type(e).__name__}: {e}",
                       **{"aci.stage": name, "event.outcome": "failure",
                          "event.duration_s": steps[name],
                          "error.type": type(e).__name__, "error.message": str(e)[:_log.MAX_STR]})
            raise
        steps[name] = round(time.time() - t, 1)
        _log.event("stage.end", message=f"{name} ok in {steps[name]}s",
                   **{"aci.stage": name, "event.outcome": "success",
                      "event.duration_s": steps[name]})
        return out

    # Per-product rule, not a global default: some products cannot have their
    # subsections split at all (see product_rules.SECTION_DEPTH).
    depth = section_depth(product)
    clause_tables = clause_table_runs(product)
    # A product rule may raise MinerU's effort; an explicit --mineru-effort wins.
    effort = mineru_effort(product, effort)
    # ---- ROUTED STRAIGHT TO MINERU ------------------------------------------------
    def _straight_to_mineru(reason: str, chain: list, *, skip_status: str,
                            fb_extra: dict | None = None) -> dict:
        """Hand the whole document to MinerU and score THAT, skipping the pre-flight,
        Stage 2 and Stage 3.

        Two routes arrive here and the work is identical: Stage 1 could not read the
        document at all, or the document is too short to have a hierarchy worth building.
        What differs is only what the scorecard records about WHY, so `reason` and `chain`
        are the whole difference and every consumer that already explains "why is this
        document on the MinerU tier" keeps working for both.

        MinerU writes all three stage dirs itself, including a stage1_report.json carrying
        the page count, so validation and scoring run over its output unchanged — and the
        short-document scoring rule still applies, because it reads that page count.

        Adoption is unconditional here: there is no first-pass tree to compare against, so
        `_better()` has nothing to weigh. That is already how this route behaved for a
        document Stage 1 could not read.
        """
        if stage1_only:
            return {"status": skip_status, "reason": reason, "steps": steps,
                    **(fb_extra or {}), "seconds": round(time.time() - t0, 1)}
        _step("mineru_full", run_mineru_full, local, dest, backend=backend, effort=effort)
        val = _step("validation", _validate, dest)
        sc = _step("scorecard", compute_scorecard, dest, val)
        fb = sc.setdefault("fallback", {})
        fb.update(triggered=True, adopted_tier="mineru_full", reason=reason, chain=chain,
                  **(fb_extra or {}))
        ok, why_not = result_is_acceptable(sc)
        fb["accepted"] = ok
        if not ok:
            fb["hard_fail"] = True
            fb["hard_fail_reason"] = why_not
        n_pages = stage1_pages(dest)
        sc["timing"] = timing_block(steps, started=t0, finished=time.time(), pages=n_pages)
        (dest / "validation.json").write_text(json.dumps(val, indent=2))
        marker.write_text(json.dumps(sc, indent=2))
        if publish_local:
            _step("publish", _publish, publish_local, product, job, dest)
        steps["total"] = round(time.time() - t0, 1)
        (dest / "timings.json").write_text(json.dumps(
            {"steps": steps, "pages": n_pages, **(fb_extra or {})}, indent=2))
        return {"status": "done", "gate": sc.get("gate"), "worst": sc.get("worst_score"),
                "tables": None, "mineru_pages": None, "steps": steps,
                "seconds": steps["total"], "fallback": True,
                "fallback_tier": "mineru_full", "fallback_reason": reason,
                "fallback_hard_fail": bool(fb.get("hard_fail")) or None,
                "fallback_accepted": fb.get("accepted"), **(fb_extra or {})}

    def _straight_to_summary_ai() -> dict:
        """Extract this document with the summary AI pipeline and score THAT.

        Deliberately does NOT call _validate/compute_scorecard. Those measure a stage-1
        tree against MinerU's tables -- orphan tables, per-section gap ratios, TOC
        fidelity -- and none of it exists here: there is no Stage 1, no Stage 2 and no
        stage-3 splice. summary_ai_extract writes its own scorecard over the three things
        a transcription can get wrong plus punctuation, and marks it
        special_mode=ai_transcription so no consumer reads it as a stage-1-3 gate.

        The stage dir it writes is 04_stage4_ai/, which is what makes the content visible
        in the corpus view: hybrid_extract_ui's Document pane builds its tree only from a
        directory named in STAGE_DIRS.
        """
        if stage1_only:
            return {"status": "skipped_summary_ai", "reason": "--stage1-only",
                    "steps": steps, "seconds": round(time.time() - t0, 1)}
        import summary_ai_extract as sai

        (dest / "summary_ai_rule.json").write_text(json.dumps(
            {"product": product, "jurisdiction": job["jurisdiction"],
             "pages": pdf_page_count(local), "decided": "before Stage 1",
             "module": "summary_ai_extract",
             "why": "a product rule extracts this folder with the summary AI pipeline: "
                    "these documents carry their structure in coloured heading bars and "
                    "embedded status squares, which no text extractor can see, so Stage "
                    "1/2/3, the printed-TOC rescue and the short-document test are all "
                    "skipped rather than run and discarded"},
            indent=2))
        # Through _step, not a bare timer: _step ticks the run heartbeat as it enters, and
        # that tick is the ONLY live signal this route has. Everything else the pipeline
        # monitor reads is a completion artifact, so without it a document sits on
        # "Fetched / queued" for the whole two-minute call and the pass column shows
        # nothing -- the same blind spot LIVE_TIERS was added for.
        res = _step("summary_ai", sai.extract_document,
                    local, dest, product=product, jurisdiction=job["jurisdiction"],
                    doc_id=job["doc_id"], log=lambda m: print(m, flush=True))
        sc = res["scorecard"]
        # No validation ran, but the file must exist: the review UI and the corpus tools
        # both open it, and a missing one reads as a broken job rather than an inapplicable
        # check. It says which, explicitly.
        (dest / "validation.json").write_text(json.dumps(
            {"not_applicable": True,
             "why": "extracted by the summary AI pipeline; the stage 1-3 checks this file "
                    "normally carries have no artefacts to run against",
             "checks": []}, indent=2))
        marker.write_text(json.dumps(sc, indent=2, ensure_ascii=False))
        if publish_local:
            _step("publish", _publish, publish_local, product, job, dest)
        steps["total"] = round(time.time() - t0, 1)
        (dest / "timings.json").write_text(json.dumps(
            {"steps": steps, "pages": res["timing"]["pages"],
             "cost_usd": res["cost_usd"]}, indent=2))
        return {"status": "done", "gate": sc.get("gate"), "worst": sc.get("worst_score"),
                "tables": None, "mineru_pages": None, "steps": steps,
                "seconds": steps["total"], "route": "summary_ai",
                "cost_usd": res["cost_usd"]}

    # ---- PRODUCT RULE: extracted by the summary AI pipeline, not by stages 1-3 -----
    # Asked FIRST, before the page-count test and before Stage 1 runs, because it does not
    # share a destination with any other route: the page count would otherwise send a
    # 5-page summary down the short-document branch and extract it by a route measured as
    # wrong for these documents. See product_rules.AI_PIPELINE for the measurement, the
    # cost, and the one open defect.
    #
    # Deliberately NOT gated on DISABLE_MINERU_FALLBACK: that switch exists to let someone
    # iterate on Stage 1 by taking the normal path, and this is not a fallback -- nothing
    # has failed. It is the route these documents are extracted by.
    #
    # IT SPENDS MONEY, which no other route here does: one Bedrock call per document,
    # ~$0.026/page. ACI_SUMMARY_AI=0 turns it off, and a disabled document is SKIPPED
    # rather than quietly re-routed -- falling through to Stage 1 would produce a
    # plausible-looking tree by the route this rule exists to avoid, and nothing
    # downstream would say so.
    if forced_ai:
        if os.environ.get("ACI_SUMMARY_AI", "1") == "0":
            return {"status": "skipped_summary_ai", "gate": None, "worst": None,
                    "reason": "ACI_SUMMARY_AI=0 — the AI route is off and stages 1-3 are "
                              "not a substitute for it on these documents",
                    "steps": steps, "seconds": round(time.time() - t0, 1)}
        return _straight_to_summary_ai()

    # ---- PAGE COUNT: a fact about the SOURCE, so it is asked before anything is extracted ----
    # A document under SHORT_DOC_MAX_PAGES pages has no hierarchy worth reconstructing: its
    # tree comes from MinerU's raw markdown whatever Stage 1 would have produced, and its
    # score is word coverage alone. The count is read straight off the PDF with fitz, so
    # NOTHING the extraction produces can change this routing.
    #
    # It used to be asked inside the fallback chain, i.e. after Stage 2 had already been paid
    # for. Measured over the 14 short documents in this corpus: 341.6s went on Stages 1-3 plus
    # scoring and all 14 then adopted MinerU's tree, so every one of those seconds built a tree
    # that was immediately thrown away. Angola (Data Privacy) 113163 recorded stage2_mineru
    # 24.9s, then mineru_full 32.0s replacing what it produced.
    #
    # This is the same move the outline pre-flight made for the same reason — a decision that
    # needs nothing from the extraction belongs above the expensive step, not below it.
    #
    # DISABLE_MINERU_FALLBACK still wins: with the chain off, a short document takes the
    # normal path and is scored on it, which is what makes that switch usable for iterating
    # on Stage 1.
    n_src_pages = pdf_page_count(local)
    if 0 < n_src_pages < SHORT_DOC_MAX_PAGES and not os.environ.get("DISABLE_MINERU_FALLBACK"):
        (dest / "short_document.json").write_text(json.dumps(
            {"pages": n_src_pages, "threshold": SHORT_DOC_MAX_PAGES,
             "decided": "before Stage 1",
             "why": "the page count is a property of the source PDF, so no extraction "
                    "output can change this routing — Stage 1, the outline pre-flight, "
                    "Stage 2 and Stage 3 are all skipped rather than run and discarded"},
            indent=2))
        return _straight_to_mineru(
            f"short document ({n_src_pages} pages): routed straight to MinerU before "
            f"Stage 1, structural tiers skipped",
            [{"tier": "toc_rescue",
              "status": f"skipped: {n_src_pages}-page document \u2014 too short for a "
                        f"printed-TOC rescue"},
             {"tier": "mineru_full", "adopted": True,
              "status": f"routed here before Stage 1 \u2014 {n_src_pages} pages, under the "
                        f"{SHORT_DOC_MAX_PAGES}-page bar"}],
            skip_status="short_document",
            fb_extra={"short_document": {"pages": n_src_pages,
                                         "threshold": SHORT_DOC_MAX_PAGES}})

    # Stage 1 either produces a tree or says, by name, that it cannot read this document.
    # The second is not a crash to report — it is a routing decision (see STAGE1_BAIL).
    bail = None
    try:
        manifest = _step("stage1", he.run_stage1, local, dest / "01_stage1_extract", depth,
                         clause_tables)
    except Exception as e:                              # noqa: BLE001 — classified, not swallowed
        cause, why = classify_stage1_failure(str(e))
        bail = {"cause": cause, "reason": why, "error": str(e)[:1000]}
        manifest = None
        # A document with a text layer but no detectable headings may simply print its
        # contents page instead of styling its headings. Building the outline from that is
        # ~1.5s and, when it works, keeps the document on the normal one-MinerU path.
        if cause == "no_structure":
            recovered = _step("stage1_toc_rebuild", _stage1_from_printed_toc,
                              dest, local, backend, effort, depth, clause_tables)
            if recovered is not None:
                manifest, bail = recovered, None

    if bail is not None:
        # Skip the pre-flight, Stage 2 and Stage 3 outright: there is no tree to repair the
        # outline of, and no tree to crop tables out of. Same route the page-count test above
        # takes, and recorded the same way, so every consumer that already explains "why is
        # this document on the MinerU tier" keeps working — plus `stage1_bail`, which is the
        # part no existing field can carry.
        (dest / "stage1_bailed.json").write_text(json.dumps(bail, indent=2))
        return _straight_to_mineru(
            f"Stage 1 could not read this document: {bail['reason']}",
            [{"tier": "toc_rescue",
              "status": "skipped: Stage 1 produced no tree to rescue"},
             {"tier": "mineru_full", "adopted": True,
              "status": f"routed here directly — {bail['reason']}"}],
            skip_status="stage1_bailed", fb_extra={"stage1_bail": bail})

    # ---- PRE-FLIGHT: is the structure source trustworthy? ---------------------
    # Judging the outline needs NOTHING that extraction produces — it is a property
    # of the PDF: does a bookmark outline exist, does it start where the document
    # does, and does the printed contents page verify against the pages it names.
    # Measured on ADGM 170680 that decision costs ~1 second (10ms for the outline,
    # 969ms to find/parse/verify the contents page).
    #
    # It used to be taken AFTER scoring, because the TOC check arrived as a scorecard
    # dimension and inherited that slot. The document therefore paid for a full MinerU
    # table pass, got scored, failed, and then the rescue re-ran Stage 1-3 — including
    # MinerU a SECOND time — against a PDF whose only difference was its bookmark
    # metadata. ADGM: 1176s total, 586s of it Stage 2 and 578s the rescue redoing it.
    #
    # Repairing the outline here instead costs one extra Stage 1 (7s) and removes the
    # duplicate MinerU pass entirely. The fallback CHAIN is untouched and still runs
    # after scoring: "did the extraction come out well enough" genuinely needs the
    # extraction, unlike "is this outline usable".
    manifest = _step("toc_preflight", _preflight_outline, dest, local, manifest,
                     backend, effort, depth, clause_tables) or manifest
    n_tables = len(manifest.get("tables", []))
    pages_to_mineru = len({p for t in manifest.get("tables", []) for p in t["pages"]})
    if stage1_only:
        return {"status": "stage1", "tables": n_tables, "steps": steps,
                "mineru_pages": pages_to_mineru, "seconds": round(time.time() - t0, 1)}

    s2 = _step("stage2_mineru", he.run_stage2, local, manifest,
               dest / "02_stage2_mineru_tables", backend, effort,
               dest / "01_stage1_extract")
    _step("stage3", he.run_stage3, dest / "01_stage1_extract",
          dest / "02_stage2_mineru_tables", dest / "03_stage3_final", s2)
    val = _step("validation", _validate, dest)
    sc = _step("scorecard", compute_scorecard, dest, val)
    # A completeness gate escalates through the fallback tiers in order — printed-TOC
    # rescue first, whole-document MinerU re-parse only if that did not recover the
    # document (see fallback_chain). Each tier is timed and announced like any other
    # stage, so a fallback document's cost is visible per tier, not as one lump.
    val, sc = run_chain(dest, local, val, sc, backend=backend, effort=effort, step=_step,
                        on_trigger=trigger_cb)
    # Stamped BEFORE the scorecard is written, because the scorecard is the artifact
    # that survives: timings.json is not cloned and nothing reads it, so a cost that
    # lives only there is lost to every consumer that matters.
    n_pages = stage1_pages(dest)
    sc["timing"] = timing_block(steps, started=t0, finished=time.time(),
                                pages=n_pages, tables=n_tables,
                                mineru_crop_pages=pages_to_mineru)
    # Written LAST: its presence is what marks the job complete for --resume, so
    # a run killed mid-document is redone rather than half-counted.
    (dest / "validation.json").write_text(json.dumps(val, indent=2))
    marker.write_text(json.dumps(sc, indent=2))
    if publish_local:   # auto-wire the finished doc into the local Doc Library
        _step("publish", _publish, publish_local, product, job, dest)
    # Kept alongside the scorecard copy, and deliberately NOT the same number: this
    # total runs to here, so it includes any --publish-local step, while the
    # scorecard's `timing.seconds` stops at the gate. Extraction cost is the
    # scorecard's; end-to-end wall clock is this one's.
    steps["total"] = round(time.time() - t0, 1)
    (dest / "timings.json").write_text(json.dumps(
        {"steps": steps, "pages": n_pages, "tables": n_tables,
         "mineru_crop_pages": pages_to_mineru}, indent=2))
    # ---- STAGE 4 + 5, when the host has opted in --------------------------------------
    # The flag existed and nothing here honoured it: stage 4 was reachable only from the
    # command line, so ACI_STAGE4_AI_ENABLED=1 on a server changed nothing, and no ledger
    # row ever carried an AI step -- which is why the Core Index could show the two
    # stages and never a document in them.
    #
    # Placed HERE, deliberately, after five things have already happened:
    #   * sc["timing"] is stamped, so the scorecard's extraction cost stays extraction
    #     cost and does not silently grow to include paid API time;
    #   * the scorecard is written, so a stage-4 failure cannot un-complete a document
    #     that extracted perfectly well;
    #   * steps["total"] is computed, for the same reason -- every capacity estimate
    #     built from a finished corpus reads `total` as extraction;
    #   * timings.json is written, so run_stage4's own _record_timings MERGES into it
    #     rather than being clobbered by a later write;
    #   * publish has run, because what is published is the stage 3 tree.
    # Stage 4 is additive (it writes 04_stage4_ai beside stage 3, never over it), so
    # nothing above depends on it.
    stage4_cost = None
    if _stage4_enabled():
        # ANNOUNCED, not just run. Every other stage enters through `_step`, which ticks the
        # progress file on the way in; this one is called directly (it sets its own per-stage
        # seconds from the run report, which `_step`'s timing would overwrite with stage 4 and
        # 5 lumped together). The cost of that was a screen that lied for eight minutes: the
        # last tick was "scorecard", EXSTAGE_NODE maps scorecard AND publish to the same node,
        # so a document paying for a 500-second AI pass showed a pulsing SCORECARD -- a stage
        # that takes 0.0s -- and the Stage 4 node stayed dark until the run was over.
        #
        # Ticked without timing, so `steps` keeps the honest per-stage numbers.
        _tick("stage4_ai")
        stage4_cost = _run_stage4_in_pipeline(dest, local, steps, log=print)

    # Did Stage 4 actually finish every section it started? Read straight back off what it
    # just wrote, the same way a monitor reading this job later would — so a document that
    # LOOKS done (the scorecard gate below only ever describes stages 1-3) cannot hide a
    # section Stage 4 silently lost. None when Stage 4 does not apply here at all (disabled,
    # or the summary-AI route), which the monitor shows as blank rather than a false green.
    from aosphere_core_index.extract.ai_postprocess import stage4_section_health
    stage4_health = stage4_section_health(dest)

    gate, worst, scored_stage = _final_verdict(dest, sc)
    # The EXTRACTION scorecard, dimension by dimension, for every document — not just the
    # ones that ran stage 4 (those also get doc.scored_post_ai, with the drop). gate and
    # worst_score alone say a document is weak but never in what way, and "which dimension
    # regressed across the run" is the question a corpus-wide comparison is actually made of.
    _log.event("doc.scored",
               message=(f"extraction: {sc.get('gate')} worst={sc.get('worst_score')} "
                        f"({sc.get('weakest_dimension')})"),
               **{"aci.gate_extraction": sc.get("gate"),
                  "aci.worst_extraction": sc.get("worst_score"),
                  "aci.weakest_dimension": sc.get("weakest_dimension"),
                  "aci.scored_stage": scored_stage,
                  "aci.chunks": (sc.get("structure") or {}).get("chunks"),
                  **_log.flatten("aci.dim", {k: (v or {}).get("score") for k, v
                                             in (sc.get("dimensions") or {}).items()},
                                 keep=20)})
    _fb = sc.get("fallback") or {}
    return {"status": "done", "gate": gate, "worst": worst,
            # BOTH verdicts, not just the one a reviewer is shown. `gate`/`worst` above are
            # whichever scorecard is final (post-AI where stage 4/5 ran), and until now the
            # stage-3 pair was computed, used for the gate, and then dropped -- so the one
            # question the AI pass exists to answer, "did it make this document better or
            # worse", could only be answered by opening two scorecards per document. These
            # two fields are ~25 bytes on a ledger row and let the monitor put SC1 and SC2
            # side by side with no extra read at all. Identical to gate/worst on a document
            # that never ran stage 4/5, and that is fine: the screen reads scored_stage to
            # know whether there is a second verdict to show.
            "gate_extraction": sc.get("gate"), "worst_extraction": sc.get("worst_score"),
            "tables": n_tables, "mineru_pages": pages_to_mineru, "steps": steps,
            "seconds": steps["total"],
            # Which tree the gate/worst above actually describe -- 3 for the (majority)
            # documents that never ran stage 4/5, 5 when the post-AI tree overrode them.
            "scored_stage": scored_stage,
            # What stage 4 spent on this document -- None on every document that never ran
            # it, which _row() drops rather than writing a false "$0.00" onto the ledger.
            "cost_usd": stage4_cost,
            # Stage 4's own per-section health -- None where it does not apply (never ran, or
            # the summary-AI route), "ok"/"failed"/"incomplete" otherwise. The failed section
            # FILENAMES only, not their reasons: this rides on the ledger row (corpus_worker's
            # 350KB-at-the-end budget), and the reason text belongs on the one-document
            # drill-down, which reads stage4_report.json itself when a row is opened.
            "stage4_status": (stage4_health or {}).get("status"),
            "stage4_failed_sections": [s["file"] for s in (stage4_health or {}).get("failed_sections", [])] or None,
            # The outline was rebuilt from the printed contents page before Stage 2 ran.
            "toc_rescued": _toc_rescued(dest) or None,
            "fallback": bool(_fb.get("triggered")),
            # WHICH tier the document ended up on: a fallback is no longer synonymous
            # with a MinerU re-parse, so the log line has to name the tier
            "fallback_tier": _fb.get("adopted_tier"),
            # WHY it escalated. Two shapes, because the chain has two entrances: the normal
            # path records it under first_attempt (preserved before any tier rewrites the
            # scorecard), the short-document path sets `reason` directly. A caller reading
            # only one of them explains 4 of this corpus's 18 fallbacks and blanks the rest.
            "fallback_reason": ((_fb.get("first_attempt") or {}).get("entry_reason")
                                or _fb.get("reason")),
            # Every tier ran and none cleared the bar — distinct from a bad gate, and the
            # signal that a human has to look rather than the run being retried.
            "fallback_hard_fail": bool(_fb.get("hard_fail")) or None,
            "fallback_accepted": _fb.get("accepted")}


def run_one(job: dict, product: str, dest: Path, *a, **kw) -> dict:
    """_run_one, wrapped in the document's own start/end/fail events.

    A wrapper rather than events inside the body because _run_one has a dozen return points
    (skipped, duplicate, stage1-only, short_document, stage1_bailed, skipped_summary_ai, the
    summary-AI route, the ordinary spine) and a document that ends on the one path nobody
    instrumented is a document that shows up in Kibana as a start with no end -- which is the
    exact signature of the hang this is here to detect. One wrapper cannot miss a path."""
    if not _log.snapshot().get("aci.doc_run_id"):
        # A caller that did not bind still gets an identified document rather than events
        # that cannot be correlated to anything.
        bind_document(job, product, dest)
    t0 = time.time()
    _log.event("doc.start", message=f"{product}/{job.get('label')}",
               **{"aci.stage1_only": bool(a[0]) if a else kw.get("stage1_only"),
                  "aci.force": bool(a[1]) if len(a) > 1 else kw.get("force")})
    try:
        res = _run_one(job, product, dest, *a, **kw)
    except BaseException as e:
        _log.exception("doc.fail", e,
                       **{"event.duration_s": round(time.time() - t0, 2),
                          # The stage bound by the last _tick: which step it died in.
                          "aci.failed_stage": _log.snapshot().get("aci.stage")})
        raise
    secs = round(time.time() - t0, 2)
    pages = _log.snapshot().get("aci.pages")
    _log.event("doc.end",
               level="warn" if (res or {}).get("gate") == "fail" else "info",
               message=(f"{product}/{job.get('label')} {res.get('status')} "
                        f"gate={res.get('gate')} in {secs}s"),
               **{"event.outcome": "success", "event.duration_s": secs,
                  "aci.seconds_per_page": (round(secs / pages, 2)
                                           if isinstance(pages, int) and pages else None),
                  **_result_fields(res or {})})
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="corpus root containing <product>/<jurisdiction>/*.pdf")
    ap.add_argument("--out", default=str(CORPUS_ROOT), help="output root")
    ap.add_argument("--only", action="append", default=None,
                    help="run only this product folder (repeatable)")
    ap.add_argument("--stage1-only", action="store_true",
                    help="deterministic Stage 1 only — no MinerU, no scores")
    ap.add_argument("--force", action="store_true", help="redo completed jobs")
    ap.add_argument("--mineru-backend", default=None)
    ap.add_argument("--mineru-effort", default=None)
    ap.add_argument("--publish-local", default=None,
                    help="auto-publish each finished doc into this local Doc Library store (e.g. out/extractions)")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    tree = discover(root)
    if args.only:
        tree = {k: v for k, v in tree.items() if k in set(args.only)}
    if not tree:
        sys.exit(f"no PDFs found under {root}" + (f" matching {args.only}" if args.only else ""))

    total = sum(len(v) for v in tree.values())
    # Same reason as the worker: an uncaught exception here is a crash whose only trace
    # would otherwise be a stderr traceback, invisible to anything reading the events.
    _log.install_crash_handlers()
    _log.bind_service("aosphere-extract", **{"aci.run_id": out_root.name,
                                             "aci.runner": "run_corpus"})
    _log.event("run.start", message=f"{len(tree)} folder(s), {total} document(s)",
               **{"aci.folders": len(tree), "aci.docs_total": total,
                  "aci.root": str(root), "aci.out_root": str(out_root),
                  "aci.stage1_only": bool(args.stage1_only), "aci.force": bool(args.force),
                  "aci.only": args.only, "aci.mineru_backend": args.mineru_backend,
                  "aci.mineru_effort": args.mineru_effort})
    # Built once from what is already on disk, then kept current as the run proceeds,
    # so a duplicate is recognised whether its twin was extracted last month or ten
    # minutes ago.
    hash_idx = {} if args.force else hash_index(out_root)
    banner(f"Corpus · {len(tree)} folder(s), {total} document(s)"
           + (f"  [{len(hash_idx)} known PDF hash(es)]" if hash_idx else "")
           + ("  [STAGE 1 ONLY]" if args.stage1_only else ""))

    # A progress file so the dashboard can show what is happening right now, not
    # just what has finished — the run is long enough that "nothing on screen yet"
    # would otherwise be indistinguishable from a hang.
    prog_path = out_root / "_progress.json"

    def write_progress(**kw):
        prog_path.write_text(json.dumps({"updated": time.time(), **kw}, indent=2))

    done = 0
    for product, jobs in tree.items():
        print(f"\n{BOLD(product)}  ({len(jobs)} document(s))")
        for job in jobs:
            label = job["label"]
            dest = out_root / product / label

            def _cb(stage, product=product, label=label, done=done):
                write_progress(state="running", product=product, document=label,
                               stage=stage, done=done, total=total)

            bind_document(job, product, dest, index=done + 1, total=total)
            # Local runs get the heartbeat too: "it has been in stage2_mineru for 40 minutes
            # and the output directory has not grown" is the same question on a laptop.
            hb = (_probe.Heartbeat(watch=dest, scratch=str(out_root)).start()
                  if _probe is not None and _log.enabled() else None)
            _cb("starting")
            try:
                r = run_one(job, product, dest, args.stage1_only, args.force,
                            args.mineru_backend, args.mineru_effort, args.publish_local,
                            hash_idx=hash_idx, progress_cb=_cb)
                if r.get("status") in ("done", "duplicate"):
                    try:
                        hash_idx.setdefault(json.loads((dest / "corpus_meta.json").read_text())["pdf_sha1"], dest)
                    except (OSError, KeyError, json.JSONDecodeError):
                        pass
            except Exception as e:  # noqa: BLE001 — one bad document must not sink the corpus
                # Not re-logged here: run_one's wrapper already emitted doc.fail with the full
                # traceback and the stage it died in. A second event would double every
                # failure count in Kibana.
                traceback.print_exc()
                r = {"status": "error", "detail": str(e)}
            finally:
                if hb is not None:
                    hb.stop()
                unbind_document()
            done += 1
            if r["status"] == "error":
                msg = RED(f"ERROR  {str(r.get('detail'))[:60]}")
            elif r["status"] == "duplicate":
                msg = DIM(f"duplicate of {r['twin']} — cloned, no extraction "
                          f"(gate={r.get('gate')})")
            elif r["status"] == "skipped":
                msg = DIM(f"skipped (already done, gate={r.get('gate')})")
            elif r["status"] == "stage1":
                msg = (f"stage1 ok  {r['tables']} tables, "
                       f"{r['mineru_pages']} pages would reach MinerU  {r['seconds']}s")
            # --stage1-only, and this document does not go through Stage 1 at all. Named
            # rather than left to fall through: the branch below reads r['tables'], which
            # neither of these routes has, so a short document used to end the run with a
            # KeyError instead of a line.
            elif r["status"] in ("short_document", "stage1_bailed",
                                 "skipped_summary_ai"):
                msg = DIM(f"stage1 skipped  {str(r.get('reason'))[:66]}")
            # The AI route has no tables and no fallback tier, and the branch below prints
            # both -- so a summary would read "None tables" and hide the one number that
            # matters on this route, which is what it cost.
            elif r.get("route") == "summary_ai":
                colour = {"pass": GREEN, "review": YELLOW,
                          "fail": RED}.get(r.get("gate"), DIM)
                msg = (f"{colour(str(r.get('gate')))}  {BOLD('summary-ai')}  "
                       f"worst={r.get('worst')}  ${r.get('cost_usd', 0):.4f}  "
                       f"{r['seconds']}s")
            else:
                colour = {"pass": GREEN, "review": YELLOW, "fail": RED}.get(r.get("gate"), DIM)
                st = r.get("steps") or {}
                # slowest-first, so the line names the step that actually cost the time
                worst_steps = " ".join(
                    f"{k} {v:.0f}s" for k, v in sorted(
                        ((k, v) for k, v in st.items() if k != "total" and v >= 1),
                        key=lambda kv: -kv[1])[:3])
                tier = {"toc_rescue": "⟲toc-rescue", "mineru_full": "⟲mineru-fallback",
                        "stage1": "⟲fallback-tried"}.get(r.get("fallback_tier"))
                fb = "  " + BOLD(tier) if r.get("fallback") and tier else ""
                msg = (f"{colour(str(r.get('gate')))}{fb}  worst={r.get('worst')}  "
                       f"{r['tables']} tables  {r['seconds']}s"
                       + (DIM(f"   [{worst_steps}]") if worst_steps else ""))
            print(f"  [{done}/{total}] {label:<28} {msg}")
    write_progress(state="finished", done=done, total=total)
    _log.event("run.end", message="corpus run complete",
               **{"aci.docs_done": done, "aci.docs_total": total,
                  "event.outcome": "success"})
    print(f"\n{GREEN(BOLD('corpus run complete'))}  ->  {out_root}")


if __name__ == "__main__":
    main()
