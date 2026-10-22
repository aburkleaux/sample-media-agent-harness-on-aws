import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";

/** Logical id of the toolkit's hybrid-search indexer role (inside its nested `HybridSearchStack`). */
export const HYBRID_SEARCH_ROLE_LOGICAL_ID = "HybridSearchLambdaRole";
/** The bucket-policy statement that lets that role read image Search_Renditions — same Sid in every env. */
export const RENDITION_READ_SID = "AllowM2CHybridSearchReadRenditions";

export interface HybridSearchRoleLookupProps {
  /** The toolkit stack (name or ARN) — or, with `nestedLogicalId`, the parent stack that nests it. */
  readonly stackName: string;
  /** Logical id of the toolkit nested stack inside `stackName`. */
  readonly nestedLogicalId?: string;
  /**
   * Literal name the toolkit stack's name starts with; nested stacks are named `<parent>-<id>-<suffix>`,
   * so `stack/<prefix>/*` + `stack/<prefix>-*` bounds what the lookup may describe.
   */
  readonly stackNamePrefix: string;
  /** Changes whenever the toolkit might have changed, so CloudFormation re-runs the lookup. */
  readonly revision: string;
}

/**
 * Resolves the media toolkit's hybrid-search indexer role to its exact ARN at deploy time. The
 * toolkit publishes no Output for it, and the role is created deep inside a nested stack under a
 * generated name and a `/umt-<id>/` path, so it cannot be known at synth. Fails the deploy if the role
 * cannot be found (see the handler) rather than leaving image assets silently unindexable.
 */
export class HybridSearchRoleLookup extends Construct {
  public readonly roleArn: string;

  constructor(scope: Construct, id: string, props: HybridSearchRoleLookupProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);
    const fn = new lambda.Function(this, "OnEvent", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.on_event",
      code: lambda.Code.fromAsset(path.join(__dirname, "hybrid-search-role-handler"), { exclude: ["__pycache__", "*.pyc"] }),
      timeout: cdk.Duration.minutes(2),
      description: "Resolve the media toolkit's hybrid-search indexer role ARN at deploy time.",
    });
    // Read-only, and only the toolkit's own stack tree (its nested stacks share its name as a prefix).
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["cloudformation:DescribeStackResource", "cloudformation:ListStackResources"],
      resources: [
        stack.formatArn({ service: "cloudformation", resource: "stack", resourceName: `${props.stackNamePrefix}/*` }),
        stack.formatArn({ service: "cloudformation", resource: "stack", resourceName: `${props.stackNamePrefix}-*` }),
      ],
    }));
    // CloudFormation-generated role names keep the logical id; the `*` before it spans the path.
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:GetRole"],
      resources: [stack.formatArn({
        service: "iam", region: "", resource: "role", resourceName: `*${HYBRID_SEARCH_ROLE_LOGICAL_ID}*`,
      })],
    }));

    const provider = new cr.Provider(this, "Provider", { onEventHandler: fn });
    const resource = new cdk.CustomResource(this, "Resource", {
      serviceToken: provider.serviceToken,
      resourceType: "Custom::MediaToolkitHybridSearchRole",
      properties: {
        ToolkitStackName: props.stackName,
        NestedLogicalId: props.nestedLogicalId ?? "",
        RoleLogicalId: HYBRID_SEARCH_ROLE_LOGICAL_ID,
        Revision: props.revision,
      },
    });
    this.roleArn = resource.getAttString("RoleArn");
  }
}

/** Grant the hybrid-search indexer role(s) GetObject on image Search_Renditions, nothing else. */
export function grantRenditionRead(bucket: s3.IBucket, principals: iam.IPrincipal[]): void {
  bucket.addToResourcePolicy(
    new iam.PolicyStatement({
      sid: RENDITION_READ_SID,
      effect: iam.Effect.ALLOW,
      principals,
      actions: ["s3:GetObject"],
      resources: [`${bucket.bucketArn}/projects/*/rendition.jpg`],
    }),
  );
}
