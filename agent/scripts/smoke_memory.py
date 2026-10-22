"""Smoke test: verify durable memory across two turns of one session.

Sends "remember the number 42" then asks "what number did I tell you" in
the same session_id. With AgentCore Memory wired correctly, the agent
should recall 42.
"""
import boto3
import json
import sys
import uuid

REGION = "us-west-2"
ARN = "arn:aws:bedrock-agentcore:us-west-2:100000000004:runtime/mediaworks_harness_main_agent-EXAMPLE001"

session = sys.argv[1] if len(sys.argv) > 1 else f"smoke-mem-{uuid.uuid4().hex}"

client = boto3.client("bedrock-agentcore", region_name=REGION)


def _invoke(prompt):
    payload = json.dumps({"prompt": prompt, "session_id": session}).encode()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=ARN,
        runtimeSessionId=session,
        payload=payload,
    )
    body = b"".join(c for c in resp["response"].iter_chunks() if c).decode()
    print(f"\n=== prompt: {prompt!r} ===")
    print(body[-600:])


_invoke("remember the number 42 for me")
_invoke("what number did I tell you to remember")
