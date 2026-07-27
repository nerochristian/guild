"""
Modern tool registry — handler registration, metadata, and executor.

Tools register themselves via the @tool_handler decorator with rich metadata.
The registry validates permissions and dispatches execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional

import discord

from .types import ToolType

if TYPE_CHECKING:
    from .aimoderation import AIModeration
    from .context import ToolResult
    from .types import Decision

logger = logging.getLogger("ModBot.AIModeration.Registry")


# =============================================================================
# Tool handler signature
# =============================================================================


class ToolHandler:
    """Protocol for tool handler functions.
    
    Must be async, accept ToolContext, return ToolResult.
    """
    pass


@dataclass
class ToolMeta:
    """Rich metadata for a registered tool handler."""
    display_name: str
    color: discord.Color
    emoji: str
    required_permission: Optional[str] = None
    category: str = "moderation"
    description: str = ""


# =============================================================================
# Registry
# =============================================================================


class ToolRegistry:
    """Central registry for AI moderation tool handlers.
    
    Usage:
        @ToolRegistry.register(ToolType.WARN, display_name="Warn", color=..., emoji="⚠️")
        async def handle_warn(ctx: ToolContext) -> ToolResult:
            ...
    """

    _handlers: ClassVar[Dict[ToolType, Callable]] = {}
    _metadata: ClassVar[Dict[ToolType, ToolMeta]] = {}
    _categories: ClassVar[Dict[str, List[ToolType]]] = {}

    @classmethod
    def register(
        cls,
        tool: ToolType,
        *,
        display_name: str,
        color: discord.Color,
        emoji: str,
        required_permission: Optional[str] = None,
        category: str = "moderation",
        description: str = "",
    ) -> Callable:
        """Decorator: register a handler function for the given tool type."""

        def decorator(func: Callable) -> Callable:
            previous = cls._metadata.get(tool)
            if previous:
                previous_category = cls._categories.get(previous.category, [])
                cls._categories[previous.category] = [item for item in previous_category if item != tool]
            cls._handlers[tool] = func
            cls._metadata[tool] = ToolMeta(
                display_name=display_name,
                color=color,
                emoji=emoji,
                required_permission=required_permission,
                category=category,
                description=description,
            )
            category_tools = cls._categories.setdefault(category, [])
            if tool not in category_tools:
                category_tools.append(tool)
            return func

        return decorator

    @classmethod
    def get_handler(cls, tool: ToolType) -> Optional[Callable]:
        return cls._handlers.get(tool)

    @classmethod
    def get_metadata(cls, tool: ToolType) -> ToolMeta:
        return cls._metadata.get(
            tool,
            ToolMeta(
                display_name=tool.value,
                color=discord.Color.orange(),
                emoji="Bot",
            ),
        )

    @classmethod
    def list_tools(cls, *, category: Optional[str] = None) -> List[ToolType]:
        """List registered tools, optionally filtered by category."""
        if category:
            return cls._categories.get(category, [])
        return list(cls._handlers.keys())

    @classmethod
    async def execute(
        cls,
        tool: ToolType,
        cog: "AIModeration",
        message: discord.Message,
        args: Dict[str, Any],
        decision: "Decision",
    ) -> "ToolResult":
        """Execute a tool handler with proper context and error handling."""
        from .context import ToolContext, ToolResult

        handler = cls.get_handler(tool)
        if not handler:
            return ToolResult.fail(f"No handler registered for `{tool.value}`.")

        if not message.guild:
            return ToolResult.fail("This action can only be used in a server.")

        if not isinstance(message.author, discord.Member):
            return ToolResult.fail("Could not verify your server membership.")

        access_error = cog.validate_tool_access(message.author, message.guild, tool)
        if access_error:
            return ToolResult.fail(access_error)

        ctx = ToolContext(
            cog=cog,
            message=message,
            args=args,
            decision=decision,
            guild=message.guild,
            actor=message.author,
        )

        try:
            return await handler(ctx)
        except discord.Forbidden as e:
            return ToolResult.fail(f"Missing Discord permissions: {e}")
        except discord.HTTPException as e:
            return ToolResult.fail(f"Discord error ({e.status}): {e.text}")
        except Exception as e:
            logger.exception("Unhandled error in tool %s", tool.value)
            return ToolResult.fail(f"Unexpected error: {type(e).__name__}")
