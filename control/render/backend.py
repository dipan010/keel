"""Which renderer produces the workload, and what follows from that.

SPIKE (ADR-0008). Two backends exist only on the spike branch, to compare them
against the same tests. If KServe wins, the manifest path is deleted rather than
kept alongside -- parallel paths rot, and this project has already argued that.
"""

from __future__ import annotations

import os
from typing import Any

from control import k8s
from control.render import kserve, manifests

BACKEND = os.environ.get("KEEL_RENDER_BACKEND", "manifests")
if BACKEND not in ("manifests", "kserve"):
    raise ValueError(f"KEEL_RENDER_BACKEND must be manifests or kserve, not {BACKEND!r}")


def render(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return kserve.render(spec) if BACKEND == "kserve" else manifests.render(spec)


def workload_kind() -> str:
    """The object Keel owns and compares. For KServe that is the
    InferenceService -- never the Deployment KServe generates from it."""
    return "InferenceService" if BACKEND == "kserve" else "Deployment"


def teardown_kinds() -> tuple[str, ...]:
    if BACKEND == "kserve":
        # KServe garbage-collects the Deployment, Service and pods it created.
        return ("InferenceService",)
    return ("ScaledObject", "Service", "Deployment")


async def upstream(cluster: k8s.Cluster, dep: dict[str, Any], ns: str) -> str:
    """Where the gateway should send traffic.

    For KServe this is READ from the address KServe reports, not rebuilt.
    KServe names the Service `{name}-predictor` and listens on 80, forwarding
    to the container's 8000 -- so the manifest path's `{name}...:8000` would
    have pointed the gateway at a Service that does not exist.
    """
    if BACKEND != "kserve":
        return f"http://{dep['k8s_object_name']}.{ns}.svc.cluster.local:8000"
    isvc = await cluster.get("InferenceService", dep["k8s_object_name"], ns)
    url = ((isvc or {}).get("status", {}).get("address") or {}).get("url")
    if not url:
        raise RuntimeError(
            f"InferenceService {dep['k8s_object_name']} is ready but reports no address"
        )
    return url


async def reconcile_failure(
    cluster: k8s.Cluster, dep: dict[str, Any], ns: str
) -> tuple[str, str] | None:
    """A KServe resource that can never produce pods.

    Found on the spike's first attempt: KServe rejected the resource outright
    (a generated hostname over DNS's 63-character limit) and created nothing.
    With no pods, the pod classifier reads that as `scheduling` and would wait
    out the full 30-minute timeout. KServe says why immediately, so ask it.
    """
    if BACKEND != "kserve":
        return None
    isvc = await cluster.get("InferenceService", dep["k8s_object_name"], ns)
    for cond in (isvc or {}).get("status", {}).get("conditions") or []:
        if cond.get("type") == "Ready" and cond.get("reason") == "ReconcileFailed":
            return "kserve_rejected", cond.get("message") or "KServe could not reconcile"
    return None
