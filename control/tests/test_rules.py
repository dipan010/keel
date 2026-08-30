import dataclasses

import pytest

from control.domain.rules import (
    CatalogEntry,
    DeploymentRequest,
    ValidationError,
    namespace,
    object_name,
    route_name,
    validate,
)
from control.domain.states import Lane, Mode

SELF_HOSTED = CatalogEntry(
    id="qwen2.5-7b-instruct",
    mode=Mode.SELF_HOSTED,
    default_lane=Lane.B,
    context_length=32768,
    license="apache-2.0",
    status="validated",
    accelerator="l4",
    gpu_count=1,
    weights_uri="s3://keel-models/qwen",
)
UPSTREAM = CatalogEntry(
    id="gpt-5",
    mode=Mode.UPSTREAM,
    default_lane=Lane.A,
    context_length=200000,
    license="proprietary",
    status="validated",
    upstream_model="openai/gpt-5",
)


def req(**kw):
    base = {"team_slug": "platform", "name": "chat", "model_id": "qwen2.5-7b-instruct"}
    return DeploymentRequest(**{**base, **kw})


def test_lane_defaults_from_catalog():
    assert validate(req(), SELF_HOSTED) is Lane.B


def test_lane_a_requires_upstream_mode():
    with pytest.raises(ValidationError, match="lane a is upstream"):
        validate(req(lane=Lane.A), SELF_HOSTED)


def test_upstream_cannot_take_a_gpu_lane():
    with pytest.raises(ValidationError):
        validate(req(lane=Lane.B), UPSTREAM)


def test_preview_models_are_refused_by_default():
    preview = dataclasses.replace(SELF_HOSTED, status="preview")
    with pytest.raises(ValidationError, match="preview"):
        validate(req(), preview)
    assert validate(req(), preview, allow_preview=True) is Lane.B


def test_deprecated_is_always_refused():
    dep = dataclasses.replace(SELF_HOSTED, status="deprecated")
    with pytest.raises(ValidationError, match="deprecated"):
        validate(req(), dep, allow_preview=True)


def test_lane_b_will_not_scale_to_zero():
    with pytest.raises(ValidationError, match="lane c"):
        validate(req(lane=Lane.B, replicas_min=0), SELF_HOSTED)


def test_naming_is_deterministic():
    # The idempotency mechanism: same id, same object name, SSA converges.
    assert object_name("abc-123") == "vllm-abc-123"
    assert namespace("platform") == "keel-inf-platform"
    assert route_name("platform", "chat") == "platform/chat"
