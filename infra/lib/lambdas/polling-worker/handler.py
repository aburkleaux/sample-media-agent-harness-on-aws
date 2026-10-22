"""Polling Worker Lambda for the async-job-tracking feature.

Triggered by an EventBridge ``rate(<n>m)`` schedule (configured in CDK
task 8.1; default 1 minute). EventBridge ``Rule`` schedules can't go
below minute granularity — see the ``pollIntervalMinutes`` prop in
``agent-infra-stack.ts`` for the rationale and tuning knob. Each
invocation:

1. Queries the JobRegistry ``poll-partition-index`` GSI for every job
   with ``pollPartition = "ACTIVE"`` (R4.1) — that's exactly the set
   of PENDING/RUNNING jobs across all actors.
2. For each active job, dispatches the trio's ``status_tool``:
   * ``gateway_id == "__aws_sfn__"`` -> direct boto3
     ``stepfunctions:DescribeExecution``. Mapped to the canonical job
     status set per the design.
   * Otherwise -> resolve the gateway from the gateway-registry table,
     perform OAuth client-credentials, call MCP ``tools/call``. The
     job's ``actorId`` is forwarded as ``__actor_id`` in the tool
     arguments so gateway-side authorization sees the original Actor
     identity (R11.3).
3. Reconciles the response (R4.2, R4.3, R4.4, R4.5):
   * Successful status response -> :meth:`JobRegistry.set_active_status`
     for active statuses, :meth:`JobRegistry.set_terminal` for terminal
     ones.
   * Transient error (``ThrottlingException``, ``ServiceUnavailable``,
     ``TimeoutError``) -> leave the record unchanged so the next cycle
     retries (R4.4).
   * Non-transient error -> :meth:`JobRegistry.bump_error`. After
     :data:`NON_TRANSIENT_THRESHOLD` consecutive non-transient errors,
     :meth:`JobRegistry.set_terminal` with status ``FAILED`` (R4.5).

Second trigger — Step Functions completion events: an EventBridge rule on
``Step Functions Execution Status Change`` (terminal statuses, this account's
state machines) invokes the same handler, which runs this cycle over just the
one job that execution belongs to (:func:`handle_execution_event`). That
settles an M2C job within about a second of its execution ending instead of up
to a minute later; the schedule stays as the fallback for everything else.

Notify-only completion (R9.1, R9.2, R9.3): the worker writes the
registry update and returns. Push fan-out to clients is the
ChangefeedFanout Lambda's job (task 9.1); this worker does NOT invoke
results tools and does NOT generate new agent turns.

Concurrency: this v1 dispatches jobs sequentially in a single
invocation. That's fine at the design's small-N scale — typical use
is 0–10 active jobs per actor and a handful of concurrent actors. A
slow status tool can stall the cycle, but the EventBridge schedule
itself is the recovery mechanism: the next tick fires regardless. If
we outgrow this, the natural next step is
``concurrent.futures.ThreadPoolExecutor`` per cycle; the per-job
reconcile logic and the registry's conditional writes are race-safe
so concurrency only changes the throughput.

Environment variables (set by CDK task 8.1):
    JOB_REGISTRY_TABLE              DDB table name for the job registry.
    JOB_REGISTRY_POLL_INDEX         GSI name. Defaults to
                                    ``poll-partition-index``.
    GATEWAY_REGISTRY_TABLE          DDB table name for the gateway
                                    registry (descriptor + auth_config
                                    lookups for the MCP path).
    AWS_REGION                      Provided by the Lambda runtime.
    NON_TRANSIENT_THRESHOLD         Override for the consecutive-error
                                    threshold. Defaults to 5.

Why this Lambda lives in ``infra/lib/lambdas/polling-worker/`` and not
``agent/src/jobs/``:
    The polling worker is its own deployment artifact (a Lambda zip),
    distinct from the AgentCore Runtime that ships ``agent/src``. To
    keep the dependency surface minimal (boto3 + stdlib only — no
    httpx, no MCP SDK), the relevant pieces of ``JobRegistry`` and the
    gateway descriptor lookups are inlined here. See design.md
    "Polling_Worker" section for the full architecture.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 9.1, 9.2, 9.3, 11.3
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from typing import Any, Iterable

import boto3
from botocore.exceptions import ClientError

from artifact_capability import has_renderable_artifacts, renderable_artifact_urls

# Shared with the plan chainer via a Lambda layer (mounted at /opt/python) so one
# copy of the resolution rules serves plan settle and job terminal alike.
import result_resolution
from gateway_invoker import (
    GatewayDescriptorMissing,
    GatewayInvocationError,
    TransientGatewayError,
    invoke_status_tool,
)
from registry import JobRegistry

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-west-2")
JOB_REGISTRY_TABLE = os.environ.get("JOB_REGISTRY_TABLE", "")
JOB_REGISTRY_POLL_INDEX = os.environ.get(
    "JOB_REGISTRY_POLL_INDEX", "poll-partition-index"
)
GATEWAY_REGISTRY_TABLE = os.environ.get("GATEWAY_REGISTRY_TABLE", "")

# Number of consecutive non-transient errors that flip a job to FAILED
# (R4.5). Mirrors the design's ``NON_TRANSIENT_THRESHOLD = 5``.
NON_TRANSIENT_THRESHOLD = int(os.environ.get("NON_TRANSIENT_THRESHOLD", "5"))

# Stuck-job sweep (resilience P0): any active job whose age exceeds this
# bound is force-transitioned to TIMED_OUT regardless of poll result — this
# closes the "job never reaches terminal" gap (SFN genuinely stuck, gateway
# returning RUNNING forever, or polls failing transiently so the error
# threshold never trips). Generous default (1h); tune per longest legit job.
MAX_JOB_AGE_SECONDS = int(os.environ.get("MAX_JOB_AGE_SECONDS", "3600"))

# CloudWatch EMF-style custom metrics namespace for resilience alarms.
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "MediaworksHarness/Jobs")
# Where a single job's resolved result artifact is written. A LONE async job has
# no plan, so it got no aggregate -- and retrieving one still cost ~27 model steps
# of fan-out while a 4-node plan cost 18. Same function, second trigger.
SKILLS_S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
# CloudFront domain for the harness artifacts bucket. Present so a LONE job's result
# aggregate can persist M2C proxy outputs into our bucket and return durable CloudFront
# urls — matching the plan-chainer, so direct-dispatch links no longer die after ~1h.
ARTIFACTS_DOMAIN = os.environ.get("ARTIFACTS_DOMAIN", "")

# Sentinel ``gateway_id`` value that routes a job to the SFN direct
# path instead of MCP gateway dispatch. Set by the agent runtime at
# registration time when the start-tool response shape matches a
# Step Functions execution.
SFN_GATEWAY_ID = "__aws_sfn__"

# Status values the polling worker treats as ``transient`` when they
# come back as exception classes. Mirrors the design's ``TRANSIENT``
# set; matches against the cause's ``__class__.__name__`` to remain
# independent of the boto3 service model.
_TRANSIENT_EXCEPTION_NAMES = frozenset(
    {"ThrottlingException", "ServiceUnavailable", "TimeoutError"}
)

# Active vs. terminal partition for the canonical job status set.
_ACTIVE_STATUSES = frozenset({"PENDING", "RUNNING"})
_TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
)

# Queue-style servers report status with their own key and vocabulary. fal returns
# IN_QUEUE / IN_PROGRESS / COMPLETED, not the canonical SUCCEEDED / RUNNING set, and
# not always under a top-level "status" string -- so every poll was rejected as
# "status response missing status field" and after 5 tries the job was marked FAILED
# while it was still rendering perfectly well. The whole auth chain worked; only this
# last translation was missing.
_STATUS_KEYS = ("status", "state", "job_status", "queue_status")
_STATUS_ALIASES = {
    "IN_QUEUE": "PENDING",
    "QUEUED": "PENDING",
    "SUBMITTED": "PENDING",
    "IN_PROGRESS": "RUNNING",
    "PROCESSING": "RUNNING",
    "STARTED": "RUNNING",
    "COMPLETED": "SUCCEEDED",
    "COMPLETE": "SUCCEEDED",
    "SUCCESS": "SUCCEEDED",
    "OK": "SUCCEEDED",
    "ERROR": "FAILED",
    "CANCELLED": "ABORTED",
    "CANCELED": "ABORTED",
}



def _result_body_error(response) -> str:
    """Return an error description when a 'successful' result body is actually a failure.

    Checks the shapes a queue result can carry an error in: an explicit error/detail
    field, an isError envelope, or a non-2xx status code alongside the payload. Returns ""
    when the body looks clean, so a normal result is unaffected.
    """
    if not isinstance(response, dict):
        return ""
    if response.get("isError"):
        for block in response.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]
        return "result marked isError"
    for key in ("error", "detail", "message"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict) and value:
            return json.dumps(value)[:300]
    code = response.get("status_code") or response.get("statusCode") or response.get("http_status")
    if isinstance(code, int) and not (200 <= code < 300):
        return f"HTTP {code}"
    return ""

def _normalize_status(response) -> Optional[str]:
    """Map a queue server's status onto the canonical vocabulary, or None.

    Tries the known status keys at the top level and one level down, since some
    servers nest the field. Unknown values map to None so the caller still treats the
    response as malformed rather than inventing a state -- guessing SUCCEEDED on an
    unrecognised value would mark an unfinished job complete.
    """
    if not isinstance(response, dict):
        return None
    candidates = []
    for key in _STATUS_KEYS:
        value = response.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
        elif isinstance(value, dict):
            for inner in _STATUS_KEYS:
                nested = value.get(inner)
                if isinstance(nested, str) and nested:
                    candidates.append(nested)
    for raw in candidates:
        upper = raw.strip().upper()
        if upper in _ACTIVE_STATUSES or upper in _TERMINAL_STATUSES:
            return upper
        mapped = _STATUS_ALIASES.get(upper)
        if mapped:
            return mapped
    return None



# ---------------------------------------------------------------------------
# Boto3 clients (created at module import for warm-invocation reuse)
# ---------------------------------------------------------------------------

_dynamodb_resource = boto3.resource("dynamodb", region_name=REGION)
_secrets_client = boto3.client("secretsmanager", region_name=REGION)
_sfn_client = boto3.client("stepfunctions", region_name=REGION)
_cloudwatch = boto3.client("cloudwatch", region_name=REGION)
# Cache of Step Functions clients keyed by region. Execution ARNs may live
# in a different region than this Lambda (e.g. the M2C state machines run in
# us-west-2 while the harness deploys in us-east-1). ``DescribeExecution``
# rejects a cross-region ARN with ``InvalidArn``, so we must call it with a
# client in the ARN's own region.
_sfn_clients_by_region: dict[str, Any] = {REGION: _sfn_client}

_registry: JobRegistry | None = None
_gateway_registry_table = None


def _get_registry() -> JobRegistry:
    """Lazily build the JobRegistry wrapper.

    Constructed on first use rather than at import time so unit tests
    can stub the env vars before the Lambda body runs. The ``boto3``
    DynamoDB resource itself is module-level and reused across
    invocations.
    """
    global _registry
    if _registry is None:
        if not JOB_REGISTRY_TABLE:
            raise RuntimeError(
                "JOB_REGISTRY_TABLE not set; polling worker cannot run"
            )
        _registry = JobRegistry(
            table_name=JOB_REGISTRY_TABLE,
            region=REGION,
            poll_index_name=JOB_REGISTRY_POLL_INDEX,
        )
    return _registry


def _get_gateway_registry_table():
    """Lazily resolve the gateway-registry DDB ``Table`` resource.

    Only the MCP path needs this; the SFN direct path doesn't touch
    it. Built lazily so a stack that has only SFN-backed trios doesn't
    require ``GATEWAY_REGISTRY_TABLE`` to be set.
    """
    global _gateway_registry_table
    if _gateway_registry_table is None:
        if not GATEWAY_REGISTRY_TABLE:
            raise RuntimeError(
                "GATEWAY_REGISTRY_TABLE not set; MCP gateway dispatch unavailable"
            )
        _gateway_registry_table = _dynamodb_resource.Table(GATEWAY_REGISTRY_TABLE)
    return _gateway_registry_table


# ---------------------------------------------------------------------------
# Status dispatch — SFN direct + MCP gateway
# ---------------------------------------------------------------------------


# SFN execution ARNs follow this shape:
#   arn:aws:states:<region>:<account>:execution:<state-machine>:<execution-name>
# Used by ``register_job`` (task 5.1 / 7.2) to flag the trio for the
# direct path; documented here so the polling worker's branch matches
# the producer side.
SFN_EXECUTION_ARN_RE = re.compile(
    r"^arn:aws:states:[^:]+:\d+:execution:[^:]+:[^:]+$"
)


def _sfn_client_for_arn(execution_arn: str) -> Any:
    """Return an SFN client in the same region as the execution ARN.

    ``DescribeExecution`` raises ``InvalidArn`` when the client region
    doesn't match the ARN region. Execution ARNs encode their region in
    the 4th ``:``-delimited field (``arn:aws:states:<region>:...``), so
    we build (and cache) a client per region rather than assuming the
    Lambda's own region.
    """
    region = execution_arn.split(":")[3]
    client = _sfn_clients_by_region.get(region)
    if client is None:
        client = boto3.client("stepfunctions", region_name=region)
        _sfn_clients_by_region[region] = client
    return client


def _describe_sfn_execution(job_id: str) -> dict[str, Any]:
    """Map an SFN execution status to the canonical job status set.

    The SFN ``DescribeExecution`` API returns one of:
    ``RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED |
    PENDING_REDRIVE``. The first five are already in the canonical
    set; ``PENDING_REDRIVE`` is treated as ``RUNNING`` because the
    job is queued for retry inside Step Functions, not user-visible
    terminal failure.

    Raises ``GatewayInvocationError`` (or a transient subclass) on
    boto3 errors so :func:`reconcile` classifies them the same way
    as MCP errors.
    """
    if not SFN_EXECUTION_ARN_RE.match(job_id):
        # The trio was tagged for the SFN path but the job_id isn't
        # an execution ARN. Fail loudly — silently falling back to
        # MCP would mask the registration bug.
        raise GatewayInvocationError(
            f"job_id {job_id!r} routed to SFN path but is not an execution ARN"
        )
    try:
        resp = _sfn_client_for_arn(job_id).describe_execution(executionArn=job_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"ThrottlingException", "ServiceUnavailable"}:
            raise TransientGatewayError(f"sfn {code}") from exc
        raise GatewayInvocationError(f"sfn {code or 'error'}: {exc}") from exc

    raw_status = resp.get("status", "")
    if raw_status == "PENDING_REDRIVE":
        status = "RUNNING"
    else:
        status = raw_status
    error: str | None = None
    if status in {"FAILED", "TIMED_OUT", "ABORTED"}:
        # SFN populates ``error`` and ``cause`` on terminal-failure
        # executions. Either one alone is informative; we prefer
        # ``cause`` because it carries the human-readable message.
        error = resp.get("cause") or resp.get("error") or None
    result: dict[str, Any] = {"status": status, "error": error}
    raw_output = resp.get("output")
    if isinstance(raw_output, str) and raw_output:
        try:
            parsed_output = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError):
            parsed_output = None
        if isinstance(parsed_output, (dict, list)):
            result["executionOutput"] = parsed_output
    return result


_s3 = boto3.client("s3", region_name=REGION)


def _read_s3_json(bucket, key, max_bytes=None):
    """``(parsed, size)`` for a small JSON object, else ``(None, size)``."""
    limit = result_resolution.MAX_INLINE_FINDINGS_BYTES if max_bytes is None else max_bytes
    head = _s3.head_object(Bucket=bucket, Key=key)
    size = head.get("ContentLength", 0) or 0
    if size > limit:
        return None, size
    body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        return json.loads(body.decode("utf-8")), size
    except Exception:
        return None, size


def _list_output_prefix(bucket, prefix):
    """List real object keys under a lone job's output prefix, so its aggregate carries the
    actual files (F4a) instead of a bare pointer. Bounded, best-effort."""
    keys = []
    try:
        resp = _s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=100)
        for obj in resp.get("Contents", []) or []:
            k = obj.get("Key")
            if k and not k.endswith("/"):
                keys.append(k)
    except Exception:
        logger.exception("job aggregate: prefix list failed for s3://%s/%s", bucket, prefix)
    return keys


# --- Session output layout (asset-lineage Phase 3 (a)) -----------------------------------------------
# A copy of agent/src/projects/storage_layout.session_output_prefix (separate bundle), pinned by
# test_session_output_layout.py over agent/tests/fixtures/session_layout_cases.json.
_LAYOUT_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_LAYOUT_PID_RE = re.compile(r"^p[0-9a-f]{32}$")
_LAYOUT_SHARED = frozenset({"external", "durable", "imports", "uploads", "m2c"})


def _layout_output_prefix(sid, pid=None):
    """``projects/<pid>/sessions/<sid>/output/`` iff SESSION_OUTPUT_LAYOUT=project and pid/sid are valid,
    else ``artifacts/<sid>/output/``; ``artifacts/`` with no session."""
    if not sid:
        return "artifacts/"
    if ((os.environ.get("SESSION_OUTPUT_LAYOUT") or "").strip().lower() == "project"
            and isinstance(pid, str) and _LAYOUT_PID_RE.fullmatch(pid)
            and _LAYOUT_SID_RE.fullmatch(sid) and sid not in _LAYOUT_SHARED):
        return f"projects/{pid}/sessions/{sid}/output/"
    return f"artifacts/{sid}/output/"


def _publish_job_result(job_id, status, payload, error, artifact_urls=None, session_id="",
                        project_id=""):
    """Write a LONE job's resolved result artifact; return its key or "".

    Mirrors the chainer's plan aggregate so both paths produce one schema. Never
    raises: the job's terminal status must be recorded even if this fails, so a
    failure degrades to "no result artifact" rather than leaving the job unpollable
    and stuck.
    """
    if not SKILLS_S3_BUCKET:
        return ""
    # Session-scoped durable output location (P1). When the job row carries a
    # sessionId (every UI-driven job does), materialize its M2C outputs under
    # artifacts/<sid>/output/<job>/ so ALL of a session's outputs live in one
    # durable, CloudFront-served place the session bucket already exposes — not
    # just the rendered media. A lone/session-less job (e.g. external invoke)
    # keeps the job-scoped artifacts/<job>/output/ fallback. With the project layout on and the row's
    # producingProjectId, the session part is projects/<pid>/sessions/<sid>/output/ (Phase 3 (a)).
    _job_seg = job_id.rsplit(":", 1)[-1].replace("/", "_") if job_id else ""
    if session_id and _job_seg:
        _import_prefix = f"{_layout_output_prefix(session_id, project_id)}{_job_seg}/"
    elif _job_seg:
        _import_prefix = f"artifacts/{_job_seg}/output/"
    else:
        _import_prefix = "artifacts/imports/"
    try:
        body = result_resolution.aggregate_json(
            plan_id=job_id,
            status=status,
            nodes={job_id: {"status": status, "value": payload, "error": error or "",
                            "artifact_urls": list(artifact_urls or [])}},
            read_s3_json=_read_s3_json,
            list_prefix=_list_output_prefix,
            # Persist M2C proxy artifacts into our CloudFront bucket so the job's
            # links are durable — the chainer already does this for plans; without
            # it here, direct-dispatch download/render links expired after ~1 hour.
            import_artifact=result_resolution.make_s3_importer(
                _s3, SKILLS_S3_BUCKET, ARTIFACTS_DOMAIN,
                import_prefix=_import_prefix,
            ),
        )
    except Exception:
        logger.exception("job aggregate: build failed for %s", job_id)
        return ""
    key = f"jobs/{job_id}/result.json"
    try:
        _s3.put_object(
            Bucket=SKILLS_S3_BUCKET, Key=key,
            Body=body.encode("utf-8"), ContentType="application/json",
        )
    except Exception:
        logger.exception("job aggregate: could not write %s", key)
        return ""
    logger.info("job aggregate: wrote %s (%d bytes)", key, len(body))
    return key


def _invoke_status(job: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a single job's status check.

    Returns the ``{"status": ..., "error": ...}``-shaped dict consumed
    by :func:`reconcile`. Raises :class:`GatewayInvocationError` (or a
    subclass) on any failure so the caller can classify it.
    """
    gateway_id = job.get("gatewayId", "")
    if gateway_id == SFN_GATEWAY_ID:
        return _describe_sfn_execution(job["jobId"])

    return invoke_status_tool(
        gateway_id=gateway_id,
        status_tool=job["statusTool"],
        job_id=job["jobId"],
        # R11.3: the original Actor's id is forwarded as part of the
        # gateway request payload so downstream authorization sees the
        # real Actor rather than this Lambda's role.
        actor_id=job["actorId"],
        gateway_registry_table=_get_gateway_registry_table(),
        secrets_client=_secrets_client,
        # Only a user_jwt gateway needs this. It is a CAPABILITY scoped to this
        # job, not a credential to the upstream service: the interceptor verifies
        # the signature and derives the actor from it, so actorId above stays
        # untrusted decoration on that path.
        delegation_grant=job.get("delegationGrant", "") or "",
        # Required by a queue status tool alongside the request id; absent for
        # trio tools that take {job_id, __actor_id}.
        endpoint_id=job.get("endpointId", "") or "",
    )


