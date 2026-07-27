import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
import logging
import json
import re
from config import Config
from utils.checks import is_bot_owner_id

# Mixins
from .extensions.helpers import HelperCommands
from .extensions.chat import ChatCommands
from .extensions.cases import CaseCommands
from .extensions.warnings import WarningCommands
from .extensions.management import ManagementCommands
from .extensions.misc import MiscCommands

logger = logging.getLogger("ModBot.Moderation")

class Moderation(
    commands.Cog,
    HelperCommands,
    ChatCommands,
    CaseCommands,
    WarningCommands,
    ManagementCommands,
    MiscCommands
):
    """Moderation command suite with role hierarchy and permission checks"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._hierarchy_cache = {}
        self._slash_commands = []

    async def _register_top_level_commands(self):
        """Register all commands as top-level commands"""
        def cmd(name: str, desc: str, callback):
            command = app_commands.Command(name=name, description=desc, callback=callback)
            self._slash_commands.append(command)
            self.bot.tree.add_command(command)
            return command

        # Chat Moderation
        cmd("lock", "Lock a channel", self.lock_slash)
        cmd("unlock", "Unlock a channel", self.unlock_slash)
        cmd("slowmode", "Set channel slowmode", self.slowmode_slash)
        cmd("glock", "Allow only specific role to talk", self.glock_slash)
        cmd("gunlock", "Remove glock restriction", self.gunlock_slash)
        cmd("lockdown", "Lock all channels", self.lockdown_slash)
        cmd("unlockdown", "Unlock all channels", self.unlockdown_slash)
        cmd("nuke", "Clone and delete channel", self.nuke_slash)
        cmd("purge", "Bulk delete messages", self.purge_slash)
        cmd("purgebots", "Delete bot messages", self.purgebots_slash)
        cmd("purgecontains", "Delete messages containing text", self.purgecontains_slash)
        cmd("purgeembeds", "Delete messages with embeds", self.purgeembeds_slash)
        cmd("purgeimages", "Delete messages with images", self.purgeimages_slash)
        cmd("purgelinks", "Delete messages with links", self.purgelinks_slash)

        # Whitelist
        whitelist_group = WhitelistGroup(self)
        self._slash_commands.append(whitelist_group)
        self.bot.tree.add_command(whitelist_group)

        # Member Management
        cmd("kick", "Kick a user", self.kick_slash)
        cmd("ban", "Ban a user", self.ban_slash)
        cmd("unban", "Unban a user", self.unban_slash)
        cmd("softban", "Softban a user", self.softban_slash)
        cmd("tempban", "Temporarily ban a user", self.tempban_slash)
        cmd("mute", "Mute/timeout a user", self.mute_slash)
        cmd("unmute", "Unmute a user", self.unmute_slash)
        cmd("timeout", "Timeout a user", self.timeout_slash)
        cmd("untimeout", "Remove timeout", self.untimeout_slash)
        cmd("rename", "Change nickname", self.rename_slash)
        cmd("setnick", "Set nickname", self.setnick_slash)
        cmd("quarantine", "Quarantine a user", self.quarantine_slash)
        cmd("unquarantine", "Unquarantine a user", self.unquarantine_slash)

        # Bulk Operations
        cmd("massban", "Ban multiple users", self.massban_slash)
        cmd("banlist", "View banned users", self.banlist_slash)
        cmd("roleall", "Give role to everyone", self.roleall_slash)
        cmd("removeall", "Remove role from everyone", self.removeall_slash)
        cmd("inrole", "List users with role", self.inrole_slash)
        cmd("nicknameall", "Set nickname for all", self.nicknameall_slash)
        cmd("resetnicks", "Reset all nicknames", self.resetnicks_slash)

        # Warnings
        cmd("warn", "Warn a user", self.warn_slash)
        cmd("warnings", "View warnings", self.warnings_slash)
        cmd("delwarn", "Delete a warning", self.delwarn_slash)
        cmd("clearwarnings", "Clear all warnings", self.clearwarnings_slash)

        # Cases & History
        cmd("case", "View a case", self.case_slash)
        cmd("editcase", "Edit case reason", self.editcase_slash)
        cmd("history", "View user history", self.history_slash)
        cmd("modlogs", "View mod logs", self.modlogs_slash)
        cmd("note", "Add a note", self.note_slash)
        cmd("notes", "View notes", self.notes_slash)
        cmd("modstats", "View mod statistics", self.modstats_slash)

        # Misc
        cmd("setwelcomebg", "Set custom welcome background image", self.setwelcomebg)
        cmd("testwelcome", "Preview welcome card", self.testwelcome)
        cmd("welcomeall", "Welcome all members", self.welcomeall)
        cmd("ownerinfo", "View owner info", self.ownerinfo)

        # Emoji
        cmd("emojitutorial", "Show emoji submission tutorial", self.emoji_tutorial_slash)
        cmd("addemoji", "Request a new emoji", self.emoji_add_slash)
        cmd("stealemoji", "Request emojis from pasted custom emojis", self.emoji_steal_slash)

        # Helper
        cmd("modguide", "Show the simplified moderation guide", self.guide_slash)

    def _unregister_top_level_commands(self):
        """Clean up commands when cog is unloaded"""
        for command in self._slash_commands:
            try:
                self.bot.tree.remove_command(command.name)
            except Exception:
                pass
        self._slash_commands.clear()

    async def guide_slash(self, interaction: discord.Interaction):
        """Show a compact moderation guide focused on day-to-day commands."""
        embed = discord.Embed(
            title="Moderation Guide",
            description="Use `/mod` for common actions and `/moderation` when you need the full toolbox.",
            color=Config.COLOR_INFO,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Fast Actions",
            value=(
                "`/mod warn user reason`\n"
                "`/mod mute user duration reason`\n"
                "`/mod kick user reason`\n"
                "`/mod ban user reason`\n"
                "`/mod purge amount user`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Channel Control",
            value="`/mod lock`, `/mod unlock`, `/mod slowmode`",
            inline=False,
        )
        embed.add_field(
            name="Records",
            value="`/mod warnings`, `/mod case`, `/mod history`, `/moderation cases note`",
            inline=False,
        )
        embed.add_field(
            name="Reply Shortcuts",
            value=(
                "Reply to a user's message with `warn`, `mute`, `kick`, or `ban`.\n"
                "Reply to a bot moderation embed with `undo` where the action supports it."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def cog_check(self, ctx: commands.Context) -> bool:
        return True

    async def cog_load(self):
        """Initialize database table for quarantines and start tasks"""
        await self._register_top_level_commands()

        async with self.bot.db.get_connection() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS quarantines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    user_id INTEGER,
                    moderator_id INTEGER,
                    reason TEXT,
                    roles_backup TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    active INTEGER DEFAULT 1
                )
            """)
            await db.commit()
        
        if not self.check_quarantine_expiry.is_running():
            self.check_quarantine_expiry.start()

    async def cog_unload(self):
        if self.check_quarantine_expiry.is_running():
            self.check_quarantine_expiry.cancel()

        self._unregister_top_level_commands()

        for command_name in ("moderation", "emoji", "mod"):
            try:
                self.bot.tree.remove_command(command_name)
            except Exception:
                pass

    @tasks.loop(minutes=1)
    async def check_quarantine_expiry(self):
        """Check for expired quarantines and restore roles"""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            async with self.bot.db.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT id, guild_id, user_id, roles_backup, moderator_id FROM quarantines WHERE active = 1 AND expires_at IS NOT NULL AND expires_at <= ?",
                    (now,),
                )
                expired = await cursor.fetchall()
            
            for q in expired:
                q_id, guild_id, user_id, roles_backup, mod_id = q
                guild = self.bot.get_guild(guild_id)
                if not guild: continue
                
                try:
                    user = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.HTTPException):
                    user = None
                
                async with self.bot.db.get_connection() as conn:
                    await conn.execute("UPDATE quarantines SET active = 0 WHERE id = ?", (q_id,))
                    await conn.commit()
                
                if user:
                    try:
                        role_ids = json.loads(roles_backup)
                        await self._restore_roles(user, role_ids)
                    except (json.JSONDecodeError, discord.HTTPException) as e:
                        logger.warning(f"Failed to restore roles for {user_id}: {e}")
                        
                    settings = await self.bot.db.get_settings(guild.id)
                    q_role_id = settings.get("automod_quarantine_role_id")
                    if q_role_id:
                        role = guild.get_role(int(q_role_id))
                        if role:
                            try:
                                await user.remove_roles(role, reason="Quarantine expired")
                            except discord.HTTPException as e:
                                logger.warning(f"Failed to remove quarantine role: {e}")
                    
                    try:
                        embed = discord.Embed(title="✅ Quarantine Expired", description=f"{user.mention} released.", color=0x00ff00)
                        await self.log_action(guild, embed)
                    except Exception as e:
                        logger.debug(f"Failed to log quarantine expiry: {e}")
        except Exception as e:
            logger.error(f"Error checking quarantine expiry: {e}")

    @check_quarantine_expiry.before_loop
    async def before_quarantine_check(self):
        await self.bot.wait_until_ready()

    # ==================== REPLY-BASED QUICK ACTIONS ====================

    # Maps embed title keywords to the original action type
    _TITLE_TO_ACTION = {
        # Moderation core titles
        "banned": "ban",
        "temporarily banned": "tempban",
        "softbanned": "softban",
        "kicked": "kick",
        "muted": "mute",
        "quarantined": "quarantine",
        "jailed": "quarantine",
        "warned": "warn",
        "unmuted": "unmute",
        "unbanned": "unban",
        "unquarantined": "unquarantine",
        "unjailed": "unquarantine",
        # AI moderation titles/phrases
        "member banned": "ban",
        "user banned": "ban",
        "member kicked": "kick",
        "user kicked": "kick",
        "member warned": "warn",
        "user warned": "warn",
        "member timed out": "mute",
        "timed out": "mute",
        "timeout removed": "unmute",
        "remove timeout": "unmute",
        "user unbanned": "unban",
        "member unbanned": "unban",
    }

    # Reply shortcuts → what action to perform
    _REPLY_COMMANDS = {
        # Undo / reverse
        "undo":         "undo",
        "reverse":      "undo",
        "revert":       "undo",
        # Direct actions
        "unban":        "unban",
        "unmute":       "unmute",
        "untimeout":    "unmute",
        "unquar":       "unquarantine",
        "unquarantine": "unquarantine",
        "unjail":       "unquarantine",
        "unwarn":       "unwarn",
        "delwarn":      "unwarn",
        # Escalation — reply to any mod embed to escalate
        "ban":          "ban",
        "kick":         "kick",
        "mute":         "mute",
        "timeout":      "mute",
        "quarantine":   "quarantine",
        "quar":         "quarantine",
        "jail":         "quarantine",
        "warn":         "warn",
    }

    # What "undo" means for each action type
    _UNDO_MAP = {
        "ban":          "unban",
        "tempban":      "unban",
        "mute":         "unmute",
        "quarantine":   "unquarantine",
        "warn":         "unwarn",
        # These can't really be undone
        "kick":         None,
        "softban":      None,
    }

    _REPLY_REASON_PLACEHOLDERS = frozenset({
        "him",
        "her",
        "them",
        "that",
        "this",
        "it",
        "user",
        "member",
        "person",
        "guy",
    })

    def _extract_user_id(self, embed: discord.Embed) -> int | None:
        """Extract target user ID from a mod embed robustly."""
        search_text = f"{embed.title or ''}\n{embed.description or ''}"
        if embed.author and embed.author.name:
            search_text += f"\n{embed.author.name}"
        if embed.footer and embed.footer.text:
            search_text += f"\n{embed.footer.text}"
        for field in embed.fields:
            search_text += f"\n{field.name}\n{field.value}"
            
        if match := re.search(r'<@!?(\d{17,20})>', search_text):
            return int(match.group(1))
            
        if match := re.search(r'(?i)\b(?:user\s*id|target|member)[:\s#]*(\d{17,20})\b', search_text):
            return int(match.group(1))
            
        if match := re.search(r'\b(\d{17,20})\b', search_text):
            return int(match.group(1))
            
        return None

    def _detect_action(self, embed: discord.Embed) -> str | None:
        """Detect what mod action an embed represents from its title."""
        if not embed.title:
            return None
        title = embed.title.lower()
        for keyword, action in self._TITLE_TO_ACTION.items():
            if keyword in title:
                return action
        return None

    def _normalize_reply_reason(self, raw_reason: str) -> str:
        """Sanitize quick-reply reason text and drop placeholder words like 'him'."""
        reason = (raw_reason or "").strip()
        if not reason:
            return "Reply action"

        # Strip wrapping punctuation/quotes.
        reason = reason.strip("`'\"“”‘’.,:;!?- ").strip()
        if not reason:
            return "Reply action"

        # Common pattern: "for spam" / "because spam".
        reason = re.sub(r"^(?:for|because)\s+", "", reason, flags=re.IGNORECASE).strip()
        if not reason:
            return "Reply action"

        # Pattern: "him for spam" => "spam"
        pronoun_lead = re.match(
            r"^(him|her|them|that|this|it|user|member|person|guy)\b(?:\s+(?:for|because)\b)?\s*(.*)$",
            reason,
            flags=re.IGNORECASE,
        )
        if pronoun_lead:
            tail = pronoun_lead.group(2).strip()
            if tail:
                reason = tail
            else:
                return "Reply action"

        compact = re.sub(r"[\W_]+", "", reason).lower()
        if not compact or compact in self._REPLY_REASON_PLACEHOLDERS:
            return "Reply action"
        return reason

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle reply-based quick actions on mod embeds."""
        # Ignore bots, DMs, or messages without a reply reference
        if message.author.bot or not message.guild or not message.reference:
            return

        # Only process short reply commands with optional reason text.
        raw_parts = message.content.strip().split()
        if not raw_parts or len(raw_parts) > 8:
            return
        content = [p.lower() for p in raw_parts]

        cmd = content[0]
        if cmd not in self._REPLY_COMMANDS:
            return

        # Resolve the replied-to message
        try:
            ref_msg = message.reference.resolved
            if ref_msg is None:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.HTTPException):
            return

        # Two modes:
        # 1. Reply to a bot mod embed → extract target from embed, support "undo"
        # 2. Reply to any user's message → target = that message's author
        target_id = None
        original_action = None

        if ref_msg.author.id == self.bot.user.id and ref_msg.embeds:
            # Replying to a bot embed — detect action for undo, always extract target
            embed = ref_msg.embeds[0]
            original_action = self._detect_action(embed)
            
            # Check for direct mentions in the message content first
            non_bot_mentions = [m for m in ref_msg.mentions if not m.bot]
            if non_bot_mentions:
                target_id = non_bot_mentions[0].id
            else:
                target_id = self._extract_user_id(embed)
        
        if target_id is None and ref_msg.author.id != self.bot.user.id:
            # Replying to a regular user's message — target is that user
            target_id = ref_msg.author.id
            original_action = None  # No "undo" context for direct replies

        if target_id is None:
            return

        # Check staff permissions
        member = message.author
        mod_level = await self.get_user_level(message.guild.id, member)
        has_discord_staff_perm = (
            member.id == message.guild.owner_id
            or is_bot_owner_id(member.id)
            or member.guild_permissions.manage_messages
        )
        if mod_level < 4 and not has_discord_staff_perm:  # At least Mod level (or fallback Discord perm)
            try:
                await message.reply(
                    embed=discord.Embed(
                        title="❌ Permission Denied",
                        description="You need at least **Moderator** level to use reply actions.",
                        color=discord.Color.red()
                    ),
                    delete_after=10
                )
            except discord.HTTPException:
                pass
            return

        # Determine what action to take
        requested = self._REPLY_COMMANDS[cmd]
        if requested == "undo":
            if original_action is None:
                try:
                    await message.reply(
                        embed=discord.Embed(
                            title="⚠️ Cannot Undo",
                            description="Reply to a **bot moderation embed** to use undo.",
                            color=discord.Color.orange()
                        ),
                        delete_after=10
                    )
                except discord.HTTPException:
                    pass
                return
            action_to_take = self._UNDO_MAP.get(original_action)
            if action_to_take is None:
                try:
                    await message.reply(
                        embed=discord.Embed(
                            title="⚠️ Cannot Undo",
                            description=f"**{original_action.title()}** actions cannot be automatically undone.",
                            color=discord.Color.orange()
                        ),
                        delete_after=10
                    )
                except discord.HTTPException:
                    pass
                return
        else:
            action_to_take = requested

        # Parse optional reason from reply (e.g. "ban toxic behavior")
        reason = self._normalize_reply_reason(" ".join(raw_parts[1:]))

        # Execute the action
        try:
            await self._execute_reply_action(message, action_to_take, target_id, reason)
        except Exception as e:
            logger.error(f"Reply action error: {e}", exc_info=True)
            try:
                await message.reply(
                    embed=discord.Embed(
                        title="❌ Error",
                        description=f"Failed to execute: `{e}`",
                        color=discord.Color.red()
                    ),
                    delete_after=15
                )
            except discord.HTTPException:
                pass

    async def _execute_reply_action(self, message: discord.Message, action: str, target_id: int, reason: str):
        """Execute a mod action from a reply."""
        guild = message.guild

        if action in ("unban",):
            # Unban works with user ID directly
            await self._unban_logic(message, target_id, reason)
            return

        # For member-based actions, fetch the member
        try:
            target = await guild.fetch_member(target_id)
        except discord.NotFound:
            if action == "ban":
                # Can ban even if they left
                try:
                    user = await self.bot.fetch_user(target_id)
                    await guild.ban(user, reason=f"[Reply Action] {message.author}: {reason}")
                    await message.reply(
                        embed=discord.Embed(
                            title="🔨 User Banned",
                            description=f"<@{target_id}> has been banned.",
                            color=discord.Color.red()
                        )
                    )
                except Exception as e:
                    await message.reply(
                        embed=discord.Embed(
                            title="❌ Failed",
                            description=f"Could not ban: {e}",
                            color=discord.Color.red()
                        ),
                        delete_after=10
                    )
                return
            await message.reply(
                embed=discord.Embed(
                    title="⚠️ User Not Found",
                    description="That user is no longer in the server.",
                    color=discord.Color.orange()
                ),
                delete_after=10
            )
            return
        except discord.HTTPException:
            return

        # Dispatch to existing logic methods
        dispatch = {
            "unmute":        lambda: self._unmute_logic(message, target, reason),
            "unquarantine":  lambda: self._unquarantine_logic(message, target, reason),
            "unwarn":        lambda: self._delete_latest_warn(message, target),
            "ban":           lambda: self._ban_logic(message, target, reason),
            "kick":          lambda: self._kick_logic(message, target, reason),
            "mute":          lambda: self._mute_logic(message, target, "1h", reason),
            "quarantine":    lambda: self._quarantine_logic(message, target, None, reason),
            "warn":          lambda: self._warn_logic(message, target, reason),
        }

        handler = dispatch.get(action)
        if handler:
            await handler()
        else:
            await message.reply(
                embed=discord.Embed(
                    title="⚠️ Unknown Action",
                    description=f"Action `{action}` is not supported as a reply action.",
                    color=discord.Color.orange()
                ),
                delete_after=10
            )

    async def _delete_latest_warn(self, source, user: discord.Member):
        """Delete the most recent warning for a user (used by reply 'unwarn')."""
        try:
            warnings = await self.bot.db.get_warnings(source.guild.id, user.id)
            if not warnings:
                await source.reply(
                    embed=discord.Embed(
                        title="ℹ️ No Warnings",
                        description=f"{user.mention} has no warnings to remove.",
                        color=discord.Color.blue()
                    ),
                    delete_after=10
                )
                return
            latest = warnings[-1]
            warn_id = latest.get("id") or latest.get("warning_id")
            if warn_id:
                await self.bot.db.delete_warning(source.guild.id, warn_id)
            await source.reply(
                embed=discord.Embed(
                    title="✅ Warning Removed",
                    description=f"Removed the latest warning from {user.mention}.",
                    color=discord.Color.green()
                )
            )
        except Exception as e:
            await source.reply(
                embed=discord.Embed(
                    title="❌ Error",
                    description=f"Could not remove warning: {e}",
                    color=discord.Color.red()
                ),
                delete_after=10
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle new member joins (Whitelist & Welcome)"""
        if member.bot: 
            return # Optionally skip bots for welcome? Original skipped bot for whitelist.
        
        settings = await self.bot.db.get_settings(member.guild.id)
        
        # Whitelist Check
        if settings.get("whitelist_mode"):
            whitelisted_ids = settings.get("whitelisted_ids", [])
            if member.id not in whitelisted_ids and not member.bot:
                try:
                    await member.send(f"🔒 **{member.guild.name}** is currently in whitelist-only mode. You are not on the whitelist.")
                except (discord.Forbidden, discord.HTTPException):
                    pass
                
                try:
                    await member.kick(reason="Whitelist mode active - User not whitelisted")
                    # Log it
                    embed = discord.Embed(title="🔒 User Kicked (Whitelist)", color=0xff0000)
                    embed.add_field(name="User", value=f"{member} ({member.id})")
                    await self.log_action(member.guild, embed)
                    return # Stop processing
                except Exception as e:
                    logger.error(f"Failed to kick non-whitelisted user {member.id}: {e}")

            # Assign whitelist role if configured and user is allowed
            role_id = settings.get("whitelisted_role")
            if role_id:
                role = member.guild.get_role(int(role_id))
                if role:
                    try:
                        await member.add_roles(role, reason="Whitelist role auto-assign")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        logger.warning(f"Failed to assign whitelist role to {member.id}: {e}")
        
        # Welcome Message
        if not settings.get("welcome_enabled", False):
            return

        channel_id = settings.get("welcome_channel") or getattr(Config, "WELCOME_CHANNEL_ID", 0)
        if channel_id:
            try:
                channel = await self._resolve_message_channel(
                    member.guild,
                    channel_id,
                    purpose="welcome channel",
                )
            except Exception:
                logger.exception(
                    "Failed resolving welcome channel %r for guild %s",
                    channel_id,
                    member.guild.id,
                )
                channel = None

            if channel is not None:
                try:
                    await self._send_welcome_message(member=member, channel=channel)
                except Exception:
                    logger.exception(
                        "Failed sending welcome message for member %s in guild %s",
                        member.id,
                        member.guild.id,
                    )


# Helper Class for whitelist group if not in management.py
class WhitelistGroup(app_commands.Group):
    def __init__(self, cog: Moderation):
        super().__init__(name="whitelist", description="Manage server whitelist")
        self.cog = cog

    @app_commands.command(name="enable", description="Enable whitelist mode (lock server)")
    async def enable(self, interaction: discord.Interaction):
        await self.cog._toggle_whitelist_mode(interaction, True)

    @app_commands.command(name="disable", description="Disable whitelist mode")
    async def disable(self, interaction: discord.Interaction):
        await self.cog._toggle_whitelist_mode(interaction, False)

    @app_commands.command(name="add", description="Add user to whitelist")
    async def add(self, interaction: discord.Interaction, user: discord.Member):
        await self.cog._whitelist_logic(interaction, user)

    @app_commands.command(name="remove", description="Remove user from whitelist")
    async def remove(self, interaction: discord.Interaction, user: discord.Member):
        await self.cog._unwhitelist_logic(interaction, user)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
