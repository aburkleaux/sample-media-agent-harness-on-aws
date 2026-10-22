"""Configuration must fail closed (R9, R14, Property 7).

Two classes of failure this covers:

* **Cross-environment configuration.** A staging task handed prod's registry
  must refuse to start, not project into it. IAM resource scoping is the real
  enforcement; this is the cheap check that catches the typo first.
* **Half-configured cutover.** A category set to ``registry`` with no registry
  id would otherwise serve an empty tab that looks like "you have no skills".
"""

from __future__ import annotations

import pytest

from capability.config import (
    DEFAULT_MODE,
    DEFAULT_SOURCE_TIMEOUT_SECONDS,
    ConfigError,
    load_config,
)


def _env(**overrides: str) -> dict[str, str]:
    env = {"DEPLOY_ENV": "sandbox", "ASSET_INDEX_TABLE": "assets"}
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Required configuration
# ---------------------------------------------------------------------------


def test_deploy_env_is_required():
    with pytest.raises(ConfigError, match="DEPLOY_ENV"):
        load_config({"ASSET_INDEX_TABLE": "assets"})


def test_asset_index_table_is_required():
    """The canonical store is not optional.

    Failing here is the difference between "empty inventory" and authorization
    computed from an incomplete source; R2 forbids reconstructing it from S3.
    """
    with pytest.raises(ConfigError, match="ASSET_INDEX_TABLE"):
        load_config({"DEPLOY_ENV": "sandbox"})


def test_defaults_are_legacy_and_registry_unconfigured():
    config = load_config(_env())
    assert config.registry_configured is False
    assert config.projection_enabled is False
    assert config.shadow_sample_percent == 0
    assert config.source_timeout_seconds == DEFAULT_SOURCE_TIMEOUT_SECONDS
    for category in ("skill", "specialty-agent", "tool", "tool-collection", "workflow", "hook"):
        assert config.mode(category) == DEFAULT_MODE
        assert config.registry_reads_enabled(category) is False


# ---------------------------------------------------------------------------
# Cross-environment isolation (Property 7)
# ---------------------------------------------------------------------------


def test_registry_from_another_environment_is_rejected():
    with pytest.raises(ConfigError, match="does not belong to"):
        load_config(
            _env(
                REGISTRY_ID="reg-prod",
                REGISTRY_NAME="mediaworks-harness-capabilities-prod",
            )
        )


def test_matching_environment_registry_is_accepted():
    config = load_config(
        _env(
            REGISTRY_ID="reg-1",
            REGISTRY_NAME="mediaworks-harness-capabilities-sandbox",
        )
    )
    assert config.registry_configured is True
    assert config.registry_id == "reg-1"


def test_registry_id_without_a_name_is_allowed():
    """The name is the cheap guard, not the enforcement.

    A registry id alone is a legitimate configuration (the id is what the API
    calls take); IAM scoping still confines it to one registry.
    """
    config = load_config(_env(REGISTRY_ID="reg-1"))
    assert config.registry_configured is True


# ---------------------------------------------------------------------------
# Feature flags (R14)
# ---------------------------------------------------------------------------


def test_registry_mode_requires_a_registry_id():
    with pytest.raises(ConfigError, match="REGISTRY_ID is unset"):
        load_config(_env(CAPABILITY_SOURCE_SKILLS="registry"))


def test_registry_mode_enables_reads_for_that_category_only():
    config = load_config(
        _env(
            REGISTRY_ID="reg-1",
            REGISTRY_NAME="mediaworks-harness-capabilities-sandbox",
            CAPABILITY_SOURCE_SKILLS="registry",
        )
    )
    assert config.registry_reads_enabled("skill") is True
    assert config.registry_reads_enabled("specialty-agent") is False
    assert config.mode("workflow") == "legacy"


