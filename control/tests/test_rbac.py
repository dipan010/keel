"""The invariants are only structural if the API server actually enforces them.

These assert against a live authorizer via SubjectAccessReview, so they fail if
someone widens a ClusterRole -- which is exactly the change that would quietly
turn "the Reconciler cannot delete" from a guarantee into a comment.

Needs: kubectl apply -f deploy/rbac.yaml
"""

from __future__ import annotations

import pytest

from control import k8s
from control.tests.test_provision_e2e import requires_cluster

pytestmark = requires_cluster

SA = "system:serviceaccount:keel-system:{}"


async def _can(
    cluster: k8s.Cluster,
    sa: str,
    verb: str,
    resource: str,
    group: str = "",
    namespace: str | None = None,
) -> bool:
    res = await cluster._call(
        "/apis/authorization.k8s.io/v1/subjectaccessreviews",
        "POST",
        body={
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SubjectAccessReview",
            "spec": {
                "user": SA.format(sa),
                "resourceAttributes": {
                    "verb": verb,
                    "resource": resource,
                    "group": group,
                    **({"namespace": namespace} if namespace else {}),
                },
            },
        },
        headers={"Content-Type": "application/json"},
    )
    return bool(res["status"]["allowed"])


@pytest.fixture
async def cluster():
    await k8s.load()
    c = k8s.Cluster()
    try:
        if await c.get("Namespace", "keel-system") is None:
            pytest.skip("keel-system namespace absent; apply deploy/rbac.yaml")
        yield c
    finally:
        await c.close()


async def test_reconciler_can_observe(cluster):
    assert await _can(cluster, "keel-reconciler", "list", "deployments", "apps")
    assert await _can(cluster, "keel-reconciler", "get", "pods/log")
    assert await _can(cluster, "keel-reconciler", "list", "namespaces")


@pytest.mark.parametrize(
    ("verb", "resource", "group"),
    [
        ("delete", "deployments", "apps"),
        ("patch", "deployments", "apps"),
        ("update", "deployments", "apps"),
        ("create", "deployments", "apps"),
        ("delete", "namespaces", ""),
        ("delete", "services", ""),
    ],
)
async def test_reconciler_cannot_act(cluster, verb, resource, group):
    """Invariant I5. Read-only by construction: it cannot delete an orphan even
    if the code tried to."""
    assert not await _can(cluster, "keel-reconciler", verb, resource, group)


async def test_provisioner_can_create_namespaces(cluster):
    assert await _can(cluster, "keel-provisioner", "create", "namespaces")


async def test_provisioner_workload_rights_are_per_namespace(cluster, team_ns):
    """Granted by a RoleBinding that render/namespace.py creates at onboarding.

    Cluster-wide it has none: that is what makes I4 structural. This test failed
    when written, because nothing was creating the RoleBinding -- the local
    Provisioner only worked because it ran with an admin kubeconfig.
    """
    assert not await _can(cluster, "keel-provisioner", "patch", "deployments", "apps")
    assert await _can(
        cluster, "keel-provisioner", "patch", "deployments", "apps", namespace=team_ns
    )


@pytest.fixture
async def team_ns(cluster):
    from control.render import namespace as ns_render

    slug = "rbacprobe"
    for obj in ns_render.render(slug, 1):
        await cluster.apply(obj)
    yield f"keel-inf-{slug}"
    await cluster.delete("Namespace", f"keel-inf-{slug}")


@pytest.mark.parametrize(
    ("verb", "resource", "group"),
    [
        ("delete", "namespaces", ""),
        ("get", "secrets", ""),
        ("create", "clusterrolebindings", "rbac.authorization.k8s.io"),
    ],
)
async def test_provisioner_is_not_cluster_admin(cluster, verb, resource, group):
    """Invariant I4. A bug cannot reach keel-system or anything outside our
    namespaces."""
    assert not await _can(cluster, "keel-provisioner", verb, resource, group)


@pytest.mark.parametrize(
    ("verb", "resource", "group"),
    [("list", "deployments", "apps"), ("get", "pods", ""), ("create", "namespaces", "")],
)
async def test_control_api_has_no_cluster_access_whatsoever(cluster, verb, resource, group):
    """The API records intent and enqueues. It never touches the cluster, and
    the absence of a RoleBinding is what keeps that true."""
    assert not await _can(cluster, "keel-api", verb, resource, group)
