"""
Weekly Staff Reports

Auto-generates periodic staff reports with moderation stats, risk data,
automod activity, and staff performance metrics.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone, date as date_type
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from utils.checks import is_admin, is_bot_owner_id
from utils.embeds import ModEmbed

logger = logging.getLogger("ModBot.StaffReports")


class StaffReports(commands.Cog):
    """Periodic staff reporting and analytics."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._report_tasks: Dict[int, Dict[str, Any]] = {}

    async def cog_load(self):
        if not self._check_scheduled.is_running():
            self._check_scheduled.start()

    async def cog_unload(self):
        if self._check_scheduled.is_running():
            self._check_scheduled.cancel()

    report_group = app_commands.Group(
        name="staffreport",
        description="Staff report commands",
        default_permissions=discord.Permissions(administrator=True),
    )

    @report_group.command(name="generate")
    @app_commands.describe(
        days="Number of days to cover (default 7)",
        channel="Channel to post the report (defaults to current)",
    )
    async def report_generate(
        self,
        interaction: discord.Interaction,
        days: Optional[int] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Generate a staff report."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        await interaction.response.defer()
        target = channel or interaction.channel
        window = days or 7

        embed = await self._build_report_embed(guild, window)

        try:
            await target.send(embed=embed)
            await interaction.followup.send(
                embed=ModEmbed.success(
                    title="Report Generated",
                    description=f"Report for last {window} days sent to {target.mention}.",
                ),
                ephemeral=True,
            )
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @report_group.command(name="config")
    @app_commands.describe(
        day="Day of week (0=Mon ... 6=Sun)",
        hour="Hour UTC (0-23)",
        channel="Channel to deliver reports",
    )
    async def report_config(
        self,
        interaction: discord.Interaction,
        day: Optional[int] = None,
        hour: Optional[int] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Configure automatic weekly report delivery."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        if day is not None and not 0 <= day <= 6:
            await interaction.response.send_message("Day must be 0-6 (Monday=0, Sunday=6).", ephemeral=True)
            return
        if hour is not None and not 0 <= hour <= 23:
            await interaction.response.send_message("Hour must be 0-23.", ephemeral=True)
            return

        current = await self.bot.db.get_settings(guild.id)
        report_cfg = current.get("staff_report", {})

        if day is not None:
            report_cfg["day"] = day
        if hour is not None:
            report_cfg["hour"] = hour
        if channel is not None:
            report_cfg["channel_id"] = channel.id

        current["staff_report"] = report_cfg
        await self.bot.db.update_settings(guild.id, current)

        parts = []
        if "day" in report_cfg:
            parts.append(f"Day: `{_day_name(report_cfg['day'])}`")
        if "hour" in report_cfg:
            parts.append(f"Hour: `{report_cfg['hour']:02d}:00 UTC`")
        if report_cfg.get("channel_id"):
            parts.append(f"Channel: <#{report_cfg['channel_id']}>")

        await interaction.response.send_message(
            embed=ModEmbed.success(
                title="Report Config Updated",
                description="\n".join(parts) if parts else "Not configured yet.",
            ),
            ephemeral=True,
        )

    @tasks.loop(minutes=5)
    async def _check_scheduled(self):
        await self.bot.wait_until_ready()
        now = datetime.now(timezone.utc)

        for guild in self.bot.guilds:
            try:
                settings = await self.bot.db.get_settings(guild.id)
                cfg = settings.get("staff_report", {})
                day = cfg.get("day")
                hour = cfg.get("hour")
                channel_id = cfg.get("channel_id")

                if day is None or hour is None or channel_id is None:
                    continue

                if now.weekday() != day or now.hour != hour or now.minute > 5:
                    continue

                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    continue

                embed = await self._build_report_embed(guild, 7)
                await channel.send(embed=embed)
                logger.info("Weekly report delivered to guild %d", guild.id)

            except Exception:
                logger.error("Failed delivery for guild %d", guild.id, exc_info=True)

    async def _build_report_embed(self, guild: discord.Guild, days: int) -> discord.Embed:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        embed = discord.Embed(
            title=f"📊 Weekly Staff Report — {guild.name}",
            description=f"Last {days} days ending {_fmt(datetime.now(timezone.utc))}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        # Moderation actions
        try:
            async with self.bot.db.get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT action, COUNT(*)
                    FROM mod_stats
                    WHERE guild_id = ? AND created_at >= ?
                    GROUP BY action
                    ORDER BY COUNT(*) DESC
                    LIMIT 8
                    """,
                    (guild.id, since),
                )
                action_rows = await cursor.fetchall()
        except Exception:
            action_rows = []

        if action_rows:
            lines = []
            for action, count in action_rows:
                lines.append(f"• {action}: **{count}**")
            embed.add_field(name="🛡️ Mod Actions", value="\n".join(lines), inline=True)

        # Risk data
        try:
            top = await self.bot.db.get_top_risky_users(guild.id, limit=5)
            if top:
                risks = []
                for entry in top:
                    risks.append(f"• <@{entry['user_id']}> — `{entry['score']}/100`")
                embed.add_field(name="🔍 Top Risks", value="\n".join(risks), inline=True)
        except Exception:
            pass

        # Member stats
        total = guild.member_count or 0
        bots = sum(1 for m in guild.members if m.bot)
        humans = total - bots
        new_joins = sum(
            1 for m in guild.members
            if m.joined_at and m.joined_at.replace(tzinfo=None) > datetime.now(timezone.utc) - timedelta(days=days)
        )
        embed.add_field(
            name="👥 Members",
            value=f"Total: **{total}**\nHumans: **{humans}**\nNew ({days}d): **{new_joins}**",
            inline=True,
        )

        return embed


def _day_name(d: int) -> str:
    return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][d]


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffReports(bot))
