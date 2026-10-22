/**
 * The client's action→host split must match the server's.
 *
 * `capability-actions.ts` duplicates a list that lives in
 * `agent/src/capability_api/lab_actions.py`, across a language boundary where no
 * compiler can help. Both directions of a mismatch are silent in different ways:
 *
 * - An action the client thinks is control-plane but the server refuses becomes a
 *   400 on a CRUD call that used to work — a broken Save, blamed on the write.
 * - An action the server would serve but the client keeps on `/lab` just pays a
 *   cold start forever, and nothing ever says so.
 *
 * So the test parses the Python rather than restating it, the same way
 * `capability-api.routing.contract.test.ts` parses the CDK stack instead of
 * duplicating its behaviour list.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CONTROL_PLANE_ACTIONS, isControlPlaneAction } from './capability-actions';

const here = dirname(fileURLToPath(import.meta.url));

function pythonSource(): string {
  return readFileSync(
    resolve(here, '../../agent/src/capability_api/lab_actions.py'),
    'utf8',
  );
}

/** Names inside a `frozenset({...})` or `dict{...}` literal, comments stripped. */
function namesIn(block: string): Set<string> {
  // Comments in these blocks explain the omissions and therefore *name* actions
  // that are deliberately absent — matching raw source would pick those up.
  const code = block.replace(/#.*$/gm, '');
  return new Set([...code.matchAll(/"([A-Za-z]+)"/g)].map((m) => m[1]));
}

function serverControlPlaneActions(): Set<string> {
  const src = pythonSource();
  const match = src.match(
    /CONTROL_PLANE_ACTIONS: frozenset\[str\] = frozenset\(\s*\{([\s\S]*?)\n\s*\}\s*\)/,
  );
  if (!match) throw new Error('could not locate CONTROL_PLANE_ACTIONS in lab_actions.py');
  return namesIn(match[1]);
}

function serverRuntimeOnlyActions(): Set<string> {
  const src = pythonSource();
  const match = src.match(/RUNTIME_ONLY_ACTIONS: dict\[str, str\] = \{([\s\S]*?)\n\}/);
  if (!match) throw new Error('could not locate RUNTIME_ONLY_ACTIONS in lab_actions.py');
  // Keys only — the reason strings contain prose, not action names, but they do
  // contain capitalised words that the name pattern would otherwise match.
  return new Set([...match[1].matchAll(/^\s{4}"([A-Za-z]+)":/gm)].map((m) => m[1]));
}

describe('client and server agree on which host serves which lab action', () => {
  it('serves exactly the actions the Capability API accepts', () => {
    const server = serverControlPlaneActions();
    const client = new Set(CONTROL_PLANE_ACTIONS);

    const clientOnly = [...client].filter((a) => !server.has(a)).sort();
    const serverOnly = [...server].filter((a) => !client.has(a)).sort();

    expect(
      { clientOnly, serverOnly },
      'capability-actions.ts and lab_actions.py disagree; clientOnly actions will ' +
        'be refused with a 400, serverOnly actions keep paying a cold start',
    ).toEqual({ clientOnly: [], serverOnly: [] });
  });

  it('keeps every runtime-only action off the control plane', () => {
    for (const action of serverRuntimeOnlyActions()) {
      expect(isControlPlaneAction(action), `${action} must stay on /lab`).toBe(false);
    }
  });

  it('classifies the two tool-collection writes as runtime-only', () => {
    // Called out explicitly because they are the ones a reader "corrects": their
    // siblings on the same category are control-plane, and the dependency that
    // separates them (ref validation against the gateway) is not visible here.
    expect(isControlPlaneAction('CreateToolCollectionDraft')).toBe(false);
    expect(isControlPlaneAction('UpdateToolCollectionDraft')).toBe(false);
    expect(isControlPlaneAction('ReadToolCollectionDraft')).toBe(true);
    expect(isControlPlaneAction('DeleteToolCollectionDraft')).toBe(true);
  });

  it('fails closed on an unknown action', () => {
    expect(isControlPlaneAction('NoSuchAction')).toBe(false);
    expect(isControlPlaneAction('')).toBe(false);
  });
});
