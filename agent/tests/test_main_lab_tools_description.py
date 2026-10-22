"""The chat CRUD tools can set a description.

``_dispatch`` drops None values (``main_lab_tools.py:70``), so an omitted
description never reaches the service and cannot accidentally clear a stored
one.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main_lab_tools  # noqa: E402


class _RecordingService:
    def __init__(self):
        self.calls = []

    async def handle(self, body):
        self.calls.append(body)
        return {"ok": True}


@pytest.fixture
def recording():
    svc = _RecordingService()
    token = main_lab_tools.set_current_lab_service(svc)
    yield svc
    main_lab_tools.reset_current_lab_service(token)


@pytest.mark.asyncio
async def test_create_forwards_the_description(recording):
    await main_lab_tools.create_specialty_agent_draft(
        name="Transcript Cleaner",
        persona="You clean transcripts.",
        description="Cleans up ASR transcripts",
    )
    assert recording.calls[0]["description"] == "Cleans up ASR transcripts"


@pytest.mark.asyncio
async def test_update_forwards_the_description(recording):
    await main_lab_tools.update_specialty_agent_draft(
        asset_id="ag-1", description="new"
    )
    assert recording.calls[0]["description"] == "new"


@pytest.mark.asyncio
async def test_omitted_description_is_not_sent(recording):
    await main_lab_tools.update_specialty_agent_draft(
        asset_id="ag-1", persona="edited"
    )
    assert "description" not in recording.calls[0]
