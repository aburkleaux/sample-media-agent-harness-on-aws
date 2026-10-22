// Projects table key schema (media-harness-projects-access design Data Models "Projects table", D5).
//
// Single-table, string `PK`/`SK`, no GSIs. Every item a registry operation touches is named here, so a
// typo in a key prefix cannot split one logical item into two. Key builders refuse a malformed project id
// or an empty sub: an empty sub would build `USER#`, a key every actor's code path could collide on.
//
// Timestamps (`createdAt`, `deletedAt`, `addedAt`, `purgedAt`) are ISO 8601 strings.
//
// Project_Asset rows and their guards (media-harness-project-ingestion design §2, Data Models) are keyed
// here too, mirroring the Python twin's additions in `agent/src/projects/schema.py`, so the proxy's asset
// read routes (`ingestionRoutes.ts`) and the Asset_Service Lambda name every item one way.
import { createHash } from "node:crypto";
import { isProjectId } from "./ids";

export const META_SK = "META";
export const PERSONAL_SK = "PERSONAL";
export const COUNTERS_SK = "COUNTERS";
export const MEMBER_SK_PREFIX = "MEMBER#";
export const MIRROR_SK_PREFIX = "PROJECT#";
export const NAME_SK_PREFIX = "NAME#";
// media-harness-project-ingestion §2 Data Models.
export const ASSET_SK_PREFIX = "ASSET#";
export const SRC_SK_PREFIX = "SRC#";
export const DNAME_SK_PREFIX = "DNAME#";

/** The GSI attribute names of `assetsByRegistered` (projection ALL, set on ASSET items). */
export const ASSET_LIST_PK_ATTR = "assetListPk";
export const ASSET_LIST_SK_ATTR = "assetListSk";
export const ASSET_LIST_INDEX = "assetsByRegistered";

/**
 * A Project_Asset id: `<kind>:<lowercase UUID v4>`, kind one of `video`, `image`, `audio`, `document`, `data` (analysis/data files, asset-lineage design §2)
 * (ingestion §2; the `asset_id` kind prefixes projects-access and search-qa-rendering rely on).
 * `document` is every file that is not video, audio, or an image (Projects Phase B indexes ALL file
 * types; document rows wait in `queued` until their indexing phase lands). A malformed id names no
 * ASSET row, so a read of it is simply absent. Twin of `ASSET_ID_PATTERN` in `agent/src/projects/schema.py`.
 */
