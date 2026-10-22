"""Secret / credential redaction filter (task 13.1).

A single, pure, importable redaction function shared by two call sites in the
custom-agents design:

* **Req 11.2 — external output filtering.** Before an external SSE frame is
  emitted to a Caller, the Task Agent passes the agent's output through this
  filter so caller-visible output never carries Owner secrets, tokens, or
  credentials.
* **Req 18.2 — log/trace secret hygiene.** The log/trace writer runs the same
  filter so secrets never land in logs or traces.

The function is intentionally **pure** (no I/O, no globals, deterministic) so
it is trivially unit/example-testable (task 13.3) and safe to call on any hot
path. It never raises: malformed input is returned as-is (empty/non-string
inputs short-circuit).

Two complementary strategies are applied, in order:

1. **Known-secret redaction.** Exact substring replacement of caller-supplied
   known secret *values* (Owner credentials/tokens the runtime already holds).
   This is the authoritative path when the concrete secret values are known.
2. **Pattern redaction.** Best-effort regex redaction of well-known credential
   *shapes* (AWS keys, bearer tokens, JWTs, PEM private-key blocks, and
   ``key=value`` secret assignments) so novel/unknown secrets are still caught.

Every redaction replaces the match with :data:`REDACTION_PLACEHOLDER`.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# Placeholder substituted for any redacted secret. Stable/importable so tests
# and downstream callers can assert on it.
REDACTION_PLACEHOLDER = "[REDACTED]"

# Known-secret substrings shorter than this are ignored to avoid redacting
# incidental short strings (which would garble unrelated output). Real
# credentials/tokens comfortably exceed this length.
_MIN_KNOWN_SECRET_LEN = 6


# Ordered patterns for well-known credential shapes. Order matters only for
# readability; matches are independent. Each is applied with a global sub.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private-key blocks (RSA/EC/OpenSSH/generic) — redact the whole block.
    re.compile(
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    # AWS access key id (AKIA/ASIA/AGPA/... + 16 uppercase alnum).
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b"),
    # HTTP Authorization bearer tokens (redact the credential, keep the scheme
    # word out of the reconstructed output for safety).
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"),
    # JWT / JWS: three base64url segments separated by dots.
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    # Generic ``secret``/``password``/``token``/``api_key``/``access_key``
    # assignments in ``key: value`` or ``key=value`` form — redact the value.
    re.compile(
        r"(?i)\b(?P<key>(?:api[_-]?key|access[_-]?key|secret(?:[_-]?key|[_-]?access[_-]?key)?"
        r"|client[_-]?secret|password|passwd|pwd|token|auth[_-]?token))\b"
        r"(?P<sep>\s*[:=]\s*)"
        r"(?P<val>\"[^\"]+\"|'[^']+'|[^\s,;]+)"
    ),
)

# Patterns whose value is captured in a named group ``val`` (so we redact only
# the value, preserving the key for readability).
_KEYED_VALUE_PATTERN = _SECRET_PATTERNS[-1]

# Presigned URLs are SigV4 query-auth *capability* URLs: the runtime mints them
# precisely to hand to the Caller (e.g. the ``get_download_url`` tool), so the
# scoped, short-lived credential/signature in the query string IS the intended
# output — not an Owner secret. Shape-based redaction would otherwise DESTROY
# them: the ``...token=`` keyed-value pattern matches ``X-Amz-Security-Token=``
# and its value class greedily eats the rest of the URL (including
# ``&X-Amz-Signature=…``), and the ``AKIA…`` pattern hits the access-key id
# inside ``X-Amz-Credential`` — either leaves S3 with an incomplete query and a
# ``AuthorizationQueryParametersError``. So whole presigned URLs are stashed
# before pattern redaction and restored after. (Exact known-secret redaction
# still runs on the full text first, so a genuine Owner secret embedded in a URL
# is still stripped.) A SigV4 query URL always carries Algorithm+Credential+
# Signature; matching any one is sufficient to recognise the whole URL token.
_PRESIGNED_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>]*[?&]X-Amz-(?:Signature|Credential|Algorithm)=[^\s\"'<>]*"
)

# Sentinel wrapping a stashed presigned URL. NUL never appears in agent text and
# carries no key/prefix any secret pattern keys on, so the stashed token
# (``\x00<n>\x00``) survives pattern redaction untouched.
_STASH_SENTINEL = "\x00"


def redact_secrets(
    text: str,
    *,
    known_secrets: Optional[Iterable[str]] = None,
) -> str:
    """Redact secrets/tokens/credentials from ``text`` (pure).

    Applies known-secret substring redaction first (authoritative when the
    concrete Owner secret values are supplied), then best-effort pattern
    redaction for well-known credential shapes. Every match is replaced with
    :data:`REDACTION_PLACEHOLDER`.

    Args:
        text: The text to scrub. Non-string / empty input is returned as-is.
        known_secrets: Optional iterable of exact secret *values* to strip
            (e.g. Owner tokens/credentials the runtime holds). Values shorter
            than :data:`_MIN_KNOWN_SECRET_LEN` are ignored.

    Returns:
        The redacted text. When nothing matches, the input is returned
        unchanged.
    """
    if not text or not isinstance(text, str):
        return text

    out = text

    # 1) Exact known-secret values (authoritative). Runs on the raw text — and
    # BEFORE presigned URLs are stashed — so a genuine Owner secret embedded in
    # a URL is still stripped.
    for secret in known_secrets or []:
        if isinstance(secret, str) and len(secret) >= _MIN_KNOWN_SECRET_LEN:
            out = out.replace(secret, REDACTION_PLACEHOLDER)

    # 2) Stash presigned (SigV4 query-auth) URLs so the shape-based patterns
    # below can't shred their credential/signature query — those URLs are the
    # intended Caller-facing output, not Owner secrets. Restored verbatim after.
    stashed: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        stashed.append(m.group(0))
        return f"{_STASH_SENTINEL}{len(stashed) - 1}{_STASH_SENTINEL}"

    out = _PRESIGNED_URL_PATTERN.sub(_stash, out)

    # 3) Pattern-based redaction of well-known credential shapes.
    for pattern in _SECRET_PATTERNS:
        if pattern is _KEYED_VALUE_PATTERN:
            out = pattern.sub(
                lambda m: f"{m.group('key')}{m.group('sep')}{REDACTION_PLACEHOLDER}",
                out,
            )
        else:
            out = pattern.sub(REDACTION_PLACEHOLDER, out)

    # 4) Restore stashed presigned URLs.
    if stashed:
        out = re.sub(
            rf"{_STASH_SENTINEL}(\d+){_STASH_SENTINEL}",
            lambda m: stashed[int(m.group(1))],
            out,
        )

    return out
