"""A fake vLLM. Answers the OpenAI API with canned text and plausible metrics.

This exists so the entire control path is testable in CI with no GPU. Without
it every integration test costs GPU-hours and nobody runs them -- which is how
a control plane ends up with no tests at all.
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

_requests = 0
_tokens_out = 0


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
    global _requests, _tokens_out
    _requests += 1
    out = random.randint(20, 80)
    _tokens_out += out
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


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    # The series the reconciler and the future estimator actually consume.
    return (
        f"vllm:request_success_total {_requests}\n"
        f"vllm:generation_tokens_total {_tokens_out}\n"
        f"vllm:num_requests_waiting {random.randint(0, 3)}\n"
        f"vllm:time_to_first_token_seconds_sum {_requests * 0.18:.3f}\n"
        f"vllm:time_per_output_token_seconds_sum {_tokens_out * 0.012:.3f}\n"
    )
