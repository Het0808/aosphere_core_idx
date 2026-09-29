"""What the worker is actually doing right now — sampled once a minute for the heartbeat.

THE PROBLEM THIS EXISTS FOR

A worker legitimately spends 20+ minutes inside one MinerU subprocess, and 90 minutes is a
legitimate worst case for one Bedrock call on the summary route (read_timeout=900 x 6 adaptive
attempts). Neither prints anything while it runs. So a healthy run and a wedged one produce
the identical observable: silence. That is why Canada and New Zealand could hang with nothing
to look at afterwards.

A heartbeat alone only narrows it to "still alive". What separates *working* from *wedged* is
whether anything is CHANGING, so every beat samples the things that move when work is
happening and stop when it is not:

    aci.out_bytes         the job directory's size. Stage 2 writes page images and crops
                          continuously, so a MinerU that is working grows it every minute.
    aci.out_bytes_delta   growth since the previous beat. Zero is the interesting value.
    aci.stalled_beats     consecutive beats with no growth. THIS is the stuck detector:
                          alert on it rather than on elapsed time, because elapsed time is
                          indistinguishable between a 165-page document and a hang.
    aci.child_*           the live subprocess and its state. D (uninterruptible sleep) means
                          blocked on I/O; Z means MinerU died and was never reaped; no child
                          at all during stage2_mineru means the stall is in our own code.
    aci.cpu_pct           a wedged process pinned at 0% is a deadlock or a network wait; one
                          at 100% with no output growth is a runaway loop. Different bugs.
    aci.gpu_*             a stage2 with 0 MiB of VRAM in use is not running on the GPU at all.
    aci.*_free_gb         the 60Gi ephemeral-storage limit filling looks exactly like a hang
                          from the outside, and is one of the few causes that is instantly
                          actionable.

Every probe is individually guarded and every one of them is optional, for the reason
env_fingerprint already gives: a diagnostic that can fail the run it is diagnosing is worse
than no diagnostic.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from . import log

# psutil is a TRANSITIVE dependency here, not a declared one: it is in uv.lock but not in
# the requirements.txt the worker image installs from. So on the cluster it is absent, and
# every field it would have provided -- rss, cpu, the live child process -- is exactly the
# set that separates "blocked on I/O" from "runaway loop" from "the stall is in our own
# code". Rather than add a dependency to a 7 GiB GPU image for a diagnostic, the fallbacks
# below read /proc directly. Linux-only, which is where the pods are; on a laptop without
# /proc psutil covers it, and if neither is available the beat simply carries fewer fields.
try:
    import psutil                                            # type: ignore
except Exception:                                            # noqa: BLE001
    psutil = None                                            # type: ignore

_PROC = Path("/proc")
# (cpu_seconds, wall_clock) at the previous sample, for the /proc cpu_pct fallback: CPU time
# is cumulative, so a percentage only exists between two readings.
_last_cpu: tuple[float, float] | None = None


def _proc_self() -> dict:
    """rss / threads / open fds / cpu% for THIS process, from /proc. Linux only."""
    out: dict = {}
    try:
        rss_pages = int((_PROC / "self/statm").read_text().split()[1])
        out["aci.rss_mb"] = round(rss_pages * os.sysconf("SC_PAGE_SIZE") / 1e6, 1)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        for line in (_PROC / "self/status").read_text().splitlines():
            if line.startswith("Threads:"):
                out["aci.threads"] = int(line.split()[1])
                break
    except Exception:                                        # noqa: BLE001
        pass
    try:
        out["aci.open_fds"] = len(os.listdir(_PROC / "self/fd"))
    except Exception:                                        # noqa: BLE001
        pass
    try:
        global _last_cpu
        # Fields 14/15 (1-indexed) of /proc/self/stat are utime/stime in clock ticks. comm
        # can contain spaces and parentheses, so the split starts after the LAST ')'.
        raw = (_PROC / "self/stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu_s = (int(fields[11]) + int(fields[12])) / ticks
        now = time.time()
        if _last_cpu is not None:
            d_cpu, d_wall = cpu_s - _last_cpu[0], now - _last_cpu[1]
            if d_wall > 0:
                out["aci.cpu_pct"] = round(100 * d_cpu / d_wall, 1)
        _last_cpu = (cpu_s, now)
        out["aci.cpu_s"] = round(cpu_s, 1)
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _proc_children() -> dict:
    """The longest-running descendant, from /proc. See children() for why the oldest."""
    try:
        me = os.getpid()
        by_ppid: dict[int, list[int]] = {}
        stats: dict[int, tuple[str, int, float, float]] = {}
        ticks = os.sysconf("SC_CLK_TCK")
        for entry in os.listdir(_PROC):
            if not entry.isdigit():
                continue
            try:
                raw = (_PROC / entry / "stat").read_text()
                fields = raw[raw.rindex(")") + 2:].split()
                pid = int(entry)
                state, ppid = fields[0], int(fields[1])
                # starttime (field 22, 1-indexed) is in ticks since boot; cpu is 14+15.
                stats[pid] = (state, ppid, int(fields[19]) / ticks,
                              (int(fields[11]) + int(fields[12])) / ticks)
                by_ppid.setdefault(ppid, []).append(pid)
            except Exception:                                # noqa: BLE001
                continue
        kids, stack = [], list(by_ppid.get(me, ()))
        while stack:
            pid = stack.pop()
            kids.append(pid)
            stack.extend(by_ppid.get(pid, ()))
        if not kids:
            return {"aci.child_count": 0}
        oldest = min(kids, key=lambda p: stats[p][2])         # smallest starttime = first born
        state, _ppid, start_ticks, cpu_s = stats[oldest]
        try:
            cmd = (_PROC / str(oldest) / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()
        except Exception:                                    # noqa: BLE001
            cmd = ""
        out = {"aci.child_count": len(kids), "aci.child_pid": oldest,
               "aci.child_status": state, "aci.child_cpu_s": round(cpu_s, 1)}
        if cmd:
            out["aci.child_cmd"] = cmd[:log.MAX_STR]
        try:
            rss_pages = int((_PROC / str(oldest) / "statm").read_text().split()[1])
            out["aci.child_rss_mb"] = round(rss_pages * os.sysconf("SC_PAGE_SIZE") / 1e6, 1)
        except Exception:                                    # noqa: BLE001
            pass
        try:
            with open(_PROC / "uptime") as fh:
                out["aci.child_age_s"] = round(float(fh.read().split()[0]) - start_ticks, 1)
        except Exception:                                    # noqa: BLE001
            pass
        return out
    except Exception:                                        # noqa: BLE001
        return {}

# nvidia-smi is absent on a laptop and on any CPU node. Probe once, then stop paying for a
# subprocess every minute to be told the same thing.
_GPU_OK: bool | None = None


def _free_gb(path: str) -> float | None:
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / 1e9, 2)
    except Exception:                                        # noqa: BLE001
        return None


def dir_bytes(path: Path | str, cap: int = 200_000) -> int | None:
    """Total size under `path`. Capped at `cap` entries so a beat cannot cost seconds.

    The absolute number matters less than whether it moved; a cap that makes the reading
    consistent-but-partial is fine for that, and it is the reason this is safe to call on a
    directory holding thousands of page images."""
    try:
        total, seen = 0, 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.stat(os.path.join(root, f)).st_size
                except OSError:
                    pass
                seen += 1
                if seen >= cap:
                    return total
        return total
    except Exception:                                        # noqa: BLE001
        return None


def gpu() -> dict:
    """VRAM in use and utilisation, from nvidia-smi."""
    global _GPU_OK
    if _GPU_OK is False:
        return {}
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            _GPU_OK = False
            return {}
        used, total, util, temp = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
        _GPU_OK = True
        return {"aci.gpu_mem_used_mb": int(used), "aci.gpu_mem_total_mb": int(total),
                "aci.gpu_util_pct": int(util), "aci.gpu_temp_c": int(temp)}
    except Exception:                                        # noqa: BLE001
        _GPU_OK = False
        return {}


def children() -> dict:
    """The live descendant doing the work — MinerU, or whatever our code spawned.

    The LONGEST-RUNNING descendant, not the newest: MinerU spawns short-lived helpers, and the
    one worth naming in a stall is the one that has been there the whole time."""
    if psutil is None:
        return _proc_children()
    try:
        me = psutil.Process()
        kids = me.children(recursive=True)
        if not kids:
            return {"aci.child_count": 0}
        now = time.time()
        oldest = max(kids, key=lambda p: now - (p.create_time() or now))
        with oldest.oneshot():
            cmd = " ".join(oldest.cmdline() or [oldest.name()])
            return {"aci.child_count": len(kids),
                    "aci.child_pid": oldest.pid,
                    "aci.child_cmd": cmd[:log.MAX_STR],
                    # R running · S sleeping · D uninterruptible (blocked on I/O, often a
                    # hung mount or a stuck GPU) · Z zombie (died, never reaped).
                    "aci.child_status": oldest.status(),
                    "aci.child_age_s": round(now - oldest.create_time(), 1),
                    "aci.child_cpu_s": round(sum(oldest.cpu_times()[:2]), 1),
                    "aci.child_rss_mb": round(oldest.memory_info().rss / 1e6, 1)}
    except Exception:                                        # noqa: BLE001
        return {}


def resources(watch: Path | str | None = None, scratch: str = "/work") -> dict:
    """One sample of everything that moves. Flat aci.* keys, ready to merge into an event."""
    out: dict = {}
    try:
        if psutil is None:
            out.update(_proc_self())
        else:
            me = psutil.Process()
            with me.oneshot():
                out["aci.rss_mb"] = round(me.memory_info().rss / 1e6, 1)
                # interval=None is the non-blocking form: the percentage is measured against
                # the previous call, which on a 60s beat is exactly the window we want and
                # costs nothing. A blocking call would hold the beat open.
                out["aci.cpu_pct"] = round(me.cpu_percent(interval=None), 1)
                out["aci.threads"] = me.num_threads()
                try:
                    out["aci.open_fds"] = me.num_fds()
                except Exception:                            # noqa: BLE001
                    pass
        out["aci.load1"] = round(os.getloadavg()[0], 2)
    except Exception:                                        # noqa: BLE001
        pass
    for label, path in (("scratch", scratch if Path(scratch).exists() else "."),
                        ("dev_shm", "/dev/shm")):
        if Path(path).exists():
            g = _free_gb(path)
            if g is not None:
                out[f"aci.{label}_free_gb"] = g
    out.update(gpu())
    out.update(children())
    if watch:
        b = dir_bytes(watch)
        if b is not None:
            out["aci.out_bytes"] = b
    return out


class Heartbeat:
    """Emit `stage.heartbeat` every `interval` seconds until stopped.

    Context comes from log.snapshot() rather than the contextvar: a new thread starts with an
    empty context, so the beat would otherwise carry no run, product, document or stage — the
    four things it exists to report. See log._mirror.

    `also` is called on each beat for anything the caller wants folded in (the worker passes
    its ShardProgress publish, so the S3 summary and the Kibana beat stay in step and there is
    still only one timer thread)."""

    def __init__(self, interval: float = 60.0, watch: Path | str | None = None,
                 scratch: str = "/work", also=None) -> None:
        self.interval = interval
        self.watch = watch
        self.scratch = scratch
        self.also = also
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev_bytes: int | None = None
        self._stalled = 0
        self._beats = 0

    def start(self) -> "Heartbeat":
        if not log.enabled():
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="aci-heartbeat")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._beat()
            except Exception:                                # noqa: BLE001
                pass
            if self.also is not None:
                try:
                    self.also()
                except Exception:                            # noqa: BLE001
                    pass

    def _beat(self) -> None:
        self._beats += 1
        ctx = log.snapshot()
        fields = resources(self.watch, self.scratch)
        now = time.time()
        for key, src in (("aci.doc_elapsed_s", "aci.doc_started_at"),
                         ("aci.stage_elapsed_s", "aci.stage_started_at")):
            started = ctx.get(src)
            if isinstance(started, (int, float)):
                fields[key] = round(now - started, 1)
        # WHAT IS OPEN RIGHT NOW: how many API calls, and the oldest of them. Absent when
        # none is open — so a beat either names a live call and its age, or says nothing and
        # the time is being spent elsewhere. That is the whole question behind "is the
        # connection still there": an age that climbs beat by beat is a call still open, and
        # beats stopping altogether is the worker itself going away.
        fields.update(log.inflight(now))
        cur = fields.get("aci.out_bytes")
        if cur is not None and self._prev_bytes is not None:
            delta = cur - self._prev_bytes
            fields["aci.out_bytes_delta"] = delta
            self._stalled = self._stalled + 1 if delta <= 0 else 0
            fields["aci.stalled_beats"] = self._stalled
        if cur is not None:
            self._prev_bytes = cur
        fields["aci.beat"] = self._beats
        # A stall is not yet a failure -- a document really can spend 10 minutes in one Bedrock
        # call -- so it is a warning, not an error. The point is that it is greppable: one
        # Kibana filter on log.level:warn finds every wedged document in a 17-hour run.
        level = "warn" if self._stalled >= 5 else "info"
        # A stage with an API call open is not the same silence as a stage with none, and
        # the message is what a person reads first — so it names the call rather than leaving
        # "still in stage4_ai" to mean both.
        # Named, counted and aged: with six concurrent calls a count alone says "busy" and
        # the newest says "fine" — only the oldest says which section to go and look at.
        n = fields.get("aci.api_inflight")
        api = ""
        if n:
            # THE PHASE, and the megabytes, in the line a person reads first. "6 calls
            # open, oldest 1200s" is the same sentence whether the pod is still encoding
            # page images or whether it sent 90 MB twenty minutes ago and Bedrock has
            # said nothing — and only the second one is Bedrock's problem. aci.api_phase
            # and aci.api_waiting_s are what separate them; the totals say how much data
            # is riding on the answer.
            mb = fields.get("aci.api_inflight_mb")
            waiting = fields.get("aci.api_waiting_s")
            api = (f", {n} {fields.get('aci.api')} call(s) open"
                   + (f" ({fields.get('aci.api_sent')} sent)"
                      if fields.get("aci.api_sent") is not None else "")
                   + (f", {mb} MB in flight" if mb else "")
                   + f", oldest {fields.get('aci.api_oldest_s')}s"
                   + (f" ({fields.get('aci.api_phase')}"
                      + (f", {waiting}s awaiting response" if waiting is not None else "")
                      + ")" if fields.get("aci.api_phase") else "")
                   + (f" ({fields['aci.api_section']})" if fields.get("aci.api_section") else ""))
        log.event("stage.heartbeat", level=level,
                  message=(f"still in {ctx.get('aci.stage')} "
                           f"({fields.get('aci.stage_elapsed_s', '?')}s"
                           + (f", NO OUTPUT for {self._stalled} beats" if self._stalled else "")
                           + ")" + api),
                  **{**ctx, **fields})
