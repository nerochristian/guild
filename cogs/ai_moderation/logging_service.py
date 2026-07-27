"""
Logging service — structured audit logging + Discord embed rendering.

Every request (including rejected ones) is logged to:
1. The database audit_log table (structured, queryable).
2. The configured Discord log channel (human-readable embeds).
3. Python's logging system (developer-facing debug logs).

The service never exposes AI credentials, system prompts, or sensitive
internal details in user-facing logs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import discord

from .database import Database
from .models import ActionType, CaseRecord, CaseStatus
from .utils import format_duration

logger = logging.getLogger("ModBot.AIModeration.Logging")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Action display colors.
_ACTION_COLORS: dict[ActionType, discord.Color] = {
    ActionType.WARN: discord.Color.gold(),
    ActionType.TIMEOUT: discord.Color.orange(),
    ActionType.UNTIMEOUT: discord.Color.green(),
    ActionType.KICK: discord.Color.red(),
    ActionType.BAN: discord.Color.dark_red(),
    ActionType.TEMP_BAN: discord.Color.dark_red(),
    ActionType.UNBAN: discord.Color.green(),
    ActionType.PURGE: discord.Color.blurple(),
    ActionType.DELETE_MESSAGE: discord.Color.blurple(),
    ActionType.ADD_MUTED_ROLE: discord.Color.orange(),
    ActionType.REMOVE_MUTED_ROLE: discord.Color.green(),
    ActionType.ADD_ROLE: discord.Color.blue(),
    ActionType.REMOVE_ROLE: discord.Color.blue(),
    ActionType.SLOWMODE: discord.Color.greyple(),
    ActionType.LOCK_CHANNEL: discord.Color.dark_grey(),
    ActionType.UNLOCK_CHANNEL: discord.Color.green(),
    ActionType.ESCALATE_HUMAN: discord.Color.gold(),
    ActionType.NO_ACTION: discord.Color.light_grey(),
    ActionType.UNDO: discord.Color.green(),
}

# Action emojis (text-based, no custom emoji needed).
_ACTION_EMOJIS: dict[ActionType, str] = {
    ActionType.WARN: "⚠️",
    ActionType.TIMEOUT: "🔇",
    ActionType.UNTIMEOUT: "🔊",
    ActionType.KICK: "👢",
    ActionType.BAN: "🔨",
    ActionType.TEMP_BAN: "🔨",
    ActionType.UNBAN: "🔓",
    ActionType.PURGE: "🧹",
    ActionType.DELETE_MESSAGE: "🗑️",
    ActionType.ADD_MUTED_ROLE: "🔇",
    ActionType.REMOVE_MUTED_ROLE: "🔊",
    ActionType.ADD_ROLE: "➕",
    ActionType.REMOVE_ROLE: "➖",
    ActionType.SLOWMODE: "🐌",
    ActionType.LOCK_CHANNEL: "🔒",
    ActionType.UNLOCK_CHANNEL: "🔓",
    ActionType.ESCALATE_HUMAN: "escalated",
    ActionType.NO_ACTION: "no action",
    ActionType.UNDO: "↩️",
}


class LoggingService:
    """Structured logging + Discord embed rendering.

    The service is the single point through which all moderation events
    flow. It writes to both the DB and Discord, ensuring nothing is lost.
    """

    def __init__(self, db: Database, bot: discord.ext.commands.Bot) -> None:  # type: ignore[name-defined]
        self.db = db
        self.bot = bot

    # ------------------------------------------------------------------
    # Audit logging (always happens, regardless of success)
    # ------------------------------------------------------------------

    async def log_request(
        self,
        *,
        guild_id: int,
        actor_id: int,
        event_type: str,
        case_id: Optional[str] = None,
        raw_instruction: Optional[str] = None,
        parsed_intent: Optional[str] = None,
        action: Optional[str] = None,
        target_id: Optional[int] = None,
        confidence: Optional[float] = None,
        result: str = "",
        error: Optional[str] = None,
    ) -> None:
        """Log a moderation request to the audit table."""
        try:
            await self.db.add_audit_entry(
                guild_id=guild_id,
                actor_id=actor_id,
                event_type=event_type,
                case_id=case_id,
                raw_instruction=raw_instruction,
                parsed_intent=parsed_intent,
                action=action,
                target_id=target_id,
                confidence=confidence,
                result=result,
                error=error,
            )
        except Exception:
            logger.debug("Failed to write audit entry", exc_info=True)

        # Also log to Python logging for developers.
        logger.info(
            "AI Mod audit: guild=%d actor=%d event=%s action=%s target=%s "
            "confidence=%s result=%s error=%s",
            guild_id, actor_id, event_type, action, target_id,
            f"{confidence:.2f}" if confidence is not None else "N/A",
            result[:100], error[:100] if error else "None",
        )

    # ------------------------------------------------------------------
    # Discord embed logging
    # ------------------------------------------------------------------

    async def send_case_embed(
        self,
        case: CaseRecord,
        guild: discord.Guild,
        log_channel_id: Optional[int],
    ) -> None:
        """Send a moderation case embed to the log channel."""
        if not log_channel_id:
            return

        channel = guild.get_channel(log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        embed = self._build_case_embed(case, guild)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "Failed to send case embed to channel %d: %s",
                log_channel_id, exc,
            )

    async def send_investigation_embed(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        investigation_summary: str,
        detected_violation: Optional[str],
        severity: str,
        confidence: float,
        recommended_action: Optional[str],
        log_channel_id: Optional[int],
    ) -> None:
        """Send an investigation result embed to the log channel."""
        if not log_channel_id:
            return
        channel = guild.get_channel(log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        color = discord.Color.blurple()
        if severity == "critical":
            color = discord.Color.dark_red()
        elif severity == "severe":
            color = discord.Color.red()
        elif severity == "moderate":
            color = discord.Color.orange()
        elif severity == "minor":
            color = discord.Color.gold()

        embed = discord.Embed(
            title="🔍 AI Investigation Result",
            color=color,
            timestamp=_now(),
        )
        embed.add_field(
            name="Moderator",
            value=f"{actor.mention} (`{actor.id}`)",
            inline=True,
        )
        if detected_violation:
            embed.add_field(
                name="Detected Violation",
                value=detected_violation,
                inline=True,
            )
        embed.add_field(name="Severity", value=severity.title(), inline=True)
        embed.add_field(
            name="Confidence",
            value=f"{confidence * 100:.0f}%",
            inline=True,
        )
        if recommended_action:
            embed.add_field(
                name="Recommended Action",
                value=recommended_action.replace("_", " ").title(),
                inline=True,
            )
        embed.add_field(
            name="Summary",
            value=investigation_summary[:1024] or "No summary provided.",
            inline=False,
        )
        embed.set_footer(text="AI Moderation Investigation")

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to send investigation embed: %s", exc)

    async def send_rejection_embed(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        instruction: str,
        reason: str,
        log_channel_id: Optional[int],
    ) -> None:
        """Send a rejection embed when a request is rejected."""
        if not log_channel_id:
            return
        channel = guild.get_channel(log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title="❌ AI Moderation Request Rejected",
            color=discord.Color.red(),
            timestamp=_now(),
        )
        embed.add_field(
            name="Moderator",
            value=f"{actor.mention} (`{actor.id}`)",
            inline=True,
        )
        embed.add_field(
            name="Instruction",
            value=f"```{instruction[:500]}```",
            inline=False,
        )
        embed.add_field(name="Rejection Reason", value=reason[:1024], inline=False)
        embed.set_footer(text="AI Moderation — Rejected Request")

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Embed builders
    # ------------------------------------------------------------------

    def _build_case_embed(
        self, case: CaseRecord, guild: discord.Guild
    ) -> discord.Embed:
        """Build a Discord embed for a moderation case."""
        action = case.action
        color = _ACTION_COLORS.get(action, discord.Color.blurple())
        emoji = _ACTION_EMOJIS.get(action, "")

        status_emoji = {
            CaseStatus.COMPLETED: "✅",
            CaseStatus.FAILED: "❌",
            CaseStatus.CANCELLED: "🚫",
            CaseStatus.EXPIRED: "⏰",
            CaseStatus.UNDONE: "↩️",
            CaseStatus.PENDING: "⏳",
            CaseStatus.CONFIRMED: "✔️",
            CaseStatus.EXECUTING: "🔄",
            CaseStatus.APPEALED: "📋",
        }.get(case.status, "❓")

        title = f"{emoji} {action.value.replace('_', ' ').title()} — {status_emoji}"

        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=case.created_at,
        )

        # Actor.
        actor = guild.get_member(case.actor_id)
        actor_name = actor.mention if actor else f"`{case.actor_id}`"
        embed.add_field(name="Moderator", value=actor_name, inline=True)

        # Target.
        if case.target_id:
            target = guild.get_member(case.target_id)
            target_name = target.mention if target else f"`{case.target_id}`"
            embed.add_field(name="Target", value=target_name, inline=True)

        # Status.
        embed.add_field(
            name="Status",
            value=case.status.value.replace("_", " ").title(),
            inline=True,
        )

        # Duration.
        if case.duration_seconds:
            embed.add_field(
                name="Duration",
                value=format_duration(case.duration_seconds),
                inline=True,
            )

        # Confidence.
        if case.confidence > 0:
            embed.add_field(
                name="AI Confidence",
                value=f"{case.confidence * 100:.0f}%",
                inline=True,
            )

        # Case ID.
        embed.add_field(name="Case ID", value=f"`{case.case_id}`", inline=True)

        # Reason.
        if case.reason:
            embed.add_field(
                name="Reason",
                value=case.reason[:1024],
                inline=False,
            )

        # AI summary.
        if case.ai_summary:
            embed.add_field(
                name="AI Summary",
                value=case.ai_summary[:1024],
                inline=False,
            )

        # Error.
        if case.error:
            embed.add_field(
                name="Error",
                value=f"```{case.error[:1000]}```",
                inline=False,
            )

        # Evidence.
        if case.evidence_message_ids:
            evidence_text = ", ".join(
                f"`{mid}`" for mid in case.evidence_message_ids[:10]
            )
            if len(case.evidence_message_ids) > 10:
                evidence_text += f" (+{len(case.evidence_message_ids) - 10} more)"
            embed.add_field(name="Evidence Messages", value=evidence_text, inline=False)

        embed.set_footer(text=f"AI Moderation • Case {case.case_id}")
        return embed

    # ------------------------------------------------------------------
    # User-facing result messages
    # ------------------------------------------------------------------

    @staticmethod
    def build_result_message(
        case: CaseRecord,
        target_name: Optional[str] = None,
    ) -> str:
        """Build a concise user-facing result message."""
        action_name = case.action.value.replace("_", " ")
        target_str = target_name or (f"<@{case.target_id}>" if case.target_id else "the channel")

        if case.status == CaseStatus.COMPLETED:
            if case.duration_seconds:
                return (
                    f"Done! {action_name.title()} applied to {target_str} "
                    f"for {format_duration(case.duration_seconds)}."
                )
            return f"Done! {action_name.title()} applied to {target_str}."
        elif case.status == CaseStatus.FAILED:
            return f"Failed to {action_name} {target_str}: {case.error or 'unknown error'}"
        elif case.status == CaseStatus.CANCELLED:
            return f"Action cancelled: {action_name} on {target_str} was not executed."
        elif case.status == CaseStatus.UNDONE:
            return f"Reversed: {action_name} on {target_str} has been undone."
        else:
            return f"Action {action_name} on {target_str} is now {case.status.value}."
