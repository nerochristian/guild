from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
import discord
from aiohttp import web

from .frontend import render_setup_page, settings_int


@dataclass(frozen=True)
class SetupWebsiteConfig:
    base_dir: Path
    server_name: str
    token_ttl_seconds: int
    max_upload_bytes: int
    upload_dir: Path
    ticket_support_role_id: int


class SetupPortalView(discord.ui.View):
    def __init__(self, setup_url: str, *, timeout_seconds: int) -> None:
        super().__init__(timeout=timeout_seconds)
        self.add_item(
            discord.ui.Button(
                label="Open Setup",
                style=discord.ButtonStyle.link,
                url=setup_url,
            )
        )


def setup_public_base_url() -> str:
    for env_name in (
        "PUBLIC_BASE_URL",
        "SETUP_BASE_URL",
        "APP_URL",
        "DIGITALOCEAN_APP_URL",
        "RENDER_EXTERNAL_URL",
    ):
        value = os.getenv(env_name, "").strip()
        if value:
            return value.rstrip("/")

    for env_name in ("APP_DOMAIN", "DIGITALOCEAN_APP_DOMAIN"):
        value = os.getenv(env_name, "").strip().strip("/")
        if value:
            if value.startswith(("http://", "https://")):
                return value.rstrip("/")
            return f"https://{value}"

    port = os.getenv("PORT", "8080").strip() or "8080"
    return f"http://localhost:{port}"


def setup_url(token: str) -> str:
    return f"{setup_public_base_url()}/setup/{quote(token)}"


def clean_setup_sessions(bot_instance: Any) -> None:
    sessions = getattr(bot_instance, "setup_sessions", {})
    now_ts = discord.utils.utcnow().timestamp()
    expired = [
        token
        for token, session in sessions.items()
        if float(session.get("expires_at", 0)) <= now_ts
    ]
    for token in expired:
        sessions.pop(token, None)


def create_setup_session(
    bot_instance: Any,
    guild: discord.Guild,
    user: discord.abc.User,
    *,
    ttl_seconds: int,
) -> tuple[str, datetime]:
    clean_setup_sessions(bot_instance)
    token = secrets.token_urlsafe(32)
    expires_at = discord.utils.utcnow().timestamp() + ttl_seconds
    bot_instance.setup_sessions[token] = {
        "guild_id": guild.id,
        "user_id": user.id,
        "created_at": discord.utils.utcnow().timestamp(),
        "expires_at": expires_at,
    }
    return token, datetime.fromtimestamp(expires_at, tz=timezone.utc)


def _private_log_overwrites(
    guild: discord.Guild,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
    return overwrites


async def _enforce_private_log_channel(
    channel: discord.TextChannel,
) -> None:
    for target, overwrite in _private_log_overwrites(
        channel.guild
    ).items():
        await channel.set_permissions(
            target, overwrite=overwrite, reason="Restrict bot log channel to admins"
        )


async def _get_or_create_category(
    guild: discord.Guild, name: str
) -> discord.CategoryChannel:
    existing = discord.utils.find(
        lambda channel: (
            isinstance(channel, discord.CategoryChannel)
            and channel.name.casefold() == name.casefold()
        ),
        guild.categories,
    )
    if isinstance(existing, discord.CategoryChannel):
        return existing
    return await guild.create_category(name, reason="Soul setup")


async def _get_or_create_setup_text_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    *,
    topic: str,
    private: bool = False,
) -> discord.TextChannel:
    existing = discord.utils.get(category.channels, name=name)
    if isinstance(existing, discord.TextChannel):
        if private:
            await _enforce_private_log_channel(existing)
        return existing

    overwrites = _private_log_overwrites(guild) if private else {}
    return await guild.create_text_channel(
        name,
        category=category,
        overwrites=overwrites,
        topic=topic,
    )


async def _selected_category(
    guild: discord.Guild,
    raw_value: object,
    default_name: str,
) -> discord.CategoryChannel:
    value = str(raw_value or "").strip()
    if value.isdigit():
        channel = guild.get_channel(int(value))
        if isinstance(channel, discord.CategoryChannel):
            return channel
    return await _get_or_create_category(guild, default_name)


