"""ConversationalMemory — session-scoped turn record persistence.

Persists conversational turn records to S3 under the namespace
`{actor_id}/conversations/{session_id}/`. Each turn record includes
the turn_id, timestamp, user message summary, affected asset IDs,
and tool calls made.

Reads and writes are scoped to the current (actor_id, session_id) pair
only — no cross-session or cross-actor leakage.

Requirements: 6.4, 6.5, 9.5
Properties: 21
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from .context import LabRequestContext
from .s3_user_files_tool import S3UserFilesTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Turn Record Model
# ---------------------------------------------------------------------------


@dataclass
class TurnRecord:
    """A single conversational turn record.

    Persisted as JSON under the conversations namespace keyed by
    (actor_id, session_id).

    Attributes:
        turn_id: Unique identifier for this turn (UUID4).
        timestamp: Epoch milliseconds when the turn was recorded.
        user_message: Summary of the user's message for this turn.
        affected_asset_ids: List of Draft assetId values modified in this turn.
        tool_calls_made: List of tool call summaries (name, success, error).
    """

    turn_id: str
    timestamp: int  # epoch ms
    user_message: str
    affected_asset_ids: list[str] = field(default_factory=list)
    tool_calls_made: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize the turn record to a JSON-compatible dict."""
        return {
            "turn_id": self.turn_id,
            "timestamp": self.timestamp,
            "user_message": self.user_message,
            "affected_asset_ids": self.affected_asset_ids,
            "tool_calls_made": self.tool_calls_made,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TurnRecord":
        """Deserialize a turn record from a dict."""
        return cls(
            turn_id=data["turn_id"],
            timestamp=data["timestamp"],
            user_message=data["user_message"],
            affected_asset_ids=data.get("affected_asset_ids", []),
            tool_calls_made=data.get("tool_calls_made", []),
        )


# ---------------------------------------------------------------------------
# ConversationalMemory
# ---------------------------------------------------------------------------


class ConversationalMemory:
    """Session-scoped conversational turn record persistence.

    Persists turn records to S3 under the namespace:
        `conversations/{session_id}/{turn_id}.json`

    All operations are scoped to the current (actor_id, session_id) pair.
    The actor_id scoping is enforced by the S3UserFilesTool which prepends
    `{actor_id}/` to all keys. The session_id scoping is enforced by this
    class which always uses the session_id from the LabRequestContext.

    Property 21: Conversational_Memory isolation — reads return only turns
    from same (actor_id, session_id) pair.

    Requirements: 6.4, 6.5, 9.5
    """

    def __init__(self, s3: S3UserFilesTool, ctx: LabRequestContext) -> None:
        """Initialize ConversationalMemory.

        Args:
            s3: The S3UserFilesTool instance (already bound to ctx.actor_id).
            ctx: The LabRequestContext providing actor_id and session_id.
        """
        self._s3 = s3
        self._ctx = ctx
        self._prefix = f"conversations/{ctx.session_id}/"

    @property
    def actor_id(self) -> str:
        """The actor_id this memory is scoped to."""
        return self._ctx.actor_id

    @property
    def session_id(self) -> str:
        """The session_id this memory is scoped to."""
        return self._ctx.session_id

    async def persist_turn(
        self,
        user_message: str,
        affected_asset_ids: list[str],
        tool_calls_made: list[dict],
    ) -> TurnRecord:
        """Persist a conversational turn record.

        Creates a new turn record with a unique turn_id and current timestamp,
        then writes it to S3 under the conversations namespace.

        Only persists turns that modified one or more Drafts (Requirement 6.4).

        Args:
            user_message: Summary of the user's message for this turn.
            affected_asset_ids: List of Draft assetId values affected.
            tool_calls_made: List of tool call summaries.

        Returns:
            The persisted TurnRecord.
        """
        turn_id = str(uuid.uuid4())
        timestamp = int(time.time() * 1000)

        record = TurnRecord(
            turn_id=turn_id,
            timestamp=timestamp,
            user_message=user_message,
            affected_asset_ids=affected_asset_ids,
            tool_calls_made=tool_calls_made,
        )

        # Write to S3 under conversations/{session_id}/{turn_id}.json
        key = f"{self._prefix}{turn_id}.json"
        data = json.dumps(record.to_dict(), indent=2).encode("utf-8")

        await self._s3.write(key, data)

        logger.info(
            "Persisted turn record turn_id=%s for actor=%s session=%s "
            "affected_assets=%d",
            turn_id,
            self._ctx.actor_id,
            self._ctx.session_id,
            len(affected_asset_ids),
        )

        return record

    async def list_turns(self) -> list[TurnRecord]:
        """List all turn records for the current (actor_id, session_id) pair.

        Returns only turns from the current session — no cross-session or
        cross-actor leakage (Property 21, Requirement 6.5).

        Returns:
            List of TurnRecord objects sorted by timestamp (oldest first).
        """
        # List all objects under conversations/{session_id}/
        keys = await self._s3.list(self._prefix)

        records: list[TurnRecord] = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            try:
                data = await self._s3.read(key)
                record_dict = json.loads(data.decode("utf-8"))
                record = TurnRecord.from_dict(record_dict)
                records.append(record)
            except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
                logger.warning(
                    "Failed to read turn record at key=%s: %s", key, str(e)
                )
                continue

        # Sort by timestamp (oldest first)
        records.sort(key=lambda r: r.timestamp)

        return records

    async def get_turn(self, turn_id: str) -> Optional[TurnRecord]:
        """Get a specific turn record by turn_id.

        Only returns the turn if it belongs to the current (actor_id, session_id)
        pair (Property 21).

        Args:
            turn_id: The unique turn identifier.

        Returns:
            The TurnRecord if found, None otherwise.
        """
        key = f"{self._prefix}{turn_id}.json"
        try:
            data = await self._s3.read(key)
            record_dict = json.loads(data.decode("utf-8"))
            return TurnRecord.from_dict(record_dict)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None
