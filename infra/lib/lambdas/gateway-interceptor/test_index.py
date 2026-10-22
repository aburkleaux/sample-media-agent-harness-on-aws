"""Tests for the gateway interceptor's per-actor tool scoping.

The security-relevant assertions are the negative ones: a caller must not
see or invoke another actor's tools, and every path that cannot establish
ownership must deny rather than allow.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import index  # noqa: E402
import ownership  # noqa: E402
from owner_naming import is_user_target, target_of_tool  # noqa: E402


def assert_untouched(event: dict, out: dict) -> None:
    """Assert `out` echoes `event`'s body in the documented MCP output schema.

    Two earlier versions of this helper pinned two different bugs as correct:
    first `out["mcp"] == {}` (field absent -> gateway forwarded nothing ->
    "Connection closed"), then a {headers, body} echo (headers are not part of
    the MCP request output -> gateway 500). The schema is body-only, from
    gateway-interceptors-types in the developer guide.
    """
    mcp = out["mcp"]
    assert out["interceptorOutputVersion"] == "1.0"
    response = event.get("mcp", {}).get("gatewayResponse")
    if response is not None:
        got = mcp["transformedGatewayResponse"]
        assert set(got) == {"body"}, f"MCP response output is body-only: {got}"
        assert got["body"] == response.get("body")
    else:
        got = mcp["transformedGatewayRequest"]
        assert set(got) == {"body"}, f"MCP request output is body-only: {got}"
        assert got["body"] == event["mcp"]["gatewayRequest"]["body"]


ALICE = "USER#alice"
BOB = "e1b7c0de-1111-2222-3333-444455556666"

ALICE_TARGET = "usr-notes-a1"
BOB_TARGET = "usr-secrets-b2"

ALICE_TOOL = f"{ALICE_TARGET}___read_note"
BOB_TOOL = f"{BOB_TARGET}___read_secret"
SEARCH_TOOL = "x_amz_bedrock_agentcore_search"
PLATFORM_TOOL = "media-outputs___get_download_url"

# The recorded ownership the fake registry returns. This is the single source of
# truth in production too -- written by the agent at registration time -- which
# is why no test derives an owner from a target name.
_OWNERS = {ALICE_TARGET: ALICE, BOB_TARGET: BOB}


class _FakeTable:
    def __init__(self, owners: dict, fail: bool = False):
        self.owners = owners
        self.fail = fail
        self.reads = 0

    def get_item(self, Key):  # noqa: N803 — boto3 casing
        self.reads += 1
        if self.fail:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
        assert Key["PK"] == ownership.OWNER_PK
        owner = self.owners.get(Key["SK"])
        return {"Item": {"ownerActorId": owner}} if owner else {}


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    """Bind a fake registry and clear the cache between tests."""
    table = _FakeTable(dict(_OWNERS))
    monkeypatch.setattr(ownership, "_get_table", lambda: table)
    ownership.reset_cache()
    yield table
    ownership.reset_cache()


# ---------------------------------------------------------------------------
# Target-name convention (string handling only -- no derivation)
# ---------------------------------------------------------------------------


def test_target_of_tool():
    assert target_of_tool(ALICE_TOOL) == ALICE_TARGET
    assert target_of_tool(SEARCH_TOOL) is None


def test_target_resolved_as_first_segment_of_multi_separator_name():
    assert target_of_tool(f"{ALICE_TARGET}___server___deep_tool") == ALICE_TARGET


def test_user_target_detection_is_a_prefix_check_only():
    """A true result means "consult the record", never "caller owns it"."""
    assert is_user_target(ALICE_TARGET)
    assert not is_user_target("media-outputs")
    assert not is_user_target(None)


# ---------------------------------------------------------------------------
# Recorded ownership
# ---------------------------------------------------------------------------


def test_owner_comes_from_the_record():
    assert ownership.owner_of_target(ALICE_TARGET) == ALICE
    assert ownership.owner_of_target(BOB_TARGET) == BOB


def test_unknown_target_has_no_owner():
    assert ownership.owner_of_target("usr-nope-zz") == ""


def test_actor_owns_only_its_own_target():
    assert ownership.actor_owns_target(ALICE, ALICE_TARGET)
    assert not ownership.actor_owns_target(BOB, ALICE_TARGET)
    assert not ownership.actor_owns_target("", ALICE_TARGET)


def test_repeat_lookups_are_cached(_registry):
    for _ in range(5):
        ownership.owner_of_target(ALICE_TARGET)
    assert _registry.reads == 1, "steady state must not read per call"


def test_unknown_target_is_read_once_before_denying(_registry):
    """A miss must resolve immediately so a new registration works at once."""
    ownership.owner_of_target("usr-fresh-xx")
    ownership.owner_of_target("usr-fresh-xx")
    assert _registry.reads == 1


def test_read_failure_denies_and_is_not_cached(monkeypatch):
    failing = _FakeTable({}, fail=True)
    monkeypatch.setattr(ownership, "_get_table", lambda: failing)
    ownership.reset_cache()
    assert ownership.owner_of_target(ALICE_TARGET) == ""
    # A transient outage must not become a minute of denials.
    assert ownership.owner_of_target(ALICE_TARGET) == ""
    assert failing.reads == 2


def test_missing_table_config_denies(monkeypatch):
    monkeypatch.setattr(ownership, "_get_table", lambda: None)
    ownership.reset_cache()
    assert ownership.owner_of_target(ALICE_TARGET) == ""


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _jwt(claims: dict) -> str:
    """Build an unsigned-but-well-formed JWT.

    The signature is deliberately garbage: the interceptor must not verify it
    (the gateway already did), so a test that had to produce a real signature
    would be testing the wrong contract.
    """
    def seg(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.not-a-real-signature"


def _bearer(actor: str) -> str:
    return f"Bearer {_jwt({'sub': actor})}"


def _request(method: str, actor: str | None = None, tool: str | None = None,
             rid: int = 1, headers: dict | None = None,
             grant: str | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
    if tool is not None:
        body["params"] = {"name": tool, "arguments": {}}
    hdrs = dict(headers) if headers else {}
    if actor is not None:
        hdrs["Authorization"] = _bearer(actor)
    if grant is not None:
        hdrs[index.DELEGATION_GRANT_HEADER] = grant
    req: dict = {"path": "/mcp", "httpMethod": "POST", "body": body}
    if hdrs:
        req["headers"] = hdrs
    return {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": req}}


def _response(method: str, result: dict, actor: str | None = None,
              tool: str | None = None, streaming: bool = False,
              rid: int = 1, grant: str | None = None) -> dict:
    event = _request(method, actor=actor, tool=tool, rid=rid, grant=grant)
    resp: dict = {
        "statusCode": 200,
        "headers": {"Mcp-Session-Id": "s1"},
        "body": {"jsonrpc": "2.0", "id": rid, "result": result},
    }
    if streaming:
        resp["isStreamingResponse"] = True
    event["mcp"]["gatewayResponse"] = resp
    return event


def _tool_entries(*names: str) -> list:
    return [{"name": n, "description": f"desc {n}"} for n in names]


def _denied(out: dict) -> bool:
    resp = out.get("mcp", {}).get("transformedGatewayResponse")
    return bool(resp) and "error" in resp.get("body", {})


def _returned_tools(out: dict) -> list | None:
    resp = out.get("mcp", {}).get("transformedGatewayResponse")
    if not resp:
        return None
    return resp["body"]["result"]["tools"]


# ---------------------------------------------------------------------------
# REQUEST phase — enforcement
# ---------------------------------------------------------------------------


def test_owner_may_call_own_tool():
    out = index.handler(_request("tools/call", actor=ALICE, tool=ALICE_TOOL), None)
    assert not _denied(out)


def test_actor_may_not_call_another_actors_tool():
    out = index.handler(_request("tools/call", actor=BOB, tool=ALICE_TOOL), None)
    assert _denied(out)


def test_denial_is_jsonrpc_error_preserving_request_id():
    out = index.handler(
        _request("tools/call", actor=BOB, tool=ALICE_TOOL, rid=77), None
    )
    resp = out["mcp"]["transformedGatewayResponse"]
    assert resp["statusCode"] == 200
    assert resp["body"]["id"] == 77
    assert resp["body"]["error"]["code"] == index._ERROR_CODE_FORBIDDEN


def test_missing_token_denies_user_tool():
    """No identified caller must not mean unrestricted access."""
    out = index.handler(_request("tools/call", tool=ALICE_TOOL), None)
    assert _denied(out)


def test_headers_absent_entirely_denies():
    """passRequestHeaders disabled leaves no headers field, so no token."""
    event = _request("tools/call", tool=ALICE_TOOL)
    event["mcp"]["gatewayRequest"].pop("headers", None)
    assert _denied(index.handler(event, None))


def test_authorization_header_is_case_insensitive():
    event = _request("tools/call", tool=ALICE_TOOL,
                     headers={"authorization": _bearer(ALICE)})
    assert not _denied(index.handler(event, None))


def test_search_tool_callable_by_anyone():
    """A gateway without a reachable search tool drops out of narrowing."""
    out = index.handler(_request("tools/call", actor=BOB, tool=SEARCH_TOOL), None)
    assert not _denied(out)


def test_platform_tool_callable():
    out = index.handler(_request("tools/call", actor=BOB, tool=PLATFORM_TOOL), None)
    assert not _denied(out)


def test_unnamed_tool_call_is_denied():
    out = index.handler(_request("tools/call", actor=ALICE), None)
    assert _denied(out)


def test_non_call_methods_pass_through():
    """`initialize` matters most here: it is the MCP handshake, and breaking it
    takes down the whole connection before any tool is ever listed. That is
    exactly what an empty passthrough did in production."""
    for method in ("tools/list", "initialize", "ping", "notifications/initialized"):
        event = _request(method, actor=ALICE)
        out = index.handler(event, None)
        assert not _denied(out), method
        assert_untouched(event, out)


def test_method_recovered_from_raw_body_when_parsed_absent():
    """Guard the fallback path so a payload variation can't blind us."""
    event = _request("tools/call", actor=BOB, tool=ALICE_TOOL)
    raw = json.dumps(event["mcp"]["gatewayRequest"]["body"])
    del event["mcp"]["gatewayRequest"]["body"]
    event["mcp"]["rawGatewayRequest"] = {"body": raw}
    assert _denied(index.handler(event, None))


