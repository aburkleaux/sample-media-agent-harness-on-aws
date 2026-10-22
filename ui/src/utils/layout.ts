// layout.ts — Layout width clamping utilities and localStorage persistence helpers.

// --- Layout constants ---

export const LEFT_MIN = 240;
export const LEFT_MAX = 500;
export const LEFT_DEFAULT = 280;

export const RIGHT_MIN = 300;
export const RIGHT_MAX = 1200;
// Default render panel width. The pro timeline editor + composite lanes + a
// wider clip player want real room, so this opens ~50% wider than the prior 660.
export const RIGHT_DEFAULT = 990;
export const RIGHT_COLLAPSED = 48;

/**
 * Horizontal space a center-mode header must leave clear on its right.
 *
 * `WorkspaceView` floats the notification bell at `top: 10 / right: 14` on every
 * non-chat center mode, at `zIndex.rail`, so it paints OVER whatever the pane
 * puts up there. Any header with a top-right control has to reserve this or the
 * bell silently covers it — which is what happened to `SpecialtyAgentEditor`'s
 * close button: the `✕` was drawn at x1017–1042 and the bell at x1021–1051, so
 * clicking the visible glyph hit the bell instead.
 *
 * A constant rather than the `52` that was copy-pasted into four separate
 * headers, one of which then forgot it.
 */
export const BELL_SAFE_AREA = 52;

// --- Width clamping ---

/**
 * Clamps a numeric value to the range [min, max].
 */
export function clampWidth(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

// --- Fitting the panels to the window ---

/** The chat / center column never gets narrower than this while a side panel can still give way. */
export const CENTER_MIN = 440;

/**
 * The widths to DRAW the side panels at, given the window. The saved widths are what the user dragged to on
 * some screen; drawn as-is in a smaller window they crushed the center column (a 1100px window with a 415px
 * sidebar and a 440px panel left the composer 245px, wrapping "Type a message" one word per line, and slid the
 * top bar's bell under the panel). The right panel gives way first, down to RIGHT_MIN, then the sidebar, down
 * to LEFT_MIN; below that the center shrinks. A collapsed sidebar keeps its rail width; a collapsed
 * panel takes none. Never persisted, so the
 * user's widths come back in a bigger window.
 */
export function fitPanelWidths(
  viewport: number, left: number, right: number, leftCollapsed: boolean, rightCollapsed: boolean, leftRail: number,
): { left: number; right: number } {
  let l = leftCollapsed ? leftRail : left;
  let r = rightCollapsed ? 0 : right;  // a collapsed panel is only a floating edge toggle
  let over = l + r + CENTER_MIN - viewport;
  if (over > 0 && !rightCollapsed) {
    const give = Math.min(over, Math.max(0, r - RIGHT_MIN));
    r -= give; over -= give;
  }
  if (over > 0 && !leftCollapsed) {
    const give = Math.min(over, Math.max(0, l - LEFT_MIN));
    l -= give;
  }
  return { left: l, right: r };
}

// --- localStorage persistence ---

/**
 * Persists a width value to localStorage under the given key.
 */
export function persistWidth(key: string, value: number): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Silently ignore storage errors (e.g., quota exceeded, private browsing)
  }
}

/**
 * Reads a width value from localStorage, parses it as a number, clamps it
 * to [min, max], and returns the result. Returns defaultValue if the key
 * is not found or the stored value is not a valid number.
 */
export function readWidth(key: string, defaultValue: number, min: number, max: number): number {
  try {
    const stored = localStorage.getItem(key);
    if (stored === null) return defaultValue;
    const parsed = Number(stored);
    if (Number.isNaN(parsed)) return defaultValue;
    return clampWidth(parsed, min, max);
  } catch {
    return defaultValue;
  }
}
