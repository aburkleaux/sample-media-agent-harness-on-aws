/**
 * Proxy-side MCP server listing — the security-critical properties.
 *
 * The one that keeps the item honest: an UNRESOLVABLE ACTOR must yield
 * empty-plus-error and NEVER a list. The gateway's `usr-` targets belong to
 * other people, so a fail-open here leaks every user's registered servers. The
 * first test proves the guard fires *before* any DynamoDB or control-plane read
 * (both spies must stay untouched).
 */
import { describe, it, expect, vi } from "vitest";
import {
  mcpServersForActor,
  ownershipRowsForActor,
  normalizeTargetStatus,
  type McpServerDeps,
} from "./mcpServers";

const REGION = "us-west-2";
const TABLE = "gateway-registry";
const GATEWAY_URL = "https://azxdlhjrqx.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp";

// A DynamoDB doc client stub that answers Query by the requested partition key.
function fakeDdb(rows: {
  gateways?: any[];
  owners?: any[];
}) {
  const send = vi.fn(async (cmd: any) => {
    const pk = cmd?.input?.ExpressionAttributeValues?.[":pk"];
    if (pk === "GATEWAY#REGISTERED") return { Items: rows.gateways ?? [] };
    if (pk === "TARGET#OWNER") return { Items: rows.owners ?? [] };
    return { Items: [] };
  });
  return { client: { send } as any, send };
}

describe("mcpServersForActor — fail closed", () => {
  it("returns empty+error for an unresolvable actor and touches nothing", async () => {
    const { client, send } = fakeDdb({});
    const listTargets = vi.fn(async () => [{ name: "usr-someone", targetId: "t", status: "READY" }]);

    const result = await mcpServersForActor(REGION, client, TABLE, "", { listTargets });

    expect(result).toEqual({ servers: [], unreadable: [], error: "actor_unresolved" });
    // The proof it is fail-CLOSED, not merely filtered: no read happened at all.
    expect(send).not.toHaveBeenCalled();
    expect(listTargets).not.toHaveBeenCalled();
  });

  it("returns empty+error when the registry is not configured", async () => {
    const listTargets = vi.fn();
    expect(await mcpServersForActor(REGION, null, TABLE, "alice", { listTargets }))
      .toEqual({ servers: [], unreadable: [], error: "registry_unavailable" });
    expect(await mcpServersForActor(REGION, {} as any, undefined, "alice", { listTargets }))
      .toEqual({ servers: [], unreadable: [], error: "registry_unavailable" });
    expect(listTargets).not.toHaveBeenCalled();
  });
});

describe("mcpServersForActor — actor-scoped visibility", () => {
  const gateways = [{ id: "azxdlhjrqx", name: "Test Gateway", url: GATEWAY_URL, enabled: true }];
  // The harness's own gateway. Platform rows come from THIS one only; see the
  // federated-gateway test below for why.
  const HARNESS = "azxdlhjrqx";
  const owners = [
    { PK: "TARGET#OWNER", SK: "usr-alice1", ownerActorId: "alice", serverName: "Alice Notes", endpoint: "https://a/mcp", targetId: "t-a", status: "ready" },
    { PK: "TARGET#OWNER", SK: "usr-bob2", ownerActorId: "bob", serverName: "Bob Secrets", endpoint: "https://b/mcp", targetId: "t-b", status: "ready" },
  ];
  const liveTargets: McpServerDeps["listTargets"] = async () => [
    { name: "mediaworks-harness-aws-mcp", targetId: "t-aws", status: "READY" }, // platform
    { name: "usr-alice1", targetId: "t-a", status: "READY" },                    // alice's
    { name: "usr-bob2", targetId: "t-b", status: "READY" },                      // bob's
  ];

  it("shows platform + own targets, hides another actor's usr- target", async () => {
    const { client } = fakeDdb({ gateways, owners });
    const { servers, error } = await mcpServersForActor(REGION, client, TABLE, "alice", { listTargets: liveTargets }, HARNESS);

    expect(error).toBeUndefined();
    const names = servers.map(s => s.target_name).sort();
    expect(names).toEqual(["mediaworks-harness-aws-mcp", "usr-alice1"]);
    // Nobody else's server, ever.
    expect(names).not.toContain("usr-bob2");

    // Platform target is enriched with the gateway label, not a bare/blank line.
    const aws = servers.find(s => s.target_name === "mediaworks-harness-aws-mcp")!;
    expect(aws.endpoint).toBe("Test Gateway");
    expect(aws.status).toBe("ready");

    // Own target keeps the user-supplied name + endpoint from the ownership row.
    const mine = servers.find(s => s.target_name === "usr-alice1")!;
    expect(mine.server_name).toBe("Alice Notes");
    expect(mine.endpoint).toBe("https://a/mcp");
  });

  it("hides ALL usr- targets when the caller owns none, keeping platform ones", async () => {
    const { client } = fakeDdb({ gateways, owners });
    const { servers } = await mcpServersForActor(REGION, client, TABLE, "carol", { listTargets: liveTargets }, HARNESS);
    expect(servers.map(s => s.target_name)).toEqual(["mediaworks-harness-aws-mcp"]);
  });

  it("still shows an owned target the control plane omitted (mid-registration)", async () => {
    const { client } = fakeDdb({ gateways, owners });
    const onlyPlatform: McpServerDeps["listTargets"] = async () => [
      { name: "mediaworks-harness-aws-mcp", targetId: "t-aws", status: "READY" },
    ];
    const { servers } = await mcpServersForActor(REGION, client, TABLE, "alice", { listTargets: onlyPlatform }, HARNESS);
    expect(servers.map(s => s.target_name).sort()).toEqual(["mediaworks-harness-aws-mcp", "usr-alice1"]);
  });

  it("degrades to the caller's own rows (never empty-erasing them) when the gateway is unreadable", async () => {
    const { client } = fakeDdb({ gateways, owners });
    const unreadable: McpServerDeps["listTargets"] = async () => null;
    const { servers, error } = await mcpServersForActor(REGION, client, TABLE, "alice", { listTargets: unreadable }, HARNESS);
    // Only alice's own row survives — still no other actor's data.
    expect(servers.map(s => s.target_name)).toEqual(["usr-alice1"]);
    expect(error).toBe("gateway_unavailable");
  });
});

