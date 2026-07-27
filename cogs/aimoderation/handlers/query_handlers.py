"""
Enhanced natural language query handlers.

Tools for analytics, bulk queries, inactivity scanning, and safety audits.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import discord

from ..context import ToolContext, ToolResult
from ..registry import ToolRegistry
from ..types import ToolType

logger = logging.getLogger("ModBot.AIModeration.Handlers.Queries")


# =============================================================================
# FIND INACTIVE MEMBERS
# =============================================================================

@ToolRegistry.register(
    ToolType.FIND_INACTIVE_MEMBERS,
    display_name="Find Inactive",
    color=discord.Color.blue(),
    emoji="🔍",
    required_permission="manage_guild",
    category="queries",
    description="Find guild members who haven't sent messages in N days.",
)
async def handle_find_inactive(ctx: ToolContext) -> ToolResult:
    days = max(1, min(ctx.int_arg("days", 30), 365))
    limit = max(1, min(ctx.int_arg("limit", 20), 50))
    guild = ctx.guild
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    active_member_ids: set[int] = set()
    history_limit = 1_000
    semaphore = asyncio.Semaphore(4)

    async def scan_channel(channel: discord.TextChannel) -> None:
        actor_permissions = channel.permissions_for(ctx.actor)
        if not actor_permissions.view_channel or not actor_permissions.read_message_history:
            return
        bot_member = guild.me
        if bot_member:
            permissions = channel.permissions_for(bot_member)
            if not permissions.view_channel or not permissions.read_message_history:
                return
        try:
            async with semaphore:
                async for message in channel.history(limit=history_limit, after=cutoff):
                    if not message.author.bot:
                        active_member_ids.add(message.author.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not scan channel %s for inactivity", channel.id, exc_info=True)

    await asyncio.gather(*(scan_channel(channel) for channel in guild.text_channels))

    inactive: List[discord.Member] = []
    for member in guild.members:
        if member.bot:
            continue
        joined_at = member.joined_at
        if joined_at and joined_at.tzinfo is None:
            joined_at = joined_at.replace(tzinfo=timezone.utc)
        if joined_at and joined_at > cutoff:
            continue
        if member.id in active_member_ids:
            continue
        inactive.append(member)

    inactive.sort(key=lambda member: member.joined_at or datetime.min.replace(tzinfo=timezone.utc))
    preview = inactive[:limit]

    lines = []
    for m in preview:
        joined = f"<t:{int(m.joined_at.timestamp())}:R>" if m.joined_at else "unknown"
        lines.append(f"• {m.mention} — Joined {joined}")

    if not lines:
        return ToolResult.ok(f"No inactive members found (>{days} days).")

    header = f"**No activity found in the last {days} days** — {len(inactive)} member(s)"
    if len(inactive) > limit:
        header += f" (showing first {limit})"
    footer = f"Checked up to {history_limit:,} recent messages per readable text channel."
    return ToolResult.ok("\n".join([header] + lines + ["", footer]))


# =============================================================================
# SCAN CHANNEL MESSAGES
# =============================================================================

@ToolRegistry.register(
    ToolType.SCAN_CHANNEL,
    display_name="Scan Channel",
    color=discord.Color.blurple(),
    emoji="📋",
    required_permission="manage_messages",
    category="queries",
    description="Dry-run automod on the last N messages in a channel.",
)
async def handle_scan_channel(ctx: ToolContext) -> ToolResult:
    amount = max(1, min(ctx.int_arg("amount", 100), 500))
    channel_id = ctx.arg("channel_id")
    guild = ctx.guild
    channel = ctx.message.channel
    if channel_id:
        try:
            resolved_id = int(channel_id)
        except (TypeError, ValueError):
            return ToolResult.fail("Channel ID must be a number or channel mention.")
        get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
        channel = get_channel_or_thread(resolved_id) if callable(get_channel_or_thread) else guild.get_channel(resolved_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return ToolResult.fail("Channel not found.")

    actor_permissions = channel.permissions_for(ctx.actor)
    if not actor_permissions.view_channel or not actor_permissions.read_message_history:
        return ToolResult.fail("You need View Channel and Read Message History in that channel.")

    try:
        messages = [m async for m in channel.history(limit=amount)]
    except discord.HTTPException:
        return ToolResult.fail("Failed to fetch messages.")

    automod_cog = ctx.cog.bot.get_cog("AutoMod")
    engine = getattr(automod_cog, "engine", None)
    db = getattr(ctx.cog.bot, "db", None)
    if engine is None or db is None:
        return ToolResult.fail("AutoMod is not available right now.")
    settings = await db.get_settings(guild.id)
    violations = 0
    rule_hits: Dict[str, int] = {}

    for msg in messages:
        if msg.author.bot:
            continue
        match = await engine.evaluate(msg, settings, dry_run=True)
        if match is not None:
            violations += 1
            rule_hits[match.rule] = rule_hits.get(match.rule, 0) + 1

    lines = [f"**Scanned {len(messages)} messages** in {channel.mention}"]
    if violations:
        lines.append(f"⚠️ {violations} violations found:")
        for rule, count in sorted(rule_hits.items(), key=lambda x: -x[1]):
            lines.append(f"• {rule}: {count}")
    else:
        lines.append("✅ No violations detected.")

    return ToolResult.ok("\n".join(lines))


# =============================================================================
# SUMMARIZE TODAY'S ACTIONS
# =============================================================================

@ToolRegistry.register(
    ToolType.SUMMARIZE_ACTIONS,
    display_name="Summarize Actions",
    color=discord.Color.green(),
    emoji="📊",
    required_permission="manage_guild",
    category="queries",
    description="Summarize all moderation actions from the last 24 hours.",
)
async def handle_summarize_today(ctx: ToolContext) -> ToolResult:
    guild = ctx.guild
    db_manager = getattr(ctx.cog.bot, "db", None)
    if db_manager is None:
        return ToolResult.fail("Database not available.")

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        async with db_manager.get_connection() as db:
            cursor = await db.execute(
                """
                SELECT action, COUNT(*) FROM mod_stats
                WHERE guild_id = ? AND created_at >= ?
                GROUP BY action
                """,
                (guild.id, since),
            )
            action_rows = await cursor.fetchall()

            cursor2 = await db.execute(
                """
                SELECT COUNT(*) FROM cases
                WHERE guild_id = ? AND created_at >= ?
                """,
                (guild.id, since),
            )
            case_count = (await cursor2.fetchone() or [0])[0]

    except Exception:
        logger.exception("Failed to summarize moderation actions for guild %s", guild.id)
        return ToolResult.fail("Failed to query moderation data.")

    lines = [f"**📊 Last 24 Hours — {guild.name}**"]
    if case_count:
        lines.append(f"Total cases: **{case_count}**")

    action_map = {r[0]: r[1] for r in action_rows}
    total_actions = sum(action_map.values())

    if action_map:
        for action, count in sorted(action_map.items(), key=lambda x: -x[1]):
            lines.append(f"• {action}: **{count}**")
        lines.append(f"\nTotal actions: **{total_actions}**")
    else:
        lines.append("No moderation actions recorded.")

    try:
        top = await db_manager.get_top_risky_users(guild.id, limit=3)
        if top:
            lines.append("\n**Top Risks:**")
            for entry in top:
                lines.append(f"• <@{entry['user_id']}> — `{entry['score']}/100`")
    except Exception:
        pass

    return ToolResult.ok("\n".join(lines))


# =============================================================================
# SERVER SAFETY CHECK
# =============================================================================

@ToolRegistry.register(
    ToolType.SAFETY_CHECK,
    display_name="Safety Check",
    color=discord.Color.gold(),
    emoji="🛡️",
    required_permission="manage_guild",
    category="queries",
    description="Audit server safety: permissions, invite audit, hierarchy check.",
)
async def handle_safety_check(ctx: ToolContext) -> ToolResult:
    guild = ctx.guild
    if guild is None:
        return ToolResult.fail("Server only.")

    issues: List[str] = []
    ok: List[str] = []

    # Admin role count
    admin_count = sum(1 for r in guild.roles if r.permissions.administrator)
    if admin_count > 3:
        issues.append(f"⚠️ {admin_count} roles have Administrator permission. Consider reducing.")
    elif admin_count <= 1:
        ok.append(f"✅ Only {admin_count} admin role(s) — good.")

    # Everyone mentionable
    everyone = guild.default_role
    if everyone.permissions.mention_everyone:
        issues.append("⚠️ @everyone can mention everyone. Lock this down.")

    # Invite audit — channels with create_invite for @everyone
    invite_leak = 0
    for ch in guild.text_channels:
        if ch.permissions_for(everyone).create_instant_invite:
            invite_leak += 1
    if invite_leak > 5:
        issues.append(f"⚠️ {invite_leak} channels allow @everyone to create invites.")
    else:
        ok.append(f"✅ {invite_leak} channels with open invite perms.")

    # Check for unverified users sending messages
    verification_level = guild.verification_level.name
    if verification_level == "none":
        issues.append("⚠️ Verification level is NONE. Consider raising it.")
    else:
        ok.append(f"✅ Verification: {verification_level}")

    # Explicit NSFW content filter
    nsfw_filter = guild.explicit_content_filter.name
    if nsfw_filter == "disabled":
        issues.append("⚠️ Explicit content filter is disabled.")
    else:
        ok.append(f"✅ Content filter: {nsfw_filter}")

    lines = ["**🛡️ Server Safety Audit**"]
    if issues:
        lines.append("\n**Issues:**")
        lines.extend(issues)
    lines.append("\n**OK:**")
    lines.extend(ok)
    lines.append(f"\n📊 Risk score across {len(guild.members)} members")

    return ToolResult.ok("\n".join(lines))
