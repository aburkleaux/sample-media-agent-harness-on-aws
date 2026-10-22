"""HeroFrame Lambda: give a newly registered video a sampled hero image (:mod:`ingestion.hero_frame`).

Invoked asynchronously by the Dispatcher with ``{"pid", "assetId"}`` for every ``INSERT`` of an ``ASSET#video:``
row — the Dispatcher's stream mapping already reads the projects stream, so this adds no third stream reader
(DynamoDB Streams throttles above ~2 per shard). Also callable with ``{"pid", "assetIds": [...]}`` for a backfill.

Idempotent and never clobbering: a row that already has a ``rendition`` (an index poster, or an earlier sample) is
skipped, and the row update is conditional on the attribute still being absent. A failure is logged and returned,
never raised — a missing thumbnail must not retry-storm or block anything.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

from botocore.exceptions import ClientError

from projects import schema

from .. import hero_frame, poster
from ..outputs import split_uri

log = logging.getLogger()
log.setLevel(logging.INFO)


class HeroFrameService:
    def __init__(self, table: Any, s3: Any, ingest_bucket: str):
        self.table, self.s3, self.bucket = table, s3, ingest_bucket

    def _row(self, pid: str, asset_id: str) -> Optional[Mapping]:
        return self.table.get_item(Key=schema.asset_key(pid, asset_id), ConsistentRead=True).get("Item")

    def one(self, pid: str, asset_id: str) -> dict:
        if schema.asset_kind(asset_id) != "video":
            return {"assetId": asset_id, "skipped": "not a video"}
        row = self._row(pid, asset_id)
        if row is None:
            return {"assetId": asset_id, "skipped": "no such asset"}
        if row.get("rendition"):
            return {"assetId": asset_id, "skipped": "already has a rendition"}
        try:
            bucket, key = split_uri(str(row.get("sourceUri") or ""))
            # Always the object's CURRENT size, never the row's `sourceSize`: a session output is registered in
            # place, and an in-place rewrite after registration (staging: shot*_trimmed.mp4, rewritten 12 min
            # later at ~2x the bytes) left the reader stopping short -> InvalidDataError.
            size = int(self.s3.head_object(Bucket=bucket, Key=key).get("ContentLength") or 0)
            reader = hero_frame.S3RangeReader(self.s3, bucket, key, size)
            frame = hero_frame.extract(reader)
        except hero_frame.HeroFrameError as exc:
            log.info("hero frame: none for %s: %s", asset_id, exc)
            return {"assetId": asset_id, "failed": str(exc)}
        except Exception as exc:  # noqa: BLE001 — unreadable source, missing grant, …: a missing thumbnail only
            log.warning("hero frame: %s failed: %s: %s", asset_id, type(exc).__name__, exc)
            return {"assetId": asset_id, "failed": type(exc).__name__}
        out_key = poster.poster_key(pid, asset_id)
        self.s3.put_object(Bucket=self.bucket, Key=out_key, Body=frame.jpeg, ContentType="image/jpeg")
        rendition = {"key": out_key, "width": frame.width, "height": frame.height, "bytes": len(frame.jpeg),
                     "sampledAtMs": int(frame.at_s * 1000)}
        try:
            self.table.update_item(
                Key=schema.asset_key(pid, asset_id),
                UpdateExpression="SET rendition = :r",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(rendition)",
                ExpressionAttributeValues={":r": rendition},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            return {"assetId": asset_id, "skipped": "rendition appeared meanwhile"}
        log.info("hero frame: %s at %.1fs, %d bytes, read %d bytes of %d",
                 asset_id, frame.at_s, len(frame.jpeg), reader.bytes_fetched, size)
        return {"assetId": asset_id, "key": out_key, "readBytes": reader.bytes_fetched}

    def handle(self, event: Mapping) -> dict:
        pid = str(event.get("pid") or "")
        ids = list(event.get("assetIds") or ([event["assetId"]] if event.get("assetId") else []))
        if not pid or not ids:
            return {"error": "need pid and assetId(s)"}
        return {"pid": pid, "results": [self.one(pid, str(a)) for a in ids]}


_SERVICE: Optional[HeroFrameService] = None


def handler(event: dict, _context: Any = None) -> dict:
    global _SERVICE
    if _SERVICE is None:
        import boto3
        _SERVICE = HeroFrameService(boto3.resource("dynamodb").Table(os.environ["PROJECTS_TABLE"]),
                                    boto3.client("s3"), os.environ["INGEST_BUCKET"])
    return _SERVICE.handle(event or {})
