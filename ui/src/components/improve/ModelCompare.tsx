/**
 * How each model did — the Improve surface's model comparison (replaces "How it went").
 *
 * One card per model, every metric Improve scores in the same order on every card, so you read across:
 * the share of judged runs that finished cleanly, then each tier and gating check as a coloured bar, then
 * what a run cost. Scores come from `scorecard.byModelScores` (each judged run credited to the model its
 * tokens mostly went to), so nothing here is re-judged.
 *
 * As many cards as FIT the width are shown — no sideways scrolling. When more models have run than fit,
 * "Models" picks which ones show and in what order (drag, or the arrows); the FIRST is the reference the
 * others' differences are taken against. The choice is remembered on this device.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import { isSentinel, type MeasuredMean, type ModelScores } from '../../improve-api';

const ORDER_KEY = 'improve-model-compare';
const CARD_MIN = 250;
const GAP = 12;
/** Fewer judged runs than this: the card says "few runs" (its rates swing on one session). */
export const FEW_RUNS = 20;

type Metric = {
  key: string;
  label: string;
  value: (m: ModelScores) => number | null;
  /** Hover text for the row: what the number is over, or why there is none. */
  note?: (m: ModelScores) => string;
};

const mean = (t: MeasuredMean | undefined): number | null =>
  t && !isSentinel(t.mean) && typeof t.mean === 'number' ? t.mean : null;

const CHECK_LABELS: Record<string, string> = {
  DagCompletionValidator: 'Plan steps all completed',
  AsyncJobFlowValidator: 'Async jobs handled',
  ToolDiscoveryValidator: 'Found the right tools',
  ControlPlaneOpValidator: 'Control-plane actions',
};

/** AgentCore's built-in online evaluators, in card order (`scorecard.byModelScores[].agentcore`). */
const AGENTCORE_LABELS: Record<string, string> = {
  'Builtin.GoalSuccessRate': 'Goal success (AgentCore)',
  'Builtin.Helpfulness': 'Helpfulness',
  'Builtin.Correctness': 'Correctness',
  'Builtin.ToolSelectionAccuracy': 'Tool choice',
  'Builtin.ToolParameterAccuracy': 'Tool arguments',
  'Builtin.SkillSelectionAccuracy': 'Skill choice',
  'Builtin.SkillInstructionFollowing': 'Followed the skill',
  'Builtin.InstructionFollowing': 'Followed instructions',
};

