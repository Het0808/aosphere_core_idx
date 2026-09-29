"""The run events that make a cluster extraction debuggable from Kibana.

The failure these exist to prevent is specific and has already happened: on the MRAM run,
Canada and New Zealand appeared to hang and there was nothing to look at afterwards. Two
properties are what make that impossible to repeat, and both are easy to break silently —

  1. every document that starts also ENDS, on every return path, or a finished document is
     indistinguishable in Kibana from a wedged one;
  2. the heartbeat, which runs on its own thread, can still see which document and stage it
     is reporting on — a new thread does not inherit a contextvar, so this is not free.

The rest pins the wire format, because a field whose type or name drifts is a Kibana query
that silently returns nothing rather than an error anybody notices.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from aosphere_core_index.obs import log, probe  # noqa: E402
import run_corpus as rc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_context():
    """Each test starts with an empty context: bindings are process-wide by design."""
    log.unbind(*log.snapshot().keys())
    yield
    log.unbind(*log.snapshot().keys())


def _events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if line.startswith("{")]


# ---------------------------------------------------------------- wire format


def test_one_event_is_one_line_of_json(capsys):
    """One line per event, because the log agent splits on newlines — a pretty-printed
    object would arrive as a dozen unrelated documents with the fields scattered."""
    log.event("doc.start", **{"aci.label": "Canada__174237"})
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    rec = json.loads(out)
    assert rec["event.action"] == "doc.start"
    assert rec["aci.label"] == "Canada__174237"
    assert rec["@timestamp"].endswith("Z")


def test_bound_context_reaches_every_later_event(capsys):
    """The MDC property: bind once at the seam, and the call site that forgot to pass
    `jurisdiction` is still findable by a filter on it."""
    log.bind(**{"aci.run_id": "2026-08-19", "aci.jurisdiction": "New Zealand"})
    log.event("stage.enter", **{"aci.stage": "stage2_mineru"})
    rec = _events(capsys)[0]
    assert rec["aci.run_id"] == "2026-08-19"
    assert rec["aci.jurisdiction"] == "New Zealand"
    assert rec["aci.stage"] == "stage2_mineru"


def test_explicit_fields_beat_the_bound_context(capsys):
    log.bind(**{"aci.stage": "stage1"})
    log.event("stage.end", **{"aci.stage": "stage3"})
    assert _events(capsys)[0]["aci.stage"] == "stage3"


def test_none_is_dropped_rather_than_bound_or_emitted(capsys):
    """A field that is sometimes absent and sometimes null is two states to query."""
    log.bind(**{"aci.cost_usd": None})
    log.event("doc.end", **{"aci.gate": None, "aci.worst": 0.82})
    rec = _events(capsys)[0]
    assert "aci.cost_usd" not in rec and "aci.gate" not in rec
    assert rec["aci.worst"] == 0.82


def test_nan_never_reaches_the_line():
    """NaN is not JSON. Elasticsearch's bulk API rejects the whole document over one, so a
    single stray float would drop the event that carried it, not just the field."""
    assert log._clean(float("nan")) is None
    assert log._clean(float("inf")) is None


def test_a_huge_value_is_truncated_not_shipped(capsys):
    """One event must never become a log — the same rule the S3 ledger already follows."""
    log.event("doc.fail", **{"error.stack_trace": "x" * 50_000})
    v = _events(capsys)[0]["error.stack_trace"]
    assert len(v) < 9_000 and v.endswith(")")


def test_an_unserialisable_value_does_not_raise(capsys):
    """A diagnostic that can fail the run it is diagnosing is worse than no diagnostic."""
    log.event("doc.end", **{"aci.thing": object(), "aci.pages": 57})
    assert _events(capsys)[0]["aci.pages"] == 57


def test_logfmt_quotes_values_containing_spaces(monkeypatch, capsys):
    """The fallback format for a log agent that does not parse JSON. An unquoted
    `src=PDF bookmark outline` parses as three fields and the tool indexes rubbish."""
    monkeypatch.setattr(log, "_FORMAT", "logfmt")
    log.event("stage.end", **{"aci.src": "PDF bookmark outline", "aci.pages": 57})
    out = capsys.readouterr().out
    assert 'aci.src="PDF bookmark outline"' in out
    assert "aci.pages=57" in out


def test_off_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(log, "_FORMAT", "off")
    log.event("doc.start")
    assert capsys.readouterr().out == ""


def test_we_never_emit_a_field_filebeat_owns(monkeypatch, capsys):
    """The cluster's Filebeat merges our parsed JSON into the root of the document with
    overwrite_keys, so a key it also sets is a collision we WIN — and `host.name` is the node
    hostname, the one field that says which machine a log came from. Emitting our pod name
    there would destroy it silently. Ours live under aci.* instead."""
    monkeypatch.setenv("HOSTNAME", "corpus-extract-2-x7k4m")
    monkeypatch.setenv("NODE_NAME", "ip-10-0-37-214.eu-west-1.compute.internal")
    log.bind_service("aosphere-extract")
    log.event("run.start")
    rec = _events(capsys)[0]
    for owned in ("host.name", "host.hostname", "kubernetes.pod.name", "kubernetes.node.name",
                  "container.id", "agent.name", "ecs.version", "log.file.path", "input.type"):
        assert owned not in rec, f"{owned} is Filebeat's — emitting it overwrites the truth"
    assert rec["aci.pod"] == "corpus-extract-2-x7k4m"
    assert rec["aci.node"] == "ip-10-0-37-214.eu-west-1.compute.internal"


# ---------------------------------------------------------------- the heartbeat


def test_the_context_is_readable_from_another_thread():
    """The heartbeat's precondition. A new thread starts with an EMPTY contextvar context,
    so without the mirror every beat would report no run, no document and no stage — the
    four things it exists to say."""
    log.bind(**{"aci.label": "Canada__174237"})
    seen = {}

    t = threading.Thread(target=lambda: seen.update(log.snapshot()))
    t.start()
    t.join()
    assert seen.get("aci.label") == "Canada__174237"


def test_a_directory_that_stops_growing_raises_stalled_beats(tmp_path, capsys):
    """The stuck detector. Elapsed time cannot separate a 165-page document from a hang;
    output that has stopped changing can."""
    (tmp_path / "page-1.png").write_bytes(b"x" * 1000)
    hb = probe.Heartbeat(interval=0.05, watch=tmp_path, scratch=str(tmp_path))
    log.bind(**{"aci.stage": "stage2_mineru", "aci.stage_started_at": time.time() - 42})
    hb.start()
    time.sleep(0.3)
    hb.stop()
    time.sleep(0.1)
    beats = [e for e in _events(capsys) if e["event.action"] == "stage.heartbeat"]
    assert beats, "no heartbeat was emitted"
    assert beats[-1]["aci.stalled_beats"] >= 1
    assert beats[-1]["aci.out_bytes_delta"] == 0
    # It still says where it is stuck, which is the actionable half.
    assert beats[-1]["aci.stage"] == "stage2_mineru"
    assert beats[-1]["aci.stage_elapsed_s"] >= 42


def test_growth_resets_the_stall_counter(tmp_path):
    """A slow document must not accumulate a stall score — otherwise the alert fires on
    every large document and stops being read."""
    hb = probe.Heartbeat(interval=60, watch=tmp_path)
    (tmp_path / "a").write_bytes(b"x" * 100)
    hb._beat()
    hb._beat()
    assert hb._stalled == 1
    (tmp_path / "b").write_bytes(b"x" * 100)
    hb._beat()
    assert hb._stalled == 0


def test_a_failing_probe_does_not_kill_the_beat(tmp_path, capsys):
    """Every probe is optional. A heartbeat that dies on a missing /work is a heartbeat
    that goes quiet exactly when the disk is the problem."""
    hb = probe.Heartbeat(interval=60, watch=tmp_path / "does-not-exist",
                         scratch="/no/such/path")
    hb._beat()
    assert [e for e in _events(capsys) if e["event.action"] == "stage.heartbeat"]


# ---------------------------------------------------------------- run_one's wrapper


def _job(tmp_path):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    return {"label": "Canada__174237", "jurisdiction": "Canada", "doc_id": "174237",
            "pdf": pdf}


def test_a_document_that_ends_early_still_emits_doc_end(tmp_path, capsys):
    """The skip path returns before any stage runs. A start with no end is the signature of
    the hang this whole change exists to detect, so an uninstrumented return path would
    manufacture false alarms."""
    dest = tmp_path / "job"
    dest.mkdir()
    (dest / "scorecard.json").write_text(json.dumps({"gate": "pass", "worst_score": 0.9}))
    res = rc.run_one(_job(tmp_path), "124_Marketing", dest, False, False)
    assert res["status"] == "skipped"
    actions = [e["event.action"] for e in _events(capsys)]
    assert "doc.start" in actions and "doc.end" in actions


def test_a_crash_emits_doc_fail_with_the_full_traceback(tmp_path, capsys, monkeypatch):
    """400 characters is right for the S3 ledger, which is a bounded file. Kibana has no
    such constraint, and the MinerU stderr appended after the traceback — the one thing
    worth reading — is exactly what a truncation drops."""
    monkeypatch.setattr(rc, "_run_one", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("mineru exploded")))
    with pytest.raises(RuntimeError):
        rc.run_one(_job(tmp_path), "124_Marketing", tmp_path / "job", False, False)
    fail = [e for e in _events(capsys) if e["event.action"] == "doc.fail"][0]
    assert fail["log.level"] == "error"
    assert fail["error.type"] == "RuntimeError"
    assert "mineru exploded" in fail["error.stack_trace"]
    assert fail["aci.jurisdiction"] == "Canada"


def test_every_attempt_gets_its_own_doc_run_id(tmp_path, capsys):
    """Filtering by jurisdiction cannot separate a retry from the pass before it, and a
    stuck document is almost always one that has been attempted more than once."""
    dest = tmp_path / "job"
    dest.mkdir()
    (dest / "scorecard.json").write_text(json.dumps({"gate": "pass"}))
    job = _job(tmp_path)
    rc.bind_document(job, "124_Marketing", dest)
    rc.run_one(job, "124_Marketing", dest, False, False)
    rc.unbind_document()
    rc.bind_document(job, "124_Marketing", dest)
    rc.run_one(job, "124_Marketing", dest, False, False)
    rc.unbind_document()
    ids = {e.get("aci.doc_run_id") for e in _events(capsys)}
    assert len(ids - {None}) == 2


def test_the_stage_is_bound_before_the_callback_runs(tmp_path, capsys, monkeypatch):
    """The heartbeat reads the stage from the bound context, so the binding has to happen on
    the way IN. Bound afterwards, every beat during a stage would name the previous one —
    which on a 40-minute MinerU call is the whole signal, wrong."""
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "long.pdf"
    doc = fitz.open()
    for i in range(12):
        doc.new_page(width=595, height=842).insert_text((72, 96), f"Section {i + 1}",
                                                        fontsize=18)
    doc.save(str(pdf))
    doc.close()

    monkeypatch.setattr(rc.he, "run_stage1", lambda *a, **k: {"tables": []})
    monkeypatch.setattr(rc, "_preflight_outline", lambda *a, **k: None)

    # What the stage looked like to the callback, which is what the heartbeat sees too.
    seen: list[tuple] = []
    rc.run_one({"label": "long", "jurisdiction": "T", "doc_id": "long", "pdf": pdf},
               "TESTPROD", tmp_path / "out", True, True,
               progress_cb=lambda stage: seen.append((stage, log.snapshot().get("aci.stage"))))

    assert seen, "no stage transition was reported"
    for announced, bound_now in seen:
        assert bound_now == announced, (
            f"the callback for {announced!r} ran while the context still said "
            f"{bound_now!r} — a heartbeat during that stage would name the wrong one")
    # and the stage's own timer is bound alongside it, or elapsed time is uncomputable
    assert isinstance(log.snapshot().get("aci.stage_started_at"), float)
    actions = [e["event.action"] for e in _events(capsys)]
    assert actions.index("stage.enter") < actions.index("stage.end")


# ---------------------------------------------------------------- stages 4 and 5

def _stage4_report(**kw):
    rep = {"ran": True, "sections_accepted": 7, "sections_total": 8,
           "seconds": 512.4,
           "usage": {"input_tokens": 184_302, "output_tokens": 41_118,
                     "total_tokens": 225_420, "cost_usd": 0.4821},
           "subchunk": {"seconds": 18.6, "subchunks_total": 143, "sections_split": 6}}
    rep.update(kw)
    return rep


def _patch_stage4(monkeypatch, report=None, boom=None):
    import aosphere_core_index.extract.ai_postprocess as ap

    def _run(*a, **kw):
        if boom is not None:
            raise boom
        return report

    monkeypatch.setattr(ap, "run_stage4", _run, raising=False)
    monkeypatch.setattr(ap, "resolve_model", lambda m: "eu.anthropic.claude-sonnet-4-6",
                        raising=False)
    monkeypatch.setattr(ap, "resolve_text_model", lambda m: "eu.anthropic.claude-haiku-4-5",
                        raising=False)
    monkeypatch.setattr(rc, "_score_post_ai", lambda *a, **kw: None)


def test_stage4_reports_what_the_paid_pass_actually_did(tmp_path, capsys, monkeypatch):
    """`sections_accepted: 0 of 8` is the difference between an AI pass that worked and one
    that silently did nothing — and the gate cannot tell you, because it reads stages 1-3."""
    _patch_stage4(monkeypatch, report=_stage4_report())
    steps = {}
    cost = rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", steps, log=lambda m: None)
    assert cost == 0.4821
    ends = {e["aci.stage"]: e for e in _events(capsys) if e["event.action"] == "stage.end"}
    s4 = ends["stage4_ai"]
    assert s4["aci.sections_accepted"] == 7 and s4["aci.sections_total"] == 8
    assert s4["aci.sections_rejected"] == 1
    assert s4["aci.cost_usd"] == 0.4821 and s4["aci.tokens_total"] == 225_420
    assert s4["aci.model"] == "eu.anthropic.claude-sonnet-4-6"
    assert s4["event.duration_s"] == 512.4
    # stage 5 gets its own row, with the count that used to print as "None sub-chunks"
    assert ends["stage5_subchunk"]["aci.subchunks_total"] == 143


def test_a_pass_that_accepted_nothing_is_a_warning(tmp_path, capsys, monkeypatch):
    """This is the 2026-09-08 failure: gate=pass, 178s, $0.00, 0 of 8 sections processed."""
    _patch_stage4(monkeypatch, report=_stage4_report(sections_accepted=0))
    rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    s4 = [e for e in _events(capsys)
          if e["event.action"] == "stage.end" and e["aci.stage"] == "stage4_ai"][0]
    assert s4["log.level"] == "warn"


def test_enabled_but_did_not_run_is_distinguishable_from_not_enabled(tmp_path, capsys,
                                                                     monkeypatch):
    """Both look like a missing 04_stage4_ai from outside. Only one of them is a problem."""
    _patch_stage4(monkeypatch, report={"ran": False, "reason": "no tables to repair"})
    assert rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None) is None
    s4 = [e for e in _events(capsys) if e["event.action"] == "stage.end"][0]
    assert s4["event.outcome"] == "skipped"
    assert s4["aci.reason"] == "no tables to repair"


def test_a_swallowed_stage4_failure_is_still_reported(tmp_path, capsys, monkeypatch):
    """The except is correct — a paid pass that fails must leave a completed extraction
    completed — but it made a stage 4 outage produce `status: done, gate: pass` with no AI
    output and no error anywhere in the run."""
    _patch_stage4(monkeypatch, boom=RuntimeError("AccessDeniedException: bedrock:InvokeModel"))
    assert rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None) is None
    ev = [e for e in _events(capsys) if e["event.action"] == "stage.end"][0]
    assert ev["log.level"] == "error" and ev["event.outcome"] == "failure"
    assert ev["aci.swallowed"] is True
    assert "AccessDenied" in ev["error.message"]


def test_the_post_ai_scorecard_reports_what_the_ai_pass_cost_in_quality(tmp_path, capsys,
                                                                        monkeypatch):
    """Bahamas 183503: completeness 99.2 -> 94.6, fidelity 91.9 -> 45.8, and "no screen has
    ever said so". A per-dimension drop makes that a sortable column."""
    (tmp_path / "scorecard.json").write_text(json.dumps({"dimensions": {
        "completeness": {"score": 99.2}, "fidelity": {"score": 91.9},
        "toc": {"score": 88.0}}}))
    post = {"gate": "fail", "worst_score": 45.8, "weakest_dimension": "fidelity",
            "dimensions": {"completeness": {"score": 94.6}, "fidelity": {"score": 45.8},
                           "toc": {"score": 88.0}}}
    import lib_validate
    monkeypatch.setattr(lib_validate, "final_stage", lambda d: 5, raising=False)
    monkeypatch.setattr(lib_validate, "run_and_gate", lambda d, stage: ({}, post),
                        raising=False)
    rc._score_post_ai(tmp_path, {}, log=lambda m: None)
    ev = [e for e in _events(capsys) if e["event.action"] == "doc.scored_post_ai"][0]
    assert ev["log.level"] == "warn"
    assert ev["aci.gate_post_ai"] == "fail"
    assert ev["aci.worst_drop_dimension"] == "fidelity"
    assert ev["aci.worst_drop"] == 46.1
    assert ev["aci.dimensions_dropped"] == 2          # toc was unchanged
    assert ev["aci.dim.fidelity"] == 45.8 and ev["aci.drop.completeness"] == 4.6


