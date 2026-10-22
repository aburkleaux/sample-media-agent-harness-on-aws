// Slack ingress Lambda (behind a public Function URL).
// Verifies the raw-body signature, answers url_verification, normalizes human
// app_mentions, async-invokes the worker, and ACKs within Slack's 3s window.
// It never reads the bot token, calls Slack, invokes the agent, or touches S3.

import type { APIGatewayProxyEventV2, APIGatewayProxyStructuredResultV2 } from "aws-lambda";
import { verifySlackSignature } from "../slack-shared/signature";
import { normalizeSlackEnvelope } from "../slack-shared/mention";
import { getSecretJson, invokeLambdaEvent } from "../slack-shared/aws";

const REGION = process.env.AWS_REGION || "us-west-2";

interface SlackSecret {
  signingSecret: string;
  botToken: string;
  botUserId?: string;
}
let secretCache: SlackSecret | null = null;
async function getSecret(): Promise<SlackSecret> {
  if (secretCache) return secretCache;
  secretCache = await getSecretJson<SlackSecret>(REGION, process.env.SLACK_SECRET_ARN as string);
  return secretCache;
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyStructuredResultV2> {
  const method = event.requestContext?.http?.method ?? "POST";
  if (method === "GET") return { statusCode: 200, body: "slack ingress ok" };

  const raw = event.body
    ? event.isBase64Encoded
      ? Buffer.from(event.body, "base64").toString("utf8")
      : event.body
    : "";
  const headers = event.headers ?? {};

  let secret: SlackSecret;
  try {
    secret = await getSecret();
  } catch {
    return { statusCode: 500, body: JSON.stringify({ error: "secret_unavailable" }) };
  }

  const check = verifySlackSignature({
    signingSecret: secret.signingSecret,
    timestampHeader: headers["x-slack-request-timestamp"],
    signatureHeader: headers["x-slack-signature"],
    rawBody: raw,
  });
  if (!check.ok) return { statusCode: 401, body: JSON.stringify({ error: check.reason }) };

  let envelope: unknown;
  try {
    envelope = JSON.parse(raw);
  } catch {
    return { statusCode: 400, body: JSON.stringify({ error: "bad_json" }) };
  }

  const r = normalizeSlackEnvelope(envelope as any, secret.botUserId);
  if (r.kind === "challenge") {
    return {
      statusCode: 200,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ challenge: r.challenge }),
    };
  }
  if (r.kind === "ignore") {
    return { statusCode: 200, body: JSON.stringify({ ok: true, ignored: r.reason }) };
  }

  // Human app_mention: hand off to the worker asynchronously, then ACK.
  await invokeLambdaEvent(REGION, process.env.WORKER_FUNCTION_NAME as string, r.value);
  return { statusCode: 200, body: JSON.stringify({ ok: true }) };
}
