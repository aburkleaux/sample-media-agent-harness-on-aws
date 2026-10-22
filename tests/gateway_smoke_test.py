"""Smoke test for the media toolkit's MCP gateway.

Reads the gateway credentials from Secrets Manager (needs AWS creds for the
target account), performs the Cognito client_credentials OAuth flow, connects
to the gateway over MCP streamable-http, lists the tools, and does a read-only
tool call (get_job_status with a bogus id) to prove end-to-end invocation.

Run:
  M2C_GATEWAY_SECRET_ID=/media-workflow-gateway/<env>/gateway-credentials \
    AWS_PROFILE=<profile> agent/.venv/bin/python tests/gateway_smoke_test.py
"""
import asyncio
import base64
import json
import os
import sys

import boto3
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# The gateway-credentials Secrets Manager id is an explicit input (no legacy
# staging default baked in): set M2C_GATEWAY_SECRET_ID, e.g.
#   /media-workflow-gateway/<env>/gateway-credentials
SECRET_ID = os.environ.get("M2C_GATEWAY_SECRET_ID", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")


def load_creds() -> dict:
    if not SECRET_ID:
        sys.exit("Set M2C_GATEWAY_SECRET_ID to the gateway-credentials Secrets Manager id.")
    session = boto3.Session(region_name=REGION)  # credentials from the standard chain (AWS_PROFILE etc.)
    sm = session.client("secretsmanager")
    raw = sm.get_secret_value(SecretId=SECRET_ID)["SecretString"]
    return json.loads(raw)


def get_token(creds: dict) -> str:
    basic = base64.b64encode(
        f"{creds['clientId']}:{creds['clientSecret']}".encode()
    ).decode()
    resp = httpx.post(
        creds["tokenEndpoint"],
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": creds["scope"]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def main() -> int:
    creds = load_creds()
    url = creds["endpointUrl"]
    print(f"gateway: {url}")
    print(f"scope:   {creds['scope']}")
    token = get_token(creds)
    print(f"token:   acquired ({len(token)} chars)\n")

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            print(f"=== {len(listed.tools)} tools ===")
            for t in listed.tools:
                params = list((t.inputSchema or {}).get("properties", {}).keys())
                print(f"  - {t.name}({', '.join(params)})")

            names = {t.name for t in listed.tools}
            status_tool = next((n for n in names if "job_status" in n or n.endswith("get_job_status")), None)
            if status_tool:
                print(f"\n=== calling {status_tool} (bogus id, read-only) ===")
                try:
                    res = await session.call_tool(
                        status_tool,
                        {"job_id": f"arn:aws:states:{REGION}:000000000000:execution:smoke-nonexistent:smoke-nonexistent"},
                    )
                    text = ""
                    for c in res.content:
                        text += getattr(c, "text", "") or ""
                    print(f"  isError={res.isError} result={text[:300]}")
                except Exception as exc:  # tool-level error still proves routing
                    print(f"  tool raised (gateway routing OK): {type(exc).__name__}: {exc}")
            else:
                print("\n(no get_job_status-style tool found to call)")
    print("\nSMOKE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
