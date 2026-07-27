"""
Member-targeted moderation handlers — warn, timeout, kick, ban, unban, snickname.

These handlers register with the ToolRegistry and are called by the AI routing
pipeline when the LLM decides a member-focused action is needed.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import discord

from ..context import ToolContext, ToolResult, action_embed
from ..registry import ToolRegistry
from ..types import ToolType

logger = logging.getLogger("ModBot.AIModeration.Handlers.Members")

MAX_WARNING_BATCH = 10


def _format_duration(seconds: int) -> str:
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400} day(s)"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600} hour(s)"
    if seconds % 60 == 0:
        return f"{seconds // 60} minute(s)"
    return f"{seconds} second(s)"


@ToolRegistry.register(
    ToolType.WARN,
    display_name="Warn Member",
    color=discord.Color.gold(),
    emoji="Warning",
    required_permission="moderate_members",
    category="members",
)
async def handle_warn(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Could not resolve target member.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot moderate {target.display_name} (role hierarchy).")

    reason = ctx.str_arg("reason")
    warning_count = ctx.int_arg("warning_count", 1)
    if not 1 <= warning_count <= MAX_WARNING_BATCH:
        return ToolResult.fail(
            f"Warning count must be between 1 and {MAX_WARNING_BATCH}."
        )
    db = getattr(ctx.cog.bot, "db", None)
    if not db:
        return ToolResult.fail("Database not available; warning was not recorded.")
    try:
        add_warnings = getattr(db, "add_warnings", None)
        if callable(add_warnings):
            _, total_count = await add_warnings(
                guild_id=ctx.guild.id,
                user_id=target.id,
                moderator_id=ctx.actor.id,
                reason=reason,
                count=warning_count,
            )
        else:
            total_count = 0
            for _ in range(warning_count):
                _, total_count = await db.add_warning(
                    guild_id=ctx.guild.id,
                    user_id=target.id,
                    moderator_id=ctx.actor.id,
                    reason=reason,
                )
    except Exception:
        logger.exception("Failed to record %d warning(s)", warning_count)
        return ToolResult.fail("Database error while recording warnings.")

    embed = action_embed(
        title="Warnings Issued" if warning_count > 1 else "Member Warned",
        color=discord.Color.gold(),
        actor=ctx.actor,
        target=target,
        reason=reason,
        extra={
            "Warnings Issued": str(warning_count),
            "Total Warnings": str(total_count),
        },
    )
    await ctx.cog.log_action(
        message=ctx.message,
        action="warn_member",
        actor=ctx.actor,
        target=target,
        reason=reason,
        decision=ctx.decision,
        extra={"Warnings Issued": warning_count, "Total Warnings": total_count},
    )
    label = "warning" if warning_count == 1 else "warnings"
    return ToolResult.ok(
        f"{warning_count} {label} issued. Total warnings: {total_count}.",
        embed=embed,
    )


@ToolRegistry.register(
    ToolType.GET_WARNINGS,
    display_name="Get Warnings",
    color=discord.Color.blue(),
    emoji="Warnings",
    required_permission="moderate_members",
    category="members",
)
async def handle_get_warnings(ctx: ToolContext) -> ToolResult:
    from datetime import datetime, timezone

    from utils.embeds import Colors

    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Could not resolve target member.")

    db = getattr(ctx.cog.bot, "db", None)
    if not db:
        return ToolResult.fail("Database not available.")

    try:
        warnings = await db.get_warnings(ctx.guild.id, target.id)
    except Exception:
        logger.exception("Failed to fetch warnings")
        return ToolResult.fail("Database error while fetching warnings.")

    if not warnings:
        embed = discord.Embed(
            title=f"Warnings for {target.display_name}",
            description=f"{target.mention} has no warnings.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        return ToolResult.ok(f"{target.display_name} has no warnings.", embed=embed, use_v2=False)

    embed = discord.Embed(
        title=f"Warnings for {target.display_name}",
        description=f"Total: **{len(warnings)}** warning(s)",
        color=Colors.WARNING,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    for warning in warnings[:10]:
        moderator_id = warning.get("moderator_id")
        moderator = ctx.guild.get_member(moderator_id) if moderator_id else None
        moderator_name = moderator.display_name if moderator else f"ID: {moderator_id or 'Unknown'}"
        reason = str(warning.get("reason") or "No reason provided")[:100]
        created_at = str(warning.get("created_at") or "Unknown time")
        embed.add_field(
            name=f"Warning #{warning.get('id', '?')}",
            value=f"**Reason:** {reason}\n**By:** {moderator_name}\n**When:** {created_at}",
            inline=False,
        )
    if len(warnings) > 10:
        embed.set_footer(text=f"Showing 10 of {len(warnings)} warnings")
    return ToolResult.ok(f"Found {len(warnings)} warning(s).", embed=embed, use_v2=False)


@ToolRegistry.register(
    ToolType.TIMEOUT,
    display_name="Timeout Member",
    color=discord.Color.orange(),
    emoji="Muted",
    required_permission="moderate_members",
    category="members",
)
async def handle_timeout(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Could not resolve target member.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot moderate {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot moderate {target.display_name}; their role is above mine.")

    raw_seconds = ctx.int_arg("seconds", ctx.cog.config.timeout_default_seconds)
    seconds = max(1, min(raw_seconds, ctx.cog.config.timeout_max_seconds))
    reason = ctx.str_arg("reason")

    await target.timeout(timedelta(seconds=seconds), reason=reason)

    duration = _format_duration(seconds)
    embed = action_embed(
        title="Muted Member Timed Out", color=discord.Color.orange(),
        actor=ctx.actor, target=target, reason=reason,
        extra={"Duration": duration},
    )
    await ctx.cog.log_action(
        message=ctx.message, action="timeout_member",
        actor=ctx.actor, target=target, reason=reason, decision=ctx.decision,
        extra={"Duration": duration},
    )
    return ToolResult.ok("Timeout applied.", embed=embed)


@ToolRegistry.register(
    ToolType.UNTIMEOUT,
    display_name="Remove Timeout",
    color=discord.Color.green(),
    emoji="Unmuted",
    required_permission="moderate_members",
    category="members",
)
async def handle_untimeout(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Could not resolve target member.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot moderate {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot moderate {target.display_name}; their role is above mine.")

    reason = ctx.str_arg("reason", "Timeout removed.")
    await target.timeout(None, reason=reason)

    embed = action_embed(
        title="Unmuted Timeout Removed", color=discord.Color.green(),
        actor=ctx.actor, target=target, reason=reason,
    )
    await ctx.cog.log_action(
        message=ctx.message, action="untimeout_member",
        actor=ctx.actor, target=target, reason=reason, decision=ctx.decision,
    )
    return ToolResult.ok("Timeout removed.", embed=embed)


@ToolRegistry.register(
    ToolType.KICK,
    display_name="Kick Member",
    color=discord.Color.red(),
    emoji="Kick",
    required_permission="kick_members",
    category="members",
)
async def handle_kick(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Could not resolve target member.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot kick {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot kick {target.display_name}; their role is above mine.")

    reason = ctx.str_arg("reason")
    await target.kick(reason=f"AI Mod ({ctx.actor}): {reason}")

    embed = action_embed(
        title="Kick Member Kicked", color=discord.Color.red(),
        actor=ctx.actor, target=target, reason=reason,
    )
    await ctx.cog.log_action(
        message=ctx.message, action="kick_member",
        actor=ctx.actor, target=target, reason=reason, decision=ctx.decision,
    )
    return ToolResult.ok("Member kicked.", embed=embed)


@ToolRegistry.register(
    ToolType.BAN,
    display_name="Ban Member",
    color=discord.Color.dark_red(),
    emoji="Ban",
    required_permission="ban_members",
    category="members",
)
async def handle_ban(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Could not resolve target member.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot ban {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot ban {target.display_name}; their role is above mine.")

    reason = ctx.str_arg("reason")
    delete_days = max(0, min(ctx.int_arg("delete_message_days", 0), 7))
    await target.ban(reason=f"AI Mod ({ctx.actor}): {reason}", delete_message_days=delete_days)

    embed = action_embed(
        title="Ban Member Banned", color=discord.Color.dark_red(),
        actor=ctx.actor, target=target, reason=reason,
        extra={"Messages Deleted": f"{delete_days} day(s)"} if delete_days else None,
    )
    await ctx.cog.log_action(
        message=ctx.message, action="ban_member",
        actor=ctx.actor, target=target, reason=reason, decision=ctx.decision,
        extra={"Delete Messages": f"{delete_days} day(s)"},
    )
    return ToolResult.ok("Member banned.", embed=embed)


@ToolRegistry.register(
    ToolType.UNBAN,
    display_name="Unban Member",
    color=discord.Color.green(),
    emoji="Done",
    required_permission="ban_members",
    category="members",
)
async def handle_unban(ctx: ToolContext) -> ToolResult:
    from datetime import datetime, timezone

    from utils.embeds import compact_kv_lines

    raw_id = ctx.args.get("target_user_id")
    try:
        target_id = int(raw_id)
    except (TypeError, ValueError):
        return ToolResult.fail("Invalid user ID for unban.")

    reason = ctx.str_arg("reason", "Unbanned.")
    await ctx.guild.unban(discord.Object(id=target_id), reason=f"AI Mod ({ctx.actor}): {reason}")

    embed = discord.Embed(title="User Unbanned", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    rows: list[tuple[str, object]] = [("Moderator", ctx.actor.mention), ("Reason", reason)]
    embed.set_footer(text=f"User ID: {target_id}")
    try:
        user = await ctx.cog.bot.fetch_user(target_id)
        embed.set_author(name=user.name, icon_url=user.display_avatar.url)
        rows.insert(0, ("User", f"{user.mention} (`{user.name}`)"))
        embed.set_thumbnail(url=user.display_avatar.url)
    except discord.HTTPException:
        rows.insert(0, ("User", f"<@{target_id}> (ID: `{target_id}`)"))
    embed.description = compact_kv_lines(rows)

    await ctx.cog.log_action(
        message=ctx.message, action="unban_member",
        actor=ctx.actor, target=None, reason=reason, decision=ctx.decision,
        extra={"User ID": str(target_id)},
    )
    return ToolResult.ok("User unbanned.", embed=embed)


@ToolRegistry.register(
    ToolType.SET_NICKNAME,
    display_name="Set Nickname",
    color=discord.Color.blue(),
    emoji="Nick",
    required_permission="manage_nicknames",
    category="members",
)
async def handle_set_nickname(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Target not found.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail("Target's role is above yours.")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail("Target's role is above mine.")

    new_nick: Optional[str] = ctx.arg("nickname")
    if new_nick and len(new_nick) > 32:
        return ToolResult.fail("Nickname too long (max 32 characters).")

    await target.edit(nick=new_nick, reason=f"AI Mod ({ctx.actor})")
    msg = f"Nickname set to `{new_nick}`." if new_nick else "Nickname reset."
    return ToolResult.ok(msg)


@ToolRegistry.register(
    ToolType.MOVE_MEMBER,
    display_name="Move Member",
    color=discord.Color.purple(),
    emoji="Move",
    required_permission="move_members",
    category="members",
)
async def handle_move_member(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Target not found.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot move {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot move {target.display_name}; their role is above mine.")
    if not target.voice:
        return ToolResult.fail(f"{target.display_name} is not in a voice channel.")

    q = str(ctx.arg("channel_name", "")).strip()
    if not q:
        return ToolResult.fail("Voice channel name or ID is required.")

    vc: Optional[discord.VoiceChannel] = None
    if q.isdigit():
        ch = ctx.guild.get_channel(int(q))
        if isinstance(ch, discord.VoiceChannel):
            vc = ch
    if not vc:
        vc = discord.utils.find(
            lambda c: isinstance(c, discord.VoiceChannel) and c.name.lower() == q.lower(),
            ctx.guild.voice_channels,
        )
    if not vc:
        return ToolResult.fail(f"Voice channel `{q}` not found.")

    await target.move_to(vc, reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok(f"Moved {target.display_name} to **{vc.name}**.")


@ToolRegistry.register(
    ToolType.DISCONNECT_MEMBER,
    display_name="Disconnect Member",
    color=discord.Color.dark_grey(),
    emoji="Disconnect",
    required_permission="move_members",
    category="members",
)
async def handle_disconnect_member(ctx: ToolContext) -> ToolResult:
    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Target not found.")
    if not ctx.cog.can_moderate(ctx.actor, target):
        return ToolResult.fail(f"Cannot disconnect {target.display_name} (role hierarchy).")
    bot_member = ctx.guild.me
    if not bot_member or not ctx.cog.can_moderate(bot_member, target):
        return ToolResult.fail(f"Cannot disconnect {target.display_name}; their role is above mine.")
    if not target.voice:
        return ToolResult.fail(f"{target.display_name} is not in a voice channel.")

    await target.move_to(None, reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok(f"Disconnected **{target.display_name}** from voice.")