# ---------------------------------------------------------------------------
# RESPONSE phase — filtering
# ---------------------------------------------------------------------------


def test_tools_list_keeps_only_own_tools():
    event = _response(
        "tools/list",
        {"tools": _tool_entries(ALICE_TOOL, BOB_TOOL, SEARCH_TOOL)},
        actor=ALICE,
    )
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [ALICE_TOOL, SEARCH_TOOL]


def test_tools_list_untouched_when_nothing_to_filter():
    event = _response(
        "tools/list", {"tools": _tool_entries(ALICE_TOOL, SEARCH_TOOL)}, actor=ALICE
    )
    out = index.handler(event, None)
    assert_untouched(event, out)


def test_tools_list_without_token_is_emptied_of_user_tools():
    event = _response("tools/list", {"tools": _tool_entries(ALICE_TOOL, SEARCH_TOOL)})
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [SEARCH_TOOL]


def test_filtered_response_returns_body_only():
    """Filtering rewrites the body and nothing else.

    This test previously asserted that statusCode and headers were echoed. They
    should NOT be: the MCP response output overrides only the fields it returns
    and leaves omitted ones intact, so returning body alone preserves the
    original head -- including Mcp-Session-Id, which the session depends on --
    and needs no special case for streaming.
    """
    event = _response(
        "tools/list", {"tools": _tool_entries(ALICE_TOOL, BOB_TOOL)}, actor=ALICE
    )
    resp = index.handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert set(resp) == {"body"}, resp
    names = [t["name"] for t in resp["body"]["result"]["tools"]]
    assert names == [ALICE_TOOL], "the filter itself must still work"


