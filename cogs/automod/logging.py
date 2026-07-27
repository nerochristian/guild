"""AutoMod logging helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord

from config import Config
from .models import Action, RuleMatch


def _trim(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text or "None"
    return text[: max(0, limit - 3)].rstrip() + "..."


class AutoModLogger:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot

    async def log_message_action(
        self,
        message: discord.Message,
        match: RuleMatch,
        action: Action,
        *,
        deleted: bool,
        case_number: Optional[int] = None,
        error: Optional[str] = None,
        offense_count: Optional[int] = None,
    ) -> None:
        if message.guild is None:
            return
        embed = discord.Embed(
            title="AutoMod Action",
            color=getattr(Config, "COLOR_WARNING", 0xF59E0B),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{message.author.mention}\n`{message.author.id}`", inline=True)
        embed.add_field(name="Channel", value=getattr(message.channel, "mention", str(message.channel)), inline=True)
        embed.add_field(name="Rule", value=f"`{match.rule}`", inline=True)
        embed.add_field(name="Action", value=action.value, inline=True)
        embed.add_field(name="Deleted", value="yes" if deleted else "no", inline=True)
        if offense_count is not None:
            embed.add_field(name="Recent Offenses", value=str(offense_count), inline=True)
        if case_number:
            embed.add_field(name="Case", value=f"#{case_number}", inline=True)
        embed.add_field(name="Reason", value=_trim(match.reason, 900), inline=False)
        if match.evidence:
            embed.add_field(name="Evidence", value=_trim(", ".join(match.evidence), 900), inline=False)
        if message.content:
            embed.add_field(name="Message", value=_trim(message.content, 1000), inline=False)
        if error:
            embed.add_field(name="Error", value=_trim(error, 900), inline=False)
        await self._send(message.guild, embed)

    async def log_member_action(
        self,
        guild: discord.Guild,
        member: discord.Member,
        match: RuleMatch,
        action: Action,
        *,
        error: Optional[str] = None,
    ) -> None:
        embed = discord.Embed(
            title="AutoMod Member Alert",
            color=getattr(Config, "COLOR_WARNING", 0xF59E0B),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="Rule", value=f"`{match.rule}`", inline=True)
        embed.add_field(name="Action", value=action.value, inline=True)
        embed.add_field(name="Reason", value=_trim(match.reason, 900), inline=False)
        if error:
            embed.add_field(name="Error", value=_trim(error, 900), inline=False)
        await self._send(guild, embed)

    async def _send(self, guild: discord.Guild, embed: discord.Embed) -> None:
        logging_cog = getattr(self.bot, "get_cog", lambda name: None)("Logging")
        if logging_cog and hasattr(logging_cog, "get_log_channel"):
            try:
                channel = await logging_cog.get_log_channel(guild, "automod", allow_audit_fallback=True)
            except TypeError:
                channel = await logging_cog.get_log_channel(guild, "automod")
            if channel is not None:
                if hasattr(logging_cog, "safe_send_log"):
                    await logging_cog.safe_send_log(channel, embed, mirror_to_audit=False)
                else:
                    await channel.send(embed=embed)
                return
        settings = await self.bot.db.get_settings(guild.id)
        channel_id = settings.get("automod_log_channel") or settings.get("log_channel_automod")
        try:
            channel = guild.get_channel(int(channel_id)) if channel_id else None
        except (TypeError, ValueError):
            channel = None
        if channel is not None:
            await channel.send(embed=embed)
