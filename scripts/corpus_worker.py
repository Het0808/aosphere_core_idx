#!/usr/bin/env python3
"""corpus_worker — extract a corpus of PDFs on a GPU worker: sources come from S3, results go
back to S3, and a worker that is killed mid-run costs one document.

This runs the SAME extraction the local CLI runs (run_corpus.run_one — Stage 1, the fallback
tiers, scoring). It deliberately stops there: no embedding, no vector push, no gallery publish.
Extraction and scoring are what a GPU worker is for; publishing is a separate step against a
finished corpus, and folding it in here would make every pod need Bedrock and index credentials
it has no other use for. The extraction path calls neither -- it needs S3 and a GPU, nothing else.

WHY THIS SHAPE

Sources are small: 1,088 PDFs total 1.23 GB, mean 1.1 MB. So a worker syncs the source prefix
down and run_corpus.discover() works unchanged on a local tree -- there is no reason to teach
discovery about S3. Results are the opposite: ~152 MB per document, ~76 GB for the corpus, which
is why they go up per-document rather than at the end.

SHARDING, WITHOUT A QUEUE

Each worker takes `--shard i/n` (or reads JOB_COMPLETION_INDEX, which a Kubernetes Job in
`completionMode: Indexed` sets for it). Assignment is longest-page-count-first onto the
least-loaded shard, so shards are balanced by PAGES rather than by document count -- documents
here run 8 to 165 pages, so splitting by count would leave one worker hours behind the others.
The assignment is a pure function of the work list, so two workers never pick the same document
and no lease, lock or queue is needed.

RESUME IS ALWAYS ON, AND THE OUTPUT PREFIX NAMES THE RUN

There is no --force and no re-extract mode. $CORPUS_RESULTS is the run's identity, and the rule
is the same every time: a document with a scorecard under that prefix is skipped, one without is
extracted. Both intentions fall out of it --

    point at a NEW prefix   -> nothing is there, so the whole corpus re-extracts
    point at the SAME one   -> an interrupted or evicted run continues where it stopped

A document is done when its scorecard.json exists: run_corpus writes it last, so that is already
its own skip rule and there is no second bookkeeping file to drift. Resume lists only the
scorecard KEYS back (a few MB), never the ~76 GB of trees. The local scratch directory follows
the prefix (/work/out/<prefix leaf>) so a restarted pod, which keeps its emptyDir, can never let
an older run's trees suppress work the current prefix needs.

INTERRUPTION

On a spot node the signal is SIGTERM, not the instance-action endpoint: EKS commonly blocks pod
access to IMDS, so the metadata probe is kept only for the plain-EC2 case.

SIGTERM stops the worker WHERE IT IS. The handler raises, which unwinds out of whatever call is
blocking -- a MinerU subprocess, a Bedrock socket read -- the document in flight is abandoned
unuploaded, its children are killed, the shard summary is marked `status: interrupted` naming
the document and the stage it died in, and the process exits 0.

Finishing the document first is what this used to do, and it does not survive stage 4: one
Bedrock call can sit in an adaptive retry for 45 minutes, so the flag was set and the worker
kept heartbeating for another 12 -- long past the termination grace period, at which point the
kubelet SIGKILLs it and the tidy exit the flag was protecting never happens anyway. Nothing is
lost by stopping mid-document: resume keys on the scorecard, so a document without one is
re-extracted whether it was abandoned politely or not. The exit is still 0, because an eviction
is not a crash -- Kubernetes sees a clean completion and a restarted pod resumes from S3.

    # local sources, results to S3
    scripts/corpus_worker.py --root corpus-src --results s3://BUCKET/corpus/2026-08-19 --plan

    # in a pod: everything from S3, shard from the Job index
    scripts/corpus_worker.py --source s3://BUCKET/corpus-src \\
        --results s3://BUCKET/corpus/2026-08-19 --shards 6 --exclude 155_Data_Privacy
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_corpus as R  # noqa: E402

# The SAME objects run_corpus resolved (the real obs modules, or its no-op shim when the
# package is not importable). Aliased rather than re-imported so the two files cannot end up
# in different states — one logging and one silent is worse than neither logging.
_log, _probe = R._log, R._probe

IMDS_TOKEN = "http://169.254.169.254/latest/api/token"
IMDS_ACTION = "http://169.254.169.254/latest/meta-data/spot/instance-action"

_STOP = False


class Interrupted(BaseException):
    """A signal reached the worker while a document was in flight.

    Deliberately a BaseException. This pipeline catches `Exception` generously and on purpose —
    stage 4 keeps going when one section fails, every marker write is guarded, run_one's own
    wrapper logs and re-raises — and a stop that any one of those swallowed would go straight
    back to sleep inside the call it was meant to end. Nothing here catches BaseException
    except to log and re-raise."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum}")
        self.signum = int(signum)
        # Set by the handler, which is where the children are actually killed — recounting in
        # the except block would report 0 and read as "MinerU was left running".
        self.children_killed = 0


def _kill_children() -> int:
    """Kill whatever this worker started — MinerU above all. Returns how many were signalled.

    Kubernetes signals the container's PID 1, not its descendants, so a MinerU subprocess 12
    minutes into a page pass never hears about the eviction: it holds the GPU for the whole
    termination grace period while the parent is trying to leave. psutil is a transitive
    dependency here rather than a declared one (obs/probe.py imports it the same guarded way),
    so this is best effort by construction — without it the children die with the container
    instead of just before it."""
    try:
        import psutil                                        # type: ignore
    except Exception:                                        # noqa: BLE001
        return 0
    killed = 0
    try:
        for child in psutil.Process().children(recursive=True):
            try:
                child.kill()
                killed += 1
            except Exception:                                # noqa: BLE001
                pass
    except Exception:                                        # noqa: BLE001
        pass
    return killed


# How long the tidy exit gets before the watchdog stops waiting for it. Comfortably inside
# Kubernetes' 30s default grace period, and long enough for the two `aws s3 cp` calls the
# bookkeeping makes.
GRACE_S = float(os.environ.get("ACI_INTERRUPT_GRACE_S", "15"))

# The shard summary in flight, so the SIGNAL HANDLER can write the interruption itself rather
# than relying on the stack unwinding far enough to reach main()'s except clause. See
# _arm_watchdog for why that reliance was not safe.
_PROG: "ShardProgress | None" = None

# The document in flight, for _disown_current_document below. Set by the loop before the
# extraction starts and cleared once the document is finished and uploaded.
_DOC: dict | None = None


def _disown_current_document() -> list[str]:
    """Make sure the interrupted document is REDONE, not quietly treated as finished.

    Resume keys on the scorecard, in two places — a local one skips a document the pod already
    extracted, a remote one skips a document already in S3 — and stopping mid-document opens a
    window where either can lie:

        interrupted after run_one wrote the scorecard, before the upload finished
            The local scorecard is there and S3 has nothing. A restarted pod (restartPolicy
            OnFailure keeps the emptyDir) SKIPS the document, so it is never uploaded and never
            extracted again: silently missing from the corpus.

        interrupted during the upload itself
            `aws s3 sync` uploads in whatever order it likes, and scorecard.json is small.
            If it lands and the 152 MB of tree behind it does not, done_labels() reads the
            document as finished and the corpus keeps a truncated one.

    Neither existed while a signal only took effect BETWEEN documents — the upload always ran to
    completion. They exist now, so both markers are removed on the way out: one unlink and, only
    if the upload was actually in flight, one S3 delete. The document is then simply outstanding,
    which is the one state resume handles correctly."""
    doc, undone = _DOC, []
    if not doc:
        return undone
    try:
        sc = doc["dest"] / "scorecard.json"
        if sc.exists():
            sc.unlink()
            undone.append("local scorecard")
    except OSError as e:
        print(f"  ⚠ could not drop the local scorecard: {e} — this document may be SKIPPED "
              f"instead of redone", flush=True)
    # Only while the upload was in flight. Once push_job has returned, S3 either holds the whole
    # document or nothing addressed by this key, and deleting a good scorecard would throw away
    # a finished document and buy back its GPU time for nothing.
    if doc.get("uploading"):
        key = f"{doc['results'].rstrip('/')}/{doc['product']}/{doc['label']}/scorecard.json"
        if _s3_write("drop the half-uploaded scorecard", "rm", key, "--only-show-errors",
                     profile=doc.get("profile")):
            undone.append("remote scorecard")
    return undone


def _arm_watchdog(signum: int) -> None:
    """Guarantee the process actually leaves, however the unwind goes.

    Raising out of the handler is not enough on its own, and stage 4 is the proof. Its sections
    run in a ThreadPoolExecutor, and `with ThreadPoolExecutor(...)` calls shutdown(WAIT=True) on
    the way out — so the exception escapes the Bedrock call in the main thread and then parks in
    __exit__ until the worker thread's own call finishes, which is the 45 minutes we were trying
    not to wait for. Measured: the raise lands in 1s and the process is still alive 25s later
    with nothing left to do but wait.

    A daemon timer sidesteps all of it. Whatever the stack is stuck in — a pool shutdown, an
    atexit hook, a botocore connection pool — the process is gone within the grace period, and
    the bookkeeping has already happened in the handler before this was armed."""
    def _give_up():
        _log.event("run.interrupt_timeout", level="warn",
                   message=f"still here {GRACE_S:.0f}s after signal {signum} — exiting hard",
                   **{"aci.signal": int(signum), "aci.grace_s": GRACE_S})
        print(f"  still here {GRACE_S:.0f}s after signal {signum} — exiting now", flush=True)
        os._exit(0)

    t = threading.Timer(GRACE_S, _give_up)
    t.daemon = True
    t.start()


