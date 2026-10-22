#!/usr/bin/env bash
# Bring up the LOCAL render agent and run the end-to-end render harness.
#
# Replaces an unversioned /tmp/e2e-env.sh that had to be hand-rebuilt every session (and was
# lost between them). Everything the harness needs is derived here, so the whole check is one
# command from a clean shell:
#
#     agent/scripts/e2e_render_up.sh
#
# What it does:
#   1. starts render_agent.py on :8085 (a real BedrockAgentCoreApp, just not deployed)
#   2. points LOCAL_AGENT_ENDPOINTS at it, keyed by the render runtime ARN the dispatcher uses
#   3. runs scripts/e2e_render_local.py, then tears the server down
#
# Requires AWS creds for the S3 bucket the manifests are written to/read from (read+write
# under artifacts/e2e-*/ only -- it never deletes). Defaults to the STAGING skills bucket,
# because that is where the captured fixture sessions the harness replays actually live.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${E2E_RENDER_PORT:-8085}"
export SKILLS_S3_BUCKET="${SKILLS_S3_BUCKET:-mediaworks-harness-skills-store-100000000002-us-west-2}"
export ARTIFACTS_DOMAIN="${ARTIFACTS_DOMAIN:-dexamplecdn002.cloudfront.net}"
export AWS_REGION="${AWS_REGION:-us-west-2}"

# The dispatcher resolves the render runtime by ARN; any well-formed ARN works because
# LOCAL_AGENT_ENDPOINTS intercepts it before any AWS call. Keep it obviously fake so a
# misconfigured run cannot hit a real runtime.
export RENDER_AGENT_RUNTIME_ARN="${RENDER_AGENT_RUNTIME_ARN:-arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/local_render-LOCAL}"
export LOCAL_AGENT_ENDPOINTS="{\"${RENDER_AGENT_RUNTIME_ARN}\":\"http://127.0.0.1:${PORT}\"}"

LOG="${TMPDIR:-/tmp}/e2e-render-agent.log"
echo "starting local render agent on :${PORT} (log: ${LOG})"
# `python src/render_agent.py` runs `app.run()`, which hardcodes port 8080 and reads no PORT
# env var — so it collides with a local MAIN agent, which also wants 8080. Import the module
# and pass the port explicitly instead of teaching production code about a test-only env var.
uv run --extra dev python -c "
import sys; sys.path.insert(0, 'src')
import render_agent; render_agent.app.run(port=${PORT})
" >"$LOG" 2>&1 &
AGENT_PID=$!
trap 'kill "$AGENT_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$AGENT_PID" 2>/dev/null; then
    echo "render agent died on startup; log follows:" >&2
    tail -40 "$LOG" >&2
    exit 1
  fi
  sleep 0.5
done

echo "render agent up; running harness"
uv run --extra dev python scripts/e2e_render_local.py
