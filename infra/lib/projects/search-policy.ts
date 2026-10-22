/**
 * Search_Policy builder (media-harness-projects-access, design Component 6 "Builder contract", D14).
 *
 * Pure: returns the single Cedar `permit` the Search_Gateway's policy engine enforces. The output is
 * byte-identical to `agent/scripts/local_search_gateway.py::policy()` for the same inputs.
 *
 * The query image is `search_image_s3uri` (its name since M2C bundle alpha.20261014a, the only bundle any
 * environment runs). The permit must name the spelling the upstream declares: strict validation accepts a
 * `has` on an attribute the schema does not declare, so a permit naming another spelling validates and leaves
 * the real input unconstrained (`test_search_policy_strict.py` pins this). `SearchTargetSync` therefore fails
 * the deploy if the upstream declares the pre-rename `search_image_uri`.
 *
 * In the Ingest bucket the image must be a Search_Rendition (`projects/<pid>/<assetId>/rendition.jpg`,
 * matched with a `*` wildcard for the middle): the index embeds
 * renditions, and the bucket policy lets M2C's search role read only those, so the permit, the harness's
 * pre-check (`call_checks.check_image_uri`) and that grant all name the same objects. Proxy-bucket frames
 * stay `/*` (M2C's own bucket; the harness limits them to this turn's hit frames).
 *
 * The clause is `if context.input has <arg> then (...) else true`. The equivalent
 * `!(context.input has search_image_s3uri) || ...` form fails Cedar strict validation ("unable to
 * guarantee safety of access to optional attribute"), so `CreatePolicy` under
 * `FAIL_ON_ANY_FINDINGS` rejects it (verified live against AgentCore).
 */
import { Fn, Token } from 'aws-cdk-lib';

export interface SearchPolicyInput {
  /** `<target>___hybrid-search___search_content`, from `SearchTargetSync`. */
  readonly searchToolName: string;
  /** The Search_Gateway ARN. */
  readonly gatewayArn: string;
  /** The Harness Ingest (Skills/upload) bucket that holds every Project_Prefix. */
  readonly ingestBucket: string | undefined;
  /** `envConfig.media.proxyBucket`, or the nested M2C output in `source`. */
  readonly m2cProxyBucket: string | undefined;
  /** Environment name, used only in error messages. */
  readonly envName: string;
}

/** S3 bucket-name character set; excludes Cedar's `*` wildcard and the `"` / `\` that break a literal. */
const BUCKET_NAME = /^[a-z0-9.-]+$/;

function checkBucket(value: string | undefined, setting: string, envName: string): string {
  if (value === undefined || value === null) {
    throw new Error(`Search_Policy (env "${envName}"): ${setting} is not set`);
  }
  // An unresolved CDK token (e.g. the nested M2C stack's output in `source`) counts as present;
  // CloudFormation substitutes the literal at deploy and FAIL_ON_ANY_FINDINGS validates the result.
  if (Token.isUnresolved(value)) return value;
  if (value === '' || !BUCKET_NAME.test(value)) {
    throw new Error(
      `Search_Policy (env "${envName}"): ${setting} ${JSON.stringify(value)} is not a valid S3 bucket name ` +
        `(must be non-empty and match ${BUCKET_NAME.source})`,
    );
  }
  return value;
}

/** The query-image input the permit constrains. */
export const SEARCH_IMAGE_ARG = 'search_image_s3uri';

export function buildSearchPolicy(input: SearchPolicyInput): string {
  const ingest = checkBucket(input.ingestBucket, 'Ingest bucket', input.envName);
  const proxy = checkBucket(input.m2cProxyBucket, 'media.proxyBucket', input.envName);

  // Literal segments interleaved with the values; joined directly, or through Fn::Join when any value is a
  // token so each token stays a separate list element.
  const imageClause = (arg: string) => [
    `  (if context.input has ${arg}\n     then (context.input.${arg} like "s3://`,
    ingest,
    `/projects/*/rendition.jpg" ||\n           context.input.${arg} like "s3://`,
    proxy,
    '/*")\n     else true)',
  ];
  const parts = [
    'permit(\n  principal,\n  action == AgentCore::Action::"',
    input.searchToolName,
    '",\n  resource == AgentCore::Gateway::"',
    input.gatewayArn,
    '"\n) when {\n' +
      '  principal.hasTag("project_id") &&\n' +
      '  context has input &&\n' +
      '  context.input has collection_id &&\n' +
      '  context.input.collection_id == principal.getTag("project_id") &&\n',
    ...imageClause(SEARCH_IMAGE_ARG),
    '\n};',
  ];

  return parts.some((p) => Token.isUnresolved(p)) ? Fn.join('', parts) : parts.join('');
}
