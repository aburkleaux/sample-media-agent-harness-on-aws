#!/usr/bin/env python3
"""Exercise the harness gateway's MCP chain locally, without a deploy.

Why this exists
---------------
Getting one Lambda's output shape right cost seven deploys and about ninety
minutes of waiting. Every one of those cycles was avoidable: the interceptor is a
pure JSON-in/JSON-out function, and the gateway is reachable over HTTPS with a
user token. Nothing about verifying either of them requires shipping code.

Local unit tests did not help, and it is worth being precise about why. They
asserted the contract I *believed* in, three times in a row -- first that omitting
`transformedGatewayRequest` meant "leave it alone", then that the MCP output
accepted `headers` like the HTTP output does. Tests written against an assumption
confirm the assumption. Only the real service, or the payload reference, can
correct it.

Two modes:

  gateway      Drive the real MCP handshake against the gateway with a real user
               token: initialize -> notifications/initialized -> tools/list.
               Covers the authorizer, the interceptor (both phases), recorded
               ownership filtering, and the user's live targets. Everything
               except the agent runtime.

  interceptor  Invoke the deployed interceptor Lambda directly with a payload,
               either a built-in fixture or a real event captured from
               CloudWatch. Answers "what does this function actually return"
               in about a second.

Usage
-----
  # Whole gateway chain. Token from the browser: devtools -> Application ->
  # Local Storage -> the Cognito access token, or copy the Authorization header
  # off any /api request.
  python3 scripts/probe_gateway.py gateway --token "$TOKEN"

  # Resolve the gateway URL automatically instead of passing it
  python3 scripts/probe_gateway.py gateway --token "$TOKEN" --env staging

  # Interceptor only, with a fixture
  python3 scripts/probe_gateway.py interceptor --method tools/list --phase response

  # Interceptor with a real captured event
  python3 scripts/probe_gateway.py interceptor --event-file /tmp/event.json

Exit status is 0 only when the checked stage actually succeeded, so this is
usable as a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

REGION = "us-west-2"
GATEWAY_ID = "mediaworks-harness-gateway-azxdlhjrqx"
INTERCEPTOR_HINT = "GatewayInterceptorFn"

PROTOCOL_VERSION = "2025-03-26"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _parse_body(raw: bytes, content_type: str) -> dict | None:
    """Return the JSON-RPC object from a plain or SSE-framed body.

    The gateway may answer `text/event-stream` even for a single response, in
    which case the JSON sits in a `data:` line rather than being the whole body.
    fal.ai does exactly this, so assuming plain JSON silently fails.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if "event-stream" in (content_type or "") or text.startswith("event:"):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def post_jsonrpc(url: str, token: str, body: dict, session_id: str | None):
    """POST one JSON-RPC message. Returns (status, parsed, headers)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("content-type", "application/json")
    # Both, because streamable-http servers may answer either way.
    req.add_header("accept", "application/json, text/event-stream")
    req.add_header("authorization", f"Bearer {token}")
    if session_id:
        req.add_header("mcp-session-id", session_id)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, _parse_body(raw, headers.get("content-type", "")), headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        return e.code, _parse_body(raw, headers.get("content-type", "")) or {
            "_raw": raw.decode("utf-8", errors="replace")[:600]
        }, headers


# ---------------------------------------------------------------------------
# Mode: gateway
# ---------------------------------------------------------------------------


def resolve_gateway_url() -> str | None:
    """Look the URL up so it cannot drift from the deployed gateway."""
    try:
        import boto3
    except ImportError:
        return None
    try:
        c = boto3.client("bedrock-agentcore-control", region_name=REGION)
        return c.get_gateway(gatewayIdentifier=GATEWAY_ID).get("gatewayUrl")
    except Exception as e:
        print(f"  ! could not resolve gateway url: {e}", file=sys.stderr)
        return None


def run_gateway(url: str, token: str) -> int:
    print(f"gateway: {url}\n")
    failures = []

    # 1. initialize
    status, body, headers = post_jsonrpc(url, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "probe_gateway", "version": "1.0"},
        },
    }, None)
    session_id = headers.get("mcp-session-id")
    print(f"initialize            HTTP {status}  session={session_id or '<none>'}")
    if status != 200 or not body:
        print(f"  FAILED: {json.dumps(body)[:500] if body else '<empty body>'}")
        if status in (401, 403):
            print("  -> the token was rejected. Check it is an ACCESS token from")
            print("     the user pool the gateway's authorizer is configured with,")
            print("     and that it has not expired.")
        return 1
    if isinstance(body, dict) and "error" in body:
        print(f"  FAILED: {json.dumps(body['error'])[:400]}")
        return 1
    server = (body.get("result") or {}).get("serverInfo") or {}
    print(f"  server={server.get('name')} v{server.get('version')}")

    # 2. notifications/initialized — no response expected; a 5xx here means the
    #    interceptor broke a notification, which is easy to overlook.
    status, _, _ = post_jsonrpc(url, token, {
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }, session_id)
    ok = status in (200, 202, 204)
    print(f"notifications/init    HTTP {status}  {'ok' if ok else 'UNEXPECTED'}")
    if not ok:
        failures.append("notifications/initialized")

    # 3. tools/list — the interceptor's RESPONSE filter runs here.
    status, body, _ = post_jsonrpc(url, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
    }, session_id)
    print(f"tools/list            HTTP {status}")
    if status != 200 or not isinstance(body, dict):
        print(f"  FAILED: {json.dumps(body)[:500] if body else '<empty body>'}")
        return 1
    if "error" in body:
        print(f"  FAILED: {json.dumps(body['error'])[:400]}")
        return 1

    tools = ((body.get("result") or {}).get("tools")) or []
    print(f"  {len(tools)} tool(s) visible to this token")
    by_target: dict[str, list[str]] = {}
    for t in tools:
        name = t.get("name", "?")
        target, _, short = name.partition("___")
        by_target.setdefault(target if short else "<no target prefix>", []).append(short or name)
    for target, names in sorted(by_target.items()):
        print(f"    {target}: {len(names)}")
        for n in sorted(names)[:12]:
            print(f"      - {n}")

    if not tools:
        print("\n  0 tools. The handshake works, so this is the ownership filter:")
        print("  either no target is recorded for this actor, or the recorded")
        print("  owner does not match the token's `sub`. Compare the interceptor's")
        print("  logged actor prefix against the TARGET#OWNER rows.")
        failures.append("tools/list returned nothing")

    print("\nRESULT: " + ("PASS" if not failures else "FAIL — " + "; ".join(failures)))
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Mode: interceptor
# ---------------------------------------------------------------------------


def fixture(method: str, phase: str) -> dict:
    """A payload shaped like the real one.

    Field set taken from an event actually observed in CloudWatch, not from the
    documentation: the live payload also carries `rawGatewayRequest` and a
    `context` key inside gatewayRequest that the reference does not mention.
    """
    request = {
        "path": "/mcp",
        "httpMethod": "POST",
        "headers": {"Authorization": "Bearer <redacted>", "Accept": "application/json"},
        "body": {"jsonrpc": "2.0", "id": 1, "method": method},
        "context": {},
    }
    mcp: dict = {"rawGatewayRequest": {"body": json.dumps(request["body"])},
                 "gatewayRequest": request}
    if phase == "response":
        mcp["gatewayResponse"] = {
            "statusCode": 200,
            "headers": {"Mcp-Session-Id": "probe-session"},
            "body": {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
        }
    return {"interceptorInputVersion": "1.0", "mcp": mcp}


def run_interceptor(event: dict, function_name: str | None) -> int:
    try:
        import boto3
    except ImportError:
        print("boto3 required for interceptor mode", file=sys.stderr)
        return 2
    lam = boto3.client("lambda", region_name=REGION)
    if not function_name:
        names = [
            f["FunctionName"]
            for page in lam.get_paginator("list_functions").paginate()
            for f in page["Functions"]
            if INTERCEPTOR_HINT in f["FunctionName"]
        ]
        if len(names) != 1:
            print(f"could not identify the interceptor uniquely: {names}", file=sys.stderr)
            return 2
        function_name = names[0]
    print(f"function: {function_name}")

    resp = lam.invoke(FunctionName=function_name, Payload=json.dumps(event).encode())
    out = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError"):
        print(f"FunctionError: {resp['FunctionError']}\n{json.dumps(out, indent=2)[:1200]}")
        return 1
    print(json.dumps(out, indent=2)[:2000])

    # Check the output against the MCP contract that took three tries to get right.
    problems = []
    if out.get("interceptorOutputVersion") != "1.0":
        problems.append("interceptorOutputVersion must be '1.0'")
    mcp = out.get("mcp")
    if not isinstance(mcp, dict) or not mcp:
        problems.append("empty `mcp` is an empty transformation, not a passthrough")
    else:
        for key in ("transformedGatewayRequest", "transformedGatewayResponse"):
            node = mcp.get(key)
            if isinstance(node, dict) and not set(node).issubset({"body", "statusCode"}):
                problems.append(
                    f"{key} carries {sorted(set(node) - {'body', 'statusCode'})}; "
                    "the MCP output takes body (and statusCode on a response)"
                )
        if not any(k.startswith("transformed") for k in mcp):
            problems.append("no transformed field present")

    print("\nCONTRACT: " + ("ok" if not problems else "PROBLEMS"))
    for p in problems:
        print(f"  - {p}")
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# Mode: isolation
# ---------------------------------------------------------------------------


def _claim_token(sub: str) -> str:
    """A JWT-SHAPED string carrying `sub`. Signature is deliberately junk.

    Legitimate for testing this component: the interceptor decodes the claim
    WITHOUT verifying, by design, because the gateway's authorizer has already
    validated the token before the Lambda is ever invoked. So the Lambda's real
    input is an already-trusted token, and that is what this reproduces. It is
    not a way to bypass anything -- the gateway would reject this at the door.
    """
    import base64
    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'RS256', 'typ': 'JWT'})}.{seg({'sub': sub})}.not-a-signature"


def _tools_list_response_event(actor_sub: str, tool_names: list[str]) -> dict:
    """A RESPONSE-phase event whose tools/list result holds `tool_names`."""
    body = {"jsonrpc": "2.0", "id": 1,
            "result": {"tools": [{"name": n, "description": f"{n} description"}
                                 for n in tool_names]}}
    request = {
        "path": "/mcp", "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {_claim_token(actor_sub)}"},
        "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    }
    return {"interceptorInputVersion": "1.0",
            "mcp": {"rawGatewayRequest": {"body": json.dumps(request["body"])},
                    "gatewayRequest": request,
                    "gatewayResponse": {"statusCode": 200, "headers": {},
                                        "body": body}}}


def _tools_call_request_event(actor_sub: str, tool_name: str) -> dict:
    """A REQUEST-phase event calling `tool_name`."""
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}}}
    request = {
        "path": "/mcp", "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {_claim_token(actor_sub)}"},
        "body": body,
    }
    return {"interceptorInputVersion": "1.0",
            "mcp": {"rawGatewayRequest": {"body": json.dumps(body)},
                    "gatewayRequest": request}}


def run_isolation(function_name: str | None, table: str) -> int:
    """Prove a user's tools are invisible and uncallable for anyone else.

    This is the guarantee the whole per-actor design exists to provide, and it
    was the last thing still unverified. It runs against the DEPLOYED interceptor
    and the REAL ownership table, so it tests the shipped enforcement rather than
    a local reimplementation of it.

    Needs no second Cognito user: identity reaches the Lambda as a claim in an
    already-validated token, so a second actor is a different `sub`.
    """
    try:
        import boto3
    except ImportError:
        print("boto3 required", file=sys.stderr)
        return 2

    ddb = boto3.client("dynamodb", region_name=REGION)
    rows = ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": "TARGET#OWNER"}},
    ).get("Items", [])
    owned = [(r["SK"]["S"], r.get("ownerActorId", {}).get("S", ""),
              r.get("serverName", {}).get("S", "?")) for r in rows]
    if not owned:
        print("no TARGET#OWNER rows: register an MCP server first", file=sys.stderr)
        return 2

    target, owner, server = owned[0]
    intruder = "00000000-dead-beef-0000-000000000000"
    if intruder == owner:
        intruder = "11111111-dead-beef-1111-111111111111"
    owned_tool = f"{target}___run_model"
    platform_tool = "mediaworks-harness-aws-mcp___aws___search_documentation"

    print(f"target   {target}  ({server})")
    print(f"owner    {owner}")
    print(f"intruder {intruder}\n")

    lam = boto3.client("lambda", region_name=REGION)
    if not function_name:
        names = [f["FunctionName"]
                 for page in lam.get_paginator("list_functions").paginate()
                 for f in page["Functions"] if INTERCEPTOR_HINT in f["FunctionName"]]
        if len(names) != 1:
            print(f"could not identify the interceptor: {names}", file=sys.stderr)
            return 2
        function_name = names[0]

    def call(event: dict) -> dict:
        r = lam.invoke(FunctionName=function_name, Payload=json.dumps(event).encode())
        return json.loads(r["Payload"].read() or b"{}")

    def listed(out: dict) -> list[str]:
        node = (out.get("mcp") or {}).get("transformedGatewayResponse") or {}
        tools = ((node.get("body") or {}).get("result") or {}).get("tools")
        return [t.get("name") for t in tools] if isinstance(tools, list) else []

    def denied(out: dict) -> bool:
        node = (out.get("mcp") or {}).get("transformedGatewayResponse") or {}
        return "error" in ((node.get("body") or {}) if isinstance(node.get("body"), dict) else {})

    failures: list[str] = []
    both = [owned_tool, platform_tool]

    # 1. tools/list, owner
    got = listed(call(_tools_list_response_event(owner, both)))
    ok = owned_tool in got
    print(f"{'PASS' if ok else 'FAIL'}  owner sees own tool        {got}")
    if not ok:
        failures.append("owner cannot see their own tool")

    # 2. tools/list, another actor -- the leak this design prevents
    got = listed(call(_tools_list_response_event(intruder, both)))
    ok = owned_tool not in got
    print(f"{'PASS' if ok else 'FAIL'}  other actor cannot see it  {got}")
    if not ok:
        failures.append("LEAK: another actor sees this user's tool in tools/list")

    # 3. platform tools stay visible to everyone
    ok = platform_tool in got
    print(f"{'PASS' if ok else 'FAIL'}  platform tool still shared")
    if not ok:
        failures.append("platform tool hidden -- fails closed too aggressively")

    # 4. tools/call, owner
    out = call(_tools_call_request_event(owner, owned_tool))
    ok = not denied(out)
    print(f"{'PASS' if ok else 'FAIL'}  owner may call own tool")
    if not ok:
        failures.append("owner denied on their own tool")

    # 5. tools/call, another actor
    out = call(_tools_call_request_event(intruder, owned_tool))
    ok = denied(out)
    print(f"{'PASS' if ok else 'FAIL'}  other actor denied on call")
    if not ok:
        failures.append("LEAK: another actor may CALL this user's tool")

    # 6. no identity at all
    ev = _tools_call_request_event(owner, owned_tool)
    ev["mcp"]["gatewayRequest"]["headers"] = {}
    ok = denied(call(ev))
    print(f"{'PASS' if ok else 'FAIL'}  unidentified caller denied (fails closed)")
    if not ok:
        failures.append("unidentified caller permitted -- does not fail closed")

    print("\nRESULT: " + ("PASS" if not failures else "FAIL"))
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    g = sub.add_parser("gateway", help="drive the real MCP handshake")
    g.add_argument("--token", required=True, help="Cognito ACCESS token (Bearer)")
    g.add_argument("--url", help="gateway MCP url; resolved from AWS when omitted")

    i = sub.add_parser("interceptor", help="invoke the interceptor Lambda directly")
    i.add_argument("--method", default="tools/list")
    i.add_argument("--phase", choices=["request", "response"], default="request")
    i.add_argument("--event-file", help="a real captured event, overrides the fixture")
    i.add_argument("--function-name", help="explicit Lambda name")

    iso = sub.add_parser("isolation", help="prove per-actor scoping on the real interceptor")
    iso.add_argument("--function-name", help="explicit Lambda name")
    iso.add_argument("--table", default="mediaworks-harness-gateway-registry")

    args = ap.parse_args()

    if args.mode == "gateway":
        url = args.url or resolve_gateway_url()
        if not url:
            print("no gateway url (pass --url)", file=sys.stderr)
            return 2
        if not url.rstrip("/").endswith("/mcp"):
            url = url.rstrip("/") + "/mcp"
        return run_gateway(url, args.token)

    if args.mode == "isolation":
        return run_isolation(args.function_name, args.table)

    if args.event_file:
        with open(args.event_file, encoding="utf-8") as fh:
            event = json.load(fh)
    else:
        event = fixture(args.method, args.phase)
    return run_interceptor(event, args.function_name)


if __name__ == "__main__":
    sys.exit(main())
