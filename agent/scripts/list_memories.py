"""List AgentCore Memory resources in the region."""
import boto3

c = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
try:
    r = c.list_memories()
    for m in r.get("memories", []):
        print(f"  {m.get('id')} status={m.get('status')} name={m.get('name')} arn={m.get('arn')}")
except Exception as e:
    print(f"err: {e}")
