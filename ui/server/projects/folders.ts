// Project_Asset folders (media-harness-project-ingestion Req 1, 9, 14; design D20).
//
// An ASSET row may carry an optional string `folder`: a normalized relative path such as `Footage/Day 1`.
// Absent (or empty) is the project root. A folder is metadata only — the object key stays
// `projects/<pid>/<assetId>/<file>` — and in v1 folders come only from uploads and generated media.
//
// `normalizeFolder` is the TS twin of `agent/src/ingestion/folders.py::normalize_folder`; both run the
// shared fixture `agent/src/ingestion/fixtures/folder_cases.json`. The rule, in order:
//   1. `null`/`undefined` is the root; any other non-string is `not_a_string`; more than RAW_MAX code
//      points is `too_long`.
//   2. NFC, split on `/`, trim each segment (the TRIM whitespace set), drop empty segments (so leading,
//      trailing and doubled slashes normalize away). Nothing left → the root (`null`).
//   3. Per segment, in order: `.`/`..` → `dot_segment`; a Unicode "Other" char → `control_character`;
//      one of `[ ] @ #` → `forbidden_character`; more than 64 code points → `segment_too_long`.
//   4. More than 8 segments → `too_deep`; joined path over 255 code points → `too_long`.

export const FOLDER_SEGMENT_MAX = 64;
export const FOLDER_DEPTH_MAX = 8;
export const FOLDER_MAX = 255;
export const FOLDER_RAW_MAX = 4096;

export type FolderErrorReason =
  | "not_a_string"
  | "too_long"
  | "dot_segment"
  | "control_character"
  | "forbidden_character"
  | "segment_too_long"
  | "too_deep";

export interface FolderError {
  error: FolderErrorReason;
}

// Unicode White_Space, spelled out to match the Python twin exactly (JS `trim` also strips U+FEFF).
const TRIM_CLASS = "\\t\\n\\u000b\\u000c\\r \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const TRIM_RE = new RegExp(`^[${TRIM_CLASS}]+|[${TRIM_CLASS}]+$`, "gu");
const OTHER_RE = /\p{C}/u;
const FORBIDDEN_RE = /[[\]@#]/u;

function codePoints(s: string): number {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

/** `true` when `v` is a `{error}` result of `normalizeFolder`. */
export function isFolderError(v: unknown): v is FolderError {
  return !!v && typeof v === "object" && typeof (v as FolderError).error === "string";
}

/** The normalized folder for `raw`, `null` for the project root, or `{error}` naming the broken rule. */
export function normalizeFolder(raw: unknown): string | null | FolderError {
  if (raw === null || raw === undefined) return null;
  if (typeof raw !== "string") return { error: "not_a_string" };
  // A string of more than 2×RAW_MAX code units has more than RAW_MAX code points; skip counting it.
  if (raw.length > FOLDER_RAW_MAX * 2 || codePoints(raw) > FOLDER_RAW_MAX) return { error: "too_long" };
  const segments: string[] = [];
  for (const part of raw.normalize("NFC").split("/")) {
    const seg = part.replace(TRIM_RE, "");
    if (!seg) continue;
    if (seg === "." || seg === "..") return { error: "dot_segment" };
    if (OTHER_RE.test(seg)) return { error: "control_character" };
    if (FORBIDDEN_RE.test(seg)) return { error: "forbidden_character" };
    if (codePoints(seg) > FOLDER_SEGMENT_MAX) return { error: "segment_too_long" };
    segments.push(seg);
  }
  if (segments.length === 0) return null;
  if (segments.length > FOLDER_DEPTH_MAX) return { error: "too_deep" };
  const folder = segments.join("/");
  if (codePoints(folder) > FOLDER_MAX) return { error: "too_long" };
  return folder;
}

/** `folder + "/" + displayName` (just the name at the root): what a name search matches against. */
export function folderPathText(row: { folder?: unknown; displayName?: unknown }): string {
  const name = typeof row.displayName === "string" ? row.displayName : "";
  return typeof row.folder === "string" && row.folder ? `${row.folder}/${name}` : name;
}

/** The S3 user-metadata key (`x-amz-meta-harness-folder`) an upload carries its folder in. */
export const FOLDER_METADATA_KEY = "harness-folder";

/** A folder as an S3 metadata value: percent-encoded, so it is US-ASCII (the Asset_Service decodes it). */
export function encodeFolderMetadata(folder: string): string {
  return encodeURIComponent(folder);
}