def test_streaming_response_also_returns_body_only():
    """Streaming needed a special case only while the head was being echoed."""
    event = _response(
        "tools/list", {"tools": _tool_entries(ALICE_TOOL, BOB_TOOL)},
        actor=ALICE, streaming=True,
    )
    resp = index.handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert "statusCode" not in resp
    assert "headers" not in resp
    assert resp["body"]["result"]["tools"]


def test_search_results_are_filtered():
    """Search enumerates tools, so an unfiltered result leaks other actors."""
    event = _response(
        "tools/call",
        {"tools": _tool_entries(ALICE_TOOL, BOB_TOOL)},
        actor=ALICE, tool=SEARCH_TOOL,
    )
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [ALICE_TOOL]


def test_search_envelope_variant_is_filtered():
    event = _response(
        "tools/call",
        {"results": _tool_entries(ALICE_TOOL, BOB_TOOL)},
        actor=ALICE, tool=SEARCH_TOOL,
    )
    resp = index.handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert [t["name"] for t in resp["body"]["result"]["results"]] == [ALICE_TOOL]


def test_unrecognised_search_envelope_left_alone():
    """Blanking an unparsed result would break search for everyone."""
    event = _response(
        "tools/call", {"payload": "opaque"}, actor=ALICE, tool=SEARCH_TOOL
    )
    assert_untouched(event, index.handler(event, None))


