"""Register a user's own remote MCP server as a target on the harness gateway.

Runs at REQUEST time, when a user submits the Add MCP form -- not at deploy
time. The platform's own targets are created by a CDK custom resource; this is
the per-user path, which is why the permissions live on the agent runtime role.

Why the gateway rather than connecting directly
-----------------------------------------------
The gateway holds the user's third-party credential in AgentCore Identity, so it
never passes through our code or our Secrets Manager. Connecting directly from
the runtime would make us custodian of every user's API keys and tokens, and put
outbound calls to arbitrary user-supplied URLs in our own egress path.

Ordering matters
----------------
The ownership row is written BEFORE the target is created. Both the gateway
interceptor and the catalog reader treat an unrecorded target as unattributable
and hide it, so a target that exists without its row is invisible to everyone
including its owner -- and the owner cannot delete what they cannot see. An
orphaned row with no target is harmless by comparison: nothing resolves to it.

Registration does not block on synchronization
----------------------------------------------
A target takes up to minutes to reach READY. The deploy-time handler waits with
``sleep(30)`` x5, which is fine for CloudFormation and unacceptable for a form
submit. This creates the target, checks briefly to surface immediate failures,
and returns a status the UI can poll.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from security import url_guard

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-west-2")
GATEWAY_REGISTRY_TABLE = os.environ.get("GATEWAY_REGISTRY_TABLE", "")
HARNESS_GATEWAY_ID = os.environ.get("HARNESS_GATEWAY_ID", "")

# Partition holding one row per user-registered target. Must match OWNER_PK in
# the interceptor's ownership.py and OWNER_PK in ui/server/toolCatalog.ts --
# this row is the single source of truth for who owns a target.
OWNER_PK = "TARGET#OWNER"

# Target names must satisfy the service's pattern, which permits alphanumerics
# with single non-leading hyphens and NO underscores:
#
#     ([0-9a-zA-Z][-]?){1,100}
#
# Taken from a live rejection, not the docs: the CDK L2 describes
# gatewayTargetName as allowing "a-z, A-Z, 0-9, _ (underscore) and - (hyphen)",
# but the service refuses underscores. An earlier `usr_<uuid>` scheme failed with
# "Value at 'name' failed to satisfy constraint".
#
# The name is OPAQUE: nothing parses it for meaning, because ownership is
# recorded rather than derived. The prefix is only a fast way to tell user
# targets from platform ones before consulting the record.
TARGET_NAME_PATTERN = re.compile(r"^([0-9a-zA-Z][-]?){1,100}$")
_TARGET_PREFIX = "usr-"
_MAX_TARGET_NAME = 100

# Hosts an MCP endpoint must never resolve to are decided by the ONE shared guard,
# ``security.url_guard`` (resolved addresses, not name patterns: a name-based list let
# any public DNS name that points at 127.0.0.1 or 169.254.169.254 through, and missed
# CGNAT, ULA, IPv4-mapped IPv6 and decimal/octal literals). The gateway makes the
# registered target's outbound call; the PROBE below connects from this runtime and
# goes through the guard's pinned session.

_CONTROL_PLANE = f"https://bedrock-agentcore-control.{REGION}.amazonaws.com"

# Brief post-create check only. Long enough to catch an immediately-rejected
# endpoint, short enough that a form submit still feels synchronous.
_STATUS_CHECK_ATTEMPTS = 3
_STATUS_CHECK_DELAY_S = 1.5

_READY_STATUSES = {"READY", "ACTIVE"}
_FAILED_STATUSES = {"FAILED", "SYNCHRONIZE_UNSUCCESSFUL"}


class RegistrationError(Exception):
    """Registration could not complete. Message is safe to show a user.

    Logs on construction. Two live failures produced no log line at all because
    several paths raise this and return before any logging happens, which left
    the only diagnosis in the user's browser. A failure the operator cannot see
    is a failure that takes an extra round trip to fix.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        logger.warning("registration refused: %s", message)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_endpoint(url: str) -> str:
    """Validate a user-supplied MCP endpoint, returning it normalised.

    Raises:
        RegistrationError: with a message intended for the user.
    """
    if not url or not isinstance(url, str):
        raise RegistrationError("URL is required")
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise RegistrationError(f"URL is not valid: {exc}") from exc

    if parsed.scheme != "https":
        # Not merely a preference: the credential travels on this connection.
        raise RegistrationError("URL must use https")
    host = parsed.hostname or ""
    if not host:
        raise RegistrationError("URL must include a host")
    try:
        url_guard.validate_url(url)
    except url_guard.BlockedURLError as exc:
        raise RegistrationError(
            "URL must point at a publicly reachable host "
            f"(private, loopback and link-local addresses are not allowed): {exc}"
        ) from None
    return url


