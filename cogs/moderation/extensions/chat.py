import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import asyncio
import io
import re
from typing import Optional, Union

from utils.embeds import ModEmbed, Colors
from utils.checks import is_mod, is_admin, is_bot_owner_id
from utils.time_parser import parse_time
from utils.transcript import EphemeralTranscriptView, generate_html_transcript
from utils.status_emojis import get_app_emoji

class ChatCommands:
    NUKE_SOURCE_URL = "https://tenor.com/view/explosion-mushroom-cloud-atomic-bomb-bomb-boom-gif-4464831"

    @staticmethod
    def _has_reason(reason: Optional[str]) -> bool:
        if not reason:
            return False
        return reason.strip().lower() != "no reason provided"

    @staticmethod
    def _build_channel_status_embed(
        *,
        emoji: str,
        title: str,
        color: int,
        moderator: Union[discord.Member, discord.User],
        reason: Optional[str] = None,
        extra_line: Optional[str] = None,
    ) -> discord.Embed:
        lines = [f"{emoji} **{title}**"]

        if ChatCommands._has_reason(reason):
            for raw in str(reason).splitlines():
                line = raw.strip()
                if line:
                    lines.append(f"> {line}")

        if extra_line:
            for raw in str(extra_line).splitlines():
                line = raw.strip()
                if line:
                    lines.append(f"> {line}")

        embed = discord.Embed(
            description="\n".join(lines),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        moderator_name = getattr(moderator, "name", str(moderator))
        moderator_icon = getattr(getattr(moderator, "display_avatar", None), "url", None)
        embed.set_footer(text=f"@{moderator_name}", icon_url=moderator_icon)
        return embed

    async def _lock_logic(self, source, channel: discord.TextChannel = None, reason: str = "No reason provided", role: discord.Role = None):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        channel = channel or (source.channel if isinstance(source, discord.Interaction) else source.channel)
        bot_member = source.guild.me
        
        try:
            # Lock for everyone
            await channel.set_permissions(
                source.guild.default_role,
                send_messages=False,
                reason=f"{author}: {reason}"
            )
            
            # Also revoke send_messages for ALL existing role overrides (except allowed role and bot)
            for target, overwrite in channel.overwrites.items():
                if target == source.guild.default_role:
                    continue
                if role and target == role:
                    continue
                if isinstance(target, discord.Role) and target == bot_member.top_role:
                    continue
                if isinstance(target, discord.Member) and target.id == bot_member.id:
                    continue
                    
                if isinstance(target, discord.Role) and overwrite.send_messages is True:
                    await channel.set_permissions(
                        target,
                        overwrite=discord.PermissionOverwrite.from_pair(
                            overwrite.pair()[0],  # Keep allow permissions
                            overwrite.pair()[1] | discord.Permissions(send_messages=True)  # Add send_messages to deny
                        ),
                        reason=f"{author} (Lock): {reason}"
                    )
            
            role_msg = None
            if role:
                await channel.set_permissions(
                    role,
                    send_messages=True,
                    reason=f"{author} (Lock Bypass): {reason}"
                )
                role_msg = f"Allowed: {role.mention}"
                
        except discord.Forbidden:
            return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to edit channel permissions."), ephemeral=True)
        
        embed = self._build_channel_status_embed(
            emoji=get_app_emoji("lock"),
            title="Channel locked",
            color=Colors.WARNING,
            moderator=author,
            reason=reason,
            extra_line=role_msg,
        )
        
        await self._respond(source, embed=embed)
        
        if channel != (source.channel if isinstance(source, discord.Interaction) else source.channel):
             lock_notice = self._build_channel_status_embed(
                emoji=get_app_emoji("lock"),
                title="Channel locked",
                color=Colors.WARNING,
                moderator=author,
                reason=reason,
                extra_line=role_msg,
             )
             await channel.send(embed=lock_notice)

    async def _unlock_logic(self, source, channel: discord.TextChannel = None, reason: str = "No reason provided"):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        channel = channel or (source.channel if isinstance(source, discord.Interaction) else source.channel)
        
        try:
            await channel.set_permissions(
                source.guild.default_role,
                send_messages=None,
                reason=f"{author}: {reason}"
            )

            bot_member = source.guild.me
            for target, overwrite in channel.overwrites.items():
                if target == source.guild.default_role:
                    continue
                if isinstance(target, discord.Role) and target == bot_member.top_role:
                    continue
                if isinstance(target, discord.Member) and target.id == bot_member.id:
                    continue

                if overwrite.send_messages is False:
                    allow, deny = overwrite.pair()
                    deny.send_messages = False
                    await channel.set_permissions(
                        target,
                        overwrite=discord.PermissionOverwrite.from_pair(allow, deny),
                        reason=f"{author} (Unlock): {reason}"
                    )
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to edit channel permissions."), ephemeral=True)
        
        embed = self._build_channel_status_embed(
            emoji=get_app_emoji("unlock"),
            title="Channel unlocked",
            color=Colors.SUCCESS,
            moderator=author,
            reason=reason,
        )
        
        await self._respond(source, embed=embed)

    async def _slowmode_logic(self, source, seconds: int, channel: discord.TextChannel = None):
        channel = channel or (source.channel if isinstance(source, discord.Interaction) else source.channel)
        try:
            await channel.edit(slowmode_delay=seconds)
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to edit this channel."), ephemeral=True)
        
        if seconds == 0:
            embed = ModEmbed.success("Slowmode Disabled", f"Slowmode has been disabled in {channel.mention}.")
        else:
            embed = ModEmbed.success("Slowmode Enabled", f"Slowmode set to **{seconds}s** in {channel.mention}.")
        
        await self._respond(source, embed=embed)

    async def _lockdown_logic(self, source, reason: str = "Server lockdown"):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if isinstance(source, discord.Interaction):
            await source.response.defer()
        
        locked = []
        failed = []
        
        for channel in source.guild.text_channels:
            try:
                await channel.set_permissions(
                    source.guild.default_role,
                    send_messages=False,
                    reason=f"[LOCKDOWN] {reason}"
                )
                locked.append(channel.mention)
            except (discord.Forbidden, discord.HTTPException):
                failed.append(channel.mention)
        
        embed = discord.Embed(
            title=f"{get_app_emoji('error')} Server Lockdown Initiated",
            description=f"Locked **{len(locked)}** channels.",
            color=Colors.DARK_RED,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=author.mention, inline=False)
        
        if failed:
            embed.add_field(
                name=f"{get_app_emoji('error')} Failed ({len(failed)})",
                value=", ".join(failed[:10]) + (f" ...and {len(failed) - 10} more" if len(failed) > 10 else ""),
                inline=False
            )
        
        await self._respond(source, embed=embed)
        await self.log_action(source.guild, embed)

    async def _unlockdown_logic(self, source, reason: str = "Lockdown lifted"):
        author = source.user if isinstance(source, discord.Interaction) else source.author
        if isinstance(source, discord.Interaction):
            await source.response.defer()
            
        unlocked = []
        failed = []
        
        for channel in source.guild.text_channels:
            try:
                await channel.set_permissions(
                    source.guild.default_role,
                    send_messages=None,
                    reason=f"[UNLOCKDOWN] {reason}"
                )
                unlocked.append(channel.mention)
            except (discord.Forbidden, discord.HTTPException):
                failed.append(channel.mention)
        
        embed = discord.Embed(
            title=f"{get_app_emoji('success')} Lockdown Lifted",
            description=f"Unlocked **{len(unlocked)}** channels.",
            color=Colors.SUCCESS,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Moderator", value=author.mention, inline=False)
        
        if failed:
            embed.add_field(
                name=f"{get_app_emoji('error')} Failed ({len(failed)})",
                value=", ".join(failed[:10]) + (f" ...and {len(failed) - 10} more" if len(failed) > 10 else ""),
                inline=False
            )
        
        await self._respond(source, embed=embed)
        await self.log_action(source.guild, embed)

    async def _nuke_logic(self, source, channel: discord.TextChannel = None, reason: str = "No reason provided"):
        channel = channel or (source.channel if isinstance(source, discord.Interaction) else source.channel)
        user = source.user if isinstance(source, discord.Interaction) else source.author

        audit_reason = f"Nuked by {user}: {reason}"

        try:
            position = channel.position
            new_channel = await channel.clone(reason=audit_reason)
            await new_channel.edit(position=position)
            await channel.delete(reason=audit_reason)
        except discord.Forbidden:
             return await self._respond(source, embed=ModEmbed.error("Failed", "I don't have permission to clone/delete channels."), ephemeral=True)
        
        embed = discord.Embed(
            title="💥 Channel Nuked",
            url=self.NUKE_SOURCE_URL,
            description=f"This channel has been nuked by {user.mention}.\n**Reason:** {reason}",
            color=Colors.ERROR
        )
        await new_channel.send(content=self.NUKE_SOURCE_URL, embed=embed)

    async def _glock_logic(self, source, channel: discord.TextChannel = None, role: discord.Role = None, reason: str = "No reason provided"):
        author_id = source.user.id if isinstance(source, discord.Interaction) else source.author.id
        guild = source.guild
        
        settings = await self.bot.db.get_settings(guild.id)
        configured_role_id = settings.get("glock_role_id") or settings.get("glock_role")
        glock_role = (
            role
            or (guild.get_role(int(configured_role_id)) if configured_role_id else None)
            or discord.utils.get(guild.roles, name="Glock")
        )
        if glock_role is None:
            return await self._respond(source,
                embed=ModEmbed.error(
                    "Missing Role",
                    "No glock role is configured for this server. Create a role named `Glock`, or pass `role:`.",
                ), ephemeral=True
            )

        if role is None and (not configured_role_id) and glock_role is not None:
            settings["glock_role_id"] = glock_role.id
            try:
                await self.bot.db.update_settings(guild.id, settings)
            except Exception:
                pass

        try:
            target = channel or (source.channel if isinstance(source, discord.Interaction) else source.channel)
            author_name = source.user if isinstance(source, discord.Interaction) else source.author
            
            await target.set_permissions(
                guild.default_role,
                send_messages=False,
                send_messages_in_threads=False,
                reason=f"[GLOCK] {author_name}: {reason}",
            )
            await target.set_permissions(
                glock_role,
                send_messages=True,
                send_messages_in_threads=True,
                reason=f"[GLOCK] {author_name}: {reason}",
            )
        except (discord.Forbidden, discord.HTTPException):
            return await self._respond(source, embed=ModEmbed.error("Failed", "I couldn't edit permissions for that channel."), ephemeral=True)

        return await self._respond(source,
            embed=ModEmbed.success(
                "Glocked",
                f"{target.mention} is now restricted to {glock_role.mention}.",
            )
        )

    async def _gunlock_logic(self, source, channel: discord.TextChannel = None, role: discord.Role = None, reason: str = "No reason provided"):
        guild = source.guild
        settings = await self.bot.db.get_settings(guild.id)
        configured_role_id = settings.get("glock_role_id") or settings.get("glock_role")
        glock_role = (
            role
            or (guild.get_role(int(configured_role_id)) if configured_role_id else None)
            or discord.utils.get(guild.roles, name="Glock")
        )
        if glock_role is None:
             return await self._respond(source,
                embed=ModEmbed.error(
                    "Missing Role",
                    "No glock role is configured for this server.",
                ), ephemeral=True
            )

        target = channel or (source.channel if isinstance(source, discord.Interaction) else source.channel)
        author_name = source.user if isinstance(source, discord.Interaction) else source.author

        try:
            everyone_overwrite = target.overwrites_for(guild.default_role)
            glock_overwrite = target.overwrites_for(glock_role)

            if not (everyone_overwrite.send_messages is False and glock_overwrite.send_messages is True):
                 return await self._respond(source, embed=ModEmbed.info("Not Glocked", f"{target.mention} doesn't look glocked."), ephemeral=True)

            await target.set_permissions(
                guild.default_role,
                send_messages=None,
                send_messages_in_threads=None,
                reason=f"[GUNLOCK] {author_name}: {reason}",
            )
            await target.set_permissions(
                glock_role,
                send_messages=None,
                send_messages_in_threads=None,
                reason=f"[GUNLOCK] {author_name}: {reason}",
            )
        except (discord.Forbidden, discord.HTTPException):
            return await self._respond(source, embed=ModEmbed.error("Failed", "I couldn't edit permissions for that channel."), ephemeral=True)

        return await self._respond(source, embed=ModEmbed.success("Gunlocked", f"{target.mention} is unlocked."))

    async def _purge_logic(self, source, amount: int, user: discord.Member = None, check=None):
        logging_cog = self.bot.get_cog("Logging")

        if isinstance(source, discord.Interaction):
            await source.response.defer(ephemeral=True)
            channel = source.channel
            interaction = source
            author = source.user
        else:
            if logging_cog and hasattr(source, "channel") and isinstance(source.channel, discord.TextChannel):
                logging_cog.suppress_message_delete_log(source.channel.id)
            try:
                await source.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            channel = source.channel
            interaction = None
            author = source.author

        if logging_cog and isinstance(channel, discord.TextChannel):
            logging_cog.suppress_message_delete_log(channel.id)
            logging_cog.suppress_bulk_delete_log(channel.id)

        settings = await self.bot.db.get_settings(source.guild.id)
        
        role_hierarchy = {
            'manager_role': 7,
            'admin_role': 6,
            'supervisor_role': 5,
            'senior_mod_role': 4,
            'mod_role': 3,
            'trial_mod_role': 2,
            'staff_role': 1
        }

        def get_sync_level(member: discord.Member) -> int:
            if is_bot_owner_id(member.id) or member.id == member.guild.owner_id:
                return 100
            if member.guild_permissions.administrator:
                return 7
            
            user_role_ids = {r.id for r in member.roles}
            current_level = 0
            for key, val in role_hierarchy.items():
                rid = settings.get(key)
                if rid and rid in user_role_ids:
                    if val > current_level:
                        current_level = val
            return current_level

        mod_level = get_sync_level(author)
        
        command_msg_id = getattr(source, "message", None)
        command_msg_id = command_msg_id.id if command_msg_id else None

        search_limit = amount + 5 if (user is None and check is None) else max(5000, amount * 5)
        match_count = [0]
        
        def combined_check(m: discord.Message):
            if m.id == command_msg_id:
                return True
            if match_count[0] >= amount:
                return False
            if user and m.author.id != user.id:
                return False
            if check and not check(m):
                return False
            match_count[0] += 1
            return True

        deleted = await channel.purge(limit=search_limit, check=combined_check)
        count = sum(1 for m in deleted if m.id != command_msg_id)
        
        # Don't show command msg in preview
        deleted_clean = [m for m in deleted if m.id != command_msg_id]

        if count > 0 and logging_cog and isinstance(channel, discord.TextChannel):
            try:
                authors = {m.author for m in deleted_clean if not m.author.bot}
                bot_count = sum(1 for m in deleted_clean if m.author.bot)
                preview_lines = []
                for msg in reversed(deleted_clean):
                    raw = (msg.content or "").strip()
                    if not raw:
                        if msg.attachments:
                            raw = f"[{len(msg.attachments)} attachment(s)]"
                        elif msg.embeds:
                            raw = "[embed]"
                        else:
                            continue
                    raw = " ".join(raw.split())
                    if len(raw) > 60:
                        raw = raw[:57].rstrip() + "..."
                    author_name = getattr(msg.author, "display_name", None) or getattr(msg.author, "name", "unknown")
                    preview_lines.append(f"`{author_name}`: {raw}")
                    if len(preview_lines) >= 3:
                        break
                preview_text = "\n".join(preview_lines) if preview_lines else "*No text content available*"

                transcript_bytes = None
                transcript_name = None
                try:
                    transcript_file = generate_html_transcript(
                        source.guild,
                        channel,
                        [],
                        purged_messages=deleted_clean,
                    )
                    transcript_bytes = transcript_file.getvalue()
                    transcript_name = f"purge-transcript-{source.guild.id}-{int(datetime.now(timezone.utc).timestamp())}.html"
                except Exception:
                    transcript_bytes = None
                    transcript_name = None

                message_log_channel = await logging_cog.get_log_channel(source.guild, "message")
                if message_log_channel:
                    log_embed = discord.Embed(
                        title="Bulk Message Delete",
                        description=(
                            f"**{count}** messages deleted in {channel.mention}\n"
                            f"**Moderator:** {author.mention} (`{author.id}`)\n"
                            f"**Breakdown:** {count - bot_count} human · {bot_count} bot · "
                            f"{len(authors)} author(s)"
                        ),
                        color=Colors.ERROR,
                    )
                    if user:
                        log_embed.description += f"\n**Filter:** {user.mention}"
                    log_embed.add_field(name="Preview", value=preview_text[:500], inline=False)

                    view = None
                    if transcript_bytes and transcript_name:
                        view = EphemeralTranscriptView(io.BytesIO(transcript_bytes), filename=transcript_name)
                    await logging_cog.safe_send_log(message_log_channel, log_embed, view=view)

                # Purges are moderation actions, so also emit a dedicated mod-log card.
                mod_log_embed = discord.Embed(
                    title="Moderator Purge",
                    description=(
                        f"{author.mention} purged **{count}** message(s) in {channel.mention}.\n"
                        f"**Breakdown:** {count - bot_count} human · {bot_count} bot · "
                        f"{len(authors)} author(s)"
                    ),
                    color=Colors.ERROR,
                )
                if user:
                    mod_log_embed.description += f"\n**Filter:** {user.mention}"

                mod_view = None
                if transcript_bytes and transcript_name:
                    mod_view = EphemeralTranscriptView(io.BytesIO(transcript_bytes), filename=transcript_name)
                await self.log_action(source.guild, mod_log_embed, log_type="mod", view=mod_view)
            except Exception:
                pass
        
        embed = ModEmbed.success("Purged", f"Deleted **{count}** messages.")
        if user:
            embed.description += f"\nFiltered by: {user.mention}"
            
        if interaction:
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await source.send(embed=embed, delete_after=5)

    # Commands
    @commands.command(name="lock", description="🔒 Lock a channel")
    @is_mod()
    async def lock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *, reason: str = "No reason provided"):
        target = channel or ctx.channel
        await self._lock_logic(ctx, target, reason)

    # Slash command - registered dynamically in __init__.py
    async def lock_slash(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, reason: str = "No reason provided"):
        target = channel or interaction.channel
        await self._lock_logic(interaction, target, reason)

    @commands.command(name="unlock", description="🔓 Unlock a channel")
    @is_mod()
    async def unlock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *, reason: str = "No reason provided"):
        target = channel or ctx.channel
        await self._unlock_logic(ctx, target, reason)

    # Slash command - registered dynamically in __init__.py
    async def unlock_slash(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, reason: str = "No reason provided"):
        target = channel or interaction.channel
        await self._unlock_logic(interaction, target, reason)

    @commands.command(name="slowmode", description="🐌 Set channel slowmode")
    @is_mod()
    async def slowmode(self, ctx: commands.Context, duration: str = "0", channel: Optional[discord.TextChannel] = None):
        target = channel or ctx.channel
        seconds = 0
        parsed = parse_time(duration)
        if parsed:
            seconds = int(parsed[0].total_seconds())
        elif duration.isdigit():
             seconds = int(duration)
        await self._slowmode_logic(ctx, seconds, target)

    # Slash command - registered dynamically in __init__.py
    async def slowmode_slash(self, interaction: discord.Interaction, duration: str, channel: Optional[discord.TextChannel] = None):
        target = channel or interaction.channel
        seconds = 0
        parsed = parse_time(duration)
        if parsed:
            seconds = int(parsed[0].total_seconds())
        elif duration.isdigit():
             seconds = int(duration)
        await self._slowmode_logic(interaction, seconds, target)

    @commands.command(name="glock", description="🔒 Only the Glock role can talk in the channel")
    @is_mod()
    async def glock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, role: Optional[discord.Role] = None, *, reason: str = "No reason provided"):
        await self._glock_logic(ctx, channel, role, reason)

    # Slash command - registered dynamically in __init__.py
    async def glock_slash(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, role: Optional[discord.Role] = None, reason: str = "No reason provided"):
        await self._glock_logic(interaction, channel, role, reason)

    @commands.command(name="gunlock", description="🔓 Remove Glock-role-only channel restriction")
    @is_mod()
    async def gunlock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, role: Optional[discord.Role] = None, *, reason: str = "No reason provided"):
        await self._gunlock_logic(ctx, channel, role, reason)

    # Slash command - registered dynamically in __init__.py
    async def gunlock_slash(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, role: Optional[discord.Role] = None, reason: str = "No reason provided"):
        await self._gunlock_logic(interaction, channel, role, reason)

    @commands.command(name="lockdown")
    @is_admin()
    async def lockdown(self, ctx: commands.Context, *, reason: str = "Server lockdown"):
        await self._lockdown_logic(ctx, reason)

    # Slash command - registered dynamically in __init__.py
    async def lockdown_slash(self, interaction: discord.Interaction, reason: str = "Server lockdown"):
        await self._lockdown_logic(interaction, reason)

    @commands.command(name="unlockdown")
    @is_admin()
    async def unlockdown(self, ctx: commands.Context, *, reason: str = "Lockdown lifted"):
        await self._unlockdown_logic(ctx, reason)

    # Slash command - registered dynamically in __init__.py
    async def unlockdown_slash(self, interaction: discord.Interaction, reason: str = "Lockdown lifted"):
        await self._unlockdown_logic(interaction, reason)

    @commands.command(name="nuke")
    @is_admin()
    async def nuke(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *, reason: str = "No reason provided"):
        await self._nuke_logic(ctx, channel, reason)

    # Slash command - registered dynamically in __init__.py
    async def nuke_slash(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, reason: str = "No reason provided"):
        await self._nuke_logic(interaction, channel, reason)

    @commands.command(name="purge", aliases=["clear"])
    @is_mod()
    async def purge(self, ctx: commands.Context, amount: int, user: Optional[discord.Member] = None):
        if amount < 1 or amount > 1000:
             return await ctx.send("Amount must be between 1 and 1000.")
        await self._purge_logic(ctx, amount, user)

    # Slash command - registered dynamically in __init__.py
    async def purge_slash(self, interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
        if amount < 1 or amount > 1000:
             return await interaction.response.send_message("Amount must be between 1 and 1000.", ephemeral=True)
        await self._purge_logic(interaction, amount, user)

    @commands.command(name="purgebots")
    @is_mod()
    async def purgebots(self, ctx: commands.Context, amount: int = 1000):
        await self._purge_logic(ctx, amount, check=lambda m: m.author.bot)

    # Slash command - registered dynamically in __init__.py
    async def purgebots_slash(self, interaction: discord.Interaction, amount: int = 1000):
        await self._purge_logic(interaction, amount, check=lambda m: m.author.bot)

    @commands.command(name="purgecontains")
    @is_mod()
    async def purgecontains(self, ctx: commands.Context, text: str, amount: int = 1000):
        await self._purge_logic(ctx, amount, check=lambda m: text.lower() in m.content.lower())

    # Slash command - registered dynamically in __init__.py
    async def purgecontains_slash(self, interaction: discord.Interaction, text: str, amount: int = 1000):
        await self._purge_logic(interaction, amount, check=lambda m: text.lower() in m.content.lower())

    @commands.command(name="purgeembeds")
    @is_mod()
    async def purgeembeds(self, ctx: commands.Context, amount: int = 1000):
        await self._purge_logic(ctx, amount, check=lambda m: len(m.embeds) > 0)

    # Slash command - registered dynamically in __init__.py
    async def purgeembeds_slash(self, interaction: discord.Interaction, amount: int = 1000):
        await self._purge_logic(interaction, amount, check=lambda m: len(m.embeds) > 0)

    @commands.command(name="purgeimages")
    @is_mod()
    async def purgeimages(self, ctx: commands.Context, amount: int = 1000):
        await self._purge_logic(ctx, amount, check=lambda m: len(m.attachments) > 0)

    # Slash command - registered dynamically in __init__.py
    async def purgeimages_slash(self, interaction: discord.Interaction, amount: int = 1000):
        await self._purge_logic(interaction, amount, check=lambda m: len(m.attachments) > 0)

    @commands.command(name="purgelinks")
    @is_mod()
    async def purgelinks(self, ctx: commands.Context, amount: int = 1000):
        url_pattern = re.compile(r'https?://')
        await self._purge_logic(ctx, amount, check=lambda m: url_pattern.search(m.content))

    # Slash command - registered dynamically in __init__.py
    async def purgelinks_slash(self, interaction: discord.Interaction, amount: int = 1000):
        url_pattern = re.compile(r'https?://')
        await self._purge_logic(interaction, amount, check=lambda m: url_pattern.search(m.content))
