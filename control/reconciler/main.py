"""Desired state (database) vs actual state (cluster), on a timer.

Reads the cluster, writes only to Postgres. Its ServiceAccount is
get/list/watch only, so invariant I5 -- orphans are alerted on, never deleted --
is structural rather than a rule someone has to remember. Drift is fixed by
enqueuing a job for the Provisioner, which is the component that holds write
permission.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import structlog

from control import db, k8s, repo
from control.domain.rules import namespace
from control.domain.states import Mode, Status
from control.reconciler.diff import RECONCILABLE, Assessment, assess, orphans
from control.render import manifests

log = structlog.get_logger()


def _stale(queued_at: datetime | None) -> bool:
    if queued_at is None:
        return False
    return (datetime.now(UTC) - queued_at).total_seconds() > STALE_JOB_SECONDS


ACTOR = "reconciler"
INTERVAL = float(os.environ.get("KEEL_RECONCILE_INTERVAL", "60"))
#: A deployment with an open job is skipped, because the Provisioner owns
#: it mid-flight. So a job nothing will ever claim disables reconciliation
#: for that deployment silently and indefinitely -- the failure mode with
#: no symptom. Past this age, say so.
STALE_JOB_SECONDS = float(os.environ.get("KEEL_STALE_JOB_SECONDS", "3600"))

DESIRED = """
select d.*, t.slug as team_slug, c.weights_uri, c.model_ref,
       c.engine_args as catalog_engine_args,
       exists (select 1 from jobs j where j.deployment_id = d.id) as has_job,
       (select min(j.created_at) from jobs j where j.deployment_id = d.id)
         as oldest_job_at
  from deployments d
  join teams t on t.id = d.team_id
  join catalog_models c on c.id = d.model_id
 where d.status <> 'deleted'
"""


def _rendered(row: dict[str, Any]) -> dict | None:
    """The Deployment we would produce for this row right now."""
    if Mode(row["mode"]) is not Mode.SELF_HOSTED:
        return None
    spec = {
        **row,
        "id": str(row["id"]),
        "engine_args": row["engine_args"] or row["catalog_engine_args"] or {},
    }
    for obj in manifests.render(spec):
        if obj["kind"] == "Deployment":
            return obj
    return None


async def _apply(row: dict[str, Any], a: Assessment) -> None:
    current = Status(row["status"])
    dep_id = str(row["id"])

    if a.alert:
        # Where a real alerting path exists this becomes a page. Logged at
        # warning for now so it is at least visible and greppable.
        log.warning("reconcile.alert", id=dep_id, reason=a.reason_code, msg=a.message, **a.detail)

    async with db.transaction() as conn:
        if a.status is not current:
            await repo.record_transition(
                conn,
                dep_id,
                current,
                a.status,
                ACTOR,
                reason_code=a.reason_code,
                detail=a.detail or None,
            )
            log.info("reconcile.transition", id=dep_id, frm=str(current), to=str(a.status))
        else:
            await conn.execute(
                "update deployments set last_reconciled_at = now() where id = %s", (dep_id,)
            )

        if a.enqueue:
            # The Provisioner owns cluster writes. We only ask.
            await conn.execute(
                """insert into jobs (deployment_id, kind)
                   select %s, 'update'
                    where not exists (select 1 from jobs where deployment_id = %s)""",
                (dep_id, dep_id),
            )


async def reconcile_once(cluster: k8s.Cluster) -> dict[str, int]:
    counts = {"checked": 0, "skipped": 0, "changed": 0, "orphans": 0, "stale_jobs": 0}

    async with db.pool().connection() as conn:
        cur = await conn.execute(DESIRED)
        desired = await cur.fetchall()

    live = await cluster._call(
        "/apis/apps/v1/deployments",
        "GET",
        query=[("labelSelector", "app.kubernetes.io/managed-by=keel")],
    )
    by_id: dict[str, dict] = {}
    for obj in live.get("items", []):
        if dep_id := (obj["metadata"].get("labels") or {}).get("keel.io/deployment-id"):
            by_id[dep_id] = obj

    known: set[str] = set()
    for row in desired:
        dep_id = str(row["id"])
        known.add(dep_id)

        # A deployment the Provisioner is mid-flight on is not ours to judge.
        if row["has_job"] or Status(row["status"]) not in RECONCILABLE:
            counts["skipped"] += 1
            if row["has_job"] and _stale(row["oldest_job_at"]):
                counts["stale_jobs"] += 1
                log.warning(
                    "reconcile.stale_job",
                    id=dep_id,
                    queued_at=str(row["oldest_job_at"]),
                    detail="this deployment has not been reconciled since; "
                    "is the Provisioner running?",
                )
            continue

        counts["checked"] += 1
        a = assess(row, by_id.get(dep_id), _rendered(row))

        if a.status is Status.DEGRADED and a.reason_code == "no_ready_replicas":
            ns = namespace(row["team_slug"])
            pods = await cluster.pods_for(ns, dep_id)
            if pods:
                tail = await cluster.pod_logs(ns, pods[0]["metadata"]["name"])
                a = Assessment(**{**a.__dict__, "detail": {**a.detail, "logs": tail}})

        if a.status is not Status(row["status"]) or a.enqueue:
            counts["changed"] += 1
        await _apply(row, a)

    for obj in orphans(list(by_id.values()), known):
        counts["orphans"] += 1
        log.warning(
            "reconcile.orphan",
            namespace=obj["metadata"]["namespace"],
            name=obj["metadata"]["name"],
            deployment_id=(obj["metadata"].get("labels") or {}).get("keel.io/deployment-id"),
            action="none -- a human decides (invariant I5)",
        )

    return counts


async def loop() -> None:
    await db.open_pool()
    await k8s.load()
    cluster = k8s.Cluster()
    log.info("reconciler.start", interval=INTERVAL)
    try:
        while True:
            try:
                counts = await reconcile_once(cluster)
                if counts["changed"] or counts["orphans"] or counts["stale_jobs"]:
                    log.info("reconcile.pass", **counts)
            except Exception:
                # A reconcile pass must never take the loop down: the next pass
                # sees the same world and tries again.
                log.exception("reconcile.failed")
            await asyncio.sleep(INTERVAL)
    finally:
        await cluster.close()
        await db.close_pool()


def run() -> None:
    asyncio.run(loop())


if __name__ == "__main__":
    run()
