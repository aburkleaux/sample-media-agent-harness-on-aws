#!/usr/bin/env python3
"""Re-synchronize and verify the Search_Gateway's M2C target (media-harness-projects-access, design
Component 6 "Re-sync command"; task 8.13).

Shares the sync-and-verify logic with the ``SearchTargetSync`` custom resource
(``infra/lib/lambdas/search-target-sync/sync.py``). It re-introspects an ALREADY-DEPLOYED target and
verifies the one permitted tool; it touches neither the Cedar policy nor the gateway authorizer. If M2C
renames ``search_content``, the verify step fails loudly here instead of leaving a permit that matches
nothing (Req 5.9, 5.10).

Dry run is the default (read + verify only). ``--execute`` additionally calls
``SynchronizeGatewayTargets`` first (idempotent re-introspection — not a destructive change).

Usage::

    # inspect (default): verify the current synchronized tool list
    uv run python scripts/search_gateway_sync.py --env sandbox

    # re-synchronize, then verify
    uv run python scripts/search_gateway_sync.py --env sandbox --execute

Exit codes: 0 on success; non-zero naming the failed step, missing tool, or missing property.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# Share the Lambda's sync/verify module (no duplicate logic).
_SYNC_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "infra", "lib", "lambdas", "search-target-sync"
)
sys.path.insert(0, os.path.abspath(_SYNC_DIR))

import sync as sync_mod  # noqa: E402  (path adjusted above)

PROJECT_NAME = os.environ.get("PROJECT_NAME", "mediaworks-harness")

#: account per env, mirroring agent/scripts/registry_provision.py; refuses a wrong-account run.
ENVIRONMENTS = {
    "sandbox": "100000000001",
    "staging": "100000000002",
    "prod": "100000000003",
}


def _gateway_and_target(control, gateway_name: str, target_name: str) -> tuple[str, str]:
    """Resolve (gatewayId, targetId) by name via the bedrock-agentcore control plane."""
    gateway_id = None
    paginator_args = {}
    while True:
        resp = control.list_gateways(**paginator_args)
        for gw in resp.get("items", resp.get("gateways", [])):
            if gw.get("name") == gateway_name:
                gateway_id = gw.get("gatewayId") or gw.get("gatewayIdentifier")
                break
        token = resp.get("nextToken")
        if gateway_id or not token:
            break
        paginator_args = {"nextToken": token}
    if not gateway_id:
        raise sync_mod.SyncError("resolve", f"Search_Gateway '{gateway_name}' not found")

    target_id = None
    paginator_args = {"gatewayIdentifier": gateway_id}
    while True:
        resp = control.list_gateway_targets(**paginator_args)
        for tgt in resp.get("items", resp.get("targets", [])):
            if tgt.get("name") == target_name:
                target_id = tgt.get("targetId") or tgt.get("targetIdentifier")
                break
        token = resp.get("nextToken")
        if target_id or not token:
            break
        paginator_args = {"gatewayIdentifier": gateway_id, "nextToken": token}
    if not target_id:
        raise sync_mod.SyncError("resolve", f"target '{target_name}' not found on {gateway_name}")
    return gateway_id, target_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--execute", action="store_true",
        help="call SynchronizeGatewayTargets before verifying (default: verify only)",
    )
    args = parser.parse_args(argv)

    try:
        import boto3
    except ImportError:
        print("boto3 is required", file=sys.stderr)
        return 2

    expected_account = ENVIRONMENTS[args.env]
    try:
        account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001
        print(f"could not determine the caller account: {exc}", file=sys.stderr)
        return 2
    if account != expected_account:
        print(
            f"refusing to run: --env {args.env} expects account {expected_account}, "
            f"but the ambient credentials are {account}",
            file=sys.stderr,
        )
        return 2

    gateway_name = f"{PROJECT_NAME}-search-gateway"
    target_name = f"{PROJECT_NAME}-m2c-search"
    control = boto3.client("bedrock-agentcore-control", region_name=args.region)

    try:
        gateway_id, target_id = _gateway_and_target(control, gateway_name, target_name)
    except sync_mod.SyncError as err:
        print(f"FAILED [{err.step}]: {err}", file=sys.stderr)
        return 1

    base = f"https://bedrock-agentcore-control.{args.region}.amazonaws.com/gateways/{gateway_id}/targets"

    def synchronize() -> None:
        if args.execute:
            control.synchronize_gateway_targets(
                gatewayIdentifier=gateway_id, targetIdList=[target_id]
            )

    def get_target() -> dict:
        return control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)

    def list_tools() -> list[dict]:
        target = get_target()
        return target.get("toolSchema", {}).get("tools", []) or target.get("tools", []) or []

    # Dry run must not require a fresh sync, so accept any already-recorded lastSynchronizedAt.
    started_at = (
        datetime.now(timezone.utc) if args.execute else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )

    try:
        result = sync_mod.sync_and_verify(
            synchronize=synchronize,
            get_target=get_target,
            list_tools=list_tools,
            target_name=target_name,
            started_at=started_at,
        )
    except sync_mod.SyncError as err:
        print(f"FAILED [{err.step}]: {err}", file=sys.stderr)
        return 1

    mode = "executed sync" if args.execute else "dry run (verify only)"
    print(f"Search_Gateway target OK ({mode}) [{base}]")
    print(f"  SearchToolName:     {result['SearchToolName']}")
    print(f"  ToolCount:          {result['ToolCount']}")
    print(f"  LastSynchronizedAt: {result['LastSynchronizedAt']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
