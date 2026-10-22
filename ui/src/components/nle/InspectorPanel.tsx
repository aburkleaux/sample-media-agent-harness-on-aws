/**
 * InspectorPanel — Displays metadata for the currently selected item.
 *
 * Shows clip metadata on clip selection (name, duration, source track, start/end timecodes)
 * and cue metadata on cue selection (text, speaker, time range).
 * When nothing is selected, shows an empty-state message.
 *
 * This component is stateless — it renders whatever selection prop it receives.
 * The parent (NLE_View) manages which item is selected.
 *
 * Validates: Requirements 9.2, 9.3, 9.9
 */

import React from 'react';
import type { ManifestClip, CaptionCue } from './manifestParser';
import { formatTimecode } from './formatTimecode';

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

export interface InspectorPanelProps {
  selection:
    | { type: 'clip'; clip: ManifestClip }
    | { type: 'cue'; cue: CaptionCue }
    | null;
}

// ---------------------------------------------------------------------------
// Speaker color palette (WCAG 2.1 AA: ≥4.5:1 against white text)
// ---------------------------------------------------------------------------

const SPEAKER_COLORS = [
  '#1a5276', // dark blue
  '#7b241c', // dark red
  '#1e8449', // dark green
  '#6c3483', // dark purple
  '#784212', // dark brown
  '#1b4f72', // dark teal
] as const;

function getSpeakerColor(speaker: string): string {
  // Simple hash based on character codes to assign a consistent color
  let hash = 0;
  for (let i = 0; i < speaker.length; i++) {
    hash = (hash * 31 + speaker.charCodeAt(i)) | 0;
  }
  return SPEAKER_COLORS[Math.abs(hash) % SPEAKER_COLORS.length];
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export const InspectorPanel: React.FC<InspectorPanelProps> = ({ selection }) => {
  return (
    <div
      className="inspector-panel"
      role="region"
      aria-label="Inspector"
    >
      <header className="inspector-panel__header">INSPECTOR</header>
      <div className="inspector-panel__content">
        {selection === null && <EmptyState />}
        {selection?.type === 'clip' && <ClipInspector clip={selection.clip} />}
        {selection?.type === 'cue' && <CueInspector cue={selection.cue} />}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

const EmptyState: React.FC = () => (
  <div className="inspector-panel__empty" role="status">
    Select an item on the timeline to view its details
  </div>
);

// ---------------------------------------------------------------------------
// Clip inspector
// ---------------------------------------------------------------------------

interface ClipInspectorProps {
  clip: ManifestClip;
}

const ClipInspector: React.FC<ClipInspectorProps> = ({ clip }) => {
  const duration = clip.end - clip.start;

  return (
    <section className="inspector-panel__section" aria-label="Selection">
      <h3 className="inspector-panel__section-header">SELECTION</h3>
      <dl className="inspector-panel__fields">
        <div className="inspector-panel__field">
          <dt>In</dt>
          <dd>{formatTimecode(clip.start)}</dd>
        </div>
        <div className="inspector-panel__field">
          <dt>Out</dt>
          <dd>{formatTimecode(clip.end)}</dd>
        </div>
        <div className="inspector-panel__field">
          <dt>Duration</dt>
          <dd>{formatTimecode(duration)}</dd>
        </div>
        <div className="inspector-panel__field">
          <dt>Source Track</dt>
          <dd>{clip.sourceTrack}</dd>
        </div>
      </dl>
    </section>
  );
};

// ---------------------------------------------------------------------------
// Cue inspector
// ---------------------------------------------------------------------------

interface CueInspectorProps {
  cue: CaptionCue;
}

const CueInspector: React.FC<CueInspectorProps> = ({ cue }) => {
  const speakerColor = getSpeakerColor(cue.speaker);

  return (
    <section className="inspector-panel__section" aria-label="Active Sentence">
      <h3 className="inspector-panel__section-header">ACTIVE SENTENCE</h3>
      <dl className="inspector-panel__fields">
        <div className="inspector-panel__field">
          <dt>Speaker</dt>
          <dd>
            <span
              className="inspector-panel__speaker-badge"
              style={{ backgroundColor: speakerColor, color: '#ffffff' }}
            >
              {cue.speaker}
            </span>
          </dd>
        </div>
        <div className="inspector-panel__field">
          <dt>Text</dt>
          <dd className="inspector-panel__cue-text">{cue.text}</dd>
        </div>
        <div className="inspector-panel__field">
          <dt>Time Range</dt>
          <dd>{formatTimecode(cue.start)} → {formatTimecode(cue.end)}</dd>
        </div>
      </dl>
    </section>
  );
};

export default InspectorPanel;
