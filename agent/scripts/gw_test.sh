#!/bin/bash
# Gateway security test — M2M OAuth2 → MCP invoke

# ── Config ──────────────────────────────────────────────────────────────────
GATEWAY_URL="https://dynamic-skills-agent-gateway-examplegw5.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
TOKEN_ENDPOINT="https://dynamic-skills-agent-m2m.auth.us-west-2.amazoncognito.com/oauth2/token"
CLIENT_ID="1exampleclientid0000000004"
SCOPE="dynamic-skills-agent-mcp-runtime/invoke"

# Pull client secret from Secrets Manager
CLIENT_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id /dynamic-skills-agent/m2m-client-secret \
  --region us-west-2 \
  --query 'SecretString' \
  --output text)

# ── Step 1: Get OAuth2 token ─────────────────────────────────────────────────
echo "=== Getting OAuth2 token ==="
TOKEN_RESPONSE=$(curl -s -X POST "$TOKEN_ENDPOINT" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=$CLIENT_ID" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "scope=$SCOPE")

echo "$TOKEN_RESPONSE" | python3 -m json.tool
ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo ""
echo "Token acquired: ${ACCESS_TOKEN:0:40}..."

# ── Step 2: MCP initialize ───────────────────────────────────────────────────
echo ""
echo "=== MCP initialize ==="
curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "test-client", "version": "1.0"}
    }
  }' | python3 -m json.tool

# ── Step 3: List tools ───────────────────────────────────────────────────────
echo ""
echo "=== MCP tools/list ==="
curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }' | python3 -m json.tool

# ── Step 4: Call a tool (search_documentation) ──────────────────────────────
echo ""
echo "=== MCP tools/call — search_documentation ==="
curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "dynamic-skills-agent-aws-mcp___aws___search_documentation",
      "arguments": {
        "search_phrase": "Lambda cold start"
      }
    }
  }' | python3 -m json.tool

