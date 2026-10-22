"""Search_Gateway target synchronization and verification (media-harness-projects-access, design
Component 6 "SearchTargetSync" + "Re-sync command"; tasks 8.7, 8.13).

Shared by two callers:

- the ``SearchTargetSync`` custom resource in ``projects-stack.ts`` (Create/Update), via ``handler``;
- the ``agent/scripts/search_gateway_sync.py`` re-sync command, via ``sync_and_verify``.

The job has four steps (Component 6):

1. ``SynchronizeGatewayTargets`` on the Search_Gateway's single target, then poll ``GetGatewayTarget``
   until it reports ``READY`` with a ``lastSynchronizedAt`` later than the moment the sync started.
2. Read the upstream M2C tool list (a read-only ``tools/list``) with the M2C client credentials.
3. Require exactly one tool ``hybrid-search___search_content`` whose
   ``inputSchema.properties.collection_id`` is ``{"type": "string"}`` and, when ``search_image_s3uri`` is
   present, whose ``search_image_s3uri`` property is ``{"type": "string"}`` — so the Cedar ``like``
   conditions validate under ``FAIL_ON_ANY_FINDINGS``. The pre-rename ``search_image_uri`` is REFUSED:
   the permit constrains only ``search_image_s3uri``, so an upstream declaring the old name would be
   searchable with an image from any bucket.
4. Return ``SearchToolName = "<target>___hybrid-search___search_content"`` and the tool count.

Any failure raises ``SyncError`` naming the failed step, the missing tool, or the missing property, so
the deploy (or the CLI) fails loudly rather than leaving a Cedar permit that matches nothing.

Clients and the tool lister are injected into ``sync_and_verify`` so the unit test (task 8.8) and the CLI
drive it without AWS. ``handler`` builds the production pieces.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: The one M2C tool the Search_Gateway permits, as it appears in the upstream ``tools/list``.
UPSTREAM_SEARCH_TOOL = "hybrid-search___search_content"

#: The query-image input the Cedar permit constrains (its name since M2C bundle alpha.20261014a).
IMAGE_ARG = "search_image_s3uri"
#: Its pre-rename spelling. The permit does not constrain it, so an upstream declaring it is refused.
OLD_IMAGE_ARG = "search_image_uri"

#: Default polling bounds for the target reaching READY.
DEFAULT_POLL_ATTEMPTS = 20
DEFAULT_POLL_INTERVAL_S = 15


class SyncError(Exception):
    """A sync/verify step failed. ``step`` is a short code safe to log and to exit on."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def poll_target_ready(
    get_target: Callable[[], dict],
    started_at: datetime,
    *,
    attempts: int = DEFAULT_POLL_ATTEMPTS,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Poll ``get_target()`` until status READY with ``lastSynchronizedAt`` after ``started_at``.

    Raises ``SyncError("sync", ...)`` on a terminal failure status or when the deadline passes.
    """
    last_status = "UNKNOWN"
    for attempt in range(attempts):
        target = get_target()
        last_status = target.get("status", "UNKNOWN")
        synced_at = _as_datetime(target.get("lastSynchronizedAt"))
        logger.info("target status (attempt %d): %s synced_at=%s", attempt + 1, last_status, synced_at)
        if last_status in ("FAILED", "SYNCHRONIZE_UNSUCCESSFUL"):
            # Capture AgentCore's own explanation. GetGatewayTarget carries a
            # `statusReasons` (list) / `statusReason` (string) describing WHY the
            # introspection failed (auth, endpoint, schema, ...). The handlers
            # never logged it before, and the target is torn down on rollback, so
            # the actual reason was lost on every failed deploy. Surface it in the
            # log AND in the raised message so it propagates into the CloudFormation
            # custom-resource failure reason (visible directly in the deploy output).
            reasons = target.get("statusReasons") or target.get("statusReason") or "(none reported)"
            logger.error(
                "target %s terminal: statusReasons=%s (full target keys=%s)",
                last_status, reasons, sorted(target.keys()),
            )
            raise SyncError("sync", f"target reached terminal status {last_status}; statusReasons={reasons}")
        if last_status in ("READY", "ACTIVE") and synced_at is not None and synced_at > started_at:
            return target
        if attempt < attempts - 1:
            sleep(interval_s)
    raise SyncError("sync", f"target did not reach READY after sync (last status {last_status})")


def _property_is_string(schema: dict, prop: str) -> bool:
    """True when ``schema.properties[prop]`` is typed ``string``.

    Checks the ``type`` field only. Real upstream schemas carry a ``description`` beside the type
    (M2C's ``collection_id`` is ``{"description": "...", "type": "string"}``), so an exact
    ``== {"type": "string"}`` comparison rejected a valid schema.
    """
    props = (schema or {}).get("properties", {})
    p = props.get(prop)
    return isinstance(p, dict) and p.get("type") == "string"


# --------------------------------------------------------------------------------------------------
# Upstream M2C tools/list (step 2). GetGatewayTarget does NOT return a target's tools (verified: its
# response keys are createdAt, credentialProviderConfigurations, description, gatewayArn,
# lastSynchronizedAt, metadataConfiguration, name, status, targetConfiguration, targetId, updatedAt),
# so the tool list must be read from the upstream MCP server itself, with the M2C client credentials.
# --------------------------------------------------------------------------------------------------

#: Bound on tools/list pages, so a misbehaving cursor cannot loop the custom resource forever.
MAX_TOOL_PAGES = 20

HttpPost = Callable[[str, bytes, dict], tuple[int, str, str]]
"""``(url, body, headers) -> (status, content_type, text)``. Injected so tests need no network."""


def _urllib_post(url: str, body: bytes, headers: dict) -> tuple[int, str, str]:
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, resp.headers.get("content-type", ""), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", "") if e.headers else "", e.read().decode() if e.fp else ""


def _jsonrpc_body(content_type: str, text: str) -> dict:
    """Parse a JSON-RPC response that may arrive as plain JSON or as one SSE ``data:`` event."""
    import json  # noqa: PLC0415

    if "text/event-stream" in (content_type or ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    return json.loads(payload)
        raise SyncError("tools_list", "upstream tools/list returned an empty event stream")
    return json.loads(text or "{}")


def fetch_client_credentials_token(
    token_endpoint: str, client_id: str, client_secret: str, scope: str, *, http: HttpPost = _urllib_post
) -> str:
    """OAuth2 ``client_credentials`` grant (client_secret_post). Raises ``SyncError("token", ...)``."""
    from urllib.parse import urlencode  # noqa: PLC0415
    import json  # noqa: PLC0415

    form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    if scope:
        form["scope"] = scope
    status, _ct, text = http(
        token_endpoint, urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"}
    )
    if status != 200:
        # The body of a token error is an OAuth error code (invalid_client / invalid_scope ...), never
        # a credential, so it is safe to surface.
        raise SyncError("token", f"M2C token request returned HTTP {status}: {text[:200]}")
    token = json.loads(text or "{}").get("access_token")
    if not token:
        raise SyncError("token", "M2C token response carried no access_token")
    return token


def list_upstream_tools(endpoint: str, access_token: str, *, http: HttpPost = _urllib_post) -> list[dict]:
    """Read every page of the upstream MCP ``tools/list`` (follows ``nextCursor``)."""
    import json  # noqa: PLC0415

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    tools: list[dict] = []
    cursor: str | None = None
    for page in range(1, MAX_TOOL_PAGES + 1):
        params = {"cursor": cursor} if cursor else {}
        body = json.dumps({"jsonrpc": "2.0", "id": page, "method": "tools/list", "params": params}).encode()
        status, ct, text = http(endpoint, body, headers)
        if status != 200:
            raise SyncError("tools_list", f"upstream tools/list returned HTTP {status}: {text[:200]}")
        msg = _jsonrpc_body(ct, text)
        if "error" in msg:
            raise SyncError("tools_list", f"upstream tools/list error: {msg['error']}")
        result = msg.get("result") or {}
        tools.extend(t for t in result.get("tools", []) if isinstance(t, dict))
        cursor = result.get("nextCursor")
        if not cursor:
            return tools
    raise SyncError("tools_list", f"upstream tools/list did not finish within {MAX_TOOL_PAGES} pages")


def verify_tools(tools: list[dict], target_name: str) -> tuple[str, int]:
    """Validate the upstream tool list and return (SearchToolName, tool_count).

    Requires exactly one ``hybrid-search___search_content`` whose ``collection_id`` input is a string and,
    when present, whose query-image input (either spelling) is a string.
    """
    if not isinstance(tools, list):
        raise SyncError("tools_list", "upstream tools/list did not return a list")

    matches = [t for t in tools if isinstance(t, dict) and t.get("name") == UPSTREAM_SEARCH_TOOL]
    if len(matches) != 1:
        raise SyncError(
            "missing_tool",
            f"expected exactly one '{UPSTREAM_SEARCH_TOOL}' tool, found {len(matches)}",
        )

    schema = matches[0].get("inputSchema") or {}
    if not _property_is_string(schema, "collection_id"):
        raise SyncError(
            "missing_property",
            "'collection_id' is missing or is not {\"type\": \"string\"} in search_content inputSchema",
        )
    # The image input is optional, but if the schema declares it, it must be a string so the Cedar
    # `like` conditions validate under FAIL_ON_ANY_FINDINGS.
    props = schema.get("properties", {})
    if OLD_IMAGE_ARG in props:
        raise SyncError(
            "unsupported_schema",
            f"search_content declares the pre-rename '{OLD_IMAGE_ARG}', which the Search_Policy does not "
            f"constrain; upgrade the M2C bundle (alpha.20261014a or later declares '{IMAGE_ARG}')",
        )
    if IMAGE_ARG in props and not _property_is_string(schema, IMAGE_ARG):
        raise SyncError(
            "missing_property",
            f"'{IMAGE_ARG}' is present but is not {{\"type\": \"string\"}}",
        )

    return f"{target_name}___{UPSTREAM_SEARCH_TOOL}", len(tools)


def sync_and_verify(
    *,
    synchronize: Callable[[], None],
    get_target: Callable[[], dict],
    list_tools: Callable[[], list[dict]],
    target_name: str,
    started_at: datetime | None = None,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run the four steps over injected callables. Returns a result dict with the tool name/count.

    Returns ``{"SearchToolName", "ToolCount", "LastSynchronizedAt"}``.
    """
    started_at = started_at or datetime.now(timezone.utc)

    try:
        synchronize()
    except SyncError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SyncError("sync", f"SynchronizeGatewayTargets failed: {exc}") from exc

    target = poll_target_ready(
        get_target, started_at, attempts=poll_attempts, interval_s=poll_interval_s, sleep=sleep
    )

    try:
        tools = list_tools()
    except SyncError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SyncError("tools_list", f"reading upstream tools/list failed: {exc}") from exc

    search_tool_name, tool_count = verify_tools(tools, target_name)
    synced_at = _as_datetime(target.get("lastSynchronizedAt"))
    return {
        "SearchToolName": search_tool_name,
        "ToolCount": tool_count,
        "LastSynchronizedAt": synced_at.isoformat() if synced_at else "",
    }


# --------------------------------------------------------------------------------------------------
# Production wiring (custom resource). Imports boto3/httpx lazily so the pure helpers above stay
# dependency-free for the unit test and the CLI.
# --------------------------------------------------------------------------------------------------


def _production_callables(props: dict):
    """Build the three injected callables from ResourceProperties for a real sync."""
    import json

    import boto3  # noqa: PLC0415
    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    region = props["Region"]
    gateway_id = props["GatewayId"]
    target_id = props["TargetId"]
    session = boto3.Session()
    base = f"https://bedrock-agentcore-control.{region}.amazonaws.com/gateways/{gateway_id}"

    def _signed(method: str, url: str, body: dict | None = None) -> dict:
        creds = session.get_credentials().get_frozen_credentials()
        data = json.dumps(body).encode() if body else None
        req = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"})
        SigV4Auth(creds, "bedrock-agentcore", region).add_auth(req)
        urllib_req = urllib.request.Request(url=url, data=data, headers=dict(req.headers), method=method)
        with urllib.request.urlopen(urllib_req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")

    def synchronize() -> None:
        # REST binding of the bedrock-agentcore-control SynchronizeGatewayTargets operation:
        # PUT /gateways/{gatewayIdentifier}/synchronizeTargets (202). The bundled CLI/boto3 in the
        # Lambda runtime predate this namespace, so the call is signed by hand — but the path and
        # method MUST match the model exactly, or this APIGW-style frontend returns 403 Forbidden
        # (its response to an unmatched route) rather than 404.
        _signed("PUT", f"{base}/synchronizeTargets", {"targetIdList": [target_id]})

    def get_target() -> dict:
        # GET /gateways/{gatewayIdentifier}/targets/{targetId}/ (note the trailing slash in the model).
        return _signed("GET", f"{base}/targets/{target_id}/")

    def list_tools() -> list[dict]:
        # Read the upstream M2C tools/list with the M2C client credentials (design Component 6,
        # step 2). The client secret comes from M2C's gateway-credentials bundle; the endpoint,
        # token endpoint, client id and scope come from ResourceProperties, falling back to the
        # same bundle (which carries endpointUrl / tokenEndpoint / clientId / scope too).
        secret_arn = props.get("M2cSecretArn", "")
        if not secret_arn:
            raise SyncError("tools_list", "M2cSecretArn is not set on the SearchTargetSync resource")
        raw = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_arn)
        bundle = json.loads(raw["SecretString"])
        secret_key = props.get("M2cSecretJsonKey") or "clientSecret"
        client_secret = bundle.get(secret_key)
        if not client_secret:
            raise SyncError("tools_list", f"'{secret_key}' is missing from the M2C credentials bundle")
        token = fetch_client_credentials_token(
            props.get("M2cTokenEndpoint") or bundle.get("tokenEndpoint", ""),
            props.get("M2cClientId") or bundle.get("clientId", ""),
            client_secret,
            props.get("M2cScope") or bundle.get("scope", ""),
        )
        return list_upstream_tools(props.get("M2cEndpoint") or bundle.get("endpointUrl", ""), token)

    return synchronize, get_target, list_tools, props.get("TargetName", "")


def handler(event, context):
    """``SearchTargetSync`` custom resource (Create/Update). Delete is a no-op."""
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {})
    logger.info("SearchTargetSync %s for target %s", request_type, props.get("TargetId"))
    if request_type == "Delete":
        return {"PhysicalResourceId": f"search-target-sync-{props.get('TargetId')}"}

    synchronize, get_target, list_tools, target_name = _production_callables(props)
    result = sync_and_verify(
        synchronize=synchronize,
        get_target=get_target,
        list_tools=list_tools,
        target_name=target_name,
    )
    logger.info("SearchTargetSync ok: %s", result)
    return {
        "PhysicalResourceId": f"search-target-sync-{props.get('TargetId')}",
        "Data": result,
    }
