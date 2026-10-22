// Slack worker Lambda (async-invoked by the ingress with a NormalizedMention).
// Ingests any Slack file attachments into artifacts/<session>/input/ (streamed
// multipart, ≤ caps), records the session→thread map (Feature 1), builds a
// ChatCanvas-style file preamble so the Main Agent gets s3://+CloudFront refs,
// invokes the Main Agent (IAM/SigV4), posts the reply (+ /render-view links),
// and natively uploads reply artifacts within the size cap (Feature 3).

import type { NormalizedMention } from "../slack-shared/mention";
import type { Context } from "aws-lambda";
import { deriveSlackSessionId } from "../slack-shared/session";
import { parseHarnessStream } from "../slack-shared/streamParse";
import { S3Client } from "@aws-sdk/client-s3";
import { getSecretJson, invokeAgentRuntime, dynamoPutItem } from "../slack-shared/aws";
import { slackCall, headArtifact, uploadFromUrl, type ArtifactSource } from "../slack-shared/slackClient";
import {
  cdnPrivateFromEnv,
  extractArtifactUrls,
  rewriteArtifactUrlsForPrivateCdn,
  shouldUploadNatively,
} from "../slack-shared/notify";
import { ingestSlackFiles, type IngestedFile } from "../slack-shared/fileIngest";

const REGION = process.env.AWS_REGION || "us-west-2";
const MAX_UPLOAD = Number(process.env.MAX_UPLOAD_BYTES || "52428800");
const ARTIFACTS_DOMAIN = process.env.ARTIFACTS_DOMAIN || "";
// artifactsCdnPrivate: our CDN links 403 for a Slack reader, so posted text links the UI's opener instead.
const UI_DOMAIN = process.env.UI_DOMAIN || "";
const ARTIFACTS_CDN_PRIVATE = cdnPrivateFromEnv(process.env.ARTIFACTS_CDN_PRIVATE);
const THREAD_MAP_TABLE = process.env.THREAD_MAP_TABLE || "";
const SKILLS_BUCKET = process.env.SKILLS_BUCKET || "";
const MAX_FILES = Number(process.env.MAX_FILES || "5");
const MAX_FILE_BYTES = Number(process.env.MAX_FILE_BYTES || "1073741824");
const MAX_AGGREGATE_BYTES = Number(process.env.MAX_AGGREGATE_BYTES || "2147483648");
const MAP_TTL_SECONDS = 7 * 24 * 3600;
const INGEST_RESERVE_MS = 120_000; // reserve for the agent turn + delivery
// Reply artifacts are read by KEY from the skills bucket, never over the (possibly private) CDN.
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

async function writeThreadMap(sessionId: string, m: NormalizedMention): Promise<void> {
  if (!THREAD_MAP_TABLE) return;
  try {
    await dynamoPutItem(REGION, THREAD_MAP_TABLE, {
      id: { S: `session#${sessionId}` },
      teamId: { S: m.teamId },
      channelId: { S: m.channelId },
      rootThreadTs: { S: m.rootThreadTs },
      expiresAt: { N: String(nowSec() + MAP_TTL_SECONDS) },
    });
  } catch (err) {
    console.warn("[slack-worker] thread-map write failed:", (err as Error).message);
  }
}

async function uploadArtifacts(botToken: string, channel: string, threadTs: string, text: string): Promise<void> {
  if (!ARTIFACTS_DOMAIN) return;
  for (const url of extractArtifactUrls(text, ARTIFACTS_DOMAIN)) {
    try {
      const head = await headArtifact(url, artifactSource);
      if (shouldUploadNatively(head.sizeBytes, MAX_UPLOAD)) {
        await uploadFromUrl(botToken, { channel, threadTs, url, filename: head.filename }, artifactSource);
      }
    } catch (err) {
      console.warn("[slack-worker] upload skipped:", url, (err as Error).message);
    }
  }
}

function filePreambles(files: IngestedFile[]): string {
  return files
    .map(
      (f) =>
        `[Attached file: ${f.name}]\n- bucket: ${f.bucket}\n- key: ${f.key}\n- s3_uri: ${f.s3Uri}\n- url: ${f.url}\n- type: ${f.mimetype}`,
    )
    .join("\n\n");
}

