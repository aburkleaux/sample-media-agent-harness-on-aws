import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import boto3
from botocore.stub import Stubber, ANY

path = Path(__file__).resolve().parents[1] / 'lib/distribution/agent-registry-handler/index.py'
spec = importlib.util.spec_from_file_location('handler', path)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client('agent-registry-control', region_name='us-east-1', aws_access_key_id='testing', aws_secret_access_key='testing')
        self.event = {'RequestType': 'Create', 'StackId': 'stack', 'RequestId': 'request', 'ResourceProperties': {'RegistryName': 'test-source'}}
        self.detail = {'name': 'test-source', 'registryId': 'abcdefghijklmnop', 'registryArn': 'arn:aws:agent-registry:us-east-1:123456789012:registry/abcdefghijklmnop', 'status': 'READY', 'createdAt': 1, 'updatedAt': 1}

    def test_create_response_arn_and_ready(self):
        with Stubber(self.client) as stub, patch.object(h, '_control', return_value=self.client):
            stub.add_response('list_registries', {'registries': []}, {})
            stub.add_response('create_registry', {'registryArn': self.detail['registryArn']}, {'name': 'test-source', 'clientToken': ANY, 'approvalConfiguration': {'autoApprovalRules': ['APPROVE_ALL']}})
            stub.add_response('get_registry', self.detail, {'registryId': self.detail['registryId']})
            self.assertEqual(h.on_event(self.event, None)['Data']['RegistryId'], self.detail['registryId'])
            stub.assert_no_pending_responses()

    def test_failure_is_not_reported_as_success(self):
        with Stubber(self.client) as stub, patch.object(h, '_control', return_value=self.client):
            stub.add_response('list_registries', {'registries': []}, {})
            stub.add_response('create_registry', {'registryArn': self.detail['registryArn']})
            stub.add_response('get_registry', {**self.detail, 'status': 'CREATE_FAILED'})
            with self.assertRaises(RuntimeError):
                h.on_event(self.event, None)

    def test_delete_retains_without_contacting_registry(self):
        with patch.object(h, '_control', side_effect=AssertionError('unexpected client')):
            result = h.on_event({**self.event, 'RequestType': 'Delete', 'PhysicalResourceId': 'existing'}, None)
            self.assertEqual(result['PhysicalResourceId'], 'existing')


if __name__ == '__main__':
    unittest.main()
