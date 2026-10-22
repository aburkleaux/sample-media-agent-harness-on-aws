/**
 * @vitest-environment jsdom
 *
 * Tests for AgentTestPanel — the Custom Agent test console.
 *
 * The panel's whole reason to exist is answering "what actually loaded?", so the
 * assertions focus on that being reported *truthfully*:
 *
 *   * a dropped tool collection is surfaced loudly (it's the usual cause of "my
 *     agent can't do the thing", and used to live only in a server log);
 *   * an unreadable gateway catalog reads as "unknown", never as "no tools" —
 *     those are materially different facts;
 *   * the run sends only a reference (agentId/source), never config, so the
 *     console can't diverge from a real invocation.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
// Matchers are not globally registered in vitest.config.ts, so import them
// here (as ui/src/components/nle/ClipManager.test.tsx does).
import '@testing-library/jest-dom';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import AgentTestPanel from './AgentTestPanel';

vi.mock('../lab-api', () => ({ resolveAgentConfig: vi.fn() }));
vi.mock('../api', () => ({ runAgentTestStreaming: vi.fn() }));

import { resolveAgentConfig } from '../lab-api';
import { runAgentTestStreaming } from '../api';

const baseConfig = {
  assetId: 'ag-1',
  name: 'MediaBot',
  source: 'published' as const,
  version: 2,
  modelId: 'us.anthropic.claude-sonnet-5',
  modelSource: 'pinned' as const,
  tools: ['media-processing___process_media', 'shot-segmentation___segment_shots'],
  skills: ['video-processing'],
  droppedCollections: [],
  toolCollections: [{ id: 'tc-1', name: 'Media' }],
  personaChars: 42,
  gatewayProjectionAvailable: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  (resolveAgentConfig as any).mockResolvedValue(baseConfig);
  (runAgentTestStreaming as any).mockResolvedValue(undefined);
});

describe('resolved config block', () => {
  it('reports the pinned model, tool count, and skill count', async () => {
    render(<AgentTestPanel assetId="ag-1" source="published" version={2} />);

    expect(await screen.findByText('us.anthropic.claude-sonnet-5')).toBeInTheDocument();
    expect(screen.getByText(/2 bound/)).toBeInTheDocument();
    expect(screen.getByText(/1 declared/)).toBeInTheDocument();
    expect(screen.getByText('42 characters')).toBeInTheDocument();
  });

  it('resolves against the requested source and version', async () => {
    render(<AgentTestPanel assetId="ag-1" source="published" version={2} />);
    await waitFor(() =>
      expect(resolveAgentConfig).toHaveBeenCalledWith({
        assetId: 'ag-1',
        source: 'published',
        version: 2,
      }),
    );
  });

  it('expands the exact gateway tool names on demand', async () => {
    render(<AgentTestPanel assetId="ag-1" source="draft" />);
    fireEvent.click(await screen.findByText(/2 bound/));

    expect(screen.getByText('media-processing___process_media')).toBeInTheDocument();
    expect(screen.getByText('shot-segmentation___segment_shots')).toBeInTheDocument();
  });

  it('says "platform default" when no model is pinned', async () => {
    (resolveAgentConfig as any).mockResolvedValue({
      ...baseConfig,
      modelId: null,
      modelSource: 'runtime-default',
    });
    render(<AgentTestPanel assetId="ag-1" source="draft" />);

    expect(await screen.findByText('platform default')).toBeInTheDocument();
    expect(screen.getByText('runtime default')).toBeInTheDocument();
  });

  it('warns loudly when a tool collection was dropped', async () => {
    (resolveAgentConfig as any).mockResolvedValue({
      ...baseConfig,
      tools: [],
      droppedCollections: ['tc-broken'],
    });
    render(<AgentTestPanel assetId="ag-1" source="draft" />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/1 tool collection.*dropped/i);
    expect(alert).toHaveTextContent('tc-broken');
  });

  it('distinguishes "catalog unavailable" from "no tools"', async () => {
    (resolveAgentConfig as any).mockResolvedValue({
      ...baseConfig,
      tools: [],
      gatewayProjectionAvailable: false,
    });
    render(<AgentTestPanel assetId="ag-1" source="draft" />);

    expect(await screen.findByText(/gateway catalog unavailable/i)).toBeInTheDocument();
    // Must NOT claim zero tools when it simply could not ask.
    expect(screen.queryByText(/0 bound/)).not.toBeInTheDocument();
  });

  it('surfaces a not_found result as a readable message', async () => {
    (resolveAgentConfig as any).mockResolvedValue({ error: 'not_found' });
    render(<AgentTestPanel assetId="ghost" source="published" />);
    expect(await screen.findByText('Agent not found.')).toBeInTheDocument();
  });

  it('surfaces a resolve failure', async () => {
    (resolveAgentConfig as any).mockRejectedValue(new Error('gateway exploded'));
    render(<AgentTestPanel assetId="ag-1" source="draft" />);
    expect(await screen.findByText('gateway exploded')).toBeInTheDocument();
  });
});

describe('live run', () => {
  it('is disabled until a prompt is entered', async () => {
    render(<AgentTestPanel assetId="ag-1" source="draft" />);
    const run = await screen.findByRole('button', { name: 'Run' });
    expect(run).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Test prompt'), { target: { value: 'hello' } });
    expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled();
  });

  it('sends only a reference — never config fields', async () => {
    render(<AgentTestPanel assetId="ag-1" source="published" version={2} />);
    fireEvent.change(await screen.findByLabelText('Test prompt'), { target: { value: 'list tools' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));

    await waitFor(() => expect(runAgentTestStreaming).toHaveBeenCalled());
    const [request] = (runAgentTestStreaming as any).mock.calls[0];
    expect(request).toEqual({
      agentId: 'ag-1',
      source: 'published',
      version: 2,
      prompt: 'list tools',
    });
    // The runtime resolves config itself; the client must not be able to steer it.
    for (const k of ['system_prompt', 'model_id', 'tools', 'skills', 'custom_agent']) {
      expect(request).not.toHaveProperty(k);
    }
  });

  it('streams steps, answer text, and the execution summary', async () => {
    (runAgentTestStreaming as any).mockImplementation(
      async (_req: any, onEvent: any) => {
        onEvent({ type: 'step', step: { type: 'step', status: 'done', label: 'process_media', duration_ms: 5393 } });
        onEvent({ type: 'text', text: 'Job ' });
        onEvent({ type: 'text', text: 'submitted.' });
        onEvent({
          type: 'execution_summary',
          summary: {
            type: 'execution_summary',
            total_duration_ms: 7222,
            cycles: 2,
            tools: { 'media-processing___process_media': { calls: 1, total_ms: 5393, avg_ms: 5393 } },
            usage: { totalTokens: 15451 },
            model: 'us.anthropic.claude-sonnet-5',
          },
        });
      },
    );

    render(<AgentTestPanel assetId="ag-1" source="draft" />);
    fireEvent.change(await screen.findByLabelText('Test prompt'), { target: { value: 'trim it' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));

    expect(await screen.findByText('process_media')).toBeInTheDocument();
    // Text chunks accumulate rather than replace.
    expect(await screen.findByText('Job submitted.')).toBeInTheDocument();
    expect(screen.getByText('2 cycles')).toBeInTheDocument();
    expect(screen.getByText('15451 tokens')).toBeInTheDocument();
    expect(screen.getByText(/called: media-processing___process_media/)).toBeInTheDocument();
  });

  it('dedups repeated steps sharing a node_id', async () => {
    (runAgentTestStreaming as any).mockImplementation(
      async (_req: any, onEvent: any) => {
        onEvent({ type: 'step', step: { type: 'step', status: 'running', label: 'poll', node_id: 'n1' } });
        onEvent({ type: 'step', step: { type: 'step', status: 'done', label: 'poll', node_id: 'n1' } });
      },
    );

    render(<AgentTestPanel assetId="ag-1" source="draft" />);
    fireEvent.change(await screen.findByLabelText('Test prompt'), { target: { value: 'go' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));

    await waitFor(() => expect(screen.getAllByText('poll')).toHaveLength(1));
  });

  it('shows an error when the run fails', async () => {
    (runAgentTestStreaming as any).mockRejectedValue(new Error('Agent test failed: 403 forbidden'));
    render(<AgentTestPanel assetId="ag-1" source="draft" />);
    fireEvent.change(await screen.findByLabelText('Test prompt'), { target: { value: 'go' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));

    expect(await screen.findByText(/403 forbidden/)).toBeInTheDocument();
  });
});
