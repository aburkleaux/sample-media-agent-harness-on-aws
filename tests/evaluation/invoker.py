"""Agent invoker for the evaluation runner.

`agent_invoker` calls the AgentCore runtime for a single turn (boto3
``invoke_agent_runtime``) with the scenario's session id, and normalizes the
project's SSE stream into a structured turn output. The SSE parsing
(`parse_sse_stream`) is pulled out as a pure function so it is unit-testable
against recorded payloads without a live agent.

Matches the project's stream shape (see ui/src/api.ts): ``data: {json}`` lines
carrying ``{"type":"text"|"step"|"output_ref", ...}``; a step with
``render_manifest_key`` signals a produced render manifest.
"""
from __future__ import annotations

from .deployment import deployment_region

import json
from dataclasses import dataclass, field


@dataclass
class TurnOutput:
    response_text: str = ""
    output_refs: list[str] = field(default_factory=list)
    manifest_keys: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "response_text": self.response_text,
            "output_refs": self.output_refs,
            "manifest_keys": self.manifest_keys,
            "steps": self.steps,
        }


def _iter_sse_lines(body) -> list[str]:
    """Normalize an SSE body (bytes, str, or an object with iter_lines) to lines."""
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body.splitlines()
    # botocore EventStream / iterable of bytes
    lines: list[str] = []
    try:
        for chunk in body:
            if isinstance(chunk, (bytes, bytearray)):
                chunk = chunk.decode("utf-8", errors="replace")
            lines.extend(str(chunk).splitlines())
    except TypeError:
        pass
    return lines


def parse_sse_stream(body) -> TurnOutput:
    """Parse an agent SSE stream into a TurnOutput. Invalid JSON lines are
    skipped (never raised), matching Requirement 3.5."""
    out = TurnOutput()
    for raw in _iter_sse_lines(body):
        line = raw.strip()
        if not line:
            continue
        payload = line[6:] if line.startswith("data: ") else line
        try:
            obj = json.loads(payload)
            if isinstance(obj, str):
                obj = json.loads(obj)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue

        etype = obj.get("type")
        if etype == "text" and isinstance(obj.get("text"), str):
            out.response_text += obj["text"]
        elif etype == "step":
            out.steps.append(obj)
            key = obj.get("render_manifest_key")
            if key:
                out.manifest_keys.append(key)
        elif etype == "output_ref":
            key = obj.get("s3_key") or obj.get("key")
            if key:
                out.output_refs.append(key)
                if "/render/" in key and str(key).endswith(".json"):
                    out.manifest_keys.append(key)
        elif "render_manifest_key" in obj and obj.get("render_manifest_key"):
            out.manifest_keys.append(obj["render_manifest_key"])
        elif isinstance(obj.get("text"), str) and etype is None:
            out.response_text += obj["text"]
    # De-dup manifest keys preserving order.
    seen: set[str] = set()
    out.manifest_keys = [k for k in out.manifest_keys if not (k in seen or seen.add(k))]
    return out


def agent_invoker(prompt: str, session_id: str, agent_arn: str,
                  region: str | None = None) -> TurnOutput:
    """Invoke the AgentCore runtime for one turn and return the parsed output.

    ``session_id`` must be stable across a scenario's turns to preserve
    conversation context (and >= 33 chars for the runtime).
    """
    import boto3
    client = boto3.client("bedrock-agentcore", region_name=deployment_region(region))
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt, "session_id": session_id}).encode(),
        contentType="application/json",
    )
    return parse_sse_stream(resp.get("response", b""))
