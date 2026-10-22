// @vitest-environment jsdom
// A turn in which the agent filed files (organize_project) moved them behind every open view: the folder store is
// told to re-ask. Any other turn leaves it alone.
import { describe, it, expect, vi } from 'vitest';

const h = vi.hoisted(() => ({ invalidate: vi.fn() }));
vi.mock('./projects/assetFolders', async (orig) => ({ ...(await orig<typeof import('./projects/assetFolders')>()),
  invalidateAssetFolders: h.invalidate }));

import { summaryEvent, type ExecutionSummary } from './api';

const summary = (tools: string[]) => ({ type: 'execution_summary', total_duration_ms: 1, cycles: 1, usage: {},
  tools: Object.fromEntries(tools.map((t) => [t, { calls: 1, total_ms: 1, avg_ms: 1 }])) }) as ExecutionSummary;

describe('summaryEvent', () => {
  it('re-asks where files live after organize_project', () => {
    expect(summaryEvent(summary(['project_tools___organize_project'])).type).toBe('execution_summary');
    expect(h.invalidate).toHaveBeenCalledTimes(1);
  });
  it('leaves the store alone for any other turn', () => {
    h.invalidate.mockClear();
    summaryEvent(summary(['search_content', 'project_status']));
    expect(h.invalidate).not.toHaveBeenCalled();
  });
});
