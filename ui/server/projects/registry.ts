// Project_Registry operations (media-harness-projects-access task 6.2; design Component 1, Data Models
// "Projects table" transaction table and retry policy, Key flows 3 and 4).
//
// Every write is ONE `TransactWriteItems` whose conditions encode the invariants; every read is consistent.
// Each operation is a `plan`: a consistent pre-read that either refuses the request (a typed error) or
// produces the transaction for the state it read. `run` commits the plan and, when the transaction is
// cancelled, re-plans from a fresh read:
//   * the re-read refuses the request (not found, Owner only, a limit, already a Member, the Personal rule)
//     → that error, without another write;
//   * the re-read still permits it (a `TransactionConflict`, or a pinned value such as `nameFold`,
//     `memberCount`, or the mirror set changed under a stale pre-read) → full-jitter backoff (base 50 ms)
//     and retry, at most 3 times, then `registry_busy` (retryable). The AWS SDK does not retry
//     `TransactionCanceledException`, so this loop is the only retry.
// A cancelled transaction changes nothing, so `registry_busy` leaves every item as it was.
//
// Create and ensure-Personal also regenerate the id when META's `attribute_not_exists(PK)` fails on a held
// id (a live Project or a tombstone, Req 1.6): at most 3 regenerations, then `id_generation_failed`.
//
// Every access denial (malformed id, never created, deleted, tombstoned, not a member) returns the one
// `PROJECT_NOT_FOUND` object (Req 1.17). No membership item carries an expiry attribute (Req 1.28).
// `ensurePersonalProject` (task 6.3, `access.ts`) wraps `ProjectRegistry.ensurePersonal`.
import { orderProjects } from "../../src/projects/orderProjects";
import { isProjectId, newProjectId } from "./ids";
import { PERSONAL_FOLD, foldName, validateProjectName } from "./names";
import type { InvalidNameError } from "./names";
import {
  MEMBER_SK_PREFIX,
  MIRROR_SK_PREFIX,
  countersKey,
  isTombstone,
  memberKey,
  metaKey,
  mirrorKey,
  nameGuardKey,
  personalKey,
  pidFromMirrorSk,
  projectPk,
  subFromMemberSk,
  userPk,
} from "./schema";
import type { Key, ProjectRole, ProjectStatus } from "./schema";
import { TransactionCancelledError, cond, keyId } from "./store";
import type { Condition, Item, TransactOp, TransactStore } from "./store";
import { graphOn, indexingOn, isSettingsError, validateDescription, validateTags } from "./settings";
import type { SettingsError } from "./settings";

/** Members per Project, the Owner not counted (Req 1.12). */
export const MAX_PROJECT_MEMBERS = 25;
/** Non-Personal Projects one Actor may own (Req 1.3, 2.5). */
export const MAX_OWNED_PROJECTS = 50;
/** Retries after the first attempt, for conflicts and stale pre-reads. */
export const MAX_REGISTRY_RETRIES = 3;
/** Id regenerations after the first id, on a held id. */
export const MAX_ID_RETRIES = 3;
export const RETRY_BACKOFF_BASE_MS = 50;
const RETRY_BACKOFF_CAP_MS = 1_000;

export const PERSONAL_PROJECT_NAME = "Personal";

// ----------------------------------------------------------------------------------------- results

export type OwnerOnlyAction = "rename" | "invite" | "remove" | "delete" | "settings";

export type RegistryError =
  | { readonly code: "project_not_found"; readonly message: "Project not found" }
  | (InvalidNameError & { message: string })
  | { code: "owned_project_limit"; limit: number; message: string }
  | { code: "member_limit"; limit: number; message: string }
  | { code: "already_in_project"; message: string }
  | { code: "owner_only"; action: OwnerOnlyAction; message: string }
  | { code: "owner_cannot_leave"; message: string }
  | { code: "personal_project"; message: string }
  | { code: "confirm_name_mismatch"; message: string }
  | { code: "member_not_found"; message: string }
  | { code: "registry_busy"; retryable: true; message: string }
  | { code: "id_generation_failed"; message: string }
  | { code: "graph_not_enabled"; message: string }
  | SettingsError;

export type RegistryErrorCode = RegistryError["code"];

export type RegistryResult<T> = { ok: true; value: T } | { ok: false; error: RegistryError };

