"""Reconcile against a real cluster and a real database.

The decisions are unit-tested in test_reconcile.py; this checks the parts that
only break in contact with an API server -- label selectors, list shapes, and
that a manual kubectl edit is actually detected.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("KEEL_RUNTIME_IMAGE", "keel/fake-runtime:dev")
os.environ.setdefault("KEEL_READY_DELAY", "2")
os.environ.setdefault("KEEL_DEV_AUTH", "1")

from control import db, k8s
from control.domain.states import Status
from control.reconciler.main import reconcile_once
from control.render import manifests
from control.render import namespace as ns_render
from control.tests.conftest import requires_db
from control.tests.test_provision_e2e import requires_cluster

pytestmark = [requires_db, requires_cluster]

CPU_MODEL = "fake-recon-test"


@pytest.fixture
async def catalog_entry():
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


async def _seed(team: str, status: str = "ready") -> tuple[str, dict]:
    """A deployment row plus the objects it describes, applied for real."""
    dep_id = str(uuid.uuid4())
    async with db.transaction() as conn:
        cur = await conn.execute("select id from teams where slug = %s", (team,))
        team_id = (await cur.fetchone())["id"]
        await conn.execute(
            """insert into deployments
                 (id, team_id, name, mode, lane, model_id, status, route_name,
                  k8s_object_name, accelerator, gpu_count, replicas_min,
                  replicas_max, engine_args, created_by)
               values (%s, %s, 'chat', 'self_hosted', 'b', %s, %s, %s, %s,
                       'cpu', 0, 1, 1, '{}', 'test')""",
            (dep_id, team_id, CPU_MODEL, status, f"{team}/chat", f"vllm-{dep_id}"),
        )
    spec = {
        "id": dep_id,
        "team_slug": team,
        "lane": "b",
        "replicas_min": 1,
        "replicas_max": 1,
        "accelerator": "cpu",
        "gpu_count": 0,
        "weights_uri": None,
        "engine_args": {},
    }
    return dep_id, spec


async def _status(dep_id: str) -> str:
    async with db.pool().connection() as conn:
        cur = await conn.execute("select status from deployments where id = %s", (dep_id,))
        return (await cur.fetchone())["status"]


async def _jobs(dep_id: str) -> int:
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select count(*) as n from jobs where deployment_id = %s", (dep_id,)
        )
        return (await cur.fetchone())["n"]


async def test_missing_workload_is_detected_and_requeued(catalog_entry, team):
    """A row that says ready with nothing in the cluster behind it."""
    dep_id, _ = await _seed(team)
    await k8s.load()
    cluster = k8s.Cluster()
    try:
        counts = await reconcile_once(cluster)
        assert counts["checked"] >= 1
        assert await _status(dep_id) == Status.DEGRADED.value
        assert await _jobs(dep_id) == 1, "drift should ask the Provisioner to fix it"
    finally:
        await cluster.close()


async def test_manual_scale_is_detected_as_drift(catalog_entry, team):
    """Someone runs kubectl scale during an incident and never mentions it."""
    dep_id, spec = await _seed(team)
    await k8s.load()
    cluster = k8s.Cluster()
    ns = f"keel-inf-{team}"
    try:
        for obj in ns_render.render(team, 2):
            await cluster.apply(obj)
        for obj in manifests.render(spec):
            await cluster.apply(obj)

        # Wait until it is genuinely serving. Scaling something that is still
        # coming up would trip the availability check instead of the drift
        # check, and prove nothing about drift.
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            obj = await cluster.get("Deployment", f"vllm-{dep_id}", ns)
            if (obj.get("status") or {}).get("readyReplicas"):
                break
            await asyncio.sleep(2)
        else:
            pytest.fail("workload never became ready")

        # Now be the human with kubectl during an incident.
        live = await cluster.get("Deployment", f"vllm-{dep_id}", ns)
        live["spec"]["replicas"] = 4
        await cluster._call(
            f"/apis/apps/v1/namespaces/{ns}/deployments/vllm-{dep_id}",
            "PUT",
            body=live,
            headers={"Content-Type": "application/json"},
        )

        await reconcile_once(cluster)
        assert await _status(dep_id) == Status.UPDATING.value
        assert await _jobs(dep_id) == 1, "drift must ask the Provisioner to re-apply"

        # And the Reconciler did not fix it itself -- it has no write access.
        after = await cluster.get("Deployment", f"vllm-{dep_id}", ns)
        assert after["spec"]["replicas"] == 4, (
            "the Reconciler wrote to the cluster; it must only enqueue"
        )
    finally:
        await cluster.delete("Namespace", ns)
        await cluster.close()


async def test_orphans_are_reported_and_survive(catalog_entry, team):
    """Labelled ours, no row. Alert, never delete -- invariant I5."""
    await k8s.load()
    cluster = k8s.Cluster()
    ns = f"keel-inf-{team}"
    ghost = str(uuid.uuid4())
    try:
        for obj in ns_render.render(team, 2):
            await cluster.apply(obj)
        spec = {
            "id": ghost,
            "team_slug": team,
            "lane": "b",
            "replicas_min": 1,
            "replicas_max": 1,
            "accelerator": "cpu",
            "gpu_count": 0,
            "weights_uri": None,
            "engine_args": {},
        }
        for obj in manifests.render(spec):
            await cluster.apply(obj)

        counts = await reconcile_once(cluster)
        assert counts["orphans"] >= 1

        # The whole point: it is still there.
        still = await cluster.get("Deployment", f"vllm-{ghost}", ns)
        assert still is not None, "the reconciler deleted an orphan -- I5 violated"
    finally:
        await cluster.delete("Namespace", ns)
        await cluster.close()


async def test_deployments_with_open_jobs_are_skipped(catalog_entry, team):
    """The Provisioner is mid-flight; a second writer would fight it."""
    dep_id, _ = await _seed(team)
    async with db.transaction() as conn:
        await conn.execute(
            "insert into jobs (deployment_id, kind) values (%s, 'provision')", (dep_id,)
        )
    await k8s.load()
    cluster = k8s.Cluster()
    try:
        await reconcile_once(cluster)
        # Untouched: still ready, despite nothing existing in the cluster.
        assert await _status(dep_id) == Status.READY.value
    finally:
        await cluster.close()


async def test_transient_states_are_not_reconciled(catalog_entry, team):
    dep_id, _ = await _seed(team, status="loading")
    await k8s.load()
    cluster = k8s.Cluster()
    try:
        await reconcile_once(cluster)
        assert await _status(dep_id) == "loading"
    finally:
        await cluster.close()
