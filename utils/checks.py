"""
Permission checks for commands
"""

import os
import re
import discord
from discord import app_commands
from discord.ext import commands
from typing import Callable
from functools import wraps



def get_owner_ids() -> set[int]:
    """Return bot owner IDs from `OWNER_IDS`/`OWNER_ID` env vars.

    Owner IDs come ONLY from configuration — there is deliberately no
    hardcoded fallback. A baked-in ID cannot be revoked without a code
    change and silently grants that account full, permission-check-bypassing
    power (including the ``execute_python`` gate), so it is treated as a
    backdoor and excluded.
    """
    raw = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID") or ""
    owner_ids: set[int] = set()

    for part in re.split(r"[,\s]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        try:
            owner_ids.add(int(part))
        except ValueError:
            continue

    return owner_ids


def is_bot_owner_id(user_id: int) -> bool:
    return user_id in get_owner_ids()


async def is_bot_owner(interaction: discord.Interaction) -> bool:
    """Return True when the interaction user is treated as the bot owner."""
    if is_bot_owner_id(interaction.user.id):
        return True

    owner_ids = getattr(interaction.client, "owner_ids", None)
    if owner_ids and interaction.user.id in owner_ids:
        return True

    is_owner = getattr(interaction.client, "is_owner", None)
    if is_owner is None:
        return False

    try:
        return bool(await is_owner(interaction.user))
    except Exception:
        return False


def is_mod():
    """Check if user is a moderator"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if await is_bot_owner(interaction):
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        if interaction.user.guild_permissions.kick_members:
            return True
        if interaction.user.guild_permissions.ban_members:
            return True
        if interaction.user.guild_permissions.manage_messages:
            return True
        
        # Check for mod roles from database
        settings = await interaction.client.db.get_settings(interaction.guild_id)
        mod_roles = settings.get("mod_roles", [])
        user_role_ids = [r.id for r in interaction.user.roles]
        
        if any(role_id in user_role_ids for role_id in mod_roles):
            return True
        
        raise app_commands.MissingPermissions(['Moderator'])
    
    return app_commands.check(predicate)

def is_owner_only():
    """Check if user is a bot owner - for owner-only commands"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if await is_bot_owner(interaction):
            return True
        raise app_commands.MissingPermissions(['Bot Owner'])
    return app_commands.check(predicate)

def is_senior_mod():
    """Check if user is a senior moderator"""
    async def predicate(interaction:  discord.Interaction) -> bool:
        if await is_bot_owner(interaction):
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        if interaction.user.guild_permissions.ban_members:
            return True
        
        settings = await interaction.client.db.get_settings(interaction.guild_id)
        senior_mod_role = settings.get("senior_mod_role")
        admin_roles = settings.get("admin_roles", [])
        user_role_ids = [r.id for r in interaction.user.roles]
        
        if senior_mod_role and senior_mod_role in user_role_ids:
            return True
        if any(role_id in user_role_ids for role_id in admin_roles):
            return True
        
        raise app_commands.MissingPermissions(['Senior Moderator'])
    
    return app_commands.check(predicate)

def is_admin():
    """Check if user is an admin"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if await is_bot_owner(interaction):
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        
        settings = await interaction.client.db.get_settings(interaction.guild_id)
        admin_roles = settings.get("admin_roles", [])
        manager_role = settings.get("manager_role")
        user_role_ids = [r.id for r in interaction.user.roles]
        
        if manager_role and manager_role in user_role_ids:
            return True
        if any(role_id in user_role_ids for role_id in admin_roles):
            return True
        
        raise app_commands.MissingPermissions(['Administrator', 'Manager'])
    
    return app_commands.check(predicate)

def has_permissions_or_owner(**perms):
    """Prefix-command permission check that always allows configured bot owners."""
    base_check = commands.has_permissions(**perms)

    async def predicate(ctx: commands.Context) -> bool:
        if is_bot_owner_id(ctx.author.id):
            return True
        return await base_check.predicate(ctx)

    return commands.check(predicate)

def can_moderate(target:  discord.Member, moderator: discord.Member) -> bool:
    """Check if moderator can moderate the target"""
    # Bot owner can moderate anyone (subject to Discord's own limitations)
    if is_bot_owner_id(moderator.id):
        return True

    # Can't moderate yourself
    if target.id == moderator.id:
        return False

    # Server owner can moderate anyone (subject to Discord's own limitations)
    if moderator.id == moderator.guild.owner_id:
        return True

    # Can't moderate the bot owner(s)
    if is_bot_owner_id(target.id):
        return False
    
    # Can't moderate the owner
    if target.id == target.guild.owner_id:
        return False
    
    # Can't moderate someone with higher/equal role
    if target.top_role >= moderator.top_role:
        return False
    
    return True

def bot_can_moderate(target: discord.Member, guild: discord.Guild) -> bool:
    """Check if the bot can moderate the target"""
    bot_member = guild.me
    
    # Can't moderate the owner
    if target.id == guild.owner_id:
        return False
    
    # Can't moderate someone with higher/equal role than bot
    if target.top_role >= bot_member.top_role:
        return False
    
    return True
