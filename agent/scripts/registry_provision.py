#!/usr/bin/env python3
"""Provision one environment's AWS Agent Registry (migration Task 8).

There is no CloudFormation/CDK resource for ``agent-registry`` yet, and the
installed ``aws`` CLI v2 does not know the namespace either
(``aws agent-registry-control help`` is a usage error), so provisioning runs
through boto3 here rather than a CLI runbook. ``boto3 >= 1.43.64`` has both
clients; ``agent/src/capability/registry.py`` also carries vendored service
models for environments whose botocore predates the namespace.

Idempotent by name: a registry that already exists is reported and left alone,
so this can be re-run safely as part of bringing up a new environment.

Safety (R16):

* Dry run is the default. Writes need ``--execute``.
* ``--env`` is required and must match the registry name suffix.
* ``prod`` additionally requires ``--i-understand-this-is-production``, and
  even then this script only *creates* — it never deletes a registry, never
  deletes a record, and never relaxes an existing approval configuration.

Usage::

    # inspect (default)
    uv run python scripts/registry_provision.py --env sandbox

    # create
    uv run python scripts/registry_provision.py --env sandbox --execute

    # print the CDK/ECS configuration to wire back
    uv run python scripts/registry_provision.py --env sandbox --output-config
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from capability import registry as registry_client  # noqa: E402

#: One registry per environment — never a shared cross-environment registry
#: (design "Registry organization"). The ``-<env>`` suffix is also what
#: ``capability.config`` checks to fail closed on cross-environment config.
NAME_TEMPLATE = "mediaworks-harness-capabilities-{env}"

ENVIRONMENTS = {
    "sandbox": "100000000001",
    "staging": "100000000002",
    "prod": "100000000003",
}

REGION = "us-west-2"

#: Auto-approval. Canonical publish authorization, harness-shared eligibility,
#: descriptor validation, and the projector's scope re-check remain the
#: governance gates — ``APPROVE_ALL`` only removes a human click that would
#: otherwise sit between a canonical publish and its discovery projection (R7).
AUTO_APPROVAL_RULES = ["APPROVE_ALL"]


def registry_name(env: str) -> str:
    return NAME_TEMPLATE.format(env=env)


def find_registry(control: Any, name: str) -> Optional[dict[str, Any]]:
    """Locate a registry by name, paging the full list."""
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 50}
        if next_token:
            kwargs["nextToken"] = next_token
        response = control.list_registries(**kwargs)
        for item in response.get("registries", []):
            if item.get("name") == name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


def wait_until_ready(control: Any, registry_id: str, *, timeout: int = 300) -> dict[str, Any]:
    """Poll ``GetRegistry`` until it leaves ``CREATING``.

    Returned rather than merely awaited: the caller needs the final status to
    decide whether to print configuration or an error, and a registry stuck in
    ``CREATE_FAILED`` must not be reported as provisioned.
    """
    deadline = time.time() + timeout
    while True:
        response = control.get_registry(registryId=registry_id)
        status = response.get("status")
        if status != "CREATING":
            return response
        if time.time() > deadline:
            raise TimeoutError(
                f"registry {registry_id} still CREATING after {timeout}s"
            )
        time.sleep(5)


def verify_caller(env: str) -> str:
    """Confirm the ambient credentials belong to ``env``'s account.

    The check is the difference between "this did nothing" and "this created a
    registry in the wrong account" — the two failure modes are indistinguishable
    from the script's own output otherwise (Property 7).
    """
    import boto3

    identity = boto3.client("sts", region_name=REGION).get_caller_identity()
    account = identity["Account"]
    expected = ENVIRONMENTS[env]
    if account != expected:
        raise SystemExit(
            f"refusing to act: --env {env} expects account {expected}, but the "
            f"current credentials are account {account} ({identity['Arn']})"
        )
    return account


def config_snippet(env: str, registry: dict[str, Any]) -> str:
    """The values to wire into env config / task definitions (Task 10)."""
    return json.dumps(
        {
            "env": env,
            "REGISTRY_ID": registry["registryId"],
            "REGISTRY_ARN": registry["registryArn"],
            "REGISTRY_NAME": registry["name"],
            "REGISTRY_PROJECTION_ENABLED": "true",
        },
        indent=2,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument("--region", default=REGION)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the create; omit for a dry run (the default)",
    )
    parser.add_argument(
        "--i-understand-this-is-production",
        action="store_true",
        help="required in addition to --execute when --env prod",
    )
    parser.add_argument(
        "--output-config",
        action="store_true",
        help="print the REGISTRY_* configuration for an existing registry",
    )
    args = parser.parse_args(argv)

    name = registry_name(args.env)
    if args.env == "prod" and args.execute and not args.i_understand_this_is_production:
        raise SystemExit(
            "production writes require --i-understand-this-is-production "
            "alongside --execute (R16)"
        )

    account = verify_caller(args.env)
    control = registry_client.control_client(args.region)

    existing = find_registry(control, name)
    if existing:
        detail = control.get_registry(registryId=existing["registryId"])
        rules = (detail.get("approvalConfiguration") or {}).get("autoApprovalRules") or []
        print(
            f"registry already exists: {name}\n"
            f"  registryId : {detail['registryId']}\n"
            f"  registryArn: {detail['registryArn']}\n"
            f"  status     : {detail['status']}\n"
            f"  autoApproval: {rules or '(none — records need manual review)'}"
        )
        if not rules:
            # Not auto-fixed: widening approval on an existing registry is a
            # governance change, not provisioning, and belongs in a reviewed
            # change rather than a re-run of this script.
            print(
                "\nWARNING: autoApprovalRules is empty. Projected records will "
                "sit in PENDING_APPROVAL. Enabling APPROVE_ALL on an existing "
                "registry is a deliberate governance change — make it "
                "explicitly, not as a side effect of provisioning."
            )
        if args.output_config:
            print("\n" + config_snippet(args.env, detail))
        return 0

    print(
        f"{'CREATING' if args.execute else 'DRY RUN — would create'} registry\n"
        f"  name    : {name}\n"
        f"  account : {account}\n"
        f"  region  : {args.region}\n"
        f"  approval: {AUTO_APPROVAL_RULES}"
    )
    if not args.execute:
        print("\nRe-run with --execute to create it.")
        return 0

    created = control.create_registry(
        name=name,
        description=(
            f"Harness-shared capability discovery catalog ({args.env}). "
            "Canonical state lives in DynamoDB/S3; this is a projection."
        ),
        approvalConfiguration={"autoApprovalRules": AUTO_APPROVAL_RULES},
        tags={
            "Project": "mediaworks-harness",
            "Environment": args.env,
            "ManagedBy": "scripts/registry_provision.py",
        },
    )
    registry_arn = created["registryArn"]
    registry_id = registry_client.record_id_from_arn(registry_arn)
    print(f"created {registry_arn}; waiting for READY...")
    final = wait_until_ready(control, registry_id)
    if final.get("status") != "READY":
        raise SystemExit(
            f"registry did not reach READY: status={final.get('status')} "
            f"reason={final.get('statusReason')}"
        )
    print("READY.\n")
    print(config_snippet(args.env, final))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
