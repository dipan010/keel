"""Desired state (database) vs actual state (cluster), every 60 seconds.

Deliberately dull, and structurally unable to delete anything: its
ServiceAccount is get/list/watch only, which makes invariant I5 impossible to
violate rather than merely forbidden.
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger()

INTERVAL = 60.0


async def reconcile_once() -> None:
    """
    desired = deployments where status != deleted
    actual  = Deployments across all namespaces labelled managed-by=keel

    missing while ready      -> degraded, enqueue provision
    unschedulable            -> degraded, alert
    zero ready, lane b       -> degraded with the pod log tail
    zero ready, lane c       -> scaled_to_zero          (expected, not a fault)
    spec differs from render -> updating, re-apply      (someone ran kubectl)
    otherwise                -> touch last_reconciled_at

    Then: any labelled object whose deployment-id has no row is an ORPHAN.
    Alert, severity high. Never delete -- see the ServiceAccount.
    """
    raise NotImplementedError


async def loop() -> None:
    log.info("reconciler.start", interval=INTERVAL)
    while True:
        try:
            await reconcile_once()
        except NotImplementedError:
            pass
        except Exception:
            log.exception("reconcile.failed")
        await asyncio.sleep(INTERVAL)


def run() -> None:
    asyncio.run(loop())


if __name__ == "__main__":
    run()
