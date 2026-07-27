"""Main AutoMod cog and slash commands."""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from .config import AUTOMOD_PRESETS, MODULE_SETTING_KEYS, MODULES, PUNISHMENTS, apply_preset, default_settings, get_preset_description
from .engine import AutoModEngine
from .health import build_automod_health_report
from .logging import AutoModLogger
from .models import Action, RuleMatch
from .panel import AutoModPanel
from .punishments import PunishmentManager
from .storage import AutoModStorage
from .utils import can_manage_automod, compact_duration, id_list, parse_duration, parse_threshold_pair
from .wizard import AutoModWizardSession


ModuleName = Literal["all", "spam", "links", "invites", "mentions", "caps", "badwords", "duplicates", "fast_messages", "emoji_spam", "wall_spam", "attachments", "unicode_spam", "new_accounts", "raid"]
PunishmentName = Literal["none", "log", "warn", "mute", "timeout", "kick", "ban"]
logger = logging.getLogger(__name__)


class AutoMod(commands.Cog):
    """Production AutoMod system with split rules, storage, logging, and commands."""

    automod = app_commands.Group(name="automod", description="Manage server AutoMod")
    bypass = app_commands.Group(name="bypass", description="Manage AutoMod bypasses", parent=automod)
    badwords = app_commands.Group(name="badwords", description="Manage blocked words", parent=automod)
    logs = app_commands.Group(name="logs", description="Manage AutoMod logging", parent=automod)
    punishment = app_commands.Group(name="punishment", description="Manage AutoMod punishments", parent=automod)
    thresholds = app_commands.Group(name="thresholds", description="Manage AutoMod thresholds", parent=automod)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.storage = AutoModStorage(bot)
        self.engine = AutoModEngine()
        self.logger = AutoModLogger(bot)
        self.punishments = PunishmentManager(bot)
        self._wizard_sessions: dict[tuple[int, int], AutoModWizardSession] = {}
        self.cleanup_loop.start()

    def cog_unload(self) -> None:
        self.cleanup_loop.cancel()

    @tasks.loop(minutes=5)
    async def cleanup_loop(self) -> None:
        self.engine.prune()

    @cleanup_loop.before_loop
    async def before_cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return False
        if not await can_manage_automod(interaction):
            await interaction.response.send_message("You need admin or moderator permissions to manage AutoMod.", ephemeral=True)
            return False
        return True

    async def _settings(self, guild_id: int) -> dict[str, Any]:
        return await self.storage.get_settings(guild_id)

    async def _update(self, guild_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        return await self.storage.update_settings(guild_id, changes)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        settings = await self._settings(message.guild.id)
        if not settings.get("automod_enabled", True):
            return
        if self.engine.bypass_reason(message, settings):
            return
        match = await self.engine.evaluate(message, settings)
        if match is None:
            return
        await self._handle_message_match(message, match, settings)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        try:
            await self.bot.db.record_member_event(member.guild.id, member.id, "join")
        except Exception:
            logger.exception("Failed to record dashboard member join for guild %s", member.guild.id)
        settings = await self._settings(member.guild.id)
        if not settings.get("automod_enabled", True):
            return
        matches = self.engine.evaluate_join(member, settings)
        for match in matches:
            action = self.engine.resolve_action(match, settings)
            result = None
            if action not in {Action.NONE, Action.LOG}:
                result = await self.punishments.apply(member.guild, member, action, match, settings)
                action = result.action
            await self.logger.log_member_action(
                member.guild,
                member,
                match,
                action,
                error=result.error if result else None,
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        try:
            await self.bot.db.record_member_event(member.guild.id, member.id, "leave")
        except Exception:
            logger.exception("Failed to record dashboard member leave for guild %s", member.guild.id)

    async def _handle_message_match(self, message: discord.Message, match: RuleMatch, settings: dict[str, Any]) -> None:
        deleted = False
        if self.engine.should_delete_message(match, settings):
            try:
                await message.delete()
                deleted = True
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                deleted = False

        base_action = self.engine.resolve_action(match, settings)
        action, duration_override, offense_count = self.engine.escalated_action(message.guild.id, message.author.id, base_action, settings)
        rule_duration = self.engine.resolve_duration(match, settings)
        duration_override = duration_override or rule_duration
        result = None
        if isinstance(message.author, discord.Member):
            result = await self.punishments.apply(message.guild, message.author, action, match, settings, duration_override=duration_override)
            action = result.action

        self.engine.record_action(message, match, action)
        try:
            await self.bot.db.record_automod_event(
                message.guild.id,
                message.author.id,
                message.channel.id,
                match.rule,
                match.category.value,
                match.severity.name.lower(),
                action.value,
                match.reason,
                deleted,
            )
        except Exception:
            logger.exception("Failed to record AutoMod dashboard event for guild %s", message.guild.id)
        await self.logger.log_message_action(
            message,
            match,
            action,
            deleted=deleted,
            case_number=result.case_number if result else None,
            error=result.error if result else None,
            offense_count=offense_count,
        )
        await self._notify_user(message, match, action, settings)
        if settings.get("automod_public_feedback", False) and deleted:
            try:
                await message.channel.send(
                    f"{message.author.mention}, AutoMod removed your message: {match.reason}",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

    async def _notify_user(self, message: discord.Message, match: RuleMatch, action: Action, settings: dict[str, Any]) -> None:
        if not settings.get("automod_notify_users", True):
            return
        appeal = str(settings.get("automod_appeal_instructions") or "").strip()
        appeal_text = f"\nAppeal: {appeal}" if appeal else ""
        try:
            await message.author.send(
                f"AutoMod in {message.guild.name}: {match.reason}. Action: {action.value}.{appeal_text}"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @automod.command(name="setup", description="Enable AutoMod with safe defaults or a preset")
    @app_commands.describe(log_channel="Channel where AutoMod actions are logged", preset="Quick setup profile: starter, community, gaming, support, marketplace, high_security, chill, lockdown")
    async def setup_command(self, interaction: discord.Interaction, log_channel: Optional[discord.TextChannel] = None, preset: Optional[str] = None) -> None:
        if not await self._guard(interaction):
            return
        if preset and preset.lower() not in AUTOMOD_PRESETS:
            await interaction.response.send_message(
                f"Unknown preset `{preset}`. Available: {', '.join(f'`{p}`' for p in AUTOMOD_PRESETS)}",
                ephemeral=True,
            )
            return
        settings = apply_preset(preset.lower()) if preset else default_settings()
        settings["automod_enabled"] = True
        if log_channel is not None:
            settings["automod_log_channel"] = log_channel.id
            settings["log_channel_automod"] = log_channel.id
        await self._update(interaction.guild.id, settings)
        preset_info = f" with `{preset}` preset" if preset else ""
        await interaction.response.send_message(
            f"AutoMod is enabled{preset_info}. Logs: {log_channel.mention if log_channel else 'not set'}. Use `/automod wizard` for the full guided setup or `/automod status` to review settings.",
            ephemeral=True,
        )

    @automod.command(name="wizard", description="Open the full guided AutoMod setup wizard")
    async def wizard_command(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        assert interaction.guild is not None
        key = (interaction.guild.id, interaction.user.id)
        existing = self._wizard_sessions.get(key)
        if existing and not existing.finished:
            channel = existing.channel
            if channel is not None:
                await interaction.response.send_message(f"You already have an AutoMod setup open in {channel.mention}.", ephemeral=True)
                return

        session = AutoModWizardSession(self.bot, interaction.guild, interaction.user.id, self.storage)
        self._wizard_sessions[key] = session
        await session.start(interaction)

    @automod.command(name="doctor", description="Check AutoMod setup quality, permissions, and missing configuration")
    async def doctor_command(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        assert interaction.guild is not None
        settings = await self._settings(interaction.guild.id)
        report = build_automod_health_report(interaction.guild, settings)
        await interaction.response.send_message(embed=report.to_embed(), ephemeral=True)

    @automod.command(name="preset", description="Apply a preset configuration profile")
    @app_commands.describe(profile="Preset profile: starter, community, gaming, support, marketplace, high_security, chill, lockdown")
    async def preset_command(self, interaction: discord.Interaction, profile: str) -> None:
        if not await self._guard(interaction):
            return
        profile = profile.lower().strip()
        if profile not in AUTOMOD_PRESETS:
            embed = discord.Embed(
                title="Available Presets",
                description="Choose a preset profile for quick AutoMod configuration:",
                color=getattr(Config, "COLOR_INFO", 0x2563EB),
            )
            for name, data in AUTOMOD_PRESETS.items():
                embed.add_field(name=f"`{name}`", value=data.get("description", "No description"), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        settings = await self._settings(interaction.guild.id)
        preset_settings = apply_preset(profile)
        # Preserve log channel and whitelists from current settings
        for key in ("automod_log_channel", "log_channel_automod", "automod_bypass_roles", "automod_bypass_users", "automod_bypass_channels", "automod_badwords", "automod_allowed_invites", "automod_whitelisted_domains"):
            if settings.get(key):
                preset_settings[key] = settings[key]
        preset_settings["automod_enabled"] = True
        await self._update(interaction.guild.id, preset_settings)
        await interaction.response.send_message(
            f"Applied `{profile}` preset: {get_preset_description(profile)}\nUse `/automod status` to review your new settings.",
            ephemeral=True,
        )

    @automod.command(name="export", description="Export AutoMod settings to copy to another server")
    async def export_command(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        import json
        settings = await self._settings(interaction.guild.id)
        # Filter to only automod settings
        export_data = {k: v for k, v in settings.items() if k.startswith("automod_") or k in ("ignored_roles", "ignored_channels", "warn_thresholds_enabled", "warn_threshold_mute", "warn_threshold_kick", "warn_threshold_ban", "warn_mute_duration")}
        export_data["automod_template_version"] = 2
        # Remove server-specific IDs
        for key in ("automod_log_channel", "log_channel_automod", "automod_bypass_roles", "automod_bypass_users", "automod_bypass_channels"):
            export_data.pop(key, None)
        export_json = json.dumps(export_data, indent=2)
        if len(export_json) > 1900:
            await interaction.response.send_message("Settings too large to display. Use `/automod status` to view.", ephemeral=True)
            return
        await interaction.response.send_message(f"```json\n{export_json}\n```\nCopy this config and use `/automod import` on another server.", ephemeral=True)

    @automod.command(name="import", description="Import AutoMod settings from exported config")
    @app_commands.describe(config="JSON config exported from /automod export")
    async def import_command(self, interaction: discord.Interaction, config: str) -> None:
        if not await self._guard(interaction):
            return
        import json
        config = config.strip()
        if config.startswith("```"):
            config = config.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            imported = json.loads(config)
        except json.JSONDecodeError:
            await interaction.response.send_message("Invalid JSON config. Copy the exact output from `/automod export`.", ephemeral=True)
            return
        if not isinstance(imported, dict):
            await interaction.response.send_message("Invalid config format.", ephemeral=True)
            return
        # Validate keys exist in defaults
        defaults = default_settings()
        valid_keys = {k for k in imported if k in defaults}
        if not valid_keys:
            await interaction.response.send_message("No valid AutoMod settings found in config.", ephemeral=True)
            return
        # Merge with current settings
        current = await self._settings(interaction.guild.id)
        for key in valid_keys:
            current[key] = imported[key]
        await self._update(interaction.guild.id, current)
        await interaction.response.send_message(f"Imported {len(valid_keys)} AutoMod settings. Use `/automod status` to review.", ephemeral=True)

    @automod.command(name="help", description="Show AutoMod setup and command help")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="AutoMod Help",
            description="Use `/automod setup` first, then tune modules, thresholds, whitelists, and punishments as needed.",
            color=getattr(Config, "COLOR_INFO", 0x2563EB),
        )
        embed.add_field(
            name="Start Here",
            value=(
                "`/automod wizard` - full private guided setup\n"
                "`/automod doctor` - check permissions and config health\n"
                "`/automod setup log_channel:#logs`\n"
                "`/automod status`\n"
                "`/automod enable module:all`\n"
                "`/automod disable module:links`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Modules",
            value=", ".join(f"`{module}`" for module in MODULES),
            inline=False,
        )
        embed.add_field(
            name="Tuning",
            value=(
                "`/automod thresholds set module:spam value:5/5`\n"
                "`/automod thresholds set module:duplicates value:3/30`\n"
                "`/automod thresholds set module:mentions value:5`\n"
                "`/automod thresholds set module:attachments value:4/15`\n"
                "`/automod punishment set default_action:warn security_action:timeout mute_duration:1h`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Whitelists And Logs",
            value=(
                "`/automod whitelist add role:@Staff`\n"
                "`/automod whitelist add channel:#bot-commands`\n"
                "`/automod whitelist remove user:@Member`\n"
                "`/automod logs set channel:#automod-logs`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Bad Words",
            value=(
                "`/automod badwords add phrase:blocked phrase`\n"
                "`/automod badwords remove phrase:blocked phrase`\n"
                "`/automod badwords list`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Advanced",
            value=(
                "`/automod scan channel:#general amount:100`\n"
                "`/automod config key:automod_spam_threshold value:6`\n"
                "`/automod reset`"
            ),
            inline=False,
        )
        embed.set_footer(text="Changing AutoMod settings requires admin or moderator permissions.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @automod.command(name="enable", description="Enable AutoMod or one module")
    async def enable_command(self, interaction: discord.Interaction, module: ModuleName = "all") -> None:
        if not await self._guard(interaction):
            return
        changes = {"automod_enabled": True} if module == "all" else {MODULE_SETTING_KEYS[module]: True}
        await self._update(interaction.guild.id, changes)
        await interaction.response.send_message(f"Enabled `{module}`.", ephemeral=True)

    @automod.command(name="disable", description="Disable AutoMod or one module")
    async def disable_command(self, interaction: discord.Interaction, module: ModuleName = "all") -> None:
        if not await self._guard(interaction):
            return
        changes = {"automod_enabled": False} if module == "all" else {MODULE_SETTING_KEYS[module]: False}
        await self._update(interaction.guild.id, changes)
        await interaction.response.send_message(f"Disabled `{module}`.", ephemeral=True)

    @automod.command(name="status", description="Show AutoMod status with interactive controls")
    async def status_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        settings = await self._settings(interaction.guild.id)
        panel = AutoModPanel(self.bot, interaction.guild, interaction.user.id, settings, self.storage)
        await interaction.response.send_message(embed=panel.build_embed(), view=panel, ephemeral=True)

    @automod.command(name="config", description="Set a raw AutoMod config key")
    @app_commands.describe(key="Setting key, for example automod_spam_threshold", value="New value")
    async def config_command(self, interaction: discord.Interaction, key: str, value: str) -> None:
        if not await self._guard(interaction):
            return
        defaults = default_settings()
        if key not in defaults:
            await interaction.response.send_message(f"Unknown setting `{key}`.", ephemeral=True)
            return
        parsed = self._parse_value(value, defaults[key])
        await self._update(interaction.guild.id, {key: parsed})
        await interaction.response.send_message(f"Set `{key}` to `{parsed}`.", ephemeral=True)

    @automod.command(name="reset", description="Reset AutoMod settings for this server")
    async def reset_command(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        await self.storage.reset_settings(interaction.guild.id)
        await interaction.response.send_message("AutoMod settings reset to defaults.", ephemeral=True)

    @logs.command(name="set", description="Set the AutoMod log channel")
    async def logs_set_command(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not await self._guard(interaction):
            return
        await self._update(interaction.guild.id, {"automod_log_channel": channel.id, "log_channel_automod": channel.id})
        await interaction.response.send_message(f"AutoMod logs will go to {channel.mention}.", ephemeral=True)

    @punishment.command(name="set", description="Set AutoMod punishments")
    @app_commands.describe(default_action="Normal content/behavior action", security_action="Link/scam/raid action", mute_duration="Duration like 30m, 1h, 1d")
    async def punishment_set_command(
        self,
        interaction: discord.Interaction,
        default_action: PunishmentName,
        security_action: Optional[PunishmentName] = None,
        mute_duration: Optional[str] = None,
    ) -> None:
        if not await self._guard(interaction):
            return
        changes: dict[str, Any] = {"automod_punishment": default_action}
        if security_action is not None:
            changes["automod_security_punishment"] = security_action
        if mute_duration:
            seconds = parse_duration(mute_duration, minimum=60, maximum=2419200)
            if seconds is None:
                await interaction.response.send_message("Duration must look like `30m`, `1h`, or `7d`.", ephemeral=True)
                return
            changes["automod_mute_duration"] = seconds
        await self._update(interaction.guild.id, changes)
        await interaction.response.send_message("Punishment settings updated.", ephemeral=True)

    @thresholds.command(name="set", description="Set common AutoMod thresholds")
    @app_commands.describe(module="spam, duplicates, fast_messages, attachments, emoji_spam, mentions, caps, raid", value="Example: spam 5/5, mentions 5, caps 70")
    async def thresholds_set_command(self, interaction: discord.Interaction, module: str, value: str) -> None:
        if not await self._guard(interaction):
            return
        module = module.strip().lower()
        changes: dict[str, Any] = {}
        if module == "spam":
            pair = parse_threshold_pair(value, count_range=(2, 50), window_range=(2, 60))
            if pair:
                changes = {"automod_spam_threshold": pair[0], "automod_spam_window": pair[1]}
        elif module == "duplicates":
            pair = parse_threshold_pair(value, count_range=(2, 20), window_range=(5, 300))
            if pair:
                changes = {"automod_duplicate_threshold": pair[0], "automod_duplicate_window": pair[1]}
        elif module == "fast_messages":
            pair = parse_threshold_pair(value, count_range=(2, 20), window_range=(1, 15))
            if pair:
                changes = {"automod_fast_message_threshold": pair[0], "automod_fast_message_window": pair[1]}
        elif module == "raid":
            pair = parse_threshold_pair(value, count_range=(2, 100), window_range=(5, 300))
            if pair:
                changes = {"automod_raid_join_threshold": pair[0], "automod_raid_join_window": pair[1]}
        elif module == "attachments":
            pair = parse_threshold_pair(value, count_range=(2, 25), window_range=(5, 120))
            if pair:
                changes = {"automod_attachment_threshold": pair[0], "automod_attachment_window": pair[1]}
        elif module == "mentions" and value.isdigit():
            changes = {"automod_max_mentions": max(1, min(50, int(value)))}
        elif module == "caps" and value.isdigit():
            changes = {"automod_caps_percentage": max(50, min(100, int(value)))}
        elif module == "emoji_spam" and value.isdigit():
            changes = {"automod_emoji_spam_threshold": max(4, min(100, int(value)))}
        if not changes:
            await interaction.response.send_message("Invalid threshold. Use `5/5` for windowed rules or a number for mentions/caps/emoji_spam.", ephemeral=True)
            return
        await self._update(interaction.guild.id, changes)
        await interaction.response.send_message(f"Updated `{module}` thresholds.", ephemeral=True)

    @bypass.command(name="add", description="Add one role, user, or channel to AutoMod bypass")
    async def whitelist_add_command(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
        user: Optional[discord.Member] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await self._whitelist_update(interaction, True, role=role, user=user, channel=channel)

    @bypass.command(name="remove", description="Remove one role, user, or channel from AutoMod bypass")
    async def whitelist_remove_command(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
        user: Optional[discord.Member] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await self._whitelist_update(interaction, False, role=role, user=user, channel=channel)

    @badwords.command(name="add", description="Add a blocked word or phrase")
    async def badwords_add_command(self, interaction: discord.Interaction, phrase: str) -> None:
        if not await self._guard(interaction):
            return
        phrase = phrase.strip()
        if not 2 <= len(phrase) <= 120:
            await interaction.response.send_message("Phrase must be 2-120 characters.", ephemeral=True)
            return
        settings = await self._settings(interaction.guild.id)
        words = list(settings.get("automod_badwords", []) or [])
        if phrase.casefold() not in {word.casefold() for word in words}:
            words.append(phrase)
        await self._update(interaction.guild.id, {"automod_badwords": words, "automod_badwords_enabled": True})
        await interaction.response.send_message(f"Added `{phrase}` to AutoMod bad words.", ephemeral=True)

    @badwords.command(name="remove", description="Remove a blocked word or phrase")
    async def badwords_remove_command(self, interaction: discord.Interaction, phrase: str) -> None:
        if not await self._guard(interaction):
            return
        settings = await self._settings(interaction.guild.id)
        words = [word for word in settings.get("automod_badwords", []) or [] if str(word).casefold() != phrase.strip().casefold()]
        await self._update(interaction.guild.id, {"automod_badwords": words})
        await interaction.response.send_message(f"Removed `{phrase}` from AutoMod bad words.", ephemeral=True)

    @badwords.command(name="list", description="List blocked words")
    async def badwords_list_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        settings = await self._settings(interaction.guild.id)
        words = list(settings.get("automod_badwords", []) or [])
        body = "\n".join(f"`{index}.` {word}" for index, word in enumerate(words[:50], 1)) or "No bad words configured."
        if len(words) > 50:
            body += f"\n...and {len(words) - 50} more."
        await interaction.response.send_message(body, ephemeral=True)

    @automod.command(name="scan", description="Dry-run scan recent messages in a channel")
    async def scan_command(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, amount: int = 50) -> None:
        if not await self._guard(interaction):
            return
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text channel.", ephemeral=True)
            return
        amount = max(1, min(200, int(amount)))
        await interaction.response.defer(ephemeral=True)
        settings = await self._settings(interaction.guild.id)
        hits: list[str] = []
        async for message in channel.history(limit=amount):
            if message.author.bot:
                continue
            match = await self.engine.evaluate(message, settings, dry_run=True)
            if match is not None:
                hits.append(f"`{match.rule}` by {message.author.mention}: {match.reason}")
        if not hits:
            await interaction.followup.send(f"Scanned {amount} messages in {channel.mention}. No matches.", ephemeral=True)
            return
        await interaction.followup.send("\n".join(hits[:20])[:1900], ephemeral=True)

    async def _whitelist_update(
        self,
        interaction: discord.Interaction,
        add: bool,
        *,
        role: Optional[discord.Role],
        user: Optional[discord.Member],
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self._guard(interaction):
            return
        selected = [item for item in (role, user, channel) if item is not None]
        if len(selected) != 1:
            await interaction.response.send_message("Provide exactly one role, user, or channel.", ephemeral=True)
            return
        if role is not None:
            key, target_id, label = "automod_bypass_roles", role.id, role.mention
        elif user is not None:
            key, target_id, label = "automod_bypass_users", user.id, user.mention
        else:
            key, target_id, label = "automod_bypass_channels", channel.id, channel.mention
        settings = await self._settings(interaction.guild.id)
        values = id_list(settings.get(key, []))
        if add:
            values.add(target_id)
        else:
            values.discard(target_id)
        await self._update(interaction.guild.id, {key: sorted(values)})
        await interaction.response.send_message(f"{'Added' if add else 'Removed'} {label} {'to' if add else 'from'} the AutoMod whitelist.", ephemeral=True)

    def _parse_value(self, raw: str, existing: Any) -> Any:
        value = raw.strip()
        if isinstance(existing, bool):
            return value.lower() in {"1", "true", "yes", "on", "enable", "enabled"}
        if isinstance(existing, int) and not isinstance(existing, bool):
            duration = parse_duration(value, minimum=1, maximum=2592000)
            if duration is not None and any(unit in value.lower() for unit in "smhd"):
                return duration
            return int(value)
        if isinstance(existing, list):
            if not value:
                return []
            return [item.strip() for item in value.split(",") if item.strip()]
        if value.lower() in PUNISHMENTS:
            return value.lower()
        return value


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoMod(bot))
