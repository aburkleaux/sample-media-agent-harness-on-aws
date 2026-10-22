"""Operator command for the search telemetry report (design Component 9, Req 4.4, 4.5).

Reads the per-searching-turn telemetry records (the ``MH_SEARCH_TELEMETRY`` lines emitted by
``project_search/telemetry.py``) for a chosen environment and time window, and prints:

  * the number of turns;
  * the p50 and p95 of time to first answer token, total turn time, and Search_Tool call latency;
  * the mean number of Search_Tool calls and the mean model input and output tokens per turn;
  * the share of turns whose answer contained a Citation that is not a Verified_Citation.

Each call that reached the 60-second Search_Tool bound (``outcome == "timeout"``) is counted as a
``timeout`` and contributes a fixed 60,000 ms to the Search_Tool call latency figures (Req 4.5), so
scheduling noise around the bound does not move the percentiles.

Sources:
  * ``--telemetry-file FILE`` — the local Main_Agent terminal log, `tee`'d to a file. Each line may be
    a raw record (JSON) or an ``MH_SEARCH_TELEMETRY <json>`` line (OTEL JSON or the plain-text mirror).
  * Deployed (default) — the live Main_Agent CloudWatch log group derived from ``AGENT_RUNTIME_ARN`` in
    ``.env.<env>`` (falling back to ``.env``), never chosen from an alphabetical listing, and never a
    group whose name starts with ``dynamic_skills_agent_`` (orphaned runtimes). Read-only.

The aggregation (:func:`aggregate`) and record extraction (:func:`extract_records`) are pure so they
can be unit-tested without AWS.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from typing import Any, Iterable, Optional

MARKER = "MH_SEARCH_TELEMETRY"
#: A call that reached the 60-second bound contributes this fixed duration (Req 4.5).
TIMEOUT_SUBSTITUTE_MS = 60_000
_ORPHAN_PREFIX = "dynamic_skills_agent_"


# --------------------------------------------------------------------------- #
# Pure core
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], pct: float) -> Optional[float]:
    """Linear-interpolation percentile (``numpy``'s default method). ``None`` for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _call_latency_ms(call: dict) -> Optional[float]:
    """One call's latency for the report: 60,000 ms for a ``timeout`` (Req 4.5), else ``latency_ms``."""
    if not isinstance(call, dict):
        return None
    if call.get("outcome") == "timeout":
        return float(TIMEOUT_SUBSTITUTE_MS)
    latency = call.get("latency_ms")
    return float(latency) if isinstance(latency, (int, float)) else None


def aggregate(records: Iterable[dict]) -> dict:
    """Compute the Req 4.4 figures over ``records``. Pure.

    Returns a dict with ``turns``; ``ttft_ms`` / ``total_ms`` / ``call_latency_ms`` each a
    ``{"p50", "p95"}`` map (values ``None`` when there is no data); ``mean_calls``;
    ``mean_tokens_in`` / ``mean_tokens_out``; and ``share_any_unverified``.
    """
    records = [r for r in records if isinstance(r, dict)]
    turns = len(records)

    ttft: list[float] = []
    total: list[float] = []
    call_latencies: list[float] = []
    call_counts: list[int] = []
    tokens_in: list[int] = []
    tokens_out: list[int] = []
    any_unverified = 0

    for r in records:
        if isinstance(r.get("ttft_ms"), (int, float)):
            ttft.append(float(r["ttft_ms"]))
        if isinstance(r.get("total_ms"), (int, float)):
            total.append(float(r["total_ms"]))
        call_counts.append(int(r.get("call_count") or 0))
        for call in r.get("calls") or []:
            latency = _call_latency_ms(call)
            if latency is not None:
                call_latencies.append(latency)
        tokens = r.get("tokens") or {}
        tokens_in.append(int(tokens.get("in") or 0))
        tokens_out.append(int(tokens.get("out") or 0))
        if bool((r.get("citations") or {}).get("any_unverified")):
            any_unverified += 1

    def _mean(xs: list) -> Optional[float]:
        return (sum(xs) / len(xs)) if xs else None

    return {
        "turns": turns,
        "ttft_ms": {"p50": _percentile(ttft, 50), "p95": _percentile(ttft, 95)},
        "total_ms": {"p50": _percentile(total, 50), "p95": _percentile(total, 95)},
        "call_latency_ms": {
            "p50": _percentile(call_latencies, 50),
            "p95": _percentile(call_latencies, 95),
        },
        "mean_calls": _mean(call_counts),
        "mean_tokens_in": _mean(tokens_in),
        "mean_tokens_out": _mean(tokens_out),
        "share_any_unverified": (any_unverified / turns) if turns else None,
    }


def extract_records(lines: Iterable[str]) -> list[dict]:
    """Parse telemetry records from log ``lines``, de-duplicated on ``(session_id, turn_id)``.

    A line contributes a record when the first ``{`` after the ``MH_SEARCH_TELEMETRY`` marker (or at
    the start of a bare line) parses as a ``search_turn`` object. OTEL JSON wraps the mirror line, so
    the same record can appear twice; the last one seen per ``(session_id, turn_id)`` wins.
    """
    seen: dict[tuple, dict] = {}
    order: list[tuple] = []
    for line in lines:
        rec = _record_from_line(line)
        if rec is None:
            continue
        key = (rec.get("session_id", ""), rec.get("turn_id", ""))
        if key not in seen:
            order.append(key)
        seen[key] = rec
    return [seen[k] for k in order]


def _record_from_line(line: str) -> Optional[dict]:
    if not isinstance(line, str) or MARKER not in line and "{" not in line:
        return None
    start = line.find(MARKER)
    search_from = (start + len(MARKER)) if start != -1 else 0
    brace = line.find("{", search_from)
    if brace == -1:
        return None
    # The record is the last balanced JSON object on the line; a greedy right brace handles the
    # trailing OTEL envelope fields that may follow on the mirror line.
    for end in range(len(line), brace, -1):
        chunk = line[brace:end]
        if not chunk.endswith("}"):
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == "search_turn":
            return obj
        # OTEL body: the record may be nested under a "body"/"message" field.
        for field in ("body", "message"):
            inner = obj.get(field) if isinstance(obj, dict) else None
            if isinstance(inner, str) and MARKER in inner:
                nested = _record_from_line(inner)
                if nested is not None:
                    return nested
    return None


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _ms(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:,.0f} ms"


def _num(value: Optional[float], digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def format_report(stats: dict, *, env: str, since: str, until: str) -> str:
    lines = [
        f"Search telemetry — env={env} window=[{since} .. {until}]",
        f"  turns: {stats['turns']}",
        f"  time to first answer token  p50={_ms(stats['ttft_ms']['p50'])}  p95={_ms(stats['ttft_ms']['p95'])}",
        f"  total turn time             p50={_ms(stats['total_ms']['p50'])}  p95={_ms(stats['total_ms']['p95'])}",
        f"  search call latency         p50={_ms(stats['call_latency_ms']['p50'])}  p95={_ms(stats['call_latency_ms']['p95'])}",
        f"  mean search calls / turn:   {_num(stats['mean_calls'])}",
        f"  mean input tokens / turn:   {_num(stats['mean_tokens_in'], 0)}",
        f"  mean output tokens / turn:  {_num(stats['mean_tokens_out'], 0)}",
        f"  turns with an unverified citation: "
        + ("n/a" if stats["share_any_unverified"] is None else f"{stats['share_any_unverified'] * 100:.1f}%"),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _read_file(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return extract_records(fh)


def _runtime_arn_for_env(env: str) -> Optional[str]:
    """Read ``AGENT_RUNTIME_ARN`` from ``.env.<env>`` (falling back to ``.env``) at the repo root."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for name in (f".env.{env}", ".env"):
        path = os.path.join(here, name)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                m = re.match(r"\s*AGENT_RUNTIME_ARN\s*=\s*(.+?)\s*$", raw)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    return None


def _log_group_for_arn(arn: str) -> str:
    """The Main_Agent runtime log group for ``arn`` (``.../runtime/<name>-<id>`` → runtime id suffix)."""
    runtime_id = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if runtime_id.startswith(_ORPHAN_PREFIX):
        raise SystemExit(
            f"refusing orphaned runtime '{runtime_id}' (dynamic_skills_agent_*); check AGENT_RUNTIME_ARN"
        )
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def _read_cloudwatch(env: str, since_ms: int, until_ms: int, region: str) -> list[dict]:
    import boto3

    arn = _runtime_arn_for_env(env)
    if not arn:
        raise SystemExit(f"no AGENT_RUNTIME_ARN found for env={env} (.env.{env} or .env)")
    log_group = _log_group_for_arn(arn)
    logs = boto3.client("logs", region_name=region)
    lines: list[str] = []
    kwargs = {
        "logGroupName": log_group,
        "startTime": since_ms,
        "endTime": until_ms,
        "filterPattern": f'"{MARKER}"',
    }
    while True:
        resp = logs.filter_log_events(**kwargs)
        lines.extend(e.get("message", "") for e in resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return extract_records(lines)


def _parse_ts_ms(value: str) -> int:
    """Parse an epoch-ms integer or an ISO-8601 timestamp into epoch ms."""
    import datetime as _dt

    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return int(value)
    dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Search telemetry report (Req 4.4).")
    parser.add_argument("--env", default="staging", help="deploy env (sandbox|staging|prod|local)")
    parser.add_argument("--since", required=True, help="window start (epoch ms or ISO-8601)")
    parser.add_argument("--until", required=True, help="window end (epoch ms or ISO-8601)")
    parser.add_argument("--telemetry-file", help="read records from a local log file instead of CloudWatch")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args(argv)

    if args.telemetry_file:
        records = _read_file(args.telemetry_file)
    else:
        records = _read_cloudwatch(
            args.env, _parse_ts_ms(args.since), _parse_ts_ms(args.until), args.region
        )

    print(format_report(aggregate(records), env=args.env, since=args.since, until=args.until))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
