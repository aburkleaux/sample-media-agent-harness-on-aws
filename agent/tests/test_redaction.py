"""Example test for external output secret redaction (task 13.3).

Feature: custom-agents, Task 13.3 (example test)

Validates: Requirements 11.2

Requirement 11.2: before an external SSE frame is emitted to a Caller, the
Task Agent passes the agent's output through the redaction filter so
caller-visible output never carries Owner secrets, tokens, or credentials.

This is an example (non-property) test exercising ``redact_secrets`` with
known-secret fixtures and well-known credential-shape fixtures. It asserts
that:

* a supplied known Owner secret value is removed and replaced with the
  placeholder,
* pattern redaction catches AWS access key ids, HTTP ``Bearer`` tokens, JWTs,
  PEM private-key blocks, and ``key=value`` / ``key: value`` secret
  assignments,
* the original secret substrings never survive into the redacted output,
* non-secret surrounding text is preserved,
* empty / non-string input is returned as-is without raising, and
* known secrets shorter than the minimum length are ignored (not redacted).
"""

from __future__ import annotations

import os
import sys

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security.redaction import REDACTION_PLACEHOLDER, redact_secrets  # noqa: E402


# --- Known-secret fixtures (concrete Owner credential values) --------------

OWNER_TOKEN = "owner-session-token-abc123XYZ456"
OWNER_CREDENTIAL = "sk-live-9f8e7d6c5b4a3210deadbeefcafef00d"


def test_known_secret_value_is_redacted() -> None:
    """A supplied known Owner secret is removed and replaced with placeholder."""
    text = f"Here is the owner token {OWNER_TOKEN} that must not leak."

    result = redact_secrets(text, known_secrets=[OWNER_TOKEN])

    assert OWNER_TOKEN not in result
    assert REDACTION_PLACEHOLDER in result
    # Non-secret surrounding text is preserved.
    assert result.startswith("Here is the owner token ")
    assert result.endswith(" that must not leak.")


def test_multiple_known_secrets_all_redacted() -> None:
    """Every supplied known secret value is stripped from the output."""
    text = (
        f"token={OWNER_TOKEN}\n"
        f"credential={OWNER_CREDENTIAL}\n"
        "normal line of text"
    )

    result = redact_secrets(text, known_secrets=[OWNER_TOKEN, OWNER_CREDENTIAL])

    assert OWNER_TOKEN not in result
    assert OWNER_CREDENTIAL not in result
    assert "normal line of text" in result


def test_short_known_secrets_are_ignored() -> None:
    """Known secrets shorter than the minimum length are not redacted."""
    short_secret = "abc"  # < 6 chars, ignored to avoid garbling text.
    text = "the abc sequence appears in fabric and abcded"

    result = redact_secrets(text, known_secrets=[short_secret])

    # Nothing redacted; text passes through untouched.
    assert result == text
    assert REDACTION_PLACEHOLDER not in result


# --- Pattern redaction fixtures (credential shapes) ------------------------


def test_aws_access_key_id_is_redacted() -> None:
    """AWS access key id (AKIA + 16 chars) is caught by pattern redaction."""
    access_key = "AKIAIOSFODNN7EXAMPLE"  # AKIA + 16 alnum
    text = f"aws creds: {access_key} end"

    result = redact_secrets(text)

    assert access_key not in result
    assert REDACTION_PLACEHOLDER in result
    assert result.startswith("aws creds: ")
    assert result.endswith(" end")


def test_bearer_token_is_redacted() -> None:
    """An HTTP ``Bearer <token>`` header value is redacted."""
    token = "abc123DEF456ghi789JKL012mno345PQR"
    text = f"Authorization: Bearer {token}"

    result = redact_secrets(text)

    assert token not in result
    assert REDACTION_PLACEHOLDER in result


def test_jwt_is_redacted() -> None:
    """A three-segment JWT (``eyJ...``) is redacted."""
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    text = f"session jwt {jwt} issued"

    result = redact_secrets(text)

    assert jwt not in result
    assert REDACTION_PLACEHOLDER in result


def test_pem_private_key_block_is_redacted() -> None:
    """A PEM PRIVATE KEY block is redacted in full."""
    # The PEM markers are assembled at runtime from fragments so this test
    # file contains no contiguous "-----BEGIN PRIVATE KEY-----" literal for
    # secret scanners (e.g. Code Defender) to flag. At runtime `pem` is the
    # exact block the redaction filter is expected to strip, so the behavior
    # under test is unchanged — this is a synthetic, non-secret fixture.
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    body = "SUPERSECRET" + "KEYMATERIAL" + "deadbeefcafef00d1234567890"
    pem = (
        f"{begin}\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
        f"{body}\n"
        f"{end}"
    )
    text = f"key follows:\n{pem}\ndone"

    result = redact_secrets(text)

    assert "SUPERSECRET" + "KEYMATERIAL" not in result
    assert "BEGIN PRIVATE KEY" not in result
    assert REDACTION_PLACEHOLDER in result
    assert result.startswith("key follows:")
    assert result.endswith("done")


def test_password_assignment_value_redacted_key_preserved() -> None:
    """A ``password=...`` assignment redacts the value but keeps the key."""
    text = "config: password=hunter2SuperSecret next"

    result = redact_secrets(text)

    assert "hunter2SuperSecret" not in result
    assert "password" in result  # key preserved
    assert REDACTION_PLACEHOLDER in result