/** `DagCompletionValidator` → "Dag completion" for a check this file has no words for yet. */
const humanize = (name: string) => {
  const s = name.replace(/Validator$|Evaluator$/, '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
};

/** `global.anthropic.claude-opus-5` → "Claude Opus 5"; anything unrecognised keeps its id. */
export function modelLabel(id: string): string {
  const bare = id
    .replace(/^(global|us|eu|apac|ap)\./, '')               // cross-region inference prefix
    .replace(/^(anthropic|openai|amazon|meta|mistral|cohere|deepseek|qwen)\./, '')
    .replace(/(-\d{8})?(-v\d+(:\d+)?)?$/, '');               // dated build / Bedrock version suffix
  // Consecutive number parts are one version ("4-5" → "4.5"); GPT keeps its hyphen ("GPT-5.5").
  const parts: string[] = [];
  for (const w of bare.split(/[-_]/).filter(Boolean)) {
    if (/^\d+$/.test(w) && parts.length && /^\d+(\.\d+)*$/.test(parts[parts.length - 1])) parts[parts.length - 1] += `.${w}`;
    else parts.push(w);
  }
  if (!parts.length) return id;
  const words = parts.map((w) => (/^gpt$/i.test(w) ? 'GPT' : w.charAt(0).toUpperCase() + w.slice(1)));
  return words[0] === 'GPT' && words.length > 1 ? `GPT-${words.slice(1).join(' ')}` : words.join(' ');
}

/**
 * The metrics, in card order: the two judged tiers, every check any shown model measured, then every
 * AgentCore evaluator that scored at least one run of a shown model.
 */
function metricsFor(models: ModelScores[]): Metric[] {
  const checks = new Set<string>();
  for (const m of models) for (const name of Object.keys(m.checks ?? {})) checks.add(name);
  const known = Object.keys(CHECK_LABELS).filter((n) => checks.has(n));
  const rest = [...checks].filter((n) => !(n in CHECK_LABELS)).sort();
  const evals = new Set<string>();
  for (const m of models) {
    for (const [name, t] of Object.entries(m.agentcore ?? {})) if (mean(t) !== null) evals.add(name);
  }
  const evalOrder = [
    ...Object.keys(AGENTCORE_LABELS).filter((n) => evals.has(n)),
    ...[...evals].filter((n) => !(n in AGENTCORE_LABELS)).sort(),
  ];
  return [
    { key: 'delivery', label: 'Task completed', value: (m) => mean(m.tiers?.delivery) },
    { key: 'dispatch', label: 'Routed to the right work', value: (m) => mean(m.tiers?.dispatch) },
    ...[...known, ...rest].map((name) => ({
      key: name, label: CHECK_LABELS[name] ?? humanize(name), value: (m: ModelScores) => m.checks?.[name]?.rate ?? null,
    })),
    ...evalOrder.map((name) => ({
      key: `agentcore:${name}`,
      label: AGENTCORE_LABELS[name] ?? `${humanize(name.replace(/^Builtin\./, ''))} (AgentCore)`,
      value: (m: ModelScores) => mean(m.agentcore?.[name]),
      note: (m: ModelScores) => {
        const t = m.agentcore?.[name];
        return t && mean(t) !== null
          ? `AgentCore scored ${t.measuredOf} of ${t.total} runs`
          : 'AgentCore scored none of these runs';
      },
    })),
  ];
}

/** 48.2k, 1.7M. */
export const fmtTokens = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : `${(n / 1000).toFixed(1)}k`);
/** 41s, 3.6m, 1.2h. */
export const fmtDuration = (ms: number) => {
  const s = ms / 1000;
  return s < 120 ? `${Math.round(s)}s` : s < 7200 ? `${(s / 60).toFixed(s < 600 ? 1 : 0)}m` : `${(s / 3600).toFixed(1)}h`;
};
const tone = (v: number) => (v >= 0.85 ? theme.colors.success : v >= 0.7 ? theme.colors.warning : theme.colors.error);
const pct = (v: number) => `${Math.round(v * 100)}%`;
const judged = (m: ModelScores) => m.population.pass + m.population.fail;

function readOrder(): { order: string[]; hidden: string[] } {
  try {
    const v = JSON.parse(localStorage.getItem(ORDER_KEY) || '{}');
    return { order: Array.isArray(v.order) ? v.order : [], hidden: Array.isArray(v.hidden) ? v.hidden : [] };
  } catch { return { order: [], hidden: [] }; }
}

