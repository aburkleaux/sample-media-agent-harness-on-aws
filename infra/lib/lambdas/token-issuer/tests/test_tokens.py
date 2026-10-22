"""Unit tests for Project_Token encoding, KMS-DIGEST signing, and local verification (task 3.1).

A fake KMS client backed by a local RSA key stands in for ``kms:Sign`` / ``kms:GetPublicKey``; it
asserts the exact request shape KMS would receive, so a wrong ``MessageType`` or algorithm fails here
rather than at deploy. Signatures are checked against a JWK derived from the DER ``GetPublicKey``
returns, the same conversion the OIDC publisher performs (prototype ``test_v2_token_kms_jwt.py``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import jwt
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

import tokens
from tokens import (
    REFRESH_GRACE_S,
    InvalidProjectToken,
    KmsSigner,
    KmsVerifier,
)

ISS = "https://d111.cloudfront.net"
AUD = "mediaworks-harness-search-sandbox"
KID = "1234abcd-12ab-34cd-56ef-1234567890ab"
PID = "p" + "c" * 32
SUB = "8f7c1e2a-1111-2222-3333-444455556666"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _der(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _der_to_jwk(der: bytes, kid: str) -> dict:
    pub = serialization.load_der_public_key(der).public_numbers()
    i2b = lambda n: n.to_bytes((n.bit_length() + 7) // 8, "big")  # noqa: E731
    return {"kty": "RSA", "n": _b64(i2b(pub.n)), "e": _b64(i2b(pub.e)), "kid": kid, "alg": "RS256", "use": "sig"}


class FakeKms:
    """``sign`` / ``verify`` / ``get_public_key`` over local RSA keys, asserting the KMS request shape."""

    def __init__(self, keys: dict[str, rsa.RSAPrivateKey]) -> None:
        self.keys = keys
        self.sign_calls: list[dict] = []
        self.verify_calls: list[str] = []
        self.get_public_key_calls: list[str] = []
        self.fail_verify = False

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        assert MessageType == "DIGEST"
        assert SigningAlgorithm == "RSASSA_PKCS1_V1_5_SHA_256"
        assert isinstance(Message, bytes) and len(Message) == 32
        self.sign_calls.append({"KeyId": KeyId, "Message": Message})
        sig = self.keys[KeyId].sign(Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
        return {"KeyId": f"arn:aws:kms:us-west-2:111122223333:key/{KeyId}", "Signature": sig,
                "SigningAlgorithm": SigningAlgorithm}

    def verify(self, *, KeyId, Message, MessageType, Signature, SigningAlgorithm):
        assert MessageType == "DIGEST"
        assert SigningAlgorithm == "RSASSA_PKCS1_V1_5_SHA_256"
        assert isinstance(Message, bytes) and len(Message) == 32
        self.verify_calls.append(KeyId)
        if self.fail_verify:
            raise RuntimeError("KMS unavailable")
        try:
            self.keys[KeyId].public_key().verify(
                Signature, Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
        except InvalidSignature:
            return {"KeyId": KeyId, "SignatureValid": False, "SigningAlgorithm": SigningAlgorithm}
        return {"KeyId": KeyId, "SignatureValid": True, "SigningAlgorithm": SigningAlgorithm}

    def get_public_key(self, *, KeyId):
        self.get_public_key_calls.append(KeyId)
        return {"KeyId": KeyId, "PublicKey": _der(self.keys[KeyId]), "KeySpec": "RSA_2048",
                "KeyUsage": "SIGN_VERIFY", "SigningAlgorithms": ["RSASSA_PKCS1_V1_5_SHA_256"]}


class LocalPemSigner:
    """Test-only RSA signer that produces the same token structure KMS does (for a dev wrapper)."""

    def __init__(self, private_key: rsa.RSAPrivateKey, kid: str) -> None:
        self._key = private_key
        self._kid = kid

    @property
    def kid(self) -> str:
        return self._kid

    def public_key(self) -> rsa.RSAPublicKey:
        return self._key.public_key()

    def sign_digest(self, digest: bytes) -> bytes:
        return self._key.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))


class LocalRsaVerifier:
    """Test-only ``tokens.Verifier`` over local RSA public keys (stands in for KmsVerifier in dev)."""

    def __init__(self, pubkeys: dict[str, rsa.RSAPublicKey]) -> None:
        self._pub = dict(pubkeys)

    def __contains__(self, kid: object) -> bool:
        return isinstance(kid, str) and kid in self._pub

    def verify_digest(self, kid: str, digest: bytes, signature: bytes) -> bool:
        try:
            self._pub[kid].verify(signature, digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False


@pytest.fixture()
def kms() -> FakeKms:
    return FakeKms({KID: _KEY})


@pytest.fixture()
def signer(kms: FakeKms) -> KmsSigner:
    return KmsSigner(kms, KID)


@pytest.fixture()
def keys(kms: FakeKms) -> KmsVerifier:
    return KmsVerifier(kms, [KID])


def _issue(signer, now=None, lifetime=3600, sub=SUB, pid=PID):
    return tokens.issue(signer, iss=ISS, aud=AUD, sub=sub, project_id=pid,
                        now=time.time() if now is None else now, lifetime=lifetime)


def _raw(header: dict, payload: dict, key: rsa.RSAPrivateKey = _KEY) -> str:
    """Sign arbitrary header/payload JSON exactly as KMS would, to build off-spec tokens."""
    si = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    sig = key.sign(hashlib.sha256(si.encode()).digest(), padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
    return f"{si}.{_b64(sig)}"


def _good_header() -> dict:
    return {"typ": "JWT", "alg": "RS256", "kid": KID}


def _good_payload(now: int) -> dict:
    return {"iss": ISS, "aud": AUD, "sub": SUB, "project_id": PID, "iat": now, "exp": now + 3600,
            "jti": str(uuid.uuid4())}


# --- encoding and signing ---------------------------------------------------------------------------


def test_header_and_exactly_seven_claims(signer):
    now = 1_800_000_000
    issued = _issue(signer, now=now, lifetime=900)
    h, p, _ = issued.token.split(".")
    assert json.loads(_unb64(h)) == {"typ": "JWT", "alg": "RS256", "kid": KID}
    payload = json.loads(_unb64(p))
    assert set(payload) == {"iss", "aud", "sub", "project_id", "iat", "exp", "jti"}
    assert payload["iss"] == ISS and payload["aud"] == AUD
    assert payload["sub"] == SUB and payload["project_id"] == PID
    assert payload["iat"] == now and payload["exp"] == now + 900
    assert uuid.UUID(payload["jti"]).version == 4
    assert (issued.exp, issued.jti, issued.iat) == (payload["exp"], payload["jti"], now)


def test_kms_is_asked_to_sign_the_sha256_digest_of_the_signing_input(kms, signer):
    issued = _issue(signer)
    signing_input = issued.token.rsplit(".", 1)[0]
    assert kms.sign_calls == [{"KeyId": KID, "Message": hashlib.sha256(signing_input.encode()).digest()}]


def test_signature_verifies_with_jwk_derived_from_kms_der(kms, signer):
    issued = _issue(signer)
    jwk = _der_to_jwk(kms.get_public_key(KeyId=KID)["PublicKey"], KID)
    claims = jwt.decode(issued.token, jwt.PyJWK(jwk).key, algorithms=["RS256"], audience=AUD, issuer=ISS)
    assert claims["project_id"] == PID


def test_jti_is_fresh_per_token(signer):
    now = 1_800_000_000
    assert len({_issue(signer, now=now).jti for _ in range(20)}) == 20


@pytest.mark.parametrize(
    "kwargs",
    [{"pid": "default"}, {"pid": "P" + "c" * 32}, {"pid": "p" + "c" * 31}, {"sub": ""}, {"lifetime": 59},
     {"lifetime": 3601}],
)
def test_bad_claims_are_refused_before_signing(kms, signer, kwargs):
    with pytest.raises(ValueError):
        _issue(signer, **kwargs)
    assert kms.sign_calls == []


def test_local_pem_signer_matches_kms_shape_and_verifies():
    local = LocalPemSigner(_KEY, "local-dev")
    issued = _issue(local)
    assert json.loads(_unb64(issued.token.split(".")[0]))["kid"] == "local-dev"
    verifier = LocalRsaVerifier({"local-dev": local.public_key()})
    claims = tokens.verify(issued.token, verifier, ISS, AUD, time.time())
    assert claims["sub"] == SUB
    # Same key, same signing scheme: the local and KMS paths verify against the same public key.
    kms_issued = _issue(KmsSigner(FakeKms({"local-dev": _KEY}), "local-dev"))
    tokens.verify(kms_issued.token, verifier, ISS, AUD, time.time())


# --- verify: acceptance and the Refresh_Grace boundary ------------------------------------------------


def test_verify_accepts_an_unexpired_token(signer, keys):
    now = int(time.time())
    issued = _issue(signer, now=now)
    claims = tokens.verify(issued.token, keys, ISS, AUD, now + 10)
    assert claims["sub"] == SUB and claims["project_id"] == PID and claims["jti"] == issued.jti


def test_grace_boundary_exactly_at_grace_is_accepted(signer, keys):
    issued = _issue(signer, now=1_800_000_000, lifetime=3600)
    tokens.verify(issued.token, keys, ISS, AUD, issued.exp + REFRESH_GRACE_S)


def test_grace_boundary_one_second_past_is_refused(signer, keys):
    issued = _issue(signer, now=1_800_000_000, lifetime=3600)
    with pytest.raises(InvalidProjectToken) as exc:
        tokens.verify(issued.token, keys, ISS, AUD, issued.exp + REFRESH_GRACE_S + 1)
    assert exc.value.reason == "expired_beyond_grace"


def test_refresh_grace_is_eight_hours():
    assert REFRESH_GRACE_S == 28800


def test_verify_calls_kms_verify_each_time(kms, signer, keys):
    now = int(time.time())
    for _ in range(3):
        tokens.verify(_issue(signer, now=now).token, keys, ISS, AUD, now)
    assert kms.verify_calls == [KID, KID, KID]


def test_retired_but_published_key_still_verifies():
    retired, current = "key-retired", "key-current"
    kms = FakeKms({retired: _FOREIGN_KEY, current: _KEY})
    keys = KmsVerifier(kms, [retired, current])
    now = int(time.time())
    tokens.verify(_issue(KmsSigner(kms, retired), now=now).token, keys, ISS, AUD, now)
    tokens.verify(_issue(KmsSigner(kms, current), now=now).token, keys, ISS, AUD, now)


# --- verify: refusals (Req 4.23) -------------------------------------------------------------------------


def test_unpublished_kid_is_refused_without_a_kms_call():
    kms = FakeKms({KID: _KEY, "unpublished": _KEY})
    keys = KmsVerifier(kms, [KID])
    now = int(time.time())
    with pytest.raises(InvalidProjectToken) as exc:
        tokens.verify(_issue(KmsSigner(kms, "unpublished"), now=now).token, keys, ISS, AUD, now)
    assert exc.value.reason == "kid"
    assert kms.verify_calls == []


def test_foreign_key_under_a_published_kid_is_refused(keys):
    now = int(time.time())
    forged = _raw(_good_header(), _good_payload(now), key=_FOREIGN_KEY)
    with pytest.raises(InvalidProjectToken) as exc:
        tokens.verify(forged, keys, ISS, AUD, now)
    assert exc.value.reason == "signature"


def test_tampered_payload_is_refused(signer, keys):
    now = int(time.time())
    h, p, s = _issue(signer, now=now).token.split(".")
    payload = json.loads(_unb64(p))
    payload["project_id"] = "p" + "d" * 32
    with pytest.raises(InvalidProjectToken) as exc:
        tokens.verify(f"{h}.{_b64(json.dumps(payload).encode())}.{s}", keys, ISS, AUD, now)
    assert exc.value.reason == "signature"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"iss": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_abc"}, "iss"),
        ({"aud": "mediaworks-harness-search-staging"}, "aud"),
        ({"aud": [AUD]}, "aud"),  # strict: a list containing the audience is not the audience
        ({"project_id": "default"}, "project_id"),
        ({"project_id": "p" + "C" * 32}, "project_id"),
        ({"project_id": None}, "project_id"),
        ({"sub": ""}, "sub"),
        ({"exp": 1.0e12}, "exp"),
        ({"exp": "9999999999"}, "exp"),
        ({"exp": True}, "exp"),
        ({"iat": "now"}, "iat"),
    ],
)
def test_claim_refusals(keys, change, reason):
    now = int(time.time())
    payload = {**_good_payload(now), **change}
    payload = {k: v for k, v in payload.items() if v is not None}
    with pytest.raises(InvalidProjectToken) as exc:
        tokens.verify(_raw(_good_header(), payload), keys, ISS, AUD, now)
    assert exc.value.reason == reason


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "iat", "exp"])
def test_missing_required_claim_is_refused(keys, claim):
    now = int(time.time())
    payload = _good_payload(now)
    del payload[claim]
    with pytest.raises(InvalidProjectToken):
        tokens.verify(_raw(_good_header(), payload), keys, ISS, AUD, now)


@pytest.mark.parametrize(
    "header",
    [
        {"typ": "jwt", "alg": "RS256", "kid": KID},
        {"alg": "RS256", "kid": KID},
        {"typ": "JWT", "alg": "RS512", "kid": KID},
        {"typ": "JWT", "alg": "none", "kid": KID},
        {"typ": "JWT", "alg": "RS256"},
        {"typ": "JWT", "alg": "RS256", "kid": 7},
    ],
)
def test_header_refusals(keys, header):
    now = int(time.time())
    with pytest.raises(InvalidProjectToken):
        tokens.verify(_raw(header, _good_payload(now)), keys, ISS, AUD, now)


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b", "a.b.c", "x" * 9000, 42, {"token": "x"}])
def test_non_tokens_are_refused(keys, token):
    with pytest.raises(InvalidProjectToken):
        tokens.verify(token, keys, ISS, AUD, time.time())


def test_truncated_token_is_refused(signer, keys):
    now = int(time.time())
    with pytest.raises(InvalidProjectToken):
        tokens.verify(_issue(signer, now=now).token[:-10], keys, ISS, AUD, now)


def test_grace_does_not_relax_iat(keys):
    now = int(time.time())
    payload = {**_good_payload(now), "iat": now + 60}
    with pytest.raises(InvalidProjectToken) as exc:
        tokens.verify(_raw(_good_header(), payload), keys, ISS, AUD, now)
    assert exc.value.reason == "iat"


def test_grace_does_not_relax_nbf(keys):
    now = int(time.time())
    payload = {**_good_payload(now), "nbf": now + 3600}
    with pytest.raises(InvalidProjectToken):
        tokens.verify(_raw(_good_header(), payload), keys, ISS, AUD, now)


def test_kms_failure_propagates_rather_than_reading_as_invalid_token(kms, signer, keys):
    now = int(time.time())
    issued = _issue(signer, now=now)
    kms.fail_verify = True
    with pytest.raises(RuntimeError, match="KMS unavailable"):
        tokens.verify(issued.token, keys, ISS, AUD, now)
    kms.fail_verify = False
    tokens.verify(issued.token, keys, ISS, AUD, now)  # failure was not cached


# --- configuration ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, value", [("60", 60), ("3600", 3600), (" 900 ", 900)])
def test_lifetime_parses_in_range(raw, value):
    assert tokens.parse_lifetime_seconds(raw) == value


@pytest.mark.parametrize("raw", [None, "", "59", "3601", "1h", "60.5"])
def test_lifetime_out_of_range_names_value_and_range(raw):
    with pytest.raises(ValueError, match="60-3600"):
        tokens.parse_lifetime_seconds(raw)


def test_lifetime_is_read_once(monkeypatch):
    tokens.configured_lifetime_seconds.cache_clear()
    monkeypatch.setenv("TOKEN_LIFETIME_SECONDS", "1200")
    assert tokens.configured_lifetime_seconds() == 1200
    monkeypatch.setenv("TOKEN_LIFETIME_SECONDS", "60")
    assert tokens.configured_lifetime_seconds() == 1200
    tokens.configured_lifetime_seconds.cache_clear()


def test_published_key_ids_parse():
    assert tokens.parse_published_key_ids(" a , b,,") == frozenset({"a", "b"})
    with pytest.raises(ValueError):
        tokens.parse_published_key_ids("")