def test_an_unchanged_document_reports_no_drop(tmp_path, capsys, monkeypatch):
    """The warn level has to mean something: a pass that harmed nothing must not raise it,
    or the filter stops being read."""
    (tmp_path / "scorecard.json").write_text(json.dumps(
        {"dimensions": {"completeness": {"score": 99.2}}}))
    post = {"gate": "pass", "worst_score": 99.2, "weakest_dimension": "completeness",
            "dimensions": {"completeness": {"score": 99.2}}}
    import lib_validate
    monkeypatch.setattr(lib_validate, "final_stage", lambda d: 5, raising=False)
    monkeypatch.setattr(lib_validate, "run_and_gate", lambda d, stage: ({}, post),
                        raising=False)
    rc._score_post_ai(tmp_path, {}, log=lambda m: None)
    ev = [e for e in _events(capsys) if e["event.action"] == "doc.scored_post_ai"][0]
    assert ev["log.level"] == "info"
    assert "aci.worst_drop" not in ev and "aci.dimensions_dropped" not in ev


def test_a_completed_document_reports_every_dimension(tmp_path, capsys, monkeypatch):
    """gate and worst_score say a document is weak but never in what way. This is the normal
    spine's final verdict — the path a healthy document actually takes — so it has to be
    reachable there and not only on the stage-4 route."""
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "doc.pdf"
    doc = fitz.open()
    for i in range(20):
        doc.new_page(width=595, height=842).insert_text((72, 96), f"Section {i + 1}",
                                                        fontsize=18)
    doc.save(str(pdf))
    doc.close()

    manifest = {"tables": [{"pages": [3, 4]}, {"pages": [7]}]}
    monkeypatch.setattr(rc.he, "run_stage1", lambda *a, **k: manifest)
    monkeypatch.setattr(rc.he, "run_stage2", lambda *a, **k: {})
    monkeypatch.setattr(rc.he, "run_stage3", lambda *a, **k: None)
    monkeypatch.setattr(rc, "_preflight_outline", lambda *a, **k: None)
    monkeypatch.setattr(rc, "run_chain", lambda dest, p, val, sc, **k: (val, sc))
    monkeypatch.setattr(rc, "_validate", lambda dest: {"passed": True})
    monkeypatch.setattr(rc, "compute_scorecard", lambda dest, val: {
        "gate": "review", "worst_score": 84.3, "weakest_dimension": "fidelity",
        "dimensions": {"completeness": {"score": 99.1}, "fidelity": {"score": 84.3},
                       "toc": {"score": 97.0}},
        "structure": {"chunks": 42}})

    rc.run_one({"label": "Peru__999", "jurisdiction": "Peru", "doc_id": "999", "pdf": pdf},
               "TESTPROD", tmp_path / "out", False, True)

    scored = [e for e in _events(capsys) if e["event.action"] == "doc.scored"]
    assert scored, "the normal spine never emitted doc.scored"
    ev = scored[0]
    assert ev["aci.gate_extraction"] == "review"
    assert ev["aci.weakest_dimension"] == "fidelity"
    assert ev["aci.dim.completeness"] == 99.1 and ev["aci.dim.fidelity"] == 84.3
    assert ev["aci.chunks"] == 42


