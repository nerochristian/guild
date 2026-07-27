"""Guild-level handlers — edit guild, create/delete emoji, create invite, help."""
from __future__ import annotations

from typing import Dict
from urllib.parse import urlparse

import aiohttp
import discord

from ..context import ToolContext, ToolResult
from ..registry import ToolRegistry
from ..types import ToolType


@ToolRegistry.register(
    ToolType.EDIT_GUILD,
    display_name="Edit Server",
    color=discord.Color.gold(),
    emoji="Server",
    required_permission="manage_guild",
    category="guild",
)
async def handle_edit_guild(ctx: ToolContext) -> ToolResult:
    kwargs: Dict[str, str] = {}
    if "name" in ctx.args:
        name = str(ctx.args["name"]).strip()
        if not 2 <= len(name) <= 100:
            return ToolResult.fail("Server name must be between 2 and 100 characters.")
        kwargs["name"] = name
    if not kwargs:
        return ToolResult.fail("Nothing to edit.")
    await ctx.guild.edit(**kwargs, reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok("Server settings updated.")


@ToolRegistry.register(
    ToolType.CREATE_EMOJI,
    display_name="Create Emoji",
    color=discord.Color.green(),
    emoji="Emoji",
    required_permission="manage_emojis",
    category="guild",
)
async def handle_create_emoji(ctx: ToolContext) -> ToolResult:
    name = str(ctx.arg("name", "")).strip()
    url = str(ctx.arg("url", "")).strip()
    if not name or not url:
        return ToolResult.fail("Both emoji name and image URL are required.")
    if not 2 <= len(name) <= 32 or not name.replace("_", "").isalnum():
        return ToolResult.fail("Emoji names must be 2-32 letters, numbers, or underscores.")

    parsed = urlparse(url)
    allowed_hosts = {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts:
        return ToolResult.fail("Emoji images must use a Discord CDN HTTPS URL.")

    session: aiohttp.ClientSession | None = getattr(ctx.cog.bot, "session", None)
    owned_session = False
    if not session or getattr(session, "closed", False):
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        owned_session = True

    try:
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status != 200:
                return ToolResult.fail(f"Failed to download image (HTTP {resp.status}).")
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                return ToolResult.fail("The supplied URL did not return an image.")
            data = await resp.content.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                return ToolResult.fail("Emoji image is too large (maximum 256 KiB).")
        emoji = await ctx.guild.create_custom_emoji(name=name, image=data, reason=f"AI Mod ({ctx.actor})")
        embed = discord.Embed(description=f"Created emoji {emoji}", color=discord.Color.green())
        return ToolResult.ok("Emoji created.", embed=embed)
    finally:
        if owned_session:
            await session.close()


@ToolRegistry.register(
    ToolType.DELETE_EMOJI,
    display_name="Delete Emoji",
    color=discord.Color.red(),
    emoji="Delete",
    required_permission="manage_emojis",
    category="guild",
)
async def handle_delete_emoji(ctx: ToolContext) -> ToolResult:
    name = ctx.arg("name")
    if not name:
        return ToolResult.fail("Emoji name is required.")
    emoji = discord.utils.find(lambda e: e.name.lower() == str(name).lower(), ctx.guild.emojis)
    if not emoji:
        return ToolResult.fail(f"Emoji `{name}` not found.")
    await emoji.delete(reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok(f"Emoji `{name}` deleted.")


@ToolRegistry.register(
    ToolType.CREATE_INVITE,
    display_name="Create Invite",
    color=discord.Color.green(),
    emoji="Invite",
    required_permission="create_instant_invite",
    category="guild",
)
async def handle_create_invite(ctx: ToolContext) -> ToolResult:
    max_age = max(0, min(ctx.int_arg("max_age", 86400), 604800))
    create_invite = getattr(ctx.message.channel, "create_invite", None)
    if not callable(create_invite):
        return ToolResult.fail("I can't create an invite from this channel type.")
    invite = await create_invite(max_age=max_age, reason=f"AI Mod ({ctx.actor})")
    return ToolResult.ok(f"Invite created: {invite.url}")


@ToolRegistry.register(
    ToolType.HELP,
    display_name="Show Help",
    color=discord.Color.blurple(),
    emoji="?",
    category="guild",
)
async def handle_help(ctx: ToolContext) -> ToolResult:
    embed = ctx.cog.build_help_embed(ctx.guild)
    return ToolResult.ok("Help displayed.", embed=embed)