/** The single not-found every access denial returns, so all denials are byte-identical (Req 1.17). */
export const PROJECT_NOT_FOUND = Object.freeze({ code: "project_not_found", message: "Project not found" } as const);

export const REGISTRY_MESSAGES = {
  ownedLimit: `You can own at most ${MAX_OWNED_PROJECTS} Projects (your Personal project is not counted)`,
  memberLimit: `A Project can have at most ${MAX_PROJECT_MEMBERS} Members`,
  alreadyInProject: "That user is already in the Project",
  ownerCannotLeave: "The Project owner cannot leave or be removed; ownership transfer is not supported",
  personal: "The Personal project is private and cannot be shared, renamed, deleted, or left",
  confirmMismatch: "Type the Project name exactly to confirm deletion",
  memberNotFound: "That user is not a Member of this Project",
  busy: "The Project registry is busy; try again",
  idGeneration: "Could not allocate a Project id",
} as const;

const OWNER_ONLY_VERB: Record<OwnerOnlyAction, string> = {
  rename: "rename this Project",
  invite: "invite Members to this Project",
  remove: "remove Members from this Project",
  delete: "delete this Project",
  settings: "change this Project's settings",
};

/** The HTTP status each error maps to (routes, task 6.9). */
export function registryErrorStatus(e: RegistryError): number {
  switch (e.code) {
    case "project_not_found":
    case "member_not_found":
    case "graph_not_enabled":
      return 404;
    case "owner_only":
    case "personal_project":
      return 403;
    case "registry_busy":
      return 503;
    case "id_generation_failed":
      return 500;
    default:
      return 400;
  }
}

const notFound = (): RegistryResult<never> => ({ ok: false, error: PROJECT_NOT_FOUND });
const fail = (error: RegistryError): RegistryResult<never> => ({ ok: false, error });
const ownerOnly = (action: OwnerOnlyAction) =>
  fail({ code: "owner_only", action, message: `Only the Project owner can ${OWNER_ONLY_VERB[action]}` });
const personalRule = () => fail({ code: "personal_project", message: REGISTRY_MESSAGES.personal });
const ownerCannotLeave = () => fail({ code: "owner_cannot_leave", message: REGISTRY_MESSAGES.ownerCannotLeave });
const invalidName = (e: InvalidNameError) => fail({ ...e, message: e.rule });

// ------------------------------------------------------------------------------------------ views

export interface ProjectMeta {
  projectId: string;
  name: string;
  nameFold: string;
  ownerSub: string;
  personal: boolean;
  status: ProjectStatus;
  /** Members only; the Owner is not counted. */
  memberCount: number;
  createdAt: string;
  deletedAt?: string;
  /** Owned by the ingestion spec; absent means unknown, not zero. */
  activeAssetCount?: number;
  collectionState?: string;
  /** Project settings: free text for members and the agent, and labels. */
  description: string;
  tags: string[];
  /** Whether files are analyzed for search. Default OFF for new Projects; owner turns it ON, never OFF. */
  indexing: boolean;
  /** Whether the Project has a knowledge graph (`graph/` Lance tables). Absent attribute = OFF. */
  graph: boolean;
  /** Files kept but not analyzed (indexing OFF); what turning indexing on would queue. Absent = unknown. */
  storedCount?: number;
}

export interface ProjectListEntry {
  projectId: string;
  name: string;
  role: ProjectRole;
  personal: boolean;
  activeAssetCount?: number;
  /** Search indexing ON (files are analyzed and searchable). Read from the META the list already loads. */
  indexing: boolean;
}

export interface ProjectMemberView {
  sub: string;
  display: string;
  role: ProjectRole;
  addedAt: string;
}

export interface ProjectDetail {
  project: ProjectMeta;
  /** The caller's role. */
  role: ProjectRole;
  owner: ProjectMemberView;
  members: ProjectMemberView[];
}

/** An invitee already resolved against Cognito (task 6.7). */
export interface Invitee {
  sub: string;
  /** Email or username, as stored on the MEMBER item. */
  display: string;
}

export interface CreateOptions {
  /** The Owner's email or username for their MEMBER item; defaults to the sub. */
  ownerDisplay?: string;
  description?: unknown;
  tags?: unknown;
  /** Create with indexing ON (default OFF). It can never be turned off afterwards. */
  indexing?: boolean;
  /**
   * Create with a knowledge graph (default ON while the graph feature is enabled; ignored when it is not).
   * Like indexing it can only ever be turned on, never off.
   */
  graph?: boolean;
}

