"""Unit tests for the registry snapshot build (task 7.1 / 7.2).

Covers ``build_capability_index(oms_client) -> CapabilityIndex``, driven by a
fake ``OMSMcpClient`` whose ``get_integration`` detail carries a per-action
``action_tool_names`` map (the service-registry metadata the bridge publishes
verbatim). The map mixes published and unpublished ``tool_name`` values,
including the currently-verified case where ``audio.stem_separation`` has **no**
entry in ``action_tool_names`` (so it must land as an unpublished Capability with
``bare_tool_name`` ``None`` — the distinct ``Bridge_Tool_Name_Publication``
dependency gap of R4.2/R4.3).

Per Option B, the gateway ``tool_name`` is read from ``get_integration``'s
``action_tool_names`` map, **not** from ``get_action_schema`` (which resolves only
``{service, action, input, output}`` from installed kits and never surfaces a
registry-published ``tool_name``).

Asserts:
- every registered ``service.action`` becomes a Capability (published AND
  unpublished) in ``capabilities`` (R4.1).
- ``lookup`` indexes only published capabilities by their bare tool name.
- two published capabilities sharing a bare name produce a >1 lookup bucket
  (the ambiguity the resolver later detects, R3.4).
- read failures propagate as ``OMSUnavailableError`` (raised by the client).

Requirements: 3.1, 4.1, 4.2
"""

from __future__ import annotations

import os
import sys

import pytest

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from persistence.capability_resolver import build_capability_index
from persistence.models import Capability, CapabilityIndex, OMSUnavailableError


# ---------------------------------------------------------------------------
# Fake OMSMcpClient — records calls, returns scripted registry snapshots
# ---------------------------------------------------------------------------


class _FakeOMSClient:
    """In-memory stand-in for ``OMSMcpClient`` over the registry read tools.

    ``integrations`` maps integration_id -> the ``get_integration`` detail dict,
    which carries ``actions: [...]`` and (per Option B) the ``action_tool_names``
    map (``{"<action>": "<gateway_tool_name>", ...}``) — the service-registry
    metadata the bridge publishes verbatim. An entry in ``get_integration_failures``
    keyed by integration_id raises to simulate a transport failure the real client
    wraps as OMSUnavailableError.

    ``get_action_schema`` is retained on the fake for interface fidelity, but the
    builder no longer reads ``tool_name`` from it; it raises if unexpectedly called
    so a regression back to the old contract is caught.
    """

    def __init__(
        self,
        integrations: dict,
        *,
        get_integration_failures: dict | None = None,
    ) -> None:
        self._integrations = integrations
        self._get_integration_failures = get_integration_failures or {}
        self.calls: list[tuple[str, tuple]] = []

    def list_integrations(self) -> list[dict]:
        self.calls.append(("list_integrations", ()))
        return [{"integration_id": iid} for iid in self._integrations]

    def get_integration(self, integration_id: str) -> dict:
        self.calls.append(("get_integration", (integration_id,)))
        if integration_id in self._get_integration_failures:
            raise self._get_integration_failures[integration_id]
        return dict(self._integrations[integration_id])

    def get_action_schema(self, integration_id: str, action: str) -> dict:
        # Option B: the builder must NOT read tool_name from get_action_schema.
        self.calls.append(("get_action_schema", (integration_id, action)))
        raise AssertionError(
            "build_capability_index must not call get_action_schema for tool_name "
            "(Option B: tool_name comes from get_integration's action_tool_names)"
        )


def _mixed_client() -> _FakeOMSClient:
    """A registry with one published, one unpublished, and one separator-less action.

    ``action_tool_names`` carries the per-action gateway tool name. Note
    ``audio.stem_separation`` has NO entry (the map only publishes ``normalize``),
    so it is the currently-verified unpublished regression case.
    """
    integrations = {
        "audio": {
            "actions": ["stem_separation", "normalize"],
            # Published only for normalize; stem_separation is intentionally absent.
            "action_tool_names": {
                "normalize": "audio-norm___normalize_loudness",
            },
        },
        "video": {
            "actions": ["transcode"],
            # Separator-less tool_name -> unpublished (no usable bare name).
            "action_tool_names": {
                "transcode": "transcode_video",
            },
        },
    }
    return _FakeOMSClient(integrations)


