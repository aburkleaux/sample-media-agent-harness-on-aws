"""Unit tests for Token_Refresh (task 3.4): ``make_refresh_handler`` and the Lambda ``refresh_handler``.

Real pieces wherever possible: a moto DynamoDB table read through ``access.check_access`` and a fake KMS
backed by local RSA keys (moto's KMS re-hashes ``MessageType=DIGEST`` input, so its signatures do not
verify). Presented tokens are minted with ``tokens.issue`` through the same ``KmsSigner`` the handler uses.
"""
from __future__ import annotations

import hashlib
import json
import time

import boto3
import jwt
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from moto import mock_aws

import index
import tokens
from tokens import REFRESH_GRACE_S

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects-test"
CURRENT_KID = "1234abcd-12ab-34cd-56ef-1234567890ab"
RETIRED_KID = "9999abcd-12ab-34cd-56ef-1234567890ab"
UNPUBLISHED_KID = "0000abcd-12ab-34cd-56ef-1234567890ab"
ISS = "https://d111.cloudfront.net"
AUD = "mediaworks-harness-search-sandbox"
PID = "p" + "a" * 32
OTHER_PID = "p" + "b" * 32
SUB = "8f7c1e2a-1111-2222-3333-444455556666"
OTHER_SUB = "11111111-2222-3333-4444-555555555555"
LIFETIME = 3600
T0 = 1_800_000_000  # issue time of presented tokens

_KEYS = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)
         for kid in (CURRENT_KID, RETIRED_KID, UNPUBLISHED_KID)}
_FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKms:
    def __init__(self) -> None:
        self.sign_calls: list[str] = []
        self.verify_calls: list[str] = []
        self.fail_sign = False
        self.fail_verify = False

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        assert (MessageType, SigningAlgorithm) == ("DIGEST", "RSASSA_PKCS1_V1_5_SHA_256")
        self.sign_calls.append(KeyId)
        if self.fail_sign:
            raise RuntimeError("KMS throttled")
        return {"Signature": _KEYS[KeyId].sign(Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))}

    def verify(self, *, KeyId, Message, MessageType, Signature, SigningAlgorithm):
        assert (MessageType, SigningAlgorithm) == ("DIGEST", "RSASSA_PKCS1_V1_5_SHA_256")
        self.verify_calls.append(KeyId)
        if self.fail_verify:
            raise RuntimeError("KMS unavailable")
        try:
            _KEYS[KeyId].public_key().verify(
                Signature, Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
        except InvalidSignature:
            return {"SignatureValid": False}
        return {"SignatureValid": True}


class MintKms(FakeKms):
    """Mints presented tokens without touching the handler's KMS call log."""


class LocalPemSigner:
    """Test-only RSA signer for the dev-wrapper path (same token structure KMS produces)."""

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
    """Test-only ``tokens.Verifier`` over local RSA public keys."""

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


def presented(*, sub: str = SUB, project_id: str = PID, kid: str = CURRENT_KID, iss: str = ISS,
              aud: str = AUD, iat: int = T0, lifetime: int = LIFETIME) -> tokens.IssuedToken:
    return tokens.issue(tokens.KmsSigner(MintKms(), kid), iss=iss, aud=aud, sub=sub, project_id=project_id,
                        now=iat, lifetime=lifetime)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        for pid in (PID, OTHER_PID):
            t.put_item(Item={"PK": f"PROJECT#{pid}", "SK": "META", "projectId": pid, "status": "active"})
            t.put_item(Item={"PK": f"PROJECT#{pid}", "SK": f"MEMBER#{SUB}", "role": "owner"})
        t.put_item(Item={"PK": f"PROJECT#{OTHER_PID}", "SK": f"MEMBER#{OTHER_SUB}", "role": "member"})
        yield t


@pytest.fixture
def reads(table, monkeypatch):
    recorded: list[dict] = []
    real = table.meta.client.batch_get_item

    def recording(**kwargs):
        recorded.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(table.meta.client, "batch_get_item", recording)
    return recorded


@pytest.fixture
def kms() -> FakeKms:
    return FakeKms()


@pytest.fixture
def metrics() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def clock() -> Clock:
    return Clock(T0 + 600.0)


@pytest.fixture
def handler(table, kms, metrics, clock):
    return index.make_refresh_handler(
        tokens.KmsVerifier(kms, [CURRENT_KID, RETIRED_KID]), table, tokens.KmsSigner(kms, CURRENT_KID),
        ISS, AUD, LIFETIME, clock=clock, emit_metric=lambda name, stage: metrics.append((name, stage)))


def _claims(token: str, now: float) -> dict:
    return tokens.verify(token, LocalRsaVerifier({CURRENT_KID: _KEYS[CURRENT_KID].public_key()}),
                         ISS, AUD, now)


def test_ok_reissues_same_sub_and_project_with_new_jti_and_iat_now(handler, kms, clock, reads):
    prev = presented()
    resp = handler({"token": prev.token})
    assert set(resp) == {"status", "token", "exp", "jti"} and resp["status"] == "ok"
    claims = _claims(resp["token"], clock.now)
    assert (claims["sub"], claims["project_id"]) == (SUB, PID)
    assert claims["jti"] == resp["jti"] != prev.jti
    assert claims["iat"] == int(clock.now) and claims["exp"] == resp["exp"] == int(clock.now) + LIFETIME
    assert jwt.get_unverified_header(resp["token"])["kid"] == CURRENT_KID
    assert kms.sign_calls == [CURRENT_KID]
    assert len(reads) == 1 and reads[0]["RequestItems"][TABLE]["ConsistentRead"] is True


def test_only_the_token_key_is_read(handler, clock):
    # OTHER_SUB is a member of OTHER_PID; naming either must not change whose token or which Project.
    resp = handler({"token": presented().token, "sub": OTHER_SUB, "actorId": OTHER_SUB,
                    "projectId": OTHER_PID, "project_id": OTHER_PID})
    claims = _claims(resp["token"], clock.now)
    assert (claims["sub"], claims["project_id"]) == (SUB, PID)


def test_grace_boundary_exactly_8h_past_exp_is_refreshed(handler, kms, clock, reads):
    prev = presented()
    clock.now = prev.exp + REFRESH_GRACE_S
    resp = handler({"token": prev.token})
    assert resp["status"] == "ok" and len(reads) == 1
    assert _claims(resp["token"], clock.now)["iat"] == prev.exp + REFRESH_GRACE_S


def test_grace_boundary_one_second_more_is_invalid_token(handler, kms, clock, reads, metrics):
    prev = presented()
    clock.now = prev.exp + REFRESH_GRACE_S + 1
    assert handler({"token": prev.token}) == {"status": "invalid_token"}
    assert kms.sign_calls == [] and reads == [] and metrics == []


@pytest.mark.parametrize("offset", [-1800, 0, 1, REFRESH_GRACE_S // 2])
def test_unexpired_and_expired_within_grace_are_treated_alike(handler, table, clock, offset):
    prev = presented()
    clock.now = prev.exp + offset
    assert handler({"token": prev.token})["status"] == "ok"
    table.delete_item(Key={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}"})
    assert handler({"token": prev.token}) == {"status": "access_ended"}


def test_token_whose_project_the_sub_was_removed_from_is_access_ended(handler, table, kms):
    prev = presented()
    assert handler({"token": prev.token})["status"] == "ok"
    kms.sign_calls.clear()
    table.delete_item(Key={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}"})
    assert handler({"token": prev.token}) == {"status": "access_ended"}
    assert kms.sign_calls == []
    # Membership in another Project does not carry over.
    assert handler({"token": prev.token, "projectId": OTHER_PID}) == {"status": "access_ended"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "status": "deleting"}),
        lambda t: t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "status": "active", "tombstone": True}),
        lambda t: t.delete_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"}),
    ],
)
def test_deleted_or_tombstoned_project_is_access_ended(handler, table, kms, mutate):
    mutate(table)
    assert handler({"token": presented().token}) == {"status": "access_ended"}
    assert kms.sign_calls == []


