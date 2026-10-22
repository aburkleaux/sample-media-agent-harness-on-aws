"""Start deployment builds and poll without holding a Lambda open.

No cleanup on Delete: consumers may still be rolling back to retained artifacts.
A stable physical ID avoids replacement/deletion on each application update.
"""
import hashlib

import boto3


def on_event(event, _context):
    physical_id = event.get('PhysicalResourceId') or event['LogicalResourceId']
    if event['RequestType'] == 'Delete':
        return {'PhysicalResourceId': physical_id}
    props = event['ResourceProperties']
    token = hashlib.sha256((event['StackId'] + event['RequestId']).encode()).hexdigest()
    tag = props['Revision'][:32] + '-' + token[:24]
    build = boto3.client('codebuild').start_build(
        projectName=props['ProjectName'], idempotencyToken=token,
        environmentVariablesOverride=[{'name': 'IMAGE_TAG', 'value': tag, 'type': 'PLAINTEXT'}],
    )['build']
    return {
        'PhysicalResourceId': physical_id,
        'Data': {'BuildId': build['id'], 'ImageTag': tag, 'SpaKey': f'ui/{tag}.zip'},
    }


def is_complete(event, _context):
    if event['RequestType'] == 'Delete':
        return {'IsComplete': True}
    build_id = event['Data']['BuildId']
    builds = boto3.client('codebuild').batch_get_builds(ids=[build_id])['builds']
    if not builds:
        raise RuntimeError(f'CodeBuild build not found: {build_id}')
    build = builds[0]
    status = build['buildStatus']
    if status == 'IN_PROGRESS':
        return {'IsComplete': False}
    if status != 'SUCCEEDED':
        raise RuntimeError(f"CodeBuild {build_id} ended with {status}; logs: {build.get('logs', {}).get('deepLink', 'see CodeBuild console')}")
    return {'IsComplete': True}
