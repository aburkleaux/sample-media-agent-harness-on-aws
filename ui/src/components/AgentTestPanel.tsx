/**
 * AgentTestPanel — resolved config + live test invoke for a Custom Agent.
 *
 * Answers the two questions an owner actually has after building an agent:
 *
 *   1. "What will actually load?" — the **Resolved config** block runs the same
 *      resolver the invoke paths use and lists the exact gateway tools, skills,
 *      and model that would bind. This costs no model tokens, and it is the only
 *      way to see a tool that is *bound but unused*: a run's
 *      `execution_summary.tools` reports tools **called**, so an unused binding
 *      is invisible there.
 *   2. "What does it actually say?" — **Run** invokes the agent for real through
 *      `POST /agent-test`, with the config resolved runtime-side so the console
 *      cannot diverge from a genuine invocation.
 *
 * Works for both a draft working copy and a published version, so an owner can
 * check a version *after* publishing (previously impossible — the harness
 * refused non-draft rows and the published view had no test affordance at all).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { theme, tint } from '../styles/tokens';
import { resolveAgentConfig, type ResolvedAgentConfig } from '../lab-api';
import {
  runAgentTestStreaming,
  type ExecutionSummary,
  type StepEvent,
} from '../api';
import WarningIcon from './icons/WarningIcon';
import Button from './ui/Button';

export interface AgentTestPanelProps {
  /** Draft asset id, or published lineage id when `source === 'published'`. */
  assetId: string;
  source: 'draft' | 'published';
  /** Explicit published version; ignored for drafts. */
  version?: number;
}

type RunStatus = 'idle' | 'running' | 'done' | 'error';

