"""``list_custom_agents`` lists the caller's own published Custom Agents.

Actor scoping is free: ``LabService.handle`` strips any ``actor_id`` from the
payload and takes identity only from ``LabRequestContext``.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main_lab_tools  # noqa: E402


class _StubService:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def handle(self, body):
        self.calls.append(body)
        return self.rows


@pytest.mark.asyncio
async def test_lists_published_agents_with_descriptions():
    rows = [{
        "assetId": "ag-1",
        "name": "Transcript Cleaner",
        "description": "Cleans up ASR transcripts",
        "version": 2,
        "current": True,
        "scope": "private",
    }]
    svc = _StubService(rows)
    token = main_lab_tools.set_current_lab_service(svc)
    try:
        out = json.loads(await main_lab_tools.list_custom_agents())
    finally:
        main_lab_tools.reset_current_lab_service(token)

    assert svc.calls[0]["action"] == "ListMyPublishedAssets"
    assert svc.calls[0]["category"] == "specialty-agent"
    assert out[0]["description"] == "Cleans up ASR transcripts"
    assert out[0]["version"] == 2


@pytest.mark.asyncio
async def test_no_agents_returns_an_empty_list():
    svc = _StubService([])
    token = main_lab_tools.set_current_lab_service(svc)
    try:
        out = json.loads(await main_lab_tools.list_custom_agents())
    finally:
        main_lab_tools.reset_current_lab_service(token)
    assert out == []
