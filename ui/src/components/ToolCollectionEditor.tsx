import React, { useState, useEffect, useRef, useCallback } from 'react';
import type { ToolRef, GatewayToolDescriptor, ToolCollectionDraft } from '../lab-api';
import {
  listGatewayCatalog,
  loadLiveGatewayCatalog,
  createToolCollectionDraft,
  readToolCollectionDraft,
  updateToolCollectionDraft,
} from '../lab-api';
import { useLabContext } from '../LabContext';
import { theme, tint, numericStyle } from '../styles/tokens';
import Button from './ui/Button';
import { Input } from './ui/Input';
import Badge from './ui/Badge';
import WarningIcon from './icons/WarningIcon';

/**
 * ToolCollectionEditor — Handles both creation and editing of tool collections.
 *
 * Create mode: empty name + GatewayBrowser-style tool selection
 * Edit mode: pre-populated name and tools from readToolCollectionDraft
 *
 * On save, calls createToolCollectionDraft or updateToolCollectionDraft.
 * Highlights unresolved tool references on error (Requirement 4.10).
 */

interface ToolCollectionEditorProps {
  /**
   * Render this editor's own title + close control. `false` when a frame already
   * supplies them (see `CenterEditor`), which is what stops one form showing two
   * titles and three ways out.
   */
  showHeader?: boolean;
  /** If provided, the editor is in edit mode for this asset */
  editAssetId?: string | null;
  /** Called when the editor should close (after save or cancel) */
  onClose: () => void;
}

