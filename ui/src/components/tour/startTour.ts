/**
 * Reopen the harness tour from anywhere (the sidebar's "Take the tour"). A window event rather than
 * shared state, so callers don't import the tour itself and the tour stays the only owner of its stage.
 */
export const TOUR_START_EVENT = 'harness-tour:start';

export function startHarnessTour(): void {
  window.dispatchEvent(new Event(TOUR_START_EVENT));
}
