/**
 * ProjectsStack — the nested stack for Projects, the per-turn Project_Token, and the Cedar-governed
 * Search_Gateway (media-harness-projects-access, design Components 3/5/6, D1, D10; tasks 8.2–8.9).
 *
 * It is a CDK `NestedStack` created by `agent-infra-stack.ts` ONLY when `envConfig.projects.enabled`.
 * Everything heavy lives here so the parent template keeps its headroom under CloudFormation's
 * 1,000,000-byte limit: the parent grows only by the `AWS::CloudFormation::Stack` resource, its
 * parameters, and a handful of policies/env entries (D1).
 *
 * Contents:
 *   - the projects DynamoDB table (+ KMS not needed; single-table, on-demand, stream, no GSI, no TTL)
 *   - one KMS RSA_2048 SIGN_VERIFY key per signing-key entry (TokenIssuerRole is the sole signer)
 *   - the OIDC publisher (private bucket + dedicated CloudFront + custom resource)
 *   - TokenIssuerFn + TokenRefreshFn on one shared TokenIssuerRole
 *   - the Search_Gateway, its role, the M2C OAuth credential provider, the m2c-search target, and the
 *     target-sync custom resource
 *   - the Cedar policy engine + permit (ENFORCE) built from `buildSearchPolicy`
 *
 * Everything AgentCore-control-plane (gateway, credential provider, target, sync) is a CloudFormation
 * resource or a custom resource — none of it is genuinely out of band. The ONE provisioning helper is
 * `agent/scripts/search_gateway_sync.py`, a re-sync CLI that shares the target-sync module; it does not
 * create resources, it re-introspects an already-deployed target (design Component 6 "Re-sync command").
 */

import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cr from "aws-cdk-lib/custom-resources";
import * as path from "path";
import { Construct } from "constructs";

import { ProjectsConfig } from "../env-config";
import { buildSearchPolicy } from "./search-policy";
import { assertSearchPlane } from "./assert-search-plane";

/** CloudWatch metric namespace the token functions emit failure counts into (matches `index.py`). */
const METRICS_NAMESPACE = "MediaworksHarness/Projects";

export interface ProjectsStackProps extends cdk.NestedStackProps {
  readonly projectName: string;
  /** Harness env name (`sandbox` | `staging` | `source`). Part of the Search audience and names. */
  readonly envName: string;
  readonly projectsConfig: ProjectsConfig;

  // ---- Cognito (end-user pool) — for first-issuance User_Token verification ----
  readonly userPoolId: string;
  readonly userPoolClientId: string;

  // ---- M2C media-workflow-gateway (the Search target's upstream) ----
  readonly m2cGatewayUrl: string;
  readonly m2cCredentialsSecretArn: string;
  readonly m2cClientId: string;
  readonly m2cTokenEndpoint: string;
  /**
   * Explicit OIDC discovery URL for M2C's auth server (the Cognito IdP endpoint). Overrides the
   * token-endpoint-based derivation, which produces a dead hosted-UI-domain URL that fails the
   * target sync. See `MediaWorkflowGatewayConfig.oidcDiscoveryUrl`.
   */
  readonly m2cDiscoveryUrl?: string;
  readonly m2cScope?: string;
  /** JSON key for the client secret inside M2C's `gateway-credentials` bundle (default `clientSecret`). */
  readonly m2cSecretJsonKey?: string;

  // ---- Buckets governed by the Cedar image-URI bound ----
  /** The Harness Ingest (Skills/upload) bucket that holds every Project_Prefix. */
  readonly ingestBucketName: string;
  /** `envConfig.media.proxyBucket`, or the nested M2C output in `source` (may be an unresolved token). */
  readonly m2cProxyBucket?: string;