async def _selected_text_channel(
    guild: discord.Guild,
    raw_value: object,
    *,
    category: discord.CategoryChannel,
    default_name: str,
    topic: str,
    private: bool = False,
    current_id: Optional[int] = None,
) -> discord.TextChannel:
    value = str(raw_value or "").strip()
    if value.isdigit():
        channel = guild.get_channel(int(value))
        if channel is None:
            try:
                fetched = await guild.fetch_channel(int(value))
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                fetched = None
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        if isinstance(channel, discord.TextChannel):
            if private:
                await _enforce_private_log_channel(channel)
            return channel

    if current_id:
        channel = guild.get_channel(current_id)
        if isinstance(channel, discord.TextChannel):
            if private:
                await _enforce_private_log_channel(channel)
            return channel

    return await _get_or_create_setup_text_channel(
        guild,
        category,
        default_name,
        topic=topic,
        private=private,
    )


async def _optional_text_channel_id(
    guild: discord.Guild, raw_value: object
) -> Optional[int]:
    value = str(raw_value or "").strip()
    if not value or not value.isdigit():
        return None
    channel = guild.get_channel(int(value))
    if channel is None:
        try:
            fetched = await guild.fetch_channel(int(value))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            fetched = None
        channel = fetched if isinstance(fetched, discord.TextChannel) else None
    return channel.id if isinstance(channel, discord.TextChannel) else None


async def _optional_role_id(guild: discord.Guild, raw_value: object) -> Optional[int]:
    value = str(raw_value or "").strip()
    if not value or not value.isdigit():
        return None
    role = guild.get_role(int(value))
    return role.id if isinstance(role, discord.Role) else None


def _safe_setup_name(value: object, fallback: str) -> str:
    name = str(value or "").strip()
    name = re.sub(r"\s+", " ", name)[:80]
    return name or fallback


def _safe_wallpaper_suffix(filename: str, content_type: str = "") -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


async def _save_wallpaper_bytes(
    config: SetupWebsiteConfig, guild_id: int, suffix: str, content: bytes
) -> str:
    if not content:
        raise ValueError("Wallpaper file was empty.")
    if len(content) > config.max_upload_bytes:
        raise ValueError("Wallpaper image is larger than the upload limit.")
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(content))
        image.verify()
    except Exception as exc:
        raise ValueError("Wallpaper must be a valid image file.") from exc
    config.upload_dir.mkdir(parents=True, exist_ok=True)
    path = config.upload_dir / f"{guild_id}{suffix}"
    await asyncio.to_thread(path.write_bytes, content)
    return str(path.relative_to(config.base_dir))


async def _wallpaper_from_setup_form(
    form: Any,
    guild_id: int,
    config: SetupWebsiteConfig,
) -> Optional[str]:
    upload = form.get("wallpaper_file")
    if getattr(upload, "filename", ""):
        suffix = _safe_wallpaper_suffix(
            str(upload.filename),
            str(getattr(upload, "content_type", "")),
        )
        content = await asyncio.to_thread(upload.file.read, config.max_upload_bytes + 1)
        return await _save_wallpaper_bytes(config, guild_id, suffix, content)

    image_url = str(form.get("wallpaper_url") or "").strip()
    if not image_url:
        return None
    if not image_url.startswith(("https://", "http://")):
        raise ValueError("Wallpaper URL must start with http:// or https://.")

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(image_url) as response:
            if response.status >= 400:
                raise ValueError("Wallpaper URL could not be downloaded.")
            content_type = response.headers.get("Content-Type", "")
            content = await response.content.read(config.max_upload_bytes + 1)
    suffix = _safe_wallpaper_suffix(image_url, content_type)
    return await _save_wallpaper_bytes(config, guild_id, suffix, content)


