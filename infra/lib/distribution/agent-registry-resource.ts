/**
 * Agent Registry provisioner for the self-contained wrapper.
 *
 * The AWS Agent Registry has no CloudFormation resource type (AGENTS.md: "the ONE out-of-band
 * object"), so a wrapper that must stand itself up from a single template cannot create it with
 * a native resource. This construct offers both supported shapes:
 *
 *   - PASSTHROUGH — `existingRegistryId` is supplied (a pre-provisioned registry, e.g. from
 *     `agent/scripts/registry_provision.py`, as staging/sandbox pin today). No resource is
 *     created; `registryId` is that value. Zero risk; use when the registry is managed
 *     out-of-band.
 *   - PROVISION — no id supplied. A CloudFormation custom resource (Lambda) creates-if-absent
 *     by name and RETAINS on delete. Makes the wrapper self-contained.
 *
 * Either way the construct exposes a single `registryId` the rest of the wrapper consumes.
 */
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as cr from "aws-cdk-lib/custom-resources";

export interface AgentRegistryProviderProps {
  /** Registry name to create-if-absent (idempotent). */
  registryName: string;
  /**
   * Pre-provisioned registry id. When set, this construct is a PASSTHROUGH — no custom
   * resource is created and {@link registryId} returns this value.
   */
  existingRegistryId?: string;
}

export class AgentRegistryProvider extends Construct {
  /** The registry id — a literal (passthrough) or a custom-resource attribute (provision). */
  public readonly registryId: string;
  /** True when a custom resource provisions the registry (vs a passthrough id). */
  public readonly provisioned: boolean;

  constructor(scope: Construct, id: string, props: AgentRegistryProviderProps) {
    super(scope, id);

    if (props.existingRegistryId) {
      this.registryId = props.existingRegistryId;
      this.provisioned = false;
      return;
    }

    const assetDir = path.resolve(__dirname, "../../.build/agent-registry-provider");
    fs.mkdirSync(assetDir, { recursive: true });
    fs.copyFileSync(path.join(__dirname, "agent-registry-handler/index.py"), path.join(assetDir, "index.py"));
    fs.cpSync(path.resolve(__dirname, "../../../agent/src/capability/botocore_models"),
      path.join(assetDir, "botocore_models"), { recursive: true });
    const onEvent = new lambda.Function(this, "OnEvent", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.on_event",
      code: lambda.Code.fromAsset(assetDir),
      environment: { AWS_DATA_PATH: "/var/task/botocore_models" },
      timeout: cdk.Duration.minutes(5),
      description:
        "Create-if-absent the Agent Registry (out-of-band object; retained on delete).",
    });
    // Registry ARNs aren't known before creation; scope to "*". Tighten to the created
    // registry ARN on Update once the id is known if least-privilege review requires it.
    onEvent.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "agent-registry:CreateRegistry",
          "agent-registry:GetRegistry",
          "agent-registry:ListRegistries",
        ],
        resources: ["*"],
      }),
    );

    // Fresh accounts do not yet have the registry service-linked role.
    onEvent.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:CreateServiceLinkedRole"],
      resources: [cdk.Stack.of(this).formatArn({
        service: "iam", region: "", resource: "role",
        resourceName: "aws-service-role/agent-registry.amazonaws.com/*",
      })],
      conditions: { StringEquals: { "iam:AWSServiceName": "agent-registry.amazonaws.com" } },
    }));

    // Registry creation provisions an AgentCore workload identity on our behalf.
    // Get is also needed when AWS retries that asynchronous creation.
    onEvent.addToRolePolicy(new iam.PolicyStatement({
      actions: ["bedrock-agentcore:CreateWorkloadIdentity", "bedrock-agentcore:GetWorkloadIdentity"],
      resources: [cdk.Stack.of(this).formatArn({
        service: "bedrock-agentcore", resource: "workload-identity-directory", resourceName: "*",
      })],
    }));

    const provider = new cr.Provider(this, "Provider", { onEventHandler: onEvent });
    const resource = new cdk.CustomResource(this, "Resource", {
      serviceToken: provider.serviceToken,
      properties: { RegistryName: props.registryName },
    });

    this.registryId = resource.getAttString("RegistryId");
    this.provisioned = true;
  }
}
