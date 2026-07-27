"""
Whitelist System Cog
Enforces strict server access control.
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List
import logging

from utils.embeds import ModEmbed, Colors
from utils.checks import is_admin, is_mod
from config import Config

logger = logging.getLogger(__name__)


class Whitelist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ==================== COMMANDS ====================

    whitelist_group = app_commands.Group(
        name="whitelist",
        description="🔒 Whitelist system configuration",
        guild_only=True
    )

    @whitelist_group.command(name="enable", description="Enable strict whitelist mode (Kicks non-whitelisted users)")
    @is_admin()
    async def whitelist_enable(self, interaction: discord.Interaction):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["whitelist_enabled"] = True
        await self.bot.db.update_settings(interaction.guild_id, settings)
        
        embed = ModEmbed.success(
            "Whitelist Enabled",
            "Strict mode is now **ACTIVE**. Users not on the whitelist will be kicked upon joining."
        )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="disable", description="Disable strict whitelist mode")
    @is_admin()
    async def whitelist_disable(self, interaction: discord.Interaction):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["whitelist_enabled"] = False
        await self.bot.db.update_settings(interaction.guild_id, settings)
        
        embed = ModEmbed.warning(
            "Whitelist Disabled",
            "Strict mode is now **INACTIVE**. Anyone can join the server."
        )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="add", description="Add a user to the whitelist")
    @app_commands.describe(user="The user to whitelist")
    @is_admin()
    async def whitelist_add(self, interaction: discord.Interaction, user: discord.User):
        success = await self.bot.db.add_whitelist(
            interaction.guild_id, 
            user.id, 
            interaction.user.id
        )
        
        if success:
            embed = ModEmbed.success(
                "User Whitelisted",
                f"{user.mention} (`{user.id}`) has been added to the whitelist."
            )
        else:
            embed = ModEmbed.warning(
                "Already Whitelisted",
                f"{user.mention} is already on the whitelist."
            )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="remove", description="Remove a user from the whitelist")
    @app_commands.describe(user="The user to remove")
    @is_admin()
    async def whitelist_remove(self, interaction: discord.Interaction, user: discord.User):
        success = await self.bot.db.remove_whitelist(interaction.guild_id, user.id)
        
        if success:
            embed = ModEmbed.success(
                "User Removed",
                f"{user.mention} (`{user.id}`) has been removed from the whitelist."
            )
        else:
            embed = ModEmbed.error(
                "Not Found",
                f"{user.mention} is not on the whitelist."
            )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="list", description="View all whitelisted users")
    @is_mod()
    async def whitelist_list(self, interaction: discord.Interaction):
        user_ids = await self.bot.db.get_whitelist(interaction.guild_id)
        
        if not user_ids:
            embed = ModEmbed.info(
                "Whitelist Empty",
                "There are no users on the whitelist."
            )
            return await interaction.response.send_message(embed=embed)

        # Pagination logic (simple for now, lists up to 50)
        users = []
        for uid in user_ids[:50]:
            user = self.bot.get_user(uid)
            name = f"{user.name} ({user.id})" if user else f"Unknown User ({uid})"
            users.append(f"• {name}")
            
        description = "\n".join(users)
        if len(user_ids) > 50:
            description += f"\n\n*...and {len(user_ids) - 50} more*"
            
        embed = discord.Embed(
            title=f"📜 Whitelist ({len(user_ids)} users)",
            description=description,
            color=Colors.INFO
        )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="check", description="Check if a user is whitelisted")
    @is_mod()
    async def whitelist_check(self, interaction: discord.Interaction, user: discord.User):
        is_whitelisted = await self.bot.db.is_whitelisted(interaction.guild_id, user.id)
        
        if is_whitelisted:
            embed = ModEmbed.success(
                "Whitelisted",
                f"✅ {user.mention} is on the whitelist."
            )
        else:
            embed = ModEmbed.error(
                "Not Whitelisted",
                f"❌ {user.mention} is **NOT** on the whitelist."
            )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="clear", description="Clear the entire whitelist")
    @is_admin()
    async def whitelist_clear(self, interaction: discord.Interaction):
        # Confirmation dialog could be added here, but for now simple command
        count = await self.bot.db.clear_whitelist(interaction.guild_id)
        
        embed = ModEmbed.success(
            "Whitelist Cleared",
            f"Removed **{count}** users from the whitelist."
        )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="mass_add", description="Whitelist all members with a specific role")
    @app_commands.describe(role="The role to whitelist")
    @is_admin()
    async def whitelist_mass_add(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer()
        
        added_count = 0
        already_whitelisted = 0
        
        for member in role.members:
            if member.bot:
                continue
            
            if await self.bot.db.add_whitelist(interaction.guild_id, member.id, interaction.user.id):
                added_count += 1
            else:
                already_whitelisted += 1
        
        embed = ModEmbed.success(
            "Mass Add Complete",
            f"Processed role {role.mention}.\n"
            f"✅ **Added:** {added_count}\n"
            f"⏭️ **Skipped:** {already_whitelisted}"
        )
        await interaction.followup.send(embed=embed)

    @whitelist_group.command(name="scan", description="Scan for non-whitelisted users")
    @is_admin()
    async def whitelist_scan(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        settings = await self.bot.db.get_settings(interaction.guild_id)
        immunity_enabled = settings.get("whitelist_immunity", True)
        whitelisted_ids = await self.bot.db.get_whitelist(interaction.guild_id)
        whitelisted_set = set(whitelisted_ids)
        
        detected = []
        
        for member in interaction.guild.members:
            if member.bot:
                continue
            if member.id == interaction.guild.owner_id:
                continue
            if immunity_enabled and member.guild_permissions.administrator:
                continue
            
            if member.id not in whitelisted_set:
                detected.append(member)
        
        if not detected:
            embed = ModEmbed.success(
                "Scan Complete",
                "✅ All members are whitelisted or immune."
            )
            return await interaction.followup.send(embed=embed)
            
        description = "\n".join([f"• {m.mention} ({m.id})" for m in detected[:20]])
        if len(detected) > 20:
            description += f"\n...and {len(detected) - 20} more"
            
        embed = ModEmbed.warning(
            f"⚠️ Detected {len(detected)} Non-Whitelisted Users",
            description
        )
        
        # Add a Kick All button
        view = KickAllView(self.bot, detected, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)

    @whitelist_group.command(name="immunity", description="Toggle Admin immunity (Admins bypass whitelist)")
    @app_commands.describe(enabled="Whether admins are immune to whitelist checks")
    @is_admin()
    async def whitelist_immunity(self, interaction: discord.Interaction, enabled: bool):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["whitelist_immunity"] = enabled
        await self.bot.db.update_settings(interaction.guild_id, settings)
        
        status = "enabled" if enabled else "disabled"
        embed = ModEmbed.success(
            "Immunity Updated",
            f"Admin immunity is now **{status}**."
        )
        await interaction.response.send_message(embed=embed)

    @whitelist_group.command(name="dm_join", description="Toggle DMing users when kicked")
    @app_commands.describe(enabled="Whether to DM users before kicking them")
    @is_admin()
    async def whitelist_dm_join(self, interaction: discord.Interaction, enabled: bool):
        settings = await self.bot.db.get_settings(interaction.guild_id)
        settings["whitelist_dm_join"] = enabled
        await self.bot.db.update_settings(interaction.guild_id, settings)
        
        status = "enabled" if enabled else "disabled"
        embed = ModEmbed.success(
            "DM Settings Updated",
            f"DMing users on kick is now **{status}**."
        )
        await interaction.response.send_message(embed=embed)


    # ==================== EVENTS ====================

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Check whitelist on join"""
        if member.bot:
            return

        settings = await self.bot.db.get_settings(member.guild.id)
        if not settings.get("whitelist_enabled", False):
            return

        # Immunity Checks
        if member.id == member.guild.owner_id:
            return
            
        if settings.get("whitelist_immunity", True) and member.guild_permissions.administrator:
            return

        is_whitelisted = await self.bot.db.is_whitelisted(member.guild.id, member.id)
        
        if not is_whitelisted:
            try:
                # Try to DM user if enabled (default True)
                if settings.get("whitelist_dm_join", True):
                    try:
                        dm_embed = discord.Embed(
                            title="Connection Rejected", 
                            description=f"You are not whitelisted on **{member.guild.name}**.",
                            color=Colors.ERROR
                        )
                        await member.send(embed=dm_embed)
                    except Exception:
                        pass

                await member.kick(reason="[Whitelist] User not whitelisted")
                logger.info(f"Kicked non-whitelisted user {member} from {member.guild}")
                
            except discord.Forbidden:
                logger.warning(f"Failed to kick {member} from {member.guild} (Missing Permissions)")
            except Exception as e:
                logger.error(f"Error enforcing whitelist: {e}")


class KickAllView(discord.ui.View):
    def __init__(self, bot, members_to_kick, author_id):
        super().__init__(timeout=60)
        self.bot = bot
        self.members = members_to_kick
        self.author_id = author_id
        self.kicked_count = 0

    @discord.ui.button(label="Kick All Detected", style=discord.ButtonStyle.danger, emoji="👢")
    async def kick_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("You cannot use this button.", ephemeral=True)
            
        await interaction.response.defer()
        button.disabled = True
        await interaction.edit_original_response(view=self)
        
        failed = 0
        for member in self.members:
            try:
                await member.kick(reason="[Whitelist Scan] User not whitelisted")
                self.kicked_count += 1
            except Exception:
                failed += 1
                
        embed = ModEmbed.success(
            "Mass Kick Complete",
            f"👢 Kicked **{self.kicked_count}** users.\n❌ Failed to kick **{failed}** users."
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Whitelist(bot))
