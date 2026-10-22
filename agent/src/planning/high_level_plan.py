"""HighLevelPlan data model — Planning Agent output schema.

Defines SubGoal, DirectDispatchParams, and HighLevelPlan dataclasses
with JSON serialization/deserialization and the embedded JSON Schema constant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class SubGoal:
    """A reasoning-level sub-goal within a HighLevelPlan."""
    id: str
    description: str
    reasoning: str
    suggested_tools: list[str]
    suggested_skills: list[str]
    depends_on: list[str]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "reasoning": self.reasoning,
            "suggested_tools": list(self.suggested_tools),
            "suggested_skills": list(self.suggested_skills),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: dict) -> SubGoal:
        return cls(
            id=data["id"],
            description=data["description"],
            reasoning=data["reasoning"],
            suggested_tools=list(data.get("suggested_tools", [])),
            suggested_skills=list(data.get("suggested_skills", [])),
            depends_on=list(data.get("depends_on", [])),
        )


@dataclass
class DirectDispatchParams:
    """Parameters for direct dispatch_tool invocation (direct execution_mode)."""
    prompt: str
    tools: list[str]
    skills: list[dict]
    model_id: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "prompt": self.prompt,
            "tools": list(self.tools),
            "skills": [dict(s) for s in self.skills],
        }
        if self.model_id is not None:
            d["model_id"] = self.model_id
        return d

    @classmethod
    def from_dict(cls, data: dict) -> DirectDispatchParams:
        # The planner may name a skill as a bare string (`"skills": ["video-processing"]`, staging
        # 2026-10-09) though the schema asks for objects. A name is a reference, kept as
        # `{"name": ...}` for `_dispatch_direct` to resolve; `dict("video-processing")` raised and
        # killed the whole plan before dispatch.
        return cls(
            prompt=data["prompt"],
            tools=list(data["tools"]),
            skills=[{"name": s} if isinstance(s, str) else dict(s) for s in (data.get("skills") or [])],
            model_id=data.get("model_id"),
        )


@dataclass
class HighLevelPlan:
    """Structured output of the Planning Agent."""
    execution_mode: str  # "direct" | "standard" | "complex"
    sub_goals: list[SubGoal]
    suggested_tools: dict[str, list[str]]  # sub_goal_id -> relevant tools
    suggested_skills: dict[str, list[str]]  # sub_goal_id -> relevant skills
    reasoning: str
    direct_dispatch_params: DirectDispatchParams | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "execution_mode": self.execution_mode,
            "sub_goals": [sg.to_dict() for sg in self.sub_goals],
            "suggested_tools": {k: list(v) for k, v in self.suggested_tools.items()},
            "suggested_skills": {k: list(v) for k, v in self.suggested_skills.items()},
            "reasoning": self.reasoning,
        }
        if self.direct_dispatch_params is not None:
            d["direct_dispatch_params"] = self.direct_dispatch_params.to_dict()
        else:
            d["direct_dispatch_params"] = None
        return d

    @classmethod
    def from_dict(cls, data: dict) -> HighLevelPlan:
        ddp_data = data.get("direct_dispatch_params")
        return cls(
            execution_mode=data["execution_mode"],
            sub_goals=[SubGoal.from_dict(sg) for sg in data["sub_goals"]],
            suggested_tools={k: list(v) for k, v in data.get("suggested_tools", {}).items()},
            suggested_skills={k: list(v) for k, v in data.get("suggested_skills", {}).items()},
            reasoning=data["reasoning"],
            direct_dispatch_params=DirectDispatchParams.from_dict(ddp_data) if ddp_data else None,
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> HighLevelPlan:
        return cls.from_dict(json.loads(s))


# JSON Schema for the HighLevelPlan — embedded in Planning Agent system prompt
HIGH_LEVEL_PLAN_JSON_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["execution_mode", "sub_goals", "suggested_tools", "reasoning"],
    "properties": {
        "execution_mode": {"enum": ["direct", "standard", "complex"]},
        "sub_goals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "description", "reasoning", "suggested_tools", "depends_on"],
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "suggested_tools": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "suggested_tools": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
        "reasoning": {"type": "string"},
        "direct_dispatch_params": {
            "type": ["object", "null"],
            "properties": {
                "prompt": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
                "skills": {"type": "array", "items": {"type": "object"}},
                "model_id": {"type": ["string", "null"]},
            },
            "required": ["prompt", "tools", "skills"],
        },
    },
}