const ASSET_ID_RE =
  /^(video|image|audio|document|data):[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export type AssetKind = "video" | "image" | "audio" | "document" | "data";

/** Every asset kind, in the order the Python twin's `ASSET_KINDS` lists them. */
export const ASSET_KINDS: readonly AssetKind[] = Object.freeze(["video", "image", "audio", "document", "data"]);

export function isAssetId(s: unknown): s is string {
  return typeof s === "string" && ASSET_ID_RE.test(s);
}

/** The asset kind encoded in a well-formed `assetId`, or null when it is not one. */
export function kindFromAssetId(assetId: unknown): AssetKind | null {
  if (!isAssetId(assetId)) return null;
  return assetId.slice(0, assetId.indexOf(":")) as AssetKind;
}

const PROJECT_PK_PREFIX = "PROJECT#";
const USER_PK_PREFIX = "USER#";

export interface Key {
  PK: string;
  SK: string;
}

export function projectPk(pid: string): string {
  if (!isProjectId(pid)) throw new Error(`not a project id: ${JSON.stringify(pid)}`);
  return PROJECT_PK_PREFIX + pid;
}

export function userPk(sub: string): string {
  if (typeof sub !== "string" || sub.length === 0) throw new Error("actor sub must be a non-empty string");
  return USER_PK_PREFIX + sub;
}

/** `PROJECT#<pid> / META`: the Project meta item, or its id tombstone after purge. */
export function metaKey(pid: string): Key {
  return { PK: projectPk(pid), SK: META_SK };
}

/** `PROJECT#<pid> / MEMBER#<sub>`: one membership (the Owner has one too, `role = owner`). */
export function memberKey(pid: string, sub: string): Key {
  return { PK: projectPk(pid), SK: MEMBER_SK_PREFIX + nonEmpty(sub) };
}

/** `USER#<sub> / PROJECT#<pid>`: the user → Project mirror that listing queries. */
export function mirrorKey(sub: string, pid: string): Key {
  return { PK: userPk(sub), SK: MIRROR_SK_PREFIX + projectId(pid) };
}

/** `USER#<sub> / PERSONAL`: the pointer to the Actor's one Personal_Project. */
export function personalKey(sub: string): Key {
  return { PK: userPk(sub), SK: PERSONAL_SK };
}

/** `USER#<sub> / COUNTERS`: `ownedCount`, the non-Personal Projects the Actor owns. */
export function countersKey(sub: string): Key {
  return { PK: userPk(sub), SK: COUNTERS_SK };
}

/** `USER#<ownerSub> / NAME#<nameFold>`: the per-Owner name uniqueness guard. */
export function nameGuardKey(ownerSub: string, nameFold: string): Key {
  if (typeof nameFold !== "string" || nameFold.length === 0) throw new Error("name fold must be non-empty");
  return { PK: userPk(ownerSub), SK: NAME_SK_PREFIX + nameFold };
}

// ---------------------------------------------------------------- Project_Asset keys (ingestion §2)

/** `PROJECT#<pid> / ASSET#<assetId>`: one Project_Asset row (ingestion §2 Data Models). */
export function assetKey(pid: string, assetId: string): Key {
  return { PK: projectPk(pid), SK: ASSET_SK_PREFIX + nonEmptyAsset(assetId) };
}

/**
 * `PROJECT#<pid> / SRC#<sha256(bucket + "/" + key)>`: the source guard that holds at most one asset per
 * bucket and key, in any status (ingestion Req 2.14–2.16). Written by the Asset_Service Lambda; the
 * builder is the twin of the Python side so the two never diverge.
 */
export function srcGuardKey(pid: string, bucket: string, key: string): Key {
  if (typeof bucket !== "string" || !bucket || typeof key !== "string" || !key) {
    throw new Error("source guard needs a non-empty bucket and key");
  }
  return { PK: projectPk(pid), SK: SRC_SK_PREFIX + sha256Hex(`${bucket}/${key}`) };
}

/**
 * `PROJECT#<pid> / DNAME#<sha256(fold(name))>`: the Display_Name guard (ingestion Req 3.5, 3.6). The
 * hash keeps the sort key well under DynamoDB's 1,024-byte limit for a 255-code-point name. `fold` is
 * `NFC`-normalized then lower-cased, matching the TS name-fold convention (`names.property.test.ts`); the
 * Python twin uses `casefold`.
 */
export function dnameGuardKey(pid: string, displayName: string): Key {
  if (typeof displayName !== "string" || !displayName) throw new Error("display name must be non-empty");
  return { PK: projectPk(pid), SK: DNAME_SK_PREFIX + sha256Hex(foldName(displayName)) };
}

/** The Display_Name fold used by the DNAME guard: `NFC`-normalize then lower-case. */
export function foldName(name: string): string {
  return name.normalize("NFC").toLowerCase();
}

function sha256Hex(s: string): string {
  return createHash("sha256").update(s, "utf8").digest("hex");
}

function nonEmptyAsset(assetId: string): string {
  if (typeof assetId !== "string" || assetId.length === 0) throw new Error("assetId must be a non-empty string");
  return assetId;
}

/** The sub of a `MEMBER#<sub>` sort key, or null for any other sort key. */
export function subFromMemberSk(sk: string): string | null {
  return sk.startsWith(MEMBER_SK_PREFIX) && sk.length > MEMBER_SK_PREFIX.length
    ? sk.slice(MEMBER_SK_PREFIX.length)
    : null;
}

/** The project id of a `PROJECT#<pid>` mirror sort key, or null when it is not one. */
export function pidFromMirrorSk(sk: string): string | null {
  if (!sk.startsWith(MIRROR_SK_PREFIX)) return null;
  const pid = sk.slice(MIRROR_SK_PREFIX.length);
  return isProjectId(pid) ? pid : null;
}

/** The S3 prefix under which a Project's content lives. */
export function projectPrefix(pid: string): string {
  return `projects/${projectId(pid)}/`;
}

// ------------------------------------------------------------------------------------------ item shapes

export type ProjectStatus = "active" | "deleted";
export type ProjectRole = "owner" | "member";

export interface ProjectMetaItem extends Key {
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
  /** Owned by the ingestion spec. */
  activeAssetCount?: number;
  /** Owned by the ingestion spec; `absent` at create. */
  collectionState?: string;
}

/** What the ingestion purge leaves at `META` once every other item of the Project is gone. */
export interface ProjectTombstoneItem extends Key {
  tombstone: true;
  deletedAt: string;
  purgedAt: string;
}

export interface MemberItem extends Key {
  role: ProjectRole;
  /** Email or username at add time. */
  display: string;
  addedAt: string;
  addedBy: string;
}

export interface MirrorItem extends Key {
  role: ProjectRole;
}

export interface PersonalItem extends Key {
  projectId: string;
}

export interface CountersItem extends Key {
  ownedCount: number;
}

export interface NameGuardItem extends Key {
  projectId: string;
}

export function tombstoneItem(pid: string, deletedAt: string, purgedAt: string): ProjectTombstoneItem {
  return { ...metaKey(pid), tombstone: true, deletedAt, purgedAt };
}

export function isTombstone(item: Record<string, unknown> | null | undefined): item is ProjectTombstoneItem & Record<string, unknown> {
  return !!item && item.tombstone === true;
}

function nonEmpty(sub: string): string {
  if (typeof sub !== "string" || sub.length === 0) throw new Error("actor sub must be a non-empty string");
  return sub;
}

function projectId(pid: string): string {
  if (!isProjectId(pid)) throw new Error(`not a project id: ${JSON.stringify(pid)}`);
  return pid;
}
