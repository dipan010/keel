"""Validation rules mirroring the database constraints.

Deliberately duplicated: the constraints are the backstop that keeps bad rows
out forever, these are what produce an error message a developer can act on.
"""

from __future__ import annotations

from dataclasses import dataclass

from .states import Lane, Mode


class ValidationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DeploymentRequest:
    team_slug: str
    name: str
    model_id: str
    lane: Lane | None = None  # None means "let the platform choose"
    replicas_min: int | None = None
    replicas_max: int | None = None


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    mode: Mode
    default_lane: Lane
    context_length: int
    license: str
    status: str
    accelerator: str | None = None
    gpu_count: int | None = None
    weights_uri: str | None = None
    upstream_model: str | None = None


def resolve_lane(req: DeploymentRequest, entry: CatalogEntry) -> Lane:
    """Lane selection is the platform's job, not the developer's.

    This is the first thing the estimator takes over: today a hand-picked
    catalog default, later a decision from measured throughput and cost.
    """
    lane = req.lane or entry.default_lane
    if (lane is Lane.A) != (entry.mode is Mode.UPSTREAM):
        raise ValidationError(
            f"lane {lane} is incompatible with mode {entry.mode}: "
            "lane a is upstream, and only upstream"
        )
    return lane


def validate(req: DeploymentRequest, entry: CatalogEntry, *, allow_preview: bool = False) -> Lane:
    if entry.status == "deprecated":
        raise ValidationError(f"model {entry.id} is deprecated")
    if entry.status != "validated" and not allow_preview:
        raise ValidationError(
            f"model {entry.id} is {entry.status}; pass allow_preview to deploy it"
        )

    lane = resolve_lane(req, entry)

    if entry.mode is Mode.SELF_HOSTED:
        if req.replicas_min is not None and req.replicas_min < 0:
            raise ValidationError("replicas_min must be >= 0")
        if (
            req.replicas_max is not None
            and req.replicas_min is not None
            and req.replicas_max < req.replicas_min
        ):
            raise ValidationError("replicas_max must be >= replicas_min")
        if lane is Lane.B and req.replicas_min is not None and req.replicas_min < 1:
            raise ValidationError("lane b has a floor of 1 replica; use lane c to scale to zero")

    return lane


def object_name(deployment_id: str) -> str:
    """Deterministic, derived from the deployment id.

    This is the idempotency mechanism: a redelivered job server-side-applies the
    same objects under the same name and converges, rather than duplicating.
    See ADR-0002.
    """
    return f"vllm-{deployment_id}"


def namespace(team_slug: str) -> str:
    return f"keel-inf-{team_slug}"


def route_name(team_slug: str, deployment_name: str) -> str:
    """What the client sends as "model". Stable across backend changes -- a swap
    from 70B to 7B, or self-hosted to upstream, must not touch any client."""
    return f"{team_slug}/{deployment_name}"
