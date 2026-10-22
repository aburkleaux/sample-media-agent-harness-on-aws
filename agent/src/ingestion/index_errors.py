"""Classify an indexing failure (Req 7.5–7.7, design Component 11 "Failures").

Pure. Inputs are what hybrid-search actually reports: a submit-time refusal (``errorCode`` + ``message``), or a
job's ``status`` with its sanitized ``error`` (``"<errorType>: <errorMessage>"`` or the SFN error name alone, e.g.
``Lambda.AWSLambdaException`` for an indexer Lambda that answered 409 while initializing — measured in the
staging spike) and its ``prepared``/``indexed`` counts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

COLD_START_OR_THROTTLE = "cold_start_or_throttle"
EMBEDDING_SERVICE = "embedding_service"
CONTRACT = "contract"
OTHER = "other"

MAX_CHUNK_RESUBMITS = 2
RESUBMIT_GAP_S = 30

_COLD = re.compile(r"Lambda\.AWSLambdaException|initializing|TooManyRequests|Throttl|Rate exceeded|"
                   r"SlowDown|Lambda\.ServiceException", re.IGNORECASE)
# Bedrock embedding failures that survived the indexer's own retries.
_EMBED = re.compile(r"ModelTimeoutException|ModelErrorException|ModelNotReadyException|"
                    r"ServiceUnavailableException|InternalServerException|InvokeModel|bedrock-runtime",
                    re.IGNORECASE)
_CONTRACT_CODES = frozenset({"INVALID_PARAMETER", "MISSING_PARAMETER"})
_CONTRACT_TEXT = re.compile(r"^ValueError\b|contract|not in schema mappings", re.IGNORECASE)


@dataclass(frozen=True)
class Classified:
    kind: str
    reason_message: str  # the message half of the Req 7.3 reason; the step is always ``indexing``


def _zero(v: object) -> bool:
    return v in (None, 0, "0", "")


def classify(error: str, *, code: str = "", prepared: object = None, indexed: object = None) -> Classified:
    text = (error or "").strip() or "failed"
    if code in _CONTRACT_CODES or _CONTRACT_TEXT.search(text):
        return Classified(CONTRACT, text)
    if _COLD.search(text) and _zero(prepared) and _zero(indexed):
        return Classified(COLD_START_OR_THROTTLE, text)
    if _EMBED.search(text):
        return Classified(EMBEDDING_SERVICE, "search backend unavailable")
    return Classified(OTHER, text)


def may_resubmit_chunk(c: Classified, resubmits_so_far: int) -> bool:
    """Req 7.7: only a cold-start/throttle failure, at most 2 more times (the caller spaces them ≥ 30 s)."""
    return c.kind == COLD_START_OR_THROTTLE and resubmits_so_far < MAX_CHUNK_RESUBMITS


def job_counts(status: dict) -> tuple[int, int, int]:
    def n(k: str) -> int:
        try:
            return int(status.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    return n("prepared"), n("skipped"), n("indexed")


def failure_of(status: dict) -> Optional[Classified]:
    """A terminal unsuccessful job status → its classification; ``None`` for anything else."""
    st = status.get("status")
    if st not in ("FAILED", "TIMED_OUT", "ABORTED"):
        return None
    return classify(str(status.get("error") or st), prepared=status.get("prepared"), indexed=status.get("indexed"))