def _invoke_declared_status(job: dict[str, Any]) -> dict[str, Any]:
    """Call an SFN-polled M2C job's shared status tool on its real gateway (resultsGatewayId).

    The poller reads an M2C job's status from DescribeExecution (gatewayId ``__aws_sfn__``), but
    only the status tool DECLARES the job's output files; reconcile folds them in at SUCCEEDED."""
    return invoke_status_tool(
        gateway_id=job.get("resultsGatewayId", ""),
        status_tool=job["statusTool"],
        job_id=job["jobId"],
        actor_id=job.get("actorId", "") or "",
        gateway_registry_table=_get_gateway_registry_table(),
        secrets_client=_secrets_client,
        delegation_grant=job.get("delegationGrant", "") or "",
    )


def _invoke_results(job: dict[str, Any]) -> dict[str, Any]:
    """Fetch a completed queue job's RESULT payload via its resultsTool.

    A fal queue job reports COMPLETED on its status tool (check_job) but the deliverable
    (e.g. video.url) lives in a separate result endpoint reached via get_job_result. Without
    fetching it the terminal write captured no artifactUrls and the video never auto-rendered.
    Invoked only when resultsTool is present (fal); M2C trios have resultsTool="" and skip this.
    Reuses the status invoker: it invokes the named tool with the same queue arg shape
    ({request_id, endpoint_id}).
    """
    return invoke_status_tool(
        gateway_id=job.get("resultsGatewayId") or job.get("gatewayId", ""),
        status_tool=job["resultsTool"],
        job_id=job["jobId"],
        actor_id=job["actorId"],
        gateway_registry_table=_get_gateway_registry_table(),
        secrets_client=_secrets_client,
        delegation_grant=job.get("delegationGrant", "") or "",
        endpoint_id=job.get("endpointId", "") or "",
    )


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    """Classify an exception as transient per the design's TRANSIENT set.

    Match by exception class name (rather than ``isinstance``) so
    boto3's dynamic ``ThrottlingException`` / ``ServiceUnavailable``
    classes — which aren't importable from a fixed module path — are
    matched the same way as :class:`TransientGatewayError`.
    """
    if isinstance(exc, TransientGatewayError):
        return True
    return type(exc).__name__ in _TRANSIENT_EXCEPTION_NAMES


