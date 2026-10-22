"""Local Search_Gateway emulator for running the Project search path in the local harness.

Stands in for the deployed CUSTOM_JWT + Cedar Search_Gateway (media-harness-projects-access
Component 6) so the REAL runtime code (agent/src/project_search) can be driven end to end with no
deploy. It reproduces the behaviours the live spike measured (validation/spike_v10_gateway.py):

  * Bearer JWT checked against a local JWKS (RS256, iss, aud, exp). Rejections mirror AgentCore:
    unknown kid -> HTTP 401 / JSON-RPC -32001 "Invalid Bearer token"; any other failure -> HTTP 403 /
    -32002 "insufficient_scope ...".
  * tools/call evaluated by real Cedar (cedarpy) with the design's exact Search_Policy statement;
    a deny returns JSON-RPC -32002 "Tool Execution Denied: ... policy enforcement ...".
  * a permitted call is forwarded to M2C's real hybrid-search `search_content` with the M2C machine
    token (read-only), exactly as the gateway's outbound OAuth target would.

DEV ONLY: `--alias pid=m2c_collection` maps a demo Project id onto an existing M2C collection AFTER
the Cedar decision, because no Project collection is indexed yet. Cedar still sees collection_id ==
the Project id, so the privacy check exercised is the real one. `--alias-any m2c_collection` does the
same for EVERY collection_id without an explicit `--alias` (so a freshly created Personal project maps
onto an indexed demo collection); it too applies only after Cedar PERMITS the call on the real id.

  cd agent
  uv run --with cedarpy python scripts/local_search_gateway.py mint --project p<32hex>      # prints a token
  AWS_PROFILE=<profile> uv run --with cedarpy python scripts/local_search_gateway.py serve \
      --alias p<32hex>=modfix4-df324611-moderation-shot
  # or map any permitted Project onto one demo collection:
  AWS_PROFILE=<profile> uv run --with cedarpy python scripts/local_search_gateway.py serve \
      --alias-any modfix4-df324611-moderation-shot
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

DIR = os.environ.get("LOCAL_SEARCH_GW_DIR", "/tmp/local-search-gw")
KEY = f"{DIR}/signing-key.pem"
ISS = "https://local-search-gateway.invalid"
AUD = "mediaworks-harness-search-local"
KID = "local-key-1"
TOOL = "mediaworks-harness-m2c-search___hybrid-search___search_content"
GW_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:gateway/local-search-gateway"
INGEST = os.environ.get("SEARCH_IMAGE_INGEST_BUCKET", "mediaworks-harness-skills-store-100000000002-us-west-2")
PROXY = os.environ.get("SEARCH_IMAGE_PROXY_BUCKET", "m2cv6-0e0000000002-100000000002-us-west-2-proxy")
M2C_URL = "https://media-workflow-gateway-v6-staging-examplegw2.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
M2C_SECRET = "arn:aws:secretsmanager:us-west-2:100000000002:secret:/media-workflow-gateway-v6/staging/gateway-credentials-EXMPL2"
M2C_TOKEN_URL = "https://media-workflow-gateway-v6-staging-100000000002.auth.us-west-2.amazoncognito.com/oauth2/token"
M2C_CLIENT_ID = "1exampleclientid0000000001"


def policy() -> str:
    # Same statement as design Component 6 (and the CI builder of task 2.1/2.3).
    return f'''permit(
  principal,
  action == AgentCore::Action::"{TOOL}",
  resource == AgentCore::Gateway::"{GW_ARN}"
) when {{
  principal.hasTag("project_id") &&
  context has input &&
  context.input has collection_id &&
  context.input.collection_id == principal.getTag("project_id") &&
  (if context.input has search_image_s3uri
     then (context.input.search_image_s3uri like "s3://{INGEST}/projects/*/rendition.jpg" ||
           context.input.search_image_s3uri like "s3://{PROXY}/*")
     else true)
}};'''


def _key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    os.makedirs(DIR, exist_ok=True)
    if not os.path.exists(KEY):
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with open(KEY, "wb") as f:
            f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
        os.chmod(KEY, 0o600)
    with open(KEY, "rb") as f:
        return serialization.load_pem_private_key(f.read(), None)


def mint(project_id: str, sub: str, exp_in: int) -> str:
    import jwt
    now = int(time.time())
    return jwt.encode({"iss": ISS, "aud": AUD, "sub": sub, "project_id": project_id, "iat": now,
                       "exp": now + exp_in, "jti": uuid.uuid4().hex},
                      _key(), algorithm="RS256", headers={"kid": KID, "typ": "JWT"})


def verify(token: str):
    """Return (claims, None) or (None, (http_status, rpc_code, message))."""
    import jwt
    try:
        hdr = jwt.get_unverified_header(token)
    except Exception:
        return None, (401, -32001, "Invalid Bearer token")
    if hdr.get("kid") != KID:
        return None, (401, -32001, "Invalid Bearer token")
    try:
        claims = jwt.decode(token, _key().public_key(), algorithms=["RS256"], audience=AUD, issuer=ISS)
    except Exception:
        return None, (403, -32002, "insufficient_scope - The request requires higher privileges than provided by the access token.")
    return claims, None


def cedar_decide(claims: dict, tool: str, args: dict) -> bool:
    import cedarpy
    tags = {k: v for k, v in claims.items() if isinstance(v, str)}
    ents = [{"uid": {"type": "AgentCore::OAuthUser", "id": str(claims.get("sub"))}, "attrs": {}, "parents": [], "tags": tags},
            {"uid": {"type": "AgentCore::Gateway", "id": GW_ARN}, "attrs": {}, "parents": []}]
    req = {"principal": f'AgentCore::OAuthUser::"{claims.get("sub")}"', "action": f'AgentCore::Action::"{tool}"',
           "resource": f'AgentCore::Gateway::"{GW_ARN}"', "context": {"input": args}}
    return cedarpy.is_authorized(req, policy(), ents).decision.name == "Allow"


class M2C:
    def __init__(self):
        self._tok, self._exp = None, 0

    def token(self) -> str:
        if self._tok and time.time() < self._exp - 60:
            return self._tok
        import boto3
        sec = json.loads(boto3.client("secretsmanager", region_name="us-west-2")
                         .get_secret_value(SecretId=M2C_SECRET)["SecretString"])
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": sec.get("clientId") or M2C_CLIENT_ID,
                                       "client_secret": sec.get("clientSecret") or sec.get("client_secret"),
                                       "scope": "media-workflows/invoke"}).encode()
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            M2C_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=10).read())
        self._tok, self._exp = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self._tok

    async def search(self, args: dict) -> str:
        import httpx2
        from mcp import ClientSession
        from mcp.client.client import negotiate_auto
        from mcp.client.streamable_http import streamable_http_client
        tok = await asyncio.to_thread(self.token)
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=httpx2.Timeout(90)) as hc:
            async with streamable_http_client(M2C_URL, http_client=hc) as (r, w, *_):
                async with ClientSession(r, w) as s:
                    await negotiate_auto(s)
                    res = await s.call_tool("hybrid-search___search_content", args)
                    return "\n".join(getattr(c, "text", "") for c in res.content or [])


def serve(port: int, aliases: dict[str, str], alias_any: str | None = None) -> None:
    import uvicorn
    from mcp.server.mcpserver import MCPServer

    m2c = M2C()
    srv = MCPServer("local-search-gateway")

    @srv.tool(name=TOOL, description="Hybrid search over the active project's indexed media (local emulator).")
    async def search_content(collection_id: str, search_term: str | None = None,
                             search_image_s3uri: str | None = None, k: int | None = None) -> str:
        # Runs only after the ASGI wrapper's Cedar PERMIT on the real collection_id (DEV-only remap).
        args = {"collection_id": aliases.get(collection_id, alias_any or collection_id)}
        for name, val in (("search_term", search_term), ("search_image_s3uri", search_image_s3uri), ("k", k)):
            if val is not None:
                args[name] = val
        print(f"[local-search-gw] forward to M2C collection={args['collection_id']}", flush=True)
        return await m2c.search(args)

    inner = srv.streamable_http_app()

    async def reply(send, status, rpc_id, code, message):
        body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        hdrs = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        try:
            rpc = json.loads(body) if body else {}
        except ValueError:
            rpc = {}
        rid = rpc.get("id") if isinstance(rpc, dict) else None
        auth = hdrs.get("authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        claims, err = verify(tok) if tok else (None, (401, -32001, "Invalid Bearer token"))
        if err:
            print(f"[local-search-gw] reject {err[0]} {err[2][:40]}", flush=True)
            return await reply(send, err[0], rid if rid is not None else 0, err[1], err[2])
        if isinstance(rpc, dict) and rpc.get("method") == "tools/call":
            p = rpc.get("params") or {}
            allowed = cedar_decide(claims, str(p.get("name")), p.get("arguments") or {})
            print(f"[local-search-gw] cedar {'PERMIT' if allowed else 'DENY'} project_id={claims.get('project_id')} "
                  f"collection_id={(p.get('arguments') or {}).get('collection_id')}", flush=True)
            if not allowed:
                return await reply(send, 200, rid, -32002, "Tool Execution Denied: Tool call not allowed due to "
                                   "policy enforcement [No policy applies to the request (denied by default).]")
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await inner(scope, replay, send)

    print(f"[local-search-gw] http://127.0.0.1:{port}/mcp  tool={TOOL}  aliases={aliases}  alias_any={alias_any}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    m = sp.add_parser("mint")
    m.add_argument("--project", required=True)
    m.add_argument("--sub", default="local-dev-user")
    m.add_argument("--exp-in", type=int, default=3600)
    s = sp.add_parser("serve")
    s.add_argument("--port", type=int, default=8099)
    s.add_argument("--alias", action="append", default=[], help="pid=m2c_collection (dev only)")
    s.add_argument("--alias-any", metavar="M2C_COLLECTION", default=None,
                   help="after a Cedar PERMIT, forward any collection_id without an --alias here (dev only)")
    a = ap.parse_args()
    if a.cmd == "mint":
        print(mint(a.project, a.sub, a.exp_in))
    else:
        serve(a.port, dict(x.split("=", 1) for x in a.alias), a.alias_any)


if __name__ == "__main__":
    main()
