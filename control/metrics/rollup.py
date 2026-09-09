"""Hourly aggregation into deployment_metrics.

Instrumented before anyone needs it, because it cannot be backfilled: every
hour of real traffic not captured is an hour the future estimator would have to
reproduce by re-running benchmarks on hardware someone pays for.

Every query below uses a metric name verified against a real vLLM 0.28.0
(bench/colab/results-2026-09-01.json). Three of the names originally assumed
were wrong -- TTFT and TPOT are histograms rather than gauges, and
gpu_cache_usage_perc was renamed kv_cache_usage_perc -- so these are pinned by
control/tests/test_metrics_contract.py against the recorded output.
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from control import db, pricing
from control.domain.states import LIVE, Status

log = structlog.get_logger()

PROM = os.environ.get("KEEL_PROMETHEUS_URL", "http://prometheus.keel-system:9090")
INTERVAL = float(os.environ.get("KEEL_ROLLUP_INTERVAL", "300"))
WINDOW = timedelta(hours=1)

#: Counters are summed over the window; the histograms give quantiles from
#: their buckets; kv cache is averaged.
QUERIES: dict[str, str] = {
    "requests": "sum by (deployment_id) (increase(vllm:request_success_total[{w}]))",
    "tokens_in": "sum by (deployment_id) (increase(vllm:prompt_tokens_total[{w}]))",
    "tokens_out": "sum by (deployment_id) (increase(vllm:generation_tokens_total[{w}]))",
    "ttft_p50_ms": (
        "histogram_quantile(0.50, sum by (deployment_id, le) "
        "(rate(vllm:time_to_first_token_seconds_bucket[{w}]))) * 1000"
    ),
    "ttft_p95_ms": (
        "histogram_quantile(0.95, sum by (deployment_id, le) "
        "(rate(vllm:time_to_first_token_seconds_bucket[{w}]))) * 1000"
    ),
    "tpot_p50_ms": (
        "histogram_quantile(0.50, sum by (deployment_id, le) "
        "(rate(vllm:request_time_per_output_token_seconds_bucket[{w}]))) * 1000"
    ),
    "kv_cache_pct": ("avg by (deployment_id) (avg_over_time(vllm:kv_cache_usage_perc[{w}])) * 100"),
    # DCGM, not vLLM. Absent until deploy/monitoring/dcgm.yaml runs on a real
    # GPU -- and absent is the correct answer then: an empty result yields NULL
    # rather than a substitute.
    "gpu_util_pct": ("avg by (deployment_id) (avg_over_time(DCGM_FI_DEV_GPU_UTIL[{w}]))"),
}


async def query(client: httpx.AsyncClient, expr: str) -> dict[str, float]:
    """Run one instant query, keyed by deployment_id.

    A series with no deployment_id label is dropped: it means the relabeling in
    deploy/monitoring (or the PodMonitor) is not in place, and attributing those
    samples to nothing would be better than attributing them to the wrong
    deployment.
    """
    r = await client.get(f"{PROM}/api/v1/query", params={"query": expr}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus rejected the query: {body.get('error')}")
    out: dict[str, float] = {}
    for series in body["data"]["result"]:
        dep_id = series["metric"].get("deployment_id")
        value = float(series["value"][1])
        # histogram_quantile over an empty bucket set returns NaN, which must
        # not be written as a number.
        if dep_id and not math.isnan(value):
            out[dep_id] = value
    return out


def gpu_seconds(row: dict[str, Any], window: timedelta) -> float:
    """Computed from our own record, not scraped.

    We placed the pod and we know the node, so this is knowable exactly -- which
    is the whole reason cost can be live here. Uses replicas_min: an HPA moving
    the count during the window is not tracked yet, so this understates a
    deployment that scaled up. Recorded as a known limitation rather than
    guessed at.
    """
    if not row.get("gpu_count"):
        return 0.0
    return float(row["gpu_count"]) * float(row["replicas_min"] or 0) * window.total_seconds()


UPSERT = """
insert into deployment_metrics
  (deployment_id, window_start, requests, tokens_in, tokens_out,
   ttft_p50_ms, ttft_p95_ms, tpot_p50_ms, gpu_util_pct, gpu_seconds, cost_usd)
values
  (%(deployment_id)s, %(window_start)s, %(requests)s, %(tokens_in)s, %(tokens_out)s,
   %(ttft_p50_ms)s, %(ttft_p95_ms)s, %(tpot_p50_ms)s, %(gpu_util_pct)s,
   %(gpu_seconds)s, %(cost_usd)s)
on conflict (deployment_id, window_start) do update set
  requests = excluded.requests, tokens_in = excluded.tokens_in,
  tokens_out = excluded.tokens_out, ttft_p50_ms = excluded.ttft_p50_ms,
  ttft_p95_ms = excluded.ttft_p95_ms, tpot_p50_ms = excluded.tpot_p50_ms,
  gpu_util_pct = excluded.gpu_util_pct, gpu_seconds = excluded.gpu_seconds,
  cost_usd = excluded.cost_usd
"""


def window_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(minute=0, second=0, microsecond=0)


async def rollup_once(now: datetime | None = None) -> int:
    start = window_start(now)
    w = "1h"

    async with httpx.AsyncClient() as client:
        series = {name: await query(client, expr.format(w=w)) for name, expr in QUERIES.items()}

    async with db.pool().connection() as conn:
        cur = await conn.execute(
            """select id, accelerator, gpu_count, replicas_min, status
                 from deployments
                where status = any(%s)""",
            ([str(s) for s in LIVE | {Status.LOADING}],),
        )
        rows = await cur.fetchall()

    written = 0
    async with db.transaction() as conn:
        for row in rows:
            dep_id = str(row["id"])
            secs = gpu_seconds(row, WINDOW)
            await conn.execute(
                UPSERT,
                {
                    "deployment_id": dep_id,
                    "window_start": start,
                    "requests": int(series["requests"].get(dep_id, 0)),
                    "tokens_in": int(series["tokens_in"].get(dep_id, 0)),
                    "tokens_out": int(series["tokens_out"].get(dep_id, 0)),
                    "ttft_p50_ms": _int(series["ttft_p50_ms"].get(dep_id)),
                    "ttft_p95_ms": _int(series["ttft_p95_ms"].get(dep_id)),
                    "tpot_p50_ms": _int(series["tpot_p50_ms"].get(dep_id)),
                    # From DCGM. Stays NULL where the exporter is not running,
                    # which is the honest answer -- KV-cache occupancy was
                    # always available and is a different thing, and writing it
                    # here would be a wrong number wearing a familiar name.
                    "gpu_util_pct": series["gpu_util_pct"].get(dep_id),
                    "gpu_seconds": int(secs),
                    "cost_usd": pricing.cost_usd(row["accelerator"], row["gpu_count"] or 0, secs),
                },
            )
            written += 1
    return written


def _int(v: float | None) -> int | None:
    return None if v is None else round(v)


async def loop() -> None:
    await db.open_pool()
    log.info("rollup.start", prometheus=PROM, interval=INTERVAL)
    try:
        while True:
            try:
                n = await rollup_once()
                log.info("rollup.pass", deployments=n)
            except Exception:
                # A failed pass must not kill the loop: the next one re-queries
                # the same window and upserts over whatever it wrote.
                log.exception("rollup.failed")
            await asyncio.sleep(INTERVAL)
    finally:
        await db.close_pool()


def run() -> None:
    asyncio.run(loop())


if __name__ == "__main__":
    run()