# ---------------------------------------------------------------- per-section AI failures

def _section(file, ok, **kw):
    rec = {"file": file, "ok": ok, "kind": "table", "reason": "accepted",
           "stop_reason": "end_turn", "pages": 3, "model": "eu.anthropic.claude-sonnet-4-6",
           "tokens_in": 9100, "tokens_out": 2400, "cost_usd": 0.0412,
           "tables_before": 2, "tables_after": 2, "rows_before": 40, "rows_after": 40,
           "cell_chars_before": 8000, "cell_chars_after": 8000, "cell_chars_lost": 0}
    rec.update(kw)
    return rec


def test_a_rejected_section_is_named_and_the_reason_carried(tmp_path, capsys, monkeypatch):
    """"7/8 accepted" says a section failed but never which or why — and those are the only
    two facts anyone can act on. The file name maps straight to a heading in the document."""
    _patch_stage4(monkeypatch, report=_stage4_report(sections=[
        _section("06-financial-promotions.md", True),
        _section("07-marketing-selling-to-the-public.md", False,
                 reason="reprojection changed content: 12 cells differ",
                 tables_after=1, rows_after=28)]))
    rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    ev = [e for e in _events(capsys) if e["event.action"] == "section.rejected"]
    assert len(ev) == 1, "only the failed section should be reported"
    assert ev[0]["aci.section"] == "07-marketing-selling-to-the-public.md"
    assert "12 cells differ" in ev[0]["aci.section_reason"]
    assert ev[0]["aci.rows_before"] == 40 and ev[0]["aci.rows_after"] == 28
    assert ev[0]["log.level"] == "warn"


