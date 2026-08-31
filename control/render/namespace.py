"""Per-team namespace: quota and a default-deny network policy.

Namespaces replace resource groups as the isolation, quota and attribution
boundary -- and do it better, because ResourceQuota and NetworkPolicy are native
objects rather than a governance layer bolted alongside.
"""

from __future__ import annotations

from typing import Any

from control.domain.rules import namespace
from control.render.manifests import LABEL_MANAGED, LABEL_TEAM, MANAGED_BY

GATEWAY_NS = "keel-gateway"
CONTROL_NS = "keel-system"
PROVISIONER_SA = "keel-provisioner"


def render(team_slug: str, gpu_quota: int) -> list[dict[str, Any]]:
    ns = namespace(team_slug)
    lb = {LABEL_MANAGED: MANAGED_BY, LABEL_TEAM: team_slug}
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": lb}},
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "keel-quota", "namespace": ns, "labels": lb},
            "spec": {"hard": {"requests.nvidia.com/gpu": str(gpu_quota)}},
        },
        {
            # The Provisioner's write permission is granted per namespace, here,
            # at onboarding. There is deliberately no ClusterRoleBinding for it:
            # that is what keeps invariant I4 ("nothing is created outside
            # keel-inf-*") enforced by the API server rather than by our code
            # remembering to scope its own writes.
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "keel-provisioner", "namespace": ns, "labels": lb},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": PROVISIONER_SA,
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": PROVISIONER_SA, "namespace": CONTROL_NS}
            ],
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "keel-default-deny", "namespace": ns, "labels": lb},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                # Only the gateway may reach a team's workloads. A developer who
                # learns a Service DNS name still cannot call it, and one team
                # cannot reach another's.
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": GATEWAY_NS}
                                }
                            }
                        ]
                    }
                ],
            },
        },
    ]
