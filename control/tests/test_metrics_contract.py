"""The KEDA query, checked against metric names a real vLLM actually emitted.

This is the failure that has no symptom: if the query selects nothing, KEDA
reads zero, and a lane C deployment comes up healthy and never scales. Nothing
errors and nothing alerts.
"""

from __future__ import annotations

import json
import pathlib
import re

from control.render import manifests
from control.render import namespace as ns_render

RESULTS = pathlib.Path(__file__).resolve().parents[2] / "bench/colab/results-2026-09-01.json"
OBSERVED = set(json.loads(RESULTS.read_text())["metric_names"])

SPEC = {
    "id": "d-1",
    "team_slug": "t",
    "lane": "c",
    "replicas_min": 0,
    "replicas_max": 3,
    "accelerator": "l4",
    "gpu_count": 1,
    "weights_uri": None,
    "engine_args": {},
}


def _query() -> str:
    so = next(o for o in manifests.render(SPEC) if o["kind"] == "ScaledObject")
    return so["spec"]["triggers"][0]["metadata"]["query"]


def test_the_metric_the_scaler_queries_actually_exists():
    metric = re.search(r"(vllm:[a-zA-Z0-9_]+)", _query()).group(1)
    assert metric in OBSERVED, (
        f"{metric} was not among the {len(OBSERVED)} metrics vLLM 0.28.0 emitted"
    )


def test_deployment_id_is_not_a_label_vllm_emits():
    """It exists only because the PodMonitor relabels it on.

    Worth pinning: the query looks self-evidently correct and would match
    nothing without that relabeling.
    """
    assert 'deployment_id="' in _query()
    pm = next(o for o in ns_render.render("t", 2) if o["kind"] == "PodMonitor")
    targets = {r["targetLabel"] for r in pm["spec"]["podMetricsEndpoints"][0]["relabelings"]}
    assert "deployment_id" in targets


def test_podmonitor_scrapes_the_named_container_port():
    pm = next(o for o in ns_render.render("t", 2) if o["kind"] == "PodMonitor")
    port = pm["spec"]["podMetricsEndpoints"][0]["port"]
    dep = next(o for o in manifests.render(SPEC) if o["kind"] == "Deployment")
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert port in {p.get("name") for p in ports}, "PodMonitor names a port the pod does not expose"


def test_histogram_metrics_are_referenced_by_their_real_names():
    """TTFT and TPOT are histograms, not gauges.

    The probe initially looked for bare `vllm:time_to_first_token_seconds` and
    found nothing -- the series are _bucket/_count/_sum. Anything reading them
    must use those suffixes.
    """
    assert "vllm:time_to_first_token_seconds" not in OBSERVED
    for suffix in ("_bucket", "_count", "_sum"):
        assert f"vllm:time_to_first_token_seconds{suffix}" in OBSERVED


def test_kv_cache_metric_was_renamed():
    # gpu_cache_usage_perc no longer exists; it is kv_cache_usage_perc.
    assert "vllm:gpu_cache_usage_perc" not in OBSERVED
    assert "vllm:kv_cache_usage_perc" in OBSERVED