def test_nothing_ran_is_distinguishable_from_the_model_declining(tmp_path, capsys,
                                                                  monkeypatch):
    """A run with no AWS credentials failed all 8 sections in four seconds and reported
    "0/8 accepted, $0.0000" — indistinguishable from a model that read every page and
    declined to change anything. The reason is the whole difference."""
    _patch_stage4(monkeypatch, report=_stage4_report(sections_accepted=0, sections=[
        _section(f"0{i}-section.md", False,
                 reason="NoCredentialsError: Unable to locate credentials")
        for i in range(1, 4)]))
    rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    ev = [e for e in _events(capsys) if e["event.action"] == "section.rejected"]
    assert len(ev) == 3
    assert all("NoCredentialsError" in e["aci.section_reason"] for e in ev)


def test_an_accepted_section_that_lost_cell_content_is_flagged(tmp_path, capsys,
                                                               monkeypatch):
    """The dangerous kind. It was ACCEPTED, so it ships — the loss is visible only by
    comparing cell_chars before and after. This is Bahamas 183503's fidelity collapse
    located to the section that caused it."""
    _patch_stage4(monkeypatch, report=_stage4_report(sections=[
        _section("04-distribution.md", True, cell_chars_before=9000,
                 cell_chars_after=4200, cell_chars_lost=4800)]))
    rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    ev = [e for e in _events(capsys) if e["event.action"] == "section.lossy"]
    assert len(ev) == 1
    assert ev[0]["aci.section"] == "04-distribution.md"
    assert ev[0]["aci.cell_chars_lost"] == 4800
    assert ev[0]["aci.cell_chars_lost_pct"] == 53.3


