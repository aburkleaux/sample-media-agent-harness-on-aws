import React from 'react';
import type { RendererProps } from '../types';

export interface ArtifactDescriptor {
  url: string;
  label: string;
  mime?: string;
  kind?: string;
}

const KIND_ICON: Record<string, string> = {
  image: '▣',
  video: '▶',
  audio: '≋',
  data: '{ }',
  file: '⬇',
};

/**
 * ArtifactCards — compact, uniform download row for the raw artifacts behind a
 * render. Shared so every template offers the same escape hatch to the
 * underlying files.
 */
export default function ArtifactCards({
  artifacts,
  theme: t,
  title = 'Artifacts',
  collapsible = false,
}: {
  artifacts: ArtifactDescriptor[];
  theme: RendererProps['theme'];
  title?: string;
  /** Hide the chips behind a "▸ title (N)" disclosure, collapsed by default.
   *  For dev-facing raw analysis files (facemetadata.json, …) that clutter a
   *  clean render but should stay reachable. */
  collapsible?: boolean;
}) {
  const [open, setOpen] = React.useState<boolean>(!collapsible);
  if (!artifacts || artifacts.length === 0) return null;
  const headerBase: React.CSSProperties = {
    fontSize: 10,
    fontWeight: 600,
    color: t.colors.textSecondary,
    textTransform: 'uppercase',
    letterSpacing: 0.4,
    margin: '0 0 6px',
  };
  return (
    <div style={{ padding: '0 14px' }} data-testid="artifact-cards">
      {collapsible ? (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          style={{ ...headerBase, display: 'flex', alignItems: 'center', gap: 5,
                   background: 'transparent', border: 'none', padding: 0, cursor: 'pointer' }}
        >
          <span aria-hidden="true" style={{ fontSize: 9 }}>{open ? '▾' : '▸'}</span>
          {title} ({artifacts.length})
        </button>
      ) : (
        <div style={headerBase}>{title}</div>
      )}
      {open && (
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {artifacts.map((artifact) => (
          <a
            key={artifact.url}
            href={artifact.url}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '4px 8px',
              borderRadius: 6,
              border: `1px solid ${t.colors.border}`,
              background: t.colors.background,
              color: t.colors.textPrimary,
              fontSize: 11,
              textDecoration: 'none',
              maxWidth: 240,
            }}
          >
            <span style={{ color: t.colors.textSecondary }}>
              {KIND_ICON[artifact.kind || 'file'] || KIND_ICON.file}
            </span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {artifact.label}
            </span>
          </a>
        ))}
      </div>
      )}
    </div>
  );
}
