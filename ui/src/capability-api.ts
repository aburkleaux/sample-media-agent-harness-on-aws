/**
 * Client for the always-on Capability API (`/control-plane/*`).
 *
 * The migration's read path. Where `lab-api.ts` posts an action to `/lab` and
 * the proxy forwards it into the Main Agent AgentCore Runtime — cold-starting
 * Strands, a model client, OAuth, and MCP sessions to answer a question that
 * needs none of them — this talks to a dedicated always-warm Fargate service
 * that reads DynamoDB, the harness-shared Agent Registry, and the Gateway
 * control plane directly.
 *
 * Same-origin through CloudFront, so no CORS and no AWS credentials in the
 * browser. Requests carry the Cognito access token via `authFetch` (the
 * `/control-plane` prefix is registered in `auth/authFetch.ts`); the service
 * verifies it and derives actor/group/org from the claims — this module never
 * sends an actor id, because one asserted by the client would be ignored (and
 * should be).
 *
 * Cutover is per category and defaults to `legacy`, so importing this module
 * changes nothing until `capabilitySources` says otherwise.
 */

import { authFetch } from './auth/authFetch';
import type { DraftCategory, PublishedAssetSummary } from './lab-api';

// --- Wire types (camelCase; mirrors agent/src/capability/dto.py) ---

export type CapabilityCategory = DraftCategory | 'tool';

export type CapabilitySource =
  | 'asset-index'
  | 'agent-registry'
  | 'builtin'
  | 'gateway';

export type CapabilityClass =
  | 'asset'
  | 'gateway-mcp'
  | 'builtin'
  | 'tool-collection';

export type ProjectionStatus = 'in_sync' | 'pending' | 'failed' | 'ineligible';

/**
 * Per-category read source.
 *
 * `canonical` and `registry` are both served by the Capability API and differ
 * only in whether Agent Registry rows are merged in server-side; `legacy` and
 * `shadow` both read through the agent runtime. See `MODES` in
 * `agent/src/capability/config.py`.
 */
export type CapabilityMode = 'legacy' | 'shadow' | 'canonical' | 'registry';

export interface CapabilitySummary {
  id: string;
  category: CapabilityCategory;
  capabilityClass: CapabilityClass;
  displayName: string;
  /**
   * The author's description. Absent when the source has none to give — the
   * server drops the field rather than sending `''` (see `CapabilitySummary`
   * in `capability/dto.py`), so absence here is "unknown", not "blank".
   */
  description?: string;
  scope: 'private' | 'group' | 'org' | 'harness-shared';
  status: 'draft' | 'published' | 'deprecated';
  source: CapabilitySource;
  assetId?: string;
  version?: number | string;
  registryRecordId?: string;
  /** Gateway provenance. Retained even when the UI groups by target. */
  gatewayId?: string;
  targetId?: string;
  targetName?: string;
  /** What a runtime actually calls, e.g. `media-processing___process_media`. */
  qualifiedToolName?: string;
  /** As the target declares it. Repeats across targets by design. */
  bareToolName?: string;
  schemaDigest?: string;
  agentAvailability?: string[];
  /** How a built-in sub-agent is reached (`dispatch_tool`). Built-ins only. */
  invokedVia?: string;
  /**
   * Whether a built-in sub-agent's runtime is wired in this deployment.
   *
   * Absent means the question does not apply; `false` means it applies and the
   * answer is no. Do not collapse the two — `?? false` would render "we did not
   * look" as "not deployed", for every row that is not a built-in sub-agent.
   */
  configured?: boolean;
  /** The runtime's id (an ARN's last segment) when `configured`. */
  runtimeId?: string;
  projectionStatus?: ProjectionStatus;
  updatedAt?: string;
}

/** A source that degraded without failing the request (R12). */
export interface CapabilitySourceError {
  source: CapabilitySource;
  /** Stable class: `timeout` | `unavailable` | `config` | `error`. */
  error: string;
  detail?: string;
}

export interface CategoryPage {
  category: CapabilityCategory;
  items: CapabilitySummary[];
  nextCursor?: string;
  partialErrors?: CapabilitySourceError[];
  sourcesQueried?: string[];
}

