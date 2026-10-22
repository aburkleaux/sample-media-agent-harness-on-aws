/**
 * The "this Project is searchable" mark: a small magnifier after a Project's name, shown ONLY when search
 * indexing is on (files are analyzed, so Q&A and the Moments search can find what is in them). A Project
 * without indexing shows nothing -- the common case stays clean -- and the hover text says what the mark means.
 *
 * One component for every place a Project is named (sidebar, Projects header, the chat's project picker and
 * composer line), fed by `ProjectSummary.indexing` from the shared project list (`useProjectIndexing`).
 */
import React from 'react';
import SearchIcon from '../icons/SearchIcon';
import { useProjectsList } from '../../projects/useProjectsList';

export const SEARCH_INDEXED_LABEL = 'Search indexing on: files are analyzed and searchable';

export default function SearchIndexedGlyph({ indexing, size = 12, color }: {
  indexing: boolean | undefined;
  size?: number;
  color?: string;
}) {
  if (indexing !== true) return null;
  return (
    <span
      data-testid="search-indexed-glyph"
      role="img"
      aria-label={SEARCH_INDEXED_LABEL}
      title={SEARCH_INDEXED_LABEL}
      style={{ display: 'inline-flex', alignItems: 'center', flexShrink: 0, color: color ?? 'currentColor', opacity: 0.75 }}
    >
      <SearchIcon size={size} />
    </span>
  );
}

/** Whether `projectId` has search indexing on, from the shared project list (`undefined` while unknown). */
export function useProjectIndexing(projectId: string | null | undefined, enabled = true): boolean | undefined {
  const { projects } = useProjectsList({ enabled: enabled && !!projectId });
  if (!projectId || !projects) return undefined;
  return projects.find((p) => p.projectId === projectId)?.indexing;
}

/**
 * A Project name followed by the mark, the name still truncating with an ellipsis: `style` is the name's own
 * text style (font, colour, flex) and is applied to the wrapper, so swapping a `<span>{name}</span>` for this
 * keeps the row's layout.
 */
export function NameWithIndexGlyph({ name, indexing, style, glyphColor }: {
  name: string;
  indexing: boolean | undefined;
  style?: React.CSSProperties;
  glyphColor?: string;
}) {
  return (
    <span style={{ ...style, display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
      <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
      <SearchIndexedGlyph indexing={indexing} color={glyphColor} />
    </span>
  );
}
