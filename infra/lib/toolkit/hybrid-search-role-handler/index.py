"""CloudFormation custom resource: the media toolkit's hybrid-search indexer role, by exact ARN.

The indexer embeds an image asset's Search_Rendition straight from the shared Ingest bucket
(``projects/<pid>/<assetId>/rendition.jpg``), but its identity policy only covers the toolkit's own
proxy bucket, so the Ingest bucket's policy must name the role. The toolkit exposes no Output for it,
so it is resolved here from the stack tree at deploy time:

    toolkit stack -> nested ``HybridSearchStack`` -> ``AWS::IAM::Role`` ``HybridSearchLambdaRole``
                  -> iam:GetRole (the ARN carries the toolkit's ``/umt-<id>/`` path)

``ToolkitStackName`` names the toolkit stack — or, with ``NestedLogicalId``, the PARENT stack whose
nested resource of that logical id is the toolkit. If the toolkit's template ever moves the role, the
role is also accepted directly in the toolkit stack or in any one other nested stack; zero or several
matches fail the deploy with a message naming what was searched (a silent skip would only re-create
the "images never index" bug). Delete is a no-op: this resource reads, it owns nothing.
"""
from __future__ import annotations

import boto3

DEFAULT_NESTED_STACK = "HybridSearchStack"
DEFAULT_ROLE = "HybridSearchLambdaRole"
_GONE = {"DELETE_COMPLETE", "DELETE_IN_PROGRESS"}


def _resources(cfn, stack: str) -> list[dict]:
    out: list[dict] = []
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        out.extend(r for r in page["StackResourceSummaries"] if r.get("ResourceStatus") not in _GONE)
    return out


def _role_in(resources: list[dict], role_logical_id: str) -> str | None:
    for r in resources:
        if r["LogicalResourceId"] == role_logical_id and r["ResourceType"] == "AWS::IAM::Role":
            if not r.get("PhysicalResourceId"):
                raise RuntimeError(f"{role_logical_id} exists but has no physical id yet ({r.get('ResourceStatus')})")
            return r["PhysicalResourceId"]
    return None


def find_role_name(cfn, toolkit_stack: str, nested_stack_logical_id: str, role_logical_id: str) -> str:
    top = _resources(cfn, toolkit_stack)
    direct = _role_in(top, role_logical_id)
    if direct:
        return direct
    nested = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in top
              if r["ResourceType"] == "AWS::CloudFormation::Stack" and r.get("PhysicalResourceId")}
    if nested_stack_logical_id in nested:
        found = _role_in(_resources(cfn, nested[nested_stack_logical_id]), role_logical_id)
        if found:
            return found
    # The expected nesting moved: accept exactly one nested stack that carries the role.
    hits = []
    for logical_id, stack_id in sorted(nested.items()):
        if logical_id == nested_stack_logical_id:
            continue
        name = _role_in(_resources(cfn, stack_id), role_logical_id)
        if name:
            hits.append((logical_id, name))
    if len(hits) == 1:
        return hits[0][1]
    if hits:
        raise RuntimeError(
            f"toolkit stack {toolkit_stack}: {role_logical_id} is in several nested stacks "
            f"({', '.join(h[0] for h in hits)}); cannot tell which is the hybrid-search indexer")
    raise RuntimeError(
        f"toolkit stack {toolkit_stack} has no AWS::IAM::Role {role_logical_id!r} (looked in the stack, in "
        f"nested {nested_stack_logical_id!r} and in {sorted(nested) or 'no other nested stacks'}). "
        "Image assets cannot be indexed without granting it the Ingest bucket's renditions; "
        "has the toolkit template changed?")


def on_event(event, _context):
    props = event["ResourceProperties"]
    physical_id = event.get("PhysicalResourceId") or "toolkit-hybrid-search-role"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical_id}

    cfn = boto3.client("cloudformation")
    stack = props["ToolkitStackName"]
    if props.get("NestedLogicalId"):
        detail = cfn.describe_stack_resource(StackName=stack, LogicalResourceId=props["NestedLogicalId"])
        stack = detail["StackResourceDetail"]["PhysicalResourceId"]
    role_name = find_role_name(
        cfn, stack,
        props.get("NestedStackLogicalId") or DEFAULT_NESTED_STACK,
        props.get("RoleLogicalId") or DEFAULT_ROLE,
    )
    role = boto3.client("iam").get_role(RoleName=role_name)["Role"]
    return {"PhysicalResourceId": physical_id, "Data": {"RoleArn": role["Arn"], "RoleName": role["RoleName"]}}
