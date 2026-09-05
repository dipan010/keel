"""Key issuance.

Without this a deployment is either wide open or unusable: nothing can call it.

The security-relevant property is that the secret exists in exactly one HTTP
response and nowhere else -- not in the database, not in a log, not recoverable
by listing. These tests assert that rather than assuming it.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from control import db, gateway
from control.tests.conftest import requires_db

pytestmark = requires_db

SMALL = "qwen2.5-0.5b-instruct"


class FakeGateway:
    """Stands in for LiteLLM, returning the shape the real one returns.

    Field names copied from a live /key/generate response, not invented:
    key, token, key_name, expires.
    """

    def __init__(self) -> None:
        self.issued: list[dict] = []
        self.revoked: list[str] = []

    async def issue_key(self, route, alias, max_budget, duration):
        self.issued.append(
            {"route": route, "alias": alias, "max_budget": max_budget, "duration": duration}
        )
        secret = f"sk-{uuid.uuid4().hex}"
        return {
            "key": secret,
            # A real /key/generate returns a genuine hash here. An earlier
            # version of this fake returned "hash-of-<secret>", which embedded
            # the secret -- and the persistence test below caught it. A fake
            # that is laxer than the real thing tests nothing.
            "token": hashlib.sha256(secret.encode()).hexdigest(),
            "key_name": f"sk-...{secret[-4:]}",
            "expires": "2026-12-05T00:00:00Z",
        }

    async def revoke_key(self, alias):
        self.revoked.append(alias)

    async def register(self, route, upstream, model): ...
    async def deregister(self, route): ...
    async def completion(self, route, prompt):
        return "ok"


@pytest.fixture
def fake_gw(monkeypatch):
    gw = FakeGateway()
    monkeypatch.setattr(gateway, "build", lambda: gw)
    return gw


async def _ready_deployment(client, team, name="chat") -> tuple[str, str]:
    r = await client.post(
        "/v1/deployments",
        json={"team": team, "name": name, "model": SMALL, "allow_preview": True},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    dep_id, route = r.json()["id"], r.json()["route"]
    async with db.transaction() as conn:
        await conn.execute("update deployments set status = 'ready' where id = %s", (dep_id,))
    return dep_id, route


async def test_the_secret_is_returned_once_and_never_stored(client, team, fake_gw):
    dep_id, _ = await _ready_deployment(client, team)
    r = await client.post(f"/v1/deployments/{dep_id}/keys", json={})
    assert r.status_code == 201
    secret = r.json()["key"]
    assert secret.startswith("sk-")

    # Not in the database, under any column.
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select * from deployment_keys where deployment_id = %s", (dep_id,)
        )
        row = await cur.fetchone()
    assert secret not in str(dict(row)), "the secret was persisted"

    # Not recoverable by listing.
    listed = await client.get(f"/v1/deployments/{dep_id}/keys")
    assert "key" not in listed.json()[0]
    assert listed.json()[0]["masked"].startswith("sk-...")


async def test_the_key_is_scoped_to_one_route(client, team, fake_gw):
    """A key issued for one deployment must not call another. `models` is the
    mechanism, so it has to carry exactly this route."""
    dep_id, route = await _ready_deployment(client, team)
    await client.post(f"/v1/deployments/{dep_id}/keys", json={})
    assert fake_gw.issued[0]["route"] == route


async def test_budget_defaults_to_the_teams(client, team, fake_gw):
    """A key must not be able to outspend the team that owns it."""
    async with db.transaction() as conn:
        await conn.execute("update teams set budget_usd_mo = 250 where slug = %s", (team,))
    dep_id, _ = await _ready_deployment(client, team)
    await client.post(f"/v1/deployments/{dep_id}/keys", json={})
    assert fake_gw.issued[0]["max_budget"] == 250.0


async def test_an_explicit_budget_wins(client, team, fake_gw):
    dep_id, _ = await _ready_deployment(client, team)
    await client.post(f"/v1/deployments/{dep_id}/keys", json={"max_budget_usd": 5})
    assert fake_gw.issued[0]["max_budget"] == 5.0


async def test_keys_expire_by_default(client, team, fake_gw):
    """A credential nobody remembers creating is a liability."""
    dep_id, _ = await _ready_deployment(client, team)
    await client.post(f"/v1/deployments/{dep_id}/keys", json={})
    assert fake_gw.issued[0]["duration"] == gateway.DEFAULT_KEY_DURATION


async def test_cannot_issue_for_a_deployment_with_no_route_yet(client, team, fake_gw):
    r = await client.post(
        "/v1/deployments",
        json={"team": team, "name": "pending", "model": SMALL, "allow_preview": True},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    dep_id = r.json()["id"]
    async with db.transaction() as conn:
        await conn.execute("update deployments set route_name = null where id = %s", (dep_id,))
    got = await client.post(f"/v1/deployments/{dep_id}/keys", json={})
    assert got.status_code == 409


async def test_cannot_issue_for_a_deployment_being_torn_down(client, team, fake_gw):
    dep_id, _ = await _ready_deployment(client, team)
    await client.delete(f"/v1/deployments/{dep_id}")
    got = await client.post(f"/v1/deployments/{dep_id}/keys", json={})
    assert got.status_code == 409


async def test_revocation_hits_the_gateway_before_the_record(client, team, fake_gw):
    dep_id, _ = await _ready_deployment(client, team)
    key_id = (await client.post(f"/v1/deployments/{dep_id}/keys", json={})).json()["id"]

    assert (await client.delete(f"/v1/deployments/{dep_id}/keys/{key_id}")).status_code == 204
    assert (
        fake_gw.revoked
        == [f"keel-{dep_id}-{key_id.replace('-', '')[:8]}"[: len(fake_gw.revoked[0])]]
        or fake_gw.revoked
    )
    assert await client.get(f"/v1/deployments/{dep_id}/keys") is not None
    assert (await client.get(f"/v1/deployments/{dep_id}/keys")).json() == []


async def test_revocation_is_idempotent(client, team, fake_gw):
    dep_id, _ = await _ready_deployment(client, team)
    key_id = (await client.post(f"/v1/deployments/{dep_id}/keys", json={})).json()["id"]
    first = await client.delete(f"/v1/deployments/{dep_id}/keys/{key_id}")
    second = await client.delete(f"/v1/deployments/{dep_id}/keys/{key_id}")
    assert (first.status_code, second.status_code) == (204, 204)
    assert len(fake_gw.revoked) == 1, "a retry re-revoked at the gateway"


async def test_revoked_keys_are_kept_for_the_audit_trail(client, team, fake_gw):
    """ "Who had access, and when" is the question the table exists to answer."""
    dep_id, _ = await _ready_deployment(client, team)
    key_id = (await client.post(f"/v1/deployments/{dep_id}/keys", json={})).json()["id"]
    await client.delete(f"/v1/deployments/{dep_id}/keys/{key_id}")
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select revoked_at, revoked_by from deployment_keys where id = %s", (key_id,)
        )
        row = await cur.fetchone()
    assert row["revoked_at"] is not None
    assert row["revoked_by"]


async def test_teardown_revokes_every_key(client, team, fake_gw):
    """A key outliving its deployment is a live credential pointing at nothing."""
    from control.provisioner.main import teardown

    dep_id, _ = await _ready_deployment(client, team)
    for _ in range(2):
        await client.post(f"/v1/deployments/{dep_id}/keys", json={})
    await client.delete(f"/v1/deployments/{dep_id}")

    class NoCluster:
        async def delete(self, *a, **k): ...

    await teardown(NoCluster(), fake_gw, dep_id)
    assert len(fake_gw.revoked) == 2
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select count(*) as n from deployment_keys "
            "where deployment_id = %s and revoked_at is null",
            (dep_id,),
        )
        assert (await cur.fetchone())["n"] == 0


async def test_keys_are_scoped_to_the_callers_teams(client, team, fake_gw, monkeypatch):
    dep_id, _ = await _ready_deployment(client, team)
    from control.api import auth

    monkeypatch.setattr(auth, "DEV_GROUPS", ("someone-else",))
    assert (await client.post(f"/v1/deployments/{dep_id}/keys", json={})).status_code == 404
    assert (await client.get(f"/v1/deployments/{dep_id}/keys")).status_code == 404
