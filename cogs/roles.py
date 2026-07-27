"""
Role Management Commands (WITH SECURITY FIXES)
Refactored to use Subcommands (/role create, /role delete, etc.)
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional, Literal
import asyncio
import re
from utils.embeds import ModEmbed
from utils.checks import is_mod, is_admin, is_bot_owner_id
from utils.server_setup import module_enabled
from utils.status_emojis import apply_status_emoji_overrides
from config import Config


def _is_server_admin_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    return bool(
        is_bot_owner_id(interaction.user.id)
        or
        interaction.user.id == interaction.guild.owner_id
        or interaction.user.guild_permissions.administrator
    )


class Roles(commands.Cog):
    # Define group at class level - discord.py will auto-register this
    mod_role_group = app_commands.Group(name="role", description="🎭 Manage server roles")
    allowlist_group = app_commands.Group(name="allowlist", description="🔐 Configure roles bot owners may assign", parent=mod_role_group)
    
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # Auto-add Staff role when any mod role is granted.
        try:
            if before.roles == after.roles:
                return
            if after.bot or not after.guild:
                return

            settings = await self.bot.db.get_settings(after.guild.id)
            staff_role_id = settings.get("staff_role")
            if not staff_role_id:
                return
            staff_role = after.guild.get_role(int(staff_role_id))
            if not staff_role or staff_role in after.roles:
                return

            mod_role_ids: set[int] = set()
            for key in ["owner_role", "manager_role", "admin_role", "supervisor_role", "senior_mod_role", "mod_role", "trial_mod_role"]:
                rid = settings.get(key)
                if isinstance(rid, int) and rid > 0:
                    mod_role_ids.add(rid)

            if not mod_role_ids:
                return

            added_ids = {r.id for r in after.roles if r not in before.roles}
            if not (added_ids & mod_role_ids):
                return

            bot_member = after.guild.me
            if not bot_member or not bot_member.guild_permissions.manage_roles:
                return
            if staff_role >= bot_member.top_role:
                return

            await after.add_roles(staff_role, reason="Auto-add staff role when mod role assigned")
        except Exception:
            return

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            if member.bot or not member.guild:
                return

            settings = await self.bot.db.get_settings(member.guild.id)
            if module_enabled(settings, "verification", False):
                return

            auto_role_id = settings.get("auto_role")
            if not auto_role_id:
                return

            role = member.guild.get_role(int(auto_role_id))
            bot_member = member.guild.me
            if (
                role is None
                or bot_member is None
                or not bot_member.guild_permissions.manage_roles
                or role >= bot_member.top_role
                or role in member.roles
            ):
                return

            await member.add_roles(role, reason="Auto role on join")
        except Exception:
            return

    # Helper methods
    @staticmethod
    def _parse_role_id(role_input: str) -> Optional[int]:
        role_input = (role_input or "").strip()
        if not role_input:
            return None
        mention_match = re.fullmatch(r"<@&(\d+)>", role_input)
        if mention_match:
            try:
                return int(mention_match.group(1))
            except Exception:
                return None
        if role_input.isdigit():
            try:
                return int(role_input)
            except Exception:
                return None
        return None

    async def _owner_override_for_role(self, interaction: discord.Interaction, role: discord.Role) -> tuple[bool, str]:
        if not is_bot_owner_id(interaction.user.id):
            return False, ""
        return True, ""

    async def cog_load(self):
        pass
            
    async def cog_unload(self):
        pass

    async def _safe_send(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        ephemeral: bool = False,
    ):
        """Reply via initial response or followup depending on interaction state."""
        if interaction.guild is not None and embed is not None:
            try:
                embed = await apply_status_emoji_overrides(embed, interaction.guild)
            except Exception:
                pass
        if interaction.response.is_done():
            return await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        return await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    def can_manage_role(self, moderator: discord.Member, target_role: discord.Role) -> tuple[bool, str]:
        if is_bot_owner_id(moderator.id) or moderator.id == moderator.guild.owner_id:
            return True, ""
        if target_role >= moderator.top_role:
            return False, "You cannot manage this role as it's higher than or equal to your highest role."
        if target_role.permissions.administrator and not moderator.guild_permissions.administrator:
            return False, "You cannot manage roles with Administrator permission."
        dangerous_perms = ['ban_members', 'kick_members', 'manage_guild', 'manage_roles', 'manage_channels']
        for perm in dangerous_perms:
            if getattr(target_role.permissions, perm) and not getattr(moderator.guild_permissions, perm):
                return False, f"You cannot manage roles with `{perm.replace('_', ' ').title()}` permission."
        return True, ""

    # ==================== /role GROUP ====================
    # mod_role_group defined in __init__ to avoid registration conflicts

    # Decorators need to refer to self.mod_role_group but we can't do that easily inside the class body
    # Standard discord.py pattern for groups in __init__ is to add them manually to tree,
    # OR define them as separate classes.
    # But wait, we can't decorate methods with `self.mod_role_group.command` inside the class body before __init__ runs.
    # Actually, we CAN keep the class attribute for defining structure, but we need to prevent AUTO-registration.
    # Discord.py auto-registers class attributes that are app_commands.Group.
    # To fix this, we should RENAME it to something else that discord.py doesn't auto-scan?
    # No, the best way:
    # 1. Define commands as methods
    # 2. In __init__, add them to the group
    # OR:
    # Use the `guilds` parameter trick? No.
    # 
    # Let's use the pattern: define the group as a class attribute but override cog_load to handle the registration conflict?
    # No, that's what we JUST tried to fix by removing the class attribute.
    # 
    # If I remove the class attribute `mod_role_group`, then `@mod_role_group.command` decorators will FAIL.
    # I must keep `mod_role_group` available for decorators.
    # 
    # SOLUTION:
    # Keep the class attribute BUT name it `role_group_CLASS` temporarily?
    # Actually, for Roles cog, the commands are defined using `@mod_role_group.command`.
    # If `mod_role_group` is not defined at class level, the decorators will error `NameError`.
    # 
    # So I CANNOT just move it to __init__ if decorators depend on it!
    # 
    # ALTERNATIVE FIX found for moderation.py (which used subgroups): Subgroups are easy to add in __init__.
    # But roles.py has direct commands on the group.
    # 
    # Correct Refactor for Roles:
    # 1. Use `app_commands.command` decorators directly (not group decorators).
    # 2. In `__init__`, create the Group and `add_command` the methods.
    
    # REVERTING THE STRATEGY FOR ROLES.PY decorators:
    # Instead of `@mod_role_group.command`, use `@app_commands.command` and name it properly.
    # Then in __init__, add them.
    
    # Actually, simpler way: define a nested Class for the Group?
    # OR: Just manually handle the error in cog_load but keep the class attribute?
    # 
    # If I keep the class attribute, discord.py TRIES to register it.
    # I need to prevent discord.py from auto-registering it.
    # 
    # If I rename it to `_mod_role_group` (private), discord.py might ignore it?
    # No, it scans all Group attributes.
    # 
    # Let's go with the manual construction in __init__:
    # Change `@mod_role_group.command` to `@app_commands.command`
    # And manually attach in __init__.
    
    @mod_role_group.command(name="create", description="✨ Create a new role with detailed options")
    @app_commands.describe(
        name="Name of the new role",
        color="Hex color (e.g. #FF0000) or name (e.g. Red)",
        hoist="Display role separately in online list?",
        mentionable="Can anyone mention this role?",
        position="Position (1 is lowest, higher is higher up)", 
        reason="Audit log reason"
    )
    @is_admin()
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        color: Optional[str] = None,
        hoist: bool = False,
        mentionable: bool = False,
        position: Optional[int] = None,
        reason: Optional[str] = None
    ):
        if not interaction.guild:
            return await interaction.response.send_message(embed=ModEmbed.error("Error", "Guild only."), ephemeral=True)
        
        await interaction.response.defer()

        # Parse color
        role_color = discord.Color.default()
        if color:
            try:
                color = color.strip().replace("#", "")
                role_color = discord.Color(int(color, 16))
            except Exception:
                pass # Default

        try:
            role = await interaction.guild.create_role(
                name=name,
                color=role_color,
                hoist=hoist,
                mentionable=mentionable,
                reason=f"{reason if reason else 'Created via /role create'} by {interaction.user}"
            )

            # Attempt position move (best effort)
            if position is not None and position > 0:
                try:
                    await role.edit(position=position)
                except Exception:
                    pass # Often fails due to hierarchy

            embed = ModEmbed.success("Role Created", f"Successfully created {role.mention}")
            embed.add_field(name="Properties", value=f"Color: {role.color}\nHoist: {role.hoist}\nMentionable: {role.mentionable}")
            await interaction.followup.send(embed=embed)

        except discord.Forbidden:
             await interaction.followup.send(embed=ModEmbed.error("Permission Denied", "I do not have permission to create roles."))
        except Exception as e:
             await interaction.followup.send(embed=ModEmbed.error("Error", str(e)))

    @mod_role_group.command(name="delete", description="🗑️ Delete a role")
    @app_commands.describe(role="The role to delete", reason="Audit log reason")
    @is_admin()
    async def delete(self, interaction: discord.Interaction, role: discord.Role, reason: Optional[str] = None):
        if not interaction.guild: return
        
        can_manage, error = self.can_manage_role(interaction.user, role)
        if not can_manage:
             return await interaction.response.send_message(embed=ModEmbed.error("Permission Denied", error), ephemeral=True)

        # Safety checks
        if role.managed:
             return await interaction.response.send_message(embed=ModEmbed.error("Error", "Cannot delete managed roles."), ephemeral=True)
        if role >= interaction.guild.me.top_role:
             return await interaction.response.send_message(embed=ModEmbed.error("Error", "Role is higher than me."), ephemeral=True)

        await interaction.response.defer()
        try:
            name = role.name
            await role.delete(reason=f"{reason if reason else 'Deleted via /role delete'} by {interaction.user}")
            await interaction.followup.send(embed=ModEmbed.success("Role Deleted", f"Deleted role **{name}**"))
        except Exception as e:
            await interaction.followup.send(embed=ModEmbed.error("Error", str(e)))

    @mod_role_group.command(name="add", description="➕ Add a role to a user")
    @is_mod()
    async def role_add(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        can_manage, error = self.can_manage_role(interaction.user, role)
        if not can_manage:
             return await self._safe_send(interaction, embed=ModEmbed.error("Permission Denied", error), ephemeral=True)
        
        if role in user.roles:
            return await self._safe_send(interaction, embed=ModEmbed.error("Error", "User already has this role."), ephemeral=True)

        await interaction.response.defer()

        try:
            await user.add_roles(role, reason=f"Added by {interaction.user}")
            await self._safe_send(
                interaction,
                embed=ModEmbed.success("Role Added", f"Added {role.mention} to {user.mention}")
            )
        except Exception as e:
            await self._safe_send(interaction, embed=ModEmbed.error("Error", str(e)), ephemeral=True)

    @mod_role_group.command(name="remove", description="➖ Remove a role from a user")
    @is_mod()
    async def role_remove(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        can_manage, error = self.can_manage_role(interaction.user, role)
        if not can_manage:
             return await self._safe_send(interaction, embed=ModEmbed.error("Permission Denied", error), ephemeral=True)

        if role not in user.roles:
            return await self._safe_send(interaction, embed=ModEmbed.error("Error", "User does not have this role."), ephemeral=True)

        await interaction.response.defer()

        try:
            await user.remove_roles(role, reason=f"Removed by {interaction.user}")
            await self._safe_send(
                interaction,
                embed=ModEmbed.success("Role Removed", f"Removed {role.mention} from {user.mention}")
            )
        except Exception as e:
            await self._safe_send(interaction, embed=ModEmbed.error("Error", str(e)), ephemeral=True)

    @mod_role_group.command(name="all", description="👥 Add a role to ALL humans (Mass add)")
    @is_admin()
    @app_commands.describe(role="Role to mass add", reason="Audit log reason")
    async def role_all(self, interaction: discord.Interaction, role: discord.Role, reason: Optional[str] = None):
        if not interaction.guild: return
        
        # Safety checks
        if role.managed:
             return await interaction.response.send_message(embed=ModEmbed.error("Error", "Cannot assign managed roles."), ephemeral=True)
        if role >= interaction.guild.me.top_role:
             return await interaction.response.send_message(embed=ModEmbed.error("Error", "Role is higher than me."), ephemeral=True)
        if role.permissions.administrator and not is_bot_owner_id(interaction.user.id):
             return await interaction.response.send_message(embed=ModEmbed.error("Error", "Cannot mass-assign Admin roles."), ephemeral=True)

        await interaction.response.defer()
        
        members = [m for m in interaction.guild.members if not m.bot and role not in m.roles]
        if not members:
            return await interaction.followup.send(embed=ModEmbed.error("Error", "No eligible members found."))

        await interaction.followup.send(embed=ModEmbed.info("Processing", f"Adding {role.mention} to {len(members)} members... this may take time."))
        
        count = 0
        for member in members:
            try:
                await member.add_roles(role, reason=reason)
                count += 1
                await asyncio.sleep(1) # Rate limit protection
            except Exception:
                pass
                
        await interaction.followup.send(embed=ModEmbed.success("Complete", f"Added {role.mention} to {count} members."))

    @mod_role_group.command(name="info", description="ℹ️ Get detailed role info")
    async def role_info(self, interaction: discord.Interaction, role: discord.Role):
        embed = discord.Embed(title=f"Role: {role.name}", color=role.color)
        embed.add_field(name="ID", value=role.id)
        embed.add_field(name="Members", value=str(len(role.members)))
        embed.add_field(name="Created", value=f"<t:{int(role.created_at.timestamp())}:R>")
        embed.add_field(name="Position", value=str(role.position))
        embed.add_field(name="Hoisted", value=str(role.hoist))
        embed.add_field(name="Mentionable", value=str(role.mentionable))
        embed.add_field(name="Managed", value=str(role.managed))
        
        perms = [p[0].replace('_', ' ').title() for p in role.permissions if p[1]]
        if "Administrator" in perms: perms = ["Administrator"]
        embed.add_field(name="Key Permissions", value=", ".join(perms[:10]) or "None", inline=False)
        
        await interaction.response.send_message(embed=embed)

    @mod_role_group.command(name="auto", description="Set the role given to new members when verification is off")
    @app_commands.describe(role="Role to auto-assign on join", clear="Clear the current auto role")
    @is_admin()
    async def role_auto(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
        clear: bool = False,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Error", "Guild only."),
                ephemeral=True,
            )

        settings = await self.bot.db.get_settings(interaction.guild_id)

        if clear:
            settings["auto_role"] = None
            await self.bot.db.update_settings(interaction.guild_id, settings)
            return await self._safe_send(
                interaction,
                embed=ModEmbed.success("Auto Role Cleared", "New members will no longer receive an automatic join role."),
                ephemeral=True,
            )

        if role is None:
            current_role = interaction.guild.get_role(int(settings.get("auto_role", 0) or 0))
            description = (
                f"Current auto role: {current_role.mention if current_role else '`Not set`'}\n"
                f"Verification: {'ON (autorole overridden by unverified)' if module_enabled(settings, 'verification', False) else 'OFF'}"
            )
            return await self._safe_send(
                interaction,
                embed=ModEmbed.info("Auto Role", description),
                ephemeral=True,
            )

        can_manage, error = self.can_manage_role(interaction.user, role)
        if not can_manage:
            return await self._safe_send(interaction, embed=ModEmbed.error("Permission Denied", error), ephemeral=True)

        bot_member = interaction.guild.me
        if role.managed:
            return await self._safe_send(
                interaction,
                embed=ModEmbed.error("Not Allowed", "Managed roles cannot be used as the join autorole."),
                ephemeral=True,
            )
        if bot_member is None or not bot_member.guild_permissions.manage_roles or role >= bot_member.top_role:
            return await self._safe_send(
                interaction,
                embed=ModEmbed.error("Permission Denied", "I cannot assign that role because it is above my highest role."),
                ephemeral=True,
            )

        settings["auto_role"] = role.id
        await self.bot.db.update_settings(interaction.guild_id, settings)

        description = f"New members will receive {role.mention} when verification is off."
        if module_enabled(settings, "verification", False):
            description += "\nVerification is currently on, so new joins will still receive the unverified role instead."

        await self._safe_send(
            interaction,
            embed=ModEmbed.success("Auto Role Updated", description),
            ephemeral=True,
        )

    # ==================== ALLOWLIST SUBGROUP ====================
    # allowlist_group moved to __init__
    
    @allowlist_group.command(name="add", description="Add role to allowlist")
    @is_admin()
    async def allowlist_add(self, interaction: discord.Interaction, role: discord.Role):
        # Check server admin permission
        if not _is_server_admin_check(interaction):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "You need to be a server admin to use this command."),
                ephemeral=True
            )
        
        if role.permissions.administrator:
            return await interaction.response.send_message(
                embed=ModEmbed.error("Not Allowed", "Administrator roles cannot be allowlisted."),
                ephemeral=True,
            )
        settings = await self.bot.db.get_settings(interaction.guild_id)
        allowlist = settings.get("owner_role_allowlist", [])
        if not isinstance(allowlist, list): allowlist = []
        if role.id not in allowlist:
            allowlist.append(role.id)
            settings["owner_role_allowlist"] = allowlist
            await self.bot.db.update_settings(interaction.guild_id, settings)
        await interaction.response.send_message(
            embed=ModEmbed.success("Allowlisted", f"Bot owners can now assign {role.mention}."),
            ephemeral=True,
        )

    @allowlist_group.command(name="remove", description="Remove a role from the allowlist")
    @app_commands.describe(role="Role to remove")
    async def allowlist_remove(self, interaction: discord.Interaction, role: discord.Role):
        # Check server admin permission
        if not _is_server_admin_check(interaction):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "You need to be a server admin to use this command."),
                ephemeral=True
            )
            
        settings = await self.bot.db.get_settings(interaction.guild_id)
        allowlist = settings.get("owner_role_allowlist", [])
        if role.id in allowlist:
            allowlist.remove(role.id)
            settings["owner_role_allowlist"] = allowlist
            await self.bot.db.update_settings(interaction.guild_id, settings)
        await interaction.response.send_message(
            embed=ModEmbed.success("Removed", f"{role.mention} removed from allowlist."),
            ephemeral=True,
        )

    @allowlist_group.command(name="list", description="List allowlisted roles")
    async def allowlist_list(self, interaction: discord.Interaction):
        # Check server admin permission (optional for list? keeping it consistent)
        if not _is_server_admin_check(interaction):
            return await interaction.response.send_message(
                embed=ModEmbed.error("Permission Denied", "You need to be a server admin to use this command."),
                ephemeral=True
            )

        settings = await self.bot.db.get_settings(interaction.guild_id)
        allowlist = settings.get("owner_role_allowlist", [])
        if not allowlist:
            return await interaction.response.send_message(embed=ModEmbed.info("Allowlist", "No roles allowlisted."), ephemeral=True)
        roles = [interaction.guild.get_role(rid) for rid in allowlist]
        roles_str = ", ".join([r.mention for r in roles if r])
        await interaction.response.send_message(embed=ModEmbed.info("Allowlisted Roles", roles_str), ephemeral=True)

async def setup(bot):
    await bot.add_cog(Roles(bot))