def test_shadow_mode_does_not_enable_registry_reads_on_the_served_path():
    """Shadow serves legacy and compares out of band.

    If shadow enabled Registry reads on the served path it would be measuring
    itself, and a parity report would prove nothing.
    """
    config = load_config(
        _env(REGISTRY_ID="reg-1", CAPABILITY_SOURCE_TOOLS="shadow")
    )
    assert config.mode("tool") == "shadow"
    assert config.registry_reads_enabled("tool") is False


def test_canonical_mode_serves_from_the_api_without_touching_the_registry():
    """The distinction the whole mode exists for.

    `canonical` moves a category off the Runtime — which is where the latency
    win is — while leaving the Registry out of the served answer. If these two
    ever collapse back into one flag, a category could not be made fast without
    also being asked to trust projected data the same day.
    """
    config = load_config(
        _env(
            REGISTRY_ID="reg-1",
            REGISTRY_NAME="mediaworks-harness-capabilities-sandbox",
            CAPABILITY_SOURCE_AGENTS="canonical",
        )
    )
    assert config.mode("specialty-agent") == "canonical"
    assert config.capability_api_serves("specialty-agent") is True
    # The point: a registry IS configured, and reads are still off.
    assert config.registry_configured is True
    assert config.registry_reads_enabled("specialty-agent") is False


def test_canonical_mode_does_not_require_a_registry_id():
    """Unlike `registry`, it reads nothing from the Registry, so it needs no id.

    Requiring one would couple the latency fix to provisioning a registry.
    """
    config = load_config(_env(CAPABILITY_SOURCE_AGENTS="canonical"))
    assert config.capability_api_serves("specialty-agent") is True
    assert config.registry_reads_enabled("specialty-agent") is False


def test_only_the_capability_api_modes_take_a_category_off_the_runtime():
    for mode, served in [
        ("legacy", False),
        ("shadow", False),
        ("canonical", True),
        ("registry", True),
    ]:
        config = load_config(
            _env(
                REGISTRY_ID="reg-1",
                REGISTRY_NAME="mediaworks-harness-capabilities-sandbox",
                CAPABILITY_SOURCE_AGENTS=mode,
            )
        )
        assert config.capability_api_serves("specialty-agent") is served, mode
        # Registry rows reach a user in exactly one mode.
        assert config.registry_reads_enabled("specialty-agent") is (mode == "registry")


def test_canonical_is_exposed_to_the_browser_verbatim():
    """The SPA branches on this value, so it must not be normalized away."""
    config = load_config(_env(CAPABILITY_SOURCE_AGENTS="canonical"))
    assert config.to_public_json()["capabilitySources"]["specialty-agent"] == "canonical"


def test_unknown_mode_is_a_startup_error_not_a_silent_default():
    with pytest.raises(ConfigError, match="not one of"):
        load_config(_env(REGISTRY_ID="reg-1", CAPABILITY_SOURCE_SKILLS="registryy"))


def test_projection_enabled_requires_a_registry_id():
    with pytest.raises(ConfigError, match="requires REGISTRY_ID"):
        load_config(_env(REGISTRY_PROJECTION_ENABLED="true"))


@pytest.mark.parametrize("value", ["-1", "101", "1000"])
def test_shadow_sample_percent_is_range_checked(value):
    with pytest.raises(ConfigError, match="within 0..100"):
        load_config(_env(REGISTRY_SHADOW_SAMPLE_PERCENT=value))


def test_source_timeout_must_be_positive():
    with pytest.raises(ConfigError, match="must be positive"):
        load_config(_env(CAPABILITY_SOURCE_TIMEOUT_SECONDS="0"))


def test_public_config_exposes_flags_but_no_identifiers():
    """`/control-plane/config` is unauthenticated.

    It may say *which* source a category uses, but not the registry id or table
    names — those are configuration facts an unauthenticated probe should not
    collect.
    """
    config = load_config(
        _env(REGISTRY_ID="reg-secret", CAPABILITY_SOURCE_SKILLS="shadow")
    )
    public = config.to_public_json()
    assert public["capabilitySources"]["skill"] == "shadow"
    assert public["registryConfigured"] is True
    serialized = repr(public)
    assert "reg-secret" not in serialized
    assert "assets" not in serialized
