import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional, Union
import re
import json
import asyncio

from utils.embeds import ModEmbed, Colors
from utils.checks import is_mod, is_senior_mod, is_admin, is_bot_owner_id
from utils.moderation_settings import (
    moderation_bool,
    moderation_id_set,
    render_moderation_response,
)
from utils.time_parser import parse_time
from config import Config

class ManagementCommands:
    @staticmethod
    def _quarantine_restricted_overwrite() -> discord.PermissionOverwrite:
        """Permissions quarantine role should have outside the jail channel."""
        return discord.PermissionOverwrite(
            view_channel=False,
            read_message_history=False,
            connect=False,
            send_messages=False,
            speak=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
        )

    @staticmethod
    def _quarantine_jail_overwrite() -> discord.PermissionOverwrite:
        """Permissions quarantine role should have inside the jail channel."""
        return discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            connect=True,
            speak=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
        )

    async def _sync_quarantine_overwrites(
        self,
        guild: discord.Guild,
        quarantine_role: discord.Role,
        jail_channel_id: Optional[Union[int, str]],
    ) -> tuple[int, int]:
        """Ensure quarantine role cannot access normal channels and can access jail."""
        target_jail_id: Optional[int] = None
        if jail_channel_id:
            try:
                target_jail_id = int(jail_channel_id)
            except (TypeError, ValueError):
                target_jail_id = None

        restricted_types = (
            discord.CategoryChannel,
            discord.TextChannel,
            discord.VoiceChannel,
            discord.StageChannel,
            discord.ForumChannel,
        )

        applied = 0
        failed = 0
        restricted_overwrite = self._quarantine_restricted_overwrite()
        jail_overwrite = self._quarantine_jail_overwrite()

        for channel in guild.channels:
            if not isinstance(channel, restricted_types):
                continue
            if target_jail_id and channel.id == target_jail_id:
                continue
            current_overwrite = channel.overwrites_for(quarantine_role)
            if current_overwrite == restricted_overwrite:
                continue
            try:
                await channel.set_permissions(
                    quarantine_role,
                    overwrite=restricted_overwrite,
                    reason="Quarantine enforcement",
                )
                applied += 1
            except Exception:
                failed += 1

        if target_jail_id:
            jail_channel = guild.get_channel(target_jail_id)
            if isinstance(
                jail_channel,
                (
                    discord.CategoryChannel,
                    discord.TextChannel,
                    discord.VoiceChannel,
                    discord.StageChannel,
                    discord.ForumChannel,
                ),
            ):
                try:
                    current_overwrite = jail_channel.overwrites_for(quarantine_role)
                    if current_overwrite != jail_overwrite:
                        await jail_channel.set_permissions(
                            quarantine_role,
                            overwrite=jail_overwrite,
                            reason="Quarantine jail access",
                        )
                        applied += 1
                except Exception:
                    failed += 1

        return applied, failed

    # ==================== KICK / BAN / MUTE LOGIC ====================

    async def _kick_logic(self, source, user: discord.Member, reason: str):
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        
        can_mod, error = await self.can_moderate(guild.id, moderator, user)
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Kick", error), ephemeral=True)
            
        can_bot, bot_error = await self.can_bot_moderate(user, moderator=moderator)
        if not can_bot:
            return await self._respond(source, embed=ModEmbed.error("Bot Permission Error", bot_error), ephemeral=True)

        settings = await self.bot.db.get_settings(guild.id)
        notify_user = moderation_bool(settings, "moderation_dm_users", True)
        dm_channel = await self.prepare_dm_channel(user) if notify_user else None
        
        dm_embed = discord.Embed(title=f"👢 Kicked from {guild.name}", description=f"**Reason:** {reason}", color=Colors.ERROR)
        case_num = await self.bot.db.create_case(guild.id, user.id, moderator.id, "Kick", reason)
        if notify_user:
            await self.send_punishment_notice(guild=guild, user=user, action="Kick", reason=reason, case_number=case_num, settings=settings, fallback_embed=dm_embed, delivery_channel=dm_channel)
        try:
            await user.kick(reason=f"{moderator}: {reason}")
        except Exception as e:
            return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not kick: {e}"), ephemeral=True)
            
        embed = await self.create_mod_embed(title="👢 User Kicked", user=user, moderator=moderator, reason=reason, color=Colors.ERROR, case_num=case_num)
        response = render_moderation_response(
            settings,
            "kick",
            user=user,
            reason=reason,
            moderator=moderator,
        )
        await self._respond(
            source,
            embed=ModEmbed.moderation_response("kick", response),
            allowed_mentions=discord.AllowedMentions.none(),
            delete_command_message=True,
        )
        await self.log_action(guild, embed)

    async def _ban_logic(self, source, user: discord.Member, reason: str, delete_days: int = 1):
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        
        can_mod, error = await self.can_moderate(guild.id, moderator, user)
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Ban", error), ephemeral=True)
            
        can_bot, bot_error = await self.can_bot_moderate(user, moderator=moderator)
        if not can_bot:
            return await self._respond(source, embed=ModEmbed.error("Bot Permission Error", bot_error), ephemeral=True)

        settings = await self.bot.db.get_settings(guild.id)
        notify_user = moderation_bool(settings, "moderation_dm_users", True)
        dm_channel = await self.prepare_dm_channel(user) if notify_user else None
        
        dm_embed = discord.Embed(title=f"🔨 Banned from {guild.name}", description=f"**Reason:** {reason}\n\nYou have been permanently banned.", color=Colors.DARK_RED)
        preserve_messages = moderation_bool(
            settings,
            "moderation_preserve_ban_messages",
            True,
        )
        effective_delete_days = 0 if preserve_messages else max(0, min(7, delete_days))
        self._suppress_duplicate_member_action_log(guild.id, user.id, "ban")
        case_num = await self.bot.db.create_case(guild.id, user.id, moderator.id, "Ban", reason)
        if notify_user:
            await self.send_punishment_notice(guild=guild, user=user, action="Ban", reason=reason, case_number=case_num, settings=settings, fallback_embed=dm_embed, delivery_channel=dm_channel)
        try:
            await user.ban(
                reason=f"{moderator}: {reason}",
                delete_message_days=effective_delete_days,
            )
        except Exception as e:
            return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not ban: {e}"), ephemeral=True)
            
        embed = await self.create_mod_embed(title="🔨 User Banned", user=user, moderator=moderator, reason=reason, color=Colors.DARK_RED, case_num=case_num)
        response = render_moderation_response(
            settings,
            "ban",
            user=user,
            reason=reason,
            moderator=moderator,
        )
        await self._respond(
            source,
            embed=ModEmbed.moderation_response("ban", response),
            allowed_mentions=discord.AllowedMentions.none(),
            delete_command_message=True,
        )
        await self.log_action(guild, embed)

    async def _unban_logic(self, source, user_id: int, reason: str):
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        
        try:
            user = await self.bot.fetch_user(user_id)
        except Exception:
             return await self._respond(source, embed=ModEmbed.error("Invalid User", "User not found."), ephemeral=True)

        self._suppress_duplicate_member_action_log(guild.id, user.id, "unban")
        try:
            await guild.unban(user, reason=f"{moderator}: {reason}")
        except discord.NotFound:
             return await self._respond(source, embed=ModEmbed.error("Not Banned", "User is not banned."), ephemeral=True)
        except Exception as e:
             return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not unban: {e}"), ephemeral=True)
             
        await self.bot.db.remove_tempban(guild.id, user.id)
        case_num = await self.bot.db.create_case(guild.id, user.id, moderator.id, "Unban", reason)

        settings = await self.bot.db.get_settings(guild.id)
        embed = await self.create_mod_embed(title="🔓 User Unbanned", user=user, moderator=moderator, reason=reason, color=Colors.SUCCESS, case_num=case_num)
        response = render_moderation_response(
            settings,
            "unban",
            user=user,
            reason=reason,
            moderator=moderator,
        )
        await self._respond(
            source,
            embed=ModEmbed.moderation_response("unban", response),
            allowed_mentions=discord.AllowedMentions.none(),
            delete_command_message=True,
        )
        await self.log_action(guild, embed)

    async def _softban_logic(self, source, user: discord.Member, reason: str):
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        
        can_mod, error = await self.can_moderate(guild.id, moderator, user)
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Softban", error), ephemeral=True)
            
        can_bot, bot_error = await self.can_bot_moderate(user)
        if not can_bot:
            return await self._respond(source, embed=ModEmbed.error("Bot Permission Error", bot_error), ephemeral=True)

        settings = await self.bot.db.get_settings(guild.id)
        notify_user = moderation_bool(settings, "moderation_dm_users", True)
        dm_channel = await self.prepare_dm_channel(user) if notify_user else None
        
        dm_embed = discord.Embed(title=f"🧹 Softbanned from {guild.name}", description=f"**Reason:** {reason}\n\nYou can rejoin the server.", color=Colors.ERROR)
        self._suppress_duplicate_member_action_log(guild.id, user.id, "ban")
        self._suppress_duplicate_member_action_log(guild.id, user.id, "unban")
        case_num = await self.bot.db.create_case(guild.id, user.id, moderator.id, "Softban", reason)
        if notify_user:
            await self.send_punishment_notice(guild=guild, user=user, action="Softban", reason=reason, case_number=case_num, settings=settings, fallback_embed=dm_embed, delivery_channel=dm_channel)
        try:
            await user.ban(reason=f"[SOFTBAN] {reason}", delete_message_days=7)
            await guild.unban(user, reason="Softban - immediate unban")
        except Exception as e:
            return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not softban: {e}"), ephemeral=True)
            
        embed = await self.create_mod_embed(title="🧹 User Softbanned", user=user, moderator=moderator, reason=reason, color=Colors.ERROR, case_num=case_num)
        embed.set_footer(text=f"Case #{case_num} | 7 days of messages deleted")
        response = render_moderation_response(
            settings,
            "softban",
            user=user,
            reason=reason,
            moderator=moderator,
        )
        await self._respond(
            source,
            embed=ModEmbed.moderation_response("softban", response),
            allowed_mentions=discord.AllowedMentions.none(),
            delete_command_message=True,
        )
        await self.log_action(guild, embed)

    async def _tempban_logic(self, source, user: discord.Member, duration: str, reason: str):
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        
        can_mod, error = await self.can_moderate(guild.id, moderator, user)
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Tempban", error), ephemeral=True)
            
        can_bot, bot_error = await self.can_bot_moderate(user, moderator=moderator)
        if not can_bot:
            return await self._respond(source, embed=ModEmbed.error("Bot Permission Error", bot_error), ephemeral=True)
        
        parsed = parse_time(duration)
        if not parsed:
            return await self._respond(source, embed=ModEmbed.error("Invalid Duration", "Use format like `1d`, `7d`, `30d`"), ephemeral=True)
        
        delta, human_duration = parsed
        expires_at = datetime.now(timezone.utc) + delta

        settings = await self.bot.db.get_settings(guild.id)
        notify_user = moderation_bool(settings, "moderation_dm_users", True)
        dm_channel = await self.prepare_dm_channel(user) if notify_user else None
        
        dm_embed = discord.Embed(title=f"⏰ Temporarily Banned from {guild.name}", description=f"**Reason:** {reason}\n**Duration:** {human_duration}\n**Expires:** <t:{int(expires_at.timestamp())}:F>", color=Colors.DARK_RED)
        preserve_messages = moderation_bool(
            settings,
            "moderation_preserve_ban_messages",
            True,
        )
        self._suppress_duplicate_member_action_log(guild.id, user.id, "ban")
        case_num = await self.bot.db.create_case(guild.id, user.id, moderator.id, "Tempban", reason, human_duration)
        if notify_user:
            await self.send_punishment_notice(guild=guild, user=user, action="Tempban", reason=reason, case_number=case_num, settings=settings, fallback_embed=dm_embed, duration=human_duration, punishment_expires_at=expires_at, delivery_channel=dm_channel)
        try:
            await user.ban(
                reason=f"[TEMPBAN] {moderator}: {reason} ({human_duration})",
                delete_message_days=0 if preserve_messages else 1,
            )
        except Exception as e:
            return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not tempban: {e}"), ephemeral=True)
        await self.bot.db.add_tempban(guild.id, user.id, moderator.id, reason, expires_at)
            
        embed = await self.create_mod_embed(title="⏰ User Temporarily Banned", user=user, moderator=moderator, reason=reason, color=Colors.DARK_RED, case_num=case_num, extra_fields={"Duration": human_duration, "Expires": f"<t:{int(expires_at.timestamp())}:R>"})
        await self._respond(source, embed=embed, delete_command_message=True)
        await self.log_action(guild, embed)

    async def _mute_logic(self, source, user: discord.Member, duration: str, reason: str):
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author

        # Discord does not support timing out bot accounts.
        if user.bot:
            return await self._respond(
                source,
                embed=ModEmbed.error(
                    "Cannot Timeout Bot",
                    "Discord does not allow timeouts on bot accounts. Use kick/ban/quarantine instead.",
                ),
                ephemeral=True,
            )
        
        can_mod, error = await self.can_moderate(guild.id, moderator, user)
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Mute", error), ephemeral=True)
            
        can_bot, bot_error = await self.can_bot_moderate(user, moderator=moderator)
        if not can_bot:
            return await self._respond(source, embed=ModEmbed.error("Bot Permission Error", bot_error), ephemeral=True)

        settings = await self.bot.db.get_settings(guild.id)

        bot_member = guild.me
        if not bot_member.guild_permissions.moderate_members:
            return await self._respond(source, embed=ModEmbed.error("Bot Missing Permissions", "I need **Timeout Members** permission."), ephemeral=True)
        
        parsed = parse_time(duration)
        if not parsed:
            return await self._respond(source, embed=ModEmbed.error("Invalid Duration", "Use format like `10m`, `1h`, `1d`"), ephemeral=True)
        
        delta, human_duration = parsed
        if delta.total_seconds() > 28 * 24 * 60 * 60:
            return await self._respond(source, embed=ModEmbed.error("Duration Too Long", "Max 28 days."), ephemeral=True)
        expires_at = datetime.now(timezone.utc) + delta

        notify_user = moderation_bool(settings, "moderation_dm_users", True)
        dm_channel = await self.prepare_dm_channel(user) if notify_user else None
        case_num = await self.bot.db.create_case(guild.id, user.id, moderator.id, "Mute", reason, human_duration)
        dm_embed = discord.Embed(title=f"🔇 Muted in {guild.name}", description=f"**Reason:** {reason}\n**Duration:** {human_duration}", color=Colors.ERROR)
        if notify_user:
            await self.send_punishment_notice(guild=guild, user=user, action="Mute", reason=reason, case_number=case_num, settings=settings, fallback_embed=dm_embed, duration=human_duration, punishment_expires_at=expires_at, delivery_channel=dm_channel)

        logging_cog = self.bot.get_cog("Logging")
        if logging_cog and hasattr(logging_cog, "suppress_timeout_change_log"):
            logging_cog.suppress_timeout_change_log(guild.id, user.id)
        
        try:
            await user.timeout(delta, reason=reason)
        except discord.Forbidden:
            return await self._respond(
                source,
                embed=ModEmbed.error(
                    "Failed",
                    "I cannot timeout this user due Discord role hierarchy/permissions. "
                    "Ensure my role is above the target and I have Timeout Members.",
                ),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not timeout: {e}"), ephemeral=True)

        log_embed = await self.create_mod_embed(
            title=f"Mute {user.display_name}",
            user=user,
            moderator=moderator,
            reason=reason,
            color=Colors.ERROR,
            case_num=case_num,
            extra_fields={"Duration": human_duration},
        )
        response = render_moderation_response(
            settings,
            "mute",
            user=user,
            reason=reason,
            moderator=moderator,
            duration=human_duration,
        )
        await self._respond(
            source,
            embed=ModEmbed.moderation_response("mute", response),
            allowed_mentions=discord.AllowedMentions.none(),
            delete_command_message=True,
        )
        await self.log_action(guild, log_embed)

    async def _unmute_logic(self, source, user: discord.Member, reason: str):
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author

        if not user.is_timed_out():
            return await self._respond(source, embed=ModEmbed.error("Not Muted", "User is not muted."), ephemeral=True)

        can_mod, error = await self.can_moderate(
            guild.id,
            moderator,
            user,
            allow_protected_target=True,
        )
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Unmute", error), ephemeral=True)

        bot_member = guild.me
        if not bot_member.guild_permissions.moderate_members:
            return await self._respond(source, embed=ModEmbed.error("Bot Missing Permissions", "I need **Timeout Members** permission."), ephemeral=True)

        logging_cog = self.bot.get_cog("Logging")
        if logging_cog and hasattr(logging_cog, "suppress_timeout_change_log"):
            logging_cog.suppress_timeout_change_log(guild.id, user.id)

        try:
            await user.timeout(None, reason=reason)
        except Exception as e:
            return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not unmute: {e}"), ephemeral=True)
            
        case_num = await self.bot.db.create_case(guild.id, user.id, moderator.id, "Unmute", reason)
        settings = await self.bot.db.get_settings(guild.id)

        log_embed = await self.create_mod_embed(
            title=f"Unmute {user.display_name}",
            user=user,
            moderator=moderator,
            reason=reason,
            color=Colors.SUCCESS,
            case_num=case_num,
        )
        response = render_moderation_response(
            settings,
            "unmute",
            user=user,
            reason=reason,
            moderator=moderator,
        )
        await self._respond(
            source,
            embed=ModEmbed.moderation_response("unmute", response),
            allowed_mentions=discord.AllowedMentions.none(),
            delete_command_message=True,
        )
        await self.log_action(guild, log_embed)

    # ==================== MASS ACTIONS ====================

    async def _massban_logic(self, source, user_ids_str: str, reason: str):
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        else:
            await source.typing()

        parts = user_ids_str.split()
        parsed_ids = []
        parsed_reason_parts = []
        
        for part in parts:
            if part.isdigit() and len(part) > 15:
                parsed_ids.append(part)
            else:
                parsed_reason_parts.append(part)
        
        if reason == "Mass ban" and parsed_reason_parts:
             reason = " ".join(parsed_reason_parts)
        
        banned = []
        failed = []
        settings = await self.bot.db.get_settings(source.guild.id)
        protected_role_ids = moderation_id_set(settings, "protected_roles")
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        owner_override = (
            moderator.id == source.guild.owner_id
            or await self._is_bot_owner(moderator)
        )
        preserve_messages = moderation_bool(
            settings,
            "moderation_preserve_ban_messages",
            True,
        )
        
        for uid in parsed_ids:
            try:
                user_id = int(uid)
                member = source.guild.get_member(user_id)
                if member is None:
                    try:
                        member = await source.guild.fetch_member(user_id)
                    except discord.NotFound:
                        member = None
                    except (discord.Forbidden, discord.HTTPException):
                        failed.append(f"`{uid}` (could not verify protected roles)")
                        continue

                if member is not None:
                    if (
                        not owner_override
                        and protected_role_ids
                        and any(
                            member_role.id in protected_role_ids
                            for member_role in member.roles
                        )
                    ):
                        failed.append(f"`{uid}` (protected role)")
                        continue
                    user_str = f"{member} (`{member.id}`)"
                    ban_target = member
                else:
                    try:
                        user = await self.bot.fetch_user(user_id)
                        user_str = f"{user} (`{user.id}`)"
                        ban_target = user
                    except discord.NotFound:
                        user_str = f"User {user_id}"
                        ban_target = discord.Object(id=user_id)

                self._suppress_duplicate_member_action_log(source.guild.id, user_id, "ban")
                await source.guild.ban(
                    ban_target,
                    reason=f"[MASSBAN] {moderator}: {reason}",
                    delete_message_days=0 if preserve_messages else 1,
                )
                banned.append(user_str)
            except ValueError:
                failed.append(f"`{uid}` (invalid ID)")
            except discord.Forbidden:
                failed.append(f"`{uid}` (permission denied)")
            except discord.HTTPException as e:
                failed.append(f"`{uid}` ({str(e)})")
        
        embed = discord.Embed(
            title="🔨 Mass Ban Complete",
            color=Colors.DARK_RED,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="✅ Banned", value=f"**{len(banned)}** users", inline=True)
        embed.add_field(name="❌ Failed", value=f"**{len(failed)}** users", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        
        if banned:
            embed.add_field(
                name="Banned Users",
                value="\n".join(banned[:10]) + (f"\n*...and {len(banned) - 10} more*" if len(banned) > 10 else ""),
                inline=False
            )
        
        if failed:
            embed.add_field(
                name="Failed Users",
                value="\n".join(failed[:10]) + (f"\n*...and {len(failed) - 10} more*" if len(failed) > 10 else ""),
                inline=False
            )
        
        await self._respond(source, embed=embed)
        await self.log_action(source.guild, embed)

    async def _mass_kick_role(self, source, role: discord.Role, reason: str):
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        settings = await self.bot.db.get_settings(source.guild.id)
        protected_role_ids = moderation_id_set(settings, "protected_roles")
        owner_override = (
            moderator.id == source.guild.owner_id
            or await self._is_bot_owner(moderator)
        )
        
        await self._respond(source, embed=ModEmbed.info("Mass Kick", f"Kicking {len(role.members)} members..."), ephemeral=True)
        
        count = 0
        failed = 0
        
        for member in role.members:
            if (
                not owner_override
                and protected_role_ids
                and any(
                    member_role.id in protected_role_ids
                    for member_role in member.roles
                )
            ):
                failed += 1
                continue
            if member.top_role >= moderator.top_role and not owner_override:
                failed += 1
                continue
            try:
                await member.kick(reason=f"Mass kick by {moderator}: {reason}")
                count += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        
        await self._respond(source, embed=ModEmbed.success("Mass Kick Complete", f"Kicked {count} members.\nFailed: {failed}"), ephemeral=False)

    async def _mass_ban_role(self, source, role: discord.Role, reason: str):
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        settings = await self.bot.db.get_settings(source.guild.id)
        preserve_messages = moderation_bool(
            settings,
            "moderation_preserve_ban_messages",
            True,
        )
        protected_role_ids = moderation_id_set(settings, "protected_roles")
        owner_override = (
            moderator.id == source.guild.owner_id
            or await self._is_bot_owner(moderator)
        )
        
        await self._respond(source, embed=ModEmbed.info("Mass Ban", f"Banning {len(role.members)} members..."), ephemeral=True)
        
        count = 0
        failed = 0
        
        for member in role.members:
            if (
                not owner_override
                and protected_role_ids
                and any(
                    member_role.id in protected_role_ids
                    for member_role in member.roles
                )
            ):
                failed += 1
                continue
            if member.top_role >= moderator.top_role and not owner_override:
                failed += 1
                continue
            try:
                self._suppress_duplicate_member_action_log(source.guild.id, member.id, "ban")
                await member.ban(
                    reason=f"Mass ban by {moderator}: {reason}",
                    delete_message_days=0 if preserve_messages else 1,
                )
                count += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        
        await self._respond(source, embed=ModEmbed.success("Mass Ban Complete", f"Banned {count} members.\nFailed: {failed}"), ephemeral=False)

    async def _banlist_logic(self, source):
        # source: commands.Context or discord.Interaction
        guild = source.guild
        try:
            bans = [entry async for entry in guild.bans(limit=50)]
        except discord.Forbidden:
            return await self._respond(source, embed=ModEmbed.error("Permission Denied", "I don't have permission to view bans."))
        
        if not bans:
            return await self._respond(source, embed=ModEmbed.info("No Bans", "No users are currently banned."))
        
        embed = discord.Embed(
            title=f"🔨 Ban List ({len(bans)} total)",
            color=Colors.ERROR,
            timestamp=datetime.now(timezone.utc)
        )
        
        ban_list = []
        for ban in bans[:20]:
            reason = ban.reason or "*No reason provided*"
            ban_list.append(f"**{ban.user}** (`{ban.user.id}`)\n└ {reason[:80]}")
        
        embed.description = "\n\n".join(ban_list)
        
        if len(bans) > 20:
            embed.set_footer(text=f"Showing 20 of {len(bans)} bans")
        
        await self._respond(source, embed=embed)

    # ==================== NICKNAME / ROLE MANAGEMENT ====================

    async def _setnick_logic(self, source, user: discord.Member, nickname: str = None):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        can_mod, error = await self.can_moderate(source.guild.id, author, user)
        if not can_mod:
             return await self._respond(source, embed=ModEmbed.error("Cannot Change", error))
        
        old_nick = user.display_name
        
        try:
            await user.edit(nick=nickname)
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to change nicknames."))
        
        new_nick = nickname or user.name
        embed = ModEmbed.success(
            "Nickname Changed",
            f"**{old_nick}** → **{new_nick}**"
        )
        await self._respond(source, embed=embed)

    async def _rename_logic(self, source, user: discord.Member, nickname: Optional[str] = None):
        # Alias for _setnick_logic with different response style or just wrapper?
        # Original code had logic inline. Let's use _setnick_logic but adapt or reimplement to match original _rename_logic from 2143.
        # Original _rename_logic used "Renamed" reason etc.
        # Let's stick to _setnick_logic but maybe improve it.
        # Wait, I'll just use _rename_logic logic here because it logs action etc.
        guild = source.guild
        moderator = source.user if isinstance(source, discord.Interaction) else source.author
        
        can_mod, error = await self.can_moderate(guild.id, moderator, user)
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Rename", error), ephemeral=True)
            
        can_bot, bot_error = await self.can_bot_moderate(user, moderator=moderator)
        if not can_bot:
            return await self._respond(source, embed=ModEmbed.error("Bot Permission Error", bot_error), ephemeral=True)

        old_nick = user.display_name
        try:
            await user.edit(nick=nickname, reason=f"Renamed by {moderator}")
        except Exception as e:
            return await self._respond(source, embed=ModEmbed.error("Failed", f"Could not rename: {e}"), ephemeral=True)
            
        action = f"Renamed to `{nickname}`" if nickname else "Reset nickname"
        embed = ModEmbed.success("User Renamed", f"{user.mention} {action}.")
        embed.set_footer(text=f"Old: {old_nick}")
        
        await self._respond(source, embed=embed)
        await self.log_action(guild, embed)

    async def _nicknameall_logic(self, source, nickname_template: str):
        guild = source.guild
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        else:
            await source.typing()
        
        changed = []
        failed = []
        
        for member in guild.members:
            if member.bot:
                continue
            
            try:
                new_nick = nickname_template.replace('{user}', member.name)
                await member.edit(nick=new_nick)
                changed.append(member.mention)
            except (discord.Forbidden, discord.HTTPException):
                failed.append(member.mention)
        
        embed = discord.Embed(
            title="✏️ Bulk Nickname Change",
            color=Colors.SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="✅ Changed", value=f"**{len(changed)}** members", inline=True)
        embed.add_field(name="❌ Failed", value=f"**{len(failed)}** members", inline=True)
        
        await self._respond(source, embed=embed)

    async def _resetnicks_logic(self, source):
        guild = source.guild
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        else:
            await source.typing()
        
        reset = []
        failed = []
        
        for member in guild.members:
            if not member.nick:
                continue
            
            try:
                await member.edit(nick=None)
                reset.append(member.mention)
            except (discord.Forbidden, discord.HTTPException):
                failed.append(member.mention)
        
        embed = discord.Embed(
            title="🔄 Nicknames Reset",
            color=Colors.SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="✅ Reset", value=f"**{len(reset)}** members", inline=True)
        embed.add_field(name="❌ Failed", value=f"**{len(failed)}** members", inline=True)
        
        await self._respond(source, embed=embed)

    async def _roleall_logic(self, source, role: discord.Role):
        guild = source.guild
        author = source.user if isinstance(source, discord.Interaction) else source.author
        
        if role >= guild.me.top_role:
             return await self._respond(source, embed=ModEmbed.error("Bot Error", "I cannot manage this role as it's higher than or equal to my highest role."))
        
        if role >= author.top_role and author.id != guild.owner_id and not is_bot_owner_id(author.id):
             return await self._respond(source, embed=ModEmbed.error("Permission Denied", "You cannot assign a role higher than or equal to your highest role."))
        
        if role.managed:
             return await self._respond(source, embed=ModEmbed.error("Managed Role", "This role is managed by an integration and cannot be manually assigned."))
        
        if (
            role.permissions.administrator
            or role.permissions.manage_guild
            or role.permissions.manage_roles
            or role.permissions.ban_members
            or role.permissions.kick_members
        ) and not is_bot_owner_id(author.id):
             return await self._respond(source, embed=ModEmbed.error("Dangerous Role", "You cannot mass-assign dangerous permissions."))
        
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        else:
            await source.typing()
        
        success = []
        failed = []
        
        for member in guild.members:
            if role in member.roles:
                continue
            
            try:
                await member.add_roles(role, reason=f"Mass role assignment by {author}")
                success.append(member.mention)
                await asyncio.sleep(1.0)  # Rate limit protection
            except (discord.Forbidden, discord.HTTPException):
                failed.append(member.mention)
        
        embed = discord.Embed(
            title="🏷️ Bulk Role Assignment",
            color=Colors.SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="✅ Added", value=f"**{len(success)}** members", inline=True)
        embed.add_field(name="❌ Failed", value=f"**{len(failed)}** members", inline=True)
        embed.add_field(name="Role", value=role.mention, inline=False)
        
        await self._respond(source, embed=embed)

    async def _removeall_logic(self, source, role: discord.Role):
        guild = source.guild
        author = source.user if isinstance(source, discord.Interaction) else source.author
        
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        else:
            await source.typing()
        
        success = []
        failed = []
        
        for member in guild.members:
            if role not in member.roles:
                continue
            
            try:
                await member.remove_roles(role)
                success.append(member.mention)
                await asyncio.sleep(1.0)
            except (discord.Forbidden, discord.HTTPException):
                failed.append(member.mention)
        
        embed = discord.Embed(
            title="🗑️ Bulk Role Removal",
            color=Colors.SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="✅ Removed", value=f"**{len(success)}** members", inline=True)
        embed.add_field(name="❌ Failed", value=f"**{len(failed)}** members", inline=True)
        embed.add_field(name="Role", value=role.mention, inline=False)
        
        await self._respond(source, embed=embed)

    async def _inrole_logic(self, source, role: discord.Role):
        members = role.members
        
        if not members:
            return await self._respond(source,
                embed=ModEmbed.info("No Members", f"No one has the {role.mention} role.")
            )
        
        embed = discord.Embed(
            title=f"Members with {role.name}",
            color=role.color,
            timestamp=datetime.now(timezone.utc)
        )
        
        member_list = [m.mention for m in members[:30]]
        embed.description = ", ".join(member_list)
        
        if len(members) > 30:
            embed.set_footer(text=f"Showing 30 of {len(members)} members")
        else:
            embed.set_footer(text=f"{len(members)} total members")
        
        await self._respond(source, embed=embed)

    # ==================== QUARANTINE / WHITELIST ====================

    async def _quarantine_logic(self, source, user: discord.Member, duration_str: str = None, reason: str = "No reason provided"):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        
        can_mod, error = await self.can_moderate(source.guild.id, author, user)
        if not can_mod:
             return await self._respond(source, embed=ModEmbed.error("Cannot Quarantine", error), ephemeral=True)
             
        can_bot, bot_error = await self.can_bot_moderate(user, moderator=author)
        if not can_bot:
             return await self._respond(source, embed=ModEmbed.error("Bot Permission Error", bot_error), ephemeral=True)

        settings = await self.bot.db.get_settings(source.guild.id)
        quarantine_role_id = settings.get("automod_quarantine_role_id")
        
        if not quarantine_role_id:
             return await self._respond(source, embed=ModEmbed.error("Not Configured", "Quarantine role is not set."), ephemeral=True)
             
        quarantine_role = source.guild.get_role(int(quarantine_role_id))
        if not quarantine_role:
             return await self._respond(source, embed=ModEmbed.error("Configuration Error", "Quarantine role not found."), ephemeral=True)

        overwrite_applied = 0
        overwrite_failed = 0
        bot_member = source.guild.me
        if bot_member and bot_member.guild_permissions.manage_channels:
            overwrite_applied, overwrite_failed = await self._sync_quarantine_overwrites(
                source.guild,
                quarantine_role,
                settings.get("quarantine_channel"),
            )

        # Calculate duration
        expires_at = None
        human_duration = "Indefinite"
        if duration_str:
            parsed = parse_time(duration_str)
            if parsed:
                delta, human_duration = parsed
                expires_at = datetime.now(timezone.utc) + delta

        notify_user = moderation_bool(settings, "moderation_dm_users", True)
        dm_channel = await self.prepare_dm_channel(user) if notify_user else None
        case_num = await self.bot.db.create_case(
            source.guild.id,
            user.id,
            author.id,
            "Quarantine",
            reason,
            human_duration,
        )
        dm_embed = discord.Embed(
            title=f"Quarantined in {source.guild.name}",
            description=f"**Reason:** {reason}\n**Duration:** {human_duration}",
            color=Colors.DARK_RED,
        )
        if notify_user:
            await self.send_punishment_notice(
                guild=source.guild,
                user=user,
                action="Quarantine",
                reason=reason,
                case_number=case_num,
                settings=settings,
                fallback_embed=dm_embed,
                duration=human_duration,
                punishment_expires_at=expires_at,
                delivery_channel=dm_channel,
            )

        # Backup roles
        backup_role_ids = await self._backup_roles(user, quarantine_role.id)
        
        # Apply quarantine
        try:
            await user.edit(roles=[quarantine_role], reason=f"[QUARANTINE] {author}: {reason}")
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to edit roles."), ephemeral=True)

        # DB
        await self.bot.db.add_quarantine(
            source.guild.id,
            user.id,
            author.id,
            reason,
            expires_at,
            backup_role_ids
        )
        
        embed = discord.Embed(
            title="☣️ User Quarantined",
            description=f"{user.mention} has been quarantined.",
            color=Colors.DARK_RED
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value=human_duration, inline=True)
        if expires_at:
             embed.add_field(name="Expires", value=f"<t:{int(expires_at.timestamp())}:R>", inline=True)
        
        embed.add_field(name="Roles Removed", value=str(len(backup_role_ids)), inline=True)
        if overwrite_applied or overwrite_failed:
            embed.add_field(
                name="Channel Sync",
                value=f"Updated {overwrite_applied} channels (failed: {overwrite_failed})",
                inline=False,
            )
        elif not (bot_member and bot_member.guild_permissions.manage_channels):
            embed.add_field(
                name="Channel Sync",
                value="Skipped: I need **Manage Channels** to enforce quarantine visibility.",
                inline=False,
            )
        
        await self._respond(source, embed=embed)
        await self.log_action(source.guild, embed)

        # Auto-post notice in jail channel (if configured).
        jail_channel_id = settings.get("quarantine_channel")
        if jail_channel_id:
            jail_channel = source.guild.get_channel(int(jail_channel_id))
            if isinstance(jail_channel, discord.TextChannel):
                jail_embed = discord.Embed(
                    title="Quarantine Notice",
                    description=(
                        f"{user.mention}, you are quarantined.\n"
                        "Please wait for staff instructions here."
                    ),
                    color=Colors.DARK_RED,
                    timestamp=datetime.now(timezone.utc),
                )
                jail_embed.add_field(name="Reason", value=reason, inline=False)
                jail_embed.add_field(name="Duration", value=human_duration, inline=True)
                if expires_at:
                    jail_embed.add_field(name="Expires", value=f"<t:{int(expires_at.timestamp())}:R>", inline=True)
                try:
                    await jail_channel.send(
                        content=user.mention,
                        embed=jail_embed,
                        allowed_mentions=discord.AllowedMentions(users=True),
                        use_v2=False,
                    )
                except Exception:
                    pass

    async def _unquarantine_logic(self, source, user: discord.Member, reason: str = "Quarantine lifted"):
        author = source.user if isinstance(source, discord.Interaction) else source.author

        active = await self._get_active_quarantine(source.guild.id, user.id)
        if not active:
             return await self._respond(source, embed=ModEmbed.error("Not Quarantined", "This user is not quarantined."), ephemeral=True)

        can_mod, error = await self.can_moderate(source.guild.id, author, user)
        if not can_mod:
            return await self._respond(source, embed=ModEmbed.error("Cannot Unquarantine", error), ephemeral=True)

        try:
            role_ids = json.loads(active['roles_backup'])
        except (json.JSONDecodeError, TypeError, KeyError):
            role_ids = []
            pass
        
        restored, failed = await self._restore_roles(user, role_ids)
        
        # Remove quarantine role
        settings = await self.bot.db.get_settings(source.guild.id)
        quarantine_role_id = settings.get("automod_quarantine_role_id")
        if quarantine_role_id:
            role = source.guild.get_role(int(quarantine_role_id))
            if role and role in user.roles:
                try:
                    await user.remove_roles(role, reason=f"[UNQUARANTINE] {author}: {reason}")
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(f"Failed to remove quarantine role: {e}")

        # DB update
        await self.bot.db.remove_quarantine(source.guild.id, user.id)
        
        embed = discord.Embed(
            title="✅ User Unquarantined",
            description=f"{user.mention} has been released from quarantine.",
            color=Colors.SUCCESS
        )
        embed.add_field(name="Restored Roles", value=f"{restored} (Failed: {failed})", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        
        await self._respond(source, embed=embed)
        await self.log_action(source.guild, embed)

    async def _whitelist_logic(self, source, user: Union[discord.Member, discord.User]):
        guild = source.guild
        settings = await self.bot.db.get_settings(guild.id)
        whitelisted_ids = settings.get("whitelisted_ids", [])
        
        if user.id not in whitelisted_ids:
            whitelisted_ids.append(user.id)
            settings["whitelisted_ids"] = whitelisted_ids
            await self.bot.db.update_settings(guild.id, settings)
        
        role_added = False
        role_id = settings.get("whitelisted_role")
        if role_id and isinstance(user, discord.Member):
            role = guild.get_role(int(role_id))
            if role:
                try:
                    await user.add_roles(role, reason="Whitelisted by admin")
                    role_added = True
                except Exception:
                    pass
        
        msg = f"**{user}** added to whitelist."
        if role_added:
            msg += f" Assigned {role.mention}."
        
        await self._respond(source, embed=ModEmbed.success("Whitelisted", msg))

    async def _unwhitelist_logic(self, source, user: Union[discord.Member, discord.User]):
        guild = source.guild
        settings = await self.bot.db.get_settings(guild.id)
        whitelisted_ids = settings.get("whitelisted_ids", [])
        
        if user.id in whitelisted_ids:
            whitelisted_ids.remove(user.id)
            settings["whitelisted_ids"] = whitelisted_ids
            await self.bot.db.update_settings(guild.id, settings)
        
        role_removed = False
        role_id = settings.get("whitelisted_role")
        if role_id and isinstance(user, discord.Member):
            role = guild.get_role(int(role_id))
            if role:
                try:
                    await user.remove_roles(role, reason="Unwhitelisted by admin")
                    role_removed = True
                except Exception:
                    pass

        msg = f"**{user}** removed from whitelist."
        if role_removed:
            msg += f" Removed {role.mention}."
        
        await self._respond(source, embed=ModEmbed.success("Unwhitelisted", msg))

    async def _toggle_whitelist_mode(self, source, enable: bool):
        guild = source.guild
        settings = await self.bot.db.get_settings(guild.id)
        
        if enable:
            settings["whitelist_mode"] = True
            await self.bot.db.update_settings(guild.id, settings)
            
            await self._respond(source, embed=ModEmbed.warning("Whitelist Enabled", "🔒 Whitelist mode enabled. Scanning for non-whitelisted members..."))
            
            whitelisted_ids = set(settings.get("whitelisted_ids", []))
            kicked = 0
            
            for member in guild.members:
                if member.bot: continue
                if member.id not in whitelisted_ids and not member.guild_permissions.administrator:
                    try:
                        await member.kick(reason="Server lockdown: Not whitelisted")
                        kicked += 1
                    except Exception:
                        pass
            
            await self._respond(source, embed=ModEmbed.success("Scan Complete", f"Kicked **{kicked}** non-whitelisted members."))
        else:
            settings["whitelist_mode"] = False
            await self.bot.db.update_settings(guild.id, settings)
            await self._respond(source, embed=ModEmbed.success("Whitelist Disabled", "🔓 Whitelist mode disabled. Regular joins allowed."))

    # ==================== COMMANDS ====================

    @commands.command(name="kick")
    @is_mod()
    async def kick_prefix(self, ctx: commands.Context, target: Union[discord.Role, discord.Member], *, reason: str = "No reason"):
        if isinstance(target, discord.Role):
            await self._mass_kick_role(ctx, target, reason)
        else:
            await self._kick_logic(ctx, target, reason)

    # Slash command - registered dynamically in __init__.py
    async def kick_slash(self, interaction: discord.Interaction, target: Union[discord.Role, discord.Member], reason: str = "No reason"):
        if isinstance(target, discord.Role):
            await self._mass_kick_role(interaction, target, reason)
        else:
            await self._kick_logic(interaction, target, reason)

    @commands.command(name="ban")
    @is_senior_mod()
    async def ban_prefix(self, ctx: commands.Context, target: Union[discord.Role, discord.Member], *, reason: str = "No reason"):
        if isinstance(target, discord.Role):
            await self._mass_ban_role(ctx, target, reason)
        else:
            await self._ban_logic(ctx, target, reason)

    # Slash command - registered dynamically in __init__.py
    async def ban_slash(self, interaction: discord.Interaction, target: Union[discord.Role, discord.Member], reason: str = "No reason", delete_days: int = 1):
        if isinstance(target, discord.Role):
            await self._mass_ban_role(interaction, target, reason)
        else:
            await self._ban_logic(interaction, target, reason, delete_days)

    @commands.command(name="unban")
    @is_senior_mod()
    async def unban_prefix(self, ctx: commands.Context, user_id: int, *, reason: str = "No reason"):
        await self._unban_logic(ctx, user_id, reason)

    # Slash command - registered dynamically in __init__.py
    async def unban_slash(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason"):
        try:
             uid = int(user_id)
             await self._unban_logic(interaction, uid, reason)
        except ValueError:
             await interaction.response.send_message(embed=ModEmbed.error("Error", "Invalid User ID."), ephemeral=True)

    @commands.command(name="softban", description="🧹 Ban and immediately unban to delete messages")
    @is_mod()
    async def mod_softban(self, ctx: commands.Context, user: discord.Member, delete_days: Optional[int] = 1, *, reason: str = "No reason provided"):

        # Check permissions inside logic or redundant check? Original had explicit check.
        # Logic handles it.
        await self._softban_logic(ctx, user, reason)

    # Slash command - registered dynamically in __init__.py
    async def softban_slash(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):

        await self._softban_logic(interaction, user, reason)

    @commands.command(name="tempban", description="⏱️ Temporarily ban a user")
    @is_mod()
    async def mod_tempban(self, ctx: commands.Context, user: discord.Member, duration: str = "1d", *, reason: str = "No reason provided"):

        await self._tempban_logic(ctx, user, duration, reason)

    # Slash command - registered dynamically in __init__.py
    async def tempban_slash(self, interaction: discord.Interaction, user: discord.Member, duration: str = "1d", reason: str = "No reason provided"):

        await self._tempban_logic(interaction, user, duration, reason)

    @commands.command(name="mute", aliases=["timeout"], description="🔇 Timeout/mute a user")
    @is_mod()
    async def mute_prefix(self, ctx: commands.Context, user: discord.Member, duration: str = "1h", *, reason: str = "No reason provided"):

        await self._mute_logic(ctx, user, duration, reason)

    # Slash command - registered dynamically in __init__.py
    async def mute_slash(self, interaction: discord.Interaction, user: discord.Member, duration: str = "1h", reason: str = "No reason provided"):

        await self._mute_logic(interaction, user, duration, reason)
    
    @commands.command(name="unmute", aliases=["untimeout"], description="🔊 Remove timeout from a user")
    @is_mod()
    async def unmute_prefix(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
        await self._unmute_logic(ctx, user, reason)

    # Slash command - registered dynamically in __init__.py
    async def unmute_slash(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        await self._unmute_logic(interaction, user, reason)

    # Slash command - registered dynamically in __init__.py
    async def timeout_slash(self, interaction: discord.Interaction, user: discord.Member, duration: str = "1h", reason: str = "No reason provided"):

        await self._mute_logic(interaction, user, duration, reason)
        
    # Slash command - registered dynamically in __init__.py
    async def untimeout_slash(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        await self._unmute_logic(interaction, user, reason)

    @commands.command(name="massban", description="🔨 Ban multiple users at once")
    @is_admin()
    async def massban(self, ctx: commands.Context, *, args: str):
        await self._massban_logic(ctx, args, "Mass ban")

    # Slash command - registered dynamically in __init__.py
    async def massban_slash(self, interaction: discord.Interaction, user_ids: str, reason: str = "Mass ban"):
        await self._massban_logic(interaction, user_ids, reason)
        
    @commands.command(name="banlist", description="📋 View all banned users")
    @is_mod()
    async def banlist(self, ctx: commands.Context):
        await self._banlist_logic(ctx)

    # Slash command - registered dynamically in __init__.py
    async def banlist_slash(self, interaction: discord.Interaction):
        await self._banlist_logic(interaction)

    @commands.command(name="rename", description="📝 Change a user's nickname")
    @is_mod()
    async def mod_rename(self, ctx: commands.Context, user: discord.Member, *, nickname: Optional[str] = None):
        await self._rename_logic(ctx, user, nickname)

    # Slash command - registered dynamically in __init__.py
    async def rename_slash(self, interaction: discord.Interaction, user: discord.Member, nickname: Optional[str] = None):
        await self._rename_logic(interaction, user, nickname)

    @commands.command(name="setnick", description="✏️ Change a user's nickname")
    @is_mod()
    async def setnick(self, ctx: commands.Context, user: discord.Member, *, nickname: Optional[str] = None):
        await self._setnick_logic(ctx, user, nickname)

    # Slash command - registered dynamically in __init__.py
    async def setnick_slash(self, interaction: discord.Interaction, user: discord.Member, nickname: Optional[str] = None):
        await self._setnick_logic(interaction, user, nickname)
        
    @commands.command(name="nicknameall", description="✏️ Change all members' nicknames")
    @is_admin()
    async def nicknameall(self, ctx: commands.Context, *, nickname: str):
        await self._nicknameall_logic(ctx, nickname)

    # Slash command - registered dynamically in __init__.py
    async def nicknameall_slash(self, interaction: discord.Interaction, nickname: str):
        await self._nicknameall_logic(interaction, nickname)

    @commands.command(name="resetnicks", description="🔄 Reset all nicknames")
    @is_admin()
    async def resetnicks(self, ctx: commands.Context):
        await self._resetnicks_logic(ctx)

    # Slash command - registered dynamically in __init__.py
    async def resetnicks_slash(self, interaction: discord.Interaction):
        await self._resetnicks_logic(interaction)

    @commands.command(name="roleall", description="🏷️ Give a role to all members")
    @is_admin()
    async def roleall(self, ctx: commands.Context, *, role: discord.Role):
        await self._roleall_logic(ctx, role)

    # Slash command - registered dynamically in __init__.py
    async def roleall_slash(self, interaction: discord.Interaction, role: discord.Role):
        await self._roleall_logic(interaction, role)
        
    @commands.command(name="removeall", description="🗑️ Remove a role from all members")
    @is_admin()
    async def removeall(self, ctx: commands.Context, *, role: discord.Role):
        await self._removeall_logic(ctx, role)

    # Slash command - registered dynamically in __init__.py
    async def removeall_slash(self, interaction: discord.Interaction, role: discord.Role):
        await self._removeall_logic(interaction, role)

    @commands.command(name="inrole", description="👥 List members with a specific role")
    @is_mod()
    async def inrole(self, ctx: commands.Context, *, role: discord.Role):
        await self._inrole_logic(ctx, role)

    # Slash command - registered dynamically in __init__.py
    async def inrole_slash(self, interaction: discord.Interaction, role: discord.Role):
        await self._inrole_logic(interaction, role)

    @commands.command(name="quarantine", aliases=["quar", "jail"])
    @is_senior_mod()
    async def quarantine(self, ctx: commands.Context, user: discord.Member, duration: Optional[str] = None, *, reason: str = "No reason provided"):
        await self._quarantine_logic(ctx, user, duration, reason)

    # Slash command - registered dynamically in __init__.py
    async def quarantine_slash(self, interaction: discord.Interaction, user: discord.Member, duration: Optional[str] = None, reason: str = "No reason provided"):
        await self._quarantine_logic(interaction, user, duration, reason)

    @commands.command(name="unquarantine", aliases=["unquar", "unjail"])
    @is_mod()
    async def unquarantine(self, ctx: commands.Context, user: discord.Member, *, reason: str = "Quarantine lifted"):
        await self._unquarantine_logic(ctx, user, reason)

    # Slash command - registered dynamically in __init__.py
    async def unquarantine_slash(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Quarantine lifted"):
        await self._unquarantine_logic(interaction, user, reason)
        
    @commands.command(name="whitelist")
    @is_admin()
    async def whitelist(self, ctx: commands.Context, user: Union[discord.Member, discord.User]):
        await self._whitelist_logic(ctx, user)