export default function ToolCollectionEditor({ editAssetId, onClose, showHeader = true }: ToolCollectionEditorProps) {
  const { optimisticAddToolCollection, refreshCategory } = useLabContext();

  // Form state
  const [name, setName] = useState('');
  const [selectedTools, setSelectedTools] = useState<ToolRef[]>([]);

  // Gateway tools state
  const [gatewayTools, setGatewayTools] = useState<GatewayToolDescriptor[]>([]);
  const [loadingTools, setLoadingTools] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);

  // Filter state with debounce
  const [filterText, setFilterText] = useState('');
  const [debouncedFilter, setDebouncedFilter] = useState('');
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Save state
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unresolvedTools, setUnresolvedTools] = useState<string[]>([]);

  // Loading state for edit mode
  const [loadingDraft, setLoadingDraft] = useState(false);

  const isEditMode = !!editAssetId;

  // --- Fetch gateway tools on mount ---
  //
  // Two phases, and the second one is not optional here.
  //
  // This list is what a user can SELECT into a collection, so an incomplete one is
  // worse than a slow one: saving a collection assembled from a short list silently
  // omits tools the agent could have had. Phase 1 is the fast control-plane read so
  // the picker appears at once; phase 2 asks the data plane for the gateways it
  // could not describe (cross-account ones, MCP-server targets) and replaces the
  // list. `loadingTools` stays true across BOTH, because until phase 2 lands the
  // choice on offer really is provisional.
  useEffect(() => {
    let cancelled = false;
    const fetchTools = async () => {
      setLoadingTools(true);
      setToolsError(null);
      try {
        const fast = await listGatewayCatalog();
        if (!cancelled) setGatewayTools(fast.tools);
        if (fast.unreadable.length === 0) return;
        const complete = await loadLiveGatewayCatalog();
        if (!cancelled) setGatewayTools(complete.tools);
      } catch (err) {
        if (!cancelled) {
          setToolsError(err instanceof Error ? err.message : 'Failed to load gateway tools');
        }
      } finally {
        if (!cancelled) setLoadingTools(false);
      }
    };
    fetchTools();
    return () => { cancelled = true; };
  }, []);

  // --- Load existing draft in edit mode ---
  useEffect(() => {
    if (!editAssetId) return;
    let cancelled = false;
    const loadDraft = async () => {
      setLoadingDraft(true);
      setError(null);
      try {
        const draft = await readToolCollectionDraft(editAssetId);
        if (!cancelled) {
          setName(draft.name);
          // `?? []` is right HERE and nowhere else: this is the full CRUD read,
          // which does carry the refs, and an editor needs a concrete array to
          // add to. Listing surfaces must keep `undefined` distinct from empty.
          setSelectedTools(draft.toolRefs ?? []);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load tool collection');
        }
      } finally {
        if (!cancelled) setLoadingDraft(false);
      }
    };
    loadDraft();
    return () => { cancelled = true; };
  }, [editAssetId]);

  // --- Debounce filter ---
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebouncedFilter(filterText);
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [filterText]);

  // --- Filter gateway tools ---
  const filteredTools = gatewayTools.filter(tool => {
    if (!debouncedFilter) return true;
    const lower = debouncedFilter.toLowerCase();
    return (
      tool.name.toLowerCase().includes(lower) ||
      tool.description.toLowerCase().includes(lower)
    );
  });

  // --- Check if a tool is selected ---
  const isToolSelected = useCallback((tool: GatewayToolDescriptor) => {
    return selectedTools.some(t => t.name === tool.name && t.version === tool.version);
  }, [selectedTools]);

  // --- Add tool to selection ---
  const handleAddTool = (tool: GatewayToolDescriptor) => {
    if (isToolSelected(tool)) return;
    setSelectedTools(prev => [...prev, { name: tool.name, version: tool.version }]);
    // Clear unresolved status for this tool if it was previously unresolved
    setUnresolvedTools(prev => prev.filter(n => n !== tool.name));
  };

  // --- Remove tool from selection ---
  const handleRemoveTool = (toolRef: ToolRef) => {
    setSelectedTools(prev => prev.filter(t => !(t.name === toolRef.name && t.version === toolRef.version)));
  };

  // --- Form validation ---
  const isFormValid = name.trim().length > 0 && name.length <= 100 && selectedTools.length > 0;

  // --- Save handler ---
  const handleSave = async () => {
    if (!isFormValid || saving) return;

    setSaving(true);
    setError(null);
    setUnresolvedTools([]);

    try {
      if (isEditMode && editAssetId) {
        await updateToolCollectionDraft(editAssetId, {
          name: name.trim(),
          toolRefs: selectedTools,
        });
        await refreshCategory('tool-collection');
      } else {
        const result = await createToolCollectionDraft(name.trim(), selectedTools);
        // Optimistically add the new collection
        const now = Date.now();
        const newDraft: ToolCollectionDraft = {
          assetId: result.assetId,
          name: name.trim(),
          category: 'tool-collection',
          status: 'active',
          createdAt: now,
          updatedAt: now,
          s3Prefix: result.s3Prefix,
          toolRefs: selectedTools,
        };
        optimisticAddToolCollection(newDraft);
      }
      onClose();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to save tool collection';
      setError(message);

      // Check for unresolved tool references in the error message
      // The backend typically returns error messages mentioning unresolved tool names
      if (message.toLowerCase().includes('unresolved')) {
        // Try to extract tool names from the error — highlight all selected tools
        // that don't appear in the gateway tools list
        const gatewayToolNames = new Set(gatewayTools.map(t => t.name));
        const unresolved = selectedTools
          .filter(t => !gatewayToolNames.has(t.name))
          .map(t => t.name);
        if (unresolved.length > 0) {
          setUnresolvedTools(unresolved);
        } else {
          // If we can't determine which are unresolved, highlight all
          setUnresolvedTools(selectedTools.map(t => t.name));
        }
      }
    } finally {
      setSaving(false);
    }
  };

  // --- Check if a tool ref is unresolved ---
  const isUnresolved = (toolRef: ToolRef) => unresolvedTools.includes(toolRef.name);

  // --- Format tool name for display (strip gateway prefix) ---
  const displayToolName = (fullName: string): string => {
    // Gateway tools have format: "prefix___server___tool_name"
    const parts = fullName.split('___');
    return parts.length > 1 ? parts[parts.length - 1] : fullName;
  };

  // --- Render ---

  if (loadingDraft) {
    return (
      <div style={editorContainerStyle}>
        {showHeader && (
          <div style={headerStyle}>
            <span>{isEditMode ? 'Edit Collection' : 'New Collection'}</span>
            <button onClick={onClose} style={closeButtonStyle} aria-label="Close editor">✕</button>
          </div>
        )}
        <div style={loadingStyle}>Loading...</div>
      </div>
    );
  }

  return (
    <div style={editorContainerStyle}>
      {showHeader && (
        <div style={headerStyle}>
          <span style={headerTitleStyle}>{isEditMode ? 'Edit Collection' : 'New Collection'}</span>
          <button onClick={onClose} style={closeButtonStyle} aria-label="Close editor">✕</button>
        </div>
      )}

      {/* Error display */}
      {error && (
        <div style={errorStyle}>
          <span>{error}</span>
          <button onClick={() => setError(null)} style={dismissErrorStyle} aria-label="Dismiss error">✕</button>
        </div>
      )}

      {/* Name input */}
      <div style={formFieldStyle}>
        <label style={labelStyle} htmlFor="collection-name-input">Collection Name</label>
        <Input
          id="collection-name-input"
          type="text"
          value={name}
          onChange={e => setName(e.target.value)}
          maxLength={100}
          placeholder="Collection name (max 100 chars)"
          disabled={saving}
        />
        {name.length > 0 && (
          <span style={charCountStyle}>{name.length}/100</span>
        )}
      </div>

      {/* Selected tools */}
      <div style={sectionStyle}>
        <label style={labelStyle}>Selected Tools ({selectedTools.length})</label>
        {selectedTools.length === 0 ? (
          <div style={emptySelectionStyle}>No tools selected. Add tools from the browser below.</div>
        ) : (
          <div style={selectedToolsListStyle}>
            {selectedTools.map(toolRef => (
              <div
                key={`${toolRef.name}@${toolRef.version}`}
                style={{
                  ...selectedToolItemStyle,
                  ...(isUnresolved(toolRef) ? unresolvedToolStyle : {}),
                }}
              >
                <div style={toolInfoStyle}>
                  <span style={toolNameStyle} title={toolRef.name}>{displayToolName(toolRef.name)}</span>
                  <span style={toolVersionStyle}>v{toolRef.version}</span>
                  {isUnresolved(toolRef) && (
                    <Badge variant="warning"><WarningIcon /> unresolved</Badge>
                  )}
                </div>
                <button
                  onClick={() => handleRemoveTool(toolRef)}
                  style={removeToolButtonStyle}
                  disabled={saving}
                  aria-label={`Remove ${toolRef.name}`}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Gateway tool browser */}
      <div style={sectionStyle}>
        <label style={labelStyle}>Available Gateway Tools</label>
        <Input
          inputSize="sm"
          type="text"
          value={filterText}
          onChange={e => setFilterText(e.target.value)}
          placeholder="Filter tools by name or description..."
          disabled={saving}
        />

        {loadingTools && <div style={loadingStyle}>Loading gateway tools...</div>}
        {toolsError && <div style={toolsErrorStyle}>{toolsError}</div>}

        {!loadingTools && !toolsError && (
          <div style={toolBrowserListStyle}>
            {filteredTools.length === 0 && debouncedFilter && (
              <div style={noMatchStyle}>No tools match your filter</div>
            )}
            {filteredTools.length === 0 && !debouncedFilter && gatewayTools.length === 0 && (
              <div style={noMatchStyle}>No tools available on the Gateway</div>
            )}
            {(() => {
              // Group tools by source
              const groups: Record<string, typeof filteredTools> = {};
              for (const tool of filteredTools) {
                const source = tool.source || 'Other';
                if (!groups[source]) groups[source] = [];
                groups[source].push(tool);
              }
              return Object.entries(groups).map(([source, tools]) => (
                <div key={source}>
                  <div style={toolSectionHeaderStyle}>{source}</div>
                  {tools.map(tool => {
                    const selected = isToolSelected(tool);
                    return (
                      <div key={`${tool.name}@${tool.version}`} style={toolBrowserItemStyle}>
                        <div style={toolBrowserInfoStyle}>
                          <span style={toolNameStyle} title={tool.name}>{displayToolName(tool.name)}</span>
                          <span style={toolVersionStyle}>v{tool.version}</span>
                          {tool.description && (
                            <div style={toolDescStyle}>
                              {tool.description.length > 60 ? tool.description.slice(0, 60) + '…' : tool.description}
                            </div>
                          )}
                        </div>
                        <button
                          onClick={() => selected ? handleRemoveTool({ name: tool.name, version: tool.version }) : handleAddTool(tool)}
                          style={selected ? removeFromSelectionButtonStyle : addToolButtonStyle}
                          disabled={saving}
                          aria-label={selected ? `Remove ${tool.name}` : `Add ${tool.name}`}
                        >
                          {selected ? '−' : '+'}
                        </button>
                      </div>
                    );
                  })}
                </div>
              ));
            })()}
          </div>
        )}
      </div>

      {/* Actions */}
      <div style={actionsStyle}>
        <Button
          variant="primary"
          size="md"
          onClick={handleSave}
          disabled={!isFormValid}
          busy={saving}
          busyLabel="Saving…"
        >
          {isEditMode ? 'Update Collection' : 'Create Collection'}
        </Button>
        <Button variant="ghost" size="md" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// --- Styles ---

const editorContainerStyle: React.CSSProperties = {
  padding: '10px 0',
  borderBottom: `1px solid ${theme.colors.border}`,
  marginBottom: 8,
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 8,
};

const headerTitleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
};

const closeButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.textTertiary,
  fontSize: 'var(--fs-xl)',
  cursor: 'pointer',
  padding: '0 4px',
  lineHeight: 1,
};

const errorStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '6px 8px',
  marginBottom: 8,
  backgroundColor: tint(theme.colors.error),
  border: `1px solid ${tint(theme.colors.error, 40)}`,
  borderRadius: theme.radius.button,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
};

const dismissErrorStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.error,
  fontSize: 'var(--fs-lg)',
  cursor: 'pointer',
  padding: '0 4px',
  lineHeight: 1,
};

const formFieldStyle: React.CSSProperties = {
  marginBottom: 8,
  position: 'relative',
};

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  marginBottom: 4,
  fontWeight: 500,
};
const charCountStyle: React.CSSProperties = {
  position: 'absolute',
  right: 4,
  top: 0,
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textSecondary,
};

