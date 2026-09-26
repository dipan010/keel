"""Deployment record -> a KServe InferenceService.

SPIKE (ADR-0008, Proposed). KServe owns InferenceService -> Deployment,
Service and pods; Keel owns row -> InferenceService and never inspects the
Deployment KServe generates.

Standard mode (v0.21's name for what was RawDeployment): no Knative, no Istio.
Requires the cluster configuration in deploy/kserve/install.sh -- in particular
disableIngressCreation, without which KServe exposes every backend outside the
gateway and invariant I1 fails while the existing ClusterIP test stays green.

Placement, resources and the runtime class are shared with the manifest
renderer on purpose: a GPU pod must land and behave identically whichever
backend rendered it.
"""

from __future__ import annotations

from typing import Any

from control.domain.rules import namespace, object_name
from control.domain.states import Lane
from control.render.manifests import (
    READY_DELAY_SECONDS,
    VLLM_IMAGE,
    VLLM_PORT,
    _placement,
    _resources,
    _runtime_class,
    labels,
    served_model_name,
)

GROUP, VERSION = "serving.kserve.io", "v1beta1"
MODE_ANNOTATION = "serving.kserve.io/deploymentMode"
#: Set per resource rather than relied on as a cluster-wide default, so a
#: cluster configured for Knative cannot silently change what Keel deploys.
MODE = "Standard"


def _container_resources(dep: dict[str, Any]) -> dict[str, Any]:
    """Always explicit, because silence is not neutral here.

    With no resources set, KServe injects cpu: 1 / memory: 2Gi as BOTH request
    and limit. A real vLLM streams weights through host memory and needs far
    more than 2Gi -- every real deployment would be OOMKilled during weight
    load. The fake runtime uses about 100 MB, so nothing noticed. Found by
    inspecting the pod KServe actually created, not by any test.
    """
    extra = dep.get("resources") or {}
    requests = {**(extra.get("requests") or {})}
    limits = {**(extra.get("limits") or {})}
    gpu = (_resources(dep).get("resources") or {}).get("limits") or {}
    limits.update(gpu)
    return {"requests": requests, "limits": limits}


def render(dep: dict[str, Any]) -> list[dict[str, Any]]:
    name = object_name(dep["id"])
    ns = namespace(dep["team_slug"])
    lb = labels(dep["id"], dep["team_slug"])

    if dep.get("weights_uri"):
        # KServe's storage initializer should replace Keel's fetch-weights init
        # container, but how it attaches to a CUSTOM predictor container in
        # v0.21 has not been verified, and the spike does not exercise it --
        # both catalog entries pull from HuggingFace. Refuse rather than render
        # a guess that would only fail on real hardware.
        raise NotImplementedError(
            "weights_uri is not yet supported by the KServe renderer (ADR-0008 spike)"
        )
    model = dep.get("model_ref")
    args = [
        "--model", str(model),
        "--served-model-name", served_model_name(dep),
        "--port", str(VLLM_PORT),
    ]
    for flag, value in (dep.get("engine_args") or {}).items():
        args += [f"--{flag}", str(value)]

    container: dict[str, Any] = {
        # KServe requires this exact name for a custom predictor container.
        "name": "kserve-container",
        "image": dep.get("image") or VLLM_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "args": args,
        # Named "http": the PodMonitor and the Prometheus scrape config select
        # the metrics port by that name.
        "ports": [{"name": "http", "containerPort": VLLM_PORT, "protocol": "TCP"}],
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": VLLM_PORT},
            "initialDelaySeconds": READY_DELAY_SECONDS,
            "periodSeconds": 10,
            "failureThreshold": 90,
        },
        "resources": _container_resources(dep),
    }

    replicas_min = dep.get("replicas_min") or 0
    if Lane(dep["lane"]) is not Lane.C:
        replicas_min = max(replicas_min, 1)
    # Lane B is FIXED replicas, as the manifest backend always rendered it.
    # Passing replicas_max through lets KServe's Standard mode create an HPA
    # scaling on CPU at 80% -- the wrong signal for a GPU-bound engine, adding
    # GPU replicas because the tokenizer got busy. min == max makes that HPA
    # inert until autoscaling is a deliberate decision on a real signal.
    replicas_max = replicas_min if Lane(dep["lane"]) is Lane.B else max(
        dep.get("replicas_max") or 1, replicas_min, 1
    )

    predictor: dict[str, Any] = {
        "minReplicas": replicas_min,
        "maxReplicas": replicas_max,
        "containers": [container],
        **_placement(dep),
        **_runtime_class(dep),
    }
    spec: dict[str, Any] = {"predictor": predictor}

    return [
        {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "InferenceService",
            "metadata": {
                "name": name,
                "namespace": ns,
                # KServe copies these onto the Deployment and the pods --
                # verified, not assumed. Prometheus relabeling, pods_for(),
                # orphan detection and DCGM attribution all depend on it.
                "labels": lb,
                "annotations": {MODE_ANNOTATION: MODE},
            },
            "spec": spec,
        }
    ]