export default function AgentTestPanel({ assetId, source, version }: AgentTestPanelProps) {
  const [config, setConfig] = useState<ResolvedAgentConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [showTools, setShowTools] = useState(false);
  const [showSkills, setShowSkills] = useState(false);

  const [prompt, setPrompt] = useState('');
  const [status, setStatus] = useState<RunStatus>('idle');
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [answer, setAnswer] = useState('');
  const [summary, setSummary] = useState<ExecutionSummary | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  // Resolve config on mount and whenever the target changes. Abort-guarded so a
  // fast target switch can't land a stale response over a newer one.
  useEffect(() => {
    let cancelled = false;
    setConfigLoading(true);
    setConfigError(null);
    resolveAgentConfig({ assetId, source, version })
      .then(cfg => {
        if (cancelled) return;
        if ((cfg as any)?.error) {
          setConfigError(
            (cfg as any).error === 'not_found'
              ? 'Agent not found.'
              : String((cfg as any).error),
          );
          setConfig(null);
        } else {
          setConfig(cfg);
        }
      })
      .catch(err => {
        if (!cancelled) setConfigError(err?.message ?? 'Could not resolve config');
      })
      .finally(() => {
        if (!cancelled) setConfigLoading(false);
      });
    return () => { cancelled = true; };
  }, [assetId, source, version]);

  // Abort an in-flight run if the panel unmounts mid-stream.
  useEffect(() => () => abortRef.current?.abort(), []);

  const canRun = prompt.trim().length > 0 && status !== 'running';

  const handleRun = useCallback(async () => {
    if (!canRun) return;
    setStatus('running');
    setSteps([]);
    setAnswer('');
    setSummary(null);
    setRunError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await runAgentTestStreaming(
        { agentId: assetId, source, version, prompt: prompt.trim() },
        event => {
          if (event.type === 'step') {
            // Dedup by node_id where present: a poll loop reuses the same
            // label, and repeated identical rows read as a hang.
            setSteps(prev => {
              const key = event.step.node_id;
              if (key) {
                const at = prev.findIndex(s => s.node_id === key);
                if (at >= 0) {
                  const next = [...prev];
                  next[at] = event.step;
                  return next;
                }
              }
              return [...prev, event.step];
            });
          } else if (event.type === 'text') {
            setAnswer(prev => prev + event.text);
          } else if (event.type === 'execution_summary') {
            setSummary(event.summary);
          }
        },
        controller.signal,
      );
      setStatus('done');
    } catch (err: any) {
      if (controller.signal.aborted) return;
      setRunError(err?.message ?? 'Test run failed');
      setStatus('error');
    } finally {
      abortRef.current = null;
    }
  }, [assetId, canRun, prompt, source, version]);

  return (
    <div style={panelStyle}>
      <div style={titleRowStyle}>
        <span style={titleStyle}>Test agent</span>
        <span style={sourceBadgeStyle}>
          {source === 'published'
            ? `published${config?.version != null ? ` · v${config.version}` : ''}`
            : 'draft'}
        </span>
      </div>

      {/* ---- Resolved config: what would actually bind ---- */}
      <div style={sectionStyle}>
        <div style={sectionHeaderStyle}>
          Resolved configuration
          <span style={hintInlineStyle}>no tokens spent</span>
        </div>

        {configLoading && <div style={hintStyle}>Resolving…</div>}
        {configError && <div style={errorStyle} role="alert">{configError}</div>}

        {config && !configLoading && (
          <>
            <dl style={kvGridStyle}>
              <dt style={dtStyle}>Model</dt>
              <dd style={ddStyle}>
                {config.modelId ?? 'platform default'}
                {config.modelSource === 'runtime-default' && (
                  <span style={hintInlineStyle}>runtime default</span>
                )}
              </dd>

              <dt style={dtStyle}>Tools</dt>
              <dd style={ddStyle}>
                {config.gatewayProjectionAvailable ? (
                  <>
                    <button
                      style={linkButtonStyle}
                      onClick={() => setShowTools(v => !v)}
                      aria-expanded={showTools}
                    >
                      {config.tools.length} bound {showTools ? '▾' : '▸'}
                    </button>
                    {showTools && (
                      <ul style={listStyle}>
                        {config.tools.length === 0 && (
                          <li style={listItemMutedStyle}>none</li>
                        )}
                        {config.tools.map(t => (
                          <li key={t} style={listItemStyle}>{t}</li>
                        ))}
                      </ul>
                    )}
                  </>
                ) : (
                  // An empty list here would read as "no tools", which is a
                  // materially different fact from "we couldn't ask".
                  <span style={warnTextStyle}>
                    unknown — gateway catalog unavailable
                  </span>
                )}
              </dd>

              <dt style={dtStyle}>Skills</dt>
              <dd style={ddStyle}>
                <button
                  style={linkButtonStyle}
                  onClick={() => setShowSkills(v => !v)}
                  aria-expanded={showSkills}
                >
                  {config.skills.length} declared {showSkills ? '▾' : '▸'}
                </button>
                {showSkills && (
                  <ul style={listStyle}>
                    {config.skills.length === 0 && (
                      <li style={listItemMutedStyle}>none</li>
                    )}
                    {config.skills.map(s => (
                      <li key={s} style={listItemStyle}>{s}</li>
                    ))}
                  </ul>
                )}
              </dd>

              <dt style={dtStyle}>Persona</dt>
              <dd style={ddStyle}>{config.personaChars} characters</dd>
            </dl>

            {config.droppedCollections.length > 0 && (
              // The highest-value line in this panel: a dropped collection is
              // the usual cause of "my agent can't do the thing", and before
              // this it only ever appeared in a server-side log.
              <div style={warnBoxStyle} role="alert">
                <WarningIcon /> {config.droppedCollections.length} tool collection
                {config.droppedCollections.length === 1 ? '' : 's'} dropped —
                its tools will NOT be available:
                <ul style={listStyle}>
                  {config.droppedCollections.map(id => (
                    <li key={id} style={listItemStyle}>{id}</li>
                  ))}
                </ul>
                <div style={hintStyle}>
                  The collection is missing, or none of its tools are currently
                  offered by a connected gateway.
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* ---- Live run ---- */}
      <div style={sectionStyle}>
        <div style={sectionHeaderStyle}>Send a prompt</div>
        <textarea
          style={textareaStyle}
          value={prompt}
          onChange={e => setPrompt(e.target.value.slice(0, 10000))}
          placeholder="e.g. What tools can you call? List their exact names."
          rows={3}
          maxLength={10000}
          disabled={status === 'running'}
          aria-label="Test prompt"
        />
        <div>
          <Button
            variant="primary"
            size="md"
            onClick={handleRun}
            disabled={!canRun}
            busy={status === 'running'}
            busyLabel="Running…"
          >
            Run
          </Button>
        </div>

        {steps.length > 0 && (
          <div style={stepsStyle}>
            {steps.map((s, i) => (
              <div key={s.node_id || i} style={stepRowStyle}>
                <span>{s.label || s.status}</span>
                {s.duration_ms !== undefined && (
                  <span style={stepDurationStyle}>
                    {s.duration_ms < 1000
                      ? `${s.duration_ms}ms`
                      : `${(s.duration_ms / 1000).toFixed(1)}s`}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}

        {answer && <pre style={outputPreStyle}>{answer}</pre>}

        {runError && <div style={errorStyle} role="alert">{runError}</div>}

        {summary && (
          <div style={summaryStyle}>
            <span>{summary.cycles} cycles</span>
            <span>
              {summary.usage?.totalTokens ?? '—'} tokens
            </span>
            <span>{(summary.total_duration_ms / 1000).toFixed(1)}s</span>
            {summary.model && <span>{summary.model}</span>}
            {Object.keys(summary.tools ?? {}).length > 0 && (
              <span>called: {Object.keys(summary.tools).join(', ')}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles — uses `theme` tokens (the older editor/harness panels predate them).
// ---------------------------------------------------------------------------

const panelStyle: React.CSSProperties = {
  marginTop: 24,
  padding: 16,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surface,
  display: 'flex',
  flexDirection: 'column',
  gap: 16,
};

const titleRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

const titleStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.sectionTitle,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};

const sourceBadgeStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  padding: '2px 8px',
  borderRadius: theme.radius.badge,
  background: theme.colors.background,
  color: theme.colors.textSecondary,
  border: `1px solid ${theme.colors.border}`,
};

const sectionStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
};

const sectionHeaderStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

const hintStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

const hintInlineStyle: React.CSSProperties = {
  ...hintStyle,
  fontWeight: theme.typography.weights.regular,
};

const kvGridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'max-content 1fr',
  gap: '6px 12px',
  margin: 0,
};

const dtStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

const ddStyle: React.CSSProperties = {
  margin: 0,
  fontSize: theme.typography.sizes.body,
  color: theme.colors.textPrimary,
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
};

const linkButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  cursor: 'pointer',
  fontSize: theme.typography.sizes.body,
  textAlign: 'left',
};

const listStyle: React.CSSProperties = {
  margin: '4px 0 0',
  paddingLeft: 18,
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
};

const listItemStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textPrimary,
  fontFamily: 'var(--font-mono)',
};

const listItemMutedStyle: React.CSSProperties = {
  ...listItemStyle,
  color: theme.colors.textSecondary,
  fontStyle: 'italic',
};

const warnBoxStyle: React.CSSProperties = {
  padding: 10,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.warning}`,
  background: tint(theme.colors.warning, 8),
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textPrimary,
};

const warnTextStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  color: theme.colors.warning,
};

const errorStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.error,
};

const textareaStyle: React.CSSProperties = {
  width: '100%',
  padding: 8,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.background,
  color: theme.colors.textPrimary,
  fontSize: theme.typography.sizes.body,
  fontFamily: 'inherit',
  resize: 'vertical',
  boxSizing: 'border-box',
};

const stepsStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
};

const stepRowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  gap: 8,
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

const stepDurationStyle: React.CSSProperties = {
  color: theme.colors.textSecondary,
  fontVariantNumeric: 'tabular-nums',
};

const outputPreStyle: React.CSSProperties = {
  margin: 0,
  padding: 10,
  borderRadius: theme.radius.button,
  background: theme.colors.background,
  color: theme.colors.textPrimary,
  fontSize: theme.typography.sizes.meta,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  maxHeight: 320,
  overflowY: 'auto',
};

const summaryStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 12,
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};
