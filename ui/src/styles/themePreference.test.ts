// @vitest-environment jsdom
//
// The theme choice and its migration. Worth pinning because the failure is
// silent and personal: get the migration wrong and every existing user is
// quietly returned to light on their next load.
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { THEMES, applyTheme, readThemePreference, writeThemePreference } from './themePreference';

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});
afterEach(() => localStorage.clear());

describe('theme preference', () => {
  it('offers exactly light, dark and dark-kirocrew', () => {
    expect(THEMES.map(t => t.id)).toEqual(['light', 'dark', 'dark-kirocrew']);
  });

  it('defaults to light for someone with no stored preference', () => {
    expect(readThemePreference()).toBe('light');
  });

  it('keeps a user who was on dark before named themes existed', () => {
    // The old boolean is the only thing they have. Ignoring it would look like
    // the app forgot their choice.
    localStorage.setItem('lab-dark-mode', 'true');
    expect(readThemePreference()).toBe('dark');
  });

  it('treats the legacy boolean being false as light', () => {
    localStorage.setItem('lab-dark-mode', 'false');
    expect(readThemePreference()).toBe('light');
  });

  it('prefers a named theme over the stale legacy boolean', () => {
    // Choosing light after having been on dark leaves the old key saying "true";
    // the named choice has to win or the preference flips back on reload.
    localStorage.setItem('lab-dark-mode', 'true');
    localStorage.setItem('lab-theme', 'light');
    expect(readThemePreference()).toBe('light');
  });

  it('ignores a stored value that is not a theme', () => {
    localStorage.setItem('lab-theme', 'solarized');
    expect(readThemePreference()).toBe('light');
  });

  it('persists and applies the chosen theme', () => {
    writeThemePreference('dark-kirocrew');
    expect(localStorage.getItem('lab-theme')).toBe('dark-kirocrew');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark-kirocrew');
    expect(readThemePreference()).toBe('dark-kirocrew');
  });

  it('sets the attribute for light too, so a dark theme is not left in place', () => {
    applyTheme('dark-kirocrew');
    writeThemePreference('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });
});
