"""OIDC verification, against tokens signed with a real key.

No IdP needed: the test mints an RSA key, signs its own tokens, and serves a
JWKS through a mock transport. That exercises the actual signature path rather
than a stub that always says yes.
"""

from __future__ import annotations

import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt

from control.api import auth

ISSUER = "https://idp.example.test"


def _keypair(kid: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    numbers = key.public_key().public_numbers()

    def b64(n: int) -> str:
        import base64

        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64(numbers.n),
        "e": b64(numbers.e),
    }
    return pem, jwk


@pytest.fixture
def idp(monkeypatch):
    """One issuer, two keys -- the second only appears after a rotation."""
    pem_a, jwk_a = _keypair("key-a")
    pem_b, jwk_b = _keypair("key-b")
    state = {"keys": [jwk_a], "fetches": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": f"{ISSUER}/jwks"})
        state["fetches"] += 1
        return httpx.Response(200, json={"keys": state["keys"]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return real_client(*a, **kw)

    monkeypatch.setattr(auth.httpx, "AsyncClient", patched)
    monkeypatch.setattr(auth, "ISSUER", ISSUER)
    monkeypatch.setattr(auth, "AUDIENCE", "keel")
    monkeypatch.setattr(auth, "DEV_AUTH", False)
    auth.JWKS.reset()
    yield {"pem_a": pem_a, "pem_b": pem_b, "jwk_b": jwk_b, "state": state}
    auth.JWKS.reset()


def mint(pem: str, kid: str, **over) -> str:
    claims = {
        "sub": "alice@example.test",
        "iss": ISSUER,
        "aud": "keel",
        "exp": int(time.time()) + 300,
        "groups": ["keel-dev", "platform"],
        **over,
    }
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


async def test_a_valid_token_yields_subject_and_groups(idp):
    who = await auth.verify(mint(idp["pem_a"], "key-a"))
    assert who.sub == "alice@example.test"
    assert who.groups == ("keel-dev", "platform")


async def test_a_token_signed_by_the_wrong_key_is_rejected(idp):
    # Signed with key-b, but the IdP only publishes key-a under that kid.
    bad = mint(idp["pem_b"], "key-a")
    with pytest.raises(HTTPException) as e:
        await auth.verify(bad)
    assert e.value.status_code == 401


async def test_an_expired_token_is_rejected(idp):
    with pytest.raises(HTTPException, match="expired"):
        await auth.verify(mint(idp["pem_a"], "key-a", exp=int(time.time()) - 60))


async def test_the_wrong_audience_is_rejected(idp):
    with pytest.raises(HTTPException) as e:
        await auth.verify(mint(idp["pem_a"], "key-a", aud="some-other-service"))
    assert e.value.status_code == 401


async def test_the_wrong_issuer_is_rejected(idp):
    with pytest.raises(HTTPException) as e:
        await auth.verify(mint(idp["pem_a"], "key-a", iss="https://evil.example"))
    assert e.value.status_code == 401


async def test_an_unsigned_token_is_rejected(idp):
    """alg=none is the classic JWT attack. jose must refuse it."""
    # Built by hand: jose refuses to *encode* alg=none, which is correct of
    # it, so the only way to test the decode path is to forge one directly.
    import base64
    import json

    def seg(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    forged = (
        seg({"alg": "none", "typ": "JWT", "kid": "key-a"})
        + "."
        + seg({"sub": "mallory", "iss": ISSUER, "aud": "keel", "exp": int(time.time()) + 300})
        + "."
    )
    with pytest.raises(HTTPException) as e:
        await auth.verify(forged)
    assert e.value.status_code == 401


async def test_algorithm_confusion_is_rejected(idp):
    """The subtler forgery: declare HS256 so the verifier treats the RSA
    PUBLIC key -- which the attacker also has, it is published -- as an HMAC
    secret. Only an algorithm allowlist stops this; checking the signature
    does not, because the signature is genuinely valid under that reading."""
    import base64
    import json

    n = idp["jwk_b"]["n"]  # any public value the attacker can read from JWKS
    forged = jwt.encode(
        {"sub": "mallory", "iss": ISSUER, "aud": "keel", "exp": int(time.time()) + 300},
        key=n,
        algorithm="HS256",
        headers={"kid": "key-a"},
    )
    assert base64.urlsafe_b64decode(forged.split(".")[0] + "==")
    assert json.loads(base64.urlsafe_b64decode(forged.split(".")[0] + "=="))["alg"] == "HS256"

    with pytest.raises(HTTPException) as e:
        await auth.verify(forged)
    assert e.value.status_code == 401


async def test_rejection_reasons_are_not_echoed_to_the_caller(idp):
    """Distinguishing "wrong audience" from "bad signature" tells an attacker
    which half of the token to work on."""
    with pytest.raises(HTTPException) as e:
        await auth.verify(mint(idp["pem_a"], "key-a", aud="wrong"))
    assert e.value.detail == "token rejected"


async def test_key_rotation_is_picked_up_without_a_restart(idp):
    """A TTL alone would fail every request between rotation and expiry."""
    await auth.verify(mint(idp["pem_a"], "key-a"))  # warms the cache
    idp["state"]["keys"] = [idp["jwk_b"]]  # IdP rotates
    who = await auth.verify(mint(idp["pem_b"], "key-b"))
    assert who.sub == "alice@example.test"


async def test_a_token_with_no_key_id_is_rejected(idp):
    token = jwt.encode(
        {"sub": "x", "iss": ISSUER, "aud": "keel", "exp": int(time.time()) + 300},
        idp["pem_a"],
        algorithm="RS256",
    )
    with pytest.raises(HTTPException, match="key id"):
        await auth.verify(token)


async def test_no_groups_is_a_valid_user_not_an_error(idp):
    """Distinct from a bad token: they simply belong to no team, and every
    team-scoped route will 404 for them."""
    who = await auth.verify(mint(idp["pem_a"], "key-a", groups=[]))
    assert who.groups == ()


async def test_groups_sent_as_a_string_are_split(idp):
    who = await auth.verify(mint(idp["pem_a"], "key-a", groups="keel-dev platform"))
    assert who.groups == ("keel-dev", "platform")


async def test_the_groups_claim_name_is_configurable(idp, monkeypatch):
    monkeypatch.setattr(auth, "GROUPS_CLAIM", "roles")
    token = mint(idp["pem_a"], "key-a", groups=["ignored"], roles=["from-roles"])
    assert (await auth.verify(token)).groups == ("from-roles",)


async def test_unconfigured_and_no_dev_auth_fails_closed(monkeypatch):
    monkeypatch.setattr(auth, "DEV_AUTH", False)
    monkeypatch.setattr(auth, "ISSUER", "")
    with pytest.raises(HTTPException) as e:
        await auth.caller(authorization="Bearer anything")
    assert e.value.status_code == 501
    assert "KEEL_OIDC_ISSUER" in e.value.detail


async def test_a_missing_header_is_401_not_500(idp):
    with pytest.raises(HTTPException) as e:
        await auth.caller(authorization=None)
    assert e.value.status_code == 401


async def test_dev_auth_short_circuits_everything(monkeypatch):
    monkeypatch.setattr(auth, "DEV_AUTH", True)
    monkeypatch.setattr(auth, "DEV_GROUPS", ("keel-dev",))
    who = await auth.caller(authorization=None)
    assert who.groups == ("keel-dev",)
