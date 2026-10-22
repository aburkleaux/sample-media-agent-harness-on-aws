export const theme = {
  colors: {
    primary: 'var(--color-primary)',
    accent: 'var(--color-accent)',
    textPrimary: 'var(--color-text)',
    textSecondary: 'var(--color-text-secondary)',
    /** Third text level — meta, timestamps, disabled. Was a raw #888/#999. */
    textTertiary: 'var(--color-text-tertiary)',
    surface: 'var(--color-surface)',
    /** Panels that sit ON a surface (modals, nested cards). */
    surfaceRaised: 'var(--color-surface-raised)',
    /**
     * Neutral fill for furniture: skeletons, table headers, read-only grounds,
     * chips. Use this — NOT `activeBg` — whenever the fill does not mean
     * "selected". Was the raw #fafafa / #f5f5f5 greys.
     */
    surfaceSubtle: 'var(--color-surface-subtle)',
    background: 'var(--color-background)',
    border: 'var(--color-border)',
    /** Inputs, dividers that must read against a surface. Was a raw #ccc/#d1d5db. */
    borderStrong: 'var(--color-border-strong)',
    success: 'var(--color-success)',
    error: 'var(--color-error)',
    /** Distinct from `accent` now — warning was aliased to it and could not be
     *  tuned separately per theme. */
    warning: 'var(--color-warning)',
    /** Modal/dialog backdrop. Was raw rgba(0,0,0,…) at several opacities. */
    overlay: 'var(--color-overlay)',
    primaryHover: 'var(--color-primary-hover)',
    /**
     * Label colour for text sitting ON a filled primary/accent background.
     *
     * Not always white — `dark-kirocrew`'s mint primary needs a dark label, and
     * white on it is ~1.5:1. Any filled button must use this rather than a
     * hardcoded '#fff', or it breaks in that theme only.
     */
    onPrimary: 'var(--color-on-primary)',
    activeBg: 'var(--color-active-bg)',
    activeBorder: 'var(--color-primary)',
    sidebarBg: 'var(--color-sidebar-bg)',
    sidebarText: 'var(--color-sidebar-text)',
    sidebarTextSecondary: 'var(--color-sidebar-text-secondary)',
    sidebarActiveBg: 'var(--color-sidebar-active-bg)',
    sidebarAccent: 'var(--color-sidebar-accent)',
    accentSubtle: 'var(--color-accent-subtle)',
    primarySubtle: 'var(--color-primary-subtle)',
    sectionBorder: 'var(--color-section-border)',
  },
  typography: {
    /*
     * Var references, NOT literal stacks. These used to spell out the system
     * stack (`-apple-system, …`) and a mono stack with no `IBM Plex Mono` in it,
     * while `global.css` sets `--font-body`/`--font-mono` to the Plex faces that
     * `index.html` actually loads. So every consumer of these two tokens rendered
     * in a DIFFERENT typeface from the rest of the app — visible on the Agents and
     * Skills search inputs, which sat in system sans directly under Plex headings.
     *
     * Caveat for anything building CSS for a FOREIGN document: a `var()` only
     * resolves where the custom property is defined. `renderer/SandboxFrame`
     * interpolates font values into an `srcDoc` iframe, so it carries its own
     * literal stacks instead of reading these.
     */
    fontFamily: 'var(--font-body)',
    fontMono: 'var(--font-mono)',
    // CSS-var references (not fixed px) so every consumer of these tokens
    // rescales with the General → Font Size control. Values are defined in
    // global.css relative to --font-size-base; the comments show the px each
    // resolves to at the medium (14px) base — identical to the former ladder.
    sizes: {
      pageTitle: 'var(--fs-2xl)',    // 20
      sectionTitle: 'var(--fs-lg)',  // 16
      heading: 'var(--fs-md)',       // 14
      body: 'var(--fs-sm)',          // 13
      meta: 'var(--fs-2xs)',         // 11
      code: 'var(--fs-xs)',          // 12
      /** Badges, count pills, chevrons — the tier below `meta`. */
      badge: 'var(--fs-3xs)',        // 10
    },
    weights: { semibold: 600, medium: 500, regular: 400 },
    lineHeight: 1.5,
  },
  spacing: { unit: 4 },
  /*
   * `button` is 4, not 6.
   *
   * The app had two button families that never reconciled: one at radius 6 /
   * 13px (Skills, Hooks, Capabilities, the modals — and `ui/Button`), one at
   * radius 4 / 12px (the Agents, Tools and Workflow editors). Radius 4 was the
   * single most common literal in the codebase and was not a token, which is
   * precisely why the two drifted: half the app could not spell its own geometry.
   *
   * Family B won, so the token now carries its value. Two consequences worth
   * knowing: the Family A surfaces got tighter corners, and the ~100 literal
   * `borderRadius: 4` sites can now be tokenised with NO visual change at all.
   */
  radius: { panel: 8, button: 4, badge: 3, pill: 999, dot: '50%' },
  elevation: {
    flat: 'var(--elevation-flat)',
    subtle: 'var(--elevation-subtle)',
    elevated: 'var(--elevation-elevated)',
  },
  /**
   * Disabled and de-emphasised affordances. There were SIX values doing this job
   * (0.3, 0.35, 0.5, 0.55, 0.6, 0.65), so two disabled buttons side by side
   * faded by different amounts.
   */
  opacity: { disabled: 0.55, muted: 0.7 },
  /**
   * Stacking order. There were eleven ad-hoc values with no relationship between
   * them (1, 2, 6, 10, 20, 21, 30, 100, 1000, 2000, 10000), which meant picking a
   * z-index for anything new was guesswork — and `ConfirmDialog` at 10000 vs
   * `AddMcpServerModal` at 1000 encoded no actual intent about which wins.
   *
   * Six rungs, ordered by what sits on top of what.
   */
  zIndex: {
    /** Lifted within a panel, still in flow. */
    base: 1,
    /** Pinned rows or headers inside a scrolling pane. */
    sticky: 10,
    /** Floating app-shell furniture — edge toggles, the notification bell. */
    rail: 30,
    /** Menus and the scrims anchored to a single control. */
    popover: 100,
    /** Dialogs and their full-screen scrims. */
    modal: 1000,
    /** Notifications, which must clear a dialog. */
    toast: 2000,
  },
  transitions: { hover: '200ms ease', slide: '250ms ease-out', fade: '150ms ease-in-out' },
} as const;

/**
 * A status colour softened against the current surface.
 *
 * Status backgrounds used to be baked light tints (`#e8f5e9`, `#ffebee`,
 * `#fff3e0`) which stayed pastel in dark mode and blew a hole in the pane.
 * Mixing against `--color-surface` means ONE declaration is correct in both
 * themes, because the surface itself flips.
 *
 * @param color A theme colour token, e.g. `theme.colors.success`.
 * @param percent How much of the colour to keep. 12–16% reads as a tint; above
 *   ~25% it starts competing with filled buttons.
 */
export function tint(color: string, percent = 14): string {
  return `color-mix(in srgb, ${color} ${percent}%, var(--color-surface))`;
}

/**
 * The numeric spine (see `.numeric` in global.css): mono with tabular figures,
 * for versions, ids, durations and counts so they align in a column.
 */
export const numericStyle = {
  fontFamily: 'var(--font-mono)',
  fontVariantNumeric: 'tabular-nums',
} as const;
