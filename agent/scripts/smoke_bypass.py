"""Smoke test for the Main Agent's Lab CRUD bypass route.

Invokes the deployed Main Agent runtime with a structured {action: ...}
payload and prints the response. Run after a CDK deploy of main-lab-merge
to confirm the merge is live.
"""
import boto3
import json
import sys

REGION = "us-west-2"
ARN = "arn:aws:bedrock-agentcore:us-west-2:100000000004:runtime/mediaworks_harness_main_agent-EXAMPLE001"

action = sys.argv[1] if len(sys.argv) > 1 else "ListSkillDrafts"

client = boto3.client("bedrock-agentcore", region_name=REGION)
payload = json.dumps({"action": action}).encode()
resp = client.invoke_agent_runtime(
    agentRuntimeArn=ARN,
    runtimeSessionId=f"smoke-bypass-{action}-1234567890123456",
    payload=payload,
)
body = b"".join(chunk for chunk in resp["response"].iter_chunks() if chunk)
print(f"STATUS: {resp['statusCode']}")
print(f"BODY: {body.decode()[:3000]}")
