/**
 * @vitest-environment jsdom
 *
 * Center-mode union contract (media-harness-projects-access task 13.9; design Component 11
 * "Registrations" 1, Req 10.3).
 *
 * `WorkspaceView.CENTER_MODES` and `SessionSidebar.SIDEBAR_CENTER_MODES` are two separate declarations by
 * design, so they can drift. This test is what catches drift two ways:
 *   - at run time, the two sets of mode strings must be equal;
 *   - at compile time, `Expect<Equal<CenterMode, SidebarCenterMode>>` fails `npx tsc --noEmit` if the
 *     types diverge (a new mode added to one union but not the other).
 */
import { describe, it, expect } from 'vitest';
import { CENTER_MODES, type CenterMode } from './WorkspaceView';
import { SIDEBAR_CENTER_MODES, type SidebarCenterMode } from '../components/SessionSidebar';

// --- Compile-time equality (erased at run time; a drift fails `tsc --noEmit`). ---
type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends (<T>() => T extends B ? 1 : 2) ? true : false;
type Expect<T extends true> = T;
// If these unions ever diverge, this line stops compiling.
type _CenterModesInStep = Expect<Equal<CenterMode, SidebarCenterMode>>;
// Reference the alias so an unused-local rule cannot quietly drop the check.
export const __centerModesInStep: _CenterModesInStep = true;

describe('center-mode unions', () => {
  it('WorkspaceView.CENTER_MODES and SessionSidebar.SIDEBAR_CENTER_MODES hold the same modes', () => {
    expect(new Set(SIDEBAR_CENTER_MODES)).toEqual(new Set(CENTER_MODES));
  });

  it('includes the projects mode', () => {
    expect(new Set(CENTER_MODES).has('projects')).toBe(true);
  });
});
