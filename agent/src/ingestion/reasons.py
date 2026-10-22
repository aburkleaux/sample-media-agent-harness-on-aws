"""Failure-reason composition (Req 7.3): ``<step>: <message>``, credentials redacted, then cut to 1,000 chars.

Pure and self-contained: the ingestion Lambda package bundles only ``ingestion/`` and ``projects/``, so this
does not import the runtime's ``security.redaction``. Redaction runs BEFORE truncation, so a credential cut in
half by the limit is never left partly visible.
"""
from __future__ import annotations

import re

MAX_REASON_CHARS = 1000
PLACEHOLDER = "[REDACTED]"

STEPS = frozenset({
    "shot-segmentation", "speech-diarization", "frame capture", "shot-metadata-enrichment",
    "conversation-analysis", "image rendition", "image description", "document build", "indexing",
    # Document (PDF) text pipeline (Requirement 16): the in-tick extract step and the skip/report step.
    "document extract", "document text",
    # Collection_Rebuild steps (Req 10.6) and Project-deletion purge steps (projects-access Req 1.24).
    "rebuild wait", "rebuild delete", "rebuild create", "rebuild resubmit",
    "purge wait", "purge collection", "purge uploads", "purge objects", "purge records",
})

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Presigned-URL credential parameters: keep the name, drop the value.
    (re.compile(r"(?i)\b(X-Amz-(?:Signature|Credential|Security-Token))=([^&\s\"']+)"), r"\1=" + PLACEHOLDER),
    (re.compile(r"(?i)\b(Signature|AWSAccessKeyId|x-amz-security-token)=([^&\s\"']+)"), r"\1=" + PLACEHOLDER),
    # Bearer tokens and bare JWTs.
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + PLACEHOLDER),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), PLACEHOLDER),
    # Named secret assignments (secret key, session token, client secret, password).
    (re.compile(r"(?i)\b(aws_secret_access_key|secret_access_key|secretaccesskey|aws_session_token|"
                r"session_?token|client_?secret|password)(\"?\s*[:=]\s*\"?)([^\s\"',;&]+)"),
     r"\1\2" + PLACEHOLDER),
    # AWS access key ids.
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"), PLACEHOLDER),
)


def redact(text: str) -> str:
    out = text or ""
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def compose(step: str, message: str) -> str:
    """``<step>: <message>`` redacted and truncated. ``step`` must be a known step name."""
    if step not in STEPS:
        raise ValueError(f"unknown failure step {step!r}")
    msg = " ".join(str(message or "").split()) or "failed"
    return redact(f"{step}: {msg}")[:MAX_REASON_CHARS]
