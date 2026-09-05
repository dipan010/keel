"""Log-tail classification.

Every signature here was copied from real vLLM 0.28.0 output captured on a T4
(bench/colab/results-2026-09-01.json), not invented. A crashlooping pod reports
"crashloop", which is accurate and useless; these turn it into something a
developer can act on.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from control.provisioner.status import classify_log

RESULTS = pathlib.Path(__file__).resolve().parents[2] / "bench/colab/results-2026-09-01.json"


def test_the_real_config_validation_error_is_recognised():
    # Verbatim from the probe: vLLM refused before allocating anything.
    tail = json.loads(RESULTS.read_text())["config_validation_error"]
    code, advice = classify_log(tail)
    assert code == "context_too_long"
    assert "max-model-len" in advice


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("ValueError: No available memory for the cache blocks.", "kv_cache_too_small"),
        ("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", "oom"),
        (
            (
                "ValueError: Bfloat16 is only supported on GPUs with compute capability "
                "of at least 8.0. Your Tesla T4 GPU has compute capability 7.5."
            ),
            "dtype_unsupported",
        ),
        (
            "OSError: Qwen/nope does not appear to have a file named config.json",
            "model_not_found",
        ),
    ],
)
def test_known_signatures(tail, expected):
    assert classify_log(tail)[0] == expected


def test_unrecognised_output_returns_none():
    # Falls back to whatever the pod-status classifier said. Guessing would be
    # worse than admitting we do not know.
    assert classify_log("Traceback (most recent call last): something novel") is None


def test_matching_is_case_insensitive():
    assert classify_log("CUDA OUT OF MEMORY")[0] == "oom"


def test_dtype_advice_names_the_catalog_field():
    _, advice = classify_log("Bfloat16 is only supported on GPUs with compute capability 8.0")
    assert "dtype" in advice and "catalog" in advice
