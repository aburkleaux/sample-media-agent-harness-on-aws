import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import type { MediaWorkflowGatewayConfig } from "../env-config";

export interface ToolkitLookupProps {
  /** The toolkit stack — or, with `nestedLogicalId`, the parent stack that nests it. */
  readonly stackName: string;
  /** Logical id of the toolkit nested stack inside `stackName` (MediaToolkitStack's `Toolkit`). */
  readonly nestedLogicalId?: string;
  /**
   * Changes whenever the toolkit might have changed, so CloudFormation re-runs the lookup. For a
   * toolkit this app deploys: its version + parameters. For an external one: a per-synth nonce.
   */
  readonly revision: string;
}

/**
 * Reads a deployed media toolkit's wiring at deploy time — the harvest script, as a custom
 * resource. Seconds long (well inside the one-hour custom-resource limit). Returns no secret
 * material: the credentials secret is referenced by ARN only.
 */
export class ToolkitLookup extends Construct {
  /** The toolkit's values as deploy-time tokens, shaped like `EnvConfig.media`. */
  public readonly media: MediaWorkflowGatewayConfig;
  /** Comma-joined ingest-bucket reader role ARNs (empty string when the toolkit lists none). */
  public readonly ingestReadRoleArnsCsv: string;

  constructor(scope: Construct, id: string, props: ToolkitLookupProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);
    const fn = new lambda.Function(this, "OnEvent", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.on_event",
      code: lambda.Code.fromAsset(path.join(__dirname, "toolkit-lookup-handler"), { exclude: ["__pycache__", "*.pyc"] }),
      timeout: cdk.Duration.minutes(2),
      description: "Read the media toolkit stack's Outputs + non-secret credential fields at deploy time.",
    });
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["cloudformation:DescribeStacks", "cloudformation:DescribeStackResource"],
      resources: [stack.formatArn({ service: "cloudformation", resource: "stack", resourceName: "*" })],
    }));
    // The toolkit names its bundle `/{GatewayName}/{Environment}/gateway-credentials`.
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["secretsmanager:GetSecretValue"],
      resources: [stack.formatArn({
        service: "secretsmanager", resource: "secret", resourceName: "*gateway-credentials*",
        arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
      })],
    }));
    // Only for a toolkit deployed with a customer-managed SecretsKmsKeyId; scoped to Secrets Manager.
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["kms:Decrypt"],
      resources: ["*"],
      conditions: { StringEquals: { "kms:ViaService": `secretsmanager.${stack.region}.amazonaws.com` } },
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["cognito-idp:DescribeUserPoolDomain"],
      resources: ["*"], // no resource-level permissions for this action
    }));

    const provider = new cr.Provider(this, "Provider", { onEventHandler: fn });
    const resource = new cdk.CustomResource(this, "Resource", {
      serviceToken: provider.serviceToken,
      resourceType: "Custom::MediaToolkitLookup",
      properties: {
        ToolkitStackName: props.stackName,
        NestedLogicalId: props.nestedLogicalId ?? "",
        Revision: props.revision,
      },
    });
    const att = (name: string) => resource.getAttString(name);
    this.media = {
      gatewayUrl: att("GatewayUrl"),
      clientId: att("ClientId"),
      tokenEndpoint: att("TokenEndpoint"),
      credentialsSecretArn: att("SecretArn"),
      oidcDiscoveryUrl: att("OidcDiscoveryUrl"),
      scope: att("Scope"),
      proxyBucket: att("ProxyBucket"),
    };
    this.ingestReadRoleArnsCsv = att("IngestReadRoleArns");
  }
}
