import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { skillsBucketName, skillsBucketProps } from "../skills-bucket";
import { grantRenditionRead, HybridSearchRoleLookup } from "./hybrid-search-role-lookup";
import { TOOLKIT_TEMPLATE_URLS, TOOLKIT_VERSION } from "./toolkit-release";

/** Logical id of the toolkit nested stack — the harness's ToolkitLookup resolves it by this id. */
export const TOOLKIT_NESTED_LOGICAL_ID = "Toolkit";

export interface MediaToolkitStackProps extends cdk.StackProps {
  readonly projectName: string;
  /**
   * Optional overrides for the toolkit template's own parameters (`Prefix`, `GatewayName`,
   * `Environment`, `ProxyBucket`, `OAuthScope`, `SecretsKmsKeyId`). `IngestBucket` is always the
   * shared Skills bucket created here.
   */
  readonly toolkitParameters?: Record<string, string>;
}

/**
 * The media toolkit, deployed from its published template, plus the bucket it and the harness
 * share. Its own stack (not nested in the harness) so a failed harness deploy never rolls back a
 * toolkit that took 1–2 hours to create, and the two can be updated or deleted independently.
 *
 * The toolkit is a native nested stack (`AWS::CloudFormation::Stack`) rather than a custom
 * resource: CloudFormation waits at most one hour for a custom resource, and a first toolkit
 * create takes longer. The harness reads the nested stack's Outputs at its own deploy time
 * (ToolkitLookup), so there are no cross-stack Exports locking the toolkit's values.
 */
export class MediaToolkitStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly toolkit: cdk.CfnStack;

  constructor(scope: Construct, id: string, props: MediaToolkitStackProps) {
    super(scope, id, props);

    if (!cdk.Token.isUnresolved(this.region) && !(this.region in TOOLKIT_TEMPLATE_URLS)) {
      throw new Error(
        `The media toolkit ${TOOLKIT_VERSION} is published in ${Object.keys(TOOLKIT_TEMPLATE_URLS).join(", ")}; ` +
          `not in ${this.region}. Deploy in one of those regions, or deploy the toolkit yourself and ` +
          `pass -c toolkitStackName=<name>.`,
      );
    }
    const urls = new cdk.CfnMapping(this, "ToolkitTemplateUrls", {
      mapping: Object.fromEntries(
        Object.entries(TOOLKIT_TEMPLATE_URLS).map(([region, url]) => [region, { url }]),
      ),
    });

    // The toolkit's IngestBucket "must already exist", so it is created here, ahead of the toolkit;
    // the harness imports it by the same name. Sharing it means the toolkit's workflow roles read
    // harness uploads through their own identity policies — no bucket-policy bridge needed.
    this.bucket = new s3.Bucket(this, "SkillsBucket", skillsBucketProps(skillsBucketName(props.projectName, this)));
    // The harness's artifacts CDN reads this bucket through Origin Access Control. The harness
    // imports the bucket, and CDK cannot add a policy to an imported bucket, so the owner grants it
    // here. The distribution is created later, in the harness stack, so its ARN is matched by
    // account rather than referenced (a reference would be a cross-stack export).
    this.bucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: "AllowCloudFrontOacRead",
      principals: [new iam.ServicePrincipal("cloudfront.amazonaws.com")],
      actions: ["s3:GetObject"],
      resources: [this.bucket.arnForObjects("*")],
      conditions: {
        StringEquals: { "AWS:SourceAccount": this.account },
        ArnLike: { "AWS:SourceArn": `arn:${this.partition}:cloudfront::${this.account}:distribution/*` },
      },
    }));

    this.toolkit = new cdk.CfnStack(this, TOOLKIT_NESTED_LOGICAL_ID, {
      templateUrl: urls.findInMap(cdk.Aws.REGION, "url"),
      parameters: { ...(props.toolkitParameters ?? {}), IngestBucket: this.bucket.bucketName },
    });
    this.toolkit.overrideLogicalId(TOOLKIT_NESTED_LOGICAL_ID);

    // The ONE toolkit reader its own identity policies do not cover: hybrid-search's indexer embeds an
    // image's Search_Rendition straight from this bucket, but its role is scoped to the toolkit's proxy
    // bucket. The toolkit exports no Output for that role, so its exact ARN is resolved from the nested
    // stack tree once the toolkit exists (the lookup takes `toolkit.ref`, so it runs after it) and named
    // as the principal. Not account-root + aws:PrincipalArn: an account principal only delegates to IAM,
    // and the role's identity policy grants nothing here.
    const hybridSearchRole = new HybridSearchRoleLookup(this, "HybridSearchRole", {
      stackName: this.toolkit.ref,
      stackNamePrefix: `${this.stackName}-${TOOLKIT_NESTED_LOGICAL_ID}`,
      revision: JSON.stringify({ TOOLKIT_VERSION, params: props.toolkitParameters ?? {} }),
    });
    grantRenditionRead(this.bucket, [new iam.ArnPrincipal(hybridSearchRole.roleArn)]);

    new cdk.CfnOutput(this, "ToolkitVersion", { value: TOOLKIT_VERSION });
    new cdk.CfnOutput(this, "ToolkitStackArn", {
      value: this.toolkit.ref,
      description: "The nested media-toolkit stack (its Outputs carry GatewayUrl, the credentials secret, the proxy bucket)",
    });
    new cdk.CfnOutput(this, "IngestBucketName", { value: this.bucket.bucketName });
  }
}
