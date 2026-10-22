/**
 * `/control-plane` must be registered everywhere it needs to be.
 *
 * CloudFront's `additionalBehaviors` map is an explicit allowlist. An unlisted
 * path falls through to the default behavior, is served `index.html` from the
 * SPA bucket with HTTP 200, and the caller sees valid-looking HTML instead of
 * JSON — which reads as "the API returned raw HTML", not as a routing bug. That
 * has already cost this repo debugging time on `/lab`, `/mcp`, and
 * `/agent-test`, and CloudFront updates take 10-15 minutes to confirm.
 *
 * The Capability API is also the FIRST path that bypasses `ui/server`: an ALB
 * listener rule forwards it straight to the service's target group. So the
 * usual four registrations become three — the client prefix, the ALB rule, and
 * the CloudFront behavior — and there is deliberately no `app.ts` route or
 * `authPaths` entry. This test asserts all three exist, and that the exact-path
 * entry is there alongside the wildcard (CloudFront patterns are exact: `/x/*`
 * does not match a bare `/x`).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

function stackSource(): string {
  return readFileSync(
    resolve(here, '../../infra/lib/agent-infra-stack.ts'),
    'utf8',
  );
}

/**
 * The stack with `//` comments stripped.
 *
 * The "must NOT contain" assertions below are about granted actions, and the
 * stack explains several of its omissions in prose ("Deliberately NO
 * `bedrock-agentcore:InvokeAgentRuntime`..."). Matching raw source would fail on
 * the comment that documents the very absence being asserted.
 */
function stackCode(): string {
  return stackSource().replace(/^\s*\/\/.*$/gm, '');
}

/** A named region of the stack's code, comments stripped. */
function codeBetween(startNeedle: string, endNeedle: string): string {
  const code = stackCode();
  const start = code.indexOf(startNeedle);
  const end = code.indexOf(endNeedle, start);
  if (start < 0 || end < 0) {
    throw new Error(`could not locate ${startNeedle} .. ${endNeedle} in the CDK stack`);
  }
  return code.slice(start, end);
}