def _foreign_signed() -> str:
    """Correct header and claims, signed by a key that is not the published one for that kid."""
    signing_input = presented().token.rsplit(".", 1)[0]
    sig = _FOREIGN_KEY.sign(hashlib.sha256(signing_input.encode()).digest(), padding.PKCS1v15(),
                            utils.Prehashed(hashes.SHA256()))
    return f"{signing_input}.{tokens._b64url(sig)}"


def _cognito_shaped() -> str:
    return jwt.encode({"sub": SUB, "iss": "https://cognito-idp.us-west-2.amazonaws.com/x", "client_id": "c",
                       "token_use": "access", "iat": T0, "exp": T0 + 3600}, _FOREIGN_KEY, algorithm="RS256",
                      headers={"kid": "pool-kid-1"})


@pytest.mark.parametrize(
    "event",
    [
        None, "x", [], {}, {"token": None}, {"token": ""}, {"token": "garbage"}, {"token": "a.b.c"},
        {"token": presented().token[:-10]},
        {"token": _foreign_signed()},
        {"token": presented(kid=UNPUBLISHED_KID).token},
        {"token": presented(iss="https://other.example").token},
        {"token": presented(aud="mediaworks-harness-search-prod").token},
        {"token": _cognito_shaped()},
    ],
)
def test_invalid_token_reads_and_signs_nothing(handler, kms, reads, metrics, event):
    assert handler(event) == {"status": "invalid_token"}
    assert kms.sign_calls == [] and reads == [] and metrics == []


def test_retired_key_still_published_refreshes_onto_the_current_key(handler, kms, clock):
    resp = handler({"token": presented(kid=RETIRED_KID).token})
    assert resp["status"] == "ok"
    assert jwt.get_unverified_header(resp["token"])["kid"] == CURRENT_KID
    assert kms.sign_calls == [CURRENT_KID]


def test_verify_failure_is_error_not_invalid_token(handler, kms, metrics):
    kms.fail_verify = True
    assert handler({"token": presented().token}) == {"status": "error"}
    assert metrics == [("TokenRefreshFailure", "verify")] and kms.sign_calls == []


def test_incomplete_access_check_is_error_not_access_ended(handler, table, kms, metrics, monkeypatch):
    def unprocessed(**kwargs):
        return {"Responses": {TABLE: []}, "UnprocessedKeys": kwargs["RequestItems"]}

    monkeypatch.setattr(table.meta.client, "batch_get_item", unprocessed)
    assert handler({"token": presented().token}) == {"status": "error"}
    assert metrics == [("TokenRefreshFailure", "access")] and kms.sign_calls == []


def test_dynamodb_error_is_error_with_metric(table, kms, metrics, clock):
    missing = boto3.resource("dynamodb", region_name=REGION).Table("no-such-table")
    h = index.make_refresh_handler(tokens.KmsVerifier(kms, [CURRENT_KID]), missing,
                                   tokens.KmsSigner(kms, CURRENT_KID), ISS, AUD, LIFETIME, clock=clock,
                                   emit_metric=lambda n, s: metrics.append((n, s)))
    assert h({"token": presented().token}) == {"status": "error"}
    assert metrics == [("TokenRefreshFailure", "access")]


def test_kms_sign_failure_is_error_with_metric(handler, kms, metrics):
    kms.fail_sign = True
    assert handler({"token": presented().token}) == {"status": "error"}
    assert metrics == [("TokenRefreshFailure", "sign")]


def test_log_line_has_only_the_allowed_fields_and_no_token(handler, capsys):
    prev = presented()
    resp = handler({"token": prev.token})
    out = capsys.readouterr().out
    for part in (*prev.token.split("."), *resp["token"].split(".")):
        assert part not in out
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 1
    assert set(lines[0]) == {"prev_jti", "jti", "project_id", "sub", "outcome", "latency_ms"}
    assert (lines[0]["prev_jti"], lines[0]["jti"], lines[0]["project_id"], lines[0]["sub"], lines[0]["outcome"]) \
        == (prev.jti, resp["jti"], PID, SUB, "ok")


