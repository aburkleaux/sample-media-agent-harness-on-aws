/**
 * The media toolkit release the harness deploys underneath itself (`-c deployToolkit=true`).
 *
 * The single place to bump the toolkit: copy the new version's Template URLs from the toolkit
 * repo's README `## Releases` section. The templates are public, pre-built and staged per region,
 * and each references its own region's artifact bucket — so the stack must deploy in the same
 * region as the URL it picks (MediaToolkitStack enforces that via a CfnMapping on AWS::Region).
 */
export const TOOLKIT_VERSION = "v0.1.0";

export const TOOLKIT_TEMPLATE_URLS: Readonly<Record<string, string>> = {
  "eu-west-1":
    "https://awsi-megs-guidances-eu-west-1.s3.eu-west-1.amazonaws.com/unified-media-toolkit/v0.1.0/micro-workflow-bundle/micro-workflow-bundle-stack.template",
  "us-east-1":
    "https://awsi-megs-guidances-us-east-1.s3.us-east-1.amazonaws.com/unified-media-toolkit/v0.1.0/micro-workflow-bundle/micro-workflow-bundle-stack.template",
  "us-west-2":
    "https://awsi-megs-guidances-us-west-2.s3.us-west-2.amazonaws.com/unified-media-toolkit/v0.1.0/micro-workflow-bundle/micro-workflow-bundle-stack.template",
  "ap-southeast-2":
    "https://awsi-megs-guidances-ap-southeast-2.s3.ap-southeast-2.amazonaws.com/unified-media-toolkit/v0.1.0/micro-workflow-bundle/micro-workflow-bundle-stack.template",
  "ap-northeast-1":
    "https://awsi-megs-guidances-ap-northeast-1.s3.ap-northeast-1.amazonaws.com/unified-media-toolkit/v0.1.0/micro-workflow-bundle/micro-workflow-bundle-stack.template",
};