def _cap(index: CapabilityIndex, service: str, action: str) -> Capability:
    for cap in index.capabilities:
        if cap.service == service and cap.action == action:
            return cap
    raise AssertionError(f"capability {service}.{action} not in index")


# ---------------------------------------------------------------------------
# Build reads every service.action (R4.1)
# ---------------------------------------------------------------------------


def test_build_reads_all_registered_service_actions():
    client = _mixed_client()
    index = build_capability_index(client)

    assert isinstance(index, CapabilityIndex)
    # All three registered service.action pairs become Capabilities.
    assert {(c.service, c.action) for c in index.capabilities} == {
        ("audio", "stem_separation"),
        ("audio", "normalize"),
        ("video", "transcode"),
    }


def test_build_walks_list_then_get_integration_only():
    client = _mixed_client()
    build_capability_index(client)

    # list_integrations once, get_integration per service; the tool_name comes
    # from the integration detail, so get_action_schema is never called (Option B).
    kinds = [name for name, _ in client.calls]
    assert kinds[0] == "list_integrations"
    assert kinds.count("get_integration") == 2
    assert kinds.count("get_action_schema") == 0


# ---------------------------------------------------------------------------
# Published capability -> bare name + lookup entry
# ---------------------------------------------------------------------------


def test_published_capability_has_bare_name_and_is_in_lookup():
    index = build_capability_index(_mixed_client())

    normalize = _cap(index, "audio", "normalize")
    assert normalize.gateway_tool_name == "audio-norm___normalize_loudness"
    assert normalize.bare_tool_name == "normalize_loudness"
    # Only published capabilities are indexed in lookup, keyed by bare name.
    assert index.lookup.get("normalize_loudness") == [normalize]


# ---------------------------------------------------------------------------
# Currently-verified: audio.stem_separation with NO tool_name is unpublished
# ---------------------------------------------------------------------------


def test_stem_separation_without_tool_name_is_unpublished():
    index = build_capability_index(_mixed_client())

    stem = _cap(index, "audio", "stem_separation")
    # No published tool name at all -> both fields None (unpublished gap R4.2/R4.3).
    assert stem.gateway_tool_name is None
    assert stem.bare_tool_name is None
    # It is present in capabilities (for gap detection) but NOT in lookup.
    assert stem in index.capabilities
    assert all(stem not in caps for caps in index.lookup.values())


def test_separatorless_tool_name_is_unpublished():
    index = build_capability_index(_mixed_client())

    transcode = _cap(index, "video", "transcode")
    # Separator-less tool_name yields no usable bare name -> unpublished.
    assert transcode.gateway_tool_name == "transcode_video"
    assert transcode.bare_tool_name is None
    assert all(transcode not in caps for caps in index.lookup.values())


def test_lookup_indexes_only_published_capabilities():
    index = build_capability_index(_mixed_client())

    # Exactly the one published capability's bare name is a lookup key.
    assert set(index.lookup) == {"normalize_loudness"}


# ---------------------------------------------------------------------------
# Ambiguity: two published capabilities share a bare name -> >1 bucket (R3.4)
# ---------------------------------------------------------------------------


def test_shared_bare_name_produces_ambiguous_lookup_bucket():
    integrations = {
        "music": {
            "actions": ["split"],
            "action_tool_names": {"split": "a-tgt___mix_tracks"},
        },
        "dj": {
            "actions": ["blend"],
            "action_tool_names": {"blend": "b-tgt___mix_tracks"},
        },
    }
    index = build_capability_index(_FakeOMSClient(integrations))

    bucket = index.lookup["mix_tracks"]
    assert len(bucket) == 2
    assert {(c.service, c.action) for c in bucket} == {("music", "split"), ("dj", "blend")}


