"""
Blacklist System - Owner-only commands to manage bot-wide blacklist
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional
import math

from utils.embeds import ModEmbed
from utils.checks import is_owner_only


class Blacklist(commands.Cog):
    """Owner-only blacklist management"""
    
    def __init__(self, bot):
        self.bot = bot
    
    blacklist_group = app_commands.Group(
        name="blacklist",
        description="🚫 Manage the bot-wide blacklist (Owner only)"
    )
    
    @blacklist_group.command(name="add", description="🚫 Add a user to the blacklist")
    @app_commands.describe(
        user="The user to blacklist",
        reason="Reason for blacklisting"
    )
    @is_owner_only()
    async def blacklist_add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: Optional[str] = "No reason provided"
    ):
        # Check if user is a bot owner (can't blacklist owners)
        owner_ids = getattr(self.bot, "owner_ids", set()) or set()
        if user.id in owner_ids:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Cannot Blacklist", "You cannot blacklist a bot owner."),
                ephemeral=True
            )
        
        # Check if already blacklisted
        if await self.bot.db.is_blacklisted(user.id):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Already Blacklisted", f"{user.mention} is already blacklisted."),
                ephemeral=True
            )
        
        # Add to blacklist
        success = await self.bot.db.add_to_blacklist(user.id, reason, interaction.user.id)
        
        if success:
            # Update cache
            self.bot.blacklist_cache.add(user.id)
            
            embed = ModEmbed.success(
                "User Blacklisted",
                f"{user.mention} has been blacklisted from using the bot.\n\n**Reason:** {reason}"
            )
            embed.set_footer(text=f"User ID: {user.id}")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=ModEmbed.error("Error", "Failed to add user to blacklist."),
                ephemeral=True
            )
    
    @blacklist_group.command(name="remove", description="✅ Remove a user from the blacklist")
    @app_commands.describe(user="The user to unblacklist")
    @is_owner_only()
    async def blacklist_remove(
        self,
        interaction: discord.Interaction,
        user: discord.User
    ):
        # Check if blacklisted
        if not await self.bot.db.is_blacklisted(user.id):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Blacklisted", f"{user.mention} is not blacklisted."),
                ephemeral=True
            )
        
        # Remove from blacklist
        success = await self.bot.db.remove_from_blacklist(user.id)
        
        if success:
            # Update cache
            self.bot.blacklist_cache.discard(user.id)
            
            embed = ModEmbed.success(
                "User Unblacklisted",
                f"{user.mention} has been removed from the blacklist and can now use the bot."
            )
            embed.set_footer(text=f"User ID: {user.id}")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=ModEmbed.error("Error", "Failed to remove user from blacklist."),
                ephemeral=True
            )
    
    @blacklist_group.command(name="list", description="📋 View all blacklisted users")
    @is_owner_only()
    async def blacklist_list(self, interaction: discord.Interaction):
        blacklist = await self.bot.db.get_blacklist()
        
        if not blacklist:
            return await interaction.response.send_message(
                embed=ModEmbed.info("Blacklist Empty", "No users are currently blacklisted."),
                ephemeral=True
            )
        
        # Paginate if needed
        per_page = 10
        pages = math.ceil(len(blacklist) / per_page)
        
        lines = []
        for i, entry in enumerate(blacklist[:per_page], 1):
            user_id = entry["user_id"]
            reason = entry["reason"] or "No reason"
            added_by = entry["added_by"]
            created = entry["created_at"]
            
            # Try to get user info
            try:
                user = await self.bot.fetch_user(user_id)
                user_str = f"{user} (`{user_id}`)"
            except Exception:
                user_str = f"Unknown User (`{user_id}`)"
            
            lines.append(f"**{i}.** {user_str}\n   └ Reason: {reason[:50]}{'...' if len(reason) > 50 else ''}")
        
        embed = discord.Embed(
            title="🚫 Blacklisted Users",
            description="\n".join(lines),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Total: {len(blacklist)} users | Page 1/{pages}")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Blacklist(bot))
