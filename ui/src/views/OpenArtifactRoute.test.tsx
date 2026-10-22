/**
 * @vitest-environment jsdom
 *
 * `/open?key=artifacts/...`: a valid key resolves to a presigned URL and replaces the location; a malformed key
 * makes no request and shows a themed error; an unresolvable key shows the no-access message. `main.tsx` mounts it
 * under AuthGate (sign-in returns to the same URL via `signinRedirect(returnTo)`), pinned in `pickRoot` below by
 * reading main.tsx's source.
 */
import '@testing-library/jest-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const mocks = vi.hoisted(() => ({ resolveArtifactUrls: vi.fn() }));
vi.mock('../artifacts/artifactUrls', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../artifacts/artifactUrls')>()),
  resolveArtifactUrls: mocks.resolveArtifactUrls,
}));

import OpenArtifactRoute, {
  OPEN_INVALID_MESSAGE,
  OPEN_NO_ACCESS_MESSAGE,
  artifactKeyFromSearch,
} from './OpenArtifactRoute';
import { EMOJI_OR_GLYPH } from '../test-utils/glyphs';

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe('OpenArtifactRoute', () => {
  it('resolves the key and replaces the location with the presigned URL', async () => {
    const key = 'artifacts/S1/output/final cut.mp4';
    mocks.resolveArtifactUrls.mockResolvedValue(new Map([[key, { url: 'https://s3/presigned', expiresAt: 1 }]]));
    const replace = vi.fn();
    render(<OpenArtifactRoute search={`?key=${encodeURIComponent(key)}`} replace={replace} />);
    expect(screen.getByRole('status')).toHaveTextContent('Opening final cut.mp4');
    await waitFor(() => expect(replace).toHaveBeenCalledWith('https://s3/presigned'));
    expect(mocks.resolveArtifactUrls).toHaveBeenCalledWith([key]);
  });

  it('shows the no-access message when the key does not resolve', async () => {
    mocks.resolveArtifactUrls.mockResolvedValue(new Map());
    const replace = vi.fn();
    render(<OpenArtifactRoute search="?key=artifacts/S2/input/x.png" replace={replace} />);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(OPEN_NO_ACCESS_MESSAGE));
    expect(OPEN_NO_ACCESS_MESSAGE).toBe('You do not have access to this file or it no longer exists');
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByTestId('open-artifact').textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
    expect(screen.getByRole('alert').querySelector('svg[aria-hidden="true"]')).not.toBeNull();
  });

  it.each([
    [''],
    ['?key='],
    ['?key=projects/p1/a/x.mp4'],
    ['?key=artifacts/../secrets'],
    ['?key=artifacts/a%5Cb'],
    ['?other=artifacts/x'],
  ])('rejects %s without a request', async (search) => {
    const replace = vi.fn();
    render(<OpenArtifactRoute search={search} replace={replace} />);
    expect(screen.getByRole('alert')).toHaveTextContent(OPEN_INVALID_MESSAGE);
    expect(mocks.resolveArtifactUrls).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it('parses only an artifacts/ key', () => {
    expect(artifactKeyFromSearch('?key=artifacts%2FS1%2Fa.png')).toBe('artifacts/S1/a.png');
    expect(artifactKeyFromSearch('?key=other/a.png')).toBeNull();
  });

  it('is routed at /open inside AuthGate', () => {
    const main = readFileSync(resolve(__dirname, '../main.tsx'), 'utf8');
    expect(main).toMatch(/pathname === "\/open"\)\s*\{\s*return <AuthGate>\{\(\) => <OpenArtifactRoute \/>\}<\/AuthGate>;/);
  });
});
