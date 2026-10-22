// The timeline action summary rides on the NEXT turn the user sends. It must be built from the committed
// messages: a snapshot taken in a `setMessages(prev => …)` updater is filled lazily, so on a chat that already
// existed it was still empty at send time and the agent never heard about the user's frames / clips (staging
// 2026-10-18: "what did I just do?" answered from an old memory instead of the Frame just taken).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const src = readFileSync(join(__dirname, 'ChatCanvas.tsx'), 'utf8');

describe('ChatCanvas action context wiring', () => {
  it('builds the hidden action summary from the committed messages, not the lazy updater snapshot', () => {
    expect(src).toMatch(/withActionContext\(committedMessagesRef\.current,/);
    expect(src).not.toMatch(/withActionContext\(capturedBase,/);
    expect(src).toMatch(/committedMessagesRef\.current = messages;/);
  });
});
