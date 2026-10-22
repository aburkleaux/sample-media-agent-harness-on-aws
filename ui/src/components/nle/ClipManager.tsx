/**
 * ClipManager — Renders the clips panel with timecodes, click-to-seek, and Open Render action.
 *
 * Displays clips from the manifest in array order with:
 * - Name, start/end timecodes (MM:SS.mmm), computed duration
 * - Click-to-seek: seeks playhead to clip start, highlights clip range on track
 * - Dangling sourceTrack detection: shows ⚠️ icon, disables click-to-seek
 * - "Open Render" action: emits InteractionEvent with clip data
 * - Empty clips array: displays empty-state message
 * - Invalid clips (start < 0 or end <= start): filtered out before rendering
 */

import React, { useMemo } from 'react';
import type { ManifestClip, ManifestTrack } from './manifestParser';
import type { InteractionEvent } from '../renderer/types';
import { formatTimecode } from './formatTimecode';
import IconBase from '../icons/IconBase';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ClipManagerProps {
  clips: ManifestClip[];
  tracks: ManifestTrack[];
  onSeek: (time: number) => void;
  onOpenRender: (clip: ManifestClip) => void;
  onSelect: (clipId: string) => void;
  selectedClipId?: string | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Returns true if a clip has valid start/end values (start >= 0 and end > start). */
function isValidClip(clip: ManifestClip): boolean {
  return clip.start >= 0 && clip.end > clip.start;
}

/** Returns true if the clip's sourceTrack exists in the tracks array. */
function hasValidSourceTrack(clip: ManifestClip, trackIds: Set<string>): boolean {
  return trackIds.has(clip.sourceTrack);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export const ClipManager: React.FC<ClipManagerProps> = ({
  clips,
  tracks,
  onSeek,
  onOpenRender,
  onSelect,
  selectedClipId,
}) => {
  // Build a set of track IDs for O(1) lookup
  const trackIds = useMemo(() => new Set(tracks.map((t) => t.id)), [tracks]);

  // Filter out invalid clips (start < 0 or end <= start)
  const validClips = useMemo(() => clips.filter(isValidClip), [clips]);

  // Handle click-to-seek on a clip row
  const handleClipClick = (clip: ManifestClip) => {
    if (!hasValidSourceTrack(clip, trackIds)) return;
    onSelect(clip.id);
    onSeek(clip.start);
  };

  // Handle "Open Render" action
  const handleOpenRender = (e: React.MouseEvent, clip: ManifestClip) => {
    e.stopPropagation();
    onOpenRender(clip);
  };

  // Empty state
  if (validClips.length === 0) {
    return (
      <div style={styles.container}>
        <div style={styles.header}>
          <span style={styles.headerTitle}>CLIPS</span>
        </div>
        <div style={styles.emptyState}>No clips available</div>
      </div>
    );
  }

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <span style={styles.headerTitle}>CLIPS</span>
      </div>
      <div style={styles.clipList} role="list">
        {validClips.map((clip) => {
          const isDangling = !hasValidSourceTrack(clip, trackIds);
          const isSelected = selectedClipId === clip.id;
          const duration = clip.end - clip.start;

          return (
            <div
              key={clip.id}
              role="listitem"
              style={{
                ...styles.clipRow,
                ...(isSelected ? styles.clipRowSelected : {}),
                ...(isDangling ? styles.clipRowDangling : {}),
              }}
              onClick={() => handleClipClick(clip)}
              aria-disabled={isDangling}
              data-testid={`clip-row-${clip.id}`}
            >
              <div style={styles.clipInfo}>
                <div style={styles.clipNameRow}>
                  {isDangling && (
                    <span
                      style={styles.warningIcon}
                      title="Source track not found"
                      aria-label="Warning: source track not found"
                    >
                      <IconBase size={14}>
                        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
                        <path d="M12 9v4" />
                        <path d="M12 17h.01" />
                      </IconBase>
                    </span>
                  )}
                  <span style={styles.clipName}>{clip.name}</span>
                </div>
                <div style={styles.timecodeRow}>
                  <span style={styles.timecode}>
                    {formatTimecode(clip.start)} → {formatTimecode(clip.end)}
                  </span>
                  <span style={styles.duration}>{formatTimecode(duration)}</span>
                </div>
              </div>
              <button
                style={styles.openRenderButton}
                onClick={(e) => handleOpenRender(e, clip)}
                title="Open Render"
                aria-label={`Open Render for ${clip.name}`}
              >
                OPEN RENDER
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    flexDirection: 'column',
    background: 'var(--color-surface, #1e1e1e)',
    borderRadius: 8,
    overflow: 'hidden',
    border: '1px solid var(--color-border, #333)',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '8px 12px',
    borderBottom: '1px solid var(--color-border, #333)',
    background: 'var(--color-sidebar-bg, #252525)',
  },
  headerTitle: {
    fontSize: 11,
    fontWeight: 600,
    letterSpacing: '0.5px',
    color: 'var(--color-text-secondary, #aaa)',
  },
  clipList: {
    display: 'flex',
    flexDirection: 'column',
    overflowY: 'auto',
    maxHeight: 300,
  },
  clipRow: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '8px 12px',
    cursor: 'pointer',
    borderBottom: '1px solid var(--color-border, #2a2a2a)',
    transition: 'background 150ms ease-in-out',
  },
  clipRowSelected: {
    background: 'var(--color-active-bg, rgba(0, 122, 204, 0.15))',
    borderLeft: '3px solid var(--color-primary, #007acc)',
  },
  clipRowDangling: {
    opacity: 0.5,
    cursor: 'not-allowed',
  },
  clipInfo: {
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
    flex: 1,
    minWidth: 0,
  },
  clipNameRow: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
  },
  clipName: {
    fontSize: 13,
    fontWeight: 500,
    color: 'var(--color-text, #e0e0e0)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  warningIcon: {
    display: 'inline-flex',
    fontSize: 14,
    flexShrink: 0,
  },
  timecodeRow: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
  },
  timecode: {
    fontSize: 11,
    fontFamily: "'SF Mono', 'Fira Code', 'Consolas', monospace",
    color: 'var(--color-text-secondary, #888)',
  },
  duration: {
    fontSize: 11,
    fontFamily: "'SF Mono', 'Fira Code', 'Consolas', monospace",
    color: 'var(--color-text-secondary, #888)',
    marginLeft: 'auto',
  },
  openRenderButton: {
    padding: '4px 10px',
    fontSize: 10,
    fontWeight: 600,
    letterSpacing: '0.3px',
    color: '#fff',
    background: '#1e8449',
    border: 'none',
    borderRadius: 4,
    cursor: 'pointer',
    whiteSpace: 'nowrap',
    marginLeft: 12,
    flexShrink: 0,
  },
  emptyState: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '24px 12px',
    fontSize: 13,
    color: 'var(--color-text-secondary, #888)',
    fontStyle: 'italic',
  },
};

export default ClipManager;
