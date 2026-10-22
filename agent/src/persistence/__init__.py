"""OMS workflow-persistence tool — "perennializes" a discovered workflow.

The `Persistence_Tool` reads the harness's already-persisted `HighLevelPlan`,
resolves each discovered tool reference to an OMS Core `service.action`
Capability via a deterministic join, assembles an OMS workflow template that
preserves the plan's dependency graph, validates it, and saves it as an inert
draft. Activation is always a separate, human-confirmed step.

This package re-exports the pure data models and typed errors from `models`.
Later tasks add the plan reader, capability resolver, template assembler,
validator gate, save/activate driver, MCP client adapter, service composition,
and the `@tool` layer.

Requirements: 2.4, 5.1, 5.2, 12.2, 12.3
"""

from .models import (
    ActivationNotConfirmedError,
    AmbiguousStepError,
    Capability,
    CapabilityIndex,
    EmptyConditionsError,
    MappedStep,
    MissingPlanError,
    OMSTask,
    OMSTemplate,
    OMSToolError,
    OMSUnavailableError,
    PersistenceError,
    PersistenceOperation,
    PersistenceReport,
    PersistenceStatus,
    PlanParseError,
    RegistryCoverageError,
    ResolutionReport,
    TemplateValidationError,
    UnmappableStepsError,
    UnpublishedToolNameError,
    WorkflowStep,
)
from .oms_mcp_client import OMS_MCP_TOOLS, OMSMcpClient
from .plan_reader import PlanReader, extract_steps

__all__ = [
    # Literals
    "PersistenceStatus",
    "PersistenceOperation",
    # Value objects / reports
    "WorkflowStep",
    "Capability",
    "CapabilityIndex",
    "MappedStep",
    "ResolutionReport",
    "OMSTask",
    "OMSTemplate",
    "PersistenceReport",
    # Errors
    "PersistenceError",
    "MissingPlanError",
    "PlanParseError",
    "UnmappableStepsError",
    "AmbiguousStepError",
    "UnpublishedToolNameError",
    "EmptyConditionsError",
    "RegistryCoverageError",
    "TemplateValidationError",
    "ActivationNotConfirmedError",
    "OMSUnavailableError",
    "OMSToolError",
    # MCP client adapter
    "OMSMcpClient",
    "OMS_MCP_TOOLS",
    # Plan reader (pure normalization)
    "PlanReader",
    "extract_steps",
]
