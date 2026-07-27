"""
Role management handlers — add, remove, create, delete, edit roles.
"""
from __future__ import annotations

from typing import Any, Dict

import discord

from ..context import ToolContext, ToolResult, parse_hex_color
from ..registry import ToolRegistry
from ..types import ToolType


@ToolRegistry.register(
    ToolType.ADD_ROLE,
    display_name="Add Role",
    color=discord.Color.green(),
    emoji="+",
    required_permission="manage_roles",
    category="roles",
)
async def handle_add_role(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Target user not found.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot change roles for {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot change roles for {target.display_name}; their role is above mine.")

    role = await ctx.resolve_role()
    if not role:
        return ToolResult.fail(f"Role `{ctx.arg('role_name')}` not found.")
    if role.is_default() or role.managed:
        return ToolResult.fail(f"Role `{role.name}` cannot be assigned manually.")

    if not ctx.cog.can_manage_role(ctx.actor, role):
        return ToolResult.fail(f"Cannot assign `{role.name}` - it's above your top role.")
    if not bot_member or not ctx.cog.can_manage_role(bot_member, role):
        return ToolResult.fail(f"Cannot assign `{role.name}` - it's above my top role.")

    reason = ctx.str_arg("reason")
    await target.add_roles(role, reason=f"AI Mod ({ctx.actor}): {reason}")

    embed = discord.Embed(description=f"Added {role.mention} to {target.mention}", color=discord.Color.green())
    return ToolResult.ok("Role added.", embed=embed)


@ToolRegistry.register(
    ToolType.REMOVE_ROLE,
    display_name="Remove Role",
    color=discord.Color.orange(),
    emoji="-",
    required_permission="manage_roles",
    category="roles",
)
async def handle_remove_role(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Target user not found.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot change roles for {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot change roles for {target.display_name}; their role is above mine.")

    role = await ctx.resolve_role()
    if not role:
        return ToolResult.fail(f"Role `{ctx.arg('role_name')}` not found.")
    if role.is_default() or role.managed:
        return ToolResult.fail(f"Role `{role.name}` cannot be removed manually.")

    if not ctx.cog.can_manage_role(ctx.actor, role):
        return ToolResult.fail(f"Cannot remove `{role.name}` - it's above your top role.")
    if not bot_member or not ctx.cog.can_manage_role(bot_member, role):
        return ToolResult.fail(f"Cannot remove `{role.name}` - it's above my top role.")

    reason = ctx.str_arg("reason")
    await target.remove_roles(role, reason=f"AI Mod ({ctx.actor}): {reason}")

    embed = discord.Embed(description=f"Removed {role.mention} from {target.mention}", color=discord.Color.orange())
    return ToolResult.ok("Role removed.", embed=embed)


@ToolRegistry.register(
    ToolType.CREATE_ROLE,
    display_name="Create Role",
    color=discord.Color.blue(),
    emoji="*",
    required_permission="manage_roles",
    category="roles",
)
async def handle_create_role(ctx: ToolContext) -> ToolResult:
    name = ctx.arg("name")
    if not name:
        return ToolResult.fail("Role name is required.")

    color = parse_hex_color(ctx.arg("color_hex"))
    hoist = ctx.bool_arg("hoist")
    reason = ctx.str_arg("reason")

    role = await ctx.guild.create_role(
        name=name, color=color, hoist=hoist,
        reason=f"AI Mod ({ctx.actor}): {reason}",
    )
    embed = discord.Embed(description=f"Created role {role.mention}", color=color)
    return ToolResult.ok("Role created.", embed=embed)


@ToolRegistry.register(
    ToolType.DELETE_ROLE,
    display_name="Delete Role",
    color=discord.Color.red(),
    emoji="Delete",
    required_permission="manage_roles",
    category="roles",
)
async def handle_delete_role(ctx: ToolContext) -> ToolResult:
    role = await ctx.resolve_role()
    if not role:
        return ToolResult.fail(f"Role `{ctx.arg('role_name')}` not found.")
    if role.is_default() or role.managed:
        return ToolResult.fail(f"Role `{role.name}` cannot be deleted.")
    if not ctx.cog.can_manage_role(ctx.actor, role):
        return ToolResult.fail("That role is above you in the hierarchy.")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_manage_role(bot_member, role):
        return ToolResult.fail("That role is above me in the hierarchy.")

    await role.delete(reason=f"AI Mod ({ctx.actor}): {ctx.str_arg('reason')}")
    embed = discord.Embed(description=f"Deleted role **{role.name}**", color=discord.Color.red())
    return ToolResult.ok("Role deleted.", embed=embed)


@ToolRegistry.register(
    ToolType.EDIT_ROLE,
    display_name="Edit Role",
    color=discord.Color.blue(),
    emoji="Edit",
    required_permission="manage_roles",
    category="roles",
)
async def handle_edit_role(ctx: ToolContext) -> ToolResult:
    role = await ctx.resolve_role()
    if not role:
        return ToolResult.fail(f"Role `{ctx.arg('role_name')}` not found.")
    if role.is_default() or role.managed:
        return ToolResult.fail(f"Role `{role.name}` cannot be edited.")
    if not ctx.cog.can_manage_role(ctx.actor, role):
        return ToolResult.fail("That role is above you in the hierarchy.")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_manage_role(bot_member, role):
        return ToolResult.fail("That role is above me in the hierarchy.")

    kwargs: Dict[str, Any] = {}
    if "new_name" in ctx.args:
        kwargs["name"] = ctx.args["new_name"]
    if "new_color" in ctx.args:
        kwargs["color"] = parse_hex_color(ctx.args["new_color"])

    if not kwargs:
        return ToolResult.fail("Nothing to edit - provide new_name and/or new_color.")

    await role.edit(**kwargs, reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok(f"Role **{role.name}** updated.")
