/**
 * Tool catalog, read without waking the agent runtime.
 *
 * `/catalog` used to be a full main-agent invocation. Measured against the
 * deployed runtime: 8.6s on a fresh session versus 0.2s on a warm one, because
 * the proxy sends no session id and so cold-starts every request. Inside that
 * cold path the runtime also connects every gateway serially — an OAuth2 token
 * exchange plus MCP handshake plus tools/list each — so browsing the tool list
 * paid for work it had no use for.
 *
 * Two sources replace it, neither of which needs the runtime:
 *
 *  - **Gateway tools** come from `bedrock-agentcore-control`. Lambda targets
 *    carry their full tool definitions in `toolSchema.inlinePayload`, so the
 *    control plane is authoritative — there is no projection to keep in sync.
 *    Measured at 1.84s serial for 6 targets; the fetches here run in parallel.
 *  - **Builtin tools** come from the registry item the runtime publishes at
 *    startup (`PK GATEWAY#CATALOG / SK __builtin__`). Builtin specs exist only
 *    inside that process, so this is the one part that must be cached.
 *
 * Both are best-effort. Either returning null means the caller falls back to
 * invoking the runtime, which is exactly the old behaviour.
 */
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";
import { defaultProvider } from "@aws-sdk/credential-provider-node";
import type { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";
import { GetCommand, QueryCommand } from "@aws-sdk/lib-dynamodb";
import https from "https";


export const CATALOG_PK = "GATEWAY#CATALOG";
export const CATALOG_BUILTIN_SK = "__builtin__";
export const REGISTERED_PK = "GATEWAY#REGISTERED";

/** The gateway synthesizes this at MCP level; it is not a target. */
export const SEARCH_TOOL_NAME = "x_amz_bedrock_agentcore_search";

export interface CatalogTool {
  name: string;
  description: string;
  inputSchema?: unknown;
}

export interface CatalogSectionOut {
  label: string;
  tools: CatalogTool[];
}

/**
 * Extract the gateway identifier from a gateway MCP URL.
 *
 * `https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp` -> `<id>`.
 * Returns null for anything else — a directly federated MCP endpoint (an
 * AgentCore *Runtime* invocations URL, say) is not a gateway and has no
 * control-plane representation, so the caller must fall back for it.
 */
export function gatewayIdFromUrl(url: string | undefined): string | null {
  if (!url) return null;
  try {
    const host = new URL(url).hostname;
    const [id, ...rest] = host.split(".");
    if (!id || rest[0] !== "gateway") return null;
    if (!rest.join(".").startsWith("gateway.bedrock-agentcore.")) return null;
    return id;
  } catch {
    return null;
  }
}

/**
 * SigV4 signing name for the control plane — **NOT** the hostname.
 *
 * The endpoint is `bedrock-agentcore-control.<region>.amazonaws.com`, but requests
 * must be signed for `bedrock-agentcore`. Signing with the host prefix instead
 * gets a 403 the service spells out:
 *
 *   Credential should be scoped to correct service: 'bedrock-agentcore'.
 *
 * That is what this constant exists to prevent someone "correcting" back into
 * place. It cost the tab its whole fast path: every gateway read 403'd, so
 * `catalogWithoutRuntime` reported nothing readable and `/catalog` invoked the
 * agent runtime on every single miss — an 8s cold start to fetch data that was
 * already two DynamoDB reads away. Verified both ways against the live endpoint:
 * `bedrock-agentcore` → 200, `bedrock-agentcore-control` → 403.
 */
export const CONTROL_PLANE_SIGNING_SERVICE = "bedrock-agentcore";

let controlPlaneSigner: SignatureV4 | null = null;

function signer(region: string): SignatureV4 {
  if (!controlPlaneSigner) {
    controlPlaneSigner = new SignatureV4({
      service: CONTROL_PLANE_SIGNING_SERVICE,
      region,
      credentials: defaultProvider(),
      sha256: Sha256,
    });
  }
  return controlPlaneSigner;
}

/**
 * Signed GET against the control plane. Returns parsed JSON, or null on any failure.
 *
 * Non-2xx is LOGGED with its status. It used to resolve null in silence, which is
 * how a 403 from the wrong signing name went unnoticed while it quietly forced a
 * runtime cold start on every catalog read — there was nothing to grep for. The
 * log carries the status and path only, never headers or credentials.
 */
export async function controlPlaneGet(region: string, path: string, timeoutMs = 5_000): Promise<any | null> {
  const hostname = `bedrock-agentcore-control.${region}.amazonaws.com`;
  const request = new HttpRequest({
    method: "GET",
    protocol: "https:",
    hostname,
    path,
    headers: { host: hostname, accept: "application/json" },
  });
  let signed;
  try {
    signed = await signer(region).sign(request);
  } catch {
    return null;
  }
  return new Promise((resolve) => {
    const req = https.request(
      `https://${hostname}${path}`,
      { method: "GET", headers: signed.headers as Record<string, string>, timeout: timeoutMs },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          if ((res.statusCode ?? 500) >= 300) {
            console.warn(
              `[toolCatalog] control plane ${res.statusCode} for ${path}`,
            );
            return resolve(null);
          }
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
          } catch {
            resolve(null);
          }
        });
      }
    );
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.end();
  });
}

