import type { PublishedAssetSummary } from '../lab-api';

/** One row per lineage: the version to show, and how many exist behind it. */
export interface PublishedLineage {
  /** The version this row represents — `current` if marked, else the highest. */
  summary: PublishedAssetSummary;
  /** Total published versions in the lineage, including `summary`. */
  versionCount: number;
}

/**
 * Which version of a lineage represents it: `current` if the backend marks one,
 * otherwise the highest.
 *
 * The same rule the Python side applies (`lab/specialty_crud._pick_published_row`
 * — explicit version, then `current`, then highest non-deprecated). Note that
 * AGENTS.md records that rule as ALREADY duplicated in two Python places; this is
 * a display-only third, so keep it here and keep it small rather than growing a
 * second selection policy.
 *
 * `PublishedAssetSummary` carries no `deprecated` flag, so the non-deprecated part
 * of the rule cannot be applied — and does not need to be, since a deprecated
 * version is filtered out upstream before the list is built.
 */
export function pickRepresentative(
  versions: readonly PublishedAssetSummary[],
): PublishedAssetSummary {
  const current = versions.find(v => v.current);
  if (current) return current;
  return versions.reduce((best, v) => (v.version > best.version ? v : best));
}

/**
 * Collapse published versions into one entry per lineage, newest lineage first.
 *
 * Skills list one row per SKILL rather than one per version, matching the Agents
 * center. Grouping is by `assetId`, which IS the version-lineage id — v2 has no
 * separate `published_id`, which is exactly why every version of a lineage shares
 * it.
 *
 * Input order is not relied on: the representative is chosen by the rule above,
 * and lineages are then sorted by their representative's `updatedAt` so the list
 * is stable regardless of how the versions arrived.
 */
export function collapseToLineages(
  versions: readonly PublishedAssetSummary[],
): PublishedLineage[] {
  const byLineage = new Map<string, PublishedAssetSummary[]>();
  for (const version of versions) {
    const existing = byLineage.get(version.assetId);
    if (existing) existing.push(version);
    else byLineage.set(version.assetId, [version]);
  }

  return [...byLineage.values()]
    .map(group => ({
      summary: pickRepresentative(group),
      versionCount: group.length,
    }))
    .sort((a, b) => (b.summary.updatedAt || 0) - (a.summary.updatedAt || 0));
}
