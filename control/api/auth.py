"""Caller identity from an OIDC bearer token.

Teams map to a groups claim, with the claim-to-team mapping held as a row in
`teams`. No user management code is ever written here -- membership is already
someone else's job and already audited.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException

#: Development escape hatch. Off unless explicitly set, and the API refuses to
#: start in a way that could leave it on by accident in a real deployment.
DEV_AUTH = os.environ.get("KEEL_DEV_AUTH") == "1"
DEV_GROUPS = tuple(g for g in os.environ.get("KEEL_DEV_GROUPS", "keel-dev").split(",") if g)


@dataclass(frozen=True, slots=True)
class Caller:
    sub: str
    groups: tuple[str, ...]


async def caller(authorization: Annotated[str | None, Header()] = None) -> Caller:
    if DEV_AUTH:
        return Caller(sub=os.environ.get("KEEL_DEV_SUB", "dev@localhost"), groups=DEV_GROUPS)

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")

    # TODO(phase1): verify the JWT against the IdP's JWKS -- signature, issuer,
    # audience, expiry -- then read `sub` and the groups claim. Until that lands
    # the API is unusable without KEEL_DEV_AUTH, which is the intended state:
    # better to be obviously unauthenticated than quietly so.
    raise HTTPException(501, "OIDC verification not implemented; set KEEL_DEV_AUTH=1 for local use")
