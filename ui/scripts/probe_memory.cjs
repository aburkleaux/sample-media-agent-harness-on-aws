/**
 * Probe each memory API the proxy uses, isolate which one returns
 * <UnknownOperationException>.
 */
const {
  BedrockAgentCoreClient,
  ListSessionsCommand,
  ListEventsCommand,
  CreateEventCommand,
} = require("@aws-sdk/client-bedrock-agentcore");

const REGION = "us-west-2";
const MEMORY_ID = "mediaworks_harness_conversation_memory-5Ii3pFECvY";
const ACTOR_ID = "local-dev-user";

async function main() {
  const c = new BedrockAgentCoreClient({ region: REGION });

  // 1. ListSessions
  try {
    const r = await c.send(new ListSessionsCommand({
      memoryId: MEMORY_ID,
      actorId: ACTOR_ID,
      maxResults: 5,
    }));
    console.log("ListSessions OK, sessions:", r.sessionSummaries?.length ?? 0);
    var firstSession = r.sessionSummaries?.[0]?.sessionId;
  } catch (e) {
    console.log("ListSessions ERR:", e.name, e.message?.slice(0, 200));
  }

  // 2. ListEvents (needs a sessionId)
  if (firstSession) {
    try {
      const r = await c.send(new ListEventsCommand({
        memoryId: MEMORY_ID,
        actorId: ACTOR_ID,
        sessionId: firstSession,
        includePayloads: true,
        maxResults: 5,
      }));
      console.log("ListEvents OK, events:", r.events?.length ?? 0);
    } catch (e) {
      console.log("ListEvents ERR:", e.name, e.message?.slice(0, 200), "status:", e.$response?.statusCode);
    }
  }

  // 3. CreateEvent
  try {
    const r = await c.send(new CreateEventCommand({
      memoryId: MEMORY_ID,
      actorId: ACTOR_ID,
      sessionId: "probe-session-" + Date.now() + "-padding-padding-padding",
      eventTimestamp: new Date(),
      payload: [
        {
          conversational: {
            content: { text: "probe message" },
            role: "USER",
          },
        },
      ],
    }));
    console.log("CreateEvent OK, eventId:", r.event?.eventId);
  } catch (e) {
    console.log("CreateEvent ERR:", e.name, e.message?.slice(0, 200), "status:", e.$response?.statusCode);
  }
}

main();
