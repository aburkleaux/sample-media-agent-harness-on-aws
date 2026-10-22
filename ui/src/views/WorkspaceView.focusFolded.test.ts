// A re-render of a file already in a tab folds into THAT tab without adding one, so "show me v4" again changed
// nothing on screen (staging a5b5acaf, four tries). The tab holding the just-announced manifest is selected.
import { describe, it, expect } from 'vitest';
import { tabIndexHoldingManifest } from './WorkspaceView';
import type { RenderHistoryEntry } from '../components/renderer/types';

const solo = (key: string): RenderHistoryEntry => ({ turnIndex: 0, timestamp: 1, key,
  blocks: [{ template: 'manifest-router', title: 't', data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(key)}` } }] });
const composite = (key: string, members: string[]): RenderHistoryEntry => ({ turnIndex: 0, timestamp: 1, key,
  blocks: [{ template: 'manifest-router', title: 't', data: { compositeKey: key, memberKeys: members } }] });

describe('tabIndexHoldingManifest', () => {
  const tabs = [solo('artifacts/s/render/a.json'), composite('composite:loc:s3://b/voices/v4/preview.wav',
    ['artifacts/s/render/6795.json', 'artifacts/s/render/5452.json']), solo('artifacts/s/render/0e88.json')];

  it('finds a solo tab by its key and a folded render by the composite that absorbed it', () => {
    expect(tabIndexHoldingManifest(tabs, 'artifacts/s/render/0e88.json')).toBe(2);
    expect(tabIndexHoldingManifest(tabs, 'artifacts/s/render/5452.json')).toBe(1);
    expect(tabIndexHoldingManifest(tabs, 'artifacts/s/render/nope.json')).toBe(-1);
  });
});
