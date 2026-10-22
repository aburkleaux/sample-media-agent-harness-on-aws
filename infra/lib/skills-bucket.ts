import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";

/**
 * The harness Skills bucket — also the media toolkit's IngestBucket. One definition, used by
 * whichever stack creates it: the harness itself (`-c createSkillsBucket=true`) or the
 * MediaToolkitStack, which must create it first because the toolkit needs an existing
 * IngestBucket and the harness then imports it by this same name.
 */
export function skillsBucketName(projectName: string, scope: cdk.Stack): string {
  return `${projectName}-skills-store-${scope.account}-${scope.region}`;
}

export function skillsBucketProps(bucketName: string): s3.BucketProps {
  return {
    bucketName,
    removalPolicy: cdk.RemovalPolicy.RETAIN,
    versioned: true,
    encryption: s3.BucketEncryption.S3_MANAGED,
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    lifecycleRules: [
      // The mirror prefixes `artifacts/durable/` and `artifacts/imports/` used to expire here
      // after 60 days. Both are retired (asset-lineage Phase 2): a manifest now REFERENCES an
      // M2C deliverable and the proxy presigns it in place, so nothing writes a copy. The old
      // 60-day rules were themselves a bug — the copies expired while the M2C originals, which
      // have no lifecycle rule at all, lived on, so a render older than 60 days broke with its
      // data still present. Existing copies are left to age out under no rule; they are only
      // read by manifests written before the switch.
      { noncurrentVersionExpiration: cdk.Duration.days(30) },
    ],
    // Allow browser PUT/GET against presigned URLs from external consoles.
    // The external portal (POST /invoke/:agentId/uploads) hands the browser
    // a presigned PUT to `artifacts/...`; the browser uploads bytes DIRECTLY
    // to S3, so the bucket must answer the cross-origin preflight. Presigned
    // URLs remain the authorization boundary — CORS only permits the browser
    // to make the request; it grants no standing access.
    cors: [
      {
        allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET, s3.HttpMethods.HEAD],
        allowedOrigins: ["*"],
        allowedHeaders: ["*"],
        exposedHeaders: ["ETag"],
        maxAge: 3000,
      },
    ],
  };
}
