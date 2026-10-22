import { describe, expect, it } from 'vitest';
import { memoryEventsToChatMessages, type MemoryEvent } from './lab-api';

// A user turn and its assistant reply, written as separate AgentCore events.
// AgentCore ListEvents returns newest-first; the chat must show oldest-first.
const userEvent: MemoryEvent = {
  eventId: 'e-user',
  sessionId: 's1',
  actorId: 'a1',
  eventTimestamp: '2026-07-30T17:05:00.000Z',
  payload: [{ conversational: { role: 'USER', content: { text: 'Run face recognition on the clip.' } } }],
};

const assistantEvent: MemoryEvent = {
  eventId: 'e-assistant',
  sessionId: 's1',
  actorId: 'a1',
  eventTimestamp: '2026-07-30T17:06:00.000Z',
  payload: [{ conversational: { role: 'ASSISTANT', content: { text: 'The job succeeded and the results have been rendered.' } } }],
};

describe('memoryEventsToChatMessages ordering', () => {
  it('renders oldest-first when the API returns newest-first', () => {
    // Newest-first, as AgentCore ListEvents actually returns them.
    const msgs = memoryEventsToChatMessages([assistantEvent, userEvent]);
    expect(msgs.map(m => m.role)).toEqual(['user', 'assistant']);
    expect(msgs[0].content).toMatch(/Run face recognition/);
    expect(msgs[1].content).toMatch(/results have been rendered/);
  });

  it('is order-independent — same result when the API returns oldest-first', () => {
    const msgs = memoryEventsToChatMessages([userEvent, assistantEvent]);
    expect(msgs.map(m => m.role)).toEqual(['user', 'assistant']);
  });

  it('sorts a multi-turn transcript chronologically', () => {
    const t = (iso: string, role: string, text: string): MemoryEvent => ({
      eventId: 'e-' + iso, sessionId: 's1', actorId: 'a1', eventTimestamp: iso,
      payload: [{ conversational: { role, content: { text } } }],
    });
    const scrambled = [
      t('2026-07-30T17:06:00.000Z', 'ASSISTANT', 'reply-1'),
      t('2026-07-30T17:05:00.000Z', 'USER', 'ask-1'),
      t('2026-07-30T17:08:00.000Z', 'ASSISTANT', 'reply-2'),
      t('2026-07-30T17:07:00.000Z', 'USER', 'ask-2'),
    ];
    expect(memoryEventsToChatMessages(scrambled).map(m => m.content))
      .toEqual(['ask-1', 'reply-1', 'ask-2', 'reply-2']);
  });

  it('preserves intra-event turn order for events sharing a timestamp', () => {
    const combined: MemoryEvent = {
      eventId: 'e-combined', sessionId: 's1', actorId: 'a1',
      eventTimestamp: '2026-07-30T17:05:00.000Z',
      payload: [
        { conversational: { role: 'USER', content: { text: 'question' } } },
        { conversational: { role: 'ASSISTANT', content: { text: 'answer' } } },
      ],
    };
    expect(memoryEventsToChatMessages([combined]).map(m => m.content)).toEqual(['question', 'answer']);
  });

  const t = (iso: string, role: string, text: string): MemoryEvent => ({
    eventId: 'e-' + iso, sessionId: 's1', actorId: 'a1', eventTimestamp: iso,
    payload: [{ conversational: { role, content: { text } } }],
  });

  it('coalesces consecutive assistant events into ONE turn (no fragmented mem-* chunks)', () => {
    // One logical turn the SDK split into 3 assistant events (pre-tool, mid, post) — the
    // "turn 0 split into many chunks" fragmentation (session cd95cd42) in the Memory fallback.
    const events = [
      t('2026-07-30T17:05:00.000Z', 'USER', 'generate an image of a fox'),
      t('2026-07-30T17:05:02.000Z', 'ASSISTANT', "I'll activate the media skill."),
      t('2026-07-30T17:05:04.000Z', 'ASSISTANT', 'This is a text-to-image request.'),
      t('2026-07-30T17:05:06.000Z', 'ASSISTANT', "Here's your fox."),
    ];
    const msgs = memoryEventsToChatMessages(events);
    expect(msgs.map(m => m.role)).toEqual(['user', 'assistant']); // ONE assistant turn, not three
    expect(msgs[1].content).toContain("I'll activate the media skill.");
    expect(msgs[1].content).toContain('This is a text-to-image request.');
    expect(msgs[1].content).toContain("Here's your fox.");
  });

  it('does NOT coalesce assistant turns separated by a user turn', () => {
    const events = [
      t('2026-07-30T17:05:00.000Z', 'USER', 'q1'),
      t('2026-07-30T17:05:02.000Z', 'ASSISTANT', 'a1'),
      t('2026-07-30T17:05:04.000Z', 'USER', 'q2'),
      t('2026-07-30T17:05:06.000Z', 'ASSISTANT', 'a2'),
    ];
    expect(memoryEventsToChatMessages(events).map(m => m.content)).toEqual(['q1', 'a1', 'q2', 'a2']);
  });
});