def _on_signal(signum, _frame):
    """SIGTERM/SIGINT: stop NOW, in the middle of the document if that is where we are.

    This used to set a flag and let the document in flight run to completion, on the reasoning
    that killing MinerU mid-page leaves a half-written tree the next run would redo anyway. That
    reasoning held right up until stage 4 started calling Bedrock: one call can sit in an
    adaptive retry for 45 minutes, so on the MRAM run the flag was set at 08:35 and the worker
    was still heartbeating stage4_ai 12 minutes later, long past any termination grace period —
    at which point the kubelet SIGKILLs it and the tidy exit the flag was protecting never
    happens. Nothing is saved by finishing: resume keys on the scorecard, and a document
    without one is re-extracted whether we abandoned it politely or not.

    So the handler RAISES, which unwinds out of whatever call is blocking (a socket read inside
    botocore, a subprocess wait) into main()'s handler, where the shard is marked interrupted
    and the process exits. A second signal means the first one did not get us out — usually
    because the raise landed somewhere that is swallowing it — so that one goes immediately
    and without bookkeeping."""
    global _STOP
    again, _STOP = _STOP, True
    _log.event("run.signal", level="warn",
               message=(f"signal {signum} — "
                        + ("second signal, exiting now" if again
                           else "interrupting the document in flight")),
               **{"aci.signal": int(signum), "aci.second_signal": again})
    if again:
        print(f"  signal {signum} again — exiting now", flush=True)
        # os._exit, not sys.exit: sys.exit raises, and we are here because a raise did not
        # get us out the first time.
        os._exit(128 + int(signum))
    print(f"  signal {signum} received — interrupting the document in flight", flush=True)
    # BEFORE the raise: a `subprocess.run` blocked on a MinerU child returns as soon as the
    # child is gone, so killing first is what makes the unwind immediate rather than merely
    # scheduled behind a 20-minute page pass.
    stop = Interrupted(signum)
    stop.children_killed = _kill_children()
    _arm_watchdog(signum)
    # The RECORD is written here, not in main()'s except clause, because the raise below is not
    # guaranteed to get there — stage 4's thread pool can swallow the unwind into a blocking
    # shutdown (see _arm_watchdog). Here it is certain: the handler runs in the main thread the
    # moment the signal is delivered, whatever the stack is doing. main() still calls this
    # again on the way out and it is idempotent, so the tidy path is unchanged.
    if _PROG is not None:
        try:
            _PROG.interrupted(signum, killed=stop.children_killed)
        except Exception as e:                               # noqa: BLE001
            print(f"  ⚠ could not record the interruption: {type(e).__name__}: {e}", flush=True)
    try:
        undone = _disown_current_document()
        if undone:
            print(f"  dropped the {' and the '.join(undone)} so this document is redone, "
                  f"not skipped", flush=True)
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠ could not disown the document in flight: {type(e).__name__}: {e}", flush=True)
    raise stop


def spot_reclaim_pending(timeout: float = 0.3) -> bool:
    """Plain-EC2 spot notice. Always False in a pod that cannot reach IMDS — SIGTERM covers that."""
    try:
        req = urllib.request.Request(IMDS_TOKEN, method="PUT",
                                    headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = urllib.request.urlopen(req, timeout=timeout).read().decode()
        req = urllib.request.Request(IMDS_ACTION, headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(req, timeout=timeout).status == 200
    except Exception:
        return False


def classify(err: str) -> tuple[str, bool]:
    """(cause, permanent) for a failure. Shared with the service so the screen and the worker
    cannot disagree about what a failure was."""
    try:
        from aosphere_core_index.service.extraction_monitor import classify_failure, is_permanent
        cause = classify_failure(err)
        return cause, is_permanent(cause)
    except Exception as e:                                   # noqa: BLE001
        # "unknown, retryable" is the safe answer -- but it is also indistinguishable from a
        # genuine unknown cause, and it means a deterministically-failing document is
        # re-attempted on every future run of the prefix. If the classifier itself is broken,
        # that is the fact worth having.
        _log.event("classify.unavailable", level="warn",
                   message=f"failure classifier unavailable ({type(e).__name__}) — treating "
                           f"this failure as retryable",
                   **{"error.type": type(e).__name__, "error.message": str(e)})
        return "unknown", False


def mark_permanent(results: str, product: str, label: str, cause: str, err: str,
                   out_root: Path, profile: str | None) -> None:
    """Record that this document CANNOT be extracted as things stand, so resume stops re-attempting it.

    A crash writes no scorecard, and resume keys on the scorecard — so without this a document
    that fails deterministically is re-attempted on every future run of the prefix. The pipeline
    has already said why ("No structure found … a heading-DETECTION gap"), and it will say exactly
    the same thing next time. This is not a verdict on the document: it is a note that the code
    needs to change before the document can be processed."""
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{product}__{label}")
    try:
        f = out_root / "_progress" / f"permanent-{safe}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"product": product, "label": label, "cause": cause,
                                 "at": time.time(), "error": err[:2000]}, indent=2))
        if not _s3_write(f"record _permanent/{safe}", "cp", str(f),
                         f"{results.rstrip('/')}/_permanent/{safe}.json",
                         "--only-show-errors", profile=profile):
            # Without the marker this deterministic failure is re-attempted on every future
            # run — the exact waste this function exists to stop.
            print(f"  ⚠ {product}/{label} will be re-attempted on every future run: its "
                  f"_permanent marker ({cause}) could not be uploaded", flush=True)
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠ could not record _permanent/{safe} ({cause}): "
              f"{type(e).__name__}: {e}", flush=True)


def permanent_labels(results: str, profile: str | None) -> set[str]:
    """Documents already known to fail deterministically — skipped, like finished ones."""
    bucket, prefix = _split_uri(results)
    return _read_markers(bucket, prefix, "_permanent", profile)

def retry_labels(results: str, profile: str | None) -> set[str]:
    """Documents a reviewer has marked for re-extraction, as "<product>/<label>".

    Resume is "a scorecard means done", which is what makes an interrupted run cheap and what
    makes a BAD result sticky — re-running the prefix skips exactly the documents someone wants
    redone. A marker under <run>/_retry/ overrides that for one document, and is cleared once it
    has been re-extracted, so it cannot cause an endless loop across runs."""
    bucket, prefix = _split_uri(results)
    return _read_markers(bucket, prefix, "_retry", profile)

def clear_retry(results: str, product: str, label: str, out_root: Path,
                profile: str | None) -> None:
    """Drop the marker, and record that this document HAS been retried.

    The record is what bounds it to one retry: a document that fails for a reason a retry does not
    change would otherwise be redone on every run of the prefix, burning GPU on the same document
    forever. A second retry then has to be asked for deliberately."""
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{product}__{label}")
    base = results.rstrip("/")
    try:
        note = out_root / "_progress" / f"retried-{safe}.json"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(json.dumps({"product": product, "label": label,
                                    "retried_at": time.time()}, indent=2))
        recorded = _s3_write(f"record _retried/{safe}", "cp", str(note),
                            f"{base}/_retried/{safe}.json", "--only-show-errors", profile=profile)
    except Exception as e:                                   # noqa: BLE001
        recorded = False
        print(f"  ⚠ could not stage the _retried record for {safe}: "
              f"{type(e).__name__}: {e}", flush=True)
    dropped = _s3_write(f"drop _retry/{safe}", "rm", f"{base}/_retry/{safe}.json",
                        "--only-show-errors", profile=profile)
    if not dropped:
        # The marker outliving its retry is the unbounded-loop case: resume will hand this
        # document back on every future run of the prefix. Say so loudly — it costs a GPU-hour
        # per run per document and nothing else reports it.
        print(f"  ⚠ {product}/{label} WILL BE RETRIED AGAIN on the next run: its _retry marker "
              f"could not be removed", flush=True)
    elif not recorded:
        # Marker gone but no _retried record: the one-retry cap has lost its memory, so a future
        # `force` check will let this document through a second time without anyone asking.
        print(f"  ⚠ {product}/{label} retried, but the _retried record was not written — the "
              f"one-retry cap will not see it", flush=True)


def resolve_run(results: str, out: str | None) -> tuple[str, Path]:
    """-> (run id, local scratch dir) for a results prefix.

    The scratch dir defaults to /work/out/<run id> rather than a fixed path: a pod restarted by
    restartPolicy: OnFailure keeps its emptyDir, so a fixed directory would let a previous run's
    trees satisfy the local skip and suppress work the current prefix has never done."""
    run_id = results.rstrip("/").rsplit("/", 1)[-1] or "run"
    return run_id, (Path(out).resolve() if out else Path("/work/out") / run_id)


def _split_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://"):] if uri.startswith("s3://") else uri
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix.strip("/")


