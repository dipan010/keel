"""What vLLM is actually told to load.

Before this existed, render hardcoded "--model /models/weights" -- a path that
only exists if the fetch-weights init container ran -- while every catalog
entry set weights_uri, so that container was always rendered, pointing at an
image that had never been built. A real model could not have started, and
nothing failed until it was tried on hardware someone was paying for.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from control.render import manifests

CATALOG = pathlib.Path(__file__).resolve().parents[2] / "catalog"

BASE = {
    "id": "d1",
    "team_slug": "t",
    "model_id": "fake-model",
    "lane": "b",
    "replicas_min": 1,
    "replicas_max": 1,
    "accelerator": "l4",
    "gpu_count": 1,
    "engine_args": {},
}


def container(spec: dict) -> dict:
    dep = next(o for o in manifests.render(spec) if o["kind"] == "Deployment")
    return dep["spec"]["template"]["spec"]


def model_arg(spec: dict) -> str:
    args = container(spec)["containers"][0]["args"]
    return args[args.index("--model") + 1]


def test_without_a_cache_vllm_is_given_the_catalog_reference():
    spec = {**BASE, "weights_uri": None, "model_ref": "Qwen/Qwen2.5-0.5B-Instruct"}
    assert model_arg(spec) == "Qwen/Qwen2.5-0.5B-Instruct"
    # No fetcher, so no dependency on an image that may not exist.
    assert "initContainers" not in container(spec)


def test_with_a_cache_vllm_is_given_the_mount_path():
    spec = {**BASE, "weights_uri": "s3://keel/qwen", "model_ref": "Qwen/Qwen2.5-0.5B-Instruct"}
    assert model_arg(spec) == "/models/weights"
    assert container(spec)["initContainers"][0]["name"] == "fetch-weights"


def test_the_reference_survives_switching_to_a_cache():
    """The point of keeping them separate: an entry can move between fetch
    strategies without changing what it identifies."""
    ref = "Qwen/Qwen2.5-0.5B-Instruct"
    assert model_arg({**BASE, "weights_uri": None, "model_ref": ref}) == ref
    cached = {**BASE, "weights_uri": "s3://x", "model_ref": ref}
    assert model_arg(cached) == "/models/weights"
    assert cached["model_ref"] == ref


def test_engine_args_follow_the_model_argument():
    spec = {
        **BASE,
        "weights_uri": None,
        "model_ref": "m",
        "engine_args": {"dtype": "float16", "max-model-len": 8192},
    }
    args = container(spec)["containers"][0]["args"]
    assert args[:2] == ["--model", "m"]
    assert "--dtype" in args and "float16" in args
    assert "--max-model-len" in args and "8192" in args


@pytest.mark.parametrize("path", sorted(CATALOG.glob("*.yaml")))
def test_every_self_hosted_catalog_entry_can_actually_start(path):
    """Mirrors the check constraint from migration 0006, at the file level so a
    bad entry fails review rather than deploy."""
    entry = yaml.safe_load(path.read_text())
    if entry.get("mode") != "self_hosted":
        return
    assert entry.get("model_ref") or entry.get("weights_uri"), (
        f"{path.name} has neither model_ref nor weights_uri, so vLLM would be told to load nothing"
    )
    assert entry.get("accelerator"), f"{path.name} has no accelerator, so placement is undecidable"


@pytest.mark.parametrize("path", sorted(CATALOG.glob("*.yaml")))
def test_dtype_is_explicit_everywhere(path):
    """ADR-0007: vLLM downcasts silently rather than failing, so 'auto' means
    the same entry runs at different precision on different accelerators."""
    entry = yaml.safe_load(path.read_text())
    if entry.get("mode") != "self_hosted":
        return
    assert (entry.get("engine_args") or {}).get("dtype"), f"{path.name} leaves dtype to auto"


# ---------- runtime class ----------


def test_no_runtime_class_is_named_unless_configured(monkeypatch):
    """Naming a RuntimeClass that does not exist makes the pod unschedulable,
    so this must stay unset by default -- every cluster without one, including
    the local fake-GPU cluster, would otherwise break."""
    monkeypatch.setattr(manifests, "RUNTIME_CLASS", "")
    assert "runtimeClassName" not in container({**BASE, "weights_uri": None, "model_ref": "m"})


def test_gpu_pods_opt_into_the_runtime_class_when_configured(monkeypatch):
    """The device plugin advertising nvidia.com/gpu is not the same as a pod
    being able to reach the GPU: on k3s the runtime is opt-in, and a pod that
    does not name it starts, finds no CUDA device and dies."""
    monkeypatch.setattr(manifests, "RUNTIME_CLASS", "nvidia")
    spec = container({**BASE, "weights_uri": None, "model_ref": "m"})
    assert spec["runtimeClassName"] == "nvidia"


def test_cpu_pods_never_name_a_gpu_runtime_class(monkeypatch):
    monkeypatch.setattr(manifests, "RUNTIME_CLASS", "nvidia")
    cpu = {**BASE, "accelerator": "cpu", "gpu_count": 0, "weights_uri": None, "model_ref": "m"}
    assert "runtimeClassName" not in container(cpu)


# ---------- served model name ----------


def test_vllm_is_told_the_name_the_gateway_will_send():
    """After model_ref landed, vLLM served "Qwen/Qwen2.5-0.5B-Instruct" while
    the gateway was registered with "/models/weights". A real vLLM rejects every
    request whose model it is not serving; the fake accepted any name, so the
    mismatch was invisible until this test existed."""
    spec = {**BASE, "weights_uri": None, "model_ref": "Qwen/Qwen2.5-0.5B-Instruct"}
    args = container(spec)["containers"][0]["args"]
    assert "--served-model-name" in args
    served = args[args.index("--served-model-name") + 1]
    assert served == manifests.served_model_name(spec)


def test_the_served_name_does_not_depend_on_where_the_weights_came_from():
    """--model changes between a HuggingFace id and a mount path; the name
    clients reach it by must not."""
    hf = {**BASE, "weights_uri": None, "model_ref": "Qwen/Qwen2.5-0.5B-Instruct"}
    cached = {**hf, "weights_uri": "s3://keel/qwen"}
    assert manifests.served_model_name(hf) == manifests.served_model_name(cached)


def test_the_provisioner_registers_the_served_name():
    """Guard against the two sides drifting apart again: the registration must
    call the same function, not rebuild the string."""
    import inspect

    from control.provisioner import main as provisioner

    src = inspect.getsource(provisioner.provision)
    assert "served_model_name(" in src
    assert "/models/weights" not in src