def test_refused_token_logs_no_unverified_claims(handler, capsys):
    foreign = _foreign_signed()
    handler({"token": foreign, "sub": OTHER_SUB})
    line = json.loads(capsys.readouterr().out)
    assert line["outcome"] == "invalid_token"
    assert (line["prev_jti"], line["jti"], line["project_id"], line["sub"]) == (None, None, None, None)


def test_dev_wrapper_reuses_the_handler_with_local_pieces(table):
    signer = LocalPemSigner(_KEYS[CURRENT_KID], "local-dev-kid")
    now = time.time()
    prev = tokens.issue(signer, iss=ISS, aud=AUD, sub=SUB, project_id=PID, now=now, lifetime=600)
    verifier = LocalRsaVerifier({"local-dev-kid": signer.public_key()})
    h = index.make_refresh_handler(verifier, table, signer, ISS, AUD, 600)
    resp = h({"token": prev.token})
    claims = tokens.verify(resp["token"], verifier, ISS, AUD, time.time())
    assert (claims["sub"], claims["project_id"]) == (SUB, PID) and claims["jti"] != prev.jti


def test_make_refresh_handler_rejects_bad_config(table, kms):
    keys = tokens.KmsVerifier(kms, [CURRENT_KID])
    s = tokens.KmsSigner(kms, CURRENT_KID)
    for args in ((ISS, AUD, 59), (ISS, AUD, 3601), (ISS, AUD, "3600"), ("", AUD, 3600), (ISS, "", 3600)):
        with pytest.raises(ValueError):
            index.make_refresh_handler(keys, table, s, *args)


# ------------------------------------------------------------------------------------------------
# Lambda path: production pieces from env
# ------------------------------------------------------------------------------------------------


def _clear_caches() -> None:
    index._production_refresh_handler.cache_clear()
    tokens.configured_lifetime_seconds.cache_clear()
    tokens.configured_published_key_ids.cache_clear()


@pytest.fixture
def lambda_env(table, kms, monkeypatch):
    for name, value in {
        "AWS_REGION": REGION, "PROJECTS_TABLE": TABLE, "SIGNING_KEY_ID": CURRENT_KID, "TOKEN_ISSUER_URL": ISS,
        "SEARCH_GATEWAY_AUDIENCE": AUD, "TOKEN_LIFETIME_SECONDS": "900",
        "PUBLISHED_KEY_IDS": f"{CURRENT_KID},{RETIRED_KID}",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(index, "_kms_client", lambda region: kms)
    _clear_caches()
    yield
    _clear_caches()


def test_lambda_refresh_handler_end_to_end_from_env(lambda_env, kms):
    now = time.time()
    for kid in (CURRENT_KID, RETIRED_KID, CURRENT_KID):
        prev = presented(kid=kid, iat=int(now) - 60)
        resp = index.refresh_handler({"token": prev.token}, None)
        assert resp["status"] == "ok"
        claims = _claims(resp["token"], time.time())
        assert claims["sub"] == SUB and claims["exp"] - claims["iat"] == 900
    # kms:Verify runs per presented token (no public-key cache); the handler is still built once.
    assert kms.verify_calls == [CURRENT_KID, RETIRED_KID, CURRENT_KID]
    assert index._production_refresh_handler() is index._production_refresh_handler()


def test_lambda_unpublished_kid_is_invalid_token_without_kms(lambda_env, kms):
    prev = presented(kid=UNPUBLISHED_KID, iat=int(time.time()) - 60)
    assert index.refresh_handler({"token": prev.token}) == {"status": "invalid_token"}
    assert kms.verify_calls == [] and kms.sign_calls == []


def test_lambda_missing_config_is_error_with_metric(lambda_env, monkeypatch, capsys):
    monkeypatch.delenv("PUBLISHED_KEY_IDS")
    assert index.refresh_handler({"token": presented().token}) == {"status": "error"}
    line = json.loads(capsys.readouterr().out)
    assert line["TokenRefreshFailure"] == 1 and line["stage"] == "config"
