/**
 * Shared chrome for the Capabilities modal — Skills, Agents, Tools, Hooks,
 * Model and General.
 *
 * These styles lived inline in GatewayBrowser while the other panels each rolled
 * their own headers, counts and refresh buttons. That produced visible
 * inconsistency (different paddings, a 🔄 emoji in one place and a glyph in
 * another) and guaranteed drift: restyling one panel silently left the rest
 * behind. Defined once here so a change lands everywhere.
 *
 * Specific problems these encode fixes for:
 *  - group rows used `padding: 6px 0 4px`, putting the chevron flush against the
 *    left edge and the count against the right where the focus ring clipped it
 *  - the chevron had no fixed box, so labels shifted x between open and closed
 *  - counts were bare digits against a border rather than a pill
 *  - a hard 1px rule under every group read as table ruling at 7 groups
 */
import React from 'react';
import { theme } from '../../styles/tokens';

/**
 * Inline SVG rather than the 🔄 emoji, which rendered as a dated low-res glyph
 * and sat at a different baseline on each platform. Inherits currentColor.
 */
export function RefreshIcon({ spinning = false }: { spinning?: boolean }) {
  return (
    <svg
      width="12" height="12" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true"
      style={spinning ? { animation: 'gwspin 900ms linear infinite' } : undefined}
    >
      <path d="M21 12a9 9 0 1 1-3.2-6.9" />
      <path d="M21 3v6h-6" />
    </svg>
  );
}

export const panelRefreshBtnStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
  fontSize: 'var(--fs-2xs)',
  padding: '4px 9px',
  borderRadius: theme.radius.button,
  border: '1px solid var(--color-border)',
  background: 'var(--color-surface)',
  color: 'var(--color-text-secondary)',
  cursor: 'pointer',
};

/** One refresh control for every panel, so label and spinner never diverge. */
export function RefreshButton({
  onClick,
  loading = false,
  label = 'Refresh',
  ariaLabel,
}: {
  onClick: () => void;
  loading?: boolean;
  label?: string;
  ariaLabel?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={loading}
      aria-label={ariaLabel ?? label}
      style={{
        ...panelRefreshBtnStyle,
        opacity: loading ? 0.5 : 1,
        cursor: loading ? 'not-allowed' : 'pointer',
      }}
    >
      <RefreshIcon spinning={loading} />
      <span>{loading ? 'Refreshing…' : label}</span>
    </button>
  );
}

export const panelSectionHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  width: '100%',
  fontSize: 'var(--fs-2xs)',
  fontWeight: 600,
  color: 'var(--color-text-secondary)',
  textTransform: 'uppercase',
  letterSpacing: 0.5,
  padding: '9px 10px',
  marginBottom: 2,
  background: 'none',
  border: 'none',
  borderRadius: theme.radius.button,
  cursor: 'pointer',
  textAlign: 'left',
  boxShadow: 'inset 0 -1px 0 var(--color-border)',
};

/** Same metrics as the collapsible header, for panels whose sections don't collapse. */
export const panelStaticHeaderStyle: React.CSSProperties = {
  ...panelSectionHeaderStyle,
  cursor: 'default',
};

// --- Settings modal close button -----------------------------------------
//
// `SettingsView` positions its ✕ absolutely at the modal's top-right, so it
// floats OVER the content pane rather than taking space in it. Any panel with a
// top-right action therefore renders UNDERNEATH it — which is exactly what
// happened to the Gateways tab, where Refresh sat beneath the ✕ and read as two
// controls stacked on each other.
//
// These live here, in the shared settings chrome, rather than being exported
// from `SettingsView`: that module imports the panels, so importing back from it
// would be a cycle, and a style object read at module-eval time is precisely
// where a cycle yields `undefined`. Two panels needing the same number is also
// what this module is for.
export const SETTINGS_CLOSE_BUTTON_SIZE = 28;
export const SETTINGS_CLOSE_BUTTON_INSET = 12;

/**
 * Horizontal space a settings panel must leave clear on its first row so the
 * modal's ✕ does not land on top of the panel's own controls.
 */
export const SETTINGS_CLOSE_BUTTON_GUTTER =
  SETTINGS_CLOSE_BUTTON_SIZE + SETTINGS_CLOSE_BUTTON_INSET;

