"""Control API -- the only writer of desired state.

It records intent and enqueues work. It never touches the cluster: provisioning
is the Provisioner's job alone, which is why this service's ServiceAccount has
no in-cluster permissions at all.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from control import db, gateway, repo
from control.api.auth import Caller, caller
from control.api.errors import Conflict, QuotaExceeded
from control.domain.rules import (
    DeploymentRequest,
    ValidationError,
    namespace,
    object_name,
    route_name,
    validate,
)
from control.domain.states import IllegalTransition, Lane, Mode, Status, assert_transition


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(title="Keel Control API", version="0.0.1", lifespan=lifespan)


class CreateDeployment(BaseModel):
    team: str
    name: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$")
    model: str
    lane: Lane | None = None
    replicas_min: int | None = Field(default=None, ge=0)
    replicas_max: int | None = Field(default=None, ge=1)
    allow_preview: bool = False


class DeploymentOut(BaseModel):
    id: str
    name: str
    mode: Mode
    lane: Lane
    model: str
    status: Status
    route: str | None = None
    namespace: str | None = None
    created_at: datetime | None = None


class EventOut(BaseModel):
    from_status: Status | None
    to_status: Status
    actor: str
    reason_code: str | None
    at: datetime


class DeploymentDetail(DeploymentOut):
    events: list[EventOut] = []


class CreateKey(BaseModel):
    max_budget_usd: float | None = Field(default=None, gt=0)
    duration: str | None = Field(default=None, pattern=r"^\d+[smhd]$")


class KeyOut(BaseModel):
    id: str
    masked: str
    max_budget_usd: float | None = None
    expires_at: datetime | None = None
    created_by: str
    created_at: datetime
    revoked_at: datetime | None = None


class IssuedKey(KeyOut):
    """The only response that ever carries the secret.

    It is returned once, at creation, and never stored -- so a caller who loses
    it issues a new key rather than recovering the old one.
    """

    key: str


def _out(row: dict[str, Any]) -> DeploymentOut:
    return DeploymentOut(
        id=str(row["id"]),
        name=row["name"],
        mode=Mode(row["mode"]),
        lane=Lane(row["lane"]),
        model=row["model_id"],
        status=Status(row["status"]),
        route=row["route_name"],
        namespace=namespace(row["team_slug"]) if row.get("team_slug") else None,
        created_at=row.get("created_at"),
    )


def fingerprint(body: CreateDeployment) -> str:
    """Stable hash of the request.

    The unique constraint stops a replayed key creating a second deployment. It
    does not stop a client reusing a key with a different body -- which would
    silently hand back a deployment that is not the one they asked for. Same key
    plus same body is a replay; same key plus different body is a client bug.
    """
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/deployments", status_code=202, response_model=DeploymentOut)
async def create(
    body: CreateDeployment,
    who: Annotated[Caller, Depends(caller)],
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DeploymentOut:
    """Record intent and enqueue. Returns in milliseconds; never blocks.

    Everything below happens in ONE transaction. That is why a deployment row
    can never exist without a job to act on it, and why the quota check cannot
    be raced by a concurrent request for the same team.
    """
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")

    fp = fingerprint(body)

    async with db.transaction() as conn:
        # Row-locked: the quota read below and the insert that consumes quota
        # must not interleave with another request for this team.
        team = await repo.get_team(conn, body.team, lock=True)
        if team is None:
            raise HTTPException(404, f"no such team: {body.team}")
        if team["oidc_group"] not in who.groups:
            raise HTTPException(403, f"you are not a member of team {body.team}")

        prior = await repo.find_by_idempotency_key(conn, team["id"], idempotency_key)
        if prior is not None:
            if prior["request_fingerprint"] != fp:
                raise Conflict(
                    f"Idempotency-Key {idempotency_key!r} was already used for a "
                    "different request. Use a new key."
                )
            response.headers["Idempotent-Replay"] = "true"
            return _out({**prior, "team_slug": team["slug"]})

        entry = await repo.get_model(conn, body.model)
        if entry is None:
            raise HTTPException(404, f"no such model in the catalog: {body.model}")

        req = DeploymentRequest(
            team_slug=body.team,
            name=body.name,
            model_id=body.model,
            lane=body.lane,
            replicas_min=body.replicas_min,
            replicas_max=body.replicas_max,
        )
        try:
            lane = validate(req, entry, allow_preview=body.allow_preview)
        except ValidationError as exc:
            raise HTTPException(400, str(exc)) from exc

        if await repo.name_taken(conn, team["id"], body.name):
            raise Conflict(f"team {body.team} already has a deployment named {body.name!r}")

        dep_id = uuid.uuid4()
        self_hosted = entry.mode is Mode.SELF_HOSTED

        if self_hosted:
            # 0 is a real value -- CPU serving. `or 1` would turn it into a GPU
            # request and charge quota for hardware the pod never asks for.
            gpus = 1 if entry.gpu_count is None else entry.gpu_count
            in_use = await repo.gpu_in_use(conn, team["id"])
            if in_use + gpus > team["gpu_quota"]:
                raise QuotaExceeded(gpus, in_use, team["gpu_quota"])
            replicas_min = 0 if lane is Lane.C else (body.replicas_min or 1)
            replicas_max = body.replicas_max or max(replicas_min, 1)
        else:
            gpus = replicas_min = replicas_max = None

        await repo.insert_deployment(
            conn,
            {
                "id": dep_id,
                "team_id": team["id"],
                "name": body.name,
                "mode": str(entry.mode),
                "lane": str(lane),
                "model_id": entry.id,
                "status": str(Status.REQUESTED),
                "route_name": route_name(body.team, body.name),
                "k8s_object_name": object_name(str(dep_id)) if self_hosted else None,
                "accelerator": entry.accelerator if self_hosted else None,
                "gpu_count": gpus,
                "replicas_min": replicas_min,
                "replicas_max": replicas_max,
                "engine_args": {} if self_hosted else None,
                "upstream_model": None if self_hosted else entry.upstream_model,
                "created_by": who.sub,
                "idempotency_key": idempotency_key,
                "request_fingerprint": fp,
            },
        )
        await repo.record_transition(
            conn, dep_id, None, Status.REQUESTED, who.sub, update_row=False
        )
        await repo.enqueue(conn, dep_id, "provision")

        row = await repo.get_deployment(conn, str(dep_id))

    assert row is not None
    return _out({**row, "team_slug": team["slug"]})


@app.get("/v1/deployments", response_model=list[DeploymentOut])
async def list_deployments(
    who: Annotated[Caller, Depends(caller)],
    team: Annotated[str | None, Query()] = None,
) -> list[DeploymentOut]:
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            """select d.*, t.slug as team_slug
                 from deployments d join teams t on t.id = d.team_id
                where d.status <> 'deleted'
                  and t.oidc_group = any(%s)
                  and (%s::text is null or t.slug = %s)
                order by d.created_at desc""",
            (list(who.groups), team, team),
        )
        return [_out(r) for r in await cur.fetchall()]


@app.delete("/v1/deployments/{dep_id}", status_code=202, response_model=DeploymentOut)
async def delete(dep_id: str, who: Annotated[Caller, Depends(caller)]) -> DeploymentOut:
    """Record the intent to tear down and enqueue it. Never blocks.

    Soft delete: the row survives and so do its events, because "why did this
    endpoint disappear" has to stay answerable after the workload is gone.
    """
    async with db.transaction() as conn:
        cur = await conn.execute(
            """select d.*, t.slug as team_slug, t.oidc_group
                 from deployments d join teams t on t.id = d.team_id
                where d.id = %s for update of d""",
            (dep_id,),
        )
        row = await cur.fetchone()
        if row is None or row["oidc_group"] not in who.groups:
            raise HTTPException(404, "no such deployment")

        current = Status(row["status"])
        if current is Status.DELETED:
            raise HTTPException(404, "no such deployment")
        # Already on its way out: report the state rather than queueing a
        # second teardown. A client retrying after a timeout must not stack
        # jobs.
        if current is Status.DELETING:
            return _out(row)

        try:
            assert_transition(current, Status.DELETING)
        except IllegalTransition as exc:
            raise Conflict(f"cannot delete a deployment that is {current}") from exc

        # Queued-but-unclaimed work for this deployment is now pointless.
        await repo.cancel_pending_jobs(conn, dep_id)
        await repo.record_transition(conn, dep_id, current, Status.DELETING, who.sub)
        await repo.enqueue(conn, dep_id, "delete")

        team_slug = row["team_slug"]
        row = await repo.get_deployment(conn, dep_id)

    assert row is not None
    return _out({**row, "team_slug": team_slug})


@app.get("/v1/deployments/{dep_id}", response_model=DeploymentDetail)
async def get(dep_id: str, who: Annotated[Caller, Depends(caller)]) -> DeploymentDetail:
    """Status plus the recent event tail -- the tail is what makes a slow
    `loading` legible instead of an unexplained spinner."""
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            """select d.*, t.slug as team_slug, t.oidc_group
                 from deployments d join teams t on t.id = d.team_id
                where d.id = %s""",
            (dep_id,),
        )
        row = await cur.fetchone()
        if row is None or row["oidc_group"] not in who.groups:
            raise HTTPException(404, "no such deployment")
        events = await repo.recent_events(conn, dep_id)

    base = _out(row)
    return DeploymentDetail(
        **base.model_dump(),
        events=[
            EventOut(
                from_status=e["from_status"],
                to_status=e["to_status"],
                actor=e["actor"],
                reason_code=e["reason_code"],
                at=e["at"],
            )
            for e in events
        ],
    )


def _key_out(row: dict[str, Any]) -> KeyOut:
    return KeyOut(
        id=str(row["id"]),
        masked=row["masked"],
        max_budget_usd=float(row["max_budget_usd"]) if row["max_budget_usd"] else None,
        expires_at=row["expires_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


async def _deployment_for(conn: Any, dep_id: str, who: Caller) -> dict[str, Any]:
    cur = await conn.execute(
        """select d.*, t.slug as team_slug, t.oidc_group, t.budget_usd_mo
             from deployments d join teams t on t.id = d.team_id
            where d.id = %s""",
        (dep_id,),
    )
    row = await cur.fetchone()
    if row is None or row["oidc_group"] not in who.groups:
        raise HTTPException(404, "no such deployment")
    return row


@app.post("/v1/deployments/{dep_id}/keys", status_code=201, response_model=IssuedKey)
async def create_key(
    dep_id: str,
    body: CreateKey,
    who: Annotated[Caller, Depends(caller)],
) -> IssuedKey:
    """Issue a key scoped to this deployment alone.

    The secret is in this response and nowhere else. We store the alias, a hash
    and a masked form -- enough to list and revoke, useless to anyone reading
    the table.
    """
    async with db.pool().connection() as conn:
        dep = await _deployment_for(conn, dep_id, who)

    if Status(dep["status"]) in {Status.DELETING, Status.DELETED}:
        raise Conflict("cannot issue a key for a deployment that is being torn down")
    if not dep["route_name"]:
        raise Conflict(
            f"deployment is {dep['status']} and has no route yet; wait for it to be ready"
        )

    key_id = uuid.uuid4()
    alias = f"keel-{dep_id}-{key_id.hex[:8]}"
    # A key must not be able to outspend the team that owns it.
    budget = body.max_budget_usd
    if budget is None and dep["budget_usd_mo"] is not None:
        budget = float(dep["budget_usd_mo"])

    gw = gateway.build()
    issued = await gw.issue_key(
        dep["route_name"], alias, budget, body.duration or gateway.DEFAULT_KEY_DURATION
    )

    row = {
        "id": key_id,
        "deployment_id": dep_id,
        "alias": alias,
        "token_hash": issued.get("token") or "",
        "masked": issued.get("key_name") or "sk-...",
        "max_budget_usd": budget,
        "expires_at": issued.get("expires"),
        "created_by": who.sub,
    }
    async with db.transaction() as conn:
        await repo.insert_key(conn, row)
        stored = await repo.get_key(conn, dep_id, str(key_id))

    assert stored is not None
    return IssuedKey(**_key_out(stored).model_dump(), key=issued["key"])


@app.get("/v1/deployments/{dep_id}/keys", response_model=list[KeyOut])
async def list_keys(dep_id: str, who: Annotated[Caller, Depends(caller)]) -> list[KeyOut]:
    """Masked only. The secret is unrecoverable by design."""
    async with db.pool().connection() as conn:
        await _deployment_for(conn, dep_id, who)
        return [_key_out(r) for r in await repo.active_keys(conn, dep_id)]


@app.delete("/v1/deployments/{dep_id}/keys/{key_id}", status_code=204)
async def revoke_key(dep_id: str, key_id: str, who: Annotated[Caller, Depends(caller)]) -> Response:
    async with db.pool().connection() as conn:
        await _deployment_for(conn, dep_id, who)
        row = await repo.get_key(conn, dep_id, key_id)

    if row is None:
        raise HTTPException(404, "no such key")
    if row["revoked_at"] is not None:
        # Already gone. Idempotent: a retry must not error.
        return Response(status_code=204)

    # Revoke at the gateway FIRST. If the order were reversed and the gateway
    # call failed, the record would say revoked while the key still worked.
    await gateway.build().revoke_key(row["alias"])
    async with db.transaction() as conn:
        await repo.mark_key_revoked(conn, key_id, who.sub)
    return Response(status_code=204)


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
