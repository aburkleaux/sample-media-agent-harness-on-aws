"""ToolkitLookup custom-resource handler: the harvest script's logic at deploy time.

Real boto3 clients under botocore's Stubber, so every request is validated against the service
models — no credentials, no network. The client secret must never reach the response.
"""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

spec = importlib.util.spec_from_file_location(
    "toolkit_lookup", Path(__file__).resolve().parents[1] / "lib/toolkit/toolkit-lookup-handler/index.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

PARENT = "mediaworks-harness-media-toolkit"
NESTED = "arn:aws:cloudformation:us-west-2:111111111111:stack/mediaworks-harness-media-toolkit-Toolkit-ABC/uuid-1"
SECRET_ARN = "arn:aws:secretsmanager:us-west-2:111111111111:secret:/umt-x/dev/gateway-credentials-AbCdEf"
CLIENT_SECRET = "SUPER-SECRET-VALUE-never-returned"
TOKEN_ENDPOINT = "https://umt-x-dev-111111111111.auth.us-west-2.amazoncognito.com/oauth2/token"
POOL = "us-west-2_AbCdEfGhI"


def _client(service):
    return boto3.client(service, region_name="us-west-2", aws_access_key_id="t", aws_secret_access_key="t")


def _stack(stack_id, status="CREATE_COMPLETE", roles="arn:aws:iam::111111111111:role/a, arn:aws:iam::111111111111:role/b"):
    outputs = {
        "GatewayUrl": "https://umt-x.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
        "GatewayCredentialsSecretArn": SECRET_ARN,
        "ProxyBucketName": "umt-x-111111111111-us-west-2-proxy",
        "IngestBucketReadRoleArns": roles,
    }
    return {"Stacks": [{
        "StackId": stack_id, "StackName": "toolkit", "CreationTime": "2026-10-12T00:00:00Z", "StackStatus": status,
        "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
    }]}


class ToolkitLookupTests(unittest.TestCase):
    def setUp(self):
        self.clients = {s: _client(s) for s in ("cloudformation", "secretsmanager", "cognito-idp")}
        self.stubs = {s: Stubber(c) for s, c in self.clients.items()}
        for st in self.stubs.values():
            st.activate()
        patcher = patch.object(h.boto3, "client", side_effect=lambda s, **_: self.clients[s])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.event = {"RequestType": "Create", "ResourceProperties": {
            "ToolkitStackName": PARENT, "NestedLogicalId": "Toolkit", "Revision": "r1"}}

    def _secret_and_pool(self):
        self.stubs["secretsmanager"].add_response("get_secret_value", {
            "ARN": SECRET_ARN, "Name": "/umt-x/dev/gateway-credentials",
            "SecretString": json.dumps({"clientId": "cid123", "clientSecret": CLIENT_SECRET,
                                        "tokenEndpoint": TOKEN_ENDPOINT, "scope": "media-workflows/invoke"}),
        }, {"SecretId": SECRET_ARN})
        self.stubs["cognito-idp"].add_response("describe_user_pool_domain",
            {"DomainDescription": {"UserPoolId": POOL, "Domain": "umt-x-dev-111111111111"}},
            {"Domain": "umt-x-dev-111111111111"})

    def test_nested_toolkit_resolved_and_secret_never_returned(self):
        cfn = self.stubs["cloudformation"]
        cfn.add_response("describe_stack_resource", {"StackResourceDetail": {
            "LogicalResourceId": "Toolkit", "PhysicalResourceId": NESTED, "ResourceType": "AWS::CloudFormation::Stack",
            "LastUpdatedTimestamp": "2026-10-12T00:00:00Z", "ResourceStatus": "CREATE_COMPLETE"}},
            {"StackName": PARENT, "LogicalResourceId": "Toolkit"})
        cfn.add_response("describe_stacks", _stack(NESTED), {"StackName": NESTED})
        self._secret_and_pool()
        out = h.on_event(self.event, None)
        d = out["Data"]
        self.assertEqual(d["SecretArn"], SECRET_ARN)
        self.assertEqual(d["ClientId"], "cid123")
        self.assertEqual(d["TokenEndpoint"], TOKEN_ENDPOINT)
        self.assertEqual(d["ProxyBucket"], "umt-x-111111111111-us-west-2-proxy")
        self.assertEqual(d["IngestReadRoleArns"], "arn:aws:iam::111111111111:role/a,arn:aws:iam::111111111111:role/b")
        self.assertEqual(d["OidcDiscoveryUrl"],
                         f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}/.well-known/openid-configuration")
        self.assertNotIn(CLIENT_SECRET, json.dumps(out), "the client secret must never leave the handler")
        self.assertNotIn("clientSecret", json.dumps(out))
        for st in self.stubs.values():
            st.assert_no_pending_responses()

    def test_external_stack_read_directly_and_empty_roles_get_a_sentinel(self):
        self.event["ResourceProperties"].update(ToolkitStackName="my-toolkit", NestedLogicalId="")
        self.stubs["cloudformation"].add_response("describe_stacks", _stack(NESTED, roles=""), {"StackName": "my-toolkit"})
        self._secret_and_pool()
        d = h.on_event(self.event, None)["Data"]
        self.assertEqual(d["IngestReadRoleArns"], "arn:aws:iam::111111111111:role/aws-service-role/no-media-toolkit-readers")

    def test_unusable_stack_status_fails_the_deploy(self):
        self.event["ResourceProperties"]["NestedLogicalId"] = ""
        self.stubs["cloudformation"].add_response("describe_stacks", _stack(NESTED, status="ROLLBACK_COMPLETE"), {"StackName": PARENT})
        with self.assertRaisesRegex(RuntimeError, "ROLLBACK_COMPLETE"):
            h.on_event(self.event, None)

    def test_a_stack_that_is_not_the_toolkit_fails_loudly(self):
        self.event["ResourceProperties"]["NestedLogicalId"] = ""
        not_toolkit = {"Stacks": [{"StackId": NESTED, "StackName": "x", "CreationTime": "2026-10-12T00:00:00Z",
                                   "StackStatus": "CREATE_COMPLETE", "Outputs": []}]}
        self.stubs["cloudformation"].add_response("describe_stacks", not_toolkit, {"StackName": PARENT})
        with self.assertRaisesRegex(RuntimeError, "GatewayUrl"):
            h.on_event(self.event, None)

    def test_delete_reads_nothing(self):
        out = h.on_event({**self.event, "RequestType": "Delete", "PhysicalResourceId": "p"}, None)
        self.assertEqual(out, {"PhysicalResourceId": "p"})
        for st in self.stubs.values():
            st.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