def reconcile(
    job: dict[str, Any],
    response_or_error: dict[str, Any] | BaseException,
    *,
    registry: JobRegistry,
    fetch_results: Any = None,
    fetch_status_outputs: Any = None,
) -> None:
    """Apply one status response (or error) to the registry.

    Mirrors the pseudocode in design.md "Polling_Worker — Error
    handling". Pure dispatch on the response shape:

    * Exception, transient -> no-op (R4.4); next tick retries.
    * Exception, non-transient -> bump consecutiveErrors. If the new
      count >= ``NON_TRANSIENT_THRESHOLD``, set_terminal(FAILED) (R4.5).
    * Successful response with terminal status -> set_terminal(...) (R4.3).
    * Successful response with active status -> set_active_status(...) (R4.2).
    * Successful response with unknown status -> log and treat as a
      non-transient error so the threshold trips eventually rather than
      letting a misbehaving tool spin forever.
    """
    job_id = job["jobId"]

    if isinstance(response_or_error, BaseException):
        if _is_transient(response_or_error):
            logger.info(
                "transient error for job %s; leaving state unchanged: %s",
                job_id,
                response_or_error,
            )
            return
        message = str(response_or_error) or type(response_or_error).__name__
        new_count = registry.bump_error(job_id, message)
        logger.warning(
            "non-transient error for job %s (count=%d/%d): %s",
            job_id,
            new_count,
            NON_TRANSIENT_THRESHOLD,
            message,
        )
        if new_count >= NON_TRANSIENT_THRESHOLD:
            registry.set_terminal(job_id, status="FAILED", error=message)
        return

    # Successful status response.
    new_status = _normalize_status(response_or_error)
    new_error = (response_or_error or {}).get("error")
    if not new_status:
        # Unknown response shape — treat as non-transient so the
        # threshold eventually trips. Same path as a non-transient
        # exception so we don't have a fourth branch.
        # Keys only, never values: a status body can carry signed urls. Naming the
        # shape is what turns the next occurrence into a one-line fix instead of an
        # inference exercise.
        # Keys alone are not enough. An MCP error envelope (isError=True) carries the
        # server's validation message, which is the whole diagnosis -- the previous
        # full-repr message is how the missing endpoint_id/request_id was found, and
        # replacing it with keys-only threw that away on the very next failure. So
        # include the TEXT for an error response, and keys only otherwise: a success
        # body can carry signed urls, an error body carries a reason.
        if isinstance(response_or_error, dict) and response_or_error.get("isError"):
            _detail = ""
            for block in response_or_error.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    _detail = block["text"][:600]
                    break
            message = f"status tool returned an error: {_detail or response_or_error!r}"
        else:
            _shape = (
                sorted(response_or_error.keys())
                if isinstance(response_or_error, dict)
                else type(response_or_error).__name__
            )
            message = f"status response has no recognisable status field; keys={_shape}"
        new_count = registry.bump_error(job_id, message)
        logger.warning(
            "malformed status response for job %s (count=%d/%d)",
            job_id,
            new_count,
            NON_TRANSIENT_THRESHOLD,
        )
        if new_count >= NON_TRANSIENT_THRESHOLD:
            registry.set_terminal(job_id, status="FAILED", error=message)
        return

    # A queue can report COMPLETED for a job whose INFERENCE failed: one job reached
    # terminal SUCCEEDED while its body carried
    #   HTTP 422 - "num_inference_steps" must be <= 8
    # so no audio was ever produced. Recording that as SUCCEEDED told the DAG to proceed,
    # and the dependent merge step blocked on a file that did not exist. A success whose
    # body is an error is a failure, and calling it one here stops it propagating.
    if new_status == "SUCCEEDED":
        _body_error = _result_body_error(response_or_error)
        if _body_error:
            logger.warning(
                "job %s reported SUCCEEDED but its result carries an error: %s",
                job_id, _body_error[:200],
            )
            new_status = "FAILED"
            new_error = new_error or _body_error

    if new_status in _TERMINAL_STATUSES:
        # A queue job reports COMPLETED on its status tool but the deliverable lives in a separate
        # result payload (fal: video.url via get_job_result). Fetch it so artifactUrls is captured
        # and the video auto-renders; fall back to the status body if the fetch fails or none is
        # wired (M2C trios have no resultsTool and keep using the status body).
        result_payload: Any = response_or_error
        if (new_status == "SUCCEEDED" and job.get("resultsTool")
                and fetch_results is not None):
            try:
                fetched = fetch_results()
                if isinstance(fetched, dict) and fetched:
                    result_payload = fetched
            except Exception:  # noqa: BLE001 - result fetch must not block the terminal write
                logger.warning("job %s: results fetch failed; using status body for artifacts",
                               job_id, exc_info=True)
        # An M2C job polled via DescribeExecution: some state machines (shot-metadata-enrichment,
        # conversation-analysis) end with an execution output of only {uuid, total_estimated_cost};
        # their files are declared solely by the shared status tool. Fold those declared outputs in
        # so artifactUrls / result.json name them. Only when the execution output has no output
        # prefix, so the shapes that already echo output.s3uri are byte-identical. One call; a
        # failure keeps the execution output.
        if (new_status == "SUCCEEDED" and fetch_status_outputs is not None
                and job.get("gatewayId") == SFN_GATEWAY_ID and job.get("statusTool")
                and job.get("resultsGatewayId") not in (None, "", SFN_GATEWAY_ID)
                and isinstance(result_payload, dict)
                and isinstance(result_payload.get("executionOutput"), dict)
                and not result_resolution.output_prefix(result_payload["executionOutput"])):
            try:
                merged = result_resolution.merge_status_outputs(
                    result_payload["executionOutput"], fetch_status_outputs())
                if merged is not result_payload["executionOutput"]:
                    result_payload = {**result_payload, "executionOutput": merged}
            except Exception:  # noqa: BLE001 - must not block the terminal write
                logger.warning("job %s: status tool %s failed; keeping the execution output "
                               "without declared outputs", job_id, job.get("statusTool"),
                               exc_info=True)
        renderable = (
            new_status == "SUCCEEDED"
            and has_renderable_artifacts(result_payload)
        )
        artifact_urls = (
            renderable_artifact_urls(result_payload) if renderable else []
        )
        result_key = _publish_job_result(job_id, new_status, result_payload, new_error,
                                         artifact_urls=artifact_urls,
                                         session_id=str(job.get("sessionId") or ""),
                                         project_id=str(job.get("producingProjectId") or ""))
        registry.set_terminal(
            job_id,
            status=new_status,
            error=new_error,
            has_renderable_artifacts=renderable,
            artifact_urls=artifact_urls,
            result_key=result_key,
        )
        logger.info(
            "job %s -> terminal %s (hasRenderableArtifacts=%s, artifactUrls=%d)",
            job_id,
            new_status,
            renderable,
            len(artifact_urls),
        )
        return

    if new_status in _ACTIVE_STATUSES:
        # The expected_status guard prevents racing a terminal write
        # back to active. If the guard fails, another writer already
        # moved the job to a different status; that's a no-op for us.
        committed = registry.set_active_status(
            job_id,
            status=new_status,
            expected_status=job.get("status", "PENDING"),
        )
        if not committed:
            logger.info(
                "job %s active update skipped (concurrent writer changed status)",
                job_id,
            )
        return

    # Unknown status string — same fallthrough as a missing status.
    message = f"unknown status value {new_status!r} for job {job_id}"
    new_count = registry.bump_error(job_id, message)
    logger.warning(
        "unknown status %r for job %s (count=%d/%d)",
        new_status,
        job_id,
        new_count,
        NON_TRANSIENT_THRESHOLD,
    )
    if new_count >= NON_TRANSIENT_THRESHOLD:
        registry.set_terminal(job_id, status="FAILED", error=message)