# Auth scheme words that are never themselves a credential. Used only to give a
# clear message when a paste is truncated.
_BARE_SCHEME_WORDS = frozenset({"bearer", "key", "token", "basic", "apikey"})


def split_header_value(value: str) -> tuple[str, str]:
    """Split a pasted header value into (prefix, token).

    Vendors document a header VALUE, not a prefix and a token: fal.ai says
    ``Key <id>:<secret>``, GitHub says ``Bearer <pat>``, others give a bare key.
    Asking for the prefix separately made the trailing space load-bearing and
    invisible -- ``Key`` and ``Key `` differ by one character and produce an
    identical-looking 401.

    So take the whole value and split on the first run of whitespace, keeping the
    separator on the prefix (AgentCore concatenates prefix + key with nothing
    between them):

        "Key abc:def"  -> ("Key ", "abc:def")
        "Bearer tok"   -> ("Bearer ", "tok")
        "rawapikey"    -> ("", "rawapikey")      # no prefix at all

    Only the first whitespace run splits, so a token containing spaces survives.
    """
    value = (value or "").strip()
    if not value:
        return "", ""
    parts = re.split(r"(\s+)", value, maxsplit=1)
    if len(parts) < 3:
        # No whitespace: the entire value is the credential.
        return "", value
    scheme, gap, rest = parts[0], parts[1], parts[2]
    if not rest.strip():
        return "", scheme
    return f"{scheme}{gap}", rest.strip()


def _validate_header_name(name: str) -> str:
    name = (name or "Authorization").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", name):
        raise RegistrationError(
            "Header name may contain only letters, numbers and hyphens"
        )
    return name


def new_target_name(prefix: str = _TARGET_PREFIX) -> str:
    """Generate an opaque, charset-safe target name.

    Opaque on purpose. An earlier design embedded a hash of the owner here so
    the interceptor could decide ownership without a lookup, which forced the
    same derivation into three codebases. Ownership is recorded now, so this only
    has to be unique and legal.
    """
    name = f"{prefix}{uuid.uuid4().hex}"[:_MAX_TARGET_NAME]
    # Fail here rather than at the gateway: a name the service rejects wastes a
    # round trip and leaves the caller reading an AWS validation message.
    if not TARGET_NAME_PATTERN.match(name):
        raise RegistrationError(f"Generated target name is not valid: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------


def _signed_request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    """SigV4-signed call to the AgentCore control plane.

    Raw HTTP rather than boto3 because ``mcpServer`` is absent from the service
    model -- the same reason the deploy-time target handler does this.
    """
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    data = json.dumps(body).encode() if body is not None else None
    aws_req = AWSRequest(
        method=method, url=url, data=data,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_req)
    req = urllib.request.Request(
        url=url, data=data, headers=dict(aws_req.headers), method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            return resp.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as exc:
        text = exc.read().decode() if exc.fp else ""
        try:
            parsed = json.loads(text) if text else {}
        except ValueError:
            parsed = {"message": text}
        return exc.code, parsed


def _create_api_key_provider(name: str, api_key: str) -> str:
    """Store the user's token in AgentCore Identity, returning the provider ARN.

    This is the point of routing through the gateway: the literal credential
    goes to AgentCore and never to our storage.
    """
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    resp = client.create_api_key_credential_provider(name=name, apiKey=api_key)
    arn = resp.get("credentialProviderArn") or ""
    if not arn:
        raise RegistrationError("Credential provider was created without an ARN")
    return arn


def _delete_api_key_provider(name: str) -> None:
    """Best-effort cleanup. Never raises: it runs on a failure path."""
    try:
        boto3.client(
            "bedrock-agentcore-control", region_name=REGION
        ).delete_api_key_credential_provider(name=name)
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the cause
        logger.warning("cleanup: could not delete credential provider %s: %s", name, exc)


def _create_target(
    gateway_id: str,
    target_name: str,
    endpoint: str,
    provider_arn: str,
    header_name: str,
    value_prefix: str,
    description: str,
) -> str:
    body = {
        "name": target_name,
        "description": description or "User-registered MCP server",
        "credentialProviderConfigurations": [
            {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "providerArn": provider_arn,
                        "credentialLocation": "HEADER",
                        "credentialParameterName": header_name,
                        # Sent verbatim, so an empty prefix means the bare token
                        # (API-key style) and "Bearer " means an OAuth-style one.
                        "credentialPrefix": value_prefix,
                    }
                },
            }
        ],
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": endpoint}}},
        # Which interceptor-injected headers reach this target. The gateway owns
        # Authorization (it attaches the credential provider's key), so only
        # identity context is forwarded.
        "allowedRequestHeaders": ["x-actor-id"],
    }
    status, payload = _signed_request(
        "POST", f"{_CONTROL_PLANE}/gateways/{gateway_id}/targets", body
    )
    if status not in (200, 201, 202):
        message = payload.get("message") or json.dumps(payload)[:200]
        raise RegistrationError(f"Gateway rejected the target: {message}")
    return payload.get("targetId") or target_name


