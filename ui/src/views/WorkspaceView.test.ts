import { describe, expect, it } from 'vitest';
import { prependEphemeralSession, type WorkspaceSession } from './WorkspaceView';

const row = (id: string, title = 'New Session'): WorkspaceSession => ({
  id,
  title,
  timestamp: 1,
});

describe('prependEphemeralSession', () => {
  it('replaces the active unpersisted empty session', () => {
    const result = prependEphemeralSession(
      [row('empty'), row('saved', 'Saved chat')],
      'empty',
      row('next'),
      new Set(['saved']),
    );
    expect(result.map(session => session.id)).toEqual(['next', 'saved']);
  });

  it('retains the active session when it is persisted', () => {
    const result = prependEphemeralSession(
      [row('saved', 'Saved chat')],
      'saved',
      row('next'),
      new Set(['saved']),
    );
    expect(result.map(session => session.id)).toEqual(['next', 'saved']);
  });

  it('never duplicates the next session id', () => {
    const result = prependEphemeralSession(
      [row('next'), row('other', 'Other')],
      'other',
      row('next'),
      new Set(['other']),
    );
    expect(result.map(session => session.id)).toEqual(['next', 'other']);
  });
});
