"""Unit tests for User_Token verification (task 3.3): the Cognito verifier and the dev-only Dev Login verifier.

The pool JWKS is a local RSA key served by a counting fetcher, so "cached per container" and "one refetch on
an unknown kid" are asserted on real calls. Tokens are minted with PyJWT in the Cognito access-token shape.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cognito import CognitoJwks, CognitoUserTokenVerifier, JwksUnavailable, UserTokenInvalid
from dev_login import DEFAULT_DEV_JWT_SECRET, DevLoginUserTokenVerifier

# Minting Dev Login tokens with the harness's 29-byte default secret is expected here.
pytestmark = pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")

REGION = "us-west-2"
POOL = "us-west-2_AbCdEf123"
CLIENT = "labuiclient123"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
KID = "pool-kid-1"
SUB = "8f7c1e2a-1111-2222-3333-444455556666"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    d = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**d, "kid": kid, "alg": "RS256", "use": "sig"}


class Fetcher:
    def __init__(self, keys: list[dict]) -> None:
        self.keys = keys
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, url: str) -> dict:
        self.calls.append(url)
        if self.fail:
            raise OSError("unreachable")
        return {"keys": list(self.keys)}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def cognito_token(now: int, *, key=_KEY, kid=KID, **overrides) -> str:
    claims = {"sub": SUB, "iss": ISSUER, "client_id": CLIENT, "token_use": "access", "scope": "openid",
              "auth_time": now, "iat": now, "exp": now + 3600, "jti": "abc", "username": SUB}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def fetcher() -> Fetcher:
    return Fetcher([jwk(_KEY, KID)])


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier(fetcher, clock) -> CognitoUserTokenVerifier:
    jwks = CognitoJwks(f"{ISSUER}/.well-known/jwks.json", fetch=fetcher, clock=clock)
    return CognitoUserTokenVerifier(region=REGION, user_pool_id=POOL, client_id=CLIENT, jwks=jwks)


def test_valid_access_token_returns_claims(verifier, fetcher):
    now = int(time.time())
    claims = verifier.verify(cognito_token(now), now=now)
    assert claims["sub"] == SUB
    assert fetcher.calls == [f"{ISSUER}/.well-known/jwks.json"]


def test_default_jwks_url_is_the_pool_well_known():
    v = CognitoUserTokenVerifier(region=REGION, user_pool_id=POOL, client_id=CLIENT)
    assert v.issuer == ISSUER
    assert v.jwks.url == f"{ISSUER}/.well-known/jwks.json"


def test_jwks_is_fetched_once_per_container(verifier, fetcher):
    now = int(time.time())
    for _ in range(5):
        verifier.verify(cognito_token(now), now=now)
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"iss": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Other"}, "iss"),
        ({"token_use": "id"}, "token_use"),
        ({"client_id": "someotherclient"}, "client_id"),
        ({"client_id": None, "aud": CLIENT}, "missing_client_id"),  # an ID-token shape
        ({"sub": ""}, "sub"),
        ({"exp": None}, "missing_exp"),
    ],
)
def test_claim_checks(verifier, overrides, reason):
    now = int(time.time())
    with pytest.raises(UserTokenInvalid) as exc:
        verifier.verify(cognito_token(now, **overrides), now=now)
    assert exc.value.reason == reason


def test_exp_is_strict_at_the_boundary(verifier):
    now = int(time.time())
    token = cognito_token(now, exp=now + 10)
    assert verifier.verify(token, now=now + 9)["sub"] == SUB
    with pytest.raises(UserTokenInvalid) as exc:
        verifier.verify(token, now=now + 10)
    assert exc.value.reason == "exp"


def test_signature_from_another_key_under_the_pool_kid_is_rejected(verifier):
    now = int(time.time())
    with pytest.raises(UserTokenInvalid) as exc:
        verifier.verify(cognito_token(now, key=_KEY2), now=now)
    assert exc.value.reason == "signature"


def test_unknown_kid_refetches_once_then_rate_limits(verifier, fetcher, clock):
    now = int(time.time())
    verifier.verify(cognito_token(now), now=now)
    clock.t += 31
    with pytest.raises(UserTokenInvalid) as exc:
        verifier.verify(cognito_token(now, key=_KEY2, kid="unknown"), now=now)
    assert exc.value.reason == "kid"
    assert len(fetcher.calls) == 2
    with pytest.raises(UserTokenInvalid):
        verifier.verify(cognito_token(now, key=_KEY2, kid="unknown"), now=now)
    assert len(fetcher.calls) == 2  # within MIN_REFETCH_INTERVAL_S: no further fetch


def test_rotated_pool_key_is_picked_up_by_the_refetch(verifier, fetcher, clock):
    now = int(time.time())
    verifier.verify(cognito_token(now), now=now)
    fetcher.keys.append(jwk(_KEY2, "pool-kid-2"))
    clock.t += 31
    assert verifier.verify(cognito_token(now, key=_KEY2, kid="pool-kid-2"), now=now)["sub"] == SUB


def test_unreachable_jwks_is_unavailable_not_invalid(verifier, fetcher):
    fetcher.fail = True
    now = int(time.time())
    with pytest.raises(JwksUnavailable):
        verifier.verify(cognito_token(now), now=now)


@pytest.mark.parametrize("token", [None, "", 123, "not-a-jwt", "a.b.c", "x" * 9000])
def test_absent_or_malformed(verifier, token):
    with pytest.raises(UserTokenInvalid):
        verifier.verify(token, now=time.time())


def test_hs256_dev_token_is_rejected_by_the_cognito_verifier(verifier, fetcher):
    now = int(time.time())
    dev = jwt.encode({"sub": "local-dev-user", "iss": ISSUER, "client_id": CLIENT, "token_use": "access",
                      "iat": now, "exp": now + 60}, DEFAULT_DEV_JWT_SECRET, algorithm="HS256",
                     headers={"kid": KID})
    with pytest.raises(UserTokenInvalid) as exc:
        verifier.verify(dev, now=now)
    assert exc.value.reason == "alg"


# ------------------------------------------------------------------------------------------------
# Dev Login verifier (DEV ONLY)
# ------------------------------------------------------------------------------------------------


def dev_token(now: int, *, secret=DEFAULT_DEV_JWT_SECRET, **overrides) -> str:
    """The claim shape `signInDev()` in ui/src/auth/devAuth.ts mints."""
    claims = {"token_use": "access", "scope": "openid email profile", "username": "local-dev-user",
              "sub": "local-dev-user", "iss": "https://cognito-idp.us-west-2.amazonaws.com/local-dev-pool",
              "aud": "local-dev-client", "iat": now, "exp": now + 86400}
    claims.update(overrides)
    return jwt.encode(claims, secret, algorithm="HS256", headers={"typ": "JWT"})


def test_dev_verifier_accepts_a_dev_login_token(monkeypatch):
    monkeypatch.delenv("VITE_DEV_JWT_SECRET", raising=False)
    now = int(time.time())
    assert DevLoginUserTokenVerifier().verify(dev_token(now), now=now)["sub"] == "local-dev-user"


def test_dev_verifier_reads_vite_dev_jwt_secret(monkeypatch):
    monkeypatch.setenv("VITE_DEV_JWT_SECRET", "my-local-secret")
    now = int(time.time())
    v = DevLoginUserTokenVerifier()
    assert v.verify(dev_token(now, secret="my-local-secret"), now=now)["sub"] == "local-dev-user"
    with pytest.raises(UserTokenInvalid) as exc:
        v.verify(dev_token(now), now=now)
    assert exc.value.reason == "signature"


@pytest.mark.parametrize(("overrides", "reason"), [({"token_use": "id"}, "token_use"), ({"sub": ""}, "sub")])
def test_dev_verifier_claim_checks(overrides, reason):
    now = int(time.time())
    with pytest.raises(UserTokenInvalid) as exc:
        DevLoginUserTokenVerifier(secret=DEFAULT_DEV_JWT_SECRET).verify(dev_token(now, **overrides), now=now)
    assert exc.value.reason == reason


def test_dev_verifier_rejects_expired_and_rs256():
    now = int(time.time())
    v = DevLoginUserTokenVerifier(secret=DEFAULT_DEV_JWT_SECRET)
    with pytest.raises(UserTokenInvalid) as exc:
        v.verify(dev_token(now, exp=now), now=now)
    assert exc.value.reason == "exp"
    with pytest.raises(UserTokenInvalid) as exc:
        v.verify(cognito_token(now), now=now)
    assert exc.value.reason == "alg"


def test_dev_verifier_checks_iss_and_aud_only_when_given():
    now = int(time.time())
    v = DevLoginUserTokenVerifier(secret=DEFAULT_DEV_JWT_SECRET, issuer="https://x", audience="local-dev-client")
    with pytest.raises(UserTokenInvalid) as exc:
        v.verify(dev_token(now), now=now)
    assert exc.value.reason == "iss"