export interface CapabilityConfigResponse {
  deployEnv: string;
  capabilitySources: Partial<Record<CapabilityCategory, CapabilityMode>>;
  registryConfigured: boolean;
}

const BASE = '/control-plane';

export class CapabilityApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** Stable server-side code (`authority_denied`, `conflict`, …), when the
     *  route surfaces one. Only the destructive routes do. */
    readonly code?: string,
  ) {
    super(message);
    this.name = 'CapabilityApiError';
  }
}

async function parse<T>(res: Response, what: string): Promise<T> {
  if (!res.ok) {
    // The response body is deliberately not surfaced: a 401 carries only a
    // stable code, and a 5xx must not leak a source's raw error into the UI.
    throw new CapabilityApiError(res.status, `${what} failed (${res.status})`);
  }
  return (await res.json()) as T;
}

/**
 * Like `parse`, but surfaces the server's typed error code and message.
 *
 * The right default for a read is to discard the body — but for an irreversible
 * write the user has to know *which* failure they hit: 403 means stop, 409 after
 * a partial purge means retry to finish, 503 means the deployment is incomplete.
 * Collapsing those into "failed (4xx)" would be actively misleading.
 */
async function parseDestructive<T>(res: Response, what: string): Promise<T> {
  if (!res.ok) {
    let code: string | undefined;
    let message: string | undefined;
    try {
      const detail = (await res.json())?.detail;
      if (detail && typeof detail === 'object') {
        code = typeof detail.code === 'string' ? detail.code : undefined;
        message = typeof detail.message === 'string' ? detail.message : undefined;
      }
    } catch {
      // Body absent, or HTML rather than JSON — CloudFront rewrites some errors
      // into the SPA shell. Fall through to the status-only message.
    }
    throw new CapabilityApiError(
      res.status,
      message ?? `${what} failed (${res.status})`,
      code,
    );
  }
  return (await res.json()) as T;
}

/**
 * Per-category cutover flags.
 *
 * Read from the Capability API rather than the proxy's `/config` so there is
 * one source of truth for the flags — the service whose behaviour they
 * describe. Unauthenticated, and every failure resolves to all-`legacy`: if
 * the control plane cannot be reached, the safe answer is "keep using the
 * path that works" (R14).
 */
/** Path of the control-plane lab dispatcher — the same actions, a warm host. */
export const CONTROL_PLANE_LAB_PATH = `${BASE}/lab`;

let labHostPromise: Promise<boolean> | null = null;

/**
 * Whether lab actions may be sent to the Capability API in this environment.
 *
 * Derived from the existing per-category flags rather than a new switch, because
 * those flags already encode the two things that must both be true: the service
 * is deployed *and* this environment intends to use it. Any category on
 * `canonical`/`registry` proves both — nothing sets those flags in an environment
 * where the service is absent.
 *
 * That matters more than it looks. Staging deploys the Capability API with
 * `desiredCount: 0` and has no `capabilitySources` block at all, so
 * `fetchCapabilityConfig` resolves to all-`legacy` there (it swallows an
 * unreachable service into exactly that). Routing writes to a service running
 * zero tasks would take Skills from slow to broken, and this path has no
 * automatic fallback — the check has to fail closed, and reusing the flags is
 * what makes it do so without a second thing to remember to set.
 *
 * Memoized: one config fetch per page load, not one per CRUD call.
 */
export async function controlPlaneServesLabActions(): Promise<boolean> {
  if (!labHostPromise) {
    labHostPromise = fetchCapabilityConfig()
      .then((config) =>
        Object.values(config.capabilitySources ?? {}).some(
          (mode) => mode === 'canonical' || mode === 'registry',
        ),
      )
      .catch(() => false);
  }
  return labHostPromise;
}

/** Test-only: clear the memoized host decision between cases. */
export function __resetLabHostCache(): void {
  labHostPromise = null;
}

export async function fetchCapabilityConfig(): Promise<CapabilityConfigResponse> {
  try {
    const res = await fetch(`${BASE}/config`, { headers: { accept: 'application/json' } });
    if (!res.ok) throw new Error(String(res.status));
    return (await res.json()) as CapabilityConfigResponse;
  } catch {
    return { deployEnv: 'unknown', capabilitySources: {}, registryConfigured: false };
  }
}

