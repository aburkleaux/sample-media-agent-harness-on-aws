"""Live M2C tool-shape drift check (complements agent/tests/test_m2c_contract.py).

The offline contract test locks the harness extraction against a golden payload.
This script calls the LIVE gateway's get_job_status on a completed job and
asserts the contract keys the harness depends on are still present — catching
M2C-SIDE drift that the offline test can't. Exits non-zero on drift so it can
be wired into a scheduled CI job / canary.

Usage:
  AWS_PROFILE=<profile> python tests/contract_check_m2c.py \
    --secret /<gateway-name>/<environment>/gateway-credentials \
    --execution-arn arn:aws:states:<region>:<account-id>:execution:<state-machine>:<execution-id>
"""
import argparse
import asyncio
import base64
import json
import sys

import boto3
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Contract the harness depends on (mirror of test_m2c_contract.py + provider.py).
_START_REQUIRED = ["job_id"]
_STATUS_REQUIRED_PREFIX = "output_s3_prefix"


async def _check(secret_id: str, execution_arn: str, region: str, profile: str) -> int:
    session = boto3.Session(profile_name=profile, region_name=region)
    bundle = json.loads(session.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"])
    basic = base64.b64encode(f"{bundle['clientId']}:{bundle['clientSecret']}".encode()).decode()
    tok = httpx.post(bundle["tokenEndpoint"],
                     headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                     data={"grant_type": "client_credentials", "scope": bundle["scope"]}, timeout=30).json()["access_token"]
    drift: list[str] = []
    async with streamablehttp_client(bundle["endpointUrl"], headers={"Authorization": f"Bearer {tok}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("speech-diarization___get_job_status", {"job_id": execution_arn})
            payload = {}
            for c in res.content:
                txt = getattr(c, "text", None)
                if txt:
                    try:
                        payload = json.loads(txt)
                    except (ValueError, TypeError):
                        pass
    status = payload.get("status")
    print(f"get_job_status → status={status}")
    if status == "SUCCEEDED":
        if not str(payload.get(_STATUS_REQUIRED_PREFIX, "")).startswith("s3://"):
            drift.append(f"missing/invalid {_STATUS_REQUIRED_PREFIX} (provider.extract_output_uris depends on it)")
        if not any(k.endswith("_key") for k in payload):
            drift.append("no *_key fields (provider.extract_output_uris depends on them)")
    if drift:
        print("CONTRACT DRIFT DETECTED:")
        for d in drift:
            print(f"  - {d}")
        print("Update agent/tests/test_m2c_contract.py + hooks/provider.py + gateway-integration-notes.md §3")
        return 1
    print("contract OK — no drift")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", required=True)
    ap.add_argument("--execution-arn", required=True)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args()
    return asyncio.run(_check(args.secret, args.execution_arn, args.region, args.profile))


if __name__ == "__main__":
    raise SystemExit(main())