# ---------------------------------------------------------------------------
# Per-cycle dispatch loop
# ---------------------------------------------------------------------------


def _job_age_seconds(job: dict[str, Any]) -> float | None:
    """Age of a job in seconds from its ``startAt`` (ISO-8601), or None."""
    raw = job.get("startAt") or job.get("updatedAt")
    if not raw:
        return None
    try:
        started = _dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()


def _publish_metrics(counters: dict[str, int]) -> None:
    """Publish resilience metrics for alarms. Best-effort — never fails a cycle."""
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        names = {
            "ActiveJobs": "polled",
            "JobsTimedOutBySweep": "timed_out_by_age",
            "PollTransientErrors": "transient",
            "PollNonTransientErrors": "non_transient",
        }
        _cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {"MetricName": m, "Value": float(counters.get(k, 0)), "Unit": "Count", "Timestamp": now}
                for m, k in names.items()
            ],
        )
    except Exception:  # noqa: BLE001 — metrics are best-effort
        logger.warning("failed to publish job metrics", exc_info=True)


def _poll_cycle(jobs: Iterable[dict[str, Any]], *, registry: JobRegistry) -> dict[str, int]:
    """Run one polling cycle over the supplied iterable of active jobs.

    Returns a small counters dict for CloudWatch metrics. Sequential
    dispatch — see module docstring "Concurrency" for the rationale.
    """
    counters = {"polled": 0, "ok": 0, "transient": 0, "non_transient": 0, "terminal": 0, "timed_out_by_age": 0, "skipped_coordinator": 0}
    for job in jobs:
        job_id = job.get("jobId", "<unknown>")
        # Skip plan-node and plan-parent entries whose lifecycle is managed
        # by the Plan Chainer Lambda and changefeed bridge, NOT polling.
        # These entries use gatewayId = "__coordinator__" and cannot be polled
        # (there is no real gateway behind them). If not skipped, the worker
        # would error out, trip the non-transient threshold, and mark nodes
        # as FAILED prematurely — before the real gateway job even completes.
        gateway_id = job.get("gatewayId", "")
        if gateway_id == "__coordinator__":
            counters["skipped_coordinator"] += 1
            continue
        counters["polled"] += 1
        # Stuck-job sweep (resilience P0): force-terminate over-age jobs
        # regardless of poll outcome, so a genuinely-stuck job (or one whose
        # polls keep failing transiently) can't sit RUNNING until TTL.
        age = _job_age_seconds(job)
        if age is not None and age > MAX_JOB_AGE_SECONDS:
            try:
                registry.set_terminal(
                    job_id,
                    status="TIMED_OUT",
                    error=f"stuck-job sweep: age {age:.0f}s exceeded max {MAX_JOB_AGE_SECONDS}s",
                )
                counters["timed_out_by_age"] += 1
                counters["terminal"] += 1
                logger.warning(
                    "job %s timed out by sweep (age=%.0fs > %ds)",
                    job_id, age, MAX_JOB_AGE_SECONDS,
                )
            except Exception:  # noqa: BLE001 — defensive; keep the cycle going
                logger.exception("failed to sweep stuck job %s", job_id)
            continue
        try:
            response = _invoke_status(job)
            status = (response or {}).get("status")
            if isinstance(status, str) and status in _TERMINAL_STATUSES:
                counters["terminal"] += 1
            counters["ok"] += 1
            reconcile(job, response, registry=registry,
                      fetch_results=(lambda j=job: _invoke_results(j)),
                      fetch_status_outputs=(lambda j=job: _invoke_declared_status(j)))
        except (GatewayInvocationError, ClientError, TimeoutError) as exc:
            if _is_transient(exc):
                counters["transient"] += 1
            else:
                counters["non_transient"] += 1
            reconcile(job, exc, registry=registry)
        except (GatewayDescriptorMissing,) as exc:
            # Already a subclass of GatewayInvocationError; the except
            # clause above catches it. Listed separately for clarity
            # — the descriptor-missing case is the canonical
            # non-transient signal that a gateway was deleted while a
            # job was in flight.
            counters["non_transient"] += 1
            reconcile(job, exc, registry=registry)
        except Exception as exc:  # noqa: BLE001 — fall-through guard
            # Any unexpected exception is logged and reported as a
            # non-transient error so the threshold eventually trips
            # rather than letting one bad job class crash the cycle
            # for everyone.
            logger.exception("unexpected error polling job %s", job_id)
            counters["non_transient"] += 1
            reconcile(job, exc, registry=registry)
    return counters


