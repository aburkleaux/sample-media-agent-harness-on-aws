/**
 * The `New project` page of the Projects_Surface (media-harness-projects-access; Req 1.1, 10.4; Project
 * settings).
 *
 * A full page, like the Agents and Skills create flows (`CenterEditor`): a `← Back to Projects` bar with the
 * title, then the form. While it is open the surface shows nothing else — it used to sit above the selected
 * project's assets, which read as part of that project.
 *
 * The sidebar's `+ New project` (the shared New_Item_Action in `projects` mode) is the only way to open
 * it. The name is trimmed and must be non-empty; Enter in the name submits; Escape goes back. Description,
 * tags, the Indexing switch, and (where the graph is deployed) the Knowledge graph switch — ON by default —
 * are optional. Indexing starts OFF and can only ever be turned ON (here or
 * later in Settings), because it analyzes every file. `onCreate` resolves with an error message to show
 * inline, or `null` on success, after which the host closes the page and selects the new Project.
 */
import React, { useState } from 'react';
import { theme } from '../../styles/tokens';
import Button from '../ui/Button';
import CapabilitySwitch from '../settings/CapabilitySwitch';
import { peekGraphEnabled } from '../../projects/api';
import type { NewProjectSettings } from '../../projects/api';

export interface ProjectCreateFormProps {
  /** Resolves with an error message to show inline, or `null` on success. */
  onCreate(name: string, settings: NewProjectSettings): Promise<string | null>;
  onCancel(): void;
}

/** "a, b ,  c" → ["a", "b", "c"]; the server trims, de-duplicates, and caps. */
export function parseTags(raw: string): string[] {
  return raw.split(',').map((t) => t.trim()).filter(Boolean);
}

export const INDEXING_NOTE =
  'Indexing analyzes every file so you can search inside the media. It cannot be turned off later. '
  + 'Without it, files are stored and you can still find them by name.';

export const GRAPH_NOTE =
  'Builds a knowledge graph of the people, places, and topics in this project, with a starter sample graph '
  + 'to explore. You can view it in the Graph tab. It cannot be turned off later.';

export default function ProjectCreateForm({ onCreate, onCancel }: ProjectCreateFormProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [tags, setTags] = useState('');
  const [indexing, setIndexing] = useState(false);
  // Shown and sent only where the graph stack is deployed (`/config.graphEnabled`); ON by default.
  const [showGraph] = useState(peekGraphEnabled);
  const [graph, setGraph] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const next = name.trim();
    if (!next || busy) return;
    setBusy(true);
    const settings: NewProjectSettings = { indexing };
    if (showGraph) settings.graph = graph;
    if (description.trim()) settings.description = description.trim();
    const parsed = parseTags(tags);
    if (parsed.length) settings.tags = parsed;
    const err = await onCreate(next, settings);
    setBusy(false);
    if (err) setError(err);
  };

  return (
    <div style={pageStyle} data-testid="project-create-page">
      <div style={topBarStyle}>
        <button type="button" onClick={onCancel} style={backButtonStyle} disabled={busy}>
          ← Back to Projects
        </button>
        <span style={titleStyle}>New project</span>
      </div>

      <div style={contentStyle}>
        <form
          style={formStyle}
          data-testid="project-create-form"
          aria-label="New project"
          onSubmit={(e) => { e.preventDefault(); void submit(); }}
          onKeyDown={(e) => { if (e.key === 'Escape' && !busy) onCancel(); }}
        >
          <section style={sectionStyle} aria-labelledby="new-project-details">
            <h3 id="new-project-details" style={headingStyle}>Details</h3>
            <label style={labelStyle} htmlFor="new-project-name">Name</label>
            <input
              id="new-project-name"
              style={inputStyle}
              value={name}
              onChange={(e) => { setName(e.target.value); setError(null); }}
              placeholder="Project name"
              aria-label="New project name"
              autoFocus
              disabled={busy}
            />
            <label style={labelStyle} htmlFor="new-project-description">Description (optional)</label>
            <textarea
              id="new-project-description"
              style={{ ...inputStyle, minHeight: 96, resize: 'vertical' }}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this project is for. The agent reads it when you chat in this project."
              aria-label="New project description"
              maxLength={500}
              disabled={busy}
            />
            <label style={labelStyle} htmlFor="new-project-tags">Tags (optional)</label>
            <input
              id="new-project-tags"
              style={inputStyle}
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="Separated by commas"
              aria-label="New project tags"
              disabled={busy}
            />
          </section>

          <section style={sectionStyle} aria-labelledby="new-project-indexing">
            <h3 id="new-project-indexing" style={headingStyle}>Search indexing</h3>
            <div style={indexRowStyle}>
              <CapabilitySwitch checked={indexing} onChange={setIndexing} label="Indexing"
                ariaLabel="Index files for search" disabled={busy} />
            </div>
            <p style={noteStyle} id="new-project-indexing-note">{INDEXING_NOTE}</p>
          </section>

          {showGraph && (
            <section style={sectionStyle} aria-labelledby="new-project-graph">
              <h3 id="new-project-graph" style={headingStyle}>Knowledge graph</h3>
              <div style={indexRowStyle}>
                <CapabilitySwitch checked={graph} onChange={setGraph} label="Knowledge graph"
                  ariaLabel="Build a knowledge graph" disabled={busy} />
              </div>
              <p style={noteStyle} id="new-project-graph-note">{GRAPH_NOTE}</p>
            </section>
          )}

          {error && <p style={errorStyle} role="alert">{error}</p>}
          <div>
            <Button type="submit" variant="primary" size="md" disabled={busy || !name.trim()}
              busy={busy} busyLabel="Creating…">
              Create project
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// The top bar matches `CenterEditor` (Agents / Skills create pages); the fields match `ProjectSettings`.
const pageStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 };
const topBarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '8px 16px',
  borderBottom: `1px solid ${theme.colors.border}`,
  flexShrink: 0,
};
const backButtonStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.primary,
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  padding: '4px 0',
  flexShrink: 0,
};
const titleStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.heading,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};
const contentStyle: React.CSSProperties = { flex: 1, minHeight: 0, overflowY: 'auto', padding: '32px 24px' };
const formStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 28,
  maxWidth: 640,
  margin: '0 auto',
};
const sectionStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 8 };
const headingStyle: React.CSSProperties = {
  margin: '0 0 4px', fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary,
};
const labelStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, marginTop: 4 };
const inputStyle: React.CSSProperties = {
  padding: '8px 10px',
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.background,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  fontFamily: 'inherit',
};
const indexRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center' };
const noteStyle: React.CSSProperties = { margin: 0, color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)' };
const errorStyle: React.CSSProperties = { color: theme.colors.error, fontSize: 'var(--fs-sm)', margin: 0 };
