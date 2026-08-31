"""Integration fixtures.

These need a real Postgres. They skip cleanly when one is absent so `make test`
still works on a laptop with nothing running -- the unit tests in domain/ are
the ones that must never need infrastructure.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
import pytest_asyncio

os.environ.setdefault("KEEL_DEV_AUTH", "1")
os.environ.setdefault("KEEL_DEV_GROUPS", "keel-dev")

DSN = os.environ.get("KEEL_DSN", "postgresql://keel:keel@localhost:5432/keel")


def _db_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2) as conn:
            conn.execute("select 1 from teams limit 1")
    except (psycopg.OperationalError, psycopg.errors.UndefinedTable):
        # No server, or a server with no schema: both mean "skip", not "fail".
        return False
    return True


requires_db = pytest.mark.skipif(
    not _db_available(), reason="no migrated Postgres at KEEL_DSN (run: make db && make migrate)"
)


@pytest_asyncio.fixture(autouse=True)
async def _pool():
    """One pool for the whole test, opened once.

    Fixtures used to open and close it individually, so whichever tore down
    first closed it out from under the others.
    """
    from control import db

    await db.open_pool()
    yield
    await db.close_pool()


@pytest_asyncio.fixture
async def client():
    import httpx
    from httpx import ASGITransport

    from control.api.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def team():
    """A throwaway team per test, so quota and name-collision tests do not
    interfere with each other."""
    from control import db

    slug = f"t{uuid.uuid4().hex[:10]}"
    async with db.transaction() as conn:
        await conn.execute(
            """insert into teams (id, slug, oidc_group, gpu_quota)
               values (gen_random_uuid(), %s, 'keel-dev', 2)""",
            (slug,),
        )
    yield slug
    async with db.transaction() as conn:
        await conn.execute(
            """delete from deployment_events where deployment_id in
                 (select id from deployments where team_id =
                   (select id from teams where slug = %s))""",
            (slug,),
        )
        await conn.execute(
            """delete from jobs where deployment_id in
                 (select id from deployments where team_id =
                   (select id from teams where slug = %s))""",
            (slug,),
        )
        await conn.execute(
            "delete from deployments where team_id = (select id from teams where slug = %s)",
            (slug,),
        )
        await conn.execute("delete from teams where slug = %s", (slug,))
