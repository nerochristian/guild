"""
Message-targeted handlers — DM, purge, pin, unpin.
"""
from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Dict, List

import discord

from ..context import ToolContext, ToolResult, _now
from ..registry import ToolRegistry
from ..types import ToolType

logger = logging.getLogger("ModBot.AIModeration.Handlers.Messages")


@ToolRegistry.register(
    ToolType.DM_USER,
    display_name="DM User",
    color=discord.Color.green(),
    emoji="DM",
    required_permission="manage_guild",
    category="messages",
)
async def handle_dm_user(ctx: ToolContext) -> ToolResult:
    is_owner = await ctx.cog.bot.is_owner(ctx.actor)
    is_admin = isinstance(ctx.actor, discord.Member) and ctx.actor.guild_permissions.administrator
    can_manage = isinstance(ctx.actor, discord.Member) and ctx.actor.guild_permissions.manage_guild
    if not (is_owner or is_admin or can_manage):
        return ToolResult.fail("You need Manage Server to DM users through the bot.")

    target = await ctx.resolve_target()
    if not target:
        return ToolResult.fail("Target user not found.")
    if target.bot:
        return ToolResult.fail("I won't DM another bot.")

    dm_text = str(ctx.arg("message", "")).strip()
    if not dm_text:
        return ToolResult.fail("What should I DM them?")
    if len(dm_text) > 1900:
        return ToolResult.fail("That DM is too long. Keep it under 1900 characters.")

    try:
        await target.send(
            f"**Message from the moderation team in {ctx.guild.name}:**\n\n{dm_text}"
        )
    except discord.Forbidden:
        return ToolResult.fail(f"I couldn't DM {target.mention}; their DMs are closed or they blocked the bot.")
    except discord.HTTPException as exc:
        return ToolResult.fail(f"Discord rejected the DM ({exc.status}).")

    await ctx.cog.log_action(
        message=ctx.message, action="dm_user",
        actor=ctx.actor, target=target, reason="AI Moderation DM",
        decision=ctx.decision, extra={"Message": dm_text[:900]},
    )
    return ToolResult.ok(f"Done! Sent a DM to {target.mention}.")


