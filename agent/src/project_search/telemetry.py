"""Per-searching-turn search telemetry (design Component 9, Req 4.1-4.5).

Every Main_Agent turn that carries a Projects turn context emits ONE telemetry record after its
answer has been delivered. The record is a structured log line in the Main_Agent runtime's log group
(the ``MH_SEARCH_TELEMETRY`` marker), and the same object rides on the turn's ``execution_summary``
frame so a local developer can read it without CloudWatch access.

Shape — arm-A-only (the second search arm was removed, so there is no ``search_arm`` dimension):

```jsonc
{"v": 1, "kind": "search_turn", "ts": "...Z", "env": "staging",
 "session_id": "...", "turn_id": "...",
 "model_id": "...", "skill_sha256": "...",
 "ttft_ms": 9120, "total_ms": 24400,
 "call_count": 2,
 "calls": [{"latency_ms": 3120, "outcome": "success"}, {"latency_ms": 60012, "outcome": "timeout"}],
 "calls_truncated": false,
 "tokens": {"in": 21400, "out": 380},
 "citations": {"total": 3, "unverified": 0, "any_unverified": false, "truncated": false}}
```

**No secrets, no content** (Req 4.2). :func:`build_record` takes no answer text, no prompt, no tool
arguments and no Hits — only this turn's per-call samples and, from the :class:`TurnOutcome`, the
citation counts. Every field is an opaque id, an enum, an integer, a boolean, the model id or a hex
digest, so the record structurally cannot carry a token, a ``search_term`` or Hit text.

Per-call samples are collected by :func:`note_call` (the Search_Tool calls it once per call, including
calls the Component 2 checks reject) and are read and dropped per ``turn_id`` by :func:`build_record`
and :func:`release`. The map is bounded to 64 turns like the other per-turn maps.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

RECORD_VERSION = 1
#: ``calls[]`` keeps at most this many samples; ``call_count`` is always the exact total.
MAX_CALLS = 100
#: Bound the per-turn sample map like ``ledger._MAX_TURNS`` / ``turn_end._MAX_TURNS``.
_MAX_TURNS = 64

_lock = threading.Lock()
_samples: dict[str, list["CallSample"]] = {}


# ---------------------------------------------------------------------------
# Per-call samples
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CallSample:
    """One Search_Tool call's measurement.

    ``latency_ms`` is the wall time the call took (``None`` when not measured); ``outcome`` is
    ``"success"`` or one of the ``errors.py`` categories (e.g. ``"timeout"``, ``"policy_denied"``,
    ``"other"``).
    """

    latency_ms: Optional[int]
    outcome: str


def note_call(turn_id: str, sample: CallSample) -> None:
    """Record one Search_Tool call's :class:`CallSample` under ``turn_id``.

    No-op when ``turn_id`` is empty or ``sample`` is not a :class:`CallSample`. Bounded to
    :data:`_MAX_TURNS` turns, evicting the oldest first. Never raises.
    """
    if not turn_id or not isinstance(sample, CallSample):
        return
    with _lock:
        if turn_id not in _samples and len(_samples) >= _MAX_TURNS:
            _samples.pop(next(iter(_samples)))  # evict oldest
        _samples.setdefault(turn_id, []).append(sample)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------
def build_record(
    *,
    tc: Any,
    session_id: Optional[str],
    model_id: Optional[str],
    ttft_ms: Optional[int],
    total_ms: int,
    usage: Mapping[str, int],
    outcome: Any,
    skill_sha256: Optional[str],
) -> dict:
    """Build one search telemetry record (Req 4.1). Pure.

    Reads this turn's samples (by ``tc.turn_id``) and, from ``outcome`` (a
    :class:`turn_end.TurnOutcome` or ``None``), only the citation counts. Takes no answer text, no
    prompt, no tool arguments and no Hits, so it cannot copy them (Req 4.2).
    """
    turn_id = str(getattr(tc, "turn_id", "") or "")
    with _lock:
        samples = list(_samples.get(turn_id, ()))

    calls = [{"latency_ms": s.latency_ms, "outcome": s.outcome} for s in samples]
    call_count = len(calls)

    tokens_in = tokens_out = 0
    if isinstance(usage, Mapping):
        tokens_in = int(usage.get("inputTokens", 0) or 0)
        tokens_out = int(usage.get("outputTokens", 0) or 0)

    return {
        "v": RECORD_VERSION,
        "kind": "search_turn",
        "ts": _utc_now_iso(),
        "env": os.environ.get("DEPLOY_ENV", "local") or "local",
        "session_id": session_id or "",
        "turn_id": turn_id,
        "model_id": model_id or None,
        "skill_sha256": skill_sha256 or None,
        "ttft_ms": ttft_ms,
        "total_ms": int(total_ms),
        "call_count": call_count,
        "calls": calls[:MAX_CALLS],
        "calls_truncated": call_count > MAX_CALLS,
        "tokens": {"in": tokens_in, "out": tokens_out},
        "citations": _citation_counts(outcome),
    }


def _citation_counts(outcome: Any) -> dict:
    """Citation counts from a :class:`turn_end.TurnOutcome`; all zero/false when ``outcome`` is ``None``."""
    records = list(getattr(outcome, "citations", None) or []) if outcome is not None else []
    unverified = sum(1 for r in records if not getattr(r, "verified", False))
    return {
        "total": len(records),
        "unverified": unverified,
        "any_unverified": unverified > 0,
        "truncated": bool(getattr(outcome, "truncated", False)),
    }


# ---------------------------------------------------------------------------
# Emit / release
# ---------------------------------------------------------------------------
def emit(record: dict) -> None:
    """Log ``record`` as the ``MH_SEARCH_TELEMETRY`` structured line (Req 4.3). Never raises.

    Any exception is logged with the session and turn ids only.
    """
    try:
        logger.info(
            "MH_SEARCH_TELEMETRY %s",
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )
    except Exception as exc:  # noqa: BLE001 - a telemetry write must never break a turn
        sid = record.get("session_id") if isinstance(record, dict) else None
        tid = record.get("turn_id") if isinstance(record, dict) else None
        logger.warning("search telemetry failed session=%s turn=%s %s", sid, tid, type(exc).__name__)


def release(turn_id: str) -> None:
    """Drop ``turn_id``'s per-call samples. Never raises."""
    with _lock:
        _samples.pop(turn_id, None)


def _utc_now_iso() -> str:
    """UTC timestamp as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (millisecond precision)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _reset_for_test() -> None:
    """Clear all per-turn samples. For tests only."""
    with _lock:
        _samples.clear()
