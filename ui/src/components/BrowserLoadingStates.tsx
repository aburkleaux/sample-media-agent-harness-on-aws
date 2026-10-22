import React from 'react';
import { browserRowStyle } from './BrowserRow';
import { theme } from '../styles/tokens';

/**
 * Loading affordances shared by the built-in Skills and Agents browsers.
 *
 * Both panels have the same job — a collapsed section the user expands, which
 * then fetches — and so the same two loading problems:
 *
 *   First load has nothing to show, so it needs a placeholder SHAPED like the
 *   list. Content otherwise jumps in underneath the toggle once it arrives.
 *
 *   A refresh already has rows on screen. Throwing them away to re-fetch the
 *   same list is worse than showing them briefly stale, so the rows stay, dim,
 *   and a live status line explains why.
 *
 * Extracted rather than copied into both: they are meant to look identical, and
 * two copies of a visual contract is how they stop being identical.
 *
 * These use theme tokens, unlike the hardcoded greys both (older) browsers still
 * carry elsewhere, so the loading states match the surrounding app.
 */

/** Keyframe name is namespaced — `caSpin` lives inside the Agents view's own
 *  loading branch, which is not mounted on either of these pages. */
const SPIN_KEYFRAME = 'browserLoadingSpin';

export function BrowserSkeleton({ label, withGroupHeader = false, rows = 3 }: {
  /** Accessible name, e.g. "Loading built-in skills". */
  label: string;
  /** Skills groups its cards under a source header; Agents does not. */
  withGroupHeader?: boolean;
  rows?: number;
}) {
  return (
    <div role="status" aria-label={label} style={{ padding: '4px 0' }}>
      {withGroupHeader && (
        <div style={{ ...barStyle, width: 140, height: 10, marginBottom: 10 }} />
      )}
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} style={{ ...cardStyle, opacity: 1 - index * 0.18 }}>
          <div style={{ ...barStyle, width: '38%', height: 10 }} />
          <div style={{ ...barStyle, width: '72%', height: 8, marginTop: 7 }} />
        </div>
      ))}
    </div>
  );
}

export function RefreshingNotice({ label }: { label: string }) {
  return (
    <div role="status" aria-live="polite" style={noticeStyle}>
      <style>{`@keyframes ${SPIN_KEYFRAME} { to { transform: rotate(360deg); } }`}</style>
      <span style={spinnerStyle} aria-hidden="true" />
      {label}
    </div>
  );
}

/** Applied to the list while a refresh is in flight: readable, clearly not current. */
export const staleListStyle: React.CSSProperties = {
  opacity: 0.55,
  transition: 'opacity 120ms ease',
};

const barStyle: React.CSSProperties = {
  borderRadius: theme.radius.badge,
  background: theme.colors.surfaceSubtle,
};

/* Shaped like the row it stands in for — same padding and hairline as
 * `browserRowStyle`. It kept drawing a bordered, filled CARD after the real rows
 * became flat, so the placeholder no longer resembled what replaced it and the
 * list visibly changed shape on load. */
const cardStyle: React.CSSProperties = {
  ...browserRowStyle,
};

const noticeStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  padding: '6px 0',
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
};

const spinnerStyle: React.CSSProperties = {
  width: 11,
  height: 11,
  borderRadius: '50%',
  border: `2px solid ${theme.colors.border}`,
  borderTopColor: theme.colors.primary,
  animation: `${SPIN_KEYFRAME} 0.8s linear infinite`,
  flexShrink: 0,
};