def test_the_document_summary_counts_lossy_sections(tmp_path, capsys, monkeypatch):
    _patch_stage4(monkeypatch, report=_stage4_report(sections=[
        _section("a.md", True, cell_chars_lost=10),
        _section("b.md", True, cell_chars_lost=200),
        _section("c.md", True)]))
    rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    s4 = [e for e in _events(capsys)
          if e["event.action"] == "stage.end" and e.get("aci.stage") == "stage4_ai"][0]
    assert s4["aci.sections_lossy"] == 2


def test_per_section_cost_is_not_the_document_cost_field(tmp_path, capsys, monkeypatch):
    """`aci.cost_usd` carries the DOCUMENT total on stage.end. If a section event reused it,
    a Kibana sum over the field would double-count every run's spend."""
    _patch_stage4(monkeypatch, report=_stage4_report(sections=[
        _section("x.md", False, reason="nope", cost_usd=0.0412)]))
    rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    sec = [e for e in _events(capsys) if e["event.action"] == "section.rejected"][0]
    assert sec["aci.section_cost_usd"] == 0.0412
    assert "aci.cost_usd" not in sec


def test_a_pathological_document_cannot_flood_the_log(tmp_path, capsys, monkeypatch):
    """One AI pass must not become a hundred records — and a truncated list still has to say
    how much it dropped, or the count silently lies."""
    _patch_stage4(monkeypatch, report=_stage4_report(sections=[
        _section(f"{i:02d}-s.md", False, reason="nope") for i in range(60)]))
    rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    evs = _events(capsys)
    assert len([e for e in evs if e["event.action"] == "section.rejected"]) == 40
    trunc = [e for e in evs if e["event.action"] == "section.rejected.truncated"][0]
    assert trunc["aci.sections_omitted"] == 20


