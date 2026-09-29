"""Run observability: structured events on stdout, which the cluster already ships to Kibana.

    from aosphere_core_index.obs import log, probe

    log.bind(**{"aci.run_id": run, "aci.shard": shard})
    log.event("run.start", **{"aci.jobs": 112})
    with probe.Heartbeat(watch=dest):
        ...
"""
from . import log, probe  # noqa: F401
