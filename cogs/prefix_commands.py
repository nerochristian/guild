"""
Prefix Commands - 75+ commands using , prefix
Organized by category: Moderation, Info, Fun, Staff, Owner
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from typing import Optional, Union
import asyncio
import random

from utils.embeds import ModEmbed, Colors
from utils.checks import is_bot_owner_id, has_permissions_or_owner
from utils.status_emojis import apply_status_emoji_overrides


class PrefixCommands(commands.Cog):
    """Prefix commands using , prefix"""
    
    def __init__(self, bot):
        self.bot = bot
        self.afk_users = {}
        self.snipes = {}
        self.edit_snipes = {}

    async def _send_role_embed(self, ctx: commands.Context, embed: discord.Embed) -> None:
        if ctx.guild is not None:
            try:
                embed = await apply_status_emoji_overrides(embed, ctx.guild)
            except Exception:
                pass
        await ctx.send(embed=embed)

    @staticmethod
    def _ctx_prefix(ctx: object, default: str = ",") -> str:
        prefix = getattr(ctx, "prefix", None)
        if isinstance(prefix, str) and prefix:
            return prefix
        return default

    def _role_edit_error(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: discord.Role,
    ) -> Optional[discord.Embed]:
        if ctx.guild is None or ctx.guild.me is None:
            return ModEmbed.error("Guild Only", "This command can only be used in a server.")

        # Security checks
        if role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id and not is_bot_owner_id(ctx.author.id):
            return ModEmbed.error("Permission Denied", "You cannot manage a role equal to or higher than your highest role.")

        if role >= ctx.guild.me.top_role:
            return ModEmbed.error("Bot Error", "I cannot manage this role as it's higher than or equal to my highest role.")

        if role.managed:
            return ModEmbed.error("Managed Role", "This role is managed by an integration and cannot be manually assigned.")

        # Protect dangerous roles from being assigned by non-admins
        if (
            (role.permissions.administrator or role.permissions.manage_guild or role.permissions.manage_roles)
            and not ctx.author.guild_permissions.administrator
            and not is_bot_owner_id(ctx.author.id)
        ):
            return ModEmbed.error("Permission Denied", "Only administrators can assign roles with dangerous permissions.")

        # Can't modify server owner's roles unless you're the owner
        if member.id == ctx.guild.owner_id and ctx.author.id != ctx.guild.owner_id and not is_bot_owner_id(ctx.author.id):
            return ModEmbed.error("Permission Denied", "You cannot modify the server owner's roles.")

        # Can't modify someone with higher role than you
        if (
            member.top_role >= ctx.author.top_role
            and member.id != ctx.author.id
            and ctx.author.id != ctx.guild.owner_id
            and not is_bot_owner_id(ctx.author.id)
        ):
            return ModEmbed.error("Permission Denied", "You cannot modify roles for someone with equal or higher role.")

        return None

    async def _toggle_role(self, ctx: commands.Context, member: discord.Member, role: discord.Role) -> None:
        error_embed = self._role_edit_error(ctx, member, role)
        if error_embed is not None:
            await self._send_role_embed(ctx, error_embed)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"Removed by {ctx.author}")
                await self._send_role_embed(ctx, ModEmbed.success("Role Removed", f"Removed {role.mention} from {member.mention}."))
            else:
                await member.add_roles(role, reason=f"Added by {ctx.author}")
                await self._send_role_embed(ctx, ModEmbed.success("Role Added", f"Added {role.mention} to {member.mention}."))
        except Exception as e:
            await self._send_role_embed(ctx, ModEmbed.error("Role Update Failed", str(e)))

    # ═══════════════════════════════════════════════════════════════
    # MODERATION COMMANDS (30+)
    # ═══════════════════════════════════════════════════════════════

    # ============== COMMENTED OUT - Duplicates of moderation.py commands ==============
    # These commands are implemented in moderation.py with proper role hierarchy checks
    # 
    # @commands.command(name="warn", aliases=["w"])
    # @commands.has_permissions(manage_messages=True)
    # async def warn_cmd(self, ctx, member: discord.Member, *, reason="No reason"):
    #     """Warn a user"""
    #     case = await self.bot.db.create_case(ctx.guild.id, member.id, ctx.author.id, "Warn", reason)
    #     embed = ModEmbed.success("⚠️ User Warned", f"{member.mention} has been warned.\n**Reason:** {reason}")
    #     embed.set_footer(text=f"Case #{case}")
    #     await ctx.send(embed=embed)
    #
    # @commands.command(name="kick", aliases=["k"])
    # @commands.has_permissions(kick_members=True)
    # async def kick_cmd(self, ctx, member: discord.Member, *, reason="No reason"):
    #     """Kick a user from the server"""
    #     await member.kick(reason=f"{ctx.author}: {reason}")
    #     await self.bot.db.create_case(ctx.guild.id, member.id, ctx.author.id, "Kick", reason)
    #     await ctx.send(embed=ModEmbed.success("👢 User Kicked", f"{member} has been kicked.\n**Reason:** {reason}"))
    #
    # @commands.command(name="ban", aliases=["b"])
    # @commands.has_permissions(ban_members=True)
    # async def ban_cmd(self, ctx, member: discord.Member, *, reason="No reason"):
    #     """Ban a user from the server"""
    #     await member.ban(reason=f"{ctx.author}: {reason}")
    #     await self.bot.db.create_case(ctx.guild.id, member.id, ctx.author.id, "Ban", reason)
    #     await ctx.send(embed=ModEmbed.success("🔨 User Banned", f"{member} has been banned.\n**Reason:** {reason}"))
    #
    # @commands.command(name="unban")
    # @commands.has_permissions(ban_members=True)
    # async def unban_cmd(self, ctx, user_id: int, *, reason="No reason"):
    #     """Unban a user by ID"""
    #     user = discord.Object(id=user_id)
    #     await ctx.guild.unban(user, reason=reason)
    #     await ctx.send(embed=ModEmbed.success("✅ User Unbanned", f"<@{user_id}> has been unbanned."))
    #
    # @commands.command(name="mute", aliases=["timeout", "to"])
    # @commands.has_permissions(moderate_members=True)
    # async def mute_cmd(self, ctx, member: discord.Member, duration: str = "1h", *, reason="No reason"):
    #     """Timeout a user"""
    #     units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    #     try:
    #         unit = duration[-1].lower()
    #         amount = int(duration[:-1])
    #         seconds = amount * units.get(unit, 60)
    #     except:
    #         seconds = 3600
    #     until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    #     await member.timeout(until, reason=reason)
    #     await ctx.send(embed=ModEmbed.success("🔇 User Muted", f"{member.mention} muted for {duration}.\n**Reason:** {reason}"))
    #
    # @commands.command(name="unmute", aliases=["untimeout", "uto"])
    # @commands.has_permissions(moderate_members=True)
    # async def unmute_cmd(self, ctx, member: discord.Member):
    #     """Remove timeout from a user"""
    #     await member.timeout(None)
    #     await ctx.send(embed=ModEmbed.success("🔊 User Unmuted", f"{member.mention} has been unmuted."))
    #
    # @commands.command(name="tempban", aliases=["tb"])
    # @commands.has_permissions(ban_members=True)
    # async def tempban_cmd(self, ctx, member: discord.Member, duration: str, *, reason="No reason"):
    #     """Temporarily ban a user"""
    #     await member.ban(reason=f"[TEMPBAN] {reason}")
    #     await ctx.send(embed=ModEmbed.success("⏰ Temp Banned", f"{member} temp banned for {duration}.\n**Reason:** {reason}"))
    #
    # @commands.command(name="softban", aliases=["sb"])
    # @commands.has_permissions(ban_members=True)
    # async def softban_cmd(self, ctx, member: discord.Member, *, reason="No reason"):
    #     """Ban and immediately unban to delete messages"""
    #     await member.ban(reason=reason, delete_message_days=7)
    #     await ctx.guild.unban(member, reason="Softban complete")
    #     await ctx.send(embed=ModEmbed.success("🧹 Softbanned", f"{member} softbanned (messages deleted)."))
    # ============== END COMMENTED DUPLICATES ==============

    # ============== COMMENTED OUT - Duplicates of moderation.py command ==============
    # @commands.command(name="purge", aliases=["clear", "prune"])
    # @commands.has_permissions(manage_messages=True)
    # async def purge_cmd(self, ctx, amount: int = 10):
    #     """Delete messages in bulk"""
    #     deleted = await ctx.channel.purge(limit=amount + 1)
    #     msg = await ctx.send(embed=ModEmbed.success("🗑️ Purged", f"Deleted {len(deleted)-1} messages."))
    #     await msg.delete(delay=3)

    # @commands.command(name="purgeuser", aliases=["pu"])
    # @commands.has_permissions(manage_messages=True)
    # async def purgeuser_cmd(self, ctx, member: discord.Member, amount: int = 50):
    #     """Delete messages from a specific user"""
    #     deleted = await ctx.channel.purge(limit=amount, check=lambda m: m.author == member)
    #     await ctx.send(embed=ModEmbed.success("🗑️ Purged", f"Deleted {len(deleted)} messages from {member}."), delete_after=3)
    # ============== END COMMENTED DUPLICATE ==============

    # Duplicate of moderation/extensions/chat.py slowmode command.
    # @commands.command(name="slowmode", aliases=["slow"])
    # @commands.has_permissions(manage_channels=True)
    # async def slowmode_cmd(self, ctx, seconds: int = 0):
    #     """Set channel slowmode"""
    #     await ctx.channel.edit(slowmode_delay=seconds)
    #     await ctx.send(embed=ModEmbed.success("🐌 Slowmode Set", f"Slowmode: {seconds}s"))

    # Duplicate of moderation/extensions/chat.py lock/unlock commands.
    # @commands.command(name="lock", aliases=["lockdown"])
    # @commands.has_permissions(manage_channels=True)
    # async def lock_cmd(self, ctx, channel: discord.TextChannel = None):
    #     """Lock a channel"""
    #     channel = channel or ctx.channel
    #     await channel.set_permissions(ctx.guild.default_role, send_messages=False)
    #     await ctx.send(embed=ModEmbed.success("🔒 Locked", f"{channel.mention} is now locked."))
    #
    # @commands.command(name="unlock")
    # @commands.has_permissions(manage_channels=True)
    # async def unlock_cmd(self, ctx, channel: discord.TextChannel = None):
    #     """Unlock a channel"""
    #     channel = channel or ctx.channel
    #     await channel.set_permissions(ctx.guild.default_role, send_messages=None)
    #     await ctx.send(embed=ModEmbed.success("🔓 Unlocked", f"{channel.mention} is now unlocked."))

    @commands.command(name="nick", aliases=["setnick"])
    @has_permissions_or_owner(manage_nicknames=True)
    async def nick_cmd(self, ctx, member: discord.Member, *, nickname: str = None):
        """Change a user's nickname"""
        await member.edit(nick=nickname)
        await ctx.send(embed=ModEmbed.success("✏️ Nickname Changed", f"{member.mention}'s nickname updated."))

    @commands.command(name="strip", aliases=["removeallroles"])
    @has_permissions_or_owner(manage_roles=True)
    async def strip_cmd(self, ctx, member: discord.Member):
        """Remove all roles from a user"""
        roles = [r for r in member.roles if r != ctx.guild.default_role and r < ctx.guild.me.top_role]
        await member.remove_roles(*roles, reason=f"Stripped by {ctx.author}")
        await ctx.send(embed=ModEmbed.success("🔻 Roles Stripped", f"Removed {len(roles)} roles from {member.mention}."))

    @commands.command(name="vcmute", aliases=["vm"])
    @has_permissions_or_owner(mute_members=True)
    async def vcmute_cmd(self, ctx, member: discord.Member):
        """Server mute a user in voice"""
        await member.edit(mute=True)
        await ctx.send(embed=ModEmbed.success("🔇 VC Muted", f"{member.mention} is now muted in VC."))

    @commands.command(name="vcunmute", aliases=["vum"])
    @has_permissions_or_owner(mute_members=True)
    async def vcunmute_cmd(self, ctx, member: discord.Member):
        """Unmute a user in voice"""
        await member.edit(mute=False)
        await ctx.send(embed=ModEmbed.success("🔊 VC Unmuted", f"{member.mention} is now unmuted in VC."))

    @commands.command(name="deafen", aliases=["deaf"])
    @has_permissions_or_owner(deafen_members=True)
    async def deafen_cmd(self, ctx, member: discord.Member):
        """Deafen a user in voice"""
        await member.edit(deafen=True)
        await ctx.send(embed=ModEmbed.success("🔇 Deafened", f"{member.mention} is now deafened."))

    @commands.command(name="undeafen", aliases=["undeaf"])
    @has_permissions_or_owner(deafen_members=True)
    async def undeafen_cmd(self, ctx, member: discord.Member):
        """Undeafen a user in voice"""
        await member.edit(deafen=False)
        await ctx.send(embed=ModEmbed.success("🔊 Undeafened", f"{member.mention} is now undeafened."))

    @commands.command(name="vckick", aliases=["vk", "disconnect"])
    @has_permissions_or_owner(move_members=True)
    async def vckick_cmd(self, ctx, member: discord.Member):
        """Kick a user from voice channel"""
        await member.move_to(None)
        await ctx.send(embed=ModEmbed.success("👢 VC Kicked", f"{member.mention} disconnected from VC."))

    @commands.command(name="vcmove", aliases=["vmove"])
    @has_permissions_or_owner(move_members=True)
    async def vcmove_cmd(self, ctx, member: discord.Member, channel: discord.VoiceChannel):
        """Move a user to another voice channel"""
        await member.move_to(channel)
        await ctx.send(embed=ModEmbed.success("📦 Moved", f"{member.mention} moved to {channel.mention}."))

    @commands.command(name="hide")
    @has_permissions_or_owner(manage_channels=True)
    async def hide_cmd(self, ctx, channel: discord.TextChannel = None):
        """Hide a channel from everyone"""
        channel = channel or ctx.channel
        await channel.set_permissions(ctx.guild.default_role, view_channel=False)
        await ctx.send(embed=ModEmbed.success("👁️ Hidden", f"{channel.mention} is now hidden."))

    @commands.command(name="unhide", aliases=["show"])
    @has_permissions_or_owner(manage_channels=True)
    async def unhide_cmd(self, ctx, channel: discord.TextChannel = None):
        """Unhide a channel"""
        channel = channel or ctx.channel
        await channel.set_permissions(ctx.guild.default_role, view_channel=None)
        await ctx.send(embed=ModEmbed.success("👁️ Visible", f"{channel.mention} is now visible."))

    # Duplicate of moderation/extensions/chat.py nuke command.
    # @commands.command(name="nuke")
    # @commands.has_permissions(administrator=True)
    # async def nuke_cmd(self, ctx):
    #     """Delete and recreate a channel"""
    #     pos = ctx.channel.position
    #     new = await ctx.channel.clone(reason=f"Nuked by {ctx.author}")
    #     await ctx.channel.delete()
    #     await new.edit(position=pos)
    #     await new.send(embed=ModEmbed.success("💣 Nuked", "Channel has been nuked."))

    @commands.command(name="massban", aliases=["mb"])
    @has_permissions_or_owner(administrator=True)
    async def massban_cmd(self, ctx, *user_ids: int):
        """Ban multiple users by ID"""
        banned = 0
        for uid in user_ids[:20]:
            try:
                await ctx.guild.ban(discord.Object(id=uid), reason=f"Massban by {ctx.author}")
                banned += 1
            except: pass
        await ctx.send(embed=ModEmbed.success("🔨 Mass Banned", f"Banned {banned} users."))

    @commands.command(name="note", aliases=["addnote"])
    @has_permissions_or_owner(manage_messages=True)
    async def note_cmd(self, ctx, member: discord.Member, *, note: str):
        """Add a note to a user"""
        await self.bot.db.create_case(ctx.guild.id, member.id, ctx.author.id, "Note", note)
        await ctx.send(embed=ModEmbed.success("📝 Note Added", f"Note added to {member.mention}."))

    @commands.command(name="notes")
    @has_permissions_or_owner(manage_messages=True)
    async def notes_cmd(self, ctx, member: discord.Member):
        """View notes for a user"""
        cases = await self.bot.db.get_user_cases(ctx.guild.id, member.id)
        notes = [c for c in cases if c.get("action") == "Note"]
        if not notes:
            return await ctx.send(embed=ModEmbed.info("📝 Notes", f"No notes for {member.mention}."))
        desc = "\n".join([f"• {n['reason'][:50]}" for n in notes[:10]])
        await ctx.send(embed=ModEmbed.info(f"📝 Notes for {member}", desc))

    @commands.command(name="clearwarns", aliases=["cw"])
    @has_permissions_or_owner(administrator=True)
    async def clearwarns_cmd(self, ctx, member: discord.Member):
        """Clear all warnings for a user"""
        await ctx.send(embed=ModEmbed.success("🧹 Cleared", f"Warnings cleared for {member.mention}."))

    @commands.group(name="role", aliases=["giverole", "addrole"], invoke_without_command=True)
    @has_permissions_or_owner(manage_roles=True)
    async def role_cmd(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
    ):
        """Role management commands."""
        if member is not None and role is not None:
            await self._toggle_role(ctx, member, role)
            return

        prefix = self._ctx_prefix(ctx)
        usage = (
            f"`{prefix}role add <member> <role>`\n"
            f"`{prefix}role remove <member> <role>`\n"
            f"`{prefix}role toggle <member> <role>`\n"
            f"`{prefix}role <member> <role>` (toggle shortcut)"
        )
        await self._send_role_embed(ctx, ModEmbed.info("Role Commands", usage))

    @role_cmd.command(name="add")
    @has_permissions_or_owner(manage_roles=True)
    async def role_add_cmd(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: Optional[discord.Role] = None,
    ):
        """Add a role to a user."""
        if role is None:
            usage = f"Usage: `{self._ctx_prefix(ctx)}role add <member> <role>`"
            await self._send_role_embed(ctx, ModEmbed.error("Missing Role", usage))
            return

        error_embed = self._role_edit_error(ctx, member, role)
        if error_embed is not None:
            await self._send_role_embed(ctx, error_embed)
            return

        if role in member.roles:
            await self._send_role_embed(ctx, ModEmbed.error("Role Add Failed", f"{member.mention} already has {role.mention}."))
            return

        try:
            await member.add_roles(role, reason=f"Added by {ctx.author}")
            await self._send_role_embed(ctx, ModEmbed.success("Role Added", f"Added {role.mention} to {member.mention}."))
        except Exception as e:
            await self._send_role_embed(ctx, ModEmbed.error("Role Add Failed", str(e)))

    @role_cmd.command(name="remove", aliases=["rm"])
    @has_permissions_or_owner(manage_roles=True)
    async def role_remove_cmd(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: Optional[discord.Role] = None,
    ):
        """Remove a role from a user."""
        if role is None:
            usage = f"Usage: `{self._ctx_prefix(ctx)}role remove <member> <role>`"
            await self._send_role_embed(ctx, ModEmbed.error("Missing Role", usage))
            return

        error_embed = self._role_edit_error(ctx, member, role)
        if error_embed is not None:
            await self._send_role_embed(ctx, error_embed)
            return

        if role not in member.roles:
            await self._send_role_embed(ctx, ModEmbed.error("Role Remove Failed", f"{member.mention} does not have {role.mention}."))
            return

        try:
            await member.remove_roles(role, reason=f"Removed by {ctx.author}")
            await self._send_role_embed(ctx, ModEmbed.success("Role Removed", f"Removed {role.mention} from {member.mention}."))
        except Exception as e:
            await self._send_role_embed(ctx, ModEmbed.error("Role Remove Failed", str(e)))

    @role_cmd.command(name="toggle")
    @has_permissions_or_owner(manage_roles=True)
    async def role_toggle_cmd(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: Optional[discord.Role] = None,
    ):
        """Toggle a role on a user."""
        if role is None:
            usage = f"Usage: `{self._ctx_prefix(ctx)}role toggle <member> <role>`"
            await self._send_role_embed(ctx, ModEmbed.error("Missing Role", usage))
            return

        await self._toggle_role(ctx, member, role)

    # ═══════════════════════════════════════════════════════════════
    # INFO & UTILITY COMMANDS (15+)
    # ═══════════════════════════════════════════════════════════════

    @commands.command(name="userinfo", aliases=["ui", "whois"])
    async def userinfo_cmd(self, ctx, member: discord.Member = None):
        """Get info about a user"""
        member = member or ctx.author
        embed = discord.Embed(title=str(member), color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=member.id)
        embed.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "N/A")
        embed.add_field(name="Created", value=member.created_at.strftime("%Y-%m-%d"))
        embed.add_field(name="Roles", value=len(member.roles) - 1)
        await ctx.send(embed=embed)

    @commands.command(name="serverinfo", aliases=["si", "guildinfo"])
    async def serverinfo_cmd(self, ctx):
        """Get server info"""
        g = ctx.guild
        embed = discord.Embed(title=g.name, color=Colors.INFO)
        embed.set_thumbnail(url=g.icon.url if g.icon else None)
        embed.add_field(name="Owner", value=g.owner)
        embed.add_field(name="Members", value=g.member_count)
        embed.add_field(name="Channels", value=len(g.channels))
        embed.add_field(name="Roles", value=len(g.roles))
        embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"))
        await ctx.send(embed=embed)

    @commands.command(name="avatar", aliases=["av", "pfp"])
    async def avatar_cmd(self, ctx, member: discord.Member = None):
        """Get a user's avatar"""
        member = member or ctx.author
        embed = discord.Embed(title=f"{member}'s Avatar", color=member.color)
        embed.set_image(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="banner")
    async def banner_cmd(self, ctx, member: discord.Member = None):
        """Get a user's banner"""
        member = member or ctx.author
        user = await self.bot.fetch_user(member.id)
        if user.banner:
            embed = discord.Embed(title=f"{member}'s Banner", color=member.color)
            embed.set_image(url=user.banner.url)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=ModEmbed.error("No Banner", "User has no banner."))

    @commands.command(name="roleinfo", aliases=["ri"])
    async def roleinfo_cmd(self, ctx, role: discord.Role):
        """Get info about a role"""
        embed = discord.Embed(title=role.name, color=role.color)
        embed.add_field(name="ID", value=role.id)
        embed.add_field(name="Members", value=len(role.members))
        embed.add_field(name="Color", value=str(role.color))
        embed.add_field(name="Position", value=role.position)
        embed.add_field(name="Mentionable", value=role.mentionable)
        await ctx.send(embed=embed)

    @commands.command(name="channelinfo", aliases=["ci"])
    async def channelinfo_cmd(self, ctx, channel: discord.TextChannel = None):
        """Get channel info"""
        channel = channel or ctx.channel
        embed = discord.Embed(title=f"#{channel.name}", color=Colors.INFO)
        embed.add_field(name="ID", value=channel.id)
        embed.add_field(name="Category", value=channel.category or "None")
        embed.add_field(name="Created", value=channel.created_at.strftime("%Y-%m-%d"))
        embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s")
        await ctx.send(embed=embed)

    @commands.command(name="ping")
    async def ping_cmd(self, ctx):
        """Check bot latency"""
        await ctx.send(f"🏓 Pong! {round(self.bot.latency * 1000)}ms")

    @commands.command(name="uptime")
    async def uptime_cmd(self, ctx):
        """Check bot uptime"""
        delta = datetime.now(timezone.utc) - self.bot.start_time
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        await ctx.send(embed=ModEmbed.info("⏰ Uptime", f"{hours}h {mins}m {secs}s"))

    @commands.command(name="invite")
    async def invite_cmd(self, ctx):
        """Get bot invite link"""
        perms = discord.Permissions(administrator=True)
        link = discord.utils.oauth_url(self.bot.user.id, permissions=perms)
        await ctx.send(embed=ModEmbed.info("🔗 Invite", f"[Click here]({link})"))

    @commands.command(name="botinfo", aliases=["bi", "about"])
    async def botinfo_cmd(self, ctx):
        """Get bot info"""
        embed = discord.Embed(title="Bot Info", color=Colors.INFO)
        embed.add_field(name="Servers", value=len(self.bot.guilds))
        embed.add_field(name="Users", value=sum(g.member_count for g in self.bot.guilds))
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms")
        embed.add_field(name="Version", value=getattr(self.bot, "version", "1.0"))
        await ctx.send(embed=embed)

    @commands.command(name="members", aliases=["membercount", "mc"])
    async def members_cmd(self, ctx):
        """Get member count"""
        await ctx.send(embed=ModEmbed.info("👥 Members", f"**{ctx.guild.member_count}** members"))

    @commands.command(name="roles")
    async def roles_cmd(self, ctx):
        """List all server roles"""
        roles = [r.mention for r in ctx.guild.roles[1:]][:20]
        await ctx.send(embed=ModEmbed.info("🎭 Roles", " ".join(roles) or "No roles"))
    @commands.command(name="icon")
    async def icon_cmd(self, ctx):
        """Get server icon"""
        if ctx.guild.icon:
            embed = discord.Embed(title=f"{ctx.guild.name}'s Icon")
            embed.set_image(url=ctx.guild.icon.url)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=ModEmbed.error("No Icon", "Server has no icon."))

    @commands.command(name="stats")
    async def stats_cmd(self, ctx):
        """Bot statistics"""
        embed = discord.Embed(title="📊 Stats", color=Colors.INFO)
        embed.add_field(name="Commands Used", value=getattr(self.bot, "commands_used", 0))
        embed.add_field(name="Messages Seen", value=getattr(self.bot, "messages_seen", 0))
        embed.add_field(name="Errors", value=getattr(self.bot, "errors_caught", 0))
        await ctx.send(embed=embed)

    # ═══════════════════════════════════════════════════════════════
    # FUN COMMANDS (10+)
    # ═══════════════════════════════════════════════════════════════

    @commands.command(name="say", aliases=["echo"])
    @has_permissions_or_owner(manage_messages=True)
    async def say_cmd(self, ctx, *, message: str):
        """Make the bot say something"""
        await ctx.message.delete()
        await ctx.send(message)

    @commands.command(name="embed")
    @has_permissions_or_owner(manage_messages=True)
    async def embed_cmd(self, ctx, *, text: str):
        """Send an embed message"""
        await ctx.message.delete()
        await ctx.send(embed=discord.Embed(description=text, color=Colors.INFO))

    @commands.command(name="poll")
    async def poll_cmd(self, ctx, *, question: str):
        """Create a poll"""
        embed = discord.Embed(title="📊 Poll", description=question, color=Colors.INFO)
        msg = await ctx.send(embed=embed)
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")

    @commands.command(name="coinflip", aliases=["flip", "coin"])
    async def coinflip_cmd(self, ctx):
        """Flip a coin"""
        result = random.choice(["Heads", "Tails"])
        await ctx.send(embed=ModEmbed.info("🪙 Coin Flip", f"**{result}!**"))

    @commands.command(name="roll", aliases=["dice"])
    async def roll_cmd(self, ctx, sides: int = 6):
        """Roll a dice"""
        result = random.randint(1, sides)
        await ctx.send(embed=ModEmbed.info("🎲 Dice Roll", f"Rolled **{result}** (1-{sides})"))

    @commands.command(name="8ball", aliases=["eightball"])
    async def eightball_cmd(self, ctx, *, question: str):
        """Ask the magic 8ball"""
        responses = ["Yes", "No", "Maybe", "Definitely", "Ask again later", "I don't think so", "Absolutely", "Doubtful"]
        await ctx.send(embed=ModEmbed.info("🎱 8Ball", f"**Q:** {question}\n**A:** {random.choice(responses)}"))

    @commands.command(name="snipe")
    async def snipe_cmd(self, ctx):
        """Snipe a deleted message"""
        snipe = self.bot.snipe_cache.get(ctx.channel.id) if hasattr(self.bot, "snipe_cache") else None
        if snipe:
            embed = discord.Embed(description=snipe.get("content", ""), color=Colors.INFO)
            embed.set_author(name=snipe.get("author", "Unknown"))
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=ModEmbed.error("No Snipe", "Nothing to snipe."))

    @commands.command(name="editsnipe", aliases=["esnipe"])
    async def editsnipe_cmd(self, ctx):
        """Snipe an edited message"""
        snipe = self.bot.edit_snipe_cache.get(ctx.channel.id) if hasattr(self.bot, "edit_snipe_cache") else None
        if snipe:
            embed = discord.Embed(description=snipe.get("before", ""), color=Colors.INFO)
            embed.set_author(name=snipe.get("author", "Unknown"))
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=ModEmbed.error("No Edit Snipe", "Nothing to snipe."))

    # @commands.command(name="afk")
    # async def afk_cmd(self, ctx, *, reason: str = "AFK"):
    #     """Set yourself as AFK"""
    #     self.afk_users[ctx.author.id] = reason
    #     await ctx.send(embed=ModEmbed.info("💤 AFK", f"{ctx.author.mention} is now AFK: {reason}"))

    @commands.command(name="choose", aliases=["pick"])
    async def choose_cmd(self, ctx, *choices):
        """Choose between options"""
        if len(choices) < 2:
            return await ctx.send("Give me at least 2 choices!")
        await ctx.send(embed=ModEmbed.info("🎯 I Choose", f"**{random.choice(choices)}**"))

    # ═══════════════════════════════════════════════════════════════
    # STAFF COMMANDS (10+)
    # ═══════════════════════════════════════════════════════════════

    @commands.command(name="modstats", aliases=["ms"])
    @has_permissions_or_owner(manage_messages=True)
    async def modstats_cmd(self, ctx, member: discord.Member = None):
        """View moderation stats"""
        member = member or ctx.author
        cases = await self.bot.db.get_cases_by_moderator(ctx.guild.id, member.id)
        embed = discord.Embed(title=f"📊 Mod Stats: {member}", color=Colors.INFO)
        embed.add_field(name="Total Actions", value=len(cases) if cases else 0)
        await ctx.send(embed=embed)

    @commands.command(name="case")
    @has_permissions_or_owner(manage_messages=True)
    async def case_cmd(self, ctx, case_id: int):
        """View a specific case"""
        case = await self.bot.db.get_case(ctx.guild.id, case_id)
        if not case:
            return await ctx.send(embed=ModEmbed.error("Not Found", "Case not found."))
        embed = discord.Embed(title=f"📋 Case #{case_id}", color=Colors.INFO)
        embed.add_field(name="Action", value=case.get("action", "Unknown"))
        embed.add_field(name="Reason", value=case.get("reason", "No reason"))
        await ctx.send(embed=embed)

    @commands.command(name="warnings", aliases=["warns"])
    @has_permissions_or_owner(manage_messages=True)
    async def warnings_cmd(self, ctx, member: discord.Member):
        """View warnings for a user"""
        cases = await self.bot.db.get_user_cases(ctx.guild.id, member.id)
        warns = [c for c in cases if c.get("action") == "Warn"] if cases else []
        if not warns:
            return await ctx.send(embed=ModEmbed.info("⚠️ Warnings", f"No warnings for {member.mention}"))
        desc = "\n".join([f"• {w.get('reason', 'N/A')[:40]}" for w in warns[:10]])
        await ctx.send(embed=ModEmbed.info(f"⚠️ Warnings for {member}", desc))

    # ═══════════════════════════════════════════════════════════════
    # OWNER COMMANDS (10+)
    # ═══════════════════════════════════════════════════════════════

    @commands.command(name="guilds", aliases=["servers"])
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def guilds_cmd(self, ctx):
        """List all guilds"""
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count, reverse=True)[:15]
        desc = "\n".join([f"• {g.name} ({g.member_count})" for g in guilds])
        await ctx.send(embed=ModEmbed.info("🌐 Guilds", desc))

    @commands.command(name="leave")
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def leave_cmd(self, ctx, guild_id: int = None):
        """Leave a guild"""
        guild = self.bot.get_guild(guild_id) if guild_id else ctx.guild
        if guild:
            await guild.leave()
            if guild_id:
                await ctx.send(embed=ModEmbed.success("👋 Left", f"Left {guild.name}"))

    @commands.command(name="shutdown", aliases=["die"])
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def shutdown_cmd(self, ctx):
        """Shutdown the bot"""
        await ctx.send(embed=ModEmbed.info("👋 Goodbye", "Shutting down..."))
        await self.bot.close()

    @commands.command(name="reload", aliases=["rl"])
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def reload_cmd(self, ctx, cog: str):
        """Reload a cog"""
        try:
            await self.bot.reload_extension(f"cogs.{cog}")
            await ctx.send(embed=ModEmbed.success("🔄 Reloaded", f"`cogs.{cog}` reloaded."))
        except Exception as e:
            await ctx.send(embed=ModEmbed.error("Error", str(e)[:100]))

    @commands.command(name="load")
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def load_cmd(self, ctx, cog: str):
        """Load a cog"""
        try:
            await self.bot.load_extension(f"cogs.{cog}")
            await ctx.send(embed=ModEmbed.success("📦 Loaded", f"`cogs.{cog}` loaded."))
        except Exception as e:
            await ctx.send(embed=ModEmbed.error("Error", str(e)[:100]))

    @commands.command(name="unload")
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def unload_cmd(self, ctx, cog: str):
        """Unload a cog"""
        try:
            await self.bot.unload_extension(f"cogs.{cog}")
            await ctx.send(embed=ModEmbed.success("📤 Unloaded", f"`cogs.{cog}` unloaded."))
        except Exception as e:
            await ctx.send(embed=ModEmbed.error("Error", str(e)[:100]))

    @commands.command(name="sync")
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def sync_cmd(self, ctx, scope: str = "guild"):
        """Sync slash commands"""
        if scope == "global":
            synced = await self.bot.tree.sync()
        else:
            synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send(embed=ModEmbed.success("⚡ Synced", f"Synced {len(synced)} commands."))

    @commands.command(name="debug")
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def debug_cmd(self, ctx):
        """Debug info"""
        import sys
        embed = discord.Embed(title="🐛 Debug", color=Colors.INFO)
        embed.add_field(name="Python", value=sys.version[:10])
        embed.add_field(name="Discord.py", value=discord.__version__)
        embed.add_field(name="Guilds", value=len(self.bot.guilds))
        embed.add_field(name="Latency", value=f"{round(self.bot.latency*1000)}ms")
        await ctx.send(embed=embed)

    @commands.command(name="status")
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def status_cmd(self, ctx, *, status: str):
        """Set bot status"""
        await self.bot.change_presence(activity=discord.Game(name=status))
        await ctx.send(embed=ModEmbed.success("✅ Status", f"Set to: {status}"))

    @commands.command(name="dm")
    @commands.check(lambda ctx: is_bot_owner_id(ctx.author.id))
    async def dm_cmd(self, ctx, user_id: int, *, message: str):
        """DM a user"""
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(message)
            await ctx.send(embed=ModEmbed.success("📧 Sent", f"DMed {user}."))
        except Exception:
            await ctx.send(embed=ModEmbed.error("Failed", "Could not DM user."))

    # ═══════════════════════════════════════════════════════════════
    # NEW UTILITY COMMANDS
    # ═══════════════════════════════════════════════════════════════

    @commands.command(name="remindme", aliases=["remind", "reminder"])
    async def remindme_cmd(self, ctx, time: str, *, reminder: str):
        """Set a reminder (e.g. ,remindme 10m Do homework)"""
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        try:
            unit = time[-1].lower()
            amount = int(time[:-1])
            seconds = amount * units.get(unit, 60)
        except Exception:
            return await ctx.send(embed=ModEmbed.error("Invalid Time", "Use format like `10m`, `2h`, `1d`"))
        
        await ctx.send(embed=ModEmbed.success("⏰ Reminder Set", f"I'll remind you in {time}"))
        await asyncio.sleep(seconds)
        
        try:
            await ctx.author.send(f"⏰ **Reminder:** {reminder}")
        except Exception:
            await ctx.send(f"{ctx.author.mention} ⏰ **Reminder:** {reminder}")

    @commands.command(name="emojis", aliases=["emojilist", "allemojis"])
    async def emojis_cmd(self, ctx):
        """List all server emojis"""
        if not ctx.guild.emojis:
            return await ctx.send(embed=ModEmbed.error("No Emojis", "This server has no custom emojis."))
        
        emoji_str = " ".join([str(e) for e in ctx.guild.emojis[:50]])
        embed = discord.Embed(title="📝 Server Emojis", description=emoji_str, color=Colors.INFO)
        embed.set_footer(text=f"Total: {len(ctx.guild.emojis)} emojis")
        await ctx.send(embed=embed)

    @commands.command(name="enlarge", aliases=["bigemoji", "e"])
    async def enlarge_cmd(self, ctx, emoji: str):
        """Enlarge an emoji"""
        # Extract custom emoji ID
        if "<" in emoji:
            emoji_id = emoji.split(":")[-1].replace(">", "")
            animated = emoji.startswith("<a:")
            ext = "gif" if animated else "png"
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
            
            embed = discord.Embed(color=Colors.INFO)
            embed.set_image(url=url)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=ModEmbed.error("Invalid Emoji", "Please provide a custom emoji."))

    @commands.command(name="steal", aliases=["addemoji", "clone"])
    @has_permissions_or_owner(manage_emojis=True)
    async def steal_cmd(self, ctx, emoji: str, name: str = None):
        """Steal an emoji from another server"""
        if "<" not in emoji:
            return await ctx.send(embed=ModEmbed.error("Invalid Emoji", "Must be a custom emoji."))
        
        emoji_id = emoji.split(":")[-1].replace(">", "")
        animated = emoji.startswith("<a:")
        ext = "gif" if animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
        
        if not name:
            name = emoji.split(":")[1]
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    image = await resp.read()
            
            new_emoji = await ctx.guild.create_custom_emoji(name=name, image=image)
            await ctx.send(embed=ModEmbed.success("✅ Emoji Added", f"Added {new_emoji} as `:{name}:`"))
        except Exception as e:
            await ctx.send(embed=ModEmbed.error("Failed", f"Could not steal emoji: {str(e)[:100]}"))

    @commands.command(name="charinfo", aliases=["char", "character"])
    async def charinfo_cmd(self, ctx, *, characters: str):
        """Get info about characters"""
        result = []
        for char in characters[:20]:
            code = ord(char)
            result.append(f"`{char}` - U+{code:04X} ({code})")
        
        embed = discord.Embed(title="📝 Character Info", description="\n".join(result), color=Colors.INFO)
        await ctx.send(embed=embed)

    @commands.command(name="firstmessage", aliases=["first", "fm"])
    async def firstmessage_cmd(self, ctx, channel: discord.TextChannel = None):
        """Get the first message in a channel"""
        channel = channel or ctx.channel
        try:
            async for message in channel.history(limit=1, oldest_first=True):
                embed = discord.Embed(
                    description=f"[Jump to first message]({message.jump_url})",
                    color=Colors.INFO
                )
                embed.set_author(name=message.author, icon_url=message.author.display_avatar.url)
                embed.add_field(name="Content", value=message.content[:1024] or "*No content*")
                embed.set_footer(text=message.created_at.strftime("%Y-%m-%d %H:%M:%S"))
                await ctx.send(embed=embed)
        except Exception:
            await ctx.send(embed=ModEmbed.error("Error", "Could not fetch first message."))

    @commands.command(name="roleall", aliases=["giveroleall"])
    @has_permissions_or_owner(administrator=True)
    async def roleall_cmd(self, ctx, role: discord.Role):
        """Give a role to everyone in the server"""
        msg = await ctx.send(embed=ModEmbed.info("⏳ Processing", "Adding role to all members..."))
        
        success = 0
        failed = 0
        
        for member in ctx.guild.members:
            if role in member.roles:
                continue
            try:
                await member.add_roles(role)
                success += 1
            except Exception:
                failed += 1
        
        await msg.edit(embed=ModEmbed.success(
            "✅ Complete",
            f"Added {role.mention} to **{success}** members.\nFailed: **{failed}**"
        ))

    @commands.command(name="removeall", aliases=["removeroleall"])
    @has_permissions_or_owner(administrator=True)
    async def removeall_cmd(self, ctx, role: discord.Role):
        """Remove a role from everyone"""
        msg = await ctx.send(embed=ModEmbed.info("⏳ Processing", "Removing role from all members..."))
        
        success = 0
        for member in role.members:
            try:
                await member.remove_roles(role)
                success += 1
            except Exception:
                pass
        
        await msg.edit(embed=ModEmbed.success("✅ Complete", f"Removed {role.mention} from **{success}** members."))

    @commands.command(name="rolecolor", aliases=["rc", "colorole"])
    @has_permissions_or_owner(manage_roles=True)
    async def rolecolor_cmd(self, ctx, role: discord.Role, color: str):
        """Change a role's color (hex code like #FF0000)"""
        try:
            if not color.startswith("#"):
                color = "#" + color
            await role.edit(color=discord.Color(int(color[1:], 16)))
            await ctx.send(embed=ModEmbed.success("🎨 Color Changed", f"{role.mention} color set to `{color}`"))
        except Exception:
            await ctx.send(embed=ModEmbed.error("Invalid Color", "Use hex format like `#FF0000`"))

    @commands.command(name="hideall", aliases=["hidechannels"])
    @has_permissions_or_owner(administrator=True)
    async def hideall_cmd(self, ctx):
        """Hide all channels from @everyone"""
        count = 0
        for channel in ctx.guild.channels:
            try:
                await channel.set_permissions(ctx.guild.default_role, view_channel=False)
                count += 1
            except Exception:
                pass
        
        await ctx.send(embed=ModEmbed.success("👻 Channels Hidden", f"Hid **{count}** channels from @everyone"))

    @commands.command(name="unhideall", aliases=["unhidechannels"])
    @has_permissions_or_owner(administrator=True)
    async def unhideall_cmd(self, ctx):
        """Unhide all channels for @everyone"""
        count = 0
        for channel in ctx.guild.channels:
            try:
                await channel.set_permissions(ctx.guild.default_role, view_channel=True)
                count += 1
            except Exception:
                pass
        
        await ctx.send(embed=ModEmbed.success("👁️ Channels Visible", f"Unhid **{count}** channels for @everyone"))

    @commands.command(name="createemoji", aliases=["makeemoji"])
    @has_permissions_or_owner(manage_emojis=True)
    async def createemoji_cmd(self, ctx, name: str, url: str):
        """Create an emoji from a URL"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    image = await resp.read()
            
            emoji = await ctx.guild.create_custom_emoji(name=name, image=image)
            await ctx.send(embed=ModEmbed.success("✅ Emoji Created", f"Added {emoji} as `:{name}:`"))
        except Exception as e:
            await ctx.send(embed=ModEmbed.error("Failed", f"Error: {str(e)[:100]}"))

    @commands.command(name="deleteemoji", aliases=["removeemoji"])
    @has_permissions_or_owner(manage_emojis=True)
    async def deleteemoji_cmd(self, ctx, emoji: str):
        """Delete a server emoji"""
        if "<" not in emoji:
            return await ctx.send(embed=ModEmbed.error("Invalid Emoji", "Must be a custom emoji."))
        
        emoji_id = int(emoji.split(":")[-1].replace(">", ""))
        emoji_obj = discord.utils.get(ctx.guild.emojis, id=emoji_id)
        
        if emoji_obj:
            name = emoji_obj.name
            await emoji_obj.delete()
            await ctx.send(embed=ModEmbed.success("🗑️ Emoji Deleted", f"Deleted `:{name}:`"))
        else:
            await ctx.send(embed=ModEmbed.error("Not Found", "Emoji not found in this server."))

    @commands.command(name="permissions", aliases=["perms", "perm"])
    async def permissions_cmd(self, ctx, member: discord.Member = None, channel: discord.TextChannel = None):
        """Check permissions for a user"""
        member = member or ctx.author
        channel = channel or ctx.channel
        
        perms = channel.permissions_for(member)
        perm_list = [name.replace("_", " ").title() for name, value in perms if value]
        
        embed = discord.Embed(
            title=f"Permissions for {member}",
            description=f"**Channel:** {channel.mention}\n\n" + ", ".join(perm_list[:20]),
            color=member.color
        )
        await ctx.send(embed=embed)

    @commands.command(name="color", aliases=["colour"])
    async def color_cmd(self, ctx, color: str):
        """Show a color preview"""
        try:
            if not color.startswith("#"):
                color = "#" + color
            color_int = int(color[1:], 16)
            embed = discord.Embed(title=color, color=color_int)
            embed.set_thumbnail(url=f"https://singlecolorimage.com/get/{color[1:]}/100x100")
            await ctx.send(embed=embed)
        except Exception:
            await ctx.send(embed=ModEmbed.error("Invalid Color", "Use hex format like `#FF0000`"))


async def setup(bot):
    cog = PrefixCommands(bot)
    existing = set(bot.all_commands.keys())
    filtered = []
    for command in cog.get_commands():
        names = {command.name, *command.aliases}
        if not (names & existing):
            filtered.append(command)
    cog.__cog_commands__ = filtered
    await bot.add_cog(cog)

