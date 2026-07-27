"""
AI Moderation types — enums, dataclasses, and constants.

Extracted from cogs/aimoderation.py into cogs/moderation/ai/types.py
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Final, Optional, Set, Tuple

if TYPE_CHECKING:
    import discord


# =============================================================================
# ENUMS
# =============================================================================


class ToolType(str, Enum):
    GET_WARNINGS = "get_warnings"
    WARN = "warn_member"
    TIMEOUT = "timeout_member"
    UNTIMEOUT = "untimeout_member"
    KICK = "kick_member"
    BAN = "ban_member"
    UNBAN = "unban_member"
    PURGE = "purge_messages"
    ADD_ROLE = "add_role"
    REMOVE_ROLE = "remove_role"
    CREATE_ROLE = "create_role"
    DELETE_ROLE = "delete_role"
    EDIT_ROLE = "edit_role"
    CREATE_CHANNEL = "create_channel"
    DELETE_CHANNEL = "delete_channel"
    EDIT_CHANNEL = "edit_channel"
    LOCK_CHANNEL = "lock_channel"
    UNLOCK_CHANNEL = "unlock_channel"
    SET_NICKNAME = "set_nickname"
    LOCK_THREAD = "lock_thread"
    MOVE_MEMBER = "move_member"
    DISCONNECT_MEMBER = "disconnect_member"
    DM_USER = "dm_user"
    EDIT_GUILD = "edit_guild"
    CREATE_EMOJI = "create_emoji"
    DELETE_EMOJI = "delete_emoji"
    CREATE_INVITE = "create_invite"
    PIN_MESSAGE = "pin_message"
    UNPIN_MESSAGE = "unpin_message"
    EXECUTE_RAW_API = "execute_raw_api"
    EXECUTE_PYTHON = "execute_python"
    HELP = "show_help"
    FIND_INACTIVE_MEMBERS = "find_inactive_members"
    SCAN_CHANNEL = "scan_channel"
    SUMMARIZE_ACTIONS = "summarize_actions"
    SAFETY_CHECK = "server_safety_check"


class DecisionType(str, Enum):
    TOOL_CALL = "tool_call"
    CHAT = "chat"
    ERROR = "error"


class ConversationMode(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"
    RESEARCH = "research"
    MOD_GUIDANCE = "mod_guidance"


# =============================================================================
# TOOL SETS
# =============================================================================


TARGETED_TOOLS: Final[Set[ToolType]] = {
    ToolType.WARN, ToolType.TIMEOUT, ToolType.UNTIMEOUT,
    ToolType.KICK, ToolType.BAN, ToolType.UNBAN,
    ToolType.ADD_ROLE, ToolType.REMOVE_ROLE,
    ToolType.SET_NICKNAME, ToolType.MOVE_MEMBER, ToolType.DISCONNECT_MEMBER,
    ToolType.DM_USER, ToolType.GET_WARNINGS, ToolType.PURGE,
}

REASONED_MODERATION_TOOLS: Final[Set[ToolType]] = {
    ToolType.WARN,
    ToolType.TIMEOUT,
    ToolType.UNTIMEOUT,
    ToolType.KICK,
    ToolType.BAN,
    ToolType.UNBAN,
    ToolType.PURGE,
}

MAX_MODERATION_REASON_LENGTH: Final[int] = 140


# =============================================================================
# CONFIGURATION DATACLASSES
# =============================================================================


def _default_ai_provider() -> str:
    explicit = (os.getenv("AI_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    return "deepseek-web"


def _default_ai_model() -> str:
    explicit = (os.getenv("AI_MODEL") or "").strip()
    if explicit:
        return explicit
    if _default_ai_provider() == "digitalocean":
        return (
            os.getenv("DO_AIMOD_MODEL")
            or os.getenv("DO_CHAT_MODEL")
            or os.getenv("DO_AUTOMOD_MODEL")
            or "deepseek-4-flash"
        ).strip()
    return "deepseek-web"


@dataclass(frozen=True)
class AIConfig:
    """Immutable configuration for AI moderation system."""
    provider: str = field(default_factory=_default_ai_provider)
    model: str = field(default_factory=_default_ai_model)
    temperature_routing: float = 0.2
    temperature_chat: float = 0.85
    max_tokens_routing: int = 1500
    max_tokens_chat: int = 1800
    memory_window: int = 50
    memory_max_chars: int = 32_000
    context_messages: int = 30
    rate_limit_calls: int = 30
    rate_limit_window: int = 60
    timeout_max_seconds: int = 259_200    # 3 days
    timeout_default_seconds: int = 3_600  # 1 hour
    confirm_timeout_seconds: int = 25
    proactive_chance: float = 0.02
    confirm_actions: frozenset = field(
        default_factory=lambda: frozenset({"ban_member", "kick_member", "purge_messages"})
    )
    target_cache_ttl_minutes: int = 15


@dataclass
class GuildSettings:
    """Per-guild AI moderation settings."""
    enabled: bool = False
    chat_enabled: bool = False
    model: Optional[str] = None
    context_messages: int = 30
    confirm_enabled: bool = True
    confirm_timeout_seconds: int = 25
    confirm_actions: Set[str] = field(
        default_factory=lambda: {
            "ban_member",
            "kick_member",
            "purge_messages",
            "delete_channel",
            "delete_role",
            "delete_emoji",
            "edit_guild",
            "execute_raw_api",
            "execute_python",
        }
    )
    proactive_chance: float = 0.02
    location_context: str = ""

    _VALID_ACTIONS: ClassVar[Set[str]] = {t.value for t in ToolType}
    _DEFAULT_CONFIRM_ACTIONS: ClassVar[Set[str]] = {
        "ban_member",
        "kick_member",
        "purge_messages",
        "delete_channel",
        "delete_role",
        "delete_emoji",
        "edit_guild",
        "execute_raw_api",
        "execute_python",
    }

    @classmethod
    def _coerce_confirm_actions(cls, raw: Any) -> Set[str]:
        if raw is None:
            return set(cls._DEFAULT_CONFIRM_ACTIONS)
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return set(cls._DEFAULT_CONFIRM_ACTIONS)
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = [s.strip() for s in raw.split(",") if s.strip()]
        if isinstance(raw, (list, tuple, set, frozenset)):
            result = {str(x).strip() for x in raw if str(x).strip() in cls._VALID_ACTIONS}
            return result or set(cls._DEFAULT_CONFIRM_ACTIONS)
        return set(cls._DEFAULT_CONFIRM_ACTIONS)

    @staticmethod
    def _coerce_bool(raw: Any, default: bool) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return default

    @staticmethod
    def _coerce_int(raw: Any, default: int, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    @staticmethod
    def _coerce_float(raw: Any, default: float, *, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GuildSettings":
        return cls(
            enabled=cls._coerce_bool(data.get("aimod_enabled", False), False),
            chat_enabled=cls._coerce_bool(data.get("aimod_chat_enabled", False), False),
            model=data.get("aimod_model"),
            context_messages=cls._coerce_int(data.get("aimod_context_messages", 30), 30, minimum=1, maximum=50),
            confirm_enabled=cls._coerce_bool(
                data.get("aimod_confirm_enabled", True), True
            ),
            confirm_timeout_seconds=cls._coerce_int(data.get("aimod_confirm_timeout_seconds", 25), 25, minimum=1, maximum=300),
            confirm_actions=cls._coerce_confirm_actions(
                data.get("aimod_confirm_actions")
            ),
            proactive_chance=cls._coerce_float(data.get("aimod_proactive_chance", 0.02), 0.02, minimum=0.0, maximum=1.0),
            location_context=str(data.get("aimod_location_context") or data.get("server_location") or "").strip(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "aimod_enabled": self.enabled,
            "aimod_chat_enabled": self.chat_enabled,
            "aimod_model": self.model,
            "aimod_context_messages": self.context_messages,
            "aimod_confirm_enabled": self.confirm_enabled,
            "aimod_confirm_timeout_seconds": self.confirm_timeout_seconds,
            "aimod_confirm_actions": sorted(self.confirm_actions),
            "aimod_proactive_chance": self.proactive_chance,
            "aimod_location_context": self.location_context,
        }


# =============================================================================
# DECISION DATACLASS
# =============================================================================


@dataclass
class Decision:
    """AI router output."""
    type: DecisionType
    reason: str
    tool: Optional[ToolType] = None
    arguments: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Decision":
        raw_type = str(data.get("type") or "").strip().lower()
        dec_type = DecisionType.ERROR
        if raw_type == "tool_call":
            dec_type = DecisionType.TOOL_CALL
        elif raw_type == "chat":
            dec_type = DecisionType.CHAT

        raw_tool = str(data.get("tool") or "").strip()
        tool = None
        if raw_tool:
            try:
                tool = ToolType(raw_tool)
            except ValueError:
                pass

        raw_arguments = data.get("arguments")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}

        return cls(
            type=dec_type,
            reason=str(data.get("reason") or ""),
            tool=tool,
            arguments=arguments,
        )

    @classmethod
    def error(cls, reason: str) -> "Decision":
        return cls(type=DecisionType.ERROR, reason=reason)

    @classmethod
    def chat(cls, reason: str = "") -> "Decision":
        return cls(type=DecisionType.CHAT, reason=reason)


# =============================================================================
# CONVERSATION DATACLASSES
# =============================================================================


@dataclass(frozen=True)
class ConversationSignals:
    """Conversation classifier output."""
    mode: ConversationMode
    confidence: float
    show_research_indicator: bool = False
    asks_for_current_info: bool = False
    asks_for_sources: bool = False
    asks_for_long_answer: bool = False
    mentions_moderation: bool = False
    focus_entities: Tuple[str, ...] = ()

    @property
    def research(self) -> bool:
        """Backward-compatible research-mode flag."""
        return self.mode == ConversationMode.RESEARCH

    @property
    def sources(self) -> bool:
        """Backward-compatible source-request flag."""
        return self.asks_for_sources

    @property
    def long_answer(self) -> bool:
        """Backward-compatible long-answer flag."""
        return self.asks_for_long_answer


@dataclass(frozen=True)
class ConversationPlan:
    """Assembled prompt plan for a conversation turn."""
    system_prompt: str
    user_prompt: str
    temperature: float
    max_tokens: int
    show_research_indicator: bool

    @property
    def show_indicator(self) -> bool:
        """Backward-compatible research-indicator flag."""
        return self.show_research_indicator


@dataclass(frozen=True)
class WebSearchResult:
    """Search hit from a web search provider."""
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class ImageContext:
    """Image data for multimodal model requests."""
    label: str
    filename: str
    mime_type: str
    data: bytes

    @property
    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{__import__('base64').b64encode(self.data).decode()}"


# =============================================================================
# PERMISSION & MENTION DATACLASSES
# =============================================================================


@dataclass
class PermissionFlags:
    """Guild permission flags derived from a member."""
    administrator: bool = False
    ban_members: bool = False
    kick_members: bool = False
    manage_guild: bool = False
    manage_channels: bool = False
    manage_roles: bool = False
    manage_messages: bool = False
    manage_threads: bool = False
    manage_nicknames: bool = False
    manage_emojis: bool = False
    create_instant_invite: bool = False
    move_members: bool = False
    moderate_members: bool = False
    mute_members: bool = False

    @classmethod
    def from_member(cls, member: "discord.Member") -> "PermissionFlags":
        perms = member.guild_permissions
        return cls(
            administrator=perms.administrator,
            ban_members=perms.ban_members,
            kick_members=perms.kick_members,
            manage_guild=perms.manage_guild,
            manage_channels=perms.manage_channels,
            manage_roles=perms.manage_roles,
            manage_messages=perms.manage_messages,
            manage_threads=perms.manage_threads,
            manage_nicknames=perms.manage_nicknames,
            manage_emojis=perms.manage_emojis_and_stickers,
            create_instant_invite=perms.create_instant_invite,
            move_members=perms.move_members,
            moderate_members=perms.moderate_members,
            mute_members=perms.mute_members,
        )

    @classmethod
    def superuser(cls) -> "PermissionFlags":
        return cls(
            administrator=True, ban_members=True, kick_members=True,
            manage_guild=True, manage_channels=True, manage_roles=True,
            manage_messages=True, manage_threads=True, manage_nicknames=True,
            manage_emojis=True, create_instant_invite=True, move_members=True,
            moderate_members=True, mute_members=True,
        )

    def to_dict(self) -> Dict[str, bool]:
        return {
            "administrator": self.administrator,
            "ban_members": self.ban_members,
            "kick_members": self.kick_members,
            "manage_guild": self.manage_guild,
            "manage_channels": self.manage_channels,
            "manage_roles": self.manage_roles,
            "manage_messages": self.manage_messages,
            "manage_threads": self.manage_threads,
            "manage_nicknames": self.manage_nicknames,
            "manage_emojis": self.manage_emojis,
            "create_instant_invite": self.create_instant_invite,
            "move_members": self.move_members,
            "moderate_members": self.moderate_members,
            "mute_members": self.mute_members,
        }


@dataclass
class MentionInfo:
    """Metadata about a mention found in a message."""
    index: int
    user_id: int
    is_bot: bool = False
    display_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "id": self.user_id, "is_bot": self.is_bot, "display": self.display_name}
