#!/usr/bin/env bash
# Run the main agent on localhost and invoke it directly -- no deploy, no CI.
#
# Why this exists: verifying an agent change through the pipeline costs 15+
# minutes (test -> build -> Docker image -> cdk deploy), and a wrong guess costs
# another 15. The agent is a BedrockAgentCoreApp with a plain __main__ entry
# point, so it serves /ping and /invocations locally against REAL AWS -- real
# Bedrock, real DynamoDB, real M2M gateways. A model-inheritance fix that would
# have taken 15 minutes to see was confirmed here in 20 seconds.
#
#   ./scripts/run_agent_local.sh start          # launches and RETURNS immediately
#   ./scripts/run_agent_local.sh wait           # blocks until /ping is healthy (~15-20s boot)
#   ./scripts/run_agent_local.sh ask "..."      # auto-waits for health, then sends a turn
#   ./scripts/run_agent_local.sh log  "Dispatching to"
#   ./scripts/run_agent_local.sh stop
#
# `start` is deliberately NON-BLOCKING: it spawns the agent detached and returns
# in ~1s. The boot itself takes ~15-20s (it connects the M2M gateways at
# startup), so an old `start` that polled /ping for up to 30s printed nothing
# and looked hung -- and got cancelled. `wait` (or `ask`, which waits on its
# own) is where you block for readiness now.
#
# LIMITS, so results are not over-read:
#   - user_jwt gateways (harness-user-mcp, which fronts fal and aws-mcp) need a
#     forwarded user token. Locally there is none, so those tools are absent.
#     Anything touching fal or AWS-docs tools still needs a deployed turn.
#   - dispatch_tool invokes the DEPLOYED sub-agent runtimes, so sub-agent code
#     is whatever is deployed -- only the orchestrator is local.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${AGENT_LOCAL_PORT:-8080}"
LOG="${AGENT_LOCAL_LOG:-/tmp/agent_local.log}"
PY="$ROOT/agent/.venv/bin/python"

# Poll /ping until healthy or timeout. $1 = max seconds (default 45). Returns
# non-zero on timeout. Kept out of `start` so `start` never blocks.
_wait_healthy() {
  local max="${1:-45}" i
  for ((i = 0; i < max; i++)); do
    if curl -fsS -m 3 "http://localhost:$PORT/ping" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

case "${1:-}" in
  start)
    [ -f "$ROOT/.env" ] || { echo "missing $ROOT/.env (see .env.example)" >&2; exit 1; }
    [ -x "$PY" ] || { echo "missing venv at $PY" >&2; exit 1; }
    if curl -fsS -m 2 "http://localhost:$PORT/ping" >/dev/null 2>&1; then
      echo "agent already healthy on :$PORT (log: $LOG)"; exit 0
    fi
    set -a; . "$ROOT/.env"; set +a
    export AWS_REGION="${AWS_REGION:-us-west-2}" PYTHONPATH="$ROOT/agent/src"
    # Detach fully: stdin from /dev/null and stdout/stderr to the log, so the
    # daemon shares NO file descriptor with the caller. Without </dev/null the
    # child inherits the caller's stdin, and a parent shell (or an agentic shell
    # tool that waits on the whole process tree) then blocks as if `start` were
    # still running -- the real reason `start` "hung" even after being made
    # non-blocking. The subshell keeps our cwd unchanged.
    ( cd "$ROOT/agent" && nohup "$PY" src/main.py </dev/null >"$LOG" 2>&1 & )
    sleep 1
    if pgrep -f "src/main.py" >/dev/null 2>&1; then
      echo "agent starting on :$PORT (pid $(pgrep -f 'src/main.py' | tr '\n' ' ' | sed 's/ $//'), log: $LOG)"
      echo "boot takes ~15-20s (gateway connects); this command does NOT block on it."
      echo "  readiness: $0 wait     |     send a turn (auto-waits): $0 ask \"...\""
      exit 0
    fi
    echo "agent failed to start; last log lines:" >&2; tail -25 "$LOG" >&2; exit 1
    ;;
  wait)
    if _wait_healthy "${2:-45}"; then
      echo "agent healthy on :$PORT (log: $LOG)"
    else
      echo "agent not healthy after ${2:-45}s; last log lines:" >&2; tail -25 "$LOG" >&2; exit 1
    fi
    ;;
  ask)
    [ $# -ge 2 ] || { echo "usage: $0 ask <prompt> [model_id]" >&2; exit 1; }
    # Auto-wait for readiness so `ask` works right after a non-blocking `start`.
    if ! _wait_healthy 60; then
      echo "agent not healthy (did you run '$0 start'?); last log lines:" >&2
      tail -20 "$LOG" >&2; exit 1
    fi
    "$PY" - "$PORT" "$2" "${3:-}" <<'PYEOF'
import json,sys,urllib.request,uuid
port,prompt,model=sys.argv[1],sys.argv[2],sys.argv[3]
body={"prompt":prompt,"actor_id":"local-test-actor","session_id":f"local-{uuid.uuid4().hex[:8]}"}
if model: body["model_id"]=model
r=urllib.request.Request(f"http://localhost:{port}/invocations",
    data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
with urllib.request.urlopen(r,timeout=300) as resp:
    print(resp.read().decode()[:4000])
PYEOF
    ;;
  log)   grep -E "${2:-.}" "$LOG" | tail -40 ;;
  # Match on the script path only. Matching "python src/main.py" failed on
  # macOS, where the interpreter reports as ".../MacOS/Python" (capital P), so
  # `stop` printed "not running" while the agent kept serving on :8080.
  stop)  pkill -f "src/main.py" 2>/dev/null && echo "stopped" || echo "not running" ;;
  *)     sed -n '2,32p' "$0"; exit 1 ;;
esac