# ---------------------------------------------------------------- the /proc fallback

def _fake_proc(root: Path, pid: int, *, comm="python3", state="R", ppid=1,
               utime=100, stime=50, starttime=9000, rss_pages=2000, threads=4,
               cmdline=b"python3\x00-m\x00mineru.cli.client\x00"):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    # pid (comm) state ppid ... utime[14] stime[15] ... starttime[22]
    fields = ["0"] * 52
    fields[0], fields[1] = state, str(ppid)          # after comm: state, ppid
    fields[11], fields[12] = str(utime), str(stime)  # utime, stime
    fields[19] = str(starttime)                      # starttime
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    (d / "statm").write_text(f"{rss_pages * 3} {rss_pages} 0 0 0 0 0\n")
    (d / "status").write_text(f"Name:\t{comm}\nThreads:\t{threads}\n")
    (d / "cmdline").write_bytes(cmdline)
    (d / "fd").mkdir(exist_ok=True)
    for i in range(3):
        (d / "fd" / str(i)).write_text("")
    return d


def test_proc_fallback_reads_this_process(tmp_path, monkeypatch):
    """psutil is in uv.lock but NOT in the requirements.txt the worker image installs, so on
    the cluster these fields come from /proc or not at all. The field offsets are the easy
    thing to get wrong: comm can contain spaces and parentheses, so everything is indexed
    from the LAST ')'."""
    monkeypatch.setattr(probe, "_PROC", tmp_path)
    monkeypatch.setattr(probe, "_last_cpu", None)
    # a comm with a space AND a bracket in it — the case that breaks a naive split()
    _fake_proc(tmp_path, os.getpid(), comm="py (odd) name", utime=300, stime=100,
               rss_pages=5000, threads=9)
    (tmp_path / "self").symlink_to(tmp_path / str(os.getpid()))

    out = probe._proc_self()
    assert out["aci.threads"] == 9
    assert out["aci.rss_mb"] == round(5000 * os.sysconf("SC_PAGE_SIZE") / 1e6, 1)
    ticks = os.sysconf("SC_CLK_TCK")
    assert out["aci.cpu_s"] == round(400 / ticks, 1)
    assert "aci.cpu_pct" not in out, "a percentage needs two samples, not one"
    out2 = probe._proc_self()
    assert "aci.cpu_pct" in out2, "the second sample should yield a percentage"


def test_proc_fallback_finds_the_longest_running_descendant(tmp_path, monkeypatch):
    """MinerU spawns short-lived helpers; the one worth naming in a stall is the one that has
    been there the whole time. And `D` — uninterruptible sleep — is the status that says
    blocked on I/O rather than merely idle."""
    monkeypatch.setattr(probe, "_PROC", tmp_path)
    monkeypatch.setattr(probe, "psutil", None)   # the cluster's state, not this laptop's
    me = os.getpid()
    _fake_proc(tmp_path, me, ppid=1)
    _fake_proc(tmp_path, me + 1, comm="mineru", state="D", ppid=me, starttime=1000,
               rss_pages=500_000, cmdline=b"/opt/venv-mineru/bin/python\x00-m\x00mineru\x00")
    _fake_proc(tmp_path, me + 2, comm="helper", ppid=me + 1, starttime=8000)
    (tmp_path / "uptime").write_text("20000.0 1.0\n")

    out = probe.children()
    assert out["aci.child_count"] == 2, "descendants are counted recursively"
    assert out["aci.child_pid"] == me + 1, "the oldest child, not the newest"
    assert out["aci.child_status"] == "D"
    assert "mineru" in out["aci.child_cmd"]
    assert out["aci.child_rss_mb"] > 1000
    assert out["aci.child_age_s"] == round(20000.0 - 1000 / os.sysconf("SC_CLK_TCK"), 1)


