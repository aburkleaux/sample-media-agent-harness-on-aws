/**
 * MCP server listing for the Tools panel, served proxy-side without waking the
 * agent runtime.
 *
 * The old path round-tripped `user_mcp_list` through `InvokeAgentRuntime`,
 * which cold-starts a container to answer what is fundamentally a control-plane
 * read (gateway targets) plus a single DynamoDB Query (the `TARGET#OWNER`
 * ownership rows). Measured elsewhere in this file's sibling (`toolCatalog.ts`)
 * the runtime cold path is ~8.6s versus ~0.5s for the direct read.
 *
 * Two things this fixes, together (backlog Wave 2):
 *
 *  2.1 — a live, working gateway target that the user does NOT own a
 *        registry row for (e.g. the platform `mediaworks-harness-aws-mcp`
 *        target) was invisible, because the old list rendered ownership rows
 *        only, never the live gateway. Reading the control plane surfaces it.
 *
 *  2.2 — no runtime cold start. And the control-plane read is split off from
 *        the fast ownership read so the panel can paint the user's own servers
 *        instantly (`ownershipRowsForActor`) and fill live state asynchronously
 *        (`mcpServersForActor`).
 *
 * *** FAIL CLOSED. *** The `usr-` targets on the gateway belong to other
 * actors. If the caller's actor cannot be resolved, this returns EMPTY-PLUS-
 * ERROR and never touches DynamoDB or the control plane — an unfiltered list
 * would leak every user's registered MCP servers. This is enforced as the very
 * first statement in both entry points and proven by `mcpServers.test.ts`.
 */
