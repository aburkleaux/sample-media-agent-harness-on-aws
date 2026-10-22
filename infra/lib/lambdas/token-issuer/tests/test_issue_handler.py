"""Unit tests for first issuance (task 3.3): ``make_issue_handler`` and the Lambda ``issue_handler``.

Real pieces wherever possible: a moto DynamoDB table read through ``access.check_access``, a fake KMS
backed by a local RSA key (moto's KMS re-hashes ``MessageType=DIGEST`` input, so its signatures do not
verify), and a local RSA JWKS standing in for the Cognito pool. Issued tokens are checked with
``tokens.verify`` against ``KmsVerifier``, the same path Token_Refresh uses.
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

import access
import cognito
import index
import tokens
from cognito import CognitoJwks, CognitoUserTokenVerifier
from dev_login import DEFAULT_DEV_JWT_SECRET, DevLoginUserTokenVerifier

# Minting Dev Login tokens with the harness's 29-byte default secret is expected here.
pytestmark = pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects-test"
POOL = "us-west-2_AbCdEf123"
CLIENT = "labuiclient123"
COGNITO_ISS = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
POOL_KID = "pool-kid-1"
SIGN_KID = "1234abcd-12ab-34cd-56ef-1234567890ab"
ISS = "https://d111.cloudfront.net"
AUD = "mediaworks-harness-search-sandbox"
PID = "p" + "a" * 32
SUB = "8f7c1e2a-1111-2222-3333-444455556666"
OTHER_SUB = "11111111-2222-3333-4444-555555555555"

_POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_SIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKms:
    def __init__(self) -> None:
        self.sign_calls: list[dict] = []
        self.fail = False

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        assert (MessageType, SigningAlgorithm) == ("DIGEST", "RSASSA_PKCS1_V1_5_SHA_256")
        self.sign_calls.append({"KeyId": KeyId})
        if self.fail:
            raise RuntimeError("KMS throttled")
        return {"Signature": _SIGN_KEY.sign(Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))}

    def verify(self, *, KeyId, Message, MessageType, Signature, SigningAlgorithm):
        assert (MessageType, SigningAlgorithm) == ("DIGEST", "RSASSA_PKCS1_V1_5_SHA_256")
        try:
            _SIGN_KEY.public_key().verify(
                Signature, Message, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
        except InvalidSignature:
            return {"SignatureValid": False}
        return {"SignatureValid": True}

    def get_public_key(self, *, KeyId):
        der = _SIGN_KEY.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"KeyId": KeyId, "PublicKey": der, "SigningAlgorithms": ["RSASSA_PKCS1_V1_5_SHA_256"]}


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


def _jwks_doc(url: str) -> dict:
    d = jwt.algorithms.RSAAlgorithm.to_jwk(_POOL_KEY.public_key(), as_dict=True)
    return {"keys": [{**d, "kid": POOL_KID, "alg": "RS256", "use": "sig"}]}


def user_token(sub: str = SUB, **overrides) -> str:
    now = int(time.time())
    claims = {"sub": sub, "iss": COGNITO_ISS, "client_id": CLIENT, "token_use": "access", "iat": now,
              "exp": now + 3600}
    claims.update(overrides)
    return jwt.encode(claims, _POOL_KEY, algorithm="RS256", headers={"kid": POOL_KID})


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
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "projectId": PID, "status": "active"})
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}", "role": "owner"})
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
def handler(table, kms, metrics):
    verifier = CognitoUserTokenVerifier(
        region=REGION, user_pool_id=POOL, client_id=CLIENT,
        jwks=CognitoJwks(f"{COGNITO_ISS}/.well-known/jwks.json", fetch=_jwks_doc))
    return index.make_issue_handler(verifier, table, tokens.KmsSigner(kms, SIGN_KID), ISS, AUD, 3600,
                                    emit_metric=lambda name, stage: metrics.append((name, stage)))


def _verify(token: str, kms: FakeKms) -> dict:
    return tokens.verify(token, tokens.KmsVerifier(kms, [SIGN_KID]), ISS, AUD, time.time())


def test_ok_issues_a_token_for_the_verified_sub(handler, kms, reads):
    resp = handler({"userToken": user_token(), "projectId": PID})
    assert set(resp) == {"status", "token", "exp", "jti"} and resp["status"] == "ok"
    claims = _verify(resp["token"], kms)
    assert (claims["sub"], claims["project_id"], claims["jti"], claims["exp"]) == (SUB, PID, resp["jti"], resp["exp"])
    assert claims["exp"] - claims["iat"] == 3600
    assert len(reads) == 1 and reads[0]["RequestItems"][TABLE]["ConsistentRead"] is True


def test_event_identifiers_other_than_the_token_are_ignored(handler, kms, table):
    # OTHER_SUB has no membership; naming it (or SUB) in the event must not change whose token this is.
    resp = handler({"userToken": user_token(), "projectId": PID, "sub": OTHER_SUB, "actorId": OTHER_SUB,
                    "__actor_id": OTHER_SUB})
    assert _verify(resp["token"], kms)["sub"] == SUB
    resp = handler({"userToken": user_token(OTHER_SUB), "projectId": PID, "sub": SUB})
    assert resp == {"status": "not_found"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.delete_item(Key={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{SUB}"}),
        lambda t: t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "status": "deleting"}),
        lambda t: t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "status": "active", "tombstone": True}),
        lambda t: t.delete_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"}),
    ],
)
def test_not_found_without_access_and_nothing_signed(handler, table, kms, mutate):
    mutate(table)
    assert handler({"userToken": user_token(), "projectId": PID}) == {"status": "not_found"}
    assert kms.sign_calls == []


@pytest.mark.parametrize("pid", [None, "", "p" + "A" * 32, "P" + "a" * 32, "../" + PID])
def test_malformed_project_id_is_not_found_with_no_read(handler, kms, reads, pid):
    assert handler({"userToken": user_token(), "projectId": pid}) == {"status": "not_found"}
    assert reads == [] and kms.sign_calls == []


@pytest.mark.parametrize(
    "token",
    [None, "", "garbage", user_token(token_use="id"), user_token(client_id="other"),
     user_token(exp=int(time.time()) - 1), user_token(iss="https://cognito-idp.us-west-2.amazonaws.com/x")],
)
def test_unauthenticated_reads_and_signs_nothing(handler, kms, reads, metrics, token):
    assert handler({"userToken": token, "projectId": PID}) == {"status": "unauthenticated"}
    assert reads == [] and kms.sign_calls == [] and metrics == []


@pytest.mark.parametrize("event", [None, "x", [], {}])
def test_non_mapping_or_empty_event_is_unauthenticated(handler, event):
    assert handler(event) == {"status": "unauthenticated"}


def test_kms_failure_is_error_with_metric(handler, kms, metrics):
    kms.fail = True
    assert handler({"userToken": user_token(), "projectId": PID}) == {"status": "error"}
    assert metrics == [("TokenIssuanceFailure", "sign")]


def test_incomplete_access_check_is_error_not_not_found(handler, table, kms, metrics, monkeypatch):
    def unprocessed(**kwargs):
        return {"Responses": {TABLE: []}, "UnprocessedKeys": kwargs["RequestItems"]}

    monkeypatch.setattr(table.meta.client, "batch_get_item", unprocessed)
    assert handler({"userToken": user_token(), "projectId": PID}) == {"status": "error"}
    assert metrics == [("TokenIssuanceFailure", "access")] and kms.sign_calls == []


def test_unreachable_jwks_is_error_with_metric(table, kms, metrics):
    def down(url):
        raise OSError("unreachable")

    verifier = CognitoUserTokenVerifier(region=REGION, user_pool_id=POOL, client_id=CLIENT,
                                        jwks=CognitoJwks("https://x/.well-known/jwks.json", fetch=down))
    h = index.make_issue_handler(verifier, table, tokens.KmsSigner(kms, SIGN_KID), ISS, AUD, 3600,
                                 emit_metric=lambda n, s: metrics.append((n, s)))
    assert h({"userToken": user_token(), "projectId": PID}) == {"status": "error"}
    assert metrics == [("TokenIssuanceFailure", "jwks")]


def test_log_line_has_only_jti_project_id_sub_latency_and_no_token(handler, capsys):
    ut = user_token()
    resp = handler({"userToken": ut, "projectId": PID})
    out = capsys.readouterr().out
    assert ut not in out and resp["token"] not in out
    for part in (*ut.split("."), *resp["token"].split(".")):
        assert part not in out
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 1
    assert set(lines[0]) == {"jti", "project_id", "sub", "latency_ms"}
    assert (lines[0]["jti"], lines[0]["project_id"], lines[0]["sub"]) == (resp["jti"], PID, SUB)


def test_malformed_project_id_is_not_echoed_into_the_log(handler, capsys):
    ut = user_token()
    handler({"userToken": ut, "projectId": ut})  # a token pasted into the wrong field
    out = capsys.readouterr().out
    assert ut not in out and json.loads(out)["project_id"] is None


def test_emf_metric_line_shape(capsys):
    index.emit_failure_metric("TokenIssuanceFailure", "sign")
    line = json.loads(capsys.readouterr().out)
    cw = line["_aws"]["CloudWatchMetrics"][0]
    assert cw["Metrics"] == [{"Name": "TokenIssuanceFailure", "Unit": "Count"}]
    assert line["TokenIssuanceFailure"] == 1 and line["stage"] == "sign"


def test_dev_wrapper_reuses_the_handler_with_dev_pieces(table):
    """A local dev wrapper: Dev Login verifier + LocalPemSigner, same handler logic."""
    now = int(time.time())
    dev_tok = jwt.encode({"sub": SUB, "token_use": "access", "iat": now, "exp": now + 60},
                         DEFAULT_DEV_JWT_SECRET, algorithm="HS256")
    signer = LocalPemSigner(_SIGN_KEY, "local-dev-kid")
    h = index.make_issue_handler(DevLoginUserTokenVerifier(secret=DEFAULT_DEV_JWT_SECRET), table, signer,
                                 ISS, AUD, 600)
    resp = h({"userToken": dev_tok, "projectId": PID})
    claims = tokens.verify(resp["token"], LocalRsaVerifier({"local-dev-kid": signer.public_key()}),
                           ISS, AUD, time.time())
    assert claims["sub"] == SUB and claims["exp"] - claims["iat"] == 600


def test_make_issue_handler_rejects_bad_config(table, kms):
    v = DevLoginUserTokenVerifier(secret="s")
    s = tokens.KmsSigner(kms, SIGN_KID)
    for args in ((ISS, AUD, 59), (ISS, AUD, 3601), (ISS, AUD, "3600"), ("", AUD, 3600), (ISS, "", 3600)):
        with pytest.raises(ValueError):
            index.make_issue_handler(v, table, s, *args)


# ------------------------------------------------------------------------------------------------
# Lambda path: production pieces from env
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def lambda_env(table, kms, monkeypatch):
    for name, value in {
        "AWS_REGION": REGION, "PROJECTS_TABLE": TABLE, "SIGNING_KEY_ID": SIGN_KID, "TOKEN_ISSUER_URL": ISS,
        "SEARCH_GATEWAY_AUDIENCE": AUD, "USER_POOL_ID": POOL, "USER_POOL_CLIENT_ID": CLIENT,
        "TOKEN_LIFETIME_SECONDS": "900",
        # Dev flags set alongside must not change the Lambda's verifier.
        "DEV_BYPASS_AUTH": "true", "VITE_DEV_BYPASS_AUTH": "true", "VITE_DEV_JWT_SECRET": DEFAULT_DEV_JWT_SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(index, "_kms_client", lambda region: kms)
    monkeypatch.setattr(cognito, "fetch_jwks_json", _jwks_doc)
    index._production_issue_handler.cache_clear()
    tokens.configured_lifetime_seconds.cache_clear()
    yield
    index._production_issue_handler.cache_clear()
    tokens.configured_lifetime_seconds.cache_clear()


def test_lambda_issue_handler_end_to_end_from_env(lambda_env, kms):
    resp = index.issue_handler({"userToken": user_token(), "projectId": PID}, None)
    assert resp["status"] == "ok"
    claims = _verify(resp["token"], kms)
    assert claims["sub"] == SUB and claims["exp"] - claims["iat"] == 900
    assert index._production_issue_handler() is index._production_issue_handler()  # built once


def test_lambda_path_never_accepts_a_dev_login_token(lambda_env, kms):
    now = int(time.time())
    dev_tok = jwt.encode({"sub": SUB, "token_use": "access", "iss": COGNITO_ISS, "client_id": CLIENT,
                          "iat": now, "exp": now + 60}, DEFAULT_DEV_JWT_SECRET, algorithm="HS256",
                         headers={"kid": POOL_KID})
    assert index.issue_handler({"userToken": dev_tok, "projectId": PID}) == {"status": "unauthenticated"}
    assert kms.sign_calls == []


def test_index_does_not_import_the_dev_verifier():
    import ast

    tree = ast.parse(open(index.__file__, encoding="utf-8").read())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "dev_login" not in imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not any("DevLogin" in name for name in names)


def test_missing_config_is_error_with_metric(lambda_env, monkeypatch, capsys):
    monkeypatch.delenv("SIGNING_KEY_ID")
    assert index.issue_handler({"userToken": user_token(), "projectId": PID}) == {"status": "error"}
    line = json.loads(capsys.readouterr().out)
    assert line["TokenIssuanceFailure"] == 1 and line["stage"] == "config"


def test_hashlib_digest_is_what_kms_signs(handler, kms, monkeypatch):
    """The DIGEST sent to KMS is SHA-256 of the token's signing input."""
    seen: list[bytes] = []
    real = kms.sign

    def spy(**kw):
        seen.append(kw["Message"])
        return real(**kw)

    monkeypatch.setattr(kms, "sign", spy)
    resp = handler({"userToken": user_token(), "projectId": PID})
    signing_input = resp["token"].rsplit(".", 1)[0]
    assert seen == [hashlib.sha256(signing_input.encode()).digest()]
