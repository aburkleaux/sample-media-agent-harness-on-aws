/**
 * The catalog must be readable without waking the agent runtime — and must fall
 * back rather than serve a partial list.
 *
 * `/catalog` was a full main-agent invocation: 8.6s on a fresh session against
 * 0.2s warm, because no session id was sent so every request cold-started. These
 * tests pin the two things that make the replacement safe: only AgentCore
 * gateways can be served from the control plane, and any missing piece returns
 * null so the caller invokes the runtime instead of under-reporting.
 */
import { describe, it, expect } from "vitest";
import {
  gatewayIdFromUrl,
  enabledGatewayDescriptors,
  builtinToolsFromRegistry,
  catalogWithoutRuntime,
  CATALOG_PK,
  CATALOG_BUILTIN_SK,
  CONTROL_PLANE_SIGNING_SERVICE,
  REGISTERED_PK,
} from "./toolCatalog";

describe("control-plane signing name", () => {
  it("signs for bedrock-agentcore, NOT the hostname prefix", () => {
    /*
     * The endpoint is bedrock-agentcore-control.<region>.amazonaws.com but the
     * SigV4 service is bedrock-agentcore. Signing with the host prefix returns
     *
     *   403 Credential should be scoped to correct service: 'bedrock-agentcore'.
     *
     * which `controlPlaneGet` turned into a silent null — so every gateway looked
     * unreadable, `catalogWithoutRuntime` gave up, and `/catalog` cold-started the
     * agent runtime on every miss. The fast path never worked for gateways at all.
     * Verified both ways against the live endpoint before fixing.
     */
    expect(CONTROL_PLANE_SIGNING_SERVICE).toBe("bedrock-agentcore");
  });
});

/** Minimal DynamoDBDocumentClient stand-in. */
function fakeDdb(handler: (cmd: any) => any) {
  return { send: async (cmd: any) => handler(cmd) } as any;
}

const GATEWAY_URL =
  "https://media-workflow-gateway-staging-examplegw6.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp";

// OMS was federated this way — an AgentCore *Runtime* invocations URL, not a gateway.
const RUNTIME_URL =
  "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A100000000006%3Aruntime%2Foms_core_mcp_endpoint-mROLsm6NgW/invocations?qualifier=DEFAULT";

describe("gatewayIdFromUrl", () => {
  it("extracts the identifier from a gateway MCP url", () => {
    expect(gatewayIdFromUrl(GATEWAY_URL)).toBe("media-workflow-gateway-staging-examplegw6");
  });

  it("rejects a directly federated runtime endpoint", () => {
    // The control plane knows nothing about it, so this MUST not be treated as
    // a gateway or its tools would silently vanish from the catalog.
    expect(gatewayIdFromUrl(RUNTIME_URL)).toBeNull();
  });

  it("rejects anything that is not a gateway host", () => {
    expect(gatewayIdFromUrl("https://example.com/mcp")).toBeNull();
    expect(gatewayIdFromUrl("not a url")).toBeNull();
    expect(gatewayIdFromUrl(undefined)).toBeNull();
    expect(gatewayIdFromUrl("")).toBeNull();
  });
});

describe("enabledGatewayDescriptors", () => {
  it("queries the registered partition and drops disabled gateways", async () => {
    let seenPk: string | undefined;
    const ddb = fakeDdb((cmd) => {
      seenPk = cmd.input.ExpressionAttributeValues[":pk"];
      return {
        Items: [
          { id: "media-workflow-gateway", name: "M2C", url: GATEWAY_URL, enabled: true },
          { id: "oms-core", name: "OMS Core", url: RUNTIME_URL, enabled: false },
        ],
      };
    });
    const out = await enabledGatewayDescriptors(ddb, "t");
    expect(seenPk).toBe(REGISTERED_PK);
    expect(out?.map((d) => d.id)).toEqual(["media-workflow-gateway"]);
  });

  it("treats a missing enabled flag as disabled", async () => {
    const ddb = fakeDdb(() => ({ Items: [{ id: "x", name: "X", url: GATEWAY_URL }] }));
    expect(await enabledGatewayDescriptors(ddb, "t")).toEqual([]);
  });

  it("returns null when the table cannot be read", async () => {
    const ddb = fakeDdb(() => { throw new Error("AccessDenied"); });
    expect(await enabledGatewayDescriptors(ddb, "t")).toBeNull();
  });
});

describe("builtinToolsFromRegistry", () => {
  it("reads the projection the runtime publishes", async () => {
    let key: any;
    const ddb = fakeDdb((cmd) => {
      key = cmd.input.Key;
      return { Item: { tools: [{ name: "artifact", description: "d", state: "trusted" }] } };
    });
    const out = await builtinToolsFromRegistry(ddb, "t");
    expect(key).toEqual({ PK: CATALOG_PK, SK: CATALOG_BUILTIN_SK });
    expect(out).toEqual([{ name: "artifact", description: "d" }]);
  });

  it("returns null when nothing has been published yet", async () => {
    // Fresh deploy: the caller must fall back to the runtime, not report zero
    // builtin tools.
    expect(await builtinToolsFromRegistry(fakeDdb(() => ({})), "t")).toBeNull();
  });

  it("returns null when the item is malformed", async () => {
    expect(await builtinToolsFromRegistry(fakeDdb(() => ({ Item: { tools: "nope" } })), "t")).toBeNull();
  });
});

