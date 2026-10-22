"""Delegated issuance: a Project_Token for a dispatched sub-agent from a delegation grant (knowledge-graph tools).

The grant is verified with (fake) ``kms:Verify`` over a local ECDSA key in exactly the wire format
``agent/src/security/delegation.py`` mints; the actor comes ONLY from the verified grant; membership is
re-checked with the real ``access.check_access`` over moto; the issued token is the same Project_Token
first issuance produces (verified with ``tokens.verify``).
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

import delegation
import index
import tokens
from test_issue_handler import AUD, ISS, OTHER_SUB, PID, SIGN_KID, SUB, FakeKms, kms, metrics, table  # noqa: F401

_GRANT_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
GRANT_ARN = "arn:aws:kms:us-west-2:111122223333:key/grant"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint(actor: str = SUB, *, exp_in: int = 3600, key=_GRANT_KEY, payload_override=None) -> str:
    header = _b64(json.dumps({"alg": "KMS_ECDSA_SHA_256", "typ": "delegation-grant", "v": 1}).encode())
    now = int(time.time())
    payload = payload_override or {"actor_id": actor, "plan_id": "dispatch-x", "iat": now, "exp": now + exp_in}
    body = _b64(json.dumps(payload).encode())
    sig = key.sign(f"{header}.{body}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return f"{header}.{body}.{_b64(sig)}"


class GrantKms:
    def __init__(self):
        self.calls = 0

    def verify(self, *, KeyId, Message, MessageType, Signature, SigningAlgorithm):
        self.calls += 1
        assert (KeyId, MessageType, SigningAlgorithm) == (GRANT_ARN, "RAW", "ECDSA_SHA_256")
        try:
            _GRANT_KEY.public_key().verify(Signature, Message, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            return {"SignatureValid": False}
        return {"SignatureValid": True}


@pytest.fixture
def grant_kms():
    return GrantKms()


@pytest.fixture
def handler(table, kms, metrics, grant_kms):
    verify = delegation.make_grant_verifier(grant_kms, GRANT_ARN)
    return index.make_delegated_issue_handler(verify, table, tokens.KmsSigner(kms, SIGN_KID), ISS, AUD, 3600,
                                              emit_metric=lambda n, s: metrics.append((n, s)))


def test_member_with_a_valid_grant_gets_the_same_project_token(handler, kms):
    resp = handler({"grant": mint(), "projectId": PID})
    assert resp["status"] == "ok"
    claims = tokens.verify(resp["token"], tokens.KmsVerifier(kms, [SIGN_KID]), ISS, AUD, time.time())
    assert claims["sub"] == SUB and claims["project_id"] == PID


def test_actor_comes_only_from_the_grant(handler, kms):
    # A member's id in the event cannot stand in for a non-member's grant.
    resp = handler({"grant": mint(OTHER_SUB), "projectId": PID, "actorId": SUB, "sub": SUB, "userToken": "x"})
    assert resp == {"status": "not_found"} and kms.sign_calls == []


@pytest.mark.parametrize("grant", [
    None, "", "a.b", "a.b.c",
    mint(exp_in=-1),                                   # expired
    mint(key=_OTHER_KEY),                              # signed by another key
    mint()[:-6] + "AAAAAA",                            # tampered signature
])
def test_bad_grants_are_unauthenticated_and_nothing_is_read_or_signed(handler, kms, grant):
    assert handler({"grant": grant, "projectId": PID}) == {"status": "unauthenticated"}
    assert kms.sign_calls == []


def test_tampered_payload_is_rejected(handler):
    good = mint()
    h, _p, s = good.split(".")
    forged = _b64(json.dumps({"actor_id": OTHER_SUB, "plan_id": "x", "iat": 0, "exp": 9e12}).encode())
    assert handler({"grant": f"{h}.{forged}.{s}", "projectId": PID}) == {"status": "unauthenticated"}


@pytest.mark.parametrize("pid", ["", "nope", "p" + "b" * 32])
def test_non_member_project_is_not_found(handler, pid):
    assert handler({"grant": mint(), "projectId": pid}) == {"status": "not_found"}


def test_no_key_configured_fails_closed(table, kms, grant_kms):
    h = index.make_delegated_issue_handler(delegation.make_grant_verifier(grant_kms, ""), table,
                                           tokens.KmsSigner(kms, SIGN_KID), ISS, AUD, 3600)
    assert h({"grant": mint(), "projectId": PID}) == {"status": "unauthenticated"} and grant_kms.calls == 0


def test_sign_failure_is_error_with_metric(handler, kms, metrics):
    kms.fail = True
    assert handler({"grant": mint(), "projectId": PID}) == {"status": "error"}
    assert ("TokenIssuanceFailure", "sign") in metrics