export interface SettingsPatch {
  description?: unknown;
  tags?: unknown;
}

export function toProjectMeta(item: Item): ProjectMeta {
  const m: ProjectMeta = {
    projectId: item.projectId as string,
    name: item.name as string,
    nameFold: item.nameFold as string,
    ownerSub: item.ownerSub as string,
    personal: item.personal === true,
    status: item.status as ProjectStatus,
    memberCount: item.memberCount as number,
    createdAt: item.createdAt as string,
    ...settingsOf(item),
  };
  if (typeof item.deletedAt === "string") m.deletedAt = item.deletedAt;
  if (typeof item.activeAssetCount === "number") m.activeAssetCount = item.activeAssetCount;
  if (typeof item.collectionState === "string") m.collectionState = item.collectionState;
  const stored = (item.statusCounts as Record<string, unknown> | undefined)?.stored;
  if (typeof stored === "number") m.storedCount = stored;
  return m;
}

function settingsOf(item: Item): Pick<ProjectMeta, "description" | "tags" | "indexing" | "graph"> {
  return {
    description: typeof item.description === "string" ? item.description : "",
    tags: Array.isArray(item.tags) ? item.tags.filter((t): t is string => typeof t === "string") : [],
    indexing: indexingOn(item),
    graph: graphOn(item),
  };
}

// ------------------------------------------------------------------------------------------ registry

export interface ProjectRegistryDeps {
  store: TransactStore;
  /** Clock for `createdAt`, `addedAt`, `deletedAt`. */
  now?: () => Date;
  /** Backoff sleep between retries. */
  sleep?: (ms: number) => Promise<void>;
  /** Uniform [0, 1) source for full jitter. */
  random?: () => number;
  /** Project id generator (tests inject held ids). */
  newId?: () => string;
  /**
   * The knowledge-graph feature (`GRAPH_ENABLED`). Off: `graph` is never written, so META rows stay exactly as
   * before. On: new Projects (and new Personal Projects) are written with `graph`, default true.
   */
  graphFeature?: boolean;
}

type Plan<T> =
  | { kind: "done"; result: RegistryResult<T> }
  | {
      kind: "write";
      ops: TransactOp[];
      value: T;
      /** Index of the op whose condition failing means "the id is held" (create, ensure-Personal). */
      idOp?: number;
    };

const done = <T>(result: RegistryResult<T>): Plan<T> => ({ kind: "done", result });

interface Access {
  meta: Item;
  role: ProjectRole;
  /** The extra items requested with the access read, by `keyId`. */
  extra: Map<string, Item>;
}

export class ProjectRegistry {
  private readonly store: TransactStore;
  private readonly now: () => Date;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly random: () => number;
  private readonly newId: () => string;
  private readonly graphFeature: boolean;

  constructor(deps: ProjectRegistryDeps) {
    this.store = deps.store;
    this.now = deps.now ?? (() => new Date());
    this.sleep = deps.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.random = deps.random ?? Math.random;
    this.newId = deps.newId ?? newProjectId;
    this.graphFeature = deps.graphFeature === true;
  }

  // ------------------------------------------------------------------------------------ create

