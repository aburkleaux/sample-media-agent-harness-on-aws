/**
 * @vitest-environment jsdom
 *
 * Category cutover must default to the legacy path (R14 / Property 6).
 *
 * The whole rollback story is "flip a flag, no deploy, no data change", which
 * only holds if the client honours the flag and treats every ambiguity as
 * `legacy`. The failure mode of getting this wrong is not a crash: it is the
 * Capabilities tab quietly reading from a control plane that is not ready,
 * showing an empty inventory that looks like data loss.
 *
 * These tests drive the real provider and assert which client module gets
 * called, rather than asserting on rendered output — the routing decision is
 * the behaviour under test.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import React from 'react';

// `vi.hoisted` because `vi.mock` factories are lifted above the module's own
// top-level statements — referencing a plain `const` from one throws
// "Cannot access 'legacy' before initialization".
const { legacy, capability } = vi.hoisted(() => ({
  legacy: {
    listSkillDrafts: vi.fn(),
    listSpecialtyAgentDrafts: vi.fn(),
    listToolCollectionDrafts: vi.fn(),
    listWorkflowDrafts: vi.fn(),
    listMyPublishedAssets: vi.fn(),
  },
  capability: {
    fetchCapabilityConfig: vi.fn(),
    bootstrapCapabilities: vi.fn(),
  },
}));

vi.mock('./lab-api', () => legacy);
vi.mock('./capability-api', async () => {
  // Only the two network functions are stubbed; the pure adapters are the real
  // implementations, so this exercises the mapping the provider actually uses.
  const actual = await vi.importActual<typeof import('./capability-api')>(
    './capability-api',
  );
  return { ...actual, ...capability };
});

import { LabContextProvider, useLabContext } from './LabContext';

function Probe({ category }: { category: 'skill' | 'workflow' | 'tool-collection' }) {
  const { ensureCategoryLoaded, skills, workflows, toolCollections } = useLabContext();
  React.useEffect(() => {
    void ensureCategoryLoaded(category);
  }, [ensureCategoryLoaded, category]);
  const rows = category === 'skill' ? skills : category === 'workflow' ? workflows : toolCollections;
  // `supportingFiles` must stay distinguishable between "source did not report
  // it" and "reported as empty" — rendering the two identically is what made a
  // 3-resource skill claim "1 file · SKILL.md".
  return (
    <>
      <div data-testid="count">{rows.length}</div>
      <div data-testid="skill-files">
        {skills.length === 0
          ? 'no-rows'
          : skills[0].supportingFiles === undefined
            ? 'absent'
            : `n=${skills[0].supportingFiles.length}`}
      </div>
      {/* Same distinction for a collection's tools. `toolRefs: []` here made
          `media-tools` — nine gateway refs in DynamoDB — render "0 tools". */}
      <div data-testid="tc-refs">
        {toolCollections.length === 0
          ? 'no-rows'
          : toolCollections[0].toolRefs === undefined
            ? 'absent'
            : `n=${toolCollections[0].toolRefs.length}`}
      </div>
    </>
  );
}

