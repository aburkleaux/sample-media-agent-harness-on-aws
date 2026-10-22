#!/usr/bin/env python3
"""DEV ONLY: seed a local Project for trying the organize features (folders, Move to…, drag, rename).

Writes into the moto Projects table that ``local_projects_stack.py moto`` (or ``all``) serves on 127.0.0.1:5005 —
never AWS. The Project "Full Swing (local demo)" gets ~20 assets spread over user folders, a ``Chats/…`` folder,
a legacy ``Sessions/…`` one and the root, owned by the Dev Login user (``local-dev-user``). Rows have the shape the
list, folder and organize routes read; there are no S3 objects behind them, so thumbnails fall back to the kind
icon. Re-running overwrites the same rows (fixed ids).

  cd agent
  uv run --extra dev --with 'moto[server]' python scripts/local_projects_stack.py moto     # terminal 1
  uv run --extra dev python scripts/local_organize_seed.py                                   # once
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import boto3

MOTO_URL = os.environ.get("PROJECTS_DDB_ENDPOINT", "http://127.0.0.1:5005")
TABLE = os.environ.get("PROJECTS_TABLE", "mediaworks-harness-projects-local")
REGION = "us-west-2"
OWNER = os.environ.get("LAB_DEFAULT_ACTOR_ID", "local-dev-user")
PID = "p" + "f5" * 16
NAME = "Full Swing (local demo)"
BUCKET = "local-ingest"

FILES: list[tuple[str, str, str]] = [  # (kind, display name, folder)
    ("video", "A003_C017_0815XK.MXF", "Footage/Day 1"),
    ("video", "A003_C018_0815RM.MXF", "Footage/Day 1"),
    ("video", "A003_C021_0815BS.MXF", "Footage/Day 1"),
    ("video", "A004_C002_0816QA.MXF", "Footage/Day 2"),
    ("video", "A004_C003_0816QA.MXF", "Footage/Day 2"),
    ("video", "DJI_0042.MOV", "Footage/Drone"),
    ("video", "DJI_0043.MOV", "Footage/Drone"),
    ("video", "GOPR0117.MP4", ""),
    ("video", "GOPR0118.MP4", ""),
    ("audio", "VO_take3.wav", "Audio"),
    ("document", "FullSwing_Boards_v3.pdf", "Boards"),
    ("document", "FullSwing_Script.pdf", "Reference"),
    ("image", "board_07.jpg", "Boards/Board 07 · Hands on club"),
    ("video", "grip_clip_01.mp4", "Chats/Grip review · 2026-10-16"),
    ("video", "grip_clip_02.mp4", "Chats/Grip review · 2026-10-16"),
    ("data", "shots.json", "Chats/Grip review · 2026-10-16"),
    ("document", "meeting_summary.pdf", "Sessions/Discovery call · 2026-10-15"),
    ("video", "transcoded.mp4", "Sessions/Discovery call · 2026-10-15/Quick Edit"),
]


def rows() -> list[dict]:
    pk = f"PROJECT#{PID}"
    now = datetime.now(timezone.utc)
    out: list[dict] = [
        {"PK": pk, "SK": "META", "projectId": PID, "name": NAME, "nameFold": NAME.casefold(), "ownerSub": OWNER,
         "personal": False, "status": "active", "memberCount": 0, "createdAt": now.isoformat(),
         "activeAssetCount": len(FILES), "collectionState": "ready"},
        {"PK": pk, "SK": f"MEMBER#{OWNER}", "role": "owner", "display": OWNER, "addedAt": now.isoformat(), "addedBy": OWNER},
        {"PK": f"USER#{OWNER}", "SK": f"PROJECT#{PID}", "role": "owner"},
    ]
    for i, (kind, name, folder) in enumerate(FILES):
        aid = f"{kind}:{i:08x}-0000-4000-8000-f5f5f5f5f5f5"
        at = (now - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        key = f"projects/{PID}/{aid}/{name}"
        row = {"PK": pk, "SK": f"ASSET#{aid}", "schemaVersion": 1, "assetId": aid, "kind": kind,
               "source": "session" if folder.startswith(("Chats/", "Sessions/")) else "upload",
               "sourceUri": f"s3://{BUCKET}/{key}", "sourceBucket": BUCKET, "sourceKey": key, "sourceSize": 4096 * (i + 1),
               "contentType": "application/octet-stream", "displayName": name, "displayFold": name.casefold(),
               "status": "stored" if kind == "data" else "indexed", "registeredAt": at, "addedBy": OWNER,
               "rowVersion": 1, "assetListPk": pk, "assetListSk": f"{at}#{aid}"}
        if kind == "data":
            row["role"] = "analysis"
        if folder:
            row["folder"] = folder
        out.append(row)
    # Two saved moments (bookmarks) of the drone take, filed with the board they illustrate.
    drone = f"video:{5:08x}-0000-4000-8000-f5f5f5f5f5f5"
    for j, (start, end) in enumerate(((62000, 65000), (4000, 9000))):
        bid = f"bookmark:{j:08x}-0000-4000-8000-b0b0b0b0b0b0"
        out.append({"PK": pk, "SK": f"BOOKMARK#{bid}", "bookmarkId": bid, "assetId": drone, "startMs": start,
                    "endMs": end, "label": f"DJI_0042 · grip close-up {j + 1}", "folder": "Boards/Board 07 · Hands on club",
                    "assetKind": "video", "assetDisplayName": "DJI_0042.MOV", "createdBy": OWNER,
                    "createdAt": (now + timedelta(seconds=j)).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    return out


def main() -> int:
    table = boto3.resource("dynamodb", endpoint_url=MOTO_URL, region_name=REGION,
                           aws_access_key_id="testing", aws_secret_access_key="testing").Table(TABLE)
    items = rows()
    with table.batch_writer() as w:
        for item in items:
            w.put_item(Item=item)
    print(f"seeded {NAME} ({PID}): {len(FILES)} assets for {OWNER} into {TABLE} at {MOTO_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
