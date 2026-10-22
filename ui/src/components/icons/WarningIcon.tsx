import React from 'react';

/**
 * Warning triangle, as inline SVG.
 *
 * Replaces a mix of `⚠️` (U+26A0 + U+FE0F — EMOJI presentation, so it renders as
 * a platform colour glyph that ignores `color`, sits on its own baseline, and
 * differs between macOS, Windows and Linux) and `⚠` (U+26A0 alone — TEXT
 * presentation). Both were in use, sometimes for the same state on adjacent
 * surfaces, so the same warning was a yellow emoji in one place and a thin
 * monochrome outline in another.
 *
 * `panelChrome`'s RefreshIcon already documents this for the 🔄 it replaced; that
 * fix never propagated to the warning glyph.
 *
 * Inherits `currentColor` and scales with `font-size` via the `1em` default, so it
 * takes the colour of the message it sits in and tracks the Font Size setting.
 */
export default function WarningIcon({ size = '1em' }: { size?: number | string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      /* Nudges the glyph onto the text baseline; `middle` leaves it floating high
         next to 11–13px copy. */
      style={{ verticalAlign: '-0.125em', flexShrink: 0 }}
    >
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}
