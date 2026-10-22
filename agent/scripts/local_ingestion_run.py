"""DEV-ONLY: run ONE asset through the real ingestion code against the real staging M2C (design "Local
development" tier 3), with the registry and Document_Records in a local moto server.

    cd agent && AWS_PROFILE=<staging> uv run --extra dev --with 'moto[server]' python scripts/local_ingestion_run.py \
        [--source s3://bucket/key.mp4] [--search "a phrase"] [--execute] [--cleanup]

What runs is the product code: AssetStore registration, the Dispatcher (claim + start), and IngestTick looped
in-process with the waits the state machine would use. Only the state machine itself is replaced by a loop.

Writes to real resources (listed, and made only with --execute): M2C workflow runs on the shared staging
gateway, one fresh ``p<uuid>`` hybrid-search collection, and its index rows. ``--cleanup`` deletes that
collection at the end. Nothing is written to the real projects table or Ingest bucket. Refuses the prod account.
Never prints tokens, secrets, or presigned URLs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3  # noqa: E402

from ingestion import collection  # noqa: E402
from ingestion.handlers.dispatcher import Dispatcher  # noqa: E402
from ingestion.handlers.tick import Deps, Tick  # noqa: E402
from ingestion.m2c import M2CClient, M2CConfig  # noqa: E402
from ingestion.pipeline_store import PipelineStore, from_ddb  # noqa: E402

PROD_ACCOUNT = "100000000003"
REGION = "us-west-2"
STAGING = M2CConfig(
    gateway_url="https://media-workflow-gateway-v6-staging-examplegw2.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
    client_id="1exampleclientid0000000001",
    token_endpoint="https://media-workflow-gateway-v6-staging-100000000002.auth.us-west-2.amazoncognito.com/oauth2/token",
    secret_arn="arn:aws:secretsmanager:us-west-2:100000000002:secret:/media-workflow-gateway-v6/staging/gateway-credentials-EXMPL2",
)
DEFAULT_SOURCE = ("s3://mediaworks-harness-skills-store-100000000002-us-west-2/artifacts/"
                  "df51db2b-65d3-4573-a791-9e72823f289d/input/1787928383597-evil_santa_trailer_enUS.mp4")
TABLE = "projects-local"
INGEST = "ingest-local"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class LocalSFN:
    def __init__(self):
        self.started: list[dict] = []

    def start_execution(self, stateMachineArn, name, input):
        self.started.append({"name": name, "input": json.loads(input)})
        return {"executionArn": f"arn:aws:states:{REGION}:000000000000:execution:local:{name}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--search", default="")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--allow-staging-writes", action="store_true",
                    help="images: write the rendition + record under projects/<run pid>/ in the real Ingest "
                         "bucket so hybrid-search can read it; --cleanup deletes that prefix")
    ap.add_argument("--port", type=int, default=5017)
    ap.add_argument("--timeout-min", type=int, default=45)
    a = ap.parse_args()

    acct = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if acct == PROD_ACCOUNT:
        raise SystemExit("refusing to run against the prod account")
    pid = "p" + uuid.uuid4().hex
    low = a.source.lower()
    kind = ("audio" if low.endswith((".mp3", ".wav", ".m4a", ".aac", ".flac")) else
            "image" if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff"))
            else "video")
    if kind == "image" and not a.allow_staging_writes:
        raise SystemExit("an image needs --allow-staging-writes (hybrid-search must read its rendition)")
    real_bucket = a.source[5:].split("/", 1)[0]
    aid = f"{kind}:{uuid.uuid4()}"
    log(f"account {acct}; source {a.source}")
    log(f"would create: M2C runs for {aid}, hybrid-search collection {pid} (+ its rows)"
        + (f", objects under s3://{real_bucket}/projects/{pid}/" if a.allow_staging_writes else "")
        + ("; delete them at the end" if a.cleanup else ""))
    if not a.execute:
        log("dry run: pass --execute to proceed")
        return 0

    from moto.server import ThreadedMotoServer
    m2c = M2CClient(STAGING)
    server = ThreadedMotoServer(port=a.port, verbose=False)
    server.start()
    try:
        local = {"endpoint_url": f"http://127.0.0.1:{a.port}", "region_name": REGION,
                 "aws_access_key_id": "local", "aws_secret_access_key": "local"}
        ddb = boto3.resource("dynamodb", **local)
        t = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("PK", "SK", "assetListPk", "assetListSk")],
            GlobalSecondaryIndexes=[{"IndexName": "assetsByRegistered", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "assetListPk", "KeyType": "HASH"},
                                                   {"AttributeName": "assetListSk", "KeyType": "RANGE"}]}])
        t.put_item(Item={"PK": f"PROJECT#{pid}", "SK": "META", "projectId": pid, "status": "active",
                         "name": "local ingestion run", "collectionState": "absent"})
        local_s3 = boto3.client("s3", **local)
        local_s3.create_bucket(Bucket=INGEST, CreateBucketConfiguration={"LocationConstraint": REGION})

        store = PipelineStore(t)
        sfn = LocalSFN()
        dispatcher = Dispatcher(store, sfn, "arn:aws:states:us-west-2:000000000000:stateMachine:local")
        real_s3 = boto3.client("s3", region_name=REGION)
        tick = Tick(Deps(store=store, m2c=m2c, s3=real_s3,
                         ingest_bucket=real_bucket if a.allow_staging_writes else INGEST,
                         records_s3=None if a.allow_staging_writes else local_s3,
                         dispatch=dispatcher.dispatch_project))
        bucket, key = a.source[5:].split("/", 1)
        store.register_asset(pid=pid, asset_id=aid, source="s3", source_uri=a.source, source_bucket=bucket,
                             source_key=key, source_size=1, content_type=f"{kind}/x",
                             file_name=key.rsplit("/", 1)[-1], added_by="local", added_by_display="local")
        dispatcher.dispatch_project(pid)
        inp = sfn.started[0]["input"]
        t0 = time.time()
        last = None
        while time.time() - t0 < a.timeout_min * 60:
            out = tick.run(inp)
            row = from_ddb(store.get_asset(pid, aid))
            view = (row["status"], {s: r.get("status") for s, r in (row.get("steps") or {}).items()},
                    ((row.get("index") or {}).get("run") or {}).get("chunk"))
            if view != last:
                log(f"{view[0]} steps={view[1]}" + (f" chunk={view[2]}" if view[2] is not None else ""))
                last = view
            if out["done"]:
                break
            time.sleep(out["wait_s"])
        row = from_ddb(store.get_asset(pid, aid))
        log(f"final: {row['status']} in {time.time() - t0:.0f}s; failure={row.get('failure')}")
        log(f"doc={json.dumps(row.get('doc'))}")
        log(f"index totals={json.dumps((row.get('index') or {}).get('totals'))}")
        if row["status"] == "indexed" and a.search:
            res = m2c.call_tool("hybrid-search___search_content",
                                {"collection_id": pid, "search_term": a.search, "k": 5})
            for hit in res.get("results") or []:
                log(f"  {hit.get('final_score', 0):.3f} {hit.get('source_index')} {hit.get('start_ms')}-"
                    f"{hit.get('end_ms')} {(hit.get('text') or '')[:100]!r}")
        return 0 if row["status"] == "indexed" else 1
    finally:
        if a.cleanup:
            try:
                res = m2c.call_tool(collection.DELETE_TOOL, {"collection_id": pid})
                log(f"deleted collection {pid}: {json.dumps(res)[:200]}")
            except Exception as exc:  # noqa: BLE001
                log(f"cleanup failed for {pid}: {exc}")
            if a.allow_staging_writes:
                s3r = boto3.client("s3", region_name=REGION)
                listed = s3r.list_objects_v2(Bucket=real_bucket, Prefix=f"projects/{pid}/").get("Contents") or []
                for o in listed:
                    s3r.delete_object(Bucket=real_bucket, Key=o["Key"])
                log(f"deleted {len(listed)} objects under projects/{pid}/")
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
