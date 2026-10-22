import React from 'react';

/**
 * Shared padlock icon for the session-lock affordance. Same single-stroke
 * house style as {@link TrashIcon} (24x24 viewBox, `currentColor`, 1.8 stroke,
 * round caps) so it blends with the existing row-action icons — callers control
 * colour + scale via their button style.
 *
 * `open` renders the unlocked glyph (shackle swung open) for the toggle's
 * "unlocked" state; the default closed padlock is the locked indicator.
 */
export default function LockIcon({ size = 14, open = false }: { size?: number; open?: boolean }) {
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
      {/* Body */}
      <rect x="4.5" y="10.5" width="15" height="9.5" rx="2" />
      {/* Shackle — closed arcs down to both shoulders; open leaves the right leg raised */}
      {open ? (
        <path d="M8 10.5V7a4 4 0 0 1 7.6-1.4" />
      ) : (
        <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
      )}
      {/* Keyhole */}
      <circle cx="12" cy="14.6" r="1.15" />
      <path d="M12 15.75V17.5" />
    </svg>
  );
}