describe("catalogWithoutRuntime falls back rather than under-reporting", () => {
  it("returns null without a registry table configured", async () => {
    expect(await catalogWithoutRuntime("us-west-2", null, "t")).toBeNull();
    expect(await catalogWithoutRuntime("us-west-2", fakeDdb(() => ({})), undefined)).toBeNull();
  });

  it("returns null when builtins have not been published", async () => {
    const ddb = fakeDdb((cmd) =>
      cmd.input.KeyConditionExpression
        ? { Items: [{ id: "g", name: "G", url: GATEWAY_URL, enabled: true }] }
        : {} // no builtin item
    );
    expect(await catalogWithoutRuntime("us-west-2", ddb, "t")).toBeNull();
  });

  it("reports a non-AgentCore gateway as unreadable and still serves the rest", async () => {
    // Only the data plane can enumerate a directly federated MCP endpoint. That
    // used to return null and cold-start the runtime, discarding the builtins
    // already in hand; now the gateway is NAMED as outstanding and the caller
    // completes it in the background.
    const ddb = fakeDdb((cmd) =>
      cmd.input.KeyConditionExpression
        ? { Items: [{ id: "oms-core", name: "OMS", url: RUNTIME_URL, enabled: true }] }
        : { Item: { tools: [{ name: "artifact", description: "" }] } }
    );

    const out = await catalogWithoutRuntime("us-west-2", ddb, "t");

    expect(out).not.toBeNull();
    expect(out!.unreadable).toEqual(["OMS"]);
    // The builtins survive — that is the whole point of degrading per gateway.
    expect(out!.sections.map((s) => s.label)).toEqual(["Native Tools"]);
  });

  it("serves builtins alone when no gateway is enabled", async () => {
    const ddb = fakeDdb((cmd) =>
      cmd.input.KeyConditionExpression
        ? { Items: [] }
        : { Item: { tools: [{ name: "artifact", description: "d" }] } }
    );
    const out = await catalogWithoutRuntime("us-west-2", ddb, "t");
    expect(out).toEqual({
      sections: [{ label: "Native Tools", tools: [{ name: "artifact", description: "d" }] }],
      // Nothing outstanding: an empty list is a positive statement of completeness,
      // which is what lets the caller skip the runtime entirely.
      unreadable: [],
    });
  });

  it("keeps the gateways it CAN read when another one is unreachable", async () => {
    /*
     * The sandbox case this change exists for. `video-graph-tools` is registered
     * and enabled here but hosted in another account (100000000002), so this
     * account's control plane answers ListGatewayTargets with
     * ResourceNotFoundException — while its tools still reach agents fine over the
     * gateway's own OAuth connection.
     *
     * Before: one such gateway made the whole assembly null, so every catalog read
     * threw away the builtins AND the readable gateway to cold-start the runtime.
     */
    const readable =
      "https://media-workflow-gateway-sandbox-dev-examplegw7.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp";
    const foreign =
      "https://staging-graph-tools-jwt-examplegw4.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp";
    const ddb = fakeDdb((cmd) =>
      cmd.input.KeyConditionExpression
        ? {
            Items: [
              { id: "media-workflow-gateway", name: "M2C", url: readable, enabled: true },
              { id: "video-graph-tools", name: "Video Graph", url: foreign, enabled: true },
            ],
          }
        : { Item: { tools: [{ name: "artifact", description: "d" }] } }
    );
    // Stands in for the control plane: the foreign gateway is invisible from here.
    const deps = {
      gatewayTools: async (_region: string, gatewayId: string) =>
        gatewayId === "media-workflow-gateway-sandbox-dev-examplegw7"
          ? [{ name: "media-processing___probe", description: "p" }]
          : null,
    };

    const out = await catalogWithoutRuntime("us-west-2", ddb, "t", "", deps);

    expect(out).not.toBeNull();
    expect(out!.sections.map((s) => s.label)).toEqual(["Native Tools", "Gateway: M2C"]);
    expect(out!.unreadable).toEqual(["Video Graph"]);
    // The readable gateway's tools are actually present, not just its label.
    expect(out!.sections[1].tools.map((t) => t.name)).toEqual(["media-processing___probe"]);
  });
});

describe("proxy wiring", () => {
  it("derives the runtime-fallback session id per actor", async () => {
    // Previously a single pinned constant, to keep the fallback warm (10.3s
    // cold vs 0.2s warm). That is unsafe once the gateway interceptor scopes
    // tools per actor: AgentCore gives each session its own microVM, so one
    // shared session id means one container — and one actor-bound gateway
    // connection — serving every user. Deriving from the actor keeps the
    // warm-reuse win per user without sharing a container across users.
    const fs = await import("node:fs/promises");
    const src = await fs.readFile(new URL("./app.ts", import.meta.url), "utf8");
    expect(src).toContain("function catalogSessionIdFor");
    // AgentCore requires >= 33 chars, so the derivation must pad.
    expect(src).toMatch(/padEnd\(\s*(3[3-9]|[4-9]\d)\s*,/);
    const call = src.slice(src.indexOf("const fetchCatalog"));
    expect(call).toContain("x-amzn-bedrock-agentcore-runtime-session-id");
    expect(call).toContain("catalogSessionIdFor(actorId)");
  });

  it("keys the catalog cache per actor so one user's tools are not served to another", () => {
    // A shared cache would defeat the per-actor session id above by handing
    // the previous caller's sections to the next one.
    return import("node:fs/promises").then(async (fs) => {
      const src = await fs.readFile(new URL("./app.ts", import.meta.url), "utf8");
      expect(src).toContain("catalogCacheByActor");
      expect(src).toContain("catalogInFlightByActor");
      expect(src).not.toMatch(/let catalogCache\s*:/);
    });
  });

  it("tries the runtime-free path before invoking the runtime", async () => {
    const fs = await import("node:fs/promises");
    const src = await fs.readFile(new URL("./app.ts", import.meta.url), "utf8");
    const body = src.slice(src.indexOf("const fetchCatalog"));
    expect(body.indexOf("catalogWithoutRuntime")).toBeLessThan(body.indexOf("signedFetch(invokeUrl"));
  });
});
