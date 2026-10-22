"""Smoke test for the Main Agent's chat path with Lab tools wired in.

Sends a natural-language prompt that should trigger the list_skill_drafts
tool. Useful for confirming the merge presents drafts to the model.
"""
import boto3
import json
import sys

REGION = "us-west-2"
ARN = "arn:aws:bedrock-agentcore:us-west-2:100000000004:runtime/mediaworks_harness_main_agent-EXAMPLE001"

prompt = sys.argv[1] if len(sys.argv) > 1 else "list my draft skills"

client = boto3.client("bedrock-agentcore", region_name=REGION)
payload = json.dumps({"prompt": prompt}).encode()
resp = client.invoke_agent_runtime(
    agentRuntimeArn=ARN,
    runtimeSessionId="smoke-chat-test-1234567890123456789012",
    payload=payload,
)

# Streaming response — print each event line
print(f"STATUS: {resp['statusCode']}")
print("--- stream ---")
seen = 0
for chunk in resp["response"].iter_chunks():
    if chunk:
        text = chunk.decode(errors="replace")
        sys.stdout.write(text)
        sys.stdout.flush()
        seen += len(text)
        if seen > 12000:
            print("\n--- truncated ---")
            break
print()
