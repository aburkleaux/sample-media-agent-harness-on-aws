"""Unit tests for the OIDC publisher (media-harness-projects-access task 8.4).

Run: `uv run --with cryptography --with pytest pytest infra/lib/lambdas/oidc-publisher/test_index.py`.
Checks the DER->JWK conversion against `cryptography`, the discovery-document fields, and that no KMS
`Sign` is ever called.
"""

import base64

import pytest

import index

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402


def _b64url_uint(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()


def test_der_to_jwk_matches_cryptography():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    jwk = index.jwk_from_der(der, kid="my-key-id")

    numbers = key.public_key().public_numbers()
    assert jwk["kty"] == "RSA"
    assert jwk["alg"] == "RS256"
    assert jwk["use"] == "sig"
    assert jwk["kid"] == "my-key-id"
    assert jwk["n"] == _b64url_uint(numbers.n)
    assert jwk["e"] == _b64url_uint(numbers.e)


def test_discovery_document_fields():
    doc = index.discovery_document("https://d123.cloudfront.net/")
    assert doc["issuer"] == "https://d123.cloudfront.net"
    assert doc["jwks_uri"] == "https://d123.cloudfront.net/.well-known/jwks.json"
    assert doc["response_types_supported"] == ["id_token"]
    assert doc["subject_types_supported"] == ["public"]
    assert doc["id_token_signing_alg_values_supported"] == ["RS256"]


def test_build_jwks_calls_get_public_key_never_sign():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    class FakeKms:
        def __init__(self):
            self.get_calls = []

        def get_public_key(self, KeyId):  # noqa: N803 (boto3 kwarg casing)
            self.get_calls.append(KeyId)
            return {"PublicKey": der}

        def sign(self, **kwargs):
            raise AssertionError("OidcPublisher must never call kms:Sign")

    kms = FakeKms()
    jwks = index.build_jwks(kms, ["k1", "k2"])
    assert kms.get_calls == ["k1", "k2"]
    assert [k["kid"] for k in jwks["keys"]] == ["k1", "k2"]