def test_no_children_is_reported_as_zero_not_as_silence(tmp_path, monkeypatch):
    """`child_count: 0` during stage2_mineru means the stall is in our own code — a fact, and
    a different bug from a missing field."""
    monkeypatch.setattr(probe, "_PROC", tmp_path)
    monkeypatch.setattr(probe, "psutil", None)
    _fake_proc(tmp_path, os.getpid(), ppid=1)
    assert probe.children()["aci.child_count"] == 0


def test_a_machine_with_no_proc_at_all_still_beats(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe, "_PROC", tmp_path / "nothing-here")
    monkeypatch.setattr(probe, "psutil", None)
    probe.Heartbeat(interval=60, watch=tmp_path)._beat()
    assert [e for e in _events(capsys) if e["event.action"] == "stage.heartbeat"]


def test_a_malformed_section_record_cannot_fail_the_ai_pass(tmp_path, capsys, monkeypatch):
    """This reporting runs inside the try whose except reports stage 4 as FAILED. A
    diagnostic that invents the outage it exists to report is worse than no diagnostic."""
    _patch_stage4(monkeypatch, report=_stage4_report(sections=["not a dict", None, 42]))
    cost = rc._run_stage4_in_pipeline(tmp_path, tmp_path / "x.pdf", {}, log=lambda m: None)
    assert cost == 0.4821, "the pass succeeded and must still be reported as success"
    ends = [e for e in _events(capsys)
            if e["event.action"] == "stage.end" and e.get("aci.stage") == "stage4_ai"]
    assert ends[0]["event.outcome"] == "success"


# ---------------------------------------------------------------- the no-op shim

def test_the_fallback_shim_matches_the_real_module(monkeypatch):
    """run_corpus falls back to a no-op _log when the obs package will not import. An
    INCOMPLETE shim turns the situation it exists to survive into an AttributeError at the
    top of the extraction pipeline — a crash instead of the intended silence. So every name
    the shim offers is called here exactly as production code calls it.
    """
    src = (Path(__file__).resolve().parent.parent / "scripts" / "run_corpus.py").read_text()
    body = src[src.index("    class _log:"):src.index("    _probe = None")]
    ns: dict = {}
    exec("import contextlib\n" + "\n".join(l[4:] for l in body.splitlines()), ns)
    shim = ns["_log"]

    # Exactly the call shapes run_corpus and corpus_worker use.
    shim.bind_service("aosphere-extract", **{"aci.run_id": "r"})
    shim.bind(**{"aci.stage": "stage1"})
    shim.unbind("aci.stage", "aci.product")           # POSITIONAL — the shape that was wrong
    shim.event("doc.start", level="info", message="m", **{"aci.pages": 1})
    shim.exception("doc.fail", RuntimeError("x"), **{"aci.failed_stage": "stage1"})
    assert shim.flatten("aci.env", {"gpu": "T4"}) == {}
    assert shim.snapshot() == {} and shim.bound() == {}
    assert shim.enabled() is False
    shim.install_crash_handlers()
    with shim.context(**{"aci.stage": "s"}):
        pass

    # and nothing the real module offers under these names is missing from the shim
    for name in ("enabled", "bind", "bind_service", "unbind", "snapshot", "bound",
                 "event", "exception", "flatten", "context", "install_crash_handlers"):
        assert hasattr(shim, name), f"the shim is missing {name}"
        assert hasattr(log, name), f"{name} no longer exists on the real module"


# ---------------------------------------------------------------- bedrock retries

def _retry_hook():
    """The handler bedrock_client registers on botocore's `needs-retry`, pulled back out of
    a client built with a stubbed session."""
    import aosphere_core_index.extract.ai_postprocess as ap
    captured = {}

    class _Events:
        def register(self, name, fn):
            captured[name] = fn

    class _Meta:
        events = _Events()

    class _Client:
        meta = _Meta()

    class _Session:
        def client(self, *a, **kw):
            return _Client()

    import boto3
    orig = boto3.Session
    boto3.Session = lambda *a, **kw: _Session()
    try:
        ap.bedrock_client()
    finally:
        boto3.Session = orig
    return next(fn for name, fn in captured.items() if "needs-retry" in name)


class _Resp:
    def __init__(self, status):
        self.status_code = status


def test_a_throttle_carries_the_aws_error_code_not_just_a_status(capsys):
    """ThrottlingException arrives as a 429 with NO exception object. If error.type were
    only filled from a raised exception it would be empty on precisely the retries that
    matter most, and the field would mean "the error" on some records and nothing on
    others."""
    hook = _retry_hook()
    assert hook(response=(_Resp(429), {"Error": {"Code": "ThrottlingException",
                                                 "Message": "Too many requests"}}),
                attempts=2) is None, "the hook must stay an observer, never vote on retries"
    ev = [e for e in _events(capsys) if e["event.action"] == "bedrock.retry"][0]
    assert ev["error.type"] == "ThrottlingException"
    assert ev["error.message"] == "Too many requests"
    assert ev["aci.http_status"] == 429 and ev["aci.attempt"] == 2
    assert ev["log.level"] == "warn"


