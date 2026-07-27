"""
Channel management handlers — create, delete, edit, lock, unlock channels.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import discord

from ..context import ToolContext, ToolResult
from ..registry import ToolRegistry
from ..types import ToolType


@ToolRegistry.register(
    ToolType.CREATE_CHANNEL,
    display_name="Create Channel",
    color=discord.Color.green(),
    emoji="Channel",
    required_permission="manage_channels",
    category="channels",
)
async def handle_create_channel(ctx: ToolContext) -> ToolResult:
    name = ctx.arg("name")
    if not name:
        return ToolResult.fail("What should the channel be called? Example: `make a channel called staff-chat`.")

    c_type = str(ctx.arg("type", "text")).lower()
    category: Optional[discord.CategoryChannel] = None
    if cat_name := ctx.arg("category"):
        category = discord.utils.find(
            lambda c: c.name.lower() == str(cat_name).lower(),
            ctx.guild.categories,
        )
        if category is None:
            return ToolResult.fail(f"Category `{cat_name}` not found.")

    reason = f"AI Mod ({ctx.actor}): {ctx.str_arg('reason')}"

    if "voice" in c_type:
        ch = await ctx.guild.create_voice_channel(name, category=category, reason=reason)
    elif "stage" in c_type:
        ch = await ctx.guild.create_stage_channel(name, category=category, reason=reason)
    elif "forum" in c_type:
        ch = await ctx.guild.create_forum_channel(name, category=category, reason=reason)
    else:
        ch = await ctx.guild.create_text_channel(name, category=category, reason=reason)

    embed = discord.Embed(description=f"Created {ch.mention}", color=discord.Color.green())
    return ToolResult.ok("Channel created.", embed=embed)


@ToolRegistry.register(
    ToolType.DELETE_CHANNEL,
    display_name="Delete Channel",
    color=discord.Color.red(),
    emoji="Delete",
    required_permission="manage_channels",
    category="channels",
)
async def handle_delete_channel(ctx: ToolContext) -> ToolResult:
    query = str(ctx.arg("channel_name", "")).strip()
    if not query:
        return ToolResult.fail("Channel name or ID is required.")

    channel: Optional[discord.abc.GuildChannel] = None
    if query.isdigit():
        channel = ctx.guild.get_channel(int(query))
    if not channel:
        channel = discord.utils.find(lambda c: c.name.lower() == query.lower(), ctx.guild.channels)
    if not channel:
        return ToolResult.fail(f"Channel `{query}` not found.")

    name = channel.name
    await channel.delete(reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok(f"Channel `{name}` deleted.")


@ToolRegistry.register(
    ToolType.EDIT_CHANNEL,
    display_name="Edit Channel",
    color=discord.Color.blue(),
    emoji="Edit",
    required_permission="manage_channels",
    category="channels",
)
async def handle_edit_channel(ctx: ToolContext) -> ToolResult:
    channel: discord.abc.GuildChannel = ctx.message.channel  # type: ignore[assignment]
    if channel_name := ctx.arg("channel_name"):
        q = str(channel_name).strip()
        found = (ctx.guild.get_channel(int(q)) if q.isdigit()
                 else discord.utils.find(lambda c: c.name.lower() == q.lower(), ctx.guild.channels))
        if not found:
            return ToolResult.fail(f"Channel `{q}` not found.")
        channel = found

    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
        return ToolResult.fail("Cannot edit that type of channel.")

    kwargs: Dict[str, Any] = {}
    if "new_name" in ctx.args:
        kwargs["name"] = ctx.args["new_name"]
    if "topic" in ctx.args and isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        kwargs["topic"] = ctx.args["topic"]
    if "nsfw" in ctx.args:
        kwargs["nsfw"] = ctx.bool_arg("nsfw")
    if "slowmode" in ctx.args:
        try:
            kwargs["slowmode_delay"] = max(0, min(int(ctx.args["slowmode"]), 21600))
        except (TypeError, ValueError):
            return ToolResult.fail("Invalid slowmode value - must be 0-21600 seconds.")
    if isinstance(channel, discord.VoiceChannel):
        if "bitrate" in ctx.args:
            try:
                kwargs["bitrate"] = int(ctx.args["bitrate"])
            except (TypeError, ValueError):
                return ToolResult.fail("Invalid bitrate.")
        if "user_limit" in ctx.args:
            try:
                kwargs["user_limit"] = max(0, min(int(ctx.args["user_limit"]), 99))
            except (TypeError, ValueError):
                return ToolResult.fail("Invalid user_limit.")

    if not kwargs:
        return ToolResult.fail("Nothing to edit.")

    await channel.edit(**kwargs, reason=f"AI Mod ({ctx.actor})")
    changes = ", ".join(f"{k}={v}" for k, v in kwargs.items())
    return ToolResult.ok(f"Channel updated: `{changes}`.")


@ToolRegistry.register(
    ToolType.LOCK_CHANNEL,
    display_name="Lock Channel",
    color=discord.Color.orange(),
    emoji="Locked",
    required_permission="manage_channels",
    category="channels",
)
async def handle_lock_channel(ctx: ToolContext) -> ToolResult:
    channel = ctx.message.channel
    if not hasattr(channel, "set_permissions"):
        return ToolResult.fail("Cannot lock this channel type.")
    await channel.set_permissions(  # type: ignore[union-attr]
        ctx.guild.default_role, send_messages=False,
        reason=f"Lock by {ctx.actor}",
    )
    return ToolResult.ok("Channel locked.")


@ToolRegistry.register(
    ToolType.UNLOCK_CHANNEL,
    display_name="Unlock Channel",
    color=discord.Color.green(),
    emoji="Unlocked",
    required_permission="manage_channels",
    category="channels",
)
async def handle_unlock_channel(ctx: ToolContext) -> ToolResult:
    channel = ctx.message.channel
    if not hasattr(channel, "set_permissions"):
        return ToolResult.fail("Cannot unlock this channel type.")
    await channel.set_permissions(  # type: ignore[union-attr]
        ctx.guild.default_role, send_messages=None,
        reason=f"Unlock by {ctx.actor}",
    )
    return ToolResult.ok("Channel unlocked.")


@ToolRegistry.register(
    ToolType.LOCK_THREAD,
    display_name="Lock Thread",
    color=discord.Color.orange(),
    emoji="Locked",
    required_permission="manage_threads",
    category="channels",
)
async def handle_lock_thread(ctx: ToolContext) -> ToolResult:
    thread: Optional[discord.Thread] = None
    if isinstance(ctx.message.channel, discord.Thread):
        thread = ctx.message.channel
    elif (hint := ctx.arg("thread_id")):
        try:
            thread = ctx.guild.get_thread(int(hint))
        except (TypeError, ValueError):
            pass

    if not thread:
        return ToolResult.fail("No target thread found. Run this in a thread, or provide a thread ID.")

    await thread.edit(locked=True, archived=True, reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok(f"Thread **{thread.name}** locked.")
