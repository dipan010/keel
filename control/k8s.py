"""Thin Kubernetes client.

Server-side apply only. No Helm, no operator -- see ADR-0002. Every object goes
through `apply()` under one field manager, so a redelivered job converges on the
same objects instead of duplicating them, and the API server tells us exactly
which fields we own.
"""

from __future__ import annotations

import json
import os
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

FIELD_MANAGER = "keel-provisioner"

#: kind -> (group, version, plural). Explicit rather than discovered: the set is
#: small, fixed by control/render/, and a typo should fail at import not at 3am.
RESOURCES: dict[str, tuple[str, str, str]] = {
    "Namespace": ("", "v1", "namespaces"),
    "Node": ("", "v1", "nodes"),
    "ResourceQuota": ("", "v1", "resourcequotas"),
    "NetworkPolicy": ("networking.k8s.io", "v1", "networkpolicies"),
    "RoleBinding": ("rbac.authorization.k8s.io", "v1", "rolebindings"),
    "Service": ("", "v1", "services"),
    "Deployment": ("apps", "v1", "deployments"),
    "ScaledObject": ("keda.sh", "v1alpha1", "scaledobjects"),
}

NAMESPACED = {k for k in RESOURCES if k != "Namespace"}


async def load() -> None:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
    else:
        await config.load_kube_config()


class Cluster:
    """Wraps the dynamic-ish apply. Held open for the life of the worker."""

    def __init__(self) -> None:
        self._api = client.ApiClient()

    async def close(self) -> None:
        await self._api.close()

    async def apply(self, obj: dict[str, Any]) -> dict[str, Any]:
        """Server-side apply. Idempotent by construction."""
        kind = obj["kind"]
        group, version, plural = RESOURCES[kind]
        name = obj["metadata"]["name"]
        ns = obj["metadata"].get("namespace")

        if group:
            base = f"/apis/{group}/{version}"
        else:
            base = f"/api/{version}"
        path = f"{base}/namespaces/{ns}/{plural}/{name}" if ns else f"{base}/{plural}/{name}"

        return await self._call(
            path,
            "PATCH",
            body=obj,
            headers={"Content-Type": "application/apply-patch+yaml"},
            query=[("fieldManager", FIELD_MANAGER), ("force", "true")],
        )

    async def get(self, kind: str, name: str, namespace: str | None = None) -> dict | None:
        group, version, plural = RESOURCES[kind]
        base = f"/apis/{group}/{version}" if group else f"/api/{version}"
        path = (
            f"{base}/namespaces/{namespace}/{plural}/{name}"
            if namespace
            else f"{base}/{plural}/{name}"
        )
        try:
            return await self._call(path, "GET")
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    async def pods_for(self, namespace: str, deployment_id: str) -> list[dict]:
        res = await self._call(
            f"/api/v1/namespaces/{namespace}/pods",
            "GET",
            query=[("labelSelector", f"keel.io/deployment-id={deployment_id}")],
        )
        return res.get("items", [])

    async def pod_logs(self, namespace: str, pod: str, tail: int = 40) -> str:
        """The log tail is what turns a `failed` event into something actionable
        -- vLLM says precisely how much memory it wanted."""
        try:
            return await self._call(
                f"/api/v1/namespaces/{namespace}/pods/{pod}/log",
                "GET",
                query=[("tailLines", str(tail))],
                raw=True,
            )
        except ApiException:
            return ""

    async def delete(self, kind: str, name: str, namespace: str | None = None) -> None:
        group, version, plural = RESOURCES[kind]
        base = f"/apis/{group}/{version}" if group else f"/api/{version}"
        path = (
            f"{base}/namespaces/{namespace}/{plural}/{name}"
            if namespace
            else f"{base}/{plural}/{name}"
        )
        try:
            await self._call(path, "DELETE")
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def _call(
        self,
        path: str,
        method: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: list[tuple[str, str]] | None = None,
        raw: bool = False,
    ) -> Any:
        hdrs = {"Accept": "*/*" if raw else "application/json"}
        hdrs.update(headers or {})
        # _preload_content=False returns the aiohttp response untouched, which
        # is what we want: the generated client's deserializer is built around
        # typed models and mangles arbitrary JSON.
        resp = await self._api.call_api(
            path,
            method,
            header_params=hdrs,
            query_params=query or [],
            body=body,
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
        text = await resp.text()
        if resp.status >= 400:
            raise ApiException(status=resp.status, reason=text)
        if raw:
            return text
        return json.loads(text) if text else {}
