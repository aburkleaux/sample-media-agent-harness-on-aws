import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import {
  createSkillDraft,
  importSkillPackage,
  listGatewayTools,
  peekGatewayTools,
  readSkillDraft,
  renameAsset,
  updateSkillMd,
} from '../lab-api';
import type { GatewayToolDescriptor, SkillDraft } from '../lab-api';
import { useLabContext } from '../LabContext';
import { theme } from '../styles/tokens';
import Button from './ui/Button';
import EmptyState from './ui/EmptyState';
import { Input, Textarea, Select } from './ui/Input';
import SkillPackageImport from './SkillPackageImport';
import SegmentedControl, { panelDomId, tabDomId } from './ui/SegmentedControl';
import {
  parseSkillDocument,
  readSkillPackageFolder,
  validateImportCandidate,
  validateSkillName,
  type SkillPackageCandidate,
} from '../utils/skillPackage';

export interface SkillEditorProps {
  /**
   * Render this editor's own title + close control. `false` when a frame already
   * supplies them (see `CenterEditor`), which is what stops one form showing two
   * titles and three ways out.
   */
  showHeader?: boolean;
  assetId: string | null;
  onClose: () => void;
}

/** Which creation path the create screen is showing. Tabs, not two stacked cards. */
export type CreateMode = 'skill-md' | 'folder';

export interface MetadataEntry {
  id: string;
  key: string;
  value: string;
}