def _s3(*args: str, profile: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["aws", "s3", *args] + (["--profile", profile] if profile else [])
    return subprocess.run(cmd, capture_output=True, text=True)


def pull_sources(source: str, root: Path, profile: str | None) -> None:
    """Sync the source PDFs down. 1.23 GB for the whole corpus, so this is not worth
    shard-filtering — a worker syncing only its own PDFs would need the page counts it does not
    have yet to decide which those are.

    Also pulls down each folder's Doc_metadata.json sidecar (still excluding everything
    else, e.g. RAG_rated_answers.json). product_rules.content_tile_name() reads that file
    from beside the PDF to route 124_Marketing_Restrictions_-_Asset_Management's summary
    documents (see AI_PIPELINE) — dropped from this sync, the tile is unreadable on the
    worker and every one of those documents silently falls through to the survey route.
    Measured against the real corpus-src bucket on 2026-09-10: the exclude-all/include-pdf
    filter downloaded 194 PDFs and 0 of the 89 Doc_metadata.json files that exist there.

    The pattern is `*Doc_metadata.json`, not the bare filename — `aws s3 sync` matches
    include/exclude patterns against the full key relative to `source`, so a jurisdiction
    subfolder ("Australia/Doc_metadata.json") needs the leading wildcard to match at all;
    verified with --dryrun against the live bucket, bare `Doc_metadata.json` matched 0 of
    89 while `*Doc_metadata.json` matched all 89."""
    root.mkdir(parents=True, exist_ok=True)
    print(f"  syncing sources {source} -> {root}", flush=True)
    t0 = time.time()
    r = _s3("sync", source, str(root), "--exclude", "*", "--include", "*.pdf",
            "--include", "*Doc_metadata.json", "--only-show-errors", profile=profile)
    if r.returncode != 0:
        sys.exit(f"  source sync failed: {r.stderr.strip()[:300]}")
    n = sum(1 for _ in root.rglob("*.pdf"))
    print(f"  {n} PDF(s) in {time.time() - t0:.0f}s", flush=True)


def _s3_write(what: str, *args: str, profile: str | None = None) -> bool:
    """One S3 mutation, with the failure REPORTED. Returns whether it succeeded.

    Every marker write used to discard its return code — `_s3("cp", ...)` with the result
    unassigned, inside a `try/except Exception: pass`. A 403 was therefore indistinguishable
    from success, which is how a denied `GetObject` on corpus/*/_retry/* went unnoticed for a
    day: the reads were silent too, so the whole retry mechanism looked like "nothing queued".

    The DELETE is the one that matters most. clear_retry drops a document's _retry/ marker once
    it has been re-extracted, and writes _retried/ to bound it to one attempt. If either write
    fails silently the marker survives, and that document is re-extracted on EVERY subsequent
    run of the prefix — the unbounded loop the one-retry cap exists to prevent, except now
    invisible."""
    r = _s3(*args, profile=profile)
    if r.returncode != 0:
        err = (r.stderr or "").strip().replace("\n", " ")[:300] or f"exit {r.returncode}"
        print(f"  ⚠ {what} FAILED: {err}", flush=True)
        return False
    return True


def _read_markers(bucket: str, prefix: str, kind: str, profile: str | None) -> set[str]:
    """Read every marker under <prefix>/<kind>/ as "<product>/<label>".

    Every failure here used to be swallowed — one `return set()` and three bare `continue`s —
    so a permissions problem, a throttle or a malformed marker was indistinguishable from "no
    markers queued". The worker then printed nothing at all (both call sites are guarded on the
    set being non-empty) and reported the run complete with 0 jobs, while 132 retry markers sat
    in S3 untouched. That cost a day of restarts to find, because the one thing the log never
    said was that it had failed to look.

    Note the TWO S3 calls: `ls` to enumerate and `cp` to read each marker's JSON. They need
    different IAM actions (ListBucket vs GetObject), so `ls` succeeding proves nothing about
    `cp` — which is exactly the shape this bug took: the listing worked from inside the
    container while the reads returned nothing.
    """
    base = f"s3://{bucket}/{prefix}/{kind}/"
    r = _s3("ls", base, profile=profile)
    if r.returncode != 0:
        err = (r.stderr or "").strip().replace("\n", " ")[:300]
        # NoSuchKey/empty prefix is normal and quiet; anything else is reported.
        if err:
            print(f"  ⚠ could not LIST {kind}: aws exited {r.returncode}: {err}", flush=True)
        return set()
    names = []
    for line in r.stdout.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) < 4 or not fields[3].endswith(".json"):
            continue
        names.append(fields[3])
    out: set[str] = set()
    read_failed, parse_failed, first_err = 0, 0, ""
    for name in names:
        got = _s3("cp", f"{base}{name}", "-", profile=profile)
        if got.returncode != 0:
            read_failed += 1
            first_err = first_err or (got.stderr or "").strip().replace("\n", " ")[:300]
            continue
        try:
            d = json.loads(got.stdout)
            out.add(f"{d['product']}/{d['label']}")
        except Exception as e:                               # noqa: BLE001
            parse_failed += 1
            first_err = first_err or f"{type(e).__name__}: {e}"
    if read_failed or parse_failed:
        print(f"  ⚠ {kind}: {len(names)} marker(s) listed, {len(out)} usable — "
              f"{read_failed} unreadable, {parse_failed} unparseable. First error: {first_err}",
              flush=True)
    elif names:
        print(f"  {kind}: {len(out)} marker(s) read", flush=True)
    return out


def done_labels(results: str, profile: str | None) -> set[str]:
    """Finished jobs, read back as <product>/<label> from the scorecard keys in S3."""
    bucket, prefix = _split_uri(results)
    r = _s3("ls", f"s3://{bucket}/{prefix}/", "--recursive", profile=profile)
    if r.returncode != 0:
        # A failed listing here reads as "nothing is done", which would re-extract the whole
        # corpus. Say so rather than quietly starting 1,088 documents again.
        print(f"  ⚠ could not LIST the run prefix: aws exited {r.returncode}: "
              f"{(r.stderr or '').strip()[:300]}", flush=True)
        return set()
    out = set()
    for line in r.stdout.splitlines():
        # `aws s3 ls --recursive` prints "<date> <time> <size> <key>" and THE KEY CONTAINS SPACES
        # in most of this corpus -- "Abu Dhabi Global Market__183256", "Cayman Islands",
        # "United States - California (Short Form)". Splitting on all whitespace and taking the
        # last field truncates those keys to their final path segment, so the document is never
        # recognised as finished and gets re-extracted on every restart. maxsplit=3 keeps the key
        # whole, spaces and all.
        fields = line.split(maxsplit=3)
        if len(fields) < 4:
            continue
        key = fields[3]
        if not key.endswith("/scorecard.json"):
            continue
        rel = key[len(prefix):].strip("/").split("/")
        # EXACTLY <product>/<label>/scorecard.json. The fallback tiers each leave their own
        # scorecard behind in a nested attempt dir (mineru_full_attempt/, hybrid_attempt/), and
        # matching those would mark a job finished on the strength of an attempt — so a document
        # interrupted midway through its fallback chain, whose FINAL scorecard was never written,
        # would be skipped for good and silently missing from the corpus.
        if len(rel) == 3:
            out.add(f"{rel[0]}/{rel[1]}")
    return out


def push_job(job_dir: Path, results: str, product: str, profile: str | None) -> bool:
    """Upload ONE finished job. Per-document, so an interruption costs one document.

    --delete makes this a MIRROR, not an add-only upload. job_dir is freshly extracted
    top to bottom on every run (a fresh pod pulls the source PDF into an empty scratch
    dir; nothing here is reused across runs), so it is always the complete, correct
    state for this document as of right now -- anything at dest that isn't in job_dir
    is this job's OWN output from a previous run, not another job's.

    That distinction matters because a retry commonly changes the file layout: stage 4
    once wrote this document as N sections and now writes it as N-1 (or with different
    numbering), and a plain sync only ever adds or overwrites -- it does not know a file
    is gone. Without --delete, S3 kept both generations side by side forever: the old
    "06-marketing-selling-to-the-public.md" sat next to the retry's own
    "07-marketing-selling-to-the-public.md", and the dashboard's FILES panel rendered
    both, alongside two top-level folders sharing the number "01" from two runs that
    numbered this document differently."""
    dest = f"{results.rstrip('/')}/{product}/{job_dir.name}/"
    r = _s3("sync", str(job_dir), dest, "--delete", "--only-show-errors", profile=profile)
    if r.returncode != 0:
        print(f"    ✗ upload failed: {r.stderr.strip()[:200]}", flush=True)
    return r.returncode == 0


