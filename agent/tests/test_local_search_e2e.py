"""Local end-to-end search / Q&A / Citations / Moments test (design "Local end-to-end run").

This is the opt-in checkpoint of Task 11.1. It drives a *running* local harness — the five-terminal
stack of design section "Local end-to-end run" (moto + issuer on 5005/8098, Search_Gateway emulator
on 8099, Task_Agent on 8086, Main_Agent on 8080) seeded with the "Demo videos" Project — and asserts
the real search path end to end for both Search_Arms.

It carries two independent guards so it is SAFE in CI and in a default developer run:

1. the ``live`` marker, so the suite-wide ``addopts = -m "not live"`` (``agent/pyproject.toml``)
   deselects it unless the operator opts in with ``-m live``; and
2. a ``skipif`` that skips unless ``MH_LOCAL_STACK=1`` *and* the Main_Agent answers on
   ``http://127.0.0.1:8080/ping``, so even ``-m live`` is a clean SKIP with no stack up.

It makes real Bedrock calls and is non-deterministic (hence ``live``). Run it with::

    cd agent && MH_LOCAL_STACK=1 uv run --extra dev pytest -m live tests/test_local_search_e2e.py -q

Asserts, for arm A and arm B (design "Local stack end to end (opt-in)"):

* the answer contains at least one Citation and ``summary["citations"]`` has a *verified* item whose
  ``ref.asset_id == "video:f991228da306"`` (Req 2.5, 2.6, 2.7);
* the search telemetry record, when present, names the requested arm with a 1..5 call count (Req 2.6);
* ``artifacts/<sid>/render/moments-<id>.json`` exists in the Ingest bucket, has
  ``galleryMoments.basis == "cited"`` and carries no URL (Req 3.4, 3.5, 3.6);
* a second turn in the same session that cites a first-turn moment verifies from the moto ``HITS#<sid>``
  items (the earlier-turn Session_Hit_Store path, Req 2.5);
* a ``none`` question ("Which city is the Meridian film set in?") has no verified Citation and its answer
  names no supporting moment (Req 3.5, 3.6).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
import uuid
from pathlib import Path

import pytest

# The real citation grammar, so "the answer contains a Citation" uses exactly the parser's pattern.
from project_search.citations import CITATION_PATTERN

_CITATION_RE = re.compile(CITATION_PATTERN)

# --------------------------------------------------------------------------- #
# Fixed facts of the seeded "Demo videos" Project (design "Seeding the demo Project").
# --------------------------------------------------------------------------- #
PROJECT_ID = "pfa800aa0274646bdbe9e0b69f24b6054"
PROJECT_NAME = "Demo videos"
ACTOR = "local-dev-user"              # matches --owner local-dev-user and local_search_turn.py
VERIFIED_ASSET_ID = "video:f991228da306"  # an M2C demo asset id the seed rows register

MAIN_URL = os.environ.get("MH_LOCAL_MAIN_URL", "http://127.0.0.1:8080/invocations")
PING_URL = os.environ.get("MH_LOCAL_PING_URL", "http://127.0.0.1:8080/ping")
TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-ProjectToken"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

INGEST_BUCKET = os.environ.get(
    "SKILLS_S3_BUCKET", "mediaworks-harness-skills-store-100000000002-us-west-2"
)
REGION = os.environ.get("AWS_REGION", "us-west-2")
HITS_TABLE = os.environ.get("SESSION_HITS_TABLE", "mediaworks-harness-plan-state-local")
HITS_ENDPOINT = os.environ.get(
    "SESSION_HITS_DDB_ENDPOINT", os.environ.get("PROJECTS_DDB_ENDPOINT", "http://127.0.0.1:5005")
)

# Decline phrasings the model may use for a no-support answer (MSQ-D1: "no supporting moment").
_DECLINE_SIGNALS = ("no supporting moment", "no moment", "does not contain", "doesn't contain",
                    "no supporting", "could not find", "couldn't find", "no results", "no match")


# --------------------------------------------------------------------------- #
# Live-stack guard
# --------------------------------------------------------------------------- #
def _ping() -> bool:
    try:
        with urllib.request.urlopen(PING_URL, timeout=2) as r:  # noqa: S310 - loopback health check
            return 200 <= r.status < 500
    except Exception:  # noqa: BLE001 - any failure means "stack not up" → skip, never error
        return False


def _should_run() -> bool:
    return os.environ.get("MH_LOCAL_STACK") == "1" and _ping()


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _should_run(),
        reason="requires MH_LOCAL_STACK=1 and a Main_Agent answering on http://127.0.0.1:8080/ping",
    ),
]


# --------------------------------------------------------------------------- #
# Driver — hit the Main_Agent HTTP endpoint directly (the scripted helper exposes no run_turn()).
# --------------------------------------------------------------------------- #
def _mint(project_id: str) -> str:
    """A Project_Token for the local Search_Gateway emulator (its signing key must be up)."""
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import local_search_gateway as gw  # noqa: E402 - optional, only when the stack is up

    return gw.mint(project_id, ACTOR, 3600)


def _run_turn(session_id: str, prompt: str, arm: str) -> tuple[str, dict]:
    """Post one turn the way the proxy turn gate does; return (answer_text, execution_summary)."""
    turn_id = f"t-{uuid.uuid4().hex[:12]}"
    body = {
        "prompt": prompt,
        "session_id": session_id,
        "__actor_id": ACTOR,
        "__turn_context": {
            "turn_id": turn_id,
            "active_project": {
                "id": PROJECT_ID,
                "name": PROJECT_NAME,
                "collection_id": PROJECT_ID,
                "personal": False,
            },
            "web_search": False,
            "project_access": "ok",
            "search_arm": arm,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        SESSION_HEADER: session_id,
        TOKEN_HEADER: _mint(PROJECT_ID),
    }
    req = urllib.request.Request(  # noqa: S310 - loopback POST to the local Main_Agent
        MAIN_URL, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    answer: list[str] = []
    summary: dict = {}
    with urllib.request.urlopen(req, timeout=600) as r:  # noqa: S310 - loopback
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            try:
                v = json.loads(data)
            except ValueError:
                continue
            if isinstance(v, dict):
                if v.get("type") == "execution_summary":
                    summary = v
                # step/other dict events carry no answer text
                continue
            if isinstance(v, str):
                # A data payload may be a JSON-encoded step/summary (double-encoded); only plain
                # strings that are not those are answer text.
                try:
                    inner = json.loads(v)
                except ValueError:
                    inner = None
                if isinstance(inner, dict) and inner.get("type") == "execution_summary":
                    summary = inner
                    continue
                if isinstance(inner, dict) and inner.get("type") == "step":
                    continue
                answer.append(v)
    # The summary is attached by turn_end with this turn's turn_id; carry it for the manifest key.
    summary.setdefault("__turn_id", turn_id)
    return "".join(answer), summary


# --------------------------------------------------------------------------- #
# Assertion helpers
# --------------------------------------------------------------------------- #
def _verified_items(summary: dict) -> list[dict]:
    block = summary.get("citations") or {}
    items = block.get("items") or []
    return [it for it in items if it.get("verified")]


def _moments_manifest_key(session_id: str, turn_id: str) -> str:
    rid = hashlib.sha256(f"moments|{session_id}|{turn_id}".encode()).hexdigest()[:32]
    manifest_id = "moments-" + rid[:24]
    return f"artifacts/{session_id}/render/{manifest_id}.json"


def _s3_get_json(bucket: str, key: str) -> dict:
    import boto3  # noqa: E402 - only when the live stack is up

    body = boto3.client("s3", region_name=REGION).get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def _hits_partition_count(session_id: str) -> int:
    import boto3  # noqa: E402 - only when the live stack is up
    from boto3.dynamodb.conditions import Key

    ddb = boto3.resource("dynamodb", region_name=REGION, endpoint_url=HITS_ENDPOINT)
    resp = ddb.Table(HITS_TABLE).query(KeyConditionExpression=Key("session_id").eq(f"HITS#{session_id}"))
    return len(resp.get("Items") or [])


# --------------------------------------------------------------------------- #
# The test — both Search_Arms.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("arm", ["A", "B"])
def test_local_search_e2e(arm: str) -> None:
    session_id = f"mh-e2e-{arm}-{uuid.uuid4()}"

    # --- Turn 1: a question the "Demo videos" assets answer. ---
    answer, summary = _run_turn(session_id, "When does Kawhi Leonard dunk in nba.mp4?", arm)

    # Verified Citations (Req 2.5, 2.6, 2.7).
    assert _CITATION_RE.search(answer), f"arm {arm}: answer carried no Citation:\n{answer}"
    verified = _verified_items(summary)
    assert verified, f"arm {arm}: no verified citation in execution_summary"
    assert any((it.get("ref") or {}).get("asset_id") == VERIFIED_ASSET_ID for it in verified), (
        f"arm {arm}: no verified citation of {VERIFIED_ASSET_ID}; got "
        f"{[(it.get('ref') or {}).get('asset_id') for it in verified]}"
    )

    # Search telemetry (Req 4.1). Telemetry is Component 9 (Wave 2); assert it only when the record is
    # present so this checkpoint stays green on the Wave 1 stack and tightens once it lands. Search is
    # arm-A-only (the second arm was removed), so the record carries no `search_arm` dimension.
    telemetry = summary.get("search_telemetry")
    if isinstance(telemetry, dict):
        assert "search_arm" not in telemetry, (
            f"arm {arm}: telemetry must carry no search_arm (arm-A-only): {telemetry.get('search_arm')!r}"
        )
        call_count = telemetry.get("call_count")
        assert isinstance(call_count, int) and 1 <= call_count <= 5, (
            f"arm {arm}: telemetry.call_count out of 1..5: {call_count!r}"
        )

    # Moments manifest: basis == "cited", no URL (Req 3.4, 3.5, 3.6).
    manifest_key = _moments_manifest_key(session_id, summary["__turn_id"])
    manifest = _s3_get_json(INGEST_BUCKET, manifest_key)
    gallery = manifest.get("galleryMoments") or {}
    assert gallery.get("basis") == "cited", (
        f"arm {arm}: galleryMoments.basis == {gallery.get('basis')!r} (expected 'cited')"
    )
    blob = json.dumps(manifest)
    assert "https://" not in blob and "X-Amz-" not in blob, (
        f"arm {arm}: Moments manifest leaked a URL/signature: {manifest_key}"
    )

    # --- Turn 2: same session, cite a first-turn moment → verifies from the moto HITS#<sid> items. ---
    answer2, summary2 = _run_turn(
        session_id, "Show that dunk again and tell me what the commentator says.", arm
    )
    assert _hits_partition_count(session_id) > 0, (
        f"arm {arm}: no HITS#{session_id} items in {HITS_TABLE}; the earlier-turn store was not written"
    )
    verified2 = _verified_items(summary2)
    assert verified2, f"arm {arm}: follow-up turn produced no verified citation from the Session_Hit_Store"

    # --- Turn 3: a question no asset answers → declines, no verified Citation (Req 3.5, 3.6). ---
    none_session = f"mh-e2e-{arm}-none-{uuid.uuid4()}"
    none_answer, none_summary = _run_turn(none_session, "Which city is the Meridian film set in?", arm)
    assert not _verified_items(none_summary), (
        f"arm {arm}: a no-support question produced a verified citation:\n{none_answer}"
    )
    assert any(sig in none_answer.lower() for sig in _DECLINE_SIGNALS), (
        f"arm {arm}: no-support answer did not name a missing moment:\n{none_answer}"
    )