function renderProbe(category: 'skill' | 'workflow' | 'tool-collection' = 'skill') {
  return render(
    <LabContextProvider>
      <Probe category={category} />
    </LabContextProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  legacy.listSkillDrafts.mockResolvedValue([]);
  legacy.listSpecialtyAgentDrafts.mockResolvedValue([]);
  legacy.listToolCollectionDrafts.mockResolvedValue([]);
  legacy.listWorkflowDrafts.mockResolvedValue([]);
  legacy.listMyPublishedAssets.mockResolvedValue([]);
  capability.fetchCapabilityConfig.mockResolvedValue({
    deployEnv: 'sandbox',
    capabilitySources: {},
    registryConfigured: false,
  });
  capability.bootstrapCapabilities.mockResolvedValue({});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('capability cutover flag', () => {
  it('uses the legacy /lab path when no flag is set', async () => {
    renderProbe();
    await waitFor(() => expect(legacy.listSkillDrafts).toHaveBeenCalled());
    expect(capability.bootstrapCapabilities).not.toHaveBeenCalled();
  });

  it('uses the legacy path when the flag says legacy', async () => {
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'legacy' },
      registryConfigured: true,
    });
    renderProbe();
    await waitFor(() => expect(legacy.listSkillDrafts).toHaveBeenCalled());
    expect(capability.bootstrapCapabilities).not.toHaveBeenCalled();
  });

  it('keeps serving legacy in shadow mode', async () => {
    // Shadow compares asynchronously on the server. If the browser switched
    // sources here, the comparison would be measuring itself.
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'shadow' },
      registryConfigured: true,
    });
    renderProbe();
    await waitFor(() => expect(legacy.listSkillDrafts).toHaveBeenCalled());
    expect(capability.bootstrapCapabilities).not.toHaveBeenCalled();
  });


  /*
   * Absent ≠ empty for a collection's tools. The Capability API's normalized
   * summary carries no `toolRefs`, and this mapper used to substitute `[]`, so
   * every listed collection claimed zero tools — verified in sandbox against
   * `media-tools`, which holds nine. The field must stay `undefined` so the row
   * can decline to state a count.
   */
  it('leaves toolRefs absent rather than fabricating an empty list', async () => {
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { 'tool-collection': 'canonical' },
      registryConfigured: true,
    });
    capability.bootstrapCapabilities.mockResolvedValue({
      'tool-collection': {
        category: 'tool-collection',
        items: [
          {
            id: 'tool-collection:tc-1',
            category: 'tool-collection',
            capabilityClass: 'asset',
            displayName: 'media-tools',
            scope: 'private',
            status: 'draft',
            source: 'asset-index',
            assetId: 'tc-1',
            updatedAt: '2026-08-25T12:00:00Z',
          },
        ],
        sourcesQueried: ['asset-index'],
      },
    });

    const { getByTestId } = renderProbe('tool-collection');
    await waitFor(() => expect(getByTestId('count').textContent).toBe('1'));
    expect(getByTestId('tc-refs').textContent).toBe('absent');
  });

  it('uses the Capability API in canonical mode', async () => {
    // `canonical` is what actually removes the runtime cold start (measured in
    // sandbox: ~7.5s first open of the Agents tab vs ~220ms). The browser makes
    // the identical request as in `registry` mode — whether Registry rows are
    // merged in is decided server-side — so this must not read as legacy.
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'canonical' },
      registryConfigured: true,
    });
    renderProbe();
    await waitFor(() => expect(capability.bootstrapCapabilities).toHaveBeenCalledWith(['skill']));
    expect(legacy.listSkillDrafts).not.toHaveBeenCalled();
    expect(legacy.listMyPublishedAssets).not.toHaveBeenCalled();
  });

  it('treats canonical as a Capability API mode even with no registry configured', async () => {
    // It reads nothing from the Registry, so it must not be gated on one
    // existing — otherwise the latency fix would depend on provisioning.
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'canonical' },
      registryConfigured: false,
    });
    renderProbe();
    await waitFor(() => expect(capability.bootstrapCapabilities).toHaveBeenCalled());
    expect(legacy.listSkillDrafts).not.toHaveBeenCalled();
  });

  it('uses the Capability API in registry mode, with one request for both lists', async () => {
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'registry' },
      registryConfigured: true,
    });
    capability.bootstrapCapabilities.mockResolvedValue({
      skill: {
        category: 'skill',
        items: [
          {
            id: 'skill:asset-1',
            category: 'skill',
            capabilityClass: 'asset',
            displayName: 'Working copy',
            scope: 'private',
            status: 'draft',
            source: 'asset-index',
            assetId: 'asset-1',
            updatedAt: '2026-08-13T12:00:00Z',
          },
          {
            id: 'skill:asset-1:v1',
            category: 'skill',
            capabilityClass: 'asset',
            displayName: 'Version one',
            scope: 'org',
            status: 'published',
            source: 'asset-index',
            assetId: 'asset-1',
            version: 1,
            updatedAt: '2026-08-13T12:00:00Z',
          },
        ],
      },
    });

    const { getByTestId } = renderProbe();
    await waitFor(() => expect(getByTestId('count')).toHaveTextContent('1'));

    // One bootstrap request covers working copies AND versions (R13), where the
    // legacy path needs two round trips through the agent runtime.
    expect(capability.bootstrapCapabilities).toHaveBeenCalledTimes(1);
    expect(capability.bootstrapCapabilities).toHaveBeenCalledWith(['skill']);
    expect(legacy.listSkillDrafts).not.toHaveBeenCalled();
    expect(legacy.listMyPublishedAssets).not.toHaveBeenCalled();

    // The normalized summary carries no file list, so this must be ABSENT rather
    // than an empty array — `formatSkillPackageCount` keys off exactly that to
    // avoid asserting a total it cannot know.
    expect(getByTestId('skill-files')).toHaveTextContent('absent');
  });

  it('reports the file list as absent, not empty, on the Capability API path', async () => {
    // Guards the cold-start flip for `skill` (env-config `capabilitySources`).
    // Moving Skills off the 7s `/lab` path is only safe if the row it produces
    // does not silently claim "1 file · SKILL.md" for a skill with resources.
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'canonical' },
      registryConfigured: false,
    });
    capability.bootstrapCapabilities.mockResolvedValue({
      skill: {
        category: 'skill',
        items: [{
          id: 'skill:asset-9', category: 'skill', capabilityClass: 'asset',
          displayName: 'Has resources upstream', scope: 'private', status: 'draft',
          source: 'asset-index', assetId: 'asset-9', updatedAt: '2026-08-14T12:00:00Z',
        }],
      },
    });

    const { getByTestId } = renderProbe();
    await waitFor(() => expect(getByTestId('count')).toHaveTextContent('1'));
    expect(getByTestId('skill-files')).toHaveTextContent('absent');
    expect(getByTestId('skill-files')).not.toHaveTextContent('n=0');
  });

  it('cuts over one category at a time', async () => {
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'registry' },
      registryConfigured: true,
    });
    capability.bootstrapCapabilities.mockResolvedValue({
      workflow: { category: 'workflow', items: [] },
    });
    renderProbe('workflow');
    await waitFor(() => expect(legacy.listWorkflowDrafts).toHaveBeenCalled());
    expect(capability.bootstrapCapabilities).not.toHaveBeenCalled();
  });

  it('does not fall back to the runtime path when the Capability API fails', async () => {
    // R12 / Property 5: an automatic fallback would mask a broken control plane
    // behind the very latency this migration removed. Rollback is an operator
    // flipping the flag.
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'registry' },
      registryConfigured: true,
    });
    capability.bootstrapCapabilities.mockRejectedValue(
      new Error('capability bootstrap 503'),
    );
    renderProbe();
    await waitFor(() =>
      expect(capability.bootstrapCapabilities).toHaveBeenCalled(),
    );
    expect(legacy.listSkillDrafts).not.toHaveBeenCalled();
  });

  it('surfaces a partial-source failure rather than presenting a full inventory', async () => {
    capability.fetchCapabilityConfig.mockResolvedValue({
      deployEnv: 'sandbox',
      capabilitySources: { skill: 'registry' },
      registryConfigured: true,
    });
    capability.bootstrapCapabilities.mockResolvedValue({
      skill: {
        category: 'skill',
        items: [],
        partialErrors: [{ source: 'agent-registry', error: 'timeout' }],
      },
    });

    function ErrorProbe() {
      const { ensureCategoryLoaded, categoryErrors } = useLabContext();
      React.useEffect(() => {
        void ensureCategoryLoaded('skill');
      }, [ensureCategoryLoaded]);
      return <div data-testid="error">{categoryErrors.skill ?? ''}</div>;
    }

    const { getByTestId } = render(
      <LabContextProvider>
        <ErrorProbe />
      </LabContextProvider>,
    );
    await waitFor(() =>
      expect(getByTestId('error')).toHaveTextContent('agent-registry (timeout)'),
    );
  });

  it('treats an unreachable config endpoint as legacy', async () => {
    capability.fetchCapabilityConfig.mockRejectedValue(new Error('offline'));
    renderProbe();
    await waitFor(() => expect(legacy.listSkillDrafts).toHaveBeenCalled());
    expect(capability.bootstrapCapabilities).not.toHaveBeenCalled();
  });
});
