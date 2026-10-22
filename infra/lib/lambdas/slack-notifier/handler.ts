// Slack notifier Lambda (Feature 1). Second consumer of the EXISTING job-registry
// DynamoDB stream. On a terminal transition of a Slack-origin job (parent plan or
// standalone — nodeId absent, so no per-node spam), it dedupes, looks up the
// originating thread, and pushes the result back: on success it asks the Main
// Agent to fetch+present the result in the same session, then posts it (+ native
// upload within the cap); on failure it posts a bounded notice.
//
// Additive: reads the existing stream + registry schema; changes nothing in the
// harness. Only slack- sessions are ever acted on.

import type { DynamoDBStreamEvent, DynamoDBRecord } from "aws-lambda";
import { S3Client } from "@aws-sdk/client-s3";
import { parseHarnessStream } from "../slack-shared/streamParse";
import {
  getSecretJson,
  invokeAgentRuntime,
  dynamoPutItem,
  dynamoGetItem,
} from "../slack-shared/aws";
import { slackCall, headArtifact, uploadFromUrl, type ArtifactSource } from "../slack-shared/slackClient";
import {
  isSlackSession,
  isTerminalStatus,
  isSuccessStatus,
  extractArtifactUrls,
  shouldUploadNatively,
  cdnPrivateFromEnv,
  rewriteArtifactUrlsForPrivateCdn,
} from "../slack-shared/notify";

const REGION = process.env.AWS_REGION || "us-west-2";
const MAX_UPLOAD = Number(process.env.MAX_UPLOAD_BYTES || "52428800");
const ARTIFACTS_DOMAIN = process.env.ARTIFACTS_DOMAIN || "";
// artifactsCdnPrivate: our CDN links 403 for a Slack reader, so posted text links the UI's opener instead.
const UI_DOMAIN = process.env.UI_DOMAIN || "";
const ARTIFACTS_CDN_PRIVATE = cdnPrivateFromEnv(process.env.ARTIFACTS_CDN_PRIVATE);
const THREAD_MAP_TABLE = process.env.THREAD_MAP_TABLE as string;
// Artifacts are read by KEY from this bucket, never over the (possibly private) artifacts CDN.
const SKILLS_BUCKET = process.env.SKILLS_BUCKET || "";
const DEDUP_TTL_SECONDS = 7 * 24 * 3600;
const artifactSource: ArtifactSource = {
  s3: SKILLS_BUCKET ? new S3Client({ region: REGION }) : undefined,
  bucket: SKILLS_BUCKET,
  artifactsDomain: ARTIFACTS_DOMAIN,
};

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

const nowSec = () => Math.floor(Date.now() / 1000);
const attrS = (img: Record<string, any> | undefined, k: string): string | undefined => img?.[k]?.S;

export async function handler(event: DynamoDBStreamEvent): Promise<void> {
  for (const rec of event.Records) {
    try {
      await handleRecord(rec);
    } catch (err) {
      // Isolate per-record failures so one bad record doesn't wedge the shard.
      console.error("[slack-notifier] record error:", (err as Error).message);
    }
  }
}

async function handleRecord(rec: DynamoDBRecord): Promise<void> {
  if (rec.eventName !== "INSERT" && rec.eventName !== "MODIFY") return;
  const img = rec.dynamodb?.NewImage as Record<string, any> | undefined;
  if (!img) return;

  const sessionId = attrS(img, "sessionId");
  const status = attrS(img, "status");
  const jobId = attrS(img, "jobId") ?? (rec.dynamodb?.Keys as any)?.jobId?.S;
  const nodeId = attrS(img, "nodeId");

  // Only Slack-origin, terminal, and only the parent/standalone record
  // (nodeId set = an individual plan node → the parent record is the summary).
  if (!isSlackSession(sessionId) || !isTerminalStatus(status) || !jobId) return;
  if (nodeId) return;

  // Dedupe: claim notified#<jobId> once (conditional put). Repeat MODIFY events
  // or retries no-op.
  const claim = await dynamoPutItem(
    REGION,
    THREAD_MAP_TABLE,
    { id: { S: `notified#${jobId}` }, expiresAt: { N: String(nowSec() + DEDUP_TTL_SECONDS) } },
    "attribute_not_exists(id)",
  );
  if (claim.conditionalFailed) return;

  // Resolve the originating thread.
  const mapItem = await dynamoGetItem(REGION, THREAD_MAP_TABLE, { id: { S: `session#${sessionId}` } });
  const channel = attrS(mapItem ?? undefined, "channelId");
  const threadTs = attrS(mapItem ?? undefined, "rootThreadTs");
  if (!channel || !threadTs) return; // can't route (no mapping)

  const secret = await getSecret();

  if (!isSuccessStatus(status)) {
    const err = attrS(img, "lastError");
    const tail = err ? "\n```" + err.slice(0, 300) + "```" : "";
    await slackCall(secret.botToken, "chat.postMessage", {
      channel,
      thread_ts: threadTs,
      text: `:warning: A job you started did not complete (\`${status}\`).${tail}`,
      unfurl_links: false,
    });
    return;
  }

  // Success: have the Main Agent fetch + present the result in the SAME session.
  const raw = await invokeAgentRuntime(REGION, process.env.MAIN_AGENT_RUNTIME_ARN as string, sessionId!, {
    prompt:
      `An async job you started (job_id ${jobId}) just completed successfully. ` +
      `Fetch its result and present it concisely with any artifact links. Do not start new work.`,
    session_id: sessionId,
    __actor_id: process.env.SLACK_DEMO_ACTOR_ID,
    __actor_groups: [],
  });
  const { text } = parseHarnessStream(raw);
  await slackCall(secret.botToken, "chat.postMessage", {
    channel,
    thread_ts: threadTs,
    // Native uploads below read the ORIGINAL CDN identifiers in `text`; only the posted copy is rewritten.
    text: `:white_check_mark: Your job finished.\n${
      (ARTIFACTS_CDN_PRIVATE ? rewriteArtifactUrlsForPrivateCdn(text, ARTIFACTS_DOMAIN, UI_DOMAIN) : text) ||
      "(result is ready)"
    }`,
    unfurl_links: false,
  });

  if (ARTIFACTS_DOMAIN) {
    for (const url of extractArtifactUrls(text, ARTIFACTS_DOMAIN)) {
      try {
        const head = await headArtifact(url, artifactSource);
        if (shouldUploadNatively(head.sizeBytes, MAX_UPLOAD)) {
          await uploadFromUrl(secret.botToken, { channel, threadTs, url, filename: head.filename }, artifactSource);
        }
      } catch (err) {
        console.warn("[slack-notifier] upload skipped:", url, (err as Error).message);
      }
    }
  }
}