def env_fingerprint(profile_unused=None) -> dict:
    """What this worker is actually running on, recorded so a failure can be diagnosed WITHOUT
    cluster access.

    Every question asked while debugging the first cluster run — is the GPU being used, which card,
    how much VRAM, did MinerU find its models, is /dev/shm the 64MB default — is answerable here
    and is invisible from S3 otherwise. Kibana held the answers and we could not reach it, so the
    run told us nothing that the output files did not.

    Best effort by construction: every probe is individually guarded, because a diagnostic that
    can fail the run it is diagnosing is worse than no diagnostic."""
    env: dict = {}
    for k in ("MINERU_MODEL_SOURCE", "MINERU_TOOLS_CONFIG_JSON", "HF_HOME", "HF_HUB_OFFLINE",
              # whether the allocator fix actually reached the pod: a setting that is documented
              # but absent at runtime is the same as not having it.
              "PYTORCH_CUDA_ALLOC_CONF",
              "ACI_MINERU_BACKEND", "ACI_MINERU_EFFORT", "HOME", "CORPUS_SHARDS",
              "JOB_COMPLETION_INDEX", "NVIDIA_VISIBLE_DEVICES"):
        if os.environ.get(k) is not None:
            env[k] = os.environ[k]

    # torch lives in MinerU's own environment, not the pipeline's.
    mineru_py = Path(os.environ.get("MINERU_VENV", "/opt/venv-mineru")) / "bin" / "python"
    if not mineru_py.exists():
        mineru_py = Path(sys.executable)
    probe = (
        "import json,os\n"
        "out={}\n"
        "try:\n"
        "    import torch\n"
        "    out['torch']=torch.__version__\n"
        "    out['cuda_available']=torch.cuda.is_available()\n"
        "    if torch.cuda.is_available():\n"
        "        out['gpu']=torch.cuda.get_device_name(0)\n"
        "        free,total=torch.cuda.mem_get_info()\n"
        "        out['gpu_total_gb']=round(total/1e9,1); out['gpu_free_gb']=round(free/1e9,1)\n"
        "except Exception as e: out['torch_error']=f'{type(e).__name__}: {e}'\n"
        "try:\n"
        "    import triton\n"
        "    out['triton']=triton.__version__\n"
        "except Exception as e: out['triton_error']=f'{type(e).__name__}: {e}'\n"
        "try:\n"
        "    import mineru\n"
        "    out['mineru']=getattr(mineru,'__version__','?')\n"
        "    from mineru.utils.config_reader import get_local_models_dir, get_configured_model_source\n"
        "    out['models_dir']=get_local_models_dir(); out['model_source_cfg']=get_configured_model_source()\n"
        "except Exception as e: out['mineru_error']=f'{type(e).__name__}: {e}'\n"
        "print(json.dumps(out))\n")
    try:
        r = subprocess.run([str(mineru_py), "-c", probe], capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and r.stdout.strip():
            env.update(json.loads(r.stdout.strip().splitlines()[-1]))
        else:
            env["probe_error"] = (r.stderr or r.stdout)[-300:]
    except Exception as e:                                   # noqa: BLE001
        env["probe_error"] = f"{type(e).__name__}: {e}"

    # /dev/shm at the 64MB container default is a known cause of "DataLoader worker killed
    # (Bus error)", and it is set by the manifest — so record whether the manifest was applied.
    try:
        st = os.statvfs("/dev/shm")
        env["dev_shm_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 2)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        st = os.statvfs("/work") if Path("/work").exists() else os.statvfs(".")
        env["scratch_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
    except Exception:                                        # noqa: BLE001
        pass
    env["cpu_count"] = os.cpu_count()
    # Triton JIT-compiles a C file for every CUDA kernel it builds, so no compiler means MinerU's
    # GPU path dies with "Failed to find C compiler" — which is invisible on Apple/MPS, where
    # Triton is never invoked. This is the check whose absence cost a whole cluster run.
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    env["cc"] = cc
    if cc:
        try:
            env["cc_version"] = subprocess.run([cc, "--version"], capture_output=True, text=True,
                                               timeout=30).stdout.splitlines()[0][:120]
        except Exception:                                    # noqa: BLE001
            pass
    try:
        env["nvidia_smi"] = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=30).stdout.strip()[:200]
    except Exception:                                        # noqa: BLE001
        pass
    return env


class ShardProgress:
    """One small JSON per shard at <results>/_progress/shard-<i>.json, rewritten after every
    document.

    Deliberately a SUMMARY, not a log of everything. The monitoring screen has to answer "how far
    along is the run" without reading 891 scorecards — we already learned that lesson loading
    scorecards in the frontend, and this corpus only grows. Eight bounded ~2KB objects give the
    screen a percentage, an ETA, the gate mix and what each worker is doing right now; `recent` is
    capped so the object cannot grow without limit over a 17-hour run.

    Beside it, ONE line per finished document goes to <results>/_progress/ledger-<i>.jsonl. That
    is the run's job list — the thing `recent` deliberately is not, because a capped tail cannot
    answer "which document was slow" four hours after it finished. It is bounded by the corpus
    rather than by time (~400 bytes x 891 documents ~= 350KB across every shard), holds only what
    run_one already computed, and is still never a scorecard read: the screen gets the whole run's
    timings for one GET per shard.

    What each row carries is chosen for ONE question — where did this document spend its time, and
    which pass produced the tree that was kept. `steps` is the per-stage breakdown, `tier` is the
    fallback tier finally adopted, and on a crash `stage` is the step it died in, which is the
    single most useful fact about a failure and was previously only in a pod log."""

    RECENT = 10

    def __init__(self, run: str, shard: int, shards: int, jobs: int, pages: int,
                 out_root: Path, results: str, profile: str | None,
                 corpus_docs: int = 0, corpus_pages: int = 0,
                 remaining_docs: int = 0, remaining_pages: int = 0) -> None:
        self.d: dict = {"run": run, "shard": shard, "shards": shards,
                        "jobs_total": jobs, "pages_total": pages,
                        # The version, not this attempt: what the run covers in total and what was
                        # still outstanding when this worker started.
                        "corpus_docs": corpus_docs, "corpus_pages": corpus_pages,
                        "remaining_docs": remaining_docs, "remaining_pages": remaining_pages,
                        "done": 0, "extracted": 0, "cloned": 0, "failed": 0,
                        "pages_done": 0, "gates": {}, "current": None, "recent": [],
                        "started_at": time.time(), "updated_at": time.time(),
                        # "running" until something says otherwise. interrupted() sets
                        # "interrupted"; a shard that finishes its partition is recognised by
                        # done >= jobs_total and needs no flag to say so.
                        "stopping": False, "status": "running"}
        self._local = out_root / "_progress" / f"shard-{shard}.json"
        self._local.parent.mkdir(parents=True, exist_ok=True)
        self._dest = f"{results.rstrip('/')}/_progress/shard-{shard}.json"
        self._results = results.rstrip("/")
        self._profile = profile
        self._ledger = out_root / "_progress" / f"ledger-{shard}.jsonl"
        self._ledger_dest = f"{results.rstrip('/')}/_progress/ledger-{shard}.jsonl"
        # The stage the document in flight last reported. A crash is announced by an exception,
        # which knows the traceback but not the pipeline stage, so this is what lets a failed row
        # say "died in stage2_mineru" instead of only naming the exception type.
        self._stage: str | None = None
        # Whether the last publish reached S3. Starts True so the first failure is a
        # transition and therefore gets logged; see publish().
        self._publishing_ok = True
        # Why the document in flight escalated, as soon as the chain decides — see fallback().
        self._trigger: dict | None = None
        self.d["env"] = env_fingerprint()
        # One field per probe, so "every stall is on the pods that got the older driver" is a
        # Kibana question rather than an S3 archaeology exercise.
        _log.event("run.env", message="worker environment",
                   **_log.flatten("aci.env", self.d["env"]))
        self._carry_forward()

    def _carry_forward(self) -> None:
        """Resume this shard's COUNTERS from what it already published.

        The counters are per-process, so a restarted pod republished done=0 and the screen read
        two live workers as idle — while S3 held 88 scorecards against 75 counted. A restart is
        the normal case on spot, so the numbers have to survive one.

        Best effort throughout, for the same reason publish() is: progress is bookkeeping, and no
        failure to READ it is worth aborting a GPU run over. `aws s3` not being resolvable raises
        rather than returning non-zero, and that would take the worker down before its first
        document."""
        try:
            prev = _s3("cp", f"{self._dest}", "-", profile=self._profile)
        except Exception:                                    # noqa: BLE001
            return
        if prev.returncode != 0 or not prev.stdout.strip():
            return
        try:
            old = json.loads(prev.stdout)
        except Exception:                                    # noqa: BLE001
            return
        for k in ("done", "extracted", "cloned", "failed", "pages_done"):
            self.d[k] = int(old.get(k) or 0)
        for g, n in (old.get("gates") or {}).items():
            self.d["gates"][g] = self.d["gates"].get(g, 0) + int(n)
        self.d["recent"] = (old.get("recent") or [])[: self.RECENT]
        self.d["restarts"] = int(old.get("restarts") or 0) + 1
        # The ledger is append-only, but S3 has no append: every row is re-uploaded with the
        # file. A restarted pod has an EMPTY local ledger, so without pulling the published one
        # back first the next upload would truncate the whole shard's history to one row.
        try:
            prev_led = _s3("cp", self._ledger_dest, "-", profile=self._profile)
            if prev_led.returncode == 0 and prev_led.stdout.strip():
                self._ledger.write_text(prev_led.stdout if prev_led.stdout.endswith("\n")
                                        else prev_led.stdout + "\n")
        except Exception:                                    # noqa: BLE001
            pass
        print(f"  carried forward from a previous process: done={self.d['done']} "
              f"failed={self.d['failed']} (restart #{self.d['restarts']})", flush=True)

    def start(self, product: str, label: str, pages: int) -> None:
        self._stage = "starting"
        self._trigger = None
        self.d["current"] = {"product": product, "label": label, "pages": pages,
                             "started_at": time.time(), "stage": "starting"}
        self.publish()

    def stage(self, name: str) -> None:
        """Which stage the document in flight is in.

        run_corpus already emits these transitions (stage1 / stage2_mineru / stage3, plus
        toc_rescue and mineru_fallback as each tier is entered) and this worker was throwing them
        away. Without them a worker looks identical whether it is 30 seconds into stage 1 or 20
        minutes into a single MinerU subprocess that reports nothing until it finishes — which is
        why a healthy run reads as a hung one."""
        now = time.time()
        cur = self.d.get("current")
        if cur:
            # CLOSE OFF the stage that just ended. run_one builds the same breakdown, but only
            # writes it to the scorecard at the very end — so for however long a document is in
            # flight (40 minutes on the largest here) the screen could say which stage it was in
            # and nothing about the ones already done. Accumulating them here means a running
            # document shows the same "where the time went" a finished one does, minus only the
            # stage still going.
            #
            # Accumulated rather than assigned: a fallback tier re-runs stages under their own
            # names, and overwriting would report only the last occurrence.
            prev, at = cur.get("stage"), cur.get("stage_at") or cur.get("started_at")
            if prev and prev != "starting" and at:
                steps = cur.setdefault("steps", {})
                steps[prev] = round(steps.get(prev, 0.0) + (now - at), 1)
        self._stage = name
        if not cur:
            return
        cur["stage"] = name
        cur["stage_at"] = now
        el = now - (cur.get("started_at") or now)
        print(f"      -> {name} ({el/60:.0f}m into {cur.get('label')})", flush=True)
        self.publish()

    def fallback(self, info: dict) -> None:
        """The document in flight has entered the fallback chain, and WHY.

        Without this a rescued document is indistinguishable from a slow one: the stage ticks
        back to stage1/stage2_mineru for a second time and the screen shows a document that has
        inexplicably gone BACKWARDS, with no way to tell that it is on its second pass or what
        sent it there. The reason is the actionable half — a document rescued because its
        printed TOC was lacking is a different problem from one that lost two thirds of its
        words — and it is only computable at this moment: the TOC rescue REPLACES the outline,
        so the re-scored `toc` dimension reads healthy afterwards and the original failure is
        gone from the final scorecard.

        Classification stays in the service (extraction_monitor.trigger_causes), so the raw
        reason is what travels. One vocabulary, one place to change it."""
        cur = self.d.get("current")
        self._trigger = {"reason": (info or {}).get("reason"),
                         "at": time.time(),
                         "first": (info or {}).get("first_attempt") or {}}
        if not cur:
            return
        cur["fallback"] = self._trigger
        _log.event("stage.fallback", level="warn",
                   message=f"fallback: {(info or {}).get('reason')}",
                   **{"aci.fallback_reason": (info or {}).get("reason"),
                      **_log.flatten("aci.fallback_first",
                                     (info or {}).get("first_attempt") or {})})
        print(f"      -> FALLBACK: {(info or {}).get('reason')}", flush=True)
        self.publish()

    def heartbeat(self) -> None:
        """Touch the summary so "last update" reflects liveness, not the last completed document.

        A worker legitimately spends 20+ minutes inside one MinerU call, and the screen's
        staleness check reads updated_at — so without this a working worker drifts toward looking
        stale, and a genuinely dead one is indistinguishable from a busy one."""
        if self.d.get("current"):
            self.publish()

    def _row(self, product: str, label: str, **extra) -> dict:
        """The common head of a ledger/recent row: which document, when, and where it started.

        `started_at` is carried rather than derived from ts-minus-seconds, because a document that
        crashed has no measured duration at all and the screen still has to place it on a
        timeline."""
        cur = self.d.get("current") or {}
        row = {"ts": time.time(), "product": product, "label": label,
               "shard": self.d.get("shard"),
               "started_at": cur.get("started_at")}
        row.update(extra)
        return {k: v for k, v in row.items() if v is not None and v != {} and v != []}

    def _append_ledger(self, row: dict) -> None:
        """One line per document, then re-upload. Best effort, exactly like publish().

        Re-uploading the whole file per document is deliberate: S3 cannot append, and the
        alternative — one object per document — is the 891-object listing the progress design
        exists to avoid. The file is ~350KB at the end of a full corpus, so the last upload of a
        17-hour run is still smaller than a single scorecard."""
        try:
            with self._ledger.open("a") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            _s3("cp", str(self._ledger), self._ledger_dest, "--only-show-errors",
                profile=self._profile)
        except Exception:                                        # noqa: BLE001
            pass

    def finished(self, product: str, label: str, pages: int, res: dict, secs: float,
                 review: list[str] | None = None) -> None:
        gate = res.get("gate") or "unknown"
        self.d["gates"][gate] = self.d["gates"].get(gate, 0) + 1
        self.d["done"] += 1
        if res.get("status") == "duplicate":
            self.d["cloned"] += 1
        else:
            self.d["extracted"] += 1
            self.d["pages_done"] += pages
        # run_one already computed every one of these and the summary was dropping all of them,
        # so "where did this document spend its time" could only be answered by opening its
        # scorecard. `steps` is the per-stage breakdown, and the TIER matters as much as the
        # gate: a tier can run, score worse than the first pass and be thrown away, so a row
        # saying "MinerU full" without saying whether it was ADOPTED is actively misleading.
        steps = res.get("steps") or {}
        fb_steps = {"toc_rescue", "mineru_full"}
        row = self._row(
            product, label, gate=gate, worst=res.get("worst"),
            # Which tree gate/worst above actually describe -- 3 for the extraction gate, 5
            # when stage 4/5 ran and its verdict overrode it (see run_corpus.run_one). Not
            # written when absent (older workers, or a document with no stage 4/5), and
            # _finished_job treats that absence as 3 -- correctly, since every ledger row
            # written before this field existed was extraction-gate-only by construction.
            scored_stage=res.get("scored_stage"),
            # The stage-3 verdict, kept beside the final one so the monitor can show the
            # AI pass's EFFECT (SC1 -> SC2) rather than only its result. _row drops Nones,
            # so a document with no stage 4/5 writes nothing extra here.
            gate_extraction=res.get("gate_extraction"),
            worst_extraction=res.get("worst_extraction"),
            seconds=round(secs, 1), pages=pages or None,
            spp=round(secs / pages, 2) if pages else None,
            steps={k: v for k, v in sorted(steps.items(), key=lambda kv: -kv[1])
                   if k != "total"},
            slowest=max((kv for kv in steps.items() if kv[0] != "total"),
                        key=lambda kv: kv[1], default=(None,))[0],
            fb_seconds=round(sum(v for k, v in steps.items() if k in fb_steps), 1) or None,
            # The summary AI route is a product rule, not a fallback tier -- checked first so
            # it is never mistaken for a plain first pass just because it set no fallback_tier.
            tier=res.get("fallback_tier")
                or ("summary_ai" if res.get("route") == "summary_ai"
                    else ("stage1" if res.get("fallback") else "first_pass")),
            fallback=bool(res.get("fallback")) or None,
            # Which route this document was extracted by -- carried through so the Core Index
            # can draw the summary route's own trace graph instead of the spine everyone else
            # walks (see service/summary_trace_graph.py). And what it cost: set by the
            # summary-AI route itself, or by the opt-in stage-4 AI post-process on the
            # ordinary spine -- the two paid passes in the pipeline, mutually exclusive
            # per document, both landing in this one field.
            route=res.get("route"),
            cost_usd=res.get("cost_usd"),
            # Stage 4's own per-section health -- absent (not just falsy) where it does not
            # apply, so the screen can tell "never ran" from "ran and failed". Filenames only,
            # same rule as everything else on this row: the reason text is a second GET the
            # drill-down pays for when a row is actually opened, not a cost every row pays.
            stage4_status=res.get("stage4_status"),
            stage4_failed_sections=res.get("stage4_failed_sections"),
            # WHY it escalated, carried raw. run_one reports the chain's own verdict on whether
            # any tier cleared the bar; a hard fail is not the same as a bad gate — it says the
            # pipeline is out of options, and nothing on the screen said so before.
            trigger_reason=(res.get("fallback_reason")
                            or (self._trigger or {}).get("reason")),
            hard_fail=res.get("fallback_hard_fail") or None,
            accepted=res.get("fallback_accepted"),
            tables=res.get("tables"), mineru_pages=res.get("mineru_pages"),
            cloned=(res.get("status") == "duplicate") or None,
            # The outline came from the document's printed contents page, not its own
            # bookmarks -- the TOC rescue, which now happens in the pre-flight before
            # Stage 2 rather than as a tier afterwards. Without this on the row the job
            # list calls such a document "first pass" and the rescue is invisible.
            toc_rescued=res.get("toc_rescued"),
            status=res.get("status"),
            # what the screen may link to; absent on older runs, which is why
            # the service can also probe.
            review=review or [])
        self.d["recent"] = ([row] + self.d["recent"])[:self.RECENT]
        self.d["current"] = None
        self._stage = None
        self._trigger = None
        self._append_ledger(row)
        self.publish()

    def failed(self, product: str, label: str, err: str) -> None:
        self.d["failed"] += 1
        # 200 characters truncated the message BEFORE the MinerU stderr appended to it, so the one
        # thing worth reading was the one thing dropped — and diagnosing the first cluster run
        # needed Kibana we could not reach. The summary keeps a usable slice; the full text goes
        # to its own object, because a bounded summary must not become a log.
        cur = self.d.get("current") or {}
        started = cur.get("started_at")
        # WHICH STAGE it died in. The exception names the fault but never the pipeline stage, and
        # the two answer different questions: a CalledProcessError in stage2_mineru is a MinerU
        # problem, the identical exception in stage1 is not. This was previously only recoverable
        # from a pod log, which on spot capacity is frequently already gone.
        row = self._row(product, label, gate="error", stage=self._stage,
                        pages=cur.get("pages") or None,
                        seconds=round(time.time() - started, 1) if started else None,
                        error=err[:1500])
        self.d["recent"] = ([row] + self.d["recent"])[:self.RECENT]
        self.d["current"] = None
        self._stage = None
        # The ledger keeps a SHORTER excerpt than `recent`: it holds every document in the run,
        # and 1,500 characters of traceback per failure is how a bounded file becomes a log. The
        # full text is already its own object under _failures/.
        self._append_ledger({**row, "error": err[:400]})
        self._write_failure(product, label, err)
        self.publish()

    def interrupted(self, signum: int, killed: int = 0) -> None:
        """A signal cut the shard short mid-document. Record it as INTERRUPTED, not as a failure.

        From S3 the two are the same observation — a document that started, wrote no scorecard
        and never reported again — and they need opposite responses: a failure is a document to
        look at, an interruption is a node that went away with work still to do. `status` is what
        separates them, and it is set BEFORE the process exits, so the screen stops counting this
        worker as live instead of waiting out the staleness timeout.

        The document in flight gets a ledger row too. It is the one thing the resume cannot
        reconstruct: every other document is either finished (scorecard in S3) or untouched, and
        "which one was mid-flight when the node went" is the first question asked afterwards."""
        if self.d.get("status") == "interrupted":
            # Already recorded — by the signal handler, which writes it first because the raise
            # is not guaranteed to reach main(). A second pass must not append a second ledger
            # row for the same document or reset the counters that went with the first.
            return
        cur = self.d.get("current") or {}
        product, label = cur.get("product"), cur.get("label")
        started = cur.get("started_at")
        # `stopping` stays set as well as `status`. Every existing reader — the monitor's state
        # ladder, watch_promotion, run.end's stopped_early — keys on that flag, and an
        # interruption is a stop: this adds a reason, it does not replace the fact.
        self.d["stopping"] = True
        self.d["status"] = "interrupted"
        self.d["interrupted_at"] = time.time()
        self.d["interrupted_signal"] = int(signum)
        self.d["interrupted_children_killed"] = int(killed)
        self.d["interrupted_document"] = f"{product}/{label}" if product and label else None
        self.d["interrupted_stage"] = self._stage
        if product and label:
            row = self._row(product, label, gate="interrupted", status="interrupted",
                            stage=self._stage, pages=cur.get("pages") or None,
                            seconds=round(time.time() - started, 1) if started else None,
                            signal=int(signum))
            self.d["recent"] = ([row] + self.d["recent"])[:self.RECENT]
            self._append_ledger(row)
        self.d["current"] = None
        self._stage = None
        self._trigger = None
        self.publish()

    def _write_failure(self, product: str, label: str, err: str) -> None:
        """One object per failed document at <results>/_failures/, readable from S3 alone."""
        try:
            safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{product}__{label}")
            f = self._local.parent / f"fail-{safe}.log"
            f.write_text(f"run={self.d.get('run')} shard={self.d.get('shard')}\n"
                         f"product={product}\nlabel={label}\n"
                         f"env={json.dumps(self.d.get('env') or {}, indent=2)}\n\n{err}\n")
            _s3("cp", str(f), f"{self._results}/_failures/{safe}.log",
                "--only-show-errors", profile=self._profile)
        except Exception:                                    # noqa: BLE001
            pass

    def publish(self) -> None:
        """Best effort: a failed progress upload must never abort an extraction run.

        Best effort, but not SILENT. This swallowed everything, including the one failure
        that matters: if the PUT is denied or keeps timing out, the shard summary in S3 stops
        moving while the worker is perfectly healthy — and the monitor, which reads
        updated_at, calls that stale and then dead. The screen says the run has died, the run
        has not died, and nothing anywhere says which is true. Same shape as the denied
        GetObject on _retry/ that cost a day: the reads failed quietly too.

        Logged on the TRANSITION, not per call. This runs on every stage tick and every
        60-second heartbeat, so logging each failure would turn one broken permission into
        thousands of identical lines — while logging only the change says exactly when the
        publishing broke and when it came back."""
        self.d["updated_at"] = time.time()
        ok, err = False, ""
        try:
            self._local.write_text(json.dumps(self.d, indent=2))
            r = _s3("cp", str(self._local), self._dest, "--only-show-errors",
                    profile=self._profile)
            ok = getattr(r, "returncode", 1) == 0
            if not ok:
                err = (getattr(r, "stderr", "") or "").strip().replace("\n", " ")
        except Exception as e:                                   # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        if ok == self._publishing_ok:
            return
        self._publishing_ok = ok
        if ok:
            _log.event("progress.publish_recovered",
                       message="shard summary is reaching S3 again",
                       **{"aci.dest": self._dest})
        else:
            _log.event("progress.publish_failing", level="error",
                       message=f"shard summary is NOT reaching S3 — the monitor will read "
                               f"this worker as stale while it is still running: {err[:300]}",
                       **{"event.outcome": "failure", "aci.dest": self._dest,
                          "error.message": err})


def page_count(pdf: Path) -> int:
    try:
        import fitz
        with fitz.open(str(pdf)) as d:
            return d.page_count
    except Exception:
        # No fitz, or an unreadable PDF: fall back to size as a page-count proxy so balancing
        # still beats round-robin. ~28KB/page measured across this corpus.
        try:
            return max(1, pdf.stat().st_size // 28_000)
        except OSError:
            return 1


def content_groups(jobs: list[tuple[str, dict]]) -> list[list[tuple[str, dict]]]:
    """Group the work list by PDF content hash, biggest group first.

    This corpus files one document under many jurisdictions — measured over corpus-src, 1,088
    files are 891 distinct documents, and 25% of the page count is redundant: one 281-page US
    Data Privacy memorandum appears 33 times, a 286-page one 22 times. run_corpus already
    handles that with hash_index/clone_job (hard-linked, so N copies cost one copy of the
    bytes), but only for copies it can SEE. Splitting copies across pods would defeat it and
    quietly buy back the whole 25%, so a content group is indivisible: it goes to one shard,
    representative first, and the rest clone from it."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for product, job in jobs:
        groups.setdefault(R.pdf_sha1(job["pdf"]), []).append((product, job))
    for g in groups.values():
        g.sort(key=lambda pj: (pj[0], pj[1]["label"]))
    return sorted(groups.values(), key=lambda g: (-page_count(g[0][1]["pdf"]),
                                                  g[0][0], g[0][1]["label"]))


def assign_shard(jobs: list[tuple[str, dict]], shard: int, shards: int) -> list[tuple[str, dict]]:
    """Longest-first onto the least-loaded shard, balanced by PAGES not document count.

    Pure function of the work list: every worker computes the same partition from the same
    inputs, so shards are disjoint without any coordination. Weight is the pages of the
    group's representative, because its duplicates are hard-link clones rather than extractions."""
    groups = content_groups(jobs)
    if shards <= 1:
        return [pj for g in groups for pj in g]
    load = [0] * shards
    buckets: list[list[tuple[str, dict]]] = [[] for _ in range(shards)]
    for g in groups:
        i = min(range(shards), key=lambda k: (load[k], k))
        load[i] += page_count(g[0][1]["pdf"])
        buckets[i].extend(g)
    spread = f"{min(load):,}–{max(load):,} pages"
    print(f"  {shards} shard(s), {spread} of unique pages each; this worker is shard {shard} "
          f"with {len(buckets[shard])} job(s) / {load[shard]:,} pages to extract", flush=True)
    return buckets[shard]


# The trees a job can hold. Stage 4 exists only where AI post-processing ran and stage 5
# only where sub-chunking did, so the viewer offers whichever are present.
STAGE_DIRS = {3: "03_stage3_final", 4: "04_stage4_ai", 5: "05_subchunks"}


def build_review_artefacts(dest: Path, product: str, label: str, route: str | None = None) -> list[str]:
    """Build the self-contained viewer and inspection page INTO the job dir, so they travel with
    the extraction to S3.

    A run writes to corpus/<run>/…, which is not where the Doc Library reads from
    (index/<version>/doc-gallery/). So a document that has just finished cannot be linked to the
    gallery viewer — it is not there, and publishing it is a separate step against a finished
    corpus. Building the same artefact here costs seconds (the tree and the PDF are already on
    local disk) and ~6MB against a job that is already ~500MB, and it means the monitoring screen
    can link straight to the real viewer rather than to a lesser copy of it.

    `route`, when "summary_ai", means there is no 03_stage3_final at all: that route's only
    tree is 04_stage4_ai (summary_ai_extract.split() writes it in the same stage-3 shape), so
    it is the one build_viewer gets. Without this a summary-AI job publishes no viewer.html —
    and PROMOTION_PIPELINE.md's `no_viewer` gate then blocks every one of them from going live,
    silently, until someone runs backfill_review_artefacts by hand.

    Best effort: a document whose viewer fails to build is still a successfully extracted
    document, and must not be reported as a failure."""
    made: list[str] = []
    try:
        import push_hybrid_s3 as PH
        jur = label.rsplit("__", 1)[0]
        title = f"{R.product_display(product) if hasattr(R, 'product_display') else product} — {jur}"
        pdf = dest / "source.pdf"
        stages = {n: dest / d for n, d in STAGE_DIRS.items() if (dest / d).is_dir()}
        # Newest tree wins. Every route but summary_ai always has 03_stage3_final; summary_ai
        # never does, so this is the one place the two routes need different code rather than
        # a hardcoded "03_stage3_final".
        tree = stages.get(max(stages)) if stages else None
        if tree and pdf.exists():
            if route == "summary_ai":
                sub = "summary AI pipeline — direct Bedrock transcription, no stage 1-3"
            else:
                # Every stage the job produced, not just stage 3. A document that went through
                # AI post-processing otherwise publishes a viewer of the pass's INPUT, with no
                # way to reach what the pass actually did.
                sub = "extraction run — pdf2mdtree + MinerU"
                if len(stages) > 1:
                    sub += f" · stages {'/'.join(str(n) for n in sorted(stages))}"
            (dest / "viewer.html").write_text(
                PH.build_viewer(tree, pdf, title, sub,
                                stages=stages if len(stages) > 1 else None))
            made.append("viewer.html")
        ins = PH.build_inspect(dest, title)
        if ins:
            (dest / "inspect.html").write_text(ins)
            made.append("inspect.html")
    except Exception as e:                                   # noqa: BLE001
        print(f"      (review artefacts not built: {type(e).__name__}: {e})", flush=True)
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=os.environ.get("CORPUS_SOURCE"),
                    help="s3://bucket/prefix holding the source PDFs [$CORPUS_SOURCE]")
    ap.add_argument("--root", default=os.environ.get("CORPUS_ROOT"),
                    help="local corpus root, skipping the source sync [$CORPUS_ROOT]")
    ap.add_argument("--results", default=os.environ.get("CORPUS_RESULTS"),
                    help="s3://bucket/prefix for the output — THIS NAMES THE RUN [$CORPUS_RESULTS]")
    ap.add_argument("--out", default=os.environ.get("CORPUS_OUT"),
                    help="local scratch for job trees; defaults to /work/out/<results prefix leaf> "
                         "so a new results prefix never inherits an old run's trees [$CORPUS_OUT]")
    ap.add_argument("--only", action="append", default=None, help="product (repeatable)")
    ap.add_argument("--exclude", action="append", default=None, help="product (repeatable)")
    ap.add_argument("--shards", type=int, default=int(os.environ.get("CORPUS_SHARDS", "1")))
    ap.add_argument("--shard", type=int,
                    default=int(os.environ.get("JOB_COMPLETION_INDEX", "0")),
                    help="this worker's index; a Kubernetes Indexed Job sets JOB_COMPLETION_INDEX")
    ap.add_argument("--profile", default=None, help="AWS profile; omit to use the pod/instance role")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--plan", action="store_true", help="print the shard and the estimate, then stop")
    ap.add_argument("--selftest", action="store_true",
                    help="record what this container can actually do (GPU, VRAM, MinerU models, "
                         "/dev/shm) to <results>/_selftest/ and exit — validates an image in "
                         "seconds, with the answer readable from S3 and no cluster access")
    ap.add_argument("--secs-per-page", type=float, default=15.9,
                    help="15.9 is MEASURED ON APPLE/MPS; pass the GPU's own benchmark here")
    args = ap.parse_args()

    # BEFORE anything that can fail. A crash outside the per-document handler used to emit
    # nothing at all — Python prints the traceback to stderr and every event this worker
    # writes goes to stdout, so the pod log had the reason and Kibana had silence, and the
    # run read as a stall rather than a failure. See obs.log.install_crash_handlers.
    _log.install_crash_handlers()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if args.shard >= args.shards:
        sys.exit(f"  shard {args.shard} does not exist in {args.shards} shard(s)")
    if not args.results:
        sys.exit("  --results / $CORPUS_RESULTS is required: it names the run")
    if bool(args.source) == bool(args.root):
        sys.exit("  give exactly one of --source/$CORPUS_SOURCE (S3) or --root/$CORPUS_ROOT (local)")

    # The results prefix names the run, and resume is ALWAYS on against it: a document with a
    # scorecard there is skipped, one without is extracted. That single rule covers both
    # intentions without a --force flag to get wrong — point at a new prefix and the whole
    # corpus re-extracts, point at the same one and an interrupted run picks up where it stopped.
    #
    # The local scratch dir therefore has to follow the prefix, not outlive it. A restarted pod
    # keeps its emptyDir (restartPolicy: OnFailure reuses the pod), so a fixed /work/out would let
    # a PREVIOUS run's trees suppress work the new prefix genuinely needs.
    run_id, out_root = resolve_run(args.results, args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    root = Path(args.root).resolve() if args.root else Path("/work/corpus-src")
    _log.bind_service("aosphere-extract",
                      **{"aci.run_id": run_id, "aci.shard": args.shard,
                         "aci.shards": args.shards, "aci.runner": "corpus_worker",
                         # ONE CONTAINER START. restartPolicy: OnFailure restarts the
                         # container in the SAME pod, so the pod name does not change between
                         # attempts and every attempt at a poisonous document would otherwise
                         # read as one long confusing run. The container restart count is not
                         # in the downward API, so this is generated rather than read: it
                         # needs nothing from the deployment and cannot be forgotten there.
                         "aci.proc_id": uuid.uuid4().hex[:12]})
    _log.event("run.start",
               message=f"run={run_id} shard={args.shard}/{args.shards}",
               **{"aci.results": args.results, "aci.source": args.source,
                  "aci.root": args.root, "aci.scratch": str(out_root),
                  "aci.mode": ("selftest" if args.selftest else
                               "plan" if args.plan else "extract"),
                  "aci.only": args.only, "aci.exclude": args.exclude,
                  "aci.limit": args.limit or None,
                  "aci.argv": " ".join(sys.argv[1:])[:1000]})
    print(f"  run={run_id}  results={args.results}  scratch={out_root}", flush=True)
    if args.source:
        # 1.23 GB over the pod's network before any work starts. A slow or failing sync is
        # indistinguishable from a slow image pull or a stuck worker from outside, and it
        # happens before the first heartbeat exists to say otherwise.
        _t_pull = time.time()
        _log.event("sources.start", message=f"syncing {args.source}",
                   **{"aci.source": args.source, "aci.dest": str(root)})
        pull_sources(args.source, root, args.profile)
        _log.event("sources.end", message="source sync complete",
                   **{"event.outcome": "success",
                      "event.duration_s": round(time.time() - _t_pull, 1),
                      "aci.source_files": sum(1 for _ in root.rglob("*.pdf"))})

    if args.selftest:
        env = env_fingerprint()
        print(json.dumps(env, indent=2, default=str), flush=True)
        f = out_root / "selftest.json"
        f.write_text(json.dumps({"run": run_id, "shard": args.shard, "at": time.time(),
                                 "env": env}, indent=2, default=str))
        dest = f"{args.results.rstrip('/')}/_selftest/shard-{args.shard}.json"
        r = _s3("cp", str(f), dest, "--only-show-errors", profile=args.profile)
        print(f"  selftest -> {dest} ({'ok' if r.returncode == 0 else 'UPLOAD FAILED'})", flush=True)
        _log.event("selftest", level="info" if r.returncode == 0 else "error",
                   message="selftest recorded",
                   **{"aci.upload_ok": r.returncode == 0, "aci.dest": dest,
                      **_log.flatten("aci.env", env)})
        # A container that cannot see a GPU is not fit to run this workload; say so in the exit
        # code so a Job fails loudly instead of grinding through the corpus on CPU.
        # A GPU with no C compiler is a container that WILL fail on MinerU's CUDA path, so it is
        # not fit for this workload even though every other probe looks healthy.
        ok = (bool(env.get("cuda_available"))
              and not env.get("mineru_error")
              and bool(env.get("cc")))
        if env.get("cuda_available") and not env.get("cc"):
            print("  FAIL: CUDA is present but there is no C compiler — triton cannot build its "
                  "kernels, and MinerU's GPU path will exit non-zero on every document.", flush=True)
        sys.exit(0 if ok else 1)

    remote_done = done_labels(args.results, args.profile)
    permanent = permanent_labels(args.results, args.profile)
    if permanent:
        # Not "done" in any good sense — but re-attempting a deterministic failure on every run
        # is a loop, and the pipeline has already diagnosed each of these.
        remote_done |= permanent
        print(f"  {len(permanent)} document(s) previously failed deterministically and will be "
              f"skipped (mark one for retry to force it)", flush=True)
    marked = retry_labels(args.results, args.profile)
    if marked:
        # A marked document is treated as not-done even though its scorecard is there, which is
        # the whole point: it is how a reviewer gets one bad result redone without discarding a
        # whole run's worth of good ones.
        remote_done -= marked
        print(f"  {len(marked)} document(s) marked for re-extraction: "
              f"{', '.join(sorted(marked)[:4])}{' …' if len(marked) > 4 else ''}", flush=True)
    print(f"  already in {args.results}: {len(remote_done)} document(s)", flush=True)
    _log.event("run.resume", message=f"{len(remote_done)} document(s) already done",
               **{"aci.done_already": len(remote_done), "aci.permanent": len(permanent),
                  "aci.marked_retry": len(marked)})

    exclude = set(args.exclude or ())
    # in_scope is the WHOLE version -- every document this run is responsible for. todo is only
    # what is still missing. Both are needed: a resumed attempt that has 40 documents left must
    # not report itself as "0% of 40" when the version is 95% extracted, so the progress objects
    # carry the version's totals alongside the attempt's.
    in_scope, todo = [], []
    for product, jobs in sorted(R.discover(root).items()):
        if product in exclude or (args.only and product not in args.only):
            continue
        for j in jobs:
            in_scope.append((product, j))
            if f"{product}/{j['label']}" in remote_done:
                continue
            if (f"{product}/{j['label']}" not in marked
                    and (out_root / product / j["label"] / "scorecard.json").exists()):
                continue
            todo.append((product, j))

    mine = assign_shard(todo, args.shard, args.shards)
    if args.limit:
        mine = mine[:args.limit]

    if args.plan:
        # Count each document ONCE. Summing pages per file would bill the 33 copies of the
        # 281-page US memorandum as 9,273 pages of GPU work when they are 281 pages plus 32
        # hard-link clones — a ~2x overstatement of a number people size clusters from.
        groups = content_groups(mine)
        pages = sum(page_count(g[0][1]["pdf"]) for g in groups)
        clones = len(mine) - len(groups)
        hours = pages * args.secs_per_page / 3600
        print(f"  would run {len(mine)} job(s) = {len(groups)} extraction(s) + {clones} clone(s)")
        print(f"  {pages:,} pages to extract at {args.secs_per_page:.1f}s/page: "
              f"{hours:.1f}h for this shard")
        _log.event("run.plan", message=f"{len(mine)} job(s), {pages:,} pages",
                   **{"aci.jobs": len(mine), "aci.extractions": len(groups),
                      "aci.clones": clones, "aci.pages": pages,
                      "aci.estimate_hours": round(hours, 2)})
        return

    hash_idx = R.hash_index(out_root)   # best-effort: only sees what THIS worker extracted
    groups = content_groups(mine)
    # Version-level totals are the same in every shard (the partition is deterministic), so the
    # aggregator takes them with max() rather than summing them.
    prog = ShardProgress(run_id, args.shard, args.shards, len(mine),
                         sum(page_count(g[0][1]["pdf"]) for g in groups),
                         out_root, args.results, args.profile,
                         corpus_docs=len(in_scope),
                         corpus_pages=sum(page_count(g[0][1]["pdf"])
                                          for g in content_groups(in_scope)),
                         remaining_docs=len(todo),
                         remaining_pages=sum(page_count(g[0][1]["pdf"])
                                             for g in content_groups(todo)))
    # From here on a signal can record itself. Before this point there is no shard summary to
    # write to, and nothing has been done that needs recording.
    global _PROG, _DOC
    _PROG = prog
    t0, ok, failed, uploaded_fail = time.time(), 0, 0, 0
    # The heartbeat thread is named OUTSIDE the loop so the interrupt handler can stop it: a
    # timer still publishing "current: <document>" after the shard has been marked interrupted
    # would put the document back on the screen as in-flight.
    hb = None
    try:
        for i, (product, job) in enumerate(mine, 1):
            # A SIGTERM no longer reaches here — the handler raises and the except clause below
            # takes it. This check now covers the plain-EC2 spot notice (which is polled, not
            # signalled) and the one case worth guarding against: an Interrupted swallowed
            # somewhere in a dependency, where _STOP is set and nothing acted on it.
            if _STOP or spot_reclaim_pending():
                _log.event("run.stopping", level="warn",
                           message=f"stopping after {ok} document(s)",
                           **{"aci.reason": "sigterm" if _STOP else "spot_reclaim",
                              "aci.remaining": len(mine) - i + 1})
                print(f"  stopping after {ok} document(s) — the rest resume from S3", flush=True)
                prog.d["stopping"] = True
                prog.publish()
                break
            dest = out_root / product / job["label"]
            pages = page_count(job["pdf"])
            # What a signal has to undo if it lands during this document — see
            # _disown_current_document. `uploading` narrows that to the one window where the
            # REMOTE scorecard can also be a lie.
            global _DOC
            _DOC = {"dest": dest, "product": product, "label": job["label"],
                    "results": args.results, "profile": args.profile, "uploading": False}
            print(f"  [{i}/{len(mine)}] {product}/{job['label']}", flush=True)
            R.bind_document(job, product, dest, pages=pages, index=i, total=len(mine))
            prog.start(product, job["label"], pages)
            t_doc = time.time()
            # ONE timer thread doing both jobs: the S3 summary PUT that keeps "last update" honest
            # for the dashboard, and the Kibana beat that says what the worker is actually doing.
            # They ask the same question at the same interval, so they share a thread and can
            # never disagree about whether the worker is alive.
            hb = _probe.Heartbeat(interval=60, watch=dest, scratch=str(out_root),
                                  also=prog.heartbeat).start()
            try:
                res = R.run_one(job, product, dest, False, False, hash_idx=hash_idx,
                                progress_cb=prog.stage, trigger_cb=prog.fallback)
            except Exception as e:                                  # noqa: BLE001
                hb.stop()
                hb = None
                failed += 1
                # A one-line exception is not diagnosable 14 hours into a GPU run. MinerU failures
                # surface as CalledProcessError, whose message names the command but never the
                # reason -- the reason is in its stderr, and WHICH tier called it is only in the
                # traceback. Both go to the log; a short form goes into the progress object so the
                # monitoring screen can show why a document failed without anyone opening a pod log.
                detail = str(getattr(e, "stderr", "") or "")
                full = f"{type(e).__name__}: {e}\n{traceback.format_exc()}\n{detail}"
                cause, permanent_fail = classify(full)
                print(f"    ✗ {type(e).__name__}: {e}", flush=True)
                print(f"      cause={cause} retryable={not permanent_fail}", flush=True)
                # run_one's wrapper already emitted doc.fail with the traceback; this adds the
                # two things only the worker knows — the classification and whether the document
                # is now permanently skipped, which is the difference between "retry the run" and
                # "a human has to look at this one".
                _log.event("doc.classified", level="error",
                           message=f"{cause} ({'permanent' if permanent_fail else 'retryable'})",
                           **{"aci.cause": cause, "aci.permanent": bool(permanent_fail),
                              "aci.retryable": not permanent_fail,
                              "aci.failed_stage": prog._stage,
                              "aci.doc_elapsed_s": round(time.time() - t_doc, 1)})
                if permanent_fail:
                    mark_permanent(args.results, product, job["label"], cause, full,
                                   out_root, args.profile)
                    print("      recorded as permanent — future runs will skip it until the code "
                          "changes", flush=True)
                if detail:
                    print(f"      mineru stderr (tail): {detail[-1500:]}", flush=True)
                traceback.print_exc()
                prog.failed(product, job["label"],
                            f"{type(e).__name__}: {e}\n\n--- traceback ---\n"
                            + traceback.format_exc()
                            + (f"\n--- mineru stderr ---\n{detail}" if detail else ""))
                R.unbind_document()
                # A crash already leaves no scorecard, so there is nothing for a later signal
                # to disown — and disowning the NEXT document's predecessor would be wrong.
                _DOC = None
                continue
            # Register what we just extracted so this shard's copies of it CLONE instead of
            # re-extracting. run_corpus's own CLI loop does the same after every job; without it the
            # index only ever holds work from previous runs and every duplicate is paid for again.
            try:
                meta = json.loads((dest / "corpus_meta.json").read_text())
                if meta.get("pdf_sha1"):
                    hash_idx.setdefault(meta["pdf_sha1"], dest)
            except (OSError, json.JSONDecodeError):
                pass
            # Built BEFORE the upload so they travel with the job: the screen links to these.
            made = build_review_artefacts(dest, product, job["label"], route=res.get("route"))
            if made:
                print(f"      built {', '.join(made)}", flush=True)
            # The upload is ~152 MB per document over the pod's network, and it is the one step
            # AFTER the extraction that can quietly cost minutes or fail — a document that is
            # extracted but not uploaded is a document the next run redoes from scratch.
            t_up = time.time()
            up_bytes = _probe.dir_bytes(dest) if _probe is not None else None
            _DOC["uploading"] = True
            pushed = push_job(dest, args.results, product, args.profile)
            _DOC["uploading"] = False
            _log.event("upload.end", level="info" if pushed else "error",
                       message=f"upload {'ok' if pushed else 'FAILED'}",
                       **{"event.outcome": "success" if pushed else "failure",
                          "event.duration_s": round(time.time() - t_up, 1),
                          "aci.upload_bytes": up_bytes,
                          "aci.upload_mb_s": (round(up_bytes / 1e6 / max(time.time() - t_up, 0.1), 1)
                                              if up_bytes else None),
                          "aci.review_artefacts": made or None})
            if pushed:
                ok += 1
            else:
                uploaded_fail += 1
            hb.stop()
            hb = None
            if f"{product}/{job['label']}" in marked:
                clear_retry(args.results, product, job["label"], out_root, args.profile)
            prog.finished(product, job["label"], pages, res, time.time() - t_doc, review=made)
            print(f"    {res.get('status')} gate={res.get('gate')} worst={res.get('worst')} "
                  f"({time.time() - t0:.0f}s elapsed)", flush=True)
            # Progress of the SHARD, on every document: the ETA question ("is this going to
            # finish before the spot node goes away") without opening the dashboard.
            _log.event("shard.progress",
                       message=f"{i}/{len(mine)} done on shard {args.shard}",
                       **{"aci.shard_done": i, "aci.shard_jobs": len(mine),
                          "aci.shard_ok": ok, "aci.shard_failed": failed,
                          "aci.shard_elapsed_s": round(time.time() - t0, 1),
                          "aci.pages_done": prog.d.get("pages_done")})
            R.unbind_document()
            # Finished AND uploaded: both scorecards are now true, and a signal arriving before
            # the next document starts has nothing to undo.
            _DOC = None
    except Interrupted as stop:
        # SIGTERM mid-document. Everything below is bounded and local — stop the beat, kill the
        # children, write the shard summary — because the kubelet is already counting down the
        # termination grace period and a slow tidy-up here is the SIGKILL the raise existed to
        # avoid. No upload of the document in flight: it has no scorecard, so it is not a
        # document, and resume will extract it again from the top.
        if hb is not None:
            try:
                hb.stop()
            except Exception:                                # noqa: BLE001
                pass
        # The handler already killed what was running; this catches anything spawned between
        # the signal and the unwind, which is why the two counts are added rather than one
        # replacing the other.
        killed = stop.children_killed + _kill_children()
        prog.interrupted(stop.signum, killed=killed)
        R.unbind_document()
        _log.event("run.interrupted", level="warn",
                   message=(f"interrupted by signal {stop.signum} after {ok} document(s)"),
                   **{"event.outcome": "unknown", "aci.signal": stop.signum,
                      "aci.reason": "sigterm", "aci.ok": ok, "aci.failed": failed,
                      "aci.jobs": len(mine),
                      "aci.interrupted_document": prog.d.get("interrupted_document"),
                      "aci.interrupted_stage": prog.d.get("interrupted_stage"),
                      "aci.children_killed": killed,
                      "event.duration_s": round(time.time() - t0, 1)})
        print(f"\n  INTERRUPTED by signal {stop.signum} after {ok} document(s) "
              f"({time.time() - t0:.0f}s){f' — killed {killed} child process(es)' if killed else ''}"
              f"\n  {prog.d.get('interrupted_document') or 'no document'} was in flight and is "
              f"NOT uploaded; the rest resume from S3", flush=True)
        # Zero, like the graceful stop below: an eviction is not a crash, and a non-zero exit
        # makes Kubernetes restart the pod into the same reclaim it was just told about. What
        # was outstanding is outstanding in S3, which is where the next attempt reads it from.
        sys.exit(0)

    _log.event("run.end",
               level="error" if (failed or uploaded_fail) else "info",
               message=(f"shard {args.shard}: {ok} uploaded, {failed} extraction failure(s), "
                        f"{uploaded_fail} upload failure(s)"),
               **{"event.outcome": "failure" if (failed or uploaded_fail) else "success",
                  "event.duration_s": round(time.time() - t0, 1),
                  "aci.ok": ok, "aci.failed": failed,
                  "aci.upload_failed": uploaded_fail,
                  "aci.jobs": len(mine), "aci.stopped_early": bool(prog.d.get("stopping"))})
    print(f"\n  shard {args.shard}: {ok} extracted and uploaded, {failed} extraction failure(s), "
          f"{uploaded_fail} upload failure(s), {time.time() - t0:.0f}s", flush=True)
    # A graceful stop is a SUCCESS: the shard's remainder is recoverable from S3, and a non-zero
    # exit would make Kubernetes treat a planned spot eviction as a crash-looping pod.
    sys.exit(1 if (failed or uploaded_fail) else 0)


if __name__ == "__main__":
    try:
        main()
    except Interrupted as _stop:
        # A signal that arrives BEFORE the first document — during the 1.23 GB source sync, the
        # resume listing, the shard plan — has no shard summary to mark and nothing in flight to
        # record. It still must not read as a crash: there is no partial work, and a traceback
        # plus a non-zero exit would have Kubernetes restart the pod into the same eviction.
        _log.event("run.interrupted", level="warn",
                   message=f"interrupted by signal {_stop.signum} before any document started",
                   **{"aci.signal": _stop.signum, "aci.reason": "sigterm",
                      "aci.before_first_document": True})
        print(f"\n  INTERRUPTED by signal {_stop.signum} before any document started",
              flush=True)
        _kill_children()
        sys.exit(0)