def _target_status(gateway_id: str, target_id: str) -> str:
    status, payload = _signed_request(
        "GET", f"{_CONTROL_PLANE}/gateways/{gateway_id}/targets/{target_id}"
    )
    if status != 200:
        return ""
    return payload.get("status") or ""


def _synchronize(gateway_id: str, target_id: str) -> None:
    _signed_request(
        "POST",
        f"{_CONTROL_PLANE}/gateways/{gateway_id}/targets/synchronize",
        {"targetIdList": [target_id]},
    )


def delete_target(gateway_id: str, target_id: str) -> None:
    """Best-effort target removal, used to unwind a partial registration."""
    try:
        _signed_request(
            "DELETE", f"{_CONTROL_PLANE}/gateways/{gateway_id}/targets/{target_id}"
        )
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the cause
        logger.warning("cleanup: could not delete target %s: %s", target_id, exc)


# ---------------------------------------------------------------------------
# Ownership record
# ---------------------------------------------------------------------------


def _table():
    if not GATEWAY_REGISTRY_TABLE:
        raise RegistrationError(
            "Gateway registry is not configured on this runtime"
        )
    return boto3.resource("dynamodb", region_name=REGION).Table(GATEWAY_REGISTRY_TABLE)


def record_ownership(
    target_name: str, actor_id: str, server_name: str, endpoint: str
) -> None:
    """Write the row both the interceptor and the catalog read.

    Written before the target exists. A target with no row is invisible to
    everyone -- including its owner, who then cannot remove it -- whereas a row
    with no target resolves to nothing and is harmless.
    """
    _table().put_item(
        Item={
            "PK": OWNER_PK,
            "SK": target_name,
            "ownerActorId": actor_id,
            "serverName": server_name,
            "endpoint": endpoint,
            "createdAt": int(time.time()),
            # Last known readiness, so listing does not need a control-plane
            # call per row. Converges: registration_status writes back when a
            # live check resolves.
            "status": "synchronizing",
        }
    )


def record_target_id(target_name: str, target_id: str, status: str) -> None:
    """Attach the gateway's target id and readiness to the ownership row.

    Separate from :func:`record_ownership` because the id only exists after the
    target is created, and the row is deliberately written first. Best-effort:
    losing this leaves deletion falling back to the target name.
    """
    try:
        _table().update_item(
            Key={"PK": OWNER_PK, "SK": target_name},
            UpdateExpression="SET targetId = :t, #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":t": target_id, ":s": status},
        )
    except Exception as exc:  # noqa: BLE001 — not worth failing registration for
        logger.warning("could not record target id for %s: %s", target_name, exc)


