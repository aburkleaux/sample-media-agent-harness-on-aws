"""Custom Resource Lambda for creating Gateway MCP Server Target.

Uses raw HTTP API with SigV4 signing because the Lambda runtime's boto3
doesn't have mcpServer in its service model yet.
"""

import json
import logging
import time
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

session = boto3.Session()


def get_region():
    return session.region_name or "us-west-2"


def signed_request(method, url, body=None):
    """Make a SigV4-signed request to the AgentCore control plane."""
    region = get_region()
    credentials = session.get_credentials().get_frozen_credentials()

    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None

    request = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)

    req = urllib.request.Request(
        url=url,
        data=data,
        headers=dict(request.headers),
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode()
            return SimpleResponse(resp.status, resp_body)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        return SimpleResponse(e.code, body_text)


class SimpleResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text) if self.text else {}


def handler(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    logger.info("Request type: %s", request_type)

    try:
        if request_type == "Create":
            return handle_create(props)
        elif request_type == "Update":
            return handle_update(event, props)
        elif request_type == "Delete":
            return handle_delete(event, props)
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        raise


def wait_for_runtime(runtime_id, max_wait=180):
    """Wait for runtime to be in ACTIVE state."""
    logger.info("Waiting for runtime %s to be ready...", runtime_id)
    client = boto3.client("bedrock-agentcore-control")
    start = time.time()
    while time.time() - start < max_wait:
        try:
            resp = client.get_agent_runtime(agentRuntimeId=runtime_id)
            status = resp.get("status", "")
            logger.info("Runtime status: %s", status)
            if status == "ACTIVE":
                return True
        except Exception as e:
            logger.warning("Error checking runtime: %s", e)
        time.sleep(15)
    return False


def handle_create(props):
    region = get_region()
    gateway_id = props["GatewayId"]
    runtime_id = props["RuntimeId"]
    target_name = props["TargetName"]
    endpoint = props["Endpoint"]
    cred_provider_arn = props["CredProviderArn"]
    scopes = json.loads(props.get("Scopes", "[]"))

    # Wait for runtime. An external MCP-server target (e.g. the Search target pointing at M2C's
    # gateway) has no runtime: RuntimeId is "". get_agent_runtime("") fails validation on every
    # attempt, so the old loop burned the full 180s on every deploy for nothing.
    if runtime_id and not wait_for_runtime(runtime_id):
        logger.warning("Runtime not ready after waiting, attempting anyway")

    # Build request body
    cred_config = {
        "credentialProviderType": "OAUTH",
        "credentialProvider": {
            "oauthCredentialProvider": {
                "providerArn": cred_provider_arn,
            }
        }
    }
    if scopes:
        cred_config["credentialProvider"]["oauthCredentialProvider"]["scopes"] = scopes

    body = {
        "name": target_name,
        "description": "AWS MCP Server proxy via AgentCore Runtime",
        "credentialProviderConfigurations": [cred_config],
        "targetConfiguration": {
            "mcp": {
                "mcpServer": {
                    "endpoint": endpoint,
                }
            }
        }
    }

    url = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}/targets"
    logger.info("Creating target at: %s", url)

    resp = signed_request("POST", url, body)
    logger.info("Response: %s %s", resp.status_code, resp.text[:500])

    if resp.status_code not in (200, 201, 202):
        raise Exception(f"Failed to create target: {resp.status_code} {resp.text}")

    result = resp.json()
    target_id = result.get("targetId", target_name)
    logger.info("Created target: %s", target_id)

    # Wait for sync
    for attempt in range(5):
        time.sleep(30)
        try:
            check_url = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}/targets/{target_id}"
            check_resp = signed_request("GET", check_url)
            if check_resp.status_code == 200:
                target_body = check_resp.json()
                status = target_body.get("status", "")
                logger.info(
                    "Target status (attempt %d): %s reasons=%s",
                    attempt + 1, status,
                    target_body.get("statusReasons") or target_body.get("statusReason"),
                )
                if status in ("READY", "ACTIVE"):
                    break
                elif status in ("FAILED", "SYNCHRONIZE_UNSUCCESSFUL"):
                    # Retry sync — the MCP runtime may have needed a cold start
                    logger.info("Target sync failed (reasons=%s), retrying sync...",
                                target_body.get("statusReasons") or target_body.get("statusReason"))
                    sync_url = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}/synchronizeTargets"
                    sync_resp = signed_request("PUT", sync_url, {"targetIdList": [target_id]})
                    logger.info("Sync retry response: %s", sync_resp.status_code)
        except Exception as e:
            logger.warning("Error checking target: %s", e)

    return {
        "PhysicalResourceId": target_id,
        "Data": {"TargetId": target_id},
    }


