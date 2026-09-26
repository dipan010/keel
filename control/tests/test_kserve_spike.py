"""ADR-0008 spike: does Keel sit cleanly on KServe?

Graded against the success criteria written into the ADR before the spike
began. Skips unless KServe is installed (deploy/kserve/install.sh).

This validates the INTEGRATION SHAPE with the fake runtime in Standard mode --
not LLMInferenceService, llm-d or KV offloading, which need real GPUs. A pass
here is necessary, not sufficient.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("KEEL_RUNTIME_IMAGE", "keel/fake-runtime:dev")
os.environ.setdefault("KEEL_READY_DELAY", "2")

from control import db, k8s
from control.domain.states import Status
from control.provisioner.status import Phase, classify
from control.reconciler.main import reconcile_once
from control.render import backend, kserve
from control.render import namespace as ns_render
from control.tests.conftest import DB_AVAILABLE
from control.tests.test_provision_e2e import requires_cluster

pytestmark = requires_cluster


def _kserve_installed() -> bool:
    async def check():
        await k8s.load()
        c = k8s.Cluster()
        try:
            crd = await c._call(
                "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/"
                "inferenceservices.serving.kserve.io",
                "GET",
            )
            return bool(crd)
        except Exception:  # noqa: BLE001
            return False
        finally:
            await c.close()

    try:
        return asyncio.new_event_loop().run_until_complete(check())
    except Exception:  # noqa: BLE001
        return False


requires_kserve = pytest.mark.skipif(
    not _kserve_installed(), reason="KServe not installed (deploy/kserve/install.sh)"
)


def _spec(dep_id: str, slug: str, **over) -> dict:
    return {
        "id": dep_id,
        "team_slug": slug,
        "model_id": "fake-model",
        "lane": "b",
        "replicas_min": 1,
        "replicas_max": 1,
        "accelerator": "cpu",
        "gpu_count": 0,
        "weights_uri": None,
        "model_ref": "fake/stand-in",
        "engine_args": {},
        "k8s_object_name": f"vllm-{dep_id}",
        # Every field explicit. Leave one out and KServe fills it -- memory 2Gi,
        # cpu 1 -- which a real vLLM would be OOMKilled under during weight load.
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        },
        **over,
    }


# ---------- pure: what gets rendered ----------


def test_the_mode_is_pinned_per_resource_not_left_to_the_cluster():
    isvc = kserve.render(_spec(str(uuid.uuid4()), "t"))[0]
    assert isvc["metadata"]["annotations"][kserve.MODE_ANNOTATION] == "Standard"


def test_keel_labels_are_on_the_resource_kserve_copies_from():
    d = str(uuid.uuid4())
    labels = kserve.render(_spec(d, "t"))[0]["metadata"]["labels"]
    assert labels["keel.io/deployment-id"] == d
    assert labels["app.kubernetes.io/managed-by"] == "keel"


def test_mounted_weights_are_refused_rather_than_guessed():
    with pytest.raises(NotImplementedError, match="weights_uri"):
        kserve.render(_spec(str(uuid.uuid4()), "t", weights_uri="s3://x"))


# ---------- live ----------


@pytest.fixture
async def isvc(monkeypatch):
    monkeypatch.setattr(backend, "BACKEND", "kserve")
    dep_id = str(uuid.uuid4())
    slug = f"ks{dep_id[:6]}"
    ns = f"keel-inf-{slug}"
    await k8s.load()
    c = k8s.Cluster()
    spec = _spec(dep_id, slug)
    for obj in ns_render.render(slug, 2):
        await c.apply(obj)
    for obj in backend.render(spec):
        await c.apply(obj)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 180
    while loop.time() < deadline:
        if (rej := await backend.reconcile_failure(c, spec, ns)) is not None:
            pytest.fail(f"KServe rejected the resource: {rej}")
        if classify(await c.pods_for(ns, dep_id)).phase is Phase.READY:
            break
        await asyncio.sleep(2)
    else:
        pytest.fail("InferenceService never became ready")
    try:
        yield c, spec, ns
    finally:
        await c.delete("Namespace", ns)
        await c.close()


@requires_kserve
async def test_criterion_1_no_ingress_exists(isvc):
    """I1. Asserted directly: the ClusterIP test stays green even when an
    Ingress exposes the backend, which is KServe's default behaviour."""
    c, _, ns = isvc
    for path in (
        f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses",
        f"/apis/gateway.networking.k8s.io/v1/namespaces/{ns}/httproutes",
    ):
        try:
            items = (await c._call(path, "GET")).get("items", [])
        except k8s.ApiException as exc:
            if exc.status == 404:  # Gateway API not installed: nothing to expose
                continue
            raise
        assert items == [], f"{path} is not empty -- backends are reachable around the gateway"


