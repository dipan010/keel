"""Caller identity from an OIDC bearer token.

Teams map to a groups claim, with the claim-to-team mapping held as a row in
`teams`. No user management code is written here -- membership is already
someone else's job and already audited.

Three ways the API can be configured, and it fails closed between them:

    KEEL_OIDC_ISSUER set   verify properly against the IdP's JWKS
    KEEL_DEV_AUTH=1        accept anything, as a named local escape hatch
    neither                501, and say so

The third case is deliberate. A permissive default is how a service ships
unauthenticated; a bypass that must be switched on cannot be left on by
accident.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import structlog
from fastapi import Header, HTTPException
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JOSEError

log = structlog.get_logger()

#: Development escape hatch. Off unless explicitly set.
DEV_AUTH = os.environ.get("KEEL_DEV_AUTH") == "1"
DEV_GROUPS = tuple(g for g in os.environ.get("KEEL_DEV_GROUPS", "keel-dev").split(",") if g)

ISSUER = os.environ.get("KEEL_OIDC_ISSUER", "").rstrip("/")
AUDIENCE = os.environ.get("KEEL_OIDC_AUDIENCE", "keel")
#: Providers disagree: "groups" for Keycloak and Dex, "roles" or a namespaced
#: URI elsewhere. Configurable rather than guessed.
GROUPS_CLAIM = os.environ.get("KEEL_OIDC_GROUPS_CLAIM", "groups")
#: Tokens are minted on another machine; a little clock drift is not an attack.
LEEWAY_SECONDS = int(os.environ.get("KEEL_OIDC_LEEWAY", "30"))
JWKS_TTL_SECONDS = int(os.environ.get("KEEL_OIDC_JWKS_TTL", "3600"))
#: An ALLOWLIST, not the token's own claim about itself. Trusting the alg
#: header is the classic JWT attack: a forged token declares "none", or
#: declares HS256 so the verifier treats the RSA public key as an HMAC
#: secret. Neither is possible if the header never chooses the algorithm.
ALGORITHMS = [
    a
    for a in os.environ.get("KEEL_OIDC_ALGORITHMS", "RS256,RS384,RS512,ES256,ES384").split(",")
    if a
]


@dataclass(frozen=True, slots=True)
class Caller:
    sub: str
    groups: tuple[str, ...]


class _Jwks:
    """Cached signing keys, refreshed on an unknown key id.

    A TTL alone is not enough: an IdP can rotate keys at any moment, and every
    request in the window between rotation and expiry would fail. Refreshing
    when a token names a `kid` we do not hold handles rotation immediately,
    while the cooldown stops an unknown kid from becoming a way to make us
    hammer the IdP.
    """

    #: Minimum gap between refreshes triggered by an unknown key id. Stops a
    #: stream of tokens naming nonexistent kids from turning us into a load
    #: generator pointed at the IdP.
    FORCED_REFRESH_COOLDOWN = 10.0

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._forced_at = 0.0
        self._uri: str | None = None

    async def _discover(self, client: httpx.AsyncClient) -> str:
        if self._uri:
            return self._uri
        r = await client.get(f"{ISSUER}/.well-known/openid-configuration", timeout=10)
        r.raise_for_status()
        self._uri = r.json()["jwks_uri"]
        return self._uri

    async def _fetch(self) -> None:
        async with httpx.AsyncClient() as client:
            uri = await self._discover(client)
            r = await client.get(uri, timeout=10)
            r.raise_for_status()
            self._keys = {k["kid"]: k for k in r.json().get("keys", []) if k.get("kid")}
            self._fetched_at = time.monotonic()
        log.info("oidc.jwks_loaded", keys=len(self._keys))

    async def key_for(self, kid: str) -> dict[str, Any] | None:
        stale = (time.monotonic() - self._fetched_at) > JWKS_TTL_SECONDS
        if not self._keys or stale:
            await self._fetch()
        if kid not in self._keys:
            # Probably a rotation. Refresh at once -- measured from the last
            # FORCED refresh, not the last fetch, so a rotation arriving just
            # after the cache warmed is still picked up rather than 401ing
            # until the cooldown from an unrelated fetch elapses.
            since_forced = time.monotonic() - self._forced_at
            if since_forced > self.FORCED_REFRESH_COOLDOWN:
                self._forced_at = time.monotonic()
                await self._fetch()
        return self._keys.get(kid)

    def reset(self) -> None:
        self._keys, self._fetched_at, self._forced_at, self._uri = {}, 0.0, 0.0, None


JWKS = _Jwks()


def _groups(claims: dict[str, Any]) -> tuple[str, ...]:
    raw = claims.get(GROUPS_CLAIM) or []
    if isinstance(raw, str):
        # Some providers send a space- or comma-separated string.
        raw = [g for g in raw.replace(",", " ").split() if g]
    return tuple(str(g) for g in raw)


async def verify(token: str) -> Caller:
    try:
        header = jwt.get_unverified_header(token)
    except JOSEError as exc:
        raise HTTPException(401, "malformed token") from exc

    kid = header.get("kid")
    if not kid:
        raise HTTPException(401, "token has no key id")

    key = await JWKS.key_for(kid)
    if key is None:
        raise HTTPException(401, "token signed by an unknown key")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"leeway": LEEWAY_SECONDS, "require_exp": True},
        )
    except ExpiredSignatureError as exc:
        raise HTTPException(401, "token has expired") from exc
    except JOSEError as exc:
        # JOSEError, not JWTError: a forged alg=none token raises JWKError,
        # which is a sibling rather than a subclass -- so catching JWTError let
        # it escape as a 500. An unhandled path in the auth layer is the last
        # place to be surprised.
        #
        # Deliberately not echoing the library's message: it distinguishes
        # "wrong audience" from "bad signature", which tells an attacker which
        # half of the token to work on.
        log.info("oidc.rejected", reason=str(exc))
        raise HTTPException(401, "token rejected") from exc

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(401, "token has no subject")

    # No groups is not an error: it is a valid user who belongs to no team, and
    # every team-scoped route will 404 for them. Failing here instead would
    # make "you are in no groups" indistinguishable from "your token is bad".
    return Caller(sub=str(sub), groups=_groups(claims))


async def caller(authorization: Annotated[str | None, Header()] = None) -> Caller:
    if DEV_AUTH:
        return Caller(sub=os.environ.get("KEEL_DEV_SUB", "dev@localhost"), groups=DEV_GROUPS)

    if not ISSUER:
        raise HTTPException(
            501,
            "no identity provider configured: set KEEL_OIDC_ISSUER, "
            "or KEEL_DEV_AUTH=1 for local use",
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")

    return await verify(authorization.split(None, 1)[1].strip())
