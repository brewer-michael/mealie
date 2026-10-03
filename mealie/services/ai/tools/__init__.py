"""
The AI tool registry (docs/ai/PHASE1.md §5): one definition of each kitchen action, callable over REST
(`/api/ai/tools`) now, and over MCP and by the meal-planning agent later. Argument and result models live
here rather than in `mealie.schema`, since the frontend never calls them.
"""

from .base import AITool, ToolArgs, ToolContext, ToolError, ToolNotFoundError, ToolResult
from .registry import all_tools, get_tool

__all__ = [
    "AITool",
    "ToolArgs",
    "ToolContext",
    "ToolError",
    "ToolNotFoundError",
    "ToolResult",
    "all_tools",
    "get_tool",
]
