"""Pod status -> what the deployment's status should be.

Pure: takes the dicts the API server returns, decides. No cluster access, so
every failure mode below is unit-testable without a GPU or even a cluster --
which matters because these are exactly the paths that are hard to reproduce on
demand and easy to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from control.domain.states import Status

#: Waiting reasons that will never resolve on their own. Retrying a GPU
#: provision that failed deterministically just costs money.
FATAL_WAITING = {
    "ImagePullBackOff": "image_pull_failed",
    "ErrImagePull": "image_pull_failed",
    "InvalidImageName": "image_pull_failed",
    "CreateContainerConfigError": "bad_config",
    "CrashLoopBackOff": "crashloop",
}

#: Scheduling can be transient -- a node is draining, another pod is
#: terminating. It is only permanent once nothing has changed for a while.
UNSCHEDULABLE_GRACE_SECONDS = 120


#: Signatures in a container's log tail that name a failure precisely.
#: A crashlooping pod is accurate but useless to a developer; these turn
#: "CrashLoopBackOff" into something actionable. Ordered: first match wins.
LOG_SIGNATURES: list[tuple[str, str, str]] = [
    (
        "is greater than the derived max_model_len",
        "context_too_long",
        (
            "the requested context exceeds what the model's config allows. Lower "
            "max-model-len in the catalog entry to at most the model's "
            "max_position_embeddings."
        ),
    ),
    (
        "No available memory for the cache blocks",
        "kv_cache_too_small",
        (
            "no room left for a KV cache after loading weights. Raise "
            "gpu-memory-utilization, lower max-model-len, or use a larger accelerator."
        ),
    ),
    (
        "CUDA out of memory",
        "oom",
        (
            "the model did not fit in VRAM. Try a larger accelerator, more GPUs, "
            "or a shorter max-model-len."
        ),
    ),
    (
        "Bfloat16 is only supported on GPUs with compute capability",
        "dtype_unsupported",
        "this accelerator has no bfloat16. Set dtype: half in the catalog entry.",
    ),
    (
        "does not appear to have a file named config.json",
        "model_not_found",
        "the model reference does not resolve. Check the catalog's model id.",
    ),
]


def classify_log(tail: str) -> tuple[str, str] | None:
    """Map a container log tail to a precise reason code, or None.

    Verified against vLLM 0.28.0 output captured in bench/colab/.
    """
    for needle, code, advice in LOG_SIGNATURES:
        if needle.lower() in tail.lower():
            return code, advice
    return None


class Phase(StrEnum):
    SCHEDULING = "scheduling"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Outcome:
    phase: Phase
    reason_code: str | None = None
    message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> Status:
        return Status(self.phase.value)

    @property
    def terminal(self) -> bool:
        return self.phase in (Phase.READY, Phase.FAILED)


def _conditions(pod: dict) -> dict[str, dict]:
    return {c["type"]: c for c in pod.get("status", {}).get("conditions", []) or []}


def _container_states(pod: dict) -> list[dict]:
    st = pod.get("status", {})
    return list(st.get("initContainerStatuses") or []) + list(st.get("containerStatuses") or [])


def classify(pods: list[dict], *, elapsed_seconds: float = 0.0) -> Outcome:
    """Decide from the pods belonging to one deployment.

    Order matters: fatal states are checked before progress states, so a pod
    that is Running with a crashlooping sidecar reports the crash rather than
    reporting progress.
    """
    if not pods:
        return Outcome(Phase.SCHEDULING, message="no pods yet")

    for pod in pods:
        name = pod["metadata"]["name"]
        ns = pod["metadata"]["namespace"]

        # -- fatal: container-level ------------------------------------------
        for cs in _container_states(pod):
            state = cs.get("state") or {}
            waiting = state.get("waiting") or {}
            reason = waiting.get("reason")
            if reason in FATAL_WAITING:
                return Outcome(
                    Phase.FAILED,
                    FATAL_WAITING[reason],
                    waiting.get("message") or reason,
                    {"pod": name, "namespace": ns, "container": cs.get("name")},
                )
            terminated = state.get("terminated") or {}
            last = (cs.get("lastState") or {}).get("terminated") or {}
            for term in (terminated, last):
                if term.get("reason") == "OOMKilled":
                    return Outcome(
                        Phase.FAILED,
                        "oom",
                        "container was OOMKilled -- the model did not fit. Try a "
                        "larger accelerator, more GPUs, or a shorter --max-model-len.",
                        {"pod": name, "namespace": ns, "container": cs.get("name")},
                    )

        # -- fatal: scheduling ------------------------------------------------
        scheduled = _conditions(pod).get("PodScheduled", {})
        if (
            scheduled.get("status") == "False"
            and scheduled.get("reason") == "Unschedulable"
            and elapsed_seconds >= UNSCHEDULABLE_GRACE_SECONDS
        ):
            return Outcome(
                Phase.FAILED,
                "unschedulable",
                scheduled.get("message") or "no node matches the requested accelerator",
                {"pod": name, "namespace": ns},
            )

    # -- progress -------------------------------------------------------------
    if any(_conditions(p).get("Ready", {}).get("status") == "True" for p in pods):
        return Outcome(Phase.READY)

    if any(p.get("status", {}).get("phase") == "Running" for p in pods):
        # Scheduled and started, not yet serving: weights are loading. This is
        # slow and it is not a fault -- which is why it is its own state.
        return Outcome(Phase.LOADING, message="loading weights")

    if any(_container_states(p) for p in pods):
        return Outcome(Phase.LOADING, message="fetching weights")

    return Outcome(Phase.SCHEDULING, message="waiting for a node")
