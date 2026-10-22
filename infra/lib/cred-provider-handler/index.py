"""Custom Resource Lambda for managing OAuth2 Credential Provider lifecycle.

Creates/updates/deletes an OAuth2 Credential Provider in AgentCore Identity.
Reads client secret from Secrets Manager to avoid logging sensitive data.
"""

import json
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

bedrock_client = boto3.client("bedrock-agentcore-control")
secrets_client = boto3.client("secretsmanager")


def resolve_client_secret(props):
    """Read the client secret from Secrets Manager.

    By default the SecretString IS the client secret. When ``SecretJsonKey`` is set (M2C's
    ``gateway-credentials`` bundle, which stores several fields as one JSON object — the same bundle
    ``m2c-gateway-seed`` parses), the SecretString is parsed as JSON and that key is returned. A
    missing key raises so a misconfiguration fails the deploy rather than registering a provider with
    an empty secret (media-harness-projects-access task 8.6).
    """
    secret = secrets_client.get_secret_value(SecretId=props["ClientSecretArn"])
    secret_string = secret["SecretString"]
    json_key = props.get("SecretJsonKey")
    if not json_key:
        return secret_string
    bundle = json.loads(secret_string)
    if json_key not in bundle:
        raise ValueError(
            f"SecretJsonKey '{json_key}' is not present in the credentials bundle "
            f"{props['ClientSecretArn']}"
        )
    return bundle[json_key]


def handler(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    logger.info("Request type: %s, Provider: %s", request_type, props.get("ProviderName"))

    try:
        if request_type == "Create":
            return handle_create(props)
        elif request_type == "Update":
            return handle_update(event, props)
        elif request_type == "Delete":
            return handle_delete(event, props)
        else:
            raise ValueError(f"Unknown request type: {request_type}")
    except Exception as e:
        logger.error("Error handling %s: %s", request_type, e, exc_info=True)
        raise


def handle_create(props):
    client_secret = resolve_client_secret(props)

    response = bedrock_client.create_oauth2_credential_provider(
        name=props["ProviderName"],
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "clientId": props["ClientId"],
                "clientSecret": client_secret,
                "oauthDiscovery": {"discoveryUrl": props["DiscoveryUrl"]},
            }
        },
    )

    provider_arn = response["credentialProviderArn"]
    logger.info("Created provider: %s", provider_arn)

    return {
        "PhysicalResourceId": props["ProviderName"],
        "Data": {"ProviderArn": provider_arn},
    }


def handle_update(event, props):
    provider_name = event["PhysicalResourceId"]
    client_secret = resolve_client_secret(props)

    response = bedrock_client.update_oauth2_credential_provider(
        name=provider_name,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "clientId": props["ClientId"],
                "clientSecret": client_secret,
                "oauthDiscovery": {"discoveryUrl": props["DiscoveryUrl"]},
            }
        },
    )

    provider_arn = response["credentialProviderArn"]
    logger.info("Updated provider: %s", provider_arn)

    return {
        "PhysicalResourceId": provider_name,
        "Data": {"ProviderArn": provider_arn},
    }


def handle_delete(event, props):
    provider_name = event["PhysicalResourceId"]
    try:
        bedrock_client.delete_oauth2_credential_provider(name=provider_name)
        logger.info("Deleted provider: %s", provider_name)
    except bedrock_client.exceptions.ResourceNotFoundException:
        logger.warning("Provider already deleted: %s", provider_name)
    except Exception as e:
        logger.error("Error deleting: %s", e)
        raise

    return {"PhysicalResourceId": provider_name}
