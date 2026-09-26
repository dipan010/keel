"""One vLLM version, everywhere it is named.

Keel deployed vllm/vllm-openai:v0.11.0 -- a stage-1 guess -- while every metric
name, log signature and the dtype finding were verified against 0.28.0. Across
seventeen minor versions metrics get renamed (gpu_cache_usage_perc became
kv_cache_usage_perc), so the contract tests would have passed against the
recorded output while production scraped something else.
"""

from __future__ import annotations

import json
import pathlib
import re

from control import pricing
from control.render import manifests

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _verified_versions() -> set[str]:
    return {
        json.loads(p.read_text())["vllm_version"]
        for p in (ROOT / "bench/colab").glob("results-*.json")
    }


def test_the_deployed_image_is_the_verified_version():
    # The module constant, not the env override: the default is what ships.
    assert manifests.VLLM_VERSION in _verified_versions(), (
        f"render deploys vLLM {manifests.VLLM_VERSION}, but only "
        f"{sorted(_verified_versions())} have been verified"
    )
    assert manifests.DEFAULT_VLLM_IMAGE.endswith(f":v{manifests.VLLM_VERSION}")


def test_the_probe_notebook_installs_the_same_version():
    """Unpinned, the next probe run pulls whatever is newest and the verified
    version silently drifts away from the deployed one again."""
    nb = (ROOT / "bench/colab/vllm_probe.ipynb").read_text()
    pins = set(re.findall(r"vllm==([0-9.]+)", nb))
    assert pins == {manifests.VLLM_VERSION}, f"notebook pins {pins or 'nothing'}"


def test_every_label_setup_can_emit_has_a_price():
    """A label with no rate prices at zero on purpose -- but it should be zero
    because nobody priced it, not because setup.sh invented a label pricing.py
    has never heard of."""
    script = (ROOT / "deploy/gpu-node/setup.sh").read_text()
    labels = set(re.findall(r"\)\s+echo ([a-z0-9-]+) ;;", script))
    assert labels, "could not find the detection table in setup.sh"
    missing = labels - set(pricing.DEFAULT_RATES)
    assert not missing, f"setup.sh can label a node {missing}, which has no rate"
