"""Property-based test for Custom_Agent integration-config validation.

Feature: custom-agents, Property 5: Integration config validation

Validates: Requirements 4.2, 4.3, 4.5

Property 5 (design.md): For any integration configuration with
``enabled == true``, validation passes if and only if ``issuer`` is a valid
HTTPS URL and ``audience`` is a non-empty string; disabling produces a stored
config that is disabled/cleared.

``SpecialtyCRUD._validate_integration_config`` is pure, synchronous logic that
never touches S3 or the AssetIndex, so "no write occurs" is demonstrated by
asserting the mocked S3/index collaborators receive no calls on any path.

Semantics under test (agent/src/lab/specialty_crud.py):

* ``cfg is None`` -> returns ``None`` (no-op).
* ``cfg.enabled is False`` -> returns a fresh disabled/cleared
  ``IntegrationConfig(enabled=False)`` regardless of stale issuer/audience
  (Requirement 4.5).
* ``cfg.enabled is True`` -> returns ``cfg`` unchanged iff ``issuer`` is a
  valid HTTPS URL (scheme ``https`` + non-empty host) and ``audience`` is a
  non-empty / non-whitespace string; otherwise raises ``ValueError`` naming
  the offending field (Requirements 4.2, 4.3).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.models import IntegrationConfig  # noqa: E402
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# A pool of hosts to build URLs from.
_host = st.sampled_from(
    ["issuer.example.com", "auth.test", "login.example.org:8443", "a.co"]
)

# Valid HTTPS issuer URLs: scheme https + non-empty host.
_https_issuer = st.builds(
    lambda host, path: f"https://{host}{path}",
    _host,
    st.sampled_from(["", "/", "/oauth2", "/realms/main"]),
)

# HTTP (non-HTTPS) issuers: wrong scheme.
_http_issuer = st.builds(lambda host: f"http://{host}", _host)

# Other-scheme / malformed issuers.
_other_scheme_issuer = st.sampled_from(
    ["ftp://issuer.example.com", "wss://issuer.example.com"]
)

# Non-URL free text (usually no scheme/host).
_non_url_text = st.text(max_size=20)

# The full issuer space: a mix of valid and invalid strings plus None.
_issuer = st.one_of(
    _https_issuer,
    _http_issuer,
    _other_scheme_issuer,
    _non_url_text,
    st.just(""),
    st.none(),
)

# Audience space: non-empty strings, whitespace-only, empty, and None.
_audience = st.one_of(
    st.text(min_size=1, max_size=20).filter(lambda s: s.strip() != ""),
    st.sampled_from(["", "   ", "\t", "\n", " \t\n "]),
    st.none(),
)

_client_ids = st.lists(st.text(min_size=1, max_size=12), max_size=4)


def _issuer_is_valid_https(issuer) -> bool:
    """Oracle mirroring the implementation's HTTPS-issuer check."""
    if not isinstance(issuer, str):
        return False
    parsed = urlparse(issuer)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _audience_is_valid(audience) -> bool:
    """Oracle mirroring the implementation's audience check."""
    return isinstance(audience, str) and bool(audience.strip())


def _make_crud() -> tuple[SpecialtyCRUD, MagicMock, MagicMock]:
    """Build a SpecialtyCRUD with mocked S3/index collaborators."""
    s3 = MagicMock(name="s3")
    index = MagicMock(name="index")
    return SpecialtyCRUD(s3=s3, index=index), s3, index


# ---------------------------------------------------------------------------
# Property 5: Integration config validation
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 5: Integration config validation
@settings(max_examples=200, deadline=None)
@given(
    enabled=st.booleans(),
    issuer=_issuer,
    audience=_audience,
    client_ids=_client_ids,
    cfg_is_none=st.booleans(),
)
def test_property5_integration_config_validation(
    enabled, issuer, audience, client_ids, cfg_is_none
):
    """_validate_integration_config accepts iff HTTPS issuer + non-empty aud
    when enabled; disabling yields a cleared/disabled config; None is a no-op.

    Validates: Requirements 4.2, 4.3, 4.5
    """
    crud, s3, index = _make_crud()

    cfg = (
        None
        if cfg_is_none
        else IntegrationConfig(
            enabled=enabled,
            issuer=issuer,
            audience=audience,
            allowed_client_ids=list(client_ids),
        )
    )

    if cfg is None:
        # None -> no-op, returns None unchanged.
        assert crud._validate_integration_config(cfg) is None

    elif not enabled:
        # Requirement 4.5: disabling clears the stored config regardless of any
        # stale issuer/audience/client-ids left populated by the caller.
        result = crud._validate_integration_config(cfg)
        assert isinstance(result, IntegrationConfig)
        assert result.enabled is False
        assert result.issuer is None
        assert result.audience is None
        assert result.allowed_client_ids == []

    else:
        # Enabled: passes iff HTTPS issuer AND non-empty audience.
        expected_ok = _issuer_is_valid_https(issuer) and _audience_is_valid(
            audience
        )
        if expected_ok:
            result = crud._validate_integration_config(cfg)
            # On success the (equivalent) enabled config is returned.
            assert result is cfg
            assert result.enabled is True
            assert result.issuer == issuer
            assert result.audience == audience
        else:
            with pytest.raises(ValueError):
                crud._validate_integration_config(cfg)

    # No write occurs on any path: _validate_integration_config must not touch
    # S3 or the AssetIndex, so a rejection can never leave a partial write.
    assert s3.mock_calls == []
    assert index.mock_calls == []
