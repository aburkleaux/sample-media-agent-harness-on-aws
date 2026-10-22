"""CloudFormation custom-resource handler: create-if-absent the AWS Agent Registry by name.

The Agent Registry is the ONE object with no CloudFormation resource type (see AGENTS.md),
so a self-contained wrapper template provisions it through this custom resource instead of a
native resource. Behavior mirrors ``agent/scripts/registry_provision.py``:

  * Idempotent BY NAME — an existing registry of the same name is reused, never duplicated.
  * RETAINED on delete — deleting the registry would orphan every projected record, so the
    Delete event is a deliberate no-op (the registry outlives the stack).

Returns ``RegistryId`` so the wrapper can pass it to the harness (``agentRegistry.registryId``)
and to M2C wiring without a hardcoded, pre-provisioned id.

The CDK construct bundles the vendored service models and sets AWS_DATA_PATH.
"""
from __future__ import annotations

import hashlib
import time

import boto3


def _control(region: str | None = None):
    return boto3.client("agent-registry-control", region_name=region)


def _find_by_name(control, name: str):
    """Locate a registry by name, paging the full list (mirrors registry_provision)."""
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        resp = control.list_registries(**kwargs)
        for r in resp.get("registries", []) or resp.get("registrySummaries", []):
            if r.get("name") == name:
                return r
        token = resp.get("nextToken")
        if not token:
            return None


def on_event(event, _context):
    request_type = event["RequestType"]
    name = event["ResourceProperties"]["RegistryName"]

    # RETAIN: never delete the registry (it would orphan projected records). No-op.
    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId") or name}

    control = _control()

    # Create / Update: create-if-absent by name.
    existing = _find_by_name(control, name)
    if existing:
        registry_id = existing["registryId"]
    else:
        created = control.create_registry(
            name=name,
            clientToken=hashlib.sha256((event["StackId"] + name + event["RequestId"]).encode()).hexdigest(),
            approvalConfiguration={"autoApprovalRules": ["APPROVE_ALL"]},
        )
        registry_id = created["registryArn"].rsplit("/", 1)[-1]
    # Both reused and newly created registries must be READY before consumers start.
    for _ in range(45):
        detail = control.get_registry(registryId=registry_id)
        status = detail.get("status")
        if status == "READY":
            break
        if status != "CREATING":
            raise RuntimeError(f"Registry {registry_id}: {status}: {detail.get('statusReason', '')}")
        time.sleep(5)
    else:
        raise TimeoutError(f"Registry {registry_id} did not become READY")

    return {"PhysicalResourceId": registry_id, "Data": {"RegistryId": registry_id}}
