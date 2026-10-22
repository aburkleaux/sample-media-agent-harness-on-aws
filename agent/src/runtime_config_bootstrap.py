"""Hydrate os.environ from a Secrets Manager config blob BEFORE any first-party import.

AgentCore Runtime V2 caps the env-var payload at 1536 bytes. The deploy stores the bulk of
static config (ARNs, bucket/table names, endpoints, Cognito client secrets) in ONE Secrets
Manager secret and leaves only AWS_REGION + RUNTIME_CONFIG_SECRET_ARN + DEPLOY_ENV in the
runtime env. This fetches the secret once at process start and os.environ.setdefault()s each
key, so every downstream os.environ.get(...) works unchanged.

MUST be the FIRST first-party import in every entrypoint, before any module that reads env at
import (security.identity, mcp_manager, tools.crop_video ...). SELF-CONTAINED: stdlib + boto3
only; imports NO first-party module. Snapshot-safe: deployment-static values, runs before
app.run(). No-op (and fail-open) when the pointer is unset or unreadable, so local dev / tests
and the agent's own fail-closed checks are unaffected.
"""
from __future__ import annotations

import json
import os


def _sm_client(region: str):
    # Local import keeps the module importable without boto3 present (tests patch this helper).
    import boto3
    return boto3.client("secretsmanager", region_name=region)


def _ssm_client(region: str):
    import boto3
    return boto3.client("ssm", region_name=region)


def _load_runtime_arns(region: str) -> int:
    """setdefault each SSM parameter under RUNTIME_ARN_PARAM_PATH into os.environ, keyed by its last
    path segment (``<path>/TASK_AGENT_RUNTIME_ARN`` → ``TASK_AGENT_RUNTIME_ARN``).

    The sub-agent ARNs are SSM parameters rather than runtime env so the runtimes do not depend on
    each other in CloudFormation (they update in parallel). Best-effort: on a brand-new environment
    the parameters may not exist yet, so ``runtime_arns.get`` re-reads them lazily at call time.
    """
    path = os.environ.get("RUNTIME_ARN_PARAM_PATH", "").strip().rstrip("/")
    if not path:
        return 0
    n = 0
    try:
        client = _ssm_client(region)
        kwargs = {"Path": path, "Recursive": False}
        while True:
            resp = client.get_parameters_by_path(**kwargs)
            for p in resp.get("Parameters") or []:
                name, value = str(p.get("Name") or ""), p.get("Value")
                key = name.rsplit("/", 1)[-1]
                if key and value and key not in os.environ:
                    os.environ[key] = str(value)
                    n += 1
            token = resp.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
    except Exception as e:  # noqa: BLE001 — fail open; resolved lazily at call time instead
        print(f"runtime_config_bootstrap: could not read runtime ARNs under {path}: {e}", flush=True)
    return n


def load() -> int:
    """Fetch the config secret and setdefault each key into os.environ, then the runtime-ARN
    SSM parameters (when RUNTIME_ARN_PARAM_PATH is set).

    Returns the count of keys newly populated (0 when nothing is configured or the fetch fails).
    Never raises in a way that would crash import — a missing/unreadable secret logs to stderr
    and leaves env as-is (fail-open; the agent's existing fail-closed checks still guard required
    keys).
    """
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
    n = _load_secret(region)
    return n + _load_runtime_arns(region)


def _load_secret(region: str) -> int:
    arn = os.environ.get("RUNTIME_CONFIG_SECRET_ARN", "").strip()
    if not arn:
        return 0
    try:
        raw = _sm_client(region).get_secret_value(SecretId=arn)["SecretString"]
        cfg = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — never block startup; fail open to existing env
        print(f"runtime_config_bootstrap: could not load {arn}: {e}", flush=True)
        return 0
    if not isinstance(cfg, dict):
        print(f"runtime_config_bootstrap: {arn} is not a JSON object; ignoring", flush=True)
        return 0
    n = 0
    for k, v in cfg.items():
        if v is None:
            continue
        key = str(k)
        if key not in os.environ:  # setdefault semantics — never override env / .env / reserved vars
            os.environ[key] = str(v)
            n += 1
    return n


load()  # run on import so a bare `import runtime_config_bootstrap` hydrates env before later imports
