"""Backfill sampled hero images for videos registered before HeroFrame existed.

Scans the projects table for ``ASSET#video:`` rows with no ``rendition`` and async-invokes the HeroFrame Lambda
once per Project. Dry-run by default; ``--execute`` to invoke. Idempotent: HeroFrame skips any row that has gained a
rendition meanwhile, so re-running is harmless.

    cd agent && AWS_PROFILE=<p> uv run python scripts/hero_frame_backfill.py --env staging [--pid p...] [--execute]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

import boto3

PROJECT = "mediaworks-harness"
ACCOUNTS = {"sandbox": "100000000001", "staging": "100000000002", "prod": "100000000003"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", required=True, choices=sorted(ACCOUNTS))
    ap.add_argument("--pid", help="only this Project")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--execute", action="store_true", help="invoke (default: list only)")
    a = ap.parse_args()

    account = boto3.client("sts").get_caller_identity()["Account"]
    if account != ACCOUNTS[a.env]:
        raise SystemExit(f"refusing: credentials are for {account}, --env {a.env} is {ACCOUNTS[a.env]}")
    ddb = boto3.client("dynamodb", region_name=a.region)
    kwargs = {
        "TableName": f"{PROJECT}-projects",
        "FilterExpression": "begins_with(SK, :v) AND attribute_not_exists(rendition)"
                            + (" AND PK = :pk" if a.pid else ""),
        "ExpressionAttributeValues": {":v": {"S": "ASSET#video:"}, **({":pk": {"S": f"PROJECT#{a.pid}"}} if a.pid else {})},
        "ProjectionExpression": "PK, SK, displayName, #s",
        "ExpressionAttributeNames": {"#s": "status"},
    }
    todo: dict[str, list[str]] = defaultdict(list)
    for page in ddb.get_paginator("scan").paginate(**kwargs):
        for it in page["Items"]:
            pid = it["PK"]["S"][len("PROJECT#"):]
            todo[pid].append(it["SK"]["S"][len("ASSET#"):])
            print(f"{pid}  {it['SK']['S'][6:]}  {it.get('displayName', {}).get('S', '')}  [{it.get('status', {}).get('S', '')}]")
    total = sum(map(len, todo.values()))
    print(f"\n{total} video(s) without a hero image across {len(todo)} project(s)")
    if not a.execute:
        print("dry run — pass --execute to invoke HeroFrame")
        return
    lam = boto3.client("lambda", region_name=a.region)
    fn = f"{PROJECT}-hero-frame"
    fns = [f["FunctionName"] for p in lam.get_paginator("list_functions").paginate() for f in p["Functions"]
           if "HeroFrameFn" in f["FunctionName"] or f["FunctionName"] == fn]
    if not fns:
        raise SystemExit("no HeroFrame Lambda found — has the deploy landed?")
    for pid, ids in todo.items():
        for i in range(0, len(ids), 10):
            lam.invoke(FunctionName=fns[0], InvocationType="Event",
                       Payload=json.dumps({"pid": pid, "assetIds": ids[i:i + 10]}).encode())
    print(f"invoked {fns[0]} for {total} video(s)")


if __name__ == "__main__":
    main()
