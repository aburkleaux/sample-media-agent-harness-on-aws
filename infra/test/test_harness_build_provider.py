import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.stub import Stubber

spec = importlib.util.spec_from_file_location('build_handler', Path(__file__).resolve().parents[1] / 'lib/distribution/harness-build-handler/index.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class BuildProviderTests(unittest.TestCase):
    def setUp(self):
        self.event = {'RequestType': 'Create', 'LogicalResourceId': 'UiBuild', 'StackId': 'stack', 'RequestId': 'request', 'ResourceProperties': {'ProjectName': 'ui', 'Revision': 'a' * 64}}
        self.client = Mock()
        self.client.start_build.return_value = {'build': {'id': 'ui:123'}}
        self.patcher = patch.object(h.boto3, 'client', return_value=self.client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_start_is_idempotent_and_outputs_follow_request(self):
        first = h.on_event(self.event, None)
        params = self.client.start_build.call_args
        second = h.on_event(self.event, None)
        self.assertEqual(first, second)
        self.assertEqual(params, self.client.start_build.call_args)
        self.assertEqual(first['Data']['BuildId'], 'ui:123')
        self.assertEqual(first['Data']['SpaKey'], 'ui/' + first['Data']['ImageTag'] + '.zip')
        updated = h.on_event({**self.event, 'RequestType': 'Update', 'RequestId': 'next', 'PhysicalResourceId': 'UiBuild'}, None)
        self.assertEqual(first['PhysicalResourceId'], updated['PhysicalResourceId'])
        self.assertNotEqual(first['Data']['ImageTag'], updated['Data']['ImageTag'])

    def test_requests_match_real_codebuild_api_shapes(self):
        # Escape the module-wide mock only while constructing an offline SDK client.
        self.patcher.stop()
        client = boto3.client('codebuild', region_name='us-east-1',
                              aws_access_key_id='testing', aws_secret_access_key='testing')
        self.patcher.start()
        token = hashlib.sha256(b'stackrequest').hexdigest()
        tag = 'a' * 32 + '-' + token[:24]
        with Stubber(client) as stub, patch.object(h.boto3, 'client', return_value=client):
            stub.add_response('start_build', {'build': {'id': 'ui:123'}}, {
                'projectName': 'ui', 'idempotencyToken': token,
                'environmentVariablesOverride': [{'name': 'IMAGE_TAG', 'value': tag, 'type': 'PLAINTEXT'}],
            })
            result = h.on_event(self.event, None)
            stub.add_response('batch_get_builds', {'builds': [{'id': 'ui:123', 'buildStatus': 'SUCCEEDED'}]}, {'ids': ['ui:123']})
            self.assertEqual(h.is_complete({**self.event, **result}, None), {'IsComplete': True})
            stub.assert_no_pending_responses()

    def test_poll_waits_then_succeeds(self):
        event = {**self.event, **h.on_event(self.event, None)}
        for status, complete in [('IN_PROGRESS', False), ('SUCCEEDED', True)]:
            self.client.batch_get_builds.return_value = {'builds': [{'buildStatus': status}]}
            self.assertEqual(h.is_complete(event, None), {'IsComplete': complete})
        self.client.batch_get_builds.assert_called_with(ids=['ui:123'])

    def test_terminal_failures_and_missing_build_fail_deployment(self):
        event = {**self.event, 'Data': {'BuildId': 'ui:123'}}
        for status in ['FAILED', 'FAULT', 'STOPPED', 'TIMED_OUT']:
            self.client.batch_get_builds.return_value = {'builds': [{'buildStatus': status}]}
            with self.assertRaisesRegex(RuntimeError, status):
                h.is_complete(event, None)
        self.client.batch_get_builds.return_value = {'builds': []}
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            h.is_complete(event, None)

    def test_delete_never_starts_build_or_removes_artifacts(self):
        event = {**self.event, 'RequestType': 'Delete', 'PhysicalResourceId': 'existing'}
        self.assertEqual(h.on_event(event, None), {'PhysicalResourceId': 'existing'})
        self.assertEqual(h.is_complete(event, None), {'IsComplete': True})
        self.assertEqual(self.client.mock_calls, [])
