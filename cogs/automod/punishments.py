"""Punishment execution for AutoMod."""

from __future__ import annotations

from typing import Any, Optional

import discord

from .models import Action, RuleMatch
from .utils import bot_can_act_on, compact_duration, timeout_delta


class PunishmentResult:
    def __init__(self, action: Action, *, case_number: Optional[int] = None, error: Optional[str] = None) -> None:
        self.action = action
        self.case_number = case_number
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None


class PunishmentManager:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot

    async def apply(
        self,
        guild: discord.Guild,
        member: discord.Member,
        action: Action,
        match: RuleMatch,
        settings: dict[str, Any],
        *,
        duration_override: Optional[int] = None,
    ) -> PunishmentResult:
        if action in {Action.NONE, Action.LOG}:
            return PunishmentResult(action)
        action = Action.TIMEOUT if action is Action.MUTE else action
        allowed, reason = bot_can_act_on(member, action.value)
        if not allowed:
            return PunishmentResult(action, error=reason)

        reason_text = f"AutoMod {match.rule}: {match.reason}"
        duration = int(duration_override or settings.get("automod_mute_duration", 3600))
        try:
            if action is Action.WARN:
                warning_id, total = await self._add_warning(guild.id, member.id, reason_text)
                case_number = await self._create_case(guild.id, member.id, "Warn", reason_text)
                threshold_result = await self._apply_warning_thresholds(guild, member, total, settings)
                return PunishmentResult(threshold_result or action, case_number=case_number or warning_id)
            if action is Action.TIMEOUT:
                await member.timeout(timeout_delta(duration), reason=reason_text)
                case_number = await self._create_case(guild.id, member.id, "Mute", reason_text, compact_duration(duration))
                return PunishmentResult(action, case_number=case_number)
            if action is Action.KICK:
                await member.kick(reason=reason_text)
                case_number = await self._create_case(guild.id, member.id, "Kick", reason_text)
                return PunishmentResult(action, case_number=case_number)
            if action is Action.BAN:
                delete_days = max(0, min(7, int(settings.get("automod_ban_delete_days", 1))))
                await guild.ban(member, reason=reason_text, delete_message_days=delete_days)
                case_number = await self._create_case(guild.id, member.id, "Ban", reason_text)
                return PunishmentResult(action, case_number=case_number)
        except discord.Forbidden:
            return PunishmentResult(action, error="Discord denied the action. Check bot role position and permissions.")
        except discord.HTTPException as exc:
            return PunishmentResult(action, error=f"Discord API error: {exc}")
        return PunishmentResult(action, error=f"Unsupported action: {action.value}")

    async def _add_warning(self, guild_id: int, user_id: int, reason: str) -> tuple[int, int]:
        db = getattr(self.bot, "db", None)
        bot_user = getattr(self.bot, "user", None)
        moderator_id = int(getattr(bot_user, "id", 0) or user_id)
        if db is not None and hasattr(db, "add_warning"):
            return await db.add_warning(guild_id, user_id, moderator_id, reason)
        return 0, 1

    async def _create_case(self, guild_id: int, user_id: int, action: str, reason: str, duration: Optional[str] = None) -> Optional[int]:
        db = getattr(self.bot, "db", None)
        bot_user = getattr(self.bot, "user", None)
        moderator_id = int(getattr(bot_user, "id", 0) or user_id)
        if db is not None and hasattr(db, "create_case"):
            return await db.create_case(guild_id, user_id, moderator_id, action, reason, duration)
        return None

    async def _apply_warning_thresholds(
        self,
        guild: discord.Guild,
        member: discord.Member,
        total_warnings: int,
        settings: dict[str, Any],
    ) -> Optional[Action]:
        if not settings.get("warn_thresholds_enabled", True):
            return None
        reason = f"AutoMod escalation: {total_warnings} warnings reached"
        try:
            ban_at = int(settings.get("warn_threshold_ban") or 0)
            kick_at = int(settings.get("warn_threshold_kick") or 0)
            mute_at = int(settings.get("warn_threshold_mute") or 0)
            if ban_at and total_warnings >= ban_at:
                await guild.ban(member, reason=reason, delete_message_days=max(0, min(7, int(settings.get("automod_ban_delete_days", 1)))))
                await self._create_case(guild.id, member.id, "Ban", reason)
                return Action.BAN
            if kick_at and total_warnings >= kick_at:
                await member.kick(reason=reason)
                await self._create_case(guild.id, member.id, "Kick", reason)
                return Action.KICK
            if mute_at and total_warnings >= mute_at:
                duration = int(settings.get("warn_mute_duration", 3600))
                await member.timeout(timeout_delta(duration), reason=reason)
                await self._create_case(guild.id, member.id, "Mute", reason, compact_duration(duration))
                return Action.TIMEOUT
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            return None
        return None