async def apply_web_setup(
    bot_instance: Any,
    guild: discord.Guild,
    form: Any,
    config: SetupWebsiteConfig,
) -> dict[str, object]:
    current_settings = await asyncio.to_thread(
        bot_instance.guild_settings.get_settings, guild.id
    )
    support_category = await _selected_category(
        guild,
        form.get("support_category_id"),
        "Support Tickets",
    )
    req_category = await _selected_category(
        guild,
        form.get("req_category_id"),
        "Req Tickets",
    )

    ticket_logs = await _selected_text_channel(
        guild,
        form.get("ticket_log_channel_id"),
        category=support_category,
        default_name="ticket-logs",
        topic="Closed ticket logs",
        private=True,
    )
    welcome_channel = await _selected_text_channel(
        guild,
        form.get("welcome_channel_id"),
        category=support_category,
        default_name="welcome",
        topic="Member welcome messages",
        current_id=settings_int(current_settings, "welcome_channel_id"),
    )
    level_channel = await _selected_text_channel(
        guild,
        form.get("level_channel_id"),
        category=support_category,
        default_name="level-ups",
        topic="Level-up announcements",
        current_id=settings_int(current_settings, "level_channel_id"),
    )
    inactive_channel = await _selected_text_channel(
        guild,
        form.get("inactive_channel_id"),
        category=support_category,
        default_name="inactive-notices",
        topic="Staff inactivity notices",
        current_id=settings_int(current_settings, "inactive_channel_id"),
    )

    await asyncio.to_thread(
        bot_instance.ticket_system.store.save_settings,
        guild.id,
        category_id=support_category.id,
        support_role_id=await _optional_role_id(guild, form.get("ticket_support_role_id")),
        log_channel_id=ticket_logs.id,
    )

    wallpaper_path = await _wallpaper_from_setup_form(form, guild.id, config)
    server_name = _safe_setup_name(
        form.get("server_name"), guild.name or config.server_name
    )
    saved = await asyncio.to_thread(
        bot_instance.guild_settings.save_settings,
        guild.id,
        announcement_channel_id=await _optional_text_channel_id(
            guild, form.get("announcement_channel_id")
        ),
        welcome_channel_id=welcome_channel.id,
        rules_channel_id=await _optional_text_channel_id(
            guild, form.get("rules_channel_id")
        ),
        agent_channel_id=await _optional_text_channel_id(
            guild, form.get("agent_channel_id")
        ),
        level_channel_id=level_channel.id,
        inactive_channel_id=inactive_channel.id,
        server_name=server_name,
        welcome_wallpaper_path=wallpaper_path,
        nsfw_enabled=bool(form.get("nsfw_enabled")),
        ticket_support_role_id=await _optional_role_id(guild, form.get("ticket_support_role_id")),
        bypass_role_id=await _optional_role_id(guild, form.get("bypass_role_id")),
        level_min_xp=int(form.get("level_min_xp")) if form.get("level_min_xp") else None,
        level_max_xp=int(form.get("level_max_xp")) if form.get("level_max_xp") else None,
        invite_xp=int(form.get("invite_xp")) if form.get("invite_xp") else None,
        max_transcript_messages=int(form.get("max_transcript_messages")) if form.get("max_transcript_messages") else None,
    )

    eco_store = getattr(bot_instance, "economy_system", None)
    if eco_store and hasattr(eco_store, "store"):
        eco_channel_id = await _optional_text_channel_id(
            guild, form.get("eco_channel_id")
        )
        bypass_role_id = await _optional_role_id(guild, form.get("bypass_role_id"))
        if eco_channel_id is not None:
            await asyncio.to_thread(
                eco_store.store.set_guild_config,
                guild.id,
                "shop_channel_id",
                eco_channel_id,
            )
        if bypass_role_id is not None:
            await asyncio.to_thread(
                eco_store.store.set_guild_config,
                guild.id,
                "shop_bypass_role_id",
                bypass_role_id,
            )

    return {
        **saved,
        "ticket_log_channel_id": ticket_logs.id,
        "support_category_id": support_category.id,
        "req_category_id": req_category.id,
    }


