import React, { useState, useEffect, useCallback } from 'react';
import { useLabContext } from '../LabContext';
import {
  WorkflowStep,
  createWorkflowDraft,
  updateWorkflowDraft,
  readWorkflowDraft,
  LabApiError,
} from '../lab-api';
import { theme, tint } from '../styles/tokens';
import Button from './ui/Button';
import { Input, Textarea, Select } from './ui/Input';
import WarningIcon from './icons/WarningIcon';

/**
 * WorkflowEditor — Editor for creating and editing workflow drafts.
 * Renders workflow name input (max 100 chars) and an ordered step list (max 20 steps).
 * Each step has: agent selector, skill selector, tool collection selector,
 * prompt template textarea (max 4000 chars), and input dependency checkboxes.
 * Supports add, reorder (up/down), and remove step operations.
 * On reorder/remove: cleans up invalid forward references in inputs arrays.
 * Displays caution icon + "unresolved" for steps with missing asset references.
 * Disables save until all references are resolved.
 * On save, calls createWorkflowDraft or updateWorkflowDraft.
 * On validation error, displays inline error identifying invalid references.
 *
 * Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.10, 5.11
 */

interface WorkflowEditorProps {
  /**
   * Render this editor's own title + close control. `false` when a frame already
   * supplies them (see `CenterEditor`), which is what stops one form showing two
   * titles and three ways out.
   */
  showHeader?: boolean;
  assetId?: string; // If provided, edit mode — fetch existing workflow
  onClose: () => void;
}

