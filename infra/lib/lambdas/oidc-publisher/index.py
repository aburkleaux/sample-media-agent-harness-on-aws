"""OIDC discovery + JWKS publisher custom resource (media-harness-projects-access, design Component 5, D3;
task 8.3).

Writes two static objects into the private OIDC bucket that the Search_Gateway's CUSTOM_JWT authorizer
reads through a dedicated CloudFront distribution:

- ``/.well-known/openid-configuration`` — the discovery document, ``issuer = https://<domain>``.
- ``/.well-known/jwks.json``           — one RSA JWK per published signing key.

For every published signing key the handler calls ``kms:GetPublicKey`` (NEVER ``kms:Sign`` — this
resource holds no signing power, Req 4.5), reads the DER ``SubjectPublicKeyInfo``, and converts it to a
JWK (``kty``, ``n``, ``e``, ``kid``, ``alg: RS256``, ``use: sig``). The ``kid`` is the KMS key id, which is
exactly what ``tokens.KmsSigner`` stamps into the token header and what ``tokens.verify`` looks up in
``PUBLISHED_KEY_IDS`` — so the JWKS a gateway fetches and the key a refresh verifies against are the same
material.

Objects are written with ``Cache-Control: max-age=300`` (the 300 s staleness bound of D3), and both paths
are invalidated on every Update so a key rotation (D10) is visible within the TTL.

The DER is parsed with a tiny ASN.1 reader rather than ``cryptography`` so this function needs no
third-party dependency: boto3 and the standard library only.

ResourceProperties:
    BucketName      the private OIDC S3 bucket
    IssuerUrl       ``https://<cloudfront-domain>`` (no trailing slash)
    KeyIds          JSON array of KMS key ids (the published keys); each becomes one JWK, ``kid`` = key id
    DistributionId  the OIDC CloudFront distribution id (for invalidation); optional
"""

from __future__ import annotations

import base64
import json
import logging
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WELL_KNOWN_CONFIG = ".well-known/openid-configuration"
WELL_KNOWN_JWKS = ".well-known/jwks.json"


# --------------------------------------------------------------------------------------------------
# Minimal DER / ASN.1 reader — just enough to pull (n, e) out of an RSA SubjectPublicKeyInfo.
# --------------------------------------------------------------------------------------------------


def _read_len(data: bytes, i: int) -> tuple[int, int]:
    """Return (length, next_index) for the DER length octet(s) starting at ``i``."""
    first = data[i]
    i += 1
    if first < 0x80:
        return first, i
    num = first & 0x7F
    length = int.from_bytes(data[i : i + num], "big")
    return length, i + num


def _read_tlv(data: bytes, i: int, expected_tag: int) -> tuple[bytes, int]:
    """Read one DER TLV with the expected tag; return (value_bytes, next_index)."""
    tag = data[i]
    if tag != expected_tag:
        raise ValueError(f"DER: expected tag 0x{expected_tag:02x}, got 0x{tag:02x} at offset {i}")
    length, i = _read_len(data, i + 1)
    return data[i : i + length], i + length


def rsa_public_numbers_from_der(der: bytes) -> tuple[int, int]:
    """Extract (modulus, publicExponent) from a DER SubjectPublicKeyInfo for an RSA key.

    SubjectPublicKeyInfo ::= SEQUENCE { algorithm AlgorithmIdentifier, subjectPublicKey BIT STRING }
    subjectPublicKey (RSA) ::= BIT STRING wrapping SEQUENCE { modulus INTEGER, publicExponent INTEGER }
    """
    spki, _ = _read_tlv(der, 0, 0x30)  # outer SEQUENCE
    # algorithm AlgorithmIdentifier (SEQUENCE) — skip it.
    _, after_alg = _read_tlv(spki, 0, 0x30)
    # subjectPublicKey BIT STRING.
    bitstring, _ = _read_tlv(spki, after_alg, 0x03)
    if not bitstring or bitstring[0] != 0x00:
        raise ValueError("DER: unexpected BIT STRING padding in SubjectPublicKeyInfo")
    rsa_seq, _ = _read_tlv(bitstring[1:], 0, 0x30)  # RSAPublicKey SEQUENCE
    modulus_bytes, after_mod = _read_tlv(rsa_seq, 0, 0x02)  # INTEGER modulus
    exponent_bytes, _ = _read_tlv(rsa_seq, after_mod, 0x02)  # INTEGER publicExponent
    modulus = int.from_bytes(modulus_bytes, "big")
    exponent = int.from_bytes(exponent_bytes, "big")
    return modulus, exponent


def _b64url_uint(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def jwk_from_der(der: bytes, kid: str) -> dict:
    n, e = rsa_public_numbers_from_der(der)
    return {
        "kty": "RSA",
        "n": _b64url_uint(n),
        "e": _b64url_uint(e),
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
    }


# --------------------------------------------------------------------------------------------------
# Document builders
# --------------------------------------------------------------------------------------------------


def discovery_document(issuer_url: str) -> dict:
    issuer = issuer_url.rstrip("/")
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/{WELL_KNOWN_JWKS}",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def build_jwks(kms_client, key_ids: list[str]) -> dict:
    keys = []
    for kid in key_ids:
        resp = kms_client.get_public_key(KeyId=kid)
        keys.append(jwk_from_der(resp["PublicKey"], kid))
    return {"keys": keys}


# --------------------------------------------------------------------------------------------------
# Custom-resource handler
# --------------------------------------------------------------------------------------------------


def _publish(props: dict) -> None:
    bucket = props["BucketName"]
    issuer_url = props["IssuerUrl"]
    key_ids = json.loads(props.get("KeyIds", "[]"))
    if not key_ids:
        raise ValueError("OidcPublisher: KeyIds is empty; at least one signing key must be published")

    s3 = boto3.client("s3")
    kms = boto3.client("kms")

    jwks = build_jwks(kms, key_ids)
    discovery = discovery_document(issuer_url)

    for key, body in ((WELL_KNOWN_JWKS, jwks), (WELL_KNOWN_CONFIG, discovery)):
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(body).encode("utf-8"),
            ContentType="application/json",
            CacheControl="max-age=300",
        )
        logger.info("Published s3://%s/%s", bucket, key)

    distribution_id = (props.get("DistributionId") or "").strip()
    if distribution_id:
        cf = boto3.client("cloudfront")
        cf.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 2, "Items": [f"/{WELL_KNOWN_CONFIG}", f"/{WELL_KNOWN_JWKS}"]},
                "CallerReference": f"oidc-{int(time.time() * 1000)}",
            },
        )
        logger.info("Invalidated OIDC paths on distribution %s", distribution_id)


def handler(event, context):
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {})
    logger.info("OidcPublisher %s for bucket %s", request_type, props.get("BucketName"))
    try:
        if request_type in ("Create", "Update"):
            _publish(props)
        # Delete: leave the objects; the bucket is removed with the stack. Nothing to sign or undo.
        return {"PhysicalResourceId": f"oidc-publisher-{props.get('BucketName')}"}
    except Exception as exc:  # noqa: BLE001 — surface the failure to CloudFormation
        logger.error("OidcPublisher %s failed: %s", request_type, exc, exc_info=True)
        raise
