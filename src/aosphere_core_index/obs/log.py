"""Structured run events on stdout, so a cluster run can be debugged from Kibana.

WHY STDOUT AND NOTHING ELSE

The cluster already ships every container's stdout to Kibana; that path belongs to the log
agent on the node, not to us. So the whole shipper is a print: one JSON object per line, and
that line is an indexed document on the other side. No Elasticsearch client, no endpoint, no
API key in the pod, no queue to flush -- and therefore nothing that can fail *while the run is
failing*, which is the only time any of this matters.

WHY A BOUND CONTEXT AND NOT ARGUMENTS (the MDC idea)

Every event needs to say which run, which shard, which product, which jurisdiction, which
document attempt and which stage it came from -- that is what makes a Kibana filter able to
isolate one document out of 891. Threading six identifiers through every call site is how
that decays: the one call that forgot `jurisdiction` is invisible to the filter that would
have found it. So the identifiers are bound once, in a contextvar, and merged into every
event automatically. Bind at the seam where the identity changes (per run, per document, per
stage) and every line below that seam inherits it.

contextvars rather than a plain dict because the heartbeat runs on its own thread: it takes a
snapshot of the context at the moment the thread starts, so a beat cannot report the stage of
whatever the main thread happens to have moved on to.

FLAT DOTTED KEYS

`aci.stage`, never `{"aci": {"stage": ...}}`. Nested objects give Elasticsearch a reason to
infer new sub-mappings on every shape it sees; flat scalar keys give it one stable field per
name. The prefixes are ECS where ECS has a name for the thing (`@timestamp`, `log.level`,
`message`, `event.*`, `error.*`, `service.*`, `host.*`) so Kibana's built-in columns and the
Logs UI work without configuration, and everything domain-specific lives under `aci.*` so one
`aci.*` filter shows the lot.

THE FORMAT IS A SWITCH

ACI_LOG_FORMAT=json (default) | logfmt | off. `json` is right when the node's log agent parses
JSON stdout into fields. `logfmt` is the fallback for an agent that does NOT, where a nested
JSON blob would land as one opaque `message` string and `pages=57 stage=stage2_mineru` at
least stays greppable and is trivially parsed by a Kibana ingest pipeline later. Same events,
same field names, one env var -- so which one is right is a deploy-time question, not a
rewrite.

NOTHING HERE MAY RAISE

A diagnostic that can fail the run it is diagnosing is worse than no diagnostic (the same rule
env_fingerprint already follows). Every public function swallows its own exceptions.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# The ambient context. Merged into every event; see the module docstring.
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("aci_log_ctx", default={})

# stdout is written from the main thread and the heartbeat thread. A line that interleaves
# with another is a line Kibana drops, so the write and the flush are one critical section.
#
# REENTRANT, and it has to be. A signal handler runs on the MAIN thread, wherever that thread
# happens to be — including inside this very critical section. corpus_worker's SIGTERM handler
# logs `run.signal` as its first act, so a plain Lock deadlocked the handler against a write
# the same thread was already doing: the interrupt bookkeeping never ran, the shard was never
# marked interrupted, and the pod left only when the watchdog gave up on it. Verified with a
# signal delivered while the lock was held.
_write_lock = threading.RLock()

# A cross-thread mirror of the context, kept in step with every bind.
#
# A new thread does NOT inherit the contextvar -- it starts with an empty context and would
# read back the defaults -- so the heartbeat thread, whose entire job is to say which document
# and stage is in flight, cannot use _ctx at all. The mirror is what it reads instead.
# Correct because this worker processes one document at a time in one thread; if a future
# caller ever runs documents concurrently, the mirror becomes last-writer-wins and the
# heartbeat would need the context passed to it explicitly.
_mirror: dict = {}
_mirror_lock = threading.Lock()


# The mirror is the MAIN thread's context, and only the main thread may write it.
#
# It exists for the heartbeat, which runs on its own thread and would otherwise report no
# run, document or stage at all. A worker thread binding into it is not a smaller version of
# the same thing, it is a different context overwriting the one the beat needs: stage 4's
# section workers each bind their own section, and on the way out restore their own (empty)
# context — which wiped `aci.stage` from the mirror and made every beat read "still in None"
# while the stage was running perfectly well. Caught by the six-worker test.
#
# Their context is not lost: log.api_call copies what the calling thread has bound onto the
# in-flight registry entry, which is how a beat names the section of the oldest open call.
_main_thread = threading.main_thread()


def _mirror_set(d: dict) -> None:
    if threading.current_thread() is not _main_thread:
        return
    with _mirror_lock:
        _mirror.clear()
        _mirror.update(d)


def snapshot() -> dict:
    """The bound context, readable from ANY thread. See _mirror."""
    with _mirror_lock:
        return dict(_mirror)

# THE hard limit, on every string in every event — one number, not a dozen call-site slices.
#
# It is applied centrally (see _clean and event) rather than trusted to callers, because a cap
# is only a limit if nothing can exceed it: the reason a Bedrock AccessDenied read as
# "…is not authorized to " in Kibana was one call site slicing at [:200] while the logger
# allowed 8,000, and no single place said what the real number was.
#
# 1,000 is a deliberate ceiling on the long fields too. error.stack_trace used to be kept whole
# and child_stderr_tail ran to 4,000; a deep fallback-chain traceback no longer fits in either.
# What survives is the head of the traceback (the exception and the innermost frames) and the
# TAIL of the child's stderr, which is where MinerU says why it died. The full text is still
# written per document to <run>/_failures/<product>__<label>.log in S3 — bounded files, one per
# failure, which is the right home for something this size.
MAX_STR = 1_000
_MAX_STR = MAX_STR                                   # kept: older call sites read this name
_MAX_ITEMS = 50

_FORMAT = (os.getenv("ACI_LOG_FORMAT") or "json").strip().lower()

# install_crash_handlers chains onto whatever hooks are already there, so calling it twice
# would log one crash twice.
_crash_handlers_installed = False


def enabled() -> bool:
    return _FORMAT != "off"


# ---------------------------------------------------------------- context


def bind(**fields) -> None:
    """Add to the ambient context. Every later event on this thread carries it.

    None values are dropped rather than bound: a field that is sometimes absent and sometimes
    null is two field states to query instead of one."""
    try:
        merged = {**_ctx.get(), **{k: v for k, v in fields.items() if v is not None}}
        _ctx.set(merged)
        _mirror_set(merged)
    except Exception:                                        # noqa: BLE001
        pass


def unbind(*keys: str) -> None:
    try:
        kept = {k: v for k, v in _ctx.get().items() if k not in keys}
        _ctx.set(kept)
        _mirror_set(kept)
    except Exception:                                        # noqa: BLE001
        pass


def bound() -> dict:
    """The current context, for a thread that wants to carry it somewhere else."""
    try:
        return dict(_ctx.get())
    except Exception:                                        # noqa: BLE001
        return {}


@contextlib.contextmanager
def context(**fields):
    """Bind for the duration of a block, then restore exactly what was there before.

    Restoring the whole previous mapping rather than unbinding the keys is deliberate: a
    fallback tier re-enters a stage that is already bound, and unbinding would leave the outer
    stage missing instead of restored."""
    try:
        prev = dict(_ctx.get())
    except Exception:                                        # noqa: BLE001
        prev = {}
    bind(**fields)
    try:
        yield
    finally:
        try:
            _ctx.set(prev)
            _mirror_set(prev)
        except Exception:                                    # noqa: BLE001
            pass


# ---------------------------------------------------------------- emit


def _clean(v):
    """One field value, reduced to something Elasticsearch can map consistently."""
    if v is None or isinstance(v, bool) or isinstance(v, int):
        return v
    if isinstance(v, float):
        # NaN/Inf are not JSON and are rejected by the bulk API -- which would silently drop
        # the whole document, not just the field.
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, str):
        return v if len(v) <= _MAX_STR else v[:_MAX_STR] + f"…(+{len(v) - _MAX_STR})"
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, (list, tuple, set)):
        items = [_clean(x) for x in list(v)[:_MAX_ITEMS]]
        return [x for x in items if x is not None]
    if isinstance(v, dict):
        # Kept as a JSON string, not as an object: a free-form dict is exactly the shape that
        # makes Elasticsearch invent a field per key it has never seen. The caller flattens
        # anything it wants queryable.
        try:
            s = json.dumps(v, separators=(",", ":"), default=str)
        except Exception:                                    # noqa: BLE001
            s = str(v)
        return _clean(s)
    return _clean(str(v))


def _logfmt(rec: dict) -> str:
    out = []
    for k, v in rec.items():
        if isinstance(v, bool):
            s = "true" if v else "false"
        elif isinstance(v, (int, float)):
            s = str(v)
        else:
            s = str(v)
        # A value with a space in it parses as two fields otherwise, and the log tool indexes
        # rubbish -- the same trap _log_stage1 already documents.
        if s == "" or any(c in s for c in ' "=\n\t'):
            s = '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        out.append(f"{k}={s}")
    return " ".join(out)


def event(action: str, *, level: str = "info", message: str = "", **fields) -> None:
    """Emit one event. Ambient context first, explicit fields win over it."""
    if _FORMAT == "off":
        return
    try:
        rec: dict = {
            "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() % 1 * 1000):03d}Z",
            "log.level": level,
            "event.action": action,
            # Through _clean like every other field: `message` bypassed it, so the one field
            # every Kibana view shows first was the one field with no limit at all.
            "message": _clean(message or action),
        }
        for k, v in list(bound().items()) + list(fields.items()):
            c = _clean(v)
            if c is not None:
                rec[k] = c
        line = (json.dumps(rec, separators=(",", ":"), default=str) if _FORMAT == "json"
                else _logfmt(rec))
        with _write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except Exception:                                        # noqa: BLE001
        pass


def write_line(s: str = "") -> None:
    """A plain, human-readable line on stdout, under the SAME lock the events use.

    WHY THIS EXISTS RATHER THAN print()

    `print(x)` is TWO writes -- the text, then the newline -- and it takes no lock. The lock
    in `event` therefore guards the structured writes against each other and against nothing
    else: when stage 4 fans sections over a thread pool, one worker's print can land between
    its own two writes while another worker emits an event, and the JSON is glued onto the
    tail of the text line:

        08-private-placement-regime.md: table section, 3 table(s)…{"@timestamp":"…

    That line does not start with `{`, so Filebeat's JSON parse fails, `keys_under_root`
    never runs, and the document reaches Elasticsearch with no `event.action` at all -- a
    `bedrock.retry` that is in the pod log, is in the index, and is findable by NO query that
    names the event. Its neighbours, written while no print was in flight, index perfectly,
    so the loss reads as a single vanished line on a healthy pod.

    One write of the whole line, then the flush, both inside the lock -- the same critical
    section `event` uses, which is the only thing that makes the two writers safe together.
    """
    if _FORMAT == "off":
        return
    try:
        with _write_lock:
            sys.stdout.write(str(s) + "\n")
            sys.stdout.flush()
    except Exception:                                        # noqa: BLE001
        pass


def exception(action: str, exc: BaseException, *, message: str = "", **fields) -> None:
    """An event carrying the FULL traceback.

    Every string on it is capped at MAX_STR (1,000), the traceback included -- one hard limit
    across every event, so no field can be cut by a number that is not written down in one
    place. That is a real loss on a deep fallback-chain traceback, and it is why the stderr
    TAIL rather than its head is what travels: MinerU says why it died in its last lines.
    The untruncated text is still written per document to <run>/_failures/ in S3."""
    stderr = ""
    try:
        stderr = str(getattr(exc, "stderr", "") or "")
    except Exception:                                        # noqa: BLE001
        pass
    event(action, level="error", message=message or f"{type(exc).__name__}: {exc}",
          **{"error.type": type(exc).__name__,
             "error.message": str(exc),
             "error.stack_trace": "".join(traceback.format_exception(
                 type(exc), exc, exc.__traceback__)),
             # Where the reason actually is on a MinerU failure: CalledProcessError's message
             # names the command and never why it died.
             "aci.child_stderr_tail": stderr[-MAX_STR:] or None,
             "event.outcome": "failure",
             **fields})


@contextlib.contextmanager
def timed(action: str, **fields):
    """start/end pair around a block, with the duration and the outcome on the end event.

    The end event is emitted on the failure path too -- a stage that only reports when it
    succeeds turns a crash into missing data, which reads identically to a hang."""
    t0 = time.time()
    event(f"{action}.start", **fields)
    try:
        yield
    except BaseException as e:
        event(f"{action}.end", level="error",
              **{"event.outcome": "failure", "event.duration_s": round(time.time() - t0, 2),
                 "error.type": type(e).__name__, "error.message": str(e)[:MAX_STR], **fields})
        raise
    else:
        event(f"{action}.end",
              **{"event.outcome": "success", "event.duration_s": round(time.time() - t0, 2),
                 **fields})


# EVERY API call currently open. See api_call().
#
# Keyed by a token of its own rather than by thread: one thread makes one call at a time
# today, but keying on the thread means a nested call silently EVICTS the outer one — its
# finally pops the shared key and the outer call vanishes from the registry while it is
# still running. A counter costs nothing and the trap is then not there to fall into.
_inflight: dict[int, dict] = {}
_inflight_lock = threading.Lock()
_inflight_seq = itertools.count(1)


@contextlib.contextmanager
def api_call(name: str, **fields):
    """Register an API call for the duration of it, so the heartbeat can report it.

    The start/end events bracket a call, but between them a 900s converse says nothing at
    all -- and twelve minutes of silence reads the same whether the model is thinking, the
    connection died, or the process is wedged.

    A REGISTRY rather than the bound context, because stage 4 runs several of these at once
    (ACI_STAGE4_AI_WORKERS) and the cross-thread mirror has exactly one slot: with six
    workers it names whichever call started most recently, which is the one least likely to
    be the stuck one. Keyed by thread -- a thread makes one call at a time -- so six open
    calls are six entries and the heartbeat can report the OLDEST, which is the one worth
    knowing about.

    Whatever this thread has bound travels with the entry (the section being repaired, the
    document), so the beat can name the call rather than just count it."""
    key = next(_inflight_seq)
    rec = {"aci.api": name, "started_at": time.time(),
           "aci.api_thread": threading.current_thread().name,
           # The OS thread id, not the name: note() finds this thread's open call by it.
           # Names are assigned by whoever created the thread and are not guaranteed
           # unique; the ident is, for as long as the thread is alive.
           "_tid": threading.get_ident(),
           # WHICH HALF OF THE CALL WE ARE IN. A call that has been open 20 minutes is
           # two completely different problems depending on this: "preparing" for that
           # long means we are the slow one (rendering pages, encoding, signing), "sent"
           # means the payload is gone and Bedrock has not answered. Flipped by note()
           # from the before-send hook; see aci.api_waiting_s in inflight().
           "aci.api_phase": "preparing", **fields}
    try:
        ctx = _ctx.get()
        for k in ("aci.section", "aci.label", "aci.product"):
            if ctx.get(k) is not None:
                rec.setdefault(f"aci.api_{k.split('.')[-1]}", ctx[k])
    except Exception:                                        # noqa: BLE001
        pass
    try:
        with _inflight_lock:
            _inflight[key] = rec
    except Exception:                                        # noqa: BLE001
        pass
    try:
        yield
    finally:
        # In a finally, because the calls worth watching are the ones that fail: an entry
        # left behind would make every later beat claim a call is open, which is a worse
        # lie than the silence it replaced.
        try:
            with _inflight_lock:
                _inflight.pop(key, None)
        except Exception:                                    # noqa: BLE001
            pass


def note(**fields) -> None:
    """Add fields to the API call THIS THREAD currently has open.

    The registry entry is created before the call is made, which is the only moment the
    caller controls -- but the facts worth knowing about an upload are not all available
    then. The serialized request body's true size exists only once botocore has signed and
    framed it, inside a `before-send` hook that runs on the calling thread and is handed no
    reference to anything we own. This is the seam between the two: the hook says "whatever
    call this thread has open, it weighs N bytes on the wire", and the heartbeat reads it
    off the registry with everything else.

    The INNERMOST open call on the thread (the highest key, since the keys are a counter)
    wins, which is the one a hook firing right now belongs to -- the same reason api_call
    keys by a counter and not by thread."""
    if not fields:
        return
    try:
        tid = threading.get_ident()
        with _inflight_lock:
            mine = [k for k, r in _inflight.items() if r.get("_tid") == tid]
            if mine:
                _inflight[max(mine)].update(fields)
    except Exception:                                        # noqa: BLE001
        pass


def inflight(now: float | None = None) -> dict:
    """What is open right now, as heartbeat fields: how many, and the OLDEST one.

    The oldest is the useful one. With six concurrent calls, five healthy ones finishing in
    40s and one that has been open for 20 minutes, a count alone says "busy" and the newest
    says "fine" -- only the oldest says which section to go and look at.

    The BYTES are totalled across all of them rather than taken from the oldest, because
    "how much is in flight" is a question about the whole pod and not about one call: six
    concurrent grounded sections at 15 MB each is 90 MB of page images on one node's
    uplink, and that is the number that explains a slow run when no single call looks
    unusual. Summed only over calls that reported a size, and omitted entirely when none
    did, so a text-only call site does not contribute a confident 0."""
    now = time.time() if now is None else now
    try:
        with _inflight_lock:
            recs = list(_inflight.values())
    except Exception:                                        # noqa: BLE001
        return {}
    if not recs:
        return {}
    oldest = min(recs, key=lambda r: r.get("started_at") or now)
    out = {k: v for k, v in oldest.items()
           if k not in ("started_at", "_tid", "_sent_at")}
    out["aci.api_inflight"] = len(recs)
    out["aci.api_oldest_s"] = round(now - (oldest.get("started_at") or now), 1)
    sizes = [r.get("aci.api_bytes") for r in recs]
    sizes = [n for n in sizes if isinstance(n, int)]
    if sizes:
        out["aci.api_inflight_bytes"] = sum(sizes)
        out["aci.api_inflight_mb"] = round(sum(sizes) / 1_000_000, 2)
    # HOW LONG THE OLDEST CALL HAS BEEN WAITING ON THE SERVER, as against how long it has
    # been open. The difference between the two is the time we spent building the request,
    # and keeping them as separate fields is what stops "the call has been open 20 minutes"
    # from being read as "Bedrock has been silent for 20 minutes" when the truth might be
    # that nineteen of them went on rendering page images.
    sent_at = oldest.get("_sent_at")
    if isinstance(sent_at, (int, float)):
        out["aci.api_waiting_s"] = round(now - sent_at, 1)
    # How many of the open calls have actually handed their payload over. With six
    # workers, "5 sent, 1 preparing" and "1 sent, 5 preparing" are different pods.
    out["aci.api_sent"] = sum(1 for r in recs if r.get("aci.api_phase") == "sent")
    return out


def install_crash_handlers() -> None:
    """Make a CRASH produce a log line, not just a traceback on stderr.

    Until this existed, an exception that escaped the per-document handler emitted nothing at
    all: Python prints the traceback to STDERR and exits, while every structured event this
    module writes goes to stdout. A pod log (which shows both streams) therefore had the
    reason and Kibana had silence -- the exact shape of "ReadTimeoutError is in ArgoCD but I
    cannot find it in Elasticsearch". The last event in the index was whatever the worker
    happened to be doing, so a crash read as a stall.

    Two hooks, because there are two ways to die:

      sys.excepthook        the main thread -- the process is going down
      threading.excepthook  a worker thread -- the PROCESS SURVIVES, and this is the worse
                            case: stage 4 fans sections over a ThreadPoolExecutor and the
                            heartbeat runs on its own thread, so a thread dying quietly leaves
                            a worker that looks alive and does less than it should

    Both chain to the previous hook afterwards, so stderr still gets its traceback and nothing
    that reads the pod log loses anything. SystemExit and KeyboardInterrupt are passed straight
    through: a clean exit is not a crash, and logging one would make every normal shutdown look
    like a failure.

    Idempotent, so a second call (a re-import, a test) does not chain the hooks twice and log
    the same crash two or three times."""
    global _crash_handlers_installed
    if _crash_handlers_installed or _FORMAT == "off":
        return
    _crash_handlers_installed = True

    prev_excepthook = sys.excepthook

    def _on_crash(exc_type, exc, tb):
        try:
            if not issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
                event("run.crash", level="error",
                      message=f"UNCAUGHT {exc_type.__name__}: {exc}",
                      **{"event.outcome": "failure",
                         "error.type": exc_type.__name__,
                         "error.message": str(exc),
                         "error.stack_trace": "".join(
                             traceback.format_exception(exc_type, exc, tb)),
                         # The one fact a stall and a crash do not share. Without it the
                         # difference has to be inferred from the absence of later events.
                         "aci.fatal": True})
        except Exception:                                    # noqa: BLE001
            pass
        try:
            prev_excepthook(exc_type, exc, tb)
        except Exception:                                    # noqa: BLE001
            pass

    sys.excepthook = _on_crash

    prev_threadhook = getattr(threading, "excepthook", None)

    def _on_thread_crash(args):
        try:
            if args.exc_type is not None and not issubclass(
                    args.exc_type, (SystemExit, KeyboardInterrupt)):
                event("thread.crash", level="error",
                      message=f"UNCAUGHT {args.exc_type.__name__} in "
                              f"{getattr(args.thread, 'name', '?')}: {args.exc_value}",
                      **{"event.outcome": "failure",
                         "error.type": args.exc_type.__name__,
                         "error.message": str(args.exc_value),
                         "error.stack_trace": "".join(traceback.format_exception(
                             args.exc_type, args.exc_value, args.exc_traceback)),
                         "aci.thread": getattr(args.thread, "name", None),
                         # NOT fatal, and that is the point: the process carries on with one
                         # less thread doing its job.
                         "aci.fatal": False})
        except Exception:                                    # noqa: BLE001
            pass
        try:
            if prev_threadhook is not None:
                prev_threadhook(args)
        except Exception:                                    # noqa: BLE001
            pass

    if prev_threadhook is not None:
        threading.excepthook = _on_thread_crash


def bind_service(name: str, **extra) -> None:
    """Bind who and where we are, from the environment the pod is already given.

    WHY aci.pod AND aci.node RATHER THAN host.name AND host.node
    ------------------------------------------------------------
    The cluster runs Filebeat 9 with `keys_under_root` + `overwrite_keys`, which merges our
    parsed JSON into the ROOT of the document -- so any key we emit that Filebeat also sets is
    a collision, and with overwrite_keys ours wins. Filebeat sets `host.name` to the NODE
    hostname; we would be overwriting that with the pod name and quietly destroying the only
    field that says which machine a log came from. (The same collision is visible unresolved
    in the cluster's own records: Filebeat's `ecs.version: 8.0.0` and a parsed line's
    `ecs.version: 1.6.0` end up as a two-valued field.)

    Filebeat already enriches every record with `kubernetes.pod.name`, `kubernetes.node.name`,
    `kubernetes.namespace` and `container.id`, so these two are belt-and-braces for the local
    and logfmt cases rather than the primary source -- which is exactly why they are cheap to
    move out of the way into our own namespace.

    `service.version` is kept as ECS: nothing else sets it for this container, and it is how
    you tell whether a fix is actually in the run you are looking at. It needs IMAGE_TAG from
    the deployment; absent, it is simply omitted and everything else still works."""
    bind(**{"service.name": name,
            "service.version": os.getenv("IMAGE_TAG") or os.getenv("ACI_IMAGE_TAG"),
            "service.environment": os.getenv("ACI_ENV") or os.getenv("ENVIRONMENT"),
            "aci.pod": os.getenv("HOSTNAME"),
            "aci.node": os.getenv("NODE_NAME"),
            "aci.pid": os.getpid(),
            "aci.python": sys.version.split()[0],
            **extra})


def flatten(prefix: str, d: dict | None, keep: int = 60) -> dict:
    """{"gpu": "Tesla T4"} -> {"aci.env.gpu": "Tesla T4"}.

    For the handful of dicts that ARE worth one field each -- the env fingerprint, a
    scorecard's dimensions -- as against the free-form dicts _clean deliberately stringifies.
    Bounded, because "one field each" stops being a good idea at a few dozen."""
    out: dict = {}
    try:
        for i, (k, v) in enumerate(sorted((d or {}).items())):
            if i >= keep:
                break
            key = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(k))
            out[f"{prefix}.{key}"] = v
    except Exception:                                        # noqa: BLE001
        pass
    return out
