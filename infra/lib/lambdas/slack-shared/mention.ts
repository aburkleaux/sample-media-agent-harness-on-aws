// Slack envelope normalization.
// Reqs 1.4, 1.6, 8.1, 9.4–9.5.

export interface NormalizedFileRef {
  id: string;
  name: string;
  mimetype: string;
  size: number;
}

export interface NormalizedMention {
  eventId: string;
  teamId: string;
  channelId: string;
  userId: string;
  text: string;
  messageTs: string;
  rootThreadTs: string;
  files: NormalizedFileRef[];
}

export type NormalizeResult =
  | { kind: "challenge"; challenge: string }
  | { kind: "mention"; value: NormalizedMention }
  | { kind: "ignore"; reason: string };

const LEADING_MENTION = /^\s*<@[A-Z0-9]+>\s*/;

export function stripLeadingMention(text: string): string {
  return text.replace(LEADING_MENTION, "");
}

interface SlackEnvelope {
  type?: string;
  challenge?: unknown;
  team_id?: string;
  event_id?: string;
  event?: {
    type?: string;
    subtype?: string;
    channel_type?: string;
    bot_id?: string;
    user?: string;
    team?: string;
    channel?: string;
    ts?: string;
    thread_ts?: string;
    text?: string;
    files?: Array<{ id?: string; name?: string; mimetype?: string; size?: number }>;
  };
}

export function normalizeSlackEnvelope(
  envelope: SlackEnvelope,
  botUserId?: string,
): NormalizeResult {
  if (envelope?.type === "url_verification") {
    return { kind: "challenge", challenge: String(envelope.challenge ?? "") };
  }
  if (envelope?.type !== "event_callback" || !envelope.event) {
    return { kind: "ignore", reason: "not_event_callback" };
  }
  const e = envelope.event;
  // Accept human @mentions in channels AND direct messages to the app
  // (message events with channel_type "im"). The bot_id/subtype/self filters
  // below prevent the bot's own DM replies from re-triggering a turn.
  const isAppMention = e.type === "app_mention";
  const isDirectMessage = e.type === "message" && e.channel_type === "im";
  if (!isAppMention && !isDirectMessage) {
    return { kind: "ignore", reason: `unsupported_event:${e.type ?? "none"}` };
  }
  if (e.bot_id || e.subtype) return { kind: "ignore", reason: "bot_or_subtype" };
  if (botUserId && e.user === botUserId) return { kind: "ignore", reason: "self" };

  const teamId = envelope.team_id ?? e.team ?? "";
  const channelId = e.channel ?? "";
  const userId = e.user ?? "";
  const messageTs = e.ts ?? "";
  if (!teamId || !channelId || !userId || !messageTs) {
    return { kind: "ignore", reason: "missing_fields" };
  }

  const files: NormalizedFileRef[] = Array.isArray(e.files)
    ? e.files
        .filter((f) => f && f.id && f.name)
        .map((f) => ({
          id: String(f.id),
          name: String(f.name),
          mimetype: String(f.mimetype ?? "application/octet-stream"),
          size: Number(f.size ?? 0),
        }))
    : [];

  return {
    kind: "mention",
    value: {
      eventId: envelope.event_id ?? `${teamId}:${channelId}:${messageTs}`,
      teamId,
      channelId,
      userId,
      text: stripLeadingMention(e.text ?? ""),
      messageTs,
      rootThreadTs: e.thread_ts || e.ts || "",
      files,
    },
  };
}
