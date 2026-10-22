/**
 * The asset kinds a project search can return — ONE list for the proxy's Moments search route, the Moments
 * tab's chips and cards, and citations. Pinned to `infra/lib/projects/fixtures/search-kinds.json` (the agent's
 * `project_search.compact.ASSET_KINDS` asserts the same file). `document` (a PDF, indexed page by page) was
 * missing from every copy after PDFs became indexed, so each layer dropped document hits silently.
 */
export const SEARCH_KINDS = ['video', 'image', 'audio', 'document'] as const;
export type SearchKind = (typeof SEARCH_KINDS)[number];

/** Kinds a player opens (timeline / image viewer). A document opens in the browser's PDF viewer instead. */
export type PlayableKind = Exclude<SearchKind, 'document'>;

export function isSearchKind(v: unknown): v is SearchKind {
  return typeof v === 'string' && (SEARCH_KINDS as readonly string[]).includes(v);
}
