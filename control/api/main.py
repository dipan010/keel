"""Control API -- the only writer of desired state.

It records intent and enqueues work. It never touches the cluster: provisioning
is the Provisioner's job alone, which is why this service's ServiceAccount has
no in-cluster permissions at all.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from control.domain.rules import route_name
from control.domain.states import Lane, Status

app = FastAPI(title="Keel Control API", version="0.0.1")


class CreateDeployment(BaseModel):
    team: str
    name: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$")
    model: str
    lane: Lane | None = None
    replicas_min: int | None = None
    replicas_max: int | None = None


class DeploymentOut(BaseModel):
    id: str
    name: str
    status: Status
    route: str | None = None


async def caller(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """OIDC token -> subject and groups.

    TODO(phase1): verify the JWT against the IdP's JWKS. Teams map to a groups
    claim, with claim-to-team held as a database row -- no user management code
    is ever written here.
    """
    if not authorization:
        raise HTTPException(401, "missing bearer token")
    return {"sub": "dev@localhost", "groups": ["keel-dev"]}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/deployments", status_code=202, response_model=DeploymentOut)
async def create(
    body: CreateDeployment,
    who: Annotated[dict[str, Any], Depends(caller)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DeploymentOut:
    """Record intent and enqueue. Returns in milliseconds; never blocks.

    The insert and the enqueue happen in ONE transaction. That is why a
    deployment row can never exist without a job to act on it.
    """
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")

    dep_id = str(uuid.uuid4())
    # TODO(phase1): look up the team and catalog entry, run domain.rules.validate,
    # resolve the lane, then insert. Sketched here so the transaction boundary --
    # the load-bearing part -- is visible from the start.
    #
    # async with db.pool().connection() as conn:
    #     async with conn.transaction():
    #         await conn.execute("insert into deployments (...) values (...)", ...)
    #         await db.record_transition(conn, dep_id, None, Status.REQUESTED, who["sub"])
    #         await conn.execute(
    #             "insert into jobs (deployment_id, kind) values (%s, 'provision')", (dep_id,)
    #         )
    return DeploymentOut(
        id=dep_id,
        name=body.name,
        status=Status.REQUESTED,
        route=route_name(body.team, body.name),
    )


@app.get("/v1/deployments/{dep_id}", response_model=DeploymentOut)
async def get(dep_id: str, who: Annotated[dict[str, Any], Depends(caller)]) -> DeploymentOut:
    """Status plus the recent event tail -- the tail is what makes a slow
    `loading` legible instead of an unexplained spinner."""
    raise HTTPException(501, "not implemented")


@app.delete("/v1/deployments/{dep_id}", status_code=202)
async def delete(dep_id: str, who: Annotated[dict[str, Any], Depends(caller)]) -> dict[str, str]:
    raise HTTPException(501, "not implemented")


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