function rendererLinks(base: string, sessionId: string, manifestKeys: string[]): string {
  if (!base || manifestKeys.length === 0) return "";
  const lines = manifestKeys.map((key, i) => {
    const url =
      `${base.replace(/\/+$/, "")}/render-view` +
      `?sessionId=${encodeURIComponent(sessionId)}&manifestKey=${encodeURIComponent(key)}`;
    return `• <${url}|Open interactive renderer${manifestKeys.length > 1 ? ` (${i + 1})` : ""}>`;
  });
  return "\n\n*Rendered results:*\n" + lines.join("\n");
}

export async function handler(event: NormalizedMention, context: Context): Promise<void> {
  const secret = await getSecret();
  const sessionId = deriveSlackSessionId(
    event.teamId,
    event.channelId,
    event.rootThreadTs,
    process.env.SLACK_SESSION_SALT || "staging",
  );
  await writeThreadMap(sessionId, event);

  let placeholderTs: string | undefined;
  try {
    const p = await slackCall(secret.botToken, "chat.postMessage", {
      channel: event.channelId,
      thread_ts: event.rootThreadTs,
      text: "_Thinking…_",
      unfurl_links: false,
    });
    placeholderTs = p.ts;
  } catch (err) {
    console.warn("[slack-worker] placeholder failed:", (err as Error).message);
  }

  const post = async (text: string): Promise<void> => {
    if (placeholderTs) {
      await slackCall(secret.botToken, "chat.update", { channel: event.channelId, ts: placeholderTs, text });
    } else {
      await slackCall(secret.botToken, "chat.postMessage", {
        channel: event.channelId,
        thread_ts: event.rootThreadTs,
        text,
        unfurl_links: false,
      });
    }
  };

  // ---- File ingest (streamed multipart into artifacts/<session>/input/) ----
  let accepted: IngestedFile[] = [];
  let rejected: Array<{ name: string; reason: string }> = [];
  if (event.files && event.files.length > 0) {
    if (!SKILLS_BUCKET) {
      rejected = event.files.map((f) => ({ name: f.name, reason: "ingest not configured" }));
    } else {
      const deadlineMs = Date.now() + Math.max(0, context.getRemainingTimeInMillis() - INGEST_RESERVE_MS);
      try {
        const res = await ingestSlackFiles({
          botToken: secret.botToken,
          region: REGION,
          bucket: SKILLS_BUCKET,
          artifactsDomain: ARTIFACTS_DOMAIN,
          sessionId,
          files: event.files,
          caps: { maxFiles: MAX_FILES, maxFileBytes: MAX_FILE_BYTES, maxAggregateBytes: MAX_AGGREGATE_BYTES },
          deadlineMs,
        });
        accepted = res.accepted;
        rejected = res.rejected;
      } catch (err) {
        console.warn("[slack-worker] ingest failed:", (err as Error).message);
        rejected = event.files.map((f) => ({ name: f.name, reason: "ingest error" }));
      }
    }
  }
  const cautions = rejected.length
    ? "\n\n_Not ingested:_\n" + rejected.map((r) => `• ${r.name}: ${r.reason}`).join("\n")
    : "";

  // ---- Build the prompt (file preambles first, then user text) ----
  let prompt = (event.text || "").trim();
  const preambles = filePreambles(accepted);
  if (preambles) prompt = preambles + (prompt ? `\n\n${prompt}` : "");

  if (!prompt.trim()) {
    // Blank text AND nothing ingested — don't invoke the agent.
    await post(`I couldn't use any input.${cautions || " Send some text or a supported file."}`);
    return;
  }

  try {
    const raw = await invokeAgentRuntime(REGION, process.env.MAIN_AGENT_RUNTIME_ARN as string, sessionId, {
      prompt,
      session_id: sessionId,
      __actor_id: process.env.SLACK_DEMO_ACTOR_ID,
      __actor_groups: [],
    });
    const { text, manifestKeys } = parseHarnessStream(raw);
    const reply =
      (text || "_(no text response)_") +
      rendererLinks(process.env.RENDERER_BASE_URL || "", sessionId, manifestKeys) +
      cautions;
    // The POSTED text links the UI opener when the CDN is private; native uploads still read the
    // original CDN identifiers (by key from S3), so they are found in the un-rewritten reply.
    await post(ARTIFACTS_CDN_PRIVATE ? rewriteArtifactUrlsForPrivateCdn(reply, ARTIFACTS_DOMAIN, UI_DOMAIN) : reply);
    await uploadArtifacts(secret.botToken, event.channelId, event.rootThreadTs, reply);
  } catch (err) {
    await post(`:warning: The agent turn failed: ${(err as Error).message}${cautions}`);
  }
}
