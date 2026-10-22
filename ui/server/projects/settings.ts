// Project settings beyond the name: description and tags (stateless validation), and the indexing flag's
// reading rule. Indexing itself is turned ON through the Asset_Service (`enable_indexing`), which also
// queues every stored file, so it is not a registry write.

export const DESCRIPTION_MAX = 500;
export const TAGS_MAX = 10;
export const TAG_MAX = 32;

export type SettingsError = { code: "invalid_settings"; field: "description" | "tags" | "indexing" | "graph"; message: string };

const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f]/u; // tab and newline are allowed in a description

/** Trimmed description, or an error. Empty clears it. */
export function validateDescription(raw: unknown): string | SettingsError {
  if (typeof raw !== "string") return bad("description", "Description must be text");
  const v = raw.replace(/\r\n?/g, "\n").trim();
  if ([...v].length > DESCRIPTION_MAX) return bad("description", `Description must be at most ${DESCRIPTION_MAX} characters`);
  if (CONTROL.test(v)) return bad("description", "Description must not contain control characters");
  return v;
}

/** Trimmed, de-duplicated (case-insensitively, first spelling wins) tags, or an error. */
export function validateTags(raw: unknown): string[] | SettingsError {
  if (!Array.isArray(raw)) return bad("tags", "Tags must be a list");
  const out: string[] = [];
  const seen = new Set<string>();
  for (const t of raw) {
    if (typeof t !== "string") return bad("tags", "Each tag must be text");
    const v = t.trim().replace(/\s+/g, " ");
    if (!v) continue;
    if ([...v].length > TAG_MAX) return bad("tags", `Each tag must be at most ${TAG_MAX} characters`);
    if (/[\u0000-\u001f\u007f]/u.test(v)) return bad("tags", "Tags must not contain control characters");
    const fold = v.normalize("NFC").toLowerCase();
    if (seen.has(fold)) continue;
    seen.add(fold);
    out.push(v);
  }
  if (out.length > TAGS_MAX) return bad("tags", `At most ${TAGS_MAX} tags`);
  return out;
}

/**
 * Whether a Project ingests its files. New Projects are created with `indexing: false` (default OFF) and the
 * owner can only turn it ON. A META row without the attribute predates the setting and was already ingesting,
 * so it reads as ON — the same rule as `agent/src/ingestion/store.py::indexing_on`.
 */
export function indexingOn(item: Record<string, unknown> | undefined | null): boolean {
  if (!item || !("indexing" in item)) return true;
  return item.indexing === true;
}

/**
 * Whether a Project has a knowledge graph (`META.graph`). Unlike indexing, an absent attribute is OFF: no
 * Project had a graph before the setting, and a deploy without the graph feature never writes it. Same rule
 * as `agent/src/ingestion/store.py::graph_on`.
 */
export function graphOn(item: Record<string, unknown> | undefined | null): boolean {
  return !!item && item.graph === true;
}

export function isSettingsError(v: unknown): v is SettingsError {
  return !!v && typeof v === "object" && (v as { code?: unknown }).code === "invalid_settings";
}

function bad(field: SettingsError["field"], message: string): SettingsError {
  return { code: "invalid_settings", field, message };
}
