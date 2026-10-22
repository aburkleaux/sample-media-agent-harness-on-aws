import React from 'react';
import { theme, tint } from '../styles/tokens';

/**
 * One row of a built-in inventory listing, shared by the Skills and Agents
 * browsers.
 *
 * Extracted for the reason given in `BrowserLoadingStates`: both panels list the
 * same kind of thing in the same three lines — name + state, a description, a
 * muted meta line — and they had two copies of that layout behind "kept in sync"
 * comments. The copies had already drifted:
 *
 *   - Skills clamped its description in CSS *and* pre-truncated it at 80
 *     characters in JS. The clamp then re-clipped a string that already ended in
 *     an ellipsis, and on a wide pane the row stopped at 80 characters with
 *     plenty of room to spare. Agents did neither, so its purposes wrapped.
 *   - The name was a `div` in one and a `span` in the other.
 *   - Both defined the same badge geometry and the same 10px italic meta style
 *     separately.
 *
 * Descriptions are clamped to a single line, not wrapped. Built-in skill
 * descriptions are 200–700 characters of prose (they are model-facing
 * instructions), so wrapping makes every row a different height and the list
 * stops reading as a table. The full text stays reachable as the `title`.
 */

export type BadgeTone = 'success' | 'warning';

export interface BrowserRowProps {
  /** Primary label. Truncates before the badge does. */
  name: string;
  /** Tooltip for the name — usually the underlying id/key. */
  nameTitle?: string;
  /** State chip beside the name, omitted entirely when there is nothing to say. */
  badge?: { label: string; tone: BadgeTone };
  /** Trailing control, pushed to the right edge of the header line. */
  action?: React.ReactNode;
  description?: string;
  /** Muted third line: "Invoked via: …" for agents, "Uses: …" for skills. */
  meta?: React.ReactNode;
}

export function BrowserRow({ name, nameTitle, badge, action, description, meta }: BrowserRowProps) {
  return (
    <div style={browserRowStyle}>
      <div style={browserRowHeaderStyle}>
        <span style={browserRowNameStyle} title={nameTitle ?? name}>{name}</span>
        {badge && <span style={badgeStyleFor(badge.tone)}>{badge.label}</span>}
        {action}
      </div>
      {description ? (
        // Full string in the DOM; the clamp is CSS, so it uses whatever width the
        // pane actually has instead of a hardcoded character count.
        <div style={browserRowDescStyle} title={description}>{description}</div>
      ) : null}
      {meta ? <div style={browserRowMetaStyle}>{meta}</div> : null}
    </div>
  );
}

/* Flat rows on a hairline, matching the published rows further down each page.
 * These were boxed cards filled with `activeBg` — the SELECTED token — so every
 * built-in entry read as highlighted, and under dark-kirocrew the fill is mint. */
export const browserRowStyle: React.CSSProperties = {
  padding: '9px 2px',
  borderBottom: `1px solid ${theme.colors.border}`,
};

/* Name and badge sit together on the left; a trailing action is pushed right by
 * its own `marginLeft: auto`, not by `space-between` — which spread all three
 * apart and left the badge floating mid-row. */
export const browserRowHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-start',
  gap: 8,
  minWidth: 0,
};

// Truncates rather than growing, so a long name cannot shove the badge away.
export const browserRowNameStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

export const browserRowDescStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  marginTop: 2,
  lineHeight: 1.3,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

export const browserRowMetaStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textTertiary,
  marginTop: 4,
  fontStyle: 'italic',
};

/* Geometry matched to the Agents center's lifecycle badge (`caStateBadgeStyle` in
 * WorkspaceView) so a built-in row and a published row read as the same kind of
 * thing. Only the hue differs, and it carries the meaning. */
const badgeBaseStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  padding: '2px 7px',
  borderRadius: theme.radius.badge,
  whiteSpace: 'nowrap',
  fontWeight: 600,
  flexShrink: 0,
};

export function badgeStyleFor(tone: BadgeTone): React.CSSProperties {
  const colour = tone === 'success' ? theme.colors.success : theme.colors.warning;
  return { ...badgeBaseStyle, background: tint(colour), color: colour };
}

/** Neutral outlined control for a row's trailing action (Skills' "View"). */
export const browserRowActionStyle: React.CSSProperties = {
  marginLeft: 'auto',
  flexShrink: 0,
  padding: '2px 7px',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: 'transparent',
  color: theme.colors.textSecondary,
  fontSize: 'var(--fs-3xs)',
  cursor: 'pointer',
};
