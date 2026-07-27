"""
Advanced Utility Commands - Enhanced info, tools, and helper commands
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal
import random
import re
from collections import defaultdict

from utils.embeds import ModEmbed, Colors
from utils.checks import is_mod, is_admin
from utils.paginator import Paginator
from config import Config

class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.afk_users = {}  # {user_id: {"reason": str, "since": datetime}}

    # ==================== UTILITY GROUP ====================
    utility_group = app_commands.Group(name="utility", description="🛠️ Utility commands")

    # ==================== FUN / MIMIC ====================

    @commands.command(name="mimic")
    async def mimic(self, ctx: commands.Context, member: discord.Member, *, message: str):
        """
        Mimic a user using a webhook (Admin only).
        Usage: ,mimic @User <message>
        """
        # Permission Check (Admin only)
        is_owner = await self.bot.is_owner(ctx.author)
        is_admin_perm = ctx.author.guild_permissions.administrator
        
        has_role = False
        if not (is_owner or is_admin_perm):
            settings = await self.bot.db.get_settings(ctx.guild.id)
            admin_roles = settings.get("admin_roles", [])
            manager_role = settings.get("manager_role")
            user_roles = [r.id for r in ctx.author.roles]
            
            if manager_role and manager_role in user_roles:
                has_role = True
            elif any(rid in user_roles for rid in admin_roles):
                has_role = True
        
        if not (is_owner or is_admin_perm or has_role):
            # Silent fail or reply? "Admin only" usually implies silent or error.
            # Let's reply to be helpful since they asked for it.
            try:
                await ctx.reply("❌ This command is restricted to Admins.", delete_after=5)
            except Exception:
                pass
            return

        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("❌ This command can only be used in text channels.")
            return

        # Delete the command message to keep it seamless
        try:
            await ctx.message.delete()
        except Exception:
            pass

        # Create webhook
        webhook = None
        try:
            webhook = await ctx.channel.create_webhook(name=member.display_name)
        except Exception as e:
            await ctx.send(f"❌ Failed to create webhook: {e}", delete_after=5)
            return

        # Send mimic message
        try:
            await webhook.send(
                content=message,
                username=member.display_name,
                avatar_url=member.display_avatar.url,
                wait=True, # Wait to ensure it sends before deleting
                use_v2=False,
            )
        except Exception as e:
            await ctx.send(f"❌ Failed to send mimic message: {e}", delete_after=5)
        
        # Cleanup
        finally:
            if webhook:
                try:
                    await webhook.delete()
                except Exception:
                    pass

    # ==================== AFK SYSTEM ====================
    
    @utility_group.command(name="afk", description="⏸️ Set your AFK status")
    @app_commands.describe(reason="Reason for being AFK")
    async def afk(self, interaction: discord.Interaction, reason: Optional[str] = "AFK"):
        """Set AFK status with auto-reply"""
        self.afk_users[interaction.user.id] = {
            "reason": reason[:100],
            "since": datetime.now(timezone.utc)
        }
        
        embed = discord.Embed(
            title="💤 AFK Status Set",
            description=f"You're now AFK: **{reason}**\n\nYou'll be automatically mentioned when someone pings you.",
            color=Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle AFK mentions and returns"""
        if message.author.bot or not message.guild:
            return
        
        # Check if user returned from AFK
        if message.author.id in self.afk_users:
            afk_data = self.afk_users.pop(message.author.id)
            duration = datetime.now(timezone.utc) - afk_data["since"]
            
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            
            time_str = ""
            if hours > 0:
                time_str += f"{hours}h "
            if minutes > 0:
                time_str += f"{minutes}m"
            if not time_str:
                time_str = "<1m"
            
            try:
                await message.channel.send(
                    f"👋 Welcome back {message.author.mention}! You were AFK for **{time_str.strip()}**",
                    delete_after=10
                )
            except Exception:
                pass
        
        # Check if any mentioned users are AFK
        for user in message.mentions:
            if user.id in self.afk_users and user.id != message.author.id:
                afk_data = self.afk_users[user.id]
                since_ts = int(afk_data["since"].timestamp())
                
                try:
                    await message.channel.send(
                        f"💤 {user.mention} is currently AFK: **{afk_data['reason']}** (since <t:{since_ts}:R>)",
                        delete_after=15
                    )
                except Exception:
                    pass
    
    # ==================== INFO COMMAND GROUP ====================



    # ==================== TIMESTAMP GENERATOR ====================
    
    @utility_group.command(name="timestamp", description="⏰ Generate Discord timestamp formats")
    @app_commands.describe(
        time="Time in format: YYYY-MM-DD HH:MM or use 'now'",
        timezone_offset="Timezone offset (e.g., -5 for EST, +1 for CET)"
    )
    async def timestamp(
        self, 
        interaction: discord.Interaction,
        time: str = "now",
        timezone_offset: Optional[int] = 0
    ):
        """Generate Discord timestamps"""
        try:
            if time.lower() == "now":
                dt = datetime.now(timezone.utc)
            else:
                # Try parsing different formats
                for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                    try:
                        dt = datetime.strptime(time, fmt)
                        dt = dt.replace(tzinfo=timezone.utc)
                        dt = dt - timedelta(hours=timezone_offset)
                        break
                    except ValueError:
                        continue
                else:
                    return await interaction.response.send_message(
                        embed=ModEmbed.error(
                            "Invalid Format",
                            "Use format: `YYYY-MM-DD HH:MM` or `now`\nExample: `2025-12-25 15:30`"
                        ),
                        ephemeral=True
                    )
            
            unix = int(dt.timestamp())
            
            embed = discord.Embed(
                title="⏰ Discord Timestamps",
                description=f"**Unix Timestamp:** `{unix}`\n**ISO Format:** `{dt.isoformat()}`",
                color=Colors.INFO,
                timestamp=datetime.now(timezone.utc)
            )
            
            formats = [
                ("Short Time", "t", f"<t:{unix}:t>"),
                ("Long Time", "T", f"<t:{unix}:T>"),
                ("Short Date", "d", f"<t:{unix}:d>"),
                ("Long Date", "D", f"<t:{unix}:D>"),
                ("Short Date/Time", "f", f"<t:{unix}:f>"),
                ("Long Date/Time", "F", f"<t:{unix}:F>"),
                ("Relative Time", "R", f"<t:{unix}:R>"),
            ]
            
            for name, style, code in formats:
                embed.add_field(
                    name=f"{name} (:{style})",
                    value=f"{code}\n`{code}`",
                    inline=True
                )
            
            embed.set_footer(text="Copy the code to use in Discord")
            
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            await interaction.response.send_message(
                embed=ModEmbed.error("Error", f"Failed to generate timestamp: {str(e)}"),
                ephemeral=True
            )
    
    # ==================== PERMISSIONS CHECKER ====================
    
    @utility_group.command(name="permissions", description="🔐 Check user permissions in channel")
    @app_commands.describe(
        user="User to check (defaults to you)",
        channel="Channel to check (defaults to current)"
    )
    async def permissions(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        channel: Optional[discord.abc.GuildChannel] = None
    ):
        """Display user permissions in a channel"""
        user = user or interaction.user
        channel = channel or interaction.channel
        
        perms = channel.permissions_for(user)
        
        embed = discord.Embed(
            title=f"🔐 Permissions: {user.display_name}",
            description=f"**Channel:** {channel.mention}\n**User:** {user.mention}",
            color=user.color if user.color != discord.Color.default() else Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        # Categorize permissions
        general_perms = []
        text_perms = []
        voice_perms = []
        advanced_perms = []
        
        perm_categories = {
            "general": ["view_channel", "manage_channels", "manage_permissions", "manage_webhooks", "create_instant_invite"],
            "text": ["send_messages", "embed_links", "attach_files", "add_reactions", "use_external_emojis", 
                    "use_external_stickers", "mention_everyone", "manage_messages", "read_message_history",
                    "send_tts_messages", "use_application_commands", "send_messages_in_threads",
                    "create_public_threads", "create_private_threads", "manage_threads"],
            "voice": ["connect", "speak", "stream", "use_voice_activation", "priority_speaker",
                     "mute_members", "deafen_members", "move_members", "use_soundboard", "use_external_sounds"],
            "advanced": ["administrator", "manage_roles", "manage_guild", "view_audit_log",
                        "view_guild_insights", "moderate_members"]
        }
        
        for perm, value in perms:
            emoji = "✅" if value else "❌"
            perm_name = perm.replace("_", " ").title()
            perm_str = f"{emoji} {perm_name}"
            
            if perm in perm_categories["general"]:
                general_perms.append(perm_str)
            elif perm in perm_categories["text"]:
                text_perms.append(perm_str)
            elif perm in perm_categories["voice"]:
                voice_perms.append(perm_str)
            elif perm in perm_categories["advanced"]:
                advanced_perms.append(perm_str)
        
        if advanced_perms:
            embed.add_field(
                name="🔑 Advanced Permissions",
                value="\n".join(advanced_perms[:10]) or "None",
                inline=False
            )
        
        if general_perms:
            embed.add_field(
                name="⚙️ General",
                value="\n".join(general_perms[:15]) or "None",
                inline=True
            )
        
        if text_perms and isinstance(channel, discord.TextChannel):
            embed.add_field(
                name="💬 Text Channel",
                value="\n".join(text_perms[:15]) or "None",
                inline=True
            )
        
        if voice_perms and isinstance(channel, discord.VoiceChannel):
            embed.add_field(
                name="🔊 Voice Channel",
                value="\n".join(voice_perms[:15]) or "None",
                inline=True
            )
        
        embed.set_footer(text=f"Permission Value: {perms.value}")
        
        await interaction.response.send_message(embed=embed)
    
    # ==================== MEMBER COUNT ====================
    
    @utility_group.command(name="members", description="📊 Detailed member count breakdown")
    async def membercount(self, interaction: discord.Interaction):
        """Show detailed member statistics"""
        guild = interaction.guild
        
        # Calculate stats
        total = guild.member_count
        humans = len([m for m in guild.members if not m.bot])
        bots = total - humans
        
        # Online status
        online = len([m for m in guild.members if m.status == discord.Status.online])
        idle = len([m for m in guild.members if m.status == discord.Status.idle])
        dnd = len([m for m in guild.members if m.status == discord.Status.dnd])
        offline = len([m for m in guild.members if m.status == discord.Status.offline])
        
        # Roles stats
        with_roles = len([m for m in guild.members if len(m.roles) > 1])
        no_roles = total - with_roles
        
        # Account ages
        now = datetime.now(timezone.utc)
        new_accounts = len([m for m in guild.members if (now - m.created_at).days < 7])
        young_accounts = len([m for m in guild.members if 7 <= (now - m.created_at).days < 30])
        
        # Recently joined
        recent_joins = len([m for m in guild.members if m.joined_at and (now - m.joined_at).days < 7])
        
        embed = discord.Embed(
            title=f"📊 Member Statistics: {guild.name}",
            color=Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        
        # Total breakdown
        embed.add_field(
            name="👥 Total Members",
            value=f"**{total:,}** members\n{humans:,} humans\n{bots:,} bots",
            inline=True
        )
        
        # Online status
        embed.add_field(
            name="🟢 Status Breakdown",
            value=f"🟢 {online:,} Online\n🟡 {idle:,} Idle\n🔴 {dnd:,} DND\n⚫ {offline:,} Offline",
            inline=True
        )
        
        # Roles
        embed.add_field(
            name="🏷️ Role Stats",
            value=f"✅ {with_roles:,} with roles\n❌ {no_roles:,} no roles",
            inline=True
        )
        
        # Account ages
        embed.add_field(
            name="📅 Account Age",
            value=f"🆕 {new_accounts:,} < 7 days\n📆 {young_accounts:,} 7-30 days",
            inline=True
        )
        
        # Recent activity
        embed.add_field(
            name="🚪 Recent Joins",
            value=f"{recent_joins:,} in last 7 days",
            inline=True
        )
        
        # Percentages
        human_pct = (humans / total * 100) if total > 0 else 0
        bot_pct = (bots / total * 100) if total > 0 else 0
        online_pct = ((online + idle + dnd) / total * 100) if total > 0 else 0
        
        embed.add_field(
            name="📈 Ratios",
            value=f"👤 {human_pct:.1f}% human\n🤖 {bot_pct:.1f}% bots\n🟢 {online_pct:.1f}% active",
            inline=True
        )
        
        await interaction.response.send_message(embed=embed)
    
    # ==================== BOTS ====================
    
    @utility_group.command(name="bots", description="🤖 List all bots in the server")
    @app_commands.describe(show_permissions="Show key permissions for each bot")
    async def bots(self, interaction: discord.Interaction, show_permissions: bool = False):
        """List all bots with details"""
        guild = interaction.guild
        bots = [m for m in guild.members if m.bot]
        
        if not bots:
            return await interaction.response.send_message(
                embed=ModEmbed.info("No Bots", "This server has no bots."),
                ephemeral=True
            )
        
        # Sort bots by join date
        bots.sort(key=lambda m: m.joined_at or datetime.now(timezone.utc))
        
        embed = discord.Embed(
            title=f"🤖 Server Bots ({len(bots)} total)",
            description=f"**Total Members:** {guild.member_count}\n**Bot Ratio:** {len(bots)/guild.member_count*100:.1f}%",
            color=Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        
        pages = []
        for i in range(0, len(bots), 10):
            chunk = bots[i:i+10]
            page_embed = embed.copy()
            
            bot_list = []
            for bot in chunk:
                status_emoji = {
                    discord.Status.online: "🟢",
                    discord.Status.idle: "🟡",
                    discord.Status.dnd: "🔴",
                    discord.Status.offline: "⚫"
                }
                
                bot_info = f"{status_emoji.get(bot.status, '⚫')} {bot.mention}"
                
                if show_permissions and bot.guild_permissions:
                    key_perms = []
                    if bot.guild_permissions.administrator:
                        key_perms.append("Admin")
                    if bot.guild_permissions.manage_guild:
                        key_perms.append("Manage Server")
                    if bot.guild_permissions.manage_roles:
                        key_perms.append("Manage Roles")
                    if bot.guild_permissions.ban_members:
                        key_perms.append("Ban")
                    if bot.guild_permissions.kick_members:
                        key_perms.append("Kick")
                    
                    if key_perms:
                        bot_info += f"\n└ 🔑 {', '.join(key_perms)}"
                
                if bot.joined_at:
                    join_ts = int(bot.joined_at.timestamp())
                    bot_info += f"\n└ 📅 Joined <t:{join_ts}:R>"
                
                bot_list.append(bot_info)
            
            page_embed.add_field(
                name="🤖 Bot List",
                value="\n\n".join(bot_list),
                inline=False
            )
            
            page_embed.set_footer(text=f"Page {len(pages)+1}/{(len(bots)-1)//10+1}")
            pages.append(page_embed)
        
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0])
        else:
            await Paginator.paginate(interaction, pages)
    
    # ==================== CHANNEL INFO ====================
    
    @utility_group.command(name="channel", description="📺 Detailed channel information")
    @app_commands.describe(channel="Channel to get info about (defaults to current)")
    async def channelinfo(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.abc.GuildChannel] = None
    ):
        """Display detailed channel information"""
        channel = channel or interaction.channel
        
        embed = discord.Embed(
            title=f"📺 Channel Information",
            color=Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Name", value=channel.name, inline=True)
        embed.add_field(name="ID", value=f"`{channel.id}`", inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        
        embed.add_field(
            name="Created",
            value=f"<t:{int(channel.created_at.timestamp())}:F>\n(<t:{int(channel.created_at.timestamp())}:R>)",
            inline=False
        )
        
        if isinstance(channel, discord.TextChannel):
            embed.add_field(name="NSFW", value="Yes" if channel.nsfw else "No", inline=True)
            embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s" if channel.slowmode_delay else "None", inline=True)
            embed.add_field(name="News Channel", value="Yes" if channel.is_news() else "No", inline=True)
            
            if channel.topic:
                embed.add_field(name="Topic", value=channel.topic[:1000], inline=False)
        
        elif isinstance(channel, discord.VoiceChannel):
            embed.add_field(name="Bitrate", value=f"{channel.bitrate//1000}kbps", inline=True)
            embed.add_field(name="User Limit", value=str(channel.user_limit) if channel.user_limit else "Unlimited", inline=True)
            embed.add_field(name="Region", value=str(channel.rtc_region or "Automatic"), inline=True)
        
        if channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        
        embed.add_field(name="Position", value=str(channel.position), inline=True)
        embed.add_field(name="Mention", value=channel.mention, inline=True)
        
        # Permission overwrites count
        overwrites_count = len(channel.overwrites)
        embed.add_field(name="Permission Overwrites", value=str(overwrites_count), inline=True)
        
        await interaction.response.send_message(embed=embed)
    
    # ==================== USER INFO ====================
    
    @utility_group.command(name="user", description="📋 Detailed information about a user")
    @app_commands.describe(user="The user to get info about")
    async def userinfo(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        user = user or interaction.user
        
        embed = discord.Embed(
            title=f"User Information - {user}",
            color=user.color if user.color != discord.Color.default() else Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.set_thumbnail(url=user.display_avatar.url)
        
        # Basic info
        embed.add_field(name="ID", value=f"`{user.id}`", inline=True)
        embed.add_field(name="Nickname", value=user.nick or "None", inline=True)
        embed.add_field(name="Bot", value="Yes" if user.bot else "No", inline=True)
        
        # Dates
        embed.add_field(
            name="Account Created",
            value=f"<t:{int(user.created_at.timestamp())}:F>\n<t:{int(user.created_at.timestamp())}:R>",
            inline=True
        )
        embed.add_field(
            name="Joined Server",
            value=f"<t:{int(user.joined_at.timestamp())}:F>\n<t:{int(user.joined_at.timestamp())}:R>" if user.joined_at else "Unknown",
            inline=True
        )
        
        # Status
        status_emoji = {
            discord.Status.online: "🟢 Online",
            discord.Status.idle: "🟡 Idle",
            discord.Status.dnd: "🔴 Do Not Disturb",
            discord.Status.offline: "⚫ Offline"
        }
        embed.add_field(name="Status", value=status_emoji.get(user.status, '⚫ Unknown'), inline=True)
        
        # Boost status
        if user.premium_since:
            boost_ts = int(user.premium_since.timestamp())
            embed.add_field(name="💎 Boosting Since", value=f"<t:{boost_ts}:R>", inline=True)
        
        # Roles
        roles = [r.mention for r in user.roles[1:]]
        roles.reverse()
        roles_text = ", ".join(roles[:20]) if roles else "None"
        if len(roles) > 20:
            roles_text += f" (+{len(roles) - 20} more)"
        embed.add_field(name=f"Roles [{len(roles)}]", value=roles_text, inline=False)
        
        # Key permissions
        key_perms = []
        if user.guild_permissions.administrator:
            key_perms.append("Administrator")
        if user.guild_permissions.manage_guild:
            key_perms.append("Manage Server")
        if user.guild_permissions.manage_roles:
            key_perms.append("Manage Roles")
        if user.guild_permissions.manage_channels:
            key_perms.append("Manage Channels")
        if user.guild_permissions.kick_members:
            key_perms.append("Kick Members")
        if user.guild_permissions.ban_members:
            key_perms.append("Ban Members")
        if user.guild_permissions.manage_messages:
            key_perms.append("Manage Messages")
        
        if key_perms:
            embed.add_field(name="Key Permissions", value=", ".join(key_perms), inline=False)
        
        # Acknowledgements
        acknowledgements = []
        if user.id == user.guild.owner_id:
            acknowledgements.append("👑 Server Owner")
        if user.premium_since:
            acknowledgements.append("💎 Server Booster")
        if user.bot:
            acknowledgements.append("🤖 Bot Account")
        if acknowledgements:
            embed.add_field(name="Acknowledgements", value=" • ".join(acknowledgements), inline=False)
        
        await interaction.response.send_message(embed=embed)
    
    # ==================== SERVER INFO ====================
    
    @utility_group.command(name="server", description="📊 Detailed server information")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        
        embed = discord.Embed(
            title=f"Server Information - {guild.name}",
            color=Colors.INFO,
            timestamp=datetime.now(timezone.utc)
        )
        
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        
        embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
        embed.add_field(name="ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(
            name="Created",
            value=f"<t:{int(guild.created_at.timestamp())}:R>",
            inline=True
        )
        
        # Member counts
        total = guild.member_count
        bots = len([m for m in guild.members if m.bot])
        humans = total - bots
        
        embed.add_field(name="Total Members", value=f"{total:,}", inline=True)
        embed.add_field(name="Humans", value=f"{humans:,}", inline=True)
        embed.add_field(name="Bots", value=f"{bots:,}", inline=True)
        
        # Channels
        text = len(guild.text_channels)
        voice = len(guild.voice_channels)
        categories = len(guild.categories)
        
        embed.add_field(name="Text Channels", value=text, inline=True)
        embed.add_field(name="Voice Channels", value=voice, inline=True)
        embed.add_field(name="Categories", value=categories, inline=True)
        
        embed.add_field(name="Roles", value=len(guild.roles), inline=True)
        embed.add_field(name="Emojis", value=f"{len(guild.emojis)}/{guild.emoji_limit}", inline=True)
        
        # Boost info
        embed.add_field(
            name="Boost Status",
            value=f"Level {guild.premium_tier} ({guild.premium_subscription_count or 0} boosts)",
            inline=True
        )
        
        # Features
        features = []
        feature_map = {
            "COMMUNITY": "📢 Community",
            "VERIFIED": "✅ Verified",
            "PARTNERED": "🤝 Partnered",
            "DISCOVERABLE": "🔍 Discoverable",
            "VANITY_URL": "🔗 Vanity URL",
            "ANIMATED_ICON": "🎬 Animated Icon",
            "BANNER": "🖼️ Banner",
            "INVITE_SPLASH": "🌊 Invite Splash",
            "PREVIEW_ENABLED": "👀 Preview Enabled"
        }
        
        for feature in guild.features:
            if feature in feature_map:
                features.append(feature_map[feature])
        
        if features:
            embed.add_field(
                name="Features",
                value="\n".join(features[:10]),
                inline=False
            )
        
        await interaction.response.send_message(embed=embed)
    
    # ==================== RULE34 SEARCH ====================
    
    @commands.command(name="r34", aliases=["rule34"])
    async def r34_search(self, ctx: commands.Context, *, query: str):
        """
        Search Rule34 for images (NSFW channels only)
        Usage: ,r34 <search terms>
        """
        # NSFW Channel Check
        if not isinstance(ctx.channel, discord.TextChannel) or not ctx.channel.nsfw:
            return await ctx.reply(
                embed=ModEmbed.error(
                    "NSFW Channel Required",
                    "This command can only be used in NSFW channels."
                ),
                delete_after=5
            )
        
        # Import here to avoid loading unless needed
        import aiohttp
        import xml.etree.ElementTree as ET
        
        # Clean up query - replace spaces with underscores for tags
        tags = query.strip().replace(" ", "_")
        
        # Rule34 API endpoint
        api_url = f"https://rule34.xxx/index.php?page=dapi&s=post&q=index&tags={tags}&limit=100"
        
        await ctx.typing()
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        return await ctx.reply(
                            embed=ModEmbed.error(
                                "API Error",
                                f"Failed to fetch results (Status: {response.status})"
                            )
                        )
                    
                    xml_data = await response.text()
                    
                    # Check if we got valid data
                    if not xml_data or len(xml_data) < 10:
                        return await ctx.reply(
                            embed=ModEmbed.error(
                                "API Error",
                                "Received empty response from API."
                            )
                        )
                    
                    # Parse XML
                    try:
                        root = ET.fromstring(xml_data)
                    except ET.ParseError as e:
                        return await ctx.reply(
                            embed=ModEmbed.error(
                                "Parse Error",
                                f"Failed to parse API response: {str(e)[:100]}"
                            )
                        )
                    
                    # Get all posts
                    posts = root.findall('post')
                    
                    if not posts:
                        return await ctx.reply(
                            embed=ModEmbed.info(
                                "No Results",
                                f"No results found for: **{query}**\nTry different tags or check spelling."
                            )
                        )
                    
                    # Randomly select a post
                    post = random.choice(posts)
                    
                    # Extract data
                    file_url = post.get('file_url')
                    sample_url = post.get('sample_url')
                    preview_url = post.get('preview_url')
                    
                    # Use sample if file_url is not available
                    image_url = file_url or sample_url or preview_url
                    
                    if not image_url:
                        return await ctx.reply(
                            embed=ModEmbed.error(
                                "Invalid Post",
                                "Selected post has no valid image URL. Trying again might help."
                            )
                        )
                    
                    score = post.get('score', '0')
                    post_id = post.get('id', 'unknown')
                    tags_str = post.get('tags', '')
                    rating = post.get('rating', 'u')
                    
                    # Rating emojis
                    rating_display = {
                        's': '🔞 Explicit',
                        'q': '⚠️ Questionable',
                        'e': '🔞 Explicit',
                        'u': '❓ Unknown'
                    }.get(rating, '❓ Unknown')
                    
                    # Create embed
                    embed = discord.Embed(
                        title=f"🔞 Rule34 - {query}",
                        color=Colors.EMBED,
                        timestamp=datetime.now(timezone.utc)
                    )
                    
                    embed.set_image(url=image_url)
                    
                    # Add metadata
                    embed.add_field(name="Post ID", value=f"`{post_id}`", inline=True)
                    embed.add_field(name="Score", value=f"⭐ {score}", inline=True)
                    embed.add_field(name="Rating", value=rating_display, inline=True)
                    
                    # Show some tags (truncate if too long)
                    if tags_str:
                        tags_list = tags_str.split()[:15]
                        tags_display = ", ".join([f"`{tag}`" for tag in tags_list])
                        if len(tags_str.split()) > 15:
                            tags_display += f" (+{len(tags_str.split()) - 15} more)"
                        embed.add_field(name="Tags", value=tags_display, inline=False)
                    
                    embed.set_footer(text=f"Rule34.xxx • Requested by {ctx.author}")
                    
                    # Add view post button
                    view = discord.ui.View()
                    view.add_item(
                        discord.ui.Button(
                            label="View on Rule34",
                            url=f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}",
                            style=discord.ButtonStyle.link
                        )
                    )
                    
                    await ctx.reply(embed=embed, view=view)
                    
        except aiohttp.ClientError as e:
            await ctx.reply(
                embed=ModEmbed.error(
                    "Connection Error",
                    f"Failed to connect to Rule34 API: {type(e).__name__}"
                )
            )
        except Exception as e:
            await ctx.reply(
                embed=ModEmbed.error(
                    "Error",
                    f"An unexpected error occurred: {type(e).__name__}\n{str(e)[:100]}"
                )
            )


async def setup(bot):
    await bot.add_cog(Utility(bot))
