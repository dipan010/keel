"""Gateway registration.

LiteLLM is adopted, not authored -- see ADR-0003. Keel's contribution is narrow:
generate the routing entry when a deployment goes ready, so the Keel name a
developer types resolves to whatever the backend actually calls itself.

THE CONSTRAINT: the gateway must never make a synchronous call to the control
plane or its database to serve a request (invariant I2). We push config to it
and it holds the runtime copy in memory. Pointing LiteLLM's own config at our
database would be an afternoon's work and would convert a routine Postgres
restart into a total inference outage.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger()

GATEWAY_URL = os.environ.get("KEEL_GATEWAY_URL", "")
GATEWAY_KEY = os.environ.get("KEEL_GATEWAY_KEY", "")


class Gateway(Protocol):
    async def register(self, route: str, upstream: str, model: str) -> None: ...
    async def deregister(self, route: str) -> None: ...
    async def completion(self, route: str, prompt: str) -> str: ...


class NullGateway:
    """Used until LiteLLM is deployed. Every call is logged and does nothing.

    Deliberately not silent: a smoke test that cannot reach a gateway must be
    visible as "not verified", never mistaken for a pass.
    """

    async def register(self, route: str, upstream: str, model: str) -> None:
        log.warning("gateway.absent.register", route=route, upstream=upstream)

    async def deregister(self, route: str) -> None:
        log.warning("gateway.absent.deregister", route=route)

    async def completion(self, route: str, prompt: str) -> str:
        raise RuntimeError("no gateway configured; set KEEL_GATEWAY_URL")


class LiteLLMGateway:
    def __init__(self, base_url: str, key: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {key}"}

    async def register(self, route: str, upstream: str, model: str) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self._base}/model/new",
                headers=self._headers,
                json={
                    "model_name": route,
                    "litellm_params": {
                        # hosted_vllm/ tells LiteLLM this is an OpenAI-compatible
                        # server rather than a provider it has built-in knowledge of.
                        "model": f"hosted_vllm/{model}",
                        "api_base": upstream,
                    },
                },
            )
            r.raise_for_status()

    async def deregister(self, route: str) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self._base}/model/delete", headers=self._headers, json={"model_name": route}
            )
            if r.status_code not in (200, 404):
                r.raise_for_status()

    async def completion(self, route: str, prompt: str) -> str:
        """A real completion through the gateway -- not a readiness probe.

        An endpoint that starts but returns garbage is worse than one that
        failed, because nobody finds out until a developer does.
        """
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{self._base}/v1/chat/completions",
                headers=self._headers,
                json={
                    "model": route,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 16,
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


def build() -> Gateway:
    if GATEWAY_URL:
        return LiteLLMGateway(GATEWAY_URL, GATEWAY_KEY)
    return NullGateway()
