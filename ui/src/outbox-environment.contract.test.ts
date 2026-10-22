/**
 * Every process that can emit an outbox event must be stamped with DEPLOY_ENV.
 *
 * This is a deployment-shaped bug that no unit test on either side can catch,
 * because both sides are individually correct:
 *
 *   - `lab/outbox.py::deploy_env()` falls back to the literal `"unknown"` when
 *     DEPLOY_ENV is unset,
 *   - `capability/projector.py` skips any event whose `environment` does not
 *     equal its own — deliberately fail-closed, so a table restored into the
 *     wrong account cannot project across environments (Property 7).
 *
 * Put together with an unstamped runtime, an org-scoped publish writes a correct
 * canonical row, emits a correct-looking outbox row, and never reaches the
 * Registry. Nothing errors. The only evidence is a CloudWatch warning.
 *
 * Observed in sandbox on 2026-08-13: a publish landed at `ORG#mediaworks#SHARED`
 * with `environment: "unknown"` and the projection-state table stayed empty,
 * because DEPLOY_ENV was set only on the projector Lambda and the Capability API
 * — not on the agent runtimes that actually do the publishing.
 *
 * Asserted here rather than in `infra/` because that package has no test runner,
 * and this repo already reads the CDK as text for contract assertions (see
 * `capability-api.routing.contract.test.ts`).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

function stackCode(): string {
  return readFileSync(resolve(here, '../../infra/lib/agent-infra-stack.ts'), 'utf8')
    .replace(/^\s*\/\/.*$/gm, '');
}

/**
 * Every `environment`/`environmentVariables` object literal in the stack, as
 * source text.
 *
 * Brace-counting rather than a regex: these literals nest (ECS `secrets`,
 * inline objects), so a non-greedy match to the first `}` would truncate and a
 * greedy one would swallow the rest of the file.
 */
function envBlocks(): string[] {
  const code = stackCode();
  const blocks: string[] = [];
  const opener = /\b(environmentVariables|environment)\s*:\s*\{/g;
  let match: RegExpExecArray | null;
  while ((match = opener.exec(code)) !== null) {
    let depth = 1;
    let i = match.index + match[0].length;
    while (i < code.length && depth > 0) {
      if (code[i] === '{') depth += 1;
      else if (code[i] === '}') depth -= 1;
      i += 1;
    }
    blocks.push(code.slice(match.index, i));
  }
  return blocks;
}

/**
 * The `ui/server` proxy's env block.
 *
 * Matched on the EXACT key at line start, not a substring. It used to be `AGENT_RUNTIME_ARN`, but the
 * runtime ARNs now come from SSM at startup (docs/ci-speed-proposal.md P1), so the proxy is the block
 * that sets `SESSIONS_TABLE` — the only process that owns the chat sessions table.
 */
function isTheNodeProxy(block: string): boolean {
  return /^\s*SESSIONS_TABLE\s*:/m.test(block);
}

/**
 * A process that can MUTATE the asset index (and thus emit an outbox event).
 *
 * Historically identified by `ASSET_INDEX_TABLE` set inline. The agent runtimes
 * (task, main) now fetch that table — with the rest of their config — from the
 * RuntimeConfigSecret at startup (V2's 1536-byte env cap; see
 * `runtime_config_bootstrap.py`), so they carry `RUNTIME_CONFIG_SECRET_ARN`
 * instead of the inline table. Both forms are outbox writers and must still stamp
 * DEPLOY_ENV inline (DEPLOY_ENV stays in env precisely so `lab/outbox.py` reads it).
 * The projector Lambda / Capability API still set `ASSET_INDEX_TABLE` inline.
 */
function canWriteAssetIndex(block: string): boolean {
  return block.includes('ASSET_INDEX_TABLE') || block.includes('RUNTIME_CONFIG_SECRET_ARN');
}

describe('DEPLOY_ENV stamping on outbox writers', () => {
  it('finds the environment blocks at all (guards the parser itself)', () => {
    // If the stack is refactored so this heuristic stops matching, the tests
    // below would vacuously pass. Fail loudly instead.
    const blocks = envBlocks();
    expect(blocks.length).toBeGreaterThan(4);
    expect(blocks.some((b) => b.includes('ASSET_INDEX_TABLE'))).toBe(true);
    // The agent runtimes now source the asset-index table from the config secret.
    expect(blocks.some((b) => b.includes('RUNTIME_CONFIG_SECRET_ARN'))).toBe(true);
  });

  it('gives DEPLOY_ENV to every PYTHON environment that can write the asset index', () => {
    // ASSET_INDEX_TABLE marks "this process can mutate canonical state", and any
    // such mutation can ride an outbox row alongside it.
    //
    // Excluding `ui/server`: it is the one asset-index reader that is Node, not
    // Python, so it cannot import `lab/outbox.py` and cannot emit an event. It
    // reads the table to resolve external-invoke agent configs. Identified by
    // AGENT_RUNTIME_ARN — it is the process that *invokes* runtimes rather than
    // being one.
    const missing = envBlocks()
      .filter(canWriteAssetIndex)
      .filter((b) => !isTheNodeProxy(b))
      .filter((b) => !b.includes('DEPLOY_ENV'));

    expect(
      missing.length,
      `${missing.length} Python environment block(s) set ASSET_INDEX_TABLE ` +
        `without DEPLOY_ENV. An outbox event written there is stamped "unknown" ` +
        `and the projector drops it silently.`,
    ).toBe(0);
  });

  it('exempts exactly one block, and still checks the rest', () => {
    // Guards the exclusion itself. Expected in scope: Task Agent, Main Agent,
    // projector Lambda, Capability API. Exempt: the Node proxy, and only it.
    const withTable = envBlocks().filter(canWriteAssetIndex);
    const exempt = withTable.filter(isTheNodeProxy);
    expect(exempt).toHaveLength(1);
    // Task + Main (via RUNTIME_CONFIG_SECRET_ARN) + projector + Capability API (inline table).
    expect(withTable.length - exempt.length).toBeGreaterThanOrEqual(4);
  });

  it('covers the projector itself, which compares against its own value', () => {
    const projector = envBlocks().find((b) => b.includes('PROJECTION_STATE_TABLE'));
    expect(projector).toBeDefined();
    expect(projector).toContain('DEPLOY_ENV');
  });

  it('sets DEPLOY_ENV from the resolved env name, never a literal', () => {
    // A hardcoded "sandbox" would make staging and prod silently drop every
    // event — the same failure, one environment over.
    for (const block of envBlocks().filter((b) => b.includes('DEPLOY_ENV'))) {
      const assignment = /DEPLOY_ENV\s*:\s*([^,\n]+)/.exec(block);
      expect(assignment, `DEPLOY_ENV present but unparseable in: ${block.slice(0, 80)}`)
        .not.toBeNull();
      expect(assignment![1].trim()).toBe('envName');
    }
  });
});
