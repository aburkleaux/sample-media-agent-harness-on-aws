/**
 * Synth assertions for the two job/gateway latency fixes. Run: `npm run test:job-latency-synth`
 * (node:assert, no framework — like the other infra synth tests).
 *
 *  - `JobSfnCompletionRule`: Step Functions terminal status changes for this account's state
 *    machines invoke the SAME polling-worker Lambda the schedule does (one code path), while the
 *    1-minute schedule stays as the fallback. Staging narrows by the M2C name prefix; prod is
 *    account-wide (always correct, one ignored GetItem per unrelated execution).
 *  - The gateway interceptor (on every user-MCP gateway call) is no longer at the 128 MB default.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";

const PROJECT = "mediaworks-harness";

function build(env: EnvName): { t: Template; account: string; region: string } {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const envConfig = resolveEnvConfig(app);
  const stack = new AgentInfraStack(app, `${PROJECT}-infra-${env}`, {
    projectName: PROJECT,
    envConfig,
    env: { account: envConfig.account, region: envConfig.region },
  });
  return { t: Template.fromStack(stack), account: envConfig.account!, region: envConfig.region! };
}

function onlyResource(t: Template, type: string, logicalPrefix: string): [string, any] {
  const hits = Object.entries(t.findResources(type)).filter(([id]) => id.startsWith(logicalPrefix));
  assert.equal(hits.length, 1, `exactly one ${type} ${logicalPrefix}*; got ${hits.map(([id]) => id)}`);
  return hits[0] as [string, any];
}

for (const [env, namePrefix] of [["staging", "m2cv6-0e0000000002-"], ["prod", ""]] as const) {
  const { t, account, region } = build(env);
  const [workerId] = onlyResource(t, "AWS::Lambda::Function", "JobPollingWorker");
  const [ruleId, rule] = onlyResource(t, "AWS::Events::Rule", "JobSfnCompletionRule");
  const p = rule.Properties;

  assert.deepEqual(p.EventPattern.source, ["aws.states"], `${env}: source`);
  assert.deepEqual(p.EventPattern["detail-type"], ["Step Functions Execution Status Change"], `${env}: detail-type`);
  assert.deepEqual(
    [...p.EventPattern.detail.status].sort(),
    ["ABORTED", "FAILED", "SUCCEEDED", "TIMED_OUT"],
    `${env}: terminal statuses only (RUNNING would be a wasted invoke)`,
  );
  assert.deepEqual(
    p.EventPattern.detail.stateMachineArn,
    [{ prefix: `arn:aws:states:${region}:${account}:stateMachine:${namePrefix}` }],
    `${env}: scoped to this account's state machines`,
  );

  // Same function as the schedule, not a second copy of the completion logic.
  assert.equal(p.Targets.length, 1, `${env}: one target`);
  assert.deepEqual(p.Targets[0].Arn, { "Fn::GetAtt": [workerId, "Arn"] }, `${env}: targets the polling worker`);
  assert.equal(p.Targets[0].RetryPolicy.MaximumEventAgeInSeconds, 300, `${env}: short event age`);
  assert.equal(p.Targets[0].RetryPolicy.MaximumRetryAttempts, 2, `${env}: bounded retries`);

  const [, schedule] = onlyResource(t, "AWS::Events::Rule", "JobPollingSchedule");
  assert.equal(schedule.Properties.ScheduleExpression, "rate(1 minute)", `${env}: the poll fallback stays`);
  assert.deepEqual(schedule.Properties.Targets[0].Arn, { "Fn::GetAtt": [workerId, "Arn"] });

  // EventBridge may invoke the worker for this rule.
  const perms = Object.values(t.findResources("AWS::Lambda::Permission")).filter(
    (r: any) =>
      r.Properties.Principal === "events.amazonaws.com" &&
      JSON.stringify(r.Properties.FunctionName).includes(workerId) &&
      JSON.stringify(r.Properties.SourceArn ?? "").includes(ruleId),
  );
  assert.equal(perms.length, 1, `${env}: invoke permission for the completion rule`);

  const [, interceptor] = onlyResource(t, "AWS::Lambda::Function", "GatewayInterceptorFn");
  assert.equal(interceptor.Properties.MemorySize, 1024, `${env}: interceptor memory`);
}

console.log("job-latency synth: OK (staging, prod)");
