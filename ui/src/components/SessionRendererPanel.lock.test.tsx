/**
 * @vitest-environment jsdom
 *
 * #3 (client): a locked (frozen) session freezes render-tab MUTATIONS and WORK-triggering
 * controls — remove-tab, refresh (re-invokes), and clicking an un-rendered tab (dispatches a
 * render). Navigating to an already-rendered tab still works so the frozen session can be shown.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SessionRendererPanel from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';

function rendered(): RenderHistoryEntry {
  return {
    label: 'A', timestamp: 1, artifactUrls: ['https://cdn/a.png'],
    blocks: [{ template: 'image-annotated', title: 'A', data: { src: 'https://cdn/a.png', images: [{ src: 'https://cdn/a.png', label: 'a' }] } }],
  } as unknown as RenderHistoryEntry;
}
function lazy(): RenderHistoryEntry {
  return { label: 'B', timestamp: 2, artifactUrls: ['https://cdn/b.png'], blocks: [] } as unknown as RenderHistoryEntry;
}

function view(locked: boolean, spies: { onRemoveTab: () => void; onRenderTab: () => void; onNavigate: (i: number) => void }) {
  return render(
    <SessionRendererPanel
      history={[rendered(), lazy()]}
      currentIndex={0}
      onNavigate={spies.onNavigate}
      onClose={vi.fn()}
      onRemoveTab={spies.onRemoveTab}
      onRenderTab={spies.onRenderTab}
      locked={locked}
    />,
  );
}

describe('SessionRendererPanel — locked freeze', () => {
  it('disables refresh + remove-tab and blocks the render-trigger when locked', () => {
    const onRemoveTab = vi.fn(); const onRenderTab = vi.fn(); const onNavigate = vi.fn();
    view(true, { onRemoveTab, onRenderTab, onNavigate });

    expect((screen.getByTestId('refresh-render') as HTMLButtonElement).disabled).toBe(true);

    const removes = screen.getAllByLabelText(/Remove .* tab/) as HTMLButtonElement[];
    expect(removes[0].disabled).toBe(true);
    fireEvent.click(removes[0]);
    expect(onRemoveTab).not.toHaveBeenCalled();

    // Clicking the un-rendered tab still NAVIGATES but must not dispatch a render.
    const tabs = screen.getAllByRole('tab');
    fireEvent.click(tabs[1]);
    expect(onNavigate).toHaveBeenCalledWith(1);
    expect(onRenderTab).not.toHaveBeenCalled();
  });

  it('allows refresh, remove, and the render-trigger when unlocked (control)', () => {
    const onRemoveTab = vi.fn(); const onRenderTab = vi.fn(); const onNavigate = vi.fn();
    view(false, { onRemoveTab, onRenderTab, onNavigate });

    expect((screen.getByTestId('refresh-render') as HTMLButtonElement).disabled).toBe(false);

    const tabs = screen.getAllByRole('tab');
    fireEvent.click(tabs[1]);
    expect(onNavigate).toHaveBeenCalledWith(1);
    // The render trigger names the tab by its stable key (R8.3), not its index.
    expect(onRenderTab).toHaveBeenCalledWith('urls:https://cdn/b.png');
  });
});
