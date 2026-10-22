// TestHarnessPanel.tsx — Test harness panel for running drafts against sample input.
// Displays form, streaming progress, and results (success/failure/timeout).
// Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9

import React, { useState, useCallback, useRef } from 'react';
import { theme } from '../styles/tokens';
import Button from './ui/Button';
import { useLabContext } from '../LabContext';
import { runHarness, cancelHarnessRun } from '../lab-api';
import type { StreamEvent, StepEvent } from '../lab-api';
import WarningIcon from './icons/WarningIcon';

// --- Types ---

interface TestHarnessPanelProps {
  onClose: () => void;
  /**
   * Label for the panel's own back control. The panel owns it so that Back and Run
   * share ONE toolbar, as the skill editor's Back and Save do; each host passes the
   * wording that fits it, since "← Back to Tools" inside the Settings modal says
   * something that a bare "← Back" does not.
   */
  backLabel?: string;
}

type RunStatus = 'idle' | 'running' | 'succeeded' | 'failed' | 'timeout';

interface RunResult {
  status: 'succeeded' | 'failed' | 'timeout';
  output: string;
  logs: string;
  durationMs: number;
  failureReason?: string;
  failingStepId?: string;
  partialOutput?: string;
}

// --- Component ---

export default function TestHarnessPanel({ onClose, backLabel = '← Back' }: TestHarnessPanelProps) {
  const { harnessTarget, specialtyAgents, skills, toolCollections, publishedSkills,
    publishedSpecialtyAgents, ensureCategoryLoaded, setRightPanelState } = useLabContext();

  React.useEffect(() => {
    void Promise.all([
      ensureCategoryLoaded('skill'),
      ensureCategoryLoaded('specialty-agent'),
      ensureCategoryLoaded('tool-collection'),
    ]);
  }, [ensureCategoryLoaded]);

  // Form state
  const [sampleInput, setSampleInput] = useState('');
  const [agentBinding, setAgentBinding] = useState('');
  const [skillBinding, setSkillBinding] = useState('');
  const [toolCollectionBinding, setToolCollectionBinding] = useState('');
  const [timeoutSeconds, setTimeoutSeconds] = useState(300);

  // Run state
  const [runStatus, setRunStatus] = useState<RunStatus>('idle');
  const [runResult, setRunResult] = useState<RunResult | null>(null);
  const [liveSteps, setLiveSteps] = useState<StepEvent[]>([]);
  const [liveText, setLiveText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [logsExpanded, setLogsExpanded] = useState(false);


  // Run ID for cancellation
  const runIdRef = useRef<string | null>(null);
  const startTimeRef = useRef<number>(0);

  /*
   * Friendly label for the bound target. Showing a raw assetId ("specialty-agent
   * / 38f389e3-…") tells the user nothing about which draft they're testing, so
   * resolve the display name and keep the category as a subtle qualifier.
   *
   * Published versions are searched as well as drafts, and that is not belt-and-
   * braces: publishing CONSUMES the working copy, so testing a skill you have
   * published means the drafts pool cannot contain it — every such run showed a bare
   * UUID.
   */
  const targetName = (() => {
    if (!harnessTarget) return null;
    const { category, assetId } = harnessTarget;
    const pools =
      category === 'skill' ? [skills, publishedSkills] :
      category === 'specialty-agent' ? [specialtyAgents, publishedSpecialtyAgents] :
      category === 'tool-collection' ? [toolCollections] : [];
    for (const pool of pools) {
      const found = (pool as { assetId: string; name: string }[]).find(a => a.assetId === assetId);
      if (found?.name) return found.name;
    }
    return null;
  })();

  // Validation
  const isInputValid = sampleInput.trim().length > 0;
  const canRun = isInputValid && runStatus !== 'running' && harnessTarget !== null;

  // Handle close
  const handleClose = useCallback(() => {
    setRightPanelState('collapsed');
    onClose();
  }, [onClose, setRightPanelState]);

  // Handle Run
  const handleRun = useCallback(async () => {
    if (!canRun || !harnessTarget) return;

    setRunStatus('running');
    setRunResult(null);
    setLiveSteps([]);
    setLiveText('');
    setError(null);
    setLogsExpanded(false);
    startTimeRef.current = Date.now();

    // Generate a run ID for cancellation tracking
    const localRunId = `run-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    runIdRef.current = localRunId;

    const params: {
      targetAssetId: string;
      sampleInput: string;
      bindings?: { agentAssetId?: string; skillAssetId?: string; toolCollectionAssetId?: string };
      timeoutSeconds?: number;
    } = {
      targetAssetId: harnessTarget.assetId,
      sampleInput: sampleInput,
      timeoutSeconds: timeoutSeconds,
    };

    // Add bindings if specified
    if (agentBinding || skillBinding || toolCollectionBinding) {
      params.bindings = {};
      if (agentBinding) params.bindings.agentAssetId = agentBinding;
      if (skillBinding) params.bindings.skillAssetId = skillBinding;
      if (toolCollectionBinding) params.bindings.toolCollectionAssetId = toolCollectionBinding;
    }

    let accumulatedText = '';
    let accumulatedLogs = '';
    let summaryDurationMs: number | null = null;

    const onEvent = (event: StreamEvent) => {
      switch (event.type) {
        case 'step':
          setLiveSteps(prev => {
            const existing = prev.findIndex(
              s => s.node_id === event.step.node_id && event.step.node_id
            );
            if (existing >= 0) {
              const updated = [...prev];
              updated[existing] = event.step;
              return updated;
            }
            return [...prev, event.step];
          });
          break;
        case 'text':
          accumulatedText += event.text;
          setLiveText(accumulatedText);
          break;
        case 'execution_summary':
          summaryDurationMs = event.summary.total_duration_ms;
          break;
      }
    };

    try {
      await runHarness(params, onEvent);

      const durationMs = summaryDurationMs ?? (Date.now() - startTimeRef.current);

      setRunResult({
        status: 'succeeded',
        output: accumulatedText,
        logs: accumulatedLogs,
        durationMs,
      });
      setRunStatus('succeeded');
    } catch (err) {
      const durationMs = Date.now() - startTimeRef.current;
      const errorMessage = err instanceof Error ? err.message : 'Unknown error';

      // Check if it's a timeout error
      if (errorMessage.toLowerCase().includes('timeout') || errorMessage.toLowerCase().includes('timed out')) {
        setRunResult({
          status: 'timeout',
          output: '',
          logs: accumulatedLogs,
          durationMs,
          partialOutput: accumulatedText || undefined,
        });
        setRunStatus('timeout');
      } else {
        // Parse failure details if available
        let failingStepId: string | undefined;
        try {
          const parsed = JSON.parse(errorMessage);
          if (parsed.stepId) failingStepId = parsed.stepId;
        } catch {
          // Not JSON, use as-is
        }

        setRunResult({
          status: 'failed',
          output: '',
          logs: accumulatedLogs,
          durationMs,
          failureReason: errorMessage,
          failingStepId,
          partialOutput: accumulatedText || undefined,
        });
        setRunStatus('failed');
      }
    } finally {
      runIdRef.current = null;
    }
  }, [canRun, harnessTarget, sampleInput, agentBinding, skillBinding, timeoutSeconds]);

  // Handle Cancel
  const handleCancel = useCallback(async () => {
    const runId = runIdRef.current;
    if (runId) {
      try {
        await cancelHarnessRun(runId);
      } catch {
        // Best-effort cancellation
      }
    }
    setRunStatus('idle');
    setLiveSteps([]);
    setLiveText('');
    runIdRef.current = null;
  }, []);

  // Handle timeout input change with clamping
  const handleTimeoutChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const val = parseInt(e.target.value, 10);
    if (isNaN(val)) return;
    setTimeoutSeconds(Math.max(1, Math.min(300, val)));
  }, []);



  return (
    <div style={panelContainerStyle}>
      {/*
        * One toolbar: the way out and the primary action. Previously the
        * way out was a host-supplied "← Back to Skills" on its own line ABOVE a
        * panel that carried a title and its own ✕ — two exits a few pixels apart,
        * on two rows — while Run sat at the foot of the form. Matches the skill
        * editor's Back-and-Save row.
        */}
      <div style={toolbarStyle}>
        <Button variant="ghost" size="md" onClick={handleClose}>{backLabel}</Button>
        <Button
          variant="primary"
          size="md"
          onClick={handleRun}
          disabled={!canRun}
          busy={runStatus === 'running'}
          busyLabel="Running…"
        >
          Run
        </Button>
        {runStatus === 'running' && (
          <Button variant="ghost" size="md" onClick={handleCancel}>
            Cancel
          </Button>
        )}
      </div>

      {/* Form */}
      <div style={formContainerStyle}>
        <div>
          <h4 style={formTitleStyle}>Test Harness</h4>
          <div style={hintStyle}>Run this draft against sample input.</div>
        </div>

        {/* Target draft (pre-selected) */}
        <div style={fieldGroupStyle}>
          <label style={labelStyle}>Target Draft</label>
          <div style={targetDisplayStyle}>
            {harnessTarget ? (
              <span style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                <span style={targetNameStyle}>{targetName ?? harnessTarget.assetId}</span>
                <span style={targetCategoryBadgeStyle}>{harnessTarget.category}</span>
              </span>
            ) : 'No target selected'}
          </div>
        </div>

        {/* Sample input */}
        <div style={fieldGroupStyle}>
          <label style={labelStyle}>Sample Input *</label>
          <textarea
            style={textareaStyle}
            value={sampleInput}
            onChange={e => setSampleInput(e.target.value.slice(0, 50000))}
            placeholder="Enter sample input for the test run..."
            maxLength={50000}
            rows={6}
            disabled={runStatus === 'running'}
          />
          <span style={charCountStyle}>
            {sampleInput.length.toLocaleString()} / 50,000
          </span>
          {!isInputValid && sampleInput.length === 0 && runStatus === 'idle' && (
            <span style={validationHintStyle}>Sample input is required to run</span>
          )}
        </div>

        {/* Optional bindings */}
        <div style={fieldGroupStyle}>
          <label style={labelStyle}>Bindings (optional)</label>
          <div style={bindingsRowStyle}>
            <div style={bindingFieldStyle}>
              <label style={subLabelStyle}>Agent</label>
              <select
                style={selectStyle}
                value={agentBinding}
                onChange={e => setAgentBinding(e.target.value)}
                disabled={runStatus === 'running'}
              >
                <option value="">None</option>
                {specialtyAgents.map(agent => (
                  <option key={agent.assetId} value={agent.assetId}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </div>
            <div style={bindingFieldStyle}>
              <label style={subLabelStyle}>Skill</label>
              <select
                style={selectStyle}
                value={skillBinding}
                onChange={e => setSkillBinding(e.target.value)}
                disabled={runStatus === 'running'}
              >
                <option value="">None</option>
                {skills.map(skill => (
                  <option key={skill.assetId} value={skill.assetId}>
                    {skill.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 2 }}>
            <label style={subLabelStyle}>Tool Collection</label>
            <select
              style={{ ...selectStyle, width: '100%' }}
              value={toolCollectionBinding}
              onChange={e => setToolCollectionBinding(e.target.value)}
              disabled={runStatus === 'running'}
            >
              <option value="">All tools (no restriction)</option>
              {toolCollections.map(tc => (
                <option key={tc.assetId} value={tc.assetId}>
                  {/* No count when the inventory source did not report one — a
                      confident "(0 tools)" next to a collection that has nine is
                      worse than no parenthetical at all. */}
                  {tc.name}{tc.toolRefs ? ` (${tc.toolRefs.length} tools)` : ''}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Timeout */}
        <div style={fieldGroupStyle}>
          <label style={labelStyle}>Timeout (seconds)</label>
          <input
            type="number"
            style={numberInputStyle}
            value={timeoutSeconds}
            onChange={handleTimeoutChange}
            min={1}
            max={300}
            disabled={runStatus === 'running'}
          />
          <span style={hintStyle}>1–300 seconds</span>
        </div>

        {/* Action buttons */}
      </div>

      {/* Progress indicator during run */}
      {runStatus === 'running' && (
        <div style={progressContainerStyle}>
          <div style={progressHeaderStyle}>
            <span style={spinnerStyle}>⟳</span>
            <span style={progressLabelStyle}>Running...</span>
          </div>
          {/* Live steps */}
          {liveSteps.length > 0 && (
            <div style={liveStepsContainerStyle}>
              {liveSteps.map((step, idx) => (
                <div key={step.node_id || idx} style={liveStepItemStyle}>
                  <span style={stepStatusDotStyle(step.status)}>
                    {stepIcon(step.status)}
                  </span>
                  <span style={liveStepLabelStyle}>{step.label}</span>
                  {step.duration_ms !== undefined && (
                    <span style={liveStepDurationStyle}>
                      {step.duration_ms < 1000
                        ? `${step.duration_ms}ms`
                        : `${(step.duration_ms / 1000).toFixed(1)}s`}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
          {/* Live text preview */}
          {liveText && (
            <div style={liveTextPreviewStyle}>
              {liveText.slice(0, 500)}{liveText.length > 500 ? '...' : ''}
            </div>
          )}
        </div>
      )}

      {/* Results */}
      {runResult && runStatus !== 'running' && (
        <div style={resultsContainerStyle}>
          {/* Success */}
          {runResult.status === 'succeeded' && (
            <>
              <div style={resultHeaderSuccessStyle}>
                <span>✓ Succeeded</span>
                <span style={durationBadgeStyle}>
                  {formatDuration(runResult.durationMs)}
                </span>
              </div>
              {runResult.output && (
                <div style={outputContainerStyle}>
                  <label style={outputLabelStyle}>Output</label>
                  <pre style={outputPreStyle}>{runResult.output}</pre>
                </div>
              )}
              {runResult.logs && (
                <div style={logsContainerStyle}>
                  <button
                    style={logsToggleStyle}
                    onClick={() => setLogsExpanded(prev => !prev)}
                  >
                    {logsExpanded ? '▼' : '▶'} Logs
                  </button>
                  {logsExpanded && (
                    <pre style={logsPreStyle}>{runResult.logs}</pre>
                  )}
                </div>
              )}
            </>
          )}

          {/* Failure */}
          {runResult.status === 'failed' && (
            <>
              <div style={resultHeaderFailureStyle}>
                <span>✗ Failed</span>
                <span style={durationBadgeStyle}>
                  {formatDuration(runResult.durationMs)}
                </span>
              </div>
              {runResult.failureReason && (
                <div style={failureReasonStyle}>
                  <label style={outputLabelStyle}>Failure Reason</label>
                  <div style={failureTextStyle}>{runResult.failureReason}</div>
                </div>
              )}
              {runResult.failingStepId && (
                <div style={failingStepStyle}>
                  <span style={failingStepLabelStyle}>Failing Step:</span>
                  <span>{runResult.failingStepId}</span>
                </div>
              )}
              {runResult.partialOutput && (
                <div style={outputContainerStyle}>
                  <label style={outputLabelStyle}>Partial Output</label>
                  <pre style={outputPreStyle}>{runResult.partialOutput}</pre>
                </div>
              )}
            </>
          )}

          {/* Timeout */}
          {runResult.status === 'timeout' && (
            <>
              <div style={resultHeaderTimeoutStyle}>
                <span>⏱ Timeout</span>
                <span style={durationBadgeStyle}>
                  {formatDuration(runResult.durationMs)}
                </span>
              </div>
              <div style={timeoutMessageStyle}>
                The run exceeded the configured timeout of {timeoutSeconds} seconds.
              </div>
              {runResult.partialOutput && (
                <div style={outputContainerStyle}>
                  <label style={outputLabelStyle}>Partial Results</label>
                  <pre style={outputPreStyle}>{runResult.partialOutput}</pre>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* Error display */}
      {error && (
        <div style={errorContainerStyle}>
          <span style={errorTextStyle}><WarningIcon /> {error}</span>
          <button style={dismissErrorStyle} onClick={() => setError(null)}>✕</button>
        </div>
      )}
    </div>
  );
}

// --- Helpers ---

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}


function stepIcon(status: StepEvent['status']): string {
  switch (status) {
    case 'running': return '⟳';
    case 'done':
    case 'completed': return '✓';
    case 'failed': return '✗';
    case 'skipped': return '⊘';
    case 'pending': return '○';
    default: return '○';
  }
}

function stepStatusDotStyle(status: StepEvent['status']): React.CSSProperties {
  const base: React.CSSProperties = { fontSize: 'var(--fs-2xs)', marginRight: 6, fontWeight: 600 };
  switch (status) {
    case 'running': return { ...base, color: theme.colors.primary };
    case 'done':
    case 'completed': return { ...base, color: theme.colors.success };
    case 'failed': return { ...base, color: theme.colors.error };
    case 'skipped': return { ...base, color: theme.colors.textSecondary };
    case 'pending': return { ...base, color: theme.colors.textSecondary };
    default: return { ...base, color: theme.colors.textSecondary };
  }
}

// --- Styles ---

const panelContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  minHeight: 0,
  background: 'transparent',
};

const toolbarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '2px 0 4px',
  flexShrink: 0,
};

const formContainerStyle: React.CSSProperties = {
  padding: 16,
  display: 'flex',
  flexDirection: 'column',
  gap: 14,
  // Bounded so the fields do not stretch the full width of a wide pane, matching the
  // editor's column rather than spreading into a dashboard.
  maxWidth: 720,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surface,
};

const formTitleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-md)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};

const fieldGroupStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

const labelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontWeight: 500,
  color: theme.colors.textSecondary,
};

const subLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  marginBottom: 2,
};

const targetDisplayStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textPrimary,
  padding: '6px 10px',
  background: theme.colors.background,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
};

const targetNameStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.textPrimary,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const targetCategoryBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  padding: '2px 6px',
  borderRadius: theme.radius.badge,
  background: theme.colors.background,
  border: `1px solid ${theme.colors.border}`,
  color: theme.colors.textSecondary,
  flexShrink: 0,
};

const textareaStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  // Mono here is deliberate — sample input is data, not prose.
  fontFamily: theme.typography.fontMono,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  padding: '8px 10px',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  resize: 'vertical',
  minHeight: 100,
  outline: 'none',
};

const charCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  textAlign: 'right',
};

const validationHintStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.warning,
};

const bindingsRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
};

const bindingFieldStyle: React.CSSProperties = {
  flex: 1,
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
};

const selectStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  padding: '6px 8px',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  fontFamily: theme.typography.fontFamily,
  outline: 'none',
};

const numberInputStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  padding: '6px 10px',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  fontFamily: theme.typography.fontFamily,
  width: 80,
  outline: 'none',
};

const hintStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
};

// Progress styles

const progressContainerStyle: React.CSSProperties = {
  padding: 16,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surface,
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
};

const progressHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
};

const spinnerStyle: React.CSSProperties = {
  fontSize: 'var(--fs-md)',
  color: theme.colors.primary,
  animation: 'spin 1s linear infinite',
};

const progressLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  fontWeight: 500,
  color: theme.colors.primary,
};

const liveStepsContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  paddingLeft: 4,
};

const liveStepItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
};

const liveStepLabelStyle: React.CSSProperties = {
  flex: 1,
};

const liveStepDurationStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  marginLeft: 8,
};

const liveTextPreviewStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  fontFamily: theme.typography.fontMono,
  padding: '6px 8px',
  background: theme.colors.background,
  borderRadius: theme.radius.button,
  maxHeight: 80,
  overflow: 'hidden',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

// Results styles

const resultsContainerStyle: React.CSSProperties = {
  padding: 16,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surface,
  display: 'flex',
  flexDirection: 'column',
  gap: 10,
};

const resultHeaderSuccessStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.success,
};

const resultHeaderFailureStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.error,
};

const resultHeaderTimeoutStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.warning,
};

const durationBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  fontWeight: 400,
  color: theme.colors.textSecondary,
  background: theme.colors.background,
  padding: '2px 6px',
  borderRadius: theme.radius.button,
};

const outputContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

const outputLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  fontWeight: 500,
  color: theme.colors.textSecondary,
};

const outputPreStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontFamily: theme.typography.fontMono,
  padding: '8px 10px',
  background: theme.colors.background,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  maxHeight: 200,
  overflowY: 'auto',
  margin: 0,
};

const logsContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

const logsToggleStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  padding: '2px 0',
  textAlign: 'left',
};

const logsPreStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  fontFamily: theme.typography.fontMono,
  padding: '6px 8px',
  background: theme.colors.background,
  borderRadius: theme.radius.button,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  maxHeight: 150,
  overflowY: 'auto',
  margin: 0,
};

const failureReasonStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

const failureTextStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
  padding: '6px 8px',
  background: `color-mix(in srgb, ${theme.colors.error} 10%, transparent)`,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.error}`,
};

const failingStepStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
};

const failingStepLabelStyle: React.CSSProperties = {
  fontWeight: 500,
  color: theme.colors.textSecondary,
};

const timeoutMessageStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.warning,
  padding: '6px 8px',
  background: `color-mix(in srgb, ${theme.colors.warning} 12%, transparent)`,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.warning}`,
};

const errorContainerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '8px 16px',
  background: `color-mix(in srgb, ${theme.colors.error} 10%, transparent)`,
  borderTop: `1px solid ${theme.colors.error}`,
};

const errorTextStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
};

const dismissErrorStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  fontSize: 'var(--fs-md)',
  color: theme.colors.error,
  padding: '2px 6px',
};

// Header actions styles

const headerActionsStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};
