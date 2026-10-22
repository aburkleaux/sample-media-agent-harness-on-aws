import React, { useState, useEffect } from 'react';
import { useLabContext } from '../LabContext';
import { fetchModels, type ModelInfo } from '../api';
import {
  readSpecialtyAgentDraft,
  updateSpecialtyAgentDraft,
  createSpecialtyAgentDraft,
  renameAsset,
  publishAsset,
  listAssetVersions,
  LabApiError,
  type IntegrationConfig,
  type AssetVersionSummary,
} from '../lab-api';
import { theme, tint, numericStyle } from '../styles/tokens';
import Button from './ui/Button';
import { BELL_SAFE_AREA } from '../utils/layout';
import { Input, Textarea, Select } from './ui/Input';

/**
 * SpecialtyAgentEditor — Editor for a Custom Agent (SpecialtyAgent) draft.
 * In edit mode (assetId provided), fetches the full draft via readSpecialtyAgentDraft.
 * In create mode (assetId === null), starts empty and creates on save.
 *
 * Presents fields for name, persona, compatible skills, model (populated from
 * discover_models() via fetchModels), and tool_collections (multi-select), plus
 * an integration panel (enable toggle default-off, issuer, audience,
 * allowed_client_ids, surfaced POST /invoke/{agentId} URL) and version/publish
 * controls. Requires a non-empty name on submit; the asset is stored as a draft.
 * On integration-save failure, unsaved input is preserved and an error is shown.
 *
 * This editor is configuration ONLY — it hosts no test surface. Testing an
 * agent lives on the read-only published view (`PublishedAgentView`, which
 * embeds `AgentTestPanel`), so there is exactly one place to run an agent
 * rather than three that resolve its config differently. It previously carried
 * both a draft test console and a sample-input Test_Harness; both were removed
 * deliberately. Don't re-add one here.
 *
 * Requirements: 1.5, 1.6, 1.7, 1.8, 2.2, 3.3, 3.4, 3.6, 4.1, 4.4, 4.6, 8.5
 */

interface SpecialtyAgentEditorProps {
  /**
   * Render this editor's own title + close control. `false` when a frame already
   * supplies them (see `CenterEditor`), which is what stops one form showing two
   * titles and three ways out.
   */
  showHeader?: boolean;
  assetId: string | null;  // null = create new
  // `saved` is true when the working copy was persisted (saved or published)
  // during this editing session. The parent uses it to decide whether a
  // just-forked working copy should be discarded on close.
  onClose: (saved: boolean) => void;
}

