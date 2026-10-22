"""Harvest all of an actor's UI sessions from AgentCore Memory into an
evaluation dataset.

Uses boto3 ``bedrock-agentcore`` ``list_sessions`` / ``list_events`` (the same
APIs the Vite proxy calls via ListSessionsCommand/ListEventsCommand), decodes
each turn (tests/evaluation/decode.py), classifies + emits scenarios
(tests/evaluation/scenario_builder.py).

Usage::

    agent/.venv/bin/python -m tests.evaluation.harvester \
        --memory-id "$MEMORY_ID" --actor local-dev-user --region "$EVAL_REGION" \
        --out tests/evaluation/datasets/harvested/harvested.json
"""
from __future__ import annotations

import argparse
import json
import os

from .deployment import deployment_region
from pathlib import Path

from tests.evaluation.decode import events_to_turns, observed_trajectory, user_turns
from tests.evaluation.scenario_builder import build_dataset, category_counts


def _client(region: str | None = None):
    import boto3  # imported lazily so the pure modules stay dependency-free
    return boto3.client("bedrock-agentcore", region_name=deployment_region(region))


def list_all_sessions(client, memory_id: str, actor_id: str) -> list[dict]:
    sessions: list[dict] = []
    token = None
    while True:
        kw = {"memoryId": memory_id, "actorId": actor_id, "maxResults": 100}
        if token:
            kw["nextToken"] = token
        resp = client.list_sessions(**kw)
        sessions.extend(resp.get("sessionSummaries", []))
        token = resp.get("nextToken")
        if not token:
            break
    return sessions


def list_all_events(client, memory_id: str, actor_id: str, session_id: str) -> list[dict]:
    events: list[dict] = []
    token = None
    while True:
        kw = {
            "memoryId": memory_id,
            "actorId": actor_id,
            "sessionId": session_id,
            "includePayloads": True,
            "maxResults": 100,
        }
        if token:
            kw["nextToken"] = token
        resp = client.list_events(**kw)
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
    # Chronological order (list_events order is not guaranteed).
    events.sort(key=lambda e: str(e.get("eventTimestamp") or ""))
    return events


def harvest(memory_id: str, actor_id: str = "local-dev-user",
            region: str | None = None) -> list[dict]:
    """Return one transcript dict per session with turns + observed trajectory."""
    client = _client(region)
    transcripts: list[dict] = []
    for s in list_all_sessions(client, memory_id, actor_id):
        sid = s.get("sessionId") or s.get("sessionSummaryId") or ""
        if not sid:
            continue
        events = list_all_events(client, memory_id, actor_id, sid)
        turns = events_to_turns(events)
        transcripts.append({
            "session_id": sid,
            "turns": turns,
            "trajectory": observed_trajectory(turns),
            "prompts": user_turns(turns),
        })
    return transcripts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Harvest UI sessions into an evaluation dataset")
    ap.add_argument("--memory-id", default=os.environ.get("MEMORY_ID", ""))
    ap.add_argument("--actor", default=os.environ.get("LAB_DEFAULT_ACTOR_ID", "local-dev-user"))
    ap.add_argument("--region", default=deployment_region())
    ap.add_argument("--out", default="tests/evaluation/datasets/harvested/harvested.json")
    args = ap.parse_args(argv)

    if not args.memory_id:
        ap.error("MEMORY_ID required (pass --memory-id or set $MEMORY_ID)")

    transcripts = harvest(args.memory_id, args.actor, args.region)
    dataset = build_dataset(transcripts)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, indent=2))

    counts = category_counts(dataset)
    print(f"Harvested {len(transcripts)} session(s) → {len(dataset['scenarios'])} scenario(s)")
    print(f"By category: {json.dumps(counts, indent=2)}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
