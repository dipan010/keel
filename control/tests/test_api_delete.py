"""Teardown: the half of the loop that had never been reachable.

The Provisioner has had `teardown()` and a "delete" handler since stage 3, but
nothing enqueued that job, so the code had never run. Everything created leaked.
"""

from __future__ import annotations

import uuid

from control import db
from control.domain.states import Status
from control.tests.conftest import requires_db

pytestmark = requires_db

SMALL = "qwen2.5-0.5b-instruct"


def body(team: str, name: str = "chat"):
    return {"team": team, "name": name, "model": SMALL, "allow_preview": True}


def key() -> str:
    return uuid.uuid4().hex


async def _create(client, team, name="chat") -> str:
    r = await client.post(
        "/v1/deployments", json=body(team, name), headers={"Idempotency-Key": key()}
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def _status(dep_id: str) -> str:
    async with db.pool().connection() as conn:
        cur = await conn.execute("select status from deployments where id = %s", (dep_id,))
        return (await cur.fetchone())["status"]


async def _jobs(dep_id: str) -> list[str]:
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select kind from jobs where deployment_id = %s order by id", (dep_id,)
        )
        return [r["kind"] for r in await cur.fetchall()]


async def test_delete_records_intent_and_enqueues_teardown(client, team):
    dep_id = await _create(client, team)
    r = await client.delete(f"/v1/deployments/{dep_id}")
    assert r.status_code == 202
    assert r.json()["status"] == Status.DELETING.value
    assert await _status(dep_id) == Status.DELETING.value
    assert "delete" in await _jobs(dep_id)


async def test_queued_provision_is_cancelled_not_left_to_run(client, team):
    """Provisioning something we are about to tear down wastes a GPU and leaves
    objects behind. Only UNCLAIMED work is dropped."""
    dep_id = await _create(client, team)
    assert await _jobs(dep_id) == ["provision"]
    await client.delete(f"/v1/deployments/{dep_id}")
    assert await _jobs(dep_id) == ["delete"]


async def test_in_flight_work_is_left_alone(client, team):
    """A job the Provisioner already holds keeps its lock and finishes.

    Killing it mid-run would leave half-created objects with nothing tracking
    them -- worse than a few wasted seconds.
    """
    dep_id = await _create(client, team)
    async with db.transaction() as conn:
        await conn.execute(
            "update jobs set locked_until = now() + interval '5 minutes' where deployment_id = %s",
            (dep_id,),
        )
    await client.delete(f"/v1/deployments/{dep_id}")
    assert await _jobs(dep_id) == ["provision", "delete"]


async def test_delete_is_idempotent(client, team):
    """A client retrying after a timeout must not stack teardown jobs."""
    dep_id = await _create(client, team)
    first = await client.delete(f"/v1/deployments/{dep_id}")
    second = await client.delete(f"/v1/deployments/{dep_id}")
    assert (first.status_code, second.status_code) == (202, 202)
    assert await _jobs(dep_id) == ["delete"]


async def test_events_survive_the_deployment(client, team):
    """Soft delete on purpose: "why did this endpoint disappear" has to stay
    answerable after the workload is gone."""
    dep_id = await _create(client, team)
    await client.delete(f"/v1/deployments/{dep_id}")
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select to_status from deployment_events where deployment_id = %s order by at",
            (dep_id,),
        )
        assert [r["to_status"] for r in await cur.fetchall()] == ["requested", "deleting"]


async def test_deleting_frees_the_name(client, team):
    dep_id = await _create(client, team)
    await client.delete(f"/v1/deployments/{dep_id}")
    async with db.transaction() as conn:
        await conn.execute("update deployments set status = 'deleted' where id = %s", (dep_id,))
    # Same name, new deployment: the unique constraint is on live rows only.
    assert await _create(client, team) != dep_id


async def test_deleting_frees_gpu_quota(client, team):
    """gpu_in_use excludes deleted rows, so a torn-down deployment stops
    counting against the team."""
    ids = [await _create(client, team, f"m{i}") for i in range(2)]
    blocked = await client.post(
        "/v1/deployments", json=body(team, "m2"), headers={"Idempotency-Key": key()}
    )
    assert blocked.status_code == 409

    await client.delete(f"/v1/deployments/{ids[0]}")
    async with db.transaction() as conn:
        await conn.execute("update deployments set status = 'deleted' where id = %s", (ids[0],))
    ok = await client.post(
        "/v1/deployments", json=body(team, "m2"), headers={"Idempotency-Key": key()}
    )
    assert ok.status_code == 202, ok.text


async def test_delete_is_scoped_to_the_callers_teams(client, team, monkeypatch):
    dep_id = await _create(client, team)
    from control.api import auth

    monkeypatch.setattr(auth, "DEV_GROUPS", ("some-other-team",))
    assert (await client.delete(f"/v1/deployments/{dep_id}")).status_code == 404


async def test_unknown_deployment_is_404(client, team):
    assert (await client.delete(f"/v1/deployments/{uuid.uuid4()}")).status_code == 404