/**
 * Every tool a gateway exposes, read from the control plane.
 *
 * Target fetches run in parallel — serially this measured 1.84s for six
 * targets, almost all of it round-trip latency.
 *
 * Returns null when the gateway itself cannot be read, so the caller falls back
 * rather than reporting an empty gateway. A single unreadable TARGET is skipped
 * instead, since partial truth beats none for a browse surface.
 */
export async function gatewayToolsFromControlPlane(
  region: string,
  gatewayId: string
): Promise<CatalogTool[] | null> {
  const [gateway, targetList] = await Promise.all([
    controlPlaneGet(region, `/gateways/${encodeURIComponent(gatewayId)}/`),
    controlPlaneGet(region, `/gateways/${encodeURIComponent(gatewayId)}/targets/`),
  ]);
  if (!targetList) return null;

  const items: any[] = Array.isArray(targetList.items) ? targetList.items : [];
  const details = await Promise.all(
    items.map((t) =>
      controlPlaneGet(
        region,
        `/gateways/${encodeURIComponent(gatewayId)}/targets/${encodeURIComponent(t?.targetId ?? "")}/`
      )
    )
  );

  const tools: CatalogTool[] = [];
  for (const detail of details) {
    if (!detail) continue; // unreadable target — skip, don't fail the gateway
    const targetName = String(detail.name ?? "");
    const mcp = detail.targetConfiguration?.mcp ?? {};
    // Lambda targets carry definitions inline. An MCP-server target keeps its
    // capabilities in the gateway's synchronized catalog instead and exposes
    // none here, so it contributes nothing rather than wrong data.
    const inline: any[] = mcp.lambda?.toolSchema?.inlinePayload ?? [];
    for (const tool of inline) {
      const bare = String(tool?.name ?? "");
      if (!bare) continue;
      tools.push({
        name: targetName ? `${targetName}___${bare}` : bare,
        description: String(tool?.description ?? ""),
        inputSchema: tool?.inputSchema,
      });
    }
  }

  // Synthesized by the gateway when semantic search is on. Not a target, so it
  // is absent from the loop above — omitting it would under-report the gateway
  // by exactly the tool that makes narrowing possible.
  const searchType = gateway?.protocolConfiguration?.mcp?.searchType;
  if (searchType === "SEMANTIC") {
    tools.push({
      name: SEARCH_TOOL_NAME,
      description:
        "Search this gateway's tools by intent and return the most relevant ones.",
    });
  }
  return tools;
}

/** Enabled gateway descriptors from the registry. Disabled ones must not appear. */
/**
 * AgentCore namespaces a target's tools as `target___tool`. Splitting on that
 * is the convention, not a derivation we own — twelve agent modules rely on it.
 */
const TARGET_TOOL_SEPARATOR = "___";
// Hyphen, not underscore: the service's target-name pattern rejects
// underscores. Must match _TARGET_PREFIX in agent/src/user_mcp.py and
// USER_TARGET_PREFIX in the gateway interceptor -- a mismatch makes every user
// tool look like platform surface, which fails OPEN.
const USER_TARGET_PREFIX = "usr-";

/** Partition holding one row per user-registered target. Written by the agent. */
const OWNER_PK = "TARGET#OWNER";

function targetOfTool(toolName: string): string | null {
  if (!toolName.includes(TARGET_TOOL_SEPARATOR)) return null;
  return toolName.split(TARGET_TOOL_SEPARATOR)[0];
}

/**
 * Tool-name prefixes withheld from the catalog (media-harness-projects-access task 11.8, design
 * Component 8). The M2C hybrid-search query and admin tools are reachable only through the
 * project-scoped Search_Tool on the dedicated Cedar-governed Search_Gateway, never as raw gateway
 * tools, so a browse surface must not list them.
 *
 * These are the SAME prefixes the runtime withholds in `agent/src/mcp_manager.py`
 * (`WITHHELD_TOOL_PREFIXES`, fixture `agent/src/project_search/withheld-tool-prefixes.json`). The
 * agent fixture is the source of truth; this is the proxy half and is covered by its own test. Each
 * prefix already carries the AgentCore `___` namespacing, so matching is on the FULL name and on the
 * name after one `___` split (`target___tool`), exactly as `_withhold_tools` does on the agent side.
 */
