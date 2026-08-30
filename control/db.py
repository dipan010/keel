"""Connection pool. The queue primitives live in repo.py."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


async def open_pool() -> None:
    await pool().open(wait=True, timeout=10)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def transaction() -> AsyncIterator[Any]:
    """One connection, one transaction. Commits on clean exit, rolls back on any
    exception -- which is what makes insert-plus-enqueue atomic."""
    async with pool().connection() as conn, conn.transaction():
        yield conn


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