# EventBridge envelope of a Step Functions execution status change. The rule in
# agent-infra-stack.ts (`JobSfnCompletionRule`) only forwards terminal statuses.
SFN_EVENT_SOURCE = "aws.states"
SFN_EVENT_DETAIL_TYPE = "Step Functions Execution Status Change"


def _is_sfn_status_event(event: Any) -> bool:
    return (
        isinstance(event, dict)
        and event.get("source") == SFN_EVENT_SOURCE
        and event.get("detail-type") == SFN_EVENT_DETAIL_TYPE
    )


def handle_execution_event(event: dict[str, Any], *, registry: JobRegistry) -> dict[str, Any]:
    """Settle ONE job as soon as its Step Functions execution finishes.

    The schedule alone detected an M2C execution ~60s after it ended (staging:
    59.5s / 58.7s), because it runs once a minute. This path removes that wait
    and nothing else: it runs the very same :func:`_poll_cycle` over the one
    job the event names, so status mapping, result fetch, the declared-output
    merge, the result artifact and the terminal write are the poller's code,
    not a copy of it.

    The event is only a TRIGGER. Its ``detail.status`` is not trusted as the
    outcome: the cycle re-reads ``DescribeExecution`` (whose ``output`` is also
    complete, while the event's may be truncated), exactly as a scheduled tick
    would. That is what makes poller + event safe in either order:

    * job unknown (not ours) or already terminal -> no-op, no AWS call beyond
      the one consistent GetItem;
    * both race on an active job -> both compute the same result, and
      ``set_terminal``'s ``status IN (PENDING, RUNNING)`` guard lets exactly one
      write land (the other returns False). The changefeed sees one transition.

    Never raises for an event it cannot use, so EventBridge does not retry it;
    a job this path misses is still settled by the next scheduled tick.
    """
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    execution_arn = detail.get("executionArn") or ""
    if not isinstance(execution_arn, str) or not SFN_EXECUTION_ARN_RE.match(execution_arn):
        logger.info("sfn event ignored: no execution ARN in detail")
        return {"event": "ignored", "reason": "no_execution_arn"}
    job = registry.get_job(execution_arn)
    if job is None:
        # Most executions in the account are not jobs (ingestion, purge, deploy waiters).
        return {"event": "ignored", "reason": "not_a_job"}
    if job.get("status") not in _ACTIVE_STATUSES:
        logger.info("sfn event for %s: job already %s; nothing to do",
                    execution_arn, job.get("status"))
        return {"event": "ignored", "reason": "already_terminal"}
    logger.info("sfn event: %s reported %s; settling job now",
                execution_arn, detail.get("status"))
    counters = _poll_cycle([job], registry=registry)
    return {"event": "settled", **counters}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for both triggers.

    * A Step Functions execution status change (``JobSfnCompletionRule``) ->
      :func:`handle_execution_event` settles that one job immediately.
    * Anything else is the ``Schedule.rate`` tick, whose payload is opaque and
      unused — the schedule is the trigger, the work is whatever's currently in
      the registry. It stays the fallback for every job no event covers (fal and
      other gateway-polled jobs, cross-account M2C, a dropped event).
    """
    if _is_sfn_status_event(event):
        # No _publish_metrics here: ActiveJobs etc. describe a full sweep of the
        # registry, and a one-job count every few seconds would corrupt them.
        return handle_execution_event(event, registry=_get_registry())
    logger.info(
        "polling cycle starting: table=%s index=%s",
        JOB_REGISTRY_TABLE,
        JOB_REGISTRY_POLL_INDEX,
    )
    registry = _get_registry()
    jobs = registry.query_active_jobs()
    counters = _poll_cycle(jobs, registry=registry)
    _publish_metrics(counters)
    logger.info("polling cycle complete: %s", counters)
    return counters
