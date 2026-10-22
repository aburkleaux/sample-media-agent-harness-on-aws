"""Declarative hook engine.

Given a set of ``HookSpec``s and a normalized event context, ``fire`` selects
the enabled hooks whose ``event`` matches and whose ``condition`` evaluates
true, runs their actions in ``priority`` order (lower first), and returns the
per-action results. Every condition evaluation and action invocation is
isolated: a bad condition or a raising/slow action never breaks the turn — it
is logged and skipped.

The engine is deterministic and LLM-free by construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .actions import ActionResult, get_action
from .expr import ExpressionError, evaluate

logger = logging.getLogger(__name__)

__all__ = ["HookSpec", "HookEngine"]


@dataclass
class HookSpec:
    """A resolved, ready-to-evaluate hook (decoupled from the asset model)."""

    name: str
    event: str
    condition: str
    action_type: str
    action_params: dict = field(default_factory=dict)
    priority: int = 100
    enabled: bool = True

    @classmethod
    def from_hook_asset(cls, hook) -> "HookSpec":
        """Build a spec from a ``lab.models.Hook`` (or any object with the fields)."""
        return cls(
            name=getattr(hook, "name", None) or getattr(hook, "asset_id", "hook"),
            event=getattr(hook, "event", ""),
            condition=getattr(hook, "condition", ""),
            action_type=getattr(hook, "action_type", "notify"),
            action_params=dict(getattr(hook, "action_params", {}) or {}),
            priority=int(getattr(hook, "priority", 100)),
            enabled=bool(getattr(hook, "enabled", True)),
        )


class HookEngine:
    """Holds the active hook set and fires matching hooks on an event."""

    def __init__(self, hooks: Optional[list[HookSpec]] = None):
        self._hooks: list[HookSpec] = list(hooks or [])

    def set_hooks(self, hooks: list[HookSpec]) -> None:
        self._hooks = list(hooks or [])

    @property
    def hooks(self) -> list[HookSpec]:
        return list(self._hooks)

    def matching(self, event_name: str, context: dict) -> list[HookSpec]:
        """Return enabled hooks for ``event_name`` whose condition matches,
        sorted by ascending priority."""
        matched: list[HookSpec] = []
        for hook in self._hooks:
            if not hook.enabled or hook.event != event_name:
                continue
            try:
                if evaluate(hook.condition, context):
                    matched.append(hook)
            except ExpressionError as exc:
                logger.warning(
                    "hook %r: skipping — invalid condition %r: %s",
                    hook.name, hook.condition, exc,
                )
        matched.sort(key=lambda h: h.priority)
        return matched

    def fire(self, event_name: str, context: dict) -> list[ActionResult]:
        """Evaluate + run all matching hooks for ``event_name``. Never raises."""
        ctx = dict(context or {})
        ctx.setdefault("event", event_name)
        results: list[ActionResult] = []
        for hook in self.matching(event_name, ctx):
            fn = get_action(hook.action_type)
            if fn is None:
                logger.warning("hook %r: unknown action %r", hook.name, hook.action_type)
                results.append(ActionResult(hook.action_type, "error", "unknown action"))
                continue
            try:
                results.append(fn(hook.action_params, ctx))
            except Exception as exc:  # isolation — never break the turn
                logger.exception("hook %r: action %r failed", hook.name, hook.action_type)
                results.append(ActionResult(hook.action_type, "error", str(exc)))
        return results
