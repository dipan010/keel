"""Queue worker: render manifests, server-side apply, smoke-test, mark ready.

Separate from the API because it runs for minutes and must survive restarts.
Stateless -- everything it knows is in Postgres.
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger()

FIELD_MANAGER = "keel-provisioner"
POLL_INTERVAL = 2.0


async def provision(dep: dict) -> None:
    """The ladder from section 3.2 of the architecture doc.

    04  ensure namespace, ResourceQuota, NetworkPolicy
    05  render manifests, SERVER-SIDE APPLY under FIELD_MANAGER with an object
        name derived from deployment_id. This is the idempotency mechanism: a
        redelivered job converges on the same objects instead of duplicating.
    06  -> scheduling. If no node matches the accelerator this fails immediately
        and permanently: surface "no capacity for a100-80g", never spin.
    07  -> loading. Init container fetches weights into the node-local cache.
    08  wait for readiness. OOM here is the classic failure -- the model fits on
        disk and not in VRAM. Capture the log tail; vLLM says what it wanted.
    09  write the LiteLLM route and trigger a config reload.
    10  smoke-test THROUGH THE GATEWAY -- a real completion, not a readiness
        probe. An endpoint that starts but returns garbage is worse than one
        that failed, because nobody finds out until a developer does.
    11  -> ready, emit the event, delete the job, notify.
    """
    raise NotImplementedError


async def loop() -> None:
    log.info("provisioner.start", field_manager=FIELD_MANAGER)
    while True:
        # async with db.pool().connection() as conn:
        #     async with conn.transaction():
        #         job = await db.claim_job(conn)
        # if job: await provision(job)
        await asyncio.sleep(POLL_INTERVAL)


def run() -> None:
    asyncio.run(loop())


if __name__ == "__main__":
    run()
