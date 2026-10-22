"""AgentCore Gateway interceptor: per-actor tool scoping.

Bound to the harness gateway as **both** the REQUEST and the RESPONSE
interceptor (a gateway permits at most one of each, and both may be the
same function). The two phases do different halves of one job:

- **RESPONSE** filters ``tools/list`` and search results so an actor only
  ever *sees* their own registered MCP servers' tools.
- **REQUEST** denies ``tools/call`` for a tool the actor does not own.

Both are required. Filtering the listing alone is advisory — a model can
name a tool it was never shown, so without the REQUEST half the scoping is
cosmetic rather than a security boundary.

Identity
--------
The caller is identified from the ``sub`` claim of the bearer token on the
request, which requires ``passRequestHeaders: true`` in the interceptor
configuration — without it the ``headers`` field is absent from the payload
entirely and every request looks anonymous.

The token is decoded but NOT signature-verified, because the gateway's
inbound authorizer (the end-user Cognito pool) has already validated it.
That makes the identity **verified rather than asserted**: an attacker
cannot present a self-signed token, since the gateway rejects it before
this code ever runs. This is precisely why inbound auth is the user pool
rather than a shared M2M identity -- with a shared identity the actor would
have to be asserted in a header, and anything holding the M2M secret could
then claim to be any user.

There is deliberately no header fallback. Accepting an asserted
``X-Actor-Id`` alongside the claim would reduce the scheme to the weaker of
the two.

Fail-closed
-----------
Every path that cannot positively establish ownership denies or filters
out, per the documented interceptor best practice. A missing token, an
unparseable payload, or an unattributable target all result in no access
rather than full access. The practical consequence is that a caller whose
token carries no identity claim sees an empty tool list — loud and safe,
rather than quiet and permissive.

Contract notes that shaped this code
------------------------------------
- REQUEST input has no ``gatewayResponse``; RESPONSE input does. That is
  how the phase is detected, so one function can serve both roles.
- Returning ``transformedGatewayResponse`` from the REQUEST phase makes the
  gateway reply with it immediately. That is the deny mechanism.
- For MCP targets the RESPONSE interceptor **still runs** after a REQUEST
  deny, so the response phase must tolerate seeing an error body it did
  not produce and pass it through untouched.
- With response streaming the RESPONSE interceptor is invoked once per
  event and only the first invocation may override ``statusCode`` /
  ``headers``; later ones may only replace ``body``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os

import delegation
from owner_naming import TARGET_TOOL_SEPARATOR, is_user_target, target_of_tool
from ownership import actor_owns_target

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_OUTPUT_VERSION = "1.0"

# Request header carrying a signed delegation grant for async plan traffic.
# The prefix matches the AgentCore runtime custom-header convention so it can
# ride the same allowlisted-header path the user token already uses. When
# present, it is the SOLE identity source for the request (see ``_actor_id``);
# the interceptor still never reads any asserted ``X-Actor-Id``.
DELEGATION_GRANT_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-DelegationGrant"

# Claim carrying the caller's identity. The gateway's inbound authorizer is the
# end-user Cognito pool, so `sub` is the user's id -- which is exactly the
# actor id the proxy already stamps elsewhere in the system. Using `sub`
# avoids adding a custom pool attribute and keeps the two identities aligned
# by construction.
IDENTITY_CLAIM = os.environ.get("IDENTITY_CLAIM", "sub")

# Tools that are not namespaced to a user target and must stay reachable
# for every caller. The gateway's semantic search is the important one: a
# gateway whose search tool is unavailable gets excluded from tool
# narrowing entirely, which silently drops all of its tools.
_GATEWAY_SEARCH_TOOL = "x_amz_bedrock_agentcore_search"

# JSON-RPC application error code returned on an authorization denial.
# -32003 sits in the implementation-defined server range, so it will not be
# confused with a protocol-level error such as "method not found".
_ERROR_CODE_FORBIDDEN = -32003

# Platform-wide tool denylist. These tools execute under the AGENT RUNTIME's
# IAM role (S3 read/write, DynamoDB, Bedrock, gateway target lifecycle,
# Secrets Manager writes, signin) and must be unreachable for EVERY caller,
# at the gateway, independent of the runtime. This is platform policy, not
# per-owner scoping: it holds regardless of whether an actor can be resolved,
# and it is enforced ahead of (and in addition to) the per-actor ownership
# check -- a tool must clear BOTH gates.
#
# Matched on the BARE tool name: the underlying MCP tool name AFTER the
# gateway's ``<target>___`` prefix is stripped, so the policy holds no matter
# what the hosting target is called. The bare names themselves contain the
# separator (``aws___call_aws``), which is exactly why the target prefix is
# removed with a SINGLE split (maxsplit=1) and never on every separator --
# splitting on all of them has already caused a bug in this repo and would let
# a denied tool slip the match.
_DENIED_TOOLS = frozenset(
    {
        "aws___call_aws",
        "aws___run_script",
        "aws___get_presigned_url",
    }
)


# ---------------------------------------------------------------------------
# Payload accessors
#
# Every accessor is defensive. The interceptor sits in the request path of
# every gateway call, so a KeyError here is an outage: an unexpected shape
# must degrade to "cannot establish ownership" (and therefore deny), never
# raise.
# ---------------------------------------------------------------------------


def _mcp(event: dict) -> dict:
    node = event.get("mcp")
    return node if isinstance(node, dict) else {}


def _gateway_request(event: dict) -> dict:
    node = _mcp(event).get("gatewayRequest")
    return node if isinstance(node, dict) else {}


def _gateway_response(event: dict) -> dict | None:
    node = _mcp(event).get("gatewayResponse")
    return node if isinstance(node, dict) else None


def _request_body(event: dict) -> dict:
    body = _gateway_request(event).get("body")
    if isinstance(body, dict):
        return body
    # `rawGatewayRequest.body` is a string; fall back to it when the parsed
    # form is absent so a payload variation doesn't blind us to the method.
    raw = _mcp(event).get("rawGatewayRequest")
    if isinstance(raw, dict) and isinstance(raw.get("body"), str):
        try:
            parsed = json.loads(raw["body"])
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _method(event: dict) -> str:
    method = _request_body(event).get("method")
    return method if isinstance(method, str) else ""


def _called_tool_name(event: dict) -> str:
    params = _request_body(event).get("params")
    if not isinstance(params, dict):
        return ""
    name = params.get("name")
    return name if isinstance(name, str) else ""


def _decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying its signature.

    Not verifying is correct here, not a shortcut: the gateway's inbound
    authorizer has already validated signature, issuer, audience and expiry
    before this interceptor is invoked. Re-verifying would mean fetching JWKS
    on the hot path and vendoring a JWT library into this asset, which is
    deployed as a plain directory with no dependency install step.

    The security property therefore comes from the gateway being the only way
    to reach this code, and from there being no other identity input -- an
    attacker cannot supply a self-signed token, because the gateway would
    reject it before the interceptor ever runs.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    # base64url, and JWT strips the padding.
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw)
    except (ValueError, TypeError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def _header_value(event: dict, name: str) -> str | None:
    """Case-insensitive lookup of a request header, or None if absent.

    Header names are case-insensitive and the gateway makes no promise about
    which casing it forwards. Returns None when the header is not present at
    all (distinct from present-but-empty, which returns "").
    """
    headers = _gateway_request(event).get("headers")
    if not isinstance(headers, dict):
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value if isinstance(value, str) else ""
    return None


def _jwt_actor_id(event: dict) -> str:
    """Resolve the caller's identity from the validated bearer token.

    The normal (interactive) path. The gateway's inbound authorizer has already
    validated the token, so decoding the ``sub`` claim yields a *verified*
    identity. Requires ``passRequestHeaders`` on the interceptor configuration.
    """
    token = _header_value(event, "authorization")
    if not token:
        return ""
    if token.lower().startswith("bearer "):
        token = token[7:]
    token = token.strip()
    if not token:
        return ""
    claims = _decode_jwt_claims(token)
    identity = claims.get(IDENTITY_CLAIM)
    return identity if isinstance(identity, str) else ""


def _actor_id(event: dict) -> str:
    """Resolve the caller's actor id from a VERIFIABLE identity, or "" (deny).

    Two mutually exclusive, verifiable sources — and an asserted ``X-Actor-Id``
    is never one of them:

    1. **Delegation grant (async plan traffic).** If the delegation-grant header
       is PRESENT, it is the sole identity source: the actor is whatever
       ``delegation.verify_grant`` returns, which is non-empty ONLY on a
       KMS-verified, unexpired grant and is "" on every failure. There is
       deliberately no fall-through to the JWT (or anything else) when a grant
       is present — a present-but-invalid grant denies. This makes the
       fail-closed property structural: once the grant path is taken, the only
       reachable actor is one the orchestrator signed.

    2. **Verified JWT ``sub`` (normal interactive traffic).** When no grant
       header is present, fall back to the gateway-validated bearer token. This
       path is unchanged, so normal user traffic is unaffected.

    An async dispatch that presents NO grant hits path 2, finds no user ``sub``,
    and returns "" — a deny — rather than trusting any stamped ``actor_id``.
    """
    grant = _header_value(event, DELEGATION_GRANT_HEADER)
    if grant is not None:
        # Grant path is committed to the moment the header is present. Its
        # result (verified actor id, or "" on any failure) is returned directly
        # with no fallback, so a verification failure can only ever DENY.
        return delegation.verify_grant(grant)
    return _jwt_actor_id(event)


def _is_streaming(event: dict) -> bool:
    response = _gateway_response(event) or {}
    return bool(response.get("isStreamingResponse"))


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _is_platform_tool(tool_name: str) -> bool:
    """Whether a tool is unowned platform surface rather than a user's.

    Anything not namespaced to a ``usr-`` target is platform surface. This
    is intentionally permissive for non-user targets because this gateway
    is provisioned to host user-registered servers; a platform target on it
    is shared infrastructure (like search), not someone's private tool.
    """
    if tool_name == _GATEWAY_SEARCH_TOOL:
        return True
    return not is_user_target(target_of_tool(tool_name))


def _bare_tool_name(tool_name: str) -> str:
    """The underlying tool name with the gateway's ``<target>___`` prefix removed.

    The gateway namespaces every target's tools as ``<target>___<tool>``. The
    prefix is stripped with a SINGLE split so a bare name that itself contains
    the separator survives intact:
    ``mediaworks-harness-aws-mcp___aws___call_aws`` -> ``aws___call_aws``.
    Splitting on every separator would corrupt such a name and let a denied
    tool slip the denylist match.

    A name with no prefix (e.g. the gateway's own search tool) is returned
    unchanged.
    """
    if TARGET_TOOL_SEPARATOR in tool_name:
        return tool_name.split(TARGET_TOOL_SEPARATOR, 1)[1]
    return tool_name


def _is_denied_tool(tool_name: str) -> bool:
    """Whether a tool is on the platform-wide denylist.

    Fail closed: an empty or unparseable name is treated as denied so an
    ambiguous request cannot slip past the policy.
    """
    if not tool_name:
        return True
    return _bare_tool_name(tool_name) in _DENIED_TOOLS


def _may_use(actor_id: str, tool_name: str) -> bool:
    if not tool_name:
        return False
    if _is_denied_tool(tool_name):
        # Platform-wide denylist, applied to EVERY actor. Checked ahead of the
        # platform-tool allow below so a denied tool on a shared (non-user)
        # target -- which would otherwise be allowed with no lookup -- is still
        # refused. This does not touch the per-actor ownership logic; a tool
        # must clear both this gate and, if user-owned, the ownership check.
        return False
    if _is_platform_tool(tool_name):
        # Not namespaced to a user target, so no ownership record to consult
        # and no lookup cost for shared tools.
        return True
    if not actor_id:
        # A user-owned tool with no identified caller: fail closed.
        return False
    target = target_of_tool(tool_name)
    if target is None:
        return False
    return actor_owns_target(actor_id, target)


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------


def _passthrough(event: dict) -> dict:
    """Echo the exchange back unchanged, in the MCP output schema.

    Shape comes from the developer guide's MCP interceptor payload reference
    (gateway-interceptors-types), not from an example elsewhere. Two earlier
    attempts were wrong for instructive reasons:

    1. ``{"mcp": {}}`` -- omitting the transformed field entirely. The field must
       be PRESENT; without it the gateway forwarded nothing and the MCP client
       died mid-handshake with "Connection closed".

    2. Echoing ``{headers, body}``. For an MCP target the request output accepts
       ONLY ``body``; ``headers``, ``path`` and ``httpMethod`` are input-side
       fields. Returning headers made the gateway answer 500 before it called
       the target. The HTTP-target output DOES take headers, which is where that
       shape came from -- the two payload families are documented on the same
       page and differ in exactly this way.

    Body only, for both phases. The guide states that returned fields override
    the original and omitted ones leave it intact, so echoing just the body
    preserves statusCode and headers -- including ``Mcp-Session-Id``, which the
    session depends on -- and sidesteps the streaming rule that only the first
    event may set statusCode or headers.
    """
    response = _gateway_response(event)
    if response is not None:
        return {
            "interceptorOutputVersion": _OUTPUT_VERSION,
            "mcp": {"transformedGatewayResponse": {"body": response.get("body")}},
        }
    return {
        "interceptorOutputVersion": _OUTPUT_VERSION,
        "mcp": {"transformedGatewayRequest": {"body": _request_body(event)}},
    }


def _deny(request_id: object, tool_name: str) -> dict:
    """Short-circuit the call with a JSON-RPC error.

    Presence of ``transformedGatewayResponse`` in REQUEST output makes the
    gateway reply with this immediately instead of calling the target.

    ``statusCode`` stays 200 because this is a JSON-RPC-level error, not an
    HTTP transport failure — MCP clients parse the body for the error and a
    non-200 tends to surface as a connection fault instead, which reads to
    the user as "the gateway is broken" rather than "not permitted".
    """
    return {
        "interceptorOutputVersion": _OUTPUT_VERSION,
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": _ERROR_CODE_FORBIDDEN,
                        "message": (
                            f"Tool {tool_name!r} is not available to this actor."
                        ),
                    },
                },
            }
        },
    }


def _replace_response_body(event: dict, body: dict) -> dict:
    """Build RESPONSE output carrying a rewritten body.

    Body only. Per the MCP interceptor reference, returned fields override the
    original and omitted fields leave it as it was, so there is no reason to
    echo statusCode or headers -- and not echoing them is what makes this safe
    under streaming, where only the first invocation may set either.
    """
    return {
        "interceptorOutputVersion": _OUTPUT_VERSION,
        "mcp": {"transformedGatewayResponse": {"body": body}},
    }


# ---------------------------------------------------------------------------
# Phase handlers
# ---------------------------------------------------------------------------


def handle_request(event: dict) -> dict:
    """REQUEST phase: deny a call the actor may not make.

    Two independent, fail-closed gates and a tool must clear BOTH:

    - a platform-wide denylist that applies to every caller, and
    - the per-actor ownership check.
    """
    method = _method(event)
    if method != "tools/call":
        return _passthrough(event)

    tool_name = _called_tool_name(event)
    actor_id = _actor_id(event)

    # Platform denylist first. It is not scoped to an owner and must hold even
    # when no actor can be resolved. ``_may_use`` also enforces it (so the
    # RESPONSE phase filters the same tools identically), but denying here
    # yields a clear, phase-specific denial log.
    if _is_denied_tool(tool_name):
        logger.warning(
            "interceptor: denied tools/call (denylist) name=%s actor_present=%s",
            tool_name, bool(actor_id),
        )
        return _deny(_request_body(event).get("id"), tool_name)

    if _may_use(actor_id, tool_name):
        return _passthrough(event)

    logger.warning(
        "interceptor: denied tools/call name=%s actor_present=%s target=%s",
        tool_name, bool(actor_id), target_of_tool(tool_name),
    )
    return _deny(_request_body(event).get("id"), tool_name)


def _filter_tool_entries(entries: list, actor_id: str) -> list:
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and _may_use(actor_id, name):
            kept.append(entry)
    return kept


def handle_response(event: dict) -> dict:
    """RESPONSE phase: strip tools the actor may not see.

    Covers both discovery surfaces:

    - ``tools/list`` — ``result.tools``.
    - the gateway search tool — its *result* enumerates tools too, so
      allowing the call without filtering the result would leak other
      actors' tool names and descriptions.
    """
    response = _gateway_response(event)
    if response is None:
        # No response to transform; nothing to do.
        return _passthrough(event)

    body = response.get("body")
    if not isinstance(body, dict):
        return _passthrough(event)

    # A REQUEST-phase denial still reaches this phase for MCP targets.
    # Leave error bodies exactly as they are.
    if "error" in body:
        return _passthrough(event)

    result = body.get("result")
    if not isinstance(result, dict):
        return _passthrough(event)

    method = _method(event)
    actor_id = _actor_id(event)

    if method == "tools/list":
        entries = result.get("tools")
        if not isinstance(entries, list):
            return _passthrough(event)
        kept = _filter_tool_entries(entries, actor_id)
        if len(kept) == len(entries):
            return _passthrough(event)
        logger.info(
            "interceptor: filtered tools/list %d -> %d (actor_present=%s)",
            len(entries), len(kept), bool(actor_id),
        )
        new_body = dict(body)
        new_result = dict(result)
        new_result["tools"] = kept
        new_body["result"] = new_result
        return _replace_response_body(event, new_body)

    if method == "tools/call" and _called_tool_name(event) == _GATEWAY_SEARCH_TOOL:
        filtered = _filter_search_result(result, actor_id)
        if filtered is None:
            return _passthrough(event)
        new_body = dict(body)
        new_body["result"] = filtered
        return _replace_response_body(event, new_body)

    return _passthrough(event)


def _filter_search_result(result: dict, actor_id: str) -> dict | None:
    """Filter a search result's tool list, or ``None`` if nothing changed.

    The search payload has been observed as ``{"tools": [...]}`` but the
    envelope is not contractual, so several plausible keys are checked.
    Anything unrecognised is left untouched rather than guessed at — the
    alternative, blanking a result we failed to parse, would break search
    for everyone on a payload change.
    """
    for key in ("tools", "results", "capabilities"):
        entries = result.get(key)
        if isinstance(entries, list):
            kept = _filter_tool_entries(entries, actor_id)
            if len(kept) == len(entries):
                return None
            logger.info(
                "interceptor: filtered search '%s' %d -> %d",
                key, len(entries), len(kept),
            )
            new_result = dict(result)
            new_result[key] = kept
            return new_result
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def handler(event, context):  # noqa: ARG001 — Lambda signature
    """Dispatch on interceptor phase.

    A raised exception here would fail the gateway call, so failures are
    contained: the REQUEST phase denies and the RESPONSE phase blanks the
    tool list, both of which are the safe direction.
    """
    if not isinstance(event, dict):
        logger.error("interceptor: non-dict event; denying")
        return _deny(None, "<unknown>")

    is_response_phase = _gateway_response(event) is not None
    # One line per invocation. This ran 60 times in production without emitting
    # anything, so the only available evidence that the component enforcing a
    # security boundary was working at all was its billing duration. The actor
    # is logged as a prefix: enough to correlate with a session, not enough to
    # be a durable record of who used what.
    actor = _actor_id(event)
    method = _method(event)
    # Keys only, never values: the body carries user content and the headers
    # carry a bearer token. Two wrong inferences about this payload's shape have
    # each cost a deploy cycle, so the shape itself is now on the record.
    logger.info(
        "interceptor: mcp_keys=%s request_keys=%s response_keys=%s",
        sorted(_mcp(event).keys()),
        sorted(_gateway_request(event).keys()),
        sorted((_gateway_response(event) or {}).keys()),
    )
    logger.info(
        "interceptor: phase=%s method=%s actor=%s tool=%s",
        "RESPONSE" if is_response_phase else "REQUEST",
        method or "<none>",
        (actor[:8] + "...") if actor else "<unresolved>",
        _called_tool_name(event) or "-",
    )
    # Header NAMES only — never values. This is the evidence channel for the
    # async delegation path (Wave 1 / option E): it shows whether the
    # DELEGATION_GRANT_HEADER actually survived the gateway hop into the
    # interceptor payload. Values are NOT logged (they carry the bearer token
    # and the signed grant); a boolean presence flag is enough to prove
    # arrival. On a request phase only (the response phase's headers are the
    # gateway's, not the caller's).
    if not is_response_phase:
        _hdrs = _gateway_request(event).get("headers")
        if isinstance(_hdrs, dict):
            logger.info(
                "interceptor: header_names=%s delegation_grant_present=%s",
                sorted(k.lower() for k in _hdrs if isinstance(k, str)),
                _header_value(event, DELEGATION_GRANT_HEADER) is not None,
            )
    try:
        if is_response_phase:
            return handle_response(event)
        return handle_request(event)
    except Exception:
        logger.exception("interceptor: unhandled error; failing closed")
        if is_response_phase:
            # Blank the discovery surface rather than returning it unfiltered.
            return _replace_response_body(
                event,
                {
                    "jsonrpc": "2.0",
                    "id": _request_body(event).get("id"),
                    "result": {"tools": []},
                },
            )
        return _deny(_request_body(event).get("id"), _called_tool_name(event))