// `project-graph___`: the knowledge-graph tools, the graph extension of project search — Search_Gateway only.
// `prompt-registry-admin___` and the four prompt-registry WRITE tools: the prompts are shared M2C workflow
// configuration (the internal ones drive shot enrichment, conversation analysis, …), so an agent may read
// them (`get_prompt`, `list_prompts`, `describe_tool`) but never save, promote, delete or deprecate one.
// `media-uploads___` (M2C alpha.20261017a): presigned uploads of LOCAL files into the ingest bucket, written with
// the gateway's role — anywhere in the bucket, other projects' keys and the import inbox included. Our agents have
// no local files and users upload through the proxy, so the target is never offered.
// Stopgap until the M2C gateway carries a Cedar policy engine; a test pins this list to the agent fixture.
export const WITHHELD_TOOL_PREFIXES = [
  "hybrid-search___", "hybrid-search-admin___", "project-graph___",
  "prompt-registry-admin___",
  "prompt-registry___put_prompt", "prompt-registry___promote_prompt",
  "prompt-registry___delete_prompt", "prompt-registry___deprecate_prompt",
  "media-uploads___",
] as const;

/** Whether a tool's full name, or its remainder after one `___` split, starts with a withheld prefix. */
export function isWithheldToolName(toolName: string): boolean {
  const candidates = [toolName];
  const i = toolName.indexOf(TARGET_TOOL_SEPARATOR);
  if (i >= 0) candidates.push(toolName.slice(i + TARGET_TOOL_SEPARATOR.length));
  return candidates.some((name) => WITHHELD_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix)));
}

/** Drop every withheld tool from a list, preserving the order of the rest. */
export function withholdTools(tools: CatalogTool[]): CatalogTool[] {
  return tools.filter((tool) => !isWithheldToolName(tool.name));
}

/**
 * Read every recorded target ownership as one map.
 *
 * The catalog needs the whole set anyway, so a single Query beats a GetItem per
 * tool. Ownership is READ here, never derived — the agent records it at
 * registration time and this is the same row the gateway interceptor consults,
 * so the two surfaces cannot disagree about who owns what.
 *
 * Returns null on failure so the caller can fall back rather than silently
 * showing an unfiltered list.
 */
export async function targetOwners(
  ddb: DynamoDBDocumentClient,
  table: string
): Promise<Map<string, string> | null> {
  try {
    const out = await ddb.send(
      new QueryCommand({
        TableName: table,
        KeyConditionExpression: "#pk = :pk",
        ExpressionAttributeNames: { "#pk": "PK" },
        ExpressionAttributeValues: { ":pk": OWNER_PK },
      })
    );
    const map = new Map<string, string>();
    for (const item of out.Items ?? []) {
      const name = typeof item.SK === "string" ? item.SK : "";
      const owner = typeof item.ownerActorId === "string" ? item.ownerActorId : "";
      if (name && owner) map.set(name, owner);
    }
    return map;
  } catch (err: any) {
    console.warn("[toolCatalog] target ownership read failed:", err?.message);
    return null;
  }
}

/**
 * Whether `actorId` may see a tool, per the recorded ownership.
 *
 * Platform tools (not namespaced to a `usr_` target) are visible to everyone.
 * A user target with no record, or no actor, is hidden — fail closed, matching
 * the interceptor.
 */
export function actorMaySeeTool(
  actorId: string,
  toolName: string,
  owners: Map<string, string>
): boolean {
  const target = targetOfTool(toolName);
  if (!target || !target.startsWith(USER_TARGET_PREFIX)) return true;
  if (!actorId) return false;
  return owners.get(target) === actorId;
}

export async function enabledGatewayDescriptors(
  ddb: DynamoDBDocumentClient,
  table: string
): Promise<Array<{ id: string; name: string; url?: string }> | null> {
  try {
    const res = await ddb.send(
      new QueryCommand({
        TableName: table,
        KeyConditionExpression: "PK = :pk",
        ExpressionAttributeValues: { ":pk": REGISTERED_PK },
      })
    );
    return (res.Items ?? [])
      .filter((it: any) => it?.enabled === true)
      .map((it: any) => ({
        id: String(it.id ?? it.SK ?? ""),
        name: String(it.name ?? it.id ?? "Unnamed"),
        url: it.url ? String(it.url) : undefined,
      }));
  } catch {
    return null;
  }
}

