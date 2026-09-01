"""GPU PLACEMENT -- not GPU execution.

The cluster node advertises nvidia.com/gpu through the extended-resource
mechanism (deploy/kind/fake-gpu.sh). No GPU exists, no CUDA exists, and no
model is accelerated. What these tests prove is that the scheduler accepts our
manifests and rejects the ones it should:

    nodeSelector on keel.io/accelerator
    toleration for the keel.io/gpu taint
    resources.limits."nvidia.com/gpu"
    ResourceQuota on requests.nvidia.com/gpu
    the unschedulable failure path, against real pod conditions

A green run here says NOTHING about whether vLLM works on a GPU.

Setup:  make cluster && make fake && make fake-gpu
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("KEEL_RUNTIME_IMAGE", "keel/fake-runtime:dev")
os.environ.setdefault("KEEL_READY_DELAY", "2")

from control import k8s
from control.provisioner.status import UNSCHEDULABLE_GRACE_SECONDS, Phase, classify
from control.render import manifests
from control.render import namespace as ns_render
from control.tests.test_provision_e2e import requires_cluster

pytestmark = requires_cluster

GPU_NODE = os.environ.get("KEEL_GPU_NODE", "k3d-keel-gpu-0")


async def _gpu_node_ready(c: k8s.Cluster) -> bool:
    node = await c.get("Node", GPU_NODE)
    if node is None:
        return False
    return bool((node["status"].get("allocatable") or {}).get("nvidia.com/gpu"))


@pytest.fixture
async def cluster():
    await k8s.load()
    c = k8s.Cluster()
    try:
        if not await _gpu_node_ready(c):
            pytest.skip(f"{GPU_NODE} advertises no nvidia.com/gpu (run: make fake-gpu)")
        yield c
    finally:
        await c.close()


@pytest.fixture
def spec():
    dep_id = str(uuid.uuid4())
    return {
        "id": dep_id,
        "team_slug": f"gpu{dep_id[:6]}",
        "lane": "b",
        "replicas_min": 1,
        "replicas_max": 1,
        "accelerator": "l4",
        "gpu_count": 1,
        "weights_uri": None,
        "engine_args": {},
    }


async def _apply_all(c: k8s.Cluster, spec: dict, quota: int = 4) -> str:
    ns = f"keel-inf-{spec['team_slug']}"
    for obj in ns_render.render(spec["team_slug"], quota):
        await c.apply(obj)
    for obj in manifests.render(spec):
        await c.apply(obj)
    return ns


async def _wait_pod(c: k8s.Cluster, ns: str, dep_id: str, timeout: float = 60) -> list[dict]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        pods = await c.pods_for(ns, dep_id)
        if pods:
            return pods
        await asyncio.sleep(1)
    return []


async def test_gpu_request_lands_on_the_gpu_node(cluster, spec):
    """The nodeSelector, the toleration and the resource limit, together.

    None of these are exercised by any other test: every deployment so far has
    used gpu_count 0, which renders none of them.
    """
    ns = await _apply_all(cluster, spec)
    try:
        rendered = next(o for o in manifests.render(spec) if o["kind"] == "Deployment")
        pod_spec = rendered["spec"]["template"]["spec"]
        assert pod_spec["nodeSelector"] == {"keel.io/accelerator": "l4"}
        assert pod_spec["tolerations"][0]["key"] == "keel.io/gpu"
        assert pod_spec["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1

        pods = await _wait_pod(cluster, ns, spec["id"])
        assert pods, "no pod was created"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 120
        while loop.time() < deadline:
            pods = await cluster.pods_for(ns, spec["id"])
            out = classify(pods)
            assert out.phase is not Phase.FAILED, f"{out.reason_code}: {out.message}"
            if out.phase is Phase.READY:
                break
            await asyncio.sleep(2)

        assert classify(await cluster.pods_for(ns, spec["id"])).phase is Phase.READY
        assert pods[0]["spec"]["nodeName"] == GPU_NODE, (
            f"scheduled onto {pods[0]['spec']['nodeName']}, not the GPU node"
        )
    finally:
        await cluster.delete("Namespace", ns)


async def test_unknown_accelerator_is_unschedulable_for_real(cluster, spec):
    """The classifier has only ever seen a hand-written fixture for this.

    Here it reads an actual PodScheduled=False/Unschedulable condition produced
    by a real scheduler, and the 120s grace period is exercised end to end.
    """
    spec["accelerator"] = "h100-does-not-exist"
    ns = await _apply_all(cluster, spec)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        cond = None
        while loop.time() < deadline:
            pods = await cluster.pods_for(ns, spec["id"])
            if pods:
                conds = {c["type"]: c for c in pods[0]["status"].get("conditions") or []}
                sched = conds.get("PodScheduled", {})
                if sched.get("reason") == "Unschedulable":
                    cond = sched
                    break
            await asyncio.sleep(2)

        assert cond is not None, "scheduler never reported Unschedulable"
        pods = await cluster.pods_for(ns, spec["id"])

        # Inside the grace period this is not a failure: a node may be draining
        # or another pod terminating.
        assert classify(pods, elapsed_seconds=5).phase is Phase.SCHEDULING

        # Past it, it is permanent, and the message names the accelerator.
        out = classify(pods, elapsed_seconds=UNSCHEDULABLE_GRACE_SECONDS + 1)
        assert out.phase is Phase.FAILED
        assert out.reason_code == "unschedulable"
        assert "affinity" in out.message or "selector" in out.message or "node" in out.message
    finally:
        await cluster.delete("Namespace", ns)


async def test_resourcequota_rejects_the_gpu_that_exceeds_it(cluster, spec):
    """The quota backstop, which nothing has verified until now.

    Quota is enforced in two places: the API's gpu_in_use check, and this
    ResourceQuota. Only the first has tests -- yet this one is what protects
    against anything reaching the cluster WITHOUT going through our API.

    Note quota is enforced at POD admission, not on the Deployment, so the
    Deployment is accepted and its ReplicaSet fails to create the pod.
    """
    team = spec["team_slug"]
    ns = f"keel-inf-{team}"
    for obj in ns_render.render(team, 2):  # quota: 2 GPUs
        await cluster.apply(obj)
    try:
        ids = []
        for i in range(3):
            s = {**spec, "id": str(uuid.uuid4())}
            ids.append(s["id"])
            for obj in manifests.render(s):
                await cluster.apply(obj)

        # Let the ReplicaSets try.
        await asyncio.sleep(12)

        running = 0
        for dep_id in ids:
            running += len(await cluster.pods_for(ns, dep_id))
        assert running == 2, f"quota of 2 GPUs admitted {running} pods"

        quota = await cluster.get("ResourceQuota", "keel-quota", ns)
        used = quota["status"]["used"]["requests.nvidia.com/gpu"]
        hard = quota["status"]["hard"]["requests.nvidia.com/gpu"]
        assert (used, hard) == ("2", "2")
    finally:
        await cluster.delete("Namespace", ns)