def handle_update(event, props):
    region = get_region()
    old_props = event.get("OldResourceProperties", {})
    target_id = event.get("PhysicalResourceId", "")
    gateway_id = props["GatewayId"]

    # If the backend identity changed (different endpoint / gateway / name), the
    # target must be recreated. Otherwise (e.g. only ToolsHash changed because the
    # MCP server's tool set was renamed/added) re-introspect the EXISTING target
    # in place via SynchronizeGatewayTargets — no destructive delete+create, so
    # the target id and any dependents stay stable.
    # `Revision` is an explicit recreate signal. A target that reached a terminal
    # FAILED status cannot be repaired by SynchronizeGatewayTargets -- re-introspection
    # operates on a target the service already considers broken. Bumping Revision is
    # how we say "rebuild this", without having to rename the target (which would
    # change its tool prefix and break every skill that references it).
    identity_changed = (
        not target_id
        or props.get("Endpoint") != old_props.get("Endpoint")
        or props.get("TargetName") != old_props.get("TargetName")
        or props.get("GatewayId") != old_props.get("GatewayId")
        or props.get("Revision") != old_props.get("Revision")
    )
    if identity_changed:
        logger.info("Target identity changed — recreating target")
        # Delete the CURRENT target from the gateway it ACTUALLY lives on. On a plane MOVE
        # (GatewayId changed, e.g. tenant -> platform) that is the OLD gateway. handle_delete
        # otherwise defaults to the NEW props' GatewayId, so the stale target would linger on
        # the previous gateway and surface the tool on BOTH gateways — a fail-closed collision
        # in the runtime's find_gateway_for_tool. Pass the old GatewayId explicitly.
        old_gateway_id = old_props.get("GatewayId") or gateway_id
        handle_delete(event, {**props, "GatewayId": old_gateway_id})
        return handle_create(props)

    logger.info("Tools changed (hash) — synchronizing target %s in place", target_id)
    sync_url = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}/synchronizeTargets"
    sync_resp = signed_request("PUT", sync_url, {"targetIdList": [target_id]})
    logger.info("Synchronize response: %s %s", sync_resp.status_code, sync_resp.text[:300])

    # Wait for the re-introspection to settle so the new tool schema is live
    # by the time the stack update completes.
    for attempt in range(6):
        time.sleep(20)
        try:
            check_url = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}/targets/{target_id}"
            check_resp = signed_request("GET", check_url)
            if check_resp.status_code == 200:
                status = check_resp.json().get("status", "")
                logger.info("Target status after sync (attempt %d): %s", attempt + 1, status)
                if status in ("READY", "ACTIVE"):
                    break
        except Exception as e:
            logger.warning("Error checking target after sync: %s", e)

    return {
        "PhysicalResourceId": target_id,
        "Data": {"TargetId": target_id},
    }


def wait_for_target_deleted(region, gateway_id, target_id, max_wait=180, interval=5, sleep=time.sleep):
    """Poll GetGatewayTarget until it 404s. Returns True once the target is gone.

    DeleteGatewayTarget is asynchronous: the target sits in DELETING for a while. CloudFormation
    deletes the gateway right after this custom resource reports success, and DeleteGateway fails
    with "Gateway ... has targets associated with it" while the target still exists — which is how
    a failed deploy's rollback ended in DELETE_FAILED. Waiting here keeps the delete ordered.
    """
    url = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}/targets/{target_id}"
    waited = 0
    while waited <= max_wait:
        resp = signed_request("GET", url)
        if resp.status_code == 404:
            return True
        logger.info("Waiting for target %s to finish deleting (HTTP %s)", target_id, resp.status_code)
        sleep(interval)
        waited += interval
    return False


def handle_delete(event, props):
    region = get_region()
    target_id = event.get("PhysicalResourceId", "")
    gateway_id = props.get("GatewayId", "")

    if target_id and gateway_id:
        try:
            url = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}/targets/{target_id}"
            resp = signed_request("DELETE", url)
            logger.info("Delete response: %s", resp.status_code)
            if resp.status_code in (200, 202, 204, 404):
                if not wait_for_target_deleted(region, gateway_id, target_id):
                    logger.warning("Target %s still present after waiting; gateway delete may fail", target_id)
        except Exception as e:
            logger.warning("Error deleting target: %s", e)

    return {"PhysicalResourceId": target_id}
