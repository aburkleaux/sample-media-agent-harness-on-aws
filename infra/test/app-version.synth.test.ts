/**
 * The release a deploy is (`-c appVersion=`, from VERSION via scripts/release/version.sh) is stamped where an
 * operator looks, and nowhere it would churn every resource. Run: `npm run test:app-version-synth`.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig } from "../lib/env-config";

const PROJECT = "mediaworks-harness";
const VERSION = "0.9.0-rc.870+abc1234";

const app = new cdk.App({ context: { env: "staging", projectName: PROJECT } });
const envConfig = resolveEnvConfig(app);
const stack = new AgentInfraStack(app, `${PROJECT}-infra-staging`, {
  projectName: PROJECT, envConfig, appVersion: VERSION,
  env: { account: envConfig.account, region: envConfig.region },
});
const t = Template.fromStack(stack);
const json = JSON.stringify(t.toJSON());

assert.equal(t.toJSON().Outputs.AppVersion.Value, VERSION, "an AppVersion output");
const secret = Object.values(t.findResources("AWS::SecretsManager::Secret"))
  .find((r: any) => JSON.stringify(r).includes("RUNTIME_ARN_PARAM_PATH"));
assert.ok(secret && JSON.stringify(secret).includes("APP_VERSION") && JSON.stringify(secret).includes(VERSION),
  "RuntimeConfigSecret carries APP_VERSION (runtimes read it; runtime env is capped at 1536 bytes)");
const envOf = (c: any) => Object.fromEntries((c.Environment ?? []).map((e: any) => [e.Name, e.Value]));
const containers = Object.values(t.findResources("AWS::ECS::TaskDefinition"))
  .flatMap((d: any) => d.Properties.ContainerDefinitions);
assert.ok(containers.length >= 2, "proxy + capability api task defs");
for (const c of containers) assert.equal(envOf(c).APP_VERSION, VERSION, `${c.Name}: APP_VERSION in task env`);
// Not a tag: a tag propagates to every resource, so each deploy would rewrite all of them.
assert.ok(!/"Key":"Version"/.test(json), "no Version resource tag");

const unversioned = new AgentInfraStack(new cdk.App({ context: { env: "staging", projectName: PROJECT } }),
  `${PROJECT}-infra-staging`, { projectName: PROJECT, envConfig, env: { account: envConfig.account, region: envConfig.region } });
assert.equal(Template.fromStack(unversioned).toJSON().Outputs.AppVersion.Value, "dev", "absent -> dev");
console.log("app-version synth: ok");
