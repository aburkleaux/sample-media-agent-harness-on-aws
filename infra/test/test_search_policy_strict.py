"""media-harness-projects-access task 2.3 (REQUIRED): strict Cedar schema-validation of the
Search_Policy builder's output, in CI, before `cdk deploy`.

The Search_Gateway's policy engine is created with `validationMode: FAIL_ON_ANY_FINDINGS`, so a
statement Cedar's strict validator flags is rejected at `CreatePolicy` — i.e. at deploy. This test
runs that same strict validation at build time against the builder's emitted output, so a bad
statement fails the build instead.

Inputs come from `fixtures/search-policy.generated.json`, written by `emit-search-policy.mjs`
(run it first; CI runs both). The schema below is the AgentCore-style gateway schema from
`docs/prototypes/media-harness-projects/validation/test_v1_cedar_policy.py`, with its single action
keyed on the fixture's `searchToolName`.

Run (no package gains a dependency):
    uv run --with cedarpy --with pytest pytest infra/test/test_search_policy_strict.py
"""
import json
import pathlib

import cedarpy
import pytest

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "search-policy.generated.json"


def _load():
    assert FIXTURE.exists(), (
        f"{FIXTURE} not found — run `node infra/test/emit-search-policy.mjs` "
        "(npm run test:emit-search-policy) first"
    )
    return json.loads(FIXTURE.read_text())


FIX = _load()


def _schema(tool_name: str, image_arg: str = "search_image_s3uri") -> str:
    # Emulates what AgentCore generates from the M2C search_content inputSchema: collection_id /
    # search_term / search_image_s3uri are optional strings, k an optional Long, none required.
    schema = {
        "AgentCore": {
            "entityTypes": {
                "OAuthUser": {
                    "shape": {"type": "Record", "attributes": {"id": {"type": "String"}}},
                    "tags": {"type": "String"},
                },
                "Gateway": {"shape": {"type": "Record", "attributes": {}}},
            },
            "commonTypes": {
                "SearchInput": {
                    "type": "Record",
                    "attributes": {
                        "collection_id": {"type": "String", "required": False},
                        "search_term": {"type": "String", "required": False},
                        image_arg: {"type": "String", "required": False},
                        "k": {"type": "Long", "required": False},
                    },
                }
            },
            "actions": {
                tool_name: {
                    "appliesTo": {
                        "principalTypes": ["OAuthUser"],
                        "resourceTypes": ["Gateway"],
                        "context": {
                            "type": "Record",
                            "attributes": {"input": {"type": "SearchInput", "required": True}},
                        },
                    }
                }
            },
        }
    }
    return json.dumps(schema)


SCHEMA = _schema(FIX["searchToolName"])


def test_fixture_covers_every_static_env_plus_sample():
    envs = {c["env"] for c in FIX["cases"]}
    assert {"staging", "prod", "sandbox", "sample"} <= envs, envs
    assert len(FIX["cases"]) >= 4


@pytest.mark.parametrize("case", FIX["cases"], ids=[c["env"] for c in FIX["cases"]])
def test_emitted_statement_validates_strictly(case):
    # Sanity: the emitted statement uses the guarded form, never the negated-disjunction form.
    assert "if context.input has search_image_s3uri" in case["policy"]
    assert "search_image_uri" not in case["policy"]
    assert "!(" not in case["policy"]
    result = cedarpy.validate_policies(case["policy"], SCHEMA)
    assert result.validation_passed, f'{case["env"]}: {result.errors}'


def test_regression_negated_disjunction_form_fails_strict_validation():
    # The design's `!(context.input has search_image_s3uri) || …` form is semantically equivalent but
    # the strict validator cannot guarantee the optional-attribute access is safe, so it is rejected
    # under FAIL_ON_ANY_FINDINGS. This guards against anyone "simplifying" the builder back to it.
    reg = FIX["regression"]
    assert "!(" in reg["policy"]
    assert reg["policy"] != FIX["cases"][0]["policy"]
    result = cedarpy.validate_policies(reg["policy"], SCHEMA)
    assert not result.validation_passed, "the negated-disjunction form must FAIL strict validation"


# ---- The permit must name the spelling the upstream declares (why SearchTargetSync refuses the old one) ----

SCHEMA_OLD = _schema(FIX["searchToolName"], "search_image_uri")


def _decide(policy, schema, image_arg, image):
    # Evaluate the policy for one request whose input carries `image_arg`.
    tool = FIX["searchToolName"]
    request = {
        "principal": 'AgentCore::OAuthUser::"u"',
        "action": f'AgentCore::Action::"{tool}"',
        "resource": f'AgentCore::Gateway::"{FIX["gatewayArn"]}"',
        "context": {"input": {"collection_id": "p1", image_arg: image}},
    }
    entities = [
        {"uid": {"type": "AgentCore::OAuthUser", "id": "u"}, "attrs": {"id": "u"}, "parents": [],
         "tags": {"project_id": "p1"}},
        {"uid": {"type": "AgentCore::Gateway", "id": FIX["gatewayArn"]}, "attrs": {}, "parents": []},
    ]
    return cedarpy.is_authorized(request, policy, entities, schema=schema).allowed


def test_the_image_input_is_constrained():
    case = FIX["cases"][0]
    rendition = f's3://{case["ingestBucket"]}/projects/p1/image:1/rendition.jpg'
    assert _decide(case["policy"], SCHEMA, "search_image_s3uri", rendition)
    # an original in the same bucket is not a Search_Rendition, so M2C may not be asked to read it
    assert not _decide(case["policy"], SCHEMA, "search_image_s3uri", f's3://{case["ingestBucket"]}/projects/p1/a.jpg')
    assert not _decide(case["policy"], SCHEMA, "search_image_s3uri", "s3://some-other-bucket/a.jpg")


def test_a_permit_naming_another_spelling_validates_but_leaves_the_declared_input_open():
    # The builder's permit against a pre-rename upstream (search_image_uri): strict validation passes, so
    # CreatePolicy would accept it, yet an image from any bucket is permitted. Hence the sync refuses it.
    case = FIX["cases"][0]
    assert cedarpy.validate_policies(case["policy"], SCHEMA_OLD).validation_passed
    assert _decide(case["policy"], SCHEMA_OLD, "search_image_uri", "s3://some-other-bucket/a.jpg")
    # ...and the mirror image, pinned from the emitted fixture.
    other = FIX["otherName"]["policy"]
    assert "search_image_s3uri" not in other
    assert cedarpy.validate_policies(other, SCHEMA).validation_passed
    assert _decide(other, SCHEMA, "search_image_s3uri", "s3://some-other-bucket/a.jpg")