def list_user_mcp_servers(actor_id: str) -> list[dict]:
    """Every MCP server ``actor_id`` has registered.

    Queries the single ownership partition and filters by owner in code. The
    partition holds one row per target across all users, which stays small
    enough that a Query beats maintaining an index; revisit with a GSI on
    ownerActorId if that stops being true.

    Returns the stored readiness rather than probing each target, so opening the
    panel costs one read. Callers poll :func:`registration_status` for rows that
    are still settling.
    """
    if not actor_id:
        return []
    try:
        resp = _table().query(
            KeyConditionExpression=Key("PK").eq(OWNER_PK),
        )
    except Exception as exc:  # noqa: BLE001 — an empty list is safer than a 500
        logger.error("could not list MCP servers: %s", exc)
        return []
    out = []
    for item in resp.get("Items") or []:
        if item.get("ownerActorId") != actor_id:
            continue
        out.append({
            "target_name": item.get("SK", ""),
            "target_id": item.get("targetId", "") or "",
            "server_name": item.get("serverName", ""),
            "endpoint": item.get("endpoint", ""),
            "status": item.get("status", "synchronizing"),
            # These ARE the caller's own registrations, so removing one affects
            # nobody else. Stated explicitly rather than left to be inferred from
            # the absence of `platform`, because the shared rows now carry the
            # opposite value and the UI keys its Remove button on this.
            "removable": True,
        })
    out.sort(key=lambda r: r["server_name"].lower())
    return out


