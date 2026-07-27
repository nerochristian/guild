"""
Server setup command.

This setup flow is intentionally status-first. It shows what is configured
and can scaffold the core roles, categories, and compact log channels.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import is_admin
from utils.embeds import ModEmbed
from utils.server_setup import quickstart_server

logger = logging.getLogger(__name__)


class Setup(commands.Cog):
    guide_group = app_commands.Group(
        name="guide",
        description="Guides and staff-update configuration",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            settings = await self.bot.db.get_settings(guild.id)
            result = await quickstart_server(guild, settings)
            settings = result["settings"]
            settings["_version"] = int(settings.get("_version", 1) or 1) + 1
            settings["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            await self.bot.db.update_settings(guild.id, settings)
            
            logging_cog = self.bot.get_cog("Logging")
            if logging_cog is not None:
                channel_cache = getattr(logging_cog, "_channel_cache", None)
                if channel_cache is not None:
                    await channel_cache.clear_guild(guild.id)
                    
            logger.info("Automatically completed quickstart setup for guild %s upon join.", guild.id)
        except Exception as exc:
            logger.exception("Automatic setup failed for guild %s", guild.id)

    @guide_group.command(
        name="updates",
        description="Configure the public channel for promotions and demotions",
    )
    @app_commands.describe(channel="Channel used for public staff update posts")
    @is_admin()
    async def staffupdates(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["staff_updates_channel"] = channel.id
        settings["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        settings["_version"] = int(settings.get("_version", 1) or 1) + 1
        await self.bot.db.update_settings(interaction.guild_id, settings)

        embed = ModEmbed.success(
            "Staff Updates Configured",
            f"Promotion and demotion updates will be posted in {channel.mention}.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @guide_group.command(name="moderation", description="Show the day-to-day moderation guide")
    async def moderation_guide(self, interaction: discord.Interaction) -> None:
        moderation = self.bot.get_cog("Moderation")
        if moderation is None or not hasattr(moderation, "guide_slash"):
            await interaction.response.send_message(
                embed=ModEmbed.error("Unavailable", "The moderation guide is temporarily unavailable."),
                ephemeral=True,
            )
            return
        await moderation.guide_slash(interaction)

    @guide_group.command(name="staff", description="Show the staff workflow guide")
    async def staff_guide(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Docket Staff Guide",
            description="A compact workflow for reviewing incidents consistently and leaving a useful record.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="1. Establish context",
            value="Check `/history`, recent messages, active cases, and available evidence before acting.",
            inline=False,
        )
        embed.add_field(
            name="2. Apply the narrowest action",
            value="Use warnings or timeouts for correctable behavior; reserve kicks and bans for serious or repeated violations.",
            inline=False,
        )
        embed.add_field(
            name="3. Leave a reviewable record",
            value="Give a specific reason, preserve relevant evidence, and review submitted appeals from the dashboard queue.",
            inline=False,
        )
        embed.add_field(
            name="Configuration",
            value="Use `/guide updates` for public promotion/demotion posts and the dashboard Modules page for staff roles, logs, tickets, and appeals.",
            inline=False,
        )
        embed.set_footer(text="Docket · Moderation records desk")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Setup(bot))
