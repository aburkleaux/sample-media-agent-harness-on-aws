import React, { useId, useMemo, useState } from 'react';
import { theme } from '../styles/tokens';
import Button from './ui/Button';
import SegmentedControl, { panelDomId, tabDomId } from './ui/SegmentedControl';
import {
  parseSkillDocument,
  type SkillPackageCandidate,
  type validateImportCandidate,
} from '../utils/skillPackage';
import { SkillMarkdownBody, SkillPackageFileList } from './SkillDetailsView';

/**
 * The folder-import REVIEW panel.
 *
 * Presentational: it renders the candidate, its validation and the package contents. It does not
 * own the name, does not validate, and does not import — `SkillEditor` does, because that screen
 * now hosts this inline in its "Upload folder" tab and carries the single primary action at the
 * bottom. Two components each owning a submit is what produced two "what is this skill called"
 * inputs and two submits on one screen.
 *
 * It used to be a full screen reached by replacing the editor, with its own heading, back control,
 * name field and Import/Cancel footer. Inline, all of that chrome belongs to the host: the frame
 * supplies Back, the tab label supplies the heading, and the footer supplies the action. That also
 * retires the `backLabel` prop, which existed only because this view's back control and
 * `CenterEditor`'s were two identically-labelled buttons doing different things — inline, `onCancel`
 * clears the chosen folder and navigates nowhere, so the collision cannot happen.
 */
export interface SkillPackageImportProps {
  candidate: SkillPackageCandidate;
  /** The name the host is importing under — already reflected in `validation`. */
  name: string;
  onNameChange: (name: string) => void;
  /** Computed by the host, so the host's primary action and this panel cannot disagree. */
  validation: ReturnType<typeof validateImportCandidate>;
  /** Clears the chosen folder and returns the tab to its picker. */
  onCancel: () => void;
  /** A failed import, owned by the host that performed it. */
  importError?: string | null;
  /** Disables the in-panel controls while the host is importing. */
  busy?: boolean;
}

type ImportTab = 'preview' | 'source' | 'package';

export default function SkillPackageImport({
  candidate,
  name,
  onNameChange,
  validation,
  onCancel,
  importError = null,
  busy = false,
}: SkillPackageImportProps) {
  const [activeTab, setActiveTab] = useState<ImportTab>('preview');
  const [highlightedPath, setHighlightedPath] = useState<string | null>(null);
  const tabSetId = useId();
  const parsed = useMemo(() => parseSkillDocument(validation.skillMd), [validation.skillMd]);
  const supportingPaths = candidate.filePaths.filter(path => path !== 'SKILL.md');

  const openPackagePath = (path: string) => {
    setHighlightedPath(path);
    setActiveTab('package');
  };

  return (
    <div role="region" aria-label="Import Skill package" style={containerStyle}>
      <p style={subtitleStyle}>Review the package before creating a private draft.</p>

      <div style={nameSectionStyle}>
        <label style={labelStyle}>
          <span>Skill name</span>
          <input
            value={name}
            onChange={event => onNameChange(event.target.value)}
            maxLength={64}
            aria-label="Imported Skill name"
            disabled={busy}
            style={inputStyle}
          />
        </label>
        <div style={nameHintStyle}>
          Folder: <code>{candidate.folderName || 'unknown'}</code>
          {candidate.declaredName && candidate.declaredName !== name.trim() && (
            <> · SKILL.md name <code>{candidate.declaredName}</code> will be updated on import</>
          )}
        </div>
      </div>

      {validation.errors.length > 0 && (
        <div role="alert" style={errorPanelStyle}>
          <strong>Resolve {validation.errors.length} validation {validation.errors.length === 1 ? 'issue' : 'issues'}</strong>
          <ul style={errorListStyle}>{validation.errors.map(error => <li key={error}>{error}</li>)}</ul>
        </div>
      )}
      {importError && <div role="alert" style={errorPanelStyle}>{importError}</div>}

      <div style={summaryStyle}>
        <span>{candidate.filePaths.length} {candidate.filePaths.length === 1 ? 'file' : 'files'}</span>
        <span aria-hidden="true">·</span>
        <span>{formatBytes(candidate.totalBytes)}</span>
        <span aria-hidden="true">·</span>
        <span>{validation.errors.length === 0 ? 'Ready to import' : 'Validation required'}</span>
        <span aria-hidden="true">·</span>
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Clear folder</Button>
      </div>

      {/* `underline`, not `segmented`: this tablist is nested inside the editor's own segmented
        * toggle, and a pill inside a pill reads badly. It is also the look these tabs already had. */}
      <SegmentedControl
        options={[
          { value: 'preview' as ImportTab, label: 'Preview' },
          { value: 'source' as ImportTab, label: 'Source' },
          { value: 'package' as ImportTab, label: `Package (${candidate.filePaths.length})` },
        ]}
        value={activeTab}
        onChange={setActiveTab}
        label="Import review"
        variant="underline"
        idPrefix={tabSetId}
      />

      <div
        role="tabpanel"
        id={panelDomId(tabSetId, activeTab)}
        aria-labelledby={tabDomId(tabSetId, activeTab)}
        style={contentStyle}
      >
        {activeTab === 'preview' && <SkillMarkdownBody content={parsed.body} onOpenPackagePath={openPackagePath} />}
        {activeTab === 'source' && <pre style={sourceStyle}><code>{validation.skillMd || candidate.skillMd}</code></pre>}
        {activeTab === 'package' && (
          <SkillPackageFileList files={supportingPaths} highlightedPath={highlightedPath} onSelectSkillMd={() => setActiveTab('preview')} />
        )}
      </div>
    </div>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.max(0.1, value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

const containerStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 15, color: theme.colors.textPrimary };
const subtitleStyle: React.CSSProperties = { margin: 0, color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)' };
const nameSectionStyle: React.CSSProperties = { padding: '12px 13px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surface };
const labelStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 5, color: theme.colors.textPrimary, fontSize: 'var(--fs-xs)', fontWeight: 600 };
const inputStyle: React.CSSProperties = { width: '100%', boxSizing: 'border-box', padding: '8px 10px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, background: theme.colors.background, color: theme.colors.textPrimary, fontSize: 'var(--fs-sm)', outline: 'none' };
const nameHintStyle: React.CSSProperties = { marginTop: 6, color: theme.colors.textSecondary, fontSize: 'var(--fs-3xs)' };
const errorPanelStyle: React.CSSProperties = { padding: '9px 11px', border: `1px solid ${theme.colors.error}`, borderRadius: theme.radius.button, color: theme.colors.error, background: `color-mix(in srgb, ${theme.colors.error} 7%, transparent)`, fontSize: 'var(--fs-2xs)' };
const errorListStyle: React.CSSProperties = { margin: '6px 0 0', paddingLeft: 19 };
const summaryStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 7, color: theme.colors.textSecondary, fontSize: 'var(--fs-2xs)' };
const contentStyle: React.CSSProperties = { minHeight: 220, padding: '8px 2px', overflow: 'hidden' };
const sourceStyle: React.CSSProperties = { margin: 0, maxHeight: 320, overflow: 'auto', padding: '10px 12px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, background: theme.colors.surface, color: theme.colors.textPrimary, fontFamily: theme.typography.fontMono, fontSize: 'var(--fs-2xs)', whiteSpace: 'pre-wrap' };