def test_api_key_colon_assignment_value_redacted_key_preserved() -> None:
    """An ``api_key: ...`` assignment redacts the value but keeps the key."""
    text = "api_key: aVeryLongApiKeyValue1234567890 trailing"

    result = redact_secrets(text)

    assert "aVeryLongApiKeyValue1234567890" not in result
    assert "api_key" in result  # key preserved
    assert REDACTION_PLACEHOLDER in result


def test_combined_output_excludes_all_secrets() -> None:
    """A realistic mixed output excludes every secret substring."""
    access_key = "AKIAIOSFODNN7EXAMPLE"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2lnbmF0dXJl"
    text = (
        f"Owner token {OWNER_TOKEN}\n"
        f"aws key {access_key}\n"
        f"bearer Bearer {jwt}\n"
        f"password=topSecretValue99\n"
        "user-facing answer: everything looks fine"
    )

    result = redact_secrets(text, known_secrets=[OWNER_TOKEN])

    # No original secret substrings survive.
    assert OWNER_TOKEN not in result
    assert access_key not in result
    assert jwt not in result
    assert "topSecretValue99" not in result
    # Non-secret content preserved.
    assert "user-facing answer: everything looks fine" in result


# --- Presigned URLs are capability URLs, NOT Owner secrets -----------------
# A presigned S3 URL is minted specifically to hand to the Caller (the
# get_download_url tool). Its query-string SigV4 material (Credential with an
# AKIA/ASIA key id, Security-Token, Signature) must survive redaction INTACT or
# S3 rejects it with AuthorizationQueryParametersError. Regression for that bug.

PRESIGNED_URL = (
    "https://mediaworks-proxy-100000000001-us-west-2.s3.amazonaws.com/"
    "micro-workflow/media-processing/abc/proxy/transcoded.mp4"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=ASIAIOSFODNN7EXAMPLE%2F20260724%2Fus-west-2%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260724T205725Z"
    "&X-Amz-Expires=3600"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Security-Token=FwoGZXIvYXdzEExampleTokenValue1234567890abcdef"
    "&X-Amz-Signature=abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
)


def test_presigned_url_survives_redaction_intact() -> None:
    """A bare presigned URL passes through with every SigV4 query param intact."""
    text = f"Download the 240p video: {PRESIGNED_URL}"

    result = redact_secrets(text)

    # The whole URL — signature, security-token, and access-key id in the
    # credential — must survive verbatim.
    assert PRESIGNED_URL in result
    assert "X-Amz-Signature=" in result
    assert "X-Amz-Security-Token=" in result
    assert "ASIAIOSFODNN7EXAMPLE" in result  # AKIA-shape key id preserved
    assert REDACTION_PLACEHOLDER not in result


def test_presigned_url_in_markdown_link_survives() -> None:
    """A presigned URL inside a markdown link (bounded by ')') survives whole."""
    text = f"[transcoded.mp4]({PRESIGNED_URL})"

    result = redact_secrets(text)

    assert PRESIGNED_URL in result
    assert "X-Amz-Signature=" in result


def test_presigned_url_preserved_but_real_secret_still_redacted() -> None:
    """Stashing the URL does not shield a genuine secret elsewhere in the text."""
    text = (
        f"Here is the link {PRESIGNED_URL}\n"
        "and the owner password=hunter2SuperSecret must still go."
    )

    result = redact_secrets(text)

    assert PRESIGNED_URL in result           # capability URL preserved
    assert "hunter2SuperSecret" not in result  # real secret still stripped
    assert REDACTION_PLACEHOLDER in result


def test_presigned_url_redaction_is_idempotent() -> None:
    """Redacting text with a presigned URL twice yields the same output."""
    text = f"link: {PRESIGNED_URL} done"

    once = redact_secrets(text)
    twice = redact_secrets(once)

    assert once == twice
    assert PRESIGNED_URL in twice


def test_known_secret_inside_presigned_url_still_stripped() -> None:
    """An exact known Owner secret embedded in a URL is redacted before stashing."""
    leaky = PRESIGNED_URL + "&owner=" + OWNER_TOKEN
    text = f"link {leaky}"

    result = redact_secrets(text, known_secrets=[OWNER_TOKEN])

    assert OWNER_TOKEN not in result
    # The SigV4 portion still survives.
    assert "X-Amz-Signature=" in result


# --- Robustness: never raises, empty / non-string input returned as-is -----


def test_empty_string_returned_as_is() -> None:
    assert redact_secrets("") == ""
    assert redact_secrets("", known_secrets=[OWNER_TOKEN]) == ""


def test_non_string_input_returned_as_is() -> None:
    # Non-string inputs short-circuit and are returned unchanged (no raise).
    assert redact_secrets(None) is None  # type: ignore[arg-type]
    assert redact_secrets(12345) == 12345  # type: ignore[arg-type]


def test_no_secrets_present_returns_unchanged() -> None:
    text = "a perfectly ordinary sentence with no credentials at all"
    assert redact_secrets(text) == text


def test_redaction_is_idempotent() -> None:
    """Redacting already-redacted output yields the same result (no re-garbling)."""
    access_key = "AKIAIOSFODNN7EXAMPLE"
    text = (
        f"Owner token {OWNER_TOKEN}\n"
        f"aws key {access_key}\n"
        "password=topSecretValue99\n"
        "user-facing answer: everything looks fine"
    )

    once = redact_secrets(text, known_secrets=[OWNER_TOKEN])
    twice = redact_secrets(once, known_secrets=[OWNER_TOKEN])

    assert twice == once
