from __future__ import annotations

import asyncio
import colorsys
import io
import logging
from pathlib import Path
from typing import Optional, Any

import aiohttp
import discord
from PIL import Image


logger = logging.getLogger("ModBot.GuildBranding")

_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_TICKET_ICON_SVG_PATHS = {
    "general": _ASSETS_DIR / "ticket_icon_support.svg",
    "report": _ASSETS_DIR / "ticket_icon_report.svg",
    "appeal": _ASSETS_DIR / "ticket_icon_appeal.svg",
    "other": _ASSETS_DIR / "ticket_icon_other.svg",
}
_TICKET_ICON_EMOJI_NAMES = {
    "general": "mod_ticket_support",
    "report": "mod_ticket_report",
    "appeal": "mod_ticket_appeal",
    "other": "mod_ticket_other",
}
_BANNER_COLOR_CACHE: dict[str, str] = {}
_TICKET_EMOJI_CACHE: dict[tuple[int, str, str], discord.PartialEmoji | str] = {}
_RENDER_CACHE: dict[tuple[str, str], bytes] = {}
_LOCKS: dict[tuple[Any, ...], asyncio.Lock] = {}
_LOCKS_GUARD = asyncio.Lock()


async def _get_lock(key: tuple[Any, ...]) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is not None:
        return lock
    async with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock


def get_guild_brand_assets(
    guild: Optional[discord.Guild],
) -> tuple[Optional[str], Optional[str]]:
    """Return the guild icon/banner URLs without config fallbacks."""
    logo_url: Optional[str] = None
    banner_url: Optional[str] = None

    if guild and getattr(guild, "icon", None):
        try:
            logo_url = str(guild.icon.url)
        except Exception:
            logo_url = None

    if guild and getattr(guild, "banner", None):
        try:
            banner_url = str(guild.banner.url)
        except Exception:
            banner_url = None

    return logo_url, banner_url


def _get_guild_banner_url(guild: Optional[discord.Guild]) -> Optional[str]:
    if guild and getattr(guild, "banner", None):
        try:
            asset = guild.banner
            return str(asset.replace(size=1024).url)
        except Exception:
            try:
                return str(guild.banner.url)
            except Exception:
                return None
    return None


def _normalize_emoji_lookup_name(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def _emoji_lookup_keys(value: str) -> tuple[str, ...]:
    normalized = _normalize_emoji_lookup_name(value)
    if not normalized:
        return ()

    keys = [normalized]
    compact = normalized.replace("_", "")
    dashed = normalized.replace("_", "-")

    if compact and compact not in keys:
        keys.append(compact)
    if dashed and dashed not in keys:
        keys.append(dashed)

    return tuple(keys)


def resolve_guild_component_emoji(
    guild: Optional[discord.Guild],
    *names: str,
    fallback: Optional[str] = None,
) -> discord.PartialEmoji | str | None:
    """
    Resolve a custom guild emoji for buttons/selects, with unicode fallback.

    The lookup is best-effort and only uses the guild's own emoji inventory.
    """
    if guild:
        lookup: dict[str, discord.PartialEmoji] = {}
        for emoji in getattr(guild, "emojis", []):
            emoji_name = getattr(emoji, "name", None)
            if not emoji_name:
                continue
            partial = discord.PartialEmoji(
                name=emoji.name,
                id=emoji.id,
                animated=emoji.animated,
            )
            for key in _emoji_lookup_keys(emoji_name):
                lookup.setdefault(key, partial)

        for name in names:
            for key in _emoji_lookup_keys(name):
                resolved = lookup.get(key)
                if resolved is not None:
                    return resolved

    fallback_value = str(fallback or "").strip()
    return fallback_value or None


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"#{r:02X}{g:02X}{b:02X}"


def _hex_suffix(color_hex: str) -> str:
    return str(color_hex or "").strip().lower().replace("#", "")[:6]


def _adjust_brand_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = rgb
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)

    if s < 0.12:
        s = 0.0
        l = min(0.78, max(l, 0.52))
    else:
        s = min(1.0, max(s, 0.42))
        l = min(0.72, max(l, 0.46))

    nr, ng, nb = colorsys.hls_to_rgb(h, l, s)
    return (
        max(0, min(255, int(round(nr * 255)))),
        max(0, min(255, int(round(ng * 255)))),
        max(0, min(255, int(round(nb * 255)))),
    )


