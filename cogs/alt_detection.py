"""
Alt Account Detection

Detects alt/suspicious accounts by comparing new joins against banned
user profiles: name similarity, avatar hashes, join patterns.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from utils.checks import is_admin, is_mod, is_bot_owner_id
from utils.embeds import ModEmbed

logger = logging.getLogger("ModBot.AltDetection")

NAME_SIMILARITY_THRESHOLD = 0.75


@dataclass
class AltResult:
    is_suspect: bool
    confidence: float
    matched_user: Optional[Dict[str, Any]]
    reasons: List[str]


def _hash_avatar(avatar: Optional[discord.Asset]) -> Optional[str]:
    if avatar is None:
        return None
    return hashlib.md5(str(avatar.key).encode()).hexdigest()


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class AltDetectionEngine:
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def check(self, member: discord.Member) -> AltResult:
        reasons: List[str] = []
        best_match: Optional[Dict[str, Any]] = None
        best_confidence = 0.0

        try:
            profiles = await self.bot.db.get_all_banned_profiles()
        except Exception:
            profiles = []

        avatar_hash = _hash_avatar(member.avatar)

        for profile in profiles:
            confidence = 0.0
            local_reasons: List[str] = []

            sim = _name_similarity(member.name, profile.get("username", ""))
            if sim >= NAME_SIMILARITY_THRESHOLD:
                confidence += sim * 0.5
                local_reasons.append(f"Name similarity {sim:.0%} with banned user `{profile['username']}`")

            if avatar_hash and profile.get("avatar_hash") and avatar_hash == profile["avatar_hash"]:
                confidence += 0.4
                local_reasons.append("Matching avatar hash with banned user")

            if confidence > best_confidence:
                best_confidence = confidence
                best_match = profile
                reasons = local_reasons

        acc_age_days = (discord.utils.utcnow() - member.created_at).days
        if acc_age_days < 7:
            reasons.append(f"New account ({acc_age_days} days old)")

        is_suspect = best_confidence >= 0.4
        return AltResult(
            is_suspect=is_suspect,
            confidence=best_confidence,
            matched_user=best_match,
            reasons=reasons,
        )


class AltDetection(commands.Cog):
    """Alt account detection and monitoring."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.engine = AltDetectionEngine(bot)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            result = await self.engine.check(member)
            if result.is_suspect:
                logger.warning(
                    "Suspected alt join: %s (%d) in guild %d — confidence %.2f",
                    member.display_name, member.id, member.guild.id, result.confidence
                )
                try:
                    await self.bot.db.upsert_risk_score(
                        member.guild.id,
                        member.id,
                        0,
                        {"alt_suspicion": min(int(result.confidence * 100), 100)},
                    )
                except Exception:
                    pass
        except Exception:
            logger.error("Alt check failed for %s", member.id, exc_info=True)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        try:
            avatar_hash = _hash_avatar(user.avatar) if user.avatar else None
            await self.bot.db.store_banned_profile(
                user_id=user.id,
                username=user.name,
                avatar_hash=avatar_hash,
                guild_id=guild.id,
            )
        except Exception:
            logger.error("Failed to store banned profile for %s", user.id, exc_info=True)

    alt_group = app_commands.Group(
        name="alt",
        description="Alt account detection",
        default_permissions=discord.Permissions(moderate_members=True),
    )

    @alt_group.command(name="check")
    @app_commands.describe(user="User to check for alt suspicion")
    async def alt_check(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Check if a user might be an alt account."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        result = await self.engine.check(user)

        if result.is_suspect:
            color = discord.Color.orange()
            title = f"⚠️ Alt Suspect: {user.display_name}"
            desc = f"**Confidence: {result.confidence:.0%}**"
        else:
            color = discord.Color.green()
            title = f"✅ Clean: {user.display_name}"
            desc = "No alt indicators detected."

        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_thumbnail(url=user.display_avatar.url)

        if result.reasons:
            embed.add_field(
                name="Reasons",
                value="\n".join(f"• {r}" for r in result.reasons),
                inline=False,
            )

        if result.matched_user:
            embed.add_field(
                name="Matched Banned User",
                value=f"`{result.matched_user.get('username', 'Unknown')}`",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AltDetection(bot))