const sectionStyle: React.CSSProperties = {
  marginBottom: 10,
};

const emptySelectionStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  fontStyle: 'italic',
  padding: '4px 0',
};

const selectedToolsListStyle: React.CSSProperties = {
  maxHeight: 120,
  overflowY: 'auto',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  backgroundColor: theme.colors.surfaceSubtle,
};

const selectedToolItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '4px 8px',
  borderBottom: `1px solid ${theme.colors.border}`,
  fontSize: 'var(--fs-xs)',
};

const unresolvedToolStyle: React.CSSProperties = {
  backgroundColor: tint(theme.colors.warning, 10),
  border: `1px solid ${tint(theme.colors.warning, 30)}`,
  borderRadius: theme.radius.badge,
};

const toolInfoStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  flex: 1,
  minWidth: 0,
};

const toolNameStyle: React.CSSProperties = {
  fontWeight: 500,
  color: theme.colors.textPrimary,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const toolVersionStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textTertiary,
  flexShrink: 0,
  ...numericStyle,
};
const removeToolButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.textTertiary,
  fontSize: 'var(--fs-xs)',
  cursor: 'pointer',
  padding: '2px 4px',
  borderRadius: theme.radius.badge,
  flexShrink: 0,
};
const loadingStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  padding: '8px 0',
  textAlign: 'center',
};

const toolsErrorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
  padding: '4px 0',
};

const toolBrowserListStyle: React.CSSProperties = {
  maxHeight: 200,
  overflowY: 'auto',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  backgroundColor: theme.colors.surfaceSubtle,
};

const noMatchStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  fontStyle: 'italic',
  padding: '8px',
  textAlign: 'center',
};

const toolSectionHeaderStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  fontWeight: 600,
  color: theme.colors.textTertiary,
  textTransform: 'uppercase',
  letterSpacing: 0.5,
  padding: '6px 8px 3px',
  borderBottom: `1px solid ${theme.colors.border}`,
  background: theme.colors.surfaceSubtle,
};

const toolBrowserItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '5px 8px',
  borderBottom: `1px solid ${theme.colors.border}`,
};

const toolBrowserInfoStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
};

const toolDescStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textTertiary,
  marginTop: 2,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const addToolButtonStyle: React.CSSProperties = {
  width: 22,
  height: 22,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: tint(theme.colors.primary, 20),
  border: `1px solid ${tint(theme.colors.primary, 40)}`,
  borderRadius: theme.radius.badge,
  color: theme.colors.primary,
  fontSize: 'var(--fs-md)',
  cursor: 'pointer',
  flexShrink: 0,
};

const removeFromSelectionButtonStyle: React.CSSProperties = {
  width: 22,
  height: 22,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: tint(theme.colors.error, 20),
  border: `1px solid ${tint(theme.colors.error, 40)}`,
  borderRadius: theme.radius.badge,
  color: theme.colors.error,
  fontSize: 'var(--fs-md)',
  cursor: 'pointer',
  flexShrink: 0,
};

const actionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  marginTop: 10,
};
