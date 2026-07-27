"""
AI Moderation Cog — the Discord cog entry point.

This is a thin layer that:
1. Instantiates all services with proper dependency injection.
2. Registers the on_message listener.
3. Provides slash commands for configuration and information.

All intelligence lives in the service modules. The cog just wires them
together and handles Discord lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .action_executor import ActionExecutor
from .ai_client import AIClient
from .config import GuildConfig
from .context_collector import ContextCollector
from .conversation_manager import ConversationManager
from .database import Database
from .exceptions import AIModerationError
from .listeners import ModerationListener
from .logging_service import LoggingService
from .parser import InstructionParser
from .permissions import PermissionChecker
from .rule_engine import RuleEngine

logger = logging.getLogger("ModBot.AIModeration")


class AIModerationCog(commands.Cog):
    """AI-powered moderation cog.

    Wires together all services and registers the message listener.
    Provides slash commands for configuration and diagnostics.
    """

    def __init__(self, bot: commands.Bot, db_path: str = "data/ai_moderation.db") -> None:
        self.bot = bot

        # --- Database ---
        self.db = Database(db_path)

        # --- Services (dependency injection) ---
        self.ai = AIClient(bot)
        self.context_collector = ContextCollector(self.db)
        self.rule_engine = RuleEngine(self.db)
        self.parser = InstructionParser(
            self.ai, self.db, self.context_collector, self.rule_engine,
        )
        self.permissions = PermissionChecker(
            bot_user_id=bot.user.id if bot.user else 0,
        )
        self.logging = LoggingService(self.db, bot)
        self.conversation = ConversationManager(self.db)
        self.executor = ActionExecutor(
            bot, self.db, self.permissions, self.logging,
        )
        self.listener = ModerationListener(
            bot, self.parser, self.conversation,
            self.executor, self.permissions, self.logging,
        )

        # --- Background tasks ---
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------
    # Cog lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        """Initialize the database and start background tasks."""
        await self.db.init()
        self._cleanup_loop.start()
        if self.ai.is_available:
            asyncio.create_task(self._prewarm_ai(), name="ai-prewarm")
        logger.info("AI Moderation cog loaded.")

    async def cog_unload(self) -> None:
        """Clean up resources."""
        self._cleanup_loop.cancel()
        with suppress(asyncio.CancelledError):
            await self._cleanup_loop
        await self.ai.close()
        await self.db.close()
        logger.info("AI Moderation cog unloaded.")

    async def _prewarm_ai(self) -> None:
        try:
            await self.ai.prewarm()
        except Exception:
            logger.warning("AI prewarm failed", exc_info=True)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def _cleanup_loop(self) -> None:
        """Periodically clean up expired conversation states."""
        try:
            count = await self.conversation.cleanup_expired()
            if count:
                logger.debug("Cleaned up %d expired conversation states.", count)
        except Exception:
            logger.debug("Conversation cleanup failed", exc_info=True)

    @_cleanup_loop.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Message listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Forward messages to the moderation listener."""
        try:
            await self.listener.on_message(message)
        except AIModerationError as exc:
            # User-facing errors are sent to the moderator.
            if exc.user_facing and message.guild:
                with suppress(discord.HTTPException):
                    await message.reply(
                        str(exc),
                        allowed_mentions=discord.AllowedMentions.none(),
                        mention_author=False,
                    )
            else:
                logger.exception("AI moderation error")
        except Exception:
            logger.exception("Unexpected error in AI moderation listener")

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    ai_group = app_commands.Group(
        name="aimod", description="AI Moderation configuration and tools"
    )

    @ai_group.command(name="status")
    async def status_command(self, interaction: discord.Interaction) -> None:
        """View the AI moderation status for this guild."""
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        config = await self.db.get_guild_config(interaction.guild.id)
        embed = discord.Embed(
            title="AI Moderation Status",
            color=discord.Color.blurple() if config.enabled else discord.Color.greyple(),
        )
        embed.add_field(
            name="Enabled",
            value="✅ Yes" if config.enabled else "❌ No",
            inline=True,
        )
        embed.add_field(
            name="AI Available",
            value="✅ Yes" if self.ai.is_available else "❌ No",
            inline=True,
        )
        embed.add_field(
            name="Provider",
            value=self.ai.availability_message()[:200],
            inline=False,
        )
        embed.add_field(
            name="Auto-Action Threshold",
            value=f"{config.auto_action_threshold * 100:.0f}%",
            inline=True,
        )
        embed.add_field(
            name="Max Timeout",
            value=f"{config.max_timeout_seconds // 3600}h",
            inline=True,
        )
        embed.add_field(
            name="Rules Configured",
            value=str(len(config.rules)),
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ai_group.command(name="doctor")
    async def doctor_command(self, interaction: discord.Interaction) -> None:
        """Diagnose AI moderation issues."""
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        lines = self.ai.diagnostic_lines()
        config = await self.db.get_guild_config(interaction.guild.id)
        lines.append(f"Enabled: {'yes' if config.enabled else 'no'}")
        lines.append(f"Log channel: {config.log_channel_id or 'not set'}")
        lines.append(f"Rules: {len(config.rules)} configured")
        embed = discord.Embed(
            title="🔍 AI Moderation Diagnostics",
            description="\n".join(f"- {line}" for line in lines)[:4000],
            color=discord.Color.blurple() if self.ai.is_available else discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ai_group.command(name="toggle")
    @app_commands.describe(enabled="Enable or disable AI moderation.")
    async def toggle_command(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        """Enable or disable AI moderation."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server permission.", ephemeral=True
            )
            return
        config = await self.db.get_guild_config(interaction.guild.id)
        config.enabled = enabled
        await self.db.save_guild_config(interaction.guild.id, config)
        await interaction.response.send_message(
            f"AI Moderation is now **{'enabled' if enabled else 'disabled'}**.",
            ephemeral=True,
        )

    @ai_group.command(name="setup")
    @app_commands.describe(
        log_channel="Channel for moderation logs.",
        muted_role="Role to use for muting (alternative to timeout).",
        use_timeout="Use Discord timeouts instead of a muted role.",
    )
    async def setup_command(
        self,
        interaction: discord.Interaction,
        log_channel: Optional[discord.TextChannel] = None,
        muted_role: Optional[discord.Role] = None,
        use_timeout: bool = True,
    ) -> None:
        """Configure basic AI moderation settings."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server permission.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        config = await self.db.get_guild_config(interaction.guild.id)
        if log_channel:
            config.log_channel_id = log_channel.id
        if muted_role:
            config.muted_role_id = muted_role.id
        config.use_timeout_over_role = use_timeout
        config.enabled = True
        await self.db.save_guild_config(interaction.guild.id, config)
        embed = discord.Embed(
            title="✅ AI Moderation Configured",
            color=discord.Color.green(),
            description=(
                f"Log channel: {log_channel.mention if log_channel else 'not set'}\n"
                f"Muted role: {muted_role.mention if muted_role else 'not set'}\n"
                f"Using: {'Timeouts' if use_timeout else 'Muted role'}\n"
                f"Mention the bot with a moderation instruction to get started."
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ai_group.command(name="help")
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Show AI moderation help and examples."""
        embed = discord.Embed(
            title="🤖 AI Moderation Help",
            color=discord.Color.blurple(),
            description=(
                "Mention the bot and give instructions in natural language.\n\n"
                "**Examples:**\n"
                "• `@Bot warn @User for advertising`\n"
                "• `@Bot timeout @User for 10 minutes for spamming`\n"
                "• `@Bot ban @User for repeated threats`\n"
                "• `@Bot deal with @User` (AI investigates and recommends)\n"
                "• `@Bot undo that` (reverses the last action)\n"
                "• `@Bot lock this channel for 30 minutes`\n\n"
                "**Commands:**\n"
                "• `/aimod status` — View current settings\n"
                "• `/aimod doctor` — Diagnose issues\n"
                "• `/aimod setup` — Configure log channel and mute role\n"
                "• `/aimod toggle` — Enable/disable AI moderation"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# =============================================================================
# Setup function
# =============================================================================


async def setup(bot: commands.Bot) -> None:
    """Add the AI Moderation cog to the bot."""
    await bot.add_cog(AIModerationCog(bot))
