"""
User Risk Score System

Calculates a 0-100 risk score per guild member based on account age,
warning history, automod violations, scam links, mass mentions, and more.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from utils.checks import is_admin, is_mod, is_bot_owner_id
from utils.embeds import ModEmbed

logger = logging.getLogger("ModBot.RiskScoring")

CACHE_TTL = 60
ESCALATION_THRESHOLD_ALERT = 50
ESCALATION_THRESHOLD_QUARANTINE = 66
ESCALATION_THRESHOLD_TIMEOUT = 81


@dataclass
class RiskResult:
    score: int
    factors: Dict[str, int]
    details: Dict[str, Any]
    suggested_action: str


class RiskEngine:
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def calculate(self, member: discord.Member) -> RiskResult:
        guild = member.guild
        factors: Dict[str, int] = {}
        details: Dict[str, Any] = {}
        now = datetime.now(timezone.utc)

        # Account age
        age_days = (now - member.created_at.replace(tzinfo=None)).days
        if age_days < 7:
            factors["account_age"] = 15
            details["account_age_days"] = age_days
        elif age_days < 30:
            factors["account_age"] = 8
            details["account_age_days"] = age_days
        else:
            factors["account_age"] = 0

        # Join recency
        if member.joined_at:
            join_hours = (now - member.joined_at.replace(tzinfo=None)).total_seconds() / 3600
            if join_hours < 1:
                factors["join_recent"] = 12
            elif join_hours < 24:
                factors["join_recent"] = 6
            else:
                factors["join_recent"] = 0
            details["join_hours"] = round(join_hours, 1)

        # Warning count
        try:
            warnings = await self.bot.db.get_warnings(guild.id, member.id)
            warn_count = len(warnings)
            factors["warnings"] = min(warn_count * 5, 25)
            details["warning_count"] = warn_count
        except Exception:
            factors["warnings"] = 0

        # Scam links from cases
        try:
            from cogs.aimoderation.aimoderation import ToolType
            scam_count = await self._count_cases_by_action(guild.id, member.id, "ban", "scam")
            factors["scam_history"] = min(scam_count * 20, 20)
            details["scam_cases"] = scam_count
        except Exception:
            factors["scam_history"] = 0

        # Default avatar
        if member.avatar is None:
            factors["default_avatar"] = 3

        # Alt suspicion (placeholder — Phase 3 will feed this)
        try:
            risk_record = await self.bot.db.get_risk_score(guild.id, member.id)
            if risk_record:
                alt_score = risk_record.get("factors", {}).get("alt_suspicion", 0)
                if alt_score:
                    factors["alt_suspicion"] = alt_score
        except Exception:
            pass

        total = sum(factors.values())
        total = max(0, min(100, total))

        action = "none"
        if total >= ESCALATION_THRESHOLD_TIMEOUT:
            action = "timeout 1 hour"
        elif total >= ESCALATION_THRESHOLD_QUARANTINE:
            action = "quarantine"
        elif total >= ESCALATION_THRESHOLD_ALERT:
            action = "alert staff"

        return RiskResult(
            score=total,
            factors=factors,
            details=details,
            suggested_action=action,
        )

    async def _count_cases_by_action(self, guild_id: int, user_id: int, action: str, reason_substr: str) -> int:
        try:
            async with self.bot.db.get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) FROM cases
                    WHERE guild_id = ? AND user_id = ? AND action = ?
                    AND reason LIKE ?
                    """,
                    (guild_id, user_id, action, f"%{reason_substr}%"),
                )
                row = await cursor.fetchone()
                return row[0] if row else 0
        except Exception:
            return 0


class RiskScoring(commands.Cog):
    """User risk scoring and scanning."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.engine = RiskEngine(bot)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            result = await self.engine.calculate(member)
            await self.bot.db.upsert_risk_score(
                member.guild.id, member.id, result.score, result.factors
            )
            if result.score >= ESCALATION_THRESHOLD_ALERT:
                logger.info(
                    "High-risk join: %s (%d) in guild %d — score %d",
                    member.display_name, member.id, member.guild.id, result.score
                )
        except Exception:
            logger.error("Failed to calculate risk on join for %s", member.id, exc_info=True)

    scan_group = app_commands.Group(
        name="scan",
        description="Risk scan commands",
        default_permissions=discord.Permissions(moderate_members=True),
    )

    @scan_group.command(name="user")
    @app_commands.describe(user="User to scan for risk")
    async def scan_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Scan a user for risk factors and get a risk score."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        result = await self.engine.calculate(user)

        await self.bot.db.upsert_risk_score(
            guild.id, user.id, result.score, result.factors
        )

        embed = await self._build_scan_embed(user, result)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @scan_group.command(name="top")
    async def scan_top(self, interaction: discord.Interaction) -> None:
        """Show the top risky users in this server."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        top = await self.bot.db.get_top_risky_users(guild.id, limit=10)
        if not top:
            await interaction.followup.send(
                embed=ModEmbed.info(title="No Risk Data", description="No risk scores recorded yet."),
                ephemeral=True,
            )
            return

        lines = []
        for i, entry in enumerate(top, 1):
            uid = entry["user_id"]
            score = entry["score"]
            emoji = "🔴" if score >= 76 else "🟡" if score >= 51 else "🟢"
            lines.append(f"{emoji} **#{i}** <@{uid}> — `{score}/100`")

        embed = ModEmbed.info(
            title="🔍 Top Risky Users",
            description="\n".join(lines),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _build_scan_embed(self, user: discord.Member, result: RiskResult) -> discord.Embed:
        color = (
            discord.Color.red() if result.score >= 76
            else discord.Color.orange() if result.score >= 51
            else discord.Color.green()
        )

        embed = discord.Embed(
            title=f"🔍 Risk Scan: {user.display_name}",
            description=f"**Score: {result.score}/100**",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        if result.factors:
            factor_lines = []
            for factor, points in sorted(result.factors.items(), key=lambda x: -x[1]):
                if points > 0:
                    label = factor.replace("_", " ").title()
                    factor_lines.append(f"• {label}: **+{points}**")
            if factor_lines:
                embed.add_field(name="Risk Factors", value="\n".join(factor_lines), inline=False)
            else:
                embed.add_field(name="Risk Factors", value="✅ No risk factors detected", inline=False)

        if result.suggested_action != "none":
            embed.add_field(
                name="Suggested Action",
                value=f"🛡️ {result.suggested_action}",
                inline=False,
            )

        if result.details:
            detail_parts = []
            if "account_age_days" in result.details:
                detail_parts.append(f"Account age: {result.details['account_age_days']} days")
            if "join_hours" in result.details:
                detail_parts.append(f"Joined: {result.details['join_hours']:.0f}h ago")
            if "warning_count" in result.details:
                detail_parts.append(f"Warnings: {result.details['warning_count']}")
            if "scam_cases" in result.details:
                detail_parts.append(f"Scam cases: {result.details['scam_cases']}")
            if detail_parts:
                embed.add_field(name="Details", value="\n".join(detail_parts), inline=False)

        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RiskScoring(bot))