/** The builtin projection the runtime publishes at startup. */
export async function builtinToolsFromRegistry(
  ddb: DynamoDBDocumentClient,
  table: string
): Promise<CatalogTool[] | null> {
  try {
    const res = await ddb.send(
      new GetCommand({ TableName: table, Key: { PK: CATALOG_PK, SK: CATALOG_BUILTIN_SK } })
    );
    const tools = res.Item?.tools;
    if (!Array.isArray(tools)) return null;
    return tools.map((t: any) => ({
      name: String(t?.name ?? ""),
      description: String(t?.description ?? ""),
    }));
  } catch {
    return null;
  }
}

/**
 * Seam for tests, mirroring `McpServerDeps` in `mcpServers.ts`.
 *
 * Injected rather than spied: `catalogWithoutRuntime` calls the enumerator through
 * a local binding, so `vi.spyOn(module, ...)` would not intercept it and the test
 * would silently exercise the real signed fetch instead.
 */
export interface CatalogDeps {
  gatewayTools: (region: string, gatewayId: string) => Promise<CatalogTool[] | null>;
}

const defaultCatalogDeps: CatalogDeps = { gatewayTools: gatewayToolsFromControlPlane };

/** What one runtime-free assembly produced, and what it could not reach. */
export interface CatalogAssembly {
  sections: CatalogSectionOut[];
  /**
   * Display names of enabled gateways whose tools the control plane could not
   * enumerate. NOT an error: the caller shows these as pending and fills them in
   * from the data plane. Empty means the assembly is complete.
   */
  unreadable: string[];
}

/**
 * Assemble the catalog without invoking the runtime.
 *
 * **Degrades per gateway, not as a whole.** This used to return null the moment
 * ONE enabled gateway could not be enumerated, and sandbox has a cross-account
 * gateway (`video-graph-tools`, hosted in 100000000002) that this account's
 * control plane answers with `ResourceNotFoundException`. So every catalog read
 * threw away 17 built-in tools and two perfectly readable gateways to cold-start
 * the runtime — the tab was slow because we discarded what was already in hand,
 * not because the data was far away. And it got worse with each remote gateway
 * added, since any one of them re-triggered it.
 *
 * A gateway the control plane cannot describe is reported in `unreadable` rather
 * than dropped. Absent ≠ empty: only the data plane can list an MCP-server
 * target's tools (see `gatewayToolsFromControlPlane`), so "we could not read this
 * one" and "this one has no tools" must not arrive as the same answer.
 *
 * Still returns null for a TOTAL failure — no registry table, an unreadable
 * registry, no published builtins (a fresh deploy, where the runtime is the only
 * source), or unreadable ownership records. That last one is a security
 * boundary, not an optimisation: without the ownership map the `usr-` filter
 * cannot be applied, and serving unfiltered would leak every actor's registered
 * tools to everyone.
 */
export async function catalogWithoutRuntime(
  region: string,
  ddb: DynamoDBDocumentClient | null,
  table: string | undefined,
  actorId = "",
  deps: CatalogDeps = defaultCatalogDeps
): Promise<CatalogAssembly | null> {
  if (!ddb || !table) return null;

  const [descriptors, builtins, owners] = await Promise.all([
    enabledGatewayDescriptors(ddb, table),
    builtinToolsFromRegistry(ddb, table),
    targetOwners(ddb, table),
  ]);
  if (!descriptors || !builtins || !owners) return null;

  const sections: CatalogSectionOut[] = [{ label: "Native Tools", tools: withholdTools(builtins) }];
  const unreadable: string[] = [];
  for (const d of descriptors) {
    const gatewayId = gatewayIdFromUrl(d.url);
    if (!gatewayId) {
      // Federated by runtime URL rather than gateway id, so the control plane has
      // nothing to enumerate. The data plane does.
      unreadable.push(d.name);
      continue;
    }
    const tools = await deps.gatewayTools(region, gatewayId);
    if (tools === null) {
      // Cross-account or otherwise invisible from here. Its tools still reach
      // agents over the gateway's own OAuth connection, so this is a gap in THIS
      // read path, not a broken gateway.
      unreadable.push(d.name);
      continue;
    }
    // The control plane is NOT interceptor-filtered — the interceptor only sees
    // the MCP data plane. Without this, every user's registered MCP tools would
    // be listed to everyone here even though calling them is denied.
    // `withholdTools` also drops the hybrid-search query/admin tools, which reach
    // the model only through the project Search_Gateway (task 11.8).
    sections.push({
      label: `Gateway: ${d.name}`,
      tools: withholdTools(tools.filter((t) => actorMaySeeTool(actorId, t.name, owners))),
    });
  }
  return { sections, unreadable };
}
