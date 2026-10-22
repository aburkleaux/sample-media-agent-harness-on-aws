#!/usr/bin/env bash
# Harvest a deployed media-toolkit stack's CloudFormation Outputs + Secrets
# Manager credentials bundle into infra/media-config/<env>.json -- the one
# manual step left in an otherwise fully decoupled deploy. env-config.ts reads
# this file automatically on the next `cdk synth`/`cdk deploy` for that env,
# so nothing in committed TypeScript source needs editing.
#
# The toolkit repo deploys standalone, first (its own `create_update_stack.sh`).
# Run this script against that stack's name, then deploy the harness:
#
#   bash ./scripts/harvest-media-toolkit-config.sh \
#       --stack-name media-toolkit-sandbox --env sandbox
#   cdk deploy -c env=sandbox
#
# Never writes the secret's `clientSecret` field to disk -- only the ARN
# (agent runtimes resolve the literal value from Secrets Manager themselves,
# at connect time, via `secretsmanager:GetSecretValue` on that ARN).
#
# Usage:
#   bash ./scripts/harvest-media-toolkit-config.sh \
#       --stack-name STACK_NAME --env ENV [--region REGION] [--profile PROFILE]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$SCRIPT_DIR/../media-config"

STACK_NAME=""
ENV_NAME=""
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-}}"
PROFILE=""

function usage() {
  echo "
Usage:
  bash ./scripts/harvest-media-toolkit-config.sh \\
      --stack-name STACK_NAME --env ENV [--region REGION] [--profile PROFILE]

  --stack-name  Name of the deployed media-toolkit CloudFormation stack
                (the micro-workflow-bundle parent stack, or a standalone
                media-toolkit-gateway stack).
  --env         Harness env name this config is for (sandbox|staging|prod|dev).
                Written to infra/media-config/ENV.json.
  --region      [optional] Defaults to \$AWS_DEFAULT_REGION / \$AWS_REGION.
  --profile     [optional] AWS CLI profile.
"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --env)        ENV_NAME="$2";   shift 2 ;;
    --region)     REGION="$2";     shift 2 ;;
    --profile)    PROFILE="$2";    shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "error: unknown argument '$1'" && usage && exit 1 ;;
  esac
done

[ -z "$STACK_NAME" ] && echo "error: --stack-name is required" && usage && exit 1
[ -z "$ENV_NAME" ] && echo "error: --env is required" && usage && exit 1
[ -z "$REGION" ] && echo "error: --region not given and no AWS_DEFAULT_REGION/AWS_REGION set" && usage && exit 1

AWS_ARGS=(--region "$REGION")
[ -n "$PROFILE" ] && AWS_ARGS+=(--profile "$PROFILE")

echo "Reading Outputs from stack '$STACK_NAME' ($REGION)..."
OUTPUTS_JSON=$(aws cloudformation describe-stacks "${AWS_ARGS[@]}" \
  --stack-name "$STACK_NAME" --query 'Stacks[0].Outputs' --output json)

function output() {
  echo "$OUTPUTS_JSON" | jq -r --arg key "$1" '.[] | select(.OutputKey == $key) | .OutputValue'
}

GATEWAY_URL=$(output GatewayUrl)
SECRET_ARN=$(output GatewayCredentialsSecretArn)
SECRET_NAME=$(output GatewayCredentialsSecretName)
PROXY_BUCKET=$(output ProxyBucketName)
ROLE_ARNS_CSV=$(output IngestBucketReadRoleArns)

[ -z "$GATEWAY_URL" ] && echo "error: stack has no GatewayUrl output -- is '$STACK_NAME' the right stack?" && exit 1
[ -z "$SECRET_NAME" ] && echo "error: stack has no GatewayCredentialsSecretName output" && exit 1

echo "Reading credentials bundle from secret '$SECRET_NAME'..."
SECRET_STRING=$(aws secretsmanager get-secret-value "${AWS_ARGS[@]}" \
  --secret-id "$SECRET_NAME" --query SecretString --output text)

CLIENT_ID=$(echo "$SECRET_STRING" | jq -r '.clientId')
TOKEN_ENDPOINT=$(echo "$SECRET_STRING" | jq -r '.tokenEndpoint')
SCOPE=$(echo "$SECRET_STRING" | jq -r '.scope')

mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/$ENV_NAME.json"

jq -n \
  --arg gatewayUrl "$GATEWAY_URL" \
  --arg clientId "$CLIENT_ID" \
  --arg credentialsSecretArn "$SECRET_ARN" \
  --arg tokenEndpoint "$TOKEN_ENDPOINT" \
  --arg scope "$SCOPE" \
  --arg proxyBucket "$PROXY_BUCKET" \
  --arg roleArnsCsv "$ROLE_ARNS_CSV" \
  '{
    gatewayUrl: $gatewayUrl,
    clientId: $clientId,
    credentialsSecretArn: $credentialsSecretArn,
    tokenEndpoint: $tokenEndpoint,
    scope: $scope,
    proxyBucket: $proxyBucket,
    m2cServiceRoleArns: ($roleArnsCsv | split(",") | map(select(length > 0)))
  }' > "$OUT_FILE"

echo
echo "========================================"
echo "  Wrote $OUT_FILE"
echo "========================================"
echo "  gatewayUrl   : $GATEWAY_URL"
echo "  proxyBucket  : $PROXY_BUCKET"
echo "  role count   : $(echo "$ROLE_ARNS_CSV" | tr ',' '\n' | grep -c . || true)"
echo "----------------------------------------"
echo "  Not committed (infra/media-config/ is gitignored) -- the client"
echo "  secret itself was never written to disk. Next: cdk deploy -c env=$ENV_NAME"
echo "========================================"
