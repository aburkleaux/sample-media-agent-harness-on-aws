#!/usr/bin/env python
"""Load / soak harness for the agent runtime (resilience P2).

Drives N concurrent invoke_agent_runtime calls with a simple prompt and reports
latency percentiles + ok/throttle/error counts. Use it to find where Bedrock
throttling kicks in and to confirm the LLM-client retries (P0 task 5) absorb
transient throttles. Point it at a non-production environment.

Usage:
  AWS_PROFILE=<profile> python tests/load/load_test.py \
    --runtime-arn arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/<project>_main_agent-<id> \
    --concurrency 5 --rounds 2

Cost note: each invocation is a real Bedrock turn. Start small; crank
--concurrency / --rounds to probe limits deliberately.
"""
import argparse
import concurrent.futures as cf
import json
import os
import statistics
import time
import uuid

import boto3
from botocore.config import Config as BotoConfig


def _one(client, arn, prompt):
    session_id = "loadtest-" + uuid.uuid4().hex + uuid.uuid4().hex  # >= 33 chars
    t0 = time.time()
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn, qualifier="DEFAULT",
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": prompt}).encode(),
            contentType="application/json",
        )
        body = resp.get("response")
        if hasattr(body, "read"):
            body.read()
        elif hasattr(body, "iter_lines"):
            for _ in body.iter_lines():
                pass
        return ("ok", time.time() - t0)
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        cls = "throttle" if "Throttl" in name or "TooManyRequests" in name else "error"
        return (cls, time.time() - t0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-arn", required=True)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--prompt", default="In one word, say hello.")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--region", default="us-west-2")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client(
        "bedrock-agentcore",
        config=BotoConfig(read_timeout=300, connect_timeout=10, retries={"max_attempts": 1}),
    )
    total = args.concurrency * args.rounds
    print(f"load: {total} invocations ({args.concurrency} concurrent x {args.rounds} rounds) → {args.runtime_arn[-40:]}")

    results = []
    for r in range(args.rounds):
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(_one, client, args.runtime_arn, args.prompt) for _ in range(args.concurrency)]
            for f in cf.as_completed(futs):
                results.append(f.result())
        print(f"  round {r + 1}/{args.rounds} done")

    lat = sorted(l for _, l in results)
    counts = {"ok": 0, "throttle": 0, "error": 0}
    for cls, _ in results:
        counts[cls] += 1

    def pct(p):
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0.0

    print("\n=== load results ===")
    print(f"  total={total} ok={counts['ok']} throttled={counts['throttle']} errored={counts['error']}")
    if lat:
        print(f"  latency s: p50={pct(0.5):.1f} p95={pct(0.95):.1f} p99={pct(0.99):.1f} "
              f"min={lat[0]:.1f} max={lat[-1]:.1f} mean={statistics.mean(lat):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
