"""Deployment record -> Kubernetes manifests.

This module is the seam. A second backend (AML, a different runtime) plugs in
here and nowhere else -- which is what keeps the Azure door in the architecture
doc's section 4.1 cheap to walk through later.
"""

from __future__ import annotations

from typing import Any

from control.domain.rules import namespace, object_name
from control.domain.states import Lane

MANAGED_BY = "keel"
LABEL_MANAGED = "app.kubernetes.io/managed-by"
LABEL_DEPLOYMENT = "keel.io/deployment-id"
LABEL_TEAM = "keel.io/team"
LABEL_ACCELERATOR = "keel.io/accelerator"
TAINT_GPU = "keel.io/gpu"

VLLM_IMAGE = "vllm/vllm-openai:v0.11.0"
VLLM_PORT = 8000


def labels(deployment_id: str, team: str) -> dict[str, str]:
    return {
        LABEL_MANAGED: MANAGED_BY,
        LABEL_DEPLOYMENT: deployment_id,
        LABEL_TEAM: team,
    }


def render(dep: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every object for one self-hosted deployment.

    Pure: takes a row, returns dicts. No cluster access, so it is trivially
    snapshot-testable and CI can diff rendered output without a GPU.
    """
    name = object_name(dep["id"])
    ns = namespace(dep["team_slug"])
    lb = labels(dep["id"], dep["team_slug"])
    lane = Lane(dep["lane"])

    objects = [_deployment(dep, name, ns, lb), _service(name, ns, lb)]
    if lane is Lane.C:
        objects.append(_scaled_object(dep, name, ns, lb))
    return objects


def _deployment(dep: dict[str, Any], name: str, ns: str, lb: dict[str, str]) -> dict[str, Any]:
    args = ["--model", "/models/weights", "--port", str(VLLM_PORT)]
    for flag, value in (dep.get("engine_args") or {}).items():
        args += [f"--{flag}", str(value)]

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": lb},
        "spec": {
            # lane c starts at zero; KEDA owns the count from there
            "replicas": 0 if Lane(dep["lane"]) is Lane.C else dep["replicas_min"],
            "selector": {"matchLabels": {LABEL_DEPLOYMENT: dep["id"]}},
            "template": {
                "metadata": {"labels": lb},
                "spec": {
                    "nodeSelector": {LABEL_ACCELERATOR: dep["accelerator"]},
                    "tolerations": [
                        {"key": TAINT_GPU, "operator": "Exists", "effect": "NoSchedule"}
                    ],
                    # Fetch weights into the node-local digest cache, or skip if
                    # already there. Cold node: minutes. Warm node: seconds.
                    # This is the single biggest lever on time-to-ready.
                    "initContainers": [
                        {
                            "name": "fetch-weights",
                            "image": "keel/weights-fetcher:dev",
                            "env": [
                                {"name": "WEIGHTS_URI", "value": dep["weights_uri"]},
                                {"name": "CACHE_DIR", "value": "/cache"},
                            ],
                            "volumeMounts": [
                                {"name": "cache", "mountPath": "/cache"},
                                {"name": "models", "mountPath": "/models"},
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": "vllm",
                            "image": dep.get("image") or VLLM_IMAGE,
                            "args": args,
                            "ports": [{"containerPort": VLLM_PORT}],
                            "resources": {"limits": {"nvidia.com/gpu": dep.get("gpu_count", 1)}},
                            "volumeMounts": [{"name": "models", "mountPath": "/models"}],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": VLLM_PORT},
                                # Weight loading is slow and that is not a fault.
                                # Fail here and the pod restarts forever.
                                "initialDelaySeconds": 30,
                                "periodSeconds": 10,
                                "failureThreshold": 90,
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": "cache",
                            "hostPath": {
                                "path": "/var/lib/keel/weights",
                                "type": "DirectoryOrCreate",
                            },
                        },
                        {"name": "models", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def _service(name: str, ns: str, lb: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": lb},
        "spec": {
            # ClusterIP, always. The gateway is the only Ingress in the cluster;
            # that is what makes "the URL is always ours" enforced by Kubernetes
            # rather than by convention.
            "type": "ClusterIP",
            "selector": {LABEL_DEPLOYMENT: lb[LABEL_DEPLOYMENT]},
            "ports": [{"port": VLLM_PORT, "targetPort": VLLM_PORT}],
        },
    }


def _scaled_object(dep: dict[str, Any], name: str, ns: str, lb: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {"name": name, "namespace": ns, "labels": lb},
        "spec": {
            "scaleTargetRef": {"name": name},
            "minReplicaCount": 0,
            "maxReplicaCount": dep["replicas_max"],
            # Waking means scheduling onto a GPU node and loading weights into
            # VRAM: tens of seconds for 7B, minutes for a large model on a cold
            # node. Lane C is right for dev and eval traffic, wrong for anything
            # user-facing, and the platform should say so at request time.
            "cooldownPeriod": 300,
            "triggers": [
                {
                    "type": "prometheus",
                    "metadata": {
                        "serverAddress": "http://prometheus.keel-system:9090",
                        "query": f'sum(rate(vllm:request_success_total{{deployment_id="{dep["id"]}"}}[2m]))',
                        "threshold": "0.1",
                    },
                }
            ],
        },
    }
