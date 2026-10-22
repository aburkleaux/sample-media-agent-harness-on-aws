"""AWS MCP Server proxy deployed as an AgentCore Runtime MCP server.

Uses mcp-proxy-for-aws to handle SigV4 authentication to the upstream
AWS MCP Server, then re-exposes its tools via streamable-HTTP for the Gateway.

Preserves the original tool schemas from the upstream server so the agent
knows exactly what parameters each tool expects.
"""

import asyncio
import json
import logging
import os

from mcp import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.types import Tool, TextContent, CallToolResult
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AWS_MCP_ENDPOINT = os.environ.get(
    "AWS_MCP_ENDPOINT", "https://aws-mcp.us-east-1.api.aws/mcp"
)
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

mcp = FastMCP(name="aws-mcp-proxy", host="0.0.0.0", stateless_http=True)

# Store discovered tool definitions with their original schemas
_upstream_tools: list[Tool] = []


async def _call_upstream(tool_name: str, arguments: dict) -> str:
    """Call a tool on the upstream AWS MCP Server with SigV4 auth."""
    async with aws_iam_streamablehttp_client(
        endpoint=AWS_MCP_ENDPOINT,
        aws_region="us-east-1",
        aws_service="aws-mcp",
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            texts = []
            if hasattr(result, "content"):
                for block in result.content:
                    if hasattr(block, "text"):
                        texts.append(block.text)
            return "\n".join(texts) if texts else str(result)


async def _discover_upstream_tools():
    """Discover tools from the upstream AWS MCP Server."""
    global _upstream_tools
    logger.info("Discovering tools from %s (with SigV4 auth)...", AWS_MCP_ENDPOINT)
    try:
        async with aws_iam_streamablehttp_client(
            endpoint=AWS_MCP_ENDPOINT,
            aws_region="us-east-1",
            aws_service="aws-mcp",
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                _upstream_tools = list(result.tools)
                logger.info(
                    "Discovered %d tools from upstream AWS MCP Server",
                    len(_upstream_tools),
                )
    except Exception as e:
        logger.error("Failed to discover upstream tools: %s", e, exc_info=True)
        if hasattr(e, "exceptions"):
            for i, sub in enumerate(e.exceptions):
                logger.error("  Sub-exception %d: %s", i, sub, exc_info=sub)
        _upstream_tools = []


def _register_tools_on_server():
    """Register upstream tools on the low-level MCP server with original schemas.

    Instead of using @mcp.tool (which inspects function signatures),
    we override the list_tools and call_tool handlers directly to
    preserve the exact input schemas from the upstream server.
    """
    # Get the underlying low-level server from FastMCP
    low_level: Server = mcp._mcp_server

    # Store the original handlers so we can chain them
    original_list_tools = None
    original_call_tool = None

    if hasattr(low_level, "_request_handlers"):
        original_list_tools = low_level._request_handlers.get("tools/list")
        original_call_tool = low_level._request_handlers.get("tools/call")

    upstream_tool_names = {t.name for t in _upstream_tools}

    @low_level.list_tools()
    async def list_tools() -> list[Tool]:
        # Start with upstream tools (with original schemas)
        tools = list(_upstream_tools)
        # Also include any tools registered via @mcp.tool (like fallback)
        try:
            fastmcp_tools = await mcp._tool_manager.list_tools()
            for ft in fastmcp_tools:
                if ft.name not in upstream_tool_names:
                    tools.append(ft)
        except Exception:
            pass
        return tools

    @low_level.call_tool()
    async def call_tool(name: str, arguments: dict | None = None) -> list[TextContent]:
        if name in upstream_tool_names:
            # Proxy to upstream
            result_text = await _call_upstream(name, arguments or {})
            return [TextContent(type="text", text=result_text)]
        else:
            # Delegate to FastMCP's tool manager
            result = await mcp._tool_manager.call_tool(name, arguments or {})
            return result


if __name__ == "__main__":
    asyncio.run(_discover_upstream_tools())

    if _upstream_tools:
        _register_tools_on_server()
        logger.info("Registered %d upstream tools with original schemas", len(_upstream_tools))
    else:
        # Fallback
        @mcp.tool(
            name="aws_tool_proxy",
            description="Proxy any tool call to the AWS MCP Server",
        )
        async def fallback(tool_name: str, arguments: str = "{}") -> str:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            return await _call_upstream(tool_name, args)
        logger.info("Registered fallback proxy tool")

    mcp.run(transport="streamable-http")
