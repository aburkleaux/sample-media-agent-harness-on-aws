import React from 'react';

/**
 * Shared modern trash/delete icon. One glyph for every delete affordance so the
 * app doesn't mix a 🗑️ emoji, a ✕ (which reads as "close"), and bespoke SVGs.
 * Inherits `currentColor` and sizes to `size` (default 14) so callers control
 * colour + scale via their button style.
 */
export default function TrashIcon({ size = 14 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 6h18" />
      <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  );
}
