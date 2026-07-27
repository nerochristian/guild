"""
AI Moderation subpackage (clean architecture).

Modular files:
- types.py         — Enums, dataclasses, constants
- prompts.py       — System prompts
- context.py       — ToolContext, ToolResult, embed helpers
- registry.py      — ToolRegistry with @register decorator
- ai_client.py     — GeminiClient (DeepSeek Web + DigitalOcean providers)
- aimoderation.py  — AIModeration cog (thin wrapper)
- bridge.py        — Delegation to Moderation cog
- handlers/        — Tool handler functions
"""
from .types import (
    ToolType, DecisionType, ConversationMode,
    TARGETED_TOOLS, REASONED_MODERATION_TOOLS, MAX_MODERATION_REASON_LENGTH,
    AIConfig, GuildSettings, Decision, ConversationSignals, ConversationPlan,
    WebSearchResult, ImageContext, PermissionFlags, MentionInfo,
    _default_ai_provider, _default_ai_model,
)
from .prompts import (
    ROUTING_SYSTEM_PROMPT, CONVERSATION_SYSTEM_PROMPT,
    DEEP_RESEARCH_SYSTEM_PROMPT, MOD_GUIDANCE_SYSTEM_PROMPT,
)
from .context import ToolResult, ToolContext, action_embed, parse_hex_color
from .registry import ToolRegistry
from .ai_client import GeminiClient
from .aimoderation import AIModeration, setup

__all__ = [
    "AIConfig",
    "AIModeration",
    "CONVERSATION_SYSTEM_PROMPT",
    "ConversationMode",
    "ConversationPlan",
    "ConversationSignals",
    "DEEP_RESEARCH_SYSTEM_PROMPT",
    "Decision",
    "DecisionType",
    "GeminiClient",
    "GuildSettings",
    "ImageContext",
    "MAX_MODERATION_REASON_LENGTH",
    "MOD_GUIDANCE_SYSTEM_PROMPT",
    "MentionInfo",
    "PermissionFlags",
    "REASONED_MODERATION_TOOLS",
    "ROUTING_SYSTEM_PROMPT",
    "TARGETED_TOOLS",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolType",
    "WebSearchResult",
    "_default_ai_model",
    "_default_ai_provider",
    "action_embed",
    "parse_hex_color",
    "setup",
]

# Handler registration (side-effect: populates ToolRegistry)
from . import handlers  # noqa: F401
from . import bridge  # noqa: F401