import type { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";
import { QueryCommand } from "@aws-sdk/lib-dynamodb";
import {
  controlPlaneGet,
  enabledGatewayDescriptors,
  gatewayIdFromUrl,
} from "./toolCatalog";

/** Partition holding one row per user-registered target. Written by the agent
 *  (`OWNER_PK` in `agent/src/user_mcp.py`), read by the interceptor and here —
 *  the same fact, so these surfaces cannot disagree about who owns what. */
const OWNER_PK = "TARGET#OWNER";

// Hyphen, not underscore: the service's target-name pattern rejects
// underscores. Must match `_TARGET_PREFIX` in `agent/src/user_mcp.py` and
// `USER_TARGET_PREFIX` in `toolCatalog.ts` / the gateway interceptor — a
// mismatch makes a user target look like platform surface, which fails OPEN.
const USER_TARGET_PREFIX = "usr-";

export type McpStatus = "ready" | "synchronizing" | "failed";

/** One MCP server row, shaped to match the agent's `user_mcp_list` output so the
 *  UI (`McpRegistration`) is a drop-in consumer. */
export interface McpServerRow {
  target_name: string;
  target_id: string;
  server_name: string;
  endpoint: string;
  status: McpStatus;
  /**
   * True for a target that ships with the harness rather than being registered by
   * the caller. The panel GROUPS on this (`platform === true`), so omitting it put
   * the platform server in the user's own group.
   *
   * Derived here from the `usr-` prefix and never in the SPA: that convention
   * already lives in four places, and the failure mode is asymmetric — a mismatch
   * makes a user target look like platform surface, which fails OPEN.
   */
  platform: boolean;
  /**
   * Whether the caller may delete this row. The panel keys its Remove button on
   * `removable !== false`, so a missing value silently offers Remove on a shared
   * target — which would take it away from everyone.
   */
  removable: boolean;
  /** Set on platform rows so the row names the gateway it came from. */
  gateway_id?: string;
  gateway_name?: string;
}

export interface McpServerListResult {
  servers: McpServerRow[];
  /**
   * Gateways whose targets could not be enumerated, by display name.
   *
   * Part of the contract, not diagnostics: without it a short list is
   * indistinguishable from "those gateways have no servers". The panel shows
   * these rather than dropping them.
   */
  unreadable: string[];
  /** Present only on a degraded/fail-closed result. `servers` is then either
   *  empty (fail-closed) or a safe subset (e.g. gateway unreadable), never an
   *  unfiltered list. */
  error?: string;
}

/**
 * Normalise a control-plane target status (or a stored ownership status) to the
 * three states the pill renders.
 *
 * Everything that is neither READY nor FAILED — SYNCHRONIZING, CREATING,
 * UPDATING, DELETING, and notably the gateway's `deferred` connect status —
 * reads as still-settling ("Setting up…") rather than an error or a blank. This
 * is the catch-all the Tools tab needs so an unhandled status string cannot
 * render as broken.
 */
export function normalizeTargetStatus(raw: string | undefined | null): McpStatus {
  const s = (raw ?? "").toUpperCase();
  if (s === "READY") return "ready";
  if (s === "FAILED") return "failed";
  return "synchronizing";
}

function isUserTarget(name: string): boolean {
  return name.startsWith(USER_TARGET_PREFIX);
}

function byServerName(a: McpServerRow, b: McpServerRow): number {
  return a.server_name.toLowerCase().localeCompare(b.server_name.toLowerCase());
}

/**
 * The caller's own registered servers, from the ownership registry alone.
 *
 * A single Query keyed on the shared `TARGET#OWNER` partition, filtered to the
 * actor in code (the partition holds one row per target across all users and
 * stays small; revisit with a GSI on `ownerActorId` if that stops being true).
 * No control-plane call, so this is the instant-paint phase for 2.2.
 *
 * Returns only rows owned by `actorId`. An empty `actorId` yields `[]` — never
 * the whole partition.
 */
export async function ownershipRowsForActor(
  ddb: DynamoDBDocumentClient,
  table: string,
  actorId: string,
): Promise<McpServerRow[]> {
  if (!actorId) return [];
  const out = await ddb.send(
    new QueryCommand({
      TableName: table,
      KeyConditionExpression: "#pk = :pk",
      ExpressionAttributeNames: { "#pk": "PK" },
      ExpressionAttributeValues: { ":pk": OWNER_PK },
    }),
  );
  const rows: McpServerRow[] = [];
  for (const item of out.Items ?? []) {
    if (item.ownerActorId !== actorId) continue;
    const name = typeof item.SK === "string" ? item.SK : "";
    if (!name) continue;
    rows.push({
      target_name: name,
      target_id: typeof item.targetId === "string" ? item.targetId : "",
      server_name: typeof item.serverName === "string" && item.serverName ? item.serverName : name,
      endpoint: typeof item.endpoint === "string" ? item.endpoint : "",
      status: normalizeTargetStatus(typeof item.status === "string" ? item.status : ""),
      // Every row here is one the caller registered, so it is theirs to remove and
      // is never platform surface. Stated rather than inferred from absence.
      platform: false,
      removable: true,
    });
  }
  rows.sort(byServerName);
  return rows;
}

/** Injected so the control-plane read is mockable in unit tests. Returns the
 *  raw `items` array of a gateway's targets, or null when the gateway itself is
 *  unreadable. */
export interface McpServerDeps {
  listTargets: (region: string, gatewayId: string) => Promise<any[] | null>;
}

const defaultDeps: McpServerDeps = {
  listTargets: async (region, gatewayId) => {
    const list = await controlPlaneGet(
      region,
      `/gateways/${encodeURIComponent(gatewayId)}/targets/`,
    );
    if (!list) return null;
    return Array.isArray(list.items) ? list.items : [];
  },
};

/**
 * Every MCP server the caller may see, live from the control plane and merged
 * with the caller's ownership rows.
 *
 * Visibility, matching the gateway interceptor:
 *   - platform targets (not `usr-` prefixed) — visible to everyone, but ONLY from
 *     the harness's own gateway (`harnessGatewayId`);
 *   - `usr-` targets — visible ONLY to their recorded owner. Any `usr-` target
 *     not in the caller's own ownership set is dropped. Fail closed.
 *
 * **Why platform rows are scoped to one gateway.** This panel answers "which MCP
 * servers do I have", and a registration creates a `usr-` target on the harness
 * gateway. The other registered gateways are federations whose targets are managed
 * under Settings → Gateways; listing them here would present M2C's five workflow
 * targets as if the user had five MCP servers. `user_mcp.list_platform_mcp_servers`
 * scopes to `HARNESS_GATEWAY_ID` for exactly this reason, and this must match it or
 * the panel's contents change when the read path does.
 *
 * With no `harnessGatewayId` configured, platform rows are reported as unreadable
 * rather than omitted — absent ≠ empty, and silently losing the platform server is
 * how a config gap looks like a product change.
 *
 * FAIL CLOSED on an unresolved actor: returns empty + error and does not read
 * DynamoDB or the control plane at all.
 */
export async function mcpServersForActor(
  region: string,
  ddb: DynamoDBDocumentClient | null,
  table: string | undefined,
  actorId: string,
  deps: McpServerDeps = defaultDeps,
  harnessGatewayId = "",
): Promise<McpServerListResult> {
  // *** The fail-closed guard. Must stay the first statement. ***
  if (!actorId) return { servers: [], unreadable: [], error: "actor_unresolved" };
  if (!ddb || !table) return { servers: [], unreadable: [], error: "registry_unavailable" };

  const [descriptors, ownedRows] = await Promise.all([
    enabledGatewayDescriptors(ddb, table),
    ownershipRowsForActor(ddb, table, actorId),
  ]);

  // Ownership rows carry the friendly name + endpoint the user typed; key them
  // by target name so live control-plane targets can be enriched, and so a
  // `usr-` target can be authorised (present ⇒ owned by this actor).
  const ownedByName = new Map(ownedRows.map((r) => [r.target_name, r]));

  if (!descriptors) {
    // Can't read the gateway registry — degrade to the caller's own rows rather
    // than an empty page. Still contains no other actor's data.
    return { servers: ownedRows, unreadable: [], error: "gateway_unavailable" };
  }

  const servers: McpServerRow[] = [];
  const seen = new Set<string>();
  const unreadable: string[] = [];
  let anyGatewayReadFailed = false;

  // ONLY the harness gateway is enumerated.
  //
  // It is the only gateway that can contribute a row here: platform targets are
  // scoped to it by definition, and a registration creates its `usr-` target on it
  // (`user_mcp.register` defaults to HARNESS_GATEWAY_ID). Any owned target the
  // control plane omits still appears via the ownership-row fallback below, which
  // reads DynamoDB and so is gateway-agnostic.
  //
  // Iterating every enabled gateway — which this did first — cost a round trip per
  // federated gateway for targets that were then discarded, and worse, reported a
  // federated gateway's failure as "this list may be incomplete". It never was: a
  // gateway that cannot contribute a row cannot leave a gap. Sandbox has exactly
  // that case (`video-graph-tools`, cross-account), so the panel warned about
  // missing MCP servers on every open while its contents were complete.
  const harnessDescriptor = harnessGatewayId
    ? descriptors.find((d) => gatewayIdFromUrl(d.url) === harnessGatewayId)
    : undefined;

  if (!harnessGatewayId) {
    // Never looked for. Absent ≠ empty — silently losing aws-mcp reads as "the
    // harness ships no MCP server".
    unreadable.push("Platform gateway (not configured)");
  } else if (!harnessDescriptor) {
    unreadable.push("Platform gateway (not in the registry)");
  } else {
    const items = await deps.listTargets(region, harnessGatewayId);
    if (items === null) {
      anyGatewayReadFailed = true;
      unreadable.push(harnessDescriptor.name);
    } else {
      for (const t of items) {
        const name = String(t?.name ?? "");
        if (!name || seen.has(name)) continue;
        const isUser = isUserTarget(name);
        // VISIBILITY GATE. A `usr-` target the caller does not own is invisible —
        // this is the confused-deputy / cross-actor leak boundary.
        if (isUser && !ownedByName.has(name)) continue;
        seen.add(name);
        const owned = ownedByName.get(name);
        servers.push({
          target_name: name,
          target_id: String(t?.targetId ?? owned?.target_id ?? ""),
          // Platform targets have no user-supplied name/endpoint; show the target
          // name and label the source gateway so the row isn't a bare name.
          server_name: owned?.server_name ?? name,
          endpoint: owned?.endpoint ?? (isUser ? "" : harnessDescriptor.name),
          status: normalizeTargetStatus(t?.status),
          platform: !isUser,
          removable: isUser,
          ...(isUser
            ? {}
            : { gateway_id: harnessGatewayId, gateway_name: harnessDescriptor.name }),
        });
      }
    }
  }

  // A `usr-` target the caller owns but the control plane did not return
  // (mid-registration, or an unreadable gateway) must still show so the owner
  // can see and remove it.
  for (const row of ownedRows) {
    if (!seen.has(row.target_name)) servers.push(row);
  }

  servers.sort(byServerName);
  // A failed gateway read means live/platform state is incomplete even if the
  // caller's own rows survived — flag it so the panel can show a staleness
  // marker rather than silently under-reporting. `unreadable` names which ones;
  // `error` is the coarse signal the panel already renders.
  return anyGatewayReadFailed
    ? { servers, unreadable, error: "gateway_unavailable" }
    : { servers, unreadable };
}
