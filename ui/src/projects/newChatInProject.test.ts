/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from 'vitest';
import { NEW_CHAT_IN_PROJECT_EVENT, onNewChatInProjectRequest, requestNewChatInProject } from './newChatInProject';

describe('new chat in a project (asset-lineage D1)', () => {
  it('delivers the request to the host until it unsubscribes', () => {
    const handler = vi.fn();
    const off = onNewChatInProjectRequest(handler);
    requestNewChatInProject('p1', 'Launch Q3');
    expect(handler).toHaveBeenCalledWith({ projectId: 'p1', projectName: 'Launch Q3' });
    off();
    requestNewChatInProject('p2', 'Other');
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('ignores an event that names no project', () => {
    const handler = vi.fn();
    const off = onNewChatInProjectRequest(handler);
    window.dispatchEvent(new CustomEvent(NEW_CHAT_IN_PROJECT_EVENT, { detail: { projectName: 'x' } }));
    window.dispatchEvent(new CustomEvent(NEW_CHAT_IN_PROJECT_EVENT));
    expect(handler).not.toHaveBeenCalled();
    off();
  });
});
