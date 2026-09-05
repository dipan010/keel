"""Data access. Every function takes an explicit connection so the caller owns
the transaction boundary -- which is the whole point of the create path."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from control.domain.rules import CatalogEntry
from control.domain.states import Lane, Mode, Status


async def get_team(conn: Any, slug: str, *, lock: bool = False) -> dict[str, Any] | None:
    """`lock=True` takes a row lock so the GPU quota check and the insert that
    consumes quota cannot interleave with a concurrent request for the same
    team. Without it two requests each see quota for one more GPU and both
    succeed."""
    sql = "select * from teams where slug = %s"
    if lock:
        sql += " for update"
    cur = await conn.execute(sql, (slug,))
    return await cur.fetchone()


async def get_model(conn: Any, model_id: str) -> CatalogEntry | None:
    cur = await conn.execute("select * from catalog_models where id = %s", (model_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return CatalogEntry(
        id=row["id"],
        mode=Mode(row["mode"]),
        default_lane=Lane(row["default_lane"]),
        context_length=row["context_length"],
        license=row["license"],
        status=row["status"],
        accelerator=row["accelerator"],
        gpu_count=row["gpu_count"],
        weights_uri=row["weights_uri"],
        upstream_model=row["upstream_model"],
    )


async def gpu_in_use(conn: Any, team_id: UUID) -> int:
    """GPUs held by deployments that are alive or on their way there.

    `failed` and `deleted` are excluded; anything else is either holding a GPU
    now or about to be, and quota has to account for both or a burst of requests
    overshoots while they are all still provisioning.
    """
    cur = await conn.execute(
        """select coalesce(sum(gpu_count), 0) as n
             from deployments
            where team_id = %s
              and mode = 'self_hosted'
              and status not in ('failed', 'deleted')""",
        (team_id,),
    )
    row = await cur.fetchone()
    return int(row["n"])


async def find_by_idempotency_key(conn: Any, team_id: UUID, key: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "select * from deployments where team_id = %s and idempotency_key = %s",
        (team_id, key),
    )
    return await cur.fetchone()


async def name_taken(conn: Any, team_id: UUID, name: str) -> bool:
    cur = await conn.execute(
        "select 1 from deployments where team_id = %s and name = %s and status <> 'deleted'",
        (team_id, name),
    )
    return await cur.fetchone() is not None


async def insert_deployment(conn: Any, dep: dict[str, Any]) -> None:
    await conn.execute(
        """insert into deployments
             (id, team_id, name, mode, lane, model_id, status, route_name,
              k8s_object_name, accelerator, gpu_count, replicas_min, replicas_max,
              engine_args, upstream_model, created_by, idempotency_key,
              request_fingerprint)
           values
             (%(id)s, %(team_id)s, %(name)s, %(mode)s, %(lane)s, %(model_id)s,
              %(status)s, %(route_name)s, %(k8s_object_name)s, %(accelerator)s,
              %(gpu_count)s, %(replicas_min)s, %(replicas_max)s, %(engine_args)s,
              %(upstream_model)s, %(created_by)s, %(idempotency_key)s,
              %(request_fingerprint)s)""",
        {**dep, "engine_args": json.dumps(dep.get("engine_args") or {})},
    )


async def record_transition(
    conn: Any,
    deployment_id: UUID | str,
    frm: Status | None,
    to: Status,
    actor: str,
    reason_code: str | None = None,
    detail: dict[str, Any] | None = None,
    *,
    update_row: bool = True,
) -> None:
    """Status is a projection of the event log, never a field somebody just sets.

    `update_row=False` is for the initial insert, where the row already carries
    its status and only the event is missing.
    """
    if update_row:
        await conn.execute(
            "update deployments set status = %s, updated_at = now() where id = %s",
            (str(to), deployment_id),
        )
    await conn.execute(
        """insert into deployment_events
             (deployment_id, from_status, to_status, actor, reason_code, detail)
           values (%s, %s, %s, %s, %s, %s)""",
        (
            deployment_id,
            str(frm) if frm else None,
            str(to),
            actor,
            reason_code,
            json.dumps(detail) if detail else None,
        ),
    )


async def cancel_pending_jobs(conn: Any, deployment_id: UUID | str) -> int:
    """Drop queued-but-unclaimed work for a deployment.

    Only unlocked rows: a job the Provisioner is already running keeps its lock
    and finishes, and the teardown queues behind it. Killing an in-flight
    provision would leave half-created objects with nothing tracking them.
    """
    cur = await conn.execute(
        "delete from jobs where deployment_id = %s and locked_until is null",
        (deployment_id,),
    )
    return cur.rowcount


async def enqueue(conn: Any, deployment_id: UUID | str, kind: str) -> None:
    await conn.execute(
        "insert into jobs (deployment_id, kind) values (%s, %s)",
        (deployment_id, kind),
    )


async def get_deployment(conn: Any, dep_id: str) -> dict[str, Any] | None:
    cur = await conn.execute("select * from deployments where id = %s", (dep_id,))
    return await cur.fetchone()


async def recent_events(conn: Any, dep_id: str, limit: int = 20) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """select from_status, to_status, actor, reason_code, detail, at
             from deployment_events
            where deployment_id = %s
            order by at desc limit %s""",
        (dep_id, limit),
    )
    return await cur.fetchall()
