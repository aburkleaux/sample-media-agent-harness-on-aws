/**
 * Document recall -- the TypeScript twin of agent/src/project_search/document_recall.py (read its docstring for
 * the why). A plain text query that could want documents also runs as `<term> #<DOCUMENT_SCOPE>`; only that
 * call's NEW document hits are merged in by final_score, the plain result first on ties. Both twins are pinned
 * by infra/lib/projects/fixtures/document-recall-cases.json (pattern + every case), so they cannot drift.
 *
 * Not "default every query to the document scope": that returns only scene hits and drops the shot frames
 * visual queries rank on (and the Moments cards and Find similar need), utterances and topics.
 */

/** The scope document pages are indexed under (agent: ingestion/documents.py DOCUMENT_SCOPE; fixture-pinned). */
export const DOCUMENT_SCOPE = "scene";

/** A query that already restricts scope or uses a field operator. Byte-identical to the fixture's pattern. */
export const SCOPED_QUERY_PATTERN = "#(?:shot|scene|utterance|topic)\\b|\\b(?:says|shows):";
const SCOPED_QUERY = new RegExp(SCOPED_QUERY_PATTERN, "i");

const DOCUMENT_ID_PREFIX = "document:";

/** The document-scope companion for a plain text query, else null. */
export function companionTerm(query: string, kinds: readonly string[] | null): string | null {
  if (typeof query !== "string" || !query.trim() || SCOPED_QUERY.test(query)) return null;
  if (kinds && !kinds.includes("document")) return null;
  return `${query} #${DOCUMENT_SCOPE}`;
}

const score = (h: any) => (typeof h?.final_score === "number" ? h.final_score : 0);
const key = (h: any) => `${h?.asset_id}|${h?.start_ms}|${h?.end_ms}`;

/** `primary` with the companion's new DOCUMENT hits merged in by final_score (primary first on ties). */
export function mergeDocumentHits(primary: readonly unknown[], companion: readonly unknown[]): unknown[] {
  const have = new Set(primary.map(key));
  const docs = companion.filter((h: any) =>
    typeof h?.asset_id === "string" && h.asset_id.startsWith(DOCUMENT_ID_PREFIX) && !have.has(key(h)));
  if (!docs.length) return [...primary];
  const out: unknown[] = [];
  let i = 0;
  let j = 0;
  while (i < primary.length || j < docs.length) {
    if (j >= docs.length || (i < primary.length && score(primary[i]) >= score(docs[j]))) out.push(primary[i++]);
    else out.push(docs[j++]);
  }
  return out;
}
