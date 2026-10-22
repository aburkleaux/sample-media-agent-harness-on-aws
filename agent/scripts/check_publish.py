"""Verify publish landed for the CM General skill."""
import boto3
import json

ARN = "arn:aws:bedrock-agentcore:us-west-2:100000000004:runtime/mediaworks_harness_main_agent-EXAMPLE001"
CM_GENERAL_ID = "0a9ff55c-9eaa-4061-99e0-4aa80400448d"

client = boto3.client("bedrock-agentcore", region_name="us-west-2")
payload = json.dumps({"action": "ListAssetVersions", "assetId": CM_GENERAL_ID}).encode()
resp = client.invoke_agent_runtime(
    agentRuntimeArn=ARN,
    runtimeSessionId=f"check-publish-cm-general-12345678901234567890",
    payload=payload,
)
body = b"".join(chunk for chunk in resp["response"].iter_chunks() if chunk)
print(f"STATUS: {resp['statusCode']}")
print(f"BODY:   {body.decode()[:1500]}")

# Also list owned to confirm working copy was deleted.
payload2 = json.dumps({"action": "ListSkillDrafts"}).encode()
resp2 = client.invoke_agent_runtime(
    agentRuntimeArn=ARN,
    runtimeSessionId=f"check-publish-list-skills-12345678901234567890",
    payload=payload2,
)
body2 = b"".join(chunk for chunk in resp2["response"].iter_chunks() if chunk)
text = body2.decode()
# Just count CM General presence.
if CM_GENERAL_ID in text:
    print(f"\n!! Working copy STILL present (id {CM_GENERAL_ID} found in ListSkillDrafts)")
else:
    print(f"\n✓ Working copy gone — {CM_GENERAL_ID} not in ListSkillDrafts")