@ToolRegistry.register(
    ToolType.PIN_MESSAGE,
    display_name="Pin Message",
    color=discord.Color.red(),
    emoji="Pin",
    required_permission="manage_messages",
    category="messages",
)
async def handle_pin_message(ctx: ToolContext) -> ToolResult:
    msg_id = ctx.arg("message_id")
    if not msg_id:
        return ToolResult.fail("Message ID is required.")
    fetch_message = getattr(ctx.message.channel, "fetch_message", None)
    if not callable(fetch_message):
        return ToolResult.fail("I can't fetch messages in this channel type.")
    try:
        msg_id = int(msg_id)
    except (TypeError, ValueError):
        return ToolResult.fail("Message ID must be a number.")
    msg = await fetch_message(msg_id)
    await msg.pin(reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok("Message pinned.")


@ToolRegistry.register(
    ToolType.UNPIN_MESSAGE,
    display_name="Unpin Message",
    color=discord.Color.orange(),
    emoji="Unpin",
    required_permission="manage_messages",
    category="messages",
)
async def handle_unpin_message(ctx: ToolContext) -> ToolResult:
    msg_id = ctx.arg("message_id")
    if not msg_id:
        return ToolResult.fail("Message ID is required - reply to the message or provide its ID.")
    fetch_message = getattr(ctx.message.channel, "fetch_message", None)
    if not callable(fetch_message):
        return ToolResult.fail("I can't fetch messages in this channel type.")
    try:
        msg_id = int(msg_id)
    except (TypeError, ValueError):
        return ToolResult.fail("Message ID must be a number.")
    msg = await fetch_message(msg_id)
    await msg.unpin(reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok("Message unpinned.")


@ToolRegistry.register(
    ToolType.PURGE,
    display_name="Purge Messages",
    color=discord.Color.blue(),
    emoji="Delete",
    required_permission="manage_messages",
    category="messages",
)
async def handle_purge(ctx: ToolContext) -> ToolResult:
    from utils.embeds import compact_kv_lines
    from utils.transcript import EphemeralTranscriptView, generate_html_transcript

    channel = ctx.message.channel
    raw_channel_id = ctx.arg("channel_id")
    if raw_channel_id:
        try:
            resolved_channel = ctx.guild.get_channel(int(raw_channel_id))
        except (TypeError, ValueError):
            resolved_channel = None
        if resolved_channel is None:
            return ToolResult.fail("I couldn't find that channel.")
        channel = resolved_channel

    if not isinstance(channel, discord.TextChannel):
        return ToolResult.fail("Purge only works in text channels.")

    actor_perms = channel.permissions_for(ctx.actor)
    if not actor_perms.manage_messages or not actor_perms.read_message_history:
        return ToolResult.fail(f"You need Manage Messages and Read Message History in {channel.mention}.")

    if ctx.arg("needs_channel_scope"):
        return ToolResult.ok(
            f"Did you mean in {ctx.message.channel.mention}, or in all channels? "
            "Mention the channel to use, or say `in this channel`."
        )

    bot_member = ctx.guild.me
    all_channels_requested = ctx.bool_arg("all_channels_requested")
    if bot_member and not all_channels_requested:
        bot_perms = channel.permissions_for(bot_member)
        if not bot_perms.manage_messages or not bot_perms.read_message_history:
            return ToolResult.fail(f"I need Manage Messages and Read Message History in {channel.mention}.")

    amount = max(1, min(ctx.int_arg("amount", 10), 500))
    reason = ctx.str_arg("reason", "AI Moderation purge")
    target_user_id = ctx.arg("target_user_id")
    try:
        target_user_id = int(target_user_id) if target_user_id else None
    except (TypeError, ValueError):
        target_user_id = None

    lookback_seconds = ctx.arg("lookback_seconds")
    try:
        lookback_seconds = int(lookback_seconds) if lookback_seconds else None
    except (TypeError, ValueError):
        lookback_seconds = None
    if lookback_seconds is not None:
        lookback_seconds = max(1, min(lookback_seconds, 14 * 24 * 60 * 60))

    if all_channels_requested and target_user_id is None:
        return ToolResult.ok("Tell me whose messages to delete when using all channels.")

    logging_cog = ctx.cog.bot.get_cog("Logging")
    if logging_cog and not all_channels_requested:
        logging_cog.suppress_message_delete_log(channel.id)
        logging_cog.suppress_bulk_delete_log(channel.id)

    cutoff = _now() - timedelta(seconds=lookback_seconds) if lookback_seconds else None

    def message_matches(candidate: discord.Message) -> bool:
        if candidate.id == ctx.message.id:
            return False
        if target_user_id is not None and candidate.author.id != target_user_id:
            return False
        if cutoff and candidate.created_at < cutoff:
            return False
        return True

    deleted_messages: List[discord.Message] = []
    deleted_by_channel: Dict[int, int] = {}

    async def purge_one_channel(target_channel: discord.TextChannel, remaining: int) -> List[discord.Message]:
        matched_count = 0

        def should_delete(candidate: discord.Message) -> bool:
            nonlocal matched_count
            if not message_matches(candidate):
                return False
            if matched_count >= remaining:
                return False
            matched_count += 1
            return True

        if logging_cog:
            logging_cog.suppress_message_delete_log(target_channel.id)
            logging_cog.suppress_bulk_delete_log(target_channel.id)
        purge_limit = (
            remaining + 1
            if target_user_id is None and lookback_seconds is None
            else min(5_000, max(100, remaining * 5))
        )
        return await target_channel.purge(limit=purge_limit, check=should_delete)

    if all_channels_requested:
        for target_channel in ctx.guild.text_channels:
            if len(deleted_messages) >= amount:
                break
            if bot_member:
                bot_perms = target_channel.permissions_for(bot_member)
                if not bot_perms.manage_messages or not bot_perms.read_message_history:
                    continue
            actor_perms = target_channel.permissions_for(ctx.actor)
            if not actor_perms.manage_messages or not actor_perms.read_message_history:
                continue
            remaining = amount - len(deleted_messages)
            try:
                deleted = await purge_one_channel(target_channel, remaining)
            except (discord.Forbidden, discord.HTTPException):
                logger.debug("Skipping channel during all-channel purge: %s", target_channel.id, exc_info=True)
                continue
            deleted_clean = [m for m in deleted if m.id != ctx.message.id]
            if deleted_clean:
                deleted_messages.extend(deleted_clean)
                deleted_by_channel[target_channel.id] = deleted_by_channel.get(target_channel.id, 0) + len(deleted_clean)
        deleted_count = len(deleted_messages)
    else:
        deleted = await purge_one_channel(channel, amount)
        deleted_messages = [m for m in deleted if m.id != ctx.message.id]
        deleted_count = len(deleted_messages)

    if all_channels_requested:
        channel_label = f"{len(deleted_by_channel)} channel{'s' if len(deleted_by_channel) != 1 else ''}"
    else:
        channel_label = channel.mention

    if all_channels_requested:
        await ctx.cog.log_action(
            message=ctx.message, action="purge_messages",
            actor=ctx.actor, target=None, reason=reason, decision=ctx.decision,
            extra={"Count": str(deleted_count), "Scope": "All channels"},
        )
        target_text = f" of <@{target_user_id}>" if target_user_id is not None else ""
        if deleted_count == 0:
            return ToolResult.ok(f"I didn't find any messages{target_text} in any channel.")
        plural = "message" if deleted_count == 1 else "messages"
        cap_note = " I stopped at the 500-message limit." if deleted_count >= amount and amount >= 500 else ""
        return ToolResult.ok(
            f"Done! All {deleted_count} {plural}{target_text} across {channel_label} have been deleted.{cap_note}"
        )

    if deleted_count > 0 and logging_cog:
        try:
            bot_count = sum(1 for m in deleted_messages if m.author.bot)
            unique_authors = {m.author for m in deleted_messages if not m.author.bot}
            preview_lines: List[str] = []
            for msg in reversed(deleted_messages):
                raw = (msg.content or "").strip()
                if not raw:
                    if msg.attachments:
                        raw = f"[{len(msg.attachments)} attachment(s)]"
                    elif msg.embeds:
                        raw = "[embed]"
                    else:
                        continue
                raw = " ".join(raw.split())
                if len(raw) > 80:
                    raw = raw[:77].rstrip() + "..."
                author_name = getattr(msg.author, "display_name", None) or getattr(msg.author, "name", "unknown")
                preview_lines.append(f"`{author_name}`: {raw}")
                if len(preview_lines) >= 8:
                    break
            preview_text = "\n".join(preview_lines) if preview_lines else "*No text content available*"

            transcript_raw = generate_html_transcript(ctx.guild, channel, [], purged_messages=deleted_messages)
            if isinstance(transcript_raw, (bytes, bytearray)):
                transcript_bytes: io.BytesIO = io.BytesIO(transcript_raw)
            elif isinstance(transcript_raw, io.BytesIO):
                transcript_bytes = transcript_raw
            else:
                transcript_bytes = io.BytesIO(str(transcript_raw).encode("utf-8"))
            transcript_name = f"purge-{ctx.guild.id}-{int(_now().timestamp())}.html"

            log_channel = await logging_cog.get_log_channel(ctx.guild, "message")
            if log_channel:
                log_embed = discord.Embed(
                    title="Bulk Message Delete",
                    description=f"**{deleted_count}** message(s) purged in {channel.mention}",
                    color=discord.Color.red(),
                    timestamp=_now(),
                )
                log_embed.description = "\n".join(filter(None, [
                    log_embed.description or "",
                    compact_kv_lines([
                        ("Moderator", f"{ctx.actor.mention} (`{ctx.actor.id}`)"),
                        ("Human Messages", deleted_count - bot_count),
                        ("Bot Messages", bot_count),
                        ("Unique Authors", len(unique_authors)),
                    ]),
                ]))
                log_embed.add_field(name="Purged Message Preview", value=preview_text[:1024], inline=False)
                transcript_bytes.seek(0)
                view = EphemeralTranscriptView(io.BytesIO(transcript_bytes.read()), filename=transcript_name)
                await logging_cog.safe_send_log(log_channel, log_embed, view=view)

            mod_log_channel = await logging_cog.get_log_channel(ctx.guild, "mod")
            if mod_log_channel:
                mod_embed = discord.Embed(
                    title="Moderator Purge",
                    description=f"{ctx.actor.mention} purged **{deleted_count}** message(s) in {channel.mention}.",
                    color=discord.Color.red(),
                    timestamp=_now(),
                )
                mod_embed.description = "\n".join(filter(None, [
                    mod_embed.description or "",
                    compact_kv_lines([
                        ("Moderator", f"{ctx.actor.mention} (`{ctx.actor.id}`)"),
                        ("Reason", reason),
                        ("Human Messages", deleted_count - bot_count),
                        ("Bot Messages", bot_count),
                        ("Unique Authors", len(unique_authors)),
                    ]),
                ]))
                transcript_bytes.seek(0)
                mod_view = EphemeralTranscriptView(io.BytesIO(transcript_bytes.read()), filename=transcript_name)
                await logging_cog.safe_send_log(mod_log_channel, mod_embed, view=mod_view)
        except Exception:
            logger.debug("Failed to post purge transcript", exc_info=True)

    await ctx.cog.log_action(
        message=ctx.message, action="purge_messages",
        actor=ctx.actor, target=None, reason=reason, decision=ctx.decision,
        extra={"Count": str(deleted_count)},
    )

    target_text = f" of <@{target_user_id}>" if target_user_id is not None else ""
    window_text = ""
    if lookback_seconds is not None:
        if lookback_seconds % 86400 == 0:
            unit_amount = lookback_seconds // 86400
            window_text = f" from the last {unit_amount} day{'s' if unit_amount != 1 else ''}"
        elif lookback_seconds % 3600 == 0:
            unit_amount = lookback_seconds // 3600
            window_text = f" from the last {unit_amount} hour{'s' if unit_amount != 1 else ''}"
        elif lookback_seconds % 60 == 0:
            unit_amount = lookback_seconds // 60
            window_text = f" from the last {unit_amount} minute{'s' if unit_amount != 1 else ''}"
        else:
            window_text = f" from the last {lookback_seconds} seconds"

    if deleted_count == 0:
        return ToolResult.ok(f"I didn't find any messages{target_text}{window_text} in {channel.mention}.")
    plural = "message" if deleted_count == 1 else "messages"
    cap_note = " I stopped at the 500-message limit." if deleted_count >= amount and amount >= 500 else ""
    return ToolResult.ok(
        f"Done! Deleted {deleted_count} {plural}{target_text}{window_text} in {channel.mention}.{cap_note}"
    )
