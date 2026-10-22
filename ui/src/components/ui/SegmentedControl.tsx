import React from 'react';
import { theme } from '../../styles/tokens';

/**
 * One tablist, two looks.
 *
 * The underline-tab pattern was hand-rolled four times — `SkillDetailsView`,
 * `SkillPackageImport`, `SessionRendererPanel`, `AddMcpServerModal` — and the first two were
 * byte-identical (`padding: '8px 13px'`, `borderBottom: '2px solid transparent'`, …). This is the
 * fifth copy replaced by one primitive, in the same folder as the other shared primitives.
 *
 * Two variants because the two jobs look different:
 *   `segmented`  two joined standard buttons, active one filled — reads as an either/or choice,
 *                right for a small top-level decision ("write this" vs "upload that")
 *   `underline`  today's look, right for a row of peer views inside a panel
 * A nested tablist should still use `underline`; two boxed rows stacked read as one grid.
 *
 * Adds keyboard navigation, which none of the four hand-rolled versions had: arrows move between
 * tabs, Home/End jump to the ends, and a roving `tabIndex` keeps one stop in the tab order (WAI-ARIA
 * tabs pattern). Selecting follows focus, which is correct here because switching is cheap and has
 * no side effects.
 */

export interface SegmentedOption<T extends string> {
  value: T;
  label: React.ReactNode;
  /** Optional per-tab a11y name, when `label` is decorative or abbreviated. */
  ariaLabel?: string;
}

export interface SegmentedControlProps<T extends string> {
  options: ReadonlyArray<SegmentedOption<T>>;
  value: T;
  onChange: (value: T) => void;
  /** Names the tablist for screen readers. Required — an unlabelled tablist is a bare row of buttons. */
  label: string;
  variant?: 'segmented' | 'underline';
  /**
   * Shared prefix for the generated `id`s, so a consumer can point its panel at the right tab.
   * Pass a `useId()` value. Use `tabDomId`/`panelDomId` to build the matching panel attributes.
   */
  idPrefix: string;
  style?: React.CSSProperties;
}

/** The `id` of a tab button — put this on the panel's `aria-labelledby`. */
export function tabDomId(idPrefix: string, value: string): string {
  return `${idPrefix}-${value}-tab`;
}

/** The `id` of a tab's panel — put this on the panel's `id`. */
export function panelDomId(idPrefix: string, value: string): string {
  return `${idPrefix}-${value}-panel`;
}

export default function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  label,
  variant = 'segmented',
  idPrefix,
  style,
}: SegmentedControlProps<T>) {
  const listStyle = variant === 'segmented' ? segmentedListStyle : underlineListStyle;

  const move = (from: number, delta: number) => {
    if (options.length === 0) return;
    // Wrap, so ArrowRight on the last tab returns to the first (WAI-ARIA tabs behaviour).
    const next = (from + delta + options.length) % options.length;
    onChange(options[next].value);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        event.preventDefault();
        move(index, 1);
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        event.preventDefault();
        move(index, -1);
        break;
      case 'Home':
        event.preventDefault();
        onChange(options[0].value);
        break;
      case 'End':
        event.preventDefault();
        onChange(options[options.length - 1].value);
        break;
      default:
        break;
    }
  };

  return (
    <div role="tablist" aria-label={label} style={{ ...listStyle, ...style }}>
      {options.map((option, index) => {
        const selected = option.value === value;
        const base = variant === 'segmented' ? segmentStyle : underlineTabStyle;
        const active = variant === 'segmented' ? activeSegmentStyle : activeUnderlineTabStyle;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            id={tabDomId(idPrefix, option.value)}
            aria-controls={panelDomId(idPrefix, option.value)}
            aria-selected={selected}
            aria-label={option.ariaLabel}
            // Roving tabIndex: exactly one stop in the tab order, arrows move within.
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(option.value)}
            onKeyDown={event => onKeyDown(event, index)}
            style={{ ...base, ...(selected ? active : {}) }}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

// --- segmented (joined standard buttons) --------------------------------------
// Deliberately NOT a pill. The geometry is `Button`'s: `theme.radius.button` and its `md` padding,
// so the control reads as two ordinary buttons that happen to be joined rather than a switch in a
// different design language from everything around it.
const segmentedListStyle: React.CSSProperties = {
  display: 'inline-flex',
  gap: 2,
  padding: 3,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface,
};
const segmentStyle: React.CSSProperties = {
  // Matches Button's `md` size exactly (`6px 12px` @ var(--fs-xs)).
  padding: '6px 12px',
  border: '1px solid transparent',
  borderRadius: theme.radius.button,
  background: 'transparent',
  color: theme.colors.textSecondary,
  fontSize: 'var(--fs-xs)',
  fontWeight: 500,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};
// Button's `primary` for the active segment, `ghost` for the rest.
const activeSegmentStyle: React.CSSProperties = {
  background: theme.colors.primary,
  color: theme.colors.onPrimary,
  fontWeight: 600,
};

// --- underline (peer views) ---------------------------------------------------
const underlineListStyle: React.CSSProperties = {
  display: 'flex',
  borderBottom: `1px solid ${theme.colors.border}`,
};
const underlineTabStyle: React.CSSProperties = {
  padding: '8px 13px',
  border: 'none',
  borderBottom: '2px solid transparent',
  background: 'none',
  color: theme.colors.textSecondary,
  fontSize: 'var(--fs-xs)',
  cursor: 'pointer',
};
const activeUnderlineTabStyle: React.CSSProperties = {
  color: theme.colors.primary,
  borderBottomColor: theme.colors.primary,
  fontWeight: 600,
};
