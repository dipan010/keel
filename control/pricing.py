"""What an accelerator-hour costs.

Cost is computed by us, not billed to us (ADR-0006): we placed the pod and we
know the node, so both terms are known in real time. That makes cost a live
signal rather than a lagging report -- but it also makes it a MODEL, not an
invoice. Reconcile against the real bill periodically and record the drift.

Rates vary by provider, region and commitment, so the defaults below are only a
starting point. Override wholesale with KEEL_GPU_PRICING as a JSON object.
"""

from __future__ import annotations

import json
import os

#: USD per GPU-hour. On-demand list prices, rounded, as of late 2026.
DEFAULT_RATES: dict[str, float] = {
    "cpu": 0.0,
    "t4": 0.35,
    "l4": 0.70,
    "a10": 1.00,
    "a100-40g": 2.00,
    "a100-80g": 3.00,
    "h100": 5.00,
}


def rates() -> dict[str, float]:
    if raw := os.environ.get("KEEL_GPU_PRICING"):
        return {k.lower(): float(v) for k, v in json.loads(raw).items()}
    return DEFAULT_RATES


def hourly(accelerator: str | None) -> float:
    """Unknown accelerators cost 0, deliberately.

    Guessing a rate would put a fabricated number in front of someone making a
    spending decision. A zero is visibly wrong and prompts the question; a
    plausible invention does not.
    """
    if not accelerator:
        return 0.0
    return rates().get(accelerator.lower(), 0.0)


def cost_usd(accelerator: str | None, gpu_count: int, gpu_seconds: float) -> float:
    return round(hourly(accelerator) * gpu_count * gpu_seconds / 3600.0, 6)
