/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const m = vi.hoisted(() => ({ getImportStatus: vi.fn(), rescanImport: vi.fn() }));
vi.mock('../../projects/api', async (o) => ({ ...(await o<typeof import('../../projects/api')>()),
  getImportStatus: m.getImportStatus, rescanImport: m.rescanImport }));

import { ImportPanel } from './ImportPanel';

const PREFIX = 'projects/p1/import/';
async function flush() { await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); }); }

beforeEach(() => {
  vi.clearAllMocks();
  m.getImportStatus.mockResolvedValue({ ok: true, value: {
    bucket: 'ingest-bucket', prefix: PREFIX, indexing: true,
    counts: { registered: 312, kept: 9, attention: 7, ignored: 934 },
    attention: [
      { key: `${PREFIX}B-cam/B001_C004.R3D`, reason: 'not_supported', size: 2048 },
      { key: `${PREFIX}Day2/A007.MXF`, reason: 'too_large', size: 8 * 1024 ** 3 },
    ],
    statusCounts: { indexed: 214, queued: 80, analyzing: 18, removed: 3 },
  } });
  m.rescanImport.mockResolvedValue({ ok: true, value: { ingested: 2 } });
});
afterEach(cleanup);

describe('ImportPanel', () => {
  it('shows where to copy, how far indexing has got, and what needs attention with what to do', async () => {
    render(<ImportPanel projectId="p1" open onClose={vi.fn()} />);
    await flush();
    expect(screen.getByTestId('import-command')).toHaveTextContent(`aws s3 sync ./MyShoot s3://ingest-bucket/${PREFIX}`);
    expect(screen.getByTestId('import-indexed')).toHaveTextContent('214of 312 files'); // removed files don't count
    expect(screen.getByTestId('import-attention')).toHaveTextContent('7');
    const groups = screen.getAllByTestId('import-attention-group');
    expect(groups.map((g) => g.getAttribute('data-reason'))).toEqual(['not_supported', 'too_large']);
    expect(groups[0]).toHaveTextContent("Camera RAW can't be decoded here");
    expect(groups[0]).toHaveTextContent('B-cam/B001_C004.R3D');
  });

  it('Scan now sweeps the inbox at once and re-reads the status', async () => {
    render(<ImportPanel projectId="p1" open onClose={vi.fn()} />);
    await flush();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Scan now' }));
    await flush();
    expect(m.rescanImport).toHaveBeenCalledWith('p1');
    expect(m.getImportStatus).toHaveBeenCalledTimes(2);
  });
});
