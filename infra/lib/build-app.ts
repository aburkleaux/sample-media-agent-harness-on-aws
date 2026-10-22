import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { AgentInfraStack } from "./agent-infra-stack";
import { resolveEnvConfig } from "./env-config";
import { MediaToolkitStack, TOOLKIT_NESTED_LOGICAL_ID } from "./toolkit/media-toolkit-stack";
import { TOOLKIT_TEMPLATE_URLS, TOOLKIT_VERSION } from "./toolkit/toolkit-release";

/** The CDK app's stacks — the harness, plus the media toolkit underneath when `toolkit.deploy`. */
export function buildApp(app: cdk.App): { harness: AgentInfraStack; toolkitStack?: MediaToolkitStack } {
  const projectName = app.node.tryGetContext("projectName") || "mediaworks-harness";
  const envConfig = resolveEnvConfig(app);
  const env = {
    account: envConfig.account || process.env.CDK_DEFAULT_ACCOUNT,
    region: envConfig.region || process.env.CDK_DEFAULT_REGION,
  };

  // Preserve the original dev stack name so adopting multi-env does not orphan
  // the already-deployed dev stack (a CDK rename = CFN destroy + recreate).
  // staging/prod get env-suffixed stack names.
  const stackName =
    envConfig.env === "dev"
      ? `${projectName}-infra`
      : `${projectName}-infra-${envConfig.env}`;

  // The release: CI passes `-c appVersion=` (scripts/release/version.sh); a deploy without it — a customer
  // install from the shared package — reports the repo's VERSION. Also in the stack description, the first
  // thing `describe-stacks` shows.
  const versionFile = path.resolve(__dirname, "../../VERSION");
  const appVersion = String(
    app.node.tryGetContext("appVersion") ||
      (fs.existsSync(versionFile) ? fs.readFileSync(versionFile, "utf8").trim() : "") ||
      "dev",
  );

  // The media toolkit underneath (`toolkit.deploy`): its own stack, deployed first by
  // `cdk deploy --all`; the harness reads its nested toolkit's Outputs at deploy time.
  const toolkit = envConfig.toolkit;
  const toolkitStack = toolkit?.deploy
    ? new MediaToolkitStack(app, `${projectName}-media-toolkit`, {
        projectName,
        toolkitParameters: toolkit.parameters,
        env,
      })
    : undefined;

  const harness = new AgentInfraStack(app, stackName, {
    projectName,
    envConfig,
    appVersion,
    description: `Media Agent Harness (${envConfig.env}) - version ${appVersion}`,
    codeInterpreterIdParam:
      app.node.tryGetContext("codeInterpreterId") || process.env.CODE_INTERPRETER_ID || "",
    toolkitLookup: toolkitStack
      ? {
          stackName: toolkitStack.stackName,
          nestedLogicalId: TOOLKIT_NESTED_LOGICAL_ID,
          // Re-read whenever the toolkit release or its parameters change.
          revision: JSON.stringify({ TOOLKIT_VERSION, urls: TOOLKIT_TEMPLATE_URLS, params: toolkit?.parameters ?? {} }),
        }
      : toolkit?.stackName
        ? {
            stackName: toolkit.stackName,
            // An external toolkit can change without this app knowing: re-read on every deploy.
            revision: app.node.tryGetContext("toolkitLookupNonce") ?? new Date().toISOString(),
          }
        : undefined,
    env,
  });
  if (toolkitStack) harness.addDependency(toolkitStack, "reads the toolkit's Outputs at deploy time");
  return { harness, toolkitStack };
}