export function ModelCompare({ models, window: windowLabel }: { models: ModelScores[]; window?: string }) {
  const [{ order, hidden }, setPrefs] = useState(readOrder);
  const save = (next: { order: string[]; hidden: string[] }) => {
    setPrefs(next);
    try { localStorage.setItem(ORDER_KEY, JSON.stringify(next)); } catch { /* storage off */ }
  };
  // The user's order first (models that still exist), then new ones by runs — the server's order.
  const ordered = useMemo(() => {
    const byId = new Map(models.map((m) => [m.model, m]));
    const first = order.filter((id) => byId.has(id)).map((id) => byId.get(id)!);
    return [...first, ...models.filter((m) => !order.includes(m.model))];
  }, [models, order]);
  const visible = ordered.filter((m) => !hidden.includes(m.model));

  const ref = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState(3);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(([e]) => setFit(Math.max(1, Math.floor((e.contentRect.width + GAP) / (CARD_MIN + GAP)))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const shown = visible.slice(0, fit);
  const metrics = useMemo(() => metricsFor(shown), [shown]);
  const base = shown[0];
  const [chooser, setChooser] = useState(false);

  const move = (id: string, dir: -1 | 1) => {
    const ids = ordered.map((m) => m.model);
    const i = ids.indexOf(id), j = i + dir;
    if (i < 0 || j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    save({ order: ids, hidden });
  };
  // A tick means ON SCREEN. Unticking hides a card; ticking one that is off screen swaps it in for the
  // last card shown (so what is ticked is always what you see, even when more are ticked than fit).
  const shownIds = new Set(visible.slice(0, fit).map((m) => m.model));
  const toggle = (id: string) => {
    if (shownIds.has(id)) { save({ order: ordered.map((m) => m.model), hidden: [...hidden, id] }); return; }
    const vis = visible.map((m) => m.model).filter((x) => x !== id);
    vis.splice(Math.min(fit - 1, vis.length), 0, id);
    const rest = ordered.map((m) => m.model).filter((x) => !vis.includes(x));
    save({ order: [...vis, ...rest], hidden: hidden.filter((h) => h !== id) });
  };
  const [drag, setDrag] = useState<string | null>(null);
  const dropOn = (target: string) => {
    if (!drag || drag === target) return;
    const ids = ordered.map((m) => m.model).filter((id) => id !== drag);
    ids.splice(ids.indexOf(target), 0, drag);
    save({ order: ids, hidden });
    setDrag(null);
  };

  if (!models.length) return null;
  return (
    <section aria-label="How each model did" data-testid="model-compare">
      <div style={headStyle}>
        <h3 style={titleStyle}>How each model did<span style={subStyle}>{windowLabel ? `${windowLabel} · ` : ''}judged runs only</span></h3>
        {models.length > 1 && (
          <span style={{ position: 'relative', display: 'inline-flex' }}>
            <button type="button" style={chooser ? { ...ctlStyle, ...ctlOnStyle } : ctlStyle} aria-haspopup="dialog" aria-expanded={chooser}
              onClick={() => setChooser((o) => !o)} data-testid="model-compare-chooser-button">
              Models · {shown.length} of {models.length} shown ▾
            </button>
            {chooser && (
              <>
                <span style={scrimStyle} aria-hidden="true" onMouseDown={() => setChooser(false)} />
                <div role="dialog" aria-label="Models to compare" style={chooserStyle} data-testid="model-compare-chooser">
                  <div style={chooserTitleStyle}>Models to compare</div>
                  <div style={chooserSubStyle}>Drag to set the order. The first shown is the reference.</div>
                  {ordered.map((m, i) => {
                    const on = shownIds.has(m.model);
                    const firstOff = !on && i > 0 && shownIds.has(ordered[i - 1].model);
                    const isRef = base?.model === m.model;
                    return (
                      <React.Fragment key={m.model}>
                      {firstOff && <div style={dividerStyle} aria-hidden="true">Not shown</div>}
                      <div style={drag === m.model ? { ...mrowStyle, opacity: 0.5 } : mrowStyle} draggable
                        onDragStart={() => setDrag(m.model)} onDragOver={(e) => e.preventDefault()} onDrop={() => dropOn(m.model)}
                        onDragEnd={() => setDrag(null)} data-testid="model-compare-row" data-model={m.model}>
                        <span style={gripStyle} aria-hidden="true">⋮⋮</span>
                        <input type="checkbox" checked={on} onChange={() => toggle(m.model)} aria-label={`Show ${modelLabel(m.model)}`}
                          style={{ margin: 0, accentColor: theme.colors.primary }} />
                        <span style={{ flex: 1, color: on ? theme.colors.textPrimary : theme.colors.textTertiary }} title={m.model}>{modelLabel(m.model)}</span>
                        {isRef && <span style={refChipStyle}>reference</span>}
                        <span style={countStyle}>{judged(m)}{judged(m) < FEW_RUNS ? ' · few' : ''}</span>
                        <button type="button" style={arrowStyle} disabled={i === 0} onClick={() => move(m.model, -1)} aria-label={`Move ${modelLabel(m.model)} up`}>↑</button>
                        <button type="button" style={arrowStyle} disabled={i === ordered.length - 1} onClick={() => move(m.model, 1)} aria-label={`Move ${modelLabel(m.model)} down`}>↓</button>
                      </div>
                      </React.Fragment>
                    );
                  })}
                  <div style={chooserFootStyle}>{fit} fit at this width; ticking another swaps it in for the last one shown. Fewer than {FEW_RUNS} judged runs is marked "few".</div>
                </div>
              </>
            )}
          </span>
        )}
      </div>
      <div ref={ref} style={{ ...rowStyle, gridTemplateColumns: `repeat(${Math.max(1, Math.min(fit, shown.length || 1))}, minmax(0, 1fr))` }}>
        {shown.map((m) => <ModelCard key={m.model} m={m} base={m === base ? undefined : base} metrics={metrics} />)}
        {!shown.length && <p style={emptyStyle}>No model is selected. Choose one under Models.</p>}
      </div>
    </section>
  );
}

function Delta({ v, vs: r, invert, unit = 'pt' }: { v: number | null; vs: number | null; invert?: boolean; unit?: 'pt' | '%' }) {
  if (v === null || r === null) return null;
  const d = unit === 'pt' ? Math.round((v - r) * 100) : r ? Math.round(((v - r) / r) * 100) : 0;
  if (!d) return null;
  const good = invert ? d < 0 : d > 0;
  return <span style={{ ...deltaStyle, color: good ? theme.colors.success : theme.colors.error }}>{d > 0 ? '+' : '−'}{Math.abs(d)}{unit === '%' ? '%' : ''}</span>;
}

function ModelCard({ m, base, metrics }: { m: ModelScores; base?: ModelScores; metrics: Metric[] }) {
  const n = judged(m);
  const tokens = mean(m.perRun?.tokens), calls = mean(m.perRun?.llmCalls), ms = mean(m.perRun?.latencyMs);
  return (
    <article style={base ? cardStyle : { ...cardStyle, ...baseCardStyle }} data-testid="model-card" data-model={m.model}>
      <div style={nameRowStyle}>
        <b style={nameStyle} title={m.model}>{modelLabel(m.model)}</b>
        {!base && <span style={refChipStyle}>reference</span>}
      </div>
      <div style={metaStyle}>
        {m.population.total.toLocaleString('en-US')} runs · {n.toLocaleString('en-US')} judged
        {n < FEW_RUNS && <span style={{ color: theme.colors.warning }}> · few runs</span>}
      </div>
      <div style={overallStyle}>
        <span style={bigStyle} data-testid="model-card-pass">{m.passRate === null ? '—' : pct(m.passRate)}</span>
        <span style={overallLabelStyle}>finished cleanly{base && <Delta v={m.passRate} vs={base.passRate} />}</span>
      </div>
      {metrics.map((x) => {
        const v = x.value(m);
        return (
          <div key={x.key} style={metricStyle} data-testid="model-card-metric" data-metric={x.key} title={x.note?.(m)}>
            <div style={metricTopStyle}>
              <span>{x.label}</span>
              <span style={metricValueStyle}>{v === null ? '—' : pct(v)}{base && <Delta v={v} vs={x.value(base)} />}</span>
            </div>
            <div style={trackStyle} title={v === null && !x.note ? 'No run needed this check' : undefined}>
              {v === null
                ? <div style={naFillStyle} />
                : <div style={{ ...fillStyle, width: `${Math.max(2, v * 100)}%`, background: tone(v) }} />}
            </div>
          </div>
        );
      })}
      <div style={sepStyle} />
      <div style={costStyle}><span>Tokens / run</span><b>{tokens === null ? '—' : fmtTokens(tokens)}
        {base && <Delta v={tokens} vs={mean(base.perRun?.tokens)} invert unit="%" />}</b></div>
      <div style={costStyle}><span>LLM calls / run</span><b>{calls === null ? '—' : calls.toFixed(1)}</b></div>
      <div style={costStyle}><span>Time / run</span><b>{ms === null ? '—' : fmtDuration(ms)}
        {base && <Delta v={ms} vs={mean(base.perRun?.latencyMs)} invert unit="%" />}</b></div>
    </article>
  );
}

const headStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 };
const titleStyle: React.CSSProperties = { margin: 0, flex: 1, fontSize: 'var(--fs-md)', fontWeight: 600, color: theme.colors.textPrimary };
const subStyle: React.CSSProperties = { marginLeft: 8, fontSize: 'var(--fs-xs)', fontWeight: 400, color: theme.colors.textTertiary };
const ctlStyle: React.CSSProperties = {
  height: 28, display: 'inline-flex', alignItems: 'center', padding: '0 10px', border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button, background: theme.colors.surface, color: theme.colors.textSecondary, font: 'inherit',
  fontSize: 'var(--fs-xs)', cursor: 'pointer', whiteSpace: 'nowrap',
};
const ctlOnStyle: React.CSSProperties = { borderColor: theme.colors.primary, color: theme.colors.primary, background: theme.colors.activeBg };
const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const chooserStyle: React.CSSProperties = {
  position: 'absolute', right: 0, top: 'calc(100% + 6px)', zIndex: theme.zIndex.popover + 1, width: 330, padding: 10,
  background: theme.colors.surfaceRaised, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated, fontSize: 'var(--fs-sm)',
};
const chooserTitleStyle: React.CSSProperties = { fontWeight: 600, color: theme.colors.textPrimary };
const chooserSubStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary, margin: '2px 0 8px' };
const mrowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, padding: '5px 6px', borderRadius: theme.radius.button };
const gripStyle: React.CSSProperties = { color: theme.colors.textTertiary, cursor: 'grab', letterSpacing: -2 };
const refChipStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)', fontWeight: 600, padding: '1px 7px', borderRadius: theme.radius.pill, background: theme.colors.activeBg, color: theme.colors.primary,
};
const countStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary, fontVariantNumeric: 'tabular-nums' };
const arrowStyle: React.CSSProperties = {
  width: 20, height: 20, padding: 0, border: 'none', background: 'none', color: theme.colors.textSecondary, cursor: 'pointer', borderRadius: theme.radius.button,
};
const dividerStyle: React.CSSProperties = {
  margin: '6px 6px 2px', paddingTop: 6, borderTop: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-2xs)',
  fontWeight: 600, color: theme.colors.textTertiary, textTransform: 'uppercase', letterSpacing: '0.04em',
};
const chooserFootStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary, marginTop: 6, lineHeight: 1.4 };
const rowStyle: React.CSSProperties = { display: 'grid', gap: GAP };
const emptyStyle: React.CSSProperties = { color: theme.colors.textTertiary, fontSize: 'var(--fs-sm)', margin: 0 };
const cardStyle: React.CSSProperties = {
  minWidth: 0, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, padding: 14, background: theme.colors.surface,
};
const baseCardStyle: React.CSSProperties = { borderColor: theme.colors.primary, boxShadow: `0 0 0 2px ${theme.colors.primarySubtle}` };
const nameRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'baseline', gap: 8, minWidth: 0 };
const nameStyle: React.CSSProperties = { fontSize: 'var(--fs-sm)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' };
const metaStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary, margin: '2px 0 12px' };
const overallStyle: React.CSSProperties = { display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: 10 };
const bigStyle: React.CSSProperties = { fontSize: 'var(--fs-2xl)', fontWeight: 700, letterSpacing: '-0.02em', color: theme.colors.textPrimary };
const overallLabelStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const metricStyle: React.CSSProperties = { margin: '8px 0' };
const metricTopStyle: React.CSSProperties = { display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, marginBottom: 4 };
const metricValueStyle: React.CSSProperties = { fontWeight: 600, color: theme.colors.textPrimary, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' };
const deltaStyle: React.CSSProperties = { marginLeft: 6, fontSize: 'var(--fs-2xs)', fontWeight: 500 };
const trackStyle: React.CSSProperties = { height: 7, borderRadius: theme.radius.pill, background: theme.colors.surfaceSubtle, overflow: 'hidden' };
const fillStyle: React.CSSProperties = { height: '100%', borderRadius: theme.radius.pill };
const naFillStyle: React.CSSProperties = {
  height: '100%', width: '100%', background: `repeating-linear-gradient(45deg, ${theme.colors.border} 0 4px, ${theme.colors.surfaceSubtle} 4px 8px)`,
};
const sepStyle: React.CSSProperties = { borderTop: `1px solid ${theme.colors.surfaceSubtle}`, margin: '12px 0 8px' };
const costStyle: React.CSSProperties = { display: 'flex', justifyContent: 'space-between', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, margin: '4px 0' };

export default ModelCompare;