describe("mcpServersForActor — the fields the panel keys on", () => {
  /*
   * The panel GROUPS on `platform === true` and shows Remove on
   * `removable !== false`. This module knew platform-ness internally but emitted
   * neither, so pointing the UI at it would have put the shared aws-mcp row in the
   * caller's own group WITH a Remove button — removing it takes it from everyone.
   *
   * Derived here rather than in the SPA on purpose: the `usr-` convention already
   * lives in four places and a mismatch makes a user target look like platform
   * surface, which fails OPEN.
   */
  const HARNESS = "azxdlhjrqx";
  const gateways = [{ id: "azxdlhjrqx", name: "Test Gateway", url: GATEWAY_URL, enabled: true }];
  const owners = [
    { PK: "TARGET#OWNER", SK: "usr-alice1", ownerActorId: "alice", serverName: "Alice Notes", endpoint: "https://a/mcp", targetId: "t-a", status: "ready" },
  ];
  const liveTargets: McpServerDeps["listTargets"] = async () => [
    { name: "mediaworks-harness-aws-mcp", targetId: "t-aws", status: "READY" },
    { name: "usr-alice1", targetId: "t-a", status: "READY" },
  ];

  it("marks the platform row unremovable and the caller's own row removable", async () => {
    const { client } = fakeDdb({ gateways, owners });
    const { servers } = await mcpServersForActor(
      REGION, client, TABLE, "alice", { listTargets: liveTargets }, HARNESS,
    );

    const aws = servers.find(s => s.target_name === "mediaworks-harness-aws-mcp")!;
    expect(aws.platform).toBe(true);
    expect(aws.removable).toBe(false);
    expect(aws.gateway_id).toBe(HARNESS);
    expect(aws.gateway_name).toBe("Test Gateway");

    const mine = servers.find(s => s.target_name === "usr-alice1")!;
    expect(mine.platform).toBe(false);
    expect(mine.removable).toBe(true);
  });

  it("marks ownership-only rows removable and not platform", async () => {
    // The instant-paint phase. Every row here is one the caller registered.
    const { client } = fakeDdb({ owners });
    const rows = await ownershipRowsForActor(client, TABLE, "alice");
    expect(rows[0].platform).toBe(false);
    expect(rows[0].removable).toBe(true);
  });

  it("does NOT list a federated gateway's targets as the caller's MCP servers", async () => {
    /*
     * The behaviour difference that would otherwise have shipped silently. The
     * agent's `list_platform_mcp_servers` scopes to HARNESS_GATEWAY_ID; this
     * enumerates every enabled gateway. Unscoped, M2C's five workflow targets
     * would appear as if the user had five MCP servers.
     */
    const twoGateways = [
      ...gateways,
      {
        id: "m2c", name: "M2C Media Workflow Gateway", enabled: true,
        url: "https://m2c-abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
      },
    ];
    const perGateway: McpServerDeps["listTargets"] = async (_r, gatewayId) =>
      gatewayId === HARNESS
        ? [{ name: "mediaworks-harness-aws-mcp", targetId: "t-aws", status: "READY" }]
        : [{ name: "media-processing", targetId: "t-m2c", status: "READY" }];

    const { client } = fakeDdb({ gateways: twoGateways, owners });
    const { servers } = await mcpServersForActor(
      REGION, client, TABLE, "alice", { listTargets: perGateway }, HARNESS,
    );

    expect(servers.map(s => s.target_name).sort()).toEqual([
      "mediaworks-harness-aws-mcp", "usr-alice1",
    ]);
    expect(servers.map(s => s.target_name)).not.toContain("media-processing");
  });

  it("reports the platform gateway as unreadable when none is configured", async () => {
    // Absent ≠ empty. With no HARNESS_GATEWAY_ID the platform rows were never
    // looked for, and silently losing aws-mcp reads as "the harness ships none".
    const { client } = fakeDdb({ gateways, owners });
    const { servers, unreadable } = await mcpServersForActor(
      REGION, client, TABLE, "alice", { listTargets: liveTargets }, "",
    );

    expect(servers.map(s => s.target_name)).toEqual(["usr-alice1"]);
    expect(unreadable).toContain("Platform gateway (not configured)");
  });

  it("does not warn when a FEDERATED gateway is unreadable", async () => {
    /*
     * The false positive this scoping removes. Sandbox has a cross-account gateway
     * (`video-graph-tools`, hosted in 100000000002) that this account's control
     * plane cannot describe. While every enabled gateway was enumerated, the panel
     * said "Couldn't read one gateway, so this list may be incomplete" and "Live
     * server status is temporarily unavailable" on EVERY open — both untrue, since
     * a federated gateway hosts none of this panel's rows. A gateway that cannot
     * contribute a row cannot leave a gap.
     */
    const twoGateways = [
      ...gateways,
      {
        id: "video-graph", name: "Video Knowledge Graph Tools", enabled: true,
        url: "https://staging-graph-tools-jwt-examplegw4.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
      },
    ];
    const onlyHarnessReadable: McpServerDeps["listTargets"] = async (_r, gatewayId) =>
      gatewayId === HARNESS
        ? [{ name: "mediaworks-harness-aws-mcp", targetId: "t-aws", status: "READY" }]
        : null;

    const { client } = fakeDdb({ gateways: twoGateways, owners });
    const { servers, unreadable, error } = await mcpServersForActor(
      REGION, client, TABLE, "alice", { listTargets: onlyHarnessReadable }, HARNESS,
    );

    expect(servers.map(s => s.target_name).sort()).toEqual([
      "mediaworks-harness-aws-mcp", "usr-alice1",
    ]);
    expect(unreadable).toEqual([]);
    expect(error).toBeUndefined();
  });

  it("names an unreadable gateway rather than dropping it", async () => {
    const { client } = fakeDdb({ gateways, owners });
    const { unreadable, error } = await mcpServersForActor(
      REGION, client, TABLE, "alice", { listTargets: async () => null }, HARNESS,
    );
    expect(unreadable).toEqual(["Test Gateway"]);
    expect(error).toBe("gateway_unavailable");
  });
});

