"""CloudFormation custom resource: read a deployed media toolkit's wiring at deploy time.

The deploy-time equivalent of ``infra/scripts/harvest-media-toolkit-config.sh``: the toolkit
stack's Outputs (gateway URL, credentials secret ARN, proxy bucket, ingest read roles) plus the
non-secret fields of its credentials bundle (``clientId``, ``tokenEndpoint``, ``scope``). Also
derives the Cognito OIDC discovery URL the Search_Gateway's outbound credential provider needs,
which cannot be computed from the token endpoint by string manipulation (the hosted-UI domain
does not encode the user-pool id).

The client secret itself NEVER leaves this function: only the secret's ARN is returned, exactly
like the harvest script.

``ToolkitStackName`` names the toolkit stack — or, with ``NestedLogicalId``, the PARENT stack whose
nested resource of that logical id is the toolkit (how MediaToolkitStack deploys it). Delete is a
no-op: this resource reads, it owns nothing.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import boto3

_USABLE = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE"}
_DEFAULT_SCOPE = "media-workflows/invoke"


def _resolve_stack(cfn, name: str, nested_logical_id: str | None) -> str:
    if not nested_logical_id:
        return name
    detail = cfn.describe_stack_resource(StackName=name, LogicalResourceId=nested_logical_id)
    return detail["StackResourceDetail"]["PhysicalResourceId"]


def _discovery_url(cognito, token_endpoint: str) -> str:
    """Token endpoint ``https://<domain>/oauth2/token`` -> the user pool's IdP discovery URL."""
    host = urlparse(token_endpoint).hostname or ""
    # A Cognito-hosted domain is looked up by its prefix; a custom domain by its full name.
    domain = host.split(".", 1)[0] if host.endswith(".amazoncognito.com") else host
    if not domain:
        raise RuntimeError(f"cannot parse a Cognito domain from tokenEndpoint {token_endpoint!r}")
    desc = cognito.describe_user_pool_domain(Domain=domain).get("DomainDescription") or {}
    pool_id = desc.get("UserPoolId")
    if not pool_id:
        raise RuntimeError(f"Cognito domain {domain!r} (from tokenEndpoint) resolves to no user pool")
    region = pool_id.split("_", 1)[0]
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"


def on_event(event, _context):
    props = event["ResourceProperties"]
    physical_id = event.get("PhysicalResourceId") or f"toolkit-lookup-{props['ToolkitStackName']}"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical_id}

    cfn = boto3.client("cloudformation")
    stack_id = _resolve_stack(cfn, props["ToolkitStackName"], props.get("NestedLogicalId") or None)
    stack = cfn.describe_stacks(StackName=stack_id)["Stacks"][0]
    status = stack.get("StackStatus", "")
    if status not in _USABLE:
        raise RuntimeError(f"toolkit stack {stack_id} is {status}; expected one of {sorted(_USABLE)}")
    outputs = {o["OutputKey"]: o.get("OutputValue", "") for o in stack.get("Outputs", [])}
    missing = [k for k in ("GatewayUrl", "GatewayCredentialsSecretArn", "ProxyBucketName") if not outputs.get(k)]
    if missing:
        raise RuntimeError(f"toolkit stack {stack_id} has no {', '.join(missing)} output; is it the toolkit bundle?")

    secret_arn = outputs["GatewayCredentialsSecretArn"]
    bundle = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
    )
    client_id = bundle.get("clientId")
    token_endpoint = bundle.get("tokenEndpoint")
    if not client_id or not token_endpoint:
        raise RuntimeError(f"credentials secret {secret_arn} lacks clientId/tokenEndpoint")

    role_arns = [a.strip() for a in outputs.get("IngestBucketReadRoleArns", "").split(",") if a.strip()]
    if not role_arns:
        # The harness feeds this into a bucket-policy ArnEquals condition via Fn::Split; an empty
        # string would split to [""], which S3 rejects. A well-formed ARN under the
        # service-linked-role path, which only AWS services can create roles in, matches nobody.
        partition, account = stack["StackId"].split(":")[1], stack["StackId"].split(":")[4]
        role_arns = [f"arn:{partition}:iam::{account}:role/aws-service-role/no-media-toolkit-readers"]
    return {
        "PhysicalResourceId": physical_id,
        "Data": {
            "ToolkitStackId": stack["StackId"],
            "GatewayUrl": outputs["GatewayUrl"],
            "SecretArn": secret_arn,
            "ProxyBucket": outputs["ProxyBucketName"],
            "IngestReadRoleArns": ",".join(role_arns),
            "ClientId": client_id,
            "TokenEndpoint": token_endpoint,
            "Scope": bundle.get("scope") or _DEFAULT_SCOPE,
            "OidcDiscoveryUrl": _discovery_url(boto3.client("cognito-idp"), token_endpoint),
        },
    }