def test_tool_result_is_not_filtered():
    """A normal tool's output must pass through untouched."""
    event = _response(
        "tools/call",
        {"content": [{"type": "text", "text": "hello"}]},
        actor=ALICE, tool=ALICE_TOOL,
    )
    assert_untouched(event, index.handler(event, None))


def test_request_phase_denial_error_passes_through_response_phase():
    """MCP targets still run the RESPONSE interceptor after a REQUEST deny."""
    event = _request("tools/call", actor=BOB, tool=ALICE_TOOL)
    event["mcp"]["gatewayResponse"] = {
        "statusCode": 200,
        "body": {
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": index._ERROR_CODE_FORBIDDEN, "message": "no"},
        },
    }
    out = index.handler(event, None)
    assert_untouched(event, out)


def test_malformed_entries_are_dropped_not_crashed():
    event = _response(
        "tools/list",
        {"tools": [{"name": ALICE_TOOL}, "not-a-dict", {"no_name": 1}]},
        actor=ALICE,
    )
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [ALICE_TOOL]


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


def test_non_dict_event_denies():
    assert _denied(index.handler("garbage", None))


def test_request_phase_error_denies(monkeypatch):
    monkeypatch.setattr(
        index, "_may_use", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert _denied(index.handler(_request("tools/call", actor=ALICE, tool=ALICE_TOOL), None))


def test_response_phase_error_blanks_the_listing(monkeypatch):
    monkeypatch.setattr(
        index, "_filter_tool_entries",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    event = _response("tools/list", {"tools": _tool_entries(ALICE_TOOL)}, actor=ALICE)
    assert _returned_tools(index.handler(event, None)) == []


def test_response_without_result_passes_through():
    event = _request("tools/list", actor=ALICE)
    event["mcp"]["gatewayResponse"] = {"statusCode": 500, "body": {"jsonrpc": "2.0"}}
    assert_untouched(event, index.handler(event, None))


def test_identity_claim_is_configurable(monkeypatch):
    monkeypatch.setattr(index, "IDENTITY_CLAIM", "custom:tenant_id")
    event = _request("tools/call", tool=ALICE_TOOL,
                     headers={"Authorization": f"Bearer {_jwt({'custom:tenant_id': ALICE})}"})
    assert not _denied(index.handler(event, None))


def test_signature_is_not_verified():
    """The gateway already validated it; re-verifying would need JWKS on the
    hot path and a vendored JWT library in this dependency-free asset."""
    event = _request("tools/call", tool=ALICE_TOOL,
                     headers={"Authorization": f"Bearer {_jwt({'sub': ALICE})}"})
    assert not _denied(index.handler(event, None))


def test_token_without_identity_claim_denies():
    event = _request("tools/call", tool=ALICE_TOOL,
                     headers={"Authorization": f"Bearer {_jwt({'aud': 'x'})}"})
    assert _denied(index.handler(event, None))


def test_malformed_token_denies():
    for bad in ("Bearer not.a.jwt", "Bearer ", "Bearer abc", "Basic xyz"):
        event = _request("tools/call", tool=ALICE_TOOL,
                         headers={"Authorization": bad})
        assert _denied(index.handler(event, None)), bad


def test_asserted_actor_header_is_ignored():
    """No header fallback: it would reduce the scheme to the weaker source."""
    event = _request("tools/call", tool=ALICE_TOOL,
                     headers={"X-Actor-Id": ALICE})
    assert _denied(index.handler(event, None))


# ---------------------------------------------------------------------------
# The output contract, pinned from the API reference rather than from inference.
# ---------------------------------------------------------------------------


def test_every_output_carries_a_transformed_field():
    """The gateway requires the transformed field to be present.

    From the API reference: the request interceptor "must return a response with
    the transformedGatewayRequest field", annotated "this field must be added by
    your code". The CDK's own pass-through example echoes
    event["mcp"]["gatewayRequest"] verbatim.

    Returning `{"mcp": {}}` instead -- on the assumption that supplying no
    transformation meant "leave it alone" -- meant the gateway forwarded an empty
    request. The MCP client died during `initialize` with `McpError: Connection
    closed`, and because the Lambda returned 200 in under 2ms it looked healthy
    in every metric available.

    This asserts the shape for every phase and method so the assumption cannot
    come back.
    """
    cases = [
        _request("initialize", actor=ALICE),
        _request("tools/list", actor=ALICE),
        _request("ping", actor=ALICE),
        _request("tools/call", tool=f"{ALICE_TARGET}___notes", actor=ALICE),
        _response("initialize", {"protocolVersion": "2025-03-26"}, actor=ALICE),
        _response("tools/list", {"tools": []}, actor=ALICE),
    ]
    for event in cases:
        out = index.handler(event, None)
        mcp = out["mcp"]
        assert out["interceptorOutputVersion"] == "1.0"
        assert mcp, f"empty mcp is an empty transformation: {event}"
        assert (
            "transformedGatewayRequest" in mcp or "transformedGatewayResponse" in mcp
        ), f"no transformed field for {event}"


def test_initialize_body_is_echoed_verbatim():
    """The handshake body must survive untouched -- protocolVersion and
    capabilities are negotiated in it, and altering it breaks the session."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "harness", "version": "1.0"},
        },
    }
    event = _response("initialize", body["result"], actor=ALICE)
    out = index.handler(event, None)
    echoed = out["mcp"]["transformedGatewayResponse"]["body"]
    assert echoed == event["mcp"]["gatewayResponse"]["body"]


# ---------------------------------------------------------------------------
# Platform-wide tool denylist
#
# Independent of per-actor ownership: these tools execute under the agent
# runtime's IAM role and must be unreachable for EVERY caller, at the gateway.
# The published names carry TWO separators
# (``<target>___aws___<verb>``); the match keys on the bare name after a single
# prefix strip.
# ---------------------------------------------------------------------------

AWS_TARGET = "mediaworks-harness-aws-mcp"
AWS_CALL = f"{AWS_TARGET}___aws___call_aws"
AWS_RUN = f"{AWS_TARGET}___aws___run_script"
AWS_PRESIGN = f"{AWS_TARGET}___aws___get_presigned_url"
AWS_SEARCH = f"{AWS_TARGET}___aws___search_documentation"  # allowed


def test_bare_tool_name_strips_only_the_target_prefix():
    """A single split removes the ``<target>___`` prefix, leaving a bare name
    that may itself contain the separator intact -- the two-separator case."""
    assert index._bare_tool_name(AWS_CALL) == "aws___call_aws"
    assert index._bare_tool_name(ALICE_TOOL) == "read_note"
    assert index._bare_tool_name(SEARCH_TOOL) == SEARCH_TOOL


def test_denylist_matches_bare_name_after_two_separator_prefix():
    assert index._is_denied_tool(AWS_CALL)
    assert index._is_denied_tool(AWS_RUN)
    assert index._is_denied_tool(AWS_PRESIGN)
    assert not index._is_denied_tool(AWS_SEARCH)


def test_unparseable_tool_name_is_denied():
    """Fail closed: an empty/ambiguous name is treated as denied."""
    assert index._is_denied_tool("")


@pytest.mark.parametrize("tool", [AWS_CALL, AWS_RUN, AWS_PRESIGN])
def test_denied_tool_call_is_refused(tool):
    out = index.handler(_request("tools/call", actor=ALICE, tool=tool), None)
    assert _denied(out)


@pytest.mark.parametrize("tool", [AWS_CALL, AWS_RUN, AWS_PRESIGN])
def test_denied_tool_refused_even_when_actor_unresolvable(tool):
    """Platform policy, not per-owner scoping: it holds with no identity."""
    out = index.handler(_request("tools/call", tool=tool), None)
    assert _denied(out)


def test_allowed_aws_tool_still_passes():
    """A non-denied tool on the same target stays reachable for everyone."""
    out = index.handler(_request("tools/call", actor=BOB, tool=AWS_SEARCH), None)
    assert not _denied(out)


def test_denied_tool_absent_from_tools_list():
    event = _response(
        "tools/list",
        {"tools": _tool_entries(AWS_CALL, AWS_RUN, AWS_PRESIGN, AWS_SEARCH)},
        actor=ALICE,
    )
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [AWS_SEARCH]


def test_denied_tool_absent_from_tools_list_without_actor():
    """The denylist filters discovery for anonymous callers too."""
    event = _response("tools/list", {"tools": _tool_entries(AWS_CALL, AWS_SEARCH)})
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [AWS_SEARCH]


def test_denied_call_reuses_the_existing_deny_shape():
    """Denylist denials keep the existing output shape exactly: body(+status)
    only, JSON-RPC error, status 200, request id preserved."""
    out = index.handler(
        _request("tools/call", actor=ALICE, tool=AWS_CALL, rid=99), None
    )
    resp = out["mcp"]["transformedGatewayResponse"]
    assert set(resp) == {"statusCode", "body"}
    assert resp["statusCode"] == 200
    assert resp["body"]["id"] == 99
    assert resp["body"]["error"]["code"] == index._ERROR_CODE_FORBIDDEN


def test_denylist_and_ownership_both_apply():
    """A tool must clear BOTH gates: ownership still governs user tools while
    the denylist independently removes a denied platform tool. The existing
    per-actor behaviour is unchanged -- Alice keeps her tool, Bob's is gone."""
    event = _response(
        "tools/list",
        {"tools": _tool_entries(ALICE_TOOL, BOB_TOOL, AWS_CALL, SEARCH_TOOL)},
        actor=ALICE,
    )
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [ALICE_TOOL, SEARCH_TOOL]
    # And Alice still cannot invoke the denied platform tool.
    assert _denied(
        index.handler(_request("tools/call", actor=ALICE, tool=AWS_CALL), None)
    )


# ---------------------------------------------------------------------------
# Signed delegation grants (Wave 1 / option E)
#
# For async plan traffic there is no user JWT `sub`. The caller instead presents
# a KMS-signed grant over {actor_id, plan_id, exp}. The interceptor derives the
# actor FROM THE VERIFIED GRANT — never from any asserted actor_id. Every
# verification failure (forged, expired, wrong key, absent) must DENY, and there
# must be no path where a failure falls back to trusting an assertion.
#
# The security assertions here are the negative ones; they are the evidence the
# scheme is fail-closed by construction.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import time as _time  # noqa: E402

import delegation  # noqa: E402

_GRANT_KEY_ARN = "arn:aws:kms:us-west-2:123456789012:key/test-delegation-key"


class _FakeKms:
    """Deterministic stand-in for KMS asymmetric sign/verify.

    `sign` MACs the message under a fixed secret; `verify` recomputes and
    compares. This lets a test mint a genuinely-valid grant and, separately,
    forge one (any other signature bytes) that must fail verification —
    without a real KMS call.
    """

    _SECRET = b"fake-kms-delegation-secret"

    def _mac(self, message: bytes) -> bytes:
        return b"SIG:" + hashlib.sha256(self._SECRET + message).digest()

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):  # noqa: N803
        assert MessageType == "RAW"
        assert SigningAlgorithm == delegation.SIGNING_ALGORITHM
        return {"Signature": self._mac(Message)}

    def verify(self, *, KeyId, Message, MessageType, Signature, SigningAlgorithm):  # noqa: N803
        assert MessageType == "RAW"
        return {"SignatureValid": Signature == self._mac(Message)}


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_grant(actor: str, plan_id: str = "plan-1", *,
                exp: float | None = None, kms: _FakeKms | None = None,
                forged: bool = False) -> str:
    """Build a delegation grant token in the wire format the verifier expects."""
    kms = kms or _FakeKms()
    if exp is None:
        exp = _time.time() + 3600
    header = {"alg": "KMS_ECDSA_SHA_256", "typ": "delegation-grant", "v": 1}
    payload = {"actor_id": actor, "plan_id": plan_id,
               "iat": int(exp) - 60, "exp": exp}
    hb = _b64u(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    pb = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{hb}.{pb}".encode("ascii")
    if forged:
        sig = b"forged-not-a-real-signature"
    else:
        sig = kms.sign(KeyId="k", Message=signing_input, MessageType="RAW",
                       SigningAlgorithm=delegation.SIGNING_ALGORITHM)["Signature"]
    return f"{hb}.{pb}.{_b64u(sig)}"


@pytest.fixture()
def _grant_key(monkeypatch):
    """Bind a fake KMS and a configured verify key for the delegation module."""
    fake = _FakeKms()
    monkeypatch.setenv(delegation.KEY_ENV, _GRANT_KEY_ARN)
    monkeypatch.setattr(delegation, "_kms_client", lambda: fake)
    return fake


# ---- valid grant: permits ONLY the granted actor's own target ----


def test_valid_grant_permits_the_actors_own_tool(_grant_key):
    grant = _make_grant(ALICE, kms=_grant_key)
    out = index.handler(_request("tools/call", tool=ALICE_TOOL, grant=grant), None)
    assert not _denied(out)


def test_valid_grant_scopes_tools_list_to_the_grant_actor(_grant_key):
    grant = _make_grant(ALICE, kms=_grant_key)
    event = _response(
        "tools/list",
        {"tools": _tool_entries(ALICE_TOOL, BOB_TOOL, SEARCH_TOOL)},
        grant=grant,
    )
    names = [t["name"] for t in _returned_tools(index.handler(event, None))]
    assert names == [ALICE_TOOL, SEARCH_TOOL]


def test_valid_grant_permits_only_that_actor_not_others(_grant_key):
    """A valid grant for ALICE authorises ALICE's target and no one else's."""
    grant = _make_grant(ALICE, kms=_grant_key)
    assert not _denied(index.handler(_request("tools/call", tool=ALICE_TOOL, grant=grant), None))
    # Same grant, Bob's tool: the actor derived from the grant is ALICE, who
    # does not own BOB_TARGET.
    assert _denied(index.handler(_request("tools/call", tool=BOB_TOOL, grant=grant), None))


# ---- the four required deny cases ----


def test_forged_grant_signature_denies(_grant_key):
    grant = _make_grant(ALICE, forged=True)
    out = index.handler(_request("tools/call", tool=ALICE_TOOL, grant=grant), None)
    assert _denied(out)


def test_expired_grant_denies(_grant_key):
    grant = _make_grant(ALICE, exp=_time.time() - 1, kms=_grant_key)
    out = index.handler(_request("tools/call", tool=ALICE_TOOL, grant=grant), None)
    assert _denied(out)


def test_grant_for_actor_a_denied_against_actor_b_target(_grant_key):
    """Grant minted for ALICE, presented against BOB's usr- target => deny."""
    grant = _make_grant(ALICE, kms=_grant_key)
    out = index.handler(_request("tools/call", tool=BOB_TOOL, grant=grant), None)
    assert _denied(out)


def test_absent_grant_on_async_dispatch_denies_not_falls_back(_grant_key):
    """An async dispatch with NO grant and no user `sub` must deny, and an
    asserted actor_id it also stamps must be ignored — never a fallback."""
    # No grant header; a token with no identity claim (M2M-shaped) plus an
    # asserted X-Actor-Id header the runtime might stamp.
    event = _request("tools/call", tool=ALICE_TOOL,
                     headers={"Authorization": f"Bearer {_jwt({'aud': 'm2m'})}",
                              "X-Actor-Id": ALICE})
    assert _denied(index.handler(event, None))


# ---- structural fail-closed: a present grant NEVER falls back to the JWT ----


def test_present_but_forged_grant_does_not_fall_back_to_valid_jwt(_grant_key):
    """Even with a valid ALICE JWT alongside, a present-but-forged grant denies.

    This is the structural property: once a grant header is present the grant is
    the sole identity source, so a verification failure cannot degrade into
    trusting any other input."""
    forged = _make_grant(ALICE, forged=True)
    event = _request("tools/call", tool=ALICE_TOOL, actor=ALICE, grant=forged)
    assert _denied(index.handler(event, None))


def test_grant_denies_when_no_verify_key_configured(monkeypatch):
    """No verification key => a grant cannot be authenticated => deny."""
    monkeypatch.delenv(delegation.KEY_ENV, raising=False)
    monkeypatch.setattr(delegation, "_kms_client",
                        lambda: (_ for _ in ()).throw(AssertionError("must not call KMS")))
    grant = _make_grant(ALICE)
    assert _denied(index.handler(_request("tools/call", tool=ALICE_TOOL, grant=grant), None))


def test_valid_grant_still_cannot_call_denied_platform_tool(_grant_key):
    """The grant is an actor source, not a denylist bypass: platform-wide
    denied tools stay denied even with a valid grant."""
    grant = _make_grant(ALICE, kms=_grant_key)
    out = index.handler(_request("tools/call", tool=AWS_CALL, grant=grant), None)
    assert _denied(out)


# ---- normal (interactive) traffic is unaffected by the new path ----


def test_normal_jwt_traffic_unaffected_no_grant_header(_grant_key):
    """With no grant header, identity resolution is exactly the JWT path: a
    valid ALICE token still authorises ALICE's tool and still denies BOB's."""
    assert not _denied(index.handler(_request("tools/call", actor=ALICE, tool=ALICE_TOOL), None))
    assert _denied(index.handler(_request("tools/call", actor=BOB, tool=ALICE_TOOL), None))


def test_grant_path_is_only_taken_when_header_present(_grant_key):
    """No grant header => `_actor_id` returns the JWT sub verbatim (grant module
    not consulted). A stray KMS call here would indicate the grant path ran."""
    monkeypatch_marker = {"called": False}

    def _boom():
        monkeypatch_marker["called"] = True
        raise AssertionError("grant path must not run without a grant header")

    # Only _kms_client would be hit if verify_grant ran; ensure it doesn't.
    import delegation as _d
    orig = _d._kms_client
    _d._kms_client = _boom
    try:
        out = index.handler(_request("tools/call", actor=ALICE, tool=ALICE_TOOL), None)
    finally:
        _d._kms_client = orig
    assert not _denied(out)
    assert monkeypatch_marker["called"] is False