def _gateway_targets(gateway_id: str) -> list[dict]:
    """Every target on one gateway, following pagination. Raises on failure."""
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    items: list[dict] = []
    token = None
    # Bounded: a runaway token loop would hang the panel, and no realistic
    # gateway has more than a few hundred targets.
    for _ in range(20):
        kwargs = {"gatewayIdentifier": gateway_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_gateway_targets(**kwargs)
        items.extend(resp.get("items") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def list_platform_mcp_servers() -> tuple[list[dict], list[str]]:
    """Platform MCP targets on the harness gateway (e.g. aws-mcp).

    These are NOT user registrations: they live on the gateway itself and are
    visible to every actor, since the interceptor only scopes ``usr-`` targets.
    That is exactly why they were missing from the MCP servers panel -- the panel
    listed ownership rows, and a platform target has none.

    Scoped to the HARNESS gateway on purpose. Other registered gateways also hold
    targets, but their tools already reach agents over the gateway's own OAuth
    connection, so enumerating them here would add nothing an operator can act on
    -- and it would need ``ListGatewayTargets`` across every gateway in the
    account, widening the runtime's role for an inventory label. The gateway
    registry (Settings -> Gateways) is where those gateways are managed.

    Returns ``(rows, unreadable)``. ``unreadable`` names the gateway when the
    control-plane read fails, so an empty panel states a reason instead of looking
    like "the harness has no MCP server" -- the same silent-shortfall failure this
    listing exists to avoid.

    Rows are marked ``removable: False``. A platform target belongs to the harness
    rather than the caller, and ``deregister_user_mcp_server`` re-checks ownership
    against a recorded row a platform target does not have, so the button could
    only ever fail.

    Never raises: a control-plane failure degrades to no platform rows plus an
    ``unreadable`` entry, so it cannot break the caller's own list.
    """
    if not HARNESS_GATEWAY_ID:
        return [], []

    try:
        items = _gateway_targets(HARNESS_GATEWAY_ID)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("could not list platform MCP targets: %s", exc)
        return [], [f"Platform gateway ({type(exc).__name__})"]

    out = []
    for it in items:
        name = str(it.get("name") or "")
        if not name or name.startswith(_TARGET_PREFIX):
            continue  # a usr- target belongs to some actor; not a platform row
        out.append({
            "target_name": name,
            "target_id": str(it.get("targetId") or ""),
            "server_name": name,
            # No user-supplied endpoint exists for these; label the source so the
            # row is not a bare name next to the user's own servers.
            "endpoint": "Platform gateway",
            "status": "ready" if str(it.get("status") or "") == "READY" else "synchronizing",
            "platform": True,
            "removable": False,
        })
    out.sort(key=lambda r: r["server_name"].lower())
    return out, []


def forget_ownership(target_name: str) -> None:
    """Best-effort removal of the ownership row."""
    try:
        _table().delete_item(Key={"PK": OWNER_PK, "SK": target_name})
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the cause
        logger.warning("cleanup: could not delete ownership row %s: %s", target_name, exc)


# ---------------------------------------------------------------------------
# Connection probe
# ---------------------------------------------------------------------------

# A user is waiting on this, so it fails fast rather than hanging on a dead host.
_PROBE_TIMEOUT_S = 10.0
_PROBE_MAX_TOOLS_SHOWN = 50

_MCP_PROTOCOL_VERSION = "2025-03-26"


def _parse_jsonrpc_body(text: str) -> dict:
    """Parse a JSON-RPC reply that may arrive as JSON or as an SSE stream.

    Streamable-HTTP MCP servers may answer a POST with ``text/event-stream``, so
    a plain ``json.loads`` is not enough. Take the last ``data:`` payload that
    parses, since that is the response to our request.
    """
    text = text.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        pass
    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            candidate = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(candidate, dict):
            result = candidate
    return result


#: Cap on one probe reply (an initialize / tools/list answer is a few KB).
_PROBE_MAX_BYTES = 2 * 1024 * 1024


def _mcp_post(url: str, headers: dict, payload: dict) -> tuple[int, dict, dict]:
    """POST one JSON-RPC message. Returns (status, body, response headers).

    The probe connects from THIS runtime to a user-supplied URL, so it goes through the
    shared SSRF guard's pinned session, with redirects OFF (a POST that redirects is not
    an MCP endpoint, and a followed redirect is how a vetted host hands us an internal
    one). A >=400 reply raises ``urllib.error.HTTPError`` as the probe expects.
    """
    data = json.dumps(payload).encode()
    with url_guard.session() as s:
        resp = s.post(url, data=data, headers=headers, timeout=_PROBE_TIMEOUT_S,
                      allow_redirects=False, stream=True)
        try:
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(url, resp.status_code, resp.reason or "",
                                             resp.headers, None)
            if 300 <= resp.status_code < 400:
                raise RegistrationError("The endpoint redirected; give the final MCP URL")
            raw = b""
            for chunk in resp.iter_content(chunk_size=65536):
                raw += chunk
                if len(raw) > _PROBE_MAX_BYTES:
                    raise RegistrationError("The endpoint's reply is too large")
            return (
                resp.status_code,
                _parse_jsonrpc_body(raw.decode(errors="replace")),
                {k.lower(): v for k, v in resp.headers.items()},
            )
        finally:
            resp.close()


def probe_mcp_endpoint(
    url: str,
    token: str = "",
    header_name: str = "Authorization",
    value_prefix: str = "Bearer ",
    header_value: str = "",
) -> dict:
    """Check that an endpoint is a reachable, authorised MCP server.

    Deliberately only ``initialize`` + ``tools/list``. That proves the endpoint
    speaks MCP, that the credential is accepted, and what it exposes -- without
    invoking anything. Calling a tool to prove connectivity would cause side
    effects on someone else's system, which is not ours to do.

    Reuses :func:`validate_endpoint`, so a probe cannot reach a host that
    registration would refuse -- otherwise this would be an SSRF tool with a
    friendly button.

    Returns ``{"ok": bool, "tools": [...], "tool_count": int, "detail": str}``.

    ``detail`` rather than ``error`` on purpose: the UI's action helper treats an
    ``error`` key as "the action itself failed" and throws, so a probe that
    reported its outcome there would raise instead of rendering. Here ``error``
    stays reserved for a failed action and ``ok`` + ``detail`` is the result.
    Never raises for a remote-side problem: a failed probe is a normal outcome
    the form displays, not an exception.
    """
    if header_value:
        # A pasted header value wins: it is what the vendor documents, and it
        # cannot disagree with itself the way a separate prefix and token can.
        value_prefix, token = split_header_value(header_value)
    try:
        endpoint = validate_endpoint(url)
        header_name = _validate_header_name(header_name)
    except RegistrationError as exc:
        return {"ok": False, "tools": [], "tool_count": 0, "detail": str(exc)}

    headers = {
        "Content-Type": "application/json",
        # Streamable HTTP servers reject a POST that does not accept both.
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers[header_name] = f"{value_prefix}{token}"

    try:
        status, body, resp_headers = _mcp_post(
            endpoint, headers,
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mediaworks-harness", "version": "1.0"},
                },
            },
        )
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}"
        if exc.code in (401, 403):
            detail = (
                f"HTTP {exc.code} — the server rejected the credential "
                "(check the token, header name and prefix)"
            )
        return {"ok": False, "tools": [], "tool_count": 0, "detail": detail}
    except Exception as exc:  # noqa: BLE001 — any transport problem is a result
        return {"ok": False, "tools": [], "tool_count": 0,
                "detail": f"Couldn't connect: {exc}"}

    if status >= 400:
        return {"ok": False, "tools": [], "tool_count": 0,
                "detail": f"Couldn't connect: HTTP {status}"}
    if "result" not in body:
        # Reachable, but not speaking MCP. Usually the wrong path -- the site
        # root instead of the /mcp or /sse endpoint.
        err = (body.get("error") or {}).get("message") if isinstance(body, dict) else ""
        return {
            "ok": False, "tools": [], "tool_count": 0,
            "detail": err or "Reachable, but this doesn't look like an MCP endpoint",
        }

    # Carry the session forward if one was issued; some servers require it.
    session_id = resp_headers.get("mcp-session-id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    try:
        _, listing, _ = _mcp_post(
            endpoint, headers, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tools": [], "tool_count": 0,
                "detail": f"Connected, but listing tools failed: {exc}"}

    entries = ((listing.get("result") or {}).get("tools")) or []
    names = [
        e.get("name") for e in entries
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    ]
    return {
        "ok": True,
        "tools": names[:_PROBE_MAX_TOOLS_SHOWN],
        "tool_count": len(names),
        # Not an error: a server can be perfectly reachable and expose nothing,
        # which the form should say plainly rather than call success.
        "detail": "" if names else "Connected, but this server exposes no tools",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_user_mcp_server(
    actor_id: str,
    server_name: str,
    url: str,
    token: str = "",
    header_name: str = "Authorization",
    value_prefix: str = "Bearer ",
    description: str = "",
    gateway_id: str = "",
    header_value: str = "",
) -> dict:
    """Register one remote MCP server for ``actor_id``.

    Returns a dict with ``target_name``, ``target_id`` and ``status``, where
    status is one of ``ready`` / ``synchronizing`` / ``failed``. Callers should
    treat ``synchronizing`` as success in progress and poll
    :func:`registration_status`.

    Raises:
        RegistrationError: validation failed or the gateway refused. Any
            resource created before the failure is unwound.
    """
    if header_value:
        # A lone scheme word is a truncated paste, never a credential. This is a
        # heuristic -- a bare API key could in principle be any string -- but
        # none of these words is ever a valid one, and catching it here gives a
        # far better message than the 401 the far end would return.
        if header_value.strip().lower() in _BARE_SCHEME_WORDS:
            raise RegistrationError(
                f"The auth header value looks incomplete: got only "
                f"{header_value.strip()!r} with no credential after it."
            )
        value_prefix, token = split_header_value(header_value)
    if not actor_id:
        raise RegistrationError("Could not determine who is registering")
    if not server_name or not server_name.strip():
        raise RegistrationError("Name is required")
    gateway_id = gateway_id or HARNESS_GATEWAY_ID
    if not gateway_id:
        raise RegistrationError("Harness gateway is not configured on this runtime")

    endpoint = validate_endpoint(url)
    header_name = _validate_header_name(header_name)
    target_name = new_target_name()

    # 1. Ownership first — see the module docstring.
    record_ownership(target_name, actor_id, server_name.strip(), endpoint)

    provider_arn = ""
    target_id = ""
    try:
        # 2. Credential into AgentCore Identity, never into our storage.
        if token:
            provider_arn = _create_api_key_provider(target_name, token)
        elif value_prefix:
            # A prefix with nothing after it is a truncated paste, not a
            # credential-free server.
            raise RegistrationError(
                f"The auth header value looks incomplete: got only {value_prefix.strip()!r} "
                "with no credential after it."
            )

        # 3. The target itself.
        target_id = _create_target(
            gateway_id, target_name, endpoint, provider_arn,
            header_name, value_prefix, description,
        )
    except RegistrationError:
        if target_id:
            delete_target(gateway_id, target_id)
        if provider_arn:
            _delete_api_key_provider(target_name)
        forget_ownership(target_name)
        raise
    except Exception as exc:  # noqa: BLE001 — unwind, then surface
        if target_id:
            delete_target(gateway_id, target_id)
        if provider_arn:
            _delete_api_key_provider(target_name)
        forget_ownership(target_name)
        logger.exception("registration failed for actor=%s", actor_id)
        raise RegistrationError(f"Registration failed: {exc}") from exc

    status = _await_initial_status(gateway_id, target_id)
    record_target_id(target_name, target_id, status)
    return {
        "target_name": target_name,
        "target_id": target_id,
        "status": status,
        "server_name": server_name.strip(),
        "endpoint": endpoint,
    }


def _await_initial_status(gateway_id: str, target_id: str) -> str:
    """Check briefly so obvious failures surface while the user is watching."""
    for attempt in range(_STATUS_CHECK_ATTEMPTS):
        raw = _target_status(gateway_id, target_id)
        if raw in _READY_STATUSES:
            return "ready"
        if raw in _FAILED_STATUSES:
            # One resync: the far end may simply have been cold.
            _synchronize(gateway_id, target_id)
            return "synchronizing"
        if attempt < _STATUS_CHECK_ATTEMPTS - 1:
            time.sleep(_STATUS_CHECK_DELAY_S)
    return "synchronizing"


def registration_status(
    target_id: str, gateway_id: str = "", target_name: str = ""
) -> str:
    """Poll a target's readiness. Returns ready / synchronizing / failed.

    Writes a resolved result back onto the ownership row when the caller supplies
    the target name, so the stored value converges and a later list does not
    show a server as still settling forever.
    """
    gateway_id = gateway_id or HARNESS_GATEWAY_ID
    if not gateway_id or not target_id:
        return "failed"
    raw = _target_status(gateway_id, target_id)
    if raw in _READY_STATUSES:
        resolved = "ready"
    elif raw in _FAILED_STATUSES:
        resolved = "failed"
    else:
        return "synchronizing"
    if target_name:
        record_target_id(target_name, target_id, resolved)
    return resolved


def deregister_user_mcp_server(
    actor_id: str, target_name: str, target_id: str = "", gateway_id: str = ""
) -> dict:
    """Remove a server the caller owns.

    Ownership is re-checked here against the record rather than trusted from the
    request, so a caller cannot delete another user's target by naming it.
    """
    gateway_id = gateway_id or HARNESS_GATEWAY_ID
    if not gateway_id:
        raise RegistrationError("Harness gateway is not configured on this runtime")
    item = _table().get_item(Key={"PK": OWNER_PK, "SK": target_name}).get("Item") or {}
    owner = item.get("ownerActorId") or ""
    if not owner or owner != actor_id:
        # Same response whether it does not exist or belongs to someone else, so
        # this cannot be used to enumerate other users' targets.
        raise RegistrationError("No such MCP server")

    delete_target(gateway_id, target_id or target_name)
    _delete_api_key_provider(target_name)
    forget_ownership(target_name)
    return {"target_name": target_name, "status": "deleted"}