  // ---- Caller ARNs for the token functions' resource policies ----
  /** Proxy ECS task role ARN — the only caller of `TokenIssuerFn`. */
  readonly proxyTaskRoleArn: string;
  /** Main_Agent runtime execution-role ARN — the only caller of `TokenRefreshFn`. */
  readonly mainAgentRuntimeRoleArn: string;
  /**
   * Knowledge-graph tools for dispatched sub-agents: the delegation-grant KMS key (verify only) and the Task Agent
   * runtime role, the only caller of `DelegatedTokenIssuerFn`. Both absent → the function is not created.
   */
  readonly delegationKeyArn?: string;
  readonly taskAgentRuntimeRoleArn?: string;
}

export class ProjectsStack extends cdk.NestedStack {
  /** The projects table. */
  public readonly projectsTable: dynamodb.Table;
  /** First-issuance function (`index.issue_handler`). */
  public readonly tokenIssuerFn: lambda.Function;
  /** Token_Refresh function (`index.refresh_handler`). */
  public readonly tokenRefreshFn: lambda.Function;
  /** Project_Token from a delegation grant, for a dispatched sub-agent's graph tools (Task Agent-only caller). */
  public readonly delegatedTokenIssuerFn?: lambda.Function;
  /** Shared execution role for both token functions (sole `kms:Sign` principal). */
  public readonly tokenIssuerRole: iam.Role;
  /** Search_Gateway URL (token) — goes into `RuntimeConfigSecret` as `SEARCH_GATEWAY_URL`. */
  public readonly searchGatewayUrl: string;
  /** `<target>___hybrid-search___search_content` — `SEARCH_TOOL_NAME`. */
  public readonly searchToolName: string;
  /** OIDC issuer URL (`https://<oidc-cloudfront-domain>`) — `PROJECT_TOKEN_ISSUER`. */
  public readonly projectTokenIssuerUrl: string;
  /** The Cedar permit statement (for the synth test). */
  public readonly searchPolicyStatement: string;
  /** The Search_Gateway L1 resource (for the synth test). */
  public readonly searchGateway: cdk.aws_bedrockagentcore.CfnGateway;
  /** The Search_Gateway's service role — the knowledge-graph Lambda target (GraphStack) adds its invoke grant. */
  public readonly searchGatewayRole: iam.Role;
  /** The Search_Gateway's ENFORCE-mode Cedar engine — GraphStack attaches one permit per graph tool to it. */
  public readonly searchPolicyEngine: cdk.aws_bedrockagentcore.CfnPolicyEngine;

