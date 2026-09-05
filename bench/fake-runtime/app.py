"""A fake vLLM. Answers the OpenAI API with canned text and plausible metrics.

This exists so the entire control path is testable in CI with no GPU. Without
it every integration test costs GPU-hours and nobody runs them -- which is how
a control plane ends up with no tests at all.

METRIC NAMES ARE COPIED FROM A REAL vLLM 0.28.0, not invented: see
bench/colab/results-2026-09-01.json. An earlier version of this file guessed
them and got three wrong, which made it useless for testing the rollup -- a
fake whose interface differs from the real thing tests nothing.
"""

from __future__ import annotations

import os
import random
import time

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI()

MODEL = os.environ.get("FAKE_MODEL", "fake-7b")
# Simulate weight loading so the SCHEDULING -> LOADING -> READY path is real.
LOAD_SECONDS = float(os.environ.get("FAKE_LOAD_SECONDS", "20"))
STARTED = time.monotonic()

TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
TPOT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)

_requests = 0
_prompt_tokens = 0
_generation_tokens = 0
_ttft: list[float] = []
_tpot: list[float] = []


def loaded() -> bool:
    return (time.monotonic() - STARTED) >= LOAD_SECONDS


@app.get("/health")
def health():
    # 503 until "weights load", exactly like the real thing.
    return ({"status": "ok"}, 200) if loaded() else PlainTextResponse("loading", 503)


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.post("/v1/chat/completions")
def chat(body: dict):
    global _requests, _prompt_tokens, _generation_tokens
    _requests += 1
    out = random.randint(20, 80)
    _prompt_tokens += 12
    _generation_tokens += out
    _ttft.append(random.uniform(0.04, 0.6))
    _tpot.append(random.uniform(0.008, 0.05))
    return {
        "id": f"chatcmpl-fake-{_requests}",
        "object": "chat.completion",
        "model": body.get("model", MODEL),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fake completion from keel"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": out, "total_tokens": 12 + out},
    }


def _histogram(name: str, values: list[float], buckets: tuple[float, ...]) -> str:
    """Real vLLM exposes TTFT and TPOT as histograms -- _bucket/_count/_sum,
    with no bare gauge. The rollup computes quantiles from the buckets, so the
    fake has to expose the same shape or the query returns nothing."""
    lines = [f"# TYPE {name} histogram"]
    for b in buckets:
        n = sum(1 for v in values if v <= b)
        lines.append(f'{name}_bucket{{le="{b}"}} {n}')
    lines.append(f'{name}_bucket{{le="+Inf"}} {len(values)}')
    lines.append(f"{name}_count {len(values)}")
    lines.append(f"{name}_sum {sum(values):.4f}")
    return "\n".join(lines)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return "\n".join(
        [
            f"vllm:request_success_total {_requests}",
            f"vllm:prompt_tokens_total {_prompt_tokens}",
            f"vllm:generation_tokens_total {_generation_tokens}",
            f"vllm:num_requests_running {random.randint(0, 2)}",
            f"vllm:num_requests_waiting {random.randint(0, 3)}",
            f"vllm:kv_cache_usage_perc {random.uniform(0.05, 0.4):.4f}",
            _histogram("vllm:time_to_first_token_seconds", _ttft, TTFT_BUCKETS),
            _histogram("vllm:request_time_per_output_token_seconds", _tpot, TPOT_BUCKETS),
            "",
        ]
    )