# ---------------------------------------------------------------------------
# Action-name shape tolerance + integration-id fallbacks
# ---------------------------------------------------------------------------


def test_actions_as_dicts_are_normalized():
    integrations = {
        "audio": {
            "actions": [{"action": "stem_separation"}, {"name": "normalize"}],
            "action_tool_names": {"normalize": "audio-norm___normalize_loudness"},
        }
    }
    index = build_capability_index(_FakeOMSClient(integrations))

    assert {(c.service, c.action) for c in index.capabilities} == {
        ("audio", "stem_separation"),
        ("audio", "normalize"),
    }


def test_service_is_the_integration_id():
    integrations = {
        "audio": {
            "actions": ["stem_separation"],
            "action_tool_names": {"stem_separation": "audio___separate"},
        }
    }
    index = build_capability_index(_FakeOMSClient(integrations))

    cap = _cap(index, "audio", "stem_separation")
    assert cap.service == "audio"
    assert cap.bare_tool_name == "separate"


# ---------------------------------------------------------------------------
# Transport failures propagate as OMSUnavailableError (raised by the client)
# ---------------------------------------------------------------------------


def test_read_failure_propagates_as_oms_unavailable():
    integrations = {"audio": {"actions": ["stem_separation"]}}
    failures = {"audio": OMSUnavailableError("gateway down", stage="resolve")}
    client = _FakeOMSClient(integrations, get_integration_failures=failures)

    with pytest.raises(OMSUnavailableError):
        build_capability_index(client)


# ---------------------------------------------------------------------------
# A wholly absent action_tool_names map -> every action unpublished
# ---------------------------------------------------------------------------


def test_absent_action_tool_names_map_makes_all_actions_unpublished():
    # No action_tool_names map at all on the integration detail.
    integrations = {"audio": {"actions": ["stem_separation", "normalize"]}}
    index = build_capability_index(_FakeOMSClient(integrations))

    for action in ("stem_separation", "normalize"):
        cap = _cap(index, "audio", action)
        assert cap.gateway_tool_name is None
        assert cap.bare_tool_name is None
    # Nothing published -> empty lookup.
    assert index.lookup == {}


# ---------------------------------------------------------------------------
# status_tool_bare_names populated from action_status_tools (bare-reduced)
# ---------------------------------------------------------------------------


def test_status_tool_bare_names_populated_from_action_status_tools():
    # The audio integration publishes its capability tool name AND a status tool
    # OMS polls for job status; the status tool's bare name must land in the set.
    integrations = {
        "audio": {
            "actions": ["stem_separation", "diarization"],
            "action_tool_names": {
                "stem_separation": "audio-stem___separate_audio_stems",
                "diarization": "speech-diarization___diarize_speech",
            },
            "action_status_tools": {
                "stem_separation": "audio-stem___get_job_status",
                "diarization": "speech-diarization___get_job_status",
            },
        }
    }
    index = build_capability_index(_FakeOMSClient(integrations))

    # Both status tools reduce to the same bare name "get_job_status".
    assert index.status_tool_bare_names == {"get_job_status"}
    # Capability tool names remain published and joinable.
    assert index.lookup.get("separate_audio_stems") is not None
    assert index.lookup.get("diarize_speech") is not None


def test_absent_action_status_tools_yields_empty_status_set():
    integrations = {
        "audio": {
            "actions": ["stem_separation"],
            "action_tool_names": {"stem_separation": "audio-stem___separate_audio_stems"},
        }
    }
    index = build_capability_index(_FakeOMSClient(integrations))

    assert index.status_tool_bare_names == set()


def test_separatorless_status_tool_name_is_dropped_from_status_set():
    # A separator-less status tool name reduces to None and is not added.
    integrations = {
        "audio": {
            "actions": ["stem_separation"],
            "action_tool_names": {"stem_separation": "audio-stem___separate_audio_stems"},
            "action_status_tools": {"stem_separation": "get_job_status"},
        }
    }
    index = build_capability_index(_FakeOMSClient(integrations))

    assert index.status_tool_bare_names == set()