describe("ownershipRowsForActor", () => {
  const owners = [
    { PK: "TARGET#OWNER", SK: "usr-alice1", ownerActorId: "alice", serverName: "Alice Notes", endpoint: "https://a/mcp", targetId: "t-a", status: "synchronizing" },
    { PK: "TARGET#OWNER", SK: "usr-bob2", ownerActorId: "bob", serverName: "Bob Secrets", endpoint: "https://b/mcp", targetId: "t-b", status: "ready" },
  ];

  it("returns only the actor's rows", async () => {
    const { client } = fakeDdb({ owners });
    const rows = await ownershipRowsForActor(client, TABLE, "alice");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ target_name: "usr-alice1", server_name: "Alice Notes", status: "synchronizing" });
  });

  it("returns [] for an empty actor without querying", async () => {
    const { client, send } = fakeDdb({ owners });
    expect(await ownershipRowsForActor(client, TABLE, "")).toEqual([]);
    expect(send).not.toHaveBeenCalled();
  });
});

describe("normalizeTargetStatus", () => {
  it("maps READY/FAILED and treats everything else (incl. deferred) as settling", () => {
    expect(normalizeTargetStatus("READY")).toBe("ready");
    expect(normalizeTargetStatus("ready")).toBe("ready");
    expect(normalizeTargetStatus("FAILED")).toBe("failed");
    expect(normalizeTargetStatus("SYNCHRONIZING")).toBe("synchronizing");
    expect(normalizeTargetStatus("CREATING")).toBe("synchronizing");
    // The gateway's connect_all `deferred` status must not render as error/blank.
    expect(normalizeTargetStatus("deferred")).toBe("synchronizing");
    expect(normalizeTargetStatus(undefined)).toBe("synchronizing");
    expect(normalizeTargetStatus("")).toBe("synchronizing");
  });
});