function generateStepId(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `step-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function createEmptyStep(): WorkflowStep {
  return {
    stepId: generateStepId(),
    agent: { kind: 'core' },
    skill: { assetId: null },
    toolCollection: { assetId: null },
    prompt: '',
    inputs: [],
  };
}

/**
 * Checks if a step has unresolved asset references by comparing against available context data.
 */
function getUnresolvedRefs(
  step: WorkflowStep,
  specialtyAgentIds: Set<string>,
  skillIds: Set<string>,
  toolCollectionIds: Set<string>,
): string[] {
  const unresolved: string[] = [];

  // Check agent reference
  if (step.agent.kind === 'specialty' && step.agent.assetId) {
    if (!specialtyAgentIds.has(step.agent.assetId)) {
      unresolved.push(`Agent: ${step.agent.ref || step.agent.assetId}`);
    }
  }

  // Check skill reference
  if (step.skill.assetId && !skillIds.has(step.skill.assetId)) {
    unresolved.push(`Skill: ${step.skill.assetId}`);
  }

  // Check tool collection reference
  if (step.toolCollection.assetId && !toolCollectionIds.has(step.toolCollection.assetId)) {
    unresolved.push(`Tool Collection: ${step.toolCollection.assetId}`);
  }

  return unresolved;
}

/**
 * Removes invalid forward references from inputs arrays after reorder.
 * A valid input reference must reference a stepId that appears at an earlier index.
 * Exported for testability (Property 9: Workflow step dependency invariant).
 */
export function cleanupDependencies(steps: WorkflowStep[]): WorkflowStep[] {
  return steps.map((step, index) => {
    const earlierStepIds = new Set(steps.slice(0, index).map(s => s.stepId));
    const validInputs = step.inputs.filter(id => earlierStepIds.has(id));
    if (validInputs.length !== step.inputs.length) {
      return { ...step, inputs: validInputs };
    }
    return step;
  });
}

export default function WorkflowEditor({ assetId, onClose, showHeader = true }: WorkflowEditorProps) {
  const { skills, specialtyAgents, toolCollections, ensureCategoryLoaded, refreshCategory } = useLabContext();

  React.useEffect(() => {
    void Promise.all([
      ensureCategoryLoaded('skill'),
      ensureCategoryLoaded('specialty-agent'),
      ensureCategoryLoaded('tool-collection'),
    ]);
  }, [ensureCategoryLoaded]);

  const [name, setName] = useState('');
  const [steps, setSteps] = useState<WorkflowStep[]>([]);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Build lookup sets for unresolved reference detection
  const specialtyAgentIds = new Set(specialtyAgents.map(a => a.assetId));
  const skillIds = new Set(skills.map(s => s.assetId));
  const toolCollectionIds = new Set(toolCollections.map(tc => tc.assetId));

  // Check if any step has unresolved references
  const hasUnresolvedRefs = steps.some(
    step => getUnresolvedRefs(step, specialtyAgentIds, skillIds, toolCollectionIds).length > 0
  );

  // Form validity: name is non-empty and no unresolved refs
  const canSave = name.trim().length > 0 && steps.length > 0 && !hasUnresolvedRefs && !saving;

  // Load existing workflow in edit mode
  useEffect(() => {
    if (!assetId) return;
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const workflow = await readWorkflowDraft(assetId!);
        if (cancelled) return;
        setName(workflow.name);
        setSteps(workflow.steps);
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load workflow';
        setError(message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => { cancelled = true; };
  }, [assetId]);

  // --- Step mutations ---

  const handleAddStep = useCallback(() => {
    if (steps.length >= 20) return;
    setSteps(prev => [...prev, createEmptyStep()]);
  }, [steps.length]);

  const handleRemoveStep = useCallback((stepId: string) => {
    setSteps(prev => {
      const filtered = prev.filter(s => s.stepId !== stepId);
      // Remove dangling references to the removed step
      return filtered.map(s => ({
        ...s,
        inputs: s.inputs.filter(id => id !== stepId),
      }));
    });
  }, []);

  const handleMoveUp = useCallback((index: number) => {
    if (index <= 0) return;
    setSteps(prev => {
      const newSteps = [...prev];
      [newSteps[index - 1], newSteps[index]] = [newSteps[index], newSteps[index - 1]];
      return cleanupDependencies(newSteps);
    });
  }, []);

  const handleMoveDown = useCallback((index: number) => {
    setSteps(prev => {
      if (index >= prev.length - 1) return prev;
      const newSteps = [...prev];
      [newSteps[index], newSteps[index + 1]] = [newSteps[index + 1], newSteps[index]];
      return cleanupDependencies(newSteps);
    });
  }, []);

  const handleStepChange = useCallback((stepId: string, updates: Partial<WorkflowStep>) => {
    setSteps(prev => prev.map(s => s.stepId === stepId ? { ...s, ...updates } : s));
  }, []);

  const handleToggleInput = useCallback((stepId: string, inputStepId: string) => {
    setSteps(prev => prev.map(s => {
      if (s.stepId !== stepId) return s;
      const hasInput = s.inputs.includes(inputStepId);
      return {
        ...s,
        inputs: hasInput
          ? s.inputs.filter(id => id !== inputStepId)
          : [...s.inputs, inputStepId],
      };
    }));
  }, []);

  // --- Save ---

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);

    try {
      if (assetId) {
        await updateWorkflowDraft(assetId, { name: name.trim(), steps });
      } else {
        await createWorkflowDraft(name.trim(), steps);
      }
      await refreshCategory('workflow');
      onClose();
    } catch (err) {
      if (err instanceof LabApiError) {
        setError(err.message);
      } else {
        setError(err instanceof Error ? err.message : 'Failed to save workflow');
      }
    } finally {
      setSaving(false);
    }
  };

  const handleDismissError = () => setError(null);

  // --- Render ---

  if (loading) {
    return (
      <div style={editorContainerStyle}>
        {showHeader && (
          <div style={headerStyle}>
            <span style={titleStyle}>{assetId ? 'Edit Workflow' : 'New Workflow'}</span>
            <button onClick={onClose} style={closeButtonStyle} aria-label="Close editor">✕</button>
          </div>
        )}
        <div style={loadingStyle}>Loading workflow...</div>
      </div>
    );
  }

  return (
    <div style={editorContainerStyle}>
      {/* Header */}
      {showHeader && (
        <div style={headerStyle}>
          <span style={titleStyle}>{assetId ? 'Edit Workflow' : 'New Workflow'}</span>
          <button onClick={onClose} style={closeButtonStyle} aria-label="Close editor">✕</button>
        </div>
      )}

      {/* Error display */}
      {error && (
        <div style={inlineErrorStyle}>
          <span>{error}</span>
          <button onClick={handleDismissError} style={dismissButtonStyle} aria-label="Dismiss error">✕</button>
        </div>
      )}

      {/* Workflow name */}
      <div style={formFieldStyle}>
        <label style={labelStyle} htmlFor="workflow-name-input">Workflow Name</label>
        <Input
          id="workflow-name-input"
          type="text"
          value={name}
          onChange={e => setName(e.target.value)}
          maxLength={100}
          placeholder="Workflow name (max 100 chars)"
          disabled={saving}
        />
        {name.length > 0 && (
          <span style={charCountStyle}>{name.length}/100</span>
        )}
      </div>

      {/* Steps list */}
      <div style={stepsHeaderStyle}>
        <span style={stepsLabelStyle}>Steps ({steps.length}/20)</span>
        <button
          onClick={handleAddStep}
          disabled={steps.length >= 20 || saving}
          style={{
            ...addStepButtonStyle,
            opacity: (steps.length >= 20 || saving) ? 0.5 : 1,
            cursor: (steps.length >= 20 || saving) ? 'not-allowed' : 'pointer',
          }}
        >
          + Add Step
        </button>
      </div>

      <div style={stepsListStyle}>
        {steps.length === 0 && (
          <div style={emptyStepsStyle}>No steps yet. Click "+ Add Step" to begin.</div>
        )}
        {steps.map((step, index) => {
          const unresolvedRefs = getUnresolvedRefs(step, specialtyAgentIds, skillIds, toolCollectionIds);
          const earlierSteps = steps.slice(0, index);

          return (
            <StepEditor
              key={step.stepId}
              step={step}
              index={index}
              totalSteps={steps.length}
              earlierSteps={earlierSteps}
              unresolvedRefs={unresolvedRefs}
              specialtyAgents={specialtyAgents}
              skills={skills}
              toolCollections={toolCollections}
              disabled={saving}
              onMoveUp={() => handleMoveUp(index)}
              onMoveDown={() => handleMoveDown(index)}
              onRemove={() => handleRemoveStep(step.stepId)}
              onChange={(updates) => handleStepChange(step.stepId, updates)}
              onToggleInput={(inputStepId) => handleToggleInput(step.stepId, inputStepId)}
            />
          );
        })}
      </div>

      {/* Unresolved warning */}
      {hasUnresolvedRefs && (
        <div style={unresolvedWarningStyle}>
          <WarningIcon /> Some steps have unresolved asset references. Resolve or remove them before saving.
        </div>
      )}

      {/* Actions */}
      <div style={formActionsStyle}>
        <Button variant="primary" size="md" onClick={handleSave} disabled={!canSave} busy={saving} busyLabel="Saving…">
          {assetId ? 'Update Workflow' : 'Create Workflow'}
        </Button>
        <Button variant="ghost" size="md" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// =============================================================================
// StepEditor sub-component
// =============================================================================

interface StepEditorProps {
  step: WorkflowStep;
  index: number;
  totalSteps: number;
  earlierSteps: WorkflowStep[];
  unresolvedRefs: string[];
  specialtyAgents: { assetId: string; name: string }[];
  skills: { assetId: string; name: string }[];
  toolCollections: { assetId: string; name: string }[];
  disabled: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
  onChange: (updates: Partial<WorkflowStep>) => void;
  onToggleInput: (inputStepId: string) => void;
}

function StepEditor({
  step,
  index,
  totalSteps,
  earlierSteps,
  unresolvedRefs,
  specialtyAgents,
  skills,
  toolCollections,
  disabled,
  onMoveUp,
  onMoveDown,
  onRemove,
  onChange,
  onToggleInput,
}: StepEditorProps) {
  const hasUnresolved = unresolvedRefs.length > 0;

  const handleAgentChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const value = e.target.value;
    if (value === 'core') {
      onChange({ agent: { kind: 'core' } });
    } else {
      const agent = specialtyAgents.find(a => a.assetId === value);
      onChange({ agent: { kind: 'specialty', assetId: value, ref: agent?.name } });
    }
  };

  const handleSkillChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const value = e.target.value;
    onChange({ skill: { assetId: value || null } });
  };

  const handleToolCollectionChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const value = e.target.value;
    onChange({ toolCollection: { assetId: value || null } });
  };

  const handlePromptChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    onChange({ prompt: e.target.value });
  };

  return (
    <div style={{ ...stepContainerStyle, borderColor: hasUnresolved ? theme.colors.warning : theme.colors.border }}>
      {/* Step header */}
      <div style={stepHeaderStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1 }}>
          <span style={stepNumberStyle}>
            {index + 1}.
          </span>
          <input
            type="text"
            value={step.label || ''}
            onChange={e => onChange({ ...step, label: e.target.value })}
            placeholder={`Step ${index + 1} name`}
            style={stepLabelInputStyle}
            disabled={disabled}
            maxLength={60}
          />
          {hasUnresolved && <span style={cautionIconStyle} title="Unresolved references"><WarningIcon /></span>}
        </div>
        <div style={stepActionsStyle}>
          <button
            onClick={onMoveUp}
            disabled={index === 0 || disabled}
            style={reorderButtonStyle}
            title="Move up"
            aria-label={`Move step ${index + 1} up`}
          >
            ↑
          </button>
          <button
            onClick={onMoveDown}
            disabled={index === totalSteps - 1 || disabled}
            style={reorderButtonStyle}
            title="Move down"
            aria-label={`Move step ${index + 1} down`}
          >
            ↓
          </button>
          <button
            onClick={onRemove}
            disabled={disabled}
            style={removeButtonStyle}
            title="Remove step"
            aria-label={`Remove step ${index + 1}`}
          >
            ✕
          </button>
        </div>
      </div>

      {/* Unresolved refs detail */}
      {hasUnresolved && (
        <div style={unresolvedDetailStyle}>
          {unresolvedRefs.map((ref, i) => (
            <div key={i} style={unresolvedRefStyle}><WarningIcon /> {ref}</div>
          ))}
        </div>
      )}

      {/* Agent selector */}
      <div style={stepFieldStyle}>
        <label style={stepLabelStyle}>Agent</label>
        <Select
          value={step.agent.kind === 'core' ? 'core' : (step.agent.assetId || '')}
          onChange={handleAgentChange}
          disabled={disabled}
        >
          <option value="core">Core Task Agent</option>
          {specialtyAgents.map(agent => (
            <option key={agent.assetId} value={agent.assetId}>{agent.name}</option>
          ))}
        </Select>
      </div>

      {/* Skill selector */}
      <div style={stepFieldStyle}>
        <label style={stepLabelStyle}>Skill</label>
        <Select
          value={step.skill.assetId || ''}
          onChange={handleSkillChange}
          disabled={disabled}
        >
          <option value="">None</option>
          {skills.map(skill => (
            <option key={skill.assetId} value={skill.assetId}>{skill.name}</option>
          ))}
        </Select>
      </div>

      {/* Tool Collection selector */}
      <div style={stepFieldStyle}>
        <label style={stepLabelStyle}>Tool Collection</label>
        <Select
          value={step.toolCollection.assetId || ''}
          onChange={handleToolCollectionChange}
          disabled={disabled}
        >
          <option value="">None</option>
          {toolCollections.map(tc => (
            <option key={tc.assetId} value={tc.assetId}>{tc.name}</option>
          ))}
        </Select>
      </div>

      {/* Execution Mode */}
      <div style={stepFieldStyle}>
        <label style={stepLabelStyle}>Execution Mode</label>
        <Select
          value={step.executionMode || 'sequential'}
          onChange={e => onChange({ ...step, executionMode: e.target.value as 'sequential' | 'parallel-map' | 'conditional' })}
          disabled={disabled}
        >
          <option value="sequential">Sequential (default)</option>
          <option value="parallel-map">Parallel Map (iterate over input array)</option>
          <option value="conditional">Conditional (run only if condition met)</option>
        </Select>
        {step.executionMode === 'parallel-map' && (
          <input
            type="text"
            value={step.mapInput || ''}
            onChange={e => onChange({ ...step, mapInput: e.target.value })}
            placeholder="JSONPath to iterate (e.g., $.categories)"
            style={{ ...selectStyle, marginTop: 4 }}
            disabled={disabled}
          />
        )}
        {step.executionMode === 'conditional' && (
          <input
            type="text"
            value={step.condition || ''}
            onChange={e => onChange({ ...step, condition: e.target.value })}
            placeholder="Condition (e.g., $.severity == 'HIGH')"
            style={{ ...selectStyle, marginTop: 4 }}
            disabled={disabled}
          />
        )}
      </div>

      {/* Prompt template */}
      <div style={stepFieldStyle}>
        <label style={stepLabelStyle}>
          Prompt Template
          {step.prompt.length > 0 && (
            <span style={promptCharCountStyle}>{step.prompt.length}/4000</span>
          )}
        </label>
        <Textarea
          mono
          value={step.prompt}
          onChange={handlePromptChange}
          maxLength={4000}
          placeholder="Enter prompt template (max 4000 chars)..."
          rows={3}
          disabled={disabled}
        />
      </div>

      {/* Output Schema */}
      <div style={stepFieldStyle}>
        <label style={stepLabelStyle}>Output Schema (optional)</label>
        <Input
          type="text"
          value={step.outputSchema || ''}
          onChange={e => onChange({ ...step, outputSchema: e.target.value })}
          placeholder="Expected output format (e.g., JSON with categories array)"
          disabled={disabled}
        />
      </div>

      {/* Input dependencies */}
      {earlierSteps.length > 0 && (
        <div style={stepFieldStyle}>
          <label style={stepLabelStyle}>Input Dependencies</label>
          <div style={dependenciesContainerStyle}>
            {earlierSteps.map((earlier, i) => (
              <label key={earlier.stepId} style={checkboxLabelStyle}>
                <input
                  type="checkbox"
                  checked={step.inputs.includes(earlier.stepId)}
                  onChange={() => onToggleInput(earlier.stepId)}
                  disabled={disabled}
                  style={checkboxStyle}
                />
                <span>Step {i + 1}{earlier.label ? `: ${earlier.label}` : (earlier.prompt ? `: ${earlier.prompt.slice(0, 30)}${earlier.prompt.length > 30 ? '...' : ''}` : '')}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Styles
// =============================================================================

const editorContainerStyle: React.CSSProperties = {
  padding: '12px 0',
  borderBottom: `1px solid ${theme.colors.border}`,
  marginBottom: 8,
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 12,
};

const titleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-md)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
};

const closeButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.textSecondary,
  fontSize: 'var(--fs-xl)',
  cursor: 'pointer',
  padding: '0 4px',
  lineHeight: 1,
};

const loadingStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  fontStyle: 'italic',
  padding: '16px 0',
  textAlign: 'center',
};

const formFieldStyle: React.CSSProperties = {
  marginBottom: 12,
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

const stepsHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 8,
};

const stepsLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  fontWeight: 500,
};

const addStepButtonStyle: React.CSSProperties = {
  padding: '4px 10px',
  fontSize: 'var(--fs-2xs)',
  backgroundColor: theme.colors.primary,
  color: theme.colors.onPrimary,
  border: 'none',
  borderRadius: theme.radius.button,
  fontWeight: 500,
};

const stepsListStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  minHeight: 100,
  maxHeight: 800,
  overflowY: 'auto',
  marginBottom: 12,
  resize: 'vertical',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  padding: 8,
};

const emptyStepsStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  fontStyle: 'italic',
  padding: '12px 0',
  textAlign: 'center',
};

const stepContainerStyle: React.CSSProperties = {
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  padding: '10px 12px',
  backgroundColor: theme.colors.surfaceSubtle,
};

const stepHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 8,
};

const stepNumberStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
};

const stepLabelInputStyle: React.CSSProperties = {
  flex: 1,
  fontSize: 'var(--fs-xs)',
  fontWeight: 500,
  color: theme.colors.textPrimary,
  border: 'none',
  borderBottom: `1px solid ${theme.colors.border}`,
  background: 'transparent',
  outline: 'none',
  padding: '2px 4px',
};

const cautionIconStyle: React.CSSProperties = {
  color: theme.colors.warning,
  fontSize: 'var(--fs-2xs)',
  marginLeft: 6,
};

const stepActionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 4,
};

const reorderButtonStyle: React.CSSProperties = {
  padding: '2px 6px',
  fontSize: 'var(--fs-xs)',
  backgroundColor: theme.colors.surface,
  color: theme.colors.textSecondary,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.badge,
  cursor: 'pointer',
  lineHeight: 1,
};

const removeButtonStyle: React.CSSProperties = {
  padding: '2px 6px',
  fontSize: 'var(--fs-xs)',
  backgroundColor: theme.colors.surface,
  color: theme.colors.error,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.badge,
  cursor: 'pointer',
  lineHeight: 1,
};

const unresolvedDetailStyle: React.CSSProperties = {
  marginBottom: 8,
  padding: '4px 8px',
  backgroundColor: tint(theme.colors.warning, 10),
  borderRadius: theme.radius.button,
};

const unresolvedRefStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.warning,
  padding: '2px 0',
};

const stepFieldStyle: React.CSSProperties = {
  marginBottom: 8,
};

const stepLabelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textTertiary,
  marginBottom: 3,
  fontWeight: 500,
};

const selectStyle: React.CSSProperties = {
  width: '100%',
  padding: '5px 8px',
  fontSize: 'var(--fs-xs)',
  backgroundColor: theme.colors.surface,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  color: theme.colors.textPrimary,
  outline: 'none',
  boxSizing: 'border-box',
};
const promptCharCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textSecondary,
  marginLeft: 8,
  fontWeight: 400,
};

const dependenciesContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  padding: '4px 0',
};

const checkboxLabelStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textPrimary,
  cursor: 'pointer',
};

const checkboxStyle: React.CSSProperties = {
  margin: 0,
};

const unresolvedWarningStyle: React.CSSProperties = {
  padding: '8px 10px',
  marginBottom: 12,
  backgroundColor: tint(theme.colors.warning, 10),
  border: `1px solid ${tint(theme.colors.warning, 30)}`,
  borderRadius: theme.radius.button,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.warning,
};

const formActionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
};
const inlineErrorStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '6px 8px',
  marginBottom: 8,
  backgroundColor: tint(theme.colors.error, 10),
  border: `1px solid ${tint(theme.colors.error, 30)}`,
  borderRadius: theme.radius.button,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
};

const dismissButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: theme.colors.error,
  fontSize: 'var(--fs-lg)',
  cursor: 'pointer',
  padding: '0 4px',
  lineHeight: 1,
};
