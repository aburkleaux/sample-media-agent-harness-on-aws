/**
 * The theme choice, in one place.
 *
 * Shared because it is read twice at different times: `main.tsx` applies it
 * before React mounts (so there is no flash of the wrong theme), and Settings →
 * General writes it. Two copies of the migration below would drift.
 */

export const THEMES = [
  { id: 'light', label: 'Light' },
  { id: 'dark', label: 'Dark' },
  { id: 'dark-kirocrew', label: 'KiroCrew' },
] as const;

export type ThemeName = (typeof THEMES)[number]['id'];

const STORAGE_KEY = 'lab-theme';
/** Predates having more than two themes; read once so nobody is reset. */
const LEGACY_BOOLEAN_KEY = 'lab-dark-mode';

function isThemeName(value: unknown): value is ThemeName {
  return THEMES.some(t => t.id === value);
}

/**
 * The theme to apply on load.
 *
 * Falls back to the old `lab-dark-mode` boolean when no named theme has been
 * chosen yet, so a user who was on dark before this shipped stays on dark
 * instead of being silently returned to light. Once a named theme is stored it
 * wins outright and the legacy key is ignored.
 */
export function readThemePreference(): ThemeName {
  let named: string | null = null;
  try {
    named = localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private-mode / disabled storage: fall through to the default.
  }
  if (isThemeName(named)) return named;

  try {
    if (localStorage.getItem(LEGACY_BOOLEAN_KEY) === 'true') return 'dark';
  } catch {
    /* ignore */
  }
  return 'light';
}

/** Persist and apply. `light` still sets the attribute explicitly, so toggling
 *  away from a dark theme does not leave the previous one in place. */
export function writeThemePreference(theme: ThemeName): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* ignore — applying still works for this session */
  }
  applyTheme(theme);
}

export function applyTheme(theme: ThemeName): void {
  document.documentElement.setAttribute('data-theme', theme);
}