  constructor(scope: Construct, id: string, props: ProjectsStackProps) {
    super(scope, id, props);

    const { projectName, envName, projectsConfig } = props;
    const region = this.region;
    const account = this.account;
    const prefix = projectName.replace(/-/g, "_");

    const audience = `${projectName}-search-${envName}`;
    const lifetime = projectsConfig.tokenLifetimeSeconds ?? 3600;

    // ---------------------------------------------------------------------------------------------
    // Projects table (design Data Models; task 8.2)
    // ---------------------------------------------------------------------------------------------
    // Prod holds durable access data → RETAIN + deletion protection. But the fixed-name table
    // (`<project>-projects`) combined with RETAIN turns every FAILED nested-stack CREATE into an
    // orphan that blocks the NEXT deploy: rollback cannot delete a deletion-protected table, so the
    // name stays taken and CloudFormation's early validation rejects the re-create with
    // "Validation failed with 1 error(s)". Prod does not run this stack today (projects.enabled is
    // false there — see the synth test), so the only envs that actually create it are
    // sandbox/staging/source, exactly where iterative rollout churn hits that trap. Use DESTROY (and
    // no deletion protection) in the non-prod envs so a failed rollout cleans up after itself; keep
    // RETAIN + protection for prod when it eventually enables projects.
    const retainData = envName === "prod";
    this.projectsTable = new dynamodb.Table(this, "ProjectsTable", {
      tableName: `${projectName}-projects`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: retainData ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      deletionProtection: retainData,
      // 35-day point-in-time restore for project data, in every env (2026-10-11, before prod holds data).
      pointInTimeRecovery: true,
      // No TimeToLiveSpecification — every access check reads the base table consistently (Req 1.28).
    });

    // media-harness-project-ingestion (design Data Models "GSI assetsByRegistered"; §20): the one GSI
    // CloudFormation adds for this spec. It serves the Assets tab (newest first) and the Dispatcher's
    // 1-minute sweep (via the sparse META `assetListPk = "SCHED"` entry). Projection ALL, and
    // eventually consistent — acceptable because no access decision reads it (checkAccess runs first on
    // the base table and the upload result comes from the registration response, not the list). The
    // table stream stays NEW_AND_OLD_IMAGES so the Dispatcher and the purge trigger can consume it.
    this.projectsTable.addGlobalSecondaryIndex({
      indexName: "assetsByRegistered",
      partitionKey: { name: "assetListPk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "assetListSk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---------------------------------------------------------------------------------------------
    // Token_Issuer execution role (shared by both functions; sole kms:Sign principal) (task 8.5)
    // ---------------------------------------------------------------------------------------------
    this.tokenIssuerRole = new iam.Role(this, "TokenIssuerRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Shared role for TokenIssuerFn + TokenRefreshFn: kms:Sign/Verify on the signing keys " +
        "and dynamodb:BatchGetItem on the projects table, nothing else.",
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
      ],
    });

    // ---------------------------------------------------------------------------------------------
    // Signing keys: one KMS RSA_2048 SIGN_VERIFY key per entry; TokenIssuerRole is the only signer
    // (design D1, D10; task 8.2)
    // ---------------------------------------------------------------------------------------------
    const signingKeys = projectsConfig.signingKeys ?? [];
    if (signingKeys.length === 0) {
      throw new Error(`ProjectsStack (${envName}): projects.signingKeys must have at least one entry`);
    }
    const keyIds: string[] = [];
    let currentSigningKeyId: string | undefined;
    for (const entry of signingKeys) {
      const key = new kms.Key(this, `SigningKey-${entry.id}`, {
        description: `${projectName} Project_Token signing key ${entry.id} (${envName})`,
        keySpec: kms.KeySpec.RSA_2048,
        keyUsage: kms.KeyUsage.SIGN_VERIFY,
        alias: `${projectName}-project-token-${entry.id}-${envName}`,
        // Persisted tokens are verified against this key material; retain across stack replacement in
        // prod. In the non-prod envs that run this stack, a failed rollout should take its keys with
        // it (same orphan-avoidance rationale as the table above) — tokens there are short-lived test
        // tokens, not durable material.
        removalPolicy: retainData ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      });
      // The key policy names TokenIssuerRole as the ONLY kms:Sign principal, so a later broad IAM
      // grant on some other role cannot add a signer.
      key.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: "TokenIssuerRoleIsSoleSigner",
          principals: [this.tokenIssuerRole],
          actions: ["kms:Sign"],
          resources: ["*"],
        }),
      );
      // Identity grants on the shared role (both functions sign; refresh verifies via kms:Verify).
      key.grant(this.tokenIssuerRole, "kms:Sign", "kms:Verify");
      keyIds.push(key.keyId);
      if (!entry.retired) currentSigningKeyId = key.keyId;
    }
    if (!currentSigningKeyId) {
      throw new Error(`ProjectsStack (${envName}): no non-retired signing key (exactly one is required)`);
    }
    const publishedKeyIds = keyIds.join(",");

    this.projectsTable.grant(this.tokenIssuerRole, "dynamodb:BatchGetItem");

    // ---------------------------------------------------------------------------------------------
    // OIDC discovery + JWKS: private bucket, dedicated CloudFront, publisher custom resource
    // (design Component 5, D3; task 8.3)
    // ---------------------------------------------------------------------------------------------
    const oidcBucket = new s3.Bucket(this, "OidcBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const oidcDistribution = new cloudfront.Distribution(this, "OidcCdn", {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(oidcBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      comment: `${projectName} Project_Token OIDC issuer (${envName})`,
    });
    this.projectTokenIssuerUrl = `https://${oidcDistribution.distributionDomainName}`;
    const oidcDiscoveryUrl = `${this.projectTokenIssuerUrl}/.well-known/openid-configuration`;

    const oidcPublisherFn = new lambda.Function(this, "OidcPublisherFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "oidc-publisher")),
      timeout: cdk.Duration.minutes(2),
      description: "Publishes the Project_Token OIDC discovery doc + JWKS (kms:GetPublicKey only).",
    });
    oidcBucket.grantReadWrite(oidcPublisherFn);
    for (const key of this.node.findAll().filter((c): c is kms.Key => c instanceof kms.Key)) {
      key.grant(oidcPublisherFn, "kms:GetPublicKey");
    }
    oidcPublisherFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cloudfront:CreateInvalidation"],
        resources: [
          `arn:aws:cloudfront::${account}:distribution/${oidcDistribution.distributionId}`,
        ],
      }),
    );
    const oidcProvider = new cr.Provider(this, "OidcPublisherProvider", {
      onEventHandler: oidcPublisherFn,
    });
    const oidcPublisher = new cdk.CustomResource(this, "OidcPublisher", {
      serviceToken: oidcProvider.serviceToken,
      properties: {
        BucketName: oidcBucket.bucketName,
        IssuerUrl: this.projectTokenIssuerUrl,
        DistributionId: oidcDistribution.distributionId,
        KeyIds: JSON.stringify(keyIds),
      },
    });

    // ---------------------------------------------------------------------------------------------
    // TokenIssuerFn + TokenRefreshFn — one code asset, two functions, one role (task 8.5)
    // ---------------------------------------------------------------------------------------------
    // NOTE(deploy): the token-issuer asset is dependency-free — it imports only the Python 3.13
    // stdlib (plus boto3, provided by the runtime). It signs via kms:Sign and verifies via kms:Verify,
    // so no cryptography/PyJWT layer or bundling step is needed. Synth zips the directory as-is.
    const tokenCode = lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "token-issuer"));
    const tokenEnv: Record<string, string> = {
      PROJECTS_TABLE: this.projectsTable.tableName,
      SIGNING_KEY_ID: currentSigningKeyId,
      PUBLISHED_KEY_IDS: publishedKeyIds,
      TOKEN_ISSUER_URL: this.projectTokenIssuerUrl,
      SEARCH_GATEWAY_AUDIENCE: audience,
      TOKEN_LIFETIME_SECONDS: String(lifetime),
      USER_POOL_ID: props.userPoolId,
      USER_POOL_CLIENT_ID: props.userPoolClientId,
      METRICS_NAMESPACE,
    };
    this.tokenIssuerFn = new lambda.Function(this, "TokenIssuerFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.issue_handler",
      code: tokenCode,
      role: this.tokenIssuerRole,
      timeout: cdk.Duration.seconds(10),
      // CPU scales with memory. At the 128 MB default a cold issuance took 6.5–7.4 s (boto3 client setup)
      // against the proxy's 2 s budget, so the first chat turn after an idle period ran without search
      // (session 66f2b85b). The handler is also built during init (index.py `_prewarm`).
      memorySize: 1024,
      environment: tokenEnv,
      description: "First issuance of a Project_Token from a verified User_Token (proxy-only caller).",
    });
    this.tokenRefreshFn = new lambda.Function(this, "TokenRefreshFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.refresh_handler",
      code: tokenCode,
      role: this.tokenIssuerRole,
      timeout: cdk.Duration.seconds(10),
      memorySize: 1024,
      environment: tokenEnv,
      description: "Token_Refresh from a current Project_Token (Main_Agent runtime-only caller).",
    });

    // Delegated issuance (knowledge-graph tools for the Task Agent / plan nodes): same code, same signing role,
    // plus kms:Verify on the delegation-grant key. A fixed name, so the parent's runtime config and the Task
    // runtime's invoke grant take no token from this nested stack.
    if (props.delegationKeyArn && props.taskAgentRuntimeRoleArn) {
      this.tokenIssuerRole.addToPolicy(new iam.PolicyStatement({
        sid: "VerifyDelegationGrants", actions: ["kms:Verify"], resources: [props.delegationKeyArn],
      }));
      const delegatedFn = new lambda.Function(this, "DelegatedTokenIssuerFn", {
        functionName: delegatedTokenIssuerFunctionName(projectName),
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "index.delegated_issue_handler",
        code: tokenCode,
        role: this.tokenIssuerRole,
        timeout: cdk.Duration.seconds(10),
        memorySize: 1024,
        environment: { ...tokenEnv, DELEGATION_KEY_ARN: props.delegationKeyArn },
        description: "Project_Token from a verified delegation grant, for a dispatched sub-agent's graph tools (Task Agent-only caller).",
      });
      delegatedFn.addPermission("TaskAgentInvoke", {
        principal: new iam.ArnPrincipal(props.taskAgentRuntimeRoleArn),
        action: "lambda:InvokeFunction",
      });
      (this as { delegatedTokenIssuerFn?: lambda.Function }).delegatedTokenIssuerFn = delegatedFn;
    }

    // Resource policies: who may invoke each function (identity policies are added by the parent).
    this.tokenIssuerFn.addPermission("ProxyInvoke", {
      principal: new iam.ArnPrincipal(props.proxyTaskRoleArn),
      action: "lambda:InvokeFunction",
    });
    this.tokenRefreshFn.addPermission("MainAgentInvoke", {
      principal: new iam.ArnPrincipal(props.mainAgentRuntimeRoleArn),
      action: "lambda:InvokeFunction",
    });
    // The deployed live test (sandbox/staging only) may invoke both directly.
    const testInvokers = projectsConfig.tokenTestInvokerRoleArns ?? [];
    testInvokers.forEach((arn, i) => {
      this.tokenIssuerFn.addPermission(`TestInvokerIssue${i}`, {
        principal: new iam.ArnPrincipal(arn),
        action: "lambda:InvokeFunction",
      });
      this.tokenRefreshFn.addPermission(`TestInvokerRefresh${i}`, {
        principal: new iam.ArnPrincipal(arn),
        action: "lambda:InvokeFunction",
      });
    });

    // Failure alarms (task 8.5).
    for (const [name, metricName] of [
      ["TokenIssuanceFailureAlarm", "TokenIssuanceFailure"],
      ["TokenRefreshFailureAlarm", "TokenRefreshFailure"],
    ] as const) {
      new cloudwatch.Alarm(this, name, {
        metric: new cloudwatch.Metric({
          namespace: METRICS_NAMESPACE,
          metricName,
          statistic: "Sum",
          period: cdk.Duration.minutes(5),
        }),
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: `${projectName} ${metricName} (${envName})`,
      });
    }

    // ---------------------------------------------------------------------------------------------
    // Search_Gateway role (copy of PlatformGatewayRole) (design Component 6; task 8.6)
    // ---------------------------------------------------------------------------------------------
    const searchGatewayRole = this.searchGatewayRole = new iam.Role(this, "SearchGatewayRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      inlinePolicies: {
        "search-gateway-identity": new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ["bedrock-agentcore:GetWorkloadAccessToken"],
              resources: [
                `arn:aws:bedrock-agentcore:${region}:${account}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${region}:${account}:workload-identity-directory/default/workload-identity/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: [
                "bedrock-agentcore:GetResourceOauth2Token",
                "bedrock-agentcore:GetResourceApiKey",
              ],
              resources: [
                `arn:aws:bedrock-agentcore:${region}:${account}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${region}:${account}:workload-identity-directory/default/workload-identity/*`,
                `arn:aws:bedrock-agentcore:${region}:${account}:token-vault/default`,
                `arn:aws:bedrock-agentcore:${region}:${account}:token-vault/default/oauth2credentialprovider/*`,
                `arn:aws:bedrock-agentcore:${region}:${account}:token-vault/default/apikeycredentialprovider/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: ["secretsmanager:GetSecretValue"],
              resources: [`arn:aws:secretsmanager:${region}:${account}:secret:bedrock-agentcore*`],
            }),
            new iam.PolicyStatement({
              actions: ["bedrock-agentcore:GetPolicyEngine"],
              resources: [`arn:aws:bedrock-agentcore:${region}:${account}:policy-engine/*`],
            }),
            new iam.PolicyStatement({
              actions: [
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
              ],
              resources: [
                `arn:aws:bedrock-agentcore:${region}:${account}:policy-engine/*`,
                `arn:aws:bedrock-agentcore:${region}:${account}:gateway/*`,
              ],
            }),
          ],
        }),
      },
    });

    // ---------------------------------------------------------------------------------------------
    // M2C OAuth2 credential provider (reuses cred-provider-handler + SecretJsonKey) (task 8.6)
    // ---------------------------------------------------------------------------------------------
    const credProviderFn = new lambda.Function(this, "M2cSearchCredProviderFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "cred-provider-handler")),
      timeout: cdk.Duration.minutes(5),
    });
    credProviderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [props.m2cCredentialsSecretArn],
      }),
    );
    credProviderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateOauth2CredentialProvider",
          "bedrock-agentcore:UpdateOauth2CredentialProvider",
          "bedrock-agentcore:DeleteOauth2CredentialProvider",
          "bedrock-agentcore:GetOauth2CredentialProvider",
          "bedrock-agentcore:CreateTokenVault",
          "bedrock-agentcore:GetTokenVault",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${region}:${account}:token-vault/default`,
          `arn:aws:bedrock-agentcore:${region}:${account}:token-vault/default/*`,
        ],
      }),
    );
    credProviderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:PutSecretValue"],
        resources: [`arn:aws:secretsmanager:${region}:${account}:secret:bedrock-agentcore-identity!default/oauth2/*`],
      }),
    );
    const credProvider = new cr.Provider(this, "M2cSearchCredProviderProvider", {
      onEventHandler: credProviderFn,
    });
    // Prefer the explicit discovery URL (Cognito's IdP endpoint). The hosted-UI domain embedded in
    // `tokenEndpoint` does NOT serve `/.well-known/openid-configuration` (it 404s) and the pool id is
    // not recoverable from it, so the old token-endpoint derivation produced a dead URL that failed
    // the target sync with SYNCHRONIZE_UNSUCCESSFUL. Fall back to the derivation only when unset
    // (e.g. a non-Cognito auth server whose discovery sits next to its token endpoint).
    const m2cDiscoveryUrl =
      props.m2cDiscoveryUrl
      || props.m2cTokenEndpoint.replace(/\/oauth2\/token\/?$/, "/.well-known/openid-configuration");
    const credProviderResource = new cdk.CustomResource(this, "M2cSearchCredentialProvider", {
      serviceToken: credProvider.serviceToken,
      properties: {
        ProviderName: `${projectName}-m2c-search-oauth2`,
        ClientSecretArn: props.m2cCredentialsSecretArn,
        SecretJsonKey: props.m2cSecretJsonKey ?? "clientSecret",
        DiscoveryUrl: m2cDiscoveryUrl,
        ClientId: props.m2cClientId,
      },
    });
    const credProviderArn = credProviderResource.getAttString("ProviderArn");

    // ---------------------------------------------------------------------------------------------
    // Search_Gateway (CUSTOM_JWT, no allowedClients, no searchType; ENFORCE engine override)
    // (design Component 6; tasks 8.6, 8.9)
    // ---------------------------------------------------------------------------------------------
    this.searchGateway = new cdk.aws_bedrockagentcore.CfnGateway(this, "SearchGateway", {
      name: `${projectName}-search-gateway`,
      roleArn: searchGatewayRole.roleArn,
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: {
          // No `searchType`: semantic tool search is disabled on the Search_Gateway (Req 6.9).
          supportedVersions: ["2025-03-26", "2026-07-28"],
        },
      },
      authorizerType: "CUSTOM_JWT",
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: oidcDiscoveryUrl,
          allowedAudience: [audience],
          // No allowedClients: the Project_Token has no client id; the audience pins acceptance.
        },
      },
    });
    // The gateway cannot come up until AgentCore can fetch the discovery document.
    this.searchGateway.node.addDependency(oidcPublisher);
    this.searchGatewayUrl = this.searchGateway.attrGatewayUrl;

    // ---- M2C search target (MCP-server at M2C's gateway with OAuth outbound) ----
    const targetName = `${projectName}-m2c-search`;
    this.searchToolName = `${targetName}___hybrid-search___search_content`;

    const targetFn = new lambda.Function(this, "SearchTargetFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "gateway-target-handler")),
      timeout: cdk.Duration.minutes(10),
    });
    targetFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:${region}:${account}:secret:bedrock-agentcore-identity!default/oauth2/*`],
      }),
    );
    targetFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateGatewayTarget",
          "bedrock-agentcore:DeleteGatewayTarget",
          "bedrock-agentcore:GetGatewayTarget",
          "bedrock-agentcore:SynchronizeGatewayTargets",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetResourceOauth2Token",
        ],
        resources: ["*"],
      }),
    );
    const targetProvider = new cr.Provider(this, "SearchTargetProvider", {
      onEventHandler: targetFn,
    });
    const searchTarget = new cdk.CustomResource(this, "SearchTarget", {
      serviceToken: targetProvider.serviceToken,
      properties: {
        GatewayId: this.searchGateway.attrGatewayIdentifier,
        // The target points at M2C's gateway URL (an external MCP server), not an AgentCore runtime.
        RuntimeId: "",
        TargetName: targetName,
        Endpoint: props.m2cGatewayUrl,
        CredProviderArn: credProviderArn,
        Scopes: JSON.stringify(props.m2cScope ? [props.m2cScope] : []),
        Revision: "1",
      },
    });
    searchTarget.node.addDependency(this.searchGateway);
    searchTarget.node.addDependency(credProviderResource);

    // ---- SearchTargetSync: sync + verify the one search_content tool (task 8.7) ----
    const syncFn = new lambda.Function(this, "SearchTargetSyncFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "sync.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "search-target-sync")),
      timeout: cdk.Duration.minutes(10),
    });
    syncFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:SynchronizeGatewayTargets",
          "bedrock-agentcore:GetGatewayTarget",
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetResourceOauth2Token",
        ],
        resources: ["*"],
      }),
    );
    // THE sync blocker (validated via CloudTrail of the 2026-10-05 deploy): SynchronizeGatewayTargets
    // makes AgentCore mint a fresh M2C OAuth token, and it reads the credential provider's client
    // secret from Secrets Manager AS THE CALLER (this sync Lambda's role), NOT as the gateway role.
    // Without this grant the read is AccessDenied → no token → the target reaches
    // SYNCHRONIZE_UNSUCCESSFUL and the whole deploy rolls back. `SearchTargetFn` (which CREATES the
    // target) already has the identical grant, which is why create succeeded but sync failed. Mirror
    // it here. (The `*` suffix covers Secrets Manager's random 6-char ARN suffix.)
    syncFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:${region}:${account}:secret:bedrock-agentcore-identity!default/oauth2/*`],
      }),
    );
    // Step 2 of the sync reads M2C's upstream tools/list directly (GetGatewayTarget does not return a
    // target's tools), so the function needs M2C's client credentials bundle.
    syncFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [props.m2cCredentialsSecretArn],
      }),
    );
    const syncProvider = new cr.Provider(this, "SearchTargetSyncProvider", {
      onEventHandler: syncFn,
    });
    const searchTargetSync = new cdk.CustomResource(this, "SearchTargetSync", {
      serviceToken: syncProvider.serviceToken,
      properties: {
        Region: region,
        GatewayId: this.searchGateway.attrGatewayIdentifier,
        TargetId: searchTarget.getAttString("TargetId"),
        TargetName: targetName,
        // Upstream tools/list inputs (sync step 2). The secret is read at run time, never inlined.
        M2cSecretArn: props.m2cCredentialsSecretArn,
        M2cSecretJsonKey: props.m2cSecretJsonKey ?? "clientSecret",
        M2cEndpoint: props.m2cGatewayUrl,
        M2cTokenEndpoint: props.m2cTokenEndpoint,
        M2cClientId: props.m2cClientId,
        M2cScope: props.m2cScope ?? "",
      },
    });
    searchTargetSync.node.addDependency(searchTarget);

    // ---------------------------------------------------------------------------------------------
    // Cedar policy engine + permit (ENFORCE) (design Component 6; task 8.9)
    // ---------------------------------------------------------------------------------------------
    this.searchPolicyStatement = buildSearchPolicy({
      searchToolName: this.searchToolName,
      gatewayArn: this.searchGateway.attrGatewayArn,
      ingestBucket: props.ingestBucketName,
      m2cProxyBucket: props.m2cProxyBucket,
      envName,
    });

    const searchPolicyEngine = this.searchPolicyEngine = new cdk.aws_bedrockagentcore.CfnPolicyEngine(this, "SearchPolicyEngine", {
      name: `${prefix}_search_policy`,
      description:
        "Cedar governance for the Search_Gateway: permit search_content for the caller's own Project; " +
        "default-deny everything else.",
    });
    const searchPolicy = new cdk.aws_bedrockagentcore.CfnPolicy(this, "SearchPolicyPermitSearchContent", {
      name: `${prefix}_permit_search_content`,
      policyEngineId: searchPolicyEngine.attrPolicyEngineId,
      definition: { cedar: { statement: this.searchPolicyStatement } },
      // FAIL_ON_ANY_FINDINGS is safe here (unlike the platform gateway): the policy depends on the
      // SearchTargetSync, so the action is already in the gateway's schema before the policy validates.
      validationMode: "FAIL_ON_ANY_FINDINGS",
    });
    searchPolicy.addDependency(searchPolicyEngine);
    searchPolicy.addDependency(searchTargetSync.node.defaultChild as cdk.CfnResource);

    // Associate engine -> gateway in ENFORCE. The policy references the gateway ARN (policy -> gateway),
    // so the gateway must NOT depend on the policy (that would be a cycle); it references the ENGINE.
    this.searchGateway.addPropertyOverride("PolicyEngineConfiguration", {
      Arn: searchPolicyEngine.attrPolicyEngineArn,
      Mode: "ENFORCE",
    });

    // ---------------------------------------------------------------------------------------------
    // Synth-time guard (task 8.9)
    // ---------------------------------------------------------------------------------------------
    assertSearchPlane({
      hasPolicyEngine: true,
      policyEngineMode: "ENFORCE",
      policyStatements: [this.searchPolicyStatement],
      expectedStatement: this.searchPolicyStatement,
      policyValidationMode: searchPolicy.validationMode,
      authorizerDiscoveryUrl: oidcDiscoveryUrl,
      expectedDiscoveryUrl: oidcDiscoveryUrl,
      authorizerAllowedAudience: [audience],
      expectedAudience: audience,
      authorizerHasAllowedClients: false,
      mcpHasSearchType: false,
    });
  }

  /** Token_Refresh function name — `RuntimeConfigSecret` key `PROJECT_TOKEN_REFRESH_FN`. */
  public get tokenRefreshFnName(): string {
    return this.tokenRefreshFn.functionName;
  }

  /** Token_Issuer function name — proxy env `TOKEN_ISSUER_FN`. */
  public get tokenIssuerFnName(): string {
    return this.tokenIssuerFn.functionName;
  }
}

/** The delegated Project_Token issuer's fixed name (parent runtime config + the Task runtime's invoke grant). */
export function delegatedTokenIssuerFunctionName(projectName: string): string {
  return `${projectName}-project-token-delegated`;
}
