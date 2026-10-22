"""Async job tracking package.

Houses the Job_Detector, the JobRegistry boto3 wrapper, the per-tool
job-tracking wrap (see :mod:`agent.src.jobs.strands_tool_wrap`), and the
``get_job`` built-in tool. This module re-exports the shared data
models so call sites can import from ``jobs`` without reaching into the
``models`` submodule.

Sentinel constants
------------------
:data:`SFN_GATEWAY_ID` is the cross-cutting sentinel ``gateway_id`` value
that flags an Async_Job for the Step Functions ``DescribeExecution``
direct-API path inside the polling worker (task 7.2). It lives at package
scope rather than inside any individual module because both the
registration path (the Strands tool wrap, via
``_resolve_gateway_id_for_job``) and any future registration code paths
need to share the exact same string. The polling worker keeps a
duplicate copy in ``infra/lib/lambdas/polling-worker/handler.py`` because
it ships as a separate Lambda artifact with no shared module surface to
the runtime; the two MUST stay in sync.

Requirements: 3.1, 4.1, 4.2, 5.1
"""

from __future__ import annotations

# Sentinel ``gateway_id`` value that routes a job to the SFN direct
# path in the polling worker. Mirrors the constant of the same name in
# ``infra/lib/lambdas/polling-worker/handler.py``. Namespaced with
# double underscores so it cannot collide with a user-defined gateway
# id (gateway ids are admin-supplied human strings).
SFN_GATEWAY_ID = "__aws_sfn__"

from .models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    JobRecord,
    JobStatus,
    JobTrio,
)

__all__ = [
    "ACTIVE_STATUSES",
    "JobRecord",
    "JobStatus",
    "JobTrio",
    "SFN_GATEWAY_ID",
    "TERMINAL_STATUSES",
]
