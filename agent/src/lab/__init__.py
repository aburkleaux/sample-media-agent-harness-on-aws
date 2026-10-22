"""MediaWorks Lab — personal draft workspace.

Exports all public classes and the create_lab_service factory function.
Dependency injection is wired through create_lab_service which reads
configuration from environment variables:
- ASSET_INDEX_TABLE: DynamoDB table name for the Asset_Index
- ARTIFACTS_BUCKET: S3 bucket name for draft content and lab run outputs
- GATEWAY_ENDPOINTS: Comma-separated list of Gateway endpoint URLs

Requirements: 9.1
"""

from .asset_index import AssetIndex
from .chat_agent import LabChatAgent
from .context import LabRequestContext
from .conversational_memory import ConversationalMemory
from .durable_knowledge import DurableKnowledge
from .errors import AuthenticationError, GatewayUnavailableError, ServiceUnavailableError
from .gateway_browser import BrowseResult, GatewayBrowser, GatewayEndpointConfig, ToolInvocationResult
from .path_validator import PathTraversalError
from .readiness import PromotionReadiness
from .s3_user_files_tool import S3UserFilesTool
from .service import LabService, create_lab_service
from .skill_crud import SkillCRUD
from .specialty_crud import SpecialtyCRUD
from .test_harness import TestHarness
from .tool_collection_crud import ToolCollectionCRUD
from .workflow_crud import WorkflowCRUD

__all__ = [
    # Core service and factory
    "LabService",
    "create_lab_service",
    # Identity and context
    "LabRequestContext",
    # CRUD classes
    "SkillCRUD",
    "SpecialtyCRUD",
    "ToolCollectionCRUD",
    "WorkflowCRUD",
    # Cross-cutting components
    "LabChatAgent",
    "TestHarness",
    "PromotionReadiness",
    "GatewayBrowser",
    # Storage primitives
    "S3UserFilesTool",
    "AssetIndex",
    "ConversationalMemory",
    "DurableKnowledge",
    # Supporting types
    "GatewayEndpointConfig",
    "BrowseResult",
    "ToolInvocationResult",
    # Errors
    "AuthenticationError",
    "GatewayUnavailableError",
    "ServiceUnavailableError",
    "PathTraversalError",
]
