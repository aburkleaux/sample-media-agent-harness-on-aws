// Minimal SigV4-signed AWS calls for the Slack Lambdas, using only the smithy
// libraries available in this repo's npm mirror (no @aws-sdk/client-* which the
// mirror lacks). Credentials come from the Lambda execution role via
// defaultProvider(). Bundled by esbuild (externalModules: []).

import https from "node:https";
import { SignatureV4 } from "@smithy/signature-v4";
import { HttpRequest } from "@smithy/protocol-http";
import { Sha256 } from "@aws-crypto/sha256-js";
import { defaultProvider } from "@aws-sdk/credential-provider-node";

const signers: Record<string, SignatureV4> = {};
function signerFor(service: string, region: string): SignatureV4 {
  const key = `${service}:${region}`;
  if (!signers[key]) {
    signers[key] = new SignatureV4({
      service,
      region,
      credentials: defaultProvider(),
      sha256: Sha256,
    });
  }
  return signers[key];
}

export async function sigv4Fetch(opts: {
  service: string;
  region: string;
  url: string;
  method?: string;
  headers?: Record<string, string>;
  body?: string;
}): Promise<{ status: number; text: string }> {
  const parsed = new URL(opts.url);
  const bodyBuf = Buffer.from(opts.body ?? "", "utf8");
  const signed = await signerFor(opts.service, opts.region).sign(
    new HttpRequest({
      method: opts.method ?? "POST",
      protocol: parsed.protocol,
      hostname: parsed.hostname,
      path: parsed.pathname,
      query: Object.fromEntries(parsed.searchParams),
      headers: { host: parsed.hostname, ...(opts.headers ?? {}) },
      body: bodyBuf,
    }),
  );
  return await new Promise((resolve, reject) => {
    const req = https.request(
      opts.url,
      { method: opts.method ?? "POST", headers: signed.headers as Record<string, string> },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () =>
          resolve({ status: res.statusCode ?? 500, text: Buffer.concat(chunks).toString("utf8") }),
        );
      },
    );
    req.on("error", reject);
    if (bodyBuf.length) req.write(bodyBuf);
    req.end();
  });
}

/** GetSecretValue → parse the JSON SecretString into T. */
export async function getSecretJson<T>(region: string, secretId: string): Promise<T> {
  const r = await sigv4Fetch({
    service: "secretsmanager",
    region,
    url: `https://secretsmanager.${region}.amazonaws.com/`,
    headers: {
      "content-type": "application/x-amz-json-1.1",
      "x-amz-target": "secretsmanager.GetSecretValue",
    },
    body: JSON.stringify({ SecretId: secretId }),
  });
  if (r.status >= 300) throw new Error(`GetSecretValue ${r.status}: ${r.text.slice(0, 200)}`);
  const envelope = JSON.parse(r.text) as { SecretString?: string };
  return JSON.parse(envelope.SecretString ?? "{}") as T;
}

/** Async (Event) Lambda invoke — fire-and-forget. */
export async function invokeLambdaEvent(
  region: string,
  functionName: string,
  payload: unknown,
): Promise<void> {
  const r = await sigv4Fetch({
    service: "lambda",
    region,
    url: `https://lambda.${region}.amazonaws.com/2015-03-31/functions/${encodeURIComponent(functionName)}/invocations`,
    headers: { "x-amz-invocation-type": "Event", "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (r.status >= 300) throw new Error(`Invoke ${functionName} ${r.status}: ${r.text.slice(0, 200)}`);
}

/** InvokeAgentRuntime (DEFAULT qualifier) → raw SSE/NDJSON text. */
export async function invokeAgentRuntime(
  region: string,
  runtimeArn: string,
  sessionId: string,
  body: unknown,
): Promise<string> {
  const url =
    `https://bedrock-agentcore.${region}.amazonaws.com` +
    `/runtimes/${encodeURIComponent(runtimeArn)}/invocations?qualifier=DEFAULT`;
  const r = await sigv4Fetch({
    service: "bedrock-agentcore",
    region,
    url,
    headers: {
      "content-type": "application/json",
      "x-amzn-bedrock-agentcore-runtime-session-id": sessionId,
    },
    body: JSON.stringify(body),
  });
  if (r.status >= 300) throw new Error(`Main Agent ${r.status}: ${r.text.slice(0, 300)}`);
  return r.text;
}


/** DynamoDB PutItem (SigV4). Returns conditionalFailed=true on a condition miss. */
export async function dynamoPutItem(
  region: string,
  table: string,
  item: Record<string, unknown>,
  conditionExpression?: string,
): Promise<{ ok: boolean; conditionalFailed?: boolean }> {
  const body: Record<string, unknown> = { TableName: table, Item: item };
  if (conditionExpression) body.ConditionExpression = conditionExpression;
  const r = await sigv4Fetch({
    service: "dynamodb",
    region,
    url: `https://dynamodb.${region}.amazonaws.com/`,
    headers: { "content-type": "application/x-amz-json-1.0", "x-amz-target": "DynamoDB_20120810.PutItem" },
    body: JSON.stringify(body),
  });
  if (r.status < 300) return { ok: true };
  if (r.text.includes("ConditionalCheckFailedException")) return { ok: false, conditionalFailed: true };
  throw new Error(`PutItem ${r.status}: ${r.text.slice(0, 200)}`);
}

/** DynamoDB GetItem (SigV4). Returns the raw typed Item, or null. */
export async function dynamoGetItem(
  region: string,
  table: string,
  key: Record<string, unknown>,
): Promise<Record<string, any> | null> {
  const r = await sigv4Fetch({
    service: "dynamodb",
    region,
    url: `https://dynamodb.${region}.amazonaws.com/`,
    headers: { "content-type": "application/x-amz-json-1.0", "x-amz-target": "DynamoDB_20120810.GetItem" },
    body: JSON.stringify({ TableName: table, Key: key }),
  });
  if (r.status >= 300) throw new Error(`GetItem ${r.status}: ${r.text.slice(0, 200)}`);
  const parsed = JSON.parse(r.text) as { Item?: Record<string, any> };
  return parsed.Item ?? null;
}
