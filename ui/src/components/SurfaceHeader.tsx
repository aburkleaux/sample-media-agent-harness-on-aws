import React from 'react';
import { theme } from '../styles/tokens';
import { BELL_SAFE_AREA } from '../utils/layout';

/**
 * The persistent header for a main-nav center surface — its title, description,
 * and the rule beneath them.
 *
 * Rendered on EVERY branch of the surface, not just its inventory. The detail,
 * harness and editor views each used to replace the whole surface, so opening one
 * item took the heading and its rule off screen and the content ran to the very
 * top of the pane. That reads as a page navigation, but none happened: you are
 * still on Skills (or Agents), one level in, and the header is what says so. It
 * also stops the pane's contents jumping up by the header's height on the way in
 * and back down on the way out.
 *
 * One definition rather than one per surface, because "Skills and Agents line up
 * 1:1" was previously an invariant maintained by a comment on each of two style
 * objects ("Matches caHeaderStyle (Agents)" / "Matches SkillsCenter headerStyle")
 * and nothing else. Per-surface wrappers (`SkillsSurfaceHeader`,
 * `AgentsSurfaceHeader`) supply only the copy.
 */
export function SurfaceHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div style={surfaceHeaderStyle}>
      <div>
        <h2 style={titleStyle}>{title}</h2>
        {subtitle && <p style={subtitleStyle}>{subtitle}</p>}
      </div>
    </div>
  );
}

/** Shell for a surface branch: fixed header, one child taking the rest. */
export const surfaceStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
};

/**
 * Takes the height the header leaves. `minHeight: 0` is load-bearing: a flex child
 * defaults to a content-sized floor, so a long body would push the header out of
 * the pane instead of scrolling inside its own box.
 */
export const surfaceNestedStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  display: 'flex',
  flexDirection: 'column',
};

/**
 * A nested branch's OWN header — the item's name plus its Back / Edit actions,
 * one level under the surface header.
 *
 * Same metrics as `surfaceHeaderStyle` so the two stack as one block, but with
 * NO bottom rule: the surface header directly above already draws it, and two
 * hairlines a heading apart read as a broken table.
 */
export const surfaceNestedHeaderStyle: React.CSSProperties = {
  padding: `20px ${BELL_SAFE_AREA}px 12px 24px`,
  display: 'flex',
  alignItems: 'flex-start',
  justifyContent: 'space-between',
  gap: 16,
  flexShrink: 0,
};

const surfaceHeaderStyle: React.CSSProperties = {
  // Right padding reserves the floating notification bell's safe area
  // (WorkspaceView renders it at top:10/right:14, ~44px wide) so nothing in the
  // header slides under it.
  padding: `20px ${BELL_SAFE_AREA}px 12px 24px`,
  display: 'flex',
  alignItems: 'flex-start',
  justifyContent: 'space-between',
  gap: 12,
  borderBottom: `1px solid ${theme.colors.border}`,
  flexShrink: 0,
};

const titleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-lg)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};

const subtitleStyle: React.CSSProperties = {
  margin: '4px 0 0',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
};