  /** Create a Project owned by `actor` (Req 1.1–1.6). */
  async create(actor: string, rawName: string, opts: CreateOptions = {}): Promise<RegistryResult<ProjectMeta>> {
    requireSub(actor);
    const stateless = validateProjectName(rawName, new Set());
    if (typeof stateless !== "string") return invalidName(stateless);
    const name = stateless;
    const fold = foldName(name);
    const guardKey = nameGuardKey(actor, fold);
    const ownerDisplay = opts.ownerDisplay || actor;
    const description = opts.description === undefined ? "" : validateDescription(opts.description);
    if (isSettingsError(description)) return fail(description);
    const tags = opts.tags === undefined ? [] : validateTags(opts.tags);
    if (isSettingsError(tags)) return fail(tags);
    if (opts.indexing !== undefined && typeof opts.indexing !== "boolean") {
      return fail({ code: "invalid_settings", field: "indexing", message: "indexing must be true or false" });
    }
    if (opts.graph !== undefined && typeof opts.graph !== "boolean") {
      return fail({ code: "invalid_settings", field: "graph", message: "graph must be true or false" });
    }
    const settings = {
      description, tags, indexing: opts.indexing === true,
      ...(this.graphFeature ? { graph: opts.graph !== false } : {}),
    };

    return this.run<ProjectMeta>(async () => {
      const got = await this.read([countersKey(actor), guardKey]);
      if (got.has(keyId(guardKey))) {
        const dup = validateProjectName(rawName, new Set([fold]));
        if (typeof dup !== "string") return done(invalidName(dup));
      }
      const owned = got.get(keyId(countersKey(actor)))?.ownedCount;
      if (typeof owned === "number" && owned >= MAX_OWNED_PROJECTS) {
        return done(fail({ code: "owned_project_limit", limit: MAX_OWNED_PROJECTS, message: REGISTRY_MESSAGES.ownedLimit }));
      }
      const pid = this.newId();
      const at = this.iso();
      const meta = metaItem(pid, name, fold, actor, false, at, settings);
      return {
        kind: "write",
        idOp: 0,
        value: toProjectMeta(meta),
        ops: [
          { kind: "put", item: meta, condition: cond.notExists("PK") },
          { kind: "put", item: memberItem(pid, actor, "owner", ownerDisplay, at, actor) },
          { kind: "put", item: { ...mirrorKey(actor, pid), role: "owner" } },
          { kind: "put", item: { ...guardKey, projectId: pid }, condition: cond.notExists("PK") },
          {
            kind: "update",
            key: countersKey(actor),
            add: { ownedCount: 1 },
            condition: cond.or(cond.notExists("ownedCount"), cond.lt("ownedCount", MAX_OWNED_PROJECTS)),
          },
        ],
      };
    });
  }

  // ---------------------------------------------------------------------------- ensure Personal

  /**
   * The Actor's one Personal_Project, created on first use (Req 2.1–2.3, 2.9). A lost race (PERSONAL
   * already held) or a conflict re-reads PERSONAL and returns the winner.
   */
  async ensurePersonal(actor: string, opts: CreateOptions = {}): Promise<RegistryResult<ProjectMeta>> {
    requireSub(actor);
    const ownerDisplay = opts.ownerDisplay || actor;
    return this.run<ProjectMeta>(async () => {
      const pointer = (await this.read([personalKey(actor)])).get(keyId(personalKey(actor)));
      if (pointer) {
        const pid = pointer.projectId as string;
        const meta = (await this.read([metaKey(pid)])).get(keyId(metaKey(pid)));
        if (!meta || isTombstone(meta)) throw new Error(`Personal pointer of ${actor} names a missing Project ${pid}`);
        return done({ ok: true, value: toProjectMeta(meta) });
      }
      const pid = this.newId();
      const at = this.iso();
      const meta = metaItem(pid, PERSONAL_PROJECT_NAME, PERSONAL_FOLD, actor, true, at, {
        description: "", tags: [], indexing: false, ...(this.graphFeature ? { graph: true } : {}),
      });
      return {
        kind: "write",
        idOp: 1,
        value: toProjectMeta(meta),
        ops: [
          { kind: "put", item: { ...personalKey(actor), projectId: pid }, condition: cond.notExists("PK") },
          { kind: "put", item: meta, condition: cond.notExists("PK") },
          { kind: "put", item: memberItem(pid, actor, "owner", ownerDisplay, at, actor) },
          { kind: "put", item: { ...mirrorKey(actor, pid), role: "owner" } },
        ],
      };
    });
  }

  // ------------------------------------------------------------------------------------ reads

  /**
   * Every active Project the Actor owns or is a Member of, Personal first, then by name (Req 1.7). Does not
   * create the Personal_Project; `GET /projects` calls `ensurePersonal` first.
   */
  async list(actor: string): Promise<ProjectListEntry[]> {
    requireSub(actor);
    const mirrors = await this.store.query(userPk(actor), MIRROR_SK_PREFIX);
    const roles = new Map<string, ProjectRole>();
    for (const m of mirrors) {
      const pid = pidFromMirrorSk(m.SK);
      if (pid) roles.set(pid, m.role === "owner" ? "owner" : "member");
    }
    // `TransactStore.batchGet` is consistent and sends at most 100 keys per BatchGetItem, re-requesting
    // `UnprocessedKeys` with backoff until none remain.
    const metas = roles.size ? await this.store.batchGet([...roles.keys()].map(metaKey)) : [];
    const out: ProjectListEntry[] = [];
    for (const item of metas) {
      if (isTombstone(item) || item.status !== "active") continue;
      const meta = toProjectMeta(item);
      const role = roles.get(meta.projectId);
      if (!role) continue;
      const entry: ProjectListEntry = {
        projectId: meta.projectId, name: meta.name, role, personal: meta.personal, indexing: meta.indexing === true,
      };
      if (meta.activeAssetCount !== undefined) entry.activeAssetCount = meta.activeAssetCount;
      out.push(entry);
    }
    return orderProjects(out);
  }