def _extract_dominant_color(image_bytes: bytes) -> Optional[str]:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width > 8 and height > 8:
                left = int(width * 0.12)
                top = int(height * 0.12)
                right = max(left + 1, int(width * 0.88))
                bottom = max(top + 1, int(height * 0.88))
                rgb = rgb.crop((left, top, right, bottom))

            rgb.thumbnail((72, 72), Image.Resampling.LANCZOS)
            quantized = rgb.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
            palette = quantized.getpalette() or []
            colors = quantized.getcolors() or []
            if not colors or not palette:
                return None

            best_rgb: Optional[tuple[int, int, int]] = None
            best_score = -1.0
            for count, index in colors:
                start = index * 3
                if start + 2 >= len(palette):
                    continue
                pr, pg, pb = palette[start:start + 3]
                _, lightness, saturation = colorsys.rgb_to_hls(pr / 255.0, pg / 255.0, pb / 255.0)
                score = float(count)
                score *= 1.0 + (saturation * 0.35)
                if 0.08 < lightness < 0.94:
                    score *= 1.08
                if score > best_score:
                    best_score = score
                    best_rgb = (pr, pg, pb)

            if best_rgb is None:
                return None
            return _rgb_to_hex(_adjust_brand_rgb(best_rgb))
    except Exception:
        logger.exception("Failed to extract dominant color from guild banner bytes")
        return None


async def get_guild_banner_color(guild: Optional[discord.Guild]) -> Optional[str]:
    banner_url = _get_guild_banner_url(guild)
    if not banner_url:
        return None

    cached = _BANNER_COLOR_CACHE.get(banner_url)
    if cached:
        return cached

    lock = await _get_lock(("banner-color", banner_url))
    async with lock:
        cached = _BANNER_COLOR_CACHE.get(banner_url)
        if cached:
            return cached

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(banner_url) as response:
                    if response.status != 200:
                        return None
                    image_bytes = await response.read()
        except Exception:
            logger.exception("Failed to fetch guild banner for color sampling")
            return None

        color_hex = await asyncio.to_thread(_extract_dominant_color, image_bytes)
        if color_hex:
            _BANNER_COLOR_CACHE[banner_url] = color_hex
        return color_hex


def _load_ticket_icon_svg(kind: str) -> Optional[str]:
    path = _TICKET_ICON_SVG_PATHS.get(kind)
    if path is None or not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read ticket icon SVG template for %s", kind)
        return None


def _tint_svg(svg_markup: str, color_hex: str) -> str:
    return (
        str(svg_markup or "")
        .replace('fill="black"', f'fill="{color_hex}"')
        .replace("fill='black'", f"fill='{color_hex}'")
        .replace('stroke="black"', f'stroke="{color_hex}"')
        .replace("stroke='black'", f"stroke='{color_hex}'")
    )


def _render_svg_to_png_bytes_sync(svg_markup: str, *, size: int = 96) -> Optional[bytes]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    html = (
        "<!doctype html><html><body style='margin:0;padding:0;background:transparent;"
        "display:flex;align-items:center;justify-content:center;width:100vw;height:100vh;'>"
        f"{svg_markup}</body></html>"
    )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(120)
            png_bytes = page.screenshot(omit_background=True, type="png")
            browser.close()
            return png_bytes
    except Exception:
        logger.exception("Failed to render tinted ticket SVG")
        return None


async def _render_ticket_icon_bytes(kind: str, color_hex: str) -> Optional[bytes]:
    cache_key = (kind, color_hex)
    cached = _RENDER_CACHE.get(cache_key)
    if cached:
        return cached

    svg_markup = _load_ticket_icon_svg(kind)
    if not svg_markup:
        return None

    tinted_markup = _tint_svg(svg_markup, color_hex)
    png_bytes = await asyncio.to_thread(_render_svg_to_png_bytes_sync, tinted_markup)
    if png_bytes and len(png_bytes) <= 256 * 1024:
        _RENDER_CACHE[cache_key] = png_bytes
        return png_bytes
    return None


