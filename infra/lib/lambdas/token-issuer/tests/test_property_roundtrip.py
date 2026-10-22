"""Property test for the Project_Token round trip (media-harness-projects-access task 3.5).

Feature: media-harness-projects-access, Property 7: Project_Token round trip

A local RSA key behind a fake KMS client (``Sign`` + ``GetPublicKey``). For generated subs, Project ids,
issue times, and lifetimes in [60, 3600], BOTH handlers (first issuance and Token_Refresh) must produce:

  * the exact header ``{"typ":"JWT","alg":"RS256","kid":<signing kid>}``,
  * the exact seven-claim payload ``iss, aud, sub, project_id, iat, exp, jti`` and nothing else,
  * ``exp == iat + lifetime`` (``iat == floor(now)``),
  * a signature that verifies against the public key the OIDC publisher derives — checked here with
    ``cryptography`` directly and through ``tokens.verify`` (the OIDC publisher of task 8.3 is not built
    yet, so we derive the JWK ourselves), and
  * a fresh ``jti`` that is never shared across issued tokens.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from types import SimpleNamespace

import index
import tokens

REGION = "us-west-2"
CURRENT_KID = "1234abcd-12ab-34cd-56ef-1234567890ab"
ISS = "https://d111.cloudfront.net"
AUD = "mediaworks-harness-search-sandbox"
TABLE = "mediaworks-harness-projects-test"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _KEY.public_key()
_PUBS = {CURRENT_KID: _PUB}


class LocalRsaVerifier:
    """Test-only ``tokens.Verifier`` over local RSA public keys (the published public key here)."""

    def __init__(self, pubkeys):
        self._pub = dict(pubkeys)

    def __contains__(self, kid):
        return isinstance(kid, str) and kid in self._pub

    def verify_digest(self, kid, digest, signature):
        try:
            self._pub[kid].verify(signature, digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False


_VERIFIER = LocalRsaVerifier(_PUBS)

# Every jti produced during this test run; a collision would be a defect (uuid4 should never repeat).
_SEEN_JTIS: set[str] = set()


class _Kms:
    """Signs/returns-public-key with ``_KEY``; a separate instance mints presented tokens for refresh."""

    def __init__(self) -> None:
        self.sign_calls: list[str] = []

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        assert (MessageType, SigningAlgorithm) == ("DIGEST", "RSASSA_PKCS1_V1_5_SHA_256")
        self.sign_calls.append(KeyId)
        return {"Signature": _KEY.sign(Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))}

    def get_public_key(self, *, KeyId):
        der = _PUB.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"KeyId": KeyId, "PublicKey": der, "SigningAlgorithms": ["RSASSA_PKCS1_V1_5_SHA_256"]}


class _EchoVerifier:
    """A stand-in User_Token verifier: the event's ``userToken`` IS the sub (first issuance only)."""

    def verify(self, token, *, now):
        return {"sub": token}


class _AlwaysMemberClient:
    """Grants access to any requested (project, sub) pair, so issuance always reaches the signing step."""

    def batch_get_item(self, *, RequestItems):
        (name, spec), = RequestItems.items()
        assert spec["ConsistentRead"] is True
        items = []
        for key in spec["Keys"]:
            if key["SK"] == "META":
                items.append({"PK": key["PK"], "SK": "META", "status": "active"})
            elif key["SK"].startswith("MEMBER#"):
                items.append({"PK": key["PK"], "SK": key["SK"], "role": "owner"})
        return {"Responses": {name: items}, "UnprocessedKeys": {}}


def _table():
    return SimpleNamespace(name=TABLE, meta=SimpleNamespace(client=_AlwaysMemberClient()))


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _check_token(token: str, *, sub: str, pid: str, iat: int, lifetime: int, now: float, jti: str) -> None:
    header_seg, payload_seg, sig_seg = token.split(".")
    header = json.loads(_b64url_decode(header_seg))
    assert header == {"typ": "JWT", "alg": "RS256", "kid": CURRENT_KID}

    payload = json.loads(_b64url_decode(payload_seg))
    assert set(payload) == {"iss", "aud", "sub", "project_id", "iat", "exp", "jti"}
    assert payload["iss"] == ISS and payload["aud"] == AUD
    assert payload["sub"] == sub and payload["project_id"] == pid
    assert payload["iat"] == iat and payload["exp"] == iat + lifetime
    assert payload["jti"] == jti
    uuid.UUID(payload["jti"])  # a well-formed uuid4

    # Signature verifies against the derived public key (cryptography directly) ...
    signing_input = f"{header_seg}.{payload_seg}"
    _PUB.verify(_b64url_decode(sig_seg), hashlib.sha256(signing_input.encode("ascii")).digest(),
                padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
    # ... and through the production verify path.
    claims = tokens.verify(token, _VERIFIER, ISS, AUD, now)
    assert claims == payload

    assert jti not in _SEEN_JTIS, "a jti was reused"
    _SEEN_JTIS.add(jti)


_subs = st.text(st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=48)
_pids = st.builds(lambda h: "p" + h, st.text("0123456789abcdef", min_size=32, max_size=32))
_iats = st.integers(min_value=0, max_value=4_000_000_000)
_lifetimes = st.integers(min_value=60, max_value=3600)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sub=_subs, pid=_pids, iat=_iats, lifetime=_lifetimes)
def test_issue_and_refresh_round_trip(sub, pid, iat, lifetime):
    now = float(iat)
    clock = lambda: now  # noqa: E731
    signer = tokens.KmsSigner(_Kms(), CURRENT_KID)

    issue = index.make_issue_handler(_EchoVerifier(), _table(), signer, ISS, AUD, lifetime,
                                     clock=clock, emit_metric=lambda *_: None)
    resp = issue({"userToken": sub, "projectId": pid})
    assert set(resp) == {"status", "token", "exp", "jti"} and resp["status"] == "ok"
    assert resp["exp"] == iat + lifetime
    _check_token(resp["token"], sub=sub, pid=pid, iat=iat, lifetime=lifetime, now=now, jti=resp["jti"])

    # Refresh: a presented token (minted on a SEPARATE KMS so the handler's own signing is isolated).
    presented = tokens.issue(tokens.KmsSigner(_Kms(), CURRENT_KID), iss=ISS, aud=AUD, sub=sub,
                             project_id=pid, now=now, lifetime=lifetime)
    refresh = index.make_refresh_handler(_VERIFIER, _table(), signer, ISS, AUD, lifetime,
                                         clock=clock, emit_metric=lambda *_: None)
    resp2 = refresh({"token": presented.token})
    assert set(resp2) == {"status", "token", "exp", "jti"} and resp2["status"] == "ok"
    assert resp2["jti"] != presented.jti and resp2["jti"] != resp["jti"]
    _check_token(resp2["token"], sub=sub, pid=pid, iat=iat, lifetime=lifetime, now=now, jti=resp2["jti"])
