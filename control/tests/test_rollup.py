"""Metrics rollup.

The pure parts need nothing. The integration test needs Prometheus, and skips
without it.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from control import pricing
from control.metrics import rollup


def _client(payload: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _result(*pairs, metric_extra=None):
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {**(metric_extra or {}), **m}, "value": [0, str(v)]} for m, v in pairs
            ]
        },
    }


# ---------- pricing ----------


def test_cost_is_rate_times_gpus_times_hours():
    assert pricing.cost_usd("l4", 1, 3600) == pytest.approx(0.70)
    assert pricing.cost_usd("a100-80g", 2, 3600) == pytest.approx(6.00)
    assert pricing.cost_usd("l4", 1, 1800) == pytest.approx(0.35)


def test_unknown_accelerator_costs_zero_rather_than_a_guess():
    """A fabricated rate would put an invented number in front of someone
    making a spending decision. Zero is visibly wrong and prompts the question.
    """
    assert pricing.cost_usd("made-up-sku", 4, 3600) == 0.0
    assert pricing.cost_usd(None, 1, 3600) == 0.0


def test_cpu_serving_is_free_of_gpu_cost():
    assert pricing.cost_usd("cpu", 0, 3600) == 0.0


def test_rates_can_be_overridden_wholesale(monkeypatch):
    monkeypatch.setenv("KEEL_GPU_PRICING", json.dumps({"L4": 1.25}))
    assert pricing.hourly("l4") == 1.25
    # An override replaces the table rather than merging into it, so anything
    # absent is unpriced rather than silently on last year's list price.
    assert pricing.hourly("h100") == 0.0


# ---------- gpu_seconds ----------


def test_gpu_seconds_from_our_own_record():
    row = {"gpu_count": 2, "replicas_min": 3}
    assert rollup.gpu_seconds(row, timedelta(hours=1)) == 2 * 3 * 3600


def test_cpu_deployments_accrue_no_gpu_seconds():
    assert rollup.gpu_seconds({"gpu_count": 0, "replicas_min": 1}, timedelta(hours=1)) == 0.0


def test_missing_replica_count_is_zero_not_one():
    assert rollup.gpu_seconds({"gpu_count": 1, "replicas_min": None}, timedelta(hours=1)) == 0.0


# ---------- window ----------


def test_window_truncates_to_the_hour():
    got = rollup.window_start(datetime(2026, 9, 5, 14, 37, 22, 500, tzinfo=UTC))
    assert got == datetime(2026, 9, 5, 14, 0, 0, tzinfo=UTC)


# ---------- query ----------


async def test_query_keys_by_deployment_id():
    async with _client(_result(({"deployment_id": "d-1"}, 9.0))) as c:
        assert await rollup.query(c, "irrelevant") == {"d-1": 9.0}


async def test_series_without_deployment_id_are_dropped():
    """Means the relabeling is missing. Attributing those samples to nothing is
    better than attributing them to the wrong deployment."""
    payload = _result(({"deployment_id": "d-1"}, 5.0), ({"model_name": "qwen"}, 99.0))
    async with _client(payload) as c:
        assert await rollup.query(c, "irrelevant") == {"d-1": 5.0}


async def test_nan_quantiles_are_dropped():
    """histogram_quantile over an empty bucket set returns NaN. Writing that as
    a number would put garbage in the estimator's corpus."""
    async with _client(_result(({"deployment_id": "d-1"}, math.nan))) as c:
        assert await rollup.query(c, "irrelevant") == {}


async def test_a_prometheus_error_is_raised_not_swallowed():
    payload = {"status": "error", "error": "parse error at char 3"}
    async with _client(payload) as c:
        with pytest.raises(RuntimeError, match="parse error"):
            await rollup.query(c, "bad{{")


# ---------- queries reference verified names ----------


def test_every_query_uses_a_metric_the_probe_observed():
    import pathlib
    import re

    observed = set(
        json.loads(
            (
                pathlib.Path(__file__).resolve().parents[2] / "bench/colab/results-2026-09-01.json"
            ).read_text()
        )["metric_names"]
    )
    for name, expr in rollup.QUERIES.items():
        for metric in re.findall(r"vllm:[a-zA-Z0-9_]+", expr):
            assert metric in observed, f"{name} queries {metric}, which vLLM 0.28.0 did not emit"


# ---------- integration ----------


# One source of truth. rollup.PROM defaults to the in-cluster DNS name, which
# does not resolve from a laptop -- so probing one URL and querying another made
# this test fail instead of skip, with a DNS error that looked nothing like
# "Prometheus is not running".
PROM_URL = os.environ.get("KEEL_PROMETHEUS_URL", "http://localhost:9090")


def _prom_up() -> bool:
    try:
        return httpx.get(f"{PROM_URL}/-/ready", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


requires_prometheus = pytest.mark.skipif(
    not _prom_up(),
    reason="no Prometheus (make monitoring, then make prom-fwd)",
)


@requires_prometheus
async def test_queries_are_accepted_by_a_real_prometheus(monkeypatch):
    """Syntax and label-grouping, checked by the server rather than by eye.

    A malformed histogram_quantile or a missing `by (le)` returns an error or
    silently nothing, and either way the corpus stays empty without any test
    failing.
    """
    monkeypatch.setattr(rollup, "PROM", PROM_URL)
    async with httpx.AsyncClient() as c:
        for name, expr in rollup.QUERIES.items():
            # Raises if Prometheus rejects it; an empty result is fine here,
            # since this asserts the query is valid, not that data exists.
            await rollup.query(c, expr.format(w="5m"))
            assert name


async def test_gpu_utilisation_is_null_without_dcgm():
    """The exporter needs a real GPU. Absent, the column stays NULL rather than
    borrowing KV-cache occupancy, which is a different measurement."""
    async with _client(_result()) as c:
        assert await rollup.query(c, rollup.QUERIES["gpu_util_pct"]) == {}


def test_gpu_utilisation_comes_from_dcgm_not_vllm():
    expr = rollup.QUERIES["gpu_util_pct"]
    assert "DCGM_FI_DEV_GPU_UTIL" in expr
    assert "vllm:" not in expr, "utilisation must not be inferred from a vLLM series"
    assert "by (deployment_id)" in expr, "unattributed utilisation is not usable"
