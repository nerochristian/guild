"""
Tool execution context — ToolContext, ToolResult, and embed helpers.

These are the data structures that flow between the routing layer,
the tool registry, and individual tool handlers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

import discord

from utils.embeds import compact_kv_lines

if TYPE_CHECKING:
    from .aimoderation import AIModeration
    from .types import Decision


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# ToolResult
# =============================================================================


@dataclass
class ToolResult:
    """Result of executing a moderation tool."""
    success: bool
    message: str
    embed: Optional[discord.Embed] = None
    delete_after: Optional[float] = None
    use_v2: bool = True

    @classmethod
    def ok(
        cls,
        message: str,
        embed: Optional[discord.Embed] = None,
        *,
        use_v2: bool = True,
    ) -> "ToolResult":
        return cls(success=True, message=message, embed=embed, use_v2=use_v2)

    @classmethod
    def fail(cls, message: str, delete_after: float = 15.0) -> "ToolResult":
        description = cls._with_followup(message)
        embed = discord.Embed(
            title="Action Failed",
            description=description,
            color=discord.Color.red(),
        )
        return cls(success=False, message=description, embed=embed, delete_after=delete_after)

    @staticmethod
    def _with_followup(message: str) -> str:
        low = message.lower()
        target_missing = (
            "not found" in low
            or "could not resolve" in low
            or "couldn't resolve" in low
            or "required" in low
            or "who" in low
        )
        if target_missing and ("target" in low or "member" in low or "user" in low):
            return f"{message}\n\nReply with a user mention, user ID, or reply directly to the user's message."
        if "role" in low and ("not found" in low or "required" in low):
            return f"{message}\n\nReply with the exact role name or mention the role."
        if "channel" in low and ("not found" in low or "required" in low or "called" in low):
            return f"{message}\n\nReply with the channel mention, ID, or exact channel name."
        if "message id" in low:
            return f"{message}\n\nReply to the target message or include its message ID."
        return message


# =============================================================================
# ToolContext
# =============================================================================


@dataclass
class ToolContext:
    """
    Single-object context passed to every tool handler.
    Centralises guild, actor, args and the originating message.
    """
    cog: "AIModeration"
    message: discord.Message
    args: Dict[str, Any]
    decision: "Decision"
    guild: discord.Guild
    actor: discord.Member

    def arg(self, key: str, default: Any = None) -> Any:
        return self.args.get(key, default)

    def int_arg(self, key: str, default: int = 0) -> int:
        try:
            return int(self.args[key])
        except (KeyError, TypeError, ValueError):
            return default

    def str_arg(self, key: str, default: str = "No reason provided") -> str:
        val = self.args.get(key)
        return str(val) if val is not None else default

    def bool_arg(self, key: str, default: bool = False) -> bool:
        val = self.args.get(key)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
                return True
            if normalized in {"0", "false", "no", "off", "disable", "disabled", "none", "null"}:
                return False
            return default
        return bool(val)

    async def resolve_target(self) -> Optional[discord.Member]:
        return await self.cog.resolve_member(self.guild, self.args.get("target_user_id"))

    async def resolve_role(self) -> Optional[discord.Role]:
        return await self.cog.resolve_role(self.guild, self.args.get("role_name"))


# =============================================================================
# Embed helpers
# =============================================================================


def action_embed(
    *,
    title: str,
    color: discord.Color,
    actor: discord.Member,
    target: Optional[Union[discord.Member, discord.User]] = None,
    reason: str,
    extra: Optional[Dict[str, str]] = None,
) -> discord.Embed:
    """Build a standardised moderation action embed."""
    embed = discord.Embed(title=title, color=color, timestamp=_now())
    rows: list[tuple[str, object]] = []
    if target:
        embed.set_author(name=target.name, icon_url=target.display_avatar.url)
        embed.set_thumbnail(url=target.display_avatar.url)
        rows.append(("User", f"{target.mention} (`{target.name}`)"))
        embed.set_footer(text=f"User ID: {target.id}")
    rows.append(("Moderator", actor.mention))
    if extra:
        for k, v in extra.items():
            rows.append((k, v))
    rows.append(("Reason", reason))
    embed.description = compact_kv_lines(rows)
    return embed


def parse_hex_color(raw: Optional[str], fallback: discord.Color = discord.Color.default()) -> discord.Color:
    if not raw:
        return fallback
    try:
        return discord.Color(int(raw.lstrip("#"), 16))
    except (ValueError, AttributeError):
        return fallback