async def _load_setup_page_context(
    bot_instance: Any,
    guild: discord.Guild,
    logger: logging.Logger,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    settings = await asyncio.to_thread(
        bot_instance.guild_settings.get_settings, guild.id
    )
    ticket_settings: dict[str, object] = {}
    ticket_store = getattr(getattr(bot_instance, "ticket_system", None), "store", None)
    if ticket_store is not None:
        try:
            ticket_settings = await asyncio.to_thread(
                ticket_store.get_settings, guild.id
            )
        except Exception:
            logger.exception(
                "Failed to load ticket setup config for guild %s", guild.id
            )

    economy_config: dict[str, object] = {}
    eco_store = getattr(getattr(bot_instance, "economy_system", None), "store", None)
    if eco_store is not None:
        try:
            economy_config = await asyncio.to_thread(
                eco_store.get_guild_config, guild.id
            )
        except Exception:
            logger.exception(
                "Failed to load economy setup config for guild %s", guild.id
            )

    return settings, ticket_settings, economy_config


def _render_for_guild(
    *,
    token: str,
    guild: discord.Guild,
    settings: dict[str, object],
    ticket_settings: dict[str, object],
    economy_config: dict[str, object],
    config: SetupWebsiteConfig,
    message: str = "",
    success: bool = False,
) -> str:
    return render_setup_page(
        token=token,
        guild=guild,
        settings=settings,
        ticket_settings=ticket_settings,
        economy_config=economy_config,
        server_name_fallback=config.server_name,
        message=message,
        success=success,
    )


def register_setup_routes(
    app: web.Application,
    bot_instance: Any,
    config: SetupWebsiteConfig,
    logger: logging.Logger,
) -> None:
    async def handle_frontend_css(request: web.Request) -> web.StreamResponse:
        css_path = config.base_dir / "website" / "frontend.css"
        if not css_path.exists():
            return web.Response(status=404, text="frontend.css not found.")
        return web.FileResponse(
            css_path,
            headers={"Content-Type": "text/css; charset=utf-8"},
        )

    async def handle_setup_page(request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        clean_setup_sessions(bot_instance)
        session = bot_instance.setup_sessions.get(token)
        if session is None:
            return web.Response(
                status=404,
                text="This setup link is invalid or expired. Run /setup again.",
            )

        guild = bot_instance.get_guild(int(session["guild_id"]))
        if guild is None:
            return web.Response(
                status=404,
                text="I cannot see that server right now. Make sure the bot is still in it.",
            )

        settings, ticket_settings, economy_config = await _load_setup_page_context(
            bot_instance, guild, logger
        )
        html_body = _render_for_guild(
            token=token,
            guild=guild,
            settings=settings,
            ticket_settings=ticket_settings,
            economy_config=economy_config,
            config=config,
        )
        return web.Response(text=html_body, content_type="text/html", charset="utf-8")

    async def handle_setup_submit(request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        clean_setup_sessions(bot_instance)
        session = bot_instance.setup_sessions.get(token)
        if session is None:
            return web.Response(
                status=404,
                text="This setup link is invalid or expired. Run /setup again.",
            )

        guild = bot_instance.get_guild(int(session["guild_id"]))
        if guild is None:
            return web.Response(
                status=404,
                text="I cannot see that server right now. Make sure the bot is still in it.",
            )

        try:
            form = await request.post()
            settings = await apply_web_setup(bot_instance, guild, form, config)
            settings, ticket_settings, economy_config = await _load_setup_page_context(
                bot_instance, guild, logger
            )
            html_body = _render_for_guild(
                token=token,
                guild=guild,
                settings=settings,
                ticket_settings=ticket_settings,
                economy_config=economy_config,
                config=config,
                message="Setup saved. You can close this tab or keep adjusting options.",
                success=True,
            )
            return web.Response(
                text=html_body, content_type="text/html", charset="utf-8"
            )
        except Exception as exc:
            logger.exception("Web setup failed for guild %s", guild.id)
            settings, ticket_settings, economy_config = await _load_setup_page_context(
                bot_instance, guild, logger
            )
            html_body = _render_for_guild(
                token=token,
                guild=guild,
                settings=settings,
                ticket_settings=ticket_settings,
                economy_config=economy_config,
                config=config,
                message=str(exc) or "Setup failed.",
                success=False,
            )
            return web.Response(
                status=400,
                text=html_body,
                content_type="text/html",
                charset="utf-8",
            )

    app.router.add_get("/frontend.css", handle_frontend_css)
    app.router.add_get("/main.css", handle_frontend_css)
    app.router.add_get("/setup/{token}", handle_setup_page)
    app.router.add_post("/setup/{token}", handle_setup_submit)