  /** The Owner and Members by email or username, and the caller's role (Req 1.20). */
  async detail(actor: string, pid: string): Promise<RegistryResult<ProjectDetail>> {
    requireSub(actor);
    const access = await this.readAccess(actor, pid);
    if (!access) return notFound();
    const items = await this.store.query(projectPk(pid), MEMBER_SK_PREFIX);
    const people: ProjectMemberView[] = items.flatMap((i) => {
      const sub = subFromMemberSk(i.SK);
      if (!sub) return [];
      return [{ sub, display: i.display as string, role: i.role === "owner" ? "owner" : "member", addedAt: i.addedAt as string }];
    });
    const owner = people.find((p) => p.role === "owner");
    if (!owner) throw new Error(`Project ${pid} has no owner MEMBER item`);
    const members = people.filter((p) => p.role === "member").sort((a, b) => a.addedAt.localeCompare(b.addedAt));
    return { ok: true, value: { project: toProjectMeta(access.meta), role: access.role, owner, members } };
  }

  // ------------------------------------------------------------------------------------ rename

  /** Rename (Owner only, not Personal; Req 1.2, 1.8, 1.16, 2.4; case-only rename keeps the guard). */
  async rename(actor: string, pid: string, rawName: string): Promise<RegistryResult<ProjectMeta>> {
    requireSub(actor);
    if (!isProjectId(pid)) return notFound();
    const stateless = validateProjectName(rawName, new Set());
    const newFold = typeof stateless === "string" ? foldName(stateless) : null;
    const newGuard = newFold ? nameGuardKey(actor, newFold) : null;

    return this.run<ProjectMeta>(async () => {
      const access = await this.readAccess(actor, pid, newGuard ? [newGuard] : []);
      if (!access) return done(notFound());
      const { meta } = access;
      if (meta.personal === true) return done(personalRule());
      if (access.role !== "owner") return done(ownerOnly("rename"));
      if (typeof stateless !== "string") return done(invalidName(stateless));
      if (!newGuard || !newFold) throw new Error("unreachable: a valid name has a non-empty fold");
      const held = access.extra.get(keyId(newGuard));
      if (held && held.projectId !== pid) {
        const dup = validateProjectName(rawName, new Set([newFold]));
        if (typeof dup !== "string") return done(invalidName(dup));
      }
      const oldFold = meta.nameFold as string;
      const ops: TransactOp[] = [
        {
          kind: "update",
          key: metaKey(pid),
          set: { name: stateless, nameFold: newFold },
          condition: cond.and(
            cond.eq("ownerSub", actor),
            cond.eq("personal", false),
            cond.eq("status", "active"),
            cond.eq("nameFold", oldFold),
          ),
        },
      ];
      // A case-only rename keeps its guard: one transaction cannot name the same item twice.
      if (newFold !== oldFold) {
        ops.push(
          { kind: "delete", key: nameGuardKey(actor, oldFold) },
          { kind: "put", item: { ...newGuard, projectId: pid }, condition: cond.notExists("PK") },
        );
      }
      return { kind: "write", ops, value: toProjectMeta({ ...meta, name: stateless, nameFold: newFold }) };
    });
  }

  // ---------------------------------------------------------------------------------- settings