/**
 * Load several categories in one request.
 *
 * Pass only the categories being displayed. The service loads exactly what it
 * is asked for — there is no "everything" mode — so a tab selection cannot
 * trigger hidden preloads of the other four (R10/R13).
 */
export async function bootstrapCapabilities(
  categories: CapabilityCategory[],
  opts: { limit?: number } = {},
): Promise<Record<string, CategoryPage>> {
  const res = await authFetch(`${BASE}/capabilities/bootstrap`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ categories, ...(opts.limit ? { limit: opts.limit } : {}) }),
  });
  const body = await parse<{ categories: Record<string, CategoryPage> }>(
    res,
    'capability bootstrap',
  );
  return body.categories ?? {};
}

/** One category, with an opaque cursor for the next page. */
export async function listCapabilities(
  category: CapabilityCategory,
  opts: { cursor?: string; limit?: number } = {},
): Promise<CategoryPage> {
  const params = new URLSearchParams();
  if (opts.cursor) params.set('cursor', opts.cursor);
  if (opts.limit) params.set('limit', String(opts.limit));
  const query = params.toString();
  const res = await authFetch(
    `${BASE}/capabilities/${encodeURIComponent(category)}${query ? `?${query}` : ''}`,
  );
  return parse<CategoryPage>(res, `capability list (${category})`);
}