def _can_manage_expressions(member: Optional[discord.Member]) -> bool:
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(
        getattr(perms, "manage_expressions", False)
        or getattr(perms, "manage_emojis_and_stickers", False)
        or getattr(perms, "manage_emojis", False)
    )


def _to_partial_emoji(emoji: discord.Emoji) -> discord.PartialEmoji:
    return discord.PartialEmoji(name=emoji.name, id=emoji.id, animated=emoji.animated)


async def _ensure_ticket_panel_emoji(
    guild: discord.Guild,
    *,
    kind: str,
    color_hex: str,
    fallback: discord.PartialEmoji | str | None,
) -> discord.PartialEmoji | str | None:
    base_name = _TICKET_ICON_EMOJI_NAMES.get(kind)
    if not base_name:
        return fallback

    expected_name = f"{base_name}_{_hex_suffix(color_hex)}"
    cache_key = (guild.id, kind, expected_name)
    cached = _TICKET_EMOJI_CACHE.get(cache_key)
    if cached:
        return cached

    lock = await _get_lock(("ticket-emoji", guild.id, kind))
    async with lock:
        cached = _TICKET_EMOJI_CACHE.get(cache_key)
        if cached:
            return cached

        existing = list(guild.emojis)
        selected = discord.utils.get(existing, name=expected_name)
        if selected is not None:
            resolved = _to_partial_emoji(selected)
            _TICKET_EMOJI_CACHE[cache_key] = resolved
            return resolved

        matching_prefix = [emoji for emoji in existing if emoji.name.startswith(f"{base_name}_")]
        bot_member = guild.me
        if bot_member is None:
            state = getattr(guild, "_state", None)
            bot_id = getattr(getattr(state, "user", None), "id", None)
            if bot_id is not None:
                bot_member = guild.get_member(bot_id)

        if not _can_manage_expressions(bot_member):
            if matching_prefix:
                resolved = _to_partial_emoji(matching_prefix[0])
                _TICKET_EMOJI_CACHE[cache_key] = resolved
                return resolved
            return fallback

        payload = await _render_ticket_icon_bytes(kind, color_hex)
        if not payload:
            return fallback

        reason = f"Auto-create {kind} ticket icon to match guild banner color"
        for emoji in matching_prefix:
            try:
                await emoji.delete(reason=reason)
            except Exception:
                pass

        try:
            created = await guild.create_custom_emoji(
                name=expected_name,
                image=payload,
                reason=reason,
            )
        except Exception:
            logger.exception("Failed to create tinted ticket emoji for guild %s", guild.id)
            return fallback

        resolved = _to_partial_emoji(created)
        _TICKET_EMOJI_CACHE[cache_key] = resolved
        return resolved


async def get_ticket_panel_component_emojis(
    guild: Optional[discord.Guild],
) -> dict[str, discord.PartialEmoji | str | None]:
    emoji_map = {
        "general": resolve_guild_component_emoji(
            guild,
            "ticket_support",
            "support",
            "ticket",
            "help",
            fallback="🛠️",
        ),
        "report": resolve_guild_component_emoji(
            guild,
            "ticket_report",
            "report",
            "alert",
            "siren",
            fallback="🚨",
        ),
        "appeal": resolve_guild_component_emoji(
            guild,
            "ticket_appeal",
            "appeal",
            "note",
            "pencil",
            fallback="📝",
        ),
        "other": resolve_guild_component_emoji(
            guild,
            "ticket_other",
            "other",
            "chat",
            "message",
            fallback="💬",
        ),
    }

    if guild is None:
        return emoji_map

    color_hex = await get_guild_banner_color(guild)
    if not color_hex:
        return emoji_map

    for kind, fallback in list(emoji_map.items()):
        tinted = await _ensure_ticket_panel_emoji(
            guild,
            kind=kind,
            color_hex=color_hex,
            fallback=fallback,
        )
        if tinted is not None:
            emoji_map[kind] = tinted

    return emoji_map