export const panelChevronStyle: React.CSSProperties = {
  display: 'inline-flex',
  justifyContent: 'center',
  // Fixed box so every label starts at the same x, collapsed or expanded.
  width: 12,
  flexShrink: 0,
  fontSize: 'var(--fs-3xs)',
  color: 'var(--color-text-secondary)',
  transition: 'transform 120ms ease',
};

export const panelCountBadgeStyle: React.CSSProperties = {
  marginLeft: 'auto',
  fontSize: 'var(--fs-3xs)',
  fontWeight: 600,
  color: 'var(--color-text-secondary)',
  textTransform: 'none',
  letterSpacing: 0,
  background: 'color-mix(in srgb, var(--color-text-secondary) 12%, transparent)',
  borderRadius: theme.radius.pill,
  padding: '1px 7px',
  minWidth: 20,
  textAlign: 'center',
};

export const panelGroupDotStyle: React.CSSProperties = {
  width: 6,
  height: 6,
  borderRadius: '50%',
  flexShrink: 0,
};

/** Stable colour per group key, so provenance reads at a glance. */
const GROUP_COLOURS = [
  theme.colors.primary, theme.colors.accent, theme.colors.primary, theme.colors.success,
  theme.colors.warning, theme.colors.error, theme.colors.accent, theme.colors.primary,
];

export function groupColour(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return GROUP_COLOURS[h % GROUP_COLOURS.length];
}

/** Collapsible section header used across the Capabilities panels. */
export function SectionHeader({
  label,
  count,
  collapsed,
  onToggle,
  colourKey,
}: {
  label: string;
  count?: number;
  collapsed?: boolean;
  onToggle?: () => void;
  colourKey?: string;
}) {
  const interactive = typeof onToggle === 'function';
  return (
    <button
      type="button"
      onClick={onToggle}
      style={interactive ? panelSectionHeaderStyle : panelStaticHeaderStyle}
      aria-expanded={interactive ? !collapsed : undefined}
      disabled={!interactive}
    >
      {interactive && (
        <span style={panelChevronStyle}>{collapsed ? '▸' : '▾'}</span>
      )}
      {colourKey && (
        <span style={{ ...panelGroupDotStyle, background: groupColour(colourKey) }} />
      )}
      <span>{label}</span>
      {typeof count === 'number' && <span style={panelCountBadgeStyle}>{count}</span>}
    </button>
  );
}

/**
 * The heading above one section of an INVENTORY list — "Yours (drafts)",
 * "Published", "Built-in".
 *
 * Distinct from `SectionHeader` above, which is the collapsible group header the
 * Gateway browser uses (chevron, count, colour dot). This one is static: an
 * inventory has two or three fixed sections, and a chevron on each invites
 * collapsing a list that is already short.
 *
 * Defined here because it existed THREE times — in `CapabilitiesSettings` for
 * Skills and Tools, again in `HooksPanel`, and again as `AgentSectionHeader` in
 * `WorkspaceView` — each a byte-identical copy of the same flex wrapper and
 * `18px 0 6px` margins, two of them carrying a comment promising to mirror the
 * first. Copies with a "keep in sync" note are drift with a delay.
 *
 * There is deliberately no `hint` prop. The Skills copy accepted one and rendered
 * nothing, so three call sites passed explanatory sentences that never reached the
 * screen; adding the line for real would have put prose under every heading on a
 * surface that has just been tightened up. If a section needs explaining, the rows
 * beneath it are the place.
 */
export function InventorySectionHeader({
  label,
  action,
}: {
  label: React.ReactNode;
  /** Optional trailing link, e.g. "Show all". */
  action?: { label: string; onClick: () => void };
}) {
  return (
    <div style={inventorySectionHeaderRowStyle}>
      <div style={panelStaticHeaderStyle}>{label}</div>
      {action && (
        <button type="button" onClick={action.onClick} style={inventorySectionActionStyle}>
          {action.label}
        </button>
      )}
    </div>
  );
}

const inventorySectionHeaderRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'baseline',
  justifyContent: 'space-between',
  margin: '18px 0 6px',
};

const inventorySectionActionStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  fontSize: 'var(--fs-2xs)',
  cursor: 'pointer',
};
