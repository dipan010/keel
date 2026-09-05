"""Queue worker: render manifests, server-side apply, wait, smoke-test, ready.

Separate from the API because it runs for minutes and must survive restarts.
Stateless -- everything it knows is in Postgres. See section 3.2 of the
architecture doc for the ladder this implements.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import structlog

from control import db, gateway, k8s, repo
from control.domain.rules import namespace
from control.domain.states import Lane, Mode, Status
from control.provisioner.status import Phase, classify, classify_log
from control.render import manifests
from control.render import namespace as ns_render

log = structlog.get_logger()

ACTOR = "provisioner"
POLL_INTERVAL = float(os.environ.get("KEEL_POLL_INTERVAL", "2"))
WATCH_INTERVAL = float(os.environ.get("KEEL_WATCH_INTERVAL", "3"))
#: Past this, something is wrong that waiting will not fix. Leave the pod in
#: place for inspection rather than tidying away the evidence.
READY_TIMEOUT = float(os.environ.get("KEEL_READY_TIMEOUT", "1800"))
SMOKE_PROMPT = "Reply with the single word: ok"


class Failed(Exception):
    def __init__(self, reason_code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.reason_code, self.message, self.detail = reason_code, message, detail or {}


async def _load(conn: Any, deployment_id: str) -> dict[str, Any]:
    cur = await conn.execute(
        """select d.*, t.slug as team_slug, t.gpu_quota, c.weights_uri, c.engine_args
                    as catalog_engine_args
             from deployments d
             join teams t on t.id = d.team_id
             join catalog_models c on c.id = d.model_id
            where d.id = %s""",
        (deployment_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise Failed("missing_deployment", f"deployment {deployment_id} vanished")
    return row


async def _transition(
    deployment_id: str,
    frm: Status | None,
    to: Status,
    reason_code: str | None = None,
    detail: dict | None = None,
) -> None:
    async with db.transaction() as conn:
        await repo.record_transition(
            conn, deployment_id, frm, to, ACTOR, reason_code=reason_code, detail=detail
        )


async def _wait_for_ready(cluster: k8s.Cluster, dep: dict, started: float) -> None:
    """Drive scheduling -> loading -> ready, transitioning as the pods move."""
    ns = namespace(dep["team_slug"])
    current = Status(dep["status"])

    while True:
        elapsed = time.monotonic() - started
        if elapsed > READY_TIMEOUT:
            raise Failed(
                "timeout",
                f"still not ready after {READY_TIMEOUT:.0f}s; pods left in place for inspection",
                {"namespace": ns},
            )

        pods = await cluster.pods_for(ns, str(dep["id"]))
        outcome = classify(pods, elapsed_seconds=elapsed)

        if outcome.phase is Phase.FAILED:
            detail = dict(outcome.detail)
            reason, message = outcome.reason_code or "failed", outcome.message or ""
            if pod := detail.get("pod"):
                # The log tail is what makes the event actionable -- vLLM says
                # precisely what it objected to.
                tail = await cluster.pod_logs(ns, pod)
                detail["logs"] = tail
                # "crashloop" is accurate and useless. If the log names the real
                # problem, report that instead.
                if precise := classify_log(tail):
                    reason, message = precise
            raise Failed(reason, message, detail)

        if outcome.phase is Phase.READY:
            return

        if outcome.status != current:
            await _transition(str(dep["id"]), current, outcome.status)
            log.info("provision.phase", id=str(dep["id"]), phase=outcome.phase)
            current = outcome.status

        await asyncio.sleep(WATCH_INTERVAL)


async def provision(cluster: k8s.Cluster, gw: gateway.Gateway, deployment_id: str) -> None:
    started = time.monotonic()
    async with db.pool().connection() as conn:
        dep = await _load(conn, deployment_id)

    dep_id = str(dep["id"])
    ns = namespace(dep["team_slug"])
    self_hosted = Mode(dep["mode"]) is Mode.SELF_HOSTED

    await _transition(dep_id, Status(dep["status"]), Status.VALIDATING)

    if self_hosted:
        # 04 -- namespace, quota, default-deny NetworkPolicy
        for obj in ns_render.render(dep["team_slug"], dep["gpu_quota"]):
            await cluster.apply(obj)

        # 05 -- render and server-side apply. Object names derive from the
        # deployment id, so a redelivered job converges instead of duplicating.
        spec = {
            **dep,
            "id": dep_id,
            "engine_args": dep["engine_args"] or dep["catalog_engine_args"] or {},
        }
        for obj in manifests.render(spec):
            await cluster.apply(obj)
        log.info("provision.applied", id=dep_id, namespace=ns)

        await _transition(dep_id, Status.VALIDATING, Status.SCHEDULING)

        if Lane(dep["lane"]) is Lane.C:
            # Starts at zero replicas by design; there is nothing to wait for.
            log.info("provision.lane_c_idle", id=dep_id)
        else:
            # 06/07/08 -- scheduling, loading, ready
            await _wait_for_ready(cluster, {**dep, "status": Status.SCHEDULING}, started)

        upstream = f"http://{dep['k8s_object_name']}.{ns}.svc.cluster.local:8000"
        model = "/models/weights"
    else:
        upstream, model = "", dep["upstream_model"]

    # 09 -- register the route
    await gw.register(dep["route_name"], upstream, model)

    # 10 -- smoke-test THROUGH THE GATEWAY, not against the pod
    if Lane(dep["lane"]) is not Lane.C:
        try:
            reply = await gw.completion(dep["route_name"], SMOKE_PROMPT)
            log.info("provision.smoke_ok", id=dep_id, reply=reply[:60])
        except RuntimeError as exc:
            # No gateway configured. Not a pass -- say so loudly.
            log.warning("provision.smoke_skipped", id=dep_id, why=str(exc))
        except Exception as exc:
            raise Failed(
                "smoke_failed",
                f"endpoint came up but did not answer through the gateway: {exc}",
                {"route": dep["route_name"]},
            ) from exc

    # 11 -- ready
    await _transition(dep_id, None if not self_hosted else Status.LOADING, Status.READY)
    log.info(
        "provision.ready",
        id=dep_id,
        route=dep["route_name"],
        secs=round(time.monotonic() - started, 1),
    )


async def teardown(cluster: k8s.Cluster, gw: gateway.Gateway, deployment_id: str) -> None:
    async with db.pool().connection() as conn:
        dep = await _load(conn, deployment_id)
    ns = namespace(dep["team_slug"])

    # Revoke issued keys before removing anything else. A key that outlives its
    # deployment is a live credential pointing at nothing -- and if the route is
    # ever reused, at something it was never meant to reach.
    async with db.pool().connection() as conn:
        keys = await repo.active_keys(conn, deployment_id)
    for k in keys:
        await gw.revoke_key(k["alias"])
    if keys:
        async with db.transaction() as conn:
            for k in keys:
                await repo.mark_key_revoked(conn, k["id"], ACTOR)
        log.info("teardown.keys_revoked", id=deployment_id, count=len(keys))

    await gw.deregister(dep["route_name"])
    if Mode(dep["mode"]) is Mode.SELF_HOSTED and dep["k8s_object_name"]:
        # delete() already treats 404 as success, which covers both "already
        # gone" and "this kind is not installed". Anything else -- a permissions
        # error, an unreachable API server -- must surface, so the job is
        # retried rather than the deployment being marked deleted while its
        # workload keeps running and billing.
        for kind in ("ScaledObject", "Service", "Deployment"):
            await cluster.delete(kind, dep["k8s_object_name"], ns)
    await _transition(str(dep["id"]), Status.DELETING, Status.DELETED)


HANDLERS = {"provision": provision, "update": provision, "delete": teardown}


async def run_job(cluster: k8s.Cluster, gw: gateway.Gateway, job: dict) -> None:
    dep_id = str(job["deployment_id"])
    try:
        await HANDLERS[job["kind"]](cluster, gw, dep_id)
    except Failed as exc:
        log.warning("provision.failed", id=dep_id, reason=exc.reason_code, msg=exc.message)
        async with db.transaction() as conn:
            cur = await conn.execute("select status from deployments where id = %s", (dep_id,))
            row = await cur.fetchone()
            await repo.record_transition(
                conn,
                dep_id,
                Status(row["status"]) if row else None,
                Status.FAILED,
                ACTOR,
                reason_code=exc.reason_code,
                detail={"message": exc.message, **exc.detail},
            )
        async with db.transaction() as conn:
            await conn.execute("delete from jobs where id = %s", (job["id"],))
        return
    except Exception as exc:
        # Unexpected: leave the job for another attempt, record why.
        log.exception("provision.error", id=dep_id)
        async with db.transaction() as conn:
            await conn.execute(
                "update jobs set locked_until = null, last_error = %s where id = %s",
                (str(exc)[:500], job["id"]),
            )
        return

    async with db.transaction() as conn:
        await conn.execute("delete from jobs where id = %s", (job["id"],))


async def loop() -> None:
    await db.open_pool()
    await k8s.load()
    cluster = k8s.Cluster()
    gw = gateway.build()
    log.info("provisioner.start", field_manager=k8s.FIELD_MANAGER)
    try:
        while True:
            async with db.transaction() as conn:
                job = await db.claim_job(conn)
            if job is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            log.info("job.claimed", kind=job["kind"], attempts=job["attempts"])
            await run_job(cluster, gw, job)
    finally:
        await cluster.close()
        await db.close_pool()


def run() -> None:
    asyncio.run(loop())


if __name__ == "__main__":
    run()