function clientProtectedPrefixes(): string[] {
  const src = readFileSync(resolve(here, './auth/authFetch.ts'), 'utf8');
  const match = src.match(/const PROTECTED_PREFIXES:[^=]*=\s*\[([\s\S]*?)\];/);
  if (!match) throw new Error('could not locate PROTECTED_PREFIXES in authFetch.ts');
  return [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

function cloudFrontBehaviorPaths(): string[] {
  // The stack now defines MORE THAN ONE `additionalBehaviors` block (the UI CDN
  // plus the artifacts CDN's `projects/*` refusal behaviour), so a single
  // non-greedy match would lock onto whichever block appears first and miss the
  // UI behaviors entirely. Scan every block and collect the `uiApiBehavior`
  // paths across all of them — behaviors that use a different (inline) target,
  // like the artifacts `projects/*` deny, contribute nothing and are ignored.
  const src = stackSource();
  const blocks = [...src.matchAll(/additionalBehaviors:\s*\{([\s\S]*?)\n {6}\},/g)];
  if (blocks.length === 0) throw new Error('could not locate additionalBehaviors in the CDK stack');
  return blocks.flatMap((b) => [...b[1].matchAll(/"([^"]+)":\s*uiApiBehavior/g)].map((m) => m[1]));
}

describe('/control-plane routing registrations', () => {
  it('attaches the access token client-side', () => {
    expect(clientProtectedPrefixes()).toContain('/control-plane');
  });

  it('has both the exact and wildcard CloudFront behaviors', () => {
    const paths = cloudFrontBehaviorPaths();
    // Both are required: CloudFront path patterns are exact, so `/control-plane/*`
    // matches `/control-plane/x` but NOT a bare `/control-plane`.
    expect(paths).toContain('/control-plane');
    expect(paths).toContain('/control-plane/*');
  });

  it('found a non-empty behavior list', () => {
    // Guards against a regex that silently matches nothing and passes vacuously.
    expect(cloudFrontBehaviorPaths().length).toBeGreaterThan(5);
  });

  it('forwards /control-plane to the Capability API target group at the ALB', () => {
    const src = stackSource();
    const rule = src.match(
      /new elbv2\.ApplicationListenerRule\([\s\S]*?ControlPlaneListenerRule[\s\S]*?\}\);/,
    );
    expect(rule, 'ControlPlaneListenerRule is missing from the CDK stack').toBeTruthy();
    expect(rule![0]).toContain('/control-plane');
    expect(rule![0]).toContain('/control-plane/*');
    expect(rule![0]).toContain('capabilityApiTargetGroup');
  });

  it('health-checks the Capability API target group on /health', () => {
    // The route the FastAPI app exposes unauthenticated for exactly this. A
    // mismatch leaves the target permanently unhealthy and the ALB rule dead.
    const src = stackSource();
    const targetGroup = src.match(
      /CapabilityApiTargetGroup"[\s\S]*?\}\,?\s*\);/,
    );
    expect(targetGroup).toBeTruthy();
    expect(targetGroup![0]).toContain('path: "/health"');
  });

  it('does not add /control-plane to the proxy authPaths', () => {
    // Deliberate: the proxy never sees these requests. Adding it would imply the
    // proxy protects a path it does not serve, and would mislead the next person
    // reading either list.
    const proxy = readFileSync(resolve(here, '../server/app.ts'), 'utf8');
    const match = proxy.match(/const authPaths\s*=\s*\[([^\]]*)\]/);
    expect(match).toBeTruthy();
    expect(match![1]).not.toContain('control-plane');
  });
});

describe('Capability API service invariants', () => {
  it('never scales to zero', () => {
    // R6/R13: at least one healthy task at all times when enabled, including mid-deploy
    // (CDK's default minHealthyPercent of 50% would take the single task offline during a
    // rolling update). desiredCount is gated by `capabilityApiEnabled` — a TEMPORARY bootstrap
    // flag that is 0 ONLY while the capability-api image push is blocked (missing ECR repo +
    // GitLab-role ecr push permission). The ENABLED (default) path stays 1, and
    // minHealthyPercent 100 still prevents a mid-deploy dip. Drop the flag + restore a plain
    // `desiredCount: 1` once the image bootstrap lands.
    const src = stackSource();
    const service = src.match(/"CapabilityApiService"[\s\S]*?\}\,?\s*\);/);
    expect(service).toBeTruthy();
    expect(service![0]).toContain('desiredCount: capabilityApiEnabled ? 1 : 0');
    expect(service![0]).toContain('minHealthyPercent: 100');
  });

  it('does not grant the task role InvokeAgentRuntime', () => {
    // The absence is load-bearing (R1 / Property 5): a role that cannot call
    // the Runtime is a stronger guarantee than code that currently does not.
    const roleBlock = codeBetween('CapabilityApiTaskRole', 'const capabilityApiTaskDef');
    expect(roleBlock.length).toBeGreaterThan(200);
    // The fully-qualified action, which is the only form a grant can take. The
    // role's `description` string mentions the bare name on purpose, so that it
    // is visible in the IAM console why the role looks incomplete.
    expect(roleBlock).not.toContain('bedrock-agentcore:InvokeAgentRuntime');
  });

  it('grants only discovery reads on the Registry, no control-plane actions', () => {
    const roleBlock = codeBetween('CapabilityApiTaskRole', 'const capabilityApiTaskDef');
    for (const forbidden of [
      'agent-registry:CreateRegistryRecord',
      'agent-registry:UpdateRegistryRecord',
      'agent-registry:UpdateRegistryRecordStatus',
      'agent-registry:SubmitRegistryRecordForApproval',
      'agent-registry:DeleteRegistryRecord',
    ]) {
      expect(roleBlock, `read role must not hold ${forbidden}`).not.toContain(forbidden);
    }
    expect(roleBlock).toContain('agent-registry:ListDiscoverableRegistryRecords');
  });

  it('never grants the projector a delete action', () => {
    // Deprecating a revision is the only removal mechanism (R16). A delete grant
    // is the difference between a bug that hides a record and one that destroys
    // history.
    const roleBlock = codeBetween('RegistryProjectorRole', 'const registryProjector =');
    expect(roleBlock.length).toBeGreaterThan(200);
    expect(roleBlock).not.toContain('DeleteRegistryRecord');
    expect(roleBlock).not.toContain('DeleteRegistry"');
    expect(roleBlock).toContain('agent-registry:UpdateRegistryRecordStatus');
  });

  it('filters the projector stream on the outbox discriminator', () => {
    // Must match OUTBOX_RECORD_TYPE in agent/src/lab/outbox.py. A mismatch stops
    // projection silently — no error, just no events.
    const src = stackSource();
    const eventSource = src.match(
      /registryProjector\.addEventSource\([\s\S]*?\n {4}\);/,
    );
    expect(eventSource).toBeTruthy();
    expect(eventSource![0]).toContain('recordType');
    expect(eventSource![0]).toContain('"outbox"');
    expect(eventSource![0]).toContain('SqsDlq');
  });

  it('enables a NEW_IMAGE stream on the AssetIndex table', () => {
    const src = stackSource();
    const table = src.match(/new dynamodb\.Table\(this, "LabAssetIndexTable"[\s\S]*?\}\);/);
    expect(table).toBeTruthy();
    expect(table![0]).toContain('StreamViewType.NEW_IMAGE');
  });
});
