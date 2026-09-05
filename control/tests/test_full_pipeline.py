"""The whole chain, for real: request -> record -> manifests -> pod -> gateway
-> traffic.

This is the test the architecture doc's phase 1 exists to make pass. Everything
below runs against a real API server, a real Postgres and a real LiteLLM; the
only thing faked is the model runtime, because a GPU is the one part that
cannot be free.

Needs a port-forward to the gateway:
    kubectl port-forward -n keel-gateway svc/litellm 4000:4000
"""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

os.environ.setdefault("KEEL_RUNTIME_IMAGE", "keel/fake-runtime:dev")
os.environ.setdefault("KEEL_READY_DELAY", "2")
os.environ.setdefault("KEEL_WATCH_INTERVAL", "2")
os.environ.setdefault("KEEL_DEV_AUTH", "1")

from control import db, gateway, k8s
from control.domain.states import Status
from control.provisioner import main as provisioner
from control.tests.conftest import requires_db
from control.tests.test_provision_e2e import requires_cluster

GATEWAY_URL = os.environ.get("KEEL_GATEWAY_URL", "http://localhost:4000")


def _gateway_up() -> bool:
    try:
        return httpx.get(f"{GATEWAY_URL}/health/liveliness", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


requires_gateway = pytest.mark.skipif(
    not _gateway_up(),
    reason="no gateway (kubectl port-forward -n keel-gateway svc/litellm 4000:4000)",
)

pytestmark = [requires_db, requires_cluster, requires_gateway]

CPU_MODEL = "fake-cpu-test"


@pytest.fixture
async def catalog_entry():
    """A model whose image carries its own weights and needs no GPU -- the
    shape migration 0003 exists to allow."""
    async with db.transaction() as conn:
        await conn.execute(
            """insert into catalog_models
                 (id, mode, default_lane, accelerator, gpu_count, context_length,
                  license, status, engine_args, weights_uri)
               values (%s, 'self_hosted', 'b', 'cpu', 0, 8192, 'apache-2.0',
                       'validated', '{}', null)
               on conflict (id) do nothing""",
            (CPU_MODEL,),
        )
    yield CPU_MODEL
    async with db.transaction() as conn:
        await conn.execute("delete from catalog_models where id = %s", (CPU_MODEL,))


# Argument order is teardown order reversed: catalog_entry must be set up
# BEFORE team so it is torn down AFTER the deployments that reference it.
async def test_request_to_completion(client, catalog_entry, team, monkeypatch):
    """One request in, one working endpoint out, verified by a real completion."""
    monkeypatch.setenv("KEEL_GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("KEEL_GATEWAY_KEY", "sk-keel-dev-master")
    monkeypatch.setattr(gateway, "GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setattr(gateway, "GATEWAY_KEY", "sk-keel-dev-master")

    # 1. a developer asks for a model
    r = await client.post(
        "/v1/deployments",
        json={"team": team, "name": "chat", "model": catalog_entry},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert r.status_code == 202, r.text
    created = r.json()
    dep_id, route = created["id"], created["route"]
    assert created["status"] == "requested"

    # 2. the provisioner picks the job off the queue and does the work
    await k8s.load()
    cluster = k8s.Cluster()
    gw = gateway.build()
    assert isinstance(gw, gateway.LiteLLMGateway), "test needs the real gateway"
    try:
        # Claim this deployment's job specifically. Claiming whichever is
        # oldest makes the test depend on an empty queue, which it has no right
        # to assume. SKIP LOCKED itself is covered in test_api_delete.
        async with db.transaction() as conn:
            cur = await conn.execute(
                """update jobs set locked_until = now() + interval '15 minutes'
                    where deployment_id = %s
                returning id, deployment_id, kind, attempts""",
                (dep_id,),
            )
            job = await cur.fetchone()
        assert job is not None, "create did not enqueue a job"

        await asyncio.wait_for(provisioner.run_job(cluster, gw, job), timeout=300)

        # 3. the record says ready, and the event log explains how it got there
        detail = (await client.get(f"/v1/deployments/{dep_id}")).json()
        assert detail["status"] == Status.READY.value, detail["events"]
        phases = [e["to_status"] for e in reversed(detail["events"])]
        assert phases[0] == "requested"
        assert phases[-1] == "ready"
        assert "loading" in phases, f"never observed loading: {phases}"

        # 4. THE POINT: a real completion, addressed by the Keel route name.
        #    The client never learns what the backend calls itself.
        async with httpx.AsyncClient(timeout=60) as c:
            resp = await c.post(
                f"{GATEWAY_URL}/v1/chat/completions",
                headers={"Authorization": "Bearer sk-keel-dev-master"},
                json={
                    "model": route,
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 8,
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"]

        # 4b. a key issued through the API works on its own route and nowhere
        #     else. The unit tests use a fake gateway; this is the only place
        #     the real one is exercised.
        issued = await client.post(f"/v1/deployments/{dep_id}/keys", json={"max_budget_usd": 5})
        assert issued.status_code == 201, issued.text
        secret = issued.json()["key"]
        assert issued.json()["masked"].startswith("sk-...")

        async with httpx.AsyncClient(timeout=60) as c:
            allowed = await c.post(
                f"{GATEWAY_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {secret}"},
                json={
                    "model": route,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8,
                },
            )
        assert allowed.status_code == 200, allowed.text

        # Revoking must actually stop it, not merely update our record.
        key_id = issued.json()["id"]
        assert (await client.delete(f"/v1/deployments/{dep_id}/keys/{key_id}")).status_code == 204
        async with httpx.AsyncClient(timeout=60) as c:
            denied = await c.post(
                f"{GATEWAY_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {secret}"},
                json={
                    "model": route,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8,
                },
            )
        assert denied.status_code != 200, "a revoked key still worked"

        # 5. and the job is gone, so nothing re-runs it
        async with db.pool().connection() as conn:
            cur = await conn.execute(
                "select count(*) as n from jobs where deployment_id = %s", (dep_id,)
            )
            assert (await cur.fetchone())["n"] == 0
    finally:
        await gw.deregister(route)
        await cluster.delete("Namespace", f"keel-inf-{team}")
        await cluster.close()


async def test_backend_service_is_not_reachable_directly(catalog_entry):
    """Invariant I1: the gateway issues the only usable address.

    Every vLLM Service is ClusterIP, so there is no address a client outside the
    cluster could use even if they knew the name.
    """
    await k8s.load()
    cluster = k8s.Cluster()
    try:
        svcs = await cluster._call(
            "/api/v1/services",
            "GET",
            query=[("labelSelector", "app.kubernetes.io/managed-by=keel")],
        )
        for svc in svcs["items"]:
            ns = svc["metadata"]["namespace"]
            if ns.startswith("keel-inf-"):
                assert svc["spec"]["type"] == "ClusterIP", (
                    f"{ns}/{svc['metadata']['name']} is externally reachable"
                )
    finally:
        await cluster.close()
