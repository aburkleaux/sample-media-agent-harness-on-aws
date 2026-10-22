import React, { useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import { safeSetItem } from '../../utils/safeStorage';

/**
 * The orchestrator's reasoning effort, as a four-segment control:
 * Default · Low · Medium · High. Default sends no effort field, so the runtime
 * applies its env BEDROCK_REASONING_EFFORT default; Low/Medium/High cap the
 * model's adaptive thinking (and latency) for that turn.
 *
 * A segmented radiogroup rather than a range: effort is a string enum, the four
 * choices fit on one line, and a range thumb parked at the far left read as "a
 * setting someone already made". Arrow keys move the selection (roving tabindex),
 * Home/End jump to the ends — the WAI-ARIA radiogroup pattern.
 *
 * Persistence MIRRORS the model_id mechanism exactly: a global localStorage key
 * (`lab-orchestrator-model-effort`, values ''|low|medium|high) plus a dispatched
 * `model-changed` event. The value is read per turn by `selectedReasoningEffort()`.
 *
 * It lives INSIDE the current-model card because it configures that model. When
 * the model does not accept effort it renders a one-line note in the same slot
 * instead of the control — the stored value is retained, so switching back to a
 * capable model restores it.
 */

export const EFFORT_STORAGE_KEY = 'lab-orchestrator-model-effort';

export type EffortValue = '' | 'low' | 'medium' | 'high';
const STOPS: { label: string; value: EffortValue; hint: string }[] = [
  { label: 'Default', value: '', hint: "Uses the model's own reasoning default." },
  { label: 'Low', value: 'low', hint: 'Fastest replies, lightest reasoning.' },
  { label: 'Medium', value: 'medium', hint: 'Balanced speed and depth.' },
  { label: 'High', value: 'high', hint: 'Deepest reasoning — slower, more tokens.' },
];

function readStored(): EffortValue {
  try {
    const v = localStorage.getItem(EFFORT_STORAGE_KEY) || '';
    return v === 'low' || v === 'medium' || v === 'high' ? v : '';
  } catch {
    return '';
  }
}

export default function EffortSlider({ supported, onChange }: {
  supported: boolean;
  onChange?: (value: EffortValue) => void;
}) {
  const [value, setValue] = useState<EffortValue>(() => readStored());
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const index = Math.max(0, STOPS.findIndex(s => s.value === value));

  const choose = (nextIndex: number, focus = false) => {
    const i = Math.min(STOPS.length - 1, Math.max(0, nextIndex));
    const next = STOPS[i].value;
    if (focus) refs.current[i]?.focus();
    if (next === value) return;
    setValue(next);
    safeSetItem(EFFORT_STORAGE_KEY, next);
    // Same event the model picker fires, so any other view reacts identically.
    window.dispatchEvent(new Event('model-changed'));
    onChange?.(next);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (step !== undefined) { e.preventDefault(); choose(index + step, true); }
    else if (e.key === 'Home') { e.preventDefault(); choose(0, true); }
    else if (e.key === 'End') { e.preventDefault(); choose(STOPS.length - 1, true); }
  };

  return (
    <div style={sectionStyle} data-testid="effort-control">
      <div style={labelRowStyle}>
        <span id="effort-label" style={labelStyle}>Reasoning effort</span>
      </div>
      {supported ? (
        <>
          <div role="radiogroup" aria-labelledby="effort-label" style={trackStyle} onKeyDown={onKeyDown}>
            {STOPS.map((s, i) => {
              const on = i === index;
              return (
                <button
                  key={s.label}
                  ref={el => { refs.current[i] = el; }}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  tabIndex={on ? 0 : -1}
                  onClick={() => choose(i)}
                  data-effort-segment=""
                  style={on ? segmentOnStyle : segmentStyle}
                >
                  {s.label}
                </button>
              );
            })}
          </div>
          <p style={hintStyle} aria-live="polite" data-testid="effort-hint">{STOPS[index].hint}</p>
        </>
      ) : (
        <p style={noteStyle} data-testid="effort-unsupported">
          Reasoning effort isn't adjustable for this model.
        </p>
      )}
      <style>{`
        [data-effort-segment]:hover:not([aria-checked="true"]) { color: var(--color-text); }
        [data-effort-segment]:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 1px; }
      `}</style>
    </div>
  );
}

const sectionStyle: React.CSSProperties = {
  marginTop: 14,
  paddingTop: 14,
  borderTop: `1px solid ${theme.colors.border}`,
};

const labelRowStyle: React.CSSProperties = { marginBottom: 8 };

const labelStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  letterSpacing: '0.04em',
  textTransform: 'uppercase',
  color: theme.colors.textSecondary,
};

const trackStyle: React.CSSProperties = {
  display: 'inline-grid',
  gridTemplateColumns: `repeat(${STOPS.length}, minmax(76px, 1fr))`,
  gap: 2,
  padding: 3,
  borderRadius: theme.radius.panel,
  background: theme.colors.surfaceSubtle,
  border: `1px solid ${theme.colors.border}`,
};

const segmentStyle: React.CSSProperties = {
  appearance: 'none',
  border: '1px solid transparent',
  background: 'transparent',
  color: theme.colors.textSecondary,
  fontFamily: theme.typography.fontFamily,
  fontSize: 'var(--fs-xs)',
  fontWeight: theme.typography.weights.medium,
  padding: '6px 12px',
  borderRadius: theme.radius.panel - 3,
  cursor: 'pointer',
  transition: `background ${theme.transitions.hover}, color ${theme.transitions.hover}, box-shadow ${theme.transitions.hover}`,
};

const segmentOnStyle: React.CSSProperties = {
  ...segmentStyle,
  background: theme.colors.surfaceRaised,
  color: theme.colors.textPrimary,
  fontWeight: theme.typography.weights.semibold,
  border: `1px solid color-mix(in srgb, ${theme.colors.primary} 45%, transparent)`,
  boxShadow: theme.elevation.subtle,
  cursor: 'default',
};

const hintStyle: React.CSSProperties = {
  margin: '8px 0 0',
  minHeight: '1.5em',
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textTertiary,
};

const noteStyle: React.CSSProperties = {
  margin: 0,
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textTertiary,
};
