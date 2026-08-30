"""Connection pool and the queue primitives.

The queue lives in Postgres rather than a broker. See ADR-0005.
"""

from __future__ import annotations

import os
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DSN = os.environ.get("KEEL_DSN", "postgresql://keel:keel@localhost:5432/keel")

_pool: AsyncConnectionPool | None = None


def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(DSN, kwargs={"row_factory": dict_row}, open=False)
    return _pool


CLAIM_JOB = """
update jobs
   set locked_until = now() + interval '15 minutes',
       attempts     = attempts + 1
 where id = (
       select id from jobs
        where locked_until is null or locked_until < now()
        order by created_at
        for update skip locked
        limit 1
 )
returning id, deployment_id, kind, attempts
"""


async def claim_job(conn: Any) -> dict[str, Any] | None:
    """FOR UPDATE SKIP LOCKED. An abandoned lock expires and the job is
    reclaimed, so there are no dead letters to manage."""
    cur = await conn.execute(CLAIM_JOB)
    return await cur.fetchone()


async def record_transition(
    conn: Any,
    deployment_id: str,
    frm: str | None,
    to: str,
    actor: str,
    reason_code: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Status is a projection of the event log, never a field somebody sets."""
    import json

    await conn.execute(
        "update deployments set status = %s, updated_at = now() where id = %s",
        (to, deployment_id),
    )
    await conn.execute(
        """insert into deployment_events
             (deployment_id, from_status, to_status, actor, reason_code, detail)
           values (%s, %s, %s, %s, %s, %s)""",
        (deployment_id, frm, to, actor, reason_code, json.dumps(detail) if detail else None),
    )