@requires_kserve
async def test_criterion_3_labels_reach_the_pods(isvc):
    c, spec, ns = isvc
    pods = await c.pods_for(ns, spec["id"])  # selects BY the label, so non-empty proves it
    assert pods
    lb = pods[0]["metadata"]["labels"]
    assert lb["app.kubernetes.io/managed-by"] == "keel"
    assert lb["keel.io/team"] == spec["team_slug"]


@requires_kserve
async def test_criterion_4_upstream_is_read_from_kserve_not_rebuilt(isvc):
    """KServe names the Service {name}-predictor on port 80; the manifest
    path's {name}...:8000 would point the gateway at nothing."""
    c, spec, ns = isvc
    url = await backend.upstream(c, spec, ns)
    assert url.startswith(f"http://{spec['k8s_object_name']}-predictor.{ns}.svc.cluster.local")
    assert ":8000" not in url


@requires_kserve
@pytest.mark.skipif(not DB_AVAILABLE, reason="reconciler reads desired state from Postgres")
async def test_the_reconciler_sees_no_drift_in_a_healthy_inferenceservice(isvc):
    """KServe's webhook defaults fields on the InferenceService. If material()
    trips over any of them, the reconciler reports drift forever and fights a
    controller it does not own -- the failure ADR-0008 set out to prevent."""
    c, spec, _ns = isvc
    team_id, dep_id = str(uuid.uuid4()), spec["id"]
    async with db.transaction() as conn:
        await conn.execute(
            "insert into teams (id, slug, oidc_group, gpu_quota) values (%s,%s,'keel-dev',2)",
            (team_id, spec["team_slug"]),
        )
        await conn.execute(
            """insert into catalog_models (id, mode, default_lane, accelerator, gpu_count,
                 context_length, license, status, engine_args, model_ref)
               values ('fake-model','self_hosted','b','cpu',0,8192,'apache-2.0',
                       'validated','{}','fake/stand-in') on conflict (id) do nothing""",
        )
        await conn.execute(
            """insert into deployments (id, team_id, name, mode, lane, model_id, status,
                 route_name, k8s_object_name, accelerator, gpu_count, replicas_min,
                 replicas_max, engine_args, created_by)
               values (%s,%s,'chat','self_hosted','b','fake-model','ready',%s,%s,
                       'cpu',0,1,1,'{}','spike')""",
            (dep_id, team_id, f"{spec['team_slug']}/chat", spec["k8s_object_name"]),
        )
    try:
        await reconcile_once(c)
        async with db.pool().connection() as conn:
            cur = await conn.execute(
                "select status, (select count(*) from jobs where deployment_id=%s) as jobs "
                "from deployments where id=%s",
                (dep_id, dep_id),
            )
            row = await cur.fetchone()
        assert row["status"] == Status.READY.value, "reconciler reported drift on a healthy ISVC"
        assert row["jobs"] == 0, "reconciler asked for a re-apply it did not need"
    finally:
        async with db.transaction() as conn:
            await conn.execute("delete from deployment_events where deployment_id=%s", (dep_id,))
            await conn.execute("delete from jobs where deployment_id=%s", (dep_id,))
            await conn.execute("delete from deployments where id=%s", (dep_id,))
            await conn.execute("delete from teams where id=%s", (team_id,))


@requires_kserve
async def test_teardown_removes_the_inferenceservice_and_its_pods(isvc):
    """KServe garbage-collects what it generated; Keel deletes only its own."""
    c, spec, ns = isvc
    for kind in backend.teardown_kinds():
        await c.delete(kind, spec["k8s_object_name"], ns)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 90
    while loop.time() < deadline:
        if not await c.pods_for(ns, spec["id"]):
            break
        await asyncio.sleep(2)
    assert await c.get("InferenceService", spec["k8s_object_name"], ns) is None
    assert not await c.pods_for(ns, spec["id"]), "KServe left pods behind"


