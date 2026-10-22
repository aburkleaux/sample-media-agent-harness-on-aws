"""HybridSearchRoleLookup custom-resource handler: the toolkit's hybrid-search role, by exact ARN.

Real boto3 clients under botocore's Stubber (requests validated against the service models — no
credentials, no network). Shapes mirror the live toolkit v0.1.0 tree (read 2026-10-22):
toolkit -> nested HybridSearchStack -> AWS::IAM::Role HybridSearchLambdaRole, path /umt-<id>/.
Run: `uv run --with boto3 python -m pytest infra/test/test_hybrid_search_role_lookup.py -q`.
"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

spec = importlib.util.spec_from_file_location(
    "hybrid_search_role", Path(__file__).resolve().parents[1] / "lib/toolkit/hybrid-search-role-handler/index.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

TOOLKIT = "arn:aws:cloudformation:us-east-1:111111111111:stack/mediaworks-harness-media-toolkit-Toolkit-ABC/uuid-1"
HS = "arn:aws:cloudformation:us-east-1:111111111111:stack/mediaworks-harness-media-toolkit-Toolkit-ABC-HybridSearchStack-XYZ/uuid-2"
OTHER = "arn:aws:cloudformation:us-east-1:111111111111:stack/mediaworks-harness-media-toolkit-Toolkit-ABC-TtsStack-Q/uuid-3"
ROLE_NAME = "mediaworks-harness-media-toolkit-T-HybridSearchLambdaRole-1r4DHmPN"
ROLE_ARN = f"arn:aws:iam::111111111111:role/umt-0e6bdd0c5e97/{ROLE_NAME}"


def _client(service):
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="t", aws_secret_access_key="t")


def _summary(logical, rtype, physical, status="CREATE_COMPLETE"):
    return {"LogicalResourceId": logical, "PhysicalResourceId": physical, "ResourceType": rtype,
            "LastUpdatedTimestamp": "2026-10-22T00:00:00Z", "ResourceStatus": status}


def _page(*resources):
    return {"StackResourceSummaries": list(resources)}


class HybridSearchRoleLookupTests(unittest.TestCase):
    def setUp(self):
        self.clients = {s: _client(s) for s in ("cloudformation", "iam")}
        self.stubs = {s: Stubber(c) for s, c in self.clients.items()}
        for st in self.stubs.values():
            st.activate()
        patcher = patch.object(h.boto3, "client", side_effect=lambda s, **_: self.clients[s])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.event = {"RequestType": "Create", "ResourceProperties": {
            "ToolkitStackName": TOOLKIT, "NestedLogicalId": "", "RoleLogicalId": "HybridSearchLambdaRole",
            "Revision": "r1"}}

    def _get_role(self):
        self.stubs["iam"].add_response("get_role", {"Role": {
            "Path": "/umt-0e6bdd0c5e97/", "RoleName": ROLE_NAME, "RoleId": "AROAEXAMPLEEXAMPLE1234",
            "Arn": ROLE_ARN, "CreateDate": "2026-10-22T00:00:00Z"}}, {"RoleName": ROLE_NAME})

    def test_role_in_the_nested_hybrid_search_stack_resolves_to_its_exact_arn(self):
        cfn = self.stubs["cloudformation"]
        cfn.add_response("list_stack_resources", _page(
            _summary("TtsStack", "AWS::CloudFormation::Stack", OTHER),
            _summary("HybridSearchStack", "AWS::CloudFormation::Stack", HS)), {"StackName": TOOLKIT})
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchSFNRole", "AWS::IAM::Role", "x-HybridSearchSFNRole-1"),
            _summary("HybridSearchLambdaRole", "AWS::IAM::Role", ROLE_NAME)), {"StackName": HS})
        self._get_role()
        out = h.on_event(self.event, None)
        self.assertEqual(out["Data"]["RoleArn"], ROLE_ARN, "ARN carries the toolkit's /umt-<id>/ path")
        for st in self.stubs.values():
            st.assert_no_pending_responses()

    def test_parent_stack_with_nested_logical_id_is_resolved_first(self):
        self.event["ResourceProperties"].update(ToolkitStackName="mediaworks-harness-media-toolkit", NestedLogicalId="Toolkit")
        cfn = self.stubs["cloudformation"]
        cfn.add_response("describe_stack_resource", {"StackResourceDetail": {
            "LogicalResourceId": "Toolkit", "PhysicalResourceId": TOOLKIT, "ResourceType": "AWS::CloudFormation::Stack",
            "LastUpdatedTimestamp": "2026-10-22T00:00:00Z", "ResourceStatus": "CREATE_COMPLETE"}},
            {"StackName": "mediaworks-harness-media-toolkit", "LogicalResourceId": "Toolkit"})
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchStack", "AWS::CloudFormation::Stack", HS)), {"StackName": TOOLKIT})
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchLambdaRole", "AWS::IAM::Role", ROLE_NAME)), {"StackName": HS})
        self._get_role()
        self.assertEqual(h.on_event(self.event, None)["Data"]["RoleArn"], ROLE_ARN)

    def test_role_moved_to_another_nested_stack_is_still_found(self):
        cfn = self.stubs["cloudformation"]
        cfn.add_response("list_stack_resources", _page(
            _summary("TtsStack", "AWS::CloudFormation::Stack", OTHER)), {"StackName": TOOLKIT})
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchLambdaRole", "AWS::IAM::Role", ROLE_NAME)), {"StackName": OTHER})
        self._get_role()
        self.assertEqual(h.on_event(self.event, None)["Data"]["RoleArn"], ROLE_ARN)

    def test_no_role_anywhere_fails_the_deploy_loudly(self):
        cfn = self.stubs["cloudformation"]
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchStack", "AWS::CloudFormation::Stack", HS)), {"StackName": TOOLKIT})
        cfn.add_response("list_stack_resources", _page(
            _summary("SomethingElse", "AWS::IAM::Role", "n")), {"StackName": HS})
        with self.assertRaisesRegex(RuntimeError, "no AWS::IAM::Role 'HybridSearchLambdaRole'.*template changed"):
            h.on_event(self.event, None)

    def test_ambiguous_matches_fail_rather_than_guess(self):
        a = OTHER
        b = OTHER.replace("TtsStack", "OtherStack")
        cfn = self.stubs["cloudformation"]
        cfn.add_response("list_stack_resources", _page(
            _summary("TtsStack", "AWS::CloudFormation::Stack", a),
            _summary("OtherStack", "AWS::CloudFormation::Stack", b)), {"StackName": TOOLKIT})
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchLambdaRole", "AWS::IAM::Role", "r1")), {"StackName": b})
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchLambdaRole", "AWS::IAM::Role", "r2")), {"StackName": a})
        with self.assertRaisesRegex(RuntimeError, "several nested stacks"):
            h.on_event(self.event, None)

    def test_deleted_role_is_not_a_match(self):
        cfn = self.stubs["cloudformation"]
        cfn.add_response("list_stack_resources", _page(
            _summary("HybridSearchLambdaRole", "AWS::IAM::Role", ROLE_NAME, status="DELETE_COMPLETE")),
            {"StackName": TOOLKIT})
        with self.assertRaises(RuntimeError):
            h.on_event(self.event, None)

    def test_delete_reads_nothing(self):
        out = h.on_event({**self.event, "RequestType": "Delete", "PhysicalResourceId": "p"}, None)
        self.assertEqual(out, {"PhysicalResourceId": "p"})
        for st in self.stubs.values():
            st.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