/** Substring search across the given categories (all, when omitted). */
export async function searchCapabilities(
  query: string,
  categories?: CapabilityCategory[],
): Promise<{ items: CapabilitySummary[]; partialErrors?: CapabilitySourceError[] }> {
  const res = await authFetch(`${BASE}/capabilities/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, ...(categories ? { categories } : {}) }),
  });
  return parse(res, 'capability search');
}

/**
 * Resolve known summary ids.
 *
 * An id the caller cannot see is simply absent from the response — the API does
 * not distinguish "no such asset" from "not yours", so a missing id must not be
 * treated as an error.
 */
export async function batchGetCapabilities(
  ids: string[],
  categories?: CapabilityCategory[],
): Promise<CapabilitySummary[]> {
  if (ids.length === 0) return [];
  const res = await authFetch(`${BASE}/capabilities/batch-get`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, ...(categories ? { categories } : {}) }),
  });
  const body = await parse<{ items: CapabilitySummary[] }>(res, 'capability batch-get');
  return body.items ?? [];
}

// --- Purge (the one destructive control-plane operation) ---

/** Something that still points at the agent about to be purged. */
export interface PurgeReference {
  kind: string;
  assetId: string;
  name: string;
  status: string;
  detail: string;
}

/** What a purge would destroy, and what the server could not see. */
export interface PurgePreflight {
  assetId: string;
  category: string;
  versions: number[];
  scopes: string[];
  workingCopyExists: boolean;
  /** External `POST /invoke/<id>` starts failing closed once this is purged. */
  externalAccessLive: boolean;
  prefixes: string[];
  references: PurgeReference[];
  /**
   * Sources that were not or could not be scanned — usually the org tier, since
   * the request context carries no org id. Non-empty means the reference list is
   * "what is visible to you", NOT "everything".
   */
  partial: string[];
}

export interface PurgeResult {
  dryRun: false;
  assetId: string;
  category: string;
  versionsDeleted: number[];
  workingCopyDeleted: boolean;
  prefixesDeleted: string[];
  /** Prefixes whose S3 sweep failed. Unreferenced bytes, not a broken asset. */
  prefixesOrphaned: string[];
  outboxEvents: number;
  /** The blast radius as computed at purge time — an audit trail. */
  preflight: PurgePreflight;
}

/** Categories the server will purge. Anything else is refused with a 400. */
export type PurgeableCategory = 'skill' | 'specialty-agent';

async function postPurge<T>(
  category: PurgeableCategory,
  assetId: string,
  dryRun: boolean,
): Promise<T> {
  const res = await authFetch(
    `${BASE}/assets/${category}/${encodeURIComponent(assetId)}/purge`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dryRun }),
    },
  );
  return parseDestructive<T>(res, dryRun ? 'purge preview' : 'purge');
}

/**
 * What purging this lineage would destroy. Writes nothing.
 *
 * Shares the server's authorization and enumeration path with the purge itself,
 * so a preview that succeeds means the purge will not be refused for authority
 * or a malformed prefix.
 */
export async function previewPurge(
  category: PurgeableCategory,
  assetId: string,
): Promise<PurgePreflight> {
  const body = await postPurge<{ preflight: PurgePreflight }>(category, assetId, true);
  return body.preflight;
}

/**
 * Destroy every published version of a lineage, and its artifacts.
 *
 * Irreversible. There is no undo and no tombstone of the asset; the Registry
 * record is deprecated rather than deleted, by design. `deprecate` is the
 * reversible alternative — but it is not yet wired to any client, so do not
 * offer it as if it were.
 */
export async function purgeAsset(
  category: PurgeableCategory,
  assetId: string,
): Promise<PurgeResult> {
  return postPurge<PurgeResult>(category, assetId, false);
}

/* Category-bound aliases. The two callers each know their category statically,
 * and naming it at the call site reads better than threading a string. */
export const previewAgentPurge = (assetId: string) =>
  previewPurge('specialty-agent', assetId);
export const purgeAgent = (assetId: string) => purgeAsset('specialty-agent', assetId);
export const previewSkillPurge = (assetId: string) => previewPurge('skill', assetId);
export const purgeSkill = (assetId: string) => purgeAsset('skill', assetId);

// --- Adapters to the existing view models ---
//
// The Capability tabs render `DraftMetadata`-shaped rows today. Rather than
// rewrite every panel at cutover, these map the normalized DTO onto the shapes
// the views already consume, so a category can be flipped independently and
// rolled back by configuration alone (R14/R15).

/** Working copies (no version) as the drafts list expects. */
export function toDraftMetadata(
  page: CategoryPage,
): Array<{
  assetId: string;
  name: string;
  /** Optional for the same reason as on `DraftMetadata`: absent means unknown. */
  description?: string;
  category: DraftCategory;
  status: 'active' | 'archived';
  createdAt: number;
  updatedAt: number;
  s3Prefix: string;
}> {
  return page.items
    .filter((item) => item.status === 'draft' && item.assetId)
    .map((item) => ({
      assetId: item.assetId as string,
      name: item.displayName,
      // Passed through as-is, INCLUDING undefined: the row renders no description
      // line for an unknown one, which is not the same as an empty one.
      description: item.description,
      category: item.category as DraftCategory,
      status: 'active' as const,
      createdAt: toEpochMs(item.updatedAt),
      updatedAt: toEpochMs(item.updatedAt),
      // The control plane deliberately does not expose canonical S3 prefixes to
      // the browser (R4: no package paths in discovery data). Editors resolve
      // content through the API, so an empty string here is the honest answer
      // rather than a fabricated path.
      s3Prefix: '',
    }));
}

/** Immutable versions as the PUBLISHED section expects. */
/**
 * Built-in sub-agents from a `specialty-agent` page, shaped for the panel.
 *
 * These rows are already in every page the Agents tab fetches — `read_builtins`
 * runs inside the bootstrap — but both `toDraftMetadata` and
 * `toPublishedSummaries` filter them out (one keeps only drafts, the other only
 * numeric versions, and built-ins are published with a string version). So the
 * panel used to re-fetch equivalent data from the agent runtime and pay a cold
 * start for it. This selects what already arrived instead: no request at all.
 *
 * `key` is the bare slug rather than the runtime's historical short form
 * (`task-agent`, not `task`) because that is this row's actual identity here.
 * It is used for the React key and the panel's search, not for dispatch.
 */
export interface BuiltInAgentRow {
  key: string;
  label: string;
  purpose: string;
  invokedVia: string;
  configured: boolean;
  runtimeId?: string;
}

export function toBuiltInAgents(page: CategoryPage): BuiltInAgentRow[] {
  return page.items
    .filter((item) => item.capabilityClass === 'builtin')
    .map((item) => ({
      key: item.assetId ?? item.id,
      label: item.displayName,
      // The panel renders these unconditionally, so an absent one becomes '' here
      // rather than leaking `undefined` into a template.
      purpose: item.description ?? '',
      invokedVia: item.invokedVia ?? '',
      // `configured` is only absent if the server did not populate it — which for
      // a built-in sub-agent means its ARN env var never reached the service. That
      // is a deployment fault, and reporting "not configured" is the honest read.
      configured: item.configured === true,
      runtimeId: item.runtimeId,
    }));
}

/**
 * Built-in skills from a `skill` page — the catalog half only.
 *
 * Deliberately carries no `activated` / `source` / `dirty`: those describe which
 * skills are loaded into a live agent session, which the control plane cannot know.
 * The panel merges them in afterwards from the runtime, and must render a row
 * WITHOUT a badge until it does — a default of "not activated" would be a false
 * negative presented as fact.
 */
export interface BuiltInSkillRow {
  name: string;
  description: string;
  tools?: string[];
}

/** One built-in skill's package, as the detail view renders it. */
export interface BuiltInSkillPackage {
  name: string;
  description: string;
  skillMd: string;
  supportingFiles: string[];
}

/**
 * Fetch a built-in skill's package from the control plane. `null` when there is no such
 * skill in the catalog.
 *
 * `null` rather than throwing on 404, because a miss is expected and actionable: the
 * built-in panel also lists skills the RUNTIME loaded (from S3 or in memory), which never
 * appear in a catalog written at deploy time. The caller falls back to the runtime for
 * those, so the fast path covers what ships without dropping what was loaded.
 *
 * Any other status still throws — a 503 means the catalog is not deployed, and silently
 * falling back would hide that behind a slow-but-working view.
 */
export async function fetchBuiltInSkill(
  slug: string,
): Promise<BuiltInSkillPackage | null> {
  const res = await authFetch(`${BASE}/builtin-skills/${encodeURIComponent(slug)}`);
  if (res.status === 404) return null;
  return parse<BuiltInSkillPackage>(res, 'built-in skill');
}

export function toBuiltInSkills(page: CategoryPage): BuiltInSkillRow[] {
  return page.items
    .filter((item) => item.capabilityClass === 'builtin')
    .map((item) => ({
      name: item.assetId ?? item.displayName,
      description: item.description ?? '',
    }));
}

export function toPublishedSummaries(page: CategoryPage): PublishedAssetSummary[] {
  return page.items
    .filter((item) => item.assetId && typeof item.version === 'number')
    .map((item) => ({
      assetId: item.assetId as string,
      name: item.displayName,
      description: item.description,
      version: item.version as number,
      // `current` is a canonical lifecycle field the summary does not carry;
      // the editor resolves it when opening a lineage. Defaulting to false
      // under-claims rather than mislabelling a version as current.
      current: false,
      scope: (item.scope === 'harness-shared' ? 'org' : item.scope) as
        | 'private'
        | 'group'
        | 'org',
      createdAt: toEpochMs(item.updatedAt),
      updatedAt: toEpochMs(item.updatedAt),
    }));
}

function toEpochMs(iso?: string): number {
  if (!iso) return 0;
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? 0 : parsed;
}

/**
 * Group Gateway tools as Gateway → Target → Tool.
 *
 * Grouping only; nothing is deduplicated. Two targets exposing `get_job_status`
 * stay two entries under their own targets, each keeping its
 * `qualifiedToolName` — collapsing them would destroy the identity a runtime
 * invokes by (R10).
 */
export function groupGatewayTools(
  items: CapabilitySummary[],
): Array<{
  gatewayId: string;
  targets: Array<{ targetId?: string; targetName?: string; tools: CapabilitySummary[] }>;
}> {
  const byGateway = new Map<string, Map<string, CapabilitySummary[]>>();
  for (const item of items) {
    if (item.capabilityClass !== 'gateway-mcp') continue;
    const gateway = item.gatewayId ?? '';
    // Key on targetId, not targetName: two targets can share a display name,
    // and the id is what the invocation path resolves.
    const targetKey = item.targetId ?? item.targetName ?? '';
    const targets = byGateway.get(gateway) ?? new Map();
    const tools = targets.get(targetKey) ?? [];
    tools.push(item);
    targets.set(targetKey, tools);
    byGateway.set(gateway, targets);
  }
  return [...byGateway.entries()].map(([gatewayId, targets]) => ({
    gatewayId,
    targets: [...targets.values()].map((tools) => ({
      targetId: tools[0]?.targetId,
      targetName: tools[0]?.targetName,
      tools,
    })),
  }));
}