@requires_kserve
async def test_kserve_undoes_manual_edits_to_the_deployment_it_generated(isvc):
    """The division of reconciliation in ADR-0008, observed rather than assumed.

    Container args are deliberately the field under test: an HPA can undo a
    replica change, but only KServe's controller rewrites the pod template. So
    reversion here is KServe enforcing the InferenceService, which is what lets
    Keel stop watching the Deployment at all.
    """
    c, spec, ns = isvc
    name = f"{spec['k8s_object_name']}-predictor"
    await c._call(
        f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
        "PATCH",
        body=[
            {"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--hacked"}
        ],
        headers={"Content-Type": "application/json-patch+json"},
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        dep = await c.get("Deployment", name, ns)
        if "--hacked" not in dep["spec"]["template"]["spec"]["containers"][0]["args"]:
            return
        await asyncio.sleep(2)
    pytest.fail("KServe did not undo a manual edit to its own Deployment within 60s")


@requires_kserve
@pytest.mark.skipif(not DB_AVAILABLE, reason="reconciler reads desired state from Postgres")
async def test_a_manual_edit_to_the_inferenceservice_is_keels_to_report(isvc):
    """The other half: the InferenceService is Keel's object. KServe will
    faithfully apply whatever it says, so an edit there is drift only Keel can
    see -- and must report, not fix, since the reconciler is read-only (I5)."""
    c, spec, ns = isvc
    team_id, dep_id = str(uuid.uuid4()), spec["id"]
    async with db.transaction() as conn:
        await conn.execute(
            "insert into teams (id, slug, oidc_group, gpu_quota) values (%s,%s,'keel-dev',2)",
            (team_id, spec["team_slug"]),
        )
        await conn.execute(
            """insert into catalog_models (id, mode, default_lane, accelerator, gpu_count,
                 context_length, license, status, engine_args, model_ref)
               values ('fake-model','self_hosted','b','cpu',0,8192,'apache-2.0',
                       'validated','{}','fake/stand-in') on conflict (id) do nothing""",
        )
        await conn.execute(
            """insert into deployments (id, team_id, name, mode, lane, model_id, status,
                 route_name, k8s_object_name, accelerator, gpu_count, replicas_min,
                 replicas_max, engine_args, created_by)
               values (%s,%s,'chat','self_hosted','b','fake-model','ready',%s,%s,
                       'cpu',0,1,1,'{}','spike')""",
            (dep_id, team_id, f"{spec['team_slug']}/chat", spec["k8s_object_name"]),
        )
    try:
        await c._call(
            f"/apis/serving.kserve.io/v1beta1/namespaces/{ns}/inferenceservices/"
            f"{spec['k8s_object_name']}",
            "PATCH",
            body={"spec": {"predictor": {"minReplicas": 3, "maxReplicas": 3}}},
            headers={"Content-Type": "application/merge-patch+json"},
        )
        # Wait for KServe to finish applying the edit. Asserting mid-rollout let
        # an earlier version accept DEGRADED too -- which would have passed even
        # with drift detection broken. Availability is judged before drift, so
        # only a READY resource isolates the drift check.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 180
        while loop.time() < deadline:
            live = await c.get("InferenceService", spec["k8s_object_name"], ns)
            conds = (live.get("status") or {}).get("conditions") or []
            ready = any(x["type"] == "Ready" and x["status"] == "True" for x in conds)
            if ready and len(await c.pods_for(ns, dep_id)) == 3:
                break
            await asyncio.sleep(3)
        else:
            pytest.fail("the edited InferenceService never settled at 3 replicas")

        await reconcile_once(c)
        async with db.pool().connection() as conn:
            cur = await conn.execute(
                """select d.status,
                          (select count(*) from jobs where deployment_id=d.id) as jobs,
                          (select reason_code from deployment_events
                            where deployment_id=d.id order by at desc limit 1) as reason
                     from deployments d where d.id=%s""",
                (dep_id,),
            )
            row = await cur.fetchone()
        assert row["status"] == Status.UPDATING.value, row
        assert row["reason"] == "drift", row
        assert row["jobs"] == 1, "drift must ask the Provisioner to re-apply"
        live = await c.get("InferenceService", spec["k8s_object_name"], ns)
        assert live["spec"]["predictor"]["minReplicas"] == 3, "the reconciler wrote to the cluster"
    finally:
        async with db.transaction() as conn:
            await conn.execute("delete from deployment_events where deployment_id=%s", (dep_id,))
            await conn.execute("delete from jobs where deployment_id=%s", (dep_id,))
            await conn.execute("delete from deployments where id=%s", (dep_id,))
            await conn.execute("delete from teams where id=%s", (team_id,))


@requires_kserve
@pytest.mark.skipif(not DB_AVAILABLE, reason="reconciler reads desired state from Postgres")
async def test_an_orphaned_inferenceservice_is_reported_and_survives(monkeypatch):
    """I5 over the new owned kind: labelled ours, no row -> alert, never delete."""
    monkeypatch.setattr(backend, "BACKEND", "kserve")
    ghost = str(uuid.uuid4())
    slug = f"og{ghost[:6]}"
    ns = f"keel-inf-{slug}"
    await k8s.load()
    c = k8s.Cluster()
    try:
        for obj in ns_render.render(slug, 2):
            await c.apply(obj)
        for obj in backend.render(_spec(ghost, slug)):
            await c.apply(obj)
        counts = await reconcile_once(c)
        assert counts["orphans"] >= 1
        assert await c.get("InferenceService", f"vllm-{ghost}", ns) is not None, "I5 violated"
    finally:
        await c.delete("Namespace", ns)
        await c.close()


@requires_kserve
async def test_explicit_resources_survive_and_nothing_is_left_to_kserve(isvc):
    """KServe injects cpu: 1 / memory: 2Gi into any field left unset -- as both
    request and limit. A real vLLM would be OOMKilled during weight load under
    that; the fake uses ~100 MB, so only inspecting the pod found it. Asserted
    on the POD, because the InferenceService spec only shows what we asked."""
    c, spec, ns = isvc
    pod = (await c.pods_for(ns, spec["id"]))[0]
    got = pod["spec"]["containers"][0]["resources"]
    assert got["requests"] == spec["resources"]["requests"]
    assert got["limits"] == spec["resources"]["limits"], "KServe replaced an explicit limit"


@requires_kserve
async def test_lane_b_does_not_autoscale_on_cpu(monkeypatch):
    """Standard mode creates an HPA on CPU at 80% -- the wrong signal for a
    GPU-bound engine. The manifest backend never autoscaled lane B; passing
    replicas_max through silently added it. min == max keeps it inert."""
    monkeypatch.setattr(backend, "BACKEND", "kserve")
    dep_id = str(uuid.uuid4())
    slug = f"hp{dep_id[:6]}"
    ns = f"keel-inf-{slug}"
    await k8s.load()
    c = k8s.Cluster()
    try:
        for obj in ns_render.render(slug, 2):
            await c.apply(obj)
        for obj in backend.render(_spec(dep_id, slug, replicas_max=3)):
            await c.apply(obj)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90
        hpas: list = []
        while loop.time() < deadline:
            hpas = (
                await c._call(
                    f"/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers", "GET"
                )
            ).get("items", [])
            if hpas:
                break
            await asyncio.sleep(3)
        for h in hpas:
            assert h["spec"]["minReplicas"] == h["spec"]["maxReplicas"], h["spec"]
    finally:
        await c.delete("Namespace", ns)
        await c.close()


@requires_kserve
async def test_gpu_placement_survives_kserve(monkeypatch):
    """Every other spike test uses gpu_count 0, so selector, toleration and the
    GPU limit had never met a scheduler through KServe. Needs make fake-gpu."""
    monkeypatch.setattr(backend, "BACKEND", "kserve")
    await k8s.load()
    c = k8s.Cluster()
    gpu_node = os.environ.get("KEEL_GPU_NODE", "k3d-keel-gpu-0")
    node = await c.get("Node", gpu_node)
    if not node or not (node["status"].get("allocatable") or {}).get("nvidia.com/gpu"):
        await c.close()
        pytest.skip("no node advertising nvidia.com/gpu (make fake-gpu)")
    dep_id = str(uuid.uuid4())
    slug = f"gp{dep_id[:6]}"
    ns = f"keel-inf-{slug}"
    try:
        for obj in ns_render.render(slug, 2):
            await c.apply(obj)
        for obj in backend.render(_spec(dep_id, slug, accelerator="l4", gpu_count=1)):
            await c.apply(obj)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 180
        while loop.time() < deadline:
            if classify(await c.pods_for(ns, dep_id)).phase is Phase.READY:
                break
            await asyncio.sleep(3)
        else:
            pytest.fail("GPU InferenceService never became ready")
        pod = (await c.pods_for(ns, dep_id))[0]
        assert pod["spec"]["nodeName"] == gpu_node
        assert pod["spec"]["nodeSelector"] == {"keel.io/accelerator": "l4"}
        assert pod["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"
        quota = await c.get("ResourceQuota", "keel-quota", ns)
        assert quota["status"]["used"]["requests.nvidia.com/gpu"] == "1"
    finally:
        await c.delete("Namespace", ns)
        await c.close()
