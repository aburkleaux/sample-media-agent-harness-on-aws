# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Write the built-in skill catalog to DynamoDB during `cdk deploy`.

The Skills tab's built-in panel paints from these rows instead of invoking the agent
runtime, and this is what puts them there — so a `SKILL.md` edit reaches the UI on a
deploy alone, with no container image to rebuild.

The catalog arrives as an S3 asset, NOT as custom-resource properties. Inlining the
bodies in the template pushed it past CloudFormation's hard 1 MB ceiling and failed the
deploy; see `builtin-skills.ts:stageBuiltinSkillCatalog`. So this handler is deliberately
dumb: every judgement call (which directories are skill packages, how a `description:`
is parsed out of frontmatter) happens at synth in `builtin-skills.ts`, where it is
covered by tests against the real packages. Here we only move bytes.

Reconciles rather than appends: a skill deleted from the repo has its row removed, or the
panel would keep offering a skill the harness no longer ships.
"""

import json
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")

#: Shared with the builtin TOOL projection the runtime publishes; see the stack.
CATALOG_PK = "GATEWAY#CATALOG"
SKILL_SK_PREFIX = "__skill__"

#: Stable, so an Update is an update. The asset key carries the content hash as a
#: property, so CloudFormation still sees a diff when a skill changes — but it does not
#: replace the resource, and there is no create-then-delete to race.
PHYSICAL_ID = "builtin-skill-catalog"


def _item(skill: dict) -> dict:
    return {
        "PK": {"S": CATALOG_PK},
        "SK": {"S": f"{SKILL_SK_PREFIX}{skill['name']}"},
        "name": {"S": skill["name"]},
        "description": {"S": skill.get("description", "")},
        "skillMd": {"S": skill["skillMd"]},
        # Always written, even empty: absent would read as "files unknown" downstream,
        # where empty means "this package is a lone SKILL.md".
        "supportingFiles": {"L": [{"S": f} for f in skill.get("supportingFiles", [])]},
    }


def _existing_skill_sks(table: str) -> set[str]:
    """Every `__skill__*` sort key currently in the table.

    Paginated: seven skills fit one page today, but a silently truncated read would
    resurrect deleted rows, and that failure is invisible.
    """
    found: set[str] = set()
    kwargs = {
        "TableName": table,
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": {"S": CATALOG_PK},
            ":sk": {"S": SKILL_SK_PREFIX},
        },
        "ProjectionExpression": "SK",
    }
    while True:
        page = dynamodb.query(**kwargs)
        found.update(i["SK"]["S"] for i in page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return found
        kwargs["ExclusiveStartKey"] = start


def handler(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    table = props["TableName"]

    # Nothing on Delete. The rows describe what the harness ships, and a stack teardown
    # takes the table (RETAIN) with it; deleting them here would also mean a rolled-back
    # update could clear the catalog while the old code is still serving it.
    if request_type == "Delete":
        logger.info("Delete: leaving the built-in skill catalog in place")
        return {"PhysicalResourceId": PHYSICAL_ID}

    body = s3.get_object(Bucket=props["CatalogBucket"], Key=props["CatalogKey"])
    skills = json.loads(body["Body"].read())["skills"]

    # An empty catalog means the synth-time read found no packages — a broken checkout or
    # a moved directory, not a harness that ships no skills. Writing it through would
    # delete every row and leave the panel claiming exactly that.
    if not skills:
        raise RuntimeError(
            f"refusing to write an empty built-in skill catalog "
            f"(s3://{props['CatalogBucket']}/{props['CatalogKey']})"
        )

    stale = _existing_skill_sks(table)
    for skill in skills:
        dynamodb.put_item(TableName=table, Item=_item(skill))
        stale.discard(f"{SKILL_SK_PREFIX}{skill['name']}")
        logger.info("wrote %s%s", SKILL_SK_PREFIX, skill["name"])

    # Written first, deleted second: the panel is briefly a superset rather than briefly
    # missing a skill somebody is looking at.
    for sk in sorted(stale):
        dynamodb.delete_item(
            TableName=table, Key={"PK": {"S": CATALOG_PK}, "SK": {"S": sk}}
        )
        logger.info("removed %s (no longer shipped)", sk)

    return {
        "PhysicalResourceId": PHYSICAL_ID,
        "Data": {"SkillCount": str(len(skills)), "Removed": str(len(stale))},
    }