function stableId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `metadata-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function metadataEntriesFromRecord(record: Record<string, string>): MetadataEntry[] {
  return Object.entries(record).map(([key, value]) => ({ id: stableId(), key, value }));
}

export function metadataRecordFromEntries(entries: MetadataEntry[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const entry of entries) {
    const key = entry.key.trim();
    if (key) result[key] = entry.value;
  }
  return result;
}

export function parseAllowedTools(value: string): string[] {
  return [...new Set(value.split(/\s+/).map(tool => tool.trim()).filter(Boolean))];
}

export function parseFrontmatter(content: string): {
  name: string;
  description: string;
  license?: string;
  allowedTools: string[];
  metadata: Record<string, string>;
  instructions: string;
} {
  const parsed = parseSkillDocument(content);
  return {
    name: parsed.name,
    description: parsed.description,
    license: parsed.license,
    allowedTools: parsed.allowedTools,
    metadata: parsed.metadata,
    instructions: parsed.body,
  };
}

function yamlString(value: string): string {
  return JSON.stringify(value);
}

export function composeFrontmatter(
  name: string,
  description: string,
  allowedTools: string[],
  metadata: Record<string, string>,
  instructions: string,
  license?: string,
): string {
  let frontmatter = `---\nname: ${name.trim()}\ndescription: ${yamlString(description.trim())}\n`;
  if (license) frontmatter += `license: ${yamlString(license)}\n`;
  if (allowedTools.length) frontmatter += `allowed-tools: ${allowedTools.join(' ')}\n`;
  const entries = Object.entries(metadata);
  if (entries.length) {
    frontmatter += 'metadata:\n';
    for (const [key, value] of entries) frontmatter += `  ${key}: ${yamlString(value)}\n`;
  }
  return `${frontmatter}---\n\n${instructions}`;
}

export { validateSkillName };

export default function SkillEditor({ assetId, onClose, showHeader = true }: SkillEditorProps) {
  const { refreshCategory, skills } = useLabContext();
  const [persistedAssetId, setPersistedAssetId] = useState<string | null>(assetId);
  const [loading, setLoading] = useState(assetId !== null);
  const [error, setError] = useState<string | null>(null);
  const [skill, setSkill] = useState<SkillDraft | null>(null);
  const [skillName, setSkillName] = useState('');
  const [description, setDescription] = useState('');
  const [license, setLicense] = useState<string | undefined>();
  const [allowedTools, setAllowedTools] = useState<string[]>([]);
  const [metadataEntries, setMetadataEntries] = useState<MetadataEntry[]>([]);
  const [instructions, setInstructions] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);
  const [toolCatalog, setToolCatalog] = useState<GatewayToolDescriptor[]>(() => peekGatewayTools() ?? []);
  const [toolsLoading, setToolsLoading] = useState(toolCatalog.length === 0);
  const [toolsError, setToolsError] = useState<string | null>(null);

  const isPersisted = persistedAssetId !== null;
  const isInitialCreate = assetId === null && !isPersisted;

  // --- Folder import, offered only while creating -------------------------
  //
  // This used to be an "Upload folder" button in the Skills surface header,
  // where it sat beside Search as though it were a way to view the list. It is
  // not: it is the second way to CREATE a skill, so it belongs on the create
  // path next to the blank form it is an alternative to.
  //
  // Gated on `isInitialCreate`, not on `assetId === null`: once the first Save
  // has persisted a draft, importing a folder over the top would create a SECOND
  // skill and leave the one just saved orphaned in the list.
  const [importCandidate, setImportCandidate] = useState<SkillPackageCandidate | null>(null);
  const [mode, setMode] = useState<CreateMode>('skill-md');
  const [importName, setImportName] = useState('');
  const [importing, setImporting] = useState(false);
  const [importSubmitError, setImportSubmitError] = useState<string | null>(null);
  const modeTabsId = useId();
  const [readingFolder, setReadingFolder] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);

  // `webkitdirectory` is not in React's DOM typings, so it is set imperatively.
  // Without it the picker chooses files instead of a folder and a package import
  // silently becomes a single-file one.
  //
  // Applied from the ref callback rather than a mount effect. This used to be load-bearing because
  // the importer early-returned above the form, unmounting and remounting the input; it now stays
  // mounted for the whole create path, independent of the active tab. The ref callback is kept
  // because it is correct under either arrangement — a `[]` effect that silently stopped applying
  // the attribute would downgrade the control to a plain file picker for the rest of the session.
  const attachFolderInput = useCallback((node: HTMLInputElement | null) => {
    folderInputRef.current = node;
    node?.setAttribute('webkitdirectory', '');
  }, []);

  const handleFolderSelection = useCallback(async (files: FileList | null) => {
    if (!files?.length) return;
    setReadingFolder(true);
    setImportError(null);
    try {
      const candidate = await readSkillPackageFolder(files);
      setImportCandidate(candidate);
      // Reveal the tab that shows it. The picker lives in the folder tab, so this is normally a
      // no-op — but a candidate set while the form is showing would otherwise sit invisible behind
      // it, and the footer's primary would silently switch to importing.
      setMode('folder');
      // Seed the name the import will use. The review panel no longer owns this state, so it can
      // no longer seed itself from its own props on mount.
      setImportName(candidate.suggestedName);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'Unable to read the selected folder');
    } finally {
      setReadingFolder(false);
      // Reset so re-picking the SAME folder fires `change` again.
      if (folderInputRef.current) folderInputRef.current.value = '';
    }
  }, []);

  useEffect(() => setPersistedAssetId(assetId), [assetId]);

  useEffect(() => {
    let cancelled = false;
    listGatewayTools()
      .then(tools => { if (!cancelled) setToolCatalog(tools); })
      .catch(err => { if (!cancelled) setToolsError(err instanceof Error ? err.message : 'Tool catalog unavailable'); })
      .finally(() => { if (!cancelled) setToolsLoading(false); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!assetId) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    readSkillDraft(assetId)
      .then(data => {
        if (cancelled) return;
        const parsed = parseFrontmatter(data.skillMd);
        setSkill(data);
        setSkillName(parsed.name || data.name);
        setDescription(parsed.description);
        setLicense(parsed.license);
        setAllowedTools(parsed.allowedTools);
        setMetadataEntries(metadataEntriesFromRecord(parsed.metadata));
        setInstructions(parsed.instructions);
      })
      .catch(err => { if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load skill draft'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [assetId]);

  const catalogNames = useMemo(() => [...new Set(toolCatalog.map(tool => tool.name).filter(Boolean))].sort(), [toolCatalog]);
  const availableToolNames = catalogNames.filter(name => !allowedTools.includes(name));

  // --- folder tab: validation and import live HERE, not in the review panel -------------------
  // One owner for the primary action, so the footer button and the panel can never disagree about
  // whether the package is importable.
  const importValidation = useMemo(
    () => (importCandidate
      ? validateImportCandidate(importCandidate, importName, skills.map(item => item.name))
      : null),
    [importCandidate, importName, skills],
  );

  const clearFolder = useCallback(() => {
    setImportCandidate(null);
    setImportSubmitError(null);
    // Reset the input so re-picking the SAME folder still fires `change`.
    if (folderInputRef.current) folderInputRef.current.value = '';
  }, []);

  const handleImport = async () => {
    if (!importCandidate || !importValidation || importValidation.errors.length) return;
    setImporting(true);
    setImportSubmitError(null);
    try {
      await importSkillPackage(importName.trim(), importValidation.skillMd, importCandidate.files);
      // The draft is created and its contents were already reviewed in-place, so there is nothing
      // further to show — refresh the list and hand the user back to it.
      await refreshCategory('skill');
      onClose();
    } catch (err) {
      setImportSubmitError(err instanceof Error ? err.message : 'Failed to import Skill package');
      setImporting(false);
    }
  };

  const handleSave = async () => {
    const nameError = validateSkillName(skillName);
    if (nameError) { setError(nameError); return; }
    if (!description.trim()) { setError('Description is required and should explain what the skill does and when to use it'); return; }

    setSaving(true);
    setSaveSuccess(null);
    setError(null);
    const cleanName = skillName.trim();
    const fullMd = composeFrontmatter(
      cleanName,
      description,
      allowedTools,
      metadataRecordFromEntries(metadataEntries),
      instructions,
      license,
    );

    try {
      if (!persistedAssetId) {
        const created = await createSkillDraft(cleanName, fullMd);
        const now = Date.now();
        setPersistedAssetId(created.assetId);
        setSkill({
          assetId: created.assetId,
          name: cleanName,
          category: 'skill',
          status: 'archived',
          createdAt: now,
          updatedAt: now,
          s3Prefix: created.s3Prefix,
          skillMd: fullMd,
          supportingFiles: [],
        });
        setSaveSuccess('SKILL.md created. Use folder upload when you need a complete package.');
      } else {
        await updateSkillMd(persistedAssetId, fullMd);
        if (cleanName !== skill?.name) await renameAsset(persistedAssetId, 'skill', cleanName);
        setSkill(previous => previous ? { ...previous, name: cleanName, skillMd: fullMd, updatedAt: Date.now() } : previous);
        setSaveSuccess('Saved');
      }
      await refreshCategory('skill');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save skill');
    } finally {
      setSaving(false);
    }
  };

  // Declared after `handleSave` on purpose: it references it, and a `const` cannot be read before
  // its initialiser runs.
  const folderMode = isInitialCreate && mode === 'folder';
  const handlePrimary = folderMode ? handleImport : handleSave;
  const primaryDisabled = folderMode
    ? !importCandidate || (importValidation?.errors.length ?? 0) > 0 || importing
    : saving;
  const primaryHint = folderMode && !importCandidate
    ? 'Choose a folder to continue.'
    : folderMode && (importValidation?.errors.length ?? 0) > 0
      ? 'Resolve the validation issues above to continue.'
      : null;

  const updateMetadata = (id: string, patch: Partial<Pick<MetadataEntry, 'key' | 'value'>>) => {
    setMetadataEntries(entries => entries.map(entry => entry.id === id ? { ...entry, ...patch } : entry));
  };

  if (loading) return <EditorFrame title="Edit Skill" onClose={onClose} showHeader={showHeader}><div style={loadingStyle}>Loading SKILL.md…</div></EditorFrame>;
  if (assetId && error && !skill) return <EditorFrame title="Edit Skill" onClose={onClose} showHeader={showHeader}><div style={errorTextStyle}>{error}</div></EditorFrame>;

  // The folder path is a TAB, not a separate screen. It used to replace the whole editor via an
  // early return here, which is why it needed its own heading, back control and submit — and why
  // those collided with the frame's. Inline it owns none of that: the tab label is the heading, the
  // frame supplies Back, and the single footer action below submits whichever tab is active.
  const folderPanel = importCandidate ? (
    <SkillPackageImport
      candidate={importCandidate}
      name={importName}
      onNameChange={setImportName}
      // Validation is computed HERE so the footer button and the panel cannot disagree about
      // whether the package is importable.
      validation={importValidation!}
      importError={importSubmitError}
      busy={importing}
      onCancel={clearFolder}
    />
  ) : (
    <>
      <div style={importPromptStyle}>
        <div style={{ minWidth: 0 }}>
          <strong style={importPromptTitleStyle}>Already have a Skill folder?</strong>
          <span style={importPromptHintStyle}>
            Upload it to bring its SKILL.md and any references, scripts or assets in together.
          </span>
        </div>
        <Button
          type="button"
          size="md"
          onClick={() => folderInputRef.current?.click()}
          disabled={readingFolder}
          busy={readingFolder}
          busyLabel="Reading folder…"
        >
          Choose folder
        </Button>
      </div>
      {/* Holds the space the review will occupy, so the picker card and the action row are not
        * crammed together and the user can see WHERE the preview will appear. `EmptyState` already
        * is a dashed placeholder card; `children` is passed because its default copy
        * ("No X yet. Click …") would be wrong — nothing is missing, nothing is chosen yet. */}
      <EmptyState>
        Choose a folder to see its SKILL.md, its files and any validation issues here before you
        create the Skill.
      </EmptyState>
    </>
  );

  return (
    <EditorFrame
      title={isInitialCreate ? 'New Skill' : 'Edit SKILL.md'}
      onClose={onClose}
      showHeader={showHeader}
      footer={
        <Button
          variant="primary"
          size="md"
          onClick={handlePrimary}
          disabled={primaryDisabled}
          busy={saving || importing}
          busyLabel={importing ? 'Importing…' : 'Saving…'}
        >
          {isInitialCreate ? 'Create Skill' : 'Save Skill'}
        </Button>
      }
      footerHint={primaryHint}
    >
      {/* Rendered ALWAYS, outside the tab panels, and never `display: none`. A file input that
        * generates no layout is a long-standing cross-engine footgun — some browsers and extensions
        * decline to open a picker for it, and the failure is silent. Keeping it mounted regardless
        * of the active tab also means switching tabs cannot drop the ref mid-pick. */}
      {isInitialCreate && (
        <input
          ref={attachFolderInput}
          type="file"
          multiple
          aria-label="Choose Skill folder"
          onChange={event => void handleFolderSelection(event.target.files)}
          style={srOnlyStyle}
        />
      )}

      {isInitialCreate ? (
        <SegmentedControl
          options={[
            { value: 'skill-md' as CreateMode, label: 'Write SKILL.md' },
            { value: 'folder' as CreateMode, label: 'Upload folder' },
          ]}
          value={mode}
          onChange={setMode}
          label="How to create this Skill"
          idPrefix={modeTabsId}
          style={{ alignSelf: 'center' }}
        />
      ) : (
        <div style={introStyle}>
          <strong>SKILL.md editor</strong>
          <span>Edit the root instructions here. Uploaded package resources are preserved unchanged.</span>
        </div>
      )}

      {importError && <Banner message={importError} onDismiss={() => setImportError(null)} error />}
      {error && <Banner message={error} onDismiss={() => setError(null)} error />}
      {saveSuccess && <Banner message={saveSuccess} onDismiss={() => setSaveSuccess(null)} />}

      {isInitialCreate && mode === 'folder' ? (
        <div
          role="tabpanel"
          id={panelDomId(modeTabsId, 'folder')}
          aria-labelledby={tabDomId(modeTabsId, 'folder')}
        >
          {folderPanel}
        </div>
      ) : (
      <div
        {...(isInitialCreate
          ? {
              role: 'tabpanel',
              id: panelDomId(modeTabsId, 'skill-md'),
              'aria-labelledby': tabDomId(modeTabsId, 'skill-md'),
            }
          : {})}
      >
      <Section title="SKILL.md" hint="Required · metadata and activation instructions">
        <Field label="Skill name" hint="Lowercase letters, numbers, and single hyphens.">
          <Input value={skillName} onChange={event => setSkillName(event.target.value)} maxLength={64} aria-label="Skill name" placeholder="document-review" />
        </Field>
        <Field label="Description" hint="Explain both what the skill does and when the agent should use it.">
          <Input value={description} onChange={event => setDescription(event.target.value)} maxLength={1024} aria-label="Skill description" placeholder="Reviews documents for… Use when…" />
        </Field>

        <Field label="Allowed tools" hint="Optional and advisory. Select tools available in the current catalog.">
          <Select
            aria-label="Add allowed tool"
            value=""
            onChange={event => {
              const selected = event.target.value;
              if (selected) setAllowedTools(current => [...current, selected]);
            }}
            disabled={availableToolNames.length === 0}
          >
            <option value="">{toolsLoading ? 'Loading tool catalog…' : availableToolNames.length ? 'Select a tool…' : 'No more tools available'}</option>
            {availableToolNames.map(name => <option key={name} value={name}>{name}</option>)}
          </Select>
          {toolsError && <span style={warningStyle}>{toolsError}. Previously selected tools are preserved.</span>}
          {allowedTools.length > 0 && (
            <div style={chipListStyle} aria-label="Selected allowed tools">
              {allowedTools.map(tool => (
                <span key={tool} style={chipStyle}>
                  {tool}
                  <button aria-label={`Remove ${tool}`} onClick={() => setAllowedTools(current => current.filter(item => item !== tool))} style={chipRemoveStyle}>✕</button>
                </span>
              ))}
            </div>
          )}
        </Field>

        <div>
          <div style={sectionHeadingRowStyle}>
            <div><div style={fieldLabelStyle}>Metadata</div><div style={fieldHintStyle}>Optional string key-value pairs.</div></div>
            <Button onClick={() => setMetadataEntries(entries => [...entries, { id: stableId(), key: '', value: '' }])}>+ Add metadata</Button>
          </div>
          {metadataEntries.map((entry, index) => (
            <div key={entry.id} style={metadataRowStyle}>
              <Input value={entry.key} onChange={event => updateMetadata(entry.id, { key: event.target.value })} aria-label={`Metadata key ${index + 1}`} placeholder="key" style={{ flex: '0 1 36%' }} />
              <Input value={entry.value} onChange={event => updateMetadata(entry.id, { value: event.target.value })} aria-label={`Metadata value ${index + 1}`} placeholder="value" style={{ flex: 1 }} />
              <button aria-label={`Remove metadata ${entry.key || 'entry'}`} onClick={() => setMetadataEntries(entries => entries.filter(item => item.id !== entry.id))} style={iconButtonStyle}>✕</button>
            </div>
          ))}
          {metadataEntries.length === 0 && <div style={emptyInlineStyle}>No custom metadata.</div>}
        </div>

        <Field label="Instructions" hint="Keep SKILL.md focused; move detailed material into references/ when importing a package.">
          {/* Braces, not a bare JSX string: JSX attribute strings do not process escapes, so
            * "…\n\n…" rendered the literal backslash-n on screen. */}
          <Textarea mono value={instructions} onChange={event => setInstructions(event.target.value)} aria-label="Skill instructions" placeholder={'# Instructions\n\nDescribe the workflow…'} rows={16} style={{ minHeight: 100 }} />
        </Field>

        {skill?.supportingFiles?.length ? (
          <div style={preservedResourcesStyle}>
            {skill.supportingFiles.length} uploaded package resource{skill.supportingFiles.length === 1 ? '' : 's'} will be preserved unchanged.
          </div>
        ) : null}

      </Section>
      </div>
      )}
    </EditorFrame>
  );
}

function EditorFrame({ title, onClose, showHeader, actions, footer, footerHint, children }: {
  title: string;
  onClose: () => void;
  /** `false` when a host frame already renders the title and the way out. */
  showHeader: boolean;
  /** Rendered in the toolbar beside Back. Kept for the branches that want it there. */
  actions?: React.ReactNode;
  /**
   * The primary action, rendered BELOW the content.
   *
   * Separate from `actions` because `← Back` must stay in the top toolbar: every branch of this
   * editor renders through this frame, including "Loading SKILL.md…" and the load-failure message,
   * and a way out that only existed under a form would strand the user on exactly those two.
   */
  footer?: React.ReactNode;
  /** Explains a disabled `footer` action, so it never reads as simply broken. */
  footerHint?: string | null;
  children: React.ReactNode;
}) {
  return (
    <div style={containerStyle}>
      {showHeader && (
        <div style={headerStyle}>
          <h3 style={titleStyle}>{title}</h3>
          <button onClick={onClose} style={iconButtonStyle} aria-label="Close editor">✕</button>
        </div>
      )}
      {/*
        * The toolbar is HERE rather than in the form below, because every branch of
        * this editor renders through this frame — including "Loading SKILL.md…" and
        * the load-failure message. `CenterEditor` no longer supplies a back control
        * for skills, so a toolbar that only existed on the form would strand the
        * user on exactly the two branches where they most need out.
        */}
      <div style={toolbarStyle}>
        <Button variant="ghost" size="md" onClick={onClose}>← Back</Button>
        {actions}
      </div>
      {children}
      {footer && (
        <div style={footerStyle}>
          {footerHint && <span style={footerHintStyle}>{footerHint}</span>}
          {footer}
        </div>
      )}
    </div>
  );
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section style={sectionStyle}>
      <div><h4 style={sectionTitleStyle}>{title}</h4>{hint && <div style={fieldHintStyle}>{hint}</div>}</div>
      {children}
    </section>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return <label style={fieldStyle}><span style={fieldLabelStyle}>{label}</span>{hint && <span style={fieldHintStyle}>{hint}</span>}{children}</label>;
}

function Banner({ message, onDismiss, error = false }: { message: string; onDismiss: () => void; error?: boolean }) {
  return (
    <div role={error ? 'alert' : 'status'} style={{ ...bannerStyle, color: error ? theme.colors.error : theme.colors.success, borderColor: error ? theme.colors.error : theme.colors.success }}>
      <span>{message}</span><button onClick={onDismiss} style={{ ...iconButtonStyle, color: 'inherit' }} aria-label="Dismiss message">✕</button>
    </div>
  );
}

const containerStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 16, color: theme.colors.textPrimary };
const headerStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', justifyContent: 'space-between' };
const titleStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-xl)', fontWeight: 600 };
const introStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 3, padding: '11px 13px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surfaceSubtle, fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
// Takes the element out of view without taking it out of layout — see the note on
// the folder input. Not `display: none`, not `visibility: hidden`, both of which
// can stop a browser treating the input as clickable.
const srOnlyStyle: React.CSSProperties = {
  position: 'absolute',
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
  border: 0,
};

// Same card metrics as `introStyle` directly above it, but a row: the copy sits
// left and the action right, so the alternative to filling in the form reads as
// an offer rather than as another instruction to read.
const importPromptStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '11px 13px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surfaceSubtle };
const importPromptTitleStyle: React.CSSProperties = { display: 'block', fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary };
const importPromptHintStyle: React.CSSProperties = { display: 'block', fontSize: 'var(--fs-2xs)', color: theme.colors.textSecondary };
const sectionStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 13, padding: 16, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surface };
const sectionHeadingRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 };
const sectionTitleStyle: React.CSSProperties = { margin: '0 0 2px', fontSize: 'var(--fs-md)', fontWeight: 600, color: theme.colors.textPrimary };
const fieldStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 5 };
const fieldLabelStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', fontWeight: 600, color: theme.colors.textPrimary };
const fieldHintStyle: React.CSSProperties = { fontSize: 'var(--fs-2xs)', color: theme.colors.textSecondary, fontWeight: 400 };
const iconButtonStyle: React.CSSProperties = { background: 'none', border: 'none', color: theme.colors.textSecondary, cursor: 'pointer', padding: '3px 6px', fontSize: 'var(--fs-lg)' };
/*
 * Back and Save on one row at the TOP of the editor. Save used to sit at the foot of
 * a form long enough to need scrolling, so the control you reach for most was the
 * one furthest away.
 */
// BELOW the form, and NOT pinned.
//
// This was briefly `position: sticky; bottom: 0`, to reconcile "put the action at the bottom" with
// the reason it had been moved into the top toolbar in the first place: "Save sat at the foot of a
// form long enough to scroll, so the control reached for most was the furthest away." Pinning it
// floated the panel OVER the form while scrolling, which is the worse of the two problems, so the
// action is simply the last thing in the flow. If reach on a long form becomes a complaint again,
// shorten the form or add a scroll-to-action affordance — do not put chrome back on top of content.
const footerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-end',
  gap: 11,
  marginTop: 4,
  paddingTop: 13,
  borderTop: `1px solid ${theme.colors.border}`,
};
const footerHintStyle: React.CSSProperties = { color: theme.colors.textSecondary, fontSize: 'var(--fs-2xs)' };
const toolbarStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8 };
const bannerStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '9px 12px', border: '1px solid', borderRadius: theme.radius.button, fontSize: 'var(--fs-xs)', background: theme.colors.background };
const warningStyle: React.CSSProperties = { fontSize: 'var(--fs-2xs)', color: theme.colors.warning };
const chipListStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 3 };
const chipStyle: React.CSSProperties = { display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 7px', borderRadius: theme.radius.badge, background: theme.colors.surfaceSubtle, border: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-2xs)', fontFamily: theme.typography.fontMono };
const chipRemoveStyle: React.CSSProperties = { background: 'none', border: 'none', color: theme.colors.textSecondary, padding: 0, cursor: 'pointer', fontSize: 'var(--fs-md)', lineHeight: 1 };
const metadataRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 7, marginTop: 7 };
const emptyInlineStyle: React.CSSProperties = { marginTop: 7, fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const preservedResourcesStyle: React.CSSProperties = { padding: '8px 10px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, color: theme.colors.textSecondary, fontSize: 'var(--fs-2xs)', background: theme.colors.surfaceSubtle };
const loadingStyle: React.CSSProperties = { padding: 24, textAlign: 'center', color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' };
const errorTextStyle: React.CSSProperties = { padding: 24, textAlign: 'center', color: theme.colors.error, fontSize: 'var(--fs-sm)' };
