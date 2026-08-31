"""The create path: catalog lookup, validation, quota, and the transaction."""

from __future__ import annotations

import uuid

import pytest

from control.tests.conftest import requires_db

pytestmark = requires_db

SMALL = "qwen2.5-0.5b-instruct"


def body(team: str, name: str = "chat", **kw):
    return {"team": team, "name": name, "model": SMALL, "allow_preview": True, **kw}


def key() -> str:
    return uuid.uuid4().hex


async def test_create_returns_202_and_a_route(client, team):
    r = await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": key()})
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["status"] == "requested"
    assert d["route"] == f"{team}/chat"
    assert d["namespace"] == f"keel-inf-{team}"
    assert d["lane"] == "b"


async def test_idempotency_key_is_required(client, team):
    r = await client.post("/v1/deployments", json=body(team))
    assert r.status_code == 400
    assert "Idempotency-Key" in r.json()["detail"]


async def test_replay_returns_the_same_deployment(client, team):
    k = key()
    first = await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": k})
    second = await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": k})
    assert second.status_code == 202
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["id"] == first.json()["id"]


async def test_same_key_different_body_is_a_conflict(client, team):
    k = key()
    await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": k})
    r = await client.post(
        "/v1/deployments", json=body(team, name="other"), headers={"Idempotency-Key": k}
    )
    assert r.status_code == 409
    assert "different request" in r.json()["detail"]


async def test_duplicate_name_is_rejected(client, team):
    await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": key()})
    r = await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": key()})
    assert r.status_code == 409
    assert "already has a deployment named" in r.json()["detail"]


async def test_unknown_model_is_404(client, team):
    r = await client.post(
        "/v1/deployments",
        json={**body(team), "model": "no-such-model"},
        headers={"Idempotency-Key": key()},
    )
    assert r.status_code == 404
    assert "catalog" in r.json()["detail"]


async def test_unknown_team_is_404(client):
    r = await client.post(
        "/v1/deployments", json=body("nope-not-a-team"), headers={"Idempotency-Key": key()}
    )
    assert r.status_code == 404


async def test_preview_model_refused_without_the_flag(client, team):
    r = await client.post(
        "/v1/deployments",
        json={**body(team), "allow_preview": False},
        headers={"Idempotency-Key": key()},
    )
    assert r.status_code == 400
    assert "preview" in r.json()["detail"]


async def test_lane_a_on_a_self_hosted_model_is_rejected(client, team):
    r = await client.post(
        "/v1/deployments", json=body(team, lane="a"), headers={"Idempotency-Key": key()}
    )
    assert r.status_code == 400
    assert "lane a is upstream" in r.json()["detail"]


async def test_gpu_quota_is_enforced(client, team):
    # team fixture has quota 2; the fixture model wants 1 GPU each
    for i in range(2):
        r = await client.post(
            "/v1/deployments", json=body(team, name=f"m{i}"), headers={"Idempotency-Key": key()}
        )
        assert r.status_code == 202, r.text
    r = await client.post(
        "/v1/deployments", json=body(team, name="m2"), headers={"Idempotency-Key": key()}
    )
    assert r.status_code == 409
    assert "quota exceeded" in r.json()["detail"]


async def test_create_writes_row_event_and_job_together(client, team):
    from control import db

    r = await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": key()})
    dep_id = r.json()["id"]

    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select count(*) as n from deployment_events where deployment_id = %s", (dep_id,)
        )
        assert (await cur.fetchone())["n"] == 1
        cur = await conn.execute("select kind from jobs where deployment_id = %s", (dep_id,))
        # The row cannot exist without its job: same transaction.
        assert (await cur.fetchone())["kind"] == "provision"


async def test_get_returns_the_event_tail(client, team):
    r = await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": key()})
    dep_id = r.json()["id"]
    detail = await client.get(f"/v1/deployments/{dep_id}")
    assert detail.status_code == 200
    events = detail.json()["events"]
    assert len(events) == 1
    assert events[0]["to_status"] == "requested"
    assert events[0]["from_status"] is None


async def test_list_is_scoped_to_the_callers_teams(client, team):
    await client.post("/v1/deployments", json=body(team), headers={"Idempotency-Key": key()})
    r = await client.get("/v1/deployments", params={"team": team})
    assert r.status_code == 200
    assert [d["name"] for d in r.json()] == ["chat"]


@pytest.mark.parametrize("name", ["Chat", "has space", "-leading", "x" * 60])
async def test_invalid_names_are_rejected_before_the_database(client, team, name):
    r = await client.post(
        "/v1/deployments", json=body(team, name=name), headers={"Idempotency-Key": key()}
    )
    assert r.status_code == 422