def test_a_raised_exception_still_wins_the_error_type(capsys):
    hook = _retry_hook()
    hook(response=None, attempts=3,
         caught_exception=ConnectionError("Read timeout on endpoint URL"))
    ev = [e for e in _events(capsys) if e["event.action"] == "bedrock.retry"][0]
    assert ev["error.type"] == "ConnectionError"
    assert "Read timeout" in ev["error.message"]


def test_the_successful_attempt_is_not_logged_as_a_retry(capsys):
    """Every call ends with a 200 through this hook. Logging those would bury the real
    retries under one record per successful Bedrock call in the run."""
    hook = _retry_hook()
    hook(response=(_Resp(200), {}), attempts=1)
    assert [e for e in _events(capsys) if e["event.action"] == "bedrock.retry"] == []


def test_a_broken_hook_never_breaks_the_call(capsys):
    """It runs inside botocore's emit, on the request path of every Bedrock call."""
    hook = _retry_hook()
    assert hook(response="not a tuple", attempts=None) is None
    assert hook() is None


def test_a_failed_attempt_carries_the_aws_request_id(capsys):
    """Without it, "Bedrock timed out on us" is an unprovable claim: an AWS support case or
    a CloudWatch model-invocation lookup starts from this id, and nothing else in the record
    identifies the individual request."""
    hook = _retry_hook()
    hook(response=(_Resp(429), {"Error": {"Code": "ThrottlingException", "Message": "slow down"},
                                "ResponseMetadata": {"RequestId": "9f1c-…-4b2a"}}),
         attempts=2)
    ev = [e for e in _events(capsys) if e["event.action"] == "bedrock.retry"][0]
    assert ev["aci.aws_request_id"] == "9f1c-…-4b2a"


# ------------------------------------------- stdout is shared by two kinds of writer


def test_a_plain_line_and_an_event_cannot_interleave_on_stdout(capsys):
    """print() is TWO unlocked writes -- the text, then the newline -- so an event emitted
    by another thread could land INSIDE one and glue its JSON onto the tail of the text
    line. Filebeat then fails to parse that line as JSON, `keys_under_root` never runs, and
    the event reaches Elasticsearch with no `event.action` on it: present in the pod log,
    present in the index, findable by no query that names the event, while its neighbours
    index perfectly. That is a single vanished line on a healthy pod, and it is why stage 4
    writes its human-readable lines through log.write_line rather than print."""
    import concurrent.futures

    def printer():
        for _ in range(40):
            log.write_line("  08-private-placement-regime.md: table section, 3 table(s)…")

    def emitter():
        for _ in range(40):
            log.event("bedrock.retry", level="warn", message="attempt 1: retry",
                      **{"aci.section": "08-private-placement-regime.md"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for f in [pool.submit(printer), pool.submit(emitter)]:
            f.result()

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 80, f"a line was lost or split: {len(out)}"
    for line in out:
        if line.startswith("{"):
            # Would raise if an unlocked write had spliced anything into it.
            assert json.loads(line)["event.action"] == "bedrock.retry"
        else:
            assert line.endswith("table(s)…"), f"text line was cut into: {line!r}"
            assert "{" not in line, f"an event was glued onto a text line: {line!r}"


def test_a_section_worker_event_still_carries_the_run(capsys):
    """A ThreadPoolExecutor worker starts at the contextvar's default -- an EMPTY context --
    so a bare submit left every stage-4 event with only the aci.section the worker binds for
    itself. aci.run_id, aci.stage and service.name were all absent, and a Kibana filter
    scoped to a run therefore matched none of that stage's bedrock events. copy_context() at
    the submit is what carries the run across the thread boundary."""
    import concurrent.futures
    import contextvars

    log.bind(**{"aci.run_id": "mram-2026-09-19", "aci.stage": "stage4_ai",
                "service.name": "aosphere-extract"})

    def worker():
        # What _repair_one_section binds: the section, and only the section.
        with log.context(**{"aci.section": "08-private-placement-regime.md"}):
            log.event("bedrock.retry", level="warn", **{"aci.attempt": 1})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(contextvars.copy_context().run, worker).result()

    ev = [e for e in _events(capsys) if e["event.action"] == "bedrock.retry"][0]
    assert ev["aci.run_id"] == "mram-2026-09-19", "the run did not cross the thread boundary"
    assert ev["aci.stage"] == "stage4_ai"
    assert ev["service.name"] == "aosphere-extract"
    assert ev["aci.section"] == "08-private-placement-regime.md", "worker context lost"


def test_stage4_submits_through_a_copied_context():
    """The property above is only true if the real submit site actually copies. Asserted on
    the source, because standing the whole stage up needs Bedrock."""
    import inspect

    import aosphere_core_index.extract.ai_postprocess as ap

    src = inspect.getsource(ap.run_stage4_section)
    assert "copy_context().run" in src, "bare submit: section workers lose the run context"
    assert ap.run_stage4_section.__defaults__ is not None
    assert print not in ap.run_stage4_section.__defaults__, \
        "log=print writes to stdout without the event lock"
