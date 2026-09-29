"""aosphere-core-index — a lean, quality-first GraphIndex.

Step 1 scope: extract documents into markdown structure, chunk along that
structure, build a linked node graph (no embeddings yet), and provide a
navigator to see how the graph would be traversed.
"""

import os as _os

__version__ = "0.1.0"


def _effective_cpus() -> int:
    """Cores actually available to THIS process — the cgroup CPU quota in a
    container (the k8s CPU limit), not the node's core count. In a pod
    os.cpu_count() reports the whole node, so BLAS/ONNX spin up far more threads
    than the limit allows and thrash under CFS throttling — the classic
    "fast locally, slow in Kubernetes" cause."""
    try:
        if _os.path.exists("/sys/fs/cgroup/cpu.max"):  # cgroup v2
            quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
            if quota != "max":
                return max(1, round(int(quota) / int(period)))
        q, p = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
        if _os.path.exists(q) and _os.path.exists(p):  # cgroup v1
            quota, period = int(open(q).read()), int(open(p).read())
            if quota > 0 and period > 0:
                return max(1, round(quota / period))
    except Exception:
        pass
    return _os.cpu_count() or 1


# Bound math-library thread pools to the cgroup quota BEFORE numpy / onnxruntime
# import (must precede import to take effect). Explicit env always wins.
_cpus = str(_effective_cpus())
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    _os.environ.setdefault(_var, _cpus)