  /**
   * Change the description and/or tags (Owner only; Personal included — it is the owner's own). Indexing is
   * not set here: turning it ON also queues every stored file, which only the Asset_Service can do.
   */
  async updateSettings(actor: string, pid: string, patch: SettingsPatch): Promise<RegistryResult<ProjectMeta>> {
    requireSub(actor);
    if (!isProjectId(pid)) return notFound();
    const set: Record<string, unknown> = {};
    if (patch.description !== undefined) {
      const d = validateDescription(patch.description);
      if (isSettingsError(d)) return fail(d);
      set.description = d;
    }
    if (patch.tags !== undefined) {
      const t = validateTags(patch.tags);
      if (isSettingsError(t)) return fail(t);
      set.tags = t;
    }
    return this.run<ProjectMeta>(async () => {
      const access = await this.readAccess(actor, pid, []);
      if (!access) return done(notFound());
      if (access.role !== "owner") return done(ownerOnly("settings"));
      if (Object.keys(set).length === 0) return done({ ok: true, value: toProjectMeta(access.meta) });
      return {
        kind: "write",
        ops: [{
          kind: "update",
          key: metaKey(pid),
          set,
          condition: cond.and(cond.eq("ownerSub", actor), cond.eq("status", "active")),
        }],
        value: toProjectMeta({ ...access.meta, ...set }),
      };
    });
  }

  // ------------------------------------------------------------------------------------ graph

  /**
   * Turn the knowledge graph ON (Owner only; one way — there is no way to turn it off). The projects-table
   * stream MODIFY this produces is what makes the graph writer seed the starter graph. Already ON is an
   * idempotent success, so a double click or a retry is harmless.
   */
  async enableGraph(actor: string, pid: string): Promise<RegistryResult<ProjectMeta>> {
    requireSub(actor);
    if (!isProjectId(pid)) return notFound();
    if (!this.graphFeature) return fail({ code: "graph_not_enabled", message: "The knowledge graph is not available here" });
    return this.run<ProjectMeta>(async () => {
      const access = await this.readAccess(actor, pid, []);
      if (!access) return done(notFound());
      if (access.role !== "owner") return done(ownerOnly("settings"));
      if (graphOn(access.meta)) return done({ ok: true, value: toProjectMeta(access.meta) });
      const set = { graph: true, graphEnabledAt: this.iso(), graphEnabledBy: actor };
      return {
        kind: "write",
        ops: [{
          kind: "update",
          key: metaKey(pid),
          set,
          condition: cond.and(cond.eq("ownerSub", actor), cond.eq("status", "active")),
        }],
        value: toProjectMeta({ ...access.meta, ...set }),
      };
    });
  }

  // ------------------------------------------------------------------------------------ delete

  /**
   * Mark deleted (Owner only, not Personal; `confirmName` must equal the name exactly; Req 1.22, 1.23, 2.4).
   * One transaction sets `status = deleted` and `deletedAt`, deletes every mirror and the name guard, and
   * decrements the owned count; the stream `MODIFY` it produces is the ingestion purge's trigger.
   */
  async delete(actor: string, pid: string, confirmName: string): Promise<RegistryResult<ProjectMeta>> {
    requireSub(actor);
    if (!isProjectId(pid)) return notFound();
    return this.run<ProjectMeta>(async () => {
      const access = await this.readAccess(actor, pid);
      if (!access) return done(notFound());
      const { meta } = access;
      if (meta.personal === true) return done(personalRule());
      if (access.role !== "owner") return done(ownerOnly("delete"));
      if (confirmName !== meta.name) return done(fail({ code: "confirm_name_mismatch", message: REGISTRY_MESSAGES.confirmMismatch }));
      const fold = meta.nameFold as string;
      const n = meta.memberCount as number;
      const subs = (await this.store.query(projectPk(pid), MEMBER_SK_PREFIX)).flatMap((i) => {
        const sub = subFromMemberSk(i.SK);
        return sub ? [sub] : [];
      });
      const deletedAt = this.iso();
      const ops: TransactOp[] = [
        {
          kind: "update",
          key: metaKey(pid),
          set: { status: "deleted", deletedAt },
          condition: cond.and(
            cond.eq("ownerSub", actor),
            cond.eq("personal", false),
            cond.eq("status", "active"),
            cond.eq("nameFold", fold),
            cond.eq("memberCount", n),
          ),
        },
        ...subs.map((sub): TransactOp => ({ kind: "delete", key: mirrorKey(sub, pid) })),
        { kind: "delete", key: nameGuardKey(actor, fold) },
        { kind: "update", key: countersKey(actor), add: { ownedCount: -1 } },
      ];
      return { kind: "write", ops, value: toProjectMeta({ ...meta, status: "deleted", deletedAt }) };
    });
  }

