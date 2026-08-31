"""Desired vs actual, as pure functions.

The Reconciler reads the cluster and writes only to Postgres -- status, events
and jobs. It never writes to the cluster, which is what makes invariant I5
structural rather than a rule someone has to remember. When it finds drift it
enqueues a job; the Provisioner, which has the write RBAC, does the re-apply.

(The architecture doc's section 3.3 pseudocode says "re-apply desired" at this
point. That was loose: re-applying needs patch permission, which would give the
Reconciler exactly the power I5 says it must not have.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from control.domain.states import Lane, Status

MANAGED_BY = "app.kubernetes.io/managed-by=keel"

#: Statuses the Reconciler is allowed to touch. Everything else -- requested,
#: validating, scheduling, loading -- belongs to the Provisioner mid-flight, and
#: a second writer would fight it.
RECONCILABLE = frozenset({Status.READY, Status.DEGRADED, Status.SCALED_TO_ZERO})


@dataclass(frozen=True, slots=True)
class Assessment:
    status: Status
    reason_code: str | None = None
    message: str | None = None
    enqueue: bool = False
    alert: bool = False
    drifted: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)


def _container(obj: dict) -> dict:
    containers = obj["spec"]["template"]["spec"].get("containers") or []
    for c in containers:
        if c.get("name") == "vllm":
            return c
    return containers[0] if containers else {}


def material(obj: dict, *, compare_replicas: bool = True) -> dict[str, Any]:
    """The fields we actually own.

    Deliberately NOT a deep comparison of the whole spec: the API server fills
    in dozens of defaults (terminationGracePeriodSeconds, dnsPolicy, imagePull
    policies, empty resource maps) that we never set, so a naive diff reports
    drift on every pass, forever.
    """
    pod = obj["spec"]["template"]["spec"]
    c = _container(obj)
    out: dict[str, Any] = {
        "image": c.get("image"),
        "args": tuple(c.get("args") or ()),
        "gpu": ((c.get("resources") or {}).get("limits") or {}).get("nvidia.com/gpu"),
        "nodeSelector": tuple(sorted((pod.get("nodeSelector") or {}).items())),
    }
    if compare_replicas:
        out["replicas"] = obj["spec"].get("replicas")
    return out


def compare_replicas_for(lane: Lane) -> bool:
    """Lane C's replica count belongs to KEDA, which scales it between 0 and max
    as traffic moves. Comparing it against the rendered value would report drift
    every time the autoscaler did its job."""
    return lane is not Lane.C


def drift(desired: dict, actual: dict) -> tuple[str, ...]:
    return tuple(sorted(k for k in desired if desired[k] != actual.get(k)))


def assess(row: dict[str, Any], live: dict | None, rendered: dict | None) -> Assessment:
    """Decide what one deployment's status should be.

    Order matters: existence, then availability, then drift. A workload that is
    gone cannot be meaningfully compared, and one that is down should be
    reported as down rather than as configuration drift.
    """
    status = Status(row["status"])
    lane = Lane(row["lane"])

    if live is None:
        return Assessment(
            Status.DEGRADED,
            "workload_vanished",
            "the Deployment is gone from the cluster",
            enqueue=True,
            alert=True,
        )

    spec_replicas = live["spec"].get("replicas") or 0
    ready_replicas = (live.get("status") or {}).get("readyReplicas") or 0

    if ready_replicas == 0:
        if lane is Lane.C and spec_replicas == 0:
            # Expected steady state, not a fault. Reporting this as degraded
            # gives a permanently alarming dashboard; reporting a genuine lane B
            # outage as idle hides a real one.
            return Assessment(Status.SCALED_TO_ZERO)
        return Assessment(
            Status.DEGRADED,
            "no_ready_replicas",
            f"{ready_replicas} of {spec_replicas} replicas ready",
            enqueue=False,  # the pods exist; restarting them is not obviously right
            alert=True,
        )

    if rendered is not None:
        cmp_replicas = compare_replicas_for(lane)
        want = material(rendered, compare_replicas=cmp_replicas)
        got = material(live, compare_replicas=cmp_replicas)
        if fields := drift(want, got):
            return Assessment(
                Status.UPDATING,
                "drift",
                f"cluster differs from desired state in: {', '.join(fields)}",
                enqueue=True,
                drifted=fields,
                detail={"fields": {k: {"want": want[k], "got": got.get(k)} for k in fields}},
            )

    if status is not Status.READY:
        return Assessment(Status.READY, message="recovered")
    return Assessment(Status.READY)


def orphans(live_objects: list[dict], known_ids: set[str]) -> list[dict]:
    """Objects labelled ours with no matching row.

    Alerted on, never deleted -- the Reconciler cannot delete even by mistake.
    Auto-deletion on a bad selector is how a platform destroys a team's
    production endpoint.
    """
    found = []
    for obj in live_objects:
        dep_id = (obj["metadata"].get("labels") or {}).get("keel.io/deployment-id")
        if dep_id and dep_id not in known_ids:
            found.append(obj)
    return found
