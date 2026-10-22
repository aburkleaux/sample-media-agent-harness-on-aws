/**
 * Where a project file is filed: a folder glyph and the path, each segment opening that folder in the Assets grid.
 * One component for every place a file shows outside its folder (mock: `.playwright-mcp/mock/location.html`):
 *   - `line`  — cards and tiles: the faintest line on the card; a long path clips from the LEFT, so the nearest
 *               folder (the one you care about) stays visible. The whole path is the tooltip.
 *   - `crumb` — headers (expanded view, timeline): `<project> / … / <folder>`, the last segment emphasised.
 *
 * Renders nothing while the folder is unknown (never a placeholder that jumps), and nothing when `hideIn` names the
 * folder already on screen. A file in no folder says "Top level", so no line never means "unknown".
 */
import React from 'react';
import { useAssetFolder } from '../../projects/assetFolders';
import '../../projects/assetUrlBatcher'; // registers the folder store's fetcher
import { openProject } from '../../projects/openProject';
import { theme } from '../../styles/tokens';
import FolderIcon from '../icons/FolderIcon';

export const TOP_LEVEL_LABEL = 'Top level';

export interface FileLocationProps {
  projectId: string | null | undefined;
  /** Look the folder up by asset id (the shared store)… */
  assetId?: string | null;
  /** …or give it directly (`""` = the top level). Wins over `assetId`. */
  folder?: string;
  variant?: 'line' | 'crumb';
  /** The crumb's root segment (the project's name). */
  projectName?: string;
  /** Hide when the file is in this folder (the folder already on screen). */
  hideIn?: string;
  /** On a dark surface (a lightbox caption). */
  dark?: boolean;
  /** Prefix for the crumb (e.g. "All in" above a gallery). */
  lead?: string;
}

export function FileLocation(p: FileLocationProps) {
  const looked = useAssetFolder(p.folder === undefined ? p.projectId : null, p.folder === undefined ? p.assetId : null);
  const folder = p.folder ?? looked;
  if (!p.projectId || folder === undefined) return null;
  if (p.hideIn !== undefined && p.hideIn === folder) return null;
  const pid = p.projectId;
  const parts = folder ? folder.split('/').filter(Boolean) : [];
  const open = (i: number) => (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    openProject(pid, i < 0 ? '' : parts.slice(0, i + 1).join('/'));
  };
  const tip = `In ${parts.length ? parts.join(' / ') : TOP_LEVEL_LABEL} — open the folder`;
  const muted = p.dark ? '#9ca3af' : theme.colors.textTertiary;
  const leaf = p.dark ? '#e5e7eb' : theme.colors.textSecondary;

  if (p.variant === 'crumb') {
    return (
      <span style={{ ...crumbStyle, color: muted }} data-testid="file-location" data-folder={folder} title={tip}>
        <FolderIcon size={13} />
        {p.lead && <span>{p.lead}</span>}
        {p.projectName && (
          <>
            <button type="button" style={{ ...segStyle, color: muted }} onClick={open(-1)}>{p.projectName}</button>
            {parts.length > 0 && <span style={sepStyle} aria-hidden="true">/</span>}
          </>
        )}
        {!p.projectName && parts.length === 0 && (
          <button type="button" style={{ ...segStyle, color: leaf, fontWeight: 600 }} onClick={open(-1)}>{TOP_LEVEL_LABEL}</button>
        )}
        {parts.map((name, i) => (
          <React.Fragment key={i}>
            {i > 0 && <span style={sepStyle} aria-hidden="true">/</span>}
            <button type="button" onClick={open(i)}
              style={{ ...segStyle, ...(i === parts.length - 1
                // The folder you care about gives way last: parents shrink first when the crumb is narrow.
                ? { color: p.dark ? leaf : theme.colors.textPrimary, fontWeight: 600, flexShrink: 0.2, minWidth: 48 }
                : { color: muted }) }}>
              {name}
            </button>
          </React.Fragment>
        ))}
      </span>
    );
  }

  return (
    <span style={{ ...lineStyle, color: muted }} data-testid="file-location" data-folder={folder} title={tip}>
      <FolderIcon size={12} />
      {/* rtl clips from the left; the bdi keeps the path itself reading left to right */}
      <span style={clipStyle}>
        <bdi style={{ direction: 'ltr' }}>
          {parts.length === 0
            ? <button type="button" style={{ ...lineSegStyle, color: leaf }} onClick={open(-1)}>{TOP_LEVEL_LABEL}</button>
            : parts.map((name, i) => (
              <React.Fragment key={i}>
                {i > 0 && ' / '}
                <button type="button" onClick={open(i)}
                  style={{ ...lineSegStyle, ...(i === parts.length - 1 ? { color: leaf } : null) }}>{name}</button>
              </React.Fragment>
            ))}
        </bdi>
      </span>
    </span>
  );
}

export default FileLocation;

const lineStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 4, minWidth: 0, maxWidth: '100%', marginTop: 2,
  fontSize: 'var(--fs-xs)', whiteSpace: 'nowrap',
};
const clipStyle: React.CSSProperties = { overflow: 'hidden', textOverflow: 'ellipsis', direction: 'rtl', textAlign: 'left', minWidth: 0 };
const lineSegStyle: React.CSSProperties = {
  border: 'none', background: 'none', padding: 0, margin: 0, font: 'inherit', color: 'inherit', cursor: 'pointer',
};
const crumbStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 2, minWidth: 0, fontSize: 'var(--fs-xs)', whiteSpace: 'nowrap', overflow: 'hidden',
};
const segStyle: React.CSSProperties = {
  border: 'none', background: 'none', padding: '1px 4px', margin: 0, borderRadius: 4, font: 'inherit', cursor: 'pointer',
  overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0,
};
const sepStyle: React.CSSProperties = { color: theme.colors.borderStrong };
