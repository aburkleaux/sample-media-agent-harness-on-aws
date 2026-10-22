"""Thread-safe progress event channel for tool → orchestrator streaming.

Tools running in Strands tool handler threads push events here.
The orchestrator's async streaming loop drains them between iterations.
Uses a thread-safe queue.Queue (not asyncio.Queue) since tools run synchronously.

Gating: tools can block on a gate (threading.Event) waiting for user approval.
The UI sends approval via a management API call that resolves the gate.
"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateResult:
    """Result of a gate approval/rejection."""
    approved: bool
    feedback: str = ""


@dataclass
class ProgressEvent:
    """A structured progress event from a tool."""
    status: str          # "running" | "done" | "pending" | "completed" | "failed" | "skipped" | "replanning" | "plan_ready"
    label: str           # Human-readable description
    plan_id: str = ""
    node_id: str = ""
    duration_ms: int = 0
    agent: str = ""      # "orchestrator" | "planning" | "design" | "task:node_id" | "coordinator"
    phase: str = ""      # "planning" | "design" | "validation" | "execution" | "direct"
    extra: dict = field(default_factory=dict)

    def to_step_json(self) -> str:
        """Serialize as a step event JSON string for the UI."""
        d: dict[str, Any] = {"type": "step", "status": self.status, "label": self.label}
        if self.duration_ms:
            d["duration_ms"] = self.duration_ms
        if self.agent:
            d["agent"] = self.agent
        if self.phase:
            d["phase"] = self.phase
        if self.node_id:
            d["node_id"] = self.node_id
        if self.extra:
            d.update(self.extra)
        return json.dumps(d)


class ProgressChannel:
    """Thread-safe channel for pushing progress events from tools to the orchestrator.

    Also supports gates: a tool can emit a plan_ready event and block until
    the user approves or rejects via the UI.
    """

    def __init__(self):
        self._queue: queue.Queue[ProgressEvent] = queue.Queue()
        self._gates: dict[str, threading.Event] = {}
        self._gate_results: dict[str, GateResult] = {}
        self._lock = threading.Lock()

    def push(self, status: str, label: str, **kwargs) -> None:
        """Push a progress event (thread-safe, non-blocking)."""
        self._queue.put_nowait(ProgressEvent(status=status, label=label, **kwargs))

    def drain(self) -> list[ProgressEvent]:
        """Drain all pending events (non-blocking). Returns empty list if none."""
        events: list[ProgressEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    def is_empty(self) -> bool:
        return self._queue.empty()

    # --- Gating ---

    def wait_for_approval(self, gate_id: str, timeout: float = 300.0) -> GateResult:
        """Block the calling thread until the gate is resolved or timeout.

        Called from the tool thread. Returns GateResult with approved=True/False.
        On timeout, returns approved=True (auto-approve to avoid deadlock).
        """
        event = threading.Event()
        with self._lock:
            self._gates[gate_id] = event

        resolved = event.wait(timeout=timeout)

        with self._lock:
            self._gates.pop(gate_id, None)
            result = self._gate_results.pop(gate_id, None)

        if not resolved or result is None:
            # Timeout — auto-approve to avoid blocking forever
            return GateResult(approved=True, feedback="Auto-approved (timeout)")

        return result

    def resolve_gate(self, gate_id: str, approved: bool, feedback: str = "") -> bool:
        """Resolve a pending gate. Called from the API handler.

        Returns True if the gate existed and was resolved, False otherwise.
        """
        with self._lock:
            event = self._gates.get(gate_id)
            if event is None:
                return False
            self._gate_results[gate_id] = GateResult(approved=approved, feedback=feedback)
            event.set()
            return True

    def pending_gates(self) -> list[str]:
        """Return list of gate IDs currently waiting for approval."""
        with self._lock:
            return list(self._gates.keys())


# --- Per-request channel (module-level, one request at a time per runtime) ---
_active_channel: ProgressChannel | None = None


def set_active_channel(channel: ProgressChannel | None) -> None:
    """Set the active progress channel for the current request."""
    global _active_channel
    _active_channel = channel


def get_active_channel() -> ProgressChannel | None:
    """Get the active progress channel (returns None if not set)."""
    return _active_channel