  // ------------------------------------------------------------------------------- membership

  /** Add an already-resolved user as a Member (Owner only, not Personal; Req 1.9, 1.11, 1.12, 1.16, 2.4). */
  async invite(actor: string, pid: string, invitee: Invitee): Promise<RegistryResult<ProjectMemberView>> {
    requireSub(actor);
    requireSub(invitee.sub);
    if (!isProjectId(pid)) return notFound();
    const inviteeKey = memberKey(pid, invitee.sub);
    return this.run<ProjectMemberView>(async () => {
      const access = await this.readAccess(actor, pid, [inviteeKey]);
      if (!access) return done(notFound());
      const { meta } = access;
      if (meta.personal === true) return done(personalRule());
      if (access.role !== "owner") return done(ownerOnly("invite"));
      if (invitee.sub === actor || access.extra.has(keyId(inviteeKey))) {
        return done(fail({ code: "already_in_project", message: REGISTRY_MESSAGES.alreadyInProject }));
      }
      if ((meta.memberCount as number) >= MAX_PROJECT_MEMBERS) {
        return done(fail({ code: "member_limit", limit: MAX_PROJECT_MEMBERS, message: REGISTRY_MESSAGES.memberLimit }));
      }
      const addedAt = this.iso();
      return {
        kind: "write",
        value: { sub: invitee.sub, display: invitee.display, role: "member", addedAt },
        ops: [
          {
            kind: "update",
            key: metaKey(pid),
            add: { memberCount: 1 },
            condition: cond.and(
              cond.eq("status", "active"),
              cond.eq("ownerSub", actor),
              cond.eq("personal", false),
              cond.lt("memberCount", MAX_PROJECT_MEMBERS),
            ),
          },
          { kind: "put", item: memberItem(pid, invitee.sub, "member", invitee.display, addedAt, actor), condition: cond.notExists("PK") },
          { kind: "put", item: { ...mirrorKey(invitee.sub, pid), role: "member" } },
        ],
      };
    });
  }

  /** Remove a Member (Owner only; the Owner cannot be removed; Req 1.13, 1.15, 1.16, 2.4). */
  async remove(actor: string, pid: string, sub: string): Promise<RegistryResult<{ sub: string }>> {
    requireSub(actor);
    if (!isProjectId(pid)) return notFound();
    if (typeof sub !== "string" || sub.length === 0) {
      return fail({ code: "member_not_found", message: REGISTRY_MESSAGES.memberNotFound });
    }
    const targetKey = memberKey(pid, sub);
    return this.run<{ sub: string }>(async () => {
      const access = await this.readAccess(actor, pid, [targetKey]);
      if (!access) return done(notFound());
      const { meta } = access;
      if (meta.personal === true) return done(personalRule());
      if (access.role !== "owner") return done(ownerOnly("remove"));
      const target = access.extra.get(keyId(targetKey));
      if (sub === meta.ownerSub || target?.role === "owner") return done(ownerCannotLeave());
      if (!target) return done(fail({ code: "member_not_found", message: REGISTRY_MESSAGES.memberNotFound }));
      return {
        kind: "write",
        value: { sub },
        ops: membershipEndOps(pid, sub, cond.and(cond.eq("status", "active"), cond.eq("ownerSub", actor))),
      };
    });
  }

  /** Leave a Project as a Member (the Owner cannot leave; Req 1.14, 1.15, 2.4). */
  async leave(actor: string, pid: string): Promise<RegistryResult<{ sub: string }>> {
    requireSub(actor);
    if (!isProjectId(pid)) return notFound();
    return this.run<{ sub: string }>(async () => {
      const access = await this.readAccess(actor, pid);
      if (!access) return done(notFound());
      if (access.meta.personal === true) return done(personalRule());
      if (access.role === "owner" || access.meta.ownerSub === actor) return done(ownerCannotLeave());
      return { kind: "write", value: { sub: actor }, ops: membershipEndOps(pid, actor, cond.eq("status", "active")) };
    });
  }

  // ----------------------------------------------------------------------------------- engine

