"""Design Agent — the "architect" AgentCore runtime.

Receives a HighLevelPlan + tool/skill/model catalog from the Planning Agent,
and produces an executable DAG (Plan JSON) conforming to the DAG_Schema.
Writes output to S3 and updates DynamoDB for durable state.
"""

import json, logging, os, time
from datetime import datetime, timezone

# How much of a skill description the planner is allowed to see.
#
# This was 120, which cut every description off mid-sentence -- and the ONE skill that
# said what it is NOT for put that clause at character 498, so the planner never saw it.
# It then authored a mux node telling the agent to combine two files with a tool that
# takes one, because nothing in its 120 characters said otherwise. Skills are the routing
# unit and there are only ~14, so the tokens buy real routing accuracy.
_SKILL_DESC_CHARS = 420


import boto3
from strands import Agent
from strands.models import BedrockModel
import runtime_config_bootstrap  # noqa: F401,E402  — hydrate env before first-party imports (V2 1536B cap)
from model.factory import build_model
from strands_tools import think  # noqa: F401 — kept for potential future use
from botocore.config import Config as BotoConfig
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from prompts.design import design_system_prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
DDB_TABLE = os.environ.get("PLAN_STATE_TABLE", "")
_boto_cfg = BotoConfig(read_timeout=300, connect_timeout=10, retries={"max_attempts": 4, "mode": "standard"})

# Lighter/faster model — reasoning is already done by the Planning Agent
DEFAULT_MODEL_ID = os.environ.get("DESIGN_MODEL_ID", "global.anthropic.claude-sonnet-5")

DESIGN_SYSTEM_PROMPT = design_system_prompt()


@app.entrypoint
async def invoke(payload, context):
    high_level_plan = payload.get("high_level_plan")
    if not high_level_plan:
        yield json.dumps({"error": "high_level_plan is required"})
        return

    catalog = payload.get("catalog", {})
    # Design is PINNED to the reasoning model (global Sonnet 5): plan design must not degrade
    # because the user selected a faster/cheaper orchestrator model. Ignore any propagated pick.
    model_id = DEFAULT_MODEL_ID
    session_id = payload.get("session_id", "")

    # Set OTEL baggage for trace correlation
    if session_id:
        try:
            from opentelemetry import baggage, context as otel_context
            ctx = baggage.set_baggage("session.id", session_id)
            otel_context.attach(ctx)
        except Exception:
            pass

    # Build the user prompt with the HighLevelPlan and catalog
    catalog_text = _format_catalog(catalog)
    plan_text = json.dumps(high_level_plan, indent=2) if isinstance(high_level_plan, dict) else str(high_level_plan)
    user_prompt = (
        f"HighLevelPlan:\n{plan_text}\n\n"
        f"{catalog_text}\n\n"
        "Produce the executable DAG Plan JSON."
    )

    # Retry self-correction: the orchestrator re-invokes with the validation errors from the
    # previous invalid plan (a preformatted "- type: message" string built by the sender). Frame
    # them as feedback on the model's OWN prior output so the retry prompt DIFFERS from attempt #1
    # — an identical prompt on the pinned reasoning model just reproduces the same invalid plan.
    validation_errors = payload.get("validation_errors")
    if validation_errors and str(validation_errors).strip():
        user_prompt += (
            "\n\nYour previous DAG Plan JSON failed validation with these errors:\n"
            f"{validation_errors}\n"
            "Produce a corrected plan that fixes them."
        )

    # No tools — the Design Agent produces JSON output directly
    tools = []

    model = build_model(model_id=model_id, boto_client_config=_boto_cfg)
    design_agent = Agent(model=model, system_prompt=DESIGN_SYSTEM_PROMPT, tools=tools)

    logger.info(
        "Designing DAG: mode=%s, sub_goals=%d, model=%s",
        high_level_plan.get("execution_mode", "?") if isinstance(high_level_plan, dict) else "?",
        len(high_level_plan.get("sub_goals", [])) if isinstance(high_level_plan, dict) else 0,
        model_id,
    )

    try:
        _exec_start = time.time()
        _cycle_count = 0
        _text_chunks: list[str] = []

        stream = design_agent.stream_async(user_prompt)
        async for event in stream:
            if event.get("start_event_loop"):
                _cycle_count += 1
                yield json.dumps({"type": "step", "status": "running", "label": f"Design cycle #{_cycle_count}"})

            if "current_tool_use" in event:
                ti = event["current_tool_use"]
                if isinstance(ti, dict):
                    tn = ti.get("name", "")
                    if tn:
                        yield json.dumps({"type": "step", "status": "running", "label": f"Using {tn}..."})

            # Collect text (do NOT stream via SSE — output goes to S3)
            if "data" in event and isinstance(event["data"], str):
                _text_chunks.append(event["data"])

        _exec_duration = time.time() - _exec_start

        # Write output to S3 and update DDB if session context provided
        s3_bucket = payload.get("s3_bucket", S3_BUCKET)
        ddb_table = payload.get("ddb_table", DDB_TABLE)
        full_text = "".join(_text_chunks)

        if s3_bucket and ddb_table and session_id:
            try:
                s3_key = f"plans/{session_id}/design/output.json"
                s3 = boto3.client("s3", region_name=REGION)
                s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=full_text.encode("utf-8"), ContentType="application/json")

                ddb = boto3.resource("dynamodb", region_name=REGION).Table(ddb_table)
                now = datetime.now(timezone.utc).isoformat()
                ddb.put_item(Item={
                    "session_id": session_id,
                    "node_id": "DESIGN",
                    "status": "completed",
                    "output_s3_key": s3_key,
                    "updated_at": now,
                    "completed_at": now,
                    "ttl": int(time.time()) + 86400,
                })
                logger.info("Design output written to s3://%s/%s", s3_bucket, s3_key)
                yield json.dumps({"type": "output_ref", "s3_key": s3_key})
            except Exception as e:
                logger.warning("Failed to write design output to S3/DDB: %s", e)
                # Fallback: stream the text directly
                yield full_text
        else:
            # No S3 config — stream text directly as fallback
            yield full_text

        yield json.dumps({
            "type": "execution_summary",
            "total_duration_ms": int(_exec_duration * 1000),
            "cycles": _cycle_count,
            "model": model_id,
        })
    except Exception as e:
        logger.exception("Design failed")
        yield json.dumps({"error": str(e)})


def _format_catalog(catalog: dict) -> str:
    """Format the tool/skill/model catalog for the user prompt."""
    sections = []

    tools = catalog.get("tools", [])
    if tools:
        lines = [
            f"  - {t}" if isinstance(t, str)
            else f"  - {t.get('name', t)}: {t.get('description', '')[:120]}"
            for t in tools
        ]
        sections.append("Available Tools:\n" + "\n".join(lines))

    skills = catalog.get("skills", [])
    if skills:
        lines = [
            f"  - {s.get('name', s)}: {s.get('description', '')[:_SKILL_DESC_CHARS]}" if isinstance(s, dict)
            else f"  - {s}"
            for s in skills
        ]
        sections.append("Available Skills:\n" + "\n".join(lines))

    models = catalog.get("models", [])
    if models:
        lines = [
            f"  - {m.get('id', m)} ({m.get('name', '')})" if isinstance(m, dict)
            else f"  - {m}"
            for m in models
        ]
        sections.append("Available Models:\n" + "\n".join(lines))

    return "\n\n".join(sections) if sections else "No catalog provided."


if __name__ == "__main__":
    app.run()
