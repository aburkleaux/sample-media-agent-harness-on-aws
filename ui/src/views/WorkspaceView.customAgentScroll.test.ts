/**
 * Guards the scroll region on the custom-agent center mode.
 *
 * The bug this exists for: the `custom-agent` branch mounts
 * `SpecialtyAgentEditor` / `PublishedAgentView` / `CustomAgentCenterList`
 * DIRECTLY, whereas the `skills` branch routes through `CenterEditor` and
 * inherits that component's `editorContentStyle` scroll region. The center pane
 * (`centerStyle`) is deliberately `overflow: hidden`, so without a scroller of
 * its own the custom-agent content grew past the pane and the bottom became
 * unreachable — measured at 1330px of editor in a 1204px pane, which put Save
 * and Cancel below the fold with no scrollbar. (That measurement predates the
 * removal of the editor's two test panels, so the editor is now shorter — but
 * the published view still embeds `AgentTestPanel` and overflows, so the
 * scroller is still load-bearing.)
 *
 * What this test can and cannot do: jsdom has no layout engine, so it cannot
 * measure scrollHeight vs clientHeight or prove that scrolling works. It
 * therefore asserts the contract instead — that all three properties are
 * present — so the fix cannot be silently dropped. `minHeight: 0` is asserted
 * explicitly because it is the non-obvious one: a flex child will not shrink
 * below its content height without it, so `overflowY: 'auto'` on its own is
 * inert. Someone "tidying" that line would reintroduce the bug with the other
 * two properties still in place.
 *
 * Real scroll behaviour was verified in a browser against the deployed sandbox.
 */
import { describe, it, expect } from 'vitest';
import { caBodyStyle, customAgentCenterScrollStyle } from './WorkspaceView';

describe('custom-agent center scroll region', () => {
  it('scrolls its own overflow rather than relying on the center pane', () => {
    expect(customAgentCenterScrollStyle.overflowY).toBe('auto');
  });

  it('can shrink below content height — minHeight: 0 is required, not cosmetic', () => {
    expect(customAgentCenterScrollStyle.minHeight).toBe(0);
  });

  it('fills the center pane so the scroll region is the full height', () => {
    expect(customAgentCenterScrollStyle.flex).toBe(1);
  });

  /*
   * The INNER scroller — the inventory / published-detail body, below the fixed
   * search row. Same `minHeight: 0` story, different consequence: without it that
   * box grew to its content instead of scrolling, so the whole surface scrolled in
   * the outer region above and took the search row up with it. Skills' equivalent
   * (`SkillsCenter`'s `bodyStyle`) has always had it, which is why only Agents lost
   * its toolbar on scroll.
   */
  it('scrolls the agent list UNDER the fixed search row', () => {
    expect(caBodyStyle.overflowY).toBe('auto');
    expect(caBodyStyle.minHeight).toBe(0);
    expect(caBodyStyle.flex).toBe(1);
  });
});
