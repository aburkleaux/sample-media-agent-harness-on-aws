/**
 * Project settings page (Projects_Surface body when `Settings` is pressed).
 *
 *   * Details — description (the agent reads it with the active project) and tags. Owner edits; a member
 *     sees them read-only. Renaming and deleting stay in the header's menu.
 *   * Search indexing — a switch, OFF by default. OFF: files are stored and findable by name, nothing is
 *     analyzed. The owner flips it ON once; a confirmation says how many stored files will be analyzed and
 *     that it cannot be undone. ON: the switch shows on and is locked.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { theme } from '../../styles/tokens';
import Button from '../ui/Button';
import ConfirmDialog from '../ConfirmDialog';
import CapabilitySwitch from '../settings/CapabilitySwitch';
import {
  enableIndexing,
  getProject,
  updateProjectSettings,
  type ProjectMeta,
  type ProjectsResult,
} from '../../projects/api';
import { parseTags } from './ProjectCreateForm';

export interface ProjectSettingsProps {
  project: { id: string; name: string; personal: boolean };
  role: 'owner' | 'member';
  onNotFound(): void;
  /** After a settings change, so the host can re-read (e.g. the asset list starts polling queued files). */
  onChanged?(): void;
  /** The Project's indexing state (absent on the META = ON) each time it is read or turned on. */
  onIndexingKnown?(indexing: boolean): void;
}

function message(r: Exclude<ProjectsResult<unknown>, { ok: true }>): string {
  if (r.kind === 'disabled') return 'Projects are not enabled';
  if (r.kind === 'access_lost') return 'Access changed; please retry';
  if (r.kind === 'not_found') return 'Project not found';
  return r.message;
}

export default function ProjectSettings({ project, role, onNotFound, onChanged, onIndexingKnown }: ProjectSettingsProps) {
  const isOwner = role === 'owner';
  const [meta, setMeta] = useState<ProjectMeta | null>(null);
  const [description, setDescription] = useState('');
  const [tags, setTags] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const apply = useCallback((m: ProjectMeta) => {
    setMeta(m);
    setDescription(m.description ?? '');
    setTags((m.tags ?? []).join(', '));
  }, []);

  useEffect(() => {
    let live = true;
    setMeta(null);
    setError(null);
    setSaved(null);
    void getProject(project.id).then((r) => {
      if (!live) return;
      if (r.ok) {
        apply(r.value.project);
        onIndexingKnown?.(r.value.project.indexing !== false);
      } else if (r.kind === 'not_found') onNotFound();
      else setError(message(r));
    });
    return () => { live = false; };
  }, [project.id, apply, onNotFound]);

  const indexing = meta ? meta.indexing !== false : null;
  const stored = meta?.storedCount;
  const dirty = !!meta && (description.trim() !== (meta.description ?? '')
    || parseTags(tags).join('\u0000') !== (meta.tags ?? []).join('\u0000'));

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(null);
    const r = await updateProjectSettings(project.id, { description: description.trim(), tags: parseTags(tags) });
    setBusy(false);
    if (r.ok) {
      apply({ ...meta!, ...r.value });
      setSaved('Saved');
      onChanged?.();
    } else if (r.kind === 'not_found') onNotFound();
    else setError(message(r));
  };

  const turnOn = async () => {
    setBusy(true);
    setError(null);
    const r = await enableIndexing(project.id);
    setBusy(false);
    setConfirming(false);
    if (r.ok) {
      setMeta((m) => (m ? { ...m, indexing: true, storedCount: 0 } : m));
      onIndexingKnown?.(true);
      setSaved(r.value.queued
        ? `Indexing is on. ${r.value.queued} file${r.value.queued === 1 ? '' : 's'} queued for analysis.`
        : 'Indexing is on.');
      onChanged?.();
    } else if (r.kind === 'not_found') onNotFound();
    else setError(message(r));
  };

  if (!meta && !error) return <p style={mutedStyle}>Loading settings…</p>;

  return (
    <div style={pageStyle} data-testid="project-settings">
      {error && <p style={errorStyle} role="alert">{error}</p>}
      {saved && <p style={savedStyle} role="status">{saved}</p>}

      <section style={sectionStyle} aria-labelledby="project-settings-details">
        <h3 id="project-settings-details" style={headingStyle}>Details</h3>
        <label style={labelStyle} htmlFor="project-settings-description">Description</label>
        <textarea
          id="project-settings-description"
          style={{ ...inputStyle, minHeight: 72, resize: 'vertical' }}
          value={description}
          onChange={(e) => { setDescription(e.target.value); setSaved(null); }}
          maxLength={500}
          readOnly={!isOwner}
          disabled={busy}
          placeholder={isOwner ? 'What this project is for. The agent reads it when you chat in this project.' : ''}
        />
        <label style={labelStyle} htmlFor="project-settings-tags">Tags</label>
        <input
          id="project-settings-tags"
          style={inputStyle}
          value={tags}
          onChange={(e) => { setTags(e.target.value); setSaved(null); }}
          readOnly={!isOwner}
          disabled={busy}
          placeholder={isOwner ? 'Separated by commas' : ''}
        />
        {isOwner ? (
          <div><Button variant="primary" size="md" onClick={() => void save()} disabled={busy || !dirty}>Save</Button></div>
        ) : (
          <p style={mutedStyle}>Only the project owner can change these settings.</p>
        )}
      </section>

      <section style={sectionStyle} aria-labelledby="project-settings-indexing">
        <h3 id="project-settings-indexing" style={headingStyle}>Search indexing</h3>
        {/* A switch, like the New project page. Off: the owner flips it, which asks first. On: it stays on and
            is locked, since indexing cannot be turned off. */}
        <div>
          <CapabilitySwitch
            checked={!!indexing}
            onChange={(next) => { if (next) setConfirming(true); }}
            label="Indexing"
            ariaLabel={indexing ? "Indexing is on and can't be turned off" : 'Turn on indexing'}
            disabled={busy || !!indexing || !isOwner}
          />
        </div>
        <p style={bodyStyle} data-testid="indexing-state">
          {indexing ? (
            <><strong>On.</strong> Every file is analyzed so you and the agent can search inside the media.
            Indexing can't be turned off.</>
          ) : (
            <><strong>Off.</strong> Files are stored and you can find them by name, but nothing is analyzed,
            so you can't search inside the media yet.</>
          )}
        </p>
        {!indexing && !isOwner && <p style={mutedStyle}>Only the project owner can turn on indexing.</p>}
      </section>

      <ConfirmDialog
        open={confirming}
        title="Turn on indexing?"
        message={
          (typeof stored === 'number'
            ? `${stored} stored file${stored === 1 ? '' : 's'} will be analyzed, and every file added later. `
            : 'Every file in this project will be analyzed, and every file added later. ')
          + "Analysis runs media workflows for each file. Indexing can't be turned off once it is on."
        }
        confirmLabel="Turn on indexing"
        onConfirm={() => void turnOn()}
        onCancel={() => setConfirming(false)}
        loading={busy}
      />
    </div>
  );
}

const pageStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 24, maxWidth: 640 };
const sectionStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 8 };
const headingStyle: React.CSSProperties = {
  margin: 0, fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary,
};
const labelStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const inputStyle: React.CSSProperties = {
  padding: '6px 8px',
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.background,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  fontFamily: 'inherit',
};
const bodyStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary };
const mutedStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const errorStyle: React.CSSProperties = { margin: 0, color: theme.colors.error, fontSize: 'var(--fs-sm)' };
const savedStyle: React.CSSProperties = { margin: 0, color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' };
