import React from 'react';
import { theme } from '../styles/tokens';

/** Which slice of an inventory the surface is showing. */
export type InventoryState = 'all' | 'drafts' | 'published';

const OPTIONS: { key: InventoryState; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'drafts', label: 'Drafts' },
  { key: 'published', label: 'Published' },
];

/**
 * All / Drafts / Published selector for the Agents and Skills inventories.
 *
 * Styled to match `ActivityPanel`'s filter bar so the app's two filtering
 * surfaces read the same. Unlike that one these buttons carry `aria-pressed` —
 * the label alone cannot tell an active tab from an inactive one, so it is both
 * the accessible signal and what the tests select on.
 *
 * Owned per surface rather than lifted: the two inventories share no state, and
 * the counts come from each surface's own already-filtered lists.
 */
export function InventoryStateFilter({ value, onChange, counts, ariaLabel }: {
  value: InventoryState;
  onChange: (next: InventoryState) => void;
  /** Per-option totals, shown muted beside each label. */
  counts?: Record<InventoryState, number>;
  ariaLabel: string;
}) {
  return (
    <div role="group" aria-label={ariaLabel} style={barStyle}>
      {OPTIONS.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          aria-pressed={value === key}
          style={value === key ? tabActiveStyle : tabStyle}
        >
          {label}
          {/* Explicit space: without it the accessible name collapses to
              "Drafts2", which a screen reader reads as one token. */}
          {counts && <>{' '}<span style={countStyle}>{counts[key]}</span></>}
        </button>
      ))}
    </div>
  );
}

const barStyle: React.CSSProperties = {
  display: 'flex',
  gap: 4,
  marginBottom: 12,
  flexShrink: 0,
};

const tabStyle: React.CSSProperties = {
  padding: '5px 10px',
  fontSize: 'var(--fs-2xs)',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  color: theme.colors.textSecondary,
  cursor: 'pointer',
};

const tabActiveStyle: React.CSSProperties = {
  ...tabStyle,
  background: theme.colors.primary,
  // Tracks the fill, not the page — same as the sidebar's New button. A literal
  // `white` here read fine on the blue primaries and was low-contrast on
  // dark-kirocrew's mint, where the label has to go dark.
  color: theme.colors.onPrimary,
  borderColor: theme.colors.primary,
};

const countStyle: React.CSSProperties = { marginLeft: 4, opacity: 0.6 };
