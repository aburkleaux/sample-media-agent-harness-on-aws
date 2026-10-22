#!/bin/bash
# Gateway VPCE-enforcement test.
#
# Runs an M2M OAuth2 -> MCP tools/call against the Gateway and prints the
# HTTP status + body. Run this TWICE to validate an SCP that requires the
# Gateway to be invoked through a VPC endpoint (aws:SourceVpce):
#
#   1. From your laptop / a host OUTSIDE the VPC  -> expect DENY (403 / explicit deny)
#   2. From an EC2/host INSIDE the VPC (with the bedrock-agentcore interface
#      VPC endpoint + private DNS enabled)         -> expect SUCCESS (200)
#
# Same code path, only the network route differs — that's what the SCP gates.
#
# Usage:
#   source ../../.env && ./gw_vpce_test.sh
# or set the vars below explicitly.

set -u

# ── Config (falls back to .env values) ───────────────────────────────────────
GATEWAY_URL="${GATEWAY_URL:?set GATEWAY_URL (source .env)}"
TOKEN_ENDPOINT="${M2M_TOKEN_ENDPOINT:?set M2M_TOKEN_ENDPOINT (source .env)}"
CLIENT_ID="${M2M_CLIENT_ID:?set M2M_CLIENT_ID (source .env)}"
CLIENT_SECRET="${M2M_CLIENT_SECRET:?set M2M_CLIENT_SECRET (source .env)}"
SCOPE="${M2M_SCOPE:?set M2M_SCOPE (source .env)}"

echo "Gateway host: $(echo "$GATEWAY_URL" | sed -E 's#https?://([^/]+).*#\1#')"

# Show which IP the gateway hostname resolves to — proves VPCE vs public path.
GW_HOST=$(echo "$GATEWAY_URL" | sed -E 's#https?://([^/]+).*#\1#')
echo "=== DNS resolution for $GW_HOST (private IP => via VPCE) ==="
getent hosts "$GW_HOST" 2>/dev/null || nslookup "$GW_HOST" 2>/dev/null | tail -n +3
echo ""

# ── Step 1: OAuth2 token ──────────────────────────────────────────────────────
echo "=== Getting OAuth2 token ==="
ACCESS_TOKEN=$(curl -s -X POST "$TOKEN_ENDPOINT" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=$CLIENT_ID" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "scope=$SCOPE" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")

if [ -z "$ACCESS_TOKEN" ]; then
  echo "FAILED to obtain token (token endpoint is public Cognito, not gated by the SCP)"
  exit 1
fi
echo "Token acquired: ${ACCESS_TOKEN:0:32}..."
echo ""

# ── Step 2: Invoke the Gateway (this is the call the SCP gates) ───────────────
echo "=== MCP tools/call via $GW_HOST ==="
HTTP_STATUS=$(curl -s -o /tmp/gw_vpce_body.txt -w "%{http_code}" -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "dynamic-skills-agent-aws-mcp___aws___search_documentation",
      "arguments": {"search_phrase": "Lambda cold start"}
    }
  }')

echo "HTTP status: $HTTP_STATUS"
echo "--- body ---"
cat /tmp/gw_vpce_body.txt
echo ""
echo ""

case "$HTTP_STATUS" in
  200) echo "RESULT: SUCCESS — Gateway invoked (expected when traffic goes through the VPCE)";;
  403) echo "RESULT: DENIED (403) — expected from the public path if the SCP requires aws:SourceVpce";;
  *)   echo "RESULT: HTTP $HTTP_STATUS — inspect body above";;
esac
