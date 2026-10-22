import * as cr from "aws-cdk-lib/custom-resources";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Construct } from "constructs";

export interface AddMediaGatewaySeedProps {
  registryTable: dynamodb.ITable;
  gatewayUrl: string;
  clientId: string;
  tokenEndpoint: string;
  credentialsSecretArn: string;
  scope?: string;
}

// The toolkit's media-workflow-gateway is deployed and owned independently of
// this stack (see env-config.ts's `media` block). Unlike the old nested-bundle
// seed this replaces, every OAuth field here is a plain env-config string, so
// this is a direct DynamoDB putItem -- no Lambda needed -- identical in shape
// to SeedOmsCoreGateway below. onDelete removes the row because, unlike OMS,
// a redeploy that drops `media` config means this env no longer federates to
// that gateway at all.
export function addMediaGatewaySeed(
  scope: Construct,
  id: string,
  props: AddMediaGatewaySeedProps
): cr.AwsCustomResource {
  const key = {
    PK: { S: "GATEWAY#REGISTERED" },
    SK: { S: "media-workflow-gateway" },
  };
  const putAction = {
    service: "DynamoDB",
    action: "putItem",
    parameters: {
      TableName: props.registryTable.tableName,
      Item: {
        ...key,
        id: { S: "media-workflow-gateway" },
        name: { S: "Media Workflow Gateway" },
        description: {
          S: "Media micro-workflow tools (transcode, diarization, shot segmentation, face recognition, TTS, etc.) federated from the independently-deployed toolkit gateway.",
        },
        url: { S: props.gatewayUrl },
        auth_type: { S: "jwt" },
        auth_config: {
          M: {
            auth_type: { S: "jwt" },
            client_id: { S: props.clientId },
            token_endpoint: { S: props.tokenEndpoint },
            client_secret_arn: { S: props.credentialsSecretArn },
            scope: { S: props.scope ?? "media-workflows/invoke" },
          },
        },
        enabled: { BOOL: true },
        created_by: { S: "infra-seed" },
      },
    },
    physicalResourceId: cr.PhysicalResourceId.of("gateway-media-workflow-gateway"),
  };
  const deleteAction = {
    service: "DynamoDB",
    action: "deleteItem",
    parameters: {
      TableName: props.registryTable.tableName,
      Key: key,
    },
    physicalResourceId: cr.PhysicalResourceId.of("gateway-media-workflow-gateway"),
  };
  const seed = new cr.AwsCustomResource(scope, id, {
    installLatestAwsSdk: true,
    onCreate: putAction,
    onUpdate: putAction, // identical to onCreate so redeploys refresh the row
    onDelete: deleteAction,
    policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
      resources: [props.registryTable.tableArn],
    }),
  });
  seed.node.addDependency(props.registryTable);
  return seed;
}