export default function SpecialtyAgentEditor({ assetId, onClose, showHeader = true }: SpecialtyAgentEditorProps) {
  const isCreate = assetId === null;
  const {
    skills, publishedSkills, toolCollections, ensureCategoryLoaded, refreshCategory,
  } = useLabContext();
  // Sticky across the session: once the user saves or publishes, closing must
  // report saved=true even if a later action (e.g. publish leaves the editor
  // open) precedes the close.
  const savedRef = React.useRef(false);

  // staging's lazy category load — the editor needs both skills AND tool
  // collections resolved before it can render their checkbox lists. The same
  // call populates drafts AND published versions per category, so the published
  // list below costs no extra request.
  React.useEffect(() => {
    void ensureCategoryLoaded('skill');
    void ensureCategoryLoaded('tool-collection');
  }, [ensureCategoryLoaded]);

  /**
   * One entry per skill LINEAGE, drafts and published together.
   *
   * This list used to be the drafts only, which made a published skill
   * unselectable — and since publishing CONSUMES the working copy
   * (`_lifecycle_ops.publish_asset` deletes the owned row for every category),
   * publishing a skill removed it from this picker. You could only attach skills
   * you had not published.
   *
   * That was backwards. The runtime prefers published: `task_agent` resolves
   * `compatible_skills` by querying the lineage first and only falls back to the
   * owner's draft (`task_agent.py:1150-1174`). So the editor was offering the
   * fallback and hiding the primary.
   *
   * No new plumbing was needed. `compatible_skills` stores lineage ids and in v2
   * `asset_id` IS the lineage id, so a draft and its published versions share
   * one id — selecting either stores the same value.
   *
   * Published deliberately overwrites draft in the map, so a lineage with both
   * (which an `EditAsset` fork produces) appears ONCE, badged with what the
   * runtime will actually load.
   */
  const selectableSkills = React.useMemo(() => {
    const byLineage = new Map<string, { assetId: string; name: string; badge: string }>();
    for (const draft of skills || []) {
      byLineage.set(draft.assetId, { assetId: draft.assetId, name: draft.name, badge: 'draft' });
    }
    for (const pub of publishedSkills || []) {
      byLineage.set(pub.assetId, {
        assetId: pub.assetId,
        name: pub.name,
        badge: `published v${pub.version}`,
      });
    }
    return [...byLineage.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [skills, publishedSkills]);

  const [loading, setLoading] = useState(!isCreate);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [persona, setPersona] = useState('');
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [invalidSkillIds, setInvalidSkillIds] = useState<string[]>([]);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [originalName, setOriginalName] = useState('');

  // Model picker (populated from discover_models() via fetchModels)
  const [model, setModel] = useState('');  // '' = platform default (Req 2.5)
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);

  // Tool collection multi-select
  const [selectedToolCollections, setSelectedToolCollections] = useState<string[]>([]);

  // Integration panel (external access — disabled by default, Req 4.1)
  const [integrationEnabled, setIntegrationEnabled] = useState(false);
  const [issuer, setIssuer] = useState('');
  const [audience, setAudience] = useState('');
  const [allowedClientIds, setAllowedClientIds] = useState('');  // one id per line / comma-separated
  // External tool opt-in: the subset of selectedToolCollections exposed to
  // external callers (IntegrationConfig.external_tool_collections). Empty ⇒ no
  // external tools (fail-closed). Separate confused-deputy gate (Req 11.1/6.6).
  const [externalToolCollections, setExternalToolCollections] = useState<string[]>([]);

  // Version/publish controls (Req 8.5)
  const [versions, setVersions] = useState<AssetVersionSummary[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publishNotice, setPublishNotice] = useState<string | null>(null);

  // Load the model catalog on mount (Req 2.2)
  useEffect(() => {
    let cancelled = false;
    fetchModels()
      .then(list => { if (!cancelled) setModels(Array.isArray(list) ? list : []); })
      .catch(() => { if (!cancelled) setModelsError('Model catalog is temporarily unavailable'); });
    return () => { cancelled = true; };
  }, []);

  // Fetch the full draft on mount (edit mode only)
  useEffect(() => {
    if (isCreate) {
      setLoading(false);
      return;
    }
    let cancelled = false;

    async function fetchDraft() {
      setLoading(true);
      setError(null);
      try {
        const draft = await readSpecialtyAgentDraft(assetId!);
        if (cancelled) return;
        setPersona(draft.persona || '');
        setSelectedSkills(draft.compatibleSkills || []);
        setName(draft.name || '');
        setDescription(draft.description || '');
        setOriginalName(draft.name || '');
        setModel(draft.model || '');
        setSelectedToolCollections(draft.toolCollections || []);
        const cfg = draft.integrationConfig;
        if (cfg && cfg.enabled) {
          setIntegrationEnabled(true);
          setIssuer(cfg.issuer || '');
          setAudience(cfg.audience || '');
          setAllowedClientIds((cfg.allowedClientIds || []).join('\n'));
          setExternalToolCollections(cfg.externalToolCollections || []);
        } else {
          setIntegrationEnabled(false);
        }
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load agent draft';
        setError(message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchDraft();
    return () => { cancelled = true; };
  }, [assetId, isCreate]);

  // Load version history (edit mode only, Req 8.5)
  const loadVersions = React.useCallback(async () => {
    if (isCreate || !assetId) return;
    setVersionsLoading(true);
    try {
      const list = await listAssetVersions(assetId);
      setVersions(list.filter(v => v.version != null));
    } catch {
      /* version history is non-fatal; leave list empty */
    } finally {
      setVersionsLoading(false);
    }
  }, [assetId, isCreate]);

  useEffect(() => { loadVersions(); }, [loadVersions]);

  const handleSkillToggle = (skillAssetId: string) => {
    setSelectedSkills(prev =>
      prev.includes(skillAssetId)
        ? prev.filter(id => id !== skillAssetId)
        : [...prev, skillAssetId]
    );
    // Clear invalid highlight for this skill if user modifies selection
    setInvalidSkillIds(prev => prev.filter(id => id !== skillAssetId));
  };

  const handleToolCollectionToggle = (tcAssetId: string) => {
    setSelectedToolCollections(prev => {
      const next = prev.includes(tcAssetId)
        ? prev.filter(id => id !== tcAssetId)
        : [...prev, tcAssetId];
      // Keep the external opt-in ⊆ selected: dropping a collection also drops
      // it from the external exposure (backend enforces the same subset).
      if (!next.includes(tcAssetId)) {
        setExternalToolCollections(ext => ext.filter(id => id !== tcAssetId));
      }
      return next;
    });
  };

  const handleExternalToolCollectionToggle = (tcAssetId: string) => {
    setExternalToolCollections(prev =>
      prev.includes(tcAssetId)
        ? prev.filter(id => id !== tcAssetId)
        : [...prev, tcAssetId]
    );
  };

  // Build the Integration_Config payload from the panel's current state.
  // When disabled, produce a disabled/cleared config (Req 4.5).
  const buildIntegrationConfig = (): IntegrationConfig => {
    if (!integrationEnabled) {
      return { enabled: false };
    }
    const clientIds = allowedClientIds
      .split(/[\n,]/)
      .map(s => s.trim())
      .filter(Boolean);
    return {
      enabled: true,
      issuer: issuer.trim(),
      audience: audience.trim(),
      allowedClientIds: clientIds,
      // Only expose opt-ins that are still selected (subset invariant); the
      // backend re-validates external_tool_collections ⊆ tool_collections.
      externalToolCollections: externalToolCollections.filter(id =>
        selectedToolCollections.includes(id)
      ),
    };
  };

  const handleSave = async () => {
    // Non-empty name required on submit (Req 1.8)
    if (!name.trim()) {
      setError('Agent name is required');
      return;
    }
    // Integration validation when enabled (Req 4.3): valid HTTPS issuer + non-empty audience
    if (integrationEnabled) {
      if (!isValidHttpsUrl(issuer.trim())) {
        setError('Integration issuer must be a valid HTTPS URL');
        return;
      }
      if (!audience.trim()) {
        setError('Integration audience is required');
        return;
      }
    }

    setSaving(true);
    setError(null);
    setInvalidSkillIds([]);

    const integrationConfig = buildIntegrationConfig();

    try {
      if (isCreate) {
        await createSpecialtyAgentDraft(
          name.trim(),
          persona,
          selectedSkills.length > 0 ? selectedSkills : undefined,
          {
            model: model || undefined,
            toolCollections: selectedToolCollections,
            integrationConfig,
            description: description.trim(),
          },
        );
        await refreshCategory('specialty-agent');
        savedRef.current = true;
        onClose(true);
        return;
      }
      await updateSpecialtyAgentDraft(assetId!, {
        persona,
        compatibleSkills: selectedSkills,
        model: model || undefined,
        toolCollections: selectedToolCollections,
        integrationConfig,
        description: description.trim(),
      });
      savedRef.current = true;
      onClose(true);
    } catch (err) {
      // On save failure, preserve the owner's unsaved input and show an error
      // (Req 4.6). No state is reset here.
      if (err instanceof LabApiError) {
        setError(err.message);
        // Check if the error indicates invalid skill references (Requirement 3.6)
        const invalidRefs = extractInvalidSkillRefs(err.message, selectedSkills);
        if (invalidRefs.length > 0) {
          setInvalidSkillIds(invalidRefs);
        }
      } else {
        const message = err instanceof Error ? err.message : 'Failed to save changes';
        setError(message);
      }
    } finally {
      setSaving(false);
    }
  };

  const handlePublish = async () => {
    if (isCreate || !assetId) return;
    setPublishing(true);
    setError(null);
    setPublishNotice(null);
    try {
      const result = await publishAsset('specialty-agent', assetId);
      // Publishing persists an immutable version — this session is "saved", so a
      // subsequent close must not discard the working copy.
      savedRef.current = true;
      setPublishNotice(`Published version ${result.version}`);
      await loadVersions();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to publish agent';
      setError(message);
    } finally {
      setPublishing(false);
    }
  };

  const handleDismissError = () => {
    setError(null);
    setInvalidSkillIds([]);
  };

  // Cancel / × / any non-save close → report whether anything was persisted
  // this session (a bare onClose from a button would otherwise pass the click
  // event as a truthy `saved`).
  const handleClose = () => onClose(savedRef.current);

  // --- Loading state ---
  if (loading) {
    return (
      <div style={containerStyle}>
        {showHeader && (
          <div style={headerStyle}>
            <span style={titleStyle}>{isCreate ? 'New Agent' : 'Edit Agent'}</span>
            <button style={closeButtonStyle} onClick={handleClose} aria-label="Close editor" title="Close">✕</button>
          </div>
        )}
        <div style={loadingStyle}>Loading agent draft…</div>
      </div>
    );
  }

  // --- Error state (failed to load) — edit mode only ---
  if (!isCreate && error && !persona && !name) {
    return (
      <div style={containerStyle}>
        {showHeader && (
          <div style={headerStyle}>
            <span style={titleStyle}>Edit Agent</span>
            <button style={closeButtonStyle} onClick={handleClose} aria-label="Close editor" title="Close">✕</button>
          </div>
        )}
        <div style={errorBannerStyle}>
          <span>{error}</span>
          <button style={dismissButtonStyle} onClick={handleDismissError} aria-label="Dismiss error">✕</button>
        </div>
      </div>
    );
  }

  return (
    <div style={containerStyle}>
      {showHeader && (
        <div style={headerStyle}>
          <span style={titleStyle}>{isCreate ? 'New Agent' : 'Edit Agent'}</span>
          <button style={closeButtonStyle} onClick={handleClose} aria-label="Close editor" title="Close">✕</button>
        </div>
      )}

      {/* Name field */}
      <div style={nameFieldStyle}>
        <label style={nameLabelStyle}>Agent Name</label>
        <Input
          type="text"
          value={name}
          onChange={e => setName(e.target.value)}
          onBlur={async () => {
            if (!isCreate && name.trim() && name !== originalName) {
              try {
                await renameAsset(assetId!, 'specialty-agent', name.trim());
                setOriginalName(name.trim());
              } catch { /* ignore */ }
            }
          }}
          placeholder="Agent name"
          maxLength={100}
          aria-label="Agent name"
        />
      </div>

      {/* Description field — powers discovery via list_custom_agents */}
      <div style={nameFieldStyle}>
        <label style={nameLabelStyle}>Description</label>
        <Input
          type="text"
          value={description}
          onChange={e => setDescription(e.target.value)}
          placeholder="One line: what is this agent for?"
          maxLength={200}
          aria-label="Agent description"
        />
      </div>

      {/* Error banner */}
      {error && (
        <div style={errorBannerStyle}>
          <span>{error}</span>
          <button style={dismissButtonStyle} onClick={handleDismissError} aria-label="Dismiss error">✕</button>
        </div>
      )}

      {/* Persona textarea */}
      <div style={fieldGroupStyle}>
        <label style={labelStyle}>Persona</label>
        <Textarea
          value={persona}
          onChange={e => setPersona(e.target.value)}
          placeholder="Enter the agent persona prompt…"
          rows={8}
          maxLength={5000}
          disabled={saving}
        />
        <div style={charCountStyle}>{persona.length} / 5000</div>
      </div>

      {/* Compatible Skills multi-select */}
      <div style={fieldGroupStyle}>
        <label style={labelStyle}>Compatible Skills</label>
        <div style={skillListContainerStyle}>
          {selectableSkills.length === 0 && (
            <div style={emptySkillsStyle}>No skills available.</div>
          )}
          {selectableSkills.map(skill => {
            const isSelected = selectedSkills.includes(skill.assetId);
            const isInvalid = invalidSkillIds.includes(skill.assetId);
            return (
              <label
                key={skill.assetId}
                style={{
                  ...skillItemStyle,
                  ...(isInvalid ? invalidSkillItemStyle : {}),
                  ...(isSelected && !isInvalid ? selectedSkillItemStyle : {}),
                }}
              >
                <input
                  type="checkbox"
                  checked={isSelected}
                  onChange={() => handleSkillToggle(skill.assetId)}
                  disabled={saving}
                  style={checkboxStyle}
                />
                <span style={skillNameStyle}>{skill.name}</span>
                {/* Names the version the runtime will load, so attaching a skill
                    that exists as both a draft and a published version is not a
                    guess. */}
                <span style={skillStateBadgeStyle}>{skill.badge}</span>
                {isInvalid && <span style={invalidBadgeStyle}>invalid</span>}
              </label>
            );
          })}
          {/*
            Selected ids that match no known lineage. Checked against drafts AND
            published: testing drafts alone reported every published reference as
            "unknown" with a bare uuid, so publishing a skill made a working
            agent look broken.
          */}
          {selectedSkills
            .filter(id => !selectableSkills.some(s => s.assetId === id))
            .map(orphanId => {
              const isInvalid = invalidSkillIds.includes(orphanId);
              return (
                <label
                  key={orphanId}
                  style={{
                    ...skillItemStyle,
                    ...(isInvalid ? invalidSkillItemStyle : orphanSkillItemStyle),
                  }}
                >
                  <input
                    type="checkbox"
                    checked={true}
                    onChange={() => handleSkillToggle(orphanId)}
                    disabled={saving}
                    style={checkboxStyle}
                  />
                  <span style={skillNameStyle}>{orphanId}</span>
                  <span style={isInvalid ? invalidBadgeStyle : orphanBadgeStyle}>
                    {isInvalid ? 'invalid' : 'unknown'}
                  </span>
                </label>
              );
            })}
        </div>
      </div>

      {/* Model picker (Req 2.2) */}
      <div style={fieldGroupStyle}>
        <label style={labelStyle}>Model</label>
        <Select
          value={model}
          onChange={e => setModel(e.target.value)}
          disabled={saving}
          aria-label="Model"
        >
          <option value="">Platform default</option>
          {/* Keep the current value visible even if it's absent from the catalog */}
          {model && !models.some(m => m.id === model) && (
            <option value={model}>{model} (not in catalog)</option>
          )}
          {models.map(m => (
            <option key={m.id} value={m.id}>{m.name || m.id}</option>
          ))}
        </Select>
        {modelsError && <div style={fieldHintErrorStyle}>{modelsError}</div>}
      </div>

      {/* Tool Collections multi-select (Req 3.x) */}
      <div style={fieldGroupStyle}>
        <label style={labelStyle}>Tool Collections</label>
        <div style={skillListContainerStyle}>
          {(!toolCollections || toolCollections.length === 0) && (
            /* Says what a collection IS and where to make one, rather than the
               bare "No tool collections available." this used to show.
               There is deliberately no create button here: this editor had an
               `onCreateToolCollection` prop for one, but NEITHER host ever passed
               it, so the button never rendered — a dead path that read as a fixed
               dead end. Creating from inside an unsaved agent draft also needs a
               save-first or modal flow that does not exist yet, so the honest
               affordance is naming where the real one lives. */
            <div style={emptySkillsStyle}>
              <div>
                No tool collections yet. A collection is a reusable set of tools
                you grant to this agent — you can only include tools you already
                have access to. Create one under Settings → Tools, then select it
                here.
              </div>
            </div>
          )}
          {(toolCollections || []).map(tc => {
            const isSelected = selectedToolCollections.includes(tc.assetId);
            return (
              <label
                key={tc.assetId}
                style={{
                  ...skillItemStyle,
                  ...(isSelected ? selectedSkillItemStyle : {}),
                }}
              >
                <input
                  type="checkbox"
                  checked={isSelected}
                  onChange={() => handleToolCollectionToggle(tc.assetId)}
                  disabled={saving}
                  style={checkboxStyle}
                />
                <span style={skillNameStyle}>{tc.name}</span>
              </label>
            );
          })}
          {/* Orphaned tool-collection references not present in the current list */}
          {selectedToolCollections
            .filter(id => !toolCollections.some(tc => tc.assetId === id))
            .map(orphanId => (
              <label key={orphanId} style={{ ...skillItemStyle, ...orphanSkillItemStyle }}>
                <input
                  type="checkbox"
                  checked={true}
                  onChange={() => handleToolCollectionToggle(orphanId)}
                  disabled={saving}
                  style={checkboxStyle}
                />
                <span style={skillNameStyle}>{orphanId}</span>
                <span style={orphanBadgeStyle}>unknown</span>
              </label>
            ))}
        </div>
      </div>

      {/* Integration panel (Req 4.1, 4.4) — external access, disabled by default */}
      <div style={integrationPanelStyle}>
        <label style={integrationToggleRowStyle}>
          <input
            type="checkbox"
            checked={integrationEnabled}
            onChange={e => setIntegrationEnabled(e.target.checked)}
            disabled={saving}
            style={checkboxStyle}
            aria-label="Enable external integration"
          />
          <span style={integrationTitleStyle}>Enable external access</span>
        </label>

        {integrationEnabled && (
          <div style={integrationBodyStyle}>
            <div style={fieldGroupStyle}>
              <label style={labelStyle}>OIDC Issuer (HTTPS)</label>
              <Input
                type="text"
                value={issuer}
                onChange={e => setIssuer(e.target.value)}
                placeholder="https://issuer.example.com"
                disabled={saving}
                aria-label="OIDC issuer"
              />
            </div>
            <div style={fieldGroupStyle}>
              <label style={labelStyle}>Audience</label>
              <Input
                type="text"
                value={audience}
                onChange={e => setAudience(e.target.value)}
                placeholder="custom-agent-audience"
                disabled={saving}
                aria-label="Audience"
              />
            </div>
            <div style={fieldGroupStyle}>
              <label style={labelStyle}>Allowed Client IDs (optional, one per line)</label>
              <Textarea
                value={allowedClientIds}
                onChange={e => setAllowedClientIds(e.target.value)}
                placeholder="client-123"
                rows={2}
                style={{ minHeight: 44 }}
                disabled={saving}
                aria-label="Allowed client IDs"
              />
            </div>
            {/* External tool opt-in (Req 11.1/6.6): the subset of the agent's
                selected tool collections exposed to external callers. Empty ⇒
                external callers get no tools (fail-closed). */}
            <div style={fieldGroupStyle}>
              <label style={labelStyle}>External Tools (opt in per collection)</label>
              {selectedToolCollections.length === 0 && (
                <div style={fieldHintStyle}>
                  Select a Tool Collection above first, then opt it in here to expose
                  it to external callers.
                </div>
              )}
              {selectedToolCollections.map(tcId => {
                const tc = toolCollections?.find(c => c.assetId === tcId);
                return (
                  <label key={tcId} style={skillItemStyle}>
                    <input
                      type="checkbox"
                      checked={externalToolCollections.includes(tcId)}
                      onChange={() => handleExternalToolCollectionToggle(tcId)}
                disabled={saving}
                      style={checkboxStyle}
                      aria-label={`Expose ${tc?.name || tcId} externally`}
                    />
                    <span style={skillNameStyle}>{tc?.name || tcId}</span>
                  </label>
                );
              })}
              {selectedToolCollections.length > 0 && externalToolCollections.length === 0 && (
                <div style={fieldHintStyle}>
                  None opted in — external callers will receive no tools.
                </div>
              )}
            </div>
            {/* Surface the invoke URL once integration is enabled (Req 4.4) */}
            {!isCreate && assetId && (
              <div style={invokeUrlStyle}>
                <span style={invokeUrlLabelStyle}>Invoke URL</span>
                <code style={invokeUrlCodeStyle}>{`POST /invoke/${assetId}`}</code>
              </div>
            )}
            {isCreate && (
              <div style={fieldHintStyle}>
                Save the agent to obtain its invoke URL.
              </div>
            )}
          </div>
        )}
      </div>

      {/* Version / publish controls (Req 8.5) */}
      {!isCreate && (
        <div style={versionPanelStyle}>
          <div style={versionHeaderRowStyle}>
            <span style={integrationTitleStyle}>Versions</span>
            <Button
              variant="primary"
              onClick={handlePublish}
              disabled={saving}
              busy={publishing}
              busyLabel="Publishing…"
            >
              Publish
            </Button>
          </div>
          {publishNotice && <div style={fieldHintStyle}>{publishNotice}</div>}
          {versionsLoading && <div style={emptySkillsStyle}>Loading versions…</div>}
          {!versionsLoading && versions.length === 0 && (
            <div style={emptySkillsStyle}>No published versions yet.</div>
          )}
          {!versionsLoading && versions.length > 0 && (
            <div style={versionListStyle}>
              {versions.map(v => (
                <div key={v.version} style={versionRowStyle}>
                  <span style={versionNumberStyle}>v{v.version}</span>
                  <span style={versionStateStyle}>
                    {v.deprecated ? 'deprecated' : v.current ? 'published (current)' : 'published'}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/*
        Actions, bottom-LEFT with the primary first — matching
        `ToolCollectionEditor`, the other left-aligned editor. A left-aligned
        group is read left-to-right, so leading with Cancel would put the
        dismissive action where the eye lands first.
      */}
      <div style={actionsStyle}>
        <Button
          variant="primary"
          size="md"
          onClick={handleSave}
          disabled={!persona.trim() || !name.trim()}
          busy={saving}
          busyLabel="Saving…"
        >
          {isCreate ? 'Create Agent' : 'Save'}
        </Button>
        <Button variant="ghost" size="md" onClick={handleClose} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// --- Helpers ---

/**
 * Attempts to extract invalid skill asset IDs from an error message.
 * The backend may return error messages referencing specific asset IDs.
 * Falls back to marking all selected skills as potentially invalid if the error
 * mentions "invalid" and "skill" but we can't parse specific IDs.
 */
function extractInvalidSkillRefs(errorMessage: string, selectedSkills: string[]): string[] {
  const lowerMsg = errorMessage.toLowerCase();
  if (!lowerMsg.includes('skill') && !lowerMsg.includes('compatible')) {
    return [];
  }

  // Try to find asset IDs mentioned in the error message
  const foundIds = selectedSkills.filter(id => errorMessage.includes(id));
  if (foundIds.length > 0) {
    return foundIds;
  }

  // If the error mentions invalid skill references generically, highlight all selected
  if (lowerMsg.includes('invalid') || lowerMsg.includes('not found') || lowerMsg.includes('unresolved')) {
    return selectedSkills;
  }

  return [];
}

/** Returns true when the given string is a syntactically valid HTTPS URL. */
function isValidHttpsUrl(value: string): boolean {
  if (!value) return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:';
  } catch {
    return false;
  }
}

// --- Styles ---

const containerStyle: React.CSSProperties = {
  padding: '12px',
  display: 'flex',
  flexDirection: 'column',
  gap: '12px',
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  /*
   * Reserves the floating notification bell's safe area. Without it the bell
   * (rendered by WorkspaceView at `zIndex.rail`) painted directly over this
   * header's close button, so clicking the visible ✕ hit the bell.
   */
  paddingRight: BELL_SAFE_AREA,
};

const titleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-md)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const nameFieldStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

const nameLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontWeight: 600,
  color: theme.colors.textSecondary,
};
const closeButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.textTertiary,
  fontSize: 'var(--fs-xl)',
  cursor: 'pointer',
  padding: '2px 6px',
  lineHeight: 1,
};

const loadingStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  padding: '16px 0',
  textAlign: 'center',
  fontStyle: 'italic',
};

const errorBannerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: '8px',
  padding: '8px 10px',
  backgroundColor: tint(theme.colors.error),
  border: `1px solid ${tint(theme.colors.error, 40)}`,
  borderRadius: theme.radius.button,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
};

const dismissButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.error,
  fontSize: 'var(--fs-md)',
  cursor: 'pointer',
  padding: '0 4px',
  lineHeight: 1,
  flexShrink: 0,
};

const fieldGroupStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: '4px',
};

const labelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontWeight: 500,
  color: theme.colors.textSecondary,
};
const charCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  textAlign: 'right',
};

const skillListContainerStyle: React.CSSProperties = {
  maxHeight: '180px',
  overflowY: 'auto',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  backgroundColor: theme.colors.surfaceSubtle,
  padding: '4px',
};

const skillItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: '6px',
  padding: '4px 8px',
  borderRadius: theme.radius.badge,
  cursor: 'pointer',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textPrimary,
};

const selectedSkillItemStyle: React.CSSProperties = {
  backgroundColor: tint(theme.colors.success),
};

const invalidSkillItemStyle: React.CSSProperties = {
  backgroundColor: tint(theme.colors.error),
  border: `1px solid ${tint(theme.colors.error, 40)}`,
};

const orphanSkillItemStyle: React.CSSProperties = {
  backgroundColor: tint(theme.colors.warning),
  border: `1px solid ${tint(theme.colors.warning, 40)}`,
};

const checkboxStyle: React.CSSProperties = {
  flexShrink: 0,
};

const skillNameStyle: React.CSSProperties = {
  flex: 1,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const invalidBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.error,
  backgroundColor: tint(theme.colors.error),
  padding: '1px 5px',
  borderRadius: theme.radius.badge,
  flexShrink: 0,
};

/**
 * Which version of a skill this row refers to. Neutral on purpose: unlike
 * `invalid` and `unknown`, "draft" and "published v2" are both ordinary states,
 * so this reads as metadata rather than a warning.
 */
const skillStateBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textTertiary,
  backgroundColor: theme.colors.surfaceSubtle,
  padding: '1px 5px',
  borderRadius: theme.radius.badge,
  flexShrink: 0,
  fontFamily: 'var(--font-mono)',
  fontVariantNumeric: 'tabular-nums',
};

const orphanBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.warning,
  backgroundColor: tint(theme.colors.warning),
  padding: '1px 5px',
  borderRadius: theme.radius.badge,
  flexShrink: 0,
};

const emptySkillsStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  padding: '8px',
  textAlign: 'center',
  fontStyle: 'italic',
};

const actionsStyle: React.CSSProperties = {
  display: 'flex',
  // Explicit rather than relying on the flex default, so the alignment is a
  // stated decision and does not look like an omission.
  justifyContent: 'flex-start',
  gap: '8px',
  paddingTop: '4px',
};
const fieldHintStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
};

const fieldHintErrorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.error,
};

const integrationPanelStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  padding: '10px',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  backgroundColor: theme.colors.surfaceSubtle,
};

const integrationToggleRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  cursor: 'pointer',
};

const integrationTitleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
};

const integrationBodyStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  paddingTop: 4,
};

const invokeUrlStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
};

const invokeUrlLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontWeight: 500,
  color: theme.colors.textSecondary,
};

const invokeUrlCodeStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.primary,
  backgroundColor: theme.colors.activeBg,
  padding: '4px 6px',
  borderRadius: theme.radius.badge,
  wordBreak: 'break-all',
};

const versionPanelStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  padding: '10px',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  backgroundColor: theme.colors.surfaceSubtle,
};

const versionHeaderRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
};
const versionListStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  maxHeight: 140,
  overflowY: 'auto',
};

const versionRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '4px 8px',
  borderRadius: theme.radius.badge,
  backgroundColor: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
  fontSize: 'var(--fs-xs)',
};

const versionNumberStyle: React.CSSProperties = {
  fontWeight: 600,
  color: theme.colors.textPrimary,
  // The numeric spine: tabular figures so v1 / v9 / v207 line up down the list.
  ...numericStyle,
};

const versionStateStyle: React.CSSProperties = {
  color: theme.colors.textSecondary,
};