  /**
   * Commit `plan()`'s transaction; on cancellation re-plan from a fresh read. A refusal ends the loop
   * without another write; a still-permitted request retries after full-jitter backoff (conflicts and
   * stale pre-reads share `MAX_REGISTRY_RETRIES`; held ids use `MAX_ID_RETRIES` and no backoff).
   */
  private async run<T>(plan: () => Promise<Plan<T>>): Promise<RegistryResult<T>> {
    let p = await plan();
    let retries = 0;
    let idRetries = 0;
    for (;;) {
      if (p.kind === "done") return p.result;
      let cancelled: TransactionCancelledError;
      try {
        await this.store.transactWrite(p.ops);
        return { ok: true, value: p.value };
      } catch (e) {
        if (!(e instanceof TransactionCancelledError)) throw e;
        cancelled = e;
      }
      const idHeld = p.idOp !== undefined && cancelled.failedConditions.includes(p.idOp);
      p = await plan();
      if (p.kind === "done") return p.result;
      if (idHeld) {
        if (++idRetries > MAX_ID_RETRIES) return fail({ code: "id_generation_failed", message: REGISTRY_MESSAGES.idGeneration });
        continue;
      }
      if (++retries > MAX_REGISTRY_RETRIES) {
        return fail({ code: "registry_busy", retryable: true, message: REGISTRY_MESSAGES.busy });
      }
      await this.sleep(this.random() * Math.min(RETRY_BACKOFF_CAP_MS, RETRY_BACKOFF_BASE_MS * 2 ** (retries - 1)));
    }
  }

  /**
   * One consistent BatchGet of META, the Actor's MEMBER item, and `extra`. Null for every denial: META
   * absent, a tombstone, not active, or no membership.
   */
  private async readAccess(actor: string, pid: string, extra: Key[] = []): Promise<Access | null> {
    if (!isProjectId(pid)) return null;
    const mk = metaKey(pid);
    const ak = memberKey(pid, actor);
    const got = await this.read([mk, ak, ...extra]);
    const meta = got.get(keyId(mk));
    const me = got.get(keyId(ak));
    if (!meta || isTombstone(meta) || meta.status !== "active" || !me) return null;
    return { meta, role: me.role === "owner" ? "owner" : "member", extra: got };
  }

  /** Consistent BatchGet of distinct keys, by `keyId`. */
  private async read(keys: Key[]): Promise<Map<string, Item>> {
    const distinct = [...new Map(keys.map((k) => [keyId(k), { PK: k.PK, SK: k.SK }])).values()];
    const items = await this.store.batchGet(distinct);
    return new Map(items.map((i) => [keyId(i), i]));
  }

  private iso(): string {
    return this.now().toISOString();
  }
}

// ---------------------------------------------------------------------------------------- items

function metaItem(
  pid: string, name: string, nameFold: string, ownerSub: string, personal: boolean, at: string,
  settings: { description: string; tags: string[]; indexing: boolean; graph?: boolean } = {
    description: "", tags: [], indexing: false,
  },
): Item {
  return {
    ...metaKey(pid),
    projectId: pid,
    name,
    nameFold,
    ownerSub,
    personal,
    status: "active",
    memberCount: 0,
    createdAt: at,
    activeAssetCount: 0,
    collectionState: "absent",
    // Written explicitly (never absent) so the legacy "absent = ON" rule only covers pre-settings rows.
    description: settings.description,
    tags: settings.tags,
    indexing: settings.indexing,
    // Only when the graph feature is on; absent reads as OFF (`graphOn`).
    ...(settings.graph !== undefined ? { graph: settings.graph } : {}),
  };
}

/** No expiry attribute, ever (Req 1.28). */
function memberItem(pid: string, sub: string, role: ProjectRole, display: string, addedAt: string, addedBy: string): Item {
  return { ...memberKey(pid, sub), role, display, addedAt, addedBy };
}

/** Remove / Leave: delete MEMBER (`role = member`) and the mirror, and decrement `memberCount`. */
function membershipEndOps(pid: string, sub: string, metaCondition: Condition): TransactOp[] {
  return [
    { kind: "delete", key: memberKey(pid, sub), condition: cond.eq("role", "member") },
    { kind: "delete", key: mirrorKey(sub, pid) },
    { kind: "update", key: metaKey(pid), add: { memberCount: -1 }, condition: metaCondition },
  ];
}

function requireSub(sub: string): void {
  if (typeof sub !== "string" || sub.length === 0) throw new Error("actor sub must be a non-empty string");
}
