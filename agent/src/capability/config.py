"""Environment-scoped configuration for the capability control plane.

Everything here is read from the process environment so the same container
image and the same Lambda asset serve every environment (R9/R14). Two rules
are enforced rather than documented:

1. **Fail closed on cross-environment configuration.** A registry whose name
   does not end in ``-<DEPLOY_ENV>`` is rejected at load time, so a staging
   task handed the production registry id refuses to start instead of
   projecting into it (Property 7). IAM resource scoping is the real
   enforcement; this is the cheap check that catches the typo first.
2. **Fail closed on missing configuration.** Projection and Registry reads
   are skipped — not guessed at — when their configuration is absent, and the
   Capability API reports the source as unavailable rather than falling back
   to the AgentCore Runtime (R12/Property 5).

Feature flags are per-category and default to ``legacy`` (R14): until an
operator sets one to ``shadow`` or ``registry``, nothing about the UI's data
source changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .dto import CATEGORIES

#: Cutover modes (R14). ``legacy`` = existing Runtime/lab path; ``shadow`` =
#: serve legacy and compare asynchronously; ``canonical`` = serve from the
#: Capability API, canonical sources only; ``registry`` = serve the merged
#: Capability API result including Registry rows.
#:
#: ``canonical`` is an addition to the spec's three modes, because those weld
#: two independent decisions to one value: *which service answers the read* and
#: *whether Registry rows are in the answer*. The latency win the migration
#: exists for comes entirely from the first — an always-on Fargate task reading
#: DynamoDB directly instead of cold-starting an agent runtime to read the same
#: DynamoDB — and it is available before the Registry is trusted to serve.
#:
#: Keeping them separate also keeps the shadow-parity gate honest: under
#: ``canonical`` no Registry row reaches a user, so there is nothing for parity
#: to be wrong about, and a Registry outage cannot raise a "partial results"
#: banner over an agent list that is in fact complete. Promoting a category from
#: ``canonical`` to ``registry`` is then the *only* step that needs that gate.
#: See ``docs/agent-registry-control-plane/canonical-mode-addendum.md``.
MODES: frozenset[str] = frozenset({"legacy", "shadow", "canonical", "registry"})

#: Modes served by the Capability API rather than the Runtime. ``registry``
#: additionally merges Registry rows; see :meth:`CapabilityConfig.mode`.
CAPABILITY_API_MODES: frozenset[str] = frozenset({"canonical", "registry"})

DEFAULT_MODE: str = "legacy"

#: Per-source read timeout. R13 budgets p95 ≤ 2s for a category list, and a
#: source that blows the budget must degrade to a partial error rather than
#: hold the whole page.
DEFAULT_SOURCE_TIMEOUT_SECONDS: float = 2.0

#: ``CAPABILITY_SOURCE_<SUFFIX>`` env var per category.
_FLAG_SUFFIX: dict[str, str] = {
    "skill": "SKILLS",
    "specialty-agent": "AGENTS",
    "tool": "TOOLS",
    "tool-collection": "TOOL_COLLECTIONS",
    "workflow": "WORKFLOWS",
    "hook": "HOOKS",
}


class ConfigError(RuntimeError):
    """Configuration is missing or belongs to another environment.

    Raised at load time (startup / cold start), never mid-request, so a
    misconfigured task fails its health check instead of serving one
    environment's data from another's registry.
    """


def _flag_env_name(category: str) -> str:
    suffix = _FLAG_SUFFIX.get(category)
    if suffix is None:
        raise ValueError(f"no feature flag defined for category {category!r}")
    return f"CAPABILITY_SOURCE_{suffix}"


def _read_mode(category: str, environ: dict[str, str]) -> str:
    raw = (environ.get(_flag_env_name(category)) or "").strip().lower()
    if not raw:
        return DEFAULT_MODE
    if raw not in MODES:
        raise ConfigError(
            f"{_flag_env_name(category)}={raw!r} is not one of {sorted(MODES)}"
        )
    return raw


def _read_float(name: str, default: float, environ: dict[str, str]) -> float:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as err:
        raise ConfigError(f"{name}={raw!r} is not a number") from err
    if value <= 0:
        raise ConfigError(f"{name}={raw!r} must be positive")
    return value


def _read_int(name: str, default: int, environ: dict[str, str]) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as err:
        raise ConfigError(f"{name}={raw!r} is not an integer") from err
    return value


@dataclass(frozen=True)
class CapabilityConfig:
    """Resolved configuration for one process in one environment."""

    deploy_env: str
    region: str
    asset_index_table: str
    projection_state_table: Optional[str] = None
    gateway_registry_table: Optional[str] = None
    improve_table: Optional[str] = None
    artifacts_bucket: Optional[str] = None
    registry_id: Optional[str] = None
    registry_name: Optional[str] = None
    projection_enabled: bool = False
    shadow_sample_percent: int = 0
    source_timeout_seconds: float = DEFAULT_SOURCE_TIMEOUT_SECONDS
    user_pool_issuer: Optional[str] = None
    user_pool_client_id: Optional[str] = None
    modes: dict[str, str] = field(default_factory=dict)

    # -- derived -------------------------------------------------------

    @property
    def registry_configured(self) -> bool:
        """Whether Registry reads/writes are possible at all.

        A missing registry id is a normal state before Task 8 has run in an
        environment: the Registry source is simply skipped and reported, and
        no Runtime call substitutes for it (R12).
        """
        return bool(self.registry_id)

    def mode(self, category: str) -> str:
        """Cutover mode for ``category``; ``legacy`` unless explicitly set."""
        return self.modes.get(category, DEFAULT_MODE)

    def registry_reads_enabled(self, category: str) -> bool:
        """Only ``registry`` mode lets Registry rows into a served response.

        ``shadow`` queries the Capability API out of band for parity metrics,
        so its Registry reads happen on the comparison path, not this one.
        ``canonical`` is served by the Capability API but from canonical state
        alone — the Registry is deliberately not consulted, so this stays False.
        """
        return self.registry_configured and self.mode(category) == "registry"

    def capability_api_serves(self, category: str) -> bool:
        """Whether the Capability API (not the Runtime) answers this category.

        The flag the SPA actually branches on. Separate from
        :meth:`registry_reads_enabled` so a category can be moved off the
        Runtime — which is the whole latency win — without also being asked to
        trust the Registry as a source on the same day.
        """
        return self.mode(category) in CAPABILITY_API_MODES

    def to_public_json(self) -> dict[str, object]:
        """Config safe to hand the browser: flags and environment only."""
        return {
            "deployEnv": self.deploy_env,
            "capabilitySources": dict(self.modes),
            "registryConfigured": self.registry_configured,
        }


def load_config(environ: Optional[dict[str, str]] = None) -> CapabilityConfig:
    """Read and validate configuration from the environment.

    Raises:
        ConfigError: A flag has an unknown value, a numeric setting is
            unparseable, ``ASSET_INDEX_TABLE`` is missing, or the configured
            registry belongs to a different environment.
    """
    env = dict(os.environ if environ is None else environ)

    deploy_env = (env.get("DEPLOY_ENV") or "").strip()
    if not deploy_env:
        raise ConfigError("DEPLOY_ENV is required (sandbox|staging|prod|dev)")

    asset_index_table = (env.get("ASSET_INDEX_TABLE") or "").strip()
    if not asset_index_table:
        # The canonical store is not optional. Failing closed here is the
        # difference between "empty inventory" and "authorization computed
        # from an incomplete source" (R2, error-handling table).
        raise ConfigError("ASSET_INDEX_TABLE is required")

    registry_id = (env.get("REGISTRY_ID") or "").strip() or None
    registry_name = (env.get("REGISTRY_NAME") or "").strip() or None

    # Cross-environment guard. Only checkable when a name is supplied; the
    # projector's IAM policy is scoped to one registry ARN regardless.
    if registry_name and not registry_name.endswith(f"-{deploy_env}"):
        raise ConfigError(
            f"REGISTRY_NAME={registry_name!r} does not belong to "
            f"DEPLOY_ENV={deploy_env!r} (expected suffix '-{deploy_env}')"
        )

    projection_enabled = (
        env.get("REGISTRY_PROJECTION_ENABLED", "").strip().lower() == "true"
    )
    if projection_enabled and not registry_id:
        raise ConfigError(
            "REGISTRY_PROJECTION_ENABLED=true requires REGISTRY_ID"
        )

    shadow = _read_int("REGISTRY_SHADOW_SAMPLE_PERCENT", 0, env)
    if not 0 <= shadow <= 100:
        raise ConfigError(
            f"REGISTRY_SHADOW_SAMPLE_PERCENT={shadow} must be within 0..100"
        )

    modes = {category: _read_mode(category, env) for category in sorted(CATEGORIES)}
    # Only the modes that actually reach the Registry need one to exist:
    # `registry` serves its rows, `shadow` compares against them. `canonical` is
    # served entirely from DynamoDB, so requiring an id there would couple a pure
    # latency change to provisioning a registry it never reads.
    needs_registry = {
        c: m for c, m in modes.items() if m in ("shadow", "registry")
    }
    if needs_registry and not registry_id:
        raise ConfigError(
            f"categories {sorted(needs_registry)} are in a mode that reads the "
            f"Registry but REGISTRY_ID is unset"
        )

    return CapabilityConfig(
        deploy_env=deploy_env,
        region=(env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-west-2"),
        asset_index_table=asset_index_table,
        projection_state_table=(env.get("PROJECTION_STATE_TABLE") or "").strip() or None,
        gateway_registry_table=(env.get("GATEWAY_REGISTRY_TABLE") or "").strip() or None,
        # Optional, like the gateway/projection tables: the Capability API runs
        # before the Improve table exists, and the routes turn an unset value
        # into a 503 rather than a startup ConfigError.
        improve_table=(env.get("IMPROVE_TABLE") or "").strip() or None,
        artifacts_bucket=(env.get("ARTIFACTS_BUCKET") or "").strip() or None,
        registry_id=registry_id,
        registry_name=registry_name,
        projection_enabled=projection_enabled,
        shadow_sample_percent=shadow,
        source_timeout_seconds=_read_float(
            "CAPABILITY_SOURCE_TIMEOUT_SECONDS", DEFAULT_SOURCE_TIMEOUT_SECONDS, env
        ),
        user_pool_issuer=(env.get("USER_POOL_ISSUER") or "").strip() or None,
        user_pool_client_id=(env.get("USER_POOL_CLIENT_ID") or "").strip() or None,
        modes=modes,
    )


__all__ = [
    "CapabilityConfig",
    "ConfigError",
    "DEFAULT_MODE",
    "DEFAULT_SOURCE_TIMEOUT_SECONDS",
    "MODES",
    "load_config",
]
