import React from 'react';

/**
 * Shared frame for the single-stroke UI icons in this folder.
 *
 * Matches the sidebar's nav glyphs exactly (`ProjectsIcon`, `WorkflowIcon` in
 * `SessionSidebar`): 16px by default, 24x24 viewBox, no fill, `currentColor`
 * stroke at 1.75 with round caps and joins. Colour comes from the caller's
 * `color`, so an icon follows whatever theme token its button uses.
 *
 * Always `aria-hidden`: the control that holds the icon carries the accessible
 * name, so the glyph must not add a second one.
 */
export interface IconProps {
  /** Rendered width and height in px. */
  size?: number;
  /** Layout-only escape hatch (e.g. `flexShrink`, `verticalAlign`). */
  style?: React.CSSProperties;
}

export default function IconBase({ size = 16, style, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      style={{ flexShrink: 0, ...style }}
    >
      {children}
    </svg>
  );
}
