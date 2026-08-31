"""End-to-end against a real API server, using the fake runtime.

This is the test that would have caught every server-side-apply and pod-status
mistake so far. It needs a cluster but no GPU: the deployment renders with
gpu_count 0, so it schedules anywhere.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("KEEL_RUNTIME_IMAGE", "keel/fake-runtime:dev")
os.environ.setdefault("KEEL_READY_DELAY", "2")

from control import k8s
from control.provisioner.status import Phase, classify
from control.render import manifests
from control.render import namespace as ns_render


def _cluster_available() -> bool:
    from kubernetes_asyncio import config

    # A probe deliberately catches everything: no kubeconfig, an unreachable
    # server, expired credentials and a half-written context all mean the same
    # thing here -- skip, do not fail the suite.
    async def check():
        try:
            await config.load_kube_config()
            c = k8s.Cluster()
        except Exception:  # noqa: BLE001
            return False
        try:
            await c.get("Namespace", "default")
        except Exception:  # noqa: BLE001
            return False
        else:
            return True
        finally:
            await c.close()

    try:
        return asyncio.new_event_loop().run_until_complete(check())
    except Exception:  # noqa: BLE001
        return False


requires_cluster = pytest.mark.skipif(
    not _cluster_available(), reason="no reachable cluster (run: make cluster)"
)

pytestmark = requires_cluster


@pytest.fixture
def spec():
    dep_id = str(uuid.uuid4())
    return {
        "id": dep_id,
        "team_slug": f"e2e{dep_id[:6]}",
        "lane": "b",
        "replicas_min": 1,
        "replicas_max": 1,
        "accelerator": "cpu",
        "gpu_count": 0,  # CPU serving -- no GPU needed to exercise this path
        "weights_uri": None,  # fake runtime carries its own "weights"
        "engine_args": {},
    }


async def test_apply_is_idempotent_and_converges(spec):
    """A redelivered job must converge on the same objects, not duplicate them.
    This is the whole reason object names derive from the deployment id."""
    await k8s.load()
    c = k8s.Cluster()
    ns = f"keel-inf-{spec['team_slug']}"
    try:
        for obj in ns_render.render(spec["team_slug"], 2):
            await c.apply(obj)

        for _ in range(3):  # apply three times, as a redelivery would
            for obj in manifests.render(spec):
                await c.apply(obj)

        dep = await c.get("Deployment", f"vllm-{spec['id']}", ns)
        assert dep is not None
        assert dep["spec"]["replicas"] == 1

        # We own the resource itself. The k3s controller legitimately co-owns
        # the status subresource -- that is not drift.
        owners = {
            m["manager"] for m in dep["metadata"]["managedFields"] if not m.get("subresource")
        }
        assert owners == {k8s.FIELD_MANAGER}, f"unexpected owners of spec: {owners}"

        # The real proof: generation only increments on an actual spec change.
        # Three applies leaving it at 1 means the API server saw no change at
        # all -- true convergence, not a same-shaped overwrite each time.
        assert dep["metadata"]["generation"] == 1, "repeated apply mutated the object"

        listed = await c._call(
            f"/apis/apps/v1/namespaces/{ns}/deployments",
            "GET",
            query=[("labelSelector", f"keel.io/deployment-id={spec['id']}")],
        )
        assert len(listed["items"]) == 1, "apply duplicated the deployment"
    finally:
        await c.delete("Namespace", ns)
        await c.close()


async def test_pod_reaches_ready_through_the_real_status_path(spec):
    """scheduling -> loading -> ready, driven by classify() against real pods."""
    await k8s.load()
    c = k8s.Cluster()
    ns = f"keel-inf-{spec['team_slug']}"
    try:
        for obj in ns_render.render(spec["team_slug"], 2):
            await c.apply(obj)
        for obj in manifests.render(spec):
            await c.apply(obj)

        seen: list[Phase] = []
        deadline = asyncio.get_running_loop().time() + 150
        while asyncio.get_running_loop().time() < deadline:
            pods = await c.pods_for(ns, spec["id"])
            out = classify(pods)
            if not seen or seen[-1] is not out.phase:
                seen.append(out.phase)
            if out.phase is Phase.READY:
                break
            assert out.phase is not Phase.FAILED, f"{out.reason_code}: {out.message}"
            await asyncio.sleep(2)

        assert seen[-1] is Phase.READY, f"never became ready; saw {seen}"
        assert seen[0] is Phase.SCHEDULING
        assert Phase.LOADING in seen, f"never observed loading; saw {seen}"
    finally:
        await c.delete("Namespace", ns)
        await c.close()


async def test_networkpolicy_and_quota_are_applied(spec):
    await k8s.load()
    c = k8s.Cluster()
    ns = f"keel-inf-{spec['team_slug']}"
    try:
        for obj in ns_render.render(spec["team_slug"], 3):
            await c.apply(obj)
        quota = await c.get("ResourceQuota", "keel-quota", ns)
        assert quota["spec"]["hard"]["requests.nvidia.com/gpu"] == "3"
        np = await c.get("NetworkPolicy", "keel-default-deny", ns)
        # Only the gateway may reach a team's workloads.
        selector = np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]
        assert selector["matchLabels"]["kubernetes.io/metadata.name"] == "keel-gateway"
    finally:
        await c.delete("Namespace", ns)
        await c.close()
