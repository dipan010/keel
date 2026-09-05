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
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger()

GATEWAY_URL = os.environ.get("KEEL_GATEWAY_URL", "")
GATEWAY_KEY = os.environ.get("KEEL_GATEWAY_KEY", "")


#: Issued keys expire by default. A key that never expires is a liability
#: nobody remembers creating; renewing one is a smaller cost than an
#: indefinitely valid credential in a CI config somebody left a job ago.
DEFAULT_KEY_DURATION = os.environ.get("KEEL_KEY_DURATION", "90d")


class Gateway(Protocol):
    async def register(self, route: str, upstream: str, model: str) -> None: ...
    async def deregister(self, route: str) -> None: ...
    async def completion(self, route: str, prompt: str) -> str: ...
    async def issue_key(
        self, route: str, alias: str, max_budget: float | None, duration: str | None
    ) -> dict[str, Any]: ...
    async def revoke_key(self, alias: str) -> None: ...


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

    async def issue_key(
        self, route: str, alias: str, max_budget: float | None, duration: str | None
    ) -> dict[str, Any]:
        # Cannot be faked: a key that does not work at the gateway is worse than
        # an error, because the caller only finds out at first use.
        raise RuntimeError("no gateway configured; cannot issue a key")

    async def revoke_key(self, alias: str) -> None:
        log.warning("gateway.absent.revoke", alias=alias)


class LiteLLMGateway:
    def __init__(self, base_url: str, key: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {key}"}

    async def register(self, route: str, upstream: str, model: str) -> None:
        # hosted_vllm expects api_base to point at the OpenAI-compatible ROOT,
        # i.e. including /v1. Handing it the bare Service URL produces a 404
        # from upstream that looks exactly like a missing route.
        if not upstream.rstrip("/").endswith("/v1"):
            upstream = upstream.rstrip("/") + "/v1"
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
        # /model/delete keys on LiteLLM's internal model id, not on the name we
        # registered, so this is a lookup then a delete. Deleting an absent
        # route is a no-op: teardown has to be safe to retry.
        async with httpx.AsyncClient(timeout=15) as c:
            info = await c.get(f"{self._base}/model/info", headers=self._headers)
            info.raise_for_status()
            ids = [
                (m.get("model_info") or {}).get("id")
                for m in info.json().get("data", [])
                if m.get("model_name") == route
            ]
            for model_id in filter(None, ids):
                r = await c.post(
                    f"{self._base}/model/delete",
                    headers=self._headers,
                    json={"id": model_id},
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

    async def issue_key(
        self, route: str, alias: str, max_budget: float | None, duration: str | None
    ) -> dict[str, Any]:
        """Generate a key scoped to ONE route.

        `models` is the scoping mechanism: a key issued for one deployment
        cannot call another, which is what makes per-deployment keys meaningful
        rather than decorative.

        The returned dict contains the secret exactly once. It is handed to the
        caller and never stored -- see migration 0005.
        """
        body: dict[str, Any] = {"key_alias": alias, "models": [route]}
        if max_budget is not None:
            body["max_budget"] = float(max_budget)
        if duration:
            body["duration"] = duration
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self._base}/key/generate", headers=self._headers, json=body)
            r.raise_for_status()
            return r.json()

    async def revoke_key(self, alias: str) -> None:
        """Revoking an absent key is a no-op: teardown must be safe to retry."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{self._base}/key/delete", headers=self._headers, json={"key_aliases": [alias]}
            )
            if r.status_code not in (200, 404):
                r.raise_for_status()


def build() -> Gateway:
    if GATEWAY_URL:
        return LiteLLMGateway(GATEWAY_URL, GATEWAY_KEY)
    return NullGateway()
